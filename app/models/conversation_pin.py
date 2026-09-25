"""Закреплённые диалоги — ЛИЧНЫЕ (требование заказчика от 7 августа).

«Каждый может для себя лично закреплять свои взятые диалоги».

ПОЧЕМУ ЛИЧНОЕ, А НЕ ОБЩЕЕ. Общее закрепление на тринадцать операторов и
девять каналов превращается в свалку за неделю: закрепляют все, снимает
никто, и наверху списка висит десяток чужих диалогов. Плюс спор «зачем ты
открепил мой», у которого нет правильного ответа. Закрепление — это отметка
«я к этому вернусь», а «я» у каждого своё.

ПОЧЕМУ ТОЛЬКО СВОИ. Закрепить чужой диалог значит поднять наверх своего
списка то, чего ты не ведёшь и вести не будешь. Отметка полезна ровно тем,
что показывает СВОЮ работу; закрепление чужого — это уже наблюдение, а для
него есть поиск и вкладка «Все».

СОСТАВНОЙ КЛЮЧ. Строка не редактируется и не адресуется по одному
идентификатору, а ключ ``(user_id, conversation_id)`` бесплатно запрещает
закрепить один диалог дважды — включая двойное нажатие.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, PrimaryKeyConstraint, func
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ConversationPin(Base):
    """Один закреплённый диалог у одного человека."""

    __tablename__ = "conversation_pins"
    __table_args__ = (
        PrimaryKeyConstraint("user_id", "conversation_id", name="pk_conversation_pins"),
        # Сторона человека: «что у меня закреплено» читается на каждое открытие
        # списка, и без индекса это был бы полный проход по таблице.
        Index("ix_conversation_pins_user", "user_id", "pinned_at"),
        # Сторона диалога: удаление диалога проверяет ключ каскада, а стирание
        # истории канала удаляет их тысячами (0086).
        Index("ix_conversation_pins_conversation", "conversation_id"),
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PgUUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    pinned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
