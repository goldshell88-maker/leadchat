"""address_funnel_weekly — снимки воронки адресов по неделям (18.09).

ЗАЧЕМ ТАБЛИЦА, А НЕ REDIS. Замер сравнивает неделю с прошлой и показывает
историю в мониторе; и то и другое обязано пережить перезапуск и не зависеть
от срока жизни ключа. Строка на ISO-неделю (Пн 00:00 МСК → Пн 00:00 МСК).

База замера — неизменяемое (реплики и строки по `created_at`/`detected_at`
в окне); карточки — снимок на момент расчёта, поэтому повторный пересчёт
недели меняет `card_*` и делается только руками (`address-funnel --store`).
"""

from datetime import date, datetime
from typing import Any

from sqlalchemy import Date, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import JSONB


class AddressFunnelWeek(Base):
    __tablename__ = "address_funnel_weekly"

    #: Понедельник недели по Москве.
    week_start: Mapped[date] = mapped_column(Date, primary_key=True)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    #: `FunnelCounts.as_dict()` — счётчики; форма — `services/address_funnel`.
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
