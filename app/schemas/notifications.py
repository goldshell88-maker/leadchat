"""Схемы центра уведомлений (14 §4 «Интерфейс программный»).

Фронт не собирает текст уведомления сам и не знает словаря событий: он
получает готовый заголовок, готовое тело и готовую надпись на кнопке. Так
веб, десктоп (04 §3) и будущая почта говорят одними словами.
"""

import uuid
from typing import Any

from pydantic import BaseModel, Field


class NotificationEntityOut(BaseModel):
    """Связанная сущность — по ней фронт строит ссылку «Посмотреть диалог»."""

    type: str  # conversation | account | user
    id: str | None


class NotificationActionOut(BaseModel):
    """Кнопка уведомления. ``code`` уезжает обратно в POST /{id}/action."""

    code: str
    label: str


class NotificationOut(BaseModel):
    id: uuid.UUID
    kind: str
    severity: str  # critical | warning | info
    title: str
    body: str | None
    entity: NotificationEntityOut | None
    action: NotificationActionOut | None
    # «повторялось 12 раз» вместо двенадцати одинаковых строк (14 §4)
    repeat_count: int
    is_read: bool
    audience: str | None  # admin | head — null у адресного уведомления
    created_at: str  # ISO 8601 UTC с Z (01 §1.5) — первое событие
    last_seen_at: str  # последний повтор; по нему отсортирована лента
    expires_at: str


class NotificationsPageOut(BaseModel):
    limit: int
    offset: int
    total: int


class NotificationsOut(BaseModel):
    items: list[NotificationOut]
    page: NotificationsPageOut
    # Счётчик колокольчика едет вместе со списком — открытая страница
    # уведомлений не должна делать второй запрос ради бейджа.
    unread: int


class UnreadCountOut(BaseModel):
    unread: int
    critical: int
    warning: int
    info: int


class MarkReadOut(BaseModel):
    """Ответ на «прочитано» — со свежим счётчиком, чтобы бейдж не отставал."""

    marked: int
    unread: int


class ActionRequest(BaseModel):
    """Тело POST /{id}/action. Пусто — берётся действие из типа события."""

    action: str | None = Field(default=None, max_length=64)


class ActionOut(BaseModel):
    action: str
    # Что вернуло действие: ссылка на переподключение, адрес высланного
    # приглашения и т.п. Форму определяет вызванная функция своей зоны.
    result: dict[str, Any] | None
    unread: int
