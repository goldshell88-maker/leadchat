"""Догон межканального признака — на НАСТОЯЩЕЙ базе, а не на SQLite.

ЗАЧЕМ ОТДЕЛЬНЫЙ ИНТЕГРАЦИОННЫЙ. Юнит на SQLite этот запрос пропускал: там
`min(uuid)` существует, а в Postgres — нет, и команда падала на живой базе,
пройдя все юниты. Поймано запуском, а не тестом; поэтому запрос теперь
проверяется тем же двигателем, на котором работает.

Правило шире одного случая: команды, которые ходят в базу агрегатами и
группировками, обязаны проверяться на Postgres — диалекты расходятся молча.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.models import AvitoAccount, Client, Conversation
from app.models.client import LINK_ASSUMED


def _канал(uid: int, title: str) -> AvitoAccount:
    from datetime import UTC, datetime, timedelta

    return AvitoAccount(
        id=uuid.uuid4(),
        title=title,
        avito_user_id=uid,
        access_token_enc=b"a",
        refresh_token_enc=b"r",
        token_expires_at=datetime.now(UTC) + timedelta(days=1),
        status="active",
        webhook_secret="s",
    )


def _диалог(client_id: uuid.UUID, account_id: uuid.UUID, chat: str) -> Conversation:
    return Conversation(
        id=uuid.uuid4(),
        channel="avito",
        external_chat_id=chat,
        account_id=account_id,
        client_id=client_id,
        status="closed",
    )


@pytest.fixture
async def sessionmaker(pg_async_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_отбор_догона_работает_на_postgres(
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    первый, второй = _канал(994001, "Дамир"), _канал(994002, "Тимофей")
    сквозной = Client(id=uuid.uuid4(), channel="avito", external_id="pg-990100001", name="Пётр")
    обычный = Client(id=uuid.uuid4(), channel="avito", external_id="pg-111", name="Один")

    async with sessionmaker() as db:
        db.add_all([первый, второй, сквозной, обычный])
        await db.flush()
        db.add_all(
            [
                _диалог(сквозной.id, первый.id, "pg-chat-a"),
                _диалог(сквозной.id, второй.id, "pg-chat-b"),
                _диалог(обычный.id, первый.id, "pg-chat-c"),
            ]
        )
        await db.commit()

    # Запрос — построчно тот же, что в `cli.backfill_cross_account`.
    async with sessionmaker() as db:
        rows = (
            await db.execute(
                sa.select(
                    Client.id,
                    Client.external_id,
                    Client.name,
                    sa.func.count(sa.distinct(Conversation.account_id)).label("каналов"),
                )
                .join(Conversation, Conversation.client_id == Client.id)
                .where(
                    Client.cross_account_since.is_(None),
                    ~Client.external_id.like("chat:%"),
                )
                .group_by(Client.id, Client.external_id, Client.name)
                .having(sa.func.count(sa.distinct(Conversation.account_id)) > 1)
            )
        ).all()

    найдены = {r.id for r in rows}
    assert сквозной.id in найдены
    assert обычный.id not in найдены
    assert next(r.каналов for r in rows if r.id == сквозной.id) == 2

    # И сама запись меры доверия проходит на Postgres.
    async with sessionmaker() as db:
        await db.execute(
            sa.update(Client).where(Client.id == сквозной.id).values(link_confidence=LINK_ASSUMED)
        )
        await db.commit()
        обновлён = await db.get(Client, сквозной.id)
    assert обновлён is not None and обновлён.link_confidence == LINK_ASSUMED
