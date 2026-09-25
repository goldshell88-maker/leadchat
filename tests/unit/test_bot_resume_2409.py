"""Бот без ожидания в идущем сценарии не молчит (проверка 24.09).

Условие №4 (02 §4) считает «мимо сценария» сообщение клиента, когда бот
ничего не ждёт. Два состояния выглядят так же, но не про это:

* бота перевели с «Подсказки» на «Отвечать клиенту» — подсказка ничего не
  ждёт, клиент не видел от бота ни слова;
* ответ шага ИИ отброшен маркером свежести — шаг не сдвинулся, ожидания нет.

В обоих бот молчал: ИИ не вызывался, а на третьем сообщении шла передача с
ложной причиной «клиент пишет мимо сценария». Само правило №4 в силе —
его сторожит `test_offscript_without_waiting_hands_off` в test_bot_engine.py.

ДИВЕРСИИ: убрать повтор шага ИИ — краснеет третий тест; убрать старт
сначала после подсказки — второй; первый держится на любом из двух путей и
краснеет, если убрать оба.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.bots.state import BotState
from app.models import Bot, Conversation
from app.services import leadbot_admin
from tests.unit import test_bot_engine as engine_fixtures
from tests.unit.test_bot_engine import PRIMARY_INTAKE, StubAI, bot_messages, load_conv

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world
world_ctx = engine_fixtures.world_ctx
chat = engine_fixtures.chat

pytestmark = pytest.mark.anyio

ANSWER = {"reply": "Мастер приедет сегодня с 18 до 20", "confidence": 0.95, "needs_operator": False}
AI_THEN_WAIT = {
    "version": 1,
    "revision": 1,
    "entry": "ai",
    "steps": [
        {"id": "ai", "type": "ai_answer", "params": {}, "next": "wait"},
        {"id": "wait", "type": "ask", "params": {"text": None, "timeout": "25m"}, "next": "ai"},
    ],
}


async def _to_auto(db_sessionmaker: Any, bot_id: Any) -> None:
    async with db_sessionmaker() as s, s.begin():
        bot = await s.get(Bot, bot_id)
        assert bot is not None
        bot.mode = "auto"


async def test_the_leadbot_answers_after_suggestions_are_switched_off(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    bot = await make_bot(leadbot_admin.default_scenario(), mode="suggest")
    world = await make_world(bot=bot)
    client = chat(world_ctx(ai=StubAI(answer=ANSWER)), world)
    assert await client.says("Здравствуйте, сломалась стиралка") == "ok"
    assert await client.says("Не сливает воду") == "ok"
    await _to_auto(db_sessionmaker, bot.id)

    assert await client.says("Вы приедете сегодня?") == "ok"

    assert await bot_messages(db_sessionmaker, world.conversation_id) == [ANSWER["reply"]]
    state = BotState.from_conv(await load_conv(db_sessionmaker, world.conversation_id))
    assert state.counters.offscript_msgs == 0 and state.handoff is None


async def test_a_scenario_started_by_suggestions_starts_over_for_the_client(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    """«Первичный приём» в подсказке дошёл до вопроса и не ждёт: клиент не видел
    ни приветствия, ни вопроса. После перевода на автоответ бот здоровается."""
    bot = await make_bot(PRIMARY_INTAKE, mode="suggest")
    world = await make_world(bot=bot)
    client = chat(world_ctx(ai=StubAI(answer=ANSWER)), world)
    assert await client.says("Здравствуйте") == "ok"
    await _to_auto(db_sessionmaker, bot.id)

    assert await client.says("Алло?") == "ok"

    sent = await bot_messages(db_sessionmaker, world.conversation_id)
    assert len(sent) == 1 and sent[0].startswith("Здравствуйте, Иван!")
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is True
    waiting = BotState.from_conv(conv).waiting
    assert waiting is not None and waiting.step_id == "ask_problem"


async def test_a_dropped_ai_answer_is_given_on_the_next_message(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    """После отброшенного ответа шаг ИИ стоит на месте без ожидания: следующее
    сообщение клиента получает ответ, а не счёт «мимо сценария»."""
    world = await make_world(AI_THEN_WAIT)
    client = chat(world_ctx(ai=StubAI(answer=ANSWER)), world)
    assert await client.says("Сколько стоит ремонт?") == "ok"
    # Как после маркера свежести: шаг ИИ, ожидания нет, бот уже писал клиенту.
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        state = BotState.from_conv(conv)
        state.stop_waiting()
        state.step = "ai"
        conv.bot_vars = state.dump()

    assert await client.says("И адрес: Ленина, 5") == "ok"

    assert len(await bot_messages(db_sessionmaker, world.conversation_id)) == 2
    state = BotState.from_conv(await load_conv(db_sessionmaker, world.conversation_id))
    assert state.counters.offscript_msgs == 0 and state.waiting is not None
