"""Постановка задач проверки адреса по карте — отдельно от самой задачи.

Ставит `inbound` (после commit'а) и починка планировщика; выполняет
`app/workers/geocode.py`. Раздельно, чтобы приём входящих не тянул за собой
httpx-провайдеров и код воркера.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from redis.asyncio import Redis

GEOCODE_JOB = "geocode_candidate"
AUTOFILL_JOB = "autofill_address"
AUTOFILL_CATCHUP_JOB = "address_autofill_catchup"
LLM_ADDRESS_JOB = "llm_address_read"

#: Окно тишины перед автозаписью. Клиент, назвав адрес, в следующей реплике
#: часто уточняет посёлок или поправляет дом; девяносто секунд дают ему это
#: сделать до того, как адрес встанет в карточку.
AUTOFILL_DELAY_SEC = 90

log = structlog.get_logger()


async def enqueue_geocode(
    redis: Redis,
    candidate_id: uuid.UUID,
    *,
    defer_sec: int | None = None,
    suffix: str | None = None,
    origin: str | None = None,
) -> bool:
    """Проверить строку по карте. Дедуп по имени: одна задача на строку.

    ``True`` — задача встала. Починка ставит пачку с задержкой `defer_sec`,
    чтобы не собрать сорок задач у одного замка темпа разом. `suffix` —
    второе имя для повтора, который ставит сама задача: её собственное имя
    занято до конца выполнения. `origin="repair"` — задачу ставит починка:
    воркер даёт ей потолки карт за вычетом запаса живому потоку.
    """
    from arq.connections import ArqRedis

    from app.services.messages import as_arq

    имя = f"geocode:{candidate_id}" + (f":{suffix}" if suffix else "")
    # Именованный аргумент задачи — только когда он есть: задачи живого
    # потока остаются как были, и старый воркер в окне выкатки их поймёт.
    kwargs: dict[str, Any] = {"origin": origin} if origin else {}
    try:
        job = await ArqRedis.enqueue_job(
            as_arq(redis),
            GEOCODE_JOB,
            candidate_id,
            _job_id=имя,
            _defer_by=defer_sec or None,
            **kwargs,
        )
        return job is not None
    except Exception:  # noqa: BLE001 — карта не стоит упавшего вебхука
        log.warning("geocode.enqueue_failed", candidate_id=str(candidate_id))
        return False


async def enqueue_llm_read(
    redis: Redis,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    candidate_id: uuid.UUID | None = None,
) -> bool:
    """Модель перечитывает реплику (13.09). Одна задача на реплику: имя — по сообщению."""
    from arq.connections import ArqRedis

    from app.services.messages import as_arq

    try:
        job = await ArqRedis.enqueue_job(
            as_arq(redis),
            LLM_ADDRESS_JOB,
            conversation_id=str(conversation_id),
            message_id=str(message_id),
            candidate_id=str(candidate_id) if candidate_id else None,
            _job_id=f"llm-addr:{message_id}",
        )
        return job is not None
    except Exception:  # noqa: BLE001 — модель не стоит упавшего вебхука
        log.warning("llm_address.enqueue_failed", message_id=str(message_id))
        return False


async def enqueue_autofill(redis: Redis, conversation_id: uuid.UUID) -> None:
    """Автозапись по диалогу — через окно тишины. Одна задача на диалог."""
    from arq.connections import ArqRedis

    from app.services.messages import as_arq

    try:
        await ArqRedis.enqueue_job(
            as_arq(redis),
            AUTOFILL_JOB,
            conversation_id,
            _job_id=f"addr-fill:{conversation_id}",
            _defer_by=AUTOFILL_DELAY_SEC,
        )
    except Exception:  # noqa: BLE001
        log.warning("geocode.autofill_enqueue_failed", conversation_id=str(conversation_id))


async def enqueue_autofill_catchup(redis: Redis) -> bool:
    """Догнать автозапись по накопленному — автозапись включили (проверка 24.09).

    Имя постоянное: повторное включение, пока прежняя задача стоит в очереди,
    её не удваивает. ``True`` — задача встала.
    """
    from arq.connections import ArqRedis

    from app.services.messages import as_arq

    try:
        job = await ArqRedis.enqueue_job(
            as_arq(redis), AUTOFILL_CATCHUP_JOB, _job_id="address-autofill-catchup"
        )
        return job is not None
    except Exception:  # noqa: BLE001 — догон не стоит упавшего сохранения настройки
        log.warning("geocode.autofill_catchup_enqueue_failed")
        return False
