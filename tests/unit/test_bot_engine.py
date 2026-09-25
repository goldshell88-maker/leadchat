"""Движок ботов на сценариях-фикстурах — обязательная таблица 07 §1.1.

Каждый кейс таблицы имеет здесь свой тест (ссылка на строку — в докстринге),
плюс покрыты предохранители зацикливания (02 §2.7), таймауты `ask` (02 §2.4),
`should_run_bot` (02 §2.2) и чистые функции шагов (02 §1.2–1.3).

Инфраструктура — юнит-уровня (07 §1.1): SQLite вместо Postgres, fakeredis
вместо Redis, AI — детерминированная заглушка. Время подаётся движку явно
(`ctx["bot_now"]`), поэтому `time_machine` не нужен.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bots.runtime import (
    bot_ask_timeout,
    bot_step,
    maybe_enqueue_bot_step,
    mute_bot,
    should_run_bot,
)
from app.bots.schedule import resolve_tz
from app.bots.state import BotState, HandoffInfo, Waiting
from app.bots.steps import (
    Limits,
    Rendered,
    Scenario,
    Step,
    detect_human_request,
    evaluate_condition,
    mask_phones,
    match_menu_option,
    parse_timeout,
    pick_condition_branch,
    render_text,
    validate_answer,
)
from app.bots.steps import render as render_step_text
from app.models import AuditLog, AvitoAccount, Bot, Client, Conversation, Message
from app.models.notification import Notification
from tests.unit.conftest import drain_events

MSK = resolve_tz("Europe/Moscow")
DAY = datetime(2026, 8, 4, 12, 0, tzinfo=MSK)  # вторник, рабочее время
NIGHT = datetime(2026, 8, 4, 22, 30, tzinfo=MSK)  # кейс «Первичный приём, ночь»

# ---------------------------------------------------------------- сценарии

# «Первичный приём» (02 §1.5). Ветка таймаута ведёт на handoff, а не на
# `close`: автоматического закрытия диалогов у нас нет (решение владельца №1).
PRIMARY_INTAKE: dict[str, Any] = {
    "version": 1,
    "revision": 3,
    "entry": "greet",
    "settings": {"max_bot_messages_row": 5, "max_offscript_messages": 2},
    "steps": [
        {
            "id": "greet",
            "type": "send",
            "params": {
                "text": "Здравствуйте, {client_name}! Это сервис Lead Partner 👋\n"
                "Подскажите, что случилось с техникой — модель и проблему?"
            },
            "next": "ask_problem",
        },
        {
            "id": "ask_problem",
            "type": "ask",
            "params": {
                "text": None,
                "var": "problem",
                "validate": "any",
                "retry_text": None,
                "max_attempts": 1,
                "timeout": "24h",
            },
            "next": "ai_draft",
            "on_timeout": "handoff_no_reply",
            "on_invalid": None,
        },
        {
            "id": "ai_draft",
            "type": "ai_answer",
            "params": {"confidence_threshold": 0.6, "max_reply_len": 800, "context_messages": 10},
            "next": "check_hours",
            "on_low_confidence": None,
        },
        {
            "id": "check_hours",
            "type": "condition",
            "params": {
                "conditions": [
                    {
                        "if": {
                            "kind": "work_hours",
                            "from": "10:00",
                            "to": "20:00",
                            "timezone": "Europe/Moscow",
                        },
                        "next": "handoff_day",
                    }
                ],
                "else": "night_msg",
            },
        },
        {
            "id": "handoff_day",
            "type": "handoff",
            "params": {
                "reason": "scenario",
                "comment": "Клиент описал проблему, бот дал предварительный ответ",
                "tags": ["первичный-приём"],
            },
        },
        {
            "id": "night_msg",
            "type": "send",
            "params": {"text": "Мастер ответит утром. Оставьте телефон — перезвоним первыми ✔"},
            "next": "ask_phone",
        },
        {
            "id": "ask_phone",
            "type": "ask",
            "params": {
                "text": None,
                "var": "phone",
                "validate": "phone",
                "retry_text": "Кажется, это не номер телефона 🙂 Напишите +7 900 000-00-00",
                "max_attempts": 2,
                "timeout": "12h",
            },
            "next": "tag_contact",
            "on_timeout": "handoff_night",
            "on_invalid": "handoff_night",
        },
        {
            "id": "tag_contact",
            "type": "tag",
            "params": {"tags": ["контакт собран"]},
            "next": "note_contact",
        },
        {
            "id": "note_contact",
            "type": "note",
            "params": {
                "text": "🤖 Бот собрал контакт: {phone}\nПроблема со слов клиента: {problem}"
            },
            "next": "handoff_night",
        },
        {
            "id": "handoff_night",
            "type": "handoff",
            "params": {
                "reason": "scenario",
                "comment": "Ночной диалог: перезвонить утром первыми",
                "tags": ["ночной-лид"],
            },
        },
        {
            "id": "handoff_no_reply",
            "type": "handoff",
            "params": {"reason": "scenario", "comment": "Клиент не ответил"},
        },
    ],
}

MENU_SCENARIO: dict[str, Any] = {
    "version": 1,
    "revision": 1,
    "entry": "what_device",
    "settings": {"max_offscript_messages": 2},
    "steps": [
        {
            "id": "what_device",
            "type": "menu",
            "params": {
                "text": "Какая техника?\n1. Телефон\n2. Ноутбук",
                "var": "device_kind",
                "options": [
                    {
                        "id": "mobile",
                        "label": "Телефон",
                        "match": ["1", "телефон", "айфон"],
                        "next": "handoff_final",
                    },
                    {
                        "id": "laptop",
                        "label": "Ноутбук",
                        "match": ["2", "ноутбук"],
                        "next": "handoff_final",
                    },
                ],
                "retry_text": "Пожалуйста, ответьте цифрой 1 или 2 🙂",
                "max_attempts": 5,
                "timeout": "24h",
            },
            "on_no_match": None,
            "on_timeout": None,
        },
        {"id": "handoff_final", "type": "handoff", "params": {"reason": "scenario"}},
    ],
}

CLOSE_SCENARIO: dict[str, Any] = {
    "version": 1,
    "revision": 1,
    "entry": "bye",
    "steps": [
        {
            "id": "bye",
            "type": "close",
            "params": {"text": "Спасибо за обращение!", "silent": False},
        }
    ],
}

VARS_SCENARIO: dict[str, Any] = {
    "version": 1,
    "revision": 1,
    "entry": "greet",
    "steps": [
        {
            "id": "greet",
            "type": "send",
            "params": {"text": "Здравствуйте, {client_name}! Вы писали по «{item_title}»."},
            "next": "final",
        },
        {"id": "final", "type": "handoff", "params": {"reason": "scenario"}},
    ],
}


def loop_scenario(**settings: int) -> dict[str, Any]:
    """Цикл `tag -> tag -> …` без единого `ask`: ловит его только рантайм."""
    return {
        "version": 1,
        "revision": 1,
        "entry": "a",
        "settings": settings,
        "steps": [
            {"id": "a", "type": "tag", "params": {"tags": ["a"]}, "next": "b"},
            {"id": "b", "type": "tag", "params": {"tags": ["b"]}, "next": "a"},
        ],
    }


def chatty_scenario() -> dict[str, Any]:
    """Бот заваливает клиента сообщениями — предохранитель `bot_msgs_row`."""
    return {
        "version": 1,
        "revision": 1,
        "entry": "s1",
        "settings": {"max_bot_messages_row": 2, "max_steps_per_tick": 50},
        "steps": [
            {"id": "s1", "type": "send", "params": {"text": "раз"}, "next": "s2"},
            {"id": "s2", "type": "send", "params": {"text": "два"}, "next": "s1"},
        ],
    }


# ------------------------------------------------------------- AI-заглушка


class StubAI:
    """Детерминированная замена `app/bots/ai.py` (02 §3): ничего не сеть."""

    def __init__(
        self,
        *,
        answer: dict[str, Any] | None = None,
        answer_error: Exception | None = None,
        classify: Any = None,
        entities: dict[str, Any] | None = None,
    ) -> None:
        self.answer = (
            answer
            if answer is not None
            else {
                "reply": "Похоже на замену экрана, ориентировочно от 8900 ₽.",
                "confidence": 0.9,
                "needs_operator": False,
            }
        )
        self.answer_error = answer_error
        self.classify = classify
        self.entities = entities
        self.dialogs: list[list[dict[str, Any]]] = []
        self.classified: list[list[str]] = []

    async def ai_answer(self, bot, dialog, item_title, **_kw):  # noqa: ANN001
        self.dialogs.append(dialog)
        if self.answer_error is not None:
            raise self.answer_error
        return self.answer

    async def classify_message(self, texts):  # noqa: ANN001
        self.classified.append(texts)
        # `classify` может быть функцией: удобно, когда негатив должен
        # появиться не с первого сообщения клиента.
        return self.classify(texts) if callable(self.classify) else self.classify

    async def extract_entities(self, texts):  # noqa: ANN001
        return self.entities


# ---------------------------------------------------------------- фикстуры


@pytest.fixture
def make_bot(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[Bot]]:
    async def _make(
        scenario: dict[str, Any] | None = None,
        *,
        schedule: dict[str, Any] | None = None,
        is_enabled: bool = True,
        knowledge_base: str | None = "Замена экрана iPhone 13 — от 8900 ₽.",
        name: str = "Первичный приём",
        # ⚠ Умолчание В БАЗЕ — «подсказка» (миграция 0035): бот выходит на живой аккаунт
        # молча, и только осознанным переключением начинает отвечать клиенту сам. Тесты
        # движка проверяют ИМЕННО отправку, поэтому здесь режим автоответа задан явно;
        # поведение подсказки проверяется отдельно, в test_bot_mode.py.
        mode: str = "auto",
    ) -> Bot:
        async with db_sessionmaker() as s:
            bot = Bot(
                name=name,
                is_enabled=is_enabled,
                schedule=schedule or {"always": True},
                scenario=scenario if scenario is not None else PRIMARY_INTAKE,
                knowledge_base=knowledge_base,
                mode=mode,
            )
            s.add(bot)
            await s.commit()
            await s.refresh(bot)
            return bot

    return _make


@pytest.fixture
def make_world(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_bot: Callable[..., Awaitable[Bot]],
) -> Callable[..., Awaitable[SimpleNamespace]]:
    """Аккаунт с привязанным ботом + клиент + свежий диалог."""

    async def _make(
        scenario: dict[str, Any] | None = None,
        *,
        schedule: dict[str, Any] | None = None,
        is_enabled: bool = True,
        client_name: str | None = "Иван",
        item_title: str | None = "Ремонт iPhone",
        bot: Bot | None = None,
    ) -> SimpleNamespace:
        bot = bot or await make_bot(scenario, schedule=schedule, is_enabled=is_enabled)
        async with db_sessionmaker() as s:
            account = AvitoAccount(
                title="LP-Москва",
                avito_user_id=100_000 + uuid.uuid4().int % 100_000,
                access_token_enc=b"a",
                refresh_token_enc=b"r",
                token_expires_at=datetime.now(UTC) + timedelta(days=1),
                status="active",
                webhook_secret="whsec",
                bot_id=bot.id,
            )
            client = Client(channel="avito", external_id=uuid.uuid4().hex[:10], name=client_name)
            s.add_all([account, client])
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
                account_id=account.id,
                client_id=client.id,
                status="new",
                bot_active=False,
                bot_vars={},
                tags=[],
                item_title=item_title,
                item_price="от 8 900 ₽",
            )
            s.add(conv)
            await s.commit()
            return SimpleNamespace(
                bot=bot,
                bot_id=bot.id,
                account_id=account.id,
                client_id=client.id,
                conversation_id=conv.id,
            )

    return _make


class Clock:
    """Монотонное «сейчас» вокруг заданного момента.

    Шаг обязателен: `created_at` сообщений — часть первичного ключа и порядка
    ленты, а с замороженным временем два сообщения одного тика становятся
    неразличимы по порядку.
    """

    def __init__(self, start: datetime, step: timedelta = timedelta(milliseconds=200)) -> None:
        self.moment = start
        self.step = step

    def __call__(self) -> datetime:
        self.moment += self.step
        return self.moment


@pytest.fixture
def world_ctx(db_sessionmaker, redis) -> Callable[..., dict[str, Any]]:
    def _ctx(*, ai: Any = None, now: datetime | None = None) -> dict[str, Any]:
        return {
            "db_session_factory": db_sessionmaker,
            "redis": redis,
            "bot_ai": ai,  # None = «AI-модуля нет» (никаких походов в сеть)
            "bot_now": Clock(now or DAY),
        }

    return _ctx


class Chat:
    """Клиент, пишущий в диалог: запись входящего + тик бота (как в проде)."""

    def __init__(self, ctx: dict[str, Any], world: SimpleNamespace, db_sessionmaker) -> None:
        self.ctx = ctx
        self.world = world
        self.factory = db_sessionmaker
        self.seq = 0

    async def says(self, text: str) -> str:
        self.seq += 1
        async with self.factory() as s, s.begin():
            s.add(
                Message(
                    conversation_id=self.world.conversation_id,
                    external_message_id=f"am-{self.seq}",
                    direction="in",
                    sender_type="client",
                    body=text,
                    attachments=[],
                    delivery_status="delivered",
                    created_at=self.ctx["bot_now"](),  # общая с ботом шкала времени
                )
            )
        return await bot_step(self.ctx, self.world.conversation_id, text)

    async def timeout(self, token: str | None = None) -> str:
        return await bot_step(self.ctx, self.world.conversation_id, None, token)


@pytest.fixture
def chat(db_sessionmaker) -> Callable[[dict[str, Any], SimpleNamespace], Chat]:
    def _chat(ctx: dict[str, Any], world: SimpleNamespace) -> Chat:
        return Chat(ctx, world, db_sessionmaker)

    return _chat


# ----------------------------------------------------------------- выборки


async def load_conv(db_sessionmaker, conv_id) -> Conversation:
    async with db_sessionmaker() as s:
        return (
            await s.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()


async def bot_messages(db_sessionmaker, conv_id) -> list[str]:
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                select(Message)
                .where(
                    Message.conversation_id == conv_id,
                    Message.direction == "out",
                    Message.sender_type == "bot",
                )
                .order_by(Message.created_at, Message.id)
            )
        ).scalars()
        return [m.body or "" for m in rows]


async def notes(db_sessionmaker, conv_id) -> list[str]:
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                select(Message).where(
                    Message.conversation_id == conv_id, Message.direction == "note"
                )
            )
        ).scalars()
        return [m.body or "" for m in rows]


async def audit_rows(db_sessionmaker, action: str) -> list[AuditLog]:
    async with db_sessionmaker() as s:
        return list((await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


def state_of(conv: Conversation) -> BotState:
    return BotState.from_conv(conv)


def handoff_of(conv: Conversation) -> HandoffInfo:
    """Передача обязана состояться — иначе тест падает здесь, а не по AttributeError."""
    info = state_of(conv).handoff
    assert info is not None, "handoff не состоялся"
    return info


def waiting_of(conv: Conversation) -> Waiting:
    waiting = state_of(conv).waiting
    assert waiting is not None, "бот не ждёт ответа"
    return waiting


def details(row: AuditLog) -> dict[str, Any]:
    assert row.details is not None
    return row.details


def option_id(option: dict[str, Any] | None) -> str:
    assert option is not None
    return str(option["id"])


# =============================================================================
# Таблица кейсов 07 §1.1
# =============================================================================


async def test_primary_intake_day_path(db_sessionmaker, world_ctx, make_world, chat):
    """07 §1.1, строка 1: send -> ask -> ai_answer(0.9) -> condition(день) -> handoff."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)

    assert await client.says("Здравствуйте!") == "ok"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is True
    assert waiting_of(conv).step_id == "ask_problem"

    await client.says("iPhone 13, разбит экран")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    state = state_of(conv)
    assert conv.bot_active is False  # бот отдал диалог
    assert conv.status == "new" and conv.assignee_id is None
    assert handoff_of(conv).reason == "scenario"
    assert handoff_of(conv).step == "handoff_day"
    assert state.vars["problem"] == "iPhone 13, разбит экран"
    assert "первичный-приём" in conv.tags
    messages = await bot_messages(db_sessionmaker, world.conversation_id)
    assert messages[0].startswith("Здравствуйте, Иван!")
    assert "8900" in messages[1]  # ответ AI ушёл клиенту
    assert len(await audit_rows(db_sessionmaker, "bot.handoff")) == 1


