"""Удаление сотрудника не растёт вместе с числом его диалогов.

⚠ ЧТО БЫЛО. Все открытые диалоги удаляемого возвращаются в очередь, и на каждый
собирался кадр `inbox:new` — поштучным `inbox_frame_addressed`. Он делает свой
`_load_related` (аккаунт, клиент, ответственный, последнее сообщение) и свой
запрос допущенных операторов: пять-шесть рейсов до базы на СТРОКУ. Предела у
выборки диалогов нет вовсе — это был единственный такой цикл в проекте без
`.limit(BATCH)`.

У диспетчера с тремя сотнями открытых диалогов удаление превращалось в полторы
тысячи запросов внутри одного запроса ручки. Ни ошибки, ни строчки в журнале:
администратор смотрит на крутящуюся кнопку и жмёт её второй раз.

Сравниваем 2 диалога и 12: если работа растёт вместе с их числом, то где-то
между ними лежит сотрудник, которого уже не удалить.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine

from app.models import Client, Conversation, User
from app.services import users as users_svc

pytestmark = pytest.mark.anyio


async def _сотрудник_с_диалогами(db_sessionmaker: Any, account: Any, сколько: int) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"uhodit-{uuid.uuid4().hex[:8]}@leadchat.test",
        full_name="Уходящий",
        role="manager",
        password_hash="x",
        is_active=True,
        handles_conversations=True,
    )
    async with db_sessionmaker() as db, db.begin():
        db.add(user)
        for i in range(сколько):
            client = Client(
                id=uuid.uuid4(), channel="avito", external_id=uuid.uuid4().hex[:10], name=f"К{i}"
            )
            db.add(client)
            db.add(
                Conversation(
                    id=uuid.uuid4(),
                    channel="avito",
                    external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
                    account_id=account.id,
                    client_id=client.id,
                    status="in_progress",
                    assignee_id=user.id,
                    bot_active=False,
                    bot_vars={},
                    tags=[],
                    unread_count=0,
                    declined_by=[],
                    offered_at=datetime.now(UTC),
                )
            )
    return user


async def _удалить_считая(
    engine: AsyncEngine, db_sessionmaker: Any, redis: Any, actor: User, user_id: uuid.UUID
) -> int:
    запросов = 0

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        nonlocal запросов
        запросов += 1

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        async with db_sessionmaker() as db:
            await users_svc.delete_user(db, redis, actor=actor, user_id=user_id)
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    return запросов


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(avito_user_id=880022)


async def test_работа_не_растёт_вместе_с_числом_диалогов(
    engine: AsyncEngine, db_sessionmaker: Any, redis: Any, users_by_role: dict[str, User], account
) -> None:
    админ = users_by_role["admin"]

    мало = await _сотрудник_с_диалогами(db_sessionmaker, account, 2)
    запросов_мало = await _удалить_считая(engine, db_sessionmaker, redis, админ, мало.id)

    много = await _сотрудник_с_диалогами(db_sessionmaker, account, 12)
    запросов_много = await _удалить_считая(engine, db_sessionmaker, redis, админ, много.id)

    # Десять лишних диалогов вправе стоить нескольких запросов (сама выборка,
    # обновления строк), но НЕ пяти-шести на каждый.
    assert запросов_много <= запросов_мало + 10, (
        f"работа растёт вместе с числом диалогов: {запросов_мало} запросов на 2 диалога "
        f"и {запросов_много} на 12 — поштучный сбор кадров вернулся"
    )


async def test_диалоги_всё_же_вернулись_в_очередь(
    db_sessionmaker: Any, redis: Any, users_by_role: dict[str, User], account
) -> None:
    """Быстро — не значит «ничего не сделали»."""
    админ = users_by_role["admin"]
    уходит = await _сотрудник_с_диалогами(db_sessionmaker, account, 3)

    async with db_sessionmaker() as db:
        await users_svc.delete_user(db, redis, actor=админ, user_id=уходит.id)

    async with db_sessionmaker() as db:
        строки = (
            (await db.execute(sa.select(Conversation).where(Conversation.account_id == account.id)))
            .scalars()
            .all()
        )
    assert len(строки) == 3
    for conv in строки:
        assert conv.assignee_id is None, "диалог остался за удалённым сотрудником"
        assert conv.offered_at is not None, "диалог не встал в очередь — клиент невидим"
