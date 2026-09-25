"""Догон межканального признака для тех, кто стал таким до выкатки.

ТРЕБОВАНИЕ ВЛАДЕЛЬЦА №6: «одни и тот же клиент пишет на разные аккаунты — не
видно, что это он». Склейка по `author_id` работала с первого дня, а видимость
появилась 11 августа и ставится ТОЛЬКО при создании нового диалога.

НАЙДЕНО НА БОЕВОЙ СИСТЕМЕ 12 августа: ровно один такой клиент — два диалога на
двух каналах, оба от 8 августа, то есть старше выкатки. Признак ему не
проставился и не проставился бы никогда: третьего диалога может не быть.
Оператор открывает карточку и не видит ничего — при том что случай ровно тот,
ради которого признак и заводили.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from app.models import Client, Conversation
from app.models.client import LINK_ASSUMED


def _диалог(client_id: uuid.UUID, account_id: uuid.UUID, chat: str) -> Conversation:
    return Conversation(
        id=uuid.uuid4(),
        channel="avito",
        external_chat_id=chat,
        account_id=account_id,
        client_id=client_id,
        status="closed",
        bot_active=False,
        bot_vars={},
        tags=[],
        unread_count=0,
        declined_by=[],
    )


@pytest.fixture
async def стенд(db_sessionmaker: Any, make_avito_account: Any) -> dict[str, Any]:
    первый = await make_avito_account(avito_user_id=990001, title="Дамир")
    второй = await make_avito_account(avito_user_id=990002, title="Тимофей")
    async with db_sessionmaker() as db:
        # Тот самый случай: один человек, два канала, признака нет.
        сквозной = Client(
            id=uuid.uuid4(), channel="avito", external_id="990100001", name="Пётр Сергеев"
        )
        # Обычный: один канал.
        обычный = Client(id=uuid.uuid4(), channel="avito", external_id="111222", name="Один канал")
        # Служебная карточка по чату: личность неизвестна, подписывать нечем.
        безымянный = Client(id=uuid.uuid4(), channel="avito", external_id="chat:u2i-XYZ")
        db.add_all([сквозной, обычный, безымянный])
        await db.flush()

        db.add_all(
            [
                _диалог(сквозной.id, первый.id, "chat-a"),
                _диалог(сквозной.id, второй.id, "chat-b"),
                _диалог(обычный.id, первый.id, "chat-c"),
                _диалог(безымянный.id, первый.id, "chat-d"),
                _диалог(безымянный.id, второй.id, "chat-e"),
            ]
        )
        await db.commit()
        return {"сквозной": сквозной.id, "обычный": обычный.id, "безымянный": безымянный.id}


async def _догнать(db_sessionmaker: Any) -> list[uuid.UUID]:
    """Тот же отбор, что в команде `backfill-cross-account`."""
    async with db_sessionmaker() as db:
        rows = (
            (
                await db.execute(
                    sa.select(Client.id)
                    .join(Conversation, Conversation.client_id == Client.id)
                    .where(
                        Client.cross_account_since.is_(None),
                        ~Client.external_id.like("chat:%"),
                    )
                    .group_by(Client.id)
                    .having(sa.func.count(sa.distinct(Conversation.account_id)) > 1)
                )
            )
            .scalars()
            .all()
        )
        if rows:
            await db.execute(
                sa.update(Client)
                .where(Client.id.in_(rows))
                .values(cross_account_since=datetime.now(UTC), link_confidence=LINK_ASSUMED)
            )
            await db.commit()
        return list(rows)


async def test_подписывается_только_настоящий_сквозной(
    db_sessionmaker: Any, стенд: dict[str, Any]
) -> None:
    найдены = await _догнать(db_sessionmaker)
    assert найдены == [стенд["сквозной"]]

    async with db_sessionmaker() as db:
        сквозной = await db.get(Client, стенд["сквозной"])
        обычный = await db.get(Client, стенд["обычный"])
        безымянный = await db.get(Client, стенд["безымянный"])

    assert сквозной is not None and сквозной.cross_account_since is not None
    # Мера доверия — «предположительно»: совпал один лишь идентификатор Авито.
    assert сквозной.link_confidence == LINK_ASSUMED
    assert обычный is not None and обычный.cross_account_since is None
    # Карточка по чату не подписывается: личность неизвестна. Однажды восемь
    # посторонних людей уже получили подпись «возможно, это тот же человек».
    assert безымянный is not None and безымянный.cross_account_since is None


async def test_повторный_прогон_ничего_не_трогает(
    db_sessionmaker: Any, стенд: dict[str, Any]
) -> None:
    """Догон обязан быть безопасен при повторе: его запускают руками."""
    await _догнать(db_sessionmaker)
    async with db_sessionmaker() as db:
        было = (await db.get(Client, стенд["сквозной"])).cross_account_since

    assert await _догнать(db_sessionmaker) == [], "второй прогон снова подписал уже подписанного"

    async with db_sessionmaker() as db:
        стало = (await db.get(Client, стенд["сквозной"])).cross_account_since
    assert стало == было, "дата склейки переписана повторным прогоном"