async def test_primary_intake_night_path_collects_phone(
    db_sessionmaker, world_ctx, make_world, chat
):
    """07 §1.1, строка 2: ночью — вопрос про телефон, тег, заметка, handoff."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=NIGHT)
    client = chat(ctx, world)

    await client.says("Здравствуйте")
    await client.says("Разбил экран iPhone 13")

    messages = await bot_messages(db_sessionmaker, world.conversation_id)
    assert "Оставьте телефон" in messages[-1]
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert waiting_of(conv).var == "phone"

    await client.says("+7 916 123-45-67")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    state = state_of(conv)
    assert state.vars["phone"] == "+79161234567"  # нормализация 02 §1.3
    assert "контакт собран" in conv.tags and "ночной-лид" in conv.tags
    assert handoff_of(conv).reason == "scenario"
    assert conv.bot_active is False
    note_bodies = "\n".join(await notes(db_sessionmaker, world.conversation_id))
    assert "+79161234567" in note_bodies and "Разбил экран" in note_bodies
    # телефон уехал и в карточку клиента (06 §0.3: source='bot')
    async with db_sessionmaker() as s:
        client_row = await s.get(Client, world.client_id)
    assert client_row.phone == "+79161234567"
    captured = await audit_rows(db_sessionmaker, "client.phone_captured")
    assert captured and details(captured[0])["source"] == "bot"


async def test_invalid_phone_retries_then_takes_on_invalid_branch(
    db_sessionmaker, world_ctx, make_world, chat
):
    """`max_attempts=2`: первый мимо — retry, второй — ветка on_invalid."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=NIGHT)
    client = chat(ctx, world)
    await client.says("Здравствуйте")
    await client.says("Не включается ноутбук")

    await client.says("позже скажу")
    messages = await bot_messages(db_sessionmaker, world.conversation_id)
    assert "Кажется, это не номер" in messages[-1]
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert waiting_of(conv).attempts == 1

    await client.says("не хочу")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    state = state_of(conv)
    assert handoff_of(conv).step == "handoff_night"  # on_invalid
    assert state.counters.attempts == 2


