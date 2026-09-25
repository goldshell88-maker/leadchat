"""Единая статусная модель диалога: ограничение, время входа в статус, отложка.

ЧТО БЫЛО. У `conversations.status` не было в базе НИ ОДНОГО ограничения — в
колонку писали тринадцать мест кода, и охраняли её три независимые проверки на
Python (кортеж в сервисе, `Literal` в схеме запроса, регулярка в фильтре
списка), ни одна из которых не была выведена из двух других. Значений было
три: `new`, `in_progress`, `closed`.

ЧТО ДЕЛАЕТ МИГРАЦИЯ. Четыре вещи, и ни одна из них не меняет поведение
работающего кода:

1. `CHECK` на пять значений. Старые три в список входят, поэтому выкатка на
   боевую базу со старым кодом безопасна: он умеет писать только их.
2. `status_since` — когда диалог вошёл в нынешний статус. Отдельная колонка,
   а не `updated_at`: тот двигается от любого изменения строки (ORM onupdate),
   и строка «в работе 12 мин» на нём показывала бы время последнего сообщения.
3. Три колонки отложки. Заводятся СРАЗУ, хотя механизм возврата по времени
   едет отдельным релизом: `ALTER TABLE` на этой таблице стоит дороже трёх
   nullable-колонок, а вторая миграция через неделю — ещё одно окно выката.
4. Частичный индекс под сторож отложки.

ЗАПОЛНЕНИЕ `status_since` — ОДНИМ ЗАПРОСОМ, И ОНО ПРИБЛИЗИТЕЛЬНО. Точное время
входа в статус за прошлое не восстановимо ниоткуда: `audit_log` хранит смены
(`conversation.status_changed`), но автопереходы до 7 августа в нём есть не
всегда. Берём `COALESCE(last_message_at, updated_at)`. Приблизительное значение
честнее NULL: у 100 % диалогов колонка станет точной через сутки работы, а
прочерк в интерфейсе на всех строках сразу читался бы как поломка.

ОБЪЁМ И БЛОКИРОВКИ. `ADD CONSTRAINT ... CHECK` без `NOT VALID` сканирует
таблицу под ACCESS EXCLUSIVE, а в `conversations` непрерывно пишет
inbound-конвейер. На нынешнем объёме (десятки тысяч строк, замер боевой базы
11 августа — 37 диалогов на двух живых каналах плюс импортированная история)
это доли секунды. ЕСЛИ ЭТА ПРАВКА КОГДА-НИБУДЬ ПОЕДЕТ НА БАЗУ В СОТНИ ТЫСЯЧ
СТРОК — разбить на `ADD CONSTRAINT ... NOT VALID` и `VALIDATE CONSTRAINT`
двумя транзакциями, а индекс делать `CONCURRENTLY` вне транзакции миграции.

ПЕРЕД ВЫКАТКОЙ ОБЯЗАТЕЛЕН ЗАПРОС (docs/38, «Миграция и порядок выката», шаг 0):

    SELECT status, count(*) FROM conversations GROUP BY status ORDER BY 2 DESC;

Ограничения в колонке не было никогда — если в выдаче окажется значение вне
пяти, `ADD CONSTRAINT` упадёт на боевом. Это правильное поведение (лучше
падения миграции, чем падения трафика), но узнать об этом надо ДО окна выката,
а не в нём.

НОМЕР РЕВИЗИИ ПРИ СЛИЯНИИ НАДО ПЕРЕПРОВЕРИТЬ — И ЭТО НЕ ФОРМАЛЬНОСТЬ.
На 12 августа номер `0026` занят параллельной работой в основном дереве
(`0026_split_glued_clients.py`, ещё не в ветке `main`), а `0024` числился
сразу за тремя ТЗ. Две ревизии с одним `down_revision` дают alembic ДВЕ
головы, и `upgrade head` на такой базе не поднимется вовсе — номер тут ни при
чём, чинится именно цепочка. Тот, кто сливает вторым, обязан переставить
`down_revision` этой ревизии на ту, что легла первой, и переименовать файл.

ОБРАТИМОСТЬ. `downgrade` полный, но обратим он только ДО первого перевода
диалога в новый статус. После — строки со значениями `waiting_client` и
`snoozed` остались бы в базе, а откатившийся код их не знает: он покажет их
латиницей и не даст сменить. Поэтому downgrade СНАЧАЛА сводит такие строки в
`in_progress` — явным запросом, а не подразумеваемо. Тот же запрос лежит в
`docs/RUNBOOK-DEPLOY.md` на случай отката релиза без отката схемы.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# НОМЕР СДВИНУТ С 0026 НА 0027 ПРИ СЛИЯНИИ, И ЭТО НЕ КОСМЕТИКА.
#
# Пока писалась эта миграция, на прод уехала другая с тем же номером —
# `0026_split_glued_clients` (расцепление карточек клиентов, слипшихся по
# пустому идентификатору). Две ревизии с одним номером рвут цепочку Alembic:
# `alembic upgrade head` находит две головы и отказывается работать.
#
# Поэтому эта встаёт СЛЕДОМ за уехавшей, а не рядом с ней. Порядок важен и по
# смыслу: 0026 чинит данные (кто есть кто), 0027 меняет схему статусов.
revision: str = "0027"
down_revision: str = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: КОРОТКОЕ имя ограничения — то, что подставляется в `naming_convention`
#: (`ck_%(table_name)s_%(constraint_name)s` из app/models/base.py). В базе оно
#: превращается в `ck_conversations_status`.
#:
#: Здесь легко ошибиться и написать полное имя: `create_check_constraint` и
#: `drop_constraint` ОБА прогоняют аргумент через соглашение, и полное имя даёт
#: `ck_conversations_ck_conversations_status` — `DROP CONSTRAINT`, не находящий
#: цели. Ровно на этом и упал тест обратимости при первом прогоне.
CK_STATUS = "status"
IX_SNOOZE = "ix_conversations_snoozed_until"


def upgrade() -> None:
    op.add_column("conversations", sa.Column("status_since", sa.DateTime(timezone=True)))
    op.add_column("conversations", sa.Column("snoozed_until", sa.DateTime(timezone=True)))
    op.add_column("conversations", sa.Column("snoozed_by_id", sa.Uuid()))
    op.add_column("conversations", sa.Column("snooze_reason", sa.Text()))
    op.create_foreign_key(
        "fk_conversations_snoozed_by_id_users",
        "conversations",
        "users",
        ["snoozed_by_id"],
        ["id"],
    )
    op.create_index(
        IX_SNOOZE,
        "conversations",
        ["snoozed_until"],
        postgresql_where=sa.text("snoozed_until IS NOT NULL"),
    )
    # Заполнение — ДО ограничения и одним запросом: он не ходит в сеть,
    # детерминирован и на нынешнем объёме занимает доли секунды.
    op.execute(
        "UPDATE conversations "
        "SET status_since = COALESCE(last_message_at, updated_at) "
        "WHERE status_since IS NULL"
    )
    op.create_check_constraint(
        CK_STATUS,
        "conversations",
        "status IN ('new','in_progress','waiting_client','snoozed','closed')",
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    #
    # ПЕРВЫМ ДЕЛОМ — ДАННЫЕ, и только потом схема. Диалоги в новых статусах
    # для откатившегося кода не существуют: он их не покажет, не отфильтрует и
    # не даст сменить. `WHERE` обязателен — без него это переписывание всей
    # таблицы и потеря закрытых.
    op.execute(
        "UPDATE conversations "
        "SET status = 'in_progress', snoozed_until = NULL, "
        "    snoozed_by_id = NULL, snooze_reason = NULL "
        "WHERE status IN ('waiting_client', 'snoozed')"
    )
    op.drop_constraint(CK_STATUS, "conversations", type_="check")
    op.drop_index(IX_SNOOZE, table_name="conversations")
    op.drop_constraint("fk_conversations_snoozed_by_id_users", "conversations", type_="foreignkey")
    op.drop_column("conversations", "snooze_reason")
    op.drop_column("conversations", "snoozed_by_id")
    op.drop_column("conversations", "snoozed_until")
    op.drop_column("conversations", "status_since")
