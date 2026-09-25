"""Тик бота перепроверяет хозяина, закрытие и включённость бота (проверка 24.09).

Ворота (`bot_entry_block`) судят в момент постановки, а тик просыпается через
окно серии (15–30 с), подгрузку истории или дедлайн `ask` (минуты):
* оператор успевал «Принять» — бот всё равно здоровался и снова ставил
  `bot_active` на чужом диалоге;
* закрытый человеком диалог подсказка с передачей переоткрывала в «Новые»;
* выключенный бот оставлял свои диалоги скрытыми из «Входящих» навсегда.

ДИВЕРСИИ: убрать ветку «принят человеком» — краснеет первый тест; ветку
«закрыт» — второй; передачу при выключенном боте — третий.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from sqlalchemy import select

from app.bots.runtime import bot_step
from app.bots.state import BotState
from app.models import AuditLog, Conversation, Message, User
from app.services import conversations as convs
from tests.unit import test_bot_engine as engine_fixtures
from tests.unit.test_bot_engine import StubAI, bot_messages, load_conv

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world
world_ctx = engine_fixtures.world_ctx
chat = engine_fixtures.chat

pytestmark = pytest.mark.anyio


async def _operator(db_sessionmaker: Any) -> User:
    async with db_sessionmaker() as s:
        user = User(
            email=f"op-{uuid.uuid4().hex[:6]}@leadchat.test",
            full_name="Оператор",
            password_hash="x",
            role="manager",
        )
        s.add(user)
        await s.commit()
        await s.refresh(user)
        return user


async def _client_writes(db_sessionmaker: Any, ctx: dict, conv_id: uuid.UUID, text: str) -> None:
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


async def test_a_dialog_claimed_during_the_series_window_is_left_to_the_human(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world()
    ctx = world_ctx(ai=StubAI())
    await _client_writes(db_sessionmaker, ctx, world.conversation_id, "Сломался телефон")
    operator = await _operator(db_sessionmaker)
    # «Принять» в окне серии — как `inbox.claim`.
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        conv.claimed_by_id = conv.assignee_id = operator.id
        conv.status = "in_progress"

    assert await bot_step(ctx, world.conversation_id, "Сломался телефон") == "claimed"

    assert await bot_messages(db_sessionmaker, world.conversation_id) == []
    assert (await load_conv(db_sessionmaker, world.conversation_id)).bot_active is False


async def test_a_closed_dialog_is_not_touched_in_any_mode(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world()
    ctx = world_ctx(ai=StubAI())
    await _client_writes(db_sessionmaker, ctx, world.conversation_id, "Спам")
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        conv.status = "closed"

    assert await bot_step(ctx, world.conversation_id, "Спам") == "closed"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.status == "closed" and conv.offered_at is None
    assert await bot_messages(db_sessionmaker, world.conversation_id) == []


async def test_a_disabled_bot_hands_its_dialogs_back_to_people(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    world = await make_world()
    ctx = world_ctx(ai=StubAI())
    client = chat(ctx, world)
    assert await client.says("Здравствуйте") == "ok"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is True and BotState.from_conv(conv).waiting is not None

    async with db_sessionmaker() as s, s.begin():
        from app.models import Bot

        bot = await s.get(Bot, world.bot_id)
        assert bot is not None
        bot.is_enabled = False

    assert await client.says("Алло?") == "no_bot"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False
    assert conv.status == "new" and conv.offered_at is not None, "диалог обязан встать в очередь"
    async with db_sessionmaker() as s:
        reasons = (
            (
                await s.execute(
                    select(AuditLog.details).where(
                        AuditLog.action == "bot.handoff",
                        AuditLog.entity_id == str(world.conversation_id),
                    )
                )
            )
            .scalars()
            .all()
        )
    assert [r["reason"] for r in reasons] == ["bot_disabled"]


async def test_assigning_a_bot_dialog_to_a_person_steps_the_bot_aside(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    world = await make_world()
    ctx = world_ctx(ai=StubAI())
    assert await chat(ctx, world).says("Здравствуйте") == "ok"
    operator = await _operator(db_sessionmaker)

    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None and conv.bot_active
        await convs.assign_conversation(s, conv, assignee=operator, actor=operator)

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False
    assert BotState.from_conv(conv).waiting is None, "дедлайн ask дожал бы клиента"


async def test_a_suggestion_handoff_keeps_the_queue_wait(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any
) -> None:
    """Подсказка не решает за человека: ничейный диалог, ждавший 20 минут, не
    уходит в конец очереди с «0 мин», статус не меняется."""
    from datetime import timedelta

    from app.services import inbox
    from tests.unit.test_bot_engine import DAY, PRIMARY_INTAKE

    bot = await make_bot(PRIMARY_INTAKE, mode="suggest")
    world = await make_world(bot=bot)
    ctx = world_ctx(
        ai=StubAI(answer={"reply": "черновик", "confidence": 0.2, "needs_operator": False})
    )
    waited_since = DAY - timedelta(minutes=20)
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        inbox.enter_queue(conv, now=waited_since)
        # Не свежий диалог: бот уже подсказывал раньше.
        conv.bot_vars = {"bot_id": str(bot.id), "step": "ask_problem", "vars": {}, "counters": {}}
    await _client_writes(db_sessionmaker, ctx, world.conversation_id, "а сколько по времени?")

    assert await bot_step(ctx, world.conversation_id, "а сколько по времени?") == "ok"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert BotState.from_conv(conv).handoff is not None, "передачи не было — проверять нечего"
    assert conv.offered_at is not None
    assert conv.offered_at.replace(tzinfo=conv.offered_at.tzinfo or waited_since.tzinfo) == (
        waited_since
    ), "подсказка переставила время ожидания"
    assert conv.status == "new"


async def test_the_ask_deadline_does_not_nudge_a_dialog_assigned_meanwhile(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    """Руководитель назначил оператору диалог, который вёл бот (мимо ручки —
    как старые данные): дедлайн `ask` не пишет клиенту дожим."""
    from datetime import timedelta

    from app.bots.runtime import bot_ask_timeout
    from app.bots.scenarios import default_scenario
    from tests.unit.test_bot_engine import DAY

    # Серверная заготовка: `ask_problem` ждёт 20 минут и дожимает.
    world = await make_world(default_scenario())
    ctx = world_ctx(ai=StubAI(), now=DAY)
    assert await chat(ctx, world).says("Здравствуйте") == "ok"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    token = BotState.from_conv(conv).waiting.token
    before = await bot_messages(db_sessionmaker, world.conversation_id)
    operator = await _operator(db_sessionmaker)
    async with db_sessionmaker() as s, s.begin():
        c = await s.get(Conversation, world.conversation_id)
        assert c is not None
        c.assignee_id = operator.id
        c.status = "in_progress"

    late = dict(ctx)
    late["bot_now"] = lambda: DAY + timedelta(minutes=25)
    assert await bot_ask_timeout(late, world.conversation_id, token) == "fired"
    assert await bot_step(late, world.conversation_id, None, token) == "claimed"

    assert await bot_messages(db_sessionmaker, world.conversation_id) == before
    assert (await load_conv(db_sessionmaker, world.conversation_id)).bot_active is False
