"""Обвязка движка ботов: монтаж, регистрация задач, точка запуска (спринт 6).

Движок может быть сколь угодно правильным — если роутер не смонтирован,
задачи не зарегистрированы в ARQ, а входящий конвейер бота не зовёт, на проде
не работает ничего и виноватого не видно нигде. Этот файл — сторож ровно
четырёх швов между зонами:

* `app/main.py` -> роутер ботов (01 §8);
* `app/workers/main.py` -> `bot_step` / `bot_ask_timeout` (02 §2.3–2.4);
* `app/services/inbound.py` -> `should_run_bot` + `enqueue_bot_step` (DESIGN §8.3,
  02 §2.2);
* `app/services/messages.py` -> `mute_bot` (02 §2.6, проверен в
  tests/unit/test_send_message.py).
"""

import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import AvitoAccount, Bot, Conversation, Message
from app.services.inbound import apply_inbound_event

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

AVITO_USER_ID = 111222333
ARQ_QUEUE = "arq:queue"


async def шагов_бота(redis) -> int:
    """Сколько в очереди задач БОТА — а не сколько в ней задач вообще.

    ⚠ ЗАЧЕМ ОТДЕЛЬНЫЙ СЧЁТ. Раньше во всех проверках ниже стояло
    `zcard(ARQ_QUEUE)`: длина всей очереди как замена вопросу «разбудили ли
    бота». Пока входящее ставило одну задачу, это совпадало — и сломалось
    02.09, когда у обогащения появился третий повод (ссылка на профиль
    клиента). Пять проверок покраснели, хотя про бота не изменилось ничего:
    они проверяли соседа.

    ⚠ ОТЛИЧАЕМ ПО ИДЕНТИФИКАТОРУ, А НЕ ПО СОДЕРЖИМОМУ. Имя функции лежит внутри
    задачи в упакованном виде, и клиент этого теста читает ответы как utf-8 —
    на упакованных байтах он падает. У обогащения идентификатор задан явно и
    детерминирован (`enrich:{conversation_id}`, `client_enrich.ENRICH_JOB`),
    поэтому отделить его от прочего можно по префиксу.

    ⚠ ЧЕСТНАЯ ОГОВОРКА. Считается «всё, кроме обогащения». На пути входящего
    сообщения других производителей задач нет, поэтому здесь это равно числу
    задач бота; появится третий — проверку придётся сузить, и упадёт она
    заметно, а не молча пропустит.
    """
    сколько = 0
    for job_id in await redis.zrange(ARQ_QUEUE, 0, -1):
        ключ = job_id.decode() if isinstance(job_id, bytes) else str(job_id)
        if not ключ.startswith("enrich:"):
            сколько += 1
    return сколько


T0 = datetime(2026, 8, 4, 10, 0, 0, tzinfo=UTC)

SCENARIO = {
    "version": 1,
    "revision": 1,
    "entry": "greet",
    "steps": [{"id": "greet", "type": "send", "params": {"text": "Здравствуйте!"}}],
}


# --- 1. роутер ботов смонтирован ---------------------------------------------


def test_bots_router_is_mounted(app):
    """01 §8: без монтажа на экран ботов нельзя попасть вообще."""
    paths = app.openapi()["paths"]
    assert "/api/v1/bots" in paths, "роутер ботов не смонтирован в app/main.py"
    # Без дополнительного prefix: пути внутри модуля уже начинаются с /bots.
    assert "/api/v1/bots/bots" not in paths
    assert {"/api/v1/bots/{bot_id}", "/api/v1/bots/sandbox/start"} <= set(paths)


async def test_bots_endpoints_answer_401_not_404(client):
    """Смонтирован — значит отвечает про авторизацию, а не «нет такой ручки»."""
    r = await client.get("/api/v1/bots")
    assert r.status_code == 401, r.text
    assert r.json()["error"]["code"] == "unauthorized"


# --- 2. задачи движка зарегистрированы в ARQ ---------------------------------


def test_bot_jobs_are_registered_in_the_worker():
    """Без этих строк задачи ставятся в очередь и висят в pending навсегда."""
    from app.bots.runtime import BOT_STEP_JOB, BOT_TIMEOUT_JOB, bot_ask_timeout, bot_step
    from app.workers.main import WorkerSettings

    assert bot_step in WorkerSettings.functions
    assert bot_ask_timeout in WorkerSettings.functions
    # ARQ ищет задачу по имени — оно обязано совпадать с тем, что кладут в
    # очередь движок и рантайм. Имя спрашиваем у самого воркера: задача может
    # быть записана и корутиной, и `Function` со своими настройками.
    from app.workers.main import registered_job_names

    assert {BOT_STEP_JOB, BOT_TIMEOUT_JOB} <= registered_job_names()


# --- 3. входящий конвейер зовёт бота -----------------------------------------


def make_event(**kw):
    defaults = {
        "external_chat_id": "chat-bot-1",
        "external_message_id": "am-bot-1",
        "author_id": 999001,
        "account_user_id": AVITO_USER_ID,
        "text": "Здравствуйте! Разбит экран",
        "created_at": T0,
        "client_name": "Иван Петров",  # имя есть -> задача enrich_client не ставится
        "item_title": "Ремонт iPhone 13",
        "item_url": "https://avito.ru/item/1",
        "item_price": "от 1500 ₽",
    }
    defaults.update(kw)
    return InboundEvent(**defaults)


