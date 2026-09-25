"""Еженедельный замер воронки адресов (18.09, автопривязка без человека).

По понедельникам в 04:40 МСК: посчитать последнюю полную неделю
(`services/address_funnel.measure`), записать снимок, сравнить с прошлой и,
если доля упала, — уведомить администраторов. Само определение счётчиков и
порогов живёт в сервисе; здесь — только расписание, транзакция и тревога.

⚠ ТРАНЗАКЦИЕЙ ВЛАДЕЕТ ЗАДАЧА. `db_mod.session_scope()` на выходе
ОТКАТЫВАЕТ открытую транзакцию (`db/session.py::_release`), а `notify` не
коммитит сам. Без явного `commit` ровная неделя (без тревоги) терялась бы
молча: `prev` был бы вечно `None`, и тревога стала бы невозможной по
построению — то есть замер выглядел бы работающим и никогда бы не сработал.

ЧТО ПРИ СБОЕ. Таймаут или ошибка базы в `measure` — исключение до commit,
строки недели нет, исключение уходит в лог планировщика (не глотаем);
повторный прогон той же недели — upsert. Перезапуск ровно в момент прогона
(выкатка в 01:40 UTC понедельника) догоняет `scheduler/catch_up.py`: в
пределах шести часов допуска задача запускается сразу после старта. Раньше
неделя терялась — расписание живёт в памяти процесса (проверка 24.09).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from apscheduler.triggers.cron import CronTrigger

from app.core import redis as redis_mod
from app.db import session as db_mod
from app.services import address_funnel
from app.services import notifications as notify_svc

log = structlog.get_logger("app.address_funnel")

JOB_ID = "address_funnel_weekly"
#: Вид события в каталоге центра уведомлений; склейка — по виду: одна
#: тревога в неделю по построению, повторы (misfire) центр складывает.
KIND = "address.funnel_dropped"
DEDUP_KEY = KIND
#: Полный проход по секциям `messages` за неделю раз в неделю ночью —
#: но держать соединение дольше трёх минут он права не имеет.
STATEMENT_TIMEOUT = "180s"
#: Недельную задачу нельзя терять из-за перезапуска: шесть часов на догон.
DEFAULTS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 6 * 3600}


def notification_body(
    week_start: Any, counts: address_funnel.FunnelCounts, prev: address_funnel.FunnelCounts | None
) -> str:
    """Без прошлой недели (первый замер, тревога только по правкам) — так и
    пишем: «против 0 %» читалось бы как падение с нуля."""
    доля = address_funnel.share_pct(counts.card, counts.dialogs)
    сравнение = (
        f"против {address_funnel.share_pct(prev.card, prev.dialogs)} % неделей раньше"
        if prev is not None
        else "первая неделя замера"
    )
    return (
        f"Неделя с {week_start:%d.%m}: диалогов с адресом {counts.dialogs}, "
        f"в карточке {counts.card} ({доля} %, {сравнение}); "
        f"правок руками поверх автоадреса той же недели {counts.edited_after_auto}. "
        f"Причины: {address_funnel.reason_words(address_funnel.compare(prev, counts))}. "
        "Подробности — «Внешние сервисы»."
    )


async def measure_last_week() -> address_funnel.FunnelCounts:
    """Точка входа планировщика: своя сессия, свой Redis (стиль bot_phantom)."""
    неделя = address_funnel.last_complete_week()
    since, until = address_funnel.week_bounds(неделя)
    redis = redis_mod.get_client()
    result: notify_svc.NotifyResult | None = None
    async with db_mod.session_scope() as db:
        if db.get_bind().dialect.name == "postgresql":
            await db.execute(sa.text(f"SET LOCAL statement_timeout = '{STATEMENT_TIMEOUT}'"))
        prev = await address_funnel.stored(db, неделя - address_funnel.WEEK)
        counts = await address_funnel.measure(db, since=since, until=until)
        await address_funnel.store(
            db, week_start=неделя, counts=counts, computed_at=datetime.now(UTC)
        )
        # ЯВНЫЙ COMMIT — до тревоги: строка недели обязана остаться, даже если
        # центр уведомлений ниже упадёт (см. шапку модуля).
        await db.commit()
        причины = address_funnel.compare(prev, counts)
        if причины:
            result = await notify_svc.notify(
                db,
                kind=KIND,
                body=notification_body(неделя, counts, prev),
                dedup_key=DEDUP_KEY,
            )
            await db.commit()
    if result is not None:
        await notify_svc.deliver(redis, result)
    log.info("address_funnel.measured", week=str(неделя), reasons=причины, **counts.as_dict())
    return counts


def register(scheduler: Any) -> None:
    """Планировщик живёт в UTC (`scheduler/main.py`): 01:40 UTC = 04:40 МСК,
    после ночных уборок и до начала смены."""
    scheduler.add_job(
        measure_last_week,
        CronTrigger(day_of_week="mon", hour=1, minute=40, timezone="UTC"),
        id=JOB_ID,
        **DEFAULTS,
    )
