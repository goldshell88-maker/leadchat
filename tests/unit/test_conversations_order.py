"""Порядок списка диалогов — три ключа и постраничная выдача (разбор от 12 августа).

ЧТО РАЗБОР УВИДЕЛ НА ПРОДЕ. Список стоял в порядке, который со стороны читался
как случайный: у ручки нет ни одного параметра сортировки (проверено четырьмя
вариантами — `direction=desc`, `sort=last_message_at&direction=desc`,
`order=desc`, `sort=-last_message_at`: все 200, порядок не меняется), а
собственный её порядок не совпадал ни со свежестью, ни с чем-либо ещё, что
видно в выдаче.

ПОЧЕМУ ТАК БЫЛО. Первыми ключами сортировки стояли `conversations.unread_count`
и производные от него. Это ГЛОБАЛЬНАЯ колонка, оставшаяся с тех пор, когда
непрочитанное считалось у диалога, а не у человека. Сегодня в ответ уезжает
ПЕР-ЮЗЕРНЫЙ счётчик: `read_markers.apply_unread_counts` переписывает
`unread_count` поверх сериализованной строки по маркерам чтения из Redis.
То есть сортировали по одному числу, а показывали другое — на боевом стенде
диалог с `unread_count: 0` в ответе стоял выше более свежего именно потому,
что в колонке у него единица, оставшаяся от чужого прочтения.

Пер-юзерное число ключом сортировки быть не может в принципе: оно считается
после выборки, уже поверх страницы, а ORDER BY обязан отработать ДО LIMIT.
Поэтому порядок собран из того, что запрос видит и что выдача показывает:
закреплённые выше → последнее сообщение убывающе → id.

Здесь же держится главное свойство порядка — ПОЛНОТА: без последнего ключа
строки с одинаковым `last_message_at` (служебные события Авито приходят
пачками с одинаковой секундой) вставали бы между запросами по-разному, и
вторая страница теряла бы одни диалоги и дублировала другие.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Client, Conversation
from app.services import pins, read_markers

pytestmark = pytest.mark.anyio

T0 = datetime(2026, 8, 12, 9, 0, 0, tzinfo=UTC)


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


def ids(payload):
    return [item["id"] for item in payload["items"]]


async def make_conv(
    db_sessionmaker,
    account,
    tag: str,
    *,
    last_at: datetime | None,
    unread: int = 0,
    status: str = "new",
    assignee=None,
    conv_id: uuid.UUID | None = None,
) -> uuid.UUID:
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"ord-{tag}", name=f"Клиент {tag}")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id=f"ord-chat-{tag}",
            account_id=account.id,
            client_id=cl.id,
            status=status,
            unread_count=unread,
            tags=[],
            last_message_at=last_at,
            assignee_id=assignee.id if assignee else None,
        )
        if conv_id is not None:
            row.id = conv_id
        s.add(row)
        await s.commit()
        return row.id


async def test_order_is_last_message_desc_not_the_invisible_unread_column(
    client, tokens, users_by_role, db_sessionmaker, make_avito_account, redis
):
    """ВОСПРОИЗВЕДЕНИЕ БОЕВОГО СИМПТОМА: старый диалог стоял выше свежего.

    Поднимала его колонка `conversations.unread_count`, которой в выдаче нет:
    оба диалога приходят с `unread_count: 0` (у смотрящего стоит маркер
    прочтения), а порядок собирался по числу, которого он не видит.
    """
    account = await make_avito_account()
    manager = users_by_role["manager"]

    stale = await make_conv(
        db_sessionmaker, account, "stale", last_at=T0 - timedelta(hours=2), unread=3
    )
    fresh = await make_conv(db_sessionmaker, account, "fresh", last_at=T0, unread=0)

    # Смотрящий прочитал оба: пер-юзерный счётчик у обоих ноль, и глобальная
    # тройка у «старого» — след чужого непрочитанного, а не его собственного.
    for conv_id in (stale, fresh):
        await read_markers.set_marker(redis, manager.id, conv_id, T0 + timedelta(minutes=1))

    r = await client.get("/api/v1/conversations", headers=auth(tokens))
    assert r.status_code == 200, r.text
    payload = r.json()

    assert [i["unread_count"] for i in payload["items"]] == [0, 0], (
        "оба диалога смотрящий прочитал — иначе тест проверяет не то"
    )
    assert ids(payload) == [str(fresh), str(stale)], (
        "порядок обязан идти по тому, что видно в выдаче (последнее сообщение), "
        f"а не по невидимой колонке unread_count; получили {ids(payload)}"
    )


async def test_pinned_first_then_last_message_desc(
    client, tokens, users_by_role, db_sessionmaker, make_avito_account
):
    """Первый ключ — закрепление, и оно личное."""
    account = await make_avito_account()
    manager = users_by_role["manager"]

    old_mine = await make_conv(
        db_sessionmaker,
        account,
        "old",
        last_at=T0 - timedelta(days=1),
        status="in_progress",
        assignee=manager,
    )
    newest = await make_conv(db_sessionmaker, account, "newest", last_at=T0)
    middle = await make_conv(db_sessionmaker, account, "middle", last_at=T0 - timedelta(hours=1))

    async with db_sessionmaker() as s:
        await pins.pin(s, await s.get(Conversation, old_mine), manager)
        await s.commit()

    r = await client.get("/api/v1/conversations", headers=auth(tokens))
    assert ids(r.json()) == [str(old_mine), str(newest), str(middle)]

    # У другого человека тот же список без закрепления — закреп ЛИЧНЫЙ.
    r2 = await client.get("/api/v1/conversations", headers=auth(tokens, "admin"))
    assert ids(r2.json()) == [str(newest), str(middle), str(old_mine)]


async def test_rows_without_last_message_go_last(
    client, tokens, db_sessionmaker, make_avito_account
):
    """Диалог без единого сообщения не может стоять выше живой переписки."""
    account = await make_avito_account()
    silent = await make_conv(db_sessionmaker, account, "silent", last_at=None)
    talking = await make_conv(db_sessionmaker, account, "talking", last_at=T0 - timedelta(days=30))

    r = await client.get("/api/v1/conversations", headers=auth(tokens))
    assert ids(r.json()) == [str(talking), str(silent)]


async def test_same_second_rows_are_ordered_by_id_and_do_not_shuffle(
    client, tokens, db_sessionmaker, make_avito_account
):
    """Тайбрейкер: пачка служебных событий Авито с одинаковой секундой.

    Без последнего ключа порядок внутри такой пачки задаёт база, и он вправе
    меняться от запроса к запросу.
    """
    account = await make_avito_account()
    made = [
        await make_conv(db_sessionmaker, account, f"tie-{n}", last_at=T0, conv_id=uuid.UUID(int=n))
        for n in (7, 3, 9, 1, 5)
    ]

    first = ids((await client.get("/api/v1/conversations", headers=auth(tokens))).json())
    assert first == [str(c) for c in sorted(made)], (
        f"внутри одинаковой секунды порядок задаёт id; получили {first}"
    )

    for _ in range(3):
        again = ids((await client.get("/api/v1/conversations", headers=auth(tokens))).json())
        assert again == first, f"порядок дрожит между запросами: {first} → {again}"


async def test_pagination_over_equal_timestamps_loses_nothing(
    client, tokens, db_sessionmaker, make_avito_account
):
    """Тот же порядок в постраничной выдаче: без дублей и пропусков.

    Проверяется на самом злом входе — у ВСЕХ строк одинаковый
    `last_message_at`. Именно так приходят пачки служебных событий Авито, и
    именно на них порядок без полного ключа разъезжается между страницами.
    """
    account = await make_avito_account()
    total = 7
    made = {
        await make_conv(
            db_sessionmaker, account, f"pg-{n}", last_at=T0, conv_id=uuid.UUID(int=100 + n)
        )
        for n in range(total)
    }

    seen: list[str] = []
    for offset in range(0, total, 2):
        r = await client.get(f"/api/v1/conversations?limit=2&offset={offset}", headers=auth(tokens))
        assert r.status_code == 200, r.text
        page = r.json()
        assert page["page"]["total"] == total
        seen.extend(ids(page))

    assert len(seen) == len(set(seen)), f"дубли между страницами: {seen}"
    assert set(seen) == {str(c) for c in made}, "часть диалогов не попала ни на одну страницу"
    assert seen == sorted(seen), "страницы обязаны склеиваться в один и тот же порядок"


async def test_sort_parameters_are_not_accepted_and_do_not_change_anything(
    client, tokens, db_sessionmaker, make_avito_account
):
    """Порядок ФИКСИРОВАННЫЙ — и это ответ на «параметров сортировки нет».

    Разбор проверил четыре написания и все получил с кодом 200 и тем же
    порядком. Так и задумано: список читают тринадцать человек, и порядок в
    нём — часть работы, а не настройка. Тест закрепляет договор, чтобы
    «молчаливое согласие» ручки на неизвестный параметр никого больше не
    вводило в заблуждение.
    """
    account = await make_avito_account()
    newest = await make_conv(db_sessionmaker, account, "s-new", last_at=T0)
    oldest = await make_conv(db_sessionmaker, account, "s-old", last_at=T0 - timedelta(days=2))
    expected = [str(newest), str(oldest)]

    for query in (
        "",
        "&direction=desc",
        "&direction=asc",
        "&sort=last_message_at&direction=asc",
        "&order=asc",
        "&sort=-last_message_at",
    ):
        r = await client.get(f"/api/v1/conversations?tab=all{query}", headers=auth(tokens))
        assert r.status_code == 200, (query, r.text)
        assert ids(r.json()) == expected, f"параметр «{query}» изменил порядок"


# ===================================== раздельная выборка закреплённых (03.09)


async def test_pinned_pagination_has_no_gaps_or_duplicates(
    client, tokens, users_by_role, db_sessionmaker, make_avito_account
):
    """⚠ ГЛАВНАЯ ПРОВЕРКА ПРАВКИ 03.09: ошибка в смещении = потерянный диалог.

    Закреплённые перестали быть ключом сортировки (`sa.case` в ORDER BY выбивал
    индексный порядок: замер на бою 50,1 -> 0,27 мс на выборке строк) и берутся
    отдельной выборкой. Цена ошибки здесь — не медленный экран, а диалог,
    которого нет НИ НА ОДНОЙ странице.

    Тест листает список страницами по два и складывает их обратно: сумма
    обязана совпасть с полной выдачей строка в строку.
    """
    account = await make_avito_account()
    manager = users_by_role["manager"]

    порядок = []
    for i in range(7):
        порядок.append(
            await make_conv(
                db_sessionmaker,
                account,
                f"p{i}",
                last_at=T0 - timedelta(hours=i),
                status="in_progress",
                assignee=manager,
            )
        )

    # Закрепляем два диалога ИЗ СЕРЕДИНЫ — иначе стык страниц не проверяется.
    async with db_sessionmaker() as s:
        for индекс in (2, 5):
            await pins.pin(s, await s.get(Conversation, порядок[индекс]), manager)
        await s.commit()

    целиком = await client.get("/api/v1/conversations?limit=50", headers=auth(tokens))
    ожидаемо = ids(целиком.json())
    assert len(ожидаемо) == 7
    assert ожидаемо[:2] == [str(порядок[2]), str(порядок[5])], (
        "закреплённые обязаны стоять первыми и между собой — по свежести"
    )

    собрано: list[str] = []
    for offset in range(0, 8, 2):
        стр = await client.get(
            f"/api/v1/conversations?limit=2&offset={offset}", headers=auth(tokens)
        )
        собрано += ids(стр.json())

    assert собрано == ожидаемо, (
        "постраничная выдача разошлась с полной: на стыке появился дубль или "
        f"пропал диалог. Постранично: {собрано}; целиком: {ожидаемо}"
    )


async def test_pinned_filtered_out_does_not_shift_the_tail(
    client, tokens, users_by_role, db_sessionmaker, make_avito_account
):
    """⚠ СМЕЩЕНИЕ СЧИТАЕТСЯ ПО ПРОШЕДШИМ ФИЛЬТР, А НЕ ПО ЧИСЛУ ЗАКРЕПЛЕНИЙ.

    Закреплённый диалог может не подойти под вкладку, поиск, «ждут ответа»,
    канал или тег. Тогда он в списке не стоит и позиций не занимает. Возьми
    смещение хвоста по длине списка закреплений — и ровно столько диалогов
    пропадёт со стыка страниц.

    Здесь закреплён ЗАКРЫТЫЙ диалог, а список запрошен без закрытых.
    """
    account = await make_avito_account()
    manager = users_by_role["manager"]

    закрытый = await make_conv(
        db_sessionmaker, account, "closed-pin", last_at=T0, status="closed", assignee=manager
    )
    живые = [
        await make_conv(
            db_sessionmaker,
            account,
            f"a{i}",
            last_at=T0 - timedelta(hours=i + 1),
            status="in_progress",
            assignee=manager,
        )
        for i in range(3)
    ]

    async with db_sessionmaker() as s:
        await pins.pin(s, await s.get(Conversation, закрытый), manager)
        await s.commit()

    целиком = ids(
        (await client.get("/api/v1/conversations?tab=mine&limit=50", headers=auth(tokens))).json()
    )
    assert целиком == [str(c) for c in живые], "закрытый закреплённый не должен попасть в «Мои»"

    собрано: list[str] = []
    for offset in (0, 2):
        собрано += ids(
            (
                await client.get(
                    f"/api/v1/conversations?tab=mine&limit=2&offset={offset}",
                    headers=auth(tokens),
                )
            ).json()
        )
    assert собрано == целиком, (
        "хвост сместился на отфильтрованное закрепление — диалог пропал со стыка"
    )
