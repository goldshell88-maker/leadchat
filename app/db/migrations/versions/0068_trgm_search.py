"""pg_trgm и три GIN-индекса под поиск по мере набора (замер боя 06.09).

ЧТО БОЛЕЛО. Поиск в списке диалогов идёт на каждое нажатие клавиши, и каждое
нажатие стоило ДВА полных прохода по `clients`: `count(*)` для «Найдено: N»
и сама страница. EXPLAIN ANALYZE на боевой базе 06.09: count 43,7–102 мс,
страница 33–77 мс, в обоих «Seq Scan on clients, 62 264 строки» — по
`name ILIKE '%…%'` и по `phone LIKE '%…%'`. Это 9,7 % всех вызовов списка и
весь его горб 90–120 мс; около 5 000 таких запросов в день. Повторный замер
07.09 той же формы по имени: Parallel Seq Scan, 31 041 строка отброшена
фильтром на каждом из двух воркеров, 4 195 буферов, 36 мс.

ПОЧЕМУ B-TREE ЗДЕСЬ НЕ ПОМОГАЕТ, А GIN ПО ТРИГРАММАМ — ДА. Человек ищет
ПОДСТРОКУ: «ре» в «Андрей», хвост «34567» в «+79151234567». Шаблон с ведущим
`%` для B-tree бесполезен (индекс упорядочен по началу строки), и это давно
записано у `ix_clients_phone_active` в модели. pg_trgm разбирает строку на
триграммы и умеет отвечать на `LIKE '%…%'` из индекса: находит те десятки
карточек, где триграммы запроса есть, и только их перечитывает из таблицы.

⚠ ИНДЕКС ПО `lower(name)`, А НЕ ПО `name`, И УСЛОВИЕ ОБЯЗАНО СОВПАСТЬ С НИМ
БУКВАЛЬНО. Планировщик берёт индекс по выражению только для того же самого
выражения. Поэтому `_search_condition` в `services/conversations.py` пишет
`lower(clients.name) LIKE lower(:q)`, а не `name ILIKE :q`: второе — другой
оператор над другим выражением, и индекс для него мёртв. Смысл при этом тот
же: ILIKE в Postgres и есть «lower с обеих сторон» (локаль базы
`en_US.utf8`, `lower('ИВАН') = 'иван'` — проверено на бою). Совпадение
выражения стережёт `tests/unit/test_search_trgm_0609.py`.

⚠ ТРИГРАММ НЕТ У ЗАПРОСА КОРОЧЕ ТРЁХ ЗНАКОВ. На «ре» индекс не применится, и
планировщик честно останется на Seq Scan — ровно как сегодня, не хуже. По
телефону запрос всегда от пяти цифр (`PHONE_QUERY_MIN_DIGITS`).

`client_phone_candidates.phone` получает тот же индекс: ветка поиска по
кандидатам — тот же `LIKE '%хвост%'`, только таблица маленькая (6 414 строк,
1,6 мс, 137 буферов на бою). Здесь выигрыш небольшой; индекс нужен, чтобы три
ветки одного поиска были устроены одинаково и следующий читатель не искал,
почему две из трёх идут индексом, а третья перебором.

⚠ РАСШИРЕНИЕ. `pg_trgm` — trusted-расширение: его ставит владелец базы без
прав суперпользователя (на бою `leadchat` и так суперпользователь). На бою
его ещё нет (`pg_extension`: только plpgsql и citext; в
`pg_available_extensions` есть, версия 1.6). `IF NOT EXISTS` — чтобы
повторный прогон и стенд, где расширение уже стоит, не падали.

⚠ ПОЧЕМУ ОБЫЧНЫЙ CREATE INDEX, А НЕ CONCURRENTLY — тот же довод, что в 0067:
CONCURRENTLY не живёт внутри транзакции миграции, а строить есть что —
43 410 имён и 7 336 телефонов, это доли секунды на выкатке до подъёма
контейнеров.

SQLite (юнит-тесты) миграцию пропускает целиком: ни GIN, ни pg_trgm там нет,
а схему тестам даёт `Base.metadata.create_all`, где те же индексы из модели
ложатся обычными.

Откат снимает три индекса и НЕ трогает расширение: оно безвредно, а
`DROP EXTENSION` уронил бы любой объект, который к тому времени на него
обопрётся.
"""

import sqlalchemy as sa
from alembic import op

revision = "0068"
down_revision = "0067"
branch_labels = None
depends_on = None

#: (имя индекса, таблица). Отдельным списком, чтобы сторож сверял имена с
#: моделью: индекс, названный в модели иначе, при следующем сравнении схем
#: выглядел бы одновременно «лишним» и «недостающим».
TRGM_INDEXES: tuple[tuple[str, str], ...] = (
    ("ix_clients_name_trgm", "clients"),
    ("ix_clients_phone_trgm", "clients"),
    ("ix_client_phone_candidates_phone_trgm", "client_phone_candidates"),
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    # Метка `name_lower` — единственный способ сказать SQLAlchemy, к какому
    # выражению относится класс операторов: `postgresql_ops` ищет ключ по
    # `.key` элемента, а у голого `text("lower(name)")` ключа нет.
    op.create_index(
        "ix_clients_name_trgm",
        "clients",
        [sa.func.lower(sa.text("name")).label("name_lower")],
        postgresql_using="gin",
        postgresql_ops={"name_lower": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_clients_phone_trgm",
        "clients",
        ["phone"],
        postgresql_using="gin",
        postgresql_ops={"phone": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_client_phone_candidates_phone_trgm",
        "client_phone_candidates",
        ["phone"],
        postgresql_using="gin",
        postgresql_ops={"phone": "gin_trgm_ops"},
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for name, table in reversed(TRGM_INDEXES):
        op.drop_index(name, table_name=table)
