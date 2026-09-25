"""Срок хранения журналов, которые росли вечно (владелец 12.09: «умная очистка»).

`audit_log` — 78 000 строк в месяц, `message_idempotency` — строка на каждое
исходящее; ни у той, ни у другой не было срока. Ночное задание убирает
записи старше AUDIT_LOG_TTL_DAYS (год) и MESSAGE_IDEMPOTENCY_TTL_DAYS
(месяц). Переписку не трогает никто — сторож на это тоже здесь.
"""

from __future__ import annotations

import contextlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.models import AuditLog, MessageIdempotency
from app.scheduler import main as scheduler_main

pytestmark = pytest.mark.anyio


def test_задание_зарегистрировано_в_планировщике() -> None:
    ids = {job.id for job in scheduler_main.build_scheduler().get_jobs()}
    assert "retention_cleanup" in ids


async def test_старые_строки_уходят_свежие_остаются(db_sessionmaker, monkeypatch) -> None:
    now = datetime.now(UTC)
    старый_аудит = AuditLog(action="x", created_at=now - timedelta(days=400))
    свежий_аудит = AuditLog(action="y", created_at=now - timedelta(days=300))
    conv = uuid.uuid4()
    старый_ключ = MessageIdempotency(
        conversation_id=conv,
        client_message_id="old",
        message_id=uuid.uuid4(),
        created_at=now - timedelta(days=40),
    )
    свежий_ключ = MessageIdempotency(
        conversation_id=conv,
        client_message_id="fresh",
        message_id=uuid.uuid4(),
        created_at=now - timedelta(days=2),
    )
    async with db_sessionmaker() as s:
        s.add_all([старый_аудит, свежий_аудит, старый_ключ, свежий_ключ])
        await s.commit()

    @contextlib.asynccontextmanager
    async def транзакция():  # noqa: ANN202
        async with db_sessionmaker() as s:
            async with s.begin():
                yield s

    monkeypatch.setattr(scheduler_main.db_mod, "transaction", транзакция)
    await scheduler_main.cleanup_retention()

    async with db_sessionmaker() as s:
        аудит = set((await s.execute(sa.select(AuditLog.action))).scalars())
        ключи = set((await s.execute(sa.select(MessageIdempotency.client_message_id))).scalars())
    assert аудит == {"y"}
    assert ключи == {"fresh"}


def test_переписка_не_убирается_никогда() -> None:
    """Задание знает ровно две таблицы; `messages` в нём быть не должно."""
    import inspect
    import re

    src = inspect.getsource(scheduler_main.cleanup_retention)
    цели = re.findall(r'\("(\w+)", (\w+), settings\.', src)
    assert цели == [("audit_log", "AuditLog"), ("message_idempotency", "MessageIdempotency")]
