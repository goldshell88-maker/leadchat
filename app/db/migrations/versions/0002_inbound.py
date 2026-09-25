"""Sprint 2 — inbound pipeline.

* ``webhook_raw_log`` (08 §2.6 + sprint-2 fields received_at/processed/error);
* ``conversations.unread_count`` (owner decision: a column is enough for now);
* indexes backing GET /conversations (01 §5.1: fixed sort by last_message_at
  DESC, tab filters by status/assignee, ``updated_since``) and the message
  feed keyset (conversation_id, created_at);
* initial monthly partitions of ``messages`` (current + next month) so the
  system can write messages before the scheduler's first ensure_partitions
  run (08 §6.3 — «первые партиции создаёт миграция»).

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-05
"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _month_bounds(year: int, month: int) -> tuple[str, str, str]:
    start = f"{year:04d}-{month:02d}-01"
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    end = f"{ny:04d}-{nm:02d}-01"
    return f"messages_y{year:04d}m{month:02d}", start, end


def upgrade() -> None:
    # --- webhook_raw_log (08 §2.6) ---
    op.create_table(
        "webhook_raw_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=True),  # no FK by design
        sa.Column("stream_id", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column(
            "received_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("processed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_webhook_raw_log"),
    )
    op.create_index("uq_webhook_raw_stream", "webhook_raw_log", ["stream_id"], unique=True)
    op.create_index("ix_webhook_raw_received", "webhook_raw_log", ["received_at"])
    op.create_index(
        "ix_webhook_raw_account_received", "webhook_raw_log", ["account_id", "received_at"]
    )

    # --- conversations: unread counter + list indexes (01 §5.1) ---
    op.add_column(
        "conversations",
        sa.Column("unread_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    # Fixed sort: unread first → «негатив» on top → last_message_at DESC.
    op.create_index(
        "ix_conversations_last_message_at",
        "conversations",
        [sa.text("last_message_at DESC NULLS LAST")],
    )
    op.create_index(
        "ix_conversations_status_last_message",
        "conversations",
        ["status", sa.text("last_message_at DESC NULLS LAST")],
    )
    op.create_index("ix_conversations_assignee_status", "conversations", ["assignee_id", "status"])
    op.create_index("ix_conversations_updated_at", "conversations", ["updated_at"])
    op.create_index("ix_conversations_account_id", "conversations", ["account_id"])

    # --- messages: keyset feed index (01 §1.4 cursor by created_at,id) ---
    op.create_index(
        "ix_messages_conversation_created", "messages", ["conversation_id", "created_at"]
    )

    # --- initial partitions: current + next month (idempotent) ---
    today = datetime.now(UTC).date()
    year, month = today.year, today.month
    for _ in range(2):
        name, start, end = _month_bounds(year, month)
        op.execute(
            f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF messages "
            f"FOR VALUES FROM ('{start}') TO ('{end}')"
        )
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)


def downgrade() -> None:
    # dev-only (08 §1.4 rule 5); partitions are left in place on purpose —
    # they may already hold data and are owned by the scheduler job.
    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_index("ix_conversations_account_id", table_name="conversations")
    op.drop_index("ix_conversations_updated_at", table_name="conversations")
    op.drop_index("ix_conversations_assignee_status", table_name="conversations")
    op.drop_index("ix_conversations_status_last_message", table_name="conversations")
    op.drop_index("ix_conversations_last_message_at", table_name="conversations")
    op.drop_column("conversations", "unread_count")
    op.drop_index("ix_webhook_raw_account_received", table_name="webhook_raw_log")
    op.drop_index("ix_webhook_raw_received", table_name="webhook_raw_log")
    op.drop_index("uq_webhook_raw_stream", table_name="webhook_raw_log")
    op.drop_table("webhook_raw_log")
