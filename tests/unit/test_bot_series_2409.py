"""Тик читает всю серию сообщений клиента, а не первое (проверка 24.09).

Тик ставится по первому сообщению серии, следующие в окне уходят в
`bot.debounced`, и текст задачи — только первое. ИИ читает переписку сам, а
проверки сценария видели одно первое сообщение: «сейчас» + номер следом
давали «Не вижу номера», «позовите оператора» вторым сообщением терялось,
ответ на меню во втором сообщении не засчитывался.

ДИВЕРСИИ: отдать движку `incoming_text` вместо серии — краснеют первые три
теста; убрать окно давности серии — четвёртый; сверять меню только со всей
серией — третий.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any

import pytest

from app.bots.runtime import bot_step
from app.bots.state import BotState
from app.models import Message
from tests.unit import test_bot_engine as engine_fixtures
from tests.unit.test_bot_engine import DAY, PRIMARY_INTAKE, StubAI, bot_messages, load_conv

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world
world_ctx = engine_fixtures.world_ctx

pytestmark = pytest.mark.anyio

ASK_PHONE = {
    "version": 1,
    "revision": 1,
    "entry": "ask_phone",
    "steps": [
        {
            "id": "ask_phone",
            "type": "ask",
            "params": {
                "text": "Оставьте номер телефона",
                "var": "phone",
                "validate": "phone",
                "retry_text": "Не вижу номера, напишите его цифрами",
                "max_attempts": 3,
                "timeout": "25m",
            },
            "next": "thanks",
        },
        {"id": "thanks", "type": "send", "params": {"text": "Спасибо, записал"}, "next": "done"},
        {"id": "done", "type": "handoff", "params": {"reason": "scenario"}},
    ],
}
PICK = {
    "version": 1,
    "revision": 1,
    "entry": "pick",
    "steps": [
        {
            "id": "pick",
            "type": "menu",
            "params": {
                "text": "Что сломалось?\n1. Телефон\n2. Ноутбук",
                "var": "device",
                "options": [
                    {"id": "phone", "label": "Телефон", "match": ["1"], "next": "done"},
                    {"id": "laptop", "label": "Ноутбук", "match": ["2"], "next": "done"},
                ],
                "retry_text": "Ответьте цифрой 🙂",
                "max_attempts": 2,
                "timeout": "25m",
            },
        },
        {"id": "done", "type": "handoff", "params": {"reason": "scenario"}},
    ],
}


async def _client_writes(db_sessionmaker: Any, conv_id: uuid.UUID, text: str, at: datetime) -> None:
    async with db_sessionmaker() as s, s.begin():
        s.add(
            Message(
                conversation_id=conv_id,
                external_message_id=f"m-{uuid.uuid4().hex[:8]}",
                direction="in",
                sender_type="client",
                body=text,
                attachments=[],
                delivery_status="delivered",
                created_at=at,
            )
        )


async def _series(db_sessionmaker: Any, ctx: dict, conv_id: uuid.UUID, *texts: str) -> None:
    for text in texts:
        await _client_writes(db_sessionmaker, conv_id, text, ctx["bot_now"]())


async def test_a_phone_in_the_second_message_answers_the_question(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(ASK_PHONE)
    ctx = world_ctx(ai=StubAI())
    await _series(db_sessionmaker, ctx, world.conversation_id, "Привет")
    assert await bot_step(ctx, world.conversation_id, "Привет") == "ok"

    await _series(db_sessionmaker, ctx, world.conversation_id, "сейчас", "89161234567")
    assert await bot_step(ctx, world.conversation_id, "сейчас") == "ok"

    sent = await bot_messages(db_sessionmaker, world.conversation_id)
    assert "Не вижу номера, напишите его цифрами" not in sent
    assert sent[-1] == "Спасибо, записал"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert BotState.from_conv(conv).vars.get("phone") == "+79161234567"


async def test_a_request_for_a_person_in_the_second_message_is_heard(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(PRIMARY_INTAKE)
    ctx = world_ctx(ai=StubAI())
    await _series(db_sessionmaker, ctx, world.conversation_id, "Здравствуйте", "позовите оператора")

    assert await bot_step(ctx, world.conversation_id, "Здравствуйте") == "ok"

    state = BotState.from_conv(await load_conv(db_sessionmaker, world.conversation_id))
    assert state.handoff is not None and state.handoff.reason == "client_request"


async def test_a_menu_answer_in_the_second_message_is_counted(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(PICK)
    ctx = world_ctx(ai=StubAI())
    await _series(db_sessionmaker, ctx, world.conversation_id, "Добрый день")
    assert await bot_step(ctx, world.conversation_id, "Добрый день") == "ok"

    await _series(db_sessionmaker, ctx, world.conversation_id, "ну", "2")
    assert await bot_step(ctx, world.conversation_id, "ну") == "ok"

    state = BotState.from_conv(await load_conv(db_sessionmaker, world.conversation_id))
    assert state.vars.get("device") == "laptop"
    assert state.counters.offscript_msgs == 0


async def test_old_history_does_not_join_the_series(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    """Свежий диалог с давней перепиской: полугодовое «соедините с менеджером»
    не повод передавать новое обращение, серия — только свежие сообщения."""
    world = await make_world(PRIMARY_INTAKE)
    ctx = world_ctx(ai=StubAI())
    await _client_writes(
        db_sessionmaker, world.conversation_id, "соедините с менеджером", DAY - timedelta(days=180)
    )
    await _series(db_sessionmaker, ctx, world.conversation_id, "Здравствуйте")

    assert await bot_step(ctx, world.conversation_id, "Здравствуйте") == "ok"

    state = BotState.from_conv(await load_conv(db_sessionmaker, world.conversation_id))
    assert state.handoff is None
    assert (await bot_messages(db_sessionmaker, world.conversation_id))[0].startswith(
        "Здравствуйте"
    )
