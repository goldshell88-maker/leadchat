"""Разбор завала непрочитанных тревог.

НАЙДЕНО НА БОЕВОЙ СИСТЕМЕ: 112 непрочитанных `inbound.stalled` за шесть дней,
все ложные — порог молчания стоял 30 минут вместо четырёх часов. Порог починен
11 августа, новых не появляется. Но старые закрыли собой два НАСТОЯЩИХ отказа
резервной копии от 8 августа: в колокольчике сто с лишним одинаковых строк, и
два важных сообщения нашлись только запросом в базу.

Пока завал не разобран, колокольчик бесполезен: его перестают открывать, и
следующая настоящая тревога опоздает ровно настолько, насколько человек привык
туда не смотреть.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.models import Notification, User


def _тревога(kind: str, когда: datetime, кому: uuid.UUID) -> Notification:
    return Notification(
        id=uuid.uuid4(),
        recipient_id=кому,
        kind=kind,
        severity="warning",
        title="Обращения не приходят",
        created_at=когда,
        last_seen_at=когда,
        # Срок хранения обязателен схемой: уведомления сами убираются из
        # колокольчика по истечении, и строка без срока туда просто не ляжет.
        expires_at=когда + timedelta(days=30),
    )


@pytest.fixture
async def завал(db_sessionmaker: Any) -> dict[str, Any]:
    сейчас = datetime.now(UTC)
    async with db_sessionmaker() as db:
        кому = User(
            id=uuid.uuid4(),
            email="head@leadpartner.ru",
            full_name="Руководитель",
            role="head",
            password_hash="x",
            is_active=True,
        )
        db.add(кому)
        await db.flush()
        for i in range(5):
            db.add(_тревога("inbound.stalled", сейчас - timedelta(days=3, minutes=i), кому.id))
        # Свежая — её гасить нельзя.
        db.add(_тревога("inbound.stalled", сейчас - timedelta(minutes=5), кому.id))
        # Чужого вида — тем более.
        db.add(_тревога("backup.failed", сейчас - timedelta(days=3), кому.id))
        await db.commit()
        return {"кому": кому.id, "сейчас": сейчас}


async def _непрочитанных(db_sessionmaker: Any, kind: str) -> int:
    async with db_sessionmaker() as db:
        return int(
            (
                await db.execute(
                    sa.select(sa.func.count())
                    .select_from(Notification)
                    .where(Notification.kind == kind, Notification.read_at.is_(None))
                )
            ).scalar_one()
        )


async def test_гасит_только_старые_своего_вида(db_sessionmaker: Any, завал: dict[str, Any]) -> None:
    граница = завал["сейчас"] - timedelta(days=1)

    async with db_sessionmaker() as db:
        await db.execute(
            sa.update(Notification)
            .where(
                Notification.kind == "inbound.stalled",
                Notification.read_at.is_(None),
                Notification.created_at < граница,
            )
            .values(read_at=datetime.now(UTC))
        )
        await db.commit()

    # Пять старых погашены, свежая осталась.
    assert await _непрочитанных(db_sessionmaker, "inbound.stalled") == 1
    # Настоящий отказ копии не тронут — ради него всё и затевалось.
    assert await _непрочитанных(db_sessionmaker, "backup.failed") == 1


async def test_тревоги_не_удаляются_а_помечаются(
    db_sessionmaker: Any, завал: dict[str, Any]
) -> None:
    """Из колокольчика уходит счётчик, из журнала — ничего.

    Удалять нельзя: тревога — это след события, и по ней потом разбирают, что
    происходило. Гасится только непрочитанность.
    """
    граница = завал["сейчас"] - timedelta(days=1)
    async with db_sessionmaker() as db:
        await db.execute(
            sa.update(Notification)
            .where(Notification.kind == "inbound.stalled", Notification.created_at < граница)
            .values(read_at=datetime.now(UTC))
        )
        await db.commit()

    async with db_sessionmaker() as db:
        всего = int(
            (
                await db.execute(
                    sa.select(sa.func.count())
                    .select_from(Notification)
                    .where(Notification.kind == "inbound.stalled")
                )
            ).scalar_one()
        )
    assert всего == 6, "тревоги пропали из журнала — их нельзя удалять, только помечать"
