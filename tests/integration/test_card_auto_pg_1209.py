"""Автоматика карточки на Postgres (12.09): миграция 0073 и гонка объединений.

ГОНКА. Воркер объединяет A←B по телефону, а оператор в ту же секунду нажимает
«Объединить» B←A. Без `FOR UPDATE` в `merge_clients` обе транзакции проходили
бы сторожей по старым строкам и фиксировались: кольцо A↔B, по которому
`_upsert_client` ходит кругами. С замком вторая транзакция дожидается первой,
перечитывает карточки и получает честный отказ `already_merged` /
`target_is_merged`.

⚠ ДИВЕРСИЯ: убрать `lock_cards` из `merge_clients` — тест краснеет на кольце.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.core.errors import ApiError
from app.models import AvitoAccount, Client, ClientPhoneCandidate, Conversation, Message, User
from app.services import clients as clients_svc

pytestmark = pytest.mark.anyio

PHONE = "+79151234567"


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> None:
    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE messages, conversations, clients, avito_accounts, users, "
                "audit_log, client_merge_vetoes CASCADE"
            )
        )


async def _пара(sessionmaker) -> tuple[Client, Client, User]:
    """Две карточки с одним доказанным номером на двух аккаунтах, оператор."""
    now = datetime.now(UTC)
    async with sessionmaker() as s:
        user = User(
            email="op@card.test", password_hash="x", full_name="Оп", role="manager", is_active=True
        )
        s.add(user)
        accs = []
        for i in (1, 2):
            acc = AvitoAccount(
                title=f"acc{i}",
                avito_user_id=100 + i,
                access_token_enc=b"a",
                refresh_token_enc=b"r",
                token_expires_at=now,
                status="active",
                webhook_secret="s",
            )
            s.add(acc)
            accs.append(acc)
        await s.flush()
        cards = []
        for i, acc in enumerate(accs, start=1):
            card = Client(channel="avito", external_id=f"500{i}", name="Иван Петров", phone=PHONE)
            s.add(card)
            await s.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{i}",
                account_id=acc.id,
                client_id=card.id,
                status="new",
                last_message_at=now,
            )
            s.add(conv)
            await s.flush()
            s.add(
                Message(
                    conversation_id=conv.id,
                    external_message_id=f"m-{i}",
                    direction="in",
                    sender_type="client",
                    body=PHONE,
                    attachments=[],
                    delivery_status="delivered",
                    created_at=now,
                )
            )
            s.add(
                ClientPhoneCandidate(
                    client_id=card.id,
                    conversation_id=conv.id,
                    phone=PHONE,
                    raw=PHONE,
                    source="inbound",
                    status="accepted",
                    detected_at=now,
                    resolved_at=now,
                    message_at=now,
                )
            )
            cards.append(card)
        await s.commit()
        return cards[0], cards[1], user


async def test_миграция_0073_на_месте(pg_engine: AsyncEngine) -> None:
    async with pg_engine.connect() as conn:
        таблицы = {
            r[0]
            for r in await conn.execute(
                text("SELECT table_name FROM information_schema.tables WHERE table_schema='public'")
            )
        }
        assert "client_merge_vetoes" in таблицы
        колонки = {
            (r[0], r[1])
            for r in await conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE column_name IN ('hint','origin_client_id')"
                )
            )
        }
        assert ("client_phone_candidates", "hint") in колонки
        assert ("conversations", "origin_client_id") in колонки


async def test_встречные_объединения_не_дают_кольца(sessionmaker, redis) -> None:
    a, b, user = await _пара(sessionmaker)

    async def объединить(winner_id: uuid.UUID, loser_id: uuid.UUID, *, задержка: float):
        async with sessionmaker() as s:
            async with s.begin():
                w = await s.get(Client, winner_id)
                lo = await s.get(Client, loser_id)
                try:
                    # Замок берётся ВНУТРИ merge_clients; задержка — чтобы вторая
                    # транзакция гарантированно упёрлась в первую, а не прошла раньше.
                    result = await clients_svc.merge_clients(
                        s, winner=w, loser=lo, actor=user, auto=False
                    )
                    await asyncio.sleep(задержка)
                    return ("ok", result["moved_conversations"])
                except ApiError as exc:
                    return ("err", exc.code)

    итоги = await asyncio.gather(
        объединить(a.id, b.id, задержка=0.5),
        объединить(b.id, a.id, задержка=0.0),
    )
    статусы = sorted(str(и[0]) for и in итоги)
    assert статусы == ["err", "ok"], итоги
    отказ = next(и for и in итоги if и[0] == "err")
    assert отказ[1] in ("already_merged", "target_is_merged")

    async with sessionmaker() as s:
        a2 = await s.get(Client, a.id)
        b2 = await s.get(Client, b.id)
    # Ровно одна карточка объединена, и не в ту, что объединена сама.
    объединённые = [c for c in (a2, b2) if c.merged_into_id is not None]
    assert len(объединённые) == 1
    (loser,) = объединённые
    winner = a2 if loser is b2 else b2
    assert loser.merged_into_id == winner.id and winner.merged_into_id is None
