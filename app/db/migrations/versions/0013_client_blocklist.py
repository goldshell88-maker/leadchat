"""Чёрный список клиентов (аудит 7 августа, docs/19).

ЧТО ЭТО ЗАКРЫВАЕТ
-----------------
Есть люди, которые пишут каждый день и клиентами не являются: спам, «а
сколько стоит» без намерения, скандалист, с которым решили не работать. Их
обращения были неотличимы от новых: диалог вставал в очередь, звенел у
тринадцати человек, кто-то открывал, тратил минуту и закрывал. Каждый день.

ПОМЕТКА НЕ ТЕРЯЕТ СООБЩЕНИЯ
---------------------------
Сообщения помеченного приходят, сохраняются, диалог виден и находится
поиском. Пометка убирает ровно одно — требование внимания: диалог не встаёт
в очередь и не звенит.

Это принципиально. «Удалить» или «не принимать» означало бы потерянные
сообщения, а среди них однажды окажется настоящий заказ от человека,
который в прошлый раз был не в духе. Чёрный список должен экономить время
команды, а не создавать риск потерять деньги.

БЕЗ БЭКОФИЛЛА
-------------
Все три поля пустые: помеченных клиентов до этой миграции не было и быть не
могло.

ИНДЕКС ЧАСТИЧНЫЙ
----------------
Помеченных единицы из сотен тысяч, а спрашивают о них двумя способами:
«покажи чёрный список» (экран) и «помечен ли этот» (на каждом входящем).
Второе идёт по первичному ключу клиента и индекса не требует; первое —
редкий экран, но по полной таблице был бы перебор.

``ON DELETE SET NULL`` для того, кто пометил: увольнение сотрудника не
должно снимать пометку — она про клиента, а не про того, кто её поставил.

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("blocked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "clients", sa.Column("blocked_by_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("clients", sa.Column("blocked_reason", sa.Text(), nullable=True))
    op.create_foreign_key(
        "fk_clients_blocked_by_id_users",
        "clients",
        "users",
        ["blocked_by_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_clients_blocked",
        "clients",
        ["blocked_at"],
        postgresql_where=sa.text("blocked_at IS NOT NULL"),
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_index("ix_clients_blocked", table_name="clients")
    op.drop_constraint("fk_clients_blocked_by_id_users", "clients")
    op.drop_column("clients", "blocked_reason")
    op.drop_column("clients", "blocked_by_id")
    op.drop_column("clients", "blocked_at")
