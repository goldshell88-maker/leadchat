"""Загрузка ВСЕЙ истории на настоящем PostgreSQL (docs/41 §11).

ЗАЧЕМ ЭТО ЗДЕСЬ, А НЕ В ЮНИТАХ. Обе беды, ради которых написан набор, на
SQLite невидимы в принципе:

1. ПАРТИЦИИ. `messages` разбита помесячно, и до 11 августа загрузка падала на
   первом же сообщении старше текущего месяца — `no partition of relation
   "messages" found for row` (#24). В SQLite партиций нет вовсе: там код
   молча проходит мимо, и «зелёный» юнит-тест ничего про это не знает.
   Здесь год переписки заезжает в настоящую партиционированную таблицу.
2. МЕТРИКИ. Скорость первого ответа и доля отвеченных считаются
   материализованным представлением `mv_conversation_stats` — это SQL,
   которого в юнит-окружении не существует. Проверяем ровно то, чего боялся
   владелец: год импортированной переписки не должен попасть в «сегодня».
"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import AvitoAccount, Conversation, Message
from app.scheduler import partitions as partitions_mod
from app.services import avito_accounts as svc
from app.services import stats as st
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

ACCOUNT_UID = 771100
CLIENT_UID = 991100
MONTHS = 13


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    await client.flushdb()
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
                "clients, avito_accounts, users CASCADE"
            )
        )


@pytest.fixture
async def account(sessionmaker) -> AvitoAccount:
    async with sessionmaker() as db:
        row = AvitoAccount(
            title="LP-История",
            avito_user_id=ACCOUNT_UID,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=datetime.now(UTC) + timedelta(days=1),
            status="active",
            webhook_secret="whsec-hist",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


def _raw_chat(chat_id: str, *, unread: bool, last_at: datetime) -> dict[str, Any]:
    return {
        "id": chat_id,
        "users": [{"id": ACCOUNT_UID, "name": "Мы"}, {"id": CLIENT_UID, "name": "Клиент"}],
        "context": {"type": "item", "value": {"title": "Ремонт стиральных машин"}},
        "has_unread": unread,
        "updated": int(last_at.timestamp()),
    }


def _raw_msg(msg_id: str, *, created: datetime, author_id: int = CLIENT_UID) -> dict[str, Any]:
    return {
        "id": msg_id,
        "author_id": author_id,
        "created": int(created.timestamp()),
        "content": {"text": f"сообщение {msg_id}"},
    }


def _api(chats: list[dict[str, Any]], messages: dict[str, list[dict[str, Any]]]):
    async def fake_call(fn, _account, _db, _redis, _limiter, *args, **kwargs):
        offset = int(kwargs.get("offset", 0))
        limit = int(kwargs.get("limit", 100))
        if fn.__name__ == "get_chats":
            return chats[offset : offset + limit]
        return messages.get(args[1], [])[offset : offset + limit]

    return fake_call


async def _partitions(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'messages'"
            )
        )
        return set(rows.scalars())


async def refresh_mv(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(st.REFRESH_MV_SQL))


async def test_a_year_of_history_lands_and_stays_in_the_past(
    monkeypatch, pg_engine, sessionmaker, redis, account
) -> None:
    """Год переписки заезжает целиком — и ни одним сообщением не попадает в «сегодня».

    Две проверки в одном прогоне намеренно: они про один и тот же прогон и
    разошлись бы, будь они на разных данных. Партиции обеспечивает НАСТОЯЩИЙ
    `PartitionCoverage` (подменён только движок, чтобы он смотрел в тестовую
    базу, а не в процессный синглтон).
    """
    monkeypatch.setattr(
        svc, "PartitionCoverage", lambda: partitions_mod.PartitionCoverage(pg_engine)
    )
    now = datetime.now(UTC)
    # ⚠ ШАГ ПО КАЛЕНДАРНЫМ МЕСЯЦАМ, А НЕ ПО 30 ДНЕЙ (28.08). Тринадцать шагов по
    # 30 дней покрывают 390 дней, но НЕ тринадцать месяцев: февраль короче шага,
    # а месяцы по 31 дню ловятся дважды. Здесь это пока не роняло тест (проверка
    # идёт по каждому легшему сообщению, а не по числу месяцев), но набор данных
    # уже не соответствовал названию — «год истории, тринадцать месяцев», — и
    # три соседних теста на том же приёме покраснели 28 августа сами.
    начало = now.replace(day=15, hour=12, minute=0, second=0, microsecond=0)
    моменты = []
    год, месяц = начало.year, начало.month
    for _ in range(MONTHS):
        моменты.append(начало.replace(year=год, month=месяц))
        месяц -= 1
        if месяц == 0:
            месяц, год = 12, год - 1
    history = [_raw_msg(f"m{n}", created=м) for n, м in enumerate(моменты)]
    assert len({(м.year, м.month) for м in моменты}) == MONTHS, "набор перестал быть годом"
    oldest = history[-1]
    # Непрочитанного в этом чате нет: он про партиции и про время, а решение
    # «архив или очередь» проверяется отдельно и на своих данных.
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _api([_raw_chat("c-year", unread=False, last_at=now)], {"c-year": history}),
    )

    await svc.backfill_account(
        {"db_session_factory": sessionmaker, "redis": redis}, account.id, svc.HISTORY_ALL
    )

    async with sessionmaker() as db:
        stored = list(
            (await db.execute(select(Message).order_by(Message.created_at))).scalars().all()
        )
        conv = (await db.execute(select(Conversation))).scalars().one()
    assert len(stored) == MONTHS, "год истории обязан лечь целиком"

    # Партиция под КАЖДЫЙ пришедший месяц — включая самый старый.
    partitions = await _partitions(pg_engine)
    for msg in stored:
        assert partitions_mod.partition_name(msg.created_at.date()) in partitions

    # Самое старое сообщение читается из базы тем же временем, каким пришло:
    # это и значит, что оно легло в свою партицию, а не в чужую.
    assert stored[0].external_message_id == oldest["id"]
    assert int(stored[0].created_at.timestamp()) == oldest["created"]

    # Импортированный диалог ложится в архив: работой он становится только
    # от свежего непрочитанного (проверяется ниже, на своих данных).
    assert conv.status == "closed"
    assert conv.offered_at is None


async def test_imported_history_does_not_touch_todays_metrics(
    monkeypatch, pg_engine, sessionmaker, redis, account
) -> None:
    """Скорость первого ответа и доля отвеченных за сегодня — без импорта.

    Это то, о чём владелец предупредил прямо: «старые диалоги не должны
    попасть в сегодня и испортить статистику первого ответа». Считается
    материализованным представлением по `first_client_at` = первое входящее
    диалога, поэтому проверка идёт через настоящий MV, а не через питон.
    """
    monkeypatch.setattr(
        svc, "PartitionCoverage", lambda: partitions_mod.PartitionCoverage(pg_engine)
    )
    now = datetime.now(UTC)
    long_ago = now - timedelta(days=120)
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _api(
            [_raw_chat("c-old", unread=False, last_at=long_ago + timedelta(minutes=40))],
            {
                "c-old": [
                    _raw_msg("вопрос", created=long_ago),
                    # Ответ через 40 минут — год назад. Попади он в «сегодня»,
                    # средняя скорость первого ответа за сутки выросла бы на
                    # сорок минут из ниоткуда.
                    _raw_msg(
                        "ответ",
                        created=long_ago + timedelta(minutes=40),
                        author_id=ACCOUNT_UID,
                    ),
                ]
            },
        ),
    )

    await svc.backfill_account(
        {"db_session_factory": sessionmaker, "redis": redis}, account.id, svc.HISTORY_ALL
    )
    await refresh_mv(pg_engine)

    today = st.Period(st.today_msk(), st.today_msk())
    then = (long_ago.astimezone(st.MSK)).date()
    async with sessionmaker() as db:
        now_agg = await st.frt_aggregate(db, today, st.Filters())
        then_agg = await st.frt_aggregate(db, st.Period(then, then), st.Filters())
        snapshot = await st.snapshot_now(db, st.Filters())

    assert now_agg["conversations_started"] == 0, "импорт не должен добавлять сегодняшних диалогов"
    assert now_agg["frt_operator_avg_sec"] is None
    # А в своём дне он есть, и с настоящей скоростью ответа — сорок минут.
    assert then_agg["conversations_started"] == 1
    assert then_agg["frt_operator_avg_sec"] == 40 * 60
    # Ни очередь, ни «ждут ответа» не растут: диалог закрыт, а не поднят в new.
    # Импорт кладёт историю в архив (`status='closed'`), поэтому ни одна
    # карточка группы «Прямо сейчас» не растёт. Сравниваем по числам, а не со
    # всем ответом целиком: с docs/38 снимок отдаёт ещё и разбивку по статусам,
    # и в неё импортированный диалог как раз попадает — закрытым, где ему и
    # место.
    assert snapshot["queue_now"] == 0
    assert snapshot["in_progress_now"] == 0
    assert snapshot["waiting_now"] == 0
    assert snapshot["open_now"] == 0, "импортированный диалог попал в открытые"


async def test_fresh_unread_from_the_import_is_real_work(
    monkeypatch, pg_engine, sessionmaker, redis, account
) -> None:
    """Свежее непрочитанное из истории попадает в очередь по-настоящему (SCEN-21).

    Обратная сторона предыдущей проверки. Переход с Jivo делается на живом
    канале: в момент подключения там лежат чаты, где человек написал вчера и
    ответа не получил. Это работа на сегодня, и она обязана дойти до
    операторов — с `offered_at`, а не «статус new и больше ничего».
    """
    monkeypatch.setattr(
        svc, "PartitionCoverage", lambda: partitions_mod.PartitionCoverage(pg_engine)
    )
    recent = datetime.now(UTC) - timedelta(hours=3)
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _api(
            [_raw_chat("c-fresh", unread=True, last_at=recent)],
            {"c-fresh": [_raw_msg("свежее", created=recent)]},
        ),
    )

    await svc.backfill_account(
        {"db_session_factory": sessionmaker, "redis": redis}, account.id, svc.HISTORY_ALL
    )

    from app.services import inbox

    async with sessionmaker() as db:
        in_queue = list(
            (await db.execute(select(Conversation.id).where(inbox.queue_condition()))).scalars()
        )
        conv = (await db.execute(select(Conversation))).scalars().one()
    assert in_queue == [conv.id]
    assert conv.status == "new"
    assert conv.offered_at is not None


async def test_a_stopped_run_can_be_continued(
    monkeypatch, pg_engine, sessionmaker, redis, account
) -> None:
    """Остановка и продолжение на настоящей базе: ничего не теряется и не двоится."""
    monkeypatch.setattr(
        svc, "PartitionCoverage", lambda: partitions_mod.PartitionCoverage(pg_engine)
    )
    now = datetime.now(UTC)
    chats = [_raw_chat(f"c{n}", unread=False, last_at=now - timedelta(days=n)) for n in range(3)]
    for n, chat in enumerate(chats):
        chat["users"][1]["id"] = CLIENT_UID + n
    history = {
        f"c{n}": [
            _raw_msg(f"m{n}", created=now - timedelta(days=n), author_id=CLIENT_UID + n),
        ]
        for n in range(3)
    }
    monkeypatch.setattr(svc, "_avito_call", _api(chats, history))

    original = svc._backfill_chat

    async def stop_after_first(*args, **kwargs):
        result = await original(*args, **kwargs)
        await svc.request_backfill_stop(redis, account.id)
        return result

    monkeypatch.setattr(svc, "_backfill_chat", stop_after_first)
    await svc.backfill_account(
        {"db_session_factory": sessionmaker, "redis": redis}, account.id, svc.HISTORY_ALL
    )

    state = await svc.get_backfill_state(redis, account.id)
    assert state["status"] == "stopped"
    assert state["loaded"] == 1 and state["total"] == 3

    monkeypatch.setattr(svc, "_backfill_chat", original)
    await svc.backfill_account(
        {"db_session_factory": sessionmaker, "redis": redis}, account.id, svc.HISTORY_ALL
    )

    async with sessionmaker() as db:
        convs = list((await db.execute(select(Conversation))).scalars().all())
        msgs = list((await db.execute(select(Message))).scalars().all())
    assert len(convs) == 3
    assert len(msgs) == 3, "продолжение не должно задваивать уже загруженное"
    assert (await svc.get_backfill_state(redis, account.id))["status"] == "idle"


async def test_answered_chat_from_history_is_not_work(
    monkeypatch, pg_engine, sessionmaker, redis, account
) -> None:
    """Мы ответили последними — диалог НЕ работа, даже если Авито держит непрочитанное.

    БОЕВОЙ СЛУЧАЙ 19.08. Свежесть считалась по ЛЮБОМУ последнему событию,
    включая наш собственный ответ. Отсюда 129 диалогов в очереди, где мы уже
    ответили и ход давно за клиентом: работа сделана, а строка висит и требует
    внимания. Владелец увидел это так: «осталось 179, некоторые висят 47 дней».

    Очередь — это «клиент написал и ждёт». Решает направление ПОСЛЕДНЕГО
    сообщения, а не то, что где-то в чате есть непрочитанное: Авито держит
    отметку почти на всём, потому что LeadChat не помечает чаты прочитанными.
    """
    monkeypatch.setattr(
        svc, "PartitionCoverage", lambda: partitions_mod.PartitionCoverage(pg_engine)
    )
    вчера = datetime.now(UTC) - timedelta(hours=20)
    час_назад = datetime.now(UTC) - timedelta(hours=1)
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _api(
            [_raw_chat("c-answered", unread=True, last_at=час_назад)],
            {
                "c-answered": [
                    _raw_msg("клиент-спросил", created=вчера),
                    # последним ответили МЫ — ход за клиентом
                    _raw_msg("мы-ответили", created=час_назад, author_id=account.avito_user_id),
                ]
            },
        ),
    )

    await svc.backfill_account(
        {"db_session_factory": sessionmaker, "redis": redis}, account.id, svc.HISTORY_ALL
    )

    from app.services import inbox

    async with sessionmaker() as db:
        in_queue = list(
            (await db.execute(select(Conversation.id).where(inbox.queue_condition()))).scalars()
        )
        conv = (await db.execute(select(Conversation))).scalars().one()

    assert in_queue == [], "мы ответили последними — в очереди этому диалогу не место"
    assert conv.offered_at is None
    assert conv.status == "closed", "история остаётся историей, пока клиент не напишет снова"


async def test_platform_offset_limit_is_not_a_crash(
    monkeypatch, pg_engine, sessionmaker, redis, account
) -> None:
    """Авито не пускает глубже тысячи — прогон заканчивается честно, а не падает.

    БОЕВОЙ СЛУЧАЙ 19.08. У владельца восемь каналов по тысяче с лишним чатов.
    Каждый прогон загрузки и сверки доходил до дальней страницы и получал
    «Авито: список чатов -> HTTP 400». Прогон падал целиком: владелец видел
    «Загрузка истории сорвалась» по всем каналам подряд, а сверка — страховка
    от недошедших вебхуков — не отрабатывала вовсе НИ РАЗУ, пока в канале
    больше тысячи чатов.

    Отказ на ДАЛЬНЕЙ странице — это конец списка, а не поломка: всё, что
    загружено, остаётся на месте, и прогон закрывается штатно. На ПЕРВОЙ
    странице 400 по-прежнему настоящая беда и уходит наверх.
    """
    from app.integrations.avito.errors import AvitoApiError

    monkeypatch.setattr(
        svc, "PartitionCoverage", lambda: partitions_mod.PartitionCoverage(pg_engine)
    )
    свежее = datetime.now(UTC) - timedelta(hours=2)
    страницы = {
        0: [_raw_chat(f"c-{i}", unread=False, last_at=свежее) for i in range(100)],
    }

    async def отказ_на_второй(fn, _account, _db, _redis, _limiter, *args, **kwargs):
        offset = int(kwargs.get("offset", 0))
        if "get_chats" in getattr(fn, "__name__", ""):
            if offset == 0:
                return страницы[0]
            raise AvitoApiError("Авито: список чатов -> HTTP 400", status=400)
        return [_raw_msg("привет", created=свежее)]

    monkeypatch.setattr(svc, "_avito_call", отказ_на_второй)

    # Прогон обязан ЗАКОНЧИТЬСЯ, а не упасть: исключения быть не должно.
    await svc.backfill_account(
        {"db_session_factory": sessionmaker, "redis": redis}, account.id, svc.HISTORY_ALL
    )
    async with sessionmaker() as db:
        диалогов = len(list((await db.execute(select(Conversation.id))).scalars()))
    assert диалогов == 100, "всё, что успели взять до отказа, остаётся на месте"
