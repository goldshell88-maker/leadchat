"""Догон карточки по прожитой переписке диалога — постановка задачи (N29, 19.09).

ЗАЧЕМ. Историческая дверь (`avito_accounts._backfill_chat` → `_insert_history_message`)
кладёт реплики клиента в ленту без разбора адреса и телефона: живой путь
(`inbound.apply_inbound_event`) через неё не идёт. Замер 27 773 диалогов за 30 дней:
47 диалогов/мес с настоящим адресом и без единой строки — по гипотезе разбора их
реплики пришли догрузкой истории (`backfill_conversation` после первого дошедшего
вебхука, кнопка «Загрузить историю», подключение канала).

Здесь только имя задачи, окно и постановка: приём и загрузка истории не должны
тянуть за собой разбор и код воркера (как `geocode_queue`). Сам догон —
`app/workers/cards_catchup.py`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta

import structlog
from redis.asyncio import Redis

CATCHUP_JOB = "catchup_conversation_cards"
#: Реплики старше не догоняем: адрес и номер годичной давности карточке не
#: нужны, а импорт канала за годы — это тысячи строк в очередь карты.
ОКНО_ДОГОНА = timedelta(days=30)

log = structlog.get_logger()


def job_id(conversation_id: uuid.UUID | str) -> str:
    """Одна задача на диалог: повтор постановки, пока первая в полёте, бесплатен.
    Имя читает и замок вопроса клиенту (`workers/address_ask._история_едет`)."""
    return f"cardcatch:{conversation_id}"


def earliest_in_window(moments: Iterable[datetime], *, now: datetime) -> datetime | None:
    """Самая ранняя отметка не старше окна — нижняя граница догона; пусто → None."""
    порог = now - ОКНО_ДОГОНА
    в_окне = [m for m in moments if m >= порог]
    return min(в_окне) if в_окне else None


async def enqueue_cards_catchup(
    redis: Redis, conversation_id: uuid.UUID, *, since: datetime
) -> bool:
    """Поставить догон диалога от `since` (время самой ранней вставленной реплики клиента).

    ``True`` — задача встала; ``False`` — уже в полёте или Redis отказал (в журнал
    warning). Не бросает: догон карточки не стоит сорванной загрузки чата.
    """
    from arq.connections import ArqRedis

    from app.services.messages import as_arq

    try:
        job = await ArqRedis.enqueue_job(
            as_arq(redis),
            CATCHUP_JOB,
            conversation_id,
            since.isoformat(),
            _job_id=job_id(conversation_id),
        )
    except Exception as exc:  # noqa: BLE001 — см. докстринг
        # Имя класса — «что пришло вместо ожидаемого»: таймаут, разрыв и ошибка
        # сериализации аргумента по журналу иначе неразличимы (ревью 19.09).
        log.warning(
            "cards_catchup.enqueue_failed",
            conversation_id=str(conversation_id),
            error=type(exc).__name__,
        )
        return False
    if job is None:
        log.info("cards_catchup.already_queued", conversation_id=str(conversation_id))
        return False
    log.info(
        "cards_catchup.enqueued", conversation_id=str(conversation_id), since=since.isoformat()
    )
    return True
