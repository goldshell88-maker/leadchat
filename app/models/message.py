"""messages — DESIGN §4.4.

In PostgreSQL the table is ``PARTITION BY RANGE (created_at)`` with a
``search tsvector`` generated column — both are written by hand in the
initial migration (08 §1.4, autogenerate rules 2–3). The ORM model is a
plain table with the composite PK; monthly partitions are created by the
scheduler job later (08 §6.3). The ``search`` column is intentionally
absent from the model — it is DB-maintained and queried with raw SQL.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Text, Uuid, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import JSONB


def _now() -> datetime:
    return datetime.now(UTC)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Webhook idempotency (partial unique; hand-written in migration for PG)
        Index(
            "uq_messages_conversation_external_created",
            "conversation_id",
            "external_message_id",
            "created_at",
            unique=True,
            postgresql_where=text("external_message_id IS NOT NULL"),
            sqlite_where=text("external_message_id IS NOT NULL"),
        ),
        # Идемпотентность исходящих — второй эшелон в БД (08 §8.2/§8.3):
        # первый рубеж это ключ ``idem:msg:{conv}:{cmid}`` в Redis, но при
        # потере Redis дубль обязан упереться в БД. ``created_at`` в ключе —
        # требование PostgreSQL: уникальный индекс партиционированной таблицы
        # обязан включать ключ партиционирования (как и индекс вебхуков выше).
        Index(
            "uq_messages_conversation_client_message_created",
            "conversation_id",
            "client_message_id",
            "created_at",
            unique=True,
            postgresql_where=text("client_message_id IS NOT NULL"),
            sqlite_where=text("client_message_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id"), nullable=False
    )
    external_message_id: Mapped[str | None] = mapped_column(Text)
    # tempId фронта (01 §1.6): приходит в POST /messages и возвращается в
    # MessageOut, чтобы оптимистичный пузырь склеился с настоящим сообщением.
    client_message_id: Mapped[str | None] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(Text, nullable=False)  # in | out | note | system
    # client | operator | bot | system | avito
    #
    # `avito` — служебная запись САМОГО Авито в ленте (всегда с
    # direction='system'). От нашей собственной системной записи
    # (sender_type='system': «Статус: Новый → В работе. Иванов») отличается
    # источником: там мы рассказываем про свою работу, здесь чужие слова, за
    # которыми может стоять действие. CHECK'а на колонке нет ни в модели, ни в
    # 0001_init — новое значение миграции не требует.
    #
    # Пара `('out','system')` — вопрос системы об адресе (workers/address_ask.py):
    # для статистики и сторожей ожидания невидим, для разбора ответа клиента —
    # исходящее (inbound._оператор_спросил_адрес фильтрует только direction).
    sender_type: Mapped[str] = mapped_column(Text, nullable=False)
    sender_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("users.id"))
    body: Mapped[str | None] = mapped_column(Text)
    attachments: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    #: На какое сообщение это ответ. Наша собственная связь, не от Авито.
    #:
    #: ⚠ У АВИТО ЦИТИРОВАНИЯ В API НЕТ — установлено тремя проверками (разбор в
    #: миграции 0060). Поэтому здесь только то, на что ответил ДИСПЕТЧЕР: на что
    #: ответил клиент, нам не сообщают, и выдумывать это нельзя.
    #:
    #: ⚠ ПАРА КОЛОНОК, А НЕ ОДНА. `messages` партиционирована помесячно, ключ
    #: составной `(id, created_at)`. Без даты неизвестно, в какой из 28 партиций
    #: искать строку, и выборка цитаты обошла бы их все.
    reply_to_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    reply_to_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_status: Mapped[str] = mapped_column(
        Text, nullable=False, default="delivered"
    )  # pending | delivered | failed
    #: Машинная расшифровка голосового — наш Whisper, не облако.
    #:
    #: ⚠ ХРАНИМ ТОЛЬКО ТЕКСТ. Сама запись живёт у Авито; мы качаем её во
    #: временный файл и удаляем сразу после расшифровки (просьба владельца:
    #: «пусть он потом их удаляет»). У сообщений без голосового колонка пуста
    #: всегда.
    voice_transcript: Mapped[str | None] = mapped_column(Text)
    #: Состояние расшифровки: NULL — не начинали, дальше значения из
    #: app/services/voice.py (running | done | failed | too_long).
    #:
    #: ⚠ БЕЗ ЭТОЙ КОЛОНКИ ПОВТОРНЫЙ ЗАПУСК НЕОТЛИЧИМ ОТ ПЕРВОГО: пустой текст
    #: означал бы и «ещё не считали», и «посчитали, а там тишина», и «не
    #: вышло». CHECK'а на значениях нет — как и у соседних `direction`,
    #: `sender_type`, `delivery_status`: новое состояние не должно требовать
    #: миграции.
    voice_transcript_status: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        primary_key=True,
        default=_now,
        server_default=func.now(),
    )
