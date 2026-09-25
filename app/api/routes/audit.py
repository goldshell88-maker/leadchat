"""Журнал аудита — `GET /audit-log` (01 §9.7, экран 11 §4.2).

Право `audit:read` — admin и head (руководителю журнал доступен только на
чтение, DESIGN §5.1); manager и observer получают 403. Ручка read-only:
записи в журнал делает бизнес-код через `services.audit.write_audit`.

Сортировка — `created_at DESC, id DESC`: события одной секунды (в PostgreSQL
это редкость, в SQLite юнит-стека — норма) обязаны идти стабильно, иначе
пагинация повторяет строки.
"""

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_permission
from app.core.errors import ApiError
from app.models import AuditLog, User
from app.schemas.audit import (
    AuditActorOut,
    AuditFilterActionOut,
    AuditFilterActorOut,
    AuditFiltersOut,
    AuditItemOut,
    AuditLogOut,
    AuditPageOut,
)
from app.services.audit import AUDIT_ACTIONS, describe, msk_day_range
from app.ws.events import iso

router = APIRouter()

audit_perm = require_permission("audit:read")

MAX_LIMIT = 200


@router.get("/audit-log/filters", response_model=AuditFiltersOut)
async def audit_log_filters(
    _: User = Depends(audit_perm),
    db: AsyncSession = Depends(get_db),
) -> AuditFiltersOut:
    """Пункты фильтров «Действие» и «Сотрудник» (проверка 24.09).

    Экран собирал «Действие» из своего словаря на 23 пункта и действий текущей
    страницы: из 72 действий, лежащих в журнале, 50 выбрать было нельзя, в том
    числе все `settings.*` — «кто выключил раздачу» не находилось. «Сотрудник»
    брался из назначаемых операторов, и удалённых, руководителя и отключённых
    в нём не было. Реестр действий один — `AUDIT_ACTIONS`, сотрудников в базе
    десятки, поэтому ни то ни другое не требует прохода по самому журналу.
    """
    users = (await db.execute(select(User).order_by(User.full_name))).scalars().all()
    return AuditFiltersOut(
        actions=sorted(
            (AuditFilterActionOut(action=a, label=label) for a, label in AUDIT_ACTIONS.items()),
            key=lambda item: item.label.lower(),
        ),
        actors=[
            AuditFilterActorOut(
                id=u.id,
                full_name=u.full_name,
                state="deleted" if u.deleted_at else ("active" if u.is_active else "inactive"),
            )
            for u in users
        ],
    )


@router.get("/audit-log", response_model=AuditLogOut)
async def list_audit_log(
    user_id: uuid.UUID | None = Query(None, description="Фильтр по сотруднику"),
    action: str | None = Query(None, max_length=64, description="Точное имя события"),
    entity: str | None = Query(None, max_length=64, description="conversation | user | client | …"),
    date_from: date | None = Query(None, description="Дата по Москве, включительно (06 §0.1)"),
    date_to: date | None = Query(None, description="Дата по Москве, включительно"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    _: User = Depends(audit_perm),
    db: AsyncSession = Depends(get_db),
) -> AuditLogOut:
    if date_from and date_to and date_from > date_to:
        raise ApiError(
            "validation_error",
            "Начало периода позже его конца",
            status=400,
            details={"fields": [{"field": "date_from", "rule": "range", "message": "> date_to"}]},
        )

    conds: list[Any] = []
    if user_id is not None:
        conds.append(AuditLog.user_id == user_id)
    if action:
        conds.append(AuditLog.action == action)
    if entity:
        conds.append(AuditLog.entity == entity)
    ts_from, ts_to = msk_day_range(date_from, date_to)
    if ts_from is not None:
        conds.append(AuditLog.created_at >= ts_from)
    if ts_to is not None:
        conds.append(AuditLog.created_at < ts_to)

    total = (
        await db.execute(select(func.count()).select_from(AuditLog).where(*conds))
    ).scalar_one()
    rows = list(
        (
            await db.execute(
                select(AuditLog)
                .where(*conds)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    # user_id в audit_log без FK (DESIGN §4.4): журнал переживает удаление
    # пользователя, поэтому автор может не найтись — отдаём null, не 500.
    actor_ids = {r.user_id for r in rows if r.user_id}
    actors = (
        {u.id: u for u in (await db.execute(select(User).where(User.id.in_(actor_ids)))).scalars()}
        if actor_ids
        else {}
    )

    def actor_of(row: AuditLog) -> AuditActorOut | None:
        user = actors.get(row.user_id) if row.user_id is not None else None
        return AuditActorOut(id=user.id, full_name=user.full_name) if user else None

    return AuditLogOut(
        items=[
            AuditItemOut(
                id=r.id,
                user=actor_of(r),
                action=r.action,
                description=describe(r.action, r.details),
                entity=r.entity,
                entity_id=r.entity_id,
                details=r.details,
                created_at=iso(r.created_at) or "",
            )
            for r in rows
        ],
        page=AuditPageOut(limit=limit, offset=offset, total=total),
    )
