"""notifications / notification_reads — центр уведомлений (14 §4).

Две таблицы вместо одной — решение про рассылку по роли, и оно объясняется
здесь, а не в коммите.

Уведомление адресуется либо конкретному человеку (``recipient_id``), либо
роли (``audience``: «всем администраторам»). Прочтение при этом всегда
персонально: если админов трое и один нажал «прочитано», у двух других
колокольчик гаснуть не должен. Отсюда развилка:

* **materialize при доставке** — на каждого админа своя строка. Тогда
  подавление повторов (§4) обязано схлопывать N строк атомарно, а хранилище
  растёт как «сбой × число админов»: ночной шторм из 12 повторов при 4
  админах — это 48 строк вместо одной. Хуже другое: набор получателей
  фиксируется в момент события, поэтому нанятый утром администратор про
  ночную поломку не узнает никогда, а у уволенного остаются его строки.
* **отдельная таблица прочтений** (выбрано) — одна строка уведомления и
  отметка на пару «уведомление × человек». Подавление повторов работает с
  одной строкой (см. services/notifications.notify), состав получателей
  вычисляется на чтении из актуальных ролей, а цена — один ``NOT EXISTS`` в
  выборке и в счётчике непрочитанного.

``read_at`` на самой строке остаётся осмысленным только у адресных
уведомлений («адресат прочитал»); у рассылки он всегда ``NULL``, а правда
про прочтение лежит в ``notification_reads``. Единый предикат «непрочитано»
для обоих случаев — отсутствие строки в ``notification_reads`` (сервис пишет
её всегда, в том числе адресату).
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

SEVERITIES: tuple[str, ...] = ("critical", "warning", "info")
AUDIENCES: tuple[str, ...] = ("admin", "head")


class Notification(Base):
    __tablename__ = "notifications"
    __table_args__ = (
        CheckConstraint("severity IN ('critical','warning','info')", name="severity"),
        CheckConstraint("audience IS NULL OR audience IN ('admin','head')", name="audience"),
        # Ровно один способ адресации. Строка без адресата не видна никому, а
        # строка с обоими адресами показалась бы дважды (лично + по роли).
        CheckConstraint("(recipient_id IS NOT NULL) <> (audience IS NOT NULL)", name="addressing"),
        CheckConstraint("repeat_count >= 1", name="repeat_count"),
        # ⚠ ИНДЕКСА «МОИ НЕПРОЧИТАННЫЕ» ЗДЕСЬ БОЛЬШЕ НЕТ (миграция 0062), И
        # ЕГО КОММЕНТАРИЙ БЫЛ НЕПРАВДОЙ. Он обещал «колокольчик: мои
        # непрочитанные» и строился по `read_at IS NULL` — а непрочитанность
        # давно живёт в отдельной таблице `notification_reads`, и по ней же
        # считают `unread_counts` и `list_for_user`. Индекс не пригодился ни
        # разу (0 сканов) и переписывался на каждой вставке.
        # Лента рассылки по роли (её непрочитанность живёт в notification_reads,
        # поэтому частичности по read_at здесь быть не может).
        Index(
            "ix_notifications_audience_created",
            "audience",
            "created_at",
            postgresql_where=text("audience IS NOT NULL"),
        ),
        # Подавление повторов: поиск «живой» записи по ключу за окно важности.
        Index(
            "ix_notifications_dedup_key",
            "dedup_key",
            "last_seen_at",
            postgresql_where=text("dedup_key IS NOT NULL"),
        ),
        # Чистка по сроку (14 §4, по умолчанию 90 дней) — без индекса это
        # ежесуточный seq scan по всей таблице.
        Index("ix_notifications_expires_at", "expires_at"),
        # Авторезолв: тревоги про диалог гаснут сами при приёме/передаче/
        # закрытии (`resolve_for_entity`), и поиск идёт по сущности — на самом
        # горячем пути системы. Частичный: у системных тревог сущности нет,
        # и авторезолв их не ищет никогда.
        Index(
            "ix_notifications_entity",
            "entity_type",
            "entity_id",
            postgresql_where=text("entity_type IS NOT NULL"),
        ),
        # ⚠ АДРЕСНАЯ ПОЛОВИНА ПРАВА ВИДЕТЬ — И БЕЗ НЕЁ НЕ РАБОТАЛА ВТОРАЯ.
        # Право видеть это «своё ЛИБО рассылка на мою роль»: `recipient_id = я
        # OR audience IN (…)`. Индекс на рассылку висел выше, на адресную часть
        # — ни одного, а `OR` с одной неиндексированной стороной даёт полный
        # перебор, сколько бы индексов ни стояло на другой.
        # Замер боя 05.09: счётчик колокольчика — «Seq Scan on notifications,
        # Rows Removed by Filter: 31 582, 7,8 мс», зовут 25 раз в минуту, то
        # есть 790 тысяч лишних прочитанных строк в минуту.
        Index(
            "ix_notifications_recipient_created",
            "recipient_id",
            "created_at",
            postgresql_where=text("recipient_id IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Конкретный получатель; NULL — рассылка по роли (14 §4).
    recipient_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE")
    )
    audience: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # каталог 14 §2
    severity: Mapped[str] = mapped_column(Text, nullable=False)  # critical | warning | info
    # Человеческий текст. Кодов ошибок в заголовке не бывает — это обращение к
    # человеку, а не строка лога (14 §4).
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str | None] = mapped_column(Text)
    entity_type: Mapped[str | None] = mapped_column(Text)  # conversation | account | user
    entity_id: Mapped[str | None] = mapped_column(Text)
    dedup_key: Mapped[str | None] = mapped_column(Text)
    # Сколько раз событие повторилось внутри окна подавления (14 §4:
    # «повторялось 12 раз» вместо двенадцати одинаковых строк).
    repeat_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    # Время последнего повтора; created_at остаётся временем ПЕРВОГО события.
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NotificationRead(Base):
    """Персональная отметка «прочитано» — в том числе для рассылки по роли."""

    __tablename__ = "notification_reads"
    __table_args__ = (Index("ix_notification_reads_user_id", "user_id"),)

    notification_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("notifications.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    read_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
