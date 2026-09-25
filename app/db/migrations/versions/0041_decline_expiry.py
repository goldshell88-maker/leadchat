"""Отказ от диалога живёт три минуты, а не вечно (требование заказчика от 13 августа).

«При нажатии „Отклонить“ диалог возвращался в течение 3 минут назад во входящие, и
работало под каждого пользователя отдельно: у того, кто нажал, вернётся в очередь; тот,
кто не нажимал, так и останется».

ЧТО ДЕЛАЕТ ЭТА МИГРАЦИЯ: заводит таблицу `conversation_declines`, где у каждого отказа
есть момент нажатия. Массив `conversations.declined_by` НЕ ТРОГАЕТСЯ.

ПОЧЕМУ ТОЛЬКО ДОБАВЛЕНИЕ. Регламент выкатки (RUNBOOK-DEPLOY §13, правила 1 и 2): миграция
накатывается ДО обновления кода, старые контейнеры в этот момент ещё работают и пишут в
`declined_by` на каждом `enter_queue`. Снеси колонку здесь — и автоматический откат на
предыдущий тег (шаг 6 деплоя) перестанет работать навсегда. Удаление колонки — отдельной
миграцией следующего релиза, и только если она вообще станет не нужна: сейчас по ней
считается эскалация «отказались ВСЕ» (см. app/models/conversation_decline.py).

ПОЧЕМУ БЕЗ ПЕРЕНОСА ДАННЫХ. Переносить нечего, и это не экономия, а смысл правки: отказ
теперь живёт три минуты, а всё, что лежит в `declined_by` на момент выкатки, старше этого
срока по определению. Такие отказы обязаны считаться истёкшими — что и произойдёт само,
если ничего не переносить. Плюс правило 3 того же регламента прямо запрещает backfill в
миграции. Отдельная неприятность, которой мы так избегаем: журнал аудита помнит и
отменённые отказы, и те, что система намеренно стирала при возврате диалога в очередь, —
перенос из него вернул бы людям спрятанные строки.

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_declines",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "declined_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_declines_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_conversation_declines_user", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("conversation_id", "user_id", name="pk_conversation_declines"),
    )
    # Сторона времени: фоновая задача раз в минуту ищет только что истёкшие отказы, чтобы
    # толкнуть диалог обратно в очередь отказавшемуся. Без индекса — полный проход на
    # каждом такте планировщика.
    op.create_index("ix_conversation_declines_expiry", "conversation_declines", ["declined_at"])


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_index("ix_conversation_declines_expiry", table_name="conversation_declines")
    op.drop_table("conversation_declines")
