"""Выдачи лидов расширению «Автозаявки» (миграция 0039)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

#: Три лид-центра владельца. Коды — те же, что понимает расширение (`srcKey`):
#: по ним оно выбирает домен, куда идти, и переименовать их нельзя.
SRC_KEYS: tuple[str, ...] = ("bt", "kp", "mnc")

#: Человеческие подписи — один словарь на сервер и на экран.
SRC_LABELS: dict[str, str] = {
    "bt": "БТ — бытовая техника",
    "kp": "КП — компьютерная помощь",
    "mnc": "МНЧ — муж на час",
}

DECISION_NEW = "new"
DECISION_EXISTING = "existing-customer"
DECISION_BLOCKED = "blocked"
DECISION_RECOVERED = "recovered"
DECISION_ERROR = "error"

DECISIONS: tuple[str, ...] = (
    DECISION_NEW,
    DECISION_EXISTING,
    DECISION_BLOCKED,
    DECISION_RECOVERED,
    DECISION_ERROR,
)

#: Подписи исходов. Формулировки — с точки зрения владельца, а не расширения:
#: ему важно «заявка есть» против «заявки нет», а не как это назвал чужой код.
DECISION_LABELS: dict[str, str] = {
    DECISION_NEW: "Заявка создана, клиент новый",
    DECISION_EXISTING: "Заявка создана на известного клиента",
    DECISION_BLOCKED: "Не создана — по номеру уже висит живая заявка",
    DECISION_RECOVERED: "Заявка создана, связь при этом обрывалась",
    DECISION_ERROR: "Лид-центр отклонил",
}

#: Исходы, при которых заявка в лид-центре ЕСТЬ. Нужны отдельно: «создана» и
#: «отдали» — разные вещи, и путать их значит считать успехом отправку.
DECISIONS_CREATED: frozenset[str] = frozenset({DECISION_NEW, DECISION_EXISTING, DECISION_RECOVERED})


class LeadHandout(Base):
    """Один лид, отданный расширению, и то, чем это кончилось.

    ОДНА ЗАПИСЬ НА ДИАЛОГ — стережёт база уникальным ограничением. У одного
    обращения не бывает двух заявок; будь иначе, дубль в лид-центре стоил бы
    второго выезда мастера к тому же человеку в тот же день.
    """

    __tablename__ = "lead_handouts"

    __table_args__ = (
        UniqueConstraint("conversation_id", name="conversation_id"),
        CheckConstraint(
            "src_key IN (" + ",".join(f"'{v}'" for v in SRC_KEYS) + ")",
            name="src_key",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN (" + ",".join(f"'{v}'" for v in DECISIONS) + ")",
            name="decision",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    handed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: Копия из канала на момент выдачи: канал могут переназначить, а знать
    #: надо, куда заявка ушла НА САМОМ ДЕЛЕ.
    src_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: Сколько раз лид отдавали расширению (аудит 19.08, находка L-005).
    #: Счётчика не было вовсе: неподтверждённый лид уезжал заново каждый час
    #: бесконечно, а `handed_at` перезаписывался — история попыток стиралась, и
    #: в журнале это выглядело одной выдачей. Теперь видно и число попыток, и
    #: то, когда пора перестать: подтверждения нет сутки — дело не в сети.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decision: Mapped[str | None] = mapped_column(Text)
    #: Номер заявки в лид-центре — текстом: чужой идентификатор, и закладываться
    #: на то, что он навсегда останется числом, незачем.
    request_id: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)

    @property
    def created(self) -> bool:
        """Заявка в лид-центре есть."""
        return self.decision in DECISIONS_CREATED


__all__ = [
    "DECISIONS",
    "DECISIONS_CREATED",
    "DECISION_LABELS",
    "SRC_KEYS",
    "SRC_LABELS",
    "LeadHandout",
]
