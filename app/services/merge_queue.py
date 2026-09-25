"""Постановка задачи «объединить двойников по телефону» — отдельно от задачи.

Ставит `inbound` (после commit'а, когда у карточки появился основной номер) и
ночной проход планировщика; выполняет `app/workers/client_merge.py`.
Раздельно, чтобы приём входящих не тянул за собой код воркера.
"""

from __future__ import annotations

import hashlib

import structlog
from redis.asyncio import Redis

MERGE_JOB = "merge_twins"
#: Сколько живёт номер, спрятанный под ключом задачи: дольше любой задержки
#: постановки (ночной проход ставит до 20 задач с шагом 3 с) с запасом.
ARG_TTL_SEC = 3600

#: Пауза перед проверкой. Человек пишет в два наших объявления почти
#: одновременно (218 пар из 300 — в пределах суток, многие — в минуты):
#: несколько секунд дают второму вебхуку закоммитить свою карточку, иначе
#: задача увидела бы одну карточку и вышла «single», а вторую постановку
#: отбросил бы дедуп по имени.
DEFER_SEC = 5
#: Память ночного прохода «пару смотрели» — чтобы порция не забивалась одними
#: и теми же отпавшими парами каждую ночь.
SEEN_DAYS = 7

log = structlog.get_logger()


def phone_key(phone: str) -> str:
    """Ключ номера без номера: имена задач и ключи видны в Redis всем, кто туда смотрит."""
    return hashlib.sha256(phone.encode()).hexdigest()[:24]


def job_id(phone: str) -> str:
    return "merge:" + phone_key(phone)


def arg_key(key: str) -> str:
    return "merge:phone:" + key


async def enqueue_merge(
    redis: Redis, phone: str, *, defer_sec: int | None = None, backfill: bool = False
) -> bool:
    """Проверить двойников по номеру. Дедуп по имени: одна задача на номер.

    ⚠ САМ НОМЕР АРГУМЕНТОМ ЗАДАЧИ НЕ ЕДЕТ: ARQ печатает аргументы каждой задачи
    в журнал воркера при старте, а телефон клиента в журнале — то, чего этот
    проект избегает везде. Номер кладётся под ключ задачи на час, задача
    получает только ключ и читает номер сама (`workers/client_merge.py`).
    """
    from arq.connections import ArqRedis

    from app.services.messages import as_arq

    key = phone_key(phone)
    try:
        await redis.set(arg_key(key), phone, ex=ARG_TTL_SEC)
        job = await ArqRedis.enqueue_job(
            as_arq(redis),
            MERGE_JOB,
            key,
            backfill=backfill,
            _job_id=job_id(phone),
            _defer_by=defer_sec if defer_sec is not None else DEFER_SEC,
        )
        if job is None:
            log.info("merge.enqueue_skipped", reason="job_exists")
        return job is not None
    except Exception:  # noqa: BLE001 — объединение не стоит упавшего вебхука
        log.warning("merge.enqueue_failed")
        return False


async def phone_for(redis: Redis, key: str) -> str | None:
    """Номер по ключу задачи — или ``None``, если час прошёл (задача устарела)."""
    value = await redis.get(arg_key(key))
    if value is None:
        return None
    return value.decode() if isinstance(value, bytes) else str(value)


def seen_key(phone: str) -> str:
    """Ключ «пару смотрели» — без номера в имени ключа."""
    return "merge:seen:" + phone_key(phone)


async def mark_seen(redis: Redis, phone: str) -> None:
    await redis.set(seen_key(phone), "1", ex=SEEN_DAYS * 86400)
