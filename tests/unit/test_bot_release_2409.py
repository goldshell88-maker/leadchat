"""Выключение бота и отвязка канала отдают его диалоги людям (проверка 24.09).

`bot_active` прячет диалог из «Входящих». Выключенный тумблером бот, снятый с
канала бот и «Выключить лид-бота» оставляли свои диалоги скрытыми: ответ
клиента упирался в «no_bot», и диалог висел невидимым, пока его не снимал
сторож зависших, а молчащего клиента не возвращал никто. Страховка в тике
(tests/unit/test_bot_tick_guards_2409.py) ловит только клиента, который
напишет, — здесь проверяется, что диалог уходит людям сразу.

ДИВЕРСИИ: убрать `release_bot_dialogs` из `_set_enabled` — краснеет первый
тест; фильтр `status != "closed"` в `release_bot_dialogs` — второй; снятые
каналы в `set_bot_accounts` — третий; перепривязанные от другого бота —
четвёртый; выключение в `patch_leadbot` — пятый; режим бота в
`release_bot_dialogs` — шестой.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from app.bots.state import BotState
from app.models import AuditLog, AvitoAccount, Conversation
from app.services import inbox, leadbot_admin
from tests.unit import test_bot_engine as engine_fixtures
from tests.unit.test_bot_engine import StubAI, load_conv

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world
world_ctx = engine_fixtures.world_ctx
chat = engine_fixtures.chat

pytestmark = pytest.mark.anyio


def admin(tokens: dict[str, str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens['admin']}"}


async def _bot_leads(world: Any, world_ctx: Any, chat: Any, db_sessionmaker: Any) -> None:
    assert await chat(world_ctx(ai=StubAI()), world).says("Здравствуйте") == "ok"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is True, "бот не взял диалог — проверять нечего"


async def _handoff_reasons(db_sessionmaker: Any, conv_id: uuid.UUID) -> list[str]:
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                select(AuditLog.details).where(
                    AuditLog.action == "bot.handoff", AuditLog.entity_id == str(conv_id)
                )
            )
        ).scalars()
        return [d["reason"] for d in rows]


async def _assert_released(db_sessionmaker: Any, conv_id: uuid.UUID) -> None:
    conv = await load_conv(db_sessionmaker, conv_id)
    assert conv.bot_active is False
    assert inbox.is_waiting(conv), "диалог обязан встать во «Входящие»"
    assert BotState.from_conv(conv).waiting is None, "дедлайн ask дожал бы клиента"
    assert await _handoff_reasons(db_sessionmaker, conv_id) == ["bot_disabled"]


async def _assert_still_led(db_sessionmaker: Any, conv_id: uuid.UUID) -> None:
    conv = await load_conv(db_sessionmaker, conv_id)
    assert conv.bot_active is True
    assert await _handoff_reasons(db_sessionmaker, conv_id) == []


async def test_disabling_a_bot_hands_its_dialogs_to_people(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: Any,
    make_world: Any,
    world_ctx: Any,
    chat: Any,
) -> None:
    world = await make_world()
    await _bot_leads(world, world_ctx, chat, db_sessionmaker)

    response = await client.post(f"/api/v1/bots/{world.bot_id}/disable", headers=admin(tokens))

    assert response.status_code == 200
    assert response.json()["is_enabled"] is False
    await _assert_released(db_sessionmaker, world.conversation_id)


async def test_disabling_a_bot_does_not_reopen_closed_dialogs(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: Any,
    make_world: Any,
) -> None:
    """Закрытый диалог с неснятым `bot_active` (старые данные) остаётся закрытым:
    передача ничейного диалога ставит статус «Новый» и вернула бы его в очередь."""
    world = await make_world()
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        conv.status = "closed"
        conv.bot_active = True

    response = await client.post(f"/api/v1/bots/{world.bot_id}/disable", headers=admin(tokens))

    assert response.status_code == 200
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.status == "closed" and conv.offered_at is None
    assert await _handoff_reasons(db_sessionmaker, world.conversation_id) == []


async def test_unbinding_a_channel_hands_only_its_dialogs_to_people(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: Any,
    make_world: Any,
    world_ctx: Any,
    chat: Any,
) -> None:
    removed = await make_world()
    kept = await make_world(bot=removed.bot)
    await _bot_leads(removed, world_ctx, chat, db_sessionmaker)
    await _bot_leads(kept, world_ctx, chat, db_sessionmaker)

    response = await client.put(
        f"/api/v1/bots/{removed.bot_id}/accounts",
        json={"account_ids": [str(kept.account_id)]},
        headers=admin(tokens),
    )

    assert response.status_code == 200
    await _assert_released(db_sessionmaker, removed.conversation_id)
    await _assert_still_led(db_sessionmaker, kept.conversation_id)


async def test_moving_a_channel_to_another_bot_releases_the_old_bot_dialogs(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: Any,
    make_bot: Any,
    make_world: Any,
    world_ctx: Any,
    chat: Any,
) -> None:
    """Канал, занятый другим ботом, перепривязывается молча. Состояние диалога —
    шаг сценария старого бота; новый бот продолжил бы с чужого шага."""
    world = await make_world()
    await _bot_leads(world, world_ctx, chat, db_sessionmaker)
    other = await make_bot(name="Второй бот")

    response = await client.put(
        f"/api/v1/bots/{other.id}/accounts",
        json={"account_ids": [str(world.account_id)]},
        headers=admin(tokens),
    )

    assert response.status_code == 200
    await _assert_released(db_sessionmaker, world.conversation_id)


async def test_turning_the_leadbot_off_hands_its_dialogs_to_people(
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
        conv.bot_vars = {"bot_id": str(system.id), "step": "ask_leadbot", "vars": {}}

    response = await client.patch("/api/v1/leadbot", json={"enabled": False}, headers=admin(tokens))

    assert response.status_code == 200
    await _assert_released(db_sessionmaker, world.conversation_id)
    async with db_sessionmaker() as s:
        account = await s.get(AvitoAccount, world.account_id)
        assert account is not None and account.bot_id == system.id, "канал отвязался"


async def test_disabling_a_suggestion_bot_keeps_the_queue_wait(
    client: httpx.AsyncClient,
    tokens: dict[str, str],
    db_sessionmaker: Any,
    make_bot: Any,
    make_world: Any,
) -> None:
    """Подсказка клиенту не отвечала: диалог, ждавший 20 минут, возвращается в
    очередь со своими 20 минутами, а не встаёт в конец с «0 мин» (как в
    передаче из подсказки, проверка 24.09)."""
    bot = await make_bot(mode="suggest")
    world = await make_world(bot=bot)
    waited_since = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=20)
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        inbox.enter_queue(conv, now=waited_since)
        conv.bot_active = True
        conv.bot_vars = {"bot_id": str(bot.id), "step": "ask_problem", "vars": {}}

    response = await client.post(f"/api/v1/bots/{bot.id}/disable", headers=admin(tokens))

    assert response.status_code == 200
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False and inbox.is_waiting(conv)
    assert conv.offered_at is not None
    assert conv.offered_at.replace(tzinfo=conv.offered_at.tzinfo or UTC) == waited_since
