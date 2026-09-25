"""Закреплённые диалоги — личные (требование заказчика от 7 августа).

«Каждый может для себя лично закреплять свои взятые диалоги».

ПОЧЕМУ ЛИЧНОЕ, А НЕ ОБЩЕЕ
--------------------------
Общее закрепление на тринадцать операторов и девять каналов превращается в
свалку за неделю: закрепляют все, снимает никто, и наверху списка висит
десяток чужих диалогов. Плюс спор «зачем ты открепил мой», у которого нет
правильного ответа. Закрепление — отметка «я к этому вернусь», а «я» у
каждого своё.

СОСТАВНОЙ КЛЮЧ, БЕЗ СУРРОГАТНОГО ID
------------------------------------
Строка не редактируется и не адресуется по одному идентификатору, а ключ
`(user_id, conversation_id)` бесплатно запрещает закрепить один диалог
дважды — включая двойное нажатие.

КАСКАДЫ ОБА CASCADE
-------------------
Удалили сотрудника или диалог — закрепление теряет смысл в обе стороны.
Здесь нет случая, как у приглашений, где пригласивший может уйти, а
приглашение обязано остаться.

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_pins",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "pinned_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_conversation_pins_user", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_pins_conversation",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "conversation_id", name="pk_conversation_pins"),
    )
    # Сторона человека: «что у меня закреплено» читается на каждое открытие
    # списка, и без индекса это полный проход по таблице.
    op.create_index("ix_conversation_pins_user", "conversation_pins", ["user_id", "pinned_at"])


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_index("ix_conversation_pins_user", table_name="conversation_pins")
    op.drop_table("conversation_pins")
