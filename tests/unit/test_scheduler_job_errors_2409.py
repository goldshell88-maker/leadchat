"""Сбой задачи планировщика виден в журнале JSON (проверка 24.09).

APScheduler пишет исключение задачи через stdlib logging, который в процессе не
настроен: текст уходил в stderr мимо журнала JSON, и задачу, падающую каждый
прогон, нельзя было найти ни поиском, ни сводкой по событиям.
"""

from __future__ import annotations

from datetime import UTC, datetime

from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent
from structlog.testing import capture_logs

from app.scheduler import main as scheduler_main

WHEN = datetime(2026, 9, 24, 3, 10, tzinfo=UTC)


def test_a_failed_job_becomes_an_error_record() -> None:
    try:
        raise RuntimeError("партиция не создалась")
    except RuntimeError as exc:
        event = JobExecutionEvent(EVENT_JOB_ERROR, "partitions", None, WHEN, exception=exc)

    with capture_logs() as logs:
        scheduler_main.on_job_event(event)

    (record,) = logs
    assert (record["event"], record["log_level"], record["job_id"]) == (
        "scheduler.job_failed",
        "error",
        "partitions",
    )
    assert "партиция не создалась" in record["error"]


def test_a_missed_run_is_a_warning() -> None:
    with capture_logs() as logs:
        scheduler_main.on_job_event(
            JobExecutionEvent(EVENT_JOB_MISSED, "address_judge_daily", None, WHEN)
        )

    assert [(e["event"], e["log_level"]) for e in logs] == [("scheduler.job_missed", "warning")]


def test_the_scheduler_listens_for_failures_and_misses() -> None:
    scheduler = scheduler_main.build_scheduler()

    masks = [
        mask for callback, mask in scheduler._listeners if callback is scheduler_main.on_job_event
    ]
    assert masks and masks[0] & EVENT_JOB_ERROR and masks[0] & EVENT_JOB_MISSED
