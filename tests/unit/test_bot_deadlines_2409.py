"""Потерянный дедлайн бота подбирает свой сторож (проверка 24.09).

Задача `bot_ask_timeout` теряется (три заминки базы, отказ очереди), а диалог
остаётся `bot_active` — скрытым из «Входящих», — и последнее слово за ботом:
сторож зависших ловит только «клиент написал, бот молчит». Клиент, не
ответивший на вопрос бота, не возвращался в очередь никогда.

ДИВЕРСИИ: убрать постановку задачи в `sweep_in_session` — краснеет первый
тест; передачу после `GIVE_UP` — второй; проверку `GRACE` — третий.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from app.bots.runtime import BOT_TIMEOUT_JOB, bot_ask_timeout
from app.bots.state import BotState, parse_iso
from app.scheduler.jobs import bot_deadlines
from app.scheduler.main import build_scheduler
from app.services import inbox
from tests.unit import test_bot_engine as engine_fixtures
from tests.unit.test_bot_engine import DAY, StubAI, load_conv

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world
world_ctx = engine_fixtures.world_ctx
chat = engine_fixtures.chat

pytestmark = pytest.mark.anyio


async def _bot_asked(db_sessionmaker: Any, make_world: Any, world_ctx: Any, chat: Any) -> Any:
    """Бот спросил клиента и ждёт: дедлайн `ask` стоит, задачи дедлайна нет."""
    from app.bots.scenarios import default_scenario

    world = await make_world(default_scenario())
    assert await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте") == "ok"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    waiting = BotState.from_conv(conv).waiting
    assert conv.bot_active and waiting is not None and waiting.deadline is not None
    world.token = waiting.token
    world.deadline = parse_iso(waiting.deadline)
    return world


async def _sweep(db_sessionmaker: Any, now: Any) -> tuple[Any, int]:
    async with db_sessionmaker() as s, s.begin():
        return await bot_deadlines.sweep_in_session(s, now=now)


async def test_a_lost_deadline_is_put_back_on_the_queue(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    world = await _bot_asked(db_sessionmaker, make_world, world_ctx, chat)
    late = world.deadline + bot_deadlines.GRACE + timedelta(minutes=1)

    outbox, picked = await _sweep(db_sessionmaker, late)

    assert picked == 1
    assert [(j.name, j.args) for j in outbox.jobs] == [
        (BOT_TIMEOUT_JOB, (world.conversation_id, world.token))
    ]
    # Поставленная задача делает то же, что сделала бы первая: срок истёк.
    ctx = world_ctx(ai=StubAI(), now=late)
    assert await bot_ask_timeout(ctx, world.conversation_id, world.token) == "fired"


async def test_a_deadline_lost_for_an_hour_hands_the_client_to_people(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    world = await _bot_asked(db_sessionmaker, make_world, world_ctx, chat)

    _, picked = await _sweep(db_sessionmaker, world.deadline + bot_deadlines.GIVE_UP)

    assert picked == 1
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False
    assert inbox.is_waiting(conv), "клиент обязан встать во «Входящие»"
    state = BotState.from_conv(conv)
    assert state.handoff is not None and state.handoff.reason == "ask_timeout"
    assert state.waiting is None


async def test_a_deadline_still_in_its_grace_is_left_to_its_task(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any, chat: Any
) -> None:
    """Задача дедлайна могла просто задержаться в очереди: до `GRACE` её ждём."""
    world = await _bot_asked(db_sessionmaker, make_world, world_ctx, chat)

    outbox, picked = await _sweep(
        db_sessionmaker, world.deadline + bot_deadlines.GRACE - timedelta(minutes=1)
    )

    assert picked == 0 and outbox.jobs == []
    assert (await load_conv(db_sessionmaker, world.conversation_id)).bot_active is True


def test_the_sweep_is_scheduled() -> None:
    assert bot_deadlines.JOB_ID in {j.id for j in build_scheduler().get_jobs()}