async def test_client_asks_for_a_human_hands_off_immediately(
    db_sessionmaker, world_ctx, make_world, chat
):
    """07 §1.1, строка 3: «позовите оператора» на любом шаге -> handoff (4.3-1)."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)

    await client.says("Здравствуйте")
    await client.says("Позовите оператора, пожалуйста")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "client_request"
    assert conv.bot_active is False
    # сценарий не продолжился: второго сообщения бота нет
    assert len(await bot_messages(db_sessionmaker, world.conversation_id)) == 1


async def test_human_request_in_the_very_first_message(
    db_sessionmaker, world_ctx, make_world, chat
):
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    await chat(ctx, world).says("Есть тут живой человек?")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "client_request"
    assert await bot_messages(db_sessionmaker, world.conversation_id) == []


async def test_low_confidence_hands_off_without_answering(
    db_sessionmaker, world_ctx, make_world, chat
):
    """07 §1.1, строка 4: confidence 0.2 -> handoff, содержательный ответ не уходит."""
    ai = StubAI(
        answer={"reply": "Наверное, дело в матрице", "confidence": 0.2, "needs_operator": False}
    )
    world = await make_world()
    ctx = world_ctx(ai=ai, now=DAY)
    client = chat(ctx, world)

    await client.says("Здравствуйте")
    await client.says("Странный дефект, не пойму")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    state = state_of(conv)
    assert handoff_of(conv).reason == "ai_low_confidence"
    assert state.vars["_ai_confidence"] == 0.2
    messages = await bot_messages(db_sessionmaker, world.conversation_id)
    assert len(messages) == 1  # только приветствие
    assert "матрице" not in " ".join(messages)


async def test_needs_operator_sends_only_the_bridge_phrase(
    db_sessionmaker, world_ctx, make_world, chat
):
    """02 §3.2: при needs_operator=true уходит вежливая фраза-мост, потом handoff."""
    ai = StubAI(
        answer={
            "reply": "Передаю ваш вопрос мастеру, он ответит в ближайшее время",
            "confidence": 0.95,
            "needs_operator": True,
        }
    )
    world = await make_world()
    client = chat(world_ctx(ai=ai, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Хочу вернуть деньги за прошлый ремонт")

    messages = await bot_messages(db_sessionmaker, world.conversation_id)
    assert "Передаю ваш вопрос мастеру" in messages[-1]
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "ai_low_confidence"


async def test_negative_classifier_tags_and_hands_off(db_sessionmaker, world_ctx, make_world, chat):
    """07 §1.1, строка 5: классификатор negative -> handoff + тег «негатив» (4.3-3)."""
    ai = StubAI(classify={"sentiment": "negative", "wants_human": False, "reason": "ругается"})
    world = await make_world()
    client = chat(world_ctx(ai=ai, now=DAY), world)

    await client.says("Ужасный сервис, я в шоке")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert "негатив" in conv.tags
    assert handoff_of(conv).reason == "negative"
    assert conv.bot_active is False
    assert ai.classified, "классификатор обязан быть вызван на каждом входящем"


async def test_negative_raises_notification(db_sessionmaker, world_ctx, make_world, chat):
    """Клиент ругается — руководитель обязан узнать (14 §2.3, вид `conversation.negative`).

    Проверяем не `notify` в вакууме, а живой путь: поднять этот вид может ровно
    одно место — передача диалога по причине `negative` (handoff.py:356), и
    только если тег «негатив» дописан ИМЕННО там.
    """
    ai = StubAI(classify={"sentiment": "negative", "wants_human": False, "reason": "ругается"})
    world = await make_world()
    client = chat(world_ctx(ai=ai, now=DAY), world)

    await client.says("Ужасный сервис, я в шоке")

    async with db_sessionmaker() as s:
        строки = (
            (
                await s.execute(
                    select(Notification).where(Notification.kind == "conversation.negative")
                )
            )
            .scalars()
            .all()
        )
    assert строки, "негатив распознан и диалог отдан человеку, а уведомления нет"
    assert строки[0].entity_id == str(world.conversation_id)
    assert строки[0].audience == "head"


async def test_negative_in_suggest_mode_raises_notification(
    db_sessionmaker, world_ctx, make_world, make_bot, chat
):
    """Режим «подсказка»: клиенту не уходит ничего, но негатив срочен так же.

    Бот-подсказчик не отвечает клиенту сам, поэтому «передача диалога человеку»
    здесь ничего не отнимает — диалог и так у человека. Но недовольство клиента
    от режима бота не зависит: руководителю оно нужно в обоих случаях.
    """
    ai = StubAI(classify={"sentiment": "negative", "wants_human": False, "reason": "ругается"})
    bot = await make_bot(mode="suggest")
    world = await make_world(bot=bot)
    client = chat(world_ctx(ai=ai, now=DAY), world)

    await client.says("Ужасный сервис, я в шоке")

    async with db_sessionmaker() as s:
        строки = (
            (
                await s.execute(
                    select(Notification).where(Notification.kind == "conversation.negative")
                )
            )
            .scalars()
            .all()
        )
    assert строки, "недовольство клиента не зависит от режима бота"


async def test_negative_after_handoff_only_tags(db_sessionmaker, world_ctx, make_world, chat):
    """Если бот уже отдал диалог — тег вешаем, второй handoff не пишем.

    Реакция классификатора приходит отдельной транзакцией уже после тика, в
    котором сработала другая причина (здесь — низкая уверенность AI).
    """
    ai = StubAI(
        answer={"reply": "", "confidence": 0.1, "needs_operator": False},
        classify=lambda texts: (
            {"sentiment": "negative", "wants_human": False}
            if any("сервис" in t for t in texts)
            else None
        ),
    )
    world = await make_world()
    client = chat(world_ctx(ai=ai, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Ну и сервис, конечно...")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "ai_low_confidence"  # причина первого тика
    assert "негатив" in conv.tags  # тег всё равно повешен
    assert len(await audit_rows(db_sessionmaker, "bot.handoff")) == 1
    # Передача уже была и про недовольство не знала — сказать о нём всё равно надо.
    async with db_sessionmaker() as s:
        строки = (
            (
                await s.execute(
                    select(Notification).where(Notification.kind == "conversation.negative")
                )
            )
            .scalars()
            .all()
        )
    assert строки, "диалог у человека, но про негатив никто не узнал"


async def test_human_heuristic_short_circuits_the_classifier(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Эвристика мгновенна и завершает тик: классификатор уже не зовём (02 §2.3)."""
    ai = StubAI(classify={"sentiment": "negative", "wants_human": True})
    world = await make_world()
    await chat(world_ctx(ai=ai, now=DAY), world).says("Позовите менеджера! Ужасно!")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "client_request"
    assert ai.classified == []
    assert len(await audit_rows(db_sessionmaker, "bot.handoff")) == 1


