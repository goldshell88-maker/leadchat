"""Задача воркера: объединить карточки-двойники по телефону.

Ставит `inbound` после commit'а, когда у карточки появился основной номер
(`merge_queue.enqueue_merge`), и ночной проход `scheduler/jobs/merge_backlog.py`.
Проверки и действие — `services/client_merge.py`; здесь только транзакция,
режим из настроек и кадры после commit'а.

Аргумент задачи — КЛЮЧ номера, а не номер: ARQ печатает аргументы в журнал
воркера (см. `merge_queue.enqueue_merge`).

`max_tries=1`: повтор после падения между commit'ом и кадрами объединил бы
ещё раз? Нет — условие «ровно две живые» уже ложно, — но и кадров бы не
вернул. Редкий сбой стоит F5, а не второй задачи.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from redis.asyncio import Redis
from sqlalchemy.exc import DBAPIError

from app.core.errors import ApiError
from app.core.observability import with_job_scope
from app.services import app_settings, client_merge, clients_events, merge_queue, phone_rules

log = structlog.get_logger("app.workers.client_merge")

MAX_TRIES = 1


@with_job_scope
async def merge_twins(ctx: dict[str, Any], key: str, *, backfill: bool = False) -> str:
    factory = ctx["db_session_factory"]
    redis: Redis = ctx["redis"]
    phone = await merge_queue.phone_for(redis, key)
    if phone is None:
        log.info("client_merge.skipped", reason="stale_job")
        return "skipped:stale_job"
    async with factory() as db:
        values = await app_settings.get_all(db)
        mode = str(values.get(app_settings.CLIENT_MERGE_AUTO) or "off")
        own = phone_rules.parse_own_numbers(values.get(app_settings.PHONE_OWN_NUMBERS))
        try:
            outcome = await client_merge.auto_merge(
                db, phone, mode=mode, own=own, backfill=backfill
            )
        except ApiError as exc:
            # Сторожа `merge_clients` сработали после замка: пару уже объединили
            # или разъединили руками. Не сбой — исход.
            await db.rollback()
            log.info("client_merge.skipped", reason=exc.code)
            return f"skipped:{exc.code}"
        except DBAPIError as exc:
            # Взаимная блокировка с входящим (Postgres снимает одну из двух
            # транзакций) — ночной проход вернётся к паре, входящий важнее.
            await db.rollback()
            if "deadlock" not in str(exc.orig).lower():
                raise
            log.warning("client_merge.deadlock")
            return "skipped:deadlock"
        # Память для ночного прохода — ТОЛЬКО у его задач: живой путь ставит
        # задачу в момент, когда вторая карточка могла ещё не появиться или имя
        # ещё не дотянуто (`single`, `names_differ`), и пометить пару «смотрели»
        # на неделю значило бы отобрать у ночного прохода ровно эти пары.
        if backfill:
            await merge_queue.mark_seen(redis, phone)
        if outcome.action == "skipped":
            await db.rollback()
            return f"skipped:{outcome.reason}"
        winner: dict[str, Any] | None = None
        if outcome.action == "merged" and outcome.winner_id is not None:
            from app.models import Client
            from app.services.clients import short_view

            card = await db.get(Client, outcome.winner_id)
            winner = short_view(card) if card is not None else None
        await db.commit()
    if outcome.action == "merged" and winner is not None and outcome.loser_id is not None:
        await clients_events.publish_merge(
            redis,
            winner=winner,
            loser_id=outcome.loser_id,
            moved=outcome.moved,
            reason="merged_auto",
        )
        async with factory() as db:
            await clients_events.publish_client_patch(
                db,
                redis,
                uuid.UUID(winner["id"]),
                {"id": winner["id"], "name": winner.get("name"), "phone": winner.get("phone")},
                skip=frozenset(outcome.moved),
            )
        log.info(
            "client_merge.merged",
            winner_id=str(outcome.winner_id),
            loser_id=str(outcome.loser_id),
            moved=len(outcome.moved),
            backfill=backfill,
        )
        return "merged"
    log.info("client_merge.shadow", winner_id=str(outcome.winner_id), backfill=backfill)
    return "shadow"
