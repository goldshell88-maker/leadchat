"""Кого позвали в диалог, не отдавая его (docs/19).

ЧТО ЭТО ЗАКРЫВАЕТ
-----------------
Была только полная передача — «отдать целиком». Часто нужно другое: позвать
мастера посмотреть, оставшись ответственным. Мастер по холодильникам скажет,
чинится ли эта модель; старший подскажет, что делать со скандалом. Ни в том,
ни в другом случае отдавать диалог не нужно, и спрос остаётся на том, кто
его вёл.

ЧЕМ ПРИГЛАШЕНИЕ ОТЛИЧАЕТСЯ ОТ ОБЩЕЙ ВИДИМОСТИ
----------------------------------------------
Вкладка «Все» и так показывает любой диалог любому оператору. Но «может
открыть, если знает адрес» и «увидит» — разные вещи при четырёхстах тысячах
диалогов. Приглашение добавляет ровно две: коллега узнаёт (уведомление с
причиной) и диалог попадает в его «Мои» — список, который он смотрит каждый
день.

СОСТАВНОЙ КЛЮЧ, БЕЗ СУРРОГАТНОГО ID
------------------------------------
Строка не редактируется и не адресуется по одному идентификатору, а ключ
`(conversation_id, user_id)` бесплатно запрещает позвать одного человека
дважды — включая гонку двух операторов, нажавших одновременно.

КАСКАДЫ РАЗНЫЕ, И ЭТО НЕ СЛУЧАЙНОСТЬ
-------------------------------------
`conversation_id` и `user_id` — CASCADE: удалили диалог или сотрудника,
участие теряет смысл. `invited_by_id` — SET NULL: увольнение пригласившего
не должно выкидывать позванного из диалога, в котором он работает.

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_participants",
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("invited_by_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "invited_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_conversation_participants_conversation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name="fk_conversation_participants_user",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["invited_by_id"],
            ["users.id"],
            name="fk_conversation_participants_invited_by",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("conversation_id", "user_id", name="pk_conversation_participants"),
    )
    # Сторона человека: «куда меня позвали» читается на каждое открытие «Моих».
    op.create_index(
        "ix_conversation_participants_user",
        "conversation_participants",
        ["user_id", "conversation_id"],
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_index("ix_conversation_participants_user", table_name="conversation_participants")
    op.drop_table("conversation_participants")
