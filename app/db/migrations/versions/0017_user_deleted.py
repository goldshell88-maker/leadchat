"""Удаление сотрудника — отметкой, а не DELETE (требование от 7 августа).

ЧТО ЗАКРЫВАЕТ
-------------
Сотрудника можно было только отключить. За год список зарастает уволенными,
и найти работающего становится задачей. Заказчик просит удаление.

ПОЧЕМУ ОТМЕТКА, А НЕ НАСТОЯЩЕЕ УДАЛЕНИЕ
----------------------------------------
Настоящее удаление снесло бы вместе с человеком его сообщения — а это
переписка с клиентами. Годовой отчёт стал бы анонимным, а в старых диалогах
вместо имени появилось бы «Сотрудник»: клиент видит подпись оператора, и она
обязана остаться правдой и через год.

ЧЕМ ОТЛИЧАЕТСЯ ОТ «ОТКЛЮЧЁН»
-----------------------------
Отключённый — временное состояние: отпуск, разбираемся с доступом, вернётся.
Он виден под галочкой «показывать отключённых» и включается одной кнопкой.
Удалённый не виден нигде и не включается: он ушёл.

ЧАСТИЧНЫЙ ИНДЕКС
----------------
Списки сотрудников читают «не удалённых», и это подавляющее большинство
строк. Индекс — по `deleted_at IS NOT NULL`, то есть по редкому случаю:
искать по нему приходится только при разборе «а куда делся Иванов».

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index(
        "ix_users_deleted",
        "users",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_index("ix_users_deleted", table_name="users")
    op.drop_column("users", "deleted_at")
