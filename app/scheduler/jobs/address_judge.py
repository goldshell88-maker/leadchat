"""Ночной судья карточек адреса — раз в сутки, без человека (пакет 6.0б, I-4).

ЗАЧЕМ. Лестница политик (`rule_policy_weekly`, понедельник 02:10 UTC) двигает
правила по доле ложных среди осуждённых решений; осуждать их должен кто-то
каждую ночь, иначе `judged` пуст и правило сидит в тени бессрочно. Задача
берёт очередь `address_judge.pick_rows` — решения правил за окно воронки без
свежего вердикта, по кругу правил, до `ADDRESS_LLM_JUDGE_DAILY_ROWS` строк —
и судит каждую `address_judge.judge_row`: транзакция на строку, между походами
пауза (бесплатные модели отдают 20 запросов в минуту).

ХОДИТ САМА, ИЗ ПРОЦЕССА ПЛАНИРОВЩИКА (вариант (а) карты 6.0б): окружение
шлюза у планировщика то же, что у воркеров (`docker-compose.prod.yml`,
`app-common`), а очередь ARQ с `job_timeout = 300` под 60 × 200 с не годится.
Бюджет времени — `BUDGET`: не уложились — остаток завтра, очередь та же.
Квоту держит `judge_row` (доля судьи + запас читателю); первая строка со
`skipped_quota` останавливает прогон — сегодня квоты больше не будет.

00:40 UTC — до воронки (пн 01:40) и лестницы (пн 02:10): понедельничный замер
читает уже осуждённые за ночь строки. Уведомлений не шлёт: итог — строка
журнала `address_judge.run`, её видно в Sentry/логах, а число осуждённых —
в `address-funnel by_rule`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from apscheduler.triggers.cron import CronTrigger

from app.core import redis as redis_mod
from app.db import session as db_mod
from app.integrations import gateway
from app.services import address_judge, app_settings

log = structlog.get_logger("app.address_judge")

JOB_ID = "address_judge_daily"
DEFAULTS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 6 * 3600}
#: Бюджет времени прогона: 80 строк × до 200 с на худшей модели не влезают ни
#: в какие 45 минут — и не должны: остаток судится завтра.
BUDGET = timedelta(minutes=45)
#: Пауза между походами — как у консольной команды (`--pause 1.5`).
PAUSE_SEC = 1.5


@dataclass(frozen=True, slots=True)
class JudgeRun:
    picked: int = 0
    judged: int = 0
    skipped_quota: int = 0
    skipped_no_speech: int = 0
    failed: int = 0
    gone: int = 0
    stopped_by_budget: bool = False
    elapsed_s: float = 0.0


async def judge_daily(now: datetime | None = None) -> JudgeRun:
    """Осудить очередь за ночь. `now` — для стенда; в бою — часы."""
    now = now or datetime.now(UTC)
    redis = redis_mod.get_client()
    await gateway.refresh_status()  # ключи читателей знает только шлюз (16.09)
    старт = time.monotonic()
    async with db_mod.session_scope() as db:
        сколько = address_judge.judge_daily_rows(
            await app_settings.get(db, app_settings.ADDRESS_LLM_JUDGE_DAILY_ROWS)
        )
        очередь = await address_judge.pick_rows(db, now=now, limit=сколько)
    счёт: dict[str, int] = {}
    по_бюджету = False
    for i, pick in enumerate(очередь):
        if time.monotonic() - старт > BUDGET.total_seconds():
            по_бюджету = True
            break
        if i:
            await asyncio.sleep(PAUSE_SEC)
        # Транзакция — на строку: сессия рождается под строку и умирает после
        # commit'а, поход к модели идёт вне транзакции (`judge_row`).
        async with db_mod.session_scope() as db:
            итог = await address_judge.judge_row(
                db, redis, pick.row_id, target=pick.target, now=now
            )
            await db.commit()
        счёт[итог.outcome] = счёт.get(итог.outcome, 0) + 1
        if итог.outcome == address_judge.OUTCOME_SKIPPED_QUOTA:
            break  # квоты на сегодня больше нет — остальное завтра
    прогон = JudgeRun(
        picked=len(очередь),
        judged=счёт.get(address_judge.OUTCOME_JUDGED, 0),
        skipped_quota=счёт.get(address_judge.OUTCOME_SKIPPED_QUOTA, 0),
        skipped_no_speech=счёт.get(address_judge.OUTCOME_SKIPPED_NO_SPEECH, 0),
        failed=счёт.get(address_judge.OUTCOME_FAILED, 0),
        gone=счёт.get(address_judge.OUTCOME_GONE, 0),
        stopped_by_budget=по_бюджету,
        elapsed_s=round(time.monotonic() - старт, 1),
    )
    log.info(
        "address_judge.run",
        picked=прогон.picked,
        judged=прогон.judged,
        skipped_quota=прогон.skipped_quota,
        skipped_no_speech=прогон.skipped_no_speech,
        failed=прогон.failed,
        gone=прогон.gone,
        stopped_by_budget=прогон.stopped_by_budget,
        elapsed_s=прогон.elapsed_s,
    )
    return прогон


def register(scheduler: Any) -> None:
    """Ежедневно 00:40 UTC = 03:40 МСК — ночью, до понедельничной воронки."""
    scheduler.add_job(
        judge_daily, CronTrigger(hour=0, minute=40, timezone="UTC"), id=JOB_ID, **DEFAULTS
    )
