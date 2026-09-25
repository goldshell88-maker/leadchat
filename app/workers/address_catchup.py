"""Задача «догнать автозапись» после её включения (проверка 24.09).

Ставит её ручка настроек при переходе автозаписи «выключено → включено»,
после commit'а, под постоянным именем (`enqueue_autofill_catchup`). Выбор
диалогов — `services/address_catchup.autofill_backlog`, тот же, что у
`address-autofill-backlog`; решает по каждому диалогу воркер `autofill_address`
со всеми своими сторожами — задача только ставит.
"""

from __future__ import annotations

from typing import Any

import structlog
from redis.asyncio import Redis

from app.core.observability import with_job_scope
from app.services import address_catchup
from app.services.geocode_queue import enqueue_autofill

log = structlog.get_logger()


@with_job_scope
async def address_autofill_catchup(ctx: dict[str, Any]) -> int:
    """Поставить автозапись диалогам, которым есть что взять. Итог — сколько поставлено."""
    redis: Redis = ctx["redis"]
    async with ctx["db_session_factory"]() as db:
        backlog = await address_catchup.autofill_backlog(db)
    conversations = backlog.conversations
    for conversation_id in conversations:
        await enqueue_autofill(redis, conversation_id)
    # `enqueue_autofill` молчит при сбое Redis (пишет свою строку): это число
    # попыток постановки, не подтверждений очереди.
    log.info(
        "geocode.autofill_catchup",
        empty_cards=len(backlog.empty_cards),
        better_grade=len(backlog.better_grade),
        enqueued=len(conversations),
    )
    return len(conversations)
