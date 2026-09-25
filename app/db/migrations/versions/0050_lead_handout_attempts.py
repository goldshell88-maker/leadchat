"""Счётчик попыток выдачи лида: сколько раз отдавали и когда пора перестать.

Находка аудита L-005. Неподтверждённый лид отдавался расширению заново каждый
час БЕСКОНЕЧНО: счётчика не было, а `handed_at` перезаписывался — история
попыток стиралась, и в журнале выдач это выглядело одной строкой. Понять по
базе, что заявка уже сутки не доезжает, было нечем.

Колонка добавляется только вверх (expand, docs/05 §5.3): `server_default='1'`
закрывает уже существующие строки — на бою их сейчас ноль, но правило одно для
всех сред.
"""

import sqlalchemy as sa
from alembic import op

revision = "0050"
down_revision = "0049"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "lead_handouts",
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    # Вниз-миграции на проде запрещены (docs/05 §5.3); здесь — ради полноты
    # локального стека и обратимости в разработке.
    op.drop_column("lead_handouts", "attempts")
