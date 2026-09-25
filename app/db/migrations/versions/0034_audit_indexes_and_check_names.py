"""Журналу аудита — два индекса; пяти CHECK — имена, которых ждут модели.

Две несвязанные правки схемы в одной ревизии намеренно: обе — чистый DDL без
переноса данных, обе занимают доли секунды, и разносить их по двум окнам
выката значит платить два раза за одно и то же.

═══ ЧАСТЬ 1. ИНДЕКСЫ ПОД `GET /audit-log` ═══════════════════════════════════

ЧТО ЗА ЗАПРОС. Ручка `api/routes/audit.py` всегда читает журнал одинаково:

    ... [WHERE user_id = :id] ORDER BY created_at DESC, id DESC LIMIT :n OFFSET :k

Порядок — часть контракта пагинации, а не украшение: события одной секунды без
второго ключа (`id DESC`) идут в произвольном порядке, и вторая страница
повторяет строки первой. Фильтр «по сотруднику» — единственный, ради которого
журнал вообще открывают чаще всего («что делал этот человек вчера»).

ПОЧЕМУ ПРЕЖНИХ ИНДЕКСОВ НЕ ХВАТАЕТ. Их два, оба из 0004, и оба мимо:

* `idx_audit_action_created` — `(action, created_at)`. Ведущий столбец
  `action` в этом запросе не задан вовсе (фильтр по событию необязателен),
  поэтому под сортировку по времени индекс не годится: btree отдаёт порядок
  только внутри одного `action`.
* `idx_audit_entity` — `(entity, entity_id, created_at)`, ровно та же история.

То есть открытие журнала — это seq scan всей таблицы плюс сортировка всей
таблицы ради пятидесяти строк, и точно так же — фильтр по сотруднику. Сейчас
это незаметно (боевая база 12 августа — 1043 записи, доли миллисекунды), но
`audit_log` не чистится: в него пишет весь бизнес-код через
`services.audit.write_audit`, и таблица только растёт. Неприятно другое —
КОГДА это станет заметно. Журнал открывают не от любопытства, а при разборе:
клиент жалуется, диалог кто-то закрыл, надо понять кто и когда. Именно в этот
момент экран и начнёт думать секундами.

ПОЧЕМУ ПОД ФИЛЬТР СОСТАВНОЙ, А НЕ ГОЛЫЙ `(user_id)`. Фильтр по сотруднику
никогда не ходит один — он всегда с тем же `ORDER BY` и `LIMIT`. Индекс
`(user_id, created_at DESC, id DESC)` отдаёт первую страницу готовой, вообще
без шага сортировки; голый `(user_id)` нашёл бы строки человека и заставил бы
отсортировать их все, а у активного диспетчера их со временем десятки тысяч.
Ведущим столбцом составной остаётся обычным индексом по `user_id` — то есть
покрывает и «просто фильтр», и `count(*)` под ним.

ЗАПИСИ БЕЗ АВТОРА (`user_id IS NULL` — то, что делает планировщик и вебхуки) в
индекс попадают, хотя фильтр «по сотруднику» их не спрашивает никогда.
Частичным (`WHERE user_id IS NOT NULL`) не делаю: доля таких записей заранее
неизвестна, а частичный индекс сузил бы применимость до одного предиката ради
экономии, которую пока нечем измерить.

`DESC` ВЫПИСАН ЯВНО, хотя btree читается в обе стороны и `(created_at, id)`
формально сгодился бы. Порядок у ручки один-единственный и меняться не
собирается, а совпадение столбец-в-столбец снимает вопрос при чтении плана.
Цена нулевая.

ОБЪЁМ И БЛОКИРОВКИ. `CREATE INDEX` без `CONCURRENTLY` держит на таблице
SHARE: чтения проходят, записи ждут. В `audit_log` пишет каждое действие
пользователя, но на тысяче строк это доли секунды. ЕСЛИ ЭТА МИГРАЦИЯ
КОГДА-НИБУДЬ ПОЕДЕТ НА ЖУРНАЛ В СОТНИ ТЫСЯЧ СТРОК — переписать на
`op.execute("CREATE INDEX CONCURRENTLY …")` внутри `autocommit_block()`.

В МОДЕЛИ ИНДЕКСЫ НЕ ОБЪЯВЛЕНЫ — как и у `audit_log` из 0004 и у
`ix_conversations_open_last_message` из 0028. Юнит-набор строит схему из
метаданных на SQLite, где планировщика Postgres нет и индекс не проверяет
ничего; правильность выдачи журнала держат тесты ручки, скорость — эти два
индекса, а факт их существования — интеграционный тест на настоящей базе
(`tests/integration/test_audit_schema_pg.py`).

═══ ЧАСТЬ 2. ДВОЙНАЯ ПРИСТАВКА В ИМЕНАХ CHECK ══════════════════════════════

ЧТО ПРОИЗОШЛО. `op.create_table` собирает таблицу на метаданных с нашим
`naming_convention` (`app/models/base.py`), а правило `ck` в нём —
`ck_%(table_name)s_%(constraint_name)s`. В отличие от `pk`/`fk`/`uq`, это
правило применяется и к ограничениям с ЯВНЫМ именем: заданное имя
подставляется в `%(constraint_name)s`. В 0001 и 0006 в `name=` передали уже
полное имя — и база получила его дважды:

    ck_users_ck_users_users_role                     ← модель ждёт ck_users_users_role
    ck_notifications_ck_notifications_severity       ← ck_notifications_severity
    ck_notifications_ck_notifications_audience       ← ck_notifications_audience
    ck_notifications_ck_notifications_addressing     ← ck_notifications_addressing
    ck_notifications_ck_notifications_repeat_count   ← ck_notifications_repeat_count

(Четыре из пяти — из 0006, пятое приехало ещё из 0001 тем же способом. Чинятся
вместе: дефект один, и оставлять половину значит оставить ту же мину.)

ЧЕМ ЭТО ГРОЗИТ. Сами данные защищены — CHECK работает независимо от того, как
он назван, и до сих пор расхождение ничего не ломало. Мина срабатывает у
первой же миграции, которая снимет ограничение ПО ИМЕНИ ИЗ МОДЕЛИ: расширить
словарь `severity`, добавить роль, поменять условие адресации — всё это
`drop_constraint("severity", "notifications", type_="check")`, то есть
`DROP CONSTRAINT ck_notifications_severity`, которого в базе нет.
`ProgrammingError` посреди выката, на боевой базе, в отведённое окно — ровно
на этом уже падала 0027 (см. комментарий `CK_STATUS` в 0033). Причём на
dev-стенде, поднятом с нуля, всё выглядит так же, как на бою, потому что
кривые имена туда приезжают тем же 0006 — расхождение не всплывает нигде,
пока не станет поздно.

ПОЧЕМУ RENAME, А НЕ DROP + ADD. `RENAME CONSTRAINT` — правка каталога:
мгновенно и без перепроверки данных. `DROP` + `ADD` пересканировал бы таблицу
под ACCESS EXCLUSIVE и на время транзакции оставил бы строки без охраны, ничего
не дав взамен: условие не меняется, меняется только имя.

ПОЧЕМУ НЕ ПРАВКОЙ 0001 И 0006. Они уже применены на боевой базе. Правка их
текста не переименует там ничего, зато базы, поднятые до и после правки,
разъедутся при одинаковом `alembic_version` — а это худший вид расхождения:
невидимый.

ПЕРЕИМЕНОВАНИЕ ИДЁТ ПОД ПРОВЕРКОЙ существования старого имени. Если кто-то уже
починил имя руками (например, на стенде), деплой не должен ложиться из-за
работы, которая уже сделана.

═══ НОМЕР РЕВИЗИИ ПРИ СЛИЯНИИ НАДО ПЕРЕПРОВЕРИТЬ ═══════════════════════════

На 12 августа над этим деревом идут параллельные работы, и `0034` мог занять
кто-то ещё. Две ревизии с одним `down_revision` дают alembic ДВЕ головы, и
`upgrade head` на такой базе не поднимется вовсе. Тот, кто сливает вторым,
переставляет `down_revision` на легшую первой ревизию и переименовывает файл.

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0034"
down_revision: str = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Порядок журнала: `ORDER BY created_at DESC, id DESC` без фильтров.
IX_JOURNAL = "ix_audit_log_created_at_id"
#: Тот же порядок внутри одного сотрудника + обычный индекс по `user_id`.
IX_BY_USER = "ix_audit_log_user_created_at"

#: (таблица, имя В БАЗЕ, имя, которое строит из модели naming_convention).
DOUBLED_CHECKS: tuple[tuple[str, str, str], ...] = (
    ("users", "ck_users_ck_users_users_role", "ck_users_users_role"),
    ("notifications", "ck_notifications_ck_notifications_severity", "ck_notifications_severity"),
    ("notifications", "ck_notifications_ck_notifications_audience", "ck_notifications_audience"),
    (
        "notifications",
        "ck_notifications_ck_notifications_addressing",
        "ck_notifications_addressing",
    ),
    (
        "notifications",
        "ck_notifications_ck_notifications_repeat_count",
        "ck_notifications_repeat_count",
    ),
)


def _rename_check(table: str, old: str, new: str) -> None:
    """`ALTER TABLE … RENAME CONSTRAINT`, но только если старое имя ещё живо.

    Имена — литералы из этого файла, снаружи в запрос ничего не приходит.
    """
    op.execute(
        f"""
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM pg_constraint
                 WHERE conname = '{old}' AND conrelid = '{table}'::regclass
            ) THEN
                ALTER TABLE {table} RENAME CONSTRAINT {old} TO {new};
            END IF;
        END
        $$;
        """
    )


def upgrade() -> None:
    op.create_index(
        IX_JOURNAL,
        "audit_log",
        [sa.text("created_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        IX_BY_USER,
        "audit_log",
        ["user_id", sa.text("created_at DESC"), sa.text("id DESC")],
    )
    for table, old, new in DOUBLED_CHECKS:
        _rename_check(table, old, new)


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    # Обратимость полная: индексы данных не несут, переименование их не меняет.
    for table, old, new in DOUBLED_CHECKS:
        _rename_check(table, new, old)
    op.drop_index(IX_BY_USER, table_name="audit_log")
    op.drop_index(IX_JOURNAL, table_name="audit_log")