@pytest.fixture
async def bot_account(db_sessionmaker, make_avito_account) -> AvitoAccount:
    """Аккаунт с привязанным включённым круглосуточным ботом."""
    account = await make_avito_account(AVITO_USER_ID)
    async with db_sessionmaker() as session:
        bot = Bot(
            id=uuid.uuid4(),
            name="Первичный приём",
            is_enabled=True,
            schedule={"always": True},
            scenario=SCENARIO,
            knowledge_base=None,
            mode="auto",  # тест про доставку клиенту; умолчание в базе — «подсказка»
        )
        session.add(bot)
        await session.flush()
        row = await session.get(AvitoAccount, account.id)
        assert row is not None
        row.bot_id = bot.id
        await session.commit()
        await session.refresh(row)
        return row


async def test_inbound_enqueues_the_bot_step(db, redis, bot_account, db_sessionmaker):
    """DESIGN §8.3: сообщение записано -> бот вызван, строго ПОСЛЕ commit'а."""
    assert await apply_inbound_event(db, redis, bot_account, make_event()) is True

    assert await шагов_бота(redis) == 1, "bot_step не поставлен в очередь"
    # Сообщение уже в БД к моменту постановки — иначе воркер прочитает пустоту.
    async with db_sessionmaker() as s:
        assert (await s.execute(select(Message))).scalars().first() is not None


async def test_inbound_passes_the_conversation_and_text(db, redis, bot_account, monkeypatch):
    """Движку нужен текст входящего: без него тик считается таймаутом."""
    seen: dict = {}

    async def spy(redis_, conversation_id, incoming_text, **kwargs):
        seen["conversation_id"] = conversation_id
        seen["text"] = incoming_text

    monkeypatch.setattr("app.bots.runtime.enqueue_bot_step", spy)
    await apply_inbound_event(db, redis, bot_account, make_event(text="Разбит экран"))

    assert seen["text"] == "Разбит экран"
    assert isinstance(seen["conversation_id"], uuid.UUID)


async def test_inbound_leaves_no_open_transaction(db, redis, bot_account):
    """05 §8: SELECT'ы бота закрывают свою транзакцию ДО похода в Redis."""
    assert await apply_inbound_event(db, redis, bot_account, make_event()) is True
    assert db.in_transaction() is False


async def test_backfill_never_wakes_the_bot(db, redis, bot_account):
    """Решение №3: история не должна разбудить бота на годовалой переписке."""
    assert (
        await apply_inbound_event(
            db, redis, bot_account, make_event(), backfill=True, publish=False
        )
        is True
    )
    assert await шагов_бота(redis) == 0


async def test_muted_conversation_does_not_wake_the_bot(db, redis, bot_account, db_sessionmaker):
    """02 §2.6: оператор вмешивался — бот молчит навсегда, даже на новом входящем."""
    assert await apply_inbound_event(db, redis, bot_account, make_event()) is True
    async with db_sessionmaker() as s:
        conv = (await s.execute(select(Conversation))).scalar_one()
        conv.bot_vars = {"muted": True}
        conv.bot_active = False
        await s.commit()

    await redis.delete(ARQ_QUEUE)
    assert (
        await apply_inbound_event(db, redis, bot_account, make_event(external_message_id="am-2"))
        is True
    )
    assert await шагов_бота(redis) == 0


async def test_account_without_a_bot_enqueues_nothing(db, redis, make_avito_account):
    account = await make_avito_account(AVITO_USER_ID)
    assert await apply_inbound_event(db, redis, account, make_event()) is True
    assert await шагов_бота(redis) == 0


async def test_duplicate_does_not_wake_the_bot_twice(db, redis, bot_account):
    """Ретрай вебхука — не новое сообщение: второй тик бота не нужен."""
    assert await apply_inbound_event(db, redis, bot_account, make_event()) is True
    await redis.delete(ARQ_QUEUE)
    assert await apply_inbound_event(db, redis, bot_account, make_event()) is False
    assert await шагов_бота(redis) == 0


async def test_broken_bot_zone_never_loses_the_webhook(
    db, redis, bot_account, monkeypatch, db_sessionmaker
):
    """Бот — не повод потерять входящее: сообщение уже закоммичено."""

    async def boom(*args, **kwargs):
        raise RuntimeError("движок недоступен")

    monkeypatch.setattr("app.bots.runtime.should_run_bot", boom)
    assert await apply_inbound_event(db, redis, bot_account, make_event()) is True

    async with db_sessionmaker() as s:
        assert (await s.execute(select(Message))).scalars().first() is not None


# --- 4. метки времени бота (миграция 0005) -----------------------------------


async def test_bot_model_has_the_migration_0005_timestamps(db_sessionmaker):
    """01 §8.1 показывает «когда изменён»; дата обязана сходиться с audit."""
    async with db_sessionmaker() as session:
        bot = Bot(id=uuid.uuid4(), name="Тест", schedule={"always": True}, scenario=SCENARIO)
        session.add(bot)
        await session.commit()
        await session.refresh(bot)
        created, updated = bot.created_at, bot.updated_at
        assert created is not None and updated is not None

        bot.name = "Тест 2"
        await session.commit()
        await session.refresh(bot)
        # onupdate двигает метку на любом UPDATE строки бота.
        assert bot.updated_at >= updated
        assert bot.created_at == created
