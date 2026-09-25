"""Схемы журнала аудита (01 §9.7; экран 11 §4.2)."""

import uuid
from typing import Any

from pydantic import BaseModel


class AuditActorOut(BaseModel):
    """Автор действия. ``null`` в строке журнала — система, бот или CLI."""

    id: uuid.UUID
    full_name: str


class AuditItemOut(BaseModel):
    id: int
    user: AuditActorOut | None
    action: str
    # Человекочитаемое описание действия — считается на бэкенде (services/audit.describe),
    # чтобы словарь событий не разъезжался между журналом, фронтом и десктопом.
    description: str
    entity: str | None
    entity_id: str | None
    details: dict[str, Any] | None
    created_at: str  # ISO 8601 UTC с Z (01 §1.5)


class AuditPageOut(BaseModel):
    limit: int
    offset: int
    total: int


class AuditLogOut(BaseModel):
    items: list[AuditItemOut]
    page: AuditPageOut


class AuditFilterActionOut(BaseModel):
    action: str
    label: str


class AuditFilterActorOut(BaseModel):
    id: uuid.UUID
    full_name: str
    #: active | inactive (отключён) | deleted (удалён): журнал хранит их действия.
    state: str


class AuditFiltersOut(BaseModel):
    """Пункты фильтров журнала: весь реестр действий и все сотрудники."""

    actions: list[AuditFilterActionOut]
    actors: list[AuditFilterActorOut]