async def test_classifier_wants_human_is_the_second_echelon(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Перефразировку, которую не поймала эвристика, ловит классификатор."""
    ai = StubAI(classify={"sentiment": "neutral", "wants_human": True})
    world = await make_world()
    await chat(world_ctx(ai=ai, now=DAY), world).says("а можно с кем-то настоящим пообщаться")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "client_request"


async def test_two_offscript_messages_hand_off(db_sessionmaker, world_ctx, make_world, chat):
    """07 §1.1, строка 6: два ответа мимо вариантов меню -> handoff (4.3-4)."""
    world = await make_world(MENU_SCENARIO)
    client = chat(world_ctx(ai=StubAI(classify=None), now=DAY), world)

    await client.says("Здравствуйте")  # стартует сценарий, бот шлёт меню
    await client.says("не знаю что писать")
    messages = await bot_messages(db_sessionmaker, world.conversation_id)
    assert "цифрой 1 или 2" in messages[-1]
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert state_of(conv).counters.offscript_msgs == 1

    await client.says("а сколько стоит вообще всё")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "offscript"


async def test_menu_matches_by_number_and_keyword(db_sessionmaker, world_ctx, make_world, chat):
    world = await make_world(MENU_SCENARIO)
    client = chat(world_ctx(ai=StubAI(), now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("2.")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    state = state_of(conv)
    assert state.vars["device_kind"] == "laptop"
    assert handoff_of(conv).reason == "scenario"
    assert state.counters.offscript_msgs == 0


async def test_operator_intervention_mutes_the_bot_forever(
    db_sessionmaker, world_ctx, make_world, chat
):
    """07 §1.1, строка 7: оператор написал во время `ask` -> бот замолкает (4.3-6)."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")

    # исходящее оператора + точка вызова mute_bot (02 §2.6)
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        s.add(
            Message(
                conversation_id=conv.id,
                direction="out",
                sender_type="operator",
                body="Здравствуйте, я мастер, чем помочь?",
                attachments=[],
                delivery_status="pending",
                created_at=datetime.now(UTC),
            )
        )
        await mute_bot(s, conv)

    before = await bot_messages(db_sessionmaker, world.conversation_id)
    assert await client.says("Разбит экран") == "muted"
    after = await bot_messages(db_sessionmaker, world.conversation_id)
    assert before == after  # бот молчит

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False and state_of(conv).muted is True
    # и в следующий раз воркер входящих его даже не позовёт
    assert await should_run_bot((await _session(db_sessionmaker)), conv, now=DAY) is False


async def test_tick_mutes_when_operator_answered_without_endpoint(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Гонка «оператор ответил, а mute не проставился»: тик обязан догнать сам."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")
    async with db_sessionmaker() as s, s.begin():
        s.add(
            Message(
                conversation_id=world.conversation_id,
                direction="out",
                sender_type="operator",
                body="Уже смотрю",
                attachments=[],
                delivery_status="pending",
                created_at=datetime.now(UTC),
            )
        )

    assert await client.says("ну что там?") == "muted"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert state_of(conv).muted is True


async def _session(db_sessionmaker) -> AsyncSession:
    return db_sessionmaker()


async def test_schedule_gates_the_start_but_not_a_running_dialog(
    db_sessionmaker, make_world, make_bot
):
    """07 §1.1, строка 8: в 15:00 бот не стартует, в 21:00 стартует."""
    night_only = {
        "always": False,
        "timezone": "Europe/Moscow",
        "intervals": [
            {
                "days": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
                "start": "20:00",
                "end": "10:00",
            }
        ],
    }
    world = await make_world(schedule=night_only)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, world.conversation_id)
        assert await should_run_bot(s, conv, now=DAY) is False
        assert await should_run_bot(s, conv, now=NIGHT) is True
        # уже начатый диалог доводится до конца независимо от расписания
        conv.bot_active = True
        assert await should_run_bot(s, conv, now=DAY) is True


async def test_should_run_bot_guards(db_sessionmaker, make_world, make_bot):
    """Остальные условия входа (02 §2.2)."""
    world = await make_world()
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, world.conversation_id)
        assert await should_run_bot(s, conv, now=DAY) is True

        conv.status = "in_progress"
        assert await should_run_bot(s, conv, now=DAY) is False
        conv.status = "new"

        conv.bot_vars = {"muted": True}
        assert await should_run_bot(s, conv, now=DAY) is False

        conv.bot_vars = {"handoff": {"reason": "scenario", "at": "2026-08-04T00:00:00+00:00"}}
        assert await should_run_bot(s, conv, now=DAY) is False
        conv.bot_vars = {}

        # менеджер уже отвечал
        s.add(
            Message(
                conversation_id=conv.id,
                direction="out",
                sender_type="operator",
                body="Здравствуйте",
                attachments=[],
                delivery_status="delivered",
                created_at=datetime.now(UTC),
            )
        )
        await s.flush()
        assert await should_run_bot(s, conv, now=DAY) is False


async def test_disabled_bot_never_runs(db_sessionmaker, world_ctx, make_world, chat):
    world = await make_world(is_enabled=False)
    ctx = world_ctx(ai=StubAI(), now=DAY)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, world.conversation_id)
        assert await should_run_bot(s, conv, now=DAY) is False
    assert await chat(ctx, world).says("Здравствуйте") == "no_bot"
    assert await bot_messages(db_sessionmaker, world.conversation_id) == []


