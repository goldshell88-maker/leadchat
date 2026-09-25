"""Индексы под два внешних ключа адреса в карточке клиента (24.09).

`clients.address_conversation_id` (→ conversations) и
`clients.address_candidate_id` (→ client_address_candidates), оба ON DELETE
SET NULL, заведены 11.09 (0071) без индекса. PostgreSQL проверяет такой ключ
при каждом удалении родителя, и без индекса это полный проход по `clients`:
89 980 строк, 17–31 мс на боевой базе. «Отключить и стереть» удаляет диалоги
канала (медиана 2 244, максимум 5 833), а с ними каскадом и строки адресов —
выходит 40–70 с полных проходов при потолке запроса API 15 с. Запрос падал
уже после того, как подписка у Авито снята: канал оставался в базе без
вебхука.

Там же `conversation_pins.conversation_id` (ON DELETE CASCADE): ключ стоял
вторым в первичном ключе и индексом со стороны диалога не служил.

Тот же класс, что 0067 для `phone_conversation_id`. Обычный CREATE INDEX по
той же причине: миграция идёт транзакцией, а таблицы небольшие.
"""

import sqlalchemy as sa
from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_clients_address_conversation",
        "clients",
        ["address_conversation_id"],
        postgresql_where=sa.text("address_conversation_id IS NOT NULL"),
    )
    op.create_index(
        "ix_clients_address_candidate",
        "clients",
        ["address_candidate_id"],
        postgresql_where=sa.text("address_candidate_id IS NOT NULL"),
    )
    op.create_index("ix_conversation_pins_conversation", "conversation_pins", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_conversation_pins_conversation", table_name="conversation_pins")
    op.drop_index("ix_clients_address_candidate", table_name="clients")
    op.drop_index("ix_clients_address_conversation", table_name="clients")
