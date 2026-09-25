"""Пакет 3, ревью 19.09, C8: `address-reparse --auto` против гонки с человеком.

Снятие автозаписи шло безусловной ORM-записью карточки, прочитанной Python-
проверкой, в одной транзакции на все автопринятые строки за 60 дней: оператор,
вписавший адрес руками в это окно, либо ждал на своём UPDATE, либо терял
набранное. Теперь:

* снятие — УСЛОВНЫЙ UPDATE (`address_candidate_id = строка AND address_set_at
  IS NULL`), как у `autofill_address`: гонку решает база, при rowcount 0 — ни
  журнала, ни кадра, ни счётчика;
* commit — пачками (`cli._ПАЧКА_АВТО`), основной обход — своей транзакцией;
* кадр `client:updated` — один на снятую карточку и только после commit'а
  своей пачки (08 §8.1), задачи воркеру — там же.

Здесь же D4 (принято): `other_conversation_cities` читает у диалогов клиента
две колонки (слаг и ссылку), а не строки целиком с тегами — город считается
тем же `conversation_city` через лёгкий объект.

ДИВЕРСИИ (каждая обязана краснеть): убрать `Client.address_set_at.is_(None)`
из WHERE → случай «руками»; убрать `Client.address_candidate_id == row.id` →
случай «воркер_переставил»; перенести `publish_client_updated` выше
`db.commit()` → кадр в транзакции; убрать `кадры.clear()` или закрытие пачки
в цикле → число кадров/commit'ов; вернуть один commit в конце → пачки;
вернуть `select(Conversation)` в `other_conversation_cities` → состав SELECT.

Телефонов и имён клиентов в тестах нет.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import AuditLog, Client, ClientAddressCandidate, Conversation
from app.models.client import CANDIDATE_REJECTED
from app.services import clients_events
from app.services.conversations import other_conversation_cities
from tests.unit.test_paket3_1909 import T0, _речь_в_карточке

pytestmark = pytest.mark.anyio


async def _ещё_карточка(db_sessionmaker, account, n: int) -> SimpleNamespace:  # noqa: ANN001
    """Ещё один клиент со своим диалогом на том же аккаунте — под вторую и
    третью карточку (у `seed_conversation` она одна)."""
    async with db_sessionmaker() as s:
        client = Client(channel="avito", external_id=f"99919{n:02d}", name="Клиент")
        s.add(client)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client.id,
            status="new",
            unread_count=0,
            last_message_at=T0,
        )
        s.add(conv)
        await s.commit()
        return SimpleNamespace(account=account, client_id=client.id, conversation_id=conv.id)


@pytest.fixture
def кадры(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Кадры карточки — списком. Перехват на `clients_events.publish_event`:
    кадр `address-reparse --auto` идёт через общий `publish_client_updated`,
    и тест, которому нужен снимок транзакции, оборачивает этот же перехват."""
    записи: list[dict[str, Any]] = []

    async def публикация(_redis: Any, kind: str, payload: dict[str, Any], **_kw: Any) -> None:
        записи.append({"kind": kind, **payload})

    monkeypatch.setattr(clients_events, "publish_event", публикация)
    return записи


async def _журнал_снятий(db_sessionmaker) -> list[AuditLog]:  # noqa: ANN001
    async with db_sessionmaker() as s:
        return list(
            (await s.execute(sa.select(AuditLog).where(AuditLog.action == "client.address_edited")))
            .scalars()
            .all()
        )


