"""Когда система сама спросила у клиента адрес (владелец 18.09).

Одна nullable-колонка, без индекса: читается только под замком строки
диалога (`workers/address_ask.py`), пишется один раз. ADD COLUMN с NULL —
операция на метаданных. Данных не пишет: старые диалоги остаются NULL, а
«оператор уже спрашивал» задача читает по ленте.
"""

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("address_asked_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "address_asked_at")  # dev-only (08 §1.4 правило 5)
