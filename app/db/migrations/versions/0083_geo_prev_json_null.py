"""Снимок улик: JSON `null` → SQL NULL (пакет 5, 20.09).

Колонка `geo_prev` (0082) описана обычным `JSONB`: присвоение Python `None`
писало `'null'::jsonb`, а обход починки различает снимок по `IS NOT NULL` —
восемь строк после `address-reparse` считались «со снимком», попадали в квоту
Яндекса и не ставились в очередь. Модель переведена на `JSONBNullable`
(`none_as_null=True`); здесь — накопленные значения. Пустой снимок и его
отсутствие — одно и то же: снимок читает только воркер, и `null` он трактует
как «снимка нет».

Downgrade не нужен: SQL NULL старый код читал так же.
"""

from alembic import op

revision = "0083"
down_revision = "0082"
branch_labels = None
depends_on = None

_T = "client_address_candidates"


def upgrade() -> None:
    op.execute(f"UPDATE {_T} SET geo_prev = NULL WHERE jsonb_typeof(geo_prev) = 'null'")


def downgrade() -> None:
    pass
