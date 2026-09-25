"""Статистика (01 §9, контракт — 06-STATS-REPORTS §4).

Права здесь — не украшение, а граница утечки данных (DESIGN §5.1, 01 §13):

* ``stats:all`` — **admin, head**. Весь раздел, любые фильтры.
* ``stats:own`` — **admin, head, manager**. Ровно один endpoint —
  ``GET /stats/my/today``, и ``user_id`` берётся из JWT: чужую статистику
  через него не увидеть даже подбором параметров.
* ``manager`` получает ``403`` на всё, кроме ``/stats/my/today``;
  ``observer`` — ``403`` на весь раздел (у него нет ни одного из прав).

Роутер тонкий: валидация query, права, форма ответа. Все определения метрик
и SQL — в ``app/services/stats.py``, чтобы виджет менеджера и дашборд
руководителя считались одним кодом и сходились цифра в цифру (06 §6.2).

Каждый ответ несёт ``refreshed_at`` — честную метку свежести MV-части
(06 §4): UI показывает «Данные обновлены в 14:05».
"""

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.models import User
from app.services import stats as stats_svc

stats_all = require_permission("stats:all")  # admin + head
stats_own = require_permission("stats:own")  # admin + head + manager

router = APIRouter()

# Права проверяются НА УРОВНЕ РОУТЕРА, а не в сигнатуре обработчика: FastAPI
# решает зависимости роутера раньше параметров запроса, поэтому наблюдатель с
# кривым периодом получит 403, а не 400. Отказ по правам обязан выигрывать у
# отказа по валидации — иначе через коды ответов просматривается контур API,
# закрытый для роли (01 §13).
protected = APIRouter(dependencies=[Depends(stats_all)])


# --- общие query-параметры (06 §4) -------------------------------------------


def common_period(
    date_from: date | None = Query(None, description="Дата по Москве, включительно"),
    date_to: date | None = Query(None, description="Дата по Москве, включительно"),
) -> stats_svc.Period:
    """``date_from``/``date_to`` → период с валидацией (≤ 366 дней, 06 §4)."""
    return stats_svc.parse_period(date_from, date_to)


def common_filters(
    account_id: uuid.UUID | None = Query(None),
    manager_id: Annotated[list[uuid.UUID] | None, Query()] = None,
) -> stats_svc.Filters:
    """``account_id`` + повторяемый ``manager_id=a&manager_id=b`` (06 §4)."""
    return stats_svc.Filters(account_id=account_id, manager_ids=tuple(manager_id or ()))


PeriodDep = Annotated[stats_svc.Period, Depends(common_period)]
FiltersDep = Annotated[stats_svc.Filters, Depends(common_filters)]


async def _with_freshness(redis: Redis, payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "refreshed_at": await stats_svc.refreshed_at(redis)}


# --- 4.1 карточки ------------------------------------------------------------


