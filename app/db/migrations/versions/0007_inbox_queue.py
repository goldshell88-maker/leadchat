"""Очередь «Входящие» с явным принятием диалога — поля очереди у ``conversations``
(план 7.1, разбор Jivo 15 §2.1).

ПОЧЕМУ ПОЛЯ, А НЕ НОВЫЙ СТАТУС
------------------------------
Просилось очевидное: добавить в ``status`` пятое значение (``waiting`` /
``offered``). Отказались осознанно — ``status`` в этой системе давно не
внутренний флаг, а публичный контракт, который читают:

* вкладки списка диалогов (01 §5.1: `my` = мои и не закрытые, `new`, `all`,
  `closed`) — новое значение пришлось бы вписывать в каждое условие, иначе
  диалог, ждущий принятия, пропал бы разом из «Новых» и из «Всех»;
* статистика (06 §0.3) — она считается по событиям ``conversation.status_changed``
  с парой ``{from,to}``; неизвестное значение молча испортило бы воронку;
* inbound-конвейер (DESIGN §8.3): возврат клиента переводит ``closed → new``;
* движок ботов (02 §2.2, ``should_run_bot``) и правило «диалог закрыт — отвечать
  нельзя» (01 §6.2);
* фронтенд и десктоп, где ``status`` покрашен и подписан (10 §7.2).

Поля очереди ортогональны статусу и ничего из перечисленного не трогают:
``status`` продолжает отвечать на вопрос «в какой стадии переписка», а очередь —
на вопрос «есть ли у диалога хозяин». Диалог, который ждёт принятия, остаётся
честно «Новым» и виден во вкладке «Новые», как и был.

ЧТО ДОБАВЛЕНО
-------------
``offered_at``     — момент постановки в очередь. Сортировка очереди и «сколько
                     ждёт»; NULL = «в очереди никогда не стоял».
``claimed_by_id``  — кто принял. ЕДИНСТВЕННЫЙ арбитр гонки: принятие делается
                     одним ``UPDATE ... WHERE claimed_by_id IS NULL``, и при
                     тринадцати операторах именно эта строчка не даёт двоим
                     писать одному клиенту.
``claimed_at``     — когда принял (метрика «сколько диалог ждал оператора»).
``declined_by``    — кто отказался. ``text[]`` c id строками — тот же приём, что
                     у ``tags``: переносится на SQLite юнит-тестов без правок.
                     Причина отказа туда НЕ кладётся намеренно: она уходит
                     системной записью в ленту диалога, где её читает человек, —
                     дублировать её в колонку значит завести второй источник
                     правды и первым же рефакторингом их рассинхронизировать.
``escalated_at``   — «отказались все, кому диалог доступен». Диалог остаётся в
                     очереди с пометкой, администраторы позваны через центр
                     уведомлений (14). Флаг, а не вычисление на лету: состав
                     операторов меняется, а факт «в тот момент отказались все»
                     обязан остаться воспроизводимым.

``claimed_by_id`` — не дубль ``assignee_id``. Ответственный меняется передачей
(01 §5.5) и снимается руководителем; «кто вынул диалог из очереди» — исторический
факт, который передача переписывать не должна. Плюс отдельная колонка даёт
принятию свой предикат: ``WHERE claimed_by_id IS NULL`` не конфликтует с
автоназначением по первому ответу (01 §6.2), которое трогает ``assignee_id``.

ОБРАТНАЯ СОВМЕСТИМОСТЬ
----------------------
Условие очереди — ``claimed_by_id IS NULL AND assignee_id IS NULL AND
offered_at IS NOT NULL AND status <> 'closed'``. Три «замка» вместо одного, и
каждый закрывает свой класс старых данных:

1. ``assignee_id IS NULL`` — диалоги, которые ведут прямо сейчас, во «Входящие»
   не всплывут, даже если про них забыл этот бэкофилл. Тем же замком очередь
   переживает старый путь «кто первым ответил, тот и ведёт» (01 §6.2,
   ``messages._auto_assign``): он ставит только ``assignee_id``, и диалог
   уходит из очереди сам, без правки чужой зоны.
2. ``offered_at IS NOT NULL`` — то, что в очередь никогда не ставили, в неё и не
   попадёт. Это страховка на случай, если какой-то путь создания диалога забудут
   научить ``inbox.enter_queue``: диалог останется в «Новых» и потеряется не
   больше, чем терялся до 7.1.
3. ``status <> 'closed'`` — закрытые (в том числе вся историческая выгрузка
   backfill'а, решение владельца №3 в inbound) очередь не засоряют.

Бэкофилл, соответственно:

* у кого есть ответственный — считаем диалог уже принятым: ``claimed_by_id =
  assignee_id``, ``claimed_at = updated_at``. Иначе после деплоя тринадцать
  операторов увидели бы во «Входящих» всю текущую работу друг друга;
* у кого ответственного нет и диалог не закрыт — ставим в очередь с честным
  временем ожидания ``COALESCE(last_message_at, updated_at)``, а не ``now()``:
  диалог, который клиент прислал два часа назад, обязан оказаться наверху
  очереди, а не в её хвосте;
* закрытым ``offered_at`` не ставим вовсе.

ИНДЕКСЫ
-------
Оба индекса очереди ЧАСТИЧНЫЕ по условию очереди. При 454 000 диалогов
(15 §1) очередь — это десятки строк, и индекс обязан быть размером с очередь, а
не с архивом: полный индекс по ``offered_at`` заставил бы сервер листать
историю ради счётчика вкладки, который дёргается на каждом событии.

* ``ix_conversations_inbox_wait`` — очередь целиком, «дольше всех ждущий
  первым»;
* ``ix_conversations_inbox_account`` — очередь одного канала (готовим 7.2:
  назначение операторов на каналы; фильтр ``?account_id=`` работает уже сейчас);
* ``ix_conversations_claimed_by`` — обратная сторона: «что принял этот
  сотрудник», плюс проверка внешнего ключа при удалении учётной записи.

Индекса по ``declined_by`` нет намеренно. Выборка спрашивает ОТРИЦАНИЕ
(«не отклонён мной»), а отрицание по GIN не ускоряется в принципе; сама выборка
уже сужена частичным индексом очереди до десятков строк.

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Условие очереди — одной строкой, чтобы предикаты частичных индексов и
# предикат выборки (app/services/inbox.queue_condition) не разъехались.
IN_QUEUE = (
    "claimed_by_id IS NULL AND assignee_id IS NULL "
    "AND offered_at IS NOT NULL AND status <> 'closed'"
)


def upgrade() -> None:
    op.add_column("conversations", sa.Column("offered_at", sa.DateTime(timezone=True)))
    op.add_column(
        "conversations", sa.Column("claimed_by_id", postgresql.UUID(as_uuid=True), nullable=True)
    )
    op.add_column("conversations", sa.Column("claimed_at", sa.DateTime(timezone=True)))
    op.add_column(
        "conversations",
        sa.Column(
            "declined_by",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
    )
    op.add_column("conversations", sa.Column("escalated_at", sa.DateTime(timezone=True)))
    op.create_foreign_key(
        "fk_conversations_claimed_by_id_users",
        "conversations",
        "users",
        ["claimed_by_id"],
        ["id"],
    )

    # --- бэкофилл: то, что уже ведут, в очередь не возвращается ---
    op.execute(
        "UPDATE conversations SET claimed_by_id = assignee_id, claimed_at = updated_at "
        "WHERE assignee_id IS NOT NULL"
    )
    # --- бэкофилл: то, что ждёт разбора, встаёт в очередь со своим ожиданием ---
    op.execute(
        "UPDATE conversations SET offered_at = COALESCE(last_message_at, updated_at) "
        "WHERE assignee_id IS NULL AND status <> 'closed'"
    )

    op.execute(
        f"CREATE INDEX ix_conversations_inbox_wait ON conversations (offered_at) WHERE {IN_QUEUE}"
    )
    op.execute(
        "CREATE INDEX ix_conversations_inbox_account ON conversations (account_id, offered_at) "
        f"WHERE {IN_QUEUE}"
    )
    op.execute(
        "CREATE INDEX ix_conversations_claimed_by ON conversations (claimed_by_id) "
        "WHERE claimed_by_id IS NOT NULL"
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.execute("DROP INDEX IF EXISTS ix_conversations_claimed_by")
    op.execute("DROP INDEX IF EXISTS ix_conversations_inbox_account")
    op.execute("DROP INDEX IF EXISTS ix_conversations_inbox_wait")
    op.drop_constraint("fk_conversations_claimed_by_id_users", "conversations", type_="foreignkey")
    op.drop_column("conversations", "escalated_at")
    op.drop_column("conversations", "declined_by")
    op.drop_column("conversations", "claimed_at")
    op.drop_column("conversations", "claimed_by_id")
    op.drop_column("conversations", "offered_at")
