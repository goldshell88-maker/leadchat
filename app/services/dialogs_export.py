"""Выгрузка «Разбора диалогов» — фоном, как выгрузка статистики (проверка 24.09).

ЧТО БЫЛО. CSV собирался прямо в запросе, в памяти, с потолком 10 000 строк.
Потолок оказался меньше любого готового периода: на бою «7 дней» — 11 191
строка, «30 дней» (умолчание экрана) — 44 052. Кнопка отказывала всегда, а
выгрузка имён и телефонов клиентов не писалась в журнал и не знала квоты —
в отличие от выгрузки статистики рядом.

ЧТО ЗДЕСЬ. Схема выгрузки статистики (06 §5.1): запрос ставит задачу и
отвечает 202, воркер пишет файл в `media/exports`, человек скачивает его по
подписанной ссылке, файлы убираются через семь дней. Слот и суточная квота
общие с выгрузкой статистики (`stats.reserve_export_slot`): один активный
экспорт на человека и двадцать в сутки на обе кнопки. Каждая выгрузка —
строка журнала `dialogs.exported` с фильтрами и числом строк.

Сам файл собирает `conversation_table.export_csv` — тот же код, что считает
экран: выгрузка обязана повторять увиденное.
"""

from __future__ import annotations

import contextlib
import csv
import dataclasses
import io
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from redis.asyncio import Redis

from app.core.errors import ApiError
from app.core.observability import with_job_scope
from app.services import conversation_table as table
from app.services import stats
from app.services.audit import write_audit

log = structlog.get_logger("app.dialogs_export")

EXPORT_JOB = "export_dialogs"

_DATETIME_FIELDS = ("date_from", "date_to")
_UUID_FIELDS = ("account_id", "assignee_id")


def export_filename(job_id: str, now: datetime | None = None) -> str:
    day = (now or datetime.now(UTC)).astimezone(stats.MSK).date()
    return f"leadchat-dialogs_{day}_{job_id[:8]}.csv"


def filters_to_params(filters: table.TableFilters) -> dict[str, Any]:
    """Фильтры таблицы → JSON-совместимый словарь для очереди и журнала."""
    params: dict[str, Any] = {}
    for name, value in dataclasses.asdict(filters).items():
        if value is None:
            continue
        if isinstance(value, datetime):
            params[name] = value.isoformat()
        elif isinstance(value, uuid.UUID):
            params[name] = str(value)
        else:
            params[name] = value
    return params


def filters_from_params(params: dict[str, Any]) -> table.TableFilters:
    values: dict[str, Any] = {}
    for field in dataclasses.fields(table.TableFilters):
        if field.name not in params:
            continue
        value = params[field.name]
        if field.name in _DATETIME_FIELDS:
            value = datetime.fromisoformat(value)
        elif field.name in _UUID_FIELDS:
            value = uuid.UUID(value)
        values[field.name] = value
    return table.TableFilters(**values)


async def create_job(
    redis: Redis,
    *,
    user_id: uuid.UUID,
    filters: table.TableFilters,
    sort: str,
    direction: str,
) -> str:
    """Занять слот, записать статус и поставить задачу в очередь (06 §5.1)."""
    from arq.connections import ArqRedis

    from app.services.messages import as_arq  # локально: избегаем цикла импорта

    table.check_sort(sort)
    job_id = uuid.uuid4().hex
    await stats.reserve_export_slot(redis, user_id, job_id)
    params = {
        "user_id": str(user_id),
        "filters": filters_to_params(filters),
        "sort": sort,
        "direction": direction,
    }
    key = stats.export_status_key(job_id)
    await redis.hset(  # type: ignore[misc]
        key,
        mapping={
            "status": "pending",
            "format": "csv",
            "user_id": str(user_id),
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    await redis.expire(key, stats.EXPORT_URL_TTL_SECONDS)
    try:
        await ArqRedis.enqueue_job(as_arq(redis), EXPORT_JOB, job_id, params, _job_id=job_id)
    except Exception:
        # Несостоявшаяся выгрузка не держит слот и не съедает попытку квоты —
        # то же правило, что у статистики.
        await stats.release_export_slot(redis, user_id)
        with contextlib.suppress(Exception):
            await redis.decr(stats.export_quota_key(user_id))
        await redis.hset(  # type: ignore[misc]
            key, mapping={"status": "failed", "error": stats.EXPORT_QUEUE_DOWN_TEXT}
        )
        log.exception("dialogs.export_enqueue_failed", job_id=job_id)
        raise ApiError("upstream_unavailable", stats.EXPORT_QUEUE_DOWN_TEXT, status=503) from None
    log.info("dialogs.export_enqueued", job_id=job_id, user_id=str(user_id))
    return job_id


def _data_rows(body: str) -> int:
    """Строк данных в CSV. Через разбор, а не по переводам строк: в имени
    клиента или названии объявления перевод строки законно стоит в кавычках."""
    return sum(1 for _ in csv.reader(io.StringIO(body), delimiter=";")) - 1


@with_job_scope
async def export_dialogs(ctx: dict[str, Any], job_id: str, params: dict[str, Any]) -> str:
    """ARQ-задача: файл «Разбора диалогов», журнал и статус для опроса."""
    redis: Redis = ctx["redis"]
    key = stats.export_status_key(job_id)
    user_id = params["user_id"]
    filters = filters_from_params(params.get("filters") or {})
    path = stats.export_dir() / export_filename(job_id)

    await redis.hset(key, mapping={"status": "running"})  # type: ignore[misc]
    try:
        async with ctx["db_session_factory"]() as db:
            body = await table.export_csv(
                db,
                filters,
                sort=params["sort"],
                direction=params["direction"],
                mv_refreshed_at=await stats.refreshed_moment(redis),
            )
            rows = _data_rows(body)
            path.parent.mkdir(parents=True, exist_ok=True)
            # BOM обязателен: без него русский Excel открывает CSV в cp1251 (06 §5.2).
            path.write_text(body, encoding="utf-8-sig", newline="")
            await write_audit(
                db,
                user_id=uuid.UUID(user_id),
                action="dialogs.exported",
                entity="dialogs",
                entity_id=job_id,
                details={
                    **params.get("filters", {}),
                    "sort": params["sort"],
                    "direction": params["direction"],
                    "rows": rows,
                },
            )
            await db.commit()
        await redis.hset(  # type: ignore[misc]
            key,
            mapping={
                "status": "done",
                "rows": rows,
                "relpath": f"{stats.EXPORT_DIRNAME}/{path.name}",
            },
        )
        log.info("dialogs.export_done", job_id=job_id, rows=rows)
        return "done"
    except ApiError as exc:
        # Отказ по существу — прежде всего «слишком много строк»: его текст
        # уже для человека, и повторять задачу незачем.
        path.unlink(missing_ok=True)
        await redis.hset(key, mapping={"status": "failed", "error": exc.message})  # type: ignore[misc]
        log.warning("dialogs.export_refused", job_id=job_id, code=exc.code)
        return "failed"
    except Exception:
        path.unlink(missing_ok=True)
        await redis.hset(  # type: ignore[misc]
            key, mapping={"status": "failed", "error": stats.EXPORT_FAILED_TEXT}
        )
        log.exception("dialogs.export_failed", job_id=job_id)
        raise
    finally:
        await redis.expire(key, stats.EXPORT_URL_TTL_SECONDS)
        await stats.release_export_slot(redis, user_id)
