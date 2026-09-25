"""Модель думает вне транзакции тика, ответ сверяется перед применением (проверка 24.09).

Тик держал `SELECT … FOR UPDATE` строки диалога всё время похода в модель (до
15 секунд): вебхук нового сообщения клиента ждал строку, маркер свежести в бою
не срабатывал никогда, а «Принять» и отправка оператора висели. Теперь тик —
две транзакции: до шага ИИ и после ответа модели. Вторая проходит те же ворота
и сверяет последнее входящее; ответ, опоздавший к переписке, отбрасывается.

Здесь — поведение; то, что строка свободна, пока модель думает, проверяет
PostgreSQL (tests/integration/test_bot_two_phase_pg.py).

ДИВЕРСИИ: убрать сверку последнего входящего в `_apply_ai_answer` — краснеет
первый тест; ворота во второй транзакции — второй и третий; постановку тика
на новое сообщение (`_keep_series_answered`) — первый; возврат счёта шага при
откладывании — четвёртый.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from sqlalchemy import select

from app.bots.runtime import DEBOUNCE_KEY, bot_step
from app.bots.state import BotState
from app.models import AuditLog, Bot, Conversation, Message, User
from tests.unit import test_bot_engine as engine_fixtures
from tests.unit.test_bot_engine import StubAI, bot_messages, load_conv

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world
world_ctx = engine_fixtures.world_ctx

pytestmark = pytest.mark.anyio

AI_THEN_WAIT = {
    "version": 1,
    "revision": 1,
    "entry": "ai",
    "steps": [
        {"id": "ai", "type": "ai_answer", "params": {}, "next": "wait"},
        {"id": "wait", "type": "ask", "params": {"text": None, "timeout": "25m"}, "next": "ai"},
    ],
}
ANSWER = {"reply": "Замена экрана — от 8900 ₽", "confidence": 0.95, "needs_operator": False}


class ThinkingAI(StubAI):
    """Модель, пока думает, даёт миру поменяться: `meanwhile` зовётся из вызова."""

    def __init__(self, meanwhile: Callable[[], Awaitable[None]] | None = None) -> None:
        super().__init__(answer=ANSWER)
        self.meanwhile = meanwhile

    async def ai_answer(self, bot, dialog, item_title, **kw):  # noqa: ANN001
        if self.meanwhile is not None:
            meanwhile, self.meanwhile = self.meanwhile, None
            await meanwhile()
        return await super().ai_answer(bot, dialog, item_title, **kw)


async def _client_writes(db_sessionmaker: Any, conv_id: uuid.UUID, text: str, ctx: dict) -> None:
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
                created_at=ctx["bot_now"](),
            )
        )


async def test_a_message_written_while_the_model_thinks_drops_the_stale_reply(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any, redis: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    ctx = world_ctx(ai=None)
    ai = ThinkingAI(
        meanwhile=lambda: _client_writes(
            db_sessionmaker, world.conversation_id, "и адрес: Ленина, 5", ctx
        )
    )
    ctx["bot_ai"] = ai
    await _client_writes(db_sessionmaker, world.conversation_id, "Сколько стоит экран?", ctx)

    assert await bot_step(ctx, world.conversation_id, "Сколько стоит экран?") == "stale"

    assert await bot_messages(db_sessionmaker, world.conversation_id) == [], "ушёл устаревший ответ"
    state = BotState.from_conv(await load_conv(db_sessionmaker, world.conversation_id))
    assert state.step == "ai" and state.waiting is None
    assert await redis.exists(DEBOUNCE_KEY.format(conversation_id=world.conversation_id)), (
        "тик на новое сообщение никто не поставил"
    )

    # Тик нового сообщения отвечает один раз и по всей переписке.
    assert await bot_step(ctx, world.conversation_id, "и адрес: Ленина, 5") == "ok"
    assert await bot_messages(db_sessionmaker, world.conversation_id) == [ANSWER["reply"]]
    assert "Ленина" in str(ai.dialogs[-1])


async def test_a_dialog_claimed_while_the_model_thinks_gets_no_reply(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    ctx = world_ctx(ai=None)

    async def operator_claims() -> None:
        async with db_sessionmaker() as s, s.begin():
            user = User(
                email=f"op-{uuid.uuid4().hex[:6]}@leadchat.test",
                full_name="Оператор",
                password_hash="x",
                role="manager",
            )
            s.add(user)
            await s.flush()
            conv = await s.get(Conversation, world.conversation_id)
            assert conv is not None
            conv.claimed_by_id = conv.assignee_id = user.id
            conv.status = "in_progress"

    ctx["bot_ai"] = ThinkingAI(meanwhile=operator_claims)
    await _client_writes(db_sessionmaker, world.conversation_id, "Сколько стоит экран?", ctx)

    assert await bot_step(ctx, world.conversation_id, "Сколько стоит экран?") == "claimed"

    assert await bot_messages(db_sessionmaker, world.conversation_id) == []
    assert (await load_conv(db_sessionmaker, world.conversation_id)).bot_active is False


async def test_a_bot_disabled_while_the_model_thinks_hands_the_dialog_back(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    ctx = world_ctx(ai=None)

    async def admin_disables() -> None:
        async with db_sessionmaker() as s, s.begin():
            bot = await s.get(Bot, world.bot_id)
            assert bot is not None
            bot.is_enabled = False

    ctx["bot_ai"] = ThinkingAI(meanwhile=admin_disables)
    await _client_writes(db_sessionmaker, world.conversation_id, "Сколько стоит экран?", ctx)

    assert await bot_step(ctx, world.conversation_id, "Сколько стоит экран?") == "no_bot"

    assert await bot_messages(db_sessionmaker, world.conversation_id) == []
    async with db_sessionmaker() as s:
        reasons = (
            await s.execute(
                select(AuditLog.details).where(
                    AuditLog.action == "bot.handoff",
                    AuditLog.entity_id == str(world.conversation_id),
                )
            )
        ).scalars()
        assert [r["reason"] for r in reasons] == ["bot_disabled"]


async def test_an_answer_to_an_unchanged_dialog_is_sent_and_counted_once(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    ctx = world_ctx(ai=ThinkingAI())
    await _client_writes(db_sessionmaker, world.conversation_id, "Сколько стоит экран?", ctx)

    assert await bot_step(ctx, world.conversation_id, "Сколько стоит экран?") == "ok"

    assert await bot_messages(db_sessionmaker, world.conversation_id) == [ANSWER["reply"]]
    state = BotState.from_conv(await load_conv(db_sessionmaker, world.conversation_id))
    assert state.counters.ai_calls == 1
    # Шаг ИИ и шаг ожидания — два шага, а не три: отложенный шаг считается один раз.
    assert state.counters.steps_total == 2
    assert state.waiting is not None and state.waiting.step_id == "wait"
