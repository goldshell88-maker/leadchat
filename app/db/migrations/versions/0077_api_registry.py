"""Монитор внешних сервисов: свои записи владельца (16.09).

Встроенные сервисы монитор считает из кода, окружения и Redis; таблица —
только для тех, что владелец завёл руками: имя, адрес, лимиты, заметка.
"""

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0076"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "api_registry",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("purpose", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("docs_url", sa.Text(), nullable=True),
        sa.Column("daily_limit", sa.Integer(), nullable=True),
        sa.Column("monthly_limit", sa.Integer(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("key", name="uq_api_registry_key"),
    )


def downgrade() -> None:
    op.drop_table("api_registry")
