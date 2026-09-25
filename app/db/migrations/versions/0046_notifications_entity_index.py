"""Индекс по сущности уведомления — под авторезолв.

С 14 августа тревоги про диалог гаснут САМИ, когда повод исчез: приём, передача
и закрытие зовут `notifications.resolve_conversation`, а та ищет живые строки по
`(kind, entity_type, entity_id)`. Индекса под этот поиск не было — только по
получателю, аудитории, ключу склейки и сроку, — то есть КАЖДОЕ действие с
диалогом читало таблицу уведомлений целиком.

Сегодня это микросекунды: строк сотни. Но таблица растёт с каждой тревогой,
живёт 90 дней ротации, а вызов стоит на самом горячем пути системы — на каждом
принятом и закрытом диалоге. Такое дорожает молча: никто не заметит день, когда
закрытие диалога начнёт стоить сканирования десятков тысяч строк.

Замечено при написании авторезолва и отложено НАМЕРЕННО: тогда в партии уже
стояло семь миграций, и восьмая ради выгоды, которой ещё нет, удлиняла бы
единственную транзакцию наката. Теперь партия своя и пустая.

ЧАСТИЧНЫЙ — `WHERE entity_type IS NOT NULL`: системные тревоги без сущности
(диск, планировщик, бэкап) авторезолвом не ищутся никогда, им в индексе делать
нечего. Тот же приём, что у соседних индексов этой таблицы.

Revision ID: 0046
Revises: 0045
Create Date: 2026-08-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0046"
down_revision: str | None = "0045"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_notifications_entity",
        "notifications",
        ["entity_type", "entity_id"],
        postgresql_where=sa.text("entity_type IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_entity", table_name="notifications")
