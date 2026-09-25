"""Cross-dialect column types.

Production schema is PostgreSQL (DESIGN §4.4: citext, jsonb, text[]);
unit tests run the same models on SQLite (07 §1.1), so each PG-specific
type gets a portable variant.
"""

from sqlalchemy import JSON, Text
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import TypeEngine

# citext on PG (case-insensitive unique emails), plain TEXT elsewhere —
# application code normalizes emails to lowercase on lookup either way.
CIText: TypeEngine[str] = Text().with_variant(postgresql.CITEXT(), "postgresql")

# jsonb on PG, generic JSON elsewhere.
JSONB = JSON().with_variant(postgresql.JSONB(), "postgresql")

# То же, но Python `None` пишется как SQL NULL, а не как JSON `null`. У обычного
# `JSON` присвоение `None` даёт значение `'null'::jsonb`, и `IS NOT NULL`
# по нему истинно: снимок улик `geo_prev` после `сброс_вердикта(улики=None)`
# считался существующим, строка попадала в квоту Яндекса и не ставилась в
# очередь (бой 20.09, восемь строк). Для колонок, которые читаются SQL-фильтром
# «есть/нет», нужен этот тип.
JSONBNullable = JSON(none_as_null=True).with_variant(
    postgresql.JSONB(none_as_null=True), "postgresql"
)

# text[] on PG, JSON array elsewhere (unit tests only touch it via ORM).
TextArray = postgresql.ARRAY(Text()).with_variant(JSON(), "sqlite")
