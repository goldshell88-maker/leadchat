"""«Почему бот молчит» называет каждую причину словами (проверка 24.09).

Причину «диалог принят человеком» тик возвращал как `claimed_by_human`, а
подписи у неё не было ни в словаре тика, ни в копии словаря на экране лид-бота
— экран показывал машинный код. Копия убрана: подпись одна, `ENTRY_BLOCKS`.

ДИВЕРСИЯ: убрать `claimed_by_human` из `ENTRY_BLOCKS` — краснеют оба теста.
"""

from __future__ import annotations

import ast
import inspect
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.bots import runtime
from app.models import Conversation, User
from app.services import leadbot_admin
from tests.unit import test_bot_engine as engine_fixtures

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world

pytestmark = pytest.mark.anyio


async def test_a_dialog_claimed_by_a_person_is_explained_in_words(
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
        operator = User(
            email=f"op-{uuid.uuid4().hex[:6]}@leadchat.test",
            full_name="Оператор",
            password_hash="x",
            role="manager",
        )
        s.add(operator)
        await s.flush()
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        conv.claimed_by_id = conv.assignee_id = operator.id
        conv.status = "in_progress"
        conv.last_message_at = datetime.now(UTC)

    response = await client.get(
        "/api/v1/leadbot/silence", headers={"Authorization": f"Bearer {tokens['admin']}"}
    )

    assert response.status_code == 200
    row = next(r for r in response.json()["not_taken"] if r["conversation_id"] == str(conv.id))
    assert row["reason"] == "claimed_by_human"
    assert row["reason_label"] == runtime.ENTRY_BLOCKS["claimed_by_human"]


def test_every_entry_block_reason_has_a_label() -> None:
    """Причины берутся из самой функции: новая ветка без подписи краснеет здесь,
    а не на экране владельца. Возврат не строкой-константой — тоже провал:
    такую причину этот сторож не увидел бы."""
    tree = ast.parse(inspect.getsource(runtime.bot_entry_block))
    reasons: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and node.value is not None:
            value = node.value
            assert isinstance(value, ast.Constant), f"причина не константа: {ast.unparse(value)}"
            if value.value is not None:
                reasons.add(value.value)
    assert reasons, "в функции не нашлось ни одной причины — сторож слеп"
    assert reasons - runtime.ENTRY_BLOCKS.keys() == set()
