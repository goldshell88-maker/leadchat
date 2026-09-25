"""Ответ на конкретное сообщение — наша собственная связь.

⚠ ПРОСЬБА ВЛАДЕЛЬЦА 02.09: «сделать так, чтобы были видны сообщения, на которые
люди ответили ответным сообщением, и чтобы я мог отвечать так же».

⚠ ЧЕСТНАЯ ГРАНИЦА, И ОНА ЗАФИКСИРОВАНА ЗДЕСЬ НАМЕРЕННО. Половина просьбы —
«на что ответил КЛИЕНТ» — невыполнима: у Авито цитирования в API нет. Проверено
тремя независимыми способами: 90 800 вебхуков (поля нет ни под каким именем),
153 492 сообщения в содержимом ответа ручки чтения (ни одного незнакомого
ключа — разбор складывает такие под их же именем), и полный перечень полей
верхнего уровня, снятый сторожем в бою 02.09: `author_id, content, created,
direction, id, isRead, type`. В приложении Авито цитирование есть, наружу не
отдаётся.

Делается вторая половина: на что ответил ДИСПЕТЧЕР. Замер боя за неделю: 5 575
из 21 196 наших ответов (26,3 %) уходят после двух и более сообщений клиента
подряд — там сейчас не остаётся никакого следа.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from app.models import Client, Conversation, Message


@pytest.fixture
async def api(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


async def отправить(
    api: httpx.AsyncClient, tokens: Any, conv_id: uuid.UUID, **тело: Any
) -> httpx.Response:
    return await api.post(
        f"/api/v1/conversations/{conv_id}/messages",
        json={"text": "Замена экрана 8 900 ₽", "client_message_id": str(uuid.uuid4()), **тело},
        headers=auth(tokens),
    )


async def test_ответ_запоминает_на_что_отвечали(api, tokens, seed_conversation, db_sessionmaker):
    """Основной случай: связь сохранена и отдана обратно вместе с цитатой."""
    r = await отправить(
        api,
        tokens,
        seed_conversation.conversation_id,
        reply_to_id=str(seed_conversation.message_id),
    )
    assert r.status_code == 201, r.text
    тело = r.json()
    assert тело["reply_to_id"] == str(seed_conversation.message_id)
    assert тело["reply_to"]["body"] == "Здравствуйте! Экран разбит, почём?"
    assert тело["reply_to"]["direction"] == "in"

    async with db_sessionmaker() as s:
        # По `id`, а не составным ключом: юнит-тесты идут на SQLite, где
        # партиционирования нет и ключ обычный.
        msg = (
            await s.execute(sa.select(Message).where(Message.id == uuid.UUID(тело["id"])))
        ).scalar_one()
        assert msg.reply_to_id == seed_conversation.message_id
        # ⚠ ПАРА, А НЕ ОДИН `id`. Без даты выборка цитаты обошла бы все 28
        # партиций `messages` вместо одной.
        assert msg.reply_to_created_at is not None


async def test_чужое_сообщение_процитировать_нельзя(
    api, tokens, seed_conversation, db_sessionmaker, make_avito_account
):
    """⚠ ГЛАВНАЯ ПРОВЕРКА ФАЙЛА: ЭТО ЗАЩИТА ОТ УТЕЧКИ ПЕРЕПИСКИ.

    Идентификатор приходит с клиента, а цитата показывается текстом в ленте.
    Прими мы чужой `id` — и в диалог одного человека уехал бы кусок разговора с
    другим, вместе с его словами. Отдельной проверки прав у сообщений нет: право
    читать переписку даёт диалог, значит и цитата обязана быть из ЭТОГО диалога.
    """
    account = await make_avito_account(avito_user_id=777888999)
    async with db_sessionmaker() as s:
        чужой_клиент = Client(channel="avito", external_id="999002", name="Другой человек")
        s.add(чужой_клиент)
        await s.flush()
        чужой = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=чужой_клиент.id,
            status="new",
            unread_count=0,
            last_message_at=datetime.now(UTC),
        )
        s.add(чужой)
        await s.flush()
        секрет = Message(
            conversation_id=чужой.id,
            external_message_id=f"am-{uuid.uuid4().hex[:8]}",
            direction="in",
            sender_type="client",
            body="Мой адрес: Ленина 5, квартира 12",
            attachments=[],
            delivery_status="delivered",
            created_at=datetime.now(UTC),
        )
        s.add(секрет)
        await s.commit()
        чужой_id = секрет.id

    r = await отправить(api, tokens, seed_conversation.conversation_id, reply_to_id=str(чужой_id))
    assert r.status_code == 422, "сообщение из чужого диалога процитировано — это утечка"
    поля = [f["field"] for f in r.json()["error"]["details"]["fields"]]
    assert "reply_to_id" in поля


async def test_несуществующее_сообщение_отвергается(api, tokens, seed_conversation):
    r = await отправить(
        api, tokens, seed_conversation.conversation_id, reply_to_id=str(uuid.uuid4())
    )
    assert r.status_code == 422


async def test_на_заметку_ответить_нельзя(api, tokens, seed_conversation, db_sessionmaker):
    """⚠ ЗАМЕТКУ НЕ ВИДЯТ НАБЛЮДАТЕЛИ БЕЗ ПРАВА `notes:read`.

    Цитата же едет в общем поле сообщения и прав не спрашивает: через неё
    снимок заметки утёк бы мимо ограничения — тому самому человеку, от которого
    заметку и прятали.
    """
    async with db_sessionmaker() as s:
        заметка = Message(
            conversation_id=seed_conversation.conversation_id,
            direction="note",
            sender_type="operator",
            body="Клиент скандальный, будь осторожнее",
            attachments=[],
            delivery_status="delivered",
            created_at=datetime.now(UTC) + timedelta(seconds=1),
        )
        s.add(заметка)
        await s.commit()
        note_id = заметка.id

    r = await отправить(api, tokens, seed_conversation.conversation_id, reply_to_id=str(note_id))
    assert r.status_code == 422, "снимок заметки уехал бы в цитате мимо права notes:read"


async def test_без_цитаты_всё_как_было(api, tokens, seed_conversation):
    """Обычная отправка не изменилась ни на поле."""
    r = await отправить(api, tokens, seed_conversation.conversation_id)
    assert r.status_code == 201, r.text
    assert r.json()["reply_to_id"] is None
    assert r.json()["reply_to"] is None


async def test_лента_отдаёт_цитату(api, tokens, seed_conversation):
    """Цитата видна и при перезагрузке ленты, а не только в кадре отправки."""
    await отправить(
        api,
        tokens,
        seed_conversation.conversation_id,
        reply_to_id=str(seed_conversation.message_id),
    )
    r = await api.get(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    ответы = [m for m in r.json()["items"] if m["reply_to_id"]]
    assert len(ответы) == 1
    assert ответы[0]["reply_to"]["body"] == "Здравствуйте! Экран разбит, почём?"


async def test_длинная_цитата_обрезается_на_сервере(
    api, tokens, seed_conversation, db_sessionmaker
):
    """⚠ ДЛИНА ЦИТАТЫ — СВОЙСТВО ПРОДУКТА, А НЕ ВКУС ЭКРАНА.

    Обрежь её на клиенте — и десктоп с браузером обрежут по-разному, а пузырь
    ответа превратится в пересказ сообщения клиента.
    """
    длинное = "А" * 300
    async with db_sessionmaker() as s:
        msg = Message(
            conversation_id=seed_conversation.conversation_id,
            external_message_id=f"am-{uuid.uuid4().hex[:8]}",
            direction="in",
            sender_type="client",
            body=длинное,
            attachments=[],
            delivery_status="delivered",
            created_at=datetime.now(UTC) + timedelta(seconds=2),
        )
        s.add(msg)
        await s.commit()
        длинный_id = msg.id

    r = await отправить(api, tokens, seed_conversation.conversation_id, reply_to_id=str(длинный_id))
    цитата = r.json()["reply_to"]
    assert len(цитата["body"]) == 100
    assert цитата["truncated"] is True


async def test_цитата_собирается_одним_запросом(api, tokens, seed_conversation, db_sessionmaker):
    """⚠ N+1 ПО 28 ПАРТИЦИЯМ — ЭТО НЕ ТЕОРИЯ, ЭТО ЗАМЕР.

    Собери цитату внутри сериализатора — и получится запрос на КАЖДОЕ сообщение
    страницы, каждый по партиционированной таблице, где одно только планирование
    стоит 85-100 мс на холодном соединении (замер боя 02.09).
    """
    for _ in range(5):
        await отправить(
            api,
            tokens,
            seed_conversation.conversation_id,
            reply_to_id=str(seed_conversation.message_id),
        )

    from app.services import conversations as convs

    вызовов = 0
    настоящий = convs._quoted_for

    async def счётчик(*a: Any, **kw: Any) -> Any:
        nonlocal вызовов
        вызовов += 1
        return await настоящий(*a, **kw)

    convs._quoted_for = счётчик  # type: ignore[assignment]
    try:
        r = await api.get(
            f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
            headers=auth(tokens),
        )
    finally:
        convs._quoted_for = настоящий  # type: ignore[assignment]

    assert r.status_code == 200
    assert len([m for m in r.json()["items"] if m["reply_to"]]) == 5
    assert вызовов == 1, f"цитаты собраны за {вызовов} проходов вместо одного"
