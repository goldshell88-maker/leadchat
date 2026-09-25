"""INT-1..5 (07 §1.2): полный тракт вебхук → стрим → консьюмер → БД → Pub/Sub
на настоящих Postgres + Redis (testcontainers), с миграциями `upgrade head`.

INT-1 полный путь; INT-2 идемпотентность дублей; INT-3 reconciliation
досоздаёт недостающее; INT-4 возврат клиента в закрытый диалог; INT-5 эхо.
"""

import json
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import event as sa_event
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.api import deps
from app.main import create_app
from app.models import AuditLog, AvitoAccount, Client, Conversation, Message, WebhookRawLog
from app.services.inbound import apply_inbound_event
from app.workers import inbound as inbound_mod
from app.workers import reconciliation as reconcile_mod
from tests.integration.conftest import requires_docker

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

pytestmark = requires_docker

NOW = datetime.now(UTC).replace(microsecond=0)


# ПОЧЕМУ ЗДЕСЬ СЧИТАЮТ ОБРАЩЕНИЯ К БАЗЕ, А НЕ СЕКУНДЫ
# ---------------------------------------------------
# INT-1 (07 §1.2) требует «время от POST до publish < 1 с», и до 29 августа
# 2026 это буквально и стояло в тесте: `assert time.monotonic() - started < 1.0`.
# В одиночку тест укладывался в доли секунды, но при параллельном прогоне
# соседнего набора на той же машине замер вышел 1,109 с — и тест покраснел.
# Порог по стенным часам меряет не код, а соседей по машине: он краснеет на
# медленном раннере сборки и молчит там, где регрессия есть, но железо быстрее
# прежнего. Красный «иногда» перестают читать вовсе — и это дороже, чем
# отсутствие проверки.
#
# Сторожил порог не саму секунду, а УСТРОЙСТВО пути, ради которого он и
# написан: шлюз вебхука отвечает 200, не делая работы конвейера (docstring
# `app/api/routes/webhooks.py`: «never writes to PostgreSQL», SLA p99 < 50 мс),
# а консьюмер обрабатывает сообщение за постоянное число обращений к базе, а не
# за N+1. И то и другое считается детерминированно — тем же прогоном на любой
# машине.
#
# Стенные часы никуда не делись, они там, где им и место: k6 меряет p95 < 200 мс
# на вебхуке и p95 < 2 с на доставке вебхук → WS (`tests/load/release.js`,
# 07 §7). Нагрузочный прогон вправе мерить время, потому что задаёт условия;
# интеграционный тест на общей машине — нет.

#: Обращений к PostgreSQL на один вебхук в шлюзе. Ровно два, и оба на чтение:
#: PK-чтение `avito_accounts` (проверка секрета) и чтение `app_settings` (срок
#: подробного следа, `app/core/trace.py`; ответ кэшируется на 5 с). Третье
#: обращение здесь — это либо запись, либо лишний поход в базу на КАЖДОМ
#: вебхуке, и то и другое стоит увидеть в лицо, а не через секундомер.
GATEWAY_SQL_BUDGET = 2

#: Потолок обращений к PostgreSQL на ОДНО входящее сообщение в консьюмере.
#: Снято с прогона 29 августа 2026: сырец, канал, настройки, клиент, диалог,
#: сообщение, счётчики диалога, строка очереди, подбор операторов, отметка
#: сырца.
#:
#: БЫЛО 21, СТАЛО 18. Три снятых обращения — это `app_settings`, за которыми
#: ходили четыре независимых слоя одного сообщения: подробный след, разбор
#: телефона, автораздача и сборка строки очереди. Теперь чтение одно на проход
#: (`app.services.app_settings.one_pass`), и следующий слой, которому
#: понадобятся настройки, не прибавит к этой цифре ничего.
#:
#: ЧТО ОСТАЛОСЬ ПОВТОРАМИ И ПОЧЕМУ. `clients` читается трижды, канал
#: (`avito_accounts`) — дважды. Два чтения клиента и два диалога — это
#: «выбрать, вставить с ON CONFLICT, перечитать»: перечитывать нужно потому,
#: что строку мог вставить параллельный вебхук, и без этого чтения строки
#: гонщика у нас бы не было. Ещё по одному чтению клиента и канала добавляет
#: `conversations._load_related` — постраничный загрузчик, которого зовут ради
#: ОДНОГО диалога: он не знает, что вызывающий уже держит эти две строки в
#: руках. Убрать это значит протащить готовые объекты через восемь мест вызова
#: `inbox_frame_addressed` ради двух запросов; пока не сделано осознанно.
#:
#: Цифра — потолок, а не идеал: она ловит N+1, из-за которого путь «вебхук →
#: publish» и начинал тормозить. Осознанно добавленный запрос поднимает потолок
#: тем же коммитом: падение здесь означает «посмотри, во что обошёлся ход», а
#: не «так нельзя».
CONSUMER_SQL_BUDGET = 18

