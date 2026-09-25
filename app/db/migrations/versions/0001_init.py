"""Initial schema — everything from DESIGN §4.4.

Hand-written parts (08 §1.4, autogenerate rule 3): ``PARTITION BY RANGE``
for messages, the ``search`` generated tsvector column, the partial unique
idempotency index, GIN indexes. Monthly partitions of ``messages`` are NOT
created here — the scheduler DDL-job owns them (08 §6.3).

Revision ID: 0001
Revises:
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("full_name", sa.Text(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "role IN ('admin','head','manager','observer')", name="ck_users_users_role"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_users"),
        sa.UniqueConstraint("email", name="uq_users_email"),
    )

    op.create_table(
        "bots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "schedule",
            postgresql.JSONB(),
            server_default=sa.text("'{\"always\": true}'::jsonb"),
            nullable=False,
        ),
        sa.Column("scenario", postgresql.JSONB(), nullable=False),
        sa.Column("knowledge_base", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_bots"),
    )

    op.create_table(
        "avito_accounts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("avito_user_id", sa.BigInteger(), nullable=False),
        sa.Column("access_token_enc", postgresql.BYTEA(), nullable=False),
        sa.Column("refresh_token_enc", postgresql.BYTEA(), nullable=False),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("webhook_secret", sa.Text(), nullable=False),
        sa.Column("bot_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["bot_id"], ["bots.id"], name="fk_avito_accounts_bot_id_bots"),
        sa.PrimaryKeyConstraint("id", name="pk_avito_accounts"),
        sa.UniqueConstraint("avito_user_id", name="uq_avito_accounts_avito_user_id"),
    )

    op.create_table(
        "clients",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), server_default=sa.text("'avito'"), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=True),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("avito_rating", sa.Numeric(), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_clients"),
        sa.UniqueConstraint("channel", "external_id", name="uq_clients_channel_external_id"),
    )

    op.create_table(
        "conversations",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), server_default=sa.text("'avito'"), nullable=False),
        sa.Column("external_chat_id", sa.Text(), nullable=False),
        sa.Column("account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("client_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("assignee_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'new'"), nullable=False),
        sa.Column("bot_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "bot_vars", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column(
            "tags",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column("item_title", sa.Text(), nullable=True),
        sa.Column("item_url", sa.Text(), nullable=True),
        sa.Column("item_price", sa.Text(), nullable=True),
        sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["avito_accounts.id"],
            name="fk_conversations_account_id_avito_accounts",
        ),
        sa.ForeignKeyConstraint(
            ["client_id"], ["clients.id"], name="fk_conversations_client_id_clients"
        ),
        sa.ForeignKeyConstraint(
            ["assignee_id"], ["users.id"], name="fk_conversations_assignee_id_users"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_conversations"),
        sa.UniqueConstraint(
            "channel", "external_chat_id", name="uq_conversations_channel_external_chat_id"
        ),
    )
    op.create_index("ix_conversations_tags", "conversations", ["tags"], postgresql_using="gin")

    op.create_table(
        "templates",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("owner_id", postgresql.UUID(as_uuid=True), nullable=True),  # NULL = shared
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("folder", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["owner_id"], ["users.id"], name="fk_templates_owner_id_users"),
        sa.PrimaryKeyConstraint("id", name="pk_templates"),
    )

    op.create_table(
        "audit_log",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("entity", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_audit_log"),
    )

    # messages: partitioned + generated tsvector column — raw DDL by design
    # (autogenerate cannot express either; 08 §1.4 rule 3).
    op.execute(
        """
        CREATE TABLE messages (
          id uuid NOT NULL DEFAULT gen_random_uuid(),
          conversation_id uuid NOT NULL,
          external_message_id text,
          direction text NOT NULL,
          sender_type text NOT NULL,
          sender_user_id uuid,
          body text,
          attachments jsonb NOT NULL DEFAULT '[]'::jsonb,
          delivery_status text NOT NULL DEFAULT 'delivered',
          created_at timestamptz NOT NULL DEFAULT now(),
          search tsvector GENERATED ALWAYS AS
            (to_tsvector('russian', coalesce(body, ''))) STORED,
          CONSTRAINT pk_messages PRIMARY KEY (id, created_at),
          CONSTRAINT fk_messages_conversation_id_conversations
            FOREIGN KEY (conversation_id) REFERENCES conversations (id),
          CONSTRAINT fk_messages_sender_user_id_users
            FOREIGN KEY (sender_user_id) REFERENCES users (id)
        ) PARTITION BY RANGE (created_at)
        """
    )
    # Webhook idempotency (partial unique — second echelon, 08 §8.2)
    op.execute(
        """
        CREATE UNIQUE INDEX uq_messages_conversation_external_created
          ON messages (conversation_id, external_message_id, created_at)
          WHERE external_message_id IS NOT NULL
        """
    )
    op.execute("CREATE INDEX ix_messages_search ON messages USING gin (search)")


def downgrade() -> None:
    # dev-only (08 §1.4 rule 5: no downgrades in prod)
    op.drop_table("messages")
    op.drop_table("audit_log")
    op.drop_table("templates")
    op.drop_index("ix_conversations_tags", table_name="conversations")
    op.drop_table("conversations")
    op.drop_table("clients")
    op.drop_table("avito_accounts")
    op.drop_table("bots")
    op.drop_table("users")
