"""Догон прогонов по расписанию, пропущенных на перезапуске (проверка 24.09).

ЗАЧЕМ. Расписание планировщика живёт в памяти процесса. Выкатка, OOM или
ручной перезапуск ровно во время ночной или недельной задачи — и новый
процесс строит расписание от «сейчас»: `misfire_grace_time` догоняет только
задержку внутри ЖИВОГО процесса, а пропущенный прогон пропадает. Бой 20.09:
планировщик перезапущен выкаткой в 23:09:57 UTC, лестница политик стояла на
23:10:00 — разминулись на три секунды, неделя потеряна, и сравнивать
следующую было не с чем.

КАК. Задача догоняется, если её плановое время лежит в пределах её
собственного `misfire_grace_time` и при этом:

* прежний процесс в это время уже не жил — его последний пульс
  (`scheduler:alive`, пишется раз в 30 с) старше планового времени. Живой
  процесс задачу запустил сам: без этой сверки первая выкатка догона
  повторила бы всё, что старый код успел сделать за окно допуска, — а
  лестница политик неидемпотентна и понизила бы правило второй раз;
* нет метки успешного прогона на это время (`scheduler:ran:{job_id}`) — её
  оставляет каждый прогон задачи по `CronTrigger`.

Окно допуска у каждой задачи своё и уже записано владельцем задачи: недельные
замеры ждут до шести часов, ночные уборки — пять минут.

ПО ОЧЕРЕДИ, А НЕ РАЗОМ. Понедельничная цепочка «суд → воронка → лестница»
читает то, что записал предыдущий шаг. Догоняемые задачи идут одна за другой
в порядке планового времени; разом (как `job.modify(next_run_time=now)` у
каждой) лестница решала бы, пока суд ещё пишет вердикты ночи.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from apscheduler.events import EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.triggers.cron import CronTrigger

from app.core import redis as redis_mod

log = structlog.get_logger("app.scheduler.catch_up")

RAN_KEY = "scheduler:ran:{job_id}"
#: Дольше самого длинного периода (неделя): метка обязана пережить его целиком.
RAN_TTL = timedelta(days=8)
#: Пульс планировщика (`scheduler.main.heartbeat`).
ALIVE_KEY = "scheduler:alive"

#: Задачи в пути: без ссылки задачу asyncio может собрать сборщик мусора.
_pending: set[asyncio.Task[Any]] = set()


def _keep(task: asyncio.Task[Any]) -> None:
    _pending.add(task)
    task.add_done_callback(_pending.discard)


def remember_runs(scheduler: Any) -> None:
    """Подписать планировщик: успешный прогон задачи по расписанию — метка в Redis."""

    def on_executed(event: JobExecutionEvent) -> None:
        job = scheduler.get_job(event.job_id)
        if job is None or not isinstance(job.trigger, CronTrigger):
            return
        _keep(asyncio.get_running_loop().create_task(_mark(event.job_id, event.scheduled_run_time)))

    scheduler.add_listener(on_executed, EVENT_JOB_EXECUTED)


async def _mark(job_id: str, scheduled: datetime) -> None:
    try:
        await redis_mod.get_client().set(
            RAN_KEY.format(job_id=job_id),
            scheduled.isoformat(),
            ex=int(RAN_TTL.total_seconds()),
        )
    except Exception:  # noqa: BLE001 — без метки догон лишь повторит прогон; задачу не роняем
        log.exception("scheduler.ran_mark_failed", job_id=job_id)


async def previous_heartbeat(redis: Any) -> datetime | None:
    """Последний пульс прежнего процесса. Читать ДО первого пульса нового."""
    raw = await redis.get(ALIVE_KEY)
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


async def catch_up_missed(
    scheduler: Any,
    redis: Any,
    *,
    now: datetime | None = None,
    alive_until: datetime | None = None,
) -> list[str]:
    """Догнать задачи, чей плановый прогон пришёлся на перезапуск.

    Возвращает id догоняемых задач; сами прогоны идут фоновой задачей по
    очереди. `alive_until` — последний пульс прежнего процесса
    (:func:`previous_heartbeat`); `None` — неизвестно, судим по меткам.
    """
    moment = now or datetime.now(UTC)
    due_jobs: list[tuple[datetime, Any]] = []
    for job in scheduler.get_jobs():
        grace = job.misfire_grace_time
        if not isinstance(job.trigger, CronTrigger) or not grace:
            continue
        due = _last_fire_time(job.trigger, moment - timedelta(seconds=grace), moment)
        if due is None:
            continue
        if alive_until is not None and due <= alive_until:
            continue  # прежний процесс был жив и запустил задачу сам
        ran = await redis.get(RAN_KEY.format(job_id=job.id))
        if ran and datetime.fromisoformat(ran) >= due:
            continue
        due_jobs.append((due, job))
    due_jobs.sort(key=lambda pair: pair[0])
    if due_jobs:
        _keep(asyncio.get_running_loop().create_task(_run_in_order(due_jobs)))
    return [job.id for _, job in due_jobs]


def _last_fire_time(trigger: CronTrigger, since: datetime, until: datetime) -> datetime | None:
    """Последнее плановое время в окне `[since, until]`; `None` — прогона не было."""
    last = None
    moment = trigger.get_next_fire_time(None, since)
    while moment is not None and moment <= until:
        last = moment
        moment = trigger.get_next_fire_time(moment, moment + timedelta(seconds=1))
    return last


async def _run_in_order(due_jobs: list[tuple[datetime, Any]]) -> None:
    for due, job in due_jobs:
        log.warning("scheduler.catch_up", job_id=job.id, scheduled_for=due.isoformat())
        try:
            result = job.func(*job.args, **job.kwargs)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — отказ одной задачи не отменяет догон следующих
            log.exception("scheduler.catch_up_failed", job_id=job.id)
            continue
        await _mark(job.id, due)