async def test_notes_do_not_mute_the_bot(db_sessionmaker, world_ctx, make_world, chat):
    """02 §2.6: внутренний комментарий менеджера бота не глушит."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")
    async with db_sessionmaker() as s, s.begin():
        s.add(
            Message(
                conversation_id=world.conversation_id,
                direction="note",
                sender_type="operator",
                body="Клиент из рекламы",
                attachments=[],
                delivery_status="delivered",
                created_at=datetime.now(UTC),
            )
        )
    assert await client.says("Разбит экран") == "ok"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert state_of(conv).muted is False


async def test_variables_are_substituted(db_sessionmaker, world_ctx, make_world, chat):
    """07 §1.1, строка 9: {client_name} и {item_title} (словарь 02 §1.2)."""
    world = await make_world(VARS_SCENARIO, client_name="Мария", item_title="Ремонт MacBook")
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")
    messages = await bot_messages(db_sessionmaker, world.conversation_id)
    assert messages == ["Здравствуйте, Мария! Вы писали по «Ремонт MacBook»."]


async def test_empty_client_name_drops_the_address(db_sessionmaker, world_ctx, make_world, chat):
    world = await make_world(VARS_SCENARIO, client_name=None, item_title="Ремонт MacBook")
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")
    messages = await bot_messages(db_sessionmaker, world.conversation_id)
    assert messages == ["Здравствуйте! Вы писали по «Ремонт MacBook»."]


async def test_ai_unavailable_never_blocks_the_dialog(db_sessionmaker, world_ctx, make_world, chat):
    """07 §1.1, строка 10: таймаут AI -> бот не падает и не молчит, а делает handoff."""
    ai = StubAI(answer_error=TimeoutError("api timeout"))
    world = await make_world()
    client = chat(world_ctx(ai=ai, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Что с телефоном делать?")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "ai_unavailable"
    assert conv.bot_active is False
    assert any("AI недоступен" in n for n in await notes(db_sessionmaker, world.conversation_id))


async def test_missing_ai_module_is_the_same_as_unavailable(
    db_sessionmaker, world_ctx, make_world, chat
):
    world = await make_world()
    ctx = world_ctx(ai=None, now=DAY)  # бэкенда нет вовсе
    client = chat(ctx, world)
    await client.says("Здравствуйте")
    await client.says("Что с телефоном делать?")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "ai_unavailable"


async def test_real_ai_module_with_a_dead_api_still_ends_in_handoff(
    db_sessionmaker, world_ctx, make_world, chat, monkeypatch
):
    """Сквозной шов: НАСТОЯЩИЙ app/bots/ai.py + мёртвый Claude API.

    Предыдущие два теста подменяют бэкенд заглушкой и проверяют движок. Здесь
    заглушка стоит на уровень ниже — на SDK, — и в диалоге работает реальный
    модуль AI. Так ловится ошибка, которую тесты движка не увидят: если
    `ai.py` выпустит исключение наружу вместо `None`, обработка сообщения
    упадёт целиком (решение владельца №4, DESIGN §4.5).
    """
    from app.bots import ai as ai_mod
    from app.core.config import settings

    class DeadAPI:
        class messages:  # noqa: N801 — форма SDK: client.messages.create
            @staticmethod
            async def create(**_: Any) -> Any:
                raise ConnectionError("прокси недоступен: 403 от api.anthropic.com")

    # Прогон тестов идёт с AI_FAKE=1 (tests/conftest.py) — здесь нужен именно
    # боевой путь модуля, поэтому заглушку снимаем точечно.
    monkeypatch.setattr(settings, "ai_fake", False, raising=False)
    monkeypatch.setattr(ai_mod, "get_client", lambda: DeadAPI())
    ai_mod.reset_breaker()

    world = await make_world()
    client = chat(world_ctx(ai=ai_mod, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Что с телефоном делать?")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "ai_unavailable"
    assert conv.bot_active is False
    # Диалог не потерян: клиент у оператора, а не в чёрной дыре.
    assert conv.status == "new"
    assert any("AI недоступен" in n for n in await notes(db_sessionmaker, world.conversation_id))
    ai_mod.reset_breaker()


async def test_real_ai_module_in_fake_mode_answers_without_a_client(
    db_sessionmaker, world_ctx, make_world, chat, monkeypatch
):
    """AI_FAKE=1: тот же модуль отвечает детерминированно и без сети (07 §1.1)."""
    from app.bots import ai as ai_mod

    def boom() -> Any:  # pragma: no cover — вызов = провал теста
        raise AssertionError("AI_FAKE не имеет права строить сетевой клиент")

    monkeypatch.setattr(ai_mod, "get_client", boom)

    world = await make_world()
    client = chat(world_ctx(ai=ai_mod, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Что с телефоном делать?")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    # Сценарий доехал до своего handoff'а, а не свалился в ai_unavailable.
    assert handoff_of(conv).reason == "scenario"
    assert len(await bot_messages(db_sessionmaker, world.conversation_id)) >= 2


# =============================================================================
# Предохранители, таймауты и техника тика
# =============================================================================


async def test_loop_protection_by_steps_per_tick(db_sessionmaker, world_ctx, make_world, chat):
    """02 §2.7: цикл из «бесплатных» шагов ловится лимитом шагов за тик."""
    world = await make_world(loop_scenario(max_steps_per_tick=5, max_steps_total=500))
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    state = state_of(conv)
    assert handoff_of(conv).reason == "loop_protection"
    assert state.counters.steps_total == 5
    assert conv.bot_active is False


async def test_loop_protection_by_bot_messages_row(db_sessionmaker, world_ctx, make_world, chat):
    """02 §2.7: анти-спам — бот не завалит клиента текстом."""
    world = await make_world(chatty_scenario())
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "loop_protection"
    assert len(await bot_messages(db_sessionmaker, world.conversation_id)) == 2


async def test_дедлайн_не_будит_закрытый_диалог(
    db_sessionmaker, redis, world_ctx, make_world, chat
):
    """⚠ АУДИТ 30.08: дожим уходил клиенту ЗАКРЫТОГО обращения.

    Диалог, который ведёт бот, в очереди не значится, поэтому закрыть его может
    любой оператор («спам, закрываю»). Отложенная задача дедлайна при этом
    оставалась в очереди со своим токеном и просыпалась через 25 минут: заслон
    «клиент уже ответил» системную запись о закрытии ответом не считает, и
    ветка `on_timeout` слала клиенту «Ну что, подскажете?» — то есть мы писали
    человеку, разговор с которым сами же завершили.

    Первый замок — гашение бота при закрытии (`services/conversations`). Этот,
    второй, нужен для задач, поставленных ДО выкатки: они уже лежат в очереди.
    """
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    waiting = waiting_of(conv)
    assert waiting.deadline is not None

    # Диалог закрыли руками, а признак бота НЕ сняли — ровно состояние задач,
    # поставленных до выкатки первого замка.
    async with db_sessionmaker() as s2:
        c = await s2.get(type(conv), world.conversation_id)
        c.status = "closed"
        await s2.commit()

    late = {**ctx, "bot_now": lambda: DAY + timedelta(hours=25)}
    assert await bot_ask_timeout(late, world.conversation_id, waiting.token) == "closed", (
        "дедлайн разбудил закрытый диалог — клиент получит дожим по завершённому разговору"
    )


async def test_ask_timeout_takes_the_on_timeout_branch(
    db_sessionmaker, redis, world_ctx, make_world, chat
):
    """02 §2.4: дедлайн истёк -> ветка on_timeout (здесь — handoff_no_reply)."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    waiting = waiting_of(conv)
    assert waiting.deadline is not None
    assert await redis.exists(f"arq:job:bot_timeout:{world.conversation_id}:{waiting.token}")

    # задача таймаута сработала: токен совпал, дедлайн прошёл
    late = {**ctx, "bot_now": lambda: DAY + timedelta(hours=25)}
    assert await bot_ask_timeout(late, world.conversation_id, waiting.token) == "fired"
    assert await bot_step(late, world.conversation_id, None, waiting.token) == "ok"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    state = state_of(conv)
    assert handoff_of(conv).step == "handoff_no_reply"
    assert state.waiting is None


async def test_ask_timeout_defaults_to_handoff(db_sessionmaker, world_ctx, make_world, chat):
    """`on_timeout: null` -> дефолт handoff(reason='ask_timeout') (02 §1.3)."""
    scenario = {
        "version": 1,
        "revision": 1,
        "entry": "ask_it",
        "steps": [
            {
                "id": "ask_it",
                "type": "ask",
                "params": {"text": "Как вас зовут?", "var": "name", "timeout": "2h"},
                "next": "final",
                "on_timeout": None,
            },
            {"id": "final", "type": "handoff", "params": {"reason": "scenario"}},
        ],
    }
    world = await make_world(scenario)
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    token = waiting_of(conv).token

    await client.timeout(token)
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "ask_timeout"


