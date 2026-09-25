"""Job'ы статистики (06 §3.2, §5.4).

Два расписания:

* ``stats_mv_refresh`` — ежечасно в HH:05 пересчитывает
  ``mv_conversation_stats`` и пишет метку ``stats:refreshed_at`` в Redis
  (её отдаёт каждый ответ ``/stats/*``: UI показывает «Данные обновлены
  в 14:05»);
* ``stats_export_cleanup`` — ежесуточно удаляет файлы выгрузок старше 7 суток
  из ``MEDIA_ROOT/exports`` (06 §5.4).

Отступление от кода-каркаса 06 §3.2, обязательное к соблюдению:
``REFRESH MATERIALIZED VIEW CONCURRENTLY`` **нельзя выполнить внутри
транзакции** (PostgreSQL: ``PreventInTransactionBlock``), а
``engine.connect()`` в SQLAlchemy открывает транзакцию на первом же
``execute``. Поэтому соединение переводится в ``AUTOCOMMIT`` — иначе job
падал бы на каждом прогоне. CONCURRENTLY нужен ради того, чтобы дашборд не
блокировался на время пересчёта (и требует уникального индекса
``mv_conversation_stats_pk`` — он создан миграцией 0004).

Порог деградации (06 §3.2): refresh дольше 2–3 минут по логам — сигнал
заменять MV инкрементальной таблицей. Поэтому длительность логируется всегда.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import text

from app.core import redis as redis_mod
from app.db import session as db_mod
from app.services.stats import (
    REFRESH_MV_SQL,
    REFRESH_STATEMENT_TIMEOUT,
    STATS_REFRESHED_KEY,
    cleanup_export_files,
)

log = structlog.get_logger("app.scheduler.stats")

REFRESH_JOB_ID = "stats_mv_refresh"
CLEANUP_JOB_ID = "stats_export_cleanup"

DEFAULTS: dict[str, Any] = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 600}


async def refresh_stats_mv() -> None:
    """``REFRESH MATERIALIZED VIEW CONCURRENTLY mv_conversation_stats`` + метка свежести."""
    engine = db_mod.init_engine()
    started = time.monotonic()
    async with engine.connect() as conn:
        # AUTOCOMMIT: CONCURRENTLY не выполняется внутри транзакционного блока.
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"SET statement_timeout = '{REFRESH_STATEMENT_TIMEOUT}'"))
        await conn.execute(text(REFRESH_MV_SQL))
    elapsed = round(time.monotonic() - started, 3)

    refreshed_at = datetime.now(UTC).isoformat()
    try:
        await redis_mod.get_client().set(STATS_REFRESHED_KEY, refreshed_at)
    except Exception:  # noqa: BLE001 — MV уже пересчитана; метка не критична
        log.warning("stats.refreshed_at_write_failed")
    log.info("stats.mv_refreshed", elapsed_sec=elapsed, refreshed_at=refreshed_at)


async def cleanup_exports() -> None:
    """Файлы выгрузок старше 7 суток (06 §5.4)."""
    removed = cleanup_export_files()
    log.info("stats.export_cleanup", removed=removed)


async def cleanup_media_orphans() -> None:
    """Вложения, загруженные и ни к чему не прикреплённые (#22).

    Человек прикладывает файл: тот улетает на диск сразу, а прикрепляется
    только при отправке. Передумал, закрыл вкладку, перезагрузил страницу —
    файл остался на диске навсегда: ключ в Redis истечёт, а байты нет.

    Уборка была ОБЕЩАНА в трёх местах — в шапке модуля вложений, в строке
    расписания и в руководстве по эксплуатации — и не существовала ни в одном.
    """
    from app.db.session import session_scope
    from app.services.media import collect_orphans

    async with session_scope() as db:
        removed = await collect_orphans(db)
    log.info("media.orphan_cleanup", removed=removed)


def register(scheduler: Any) -> None:
    """Регистрация в APScheduler — вызывается из ``app/scheduler/main.py``."""
    from apscheduler.triggers.cron import CronTrigger

    scheduler.add_job(
        refresh_stats_mv,
        CronTrigger(minute=5, timezone="UTC"),  # каждый час в HH:05
        id=REFRESH_JOB_ID,
        **DEFAULTS,
    )
    scheduler.add_job(
        cleanup_exports,
        CronTrigger(hour=3, minute=30, timezone="UTC"),
        id=CLEANUP_JOB_ID,
        **DEFAULTS,
    )
    # Разнесено с выгрузками на полчаса намеренно: обе задачи ходят по одному
    # каталогу, и запускать их одновременно значит мешать друг другу без
    # всякой нужды — работы у обеих на секунды.
    scheduler.add_job(
        cleanup_media_orphans,
        CronTrigger(hour=4, minute=0, timezone="UTC"),
        id="media_orphan_cleanup",
        **DEFAULTS,
    )
