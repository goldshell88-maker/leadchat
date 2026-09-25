"""Повторная привязка адреса без человека (N13, 19.09): два факта о вердикте.

`geo_verdict_version` — версия судьи карты (`geocode.VERDICT_VERSION`),
вынесшего `geo_status`; NULL — судили до колонки или вердикт сброшен.
`geo_without_dadata` — вердикт вынесен без ответа DaData (выключена, без
ключа, потолок, 403, сеть). Починка (`geo_repair`) пересматривает строки,
судимые не текущей версией, и строки с флагом — когда DaData снова в деле;
дедуп «уже повторяли по этому триггеру» — на строке, а не в Redis.

Обе колонки expand-safe: ship.sh делает `alembic upgrade head` ДО подмены
кода; старый воркер в окне выкатки пишет NULL/false — такие строки первый
заход починки пересмотрит один раз.

`0080` — снятие слага «вся Россия» из колонки города (федеральные объявления).
"""

import sqlalchemy as sa
from alembic import op

revision = "0081"
down_revision = "0080"
branch_labels = None
depends_on = None

_T = "client_address_candidates"


def upgrade() -> None:
    op.add_column(_T, sa.Column("geo_verdict_version", sa.SmallInteger(), nullable=True))
    op.add_column(
        _T,
        sa.Column("geo_without_dadata", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column(_T, "geo_without_dadata")
    op.drop_column(_T, "geo_verdict_version")
