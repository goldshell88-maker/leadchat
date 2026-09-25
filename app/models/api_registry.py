"""api_registry — свои записи в мониторе внешних сервисов (владелец 16.09).

ЗАЧЕМ ТАБЛИЦА. Встроенные сервисы (DaData, Яндекс, Ahunter, Спеллер,
OpenRouter…) описаны в коде: у них есть ключи в окружении, счётчики в Redis
и переключатели в настройках — их состояние монитор считает сам. Но владелец
хочет и «вручную добавлять API и смотреть лимиты»: демо-ключ 2ГИС на тысячу
запросов, аккаунт Foursquare, ключ, который ещё не подключён. Это записи
человека — имя, адрес, лимит, заметка, — и живут они здесь.
"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ApiRegistryEntry(Base):
    __tablename__ = "api_registry"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    #: Короткий ключ для адресной строки и Redis («2gis-demo»); уникален.
    key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    #: Зачем сервис нужен — словами владельца.
    purpose: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Адрес, по которому монитор проверяет доступность (https://…).
    url: Mapped[str | None] = mapped_column(Text)
    docs_url: Mapped[str | None] = mapped_column(Text)
    daily_limit: Mapped[int | None] = mapped_column(Integer)
    monthly_limit: Mapped[int | None] = mapped_column(Integer)
    notes: Mapped[str] = mapped_column(Text, nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
