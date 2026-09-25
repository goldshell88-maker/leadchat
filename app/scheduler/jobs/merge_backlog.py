"""Ночной проход по двойникам: пары карточек с одним номером — в задачу объединения.

ЗАЧЕМ. Живой путь ставит проверку двойников ровно один раз — когда у карточки
появился основной номер (после commit'а входящего). Он не срабатывает: для
пар, накопленных до 12.09 (300 номеров на двух карточках); для пар, чью
постановку отбросил дедуп очереди (второй аккаунт закоммитил позже пяти
секунд); для догрузки истории (там задача не ставится намеренно); для пар,
у которых условие стало верным позже (имя дотянуто `enrich`, оператор снял
пометку). Проход ничего не решает сам — решает задача воркера по свежим
данным и настройке (`off` — молчит, `shadow` — тень, `on` — объединяет).

НОЧЬЮ И ПОРЦИЯМИ. Объединение переезжает диалоги и шлёт кадры в открытые
экраны; триста склеек в рабочие часы — залп кадров и запросов на офисный NAT.
Окно — :data:`НОЧЬ_С`…:data:`НОЧЬ_ДО` по UTC (01:00–06:00 по Москве),
порция :data:`ПОРЦИЯ` номеров раз в :data:`ИНТЕРВАЛ`, задачи с шагом
:data:`ШАГ_СЕК`. Окно проверяется ЧАСОМ ВНУТРИ задачи, а не временем старта:
`IntervalTrigger` сбрасывается каждой выкаткой.

ПАМЯТЬ О ПРОВЕРЕННЫХ. Пара, которую задача уже смотрела (любой исход),
помечается в Redis (`merge_queue.mark_seen`); проход её пропускает. Без этого
первые шестьдесят пар, отпавшие по именам, занимали бы порцию каждую ночь, а
до трёхсотой пары дело не дошло бы никогда.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from apscheduler.triggers.interval import IntervalTrigger

from app.core import redis as redis_mod
from app.db import session as db_mod
from app.models import Client
from app.services import app_settings
from app.services.merge_queue import enqueue_merge, seen_key

log = structlog.get_logger("app.merge_backlog")

ПОРЦИЯ = 20
ШАГ_СЕК = 3
ОТБОР = 500
ИНТЕРВАЛ = timedelta(minutes=10)
#: Окно по UTC: 22:00–03:00 = 01:00–06:00 по Москве.
НОЧЬ_С = 22
НОЧЬ_ДО = 3
DEFAULTS: dict[str, Any] = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}
JOB_ID = "merge_backlog"
ПЕРВЫЙ_ЗАХОД = timedelta(minutes=5)


def ночь(now: datetime) -> bool:
    час = now.astimezone(UTC).hour
    return час >= НОЧЬ_С or час < НОЧЬ_ДО


async def merge_backlog(*, now: datetime | None = None) -> int:
    """Точка входа планировщика. Возвращает число задач, ВСТАВШИХ в очередь."""
    now = now or datetime.now(UTC)
    if not ночь(now):
        return 0
    redis = redis_mod.get_client()
    async with db_mod.session_scope() as db:
        режим = await app_settings.get(db, app_settings.CLIENT_MERGE_AUTO)
        if режим not in ("shadow", "on"):
            return 0
        номера = await найти_пары(db)

    поставлено = 0
    for phone in номера:
        if await redis.exists(seen_key(phone)):
            continue
        if await enqueue_merge(redis, phone, defer_sec=поставлено * ШАГ_СЕК, backfill=True):
            поставлено += 1
        if поставлено >= ПОРЦИЯ:
            break
    if номера:
        log.info("merge.backlog", found=len(номера), enqueued=поставлено)
    return поставлено


async def найти_пары(db: Any) -> list[str]:
    """Номера, стоящие основным у двух и более живых карточек (группа — тоже пара).

    Остальные условия (история, имена, доказательства, запрет) проверяет
    задача: здесь только грубый отбор, чтобы не ставить тысячи заведомо
    пустых задач. Порядок по номеру — детерминированный, а свежесть даёт
    память о проверенных: непроверенные всплывают сами.
    """
    rows = await db.execute(
        sa.select(Client.phone)
        .where(Client.phone.is_not(None), Client.merged_into_id.is_(None))
        .group_by(Client.phone)
        .having(sa.func.count() >= 2)
        .order_by(Client.phone)
        .limit(ОТБОР)
    )
    return [r[0] for r in rows if r[0]]


def register(scheduler: Any) -> None:
    scheduler.add_job(
        merge_backlog,
        IntervalTrigger(
            seconds=int(ИНТЕРВАЛ.total_seconds()),
            start_date=datetime.now(UTC) + ПЕРВЫЙ_ЗАХОД,
        ),
        id=JOB_ID,
        **DEFAULTS,
    )