async def test_client_answer_annuls_the_pending_timeout(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Ответ клиента обнуляет ожидание — старый токен больше не совпадёт."""
    world = await make_world(MENU_SCENARIO)
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    old_token = waiting_of(conv).token

    await client.says("1")  # выбрал вариант -> waiting сброшен, ушёл handoff

    assert await bot_ask_timeout(ctx, world.conversation_id, old_token) in ("stale", "inactive")
    # После передачи тик в сценарий не заходит вовсе (проверка 24.09).
    assert await bot_step(ctx, world.conversation_id, None, old_token) == "handoff_done"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "scenario"  # ветка меню, а не таймаут


async def test_early_timeout_job_redefers_itself(
    db_sessionmaker, redis, world_ctx, make_world, chat
):
    """Задача проснулась раньше дедлайна — переставляет себя, а не режет ожидание."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    token = waiting_of(conv).token

    assert await bot_ask_timeout(ctx, world.conversation_id, token) == "early"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert state_of(conv).waiting is not None  # ожидание не тронуто


async def test_timeout_without_waiting_is_a_noop(db_sessionmaker, world_ctx, make_world, chat):
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    assert await bot_step(ctx, world.conversation_id, None, "deadbeef") == "ok"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False and state_of(conv).handoff is None


async def test_close_step_closes_the_dialog(db_sessionmaker, world_ctx, make_world, chat):
    world = await make_world(CLOSE_SCENARIO)
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Спасибо, вопрос снят")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.status == "closed" and conv.bot_active is False
    assert await bot_messages(db_sessionmaker, world.conversation_id) == ["Спасибо за обращение!"]
    assert any("закрыт ботом" in n for n in await notes(db_sessionmaker, world.conversation_id))
    rows = await audit_rows(db_sessionmaker, "conversation.status_changed")
    assert rows and details(rows[-1])["by"] == "bot"
    assert details(rows[-1])["to"] == "closed"


async def test_close_with_visit_outcome_makes_the_dialog_a_lead(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Закрытие с итогом «Выезд» — то, из чего рождается автозаявка.

    Решение владельца от 15 августа, дословно: «авто создание нужно только для
    бота». Окно «Чем закончилось?» снято 12 августа, и до этого шага у колонки
    `outcome` не было НИ ОДНОГО писателя — автозаявки (`leads.py` отбирает
    `outcome='visit'`) не могли родиться ни из чего. Проверяются все три следа:
    колонка, авторство в журнале с шагом сценария и пустой `outcome_by_id` —
    это колонка «какой сотрудник решил», а решил сценарий.
    """
    scenario = {
        "version": 1,
        "revision": 1,
        "entry": "bye",
        "steps": [
            {
                "id": "bye",
                "type": "close",
                "params": {"text": "Мастер выезжает!", "outcome": "visit"},
            }
        ],
    }
    world = await make_world(scenario)
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Да, приезжайте")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.status == "closed"
    assert conv.outcome == "visit"
    assert conv.outcome_at is not None
    assert conv.outcome_by_id is None, "итог поставил сценарий, а не сотрудник"

    rows = await audit_rows(db_sessionmaker, "conversation.outcome_set")
    assert len(rows) == 1
    assert details(rows[0]) == {"outcome": "visit", "by": "bot", "step": "bye"}


async def test_close_without_outcome_keeps_the_column_empty(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Обычное закрытие итога не выдумывает: NULL законен и значит «не проставили»."""
    world = await make_world(CLOSE_SCENARIO)
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Спасибо, вопрос снят")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.outcome is None
    assert await audit_rows(db_sessionmaker, "conversation.outcome_set") == []


async def test_unknown_outcome_in_a_scenario_does_not_break_the_close(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Опечатка в сценарии не имеет права уронить закрытие живого диалога.

    Валидатор такой сценарий не пропустит, но сценарий мог приехать в базу и
    мимо него (правка руками, старая ревизия). Упади здесь CHECK-ограничение —
    закрытие бы откатилось, и клиент остался бы с ботом в вечном цикле.
    """
    scenario = {
        "version": 1,
        "revision": 1,
        "entry": "bye",
        "steps": [
            {
                "id": "bye",
                "type": "close",
                "params": {"text": "До связи!", "outcome": "vizit-opechatka"},
            }
        ],
    }
    world = await make_world(scenario)
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("ок")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.status == "closed", "диалог обязан закрыться, несмотря на опечатку"
    assert conv.outcome is None


async def test_bot_does_not_overwrite_an_outcome_set_by_a_human(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Итог, проставленный человеком, бот не трогает: человек ближе к разговору."""
    world = await make_world(
        {
            "version": 1,
            "revision": 1,
            "entry": "bye",
            "steps": [
                {"id": "bye", "type": "close", "params": {"outcome": "visit", "silent": True}}
            ],
        }
    )
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, world.conversation_id)
        conv.outcome = "declined"
        await db.commit()

    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("ок")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.outcome == "declined", "решение человека пережило сценарий"


async def test_closed_dialog_starts_from_scratch_when_client_returns(
    db_sessionmaker, world_ctx, make_world, chat
):
    """02 §1.3: клиент вернулся в закрытый ботом диалог — сценарий с нуля."""
    scenario = {
        "version": 1,
        "revision": 1,
        "entry": "greet",
        "steps": [
            {"id": "greet", "type": "send", "params": {"text": "Здравствуйте!"}, "next": "bye"},
            {"id": "bye", "type": "close", "params": {"text": None, "silent": True}},
        ],
    }
    world = await make_world(scenario)
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Спасибо, вопрос снят")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.status == "closed"
    state = state_of(conv)
    assert state.is_fresh() and state.closed_at is not None
    assert state.vars["_closed_by_step"] == "bye"

    # воркер входящих переоткрывает диалог (DESIGN §8.3)
    async with db_sessionmaker() as s, s.begin():
        reopened = await s.get(Conversation, world.conversation_id)
        reopened.status = "new"
        assert await should_run_bot(s, reopened, now=DAY) is True

    await client.says("А ещё вопрос")
    assert await bot_messages(db_sessionmaker, world.conversation_id) == [
        "Здравствуйте!",
        "Здравствуйте!",
    ]


async def test_scenario_changed_under_the_dialog(db_sessionmaker, world_ctx, make_world, chat):
    """02 §2.1: текущий шаг исчез из нового сценария -> аккуратный handoff."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")

    async with db_sessionmaker() as s, s.begin():
        bot = await s.get(Bot, world.bot_id)
        bot.scenario = {
            "version": 1,
            "revision": 4,
            "entry": "other",
            "steps": [{"id": "other", "type": "handoff", "params": {"reason": "scenario"}}],
        }

    await client.says("Разбит экран")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "scenario_changed"


async def test_tick_enqueues_delivery_and_publishes_events(
    db_sessionmaker, redis, world_ctx, make_world, chat
):
    """08 §8.1: события и задачи уезжают ПОСЛЕ commit'а — из outbox тика."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await drain_events(pubsub)

    await chat(ctx, world).says("Здравствуйте")

    events = await drain_events(pubsub)
    types = [e["type"] for e in events]
    assert "message:new" in types
    async with db_sessionmaker() as s:
        msg_id = (
            await s.execute(
                select(Message.id).where(
                    Message.conversation_id == world.conversation_id,
                    Message.sender_type == "bot",
                )
            )
        ).scalar_one()
    assert await redis.exists(f"arq:job:deliver:{msg_id}")
    await pubsub.aclose()


async def test_second_tick_is_serialized_by_the_lock(db_sessionmaker, redis, world_ctx, make_world):
    """02 §2.3: пер-диалоговый лок — параллельный тик переставляется на потом."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    await redis.set(f"lock:bot:{world.conversation_id}", "1", ex=30)
    assert await bot_step(ctx, world.conversation_id, "Здравствуйте") == "locked"
    assert await bot_messages(db_sessionmaker, world.conversation_id) == []
    assert await redis.zcard("arq:queue") >= 1


async def test_tick_on_missing_conversation_is_a_noop(world_ctx):
    ctx = world_ctx(ai=StubAI(), now=DAY)
    assert await bot_step(ctx, uuid.uuid4(), "Здравствуйте") == "no_conversation"


async def test_maybe_enqueue_bot_step_is_the_entry_point(db_sessionmaker, redis, make_world):
    """Точка вызова для зоны входящих: проверка + постановка одной строкой."""
    world = await make_world()
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, world.conversation_id)
        assert await maybe_enqueue_bot_step(s, redis, conv, text="Привет", now=DAY) is True
        conv.bot_vars = {"muted": True}
        assert await maybe_enqueue_bot_step(s, redis, conv, text="Привет", now=DAY) is False
    assert await redis.zcard("arq:queue") == 1


async def test_phones_are_masked_before_going_to_the_model(
    db_sessionmaker, world_ctx, make_world, chat
):
    """ПРИВАТНОСТЬ: запрос уходит за границу, телефон в промпт не попадает."""
    ai = StubAI()
    world = await make_world()
    client = chat(world_ctx(ai=ai, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Разбит экран, мой номер +7 916 123-45-67")

    assert ai.dialogs, "ai_answer обязан быть вызван"
    payload = " ".join(m["content"] for d in ai.dialogs for m in d)
    assert "{PHONE}" in payload
    assert "9161234567" not in payload.replace(" ", "").replace("-", "")
    # но локально номер сохранён
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert state_of(conv).vars["phone"] == "+79161234567"


async def test_extracted_entities_enrich_the_summary(db_sessionmaker, world_ctx, make_world, chat):
    """02 §3.5, эшелон 2: извлечённое AI дополняет сводку, не перетирая собранное."""
    ai = StubAI(
        answer={"reply": "Понял вас", "confidence": 0.9, "needs_operator": False},
        entities={"device_model": "iPhone 13", "phone": "+70000000000", "problem": "выдумка"},
    )
    world = await make_world()
    client = chat(world_ctx(ai=ai, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Разбит экран айфона")

    note_bodies = "\n".join(await notes(db_sessionmaker, world.conversation_id))
    assert "iPhone 13" in note_bodies
    assert "Разбит экран айфона" in note_bodies  # собранное `ask`-ом важнее
    assert "выдумка" not in note_bodies
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert "+70000000000" not in str(conv.bot_vars)  # телефон из модели не берём


async def test_shipped_default_scenario_runs_end_to_end(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Сценарий, который реально уезжает в прод (`app/bots/scenarios`)."""
    scenarios = pytest.importorskip("app.bots.scenarios")
    world = await make_world(scenarios.default_scenario())
    client = chat(world_ctx(ai=StubAI(), now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Не включается ноутбук")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert state_of(conv).handoff is not None
    assert conv.bot_active is False


async def test_offscript_without_waiting_hands_off(db_sessionmaker, world_ctx, make_world, chat):
    """Условие №4, случай «а»: бот ничего не ждёт, а клиент пишет (02 §4)."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")
    # Аварийная раскладка: бот «владеет» диалогом, но ожидание потеряно.
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        conv.bot_vars = {**conv.bot_vars, "waiting": None}

    await client.says("ну так что?")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert state_of(conv).counters.offscript_msgs == 1

    await client.says("эй, ответьте")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "offscript"


async def test_ask_invalid_defaults_to_handoff(db_sessionmaker, world_ctx, make_world, chat):
    """`on_invalid: null` -> дефолт handoff(reason='ask_invalid') (02 §1.3)."""
    scenario = {
        "version": 1,
        "revision": 1,
        "entry": "ask_phone",
        "steps": [
            {
                "id": "ask_phone",
                "type": "ask",
                "params": {
                    "text": "Оставьте телефон",
                    "var": "phone",
                    "validate": "phone",
                    "max_attempts": 1,
                    "timeout": "2h",
                },
                "next": "final",
                "on_invalid": None,
            },
            {"id": "final", "type": "handoff", "params": {"reason": "scenario"}},
        ],
    }
    world = await make_world(scenario)
    client = chat(world_ctx(ai=StubAI(), now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("не дам телефон")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "ask_invalid"


async def test_low_confidence_takes_the_explicit_branch(
    db_sessionmaker, world_ctx, make_world, chat
):
    """`on_low_confidence` задан — идём туда, а не в дефолтный handoff."""
    scenario = {
        "version": 1,
        "revision": 1,
        "entry": "ai_draft",
        "steps": [
            {
                "id": "ai_draft",
                "type": "ai_answer",
                "params": {"confidence_threshold": 0.8, "context_messages": 5},
                "next": "final",
                "on_low_confidence": "sorry",
            },
            {
                "id": "sorry",
                "type": "send",
                "params": {"text": "Уточню у мастера и вернусь"},
                "next": "final",
            },
            {"id": "final", "type": "handoff", "params": {"reason": "scenario"}},
        ],
    }
    ai = StubAI(answer={"reply": "", "confidence": 0.3, "needs_operator": False})
    world = await make_world(scenario)
    await chat(world_ctx(ai=ai, now=DAY), world).says("Здравствуйте")

    assert "Уточню у мастера" in (await bot_messages(db_sessionmaker, world.conversation_id))[-1]
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "scenario"


async def test_broken_next_reference_hands_off(db_sessionmaker, world_ctx, make_world, chat):
    """Ссылка в никуда — не зависший диалог, а handoff (валидатор её не пустил бы)."""
    scenario = {
        "version": 1,
        "revision": 1,
        "entry": "greet",
        "steps": [
            {"id": "greet", "type": "send", "params": {"text": "Привет"}, "next": "нет_такого"}
        ],
    }
    world = await make_world(scenario)
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "scenario_changed"
    assert conv.bot_active is False


async def test_missing_entry_hands_off(db_sessionmaker, world_ctx, make_world, chat):
    scenario = {
        "version": 1,
        "revision": 1,
        "entry": "нет_такого",
        "steps": [{"id": "final", "type": "handoff", "params": {"reason": "scenario"}}],
    }
    world = await make_world(scenario)
    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).reason == "scenario_changed"


async def test_timeout_with_a_foreign_token_is_ignored(
    db_sessionmaker, world_ctx, make_world, chat
):
    """Токен из прошлого ожидания не смеет срезать текущее (02 §2.4)."""
    world = await make_world()
    ctx = world_ctx(ai=StubAI(), now=DAY)
    client = chat(ctx, world)
    await client.says("Здравствуйте")

    assert await client.timeout("00000000") == "ok"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert state_of(conv).waiting is not None  # ожидание на месте
    assert state_of(conv).handoff is None


# =============================================================================
# Пустая подстановка не уезжает клиенту (02 §1.2)
# =============================================================================


def item_scenario(**params: Any) -> dict[str, Any]:
    """Сценарий из одного шага, текст которого держится на {item_title}."""
    step_type = str(params.pop("type", "send"))
    return {
        "version": 1,
        "revision": 1,
        "entry": "s1",
        "steps": [
            {"id": "s1", "type": step_type, "params": params, "next": "done"},
            {"id": "done", "type": "handoff", "params": {"reason": "scenario"}},
        ],
    }


async def test_send_without_the_item_keeps_the_rest_of_the_message(
    world_ctx, make_world, chat, db_sessionmaker
):
    """Объявления нет — фраза о нём уходит, приветствие остаётся."""
    scenario = item_scenario(
        text="Здравствуйте, {client_name}! Вы пишете по объявлению «{item_title}». Что случилось?"
    )
    world = await make_world(scenario, item_title=None)
    await chat(world_ctx(), world).says("привет")

    assert await bot_messages(db_sessionmaker, world.conversation_id) == [
        "Здравствуйте, Иван! Что случилось?"
    ]


async def test_send_that_loses_everything_leaves_a_note_for_the_team(
    world_ctx, make_world, chat, db_sessionmaker
):
    """Сообщение съедено целиком: клиенту молчим, сотрудникам объясняем."""
    world = await make_world(item_scenario(text="По объявлению «{item_title}»."), item_title="")
    await chat(world_ctx(), world).says("привет")

    assert await bot_messages(db_sessionmaker, world.conversation_id) == []
    staff = await notes(db_sessionmaker, world.conversation_id)
    assert any("s1" in n and "item_title" in n for n in staff), staff
    # сценарий не встал: шаг отработал и увёл на следующий
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_of(conv).step == "done"


async def test_question_without_a_text_is_handed_over_instead_of_hanging(
    world_ctx, make_world, chat, db_sessionmaker
):
    """`ask`, чей вопрос съеден пустой подстановкой, НЕ встаёт в ожидание.

    Иначе диалог молча висит сутки: клиент не видел вопроса, а бот его ждёт.
    """
    scenario = item_scenario(
        type="ask",
        text="Уточните по объявлению «{item_title}»?",
        var="detail",
        timeout="24h",
    )
    world = await make_world(scenario, item_title=None)
    await chat(world_ctx(), world).says("привет")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert state_of(conv).waiting is None, "бот ждёт ответа на незаданный вопрос"
    assert conv.bot_active is False
    info = handoff_of(conv)
    assert (info.reason, info.step) == ("scenario", "s1")
    assert "item_title" in "\n".join(await notes(db_sessionmaker, world.conversation_id))
    assert await bot_messages(db_sessionmaker, world.conversation_id) == []


async def test_question_with_a_filled_item_asks_as_usual(
    world_ctx, make_world, chat, db_sessionmaker
):
    """Контроль: с заполненным объявлением тот же шаг работает как раньше."""
    scenario = item_scenario(
        type="ask",
        text="Уточните по объявлению «{item_title}»?",
        var="detail",
        timeout="24h",
    )
    world = await make_world(scenario, item_title="Ремонт iPhone")
    await chat(world_ctx(), world).says("привет")

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert waiting_of(conv).var == "detail"
    assert await bot_messages(db_sessionmaker, world.conversation_id) == [
        "Уточните по объявлению «Ремонт iPhone»?"
    ]


# =============================================================================
# Чистые функции шагов (02 §1.2–1.3) и состояние (02 §2.1)
# =============================================================================


def test_render_text_dictionary():
    values = {"client_name": "Иван", "item_title": "Ремонт", "problem": "экран"}
    assert render_text("Здравствуйте, {client_name}!", values) == "Здравствуйте, Иван!"
    assert render_text("По «{item_title}»: {problem}", values) == "По «Ремонт»: экран"
    # пустое имя вырезает обращение целиком
    assert render_text("Здравствуйте, {client_name}!", {"client_name": ""}) == "Здравствуйте!"
    # неизвестный плейсхолдер уносит свой оборот, а не оставляет «Цена !»
    assert render_text("Цена {unknown}!", values) == ""
    assert render_text(None, values) == ""


def render_with_item(item_title: str, template: str, **extra: Any) -> Rendered:
    """Подстановка с системным словарём 02 §1.2 и заданным объявлением."""
    values: dict[str, Any] = {"client_name": "Иван", "item_title": item_title, **extra}
    return render_step_text(template, values)


def test_blank_placeholder_takes_its_whole_phrase_with_it():
    """Клиенту не должно уезжать «по объявлению «».» (02 §1.2)."""
    template = "Здравствуйте, {client_name}! Вы пишете по объявлению «{item_title}». Что случилось?"

    full = render_with_item("Ремонт", template)
    assert full.text == "Здравствуйте, Иван! Вы пишете по объявлению «Ремонт». Что случилось?"
    assert full.dropped == ()

    empty = render_with_item("", template)
    assert empty.text == "Здравствуйте, Иван! Что случилось?"
    assert empty.dropped == ("item_title",)
    assert empty.lost_everything is False
    assert "«»" not in empty.text

    # Пробелы вместо названия — тот же пустой случай, а не «объявление есть».
    assert render_with_item("   ", template).text == "Здравствуйте, Иван! Что случилось?"


def test_render_drops_the_line_not_the_whole_message():
    """Границей служит и перевод строки: соседние строки не страдают."""
    rendered = render_with_item(
        "",
        "🤖 Бот собрал контакт: {phone}\nОбъявление: {item_title}\nПроблема: {problem}",
        phone="+79161234567",
        problem="экран",
    )
    assert rendered.text == "🤖 Бот собрал контакт: +79161234567\nПроблема: экран"


def test_render_reports_when_nothing_is_left():
    """Шаблон был, текста не осталось — вызывающий обязан об этом узнать."""
    rendered = render_with_item("", "По объявлению «{item_title}».")
    assert rendered.text == ""
    assert rendered.lost_everything is True
    # Пустой шаблон — не потеря: терять было нечего.
    assert render_step_text("", {}).lost_everything is False


def test_validate_answer_rules():
    assert validate_answer({"validate": "any"}, "  ") == (False, None)
    assert validate_answer({"validate": "any"}, "текст") == (True, "текст")
    assert validate_answer({"validate": "phone"}, "8 916 123-45-67") == (True, "+79161234567")
    assert validate_answer({"validate": "phone"}, "не скажу") == (False, None)
    assert validate_answer({"validate": "number"}, "около 5000 рублей")[0] is True
    assert validate_answer({"validate": {"regex": "^[A-Z]{2}-\\d+$"}}, "AB-12")[0] is True
    assert validate_answer({"validate": {"regex": "^[A-Z]{2}-\\d+$"}}, "мимо")[0] is False
    # битая регулярка не роняет тик (валидатор ловит её при сохранении)
    assert validate_answer({"validate": {"regex": "([unclosed"}}, "что-то")[0] is True


def test_menu_matching_rules():
    options = [
        {"id": "mobile", "match": ["1", "телефон"], "next": "a"},
        {"id": "laptop", "match": ["2", "ноутбук"], "next": "b"},
    ]
    assert option_id(match_menu_option(options, "1")) == "mobile"
    assert option_id(match_menu_option(options, "2.")) == "laptop"
    assert option_id(match_menu_option(options, "у меня НОУТБУК сломался")) == "laptop"
    assert match_menu_option(options, "не знаю") is None
    # короткий ключ подстрокой не ищется: «1» не должна найтись в «1000 оборотов»
    assert match_menu_option(options, "стиралка на 1000 оборотов") is None


def test_parse_timeout_formats():
    assert parse_timeout("24h") == timedelta(hours=24)
    assert parse_timeout("30m") == timedelta(minutes=30)
    assert parse_timeout("3d") == timedelta(days=3)
    assert parse_timeout(3600) == timedelta(seconds=3600)
    assert parse_timeout(None) is None
    assert parse_timeout("скоро") is None
    assert parse_timeout(10**9) == timedelta(seconds=604_800)  # верхняя граница схемы


def test_mask_phones_keeps_other_numbers():
    assert mask_phones("звоните +7 916 123-45-67") == "звоните {PHONE}"
    assert mask_phones("замена от 8900 ₽") == "замена от 8900 ₽"
    assert mask_phones(None) == ""


def test_detect_human_request_words():
    assert detect_human_request("позовите оператора")
    assert detect_human_request("Хватит ботом, дайте ЖИВОГО человека")
    assert detect_human_request("есть тут менеджер?")
    assert not detect_human_request("сколько стоит замена экрана?")
    assert not detect_human_request("")


def test_condition_kinds():
    """Все виды условий шага `condition` (таблица 02 §1.3)."""
    values = {"phone": "+79161234567", "device_kind": "Mobile", "empty": "  "}
    text = "Разбил экран айфона"

    def check(cond: dict[str, Any]) -> bool:
        return evaluate_condition(cond, values=values, last_text=text, now=DAY)

    assert check({"kind": "var_exists", "var": "phone"}) is True
    assert check({"kind": "var_exists", "var": "empty"}) is False
    assert check({"kind": "var_exists", "var": "нет"}) is False
    assert check({"kind": "var_equals", "var": "device_kind", "value": "mobile"}) is True
    assert check({"kind": "var_equals", "var": "device_kind", "value": "laptop"}) is False
    assert check({"kind": "text_contains", "keywords": ["экран", "батарея"]}) is True
    assert check({"kind": "text_contains", "keywords": ["ноутбук"]}) is False
    assert check({"kind": "text_matches", "regex": "айфон|iphone"}) is True
    assert check({"kind": "text_matches", "regex": "([битая"}) is False
    assert check({"kind": "work_hours", "from": "10:00", "to": "20:00"}) is True
    assert check({"kind": "чего-то_новенькое"}) is False
    assert check({}) is False
    assert evaluate_condition("мусор", values=values, last_text=text, now=DAY) is False


def test_pick_condition_branch_order():
    step = Step(
        id="check",
        type="condition",
        params={
            "conditions": [
                {"if": {"kind": "var_exists", "var": "phone"}, "next": "with_phone"},
                {"if": {"kind": "var_exists", "var": "problem"}, "next": "with_problem"},
                "мусор",
            ],
            "else": "fallback",
        },
    )
    assert pick_condition_branch(step, values={"phone": "+7900"}, last_text=None) == "with_phone"
    assert (
        pick_condition_branch(step, values={"problem": "экран"}, last_text=None) == "with_problem"
    )
    assert pick_condition_branch(step, values={}, last_text=None) == "fallback"


def test_scenario_parsing_is_forgiving():
    scenario = Scenario.from_dict(
        {
            "version": 1,
            "entry": "a",
            "settings": {"max_steps_per_tick": 999, "max_bot_messages_row": 0},
            "steps": [
                {"id": "a", "type": "send", "params": {"text": "hi"}, "next": "b"},
                {"id": "a", "type": "send", "params": {"text": "dup"}},  # дубль id
                {"id": "b", "type": "unknown", "params": {}},  # неизвестный тип
                "мусор",
            ],
        }
    )
    assert list(scenario.steps) == ["a"]
    assert scenario.limits.max_steps_per_tick == 50  # клип по схеме 02 §1.4
    assert scenario.limits.max_bot_messages_row == 2
    assert Scenario.from_dict(None).entry is None
    assert Limits.from_settings(None) == Limits()


def test_bot_state_roundtrip_and_legacy_migration():
    """02 §2.1 + миграция старых значений: читаем всё, что уже лежит в колонке."""
    fresh = BotState.from_dict({})
    assert fresh.is_fresh() and fresh.vars == {} and fresh.counters.steps_total == 0

    # раскладка, которую ставит отправка сообщения оператором (спринт 3)
    muted = BotState.from_dict({"muted": True, "waiting": None})
    assert muted.muted is True and muted.waiting is None

    legacy = BotState.from_dict(
        {
            "current_step": "ask_phone",  # алиас имени шага
            "phone": "+79161234567",  # переменная лежала на верхнем уровне
            "counters": {"steps_total": 3},
            "custom_zone_key": {"kept": True},  # чужой ключ обязан пережить запись
            "handoff": "negative",  # раньше писали одну строку
        }
    )
    assert legacy.step == "ask_phone"
    assert legacy.vars["phone"] == "+79161234567"
    assert legacy.counters.steps_total == 3
    assert legacy.handoff is not None and legacy.handoff.reason == "negative"
    dumped = legacy.dump()
    assert dumped["step"] == "ask_phone"
    assert dumped["custom_zone_key"] == {"kept": True}
    assert BotState.from_dict(dumped).vars == legacy.vars


def test_waiting_without_token_is_readable():
    waiting = Waiting.from_dict({"kind": "ask", "var": "phone"})
    assert waiting is not None and waiting.token == ""
    assert Waiting.from_dict(None) is None


def test_waiting_expiry():
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert Waiting(deadline=past).expired() is True
    assert Waiting(deadline=future).expired() is False
    assert Waiting(deadline=None).expired() is False  # ждём бесконечно
