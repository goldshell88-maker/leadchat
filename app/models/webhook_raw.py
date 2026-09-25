"""webhook_raw_log — raw webhook payload store, 30 days (08 §2.6; DESIGN §7 risk #2).

No FK on account_id: the log must survive any account manipulation.
Written by the inbound worker only; the gateway never touches PostgreSQL.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, DateTime, Identity, Index, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import JSONB


class WebhookRawLog(Base):
    __tablename__ = "webhook_raw_log"
    __table_args__ = (
        Index("uq_webhook_raw_stream", "stream_id", unique=True),
        Index("ix_webhook_raw_received", "received_at"),
        # ⚠ ИНДЕКСА ПО account_id ЗДЕСЬ БОЛЬШЕ НЕТ (миграция 0062). Он был
        # самым крупным мертвецом базы — 6688 кБ при нуле сканов за 20+
        # суток: читатели этого журнала ходят по received_at, по error и по
        # stream_id, а по аккаунту не ходит никто. Вернуть строку — значит
        # вернуть индекс автогеном, поэтому её тут и не должно быть.
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)  # no FK by design
    stream_id: Mapped[str] = mapped_column(Text, nullable=False)  # webhooks:avito entry id
    payload: Mapped[Any] = mapped_column(JSONB, nullable=False)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    processed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text)
