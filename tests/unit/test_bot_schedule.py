"""Расписание бота и рабочие часы — 02 §2.5 (кейс «Расписание» из 07 §1.1).

Чистые функции, ни БД, ни Redis: время подаётся аргументом, а не
`time_machine` — так же, как его подаёт песочница редактора (02 §5.3).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.bots.schedule import (
    DAYS,
    in_work_hours,
    interval_hits,
    is_bot_scheduled_now,
    resolve_tz,
    schedule_of,
)

MSK = resolve_tz("Europe/Moscow")

# Расписание из таблицы 07 §1.1: «только вне рабочих часов 20:00–10:00».
NIGHT_SCHEDULE = {
    "always": False,
    "timezone": "Europe/Moscow",
    "intervals": [
        {"days": list(DAYS), "start": "20:00", "end": "10:00"},
    ],
}
WEEKEND_SCHEDULE = {
    "always": False,
    "timezone": "Europe/Moscow",
    "intervals": [{"days": ["sat", "sun"], "start": "10:00", "end": "20:00"}],
}


def bot(schedule: dict | None) -> SimpleNamespace:
    """Утиный «бот»: расписание читается и с ORM-объекта, и с дикта."""
    return SimpleNamespace(schedule=schedule)


def msk(day: int, hour: int, minute: int = 0, month: int = 8, year: int = 2026) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=MSK)


# ------------------------------------------------------- круглосуточный режим


def test_default_schedule_is_always_on():
    assert is_bot_scheduled_now(bot(None), msk(4, 15)) is True
    assert is_bot_scheduled_now(bot({}), msk(4, 3)) is True
    assert is_bot_scheduled_now(bot({"always": True}), msk(4, 3)) is True
    assert is_bot_scheduled_now(None, msk(4, 3)) is True


def test_always_false_without_intervals_never_runs():
    """«По расписанию» без единого интервала — это выключено, а не 24/7."""
    assert is_bot_scheduled_now(bot({"always": False}), msk(4, 15)) is False
    assert is_bot_scheduled_now(bot({"always": False, "intervals": []}), msk(4, 15)) is False


# ------------------------------------------------ кейс 07 §1.1 «Расписание»


def test_night_schedule_15_00_off_21_00_on():
    """Ровно требование таблицы: в 15:00 бот не стартует, в 21:00 стартует."""
    assert is_bot_scheduled_now(bot(NIGHT_SCHEDULE), msk(4, 15)) is False
    assert is_bot_scheduled_now(bot(NIGHT_SCHEDULE), msk(4, 21)) is True


def test_night_schedule_covers_after_midnight_tail():
    """20:00–10:00 — это хвост суток И голова следующих (интервал через полночь)."""
    assert is_bot_scheduled_now(bot(NIGHT_SCHEDULE), msk(5, 2)) is True
    assert is_bot_scheduled_now(bot(NIGHT_SCHEDULE), msk(5, 9, 59)) is True
    assert is_bot_scheduled_now(bot(NIGHT_SCHEDULE), msk(5, 10)) is False  # правая граница строгая
    assert is_bot_scheduled_now(bot(NIGHT_SCHEDULE), msk(4, 20)) is True  # левая — включительно


def test_utc_input_is_converted_to_moscow():
    """Момент приходит в UTC (так его отдаёт воркер) — сдвиг +3 обязан учитываться."""
    # 18:30 UTC = 21:30 МСК -> ночной интервал активен
    assert is_bot_scheduled_now(bot(NIGHT_SCHEDULE), datetime(2026, 8, 4, 18, 30, tzinfo=UTC))
    # 12:00 UTC = 15:00 МСК -> нет
    assert not is_bot_scheduled_now(bot(NIGHT_SCHEDULE), datetime(2026, 8, 4, 12, 0, tzinfo=UTC))


def test_naive_datetime_is_treated_as_utc():
    assert is_bot_scheduled_now(bot(NIGHT_SCHEDULE), datetime(2026, 8, 4, 18, 30))


# --------------------------------------------------------------- дни недели


def test_days_filter_applies_to_the_day_the_interval_starts():
    # 2026-08-08 — суббота, 2026-08-10 — понедельник
    assert is_bot_scheduled_now(bot(WEEKEND_SCHEDULE), msk(8, 12)) is True
    assert is_bot_scheduled_now(bot(WEEKEND_SCHEDULE), msk(10, 12)) is False


def test_over_midnight_interval_belongs_to_the_starting_day():
    """Интервал пт 22:00–06:00 живёт и в субботу утром — по дню НАЧАЛА."""
    schedule = {
        "always": False,
        "timezone": "Europe/Moscow",
        "intervals": [{"days": ["fri"], "start": "22:00", "end": "06:00"}],
    }
    assert is_bot_scheduled_now(bot(schedule), msk(7, 23)) is True  # пятница 23:00
    assert is_bot_scheduled_now(bot(schedule), msk(8, 3)) is True  # суббота 03:00 — хвост пятницы
    assert (
        is_bot_scheduled_now(bot(schedule), msk(8, 23)) is False
    )  # суббота 23:00 — уже не тот день


def test_intervals_are_or_ed():
    schedule = {
        "always": False,
        "intervals": [
            {"days": ["tue"], "start": "08:00", "end": "09:00"},
            {"days": ["tue"], "start": "18:00", "end": "19:00"},
        ],
    }
    assert is_bot_scheduled_now(bot(schedule), msk(4, 8, 30)) is True
    assert is_bot_scheduled_now(bot(schedule), msk(4, 18, 30)) is True
    assert is_bot_scheduled_now(bot(schedule), msk(4, 12)) is False


# -------------------------------------------------------------- устойчивость


def test_broken_interval_does_not_raise():
    """Кривой интервал — не исключение в воркере, а «не попали»."""
    schedule = {"always": False, "intervals": [{"start": "25:00", "end": "оно"}, "мусор"]}
    assert is_bot_scheduled_now(bot(schedule), msk(4, 15)) is False


def test_unknown_timezone_falls_back_to_moscow():
    schedule = {**NIGHT_SCHEDULE, "timezone": "Mars/Olympus"}
    assert is_bot_scheduled_now(bot(schedule), msk(4, 21)) is True


def test_resolve_tz_offset_is_msk():
    assert resolve_tz("Europe/Moscow").utcoffset(datetime(2026, 8, 4)) == timedelta(hours=3)
    assert resolve_tz(None).utcoffset(datetime(2026, 1, 4)) == timedelta(hours=3)


def test_schedule_of_accepts_plain_dict():
    assert schedule_of({"always": False, "intervals": []}) == {"always": False, "intervals": []}
    assert schedule_of(bot(None)) == {"always": True}


def test_interval_hits_directly():
    local = msk(4, 21).astimezone(MSK)
    assert interval_hits({"start": "20:00", "end": "10:00"}, local) is True
    assert interval_hits({"start": "10:00", "end": "20:00"}, local) is False
    assert interval_hits("не дикт", local) is False


# ---------------------------------------------- work_hours для шага condition


def test_work_hours_condition_day_and_night():
    cond = {"kind": "work_hours", "from": "10:00", "to": "20:00", "timezone": "Europe/Moscow"}
    assert in_work_hours(cond, msk(4, 12)) is True
    assert in_work_hours(cond, msk(4, 22, 30)) is False  # кейс «Первичный приём, ночь»
    assert in_work_hours(cond, msk(4, 10)) is True
    assert in_work_hours(cond, msk(4, 20)) is False


def test_work_hours_respects_days_and_bad_input():
    cond = {"kind": "work_hours", "from": "10:00", "to": "20:00", "days": ["sat", "sun"]}
    assert in_work_hours(cond, msk(8, 12)) is True  # суббота
    assert in_work_hours(cond, msk(4, 12)) is False  # вторник
    assert in_work_hours(None, msk(4, 12)) is False
    assert in_work_hours({"kind": "work_hours"}, msk(4, 12)) is False


# ЗДЕСЬ ЖИЛ `test_describe_reads_for_humans` — снят вместе с самой `describe`
# 23.08. Он был её ЕДИНСТВЕННЫМ потребителем: тест, который держит функцию,
# нужную только этому тесту, доказывает не работу продукта, а сам себя.
