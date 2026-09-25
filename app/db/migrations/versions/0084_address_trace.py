"""След решения и регион клиента (пакет 6.0а, 20.09): `trace JSONB NULL`,
`region TEXT NULL` в `client_address_candidates`.

`trace` — «почему» строки: чем разобрана и каким правилом решена картой, с
политикой правила, расстоянием, регионом и городом ответа карты, тенью
правила в `shadow` и прежним судом в `prev`. По нему аудит фильтрует
`--trace rule=…`, воронка считает `by_rule`, экран подписывает причину
степени (`RULE_LABEL`), откат `address-unfill --trace rule=X` находит свои
карточки. Вердикт по следу не считает никто — колонка описательная.

`region` — регион, названный клиентом словами, рядом с `district` и
`locality`: слой разбора, читает воркер как `Parsed.region`. Регион ответа
карты — в `trace.region`, не здесь.

Тип `trace` — `JSONBNullable` (урок 0083): Python `None` обязан лечь SQL NULL,
иначе фильтр «след есть/нет» в SQL считал бы `'null'::jsonb` следом.

Expand-safe: `ship.sh` мигрирует ДО подмены кода; старый воркер колонок не
читает и не пишет, у старых строк — NULL. Индекса нет: аудит идёт по
`detected_at`/`geo_status` с существующими индексами и фильтрует след уже в
выборке; откат читает строку по `address_candidate_id` карточки.

`0083` — снимок улик `geo_prev`: JSON `null` → SQL NULL.
"""

import sqlalchemy as sa
from alembic import op

from app.models.types import JSONBNullable

revision = "0084"
down_revision = "0083"
branch_labels = None
depends_on = None

_T = "client_address_candidates"


def upgrade() -> None:
    op.add_column(_T, sa.Column("trace", JSONBNullable, nullable=True))
    op.add_column(_T, sa.Column("region", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column(_T, "region")
    op.drop_column(_T, "trace")
