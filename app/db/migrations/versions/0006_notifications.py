"""Центр уведомлений — таблицы ``notifications`` и ``notification_reads`` (14 §4).

Две таблицы, а не одна: уведомление может адресоваться роли («всем
администраторам»), а прочтение всегда персонально — если админов трое и один
нажал «прочитано», у двух других колокольчик гаснуть не должен. Разбор
альтернативы (materialize строки на каждого получателя при доставке) — в
шапке app/models/notification.py; коротко: она множит ночной шторм на число
админов и навсегда прячет ночную поломку от нанятого утром администратора.

Индексы ровно под четыре выборки, которые эта таблица обслуживает:

1. ``ix_notifications_recipient_unread`` — колокольчик: «мои непрочитанные,
   свежие сверху». Частичный (``WHERE read_at IS NULL``): прочитанное в него
   не попадает, а прочитанного со временем становится 99% таблицы.
2. ``ix_notifications_audience_created`` — лента рассылки по роли. Частичным
   по прочтению быть не может: прочтение рассылки живёт в
   ``notification_reads``, а не в колонке.
3. ``ix_notifications_dedup_key`` — подавление повторов (14 §4): поиск живой
   записи по ключу внутри окна важности. Без него каждая неудачная попытка
   планировщика — seq scan.
4. ``ix_notifications_expires_at`` — ежесуточная чистка по сроку (90 дней).

``ON DELETE CASCADE`` у обоих ключей ``notification_reads``: чистка удаляет
строку уведомления и не должна знать про отметки прочтения, а удаление
сотрудника не должно держать чужие уведомления живыми.

Про ``recipient_id → users.id ON DELETE CASCADE``: адресное уведомление
принадлежит человеку и умирает вместе с ним. Это отличается от ``audit_log``,
где FK нет намеренно (журнал переживает удаление пользователя, DESIGN §4.4) —
там хранится история действий, здесь личный ящик.

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-05
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # NULL — рассылка по роли (audience); ровно один из двух адресов.
        sa.Column("recipient_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("audience", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("severity", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=True),
        sa.Column("entity_id", sa.Text(), nullable=True),
        sa.Column("dedup_key", sa.Text(), nullable=True),
        sa.Column("repeat_count", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "severity IN ('critical','warning','info')", name="ck_notifications_severity"
        ),
        sa.CheckConstraint(
            "audience IS NULL OR audience IN ('admin','head')", name="ck_notifications_audience"
        ),
        # Уведомление без адреса не увидит никто, с двумя адресами — покажется
        # дважды (лично и по роли). Ровно один.
        sa.CheckConstraint(
            "(recipient_id IS NOT NULL) <> (audience IS NOT NULL)",
            name="ck_notifications_addressing",
        ),
        sa.CheckConstraint("repeat_count >= 1", name="ck_notifications_repeat_count"),
        sa.ForeignKeyConstraint(
            ["recipient_id"],
            ["users.id"],
            name="fk_notifications_recipient_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_notifications"),
    )
    op.execute(
        "CREATE INDEX ix_notifications_recipient_unread "
        "ON notifications (recipient_id, created_at DESC) WHERE read_at IS NULL"
    )
    op.execute(
        "CREATE INDEX ix_notifications_audience_created "
        "ON notifications (audience, created_at DESC) WHERE audience IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX ix_notifications_dedup_key "
        "ON notifications (dedup_key, last_seen_at DESC) WHERE dedup_key IS NOT NULL"
    )
    op.create_index("ix_notifications_expires_at", "notifications", ["expires_at"])

    op.create_table(
        "notification_reads",
        sa.Column("notification_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "read_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["notification_id"],
            ["notifications.id"],
            name="fk_notification_reads_notification_id_notifications",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_notification_reads_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("notification_id", "user_id", name="pk_notification_reads"),
    )
    # «Что прочитал этот человек» — вторая половина предиката непрочитанного
    # (первая идёт по первичному ключу).
    op.create_index("ix_notification_reads_user_id", "notification_reads", ["user_id"])


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_index("ix_notification_reads_user_id", table_name="notification_reads")
    op.drop_table("notification_reads")
    op.drop_index("ix_notifications_expires_at", table_name="notifications")
    op.execute("DROP INDEX IF EXISTS ix_notifications_dedup_key")
    op.execute("DROP INDEX IF EXISTS ix_notifications_audience_created")
    op.execute("DROP INDEX IF EXISTS ix_notifications_recipient_unread")
    op.drop_table("notifications")
