"""Воронка адресов: снимок недели (18.09, автопривязка адреса без человека).

Раз в неделю (понедельник 04:40 МСК) планировщик считает, у скольких
диалогов с адресом в переписке адрес лёг в карточку и с какой степенью, и
кладёт счётчики строкой на неделю. Сравнение с прошлой неделей и история в
мониторе обязаны пережить перезапуск — поэтому таблица, а не Redis.

`0078` — колонка `conversations.address_asked_at` (участок вопроса клиенту).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0079"
down_revision = "0078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "address_funnel_weekly",
        sa.Column("week_start", sa.Date(), primary_key=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("counts", postgresql.JSONB(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("address_funnel_weekly")
