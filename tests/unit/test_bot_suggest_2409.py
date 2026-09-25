"""Режим «Подсказка» диалог не ведёт и не прячет (проверка 24.09).

Для свежего нового диалога подсказка шла по сценарию как автоответ: ставила
`bot_active` и ожидание `ask` с дедлайном. Строка пропадала из «Входящих» у
всех, клиенту никто не отвечал, пока сторож зависших не снимал бота через
10–12 минут (замер 29.08: 288 снятий, 0 реплик клиенту). А по дедлайну
приходила передача «клиент не ответил» на вопрос, которого клиент не видел.

ДИВЕРСИИ: вернуть `bot_active` для свежего диалога в подсказке или ожидание
в `_begin_waiting` — краснеют первый и второй тесты; снятие оставшихся меток
в тике — третий и шестой; отдачу диалогов при переводе в подсказки в
`replace_bot` — четвёртый, в `patch_leadbot` — пятый.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.bots.runtime import bot_step
from app.bots.state import BotState
from app.models import AuditLog, Conversation
from app.services import inbox, leadbot_admin
from tests.unit import test_bot_engine as engine_fixtures
from tests.unit.test_bot_engine import PRIMARY_INTAKE, StubAI, bot_messages, load_conv, notes

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world
world_ctx = engine_fixtures.world_ctx
chat = engine_fixtures.chat

pytestmark = pytest.mark.anyio

DRAFT = {"reply": "Подскажите модель", "confidence": 0.9, "needs_operator": False}


def admin(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['admin']}"}


async def _queued(db_sessionmaker: Any, conv_id: Any, since: datetime) -> None:
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_id)
        assert conv is not None
        inbox.enter_queue(conv, now=since)


async def _reasons(db_sessionmaker: Any, conv_id: Any) -> list[str]:
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                select(AuditLog.details).where(
                    AuditLog.action == "bot.handoff", AuditLog.entity_id == str(conv_id)
                )
            )
        ).scalars()
        return [d["reason"] for d in rows]


async def test_a_suggestion_keeps_a_new_dialog_in_the_inbox(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    bot = await make_bot(PRIMARY_INTAKE, mode="suggest")
    world = await make_world(bot=bot)
    await _queued(db_sessionmaker, world.conversation_id, datetime.now(UTC))

    assert await chat(world_ctx(ai=StubAI(answer=DRAFT)), world).says("Здравствуйте") == "ok"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False
    assert inbox.is_waiting(conv), "подсказка спрятала новый диалог из «Входящих»"
    assert BotState.from_conv(conv).waiting is None
    assert await bot_messages(db_sessionmaker, world.conversation_id) == []
    assert await notes(db_sessionmaker, world.conversation_id), "подсказки нет вовсе"


async def test_the_leadbot_suggestion_sets_no_deadline(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    """Сценарий лид-бота: ответ → ждать клиента. В подсказке ждать нечего."""
    bot = await make_bot(leadbot_admin.default_scenario(), mode="suggest")
    world = await make_world(bot=bot)
    await _queued(db_sessionmaker, world.conversation_id, datetime.now(UTC))

    assert await chat(world_ctx(ai=StubAI(answer=DRAFT)), world).says("Сломалась стиралка") == "ok"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False and BotState.from_conv(conv).waiting is None
    assert inbox.is_waiting(conv)


async def test_leftover_marks_are_cleared_on_the_next_suggestion(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    """Диалог, спрятанный подсказкой до правки (или автоответом до перевода в
    подсказки), снова виден после следующего сообщения клиента."""
    bot = await make_bot(PRIMARY_INTAKE, mode="suggest")
    world = await make_world(bot=bot)
    await _queued(db_sessionmaker, world.conversation_id, datetime.now(UTC))
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        state = BotState.from_conv(conv)
        state.start(bot_id=bot.id)
        state.step = "ask_problem"
        state.begin_waiting(kind="ask", step_id="ask_problem", var="problem", timeout=None)
        conv.bot_vars = state.dump()
        conv.bot_active = True

    assert await chat(world_ctx(ai=StubAI(answer=DRAFT)), world).says("Не сливает воду") == "ok"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False and inbox.is_waiting(conv)
    assert BotState.from_conv(conv).waiting is None


async def test_switching_a_bot_to_suggestions_hands_its_dialogs_to_people(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: Any,
    make_world: Any,
    world_ctx: Any,
    chat: Any,
) -> None:
    world = await make_world()
    assert await chat(world_ctx(ai=StubAI()), world).says("Здравствуйте") == "ok"
    assert (await load_conv(db_sessionmaker, world.conversation_id)).bot_active is True
    detail = (await client.get(f"/api/v1/bots/{world.bot_id}", headers=admin(tokens))).json()

    response = await client.put(
        f"/api/v1/bots/{world.bot_id}",
        json={
            "name": detail["name"],
            "schedule": detail["schedule"],
            "scenario": detail["scenario"],
            "knowledge_base": detail["knowledge_base"] or "",
            "ai_provider": detail["ai_provider"],
            "mode": "suggest",
        },
        headers=admin(tokens),
    )

    assert response.status_code == 200, response.text
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False and inbox.is_waiting(conv)
    assert await _reasons(db_sessionmaker, world.conversation_id) == ["bot_to_suggest"]


async def test_switching_the_leadbot_to_suggestions_hands_its_dialogs_to_people(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: Any,
    make_world: Any,
) -> None:
    async with db_sessionmaker() as s, s.begin():
        system = await leadbot_admin.ensure_system_bot(s)
        system.is_enabled = True
        system.mode = "auto"
    world = await make_world(bot=system)
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        conv.status = "in_progress"
        conv.bot_active = True
        conv.bot_vars = {"bot_id": str(system.id), "step": "wait_client", "vars": {}}

    response = await client.patch(
        "/api/v1/leadbot", json={"mode": "suggest"}, headers=admin(tokens)
    )

    assert response.status_code == 200
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False and inbox.is_waiting(conv)
    assert await _reasons(db_sessionmaker, world.conversation_id) == ["bot_to_suggest"]


async def test_a_legacy_suggestion_deadline_does_not_hand_off(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any
) -> None:
    """Дедлайн, поставленный подсказкой до правки, не отдаёт диалог «клиент не
    ответил»: вопроса клиент не видел. Метки снимаются, передачи нет."""
    bot = await make_bot(PRIMARY_INTAKE, mode="suggest")
    world = await make_world(bot=bot)
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        state = BotState.from_conv(conv)
        state.start(bot_id=bot.id)
        state.step = "ask_problem"
        waiting = state.begin_waiting(
            kind="ask", step_id="ask_problem", var="problem", timeout=timedelta(minutes=20)
        )
        conv.bot_vars = state.dump()
        conv.bot_active = True
        token = waiting.token

    ctx = world_ctx(ai=StubAI(answer=DRAFT))
    assert await bot_step(ctx, world.conversation_id, None, token) == "ok"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False
    assert await _reasons(db_sessionmaker, world.conversation_id) == []
