"""Снимок улик пересуда (пакет 5, 20.09): `geo_prev JSONB NULL`.

Решение владельца 20.09 — «пересуд никогда не ухудшает вердикт с уликами».
Сброс по триггеру (`geo_repair`) и `address-recheck` кладут сюда улики
вердикта, который был у строки, а воркер, судящий заново без части карт
(доля Яндекса выбрана, DaData лежит), удерживает прежний вердикт, если новый
слабее (`geocode.keeps_previous`), и оставляет снимок с `missing=[…]` —
обход `kept_blind` вернёт строку, когда карты снова в деле. Суд полным набором
стирает снимок. Форма — `clients.СНИМОК_УЛИК`; читает только воркер.

Expand-safe: `ship.sh` мигрирует ДО подмены кода; старый воркер колонку не
читает и не пишет — строки, сброшенные в окне выкатки, судятся как сегодня.
Индекса нет: колонку читают по `id`; обход берёт `IS NOT NULL` вместе с
`geo_status` и `detected_at`, у которых индексы есть.

`0081` — версия судьи и флаг «без DaData».
"""

import sqlalchemy as sa
from alembic import op

from app.models.types import JSONB

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None

_T = "client_address_candidates"


def upgrade() -> None:
    op.add_column(_T, sa.Column("geo_prev", JSONB, nullable=True))


def downgrade() -> None:
    op.drop_column(_T, "geo_prev")
