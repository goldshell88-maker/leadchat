"""Телефон, распознанный в тексте входящего: запись, предложение, поиск.

ЧТО ЗДЕСЬ ОХРАНЯЕТСЯ. Разбор строки проверяется отдельно и без базы
(`tests/unit/test_phone_parse.py`); здесь — ПРАВИЛА ВЛАДЕЛЬЦА о том, что с
распознанным номером можно делать, а чего нельзя ни при каких настройках:

* по умолчанию номер в карточку НЕ пишется — он предлагается оператору;
* уже известный телефон не перезаписывается никогда;
* автообъединения карточек по распознанному номеру не бывает;
* отклонённое оператором не возвращается предложением;
* диалог находится поиском и по «1112240», и по «+7 900 111-22-40» — то есть по
  номеру, которого в карточке может и не быть.

Каждое из этих правил однажды нарушалось в этой системе в том или ином виде, и
каждое стоило либо чужого телефона в чужой карточке, либо потерянного клиента.
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Client, ClientPhoneCandidate, Conversation, Message
from app.services import app_settings
from app.services import clients as clients_svc
from app.services.inbound import apply_inbound_event

try:  # настоящее событие адаптера, когда зона OAuth на месте
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

AVITO_USER_ID = 111222333
T0 = datetime(2026, 8, 12, 16, 35, 0, tzinfo=UTC)

#: Живое входящее с боевой системы (диалог Анны Сергеевны).
АННА = (
    "В любое время в течении дня. Прошу сообщить о времени прихода за 1 час "
    "в СМС по номеру : +7(900)1112240."
)
НОМЕР = "+79001112240"

#: Строки из того же диалога, которые номером НЕ являются.
НЕ_НОМЕРА = [
    "Код входной двери в дом: 37ключ2580",
    "Телевизор Samsung QLED Q70A 55cm",
    "Заречное шоссе дом 7 кв.84 (12 этаж)",
]


def make_event(**kw) -> InboundEvent:
    defaults = {
        "external_chat_id": "chat-phone",
        "external_message_id": "am-phone-1",
        "author_id": 999001,
        "account_user_id": AVITO_USER_ID,
        "text": АННА,
        "created_at": T0,
        "client_name": "Анна Сергеевна",
        "item_title": "Ремонт стиральной машины",
        "item_url": "https://avito.ru/item/1",
        "item_price": "от 1500 ₽",
    }
    defaults.update(kw)
    return InboundEvent(**defaults)


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


@pytest.fixture
def read(db_sessionmaker):
    async def _read(stmt):
        async with db_sessionmaker() as session:
            return list((await session.execute(stmt)).scalars())

    return _read


async def _autofill(db, *, enabled: bool = True, autofill: bool = False) -> None:
    """Настройки пишем в базу, а не подменяем заглушкой: путь до записи должен
    быть тем же, что в проде, включая чтение из `app_settings`."""
    await app_settings.set_many(
        db,
        {
            app_settings.PHONE_DETECT_ENABLED: enabled,
            app_settings.PHONE_DETECT_AUTOFILL: autofill,
        },
        user_id=None,
    )
    await db.commit()


# --- значения по умолчанию ----------------------------------------------------


async def test_defaults_are_recognize_but_only_suggest():
    """Умолчания — часть требования, а не деталь реализации.

    «Распознавать» включено: без этого правка не работает ни у кого, пока
    руководитель не найдёт переключатель. «Писать в карточку» выключено: тихая
    запись ЧУЖОГО номера в карточку хуже, чем несделанная работа — номер оттуда
    набирают и диктуют мастеру вслух.
    """
    assert app_settings.SPECS[app_settings.PHONE_DETECT_ENABLED].default is True
    assert app_settings.SPECS[app_settings.PHONE_DETECT_AUTOFILL].default is False


async def test_by_default_the_number_is_offered_and_not_written(db, redis, account, read):
    """Карточка пуста, номер в тексте есть — но карточку мы не трогаем."""
    await apply_inbound_event(db, redis, account, make_event())

    (client_row,) = await read(sa.select(Client))
    assert client_row.phone is None, "по умолчанию номер в карточку не пишется"

    (candidate,) = await read(sa.select(ClientPhoneCandidate))
    assert candidate.phone == НОМЕР
    assert candidate.status == "pending"
    assert candidate.raw == "+7(900)1112240", "оператору нужно исходное написание"
    assert candidate.resolved_by_id is None

    # Метрика «собрано телефонов» не должна расти от предложений: собранным
    # телефон становится, когда он в карточке.
    assert (await read(sa.select(AuditLog.action))).count("client.phone_captured") == 0


async def test_the_offer_remembers_which_message_it_came_from(db, redis, account, read):
    """Идентификатор сообщения и время распознавания — требование владельца.

    Без них предложение нечем проверить: оператор увидит цепочку цифр и не
    сможет открыть то место в переписке, откуда она взялась. Непроверяемое
    предложение либо принимают не глядя, либо перестают замечать.
    """
    await apply_inbound_event(db, redis, account, make_event())
    (msg,) = await read(sa.select(Message))
    (conv,) = await read(sa.select(Conversation))
    (candidate,) = await read(sa.select(ClientPhoneCandidate))

    assert candidate.message_id == msg.id
    assert candidate.conversation_id == conv.id
    assert candidate.message_at is not None
    assert candidate.detected_at is not None
    assert candidate.source == "inbound"


# --- «писать в карточку сразу» ------------------------------------------------


async def test_with_autofill_an_empty_card_is_filled_from_the_dialog(
    db, redis, account, read, db_sessionmaker
):
    """Руководитель включил запись — пустая карточка заполняется с источником."""
    await _autofill(db, autofill=True)
    await apply_inbound_event(db, redis, account, make_event())

    (client_row,) = await read(sa.select(Client))
    assert client_row.phone == НОМЕР
    assert client_row.phone_account_id == account.id
    assert client_row.phone_set_at is None, "руками номер не вводили"

    rows = await read(sa.select(AuditLog).where(AuditLog.action == "client.phone_captured"))
    assert len(rows) == 1
    # `regex` — колонка «из них автоизвлечением» в отчёте «собрано телефонов»
    # (06 §1.4). Переименовать её здесь значило бы обнулить столбец задним числом.
    assert rows[0].details["source"] == "regex"

    async with db_sessionmaker() as session:
        view = await clients_svc.identity_view(session, await session.get(Client, client_row.id))
    assert view["phone_source"] == "dialog", "карточка обязана честно сказать, откуда номер"
    assert view["phone_candidates"] == [], "спрашивать не о чем — номер уже в карточке"


async def test_a_known_phone_is_never_overwritten_the_second_becomes_extra(
    db, redis, account, read, db_sessionmaker
):
    """Второй номер не перезаписывает первый — он становится ДОПОЛНИТЕЛЬНЫМ сам.

    Правило 12.09 (вместо «кандидата» 12.08): при включённой записи всё, что
    клиент написал, — его номера, и они попадают в карточку без вопроса. А
    заполненный основной автоматика не меняет никогда: какой из двух главный,
    решает одним нажатием тот, кто с человеком разговаривал.
    """
    await _autofill(db, autofill=True)
    await apply_inbound_event(db, redis, account, make_event(text="мой 8 900 111 22 51"))
    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(external_message_id="am-phone-2", created_at=T0 + timedelta(minutes=2)),
    )

    (client_row,) = await read(sa.select(Client))
    assert client_row.phone == "+79001112251", "первый номер обязан остаться"

    rows = await read(sa.select(ClientPhoneCandidate).order_by(ClientPhoneCandidate.detected_at))
    assert [(r.phone, r.status, r.resolved_by_id) for r in rows] == [
        ("+79001112251", "accepted", None),
        (НОМЕР, "accepted", None),
    ]

    async with db_sessionmaker() as session:
        view = await clients_svc.identity_view(session, await session.get(Client, client_row.id))
    assert view["phone_candidates"] == [], "спрашивать не о чем — номер уже в карточке"
    extra = [p for p in view["phones"] if not p["primary"]]
    assert [(p["value"], p["decided_by"], p["source"]) for p in extra] == [
        (НОМЕР, "auto", "dialog")
    ]


async def test_without_autofill_the_second_number_is_only_offered(db, redis, account, read):
    """Запись выключена — второй номер по-прежнему ложится вопросом оператору."""
    await _autofill(db, autofill=False)
    await apply_inbound_event(db, redis, account, make_event(text="мой 8 900 111 22 51"))
    rows = await read(sa.select(ClientPhoneCandidate))
    assert [(r.phone, r.status) for r in rows] == [("+79001112251", "pending")]


# --- выключатель --------------------------------------------------------------


async def test_the_switch_stops_everything(db, redis, account, read):
    """Выключено — значит ни записи, ни предложения, ни строки в базе."""
    await _autofill(db, enabled=False, autofill=True)
    await apply_inbound_event(db, redis, account, make_event())

    (client_row,) = await read(sa.select(Client))
    assert client_row.phone is None
    assert await read(sa.select(ClientPhoneCandidate)) == []


# --- защита от ложных ---------------------------------------------------------


@pytest.mark.parametrize("text", НЕ_НОМЕРА)
async def test_lines_that_are_not_phones_leave_no_trace(db, redis, account, read, text):
    """Критерии приёмки владельца, проверенные СКВОЗЬ настоящий приём.

    Отдельный тест разбора это уже проверяет, но проверяет строку. Здесь
    проверяется система: карточка чиста и предложений нет — то есть оператор не
    увидит «распознан телефон 37ключ2580» ни при каких настройках.
    """
    await _autofill(db, autofill=True)
    await apply_inbound_event(db, redis, account, make_event(text=text))

    (client_row,) = await read(sa.select(Client))
    assert client_row.phone is None
    assert await read(sa.select(ClientPhoneCandidate)) == []


async def test_an_avito_service_message_with_a_number_is_ignored(db, redis, account, read):
    """Служебная запись Авито — не переписка, и телефон из неё не наш клиент.

    Такое событие вообще не доходит до разбора телефона: у него своя ветка.
    Проверяем это снаружи, потому что «не доходит» держится на порядке двух
    строк в `apply_inbound_event`, а порядок строк ломается легче всего.
    """
    await _autofill(db, autofill=True)
    # Диалог должен существовать: служебное событие в неизвестный чат
    # отбрасывается раньше по другой причине.
    await apply_inbound_event(db, redis, account, make_event(text="здравствуйте"))
    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(
            external_message_id="am-sys-1",
            created_at=T0 + timedelta(minutes=1),
            text="Наш телефон поддержки 8 900 111 22 40",
            is_system=True,
        ),
    )

    (client_row,) = await read(sa.select(Client))
    assert client_row.phone is None
    assert await read(sa.select(ClientPhoneCandidate)) == []


async def test_the_same_number_twice_does_not_double_the_offer(db, redis, account, read):
    """Клиент повторил номер — второго предложения быть не должно."""
    await apply_inbound_event(db, redis, account, make_event())
    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(
            external_message_id="am-phone-2",
            created_at=T0 + timedelta(minutes=5),
            text="повторяю: 8 900 111-22-40",
        ),
    )
    assert len(await read(sa.select(ClientPhoneCandidate))) == 1


# --- решение оператора --------------------------------------------------------


async def test_rejected_number_never_comes_back(db, redis, account, read, users_by_role):
    """Отклонённое не предлагается заново — иначе подсказки перестают читать."""
    await apply_inbound_event(db, redis, account, make_event())
    (candidate,) = await read(sa.select(ClientPhoneCandidate))

    async with db.begin():
        row = await db.get(ClientPhoneCandidate, candidate.id)
        assert row is not None
        await clients_svc.resolve_phone_candidate(
            db, row, decision="reject", actor=users_by_role["manager"]
        )

    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(
            external_message_id="am-phone-3",
            created_at=T0 + timedelta(minutes=9),
            text="ещё раз: +7 900 111 22 40",
        ),
    )
    rows = await read(sa.select(ClientPhoneCandidate))
    assert [(r.phone, r.status) for r in rows] == [(НОМЕР, "rejected")]

    (client_row,) = await read(sa.select(Client))
    assert client_row.phone is None
    actions = await read(sa.select(AuditLog.action))
    assert actions.count("client.phone_candidate_rejected") == 1


async def test_autofill_does_not_argue_with_a_rejection(db, redis, account, read, users_by_role):
    """Включённая автозапись не имеет права переиграть отказ оператора.

    ЛОВУШКА, КОТОРУЮ ЭТО ЗАКРЫВАЕТ. Автозапись смотрит на пустое поле
    `clients.phone`, а память об отказе лежит в другом месте. Клиент повторяет
    свой «код домофона» в следующем сообщении — и отклонённое молча оказывается
    в карточке, откуда его наберут и позвонят постороннему человеку.
    """
    await apply_inbound_event(db, redis, account, make_event())
    (candidate,) = await read(sa.select(ClientPhoneCandidate))
    async with db.begin():
        row = await db.get(ClientPhoneCandidate, candidate.id)
        assert row is not None
        await clients_svc.resolve_phone_candidate(
            db, row, decision="reject", actor=users_by_role["manager"]
        )

    await _autofill(db, autofill=True)
    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(
            external_message_id="am-phone-4",
            created_at=T0 + timedelta(minutes=20),
            text="повторю: +7 900 111-22-40",
        ),
    )
    (client_row,) = await read(sa.select(Client))
    assert client_row.phone is None, "отклонённый номер не имеет права попасть в карточку"
    rows = await read(sa.select(ClientPhoneCandidate))
    assert [(r.phone, r.status) for r in rows] == [(НОМЕР, "rejected")]


async def test_replace_puts_the_number_into_the_card_without_calling_it_manual(
    db, redis, account, read, users_by_role, db_sessionmaker
):
    """«Заменить» — номер в карточке, но подпись честная: он из диалога.

    Поставить сюда `phone_set_by_id`/`phone_set_at` (как у ручного ввода) значило
    бы подписать карточку словами «введено вручную» под цифрами, которые человек
    не набирал.
    """
    await apply_inbound_event(db, redis, account, make_event())
    (candidate,) = await read(sa.select(ClientPhoneCandidate))

    async with db.begin():
        row = await db.get(ClientPhoneCandidate, candidate.id)
        assert row is not None
        await clients_svc.resolve_phone_candidate(
            db, row, decision="replace", actor=users_by_role["manager"]
        )

    (client_row,) = await read(sa.select(Client))
    assert client_row.phone == НОМЕР
    assert client_row.phone_set_at is None
    assert client_row.phone_account_id == account.id

    async with db_sessionmaker() as session:
        view = await clients_svc.identity_view(session, await session.get(Client, client_row.id))
    assert view["phone_source"] == "dialog"
    assert view["phone_manual"] is False


async def test_add_keeps_the_primary_number_and_shows_the_second(
    db, redis, account, read, users_by_role, db_sessionmaker
):
    """«Добавить» — второй телефон человека, а не замена основному.

    Запись включается ПОСЛЕ первого номера: при включённой записи второй номер
    лёг бы дополнительным сам (правило 12.09), и кнопке было бы нечего решать.
    """
    await _autofill(db, autofill=True)
    await apply_inbound_event(db, redis, account, make_event(text="мой 8 900 111 22 51"))
    await _autofill(db, autofill=False)
    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(external_message_id="am-phone-2", created_at=T0 + timedelta(minutes=2)),
    )
    pending = [r for r in await read(sa.select(ClientPhoneCandidate)) if r.status == "pending"]
    async with db.begin():
        row = await db.get(ClientPhoneCandidate, pending[0].id)
        assert row is not None
        await clients_svc.resolve_phone_candidate(
            db, row, decision="add", actor=users_by_role["manager"]
        )

    (client_row,) = await read(sa.select(Client))
    assert client_row.phone == "+79001112251", "основной номер не тронут"
    async with db_sessionmaker() as session:
        view = await clients_svc.identity_view(session, await session.get(Client, client_row.id))
    assert {p["value"] for p in view["phones"]} == {"+79001112251", НОМЕР}
    assert view["phone_candidates"] == []


async def test_a_decision_cannot_be_taken_twice(db, redis, account, read, users_by_role):
    """Двойной клик по «Отклонить» не должен переписывать чужое решение."""
    from app.core.errors import ApiError

    await apply_inbound_event(db, redis, account, make_event())
    (candidate,) = await read(sa.select(ClientPhoneCandidate))
    async with db.begin():
        row = await db.get(ClientPhoneCandidate, candidate.id)
        assert row is not None
        await clients_svc.resolve_phone_candidate(
            db, row, decision="reject", actor=users_by_role["manager"]
        )
    async with db.begin():
        row = await db.get(ClientPhoneCandidate, candidate.id)
        assert row is not None
        with pytest.raises(ApiError) as exc:
            await clients_svc.resolve_phone_candidate(
                db, row, decision="replace", actor=users_by_role["manager"]
            )
    assert exc.value.code == "already_resolved"


async def test_typing_the_same_number_by_hand_closes_the_offer(
    db, redis, account, read, users_by_role
):
    """Оператор ввёл номер руками — предложение про него снимается само.

    Иначе карточка выглядела бы издевательски: номер уже в поле, а под ним
    висит вопрос «принять или отклонить» про него же.
    """
    await apply_inbound_event(db, redis, account, make_event())
    (client_row,) = await read(sa.select(Client))
    async with db.begin():
        card = await db.get(Client, client_row.id)
        assert card is not None
        await clients_svc.set_phone(
            db, card, "8 900 111-22-40", actor=users_by_role["manager"], conversation_id=None
        )
    (candidate,) = await read(sa.select(ClientPhoneCandidate))
    assert candidate.status == "accepted"
    assert candidate.resolved_by_id == users_by_role["manager"].id


# --- никакого автообъединения -------------------------------------------------


async def test_a_recognized_number_suggests_a_twin_but_never_merges(
    db, redis, account, read, db_sessionmaker
):
    """Прямое требование владельца: только подсказка, никакого объединения.

    Молчаливая склейка 11 августа собрала под одним именем восемь человек из
    разных городов. Распознанный номер — довод слабее уже подтверждённого, и
    выдавать его за подтверждение нельзя.
    """
    async with db_sessionmaker() as session:
        twin = Client(channel="avito", external_id="777042", name="Анна С.", phone=НОМЕР)
        session.add(twin)
        await session.commit()
        twin_id = twin.id

    await apply_inbound_event(db, redis, account, make_event())
    (client_row,) = [c for c in await read(sa.select(Client)) if c.id != twin_id]

    async with db_sessionmaker() as session:
        card = await session.get(Client, client_row.id)
        assert card is not None
        items = await clients_svc.merge_candidates(session, card)
        view = await clients_svc.identity_view(session, card)

    assert [(i["id"], i["reason"], i["confidence"]) for i in items] == [
        (str(twin_id), "phone_candidate", "assumed")
    ]
    # Ни одна карточка не объединена: подсказка — это подсказка.
    assert view["merged_from"] == [] and view["merged_into"] is None
    assert all(c.merged_into_id is None for c in await read(sa.select(Client)))


# --- поиск ---------------------------------------------------------------------


async def test_search_finds_the_dialog_by_any_way_of_writing_the_number(
    db, redis, account, client, tokens
):
    """Критерий приёмки: и «1112240», и «+7 900 111-22-40» находят диалог.

    Номера в карточке при этом НЕТ — он только в тексте сообщения, написанный
    третьим способом: «+7(900)1112240». Поиск по буквам запроса (ILIKE, а на
    Postgres — полнотекстовый) сравнивает написание и на первом же расхождении
    отвечает «ничего не найдено» про диалог, который лежит в базе. Именно так и
    терялась Анна Сергеевна.
    """
    await apply_inbound_event(db, redis, account, make_event())

    for запрос in ("1112240", "+7 900 111-22-40", "89001112240", "900 111 22 40"):
        r = await client.get(
            f"/api/v1/conversations?tab=any&q={запрос}",
            headers={"Authorization": f"Bearer {tokens['manager']}"},
        )
        assert r.status_code == 200, r.text
        ids = [item["id"] for item in r.json()["items"]]
        assert len(ids) == 1, f"«{запрос}» не нашёл диалог: {r.json()}"


async def test_search_by_a_short_number_does_not_drag_in_everything(
    db, redis, account, client, tokens
):
    """«iPhone 13» не должен превращаться в поиск по всем телефонам базы.

    Порог в пять цифр стоял и раньше — здесь он охраняется вместе с новой
    веткой поиска по распознанным номерам.
    """
    await apply_inbound_event(db, redis, account, make_event())
    r = await client.get(
        "/api/v1/conversations?tab=any&q=13",
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["items"] == []


# --- разовый пересчёт по истории ------------------------------------------------


async def _history(db_sessionmaker, account, *, text: str, direction: str = "in") -> uuid.UUID:
    """Диалог с одним старым сообщением — как будто он приехал до правки."""
    async with db_sessionmaker() as session:
        # Идентификатор уникален по построению: у карточек `UNIQUE(channel,
        # external_id)`, и случайное число тут однажды дало бы падение раз в
        # десять тысяч прогонов — то есть на чужой правке и без объяснения.
        client_row = Client(channel="avito", external_id=f"hist-{uuid.uuid4().hex}")
        session.add(client_row)
        await session.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client_row.id,
            status="closed",
            unread_count=0,
            last_message_at=T0,
        )
        session.add(conv)
        await session.flush()
        session.add(
            Message(
                conversation_id=conv.id,
                external_message_id=f"am-{uuid.uuid4().hex[:8]}",
                direction=direction,
                sender_type="client" if direction == "in" else "operator",
                body=text,
                attachments=[],
                delivery_status="delivered",
                created_at=T0,
            )
        )
        await session.commit()
        return client_row.id


async def test_the_history_rescan_only_proposes_and_never_writes(
    db, db_sessionmaker, account, read, capsys
):
    """Разовый пересчёт формирует СПИСОК, а не переписывает сотни карточек.

    Разом записать догадки в накопленную историю — это сотни звонков
    посторонним людям, и заметить их можно будет только по жалобам.
    """
    from app.cli import run_scan_phones

    client_id = await _history(db_sessionmaker, account, text=АННА)
    await _history(db_sessionmaker, account, text="Код входной двери в дом: 37ключ2580")

    await run_scan_phones(db, limit=100, dry_run=True)
    вывод = capsys.readouterr().out
    assert "номеров распознано: 1" in вывод
    assert "Сухой прогон" in вывод
    assert await read(sa.select(ClientPhoneCandidate)) == [], "сухой прогон ничего не пишет"

    await run_scan_phones(db, limit=100, dry_run=False)
    (candidate,) = await read(sa.select(ClientPhoneCandidate))
    assert candidate.phone == НОМЕР
    assert candidate.status == "pending"
    assert candidate.source == "rescan"

    card = (await read(sa.select(Client).where(Client.id == client_id)))[0]
    assert card.phone is None, "команда не имеет права писать номера в карточки"

    # Второй прогон ничего не задваивает — иначе карточка обросла бы
    # одинаковыми предложениями после каждого запуска.
    await run_scan_phones(db, limit=100, dry_run=False)
    assert len(await read(sa.select(ClientPhoneCandidate))) == 1


async def test_the_history_rescan_ignores_our_own_outgoing_numbers(
    db, db_sessionmaker, account, read
):
    """В исходящих стоит НАШ телефон — записать его клиенту значило бы
    позвонить самому себе."""
    from app.cli import run_scan_phones

    await _history(
        db_sessionmaker,
        account,
        text="Мастер приедет, наш номер +7 900 111 22 40",
        direction="out",
    )
    await run_scan_phones(db, limit=100, dry_run=False)
    assert await read(sa.select(ClientPhoneCandidate)) == []


# --- backfill-phones: заполнение пустых карточек по истории -------------------


async def test_backfill_fills_an_empty_card_from_the_clients_own_message(
    db, db_sessionmaker, account, read
):
    """⚠ ПРОСЬБА ВЛАДЕЛЬЦА 02.09: «добавь везде номера, где есть и не привязаны».

    Замер боя: 1813 карточек пусты, хотя клиент сам назвал номер в переписке.
    Это исторический хвост — распознавание появилось позже этих сообщений, а
    `phone_detect.autofill` в бою включён, то есть правило «пустую карточку
    заполняет распознанный номер» владелец уже выбрал. Команда лишь применяет
    его к тем сообщениям, которые пришли до его появления.
    """
    from app.cli import run_backfill_phones

    await _autofill(db, autofill=True)
    client_id = await _history(db_sessionmaker, account, text="Мой номер 8 900 111-22-40")

    await run_backfill_phones(db, limit=100, dry_run=False)

    async with db_sessionmaker() as s:
        карточка = await s.get(Client, client_id)
        assert карточка.phone == "+79001112240"
        # Диалог, которым номер доказан, — без него заявка встанет с причиной
        # «телефон не привязан к диалогу».
        assert карточка.phone_conversation_id is not None
        assert карточка.phone_account_id is not None
        # Руками не вводили: подпись «введено вручную» была бы неправдой.
        assert карточка.phone_set_at is None
        assert карточка.phone_set_by_id is None


async def test_backfill_does_nothing_in_dry_run(db, db_sessionmaker, account):
    """Сухой прогон по умолчанию — иначе первая же опечатка в команде стоит 1800 карточек."""
    from app.cli import run_backfill_phones

    await _autofill(db, autofill=True)
    client_id = await _history(db_sessionmaker, account, text="телефон 89001112240")

    await run_backfill_phones(db, limit=100, dry_run=True)

    async with db_sessionmaker() as s:
        assert (await s.get(Client, client_id)).phone is None


async def test_backfill_never_takes_a_number_from_our_own_message(db, db_sessionmaker, account):
    """⚠ В ИСХОДЯЩИХ СТОИТ НАШ ТЕЛЕФОН ИЛИ ТЕЛЕФОН МАСТЕРА.

    Замер боя 02.09: у семи карточек номер написали ТОЛЬКО мы, и у трёх наш
    номер отличается от клиентского. Записать его клиенту — значит потом
    позвонить самому себе или мастеру вместо заказчика.
    """
    from app.cli import run_backfill_phones

    await _autofill(db, autofill=True)
    client_id = await _history(
        db_sessionmaker, account, text="Мастер приедет, его номер 8 900 111-22-40", direction="out"
    )

    await run_backfill_phones(db, limit=100, dry_run=False)

    async with db_sessionmaker() as s:
        assert (await s.get(Client, client_id)).phone is None


async def test_backfill_refuses_when_autofill_is_off(db, db_sessionmaker, account):
    """⚠ НАСТРОЙКА ГЛАВНЕЕ КОМАНДЫ.

    Выключенная автозапись — это решение владельца «в карточку пишет только
    человек». Массовая запись мимо неё подменяла бы его решение нашим.
    """
    from app.cli import run_backfill_phones

    await _autofill(db, autofill=False)
    client_id = await _history(db_sessionmaker, account, text="звоните 89001112240")

    await run_backfill_phones(db, limit=100, dry_run=False)

    async with db_sessionmaker() as s:
        assert (await s.get(Client, client_id)).phone is None


async def test_backfill_does_not_argue_with_a_human_decision(
    db, db_sessionmaker, account, users_by_role
):
    """⚠ ОТКЛОНЁННОЕ ЧЕЛОВЕКОМ НЕ ВОЗВРАЩАЕТСЯ.

    Память об отказе лежит в `client_phone_candidates`, а не в пустом поле
    `clients.phone`. Смотри автозапись только на пустое поле — и отклонённый
    «код домофона» молча оказался бы в карточке.
    """
    from app.cli import run_backfill_phones
    from app.models.client import CANDIDATE_REJECTED, ClientPhoneCandidate

    await _autofill(db, autofill=True)
    client_id = await _history(db_sessionmaker, account, text="код 8 900 111-22-40")

    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.client_id == client_id))
        ).scalar_one()
        s.add(
            ClientPhoneCandidate(
                client_id=client_id,
                conversation_id=conv.id,
                phone="+79001112240",
                raw="8 900 111-22-40",
                status=CANDIDATE_REJECTED,
                detected_at=T0,
            )
        )
        await s.commit()

    await run_backfill_phones(db, limit=100, dry_run=False)

    async with db_sessionmaker() as s:
        assert (await s.get(Client, client_id)).phone is None, "вернули отклонённое человеком"


async def test_backfill_leaves_a_filled_card_alone(db, db_sessionmaker, account):
    """Непустую карточку не трогаем — ни своей записью, ни поверх чужой правки."""
    from app.cli import run_backfill_phones

    await _autofill(db, autofill=True)
    client_id = await _history(db_sessionmaker, account, text="а вот мой 8 900 111-22-40")
    async with db_sessionmaker() as s:
        карточка = await s.get(Client, client_id)
        карточка.phone = "+79001112252"
        карточка.phone_set_by_id = None
        карточка.phone_set_at = T0
        await s.commit()

    await run_backfill_phones(db, limit=100, dry_run=False)

    async with db_sessionmaker() as s:
        assert (await s.get(Client, client_id)).phone == "+79001112252"


async def test_backfill_respects_the_mark_of_a_human_hand(db, db_sessionmaker, account):
    """⚠ ПУСТОЕ ПОЛЕ С ОТМЕТКОЙ РУКИ — ЭТО СТРАХОВКА, А НЕ ЗАЩИТА ОТ ЖИВОГО ПУТИ.

    Честно: сегодня такое состояние через интерфейс НЕ достижимо — ручка ввода
    требует валидный номер и очистить поле не даёт (`clients.set_phone`, 422
    «Не похоже на телефон»). Отметка проверяется потому, что состояние может
    приехать иначе — объединением карточек, разъединением, чужой правкой в
    базе, — и цена ошибки здесь несимметрична: вернуть номер, который человек
    убрал, значит позвонить не туда от его имени.

    Тест строит состояние напрямую, потому что живого пути к нему нет. Если он
    когда-нибудь появится, проверка уже стоит.
    """
    from app.cli import run_backfill_phones

    await _autofill(db, autofill=True)
    client_id = await _history(db_sessionmaker, account, text="мой 8 900 111-22-40")
    async with db_sessionmaker() as s:
        карточка = await s.get(Client, client_id)
        карточка.phone = None
        карточка.phone_set_at = T0  # человек поле трогал
        await s.commit()

    await run_backfill_phones(db, limit=100, dry_run=False)

    async with db_sessionmaker() as s:
        assert (await s.get(Client, client_id)).phone is None, (
            "вернули номер в поле, которого касался человек"
        )


async def test_backfill_takes_the_first_number_by_time(db, db_sessionmaker, account):
    """Карточку заполняет ПЕРВЫЙ названный номер — как заполнил бы её бой."""
    from app.cli import run_backfill_phones

    await _autofill(db, autofill=True)
    client_id = await _history(db_sessionmaker, account, text="сначала 8 900 111-22-40")
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.client_id == client_id))
        ).scalar_one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=f"am-{uuid.uuid4().hex[:8]}",
                direction="in",
                sender_type="client",
                body="а лучше на 8 900 111-22-53",
                attachments=[],
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=5),
            )
        )
        await s.commit()

    await run_backfill_phones(db, limit=100, dry_run=False)

    async with db_sessionmaker() as s:
        assert (await s.get(Client, client_id)).phone == "+79001112240"


async def test_backfill_skips_a_number_we_wrote_to_him_ourselves(db, db_sessionmaker, account):
    """⚠ КЛИЕНТ ПРОЦИТИРОВАЛ ТО, ЧТО ДАЛИ ЕМУ МЫ, — ЭТО НЕ ЕГО НОМЕР.

    «Вы прислали 8-900-111-22-40, он не отвечает». Разбор видит цифры во
    входящем и по букве правила прав; записать это в карточку — значит
    подставить телефон мастера вместо заказчика. Замер боя 02.09: таких три из
    2040.

    ⚠ ЗДЕСЬ ПРАВИЛО СТРОЖЕ, ЧЕМ В БОЮ, И ЭТО НАМЕРЕННО. Живую автозапись видит
    диспетчер и поправит в ту же минуту; массовую заливку не смотрит никто —
    две тысячи карточек меняются молча. Одинаковая строгость там, где разная
    цена ошибки, — не последовательность, а невнимательность.
    """
    from app.cli import run_backfill_phones

    await _autofill(db, autofill=True)
    client_id = await _history(
        db_sessionmaker, account, text="Мастер приедет, телефон 8 900 111-22-40", direction="out"
    )
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.client_id == client_id))
        ).scalar_one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=f"am-{uuid.uuid4().hex[:8]}",
                direction="in",
                sender_type="client",
                body="я звонил на 8 900 111-22-40, никто не берёт",
                attachments=[],
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=5),
            )
        )
        await s.commit()

    await run_backfill_phones(db, limit=100, dry_run=False)

    async with db_sessionmaker() as s:
        assert (await s.get(Client, client_id)).phone is None, (
            "записали клиенту телефон, который мы сами ему и прислали"
        )


async def test_backfill_still_takes_a_number_the_client_added_himself(db, db_sessionmaker, account):
    """Страховка выше не должна съедать законные номера.

    Мы писали человеку ОДИН номер, а он назвал СВОЙ — другой. Его и берём:
    иначе новое правило тихо обнулило бы половину заливки.
    """
    from app.cli import run_backfill_phones

    await _autofill(db, autofill=True)
    client_id = await _history(
        db_sessionmaker, account, text="Мастер: 8 900 111-22-40", direction="out"
    )
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.client_id == client_id))
        ).scalar_one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=f"am-{uuid.uuid4().hex[:8]}",
                direction="in",
                sender_type="client",
                body="мой 8 900 111-22-53",
                attachments=[],
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=5),
            )
        )
        await s.commit()

    await run_backfill_phones(db, limit=100, dry_run=False)

    async with db_sessionmaker() as s:
        assert (await s.get(Client, client_id)).phone == "+79001112253"
