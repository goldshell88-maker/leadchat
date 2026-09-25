"""Строка диалога свободна, пока модель думает (проверка 24.09) — на PostgreSQL.

Тик держал `SELECT … FOR UPDATE` строки диалога всё время похода в модель: до
15 секунд вебхук нового сообщения клиента, «Принять» и отправка оператора по
этому диалогу ждали. SQLite блокировок строк не знает, поэтому свойство
проверяется здесь: пока модель думает, другой сеанс берёт ту же строку с
`NOWAIT` — со старым тиком это падало сразу, «строка занята».

ДИВЕРСИИ: вернуть вызов модели внутрь первой транзакции (`defer_ai=False` в
`_run_tick`) или классификатор под `FOR UPDATE` в `_classify_after_commit` —
краснеет первый тест; вернуть `skip_locked` в `release_bot_dialogs` — второй.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import fakeredis.aioredis
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.bots.runtime import bot_step, release_bot_dialogs
from app.models import AvitoAccount, Bot, Client, Conversation, Message
from app.scheduler.partitions import ensure_message_partitions
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

CLEANUP = (
    "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
    "clients, avito_accounts, bots, users CASCADE"
)
AI_THEN_WAIT = {
    "version": 1,
    "revision": 1,
    "entry": "ai",
    "steps": [
        {"id": "ai", "type": "ai_answer", "params": {}, "next": "wait"},
        {"id": "wait", "type": "ask", "params": {"text": None, "timeout": "25m"}, "next": "ai"},
    ],
}
REPLY = "Замена экрана — от 8900 ₽"


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> AsyncIterator[None]:
    """Чисто и ДО, и ПОСЛЕ: набор обязан вернуть базу такой, какой взял."""
    async with pg_engine.begin() as conn:
        await conn.execute(text(CLEANUP))
    yield
    async with pg_engine.begin() as conn:
        await conn.execute(text(CLEANUP))


class ThinkingAI:
    """Модель, которая, пока думает, пробует взять строку диалога другим сеансом."""

    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession], conv_id: uuid.UUID):
        self.sessionmaker = sessionmaker
        self.conv_id = conv_id
        self.row_while_thinking: str | None = None
        self.row_while_classifying: str | None = None

    async def _row(self) -> str:
        try:
            async with self.sessionmaker() as s, s.begin():
                await s.execute(
                    text("SELECT id FROM conversations WHERE id = :id FOR UPDATE NOWAIT"),
                    {"id": self.conv_id},
                )
        except DBAPIError:
            return "locked"
        return "free"

    async def ai_answer(self, bot: Any, dialog: Any, item_title: Any, **_kw: Any) -> dict:
        self.row_while_thinking = await self._row()
        return {"reply": REPLY, "confidence": 0.95, "needs_operator": False}

    async def classify_message(self, texts: list[str]) -> dict:
        self.row_while_classifying = await self._row()
        return {"sentiment": "neutral"}


async def _world(sessionmaker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    now = datetime.now(UTC)
    async with sessionmaker() as s, s.begin():
        bot = Bot(
            name="Первичный приём",
            is_enabled=True,
            schedule={"always": True},
            scenario=AI_THEN_WAIT,
            knowledge_base="Замена экрана — от 8900 ₽",
            mode="auto",
        )
        s.add(bot)
        await s.flush()
        account = AvitoAccount(
            title="Канал",
            avito_user_id=777_501,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=now + timedelta(days=1),
            status="active",
            webhook_secret="s",
            bot_id=bot.id,
        )
        client = Client(channel="avito", external_id="two-phase-cli", name="Клиент")
        s.add_all([account, client])
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-two-phase",
            account_id=account.id,
            client_id=client.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            last_message_at=now,
        )
        s.add(conv)
        await s.flush()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id="m-two-phase-1",
                direction="in",
                sender_type="client",
                body="Сколько стоит экран?",
                attachments=[],
                delivery_status="delivered",
                created_at=now,
            )
        )
        return conv.id


async def test_the_conversation_row_is_free_while_the_model_thinks(
    pg_engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    await ensure_message_partitions(pg_engine)
    conv_id = await _world(sessionmaker)
    ai = ThinkingAI(sessionmaker, conv_id)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    ctx = {"db_session_factory": sessionmaker, "redis": redis, "bot_ai": ai}

    assert await bot_step(ctx, conv_id, "Сколько стоит экран?") == "ok"

    assert ai.row_while_thinking == "free", "тик держал строку диалога, пока модель думала"
    assert ai.row_while_classifying == "free", "строка занята, пока думает классификатор"
    async with sessionmaker() as s:
        sent = (
            await s.execute(
                select(Message.body).where(
                    Message.conversation_id == conv_id, Message.sender_type == "bot"
                )
            )
        ).scalars()
        assert list(sent) == [REPLY]
    await redis.aclose()


async def test_releasing_a_bot_waits_for_its_tick_instead_of_skipping_the_dialog(
    pg_engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Диалог ведёт бот; вторая транзакция тика держит строку, пока применяет
    ответ модели. Выключение с `SKIP LOCKED` этот диалог пропускало — и он
    оставался спрятанным за выключенным ботом. Теперь выключение ждёт тик."""
    await ensure_message_partitions(pg_engine)
    conv_id = await _world(sessionmaker)
    async with sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_id)
        assert conv is not None
        conv.bot_active = True
        conv.bot_vars = {"step": "ai", "vars": {}}
        account_id = conv.account_id

    tick = sessionmaker()
    try:
        await tick.begin()
        await tick.execute(select(Conversation).where(Conversation.id == conv_id).with_for_update())

        async def disable() -> None:
            async with sessionmaker() as s, s.begin():
                await release_bot_dialogs(s, account_ids={account_id})

        released = asyncio.create_task(disable())
        await asyncio.sleep(0.5)
        waited = not released.done()
        await tick.commit()
    finally:
        await tick.close()
    await released

    assert waited, "выключение не дождалось тика"
    async with sessionmaker() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv is not None and conv.bot_active is False
