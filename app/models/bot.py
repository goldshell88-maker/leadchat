"""bots — DESIGN §4.4."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, CheckConstraint, DateTime, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import JSONB


class Bot(Base):
    __tablename__ = "bots"

    __table_args__ = (
        # ОГРАНИЧЕНИЕ НА СПИСОК ПОСТАВЩИКОВ ОТВЕТА (миграция 0035, docs/42).
        #
        # Значения выписаны ЛИТЕРАЛАМИ, а не подставлены из
        # `app.bots.provider.PROVIDERS`, по той же причине, что у `status` и
        # `outcome` в `models/conversation.py`: ограничение уезжает в миграцию
        # текстом SQL, и собранное из кода оно означало бы, что содержимое уже
        # применённой миграции зависит от версии приложения. Разъезд со списком
        # в коде ловит охранный тест в `tests/unit/test_bot_leadbot.py`.
        #
        # Чем это опасно именно здесь: в колонке лежит ответ на вопрос «чей
        # текст читает клиент». Опечатка в ней не падает и не подсвечивается —
        # `provider_of` честно откатится на Claude, — и бот, который владелец
        # считает переведённым на лид-бота, месяц отвечал бы чужим регламентом.
        CheckConstraint("ai_provider IN ('claude','leadbot')", name="ai_provider"),
        # Справочник режимов закрыт по той же причине и тем же способом (миграция 0036).
        # Объявлен здесь, чтобы схема, собранная из метаданных (тесты, локальный стенд),
        # совпадала с боевой: иначе проверка жила бы только на проде и впервые падала бы
        # тоже там. В колонке — ответ на вопрос «дойдёт ли текст до клиента вообще».
        CheckConstraint("mode IN ('suggest','auto')", name="mode"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    schedule: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=lambda: {"always": True}
    )
    scenario: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)  # step graph
    knowledge_base: Mapped[str | None] = mapped_column(Text)  # for ai_answer
    # Кто думает за шаг `ai_answer`: наш Claude (`app/bots/ai.py`) или лид-бот
    # владельца по HTTP (`app/bots/leadbot.py`). Добавлено миграцией 0035.
    #
    # ПОЧЕМУ У БОТА, А НЕ У КАНАЛА — разбор в `app/bots/provider.py`: всё, что
    # определяет ответ (сценарий, база знаний, расписание), уже лежит здесь, а
    # канал только доставляет. Значение по умолчанию `claude` выбрано не для
    # симметрии: подключение лид-бота не имеет права само собой сменить мозг
    # работающему боту — у него другой регламент, другие цены и другие
    # формулировки, и заметили бы подмену уже по ответам клиентам.
    ai_provider: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="claude", default="claude"
    )
    # Что делать с ответом: «suggest» — положить заметкой оператору, клиент не получает
    # ничего; «auto» — отправить клиенту. Добавлено миграцией 0036.
    #
    # Это ВТОРОЙ, независимый от `ai_provider` вопрос: тот решает, КТО придумал текст,
    # этот — дойдёт ли текст до клиента. Умолчание «подсказка»: на живой аккаунт бота
    # выпускают, только посмотрев, что он пишет, и переключают руками.
    mode: Mapped[str] = mapped_column(
        Text, nullable=False, server_default="suggest", default="suggest"
    )
    # ЗАПИСЬ, КОТОРОЙ НЕТ В ИНТЕРФЕЙСЕ БОТОВ. Добавлено миграцией 0038 по
    # решению владельца: «сам LeadBot это не совсем бот, я хочу чтобы у него
    # была своя собственная вкладка и настройки».
    #
    # Снаружи у лид-бота свой раздел, и в списке ботов его нет. Внутри он
    # обязан остаться обычным ботом: отвечает он на шаге `ai_answer` сценария,
    # а в движке живут расписание, замолкание при ответе менеджера, handoff,
    # лимиты и правило «недоступность ИИ не блокирует доставку». Вынеси его
    # оттуда «ради чистоты» — всё это пришлось бы написать заново, и каждая
    # потерянная мелочь означала бы сообщение, которого клиент не дождался.
    #
    # Поэтому признак, а не отдельная таблица: движку такая запись — обычный
    # бот, и развилка «а это точно бот?» не расползается по движку. Прячет её
    # ровно один слой — выдача списка и редактор (`app/api/routes/bots.py`).
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false", default=False
    )
    # Метки времени добавлены миграцией 0005: список ботов (01 §8.1) показывает
    # «когда изменён», и эта дата обязана сходиться с audit `bot.updated`
    # (06 §0.3). `onupdate` двигает её на любом UPDATE строки бота — включая
    # enable/disable и привязку аккаунтов.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