@protected.get("/stats/summary")
async def get_summary(
    period: PeriodDep,
    filters: FiltersDep,
    user: User = Depends(stats_all),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Карточки-метрики + сравнение с предыдущим периодом той же длины (06 §4.1)."""
    # Метка свежести витрины нужна ДО расчёта, а не только в ответе: по ней
    # `summary` решает, можно ли взять прошлые сутки с витрины вместо того,
    # чтобы пересчитывать весь период живьём (замер: 6,77 с против 0,010 с).
    mv_refreshed_at = await stats_svc.refreshed_at(redis)
    payload = await stats_svc.summary(db, period, filters, mv_refreshed_at=mv_refreshed_at)
    return {**payload, "refreshed_at": mv_refreshed_at}


# --- 4.2 график --------------------------------------------------------------


@protected.get("/stats/timeseries")
async def get_timeseries(
    period: PeriodDep,
    filters: FiltersDep,
    metric: str = Query("conversations_new"),
    group: str = Query("day"),
    user: User = Depends(stats_all),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Ряд для графика; ``group=hour`` — только на периоде ≤ 7 дней (06 §4.2)."""
    # Метка нужна ДО расчёта, как и в `summary`: по ней ряд решает, брать ли
    # прошлые сутки с витрины вместо пересчёта всего периода живьём.
    mv_refreshed_at = await stats_svc.refreshed_at(redis)
    payload = await stats_svc.timeseries(
        db, period, filters, metric=metric, group=group, mv_refreshed_at=mv_refreshed_at
    )
    return {**payload, "refreshed_at": mv_refreshed_at}


# --- 4.3 тепловая карта ------------------------------------------------------


@protected.get("/stats/heatmap")
async def get_heatmap(
    period: PeriodDep,
    filters: FiltersDep,
    user: User = Depends(stats_all),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """7 × 24 входящих сообщений клиентов, всегда 168 ячеек (06 §4.3)."""
    return await _with_freshness(redis, await stats_svc.heatmap(db, redis, period, filters))


# --- 4.4 таблица менеджеров --------------------------------------------------


@protected.get("/stats/managers")
async def get_managers(
    period: PeriodDep,
    filters: FiltersDep,
    sort: str = Query("messages_sent"),
    order: str = Query("desc"),
    user: User = Depends(stats_all),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Таблица по менеджерам с серверной сортировкой (06 §4.4). Пагинации нет."""
    # Метка нужна ДО расчёта, как и в `summary`: по ней таблица решает, брать
    # ли витрину или считать живьём (разбор у `_frt_rows`).
    mv_refreshed_at = await stats_svc.refreshed_at(redis)
    payload = await stats_svc.managers(
        db, period, filters, sort=sort, order=order, mv_refreshed_at=mv_refreshed_at
    )
    return {**payload, "refreshed_at": mv_refreshed_at}


# --- 4.6 виджет менеджера ----------------------------------------------------


@router.get("/stats/my/today")
async def get_my_today(
    user: User = Depends(stats_own),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """«Моя статистика за сегодня» (06 §6).

    Единственный endpoint раздела, доступный менеджеру. ``user_id`` — строго
    из JWT (объект ``user`` пришёл из БД через ``get_current_user``), никаких
    параметров: подставить чужой id физически нечем.
    """
    return await stats_svc.my_today(db, redis, user.id)


# --- 4.5 экспорт -------------------------------------------------------------


SheetName = Literal["summary", "managers", "conversations"]
DEFAULT_SHEETS: list[SheetName] = ["summary", "managers", "conversations"]


class ExportRequest(BaseModel):
    """Тело ``POST /stats/export`` (06 §4.5)."""

    format: Literal["csv", "xlsx"] = "xlsx"
    date_from: date | None = None
    date_to: date | None = None
    account_id: uuid.UUID | None = None
    manager_id: list[uuid.UUID] = Field(default_factory=list)
    sheets: list[SheetName] = Field(default_factory=lambda: list(DEFAULT_SHEETS))


@protected.post("/stats/export", status_code=202)
async def post_export(
    body: ExportRequest,
    user: User = Depends(stats_all),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Ставит выгрузку в очередь ARQ → ``202 {"job_id"}`` (06 §5.1).

    Ошибки: ``400`` — период > 366 дней; ``409 export_already_running`` — у
    пользователя уже идёт экспорт; ``429`` — исчерпан дневной лимит
    (20/сутки). Сам файл пишет воркер, API не блокируется ни на секунду.
    """
    period = stats_svc.parse_period(body.date_from, body.date_to)
    filters = stats_svc.Filters(
        account_id=body.account_id, manager_ids=tuple(body.manager_id or ())
    )
    job_id = await stats_svc.create_export_job(
        redis,
        user_id=user.id,
        period=period,
        filters=filters,
        fmt=body.format,
        sheets=list(body.sheets) or list(stats_svc.EXPORT_SHEETS),
    )
    return {"job_id": job_id}


@protected.get("/stats/export/{job_id}")
async def get_export_status(
    job_id: str,
    user: User = Depends(stats_all),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Статус выгрузки — только автору job'а (06 §4.5).

    Готовый файл отдаётся подписанной ссылкой (``signed_media_url``, TTL 24 ч)
    — тем же механизмом nginx ``secure_link``, что и вложения: ``EXPORT_DIR``
    лежит под ``/var/leadchat/media/``. Чужой job неотличим от несуществующего
    (``404``) — по коду ответа нельзя узнать, выгружал ли кто-то данные.
    """
    return await stats_svc.export_status(redis, job_id, user_id=user.id)


# Подроутер со «stats:all» монтируется в основной — main.py импортирует
# ровно один объект ``router`` (та же схема, что у остальных модулей).
router.include_router(protected)
