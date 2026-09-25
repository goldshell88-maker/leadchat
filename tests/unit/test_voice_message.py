"""Голосовое сообщение можно послушать (жалоба владельца 19.08).

ЧТО БЫЛО. Авито присылает голосовое ОДНИМ идентификатором — самой записи в
сообщении нет, за ней надо идти отдельным запросом, которого мы не делали.
В ленте была строка «Голосовое сообщение» без ссылки: клиент говорит, а мы не
слышим. На бою таких сообщений 174, и в голосовом обычно и есть суть заказа.

Здесь же заперто ПРАВО на эту ручку: она не в общей RBAC-матрице, потому что
её успешная ветка требует и вложения-голоса, и ответа Авито (см. пометку в
COVERED_ELSEWHERE).
"""

from __future__ import annotations

import uuid

import httpx
import pytest

from app.integrations.avito.client import AvitoClient


@pytest.fixture
def без_похода_в_авито(monkeypatch):
    """Авито отвечает ссылкой — саму площадку в модульном тесте не трогаем.

    Токен в тестовом канале — заглушка (`b"enc-access"`), настоящей расшифровке
    не поддаётся; подменяем и её. Проверяем ПРАВО и путь до ответа, а не
    криптографию — она заперта своими тестами.
    """
    from app.services import crypto

    async def ссылки(self, token, user_id, voice_ids):  # noqa: ANN001, ANN202
        return {voice_ids[0]: f"https://avito.example/{voice_ids[0]}.mp3"}

    monkeypatch.setattr(AvitoClient, "get_voice_urls", ссылки)
    monkeypatch.setattr(crypto, "decrypt_token", lambda _: "тестовый-токен")


async def _голосовое(db_sessionmaker, seed_conversation) -> uuid.UUID:
    from app.models import Message

    async with db_sessionmaker() as db:
        msg = Message(
            id=uuid.uuid4(),
            conversation_id=seed_conversation.conversation_id,
            direction="in",
            sender_type="client",
            body=None,
            attachments=[
                {
                    "media_id": "avito_voice_2229d5a7",
                    "kind": "file",
                    "name": "Голосовое сообщение",
                    "avito_type": "voice",
                }
            ],
            delivery_status="delivered",
        )
        db.add(msg)
        await db.commit()
        return msg.id


async def test_запись_отдаётся_тому_кто_видит_диалог(
    client: httpx.AsyncClient, tokens, db_sessionmaker, seed_conversation, без_похода_в_авито
):
    """Кто видит переписку — тот и слышит. Отдельного права заводить не за что."""
    message_id = await _голосовое(db_sessionmaker, seed_conversation)

    for роль in ("admin", "head", "manager", "observer"):
        ответ = await client.get(
            f"/api/v1/messages/{message_id}/voice",
            headers={"Authorization": f"Bearer {tokens[роль]}"},
        )
        assert ответ.status_code == 200, (роль, ответ.text)
        assert ответ.json()["url"].endswith(".mp3")


async def test_без_входа_запись_не_отдаётся(
    client: httpx.AsyncClient, db_sessionmaker, seed_conversation, без_похода_в_авито
):
    """Ссылка на речь клиента — не публичный адрес."""
    message_id = await _голосовое(db_sessionmaker, seed_conversation)

    ответ = await client.get(f"/api/v1/messages/{message_id}/voice")

    assert ответ.status_code == 401


async def test_в_сообщении_без_голоса_честный_отказ(
    client: httpx.AsyncClient, tokens, seed_conversation, без_похода_в_авито
):
    """Обычное сообщение — 404 с объяснением, а не пустая ссылка."""
    ответ = await client.get(
        f"/api/v1/messages/{seed_conversation.message_id}/voice",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )

    assert ответ.status_code == 404
    assert "голосов" in ответ.json()["error"]["message"].lower()
