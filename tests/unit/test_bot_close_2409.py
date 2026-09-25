"""Закрытие ботом: замок чужого диалога, уборка и след для экрана (проверка 24.09).

* шаг `close` закрывал диалог и в режиме подсказки, и у назначенного
  оператора — замок 30.08 стоял только в `close_by_leadbot`;
* бот закрывал, оставляя `offered_at`, ожидание, непрочитанное и чужие
  закрепления: переоткрытый диалог вставал в очередь первым с «ждёт N дней»;
* закрытия бота не видел экран «Диалоги бота»: плитка «Бот закрыл сам» всегда 0,
  строка писала «закрыт без передачи», то есть закрытие приписывалось людям.

ДИВЕРСИИ: убрать замок в `exec_close` — краснеет первый тест; убрать
`clear_closed_marks` из `exec_close` — второй; убрать `bot.closed` из
`BOT_EVENTS` — третий (и счётчик в tests/integration/test_bot_dialogs_pg.py).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select

from app.bots.runtime import bot_step
from app.models import AuditLog, Conversation, ConversationPin, Message, User
from app.services import bot_dialogs
from app.services import conversation_table as table
from tests.unit import test_bot_engine as engine_fixtures
from tests.unit.test_bot_engine import StubAI, bot_messages, load_conv

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world
world_ctx = engine_fixtures.world_ctx

pytestmark = pytest.mark.anyio

CLOSE_FIRST = {
    "version": 1,
    "revision": 1,
    "entry": "bye",
    "steps": [
        {
            "id": "bye",
            "type": "close",
            "params": {"text": "Спасибо, всего доброго", "silent": False},
            "next": None,
        }
    ],
}
AI_THEN_WAIT = {
    "version": 1,
    "revision": 1,
    "entry": "ai",
    "steps": [
        {"id": "ai", "type": "ai_answer", "params": {}, "next": "wait"},
        {"id": "wait", "type": "ask", "params": {"text": None, "timeout": "25m"}, "next": "ai"},
    ],
}


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


async def test_a_suggestion_does_not_close_the_dialog(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any
) -> None:
    bot = await make_bot(CLOSE_FIRST, mode="suggest")
    world = await make_world(bot=bot)
    ctx = world_ctx(ai=StubAI())
    await _client_writes(db_sessionmaker, ctx, world.conversation_id, "Здравствуйте")

    assert await bot_step(ctx, world.conversation_id, "Здравствуйте") == "ok"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.status != "closed", "подсказка закрыла диалог по-настоящему"
    assert await bot_messages(db_sessionmaker, world.conversation_id) == []


async def test_a_bot_close_cleans_up_like_a_manual_one(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(CLOSE_FIRST)
    ctx = world_ctx(ai=StubAI())
    async with db_sessionmaker() as s:
        someone = User(
            email=f"pin-{uuid.uuid4().hex[:6]}@leadchat.test",
            full_name="Коллега",
            password_hash="x",
            role="manager",
        )
        s.add(someone)
        await s.commit()
        await s.refresh(someone)
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        conv.offered_at = datetime.now(UTC)
        conv.unread_count = 2
        s.add(ConversationPin(user_id=someone.id, conversation_id=conv.id))
    await _client_writes(db_sessionmaker, ctx, world.conversation_id, "Здравствуйте")

    assert await bot_step(ctx, world.conversation_id, "Здравствуйте") == "ok"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.status == "closed"
    assert conv.offered_at is None and conv.unread_count == 0
    async with db_sessionmaker() as s:
        pins = await s.scalar(
            select(func.count())
            .select_from(ConversationPin)
            .where(ConversationPin.conversation_id == conv.id)
        )
        closed = (
            (
                await s.execute(
                    select(AuditLog.details).where(
                        AuditLog.action == "bot.closed", AuditLog.entity_id == str(conv.id)
                    )
                )
            )
            .scalars()
            .all()
        )
    assert pins == 0
    assert [d["reason"] for d in closed] == ["scenario"]


async def test_a_refusal_closed_by_the_bot_counts_as_closed_itself(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    ai = StubAI(
        answer={
            "reply": "С заменой матрицы, к сожалению, не поможем.",
            "confidence": 0.95,
            "needs_operator": False,
            "meta": {"flag": {"kind": "refuse"}},
        }
    )
    ctx = world_ctx(ai=ai)
    await _client_writes(db_sessionmaker, ctx, world.conversation_id, "нужна замена матрицы")

    assert await bot_step(ctx, world.conversation_id, "нужна замена матрицы") == "ok"

    # Счётчик плиток сверяет журнал с диалогом через `CAST(id AS TEXT)` — это
    # проверяется на PostgreSQL (tests/integration/test_bot_dialogs_pg.py); здесь
    # — исход строки, который считается в Python.
    async with db_sessionmaker() as s:
        page = await bot_dialogs.page(s, table.TableFilters())
    rows = {str(r["id"]): r["outcome"] for r in page["items"]}
    assert rows[str(world.conversation_id)]["group"] == "closed_itself"


async def test_a_collected_lead_is_named_as_such(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    ai = StubAI(
        answer={
            "reply": "Записал вас на завтра к 10:00.",
            "confidence": 0.95,
            "needs_operator": False,
            "meta": {"lead_ready": True},
        }
    )
    ctx = world_ctx(ai=ai)
    await _client_writes(db_sessionmaker, ctx, world.conversation_id, "да, завтра в 10")

    assert await bot_step(ctx, world.conversation_id, "да, завтра в 10") == "ok"

    async with db_sessionmaker() as s:
        page = await bot_dialogs.page(s, table.TableFilters())
    row = next(r for r in page["items"] if str(r["id"]) == str(world.conversation_id))
    assert row["outcome"] == {"group": None, "label": "заявка собрана — бот закрыл"}