@pytest.mark.parametrize("кто", ["руками", "воркер_переставил"])
async def test_reparse_auto_не_снимает_карточку_изменённую_между_чтением_и_записью(
    seed_conversation, db_sessionmaker, redis, capsys, monkeypatch, кадры, кто: str
):
    """Между `db.get(Client)` и UPDATE карточку меняют мимо сессии: оператор
    вписал адрес руками (`address_set_at`), либо воркер уже переставил её на
    другую строку. Строка речи отклоняется (речь есть речь), а карточка не
    тронута: rowcount 0 → счётчик `авто_карточек` не растёт, журнала, кадра и
    задачи воркеру нет. Python-проверка при этом видит СТАРЫЙ объект и пускает
    к UPDATE — решает WHERE, а не порядок чтения."""
    from app.cli import run_address_reparse

    речь, годная = await _речь_в_карточке(seed_conversation, db_sessionmaker)
    оператор = uuid.uuid4()
    чужая_запись = (
        {"address": "ул Ленина, 7", "address_set_at": T0, "address_set_by_id": оператор}
        if кто == "руками"
        else {"address": "улица Ленина, 5, Орск", "address_candidate_id": годная}
    )
    async with db_sessionmaker() as s:
        исходный_get = s.get
        вмешались: list[uuid.UUID] = []

        async def get_и_чужая_запись(model, ident, *args, **kwargs):  # noqa: ANN001
            obj = await исходный_get(model, ident, *args, **kwargs)
            if model is Client and not вмешались:
                вмешались.append(ident)
                # Мимо объектов сессии — как чужая транзакция: объект в памяти
                # остаётся прежним, база — уже нет.
                await s.execute(
                    sa.update(Client)
                    .where(Client.id == ident)
                    .values(**чужая_запись)
                    .execution_options(synchronize_session=False)
                )
            return obj

        monkeypatch.setattr(s, "get", get_и_чужая_запись)
        await run_address_reparse(s, days=30, dry_run=False, auto=True, redis=redis)
    assert вмешались == [seed_conversation.client_id], "чужая запись легла между чтением и UPDATE"
    вывод = capsys.readouterr().out
    assert "авто_строк=1 авто_отклонено=1 авто_карточек=0" in вывод
    assert "авто_поставлено" not in вывод
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, речь)).status == CANDIDATE_REJECTED
        card = await s.get(Client, seed_conversation.client_id)
        assert card.address == чужая_запись["address"]
        if кто == "руками":
            assert (card.address_set_by_id, card.address_candidate_id) == (оператор, речь)
            assert card.address_set_at is not None
        else:
            assert (card.address_candidate_id, card.address_set_at) == (годная, None)
    assert await _журнал_снятий(db_sessionmaker) == []
    assert кадры == []
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_reparse_auto_кадр_один_на_карточку_и_после_commit(
    seed_conversation, db_sessionmaker, redis, monkeypatch, кадры
):
    """Две карточки со снятой автозаписью — два кадра `client:updated`, по одному
    на карточку, с диалогом-источником и причиной `address_reparsed`; каждый —
    вне транзакции (после commit'а пачки), задачи воркеру — тоже после."""
    from app.cli import run_address_reparse

    вторая = await _ещё_карточка(db_sessionmaker, seed_conversation.account, 1)
    await _речь_в_карточке(seed_conversation, db_sessionmaker)
    await _речь_в_карточке(вторая, db_sessionmaker)
    в_транзакции: list[bool] = []
    async with db_sessionmaker() as s:
        публикация = clients_events.publish_event

        async def публикация_со_снимком(*args: Any, **kwargs: Any) -> None:
            в_транзакции.append(s.in_transaction())
            await публикация(*args, **kwargs)

        monkeypatch.setattr(clients_events, "publish_event", публикация_со_снимком)
        await run_address_reparse(s, days=30, dry_run=False, auto=True, redis=redis)
    assert в_транзакции == [False, False], "кадр ушёл до commit'а"
    ожидание = {
        (str(seed_conversation.client_id), str(seed_conversation.conversation_id)),
        (str(вторая.client_id), str(вторая.conversation_id)),
    }
    assert {(к["client_id"], к["conversation_id"]) for к in кадры} == ожидание
    assert [(к["kind"], к["reason"]) for к in кадры] == [("client:updated", "address_reparsed")] * 2
    assert len(await _журнал_снятий(db_sessionmaker)) == 2
    for сид in (seed_conversation, вторая):
        assert await redis.exists(f"arq:job:addr-fill:{сид.conversation_id}")
        async with db_sessionmaker() as s:
            assert (await s.get(Client, сид.client_id)).address is None


