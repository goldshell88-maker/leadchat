"""app_settings — настройки, которые меняет администратор из интерфейса.

ЗАЧЕМ ОТДЕЛЬНАЯ ТАБЛИЦА, А НЕ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
---------------------------------------------------
Переменные окружения задают то, что решает инженер при развёртывании: адрес
базы, ключи, лимиты запросов. Настройка «раздавать ли диалоги автоматически» —
решение управленческое, и принимает его руководитель. Держать её в `.env`
значит, что каждое включение и выключение требует правки файла на сервере и
перезапуска процессов, то есть меня. Управлять своей командой руководитель
должен без посредника.

ПОЧЕМУ КЛЮЧ-ЗНАЧЕНИЕ, А НЕ КОЛОНКА НА КАЖДУЮ НАСТРОЙКУ
-------------------------------------------------------
Настроек будет прибавляться, и колонка на каждую означала бы миграцию на
каждую. Плата за это — бесструктурность: в таблице «ключ-значение» опечатка в
имени ключа не ловится ничем. Поэтому сама таблица свободная, а доступ к ней —
нет: каждая настройка объявлена в `services/app_settings.py` явно, с типом и
значением по умолчанию, и читается только через него.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import JSONB


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    #: Значение любого типа — разбор и проверка на стороне сервиса настроек.
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    # Кто менял. Настройка, влияющая на работу всей смены, не должна меняться
    # анонимно: «почему со вчера диалоги не раздаются» — вопрос, у которого
    # обязан быть ответ.
    updated_by_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
