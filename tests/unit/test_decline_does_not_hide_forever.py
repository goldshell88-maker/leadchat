"""Отказ не должен прятать клиента навсегда.

НАЙДЕНО НА БОЕВОЙ СИСТЕМЕ 12 августа. Отказ персональный: он убирает диалог из
«Входящих» того, кто отказался. Снимался он в `enter_queue`, `return_to_queue` и
автораздаче — но НЕ при взятии. Получалось противоречие: человек диалог ВЗЯЛ,
а система продолжала считать, что он от него отказался.

Живой пострадавший: клиент Иван отклонён в 03:25, дальше диалог трижды
принимали и возвращали в очередь — и он всё равно оставался невидимым во
«Входящих», вися ничьим с тикающим таймером 7 часов 37 минут. Взять его можно
было только случайно найдя во вкладке «Все»; снять отказ — исключительно
кнопкой в тосте, который живёт несколько секунд.

Проверка ломанием: убрать `declined_by=[]` из запроса взятия — падает
``test_claim_clears_the_decline``.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models import Conversation, User
from app.services import inbox as inbox_svc


class _FakeRedis:
    async def publish(self, *_a: Any, **_kw: Any) -> int:
        return 0

    async def set(self, *_a: Any, **_kw: Any) -> bool:
        return True

    async def get(self, *_a: Any, **_kw: Any) -> None:
        return None

    async def delete(self, *_a: Any) -> int:
        return 1

    async def incr(self, *_a: Any, **_kw: Any) -> int:
        return 1

    async def expire(self, *_a: Any, **_kw: Any) -> bool:
        return True


@pytest.fixture
async def operator(db_sessionmaker: Any) -> User:
    async with db_sessionmaker() as db:
        user = User(
            id=uuid.uuid4(),
            email="operator@leadpartner.ru",
            full_name="Оператор",
            role="manager",
            password_hash="x",
            is_active=True,
            handles_conversations=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


@pytest.fixture
async def queued(db_sessionmaker: Any, make_avito_account: Any) -> Conversation:
    from datetime import UTC, datetime

    from app.models import Client

    account = await make_avito_account(avito_user_id=990011223)
    async with db_sessionmaker() as db:
        client = Client(id=uuid.uuid4(), channel="avito", external_id="777001", name="Иван")
        db.add(client)
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id="chat-declined",
            account_id=account.id,
            client_id=client.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=1,
            declined_by=[],
            offered_at=datetime.now(UTC),
        )
        db.add(conv)
        await db.commit()
        await db.refresh(conv)
        return conv


async def test_claim_clears_the_decline(
    db_sessionmaker: Any, queued: Conversation, operator: User
) -> None:
    """Взял — значит не отказывался."""
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, queued.id)
        assert conv is not None
        conv.declined_by = [str(operator.id)]
        await db.commit()

    async with db_sessionmaker() as db:
        await inbox_svc.claim(db, queued.id, operator)
        await db.commit()

    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, queued.id)
        assert conv is not None
        assert conv.assignee_id == operator.id
        assert list(conv.declined_by or []) == [], "взятый диалог не может числиться отклонённым"


async def test_declined_dialog_is_hidden_only_from_the_one_who_declined(
    db_sessionmaker: Any, queued: Conversation, operator: User
) -> None:
    """Отказ персональный — это НЕ дефект, а решение. Закрепляем его явно,
    чтобы починка выше не превратилась в «отказ ничего не значит»."""
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, queued.id)
        assert conv is not None
        await inbox_svc.decline(db, conv.id, operator)
        await db.commit()

    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, queued.id)
        assert conv is not None
        assert str(operator.id) in [str(x) for x in (conv.declined_by or [])]
        # И он по-прежнему в очереди: отказ прячет его от отказавшегося,
        # а не выкидывает из очереди вовсе.
        assert conv.offered_at is not None