async def test_reparse_auto_коммитит_пачками_а_сухой_прогон_не_коммитит(
    seed_conversation, db_sessionmaker, redis, monkeypatch, кадры, capsys
):
    """Три карточки при пачке в две строки: commit основного обхода, commit
    первой пачки, commit хвоста — три, и кадры первых двух карточек уходят
    после ВТОРОГО commit'а, не дожидаясь третьего. Сухой прогон не коммитит и
    не шлёт ничего."""
    from app import cli

    monkeypatch.setattr(cli, "_ПАЧКА_АВТО", 2)
    сиды = [seed_conversation] + [
        await _ещё_карточка(db_sessionmaker, seed_conversation.account, n) for n in (2, 3)
    ]
    for сид in сиды:
        await _речь_в_карточке(сид, db_sessionmaker)

    async def прогон(*, dry_run: bool) -> tuple[int, list[int]]:
        кадры.clear()
        async with db_sessionmaker() as s:
            commit = s.commit
            счёт = {"commit": 0}
            при_каком_commit: list[int] = []

            async def commit_шпион() -> None:
                счёт["commit"] += 1
                await commit()

            публикация = clients_events.publish_event

            async def публикация_со_счётом(*args: Any, **kwargs: Any) -> None:
                при_каком_commit.append(счёт["commit"])
                await публикация(*args, **kwargs)

            monkeypatch.setattr(s, "commit", commit_шпион)
            monkeypatch.setattr(clients_events, "publish_event", публикация_со_счётом)
            await cli.run_address_reparse(s, days=30, dry_run=dry_run, auto=True, redis=redis)
        return счёт["commit"], при_каком_commit

    assert await прогон(dry_run=True) == (0, [])
    assert "авто_карточек=3" in capsys.readouterr().out
    assert кадры == []
    commits, при_каком = await прогон(dry_run=False)
    assert commits == 3, "основной обход + пачка + хвост"
    assert sorted(при_каком) == [2, 2, 3], "кадры первой пачки — после её commit'а, не в конце"
    assert {к["client_id"] for к in кадры} == {str(с.client_id) for с in сиды}
    assert len(кадры) == 3, "кадр ровно один на карточку"
    assert "авто_карточек=3 авто_поставлено=3" in capsys.readouterr().out
    async with db_sessionmaker() as s:
        карточки = (
            (await s.execute(sa.select(Client).where(Client.id.in_([с.client_id for с in сиды]))))
            .scalars()
            .all()
        )
        assert [(к.address, к.address_candidate_id) for к in карточки] == [(None, None)] * 3
        статусы = (
            await s.execute(
                sa.select(ClientAddressCandidate.status).where(
                    ClientAddressCandidate.value == "Камера, 4G"
                )
            )
        ).scalars()
        assert list(статусы) == [CANDIDATE_REJECTED] * 3


def test_пачка_авто_задана_числом_и_не_меньше_ста() -> None:
    """Порог живёт константой модуля (тест выше её подменяет): один UPDATE на
    транзакцию — лишние commit'ы, тысячи — минуты замков карточек."""
    from app import cli

    assert isinstance(cli._ПАЧКА_АВТО, int) and 100 <= cli._ПАЧКА_АВТО <= 1000


# ── D4. города других диалогов — две колонки, не строка ─────────────────────


async def test_города_других_диалогов_читают_две_колонки_а_не_строку(
    seed_conversation, db_sessionmaker, engine: AsyncEngine
):
    """Стережём САМ ЗАПРОС к базе, а не текст исходника: SELECT берёт у
    `conversations` только `item_city_slug` и `item_url` — ни тегов
    (массив), ни остальных колонок; предел 20 на месте. Довод: сюда ходят с
    любой пары «N-M» в речи клиента («2-3 дня», «10-12»), и грузить ради имени
    города полные строки двадцати диалогов — лишняя работа на заметной доле
    реплик. Город при этом считается тем же путём: диалог со слагом даёт
    город по колонке, диалог без слага — по ссылке."""
    async with db_sessionmaker() as s:
        for n, поля in enumerate(
            (
                {"item_city_slug": "orsk"},
                {
                    "item_url": "https://www.avito.ru/saransk/predlozheniya_uslug/remont_tv_1234567890"
                },
            )
        ):
            s.add(
                Conversation(
                    channel="avito",
                    external_chat_id=f"chat-d4-{n}",
                    account_id=seed_conversation.account.id,
                    client_id=seed_conversation.client_id,
                    status="closed",
                    last_message_at=T0,
                    **поля,
                )
            )
        await s.commit()
    запросы: list[tuple[str, Any]] = []

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        if "FROM conversations" in statement:
            запросы.append((" ".join(statement.split()), parameters))

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        async with db_sessionmaker() as s:
            города = await other_conversation_cities(
                s, seed_conversation.client_id, seed_conversation.conversation_id
            )
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    assert [c.name for c in города] == ["Орск", "Саранск"]
    ((sql, params),) = запросы
    assert sql.startswith(
        "SELECT conversations.item_city_slug, conversations.item_url FROM conversations"
    ), sql
    assert "tags" not in sql and "conversations.id," not in sql
    assert " LIMIT " in sql and 20 in tuple(params)
