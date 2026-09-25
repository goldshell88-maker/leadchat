"""Адрес по частям: строка-кандидат «место» без улицы и дома.

Владелец 13.09: клиент пишет «Гатчинский р-н. Д. Малая Сосновка, массив
Южный» — улицы ещё нет, а перейти на карту хочется сразу; как назовёт
улицу — адрес обновляется. Такое сообщение становится строкой `kind='place'`:
`street`/`house` пустые, ключ `value` — строка места, район и массив —
своими колонками, точку даёт карта.
"""

import sqlalchemy as sa
from alembic import op

revision = "0076"
down_revision = "0075"
branch_labels = None
depends_on = None

_T = "client_address_candidates"


def upgrade() -> None:
    op.add_column(_T, sa.Column("kind", sa.Text(), nullable=False, server_default="house"))
    op.add_column(_T, sa.Column("district", sa.Text(), nullable=True))
    op.add_column(_T, sa.Column("area", sa.Text(), nullable=True))
    op.create_check_constraint("kind", _T, "kind IN ('house', 'place')")


def downgrade() -> None:
    op.execute(f"DELETE FROM {_T} WHERE kind = 'place'")
    op.execute(f"ALTER TABLE {_T} DROP CONSTRAINT ck_client_address_candidates_kind")
    op.drop_column(_T, "area")
    op.drop_column(_T, "district")
    op.drop_column(_T, "kind")
