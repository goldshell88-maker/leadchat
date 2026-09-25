"""Три индекса для блока «Возможно, это тот же человек» — он читал таблицу целиком.

ЧТО ИЗМЕРЕНО НА БОЮ (аудит 22.08, шаг 4). У таблицы `clients` было три индекса:
первичный ключ, `UNIQUE(channel, external_id)` и крошечный `ix_clients_blocked`.
А блок сквозного клиента ищет по трём другим полям — и каждый поиск шёл
последовательным чтением ВСЕЙ таблицы:

    phone       → Seq Scan, 17 518 строк отброшено фильтром, 10.8 мс
    external_id → Seq Scan, 17 518 строк отброшено фильтром, 10.9 мс
    name        → Seq Scan, 17 518 строк отброшено фильтром,  4.9 мс

ИНДЕКС ПО ИМЕНИ — ПО ВЫРАЖЕНИЮ `lower(name)`, и это не придирка. Код ищет
однофамильцев через `sa.func.lower(Client.name) == …`; с индексом по голому
`name` запрос остаётся последовательным чтением (проверено на боевой базе в
транзакции с откатом: 17 700 строк отброшено, 4.7 мс). С индексом по выражению —
Bitmap Index Scan, 0.3 мс. Индекс по колонке занимал бы диск, замедлял записи и
не использовался бы никогда.

Составной `UNIQUE(channel, external_id)` тут не помогает и не должен: поиск
однофамильцев по `external_id` идёт НАМЕРЕННО без канала — он ищет того же
человека на другом канале, и добавлять `channel` в условие значило бы сломать
смысл. Индекс нужен по одному полю.

Итог по счётчикам базы: 88 450 последовательных чтений `clients` и 146 401 343
прочитанных строки. Стоимость растёт линейно с числом карточек — сегодня 17.5
тысяч, и на каждую отрисовку карточки уходит 25–30 мс чистых потерь.

ПОЧЕМУ ЧАСТИЧНЫЕ. Все три запроса всегда несут `merged_into_id IS NULL`:
объединённые карточки предлагать нельзя (получится цепочка, которую
«Разъединить» не распутает). Условие в индексе делает его втрое меньше и
избавляет планировщик от лишней проверки. Телефон и имя вдобавок бывают пустыми
— из 17 519 карточек номер есть у 1 426.

БЛОКИРОВКИ. `CREATE INDEX` без `CONCURRENTLY` держит на таблице ShareLock, и
пишущие ждут. Здесь это доли секунды: таблица 4.7 МБ, 17.5 тысяч строк. На
порядок большей базе те же строки надо переписать на
`op.execute("CREATE INDEX CONCURRENTLY …")` внутри `autocommit_block()`.
"""

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None

IX_PHONE = "ix_clients_phone_active"
IX_EXTERNAL = "ix_clients_external_id_active"
IX_NAME = "ix_clients_name_active"


def upgrade() -> None:
    op.create_index(
        IX_PHONE,
        "clients",
        ["phone"],
        postgresql_where=sa.text("phone IS NOT NULL AND merged_into_id IS NULL"),
    )
    op.create_index(
        IX_EXTERNAL,
        "clients",
        ["external_id"],
        postgresql_where=sa.text("merged_into_id IS NULL"),
    )
    # По ВЫРАЖЕНИЮ: код ищет через lower(name), и индекс по колонке планировщик
    # не возьмёт — проверено на боевой базе в транзакции с откатом.
    op.create_index(
        IX_NAME,
        "clients",
        [sa.text("lower(name)")],
        postgresql_where=sa.text("name IS NOT NULL AND merged_into_id IS NULL"),
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5). Обратимость полная: индекс данных не несёт.
    op.drop_index(IX_NAME, table_name="clients")
    op.drop_index(IX_EXTERNAL, table_name="clients")
    op.drop_index(IX_PHONE, table_name="clients")
