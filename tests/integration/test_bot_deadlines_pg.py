"""Сторож потерянных дедлайнов бота на настоящем PostgreSQL (проверка 24.09).

Юнит-тесты (tests/unit/test_bot_deadlines_2409.py) идут на SQLite, а там нет
ни `FOR UPDATE SKIP LOCKED`, ни `NULLS FIRST` в той форме, что строит запрос.
Здесь проверяется главное свойство блокировки: диалог, который держит живой
тик, сторож пропускает, а не ждёт и не трогает.
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

from app.bots.runtime import BOT_TIMEOUT_JOB
from app.models import AvitoAccount, Client, Conversation
from app.scheduler.jobs import bot_deadlines
from app.scheduler.partitions import ensure_message_partitions
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

CLEANUP = (
    "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
    "clients, avito_accounts, users CASCADE"
)


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


async def _dialogs(
    pg_engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession], now: datetime
) -> dict[str, uuid.UUID]:
    """Диалоги бота с дедлайнами по разные стороны от порогов сторожа."""
    await ensure_message_partitions(pg_engine)
    ids: dict[str, uuid.UUID] = {}
    async with sessionmaker() as s:
        account = AvitoAccount(
            title="Канал",
            avito_user_id=777_401,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=now + timedelta(days=1),
            status="active",
            webhook_secret="s",
        )
        client = Client(channel="avito", external_id="deadline-cli", name="Клиент")
        s.add_all([account, client])
        await s.flush()
        for key, overdue, status in (
            ("lost", timedelta(minutes=15), "new"),
            ("hopeless", timedelta(hours=2), "new"),
            ("fresh", timedelta(minutes=-5), "new"),
            ("closed", timedelta(hours=2), "closed"),
            ("under_tick", timedelta(minutes=15), "new"),
        ):
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{key}",
                account_id=account.id,
                client_id=client.id,
                status=status,
                bot_active=True,
                bot_vars={
                    "step": "ask_problem",
                    "waiting": {
                        "kind": "ask",
                        "var": "problem",
                        "token": f"token-{key}",
                        "deadline": (now - overdue).isoformat(),
                    },
                },
                last_message_at=now - overdue - timedelta(minutes=20),
            )
            s.add(conv)
            await s.flush()
            ids[key] = conv.id
        await s.commit()
    return ids


async def test_the_sweep_skips_a_dialog_held_by_a_live_tick(
    pg_engine: AsyncEngine, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    now = datetime.now(UTC).replace(microsecond=0)
    ids = await _dialogs(pg_engine, sessionmaker, now)

    async with sessionmaker() as tick, tick.begin():
        # Живой тик держит свою строку (`_conversation_for_update`).
        await tick.execute(
            select(Conversation).where(Conversation.id == ids["under_tick"]).with_for_update()
        )
        async with sessionmaker() as s, s.begin():
            outbox, picked = await bot_deadlines.sweep_in_session(s, now=now)

    assert [(j.name, j.args) for j in outbox.jobs] == [
        (BOT_TIMEOUT_JOB, (ids["lost"], "token-lost"))
    ]
    assert picked == 2, "лишний или пропущенный диалог"
    async with sessionmaker() as s:
        rows = {
            c.id: c
            for c in (
                await s.execute(select(Conversation).where(Conversation.id.in_(ids.values())))
            ).scalars()
        }
    assert rows[ids["hopeless"]].bot_active is False
    assert rows[ids["hopeless"]].offered_at is not None, "клиент не встал в очередь"
    assert rows[ids["closed"]].status == "closed" and rows[ids["closed"]].offered_at is None
    for key in ("lost", "fresh", "closed", "under_tick"):
        assert rows[ids[key]].bot_active is True, key
