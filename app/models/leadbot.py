"""Журнал работы лид-бота (миграция 0038).

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ ПОЛЯ В `bot.py`. Здесь лежит не поведение бота
(сценарий, расписание, режим — они в `bots`), а СЛЕД его работы: по строке на
каждое обращение. Складывать след туда же, где настройки, значит смешать
«как настроено» с «что произошло».

АДРЕС И ТОКЕН — НЕ ЗДЕСЬ. Они в `app_settings` под ключом `leadbot.connection`
(`app/bots/leadbot.py`): зашифрованные, с запасом из окружения. Второе
хранилище одной настройки — две двери, которые однажды разойдутся.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import JSONB

#: Чем кончилось обращение — с точки зрения КЛИЕНТА, а не HTTP. Двухсотый ответ,
#: выброшенный за низкую уверенность, для клиента такое же молчание, как таймаут.
#: Список закрыт CHECK'ом в базе; литералы продублированы там намеренно —
#: содержимое применённой миграции не имеет права зависеть от версии кода.
OUTCOME_SENT = "sent"
OUTCOME_HINT = "hint"
OUTCOME_BRIDGE = "bridge"
OUTCOME_HANDOFF = "handoff"
OUTCOME_LOW_CONFIDENCE = "low_confidence"
OUTCOME_UNAVAILABLE = "unavailable"

OUTCOMES: tuple[str, ...] = (
    OUTCOME_SENT,
    OUTCOME_HINT,
    OUTCOME_BRIDGE,
    OUTCOME_HANDOFF,
    OUTCOME_LOW_CONFIDENCE,
    OUTCOME_UNAVAILABLE,
)

#: Человеческие подписи к исходам — один словарь на сервер и на экран.
OUTCOME_LABELS: dict[str, str] = {
    OUTCOME_SENT: "Ответ ушёл клиенту",
    OUTCOME_HINT: "Легло подсказкой оператору",
    OUTCOME_BRIDGE: "Ответил и передал человеку",
    OUTCOME_HANDOFF: "Передал человеку без ответа",
    OUTCOME_LOW_CONFIDENCE: "Ответ отброшен — низкая уверенность",
    OUTCOME_UNAVAILABLE: "Лид-бот не ответил",
}

#: Сколько символов вопроса и ответа держим в журнале. Журнал отвечает на
#: «почему бот ответил так», а не заменяет переписку: она уже лежит в
#: `messages`, и вторая её полная копия — это второе место, где переписка
#: клиентов способна утечь. Четырёхсот символов хватает, чтобы узнать реплику.
TEXT_LIMIT = 400


class LeadbotCall(Base):
    """Одно обращение к лид-боту: что спросили, что он ответил и чем кончилось.

    ЭТО И ЕСТЬ ОТВЕТ НА «ЧТОБЫ БЫЛО ВСЁ ВИДНО». Лид-бот уже сегодня отдаёт в
    `meta` слой, который ответил, эскалацию с причиной и сроком, готовность
    заявки, предупреждения и время. LeadChat часть этого писал в поток логов, а
    остальное терял. Поток логов читает один человек в проекте через ssh;
    журнал читают из интерфейса.
    """

    __tablename__ = "leadbot_calls"

    __table_args__ = (
        CheckConstraint(
            "outcome IN (" + ",".join(f"'{v}'" for v in OUTCOMES) + ")",
            name="outcome",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: SET NULL, а не CASCADE: запись про работу лид-бота, а не про переписку.
    #: Уборка старых диалогов не имеет права уносить историю его решений —
    #: иначе «сколько раз он ошибся в июле» перестаёт иметь ответ.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL"), index=True
    )
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("avito_accounts.id", ondelete="SET NULL")
    )
    #: Сквозной идентификатор: его же кладёт в ответ лид-бот (`meta.request_id`).
    #: По нему одну работу видно с обеих сторон, без сопоставления по времени.
    request_id: Mapped[str | None] = mapped_column(Text)
    question: Mapped[str | None] = mapped_column(Text)
    reply: Mapped[str | None] = mapped_column(Text)
    #: «роутер» — сработало детерминированное правило регламента, «модель» —
    #: думала модель. Главный вопрос владельца к любому ответу: это регламент
    #: или он сам придумал.
    layer: Mapped[str | None] = mapped_column(Text)
    flag: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(3, 2))
    needs_operator: Mapped[bool | None] = mapped_column(Boolean)
    outcome: Mapped[str] = mapped_column(Text, nullable=False)
    escalation_reason: Mapped[str | None] = mapped_column(Text)
    escalation_label: Mapped[str | None] = mapped_column(Text)
    escalation_deadline_min: Mapped[int | None] = mapped_column(Integer)
    lead_ready: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    ms: Mapped[int | None] = mapped_column(Integer)
    #: Причина отказа человеческими словами. Пусто у удачного обращения.
    error: Mapped[str | None] = mapped_column(Text)
