"""Удаление заметки через НАСТОЯЩУЮ ручку (правка владельца 17.08).

⚠ ТЕСТ ХОДИТ ПО HTTP, А НЕ ЗОВЁТ ФУНКЦИЮ. Первая версия ручки читала
сообщение через `db.get(Message, id)` — и падала пятисоткой на боевом:
у `messages` СОСТАВНОЙ первичный ключ (id, created_at), таблица
секционирована по дате. Юнит на функции этого не увидел бы; увидел
человек, которому интерфейс сказал «удалить можно только свою заметку».
"""

import uuid
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from app.models import Message


@pytest.fixture
async def api(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


async def _make_note(api, tokens, conv_id, role="manager") -> str:
    r = await api.post(
        f"/api/v1/conversations/{conv_id}/notes",
        json={"text": "Клиент просил перезвонить после 18", "client_message_id": str(uuid.uuid4())},
        headers=auth(tokens, role),
    )
    assert r.status_code == 201, r.text
    body = r.json()
    return (body.get("message") or body)["id"]


async def test_author_deletes_own_note(api, tokens, seed_conversation, db_sessionmaker):
    note_id = await _make_note(api, tokens, seed_conversation.conversation_id)

    r = await api.delete(f"/api/v1/messages/{note_id}", headers=auth(tokens))
    assert r.status_code == 204, r.text

    async with db_sessionmaker() as db:
        left = (await db.execute(select(Message).where(Message.id == uuid.UUID(note_id)))).first()
        assert left is None, "заметка обязана исчезнуть из базы"


async def test_someone_elses_note_survives(api, tokens, seed_conversation):
    note_id = await _make_note(api, tokens, seed_conversation.conversation_id, role="manager")

    r = await api.delete(f"/api/v1/messages/{note_id}", headers=auth(tokens, "head"))
    assert r.status_code == 403, r.text


async def test_client_message_is_untouchable(api, tokens, seed_conversation, db_sessionmaker):
    async with db_sessionmaker() as db:
        msg_id = (
            await db.execute(
                select(Message.id)
                .where(Message.conversation_id == seed_conversation.conversation_id)
                .where(Message.direction == "in")
                .limit(1)
            )
        ).scalar_one_or_none()
    if msg_id is None:
        pytest.skip("в фикстуре нет входящего сообщения")

    r = await api.delete(f"/api/v1/messages/{msg_id}", headers=auth(tokens))
    assert r.status_code == 409, r.text
