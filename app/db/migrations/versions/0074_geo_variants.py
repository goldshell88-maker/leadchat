"""Варианты адреса от карты и вердикт «нашёлся в другом городе области».

ПРОСЬБА ВЛАДЕЛЬЦА 12.09 (три скриншота): «почему просит уточнить посёлок —
адрес спокойно находится»; «было бы неплохо, чтобы искал дальше, если не
может найти, и писал варианты, если не понимает точно, где клиент».

`geo_variants` — список того, что карта нашла, когда не смогла выбрать один
дом: `[{formatted, lat, lon, city}]`. Оператор выбирает вариант кнопкой, и
выбранный становится строкой карты у этого же кандидата.

`elsewhere` — новый вердикт: в городе объявления дома нет, а в области
области он есть (Хабаровск в объявлении, дом — в Комсомольске-на-Амуре).
Сам в карточку не пишется: показывается вариантом с названным городом.
"""

import sqlalchemy as sa
from alembic import op

from app.models.types import JSONB

revision = "0074"
down_revision = "0073"
branch_labels = None
depends_on = None

_T = "client_address_candidates"
_OLD = (
    "'pending','exact','ambiguous','not_found','house_missing','house_mismatch',"
    "'street_mismatch','settlement_mismatch','city_mismatch','region_mismatch',"
    "'other_city_in_text','no_city','blocked','error'"
)
_NEW = _OLD + ",'elsewhere'"


def upgrade() -> None:
    op.add_column(_T, sa.Column("geo_variants", JSONB, nullable=True))
    # Снимать — сырым SQL: `op.drop_constraint` прогоняет имя через соглашение
    # и ищет `ck_…_ck_…_e392`, которого нет (тот же класс, что в 0034).
    op.execute(f"ALTER TABLE {_T} DROP CONSTRAINT ck_client_address_candidates_geo_status")
    op.create_check_constraint("geo_status", _T, f"geo_status IS NULL OR geo_status IN ({_NEW})")


def downgrade() -> None:
    op.execute(f"UPDATE {_T} SET geo_status = 'not_found' WHERE geo_status = 'elsewhere'")
    op.execute(f"ALTER TABLE {_T} DROP CONSTRAINT ck_client_address_candidates_geo_status")
    op.create_check_constraint("geo_status", _T, f"geo_status IS NULL OR geo_status IN ({_OLD})")
    op.drop_column(_T, "geo_variants")
