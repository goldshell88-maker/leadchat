"""Расписание бота и рабочие часы (02 §2.5).

Два потребителя одного и того же кода:

* :func:`is_bot_scheduled_now` — фильтр на **вход** бота в диалог
  (``should_run_bot``, 02 §2.2). Уже начатый сценарий доводится до конца
  независимо от расписания, и ``bot_ask_timeout`` срабатывает всегда: иначе
  клиент, ответивший в 10:01 на ночной вопрос, остался бы без реакции.
* :func:`in_work_hours` — условие ``work_hours`` шага ``condition`` (02 §1.3).

Таймзона по умолчанию всюду ``Europe/Moscow``; ``zoneinfo``, никакого ``pytz``.
В контейнере ``TZ`` не важен — все вычисления явные. Если tzdata в образе нет
(``python:slim`` её не обязан нести), падаем на фиксированный +03:00: Москва без
перехода на летнее время с 2014 года, поведение идентично — так же сделано в
``services/audit.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import structlog

log = structlog.get_logger("app.bots.schedule")

DAYS: tuple[str, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
DEFAULT_TIMEZONE = "Europe/Moscow"
MSK_FALLBACK = timezone(timedelta(hours=3))


def utcnow() -> datetime:
    return datetime.now(UTC)


def resolve_tz(name: Any) -> tzinfo:
    """Имя таймзоны -> tzinfo. Неизвестное имя не роняет тик — МСК по умолчанию."""
    if not isinstance(name, str) or not name:
        name = DEFAULT_TIMEZONE
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        if name != DEFAULT_TIMEZONE:
            log.warning("bot.unknown_timezone", timezone=name)
        return MSK_FALLBACK


def _hm(value: Any) -> tuple[int, int] | None:
    """«20:00» -> (20, 0). None, если формат не «HH:MM»."""
    if not isinstance(value, str) or ":" not in value:
        return None
    hours, _, minutes = value.partition(":")
    try:
        h, m = int(hours), int(minutes)
    except ValueError:
        return None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h, m


def _local(now: datetime | None, tz: tzinfo) -> datetime:
    """Момент в нужной зоне. Наивный ``now`` считаем UTC — во всём проекте так."""
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(tz)


def _days_of(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        return DAYS
    picked = tuple(str(d).lower()[:3] for d in raw if isinstance(d, str))
    return picked or DAYS


def interval_hits(interval: Any, local: datetime) -> bool:
    """Попадает ли локальное время в интервал (02 §2.5).

    ``start > end`` — интервал через полночь (20:00–10:00 = хвост сегодняшнего
    дня + голова следующего); ``days`` относится ко дню **начала** интервала.
    """
    if not isinstance(interval, dict):
        return False
    start, end = _hm(interval.get("start")), _hm(interval.get("end"))
    if start is None or end is None:
        log.warning("bot.bad_schedule_interval", interval=interval)
        return False
    now_hm = (local.hour, local.minute)
    days = _days_of(interval.get("days"))
    today = DAYS[local.weekday()]
    if start <= end:  # обычный интервал в пределах суток
        return today in days and start <= now_hm < end
    yesterday = DAYS[(local.weekday() - 1) % 7]
    return (today in days and now_hm >= start) or (yesterday in days and now_hm < end)


def schedule_of(bot: Any) -> dict[str, Any]:
    """``bots.schedule`` из ORM-объекта, дикта или None -> всегда дикт."""
    if bot is None:
        return {"always": True}
    raw = getattr(bot, "schedule", bot)
    if not isinstance(raw, dict) or not raw:
        return {"always": True}
    return raw


def is_bot_scheduled_now(bot: Any, now: datetime | None = None) -> bool:
    """Активен ли бот прямо сейчас по своему расписанию (02 §2.5).

    ``bot`` — ORM-объект ``Bot`` либо сам дикт расписания (песочница 02 §5.3
    гоняет черновик, у которого записи в БД ещё нет).
    """
    schedule = schedule_of(bot)
    if schedule.get("always", False):
        return True
    intervals = schedule.get("intervals")
    if not isinstance(intervals, list) or not intervals:
        # «always: false» без интервалов — бот выключен по расписанию.
        return False
    local = _local(now, resolve_tz(schedule.get("timezone", DEFAULT_TIMEZONE)))
    return any(interval_hits(iv, local) for iv in intervals)


def in_work_hours(cond: Any, now: datetime | None = None) -> bool:
    """Условие ``work_hours`` шага ``condition`` (02 §1.3).

    Формат совпадает с интервалом расписания, только ключи ``from``/``to``, а
    таймзона задаётся в самом условии.
    """
    if not isinstance(cond, dict):
        return False
    local = _local(now, resolve_tz(cond.get("timezone", DEFAULT_TIMEZONE)))
    return interval_hits(
        {"start": cond.get("from"), "end": cond.get("to"), "days": cond.get("days")},
        local,
    )


# ЗДЕСЬ ЖИЛА `describe` — расписание одной строкой для списка ботов. Снята 23.08:
# в бою её не звал никто, единственным потребителем был собственный тест. Строку
# для таблицы строит фронт (`scenario.ts::formatSchedule`) и там же её проверяет
# (`BotsPage.test.tsx`) — питоновская была вторым, неживым ответом на тот же вопрос.
