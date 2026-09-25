"""Прогон по расписанию, пришедшийся на перезапуск, догоняется (проверка 24.09).

Расписание живёт в памяти процесса: новый процесс строил его от «сейчас», и
`misfire_grace_time` ничего не догонял. Бой 20.09: выкатка перезапустила
планировщик в 23:09:57 UTC, недельная лестница политик стояла на 23:10:00 —
неделя потеряна. Теперь прогон в окне допуска догоняется при старте, если
прежний процесс в это время уже не жил и метки прогона нет; догоняемые
задачи идут по очереди, в порядке планового времени.

ДИВЕРСИИ: убрать сверку с меткой — краснеет второй тест; с пульсом прежнего
процесса — третий; окно допуска — четвёртый; очередь (запускать разом) —
пятый; метку прогона — шестой.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from apscheduler.events import EVENT_JOB_EXECUTED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core import redis as redis_mod
from app.scheduler import catch_up
from app.scheduler.main import build_scheduler

pytestmark = pytest.mark.anyio

#: Понедельник, 02:15 UTC — после суда (00:40) и лестницы (02:10).
MONDAY = datetime(2026, 9, 21, 2, 15, tzinfo=UTC)
JUDGE_DUE = datetime(2026, 9, 21, 0, 40, tzinfo=UTC)
LADDER_DUE = datetime(2026, 9, 21, 2, 10, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _marks_to_fake_redis(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(redis_mod, "get_client", lambda *a, **k: redis)


def _scheduler(events: list[str]) -> AsyncIOScheduler:
    async def judge() -> None:
        events.append("judge:start")
        await asyncio.sleep(0.05)  # суд пишет вердикты не мгновенно
        events.append("judge:end")

    async def ladder() -> None:
        events.append("ladder:start")

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        judge,
        CronTrigger(hour=0, minute=40, timezone="UTC"),
        id="judge",
        misfire_grace_time=6 * 3600,
    )
    scheduler.add_job(
        ladder,
        CronTrigger(day_of_week="mon", hour=2, minute=10, timezone="UTC"),
        id="ladder",
        misfire_grace_time=6 * 3600,
    )
    scheduler.start(paused=True)
    return scheduler


async def _settle() -> None:
    await asyncio.gather(*catch_up._pending)


async def test_a_run_missed_on_restart_runs_right_away(redis: Any) -> None:
    events: list[str] = []
    scheduler = _scheduler(events)
    try:
        caught = await catch_up.catch_up_missed(scheduler, redis, now=MONDAY)
        await _settle()
    finally:
        scheduler.shutdown(wait=False)

    assert caught == ["judge", "ladder"]
    assert "ladder:start" in events
    assert await redis.get(catch_up.RAN_KEY.format(job_id="ladder")) == LADDER_DUE.isoformat()


async def test_a_run_done_before_the_restart_is_not_repeated(redis: Any) -> None:
    await redis.set(catch_up.RAN_KEY.format(job_id="judge"), JUDGE_DUE.isoformat())
    await redis.set(catch_up.RAN_KEY.format(job_id="ladder"), LADDER_DUE.isoformat())
    scheduler = _scheduler([])
    try:
        assert await catch_up.catch_up_missed(scheduler, redis, now=MONDAY) == []
    finally:
        scheduler.shutdown(wait=False)


async def test_a_run_the_previous_process_was_alive_for_is_not_repeated(redis: Any) -> None:
    """Первая выкатка догона: старый код меток не писал, но был жив в 02:10 —
    лестница им уже запущена, и второй прогон понизил бы правило ещё раз."""
    scheduler = _scheduler([])
    try:
        caught = await catch_up.catch_up_missed(
            scheduler, redis, now=MONDAY, alive_until=MONDAY - timedelta(seconds=20)
        )
    finally:
        scheduler.shutdown(wait=False)

    assert caught == []


async def test_a_run_beyond_the_grace_window_waits_for_its_next_time(redis: Any) -> None:
    scheduler = _scheduler([])
    try:
        late = LADDER_DUE + timedelta(hours=7)
        assert "ladder" not in await catch_up.catch_up_missed(scheduler, redis, now=late)
    finally:
        scheduler.shutdown(wait=False)


async def test_caught_up_runs_keep_their_order(redis: Any) -> None:
    """Лестница читает вердикты суда: она не стартует, пока суд не закончил."""
    events: list[str] = []
    scheduler = _scheduler(events)
    try:
        await catch_up.catch_up_missed(scheduler, redis, now=MONDAY)
        await _settle()
    finally:
        scheduler.shutdown(wait=False)

    assert events == ["judge:start", "judge:end", "ladder:start"]


async def test_a_successful_scheduled_run_leaves_its_mark(redis: Any) -> None:
    scheduler = _scheduler([])
    try:
        catch_up.remember_runs(scheduler)
        scheduler._dispatch_event(
            JobExecutionEvent(EVENT_JOB_EXECUTED, "ladder", "default", LADDER_DUE)
        )
        await _settle()
        assert await redis.get(catch_up.RAN_KEY.format(job_id="ladder")) == LADDER_DUE.isoformat()
    finally:
        scheduler.shutdown(wait=False)


def test_the_scheduler_remembers_its_runs() -> None:
    """Подписка стоит в боевом наборе, а не только в тесте."""
    scheduler = build_scheduler()
    listeners = [callback for callback, mask in scheduler._listeners if mask & EVENT_JOB_EXECUTED]
    assert any(getattr(c, "__qualname__", "").startswith("remember_runs") for c in listeners)
