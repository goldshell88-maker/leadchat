"""Sprint 3 — идемпотентность исходящих сообщений в БД.

``messages.client_message_id`` (tempId фронта, 01 §1.6) + партиальный
уникальный индекс — второй эшелон защиты от дублей по 08 §8.2/§8.3.
Первый рубеж — ключ ``idem:msg:{conversation_id}:{cmid}`` в Redis
(``SET NX EX 86400``); БД страхует его на случай потери Redis.

``created_at`` входит в ключ индекса вынужденно: ``messages``
партиционирована ``BY RANGE (created_at)``, а PostgreSQL требует, чтобы
уникальный индекс партиционированной таблицы содержал ключ
партиционирования. Ровно так же устроен индекс вебхуков из 0001
(``uq_messages_conversation_external_created``).

DDL обычный, не ``CONCURRENTLY``, и вся миграция — одна транзакция. Это
сознательный выбор, проверенный на живом стенде: ``CREATE INDEX
CONCURRENTLY`` ждёт завершения ВСЕХ параллельных транзакций (virtualxid), а
пул API держит соединения в состоянии ``idle in transaction`` — миграция
висла бесконечно и оставляла за собой невалидный индекс, потому что
autocommit-блок коммитит всё, что было до него. Обычный ``CREATE INDEX``
берёт SHARE, конфликтует только с пишущими транзакциями и на текущем
объёме ``messages`` отрабатывает мгновенно. Если таблица дорастёт до
десятков миллионов строк, индекс надо будет строить руками
(``CONCURRENTLY`` по каждой партиции + ``ALTER INDEX ... ATTACH PARTITION``)
при остановленных api/worker — и только тогда, отдельным шагом рантбука.

Revision ID: 0003
Revises: 0002
Create Date: 2026-08-05
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PARENT_INDEX = "uq_messages_conversation_client_message_created"


def upgrade() -> None:
    # NULL-колонка без DEFAULT — метаданные, переписывания таблицы нет.
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS client_message_id text")
    # На партиционированной таблице PostgreSQL сам создаёт индекс у каждой
    # партиции и присоединяет его к родительскому (08 §6.3 продолжит это для
    # новых месяцев автоматически).
    op.execute(
        f"CREATE UNIQUE INDEX IF NOT EXISTS {PARENT_INDEX} "
        f"ON messages (conversation_id, client_message_id, created_at) "
        f"WHERE client_message_id IS NOT NULL"
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется)
    op.execute(f"DROP INDEX IF EXISTS {PARENT_INDEX}")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS client_message_id")
