"""Три недостающих индекса, каждый найден замером на боевых данных (05.09).

Общее у всех трёх: без них выполняется ПОЛНЫЙ ПЕРЕБОР таблицы там, где путь
горячий. Это не догадка по коду — под каждым лежит EXPLAIN (ANALYZE, BUFFERS)
на боевой базе.

1. ``notifications.recipient_id`` — счётчик колокольчика.

   Замер: «Seq Scan on notifications … Rows Removed by Filter: 31 582,
   Buffers: shared hit=1867, Execution Time: 7,830 ms». Зовут его около 25 раз
   в минуту, дельта `pg_stat_user_tables` за 420 секунд: seq_scan +185,
   seq_tup_read +5 849 346 — то есть 790 тысяч лишних прочитанных строк в
   минуту.

   ⚠ ПОЧЕМУ ИНДЕКС ПО ОДНОМУ ПОЛЮ ВЫЛЕЧИТ ЗАПРОС С `OR`. Право видеть — это
   «своё ЛИБО рассылка на мою роль»: `recipient_id = я OR audience IN (…)`.
   У второй половины индекс есть (`ix_notifications_audience_created`), у
   первой не было ни одного — и `OR` c одной неиндексированной стороной
   всегда даёт полный перебор, сколько бы индексов ни висело на другой. С
   обеими сторонами планировщик собирает BitmapOr.

   Форма повторяет соседний индекс рассылки: `(поле, created_at)`,
   частичный. Частичность здесь не украшение — адресных уведомлений и
   рассылок примерно поровну, и половина строк в индекс просто не попадает.

2 и 3. Дочерние стороны внешних ключей: ``clients.phone_conversation_id``
   (ON DELETE SET NULL) и ``client_phone_candidates.conversation_id``
   (ON DELETE CASCADE).

   PostgreSQL не заводит индексы под внешние ключи сам, а проверять их при
   удалении родителя обязан. Замер той формы, которую выполняет проверка:
   «Seq Scan on clients … Rows Removed by Filter: 61 364, Buffers: shared
   hit=1330, Execution Time: 11,137 ms». За 32 суток на `conversations`
   11 738 удалений — и каждое стоило полного прохода по `clients`.

   Цена в живом виде: чистка истории канала на 2 700 диалогов — это около
   тридцати секунд сплошных полных проходов ПОД БЛОКИРОВКАМИ, во время
   которых ждут все. То же умножается на каждое удаление сотрудника.

⚠ ПОЧЕМУ ОБЫЧНЫЙ CREATE INDEX, А НЕ CONCURRENTLY. CONCURRENTLY не работает
внутри транзакции, а миграции идут транзакцией — пришлось бы городить
autocommit. Платить за это нечем: `notifications` — 31 тысяча строк,
`clients` — 61 тысяча, `client_phone_candidates` — 6 тысяч. Построение такого
индекса измеряется десятками миллисекунд, и происходит оно на выкатке, до
подъёма новых контейнеров. CONCURRENTLY понадобится на `messages`, где строк
352 тысячи в 28 партициях, — но сюда ни один из трёх не идёт.

Откат снимает все три: они чисто ускоряющие, ни одно правило целостности на
них не держится.
"""

import sqlalchemy as sa
from alembic import op

revision = "0067"
down_revision = "0066"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_notifications_recipient_created",
        "notifications",
        ["recipient_id", "created_at"],
        postgresql_where=sa.text("recipient_id IS NOT NULL"),
    )
    op.create_index(
        "ix_clients_phone_conversation",
        "clients",
        ["phone_conversation_id"],
        postgresql_where=sa.text("phone_conversation_id IS NOT NULL"),
    )
    # Здесь частичности нет намеренно: колонка объявлена NOT NULL, и условие
    # «IS NOT NULL» не отсекло бы ни одной строки — только сбило бы с толку
    # следующего читателя, который стал бы искать в нём смысл.
    op.create_index(
        "ix_client_phone_candidates_conversation",
        "client_phone_candidates",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_client_phone_candidates_conversation", table_name="client_phone_candidates")
    op.drop_index("ix_clients_phone_conversation", table_name="clients")
    op.drop_index("ix_notifications_recipient_created", table_name="notifications")
