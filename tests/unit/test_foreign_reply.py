"""Ответ в чужой диалог оставляет след (SCEN-49).

Отвечать в диалог коллеги не запрещено и запрещать нечем: диспетчер
подхватывает клиента ушедшего на обед — это нормальная работа. Плохо было
другое: следа не оставалось НИКАКОГО. Ответственный не менялся, «Мои» и
статистика первого ответа оставались за первым, а человек, за которым диалог
числится, не узнавал, что клиенту уже ответили, — и отвечал вторым.
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import AuditLog, Conversation
from tests.unit.conftest import drain_events


@pytest.fixture
async def api(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


async def set_assignee(db_sessionmaker, conv_id, user_id) -> None:
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv is not None
        conv.assignee_id = user_id
        conv.status = "in_progress"
        await s.commit()


async def send(api, tokens, conv_id, *, role: str = "manager") -> httpx.Response:
    return await api.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"text": "Подскажу: замена экрана от 8 900 ₽", "client_message_id": str(uuid.uuid4())},
        headers=auth(tokens, role),
    )


async def audit_actions(db_sessionmaker, conv_id) -> list[str]:
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(select(AuditLog).where(AuditLog.entity_id == str(conv_id)))
        ).scalars()
        return [r.action for r in rows]


async def test_reply_into_someone_elses_dialog_is_recorded(
    api, tokens, users_by_role, seed_conversation, db_sessionmaker
):
    """Журнал знает, что клиенту ответил не ответственный."""
    owner = users_by_role["admin"]
    await set_assignee(db_sessionmaker, seed_conversation.conversation_id, owner.id)

    r = await send(api, tokens, seed_conversation.conversation_id, role="manager")
    assert r.status_code == 201, r.text

    assert "conversation.foreign_reply" in await audit_actions(
        db_sessionmaker, seed_conversation.conversation_id
    )
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv is not None
        # Ответственный НЕ меняется: переназначение молча — отдельная беда,
        # диалог просто перестал бы находиться у того, кто его вёл.
        assert conv.assignee_id == owner.id


async def test_the_assignee_is_told(
    api, tokens, users_by_role, seed_conversation, db_sessionmaker, redis
):
    """Тот, за кем диалог, узнаёт об этом — и лично, а не всей командой."""
    owner = users_by_role["admin"]
    await set_assignee(db_sessionmaker, seed_conversation.conversation_id, owner.id)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await send(api, tokens, seed_conversation.conversation_id, role="manager")

    notes = [e for e in await drain_events(pubsub) if e["type"] == "notify"]
    assert len(notes) == 1
    assert notes[0]["meta"]["only_user"] == str(owner.id)
    assert notes[0]["data"]["level"] == "warning"
    assert users_by_role["manager"].full_name in notes[0]["data"]["text"]


async def test_reply_in_your_own_dialog_says_nothing(
    api, tokens, users_by_role, seed_conversation, db_sessionmaker, redis
):
    """Свой диалог — обычная работа: ни записи в журнале, ни тоста."""
    me = users_by_role["manager"]
    await set_assignee(db_sessionmaker, seed_conversation.conversation_id, me.id)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await send(api, tokens, seed_conversation.conversation_id, role="manager")

    assert "conversation.foreign_reply" not in await audit_actions(
        db_sessionmaker, seed_conversation.conversation_id
    )
    assert [e for e in await drain_events(pubsub) if e["type"] == "notify"] == []


async def test_first_reply_into_a_free_dialog_is_not_foreign(
    api, tokens, users_by_role, seed_conversation, db_sessionmaker, redis
):
    """Бесхозный диалог: ответил — значит взял (01 §6.2), и это не «чужой».

    Снимок ответственного берётся ДО автоназначения именно ради этого случая:
    после него ответственным станет сам автор, и отличить «взял себе» от
    «ответил в чужой» было бы уже нечем.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await send(api, tokens, seed_conversation.conversation_id, role="manager")

    actions = await audit_actions(db_sessionmaker, seed_conversation.conversation_id)
    assert "conversation.assigned" in actions
    assert "conversation.foreign_reply" not in actions
    assert [e for e in await drain_events(pubsub) if e["type"] == "notify"] == []
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        assert conv is not None
        assert conv.assignee_id == users_by_role["manager"].id
