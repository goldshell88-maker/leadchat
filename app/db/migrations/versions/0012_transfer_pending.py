"""Передача диалога с подтверждением получателя.

ЧТО ЭТО ЗАКРЫВАЕТ
-----------------
Требование заказчика от 7 августа: «диалог не считается переданным, если
другой сотрудник его не принял».

Передача была мгновенной: нажал «Передать» — ответственность ушла, даже если
коллега обедал, был не в сети или просто не заметил. Клиент оставался без
ответа, а спросить было не с кого: передавший считал, что дело сделано,
принимающий — что ему ничего не давали.

Теперь пока `transfer_to_id` заполнен, диалог ЕЩЁ ЧИСЛИТСЯ ЗА ПЕРЕДАЮЩИМ:
`assignee_id` не меняется, и ответственность не может повиснуть в воздухе
между двумя людьми.

БЭКОФИЛЛА НЕТ, И ЭТО ГЛАВНОЕ СВОЙСТВО МИГРАЦИИ
-----------------------------------------------
Все три поля пустые. Диалоги, переданные СТАРЫМ способом, уже перешли к
получателям — они не «висят непринятыми», и трогать их нельзя: заполни мы
`transfer_to_id` задним числом, у половины команды диалоги вдруг вернулись бы
к прежним владельцам с требованием подтвердить то, что подтверждено месяц
назад.

ИНДЕКС ЧАСТИЧНЫЙ
----------------
Единственная выборка по этим полям — «предложения, провисевшие дольше
пятнадцати минут», и её делает планировщик раз в минуту. Строк с непустым
`transfer_to_id` в любой момент единицы, поэтому индекс покрывает только их:
полный индекс по четырёмстам тысячам строк ради десятка — плата за пустоту.

`ON DELETE SET NULL` с обеих сторон: увольнение сотрудника не должно ни
удалять диалог, ни оставлять предложение, адресованное несуществующему
человеку. Предложение просто снимается, и диалог остаётся у того, за кем
числится.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("transfer_to_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("transfer_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "conversations", sa.Column("transfer_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("conversations", sa.Column("transfer_comment", sa.Text(), nullable=True))

    op.create_foreign_key(
        "fk_conversations_transfer_to_id_users",
        "conversations",
        "users",
        ["transfer_to_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_conversations_transfer_by_id_users",
        "conversations",
        "users",
        ["transfer_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    # Планировщик ищет строго по непустому `transfer_to_id` — их единицы.
    op.create_index(
        "ix_conversations_transfer_pending",
        "conversations",
        ["transfer_at"],
        postgresql_where=sa.text("transfer_to_id IS NOT NULL"),
    )
    # Кому предложили — вторая выборка: «есть ли что-то, ждущее МЕНЯ».
    op.create_index(
        "ix_conversations_transfer_to",
        "conversations",
        ["transfer_to_id"],
        postgresql_where=sa.text("transfer_to_id IS NOT NULL"),
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_index("ix_conversations_transfer_to", table_name="conversations")
    op.drop_index("ix_conversations_transfer_pending", table_name="conversations")
    op.drop_constraint("fk_conversations_transfer_by_id_users", "conversations")
    op.drop_constraint("fk_conversations_transfer_to_id_users", "conversations")
    op.drop_column("conversations", "transfer_comment")
    op.drop_column("conversations", "transfer_at")
    op.drop_column("conversations", "transfer_by_id")
    op.drop_column("conversations", "transfer_to_id")
