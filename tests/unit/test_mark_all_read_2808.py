"""«Отметить все прочитанными» — один рейс до базы, а не два на строку.

Ручка `POST /notifications/read-all` (14 §3) звала `mark_read` в цикле, а тот на
КАЖДОЙ строке спрашивал базу «а не прочитано ли уже?» и делал свой `flush`.
Вопрос был лишний: выборка выше отсекает прочитанное этим человеком, и в одной
транзакции ответ на него всегда «нет». Колокольчик руководителя, вернувшегося из
отпуска, копит сотни строк — одно нажатие превращалось в сотни рейсов.

Тест сравнивает 3 строки и 30: если работа растёт вместе со списком, то где-то
между ними лежит колокольчик, который уже не разгрести.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from app.models import User
from app.models.notification import Notification, NotificationRead
from app.services import notifications as svc

pytestmark = pytest.mark.anyio


async def _наполнить(db: AsyncSession, сколько: int) -> None:
    for _ in range(сколько):
        await svc.notify(
            db,
            kind="conversation.negative",
            entity_type="conversation",
            entity_id=str(uuid.uuid4()),  # разные сущности: склейки не будет
        )
    await db.commit()


async def _пометить_считая(engine: AsyncEngine, db: AsyncSession, user: User) -> tuple[int, int]:
    """Отметить всё прочитанным. Отдаёт (сколько отмечено, сколько запросов)."""
    запросов = 0

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        nonlocal запросов
        запросов += 1

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        отмечено = await svc.mark_all_read(db, user)
        await db.commit()
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    return отмечено, запросов


async def test_работа_не_растёт_вместе_со_списком(
    engine: AsyncEngine,
    db_sessionmaker: Any,
    users_by_role: dict[str, User],
) -> None:
    head = users_by_role["head"]

    async with db_sessionmaker() as s:
        await _наполнить(s, 3)
    async with db_sessionmaker() as s:
        мало, запросов_мало = await _пометить_считая(engine, s, head)

    async with db_sessionmaker() as s:
        await _наполнить(s, 30)
    async with db_sessionmaker() as s:
        много, запросов_много = await _пометить_считая(engine, s, head)

    assert (мало, много) == (3, 30), "отмечено должно быть ровно столько, сколько висело"
    assert запросов_много <= запросов_мало + 1, (
        f"работа растёт вместе со списком: {запросов_мало} запросов на 3 строки "
        f"и {запросов_много} на 30"
    )


async def test_отметка_действительно_записана(
    db_sessionmaker: Any, users_by_role: dict[str, User]
) -> None:
    """Быстро — не значит «ничего не сделали»: проверяем сами отметки."""
    head = users_by_role["head"]
    async with db_sessionmaker() as s:
        await _наполнить(s, 4)

    async with db_sessionmaker() as s:
        assert await svc.mark_all_read(s, head) == 4
        await s.commit()

    async with db_sessionmaker() as s:
        отметок = len((await s.execute(select(NotificationRead.notification_id))).scalars().all())
        всего = len((await s.execute(select(Notification.id))).scalars().all())
        assert отметок == всего == 4
        # Повторное нажатие не находит непрочитанного и ничего не пишет.
        assert await svc.mark_all_read(s, head) == 0
