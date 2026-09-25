"""Индекс под ленту уведомлений: сортировка перестаёт перебирать таблицу.

⚠ НАЙДЕНО ЗАМЕРОМ БОЯ 02.09, И ЗАМЕР ЖЕ ПОКАЗАЛ ЦЕНУ.

Журнал уведомлений отдаётся с `ORDER BY last_seen_at DESC, id DESC LIMIT 30`, а
индекса под этот порядок не было. План на боевых данных:

    Seq Scan on notifications  (rows=23 773)
      -> Sort  (Sort Method: top-N heapsort, Memory: 61kB)
    Execution Time: 7.529 ms

То есть на каждое открытие колокольчика читалась вся таблица, чтобы отдать
тридцать строк. С индексом тот же запрос:

    Index Scan using ix_notifications_feed  (actual rows=30)
    Execution Time: 0.129 ms

В 58 раз. Проверено прямо на боевой базе: индекс создан пробным
`CREATE INDEX CONCURRENTLY`, замерен и оставлен под этим именем — поэтому
миграция написана идемпотентно и на проде окажется холостой.

⚠ NULLS LAST В ИНДЕКСЕ ОБЯЗАТЕЛЕН. У `last_seen_at` бывает NULL, и порядок
сортировки в запросе — `DESC NULLS LAST`. Индекс с другим размещением NULL'ов
планировщик для этой сортировки не возьмёт: он окажется в базе, будет платиться
на каждой записи и не пригодится ни разу.

⚠ ПОЧЕМУ БЕЗ CONCURRENTLY ЗДЕСЬ. Alembic выполняет миграцию в транзакции, а
`CREATE INDEX CONCURRENTLY` в транзакции запрещён. Таблица маленькая (23 773
строки, индекс 960 кБ) — обычное создание занимает доли секунды. На проде
индекс уже есть, и `IF NOT EXISTS` делает шаг холостым.
"""

from alembic import op

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_notifications_feed "
        "ON notifications (last_seen_at DESC NULLS LAST, id DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_notifications_feed")
