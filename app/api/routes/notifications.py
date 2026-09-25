"""Центр уведомлений — `/notifications` (14 §4, экран 14 §3).

Права (DESIGN §5.1, матрица 01 §12):

* раздел целиком закрыт правом ``conversations:manage`` — admin, head,
  manager; **наблюдатель не видит ничего** (у него только чтение диалогов);
* системные уведомления (рассылка ``audience='admin'``) — только
  администраторам, рабочие (``audience='head'``) — руководителю и админу,
  адресные — своему получателю. Это решает не ручка, а
  ``services.notifications.visibility_condition``: один предикат на список,
  счётчик, чтение и действие — разъехаться им негде.

Своего права ``notifications:read`` в каталоге 01 §12 пока нет (rbac.py —
чужая зона); почему взято ``conversations:manage`` и как это меняется одной
строкой — см. шапку services/notifications.py.
"""

import uuid
from datetime import date

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.core.errors import ApiError
from app.models import User
from app.models.notification import SEVERITIES, Notification
from app.schemas.notifications import (
    ActionOut,
    ActionRequest,
    MarkReadOut,
    NotificationActionOut,
    NotificationEntityOut,
    NotificationOut,
    NotificationsOut,
    NotificationsPageOut,
    UnreadCountOut,
)
from app.services import notifications as svc
from app.ws.events import iso

router = APIRouter()

notifications_perm = require_permission(svc.SECTION_PERMISSION)

MAX_LIMIT = 200


def _out(row: Notification, *, is_read: bool) -> NotificationOut:
    action = svc.action_for(row.kind)
    return NotificationOut(
        id=row.id,
        kind=row.kind,
        severity=row.severity,
        title=row.title,
        body=row.body,
        entity=(
            NotificationEntityOut(type=row.entity_type, id=row.entity_id)
            if row.entity_type
            else None
        ),
        action=(NotificationActionOut(code=action.code, label=action.label) if action else None),
        repeat_count=row.repeat_count,
        is_read=is_read,
        audience=row.audience,
        created_at=iso(svc.as_utc(row.created_at)) or "",
        last_seen_at=iso(svc.as_utc(row.last_seen_at)) or "",
        expires_at=iso(svc.as_utc(row.expires_at)) or "",
    )


@router.get("/notifications", response_model=NotificationsOut)
async def list_notifications(
    severity: str | None = Query(None, description="critical | warning | info"),
    kind: str | None = Query(None, max_length=64, description="Тип события из каталога 14 §2"),
    date_from: date | None = Query(None, description="Дата по Москве, включительно (06 §0.1)"),
    date_to: date | None = Query(None, description="Дата по Москве, включительно"),
    unread_only: bool = Query(False, description="Только непрочитанные"),
    limit: int = Query(50, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    user: User = Depends(notifications_perm),
    db: AsyncSession = Depends(get_db),
) -> NotificationsOut:
    """Полный журнал с фильтрами (14 §3 «Страница /notifications»)."""
    if severity is not None and severity not in SEVERITIES:
        raise ApiError(
            "validation_error",
            "Неизвестная важность",
            status=400,
            details={
                "fields": [{"field": "severity", "rule": "enum", "message": " | ".join(SEVERITIES)}]
            },
        )
    if date_from and date_to and date_from > date_to:
        raise ApiError(
            "validation_error",
            "Начало периода позже его конца",
            status=400,
            details={"fields": [{"field": "date_from", "rule": "range", "message": "> date_to"}]},
        )

    rows, total, read = await svc.list_for_user(
        db,
        user,
        severity=severity,
        kind=kind,
        date_from=date_from,
        date_to=date_to,
        unread_only=unread_only,
        limit=limit,
        offset=offset,
    )
    counts = await svc.unread_counts(db, user)
    return NotificationsOut(
        items=[_out(r, is_read=r.id in read) for r in rows],
        page=NotificationsPageOut(limit=limit, offset=offset, total=total),
        unread=counts["unread"],
    )


@router.get("/notifications/unread-count", response_model=UnreadCountOut)
async def unread_count(
    user: User = Depends(notifications_perm),
    db: AsyncSession = Depends(get_db),
) -> UnreadCountOut:
    """Счётчик для колокольчика (14 §3). Разбивка по важности — чтобы фронт
    знал, красить бейдж красным или серым, без второго запроса."""
    counts = await svc.unread_counts(db, user)
    return UnreadCountOut(**counts)


@router.post("/notifications/{notification_id}/read", response_model=MarkReadOut)
async def read_notification(
    notification_id: uuid.UUID,
    user: User = Depends(notifications_perm),
    db: AsyncSession = Depends(get_db),
) -> MarkReadOut:
    """Отметить прочитанным. Идемпотентно: повтор даёт ``marked: 0``."""
    row = await svc.get_visible(db, user, notification_id)
    marked = await svc.mark_read(db, user, row)
    await db.commit()
    counts = await svc.unread_counts(db, user)
    return MarkReadOut(marked=int(marked), unread=counts["unread"])


@router.post("/notifications/read-all", response_model=MarkReadOut)
async def read_all_notifications(
    user: User = Depends(notifications_perm),
    db: AsyncSession = Depends(get_db),
) -> MarkReadOut:
    """«Отметить все прочитанными» (14 §3) — в пределах видимости этой роли."""
    marked = await svc.mark_all_read(db, user)
    await db.commit()
    counts = await svc.unread_counts(db, user)
    return MarkReadOut(marked=marked, unread=counts["unread"])


@router.post("/notifications/{notification_id}/action", response_model=ActionOut)
async def run_notification_action(
    notification_id: uuid.UUID,
    body: ActionRequest | None = None,
    user: User = Depends(notifications_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ActionOut:
    """Выполнить действие уведомления — «одно нажатие вместо консоли» (14 §3).

    Реестр действий расширяемый (services/notifications.ACTIONS); сами
    действия живут в чужих модулях и зовутся поздним импортом. Ненаписанный
    модуль — это ``503 action_unavailable`` с человеческим текстом, а не 500.
    """
    row = await svc.get_visible(db, user, notification_id)
    result = await svc.run_action(
        db, redis, user=user, row=row, action_code=body.action if body else None
    )
    await db.commit()
    counts = await svc.unread_counts(db, user)
    return ActionOut(action=result["action"], result=result["result"], unread=counts["unread"])
