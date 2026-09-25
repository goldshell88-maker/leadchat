"""audit_log — DESIGN §4.4 (no FK on user_id by design)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, Integer, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base
from app.models.types import JSONB

# SQLite needs INTEGER for autoincrement PKs; PG gets bigint IDENTITY.
_BigIntPK = BigInteger().with_variant(Integer(), "sqlite")


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(_BigIntPK, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    action: Mapped[str] = mapped_column(Text, nullable=False)
    entity: Mapped[str | None] = mapped_column(Text)
    entity_id: Mapped[str | None] = mapped_column(Text)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
