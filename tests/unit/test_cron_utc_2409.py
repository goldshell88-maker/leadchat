"""Ночные задачи планировщика идут по UTC, а не по поясу контейнера (24.09).

У APScheduler 3.x пояс планировщика на готовый `CronTrigger` не действует:
триггер без своего timezone берёт пояс процесса. В бою `.env` задаёт
TZ=Europe/Moscow, и все суточные и недельные задачи шли на три часа раньше
записанного в коде: судья адресов — в конце суточного счётчика модели, а не в
начале, сторож дня — в 06:15 МСК вместо 09:15.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from apscheduler.triggers import cron

from app.scheduler.main import build_scheduler


@pytest.fixture
def moscow_container(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cron, "get_localzone", lambda: ZoneInfo("Europe/Moscow"))


def test_every_cron_job_is_in_utc_whatever_the_container_zone(moscow_container) -> None:
    jobs = [j for j in build_scheduler().get_jobs() if isinstance(j.trigger, cron.CronTrigger)]

    assert jobs, "в расписании нет ни одной задачи по часам"
    assert {j.id: str(j.trigger.timezone) for j in jobs} == {j.id: "UTC" for j in jobs}


def test_the_daily_watchdog_fires_at_the_written_hour(moscow_container) -> None:
    job = next(j for j in build_scheduler().get_jobs() if j.id == "watchdog_daily")
    after = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)

    fire = job.trigger.get_next_fire_time(None, after)

    assert fire == datetime(2026, 9, 25, 6, 15, tzinfo=UTC)