#: Ключевые слова операторов, меняющих данные. Проверяем по первому слову
#: запроса: шлюзу вебхука писать в PostgreSQL нечем и незачем (см. INT-1).
WRITE_VERBS = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE"})


class SqlTally:
    """Сколько операторов SQL доехало до драйвера за отрезок работы.

    Считает обращения, а не время: замер стенных часов на общей машине
    зависит от соседей по прогону, счёт запросов — нет.
    """

    def __init__(self) -> None:
        self.statements: list[str] = []

    def __len__(self) -> int:
        return len(self.statements)

    @property
    def writes(self) -> list[str]:
        return [s for s in self.statements if s.strip().split(None, 1)[0].upper() in WRITE_VERBS]

    def reset(self) -> None:
        self.statements.clear()

    def report(self) -> str:
        """Список запросов одной строкой — чтобы падение сразу называло виновника."""
        return "\n".join(
            f"  {i + 1}. {' '.join(s.split())[:120]}" for i, s in enumerate(self.statements)
        )


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def pg_sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
def sql_tally(pg_engine: AsyncEngine) -> Iterator[SqlTally]:
    """Счётчик обращений к PostgreSQL — на том же движке, что и приложение."""
    tally = SqlTally()

    def _record(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        tally.statements.append(statement)

    sa_event.listen(pg_engine.sync_engine, "before_cursor_execute", _record)
    yield tally
    sa_event.remove(pg_engine.sync_engine, "before_cursor_execute", _record)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean(pg_sessionmaker, redis):
    async with pg_sessionmaker() as s:
        await s.execute(
            text(
                "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
                "clients, avito_accounts, users CASCADE"
            )
        )
        await s.commit()
    await redis.flushdb()


@pytest.fixture
async def account(pg_sessionmaker) -> AvitoAccount:
    async with pg_sessionmaker() as s:
        row = AvitoAccount(
            title="LP-Интеграция",
            avito_user_id=111222333,
            access_token_enc=b"enc-access",
            refresh_token_enc=b"enc-refresh",
            token_expires_at=NOW + timedelta(days=1),
            status="active",
            webhook_secret="whsec-int",
            # МОМЕНТ ПОДКЛЮЧЕНИЯ ЗАДАЁТСЯ ЯВНО, А НЕ БЕРЁТСЯ ИЗ ЧАСОВ.
            #
            # Сверка делит сообщения на живые и исторические по
            # `account.created_at` (две двери, см. `reconciliation`). `NOW`
            # вычисляется при ИМПОРТЕ модуля, а канал заводился при выполнении
            # фикстуры — то есть на столько позже, сколько шёл весь прогон.
            # Пока набор укладывался в минуту, разницы не было; 12 августа он
            # дорос до пяти, и сообщения `NOW + 1 мин` оказались СТАРШЕ момента
            # подключения. Два из трёх поехали исторической дверью, и тест
            # покраснел — в одиночку зелёный, в наборе красный.
            #
            # Час назад — заведомо раньше любого `NOW + n минут`, сколько бы
            # ни длился прогон.
            created_at=NOW - timedelta(hours=1),
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


@pytest.fixture
async def api(pg_sessionmaker, redis) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with pg_sessionmaker() as session:
            yield session

    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[deps.get_redis] = lambda: redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def ctx(pg_sessionmaker, redis) -> dict:
    return {"db_session_factory": pg_sessionmaker, "redis": redis}


# ------------------------------------------------------------------- helpers


def webhook_payload(
    *,
    chat_id: str,
    message_id: str,
    author_id: int,
    user_id: int = 111222333,
    text_: str = "Здравствуйте! Экран разбит, почём?",
    created: datetime | None = None,
) -> dict:
    created = created or NOW
    return {
        "id": f"wh-{message_id}",
        "version": "v3.0.0",
        "timestamp": int(created.timestamp()),
        "payload": {
            "type": "message",
            "value": {
                "id": message_id,
                "chat_id": chat_id,
                "author_id": author_id,
                "user_id": user_id,
                "created": int(created.timestamp()),
                "type": "text",
                "chat_type": "u2i",
                "content": {"text": text_},
            },
        },
    }


async def run_consumer(ctx, redis, *, consumer: str = "test:1") -> int:
    """Одна итерация консьюмера: XREADGROUP + обработка + XACK (08 §2.4)."""
    await inbound_mod._ensure_group(redis)
    handled = 0
    resp = await redis.xreadgroup(
        inbound_mod.GROUP, consumer, {inbound_mod.STREAM: ">"}, count=50, block=200
    )
    for _stream, entries in resp or []:
        for entry_id, fields in entries:
            await inbound_mod._handle_entry(ctx, entry_id, fields)
            handled += 1
    return handled


async def collect_events(pubsub, *, at_most: int, timeout: float = 2.0) -> list[dict]:
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(events) < at_most:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if msg and msg["type"] == "message":
            events.append(json.loads(msg["data"]))
    return events


async def fetch_all(pg_sessionmaker, stmt) -> list:
    async with pg_sessionmaker() as s:
        return list((await s.execute(stmt)).scalars())


# --------------------------------------------------------------------- tests


async def test_int1_full_pipeline(api, redis, ctx, account, pg_sessionmaker, sql_tally):
    """INT-1: POST → мгновенный 200 → стрим → консьюмер → БД → Pub/Sub → ack."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    sql_tally.reset()
    r = await api.post(
        f"/api/hooks/avito/{account.id}?secret=whsec-int",
        json=webhook_payload(chat_id="chat-int1", message_id="am-int1", author_id=999001),
    )
    assert r.status_code == 200 and r.json() == {"ok": True}
    assert await redis.xlen(inbound_mod.STREAM) == 1

    # Шлюз отдал 200, НЕ СДЕЛАВ РАБОТЫ КОНВЕЙЕРА, — здесь это проверяется
    # счётом обращений, а не секундомером (см. GATEWAY_SQL_BUDGET).
    assert sql_tally.writes == [], (
        "шлюз вебхука пишет в PostgreSQL — приём стал синхронным:\n" + sql_tally.report()
    )
    assert len(sql_tally) == GATEWAY_SQL_BUDGET, (
        f"обращений к PostgreSQL на шлюзе {len(sql_tally)}, "
        f"бюджет {GATEWAY_SQL_BUDGET}:\n{sql_tally.report()}"
    )
    # И то же самое со стороны данных: до прогона консьюмера в базе пусто.
    for model in (Client, Conversation, Message, WebhookRawLog):
        rows = await fetch_all(pg_sessionmaker, select(model))
        assert rows == [], f"{model.__tablename__} заполнена ещё до консьюмера: приём ждал БД"

    sql_tally.reset()
    assert await run_consumer(ctx, redis) == 1
    events = await collect_events(pubsub, at_most=2)
    assert len(sql_tally) <= CONSUMER_SQL_BUDGET, (
        f"одно входящее сообщение стоит {len(sql_tally)} обращений к PostgreSQL "
        f"при бюджете {CONSUMER_SQL_BUDGET} — похоже на N+1:\n{sql_tally.report()}"
    )

    # БД: клиент, диалог status=new, сообщение direction=in
    (client_row,) = await fetch_all(pg_sessionmaker, select(Client))
    assert client_row.external_id == "999001"
    (conv,) = await fetch_all(pg_sessionmaker, select(Conversation))
    assert conv.status == "new"
    assert conv.external_chat_id == "chat-int1"
    assert conv.unread_count == 1
    (msg,) = await fetch_all(pg_sessionmaker, select(Message))
    assert msg.direction == "in"
    assert msg.external_message_id == "am-int1"

    # Pub/Sub: конверт message:new (01 §11.3)
    assert events and events[0]["type"] == "message:new"
    assert events[0]["data"]["conversation_id"] == str(conv.id)
    assert events[0]["data"]["conversation_patch"]["unread_delta"] == 1  # 01 §11.3

    # 7.1: тем же трактом диалог встал в очередь «Входящие» — на настоящем
    # PostgreSQL, с настоящим `text[]` и настоящей колонкой `offered_at`.
    assert conv.offered_at is not None, "диалог из Авито не попал в очередь"
    assert conv.claimed_by_id is None and list(conv.declined_by) == []
    queue_frame = next(e for e in events if e["type"] == "inbox:new")
    assert queue_frame["data"]["conversation"]["id"] == str(conv.id)
    assert queue_frame["data"]["conversation"]["in_inbox"] is True

    # ack: PEL пуст
    pending = await redis.xpending(inbound_mod.STREAM, inbound_mod.GROUP)
    assert pending["pending"] == 0

    # raw-лог сохранён и помечен обработанным
    (raw,) = await fetch_all(pg_sessionmaker, select(WebhookRawLog))
    assert raw.account_id == account.id and raw.processed is True

    # INT-9 (срез): русский FTS находит сообщение
    async with pg_sessionmaker() as s:
        found = (
            await s.execute(
                text(
                    "SELECT count(*) FROM messages "
                    "WHERE search @@ websearch_to_tsquery('russian', 'экран')"
                )
            )
        ).scalar_one()
    assert found == 1


async def test_int2_duplicate_webhooks(api, redis, ctx, account, pg_sessionmaker):
    """INT-2: ретраи Авито — ровно одна строка, ровно одно событие, всё ack'нуто."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    payload = webhook_payload(chat_id="chat-int2", message_id="am-int2", author_id=999002)
    for _ in range(3):
        r = await api.post(f"/api/hooks/avito/{account.id}?secret=whsec-int", json=payload)
        assert r.status_code == 200
    assert await run_consumer(ctx, redis) == 3

    assert len(await fetch_all(pg_sessionmaker, select(Message))) == 1
    # Два кадра на первый вебхук (сообщение + строка очереди 7.1) и ни одного
    # на два ретрая: дубль не звенит операторам и не двоит строку в очереди.
    events = await collect_events(pubsub, at_most=4, timeout=1.0)
    assert [e["type"] for e in events] == ["message:new", "inbox:new"]
    pending = await redis.xpending(inbound_mod.STREAM, inbound_mod.GROUP)
    assert pending["pending"] == 0  # все три entry ack'нуты
    # сырец каждого ретрая сохранён (разные stream_id)
    assert len(await fetch_all(pg_sessionmaker, select(WebhookRawLog))) == 3


async def test_int4_client_returns_to_closed_conversation(
    api, redis, ctx, account, pg_sessionmaker
):
    """INT-4: closed + вебхук клиента -> status new, assignee NULL + audit-пара."""
    await api.post(
        f"/api/hooks/avito/{account.id}?secret=whsec-int",
        json=webhook_payload(chat_id="chat-int4", message_id="am-int4-1", author_id=999004),
    )
    await run_consumer(ctx, redis)
    async with pg_sessionmaker() as s:
        await s.execute(text("UPDATE conversations SET status = 'closed'"))
        await s.commit()

    await api.post(
        f"/api/hooks/avito/{account.id}?secret=whsec-int",
        json=webhook_payload(
            chat_id="chat-int4",
            message_id="am-int4-2",
            author_id=999004,
            created=NOW + timedelta(minutes=1),
        ),
    )
    await run_consumer(ctx, redis)

    (conv,) = await fetch_all(pg_sessionmaker, select(Conversation))
    assert conv.status == "new"
    assert conv.assignee_id is None
    actions = await fetch_all(pg_sessionmaker, select(AuditLog.action))
    assert "conversation.reopened" in actions
    assert "conversation.status_changed" in actions


async def test_int5_own_echo_mirrored_not_queued(api, redis, ctx, account, pg_sessionmaker):
    """INT-5: исходящее с самого аккаунта ЗЕРКАЛИТСЯ, но очередь не поднимает.

    ⚠ СТРАЖ ПЕРЕВЁРНУТ ОСОЗНАННО (аудит 19.08). Прежняя редакция требовала
    «сообщений нет вовсе» и была верна до зеркала Jivo: тогда любое исходящее
    с аккаунта считалось нашим эхом и выбрасывалось. С появлением зеркала
    (`_apply_external_outgoing`) решение снято владельцем: ответ, отправленный
    клиенту из приложения Авито или Jivo, ОБЯЗАН быть виден в LeadChat — иначе
    диспетчер не знает, что клиенту уже ответили, и пишет второй раз.

    Тест был КРАСНЫМ на main и этого никто не видел: интеграционный набор без
    Docker молча пропускается, а `ship.sh` его не гоняет вовсе (находка L-011).

    Теперь проверяется настоящий инвариант: сообщение сохранено как исходящее
    без нашего автора, диалог создан закрытым и в очередь не встал — работы
    он не требует, потому что ответ уже ушёл.
    """
    await api.post(
        f"/api/hooks/avito/{account.id}?secret=whsec-int",
        json=webhook_payload(
            chat_id="chat-int5", message_id="am-int5", author_id=account.avito_user_id
        ),
    )
    assert await run_consumer(ctx, redis) == 1

    сообщения = await fetch_all(pg_sessionmaker, select(Message))
    assert len(сообщения) == 1, "ответ, ушедший снаружи, обязан быть виден в LeadChat"
    зеркальное = сообщения[0]
    assert зеркальное.direction == "out"
    assert зеркальное.sender_user_id is None, "автор не наш пользователь — это ответ снаружи"

    (conv,) = await fetch_all(
        pg_sessionmaker, select(Conversation).where(Conversation.external_chat_id == "chat-int5")
    )
    assert conv.offered_at is None, "зеркальный ответ не ставит диалог в очередь"
    assert conv.assignee_id is None

    pending = await redis.xpending(inbound_mod.STREAM, inbound_mod.GROUP)
    assert pending["pending"] == 0


async def test_int3_reconciliation_recovers_missing(
    redis, ctx, account, pg_sessionmaker, monkeypatch
):
    """INT-3: 5 сообщений в «Авито», 2 уже у нас — догоняются ровно 3;
    повторный прогон не создаёт ничего."""
    chat = SimpleNamespace(
        external_chat_id="chat-int3",
        has_unread=True,
        last_message_at=None,
        item_title="Ремонт iPhone 13",
        item_url=None,
        item_price=None,
        client_external_id="999003",
        client_name="Иван Реконсиляция",
    )
    history = [
        InboundEvent(
            external_chat_id="chat-int3",
            external_message_id=f"am-int3-{n}",
            author_id=999003,
            account_user_id=account.avito_user_id,
            text=f"история {n}",
            created_at=NOW + timedelta(minutes=n),
            client_name="Иван Реконсиляция",
        )
        for n in range(1, 6)
    ]

    class FakeAdapter:
        async def fetch_chats(self, account_, *, unread_only=True):
            yield chat

        async def fetch_history(self, account_, chat_, *, since=None):
            for event in history:
                yield event

    monkeypatch.setattr(reconcile_mod, "get_adapter", lambda _ctx: FakeAdapter())

    # два сообщения уже в БД (пришли вебхуками раньше)
    for event in history[:2]:
        async with pg_sessionmaker() as db:
            assert await apply_inbound_event(db, redis, account, event, publish=False)

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    result = await reconcile_mod.reconcile_account(ctx, account.id)
    # `history_imported` — сообщения СТАРШЕ момента подключения канала: они
    # приезжают исторической дверью (архив, без очереди и без бота, docs/41
    # §11). Здесь вся переписка свежая, поэтому счётчик нулевой, и это тоже
    # часть контракта: сегодняшнее пропавшее сообщение обязано остаться
    # работой, а не превратиться в историю.
    assert result == {
        "chats_checked": 1,
        # Сбойные чаты считаются ОТДЕЛЬНО с 28.08. Отказ Авито на одном чате
        # больше не уносит прогон канала (в бою это стоило 22 смерти из 291
        # прогонов, и чаты за сбойным могли не проверяться никогда) — но и
        # пропадать из отчёта он не должен: молчаливый пропуск неотличим от
        # успеха.
        "chats_failed": 0,
        # Чаты, отсечённые нижней границей обхода (08.09). Здесь чат один и
        # свежий, поэтому ноль — но счётчик в отчёте обязан быть: по нему
        # видно, сколько работы окно снимает на бою.
        "chats_old": 0,
        "messages_recovered": 3,
        "history_imported": 0,
    }
    assert len(await fetch_all(pg_sessionmaker, select(Message))) == 5
    (conv,) = await fetch_all(pg_sessionmaker, select(Conversation))
    assert conv.last_message_at == NOW + timedelta(minutes=5)
    # события опубликованы только для реально добавленных
    events = await collect_events(pubsub, at_most=4, timeout=1.0)
    assert len(events) == 3

    # повторный прогон — 0 новых строк (ассерт INT-3)
    result2 = await reconcile_mod.reconcile_account(ctx, account.id)
    assert result2["messages_recovered"] == 0
    assert len(await fetch_all(pg_sessionmaker, select(Message))) == 5


async def test_reconciliation_skips_inactive_account(ctx, account, pg_sessionmaker):
    async with pg_sessionmaker() as s:
        await s.execute(text("UPDATE avito_accounts SET status = 'needs_reauth'"))
        await s.commit()
    assert await reconcile_mod.reconcile_account(ctx, account.id) == {"skipped": True}


async def test_garbage_payload_logged_and_acked(api, redis, ctx, account, pg_sessionmaker):
    """Мусорный payload: 200 на gateway, сырец в логе с ошибкой, entry ack'нут."""
    r = await api.post(
        f"/api/hooks/avito/{account.id}?secret=whsec-int",
        content=b"this is not json at all",
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 200  # gateway всегда 200 после XADD (01 §10)
    assert await run_consumer(ctx, redis) == 1
    (raw,) = await fetch_all(pg_sessionmaker, select(WebhookRawLog))
    assert raw.processed is False and raw.error == "invalid_json"
    pending = await redis.xpending(inbound_mod.STREAM, inbound_mod.GROUP)
    assert pending["pending"] == 0
    assert await fetch_all(pg_sessionmaker, select(Message)) == []


async def test_int3_recovers_after_operator_reply(
    redis, ctx, account, pg_sessionmaker, monkeypatch
):
    """Регрессия (07 §5 сценарий 34): ответ менеджера НЕ прячет потерянное входящее.

    `conversations.last_message_at` двигает и наш исходящий, поэтому отсечка
    reconciliation по нему теряла клиентское сообщение навсегда: вебхук упал,
    менеджер ответил вслепую, `since` уехал выше пропущенного входящего — и оно
    не появлялось уже ни на одном прогоне. Отсечка считается по последнему
    сообщению КЛИЕНТА.
    """
    chat = SimpleNamespace(
        external_chat_id="chat-int3b",
        has_unread=True,
        last_message_at=None,
        item_title=None,
        item_url=None,
        item_price=None,
        client_external_id="999013",
        client_name="Клиент Регрессия",
    )

    def event(n: int) -> InboundEvent:
        return InboundEvent(
            external_chat_id="chat-int3b",
            external_message_id=f"am-int3b-{n}",
            author_id=999013,
            account_user_id=account.avito_user_id,
            text=f"клиент пишет {n}",
            created_at=NOW + timedelta(minutes=n),
            client_name="Клиент Регрессия",
        )

    class FakeAdapter:
        """`since` фейк обязан соблюдать: у боевого адаптера отсечка внутри
        ``fetch_history`` (workers/reconciliation.py, `_LiveAdapter`), и именно
        она решает, увидим мы пропущенное сообщение или нет."""

        async def fetch_chats(self, account_, *, unread_only=True):
            yield chat

        async def fetch_history(self, account_, chat_, *, since=None):
            for n in (1, 2):
                ev = event(n)
                if since is not None and ev.created_at <= reconcile_mod._aware(since):
                    continue
                yield ev

    monkeypatch.setattr(reconcile_mod, "get_adapter", lambda _ctx: FakeAdapter())

    # первое сообщение дошло вебхуком, второе — потеряно
    async with pg_sessionmaker() as db:
        assert await apply_inbound_event(db, redis, account, event(1), publish=False)

    # менеджер ответил вслепую: last_message_at уезжает ЗА потерянное входящее
    async with pg_sessionmaker() as db:
        conv = (await db.execute(select(Conversation))).scalars().one()
        db.add(
            Message(
                conversation_id=conv.id,
                direction="out",
                sender_type="operator",
                body="Здравствуйте!",
                created_at=NOW + timedelta(minutes=3),
                delivery_status="sent",
            )
        )
        conv.last_message_at = NOW + timedelta(minutes=3)
        await db.commit()

    result = await reconcile_mod.reconcile_account(ctx, account.id)
    assert result["messages_recovered"] == 1, "потерянное входящее обязано догнаться"
    bodies = [
        m.body
        for m in await fetch_all(pg_sessionmaker, select(Message).where(Message.direction == "in"))
    ]
    assert "клиент пишет 2" in bodies

    # идемпотентность сохраняется
    assert (await reconcile_mod.reconcile_account(ctx, account.id))["messages_recovered"] == 0


async def test_reconciliation_fills_empty_item(redis, ctx, account, pg_sessionmaker, monkeypatch):
    """Сверка дописывает объявление диалогу, у которого оно пусто.

    Вторая половина критичного дефекта docs/33 §14а: сверка тянула чаты с
    названием объявления, парсер его доставал — и результат выбрасывался,
    потому что строка диалога уже существует. Во всём коде было четыре
    присваивания item_title и ни одного обновления.
    """
    chat = SimpleNamespace(
        external_chat_id="chat-item-fill",
        has_unread=True,
        last_message_at=None,
        item_title="Ремонт стиральных машин на дому",
        item_url="https://avito.ru/item/4242",
        item_price="от 1 200 ₽",
        client_external_id="999042",
        client_name="Пётр",
    )
    # Диалог приезжает вебхуком БЕЗ объявления — так делает боевой Авито.
    first = InboundEvent(
        external_chat_id="chat-item-fill",
        external_message_id="am-item-1",
        author_id=999042,
        account_user_id=account.avito_user_id,
        text="Здравствуйте, машинка не отжимает",
        created_at=NOW,
        client_name="Пётр",
    )

    class FakeAdapter:
        async def fetch_chats(self, account_, *, unread_only=True):
            yield chat

        async def fetch_history(self, account_, chat_, *, since=None):
            return
            yield  # pragma: no cover — генератор без элементов

    monkeypatch.setattr(reconcile_mod, "get_adapter", lambda _ctx: FakeAdapter())

    async with pg_sessionmaker() as db:
        assert await apply_inbound_event(db, redis, account, first, publish=False)
    (before,) = await fetch_all(pg_sessionmaker, select(Conversation))
    assert before.item_title is None, "исходное состояние: объявления нет"

    await reconcile_mod.reconcile_account(ctx, account.id)

    (after,) = await fetch_all(pg_sessionmaker, select(Conversation))
    assert after.item_title == "Ремонт стиральных машин на дому"
    assert after.item_url == "https://avito.ru/item/4242"
    assert after.item_price == "от 1 200 ₽"


async def test_reconciliation_keeps_known_item(redis, ctx, account, pg_sessionmaker, monkeypatch):
    """Известное объявление сверка не перезаписывает."""
    chat = SimpleNamespace(
        external_chat_id="chat-item-keep",
        has_unread=True,
        last_message_at=None,
        item_title="Из Авито",
        item_url=None,
        item_price=None,
        client_external_id="999043",
        client_name="Анна",
    )
    first = InboundEvent(
        external_chat_id="chat-item-keep",
        external_message_id="am-keep-1",
        author_id=999043,
        account_user_id=account.avito_user_id,
        text="Добрый день",
        created_at=NOW,
        client_name="Анна",
        item_title="Уже знаем",
    )

    class FakeAdapter:
        async def fetch_chats(self, account_, *, unread_only=True):
            yield chat

        async def fetch_history(self, account_, chat_, *, since=None):
            return
            yield  # pragma: no cover

    monkeypatch.setattr(reconcile_mod, "get_adapter", lambda _ctx: FakeAdapter())

    async with pg_sessionmaker() as db:
        assert await apply_inbound_event(db, redis, account, first, publish=False)
    await reconcile_mod.reconcile_account(ctx, account.id)

    (after,) = await fetch_all(pg_sessionmaker, select(Conversation))
    assert after.item_title == "Уже знаем"


async def test_int6_call_into_new_chat_does_not_enter_the_queue(
    api, redis, ctx, account, pg_sessionmaker
):
    """INT-6: ЗВОНОК В НОВЫЙ ЧАТ создаёт диалог, но НЕ ставит его в очередь.

    ⚠⚠ СТРАЖ ПЕРЕВЁРНУТ ПО РЕШЕНИЮ ВЛАДЕЛЬЦА 19.08 — «пусть во входящие не
    приходят такие чаты». Он показал экран: в очереди диалог с тремя строками
    «Клиент звонил через приложение Авито», ни одного слова клиента, ждёт 42
    минуты. Прежнее правило (моё, от 17.08) считало такой звонок лидом.

    На деле звонки идут В ТЕЛЕФОН и там же обрабатываются, а в LeadChat от них
    остаётся только след: ни имени, ни вопроса, ни номера в самом чате —
    работать с ним нечем. Диалог создаётся и виден в «Все», чип звонка в ленте
    на месте, поиск находит. Он просто не требует хода. Напишет — встанет в
    очередь обычным путём.

    НИЖЕ — ПРЕЖНЕЕ ОБОСНОВАНИЕ, оно объясняет, почему кадр вообще существует:

    БОЕВОЙ СЛУЧАЙ 18.08 (находка L-001, два клиента за сутки). Клиент нашёл
    объявление и позвонил. Диалог заводился, а публикация кадра падала
    ``KeyError: 'id'``: в ``publish_inbox_new`` уезжал уже готовый конверт
    ``{"conversation_id": …, "conversation": …}``, тогда как функция ждёт САМУ
    строку очереди и заворачивает её сама (``app/ws/hub.py``). Обработчик
    входящих валился с ``webhook.process_failed``; при повторе диалог уже
    существовал, ветка звонка не срабатывала — и кадр не уходил НИКОГДА.
    У тринадцати диспетчеров не звенело и не росло, обращение находили
    только после обновления экрана.

    Тест держит ровно это свойство и падает на прежнем коде: событие
    ``inbox:new`` есть, и в нём — идентификатор диалога, а не пустота.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    payload = webhook_payload(
        chat_id="u2i-CALL-LEAD",
        message_id="call-lead-1",
        author_id=999006,
        text_="",
    )
    payload["payload"]["value"]["type"] = "appCall"
    payload["payload"]["value"]["content"] = {}

    resp = await api.post(f"/api/hooks/avito/{account.id}?secret=whsec-int", json=payload)
    assert resp.status_code == 200
    assert await run_consumer(ctx, redis) == 1

    events = await collect_events(pubsub, at_most=6)
    await pubsub.unsubscribe("events")
    кадры = [e for e in events if e.get("type") == "inbox:new"]
    assert not кадры, "звонок не должен звенеть в очереди: работать с ним нечем"

    диалоги = await fetch_all(
        pg_sessionmaker,
        select(Conversation).where(Conversation.external_chat_id == "u2i-CALL-LEAD"),
    )
    assert len(диалоги) == 1, "диалог всё равно создаётся: след звонка виден в «Все»"
    assert диалоги[0].status == "closed", "звонок не требует хода"
    assert диалоги[0].offered_at is None, "в очередь не встаёт"
    assert диалоги[0].awaiting_since is None, "ждать нечего: клиент ничего не спросил"


async def test_int7_external_answer_takes_dialog_out_of_queue(
    api, redis, ctx, account, pg_sessionmaker
):
    """INT-7: ответили клиенту СНАРУЖИ — диалог уходит из очереди и возвращается сам.

    БОЕВОЙ СЛУЧАЙ 18.08 (находка L-008). Клиент спросил про ремонт телевизора,
    из приложения Авито ему ответили шесть раз, а в LeadChat диалог час висел
    ``new``, без владельца, с истинным предикатом очереди — то есть у всех
    тринадцати диспетчеров как неразобранный. Любой мог ответить вторым
    голосом.

    Решение владельца 19.08: свежий ответ снаружи снимает диалог с очереди
    (он больше не требует НАШЕГО хода), а следующее сообщение клиента ставит
    его обратно. Вторая половина закрывает дыру, которая была и до зеркала:
    вернуть диалог в очередь умел единственный случай «закрыт → клиент
    написал снова», и диалог в статусе ``new`` без ``offered_at`` не видел
    никто.
    """
    # 1. клиент пишет — диалог встаёт в очередь
    r = await api.post(
        f"/api/hooks/avito/{account.id}?secret=whsec-int",
        json=webhook_payload(chat_id="chat-int7", message_id="am-int7-1", author_id=999007),
    )
    assert r.status_code == 200
    assert await run_consumer(ctx, redis) == 1
    (conv,) = await fetch_all(
        pg_sessionmaker, select(Conversation).where(Conversation.external_chat_id == "chat-int7")
    )
    assert conv.offered_at is not None, "диалог обязан встать в очередь"

    # 2. ответ СНАРУЖИ: исходящее с самого аккаунта, не из LeadChat
    await api.post(
        f"/api/hooks/avito/{account.id}?secret=whsec-int",
        json=webhook_payload(
            chat_id="chat-int7",
            message_id="am-int7-out",
            author_id=account.avito_user_id,
            text_="Здравствуйте, подъеду сегодня к 17:00",
        ),
    )
    assert await run_consumer(ctx, redis) == 1
    (conv,) = await fetch_all(
        pg_sessionmaker, select(Conversation).where(Conversation.external_chat_id == "chat-int7")
    )
    assert conv.offered_at is None, (
        "на диалог ответили снаружи — он не должен висеть в очереди как неразобранный"
    )
    assert conv.assignee_id is None, "владельца при этом не назначаем: человек не брал диалог"
    assert conv.awaiting_since is None, "клиент больше не ждёт нашего хода"

    # 3. клиент пишет снова — диалог ОБЯЗАН вернуться в очередь
    await api.post(
        f"/api/hooks/avito/{account.id}?secret=whsec-int",
        json=webhook_payload(
            chat_id="chat-int7",
            message_id="am-int7-2",
            author_id=999007,
            text_="А раньше никак?",
        ),
    )
    assert await run_consumer(ctx, redis) == 1
    (conv,) = await fetch_all(
        pg_sessionmaker, select(Conversation).where(Conversation.external_chat_id == "chat-int7")
    )
    assert conv.offered_at is not None, (
        "клиент написал снова, а диалога нет ни в очереди, ни у кого — это чёрная дыра"
    )
    assert conv.awaiting_since is not None, "клиент снова ждёт ответа"


async def test_int8_historic_call_does_not_flood_the_queue(
    api, redis, ctx, account, pg_sessionmaker
):
    """INT-8: звонок ИЗ ИСТОРИИ не встаёт в очередь, живой — встаёт.

    БОЕВОЙ СЛУЧАЙ 19.08. Владелец подключил восемь каналов и запустил загрузку
    истории. Она подняла ВСЕ звонки за всё время существования кабинетов и
    каждый поставила в очередь как новое обращение: 4611 диалогов, в которых
    нет ни одного слова клиента — одни служебные записи «Клиент звонил через
    приложение Авито». Очередь перестала быть очередью, а 172 настоящих
    обращения утонули в этом потоке.

    Живой звонок при этом остаётся лидом — человек позвонил сейчас, ему надо
    перезвонить. Разницу знает только флаг загрузки истории.
    """
    from app.services.inbound import apply_inbound_event

    def звонок(chat: str, msg: str):
        payload = webhook_payload(chat_id=chat, message_id=msg, author_id=999008, text_="")
        value = payload["payload"]["value"]
        value["type"] = "appCall"
        value["content"] = {}
        return SimpleNamespace(
            chat_id=chat,
            message_id=msg,
            author_id=999008,
            account_user_id=account.avito_user_id,
            text="",
            created_at=NOW,
            kind="message",
            source_type="appCall",
            external_chat_id=chat,
            external_message_id=msg,
            attachments=[],
            is_system=True,
        )

    async with pg_sessionmaker() as db:
        await apply_inbound_event(
            db, redis, account, звонок("chat-hist", "call-hist"), backfill=True
        )
        await apply_inbound_event(db, redis, account, звонок("chat-live", "call-live"))

    история = await fetch_all(
        pg_sessionmaker, select(Conversation).where(Conversation.external_chat_id == "chat-hist")
    )
    живой = await fetch_all(
        pg_sessionmaker, select(Conversation).where(Conversation.external_chat_id == "chat-live")
    )
    assert len(история) == 1 and len(живой) == 1

    assert история[0].status == "closed", "звонок из истории не работа — он не требует ответа"
    assert история[0].offered_at is None, "история не должна стоять в очереди"
    assert история[0].awaiting_since is None, "у звонка годовой давности нечего ждать"

    # ⚠ 19.08 владелец отменил и это: живой звонок тоже не встаёт в очередь.
    # Разницы между историческим и живым звонком больше нет — работать нечем
    # ни с тем, ни с другим, пока клиент не написал.
    assert живой[0].status == "closed", "звонок без слов клиента — не работа"
    assert живой[0].offered_at is None
