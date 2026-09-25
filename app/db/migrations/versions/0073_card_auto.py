"""Автоматика карточки: память о разъединении, подсказка у номера, исходная карточка диалога.

ПРОСЬБА ВЛАДЕЛЬЦА 12.09: карточка клиента «полностью автоматическая — сама
добавляет номера, сама объединяет». Три колонки под это:

`client_merge_vetoes` — «эту пару человек разъединил». Автоматическое
объединение по телефону обязано это помнить: без памяти пара, которую оператор
разъединил, удовлетворяла бы правилу снова и склеивалась бы обратно первым же
входящим (в журнале 15 разъединений — все они склеились бы за одну ночь).
Пара хранится упорядоченной (`a_id < b_id`), чтобы у одной пары была одна
строка независимо от того, кто был победителем.

`client_phone_candidates.hint` — слово рядом с номером в сообщении («жена»,
«мастер», «второй»). Только подпись на экране, никогда не решение: ошибка
словаря стоит неверной подписи, а не звонка не тому.

`conversations.origin_client_id` — карточка, под которую диалог встал бы, не
будь она объединена. `_upsert_client` ведёт диалог новой переписки к
победителю по цепочке `merged_into_id`; «Разъединить» возвращает диалоги по
снимку журнала, и диалоги, заведённые ПОСЛЕ объединения, оставались у
победителя — ошибочная склейка, замеченная через неделю, отменялась наполовину.
"""

import sqlalchemy as sa
from alembic import op

revision = "0073"
down_revision = "0072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_merge_vetoes",
        sa.Column(
            "a_id", sa.Uuid(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "b_id", sa.Uuid(), sa.ForeignKey("clients.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "created_by_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("merge_audit_id", sa.BigInteger(), nullable=True),
        sa.PrimaryKeyConstraint("a_id", "b_id", name="pk_client_merge_vetoes"),
        # Короткое имя: соглашение `ck_%(table_name)s_%(constraint_name)s`
        # достроит его само (двойная приставка — класс дефекта 0034).
        sa.CheckConstraint("a_id < b_id", name="ordered"),
    )
    op.create_index("ix_client_merge_vetoes_b", "client_merge_vetoes", ["b_id"])

    op.add_column("client_phone_candidates", sa.Column("hint", sa.Text(), nullable=True))

    op.add_column(
        "conversations",
        sa.Column(
            "origin_client_id",
            sa.Uuid(),
            sa.ForeignKey("clients.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_conversations_origin_client",
        "conversations",
        ["origin_client_id"],
        postgresql_where=sa.text("origin_client_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_origin_client", table_name="conversations")
    op.drop_column("conversations", "origin_client_id")
    op.drop_column("client_phone_candidates", "hint")
    op.drop_index("ix_client_merge_vetoes_b", table_name="client_merge_vetoes")
    op.drop_table("client_merge_vetoes")
