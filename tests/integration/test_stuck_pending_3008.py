"""Сообщение не остаётся «Отправляется» навсегда (аудит 30.08).

⚠ ПОЧЕМУ НАСТОЯЩАЯ БАЗА, А НЕ РАЗБОР ИСХОДНИКА. Существующий сторож
`test_lost_enqueue_and_voice_2708.py` проверяет этот же путь через
`inspect.getsource` — и потому не заметил, что страховка падает на первой же
строке. У таблицы `messages` СОСТАВНОЙ первичный ключ (id, created_at): она
секционирована по дате, и `db.get(Message, id)` кидает InvalidRequestError.

Цена дефекта несимметрична. `mark_enqueue_failed` вызывается ИМЕННО ТОГДА,
когда задача доставки не встала в очередь. Падая, она оставляла сообщение
навсегда в `pending`: его не видит ни `/retry` (берёт только `failed`), ни
повторный POST (уходит в replay), ни красная метка диалога. А фронт на 500
находил в ленте уже опубликованного близнеца и показывал «Сообщение ушло …
клиент его получил — повторять не нужно». Клиент не получал ничего.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.models import AvitoAccount, Client, Conversation, Message
from app.scheduler.partitions import ensure_message_partitions
from app.services.messages import mark_enqueue_failed

pytestmark = [pytest.mark.asyncio]


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


ЧИСТКА = (
    "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
    "clients, avito_accounts, users CASCADE"
)


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> AsyncIterator[None]:
    """Чисто и ДО, и ПОСЛЕ: набор обязан вернуть базу такой, какой взял."""
    async with pg_engine.begin() as conn:
        await conn.execute(text(ЧИСТКА))
    yield
    async with pg_engine.begin() as conn:
        await conn.execute(text(ЧИСТКА))


@pytest.fixture
async def сообщение(pg_engine: AsyncEngine, sessionmaker) -> uuid.UUID:
    """Одно исходящее в состоянии «Отправляется»."""
    сейчас = datetime.now(UTC)
    await ensure_message_partitions(pg_engine)
    async with sessionmaker() as s:
        acc = AvitoAccount(
            title="Канал",
            avito_user_id=777_301,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=сейчас + timedelta(days=1),
            status="active",
            webhook_secret="s",
        )
        s.add(acc)
        cli = Client(channel="avito", external_id="stuck-cli-1", name="Клиент")
        s.add(cli)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-stuck",
            account_id=acc.id,
            client_id=cli.id,
            status="in_progress",
            last_message_at=сейчас,
        )
        s.add(conv)
        await s.flush()
        msg = Message(
            conversation_id=conv.id,
            direction="out",
            sender_type="operator",
            body="Мастер подъедет к 15:00",
            attachments=[],
            delivery_status="pending",
            created_at=сейчас,
        )
        s.add(msg)
        await s.commit()
        return msg.id


async def статус(sessionmaker, message_id: uuid.UUID) -> str:
    async with sessionmaker() as s:
        return (
            await s.execute(select(Message.delivery_status).where(Message.id == message_id))
        ).scalar_one()


async def test_страховка_переводит_зависшее_в_отказ(sessionmaker, сообщение) -> None:
    """⚠ ГЛАВНАЯ ПРОВЕРКА: функция обязана ОТРАБОТАТЬ, а не упасть."""
    async with sessionmaker() as s:
        сработала = await mark_enqueue_failed(s, сообщение)

    assert сработала is True, "страховка не сработала — сообщение осталось «Отправляется» навсегда"
    assert await статус(sessionmaker, сообщение) == "failed", (
        "статус не переведён в «не отправлено»: ни /retry, ни красная метка его не увидят"
    )


async def test_повторный_вызов_ничего_не_ломает(sessionmaker, сообщение) -> None:
    """Задача доставки могла встать со второй попытки — двойной вызов безопасен."""
    async with sessionmaker() as s:
        assert await mark_enqueue_failed(s, сообщение) is True
    async with sessionmaker() as s:
        assert await mark_enqueue_failed(s, сообщение) is False, (
            "второй вызов снова пометил отказом — счётчики повторов разъедутся"
        )


async def test_чужой_идентификатор_не_роняет(sessionmaker) -> None:
    """Сообщения нет — спокойное «нечего помечать», а не исключение."""
    async with sessionmaker() as s:
        assert await mark_enqueue_failed(s, uuid.uuid4()) is False
