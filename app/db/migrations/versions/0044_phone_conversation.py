"""Телефон карточки помнит, каким диалогом он доказан.

ЗАЧЕМ. Заявка в лид-центр собирается из карточки клиента: `_lead_payload`
(`app/services/leads.py`) берёт `client.phone` и `client.name`. Пока карточка
описывает одного человека, это верно. После объединения — уже нет: в карточке
может лежать номер, названный в СОСЕДНЕМ диалоге, и мастер поедет к другому
человеку.

Это худшее последствие ошибочной склейки — хуже, чем показ лишней истории на
экране: там неприятно, здесь выезд к постороннему и разговор о чужом ремонте.

ПОЧЕМУ КОЛОНКА, А НЕ ЖУРНАЛ. Диалог, из которого ввели номер, уже приезжает в
`set_phone` и уже пишется — но только в `details` записи аудита
(`app/services/clients.py`). Спросить у базы «каким диалогом доказан этот
номер» по журналу нельзя: это разбор JSON по всей истории на каждую заявку.

Миграция добавляющая: старый код колонки не знает, новый читает NULL как
«происхождение неизвестно» и ведёт себя осторожно (см. `leads.lead_phone`).
"""

import sqlalchemy as sa
from alembic import op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("phone_conversation_id", sa.Uuid(), nullable=True),
    )
    # ON DELETE SET NULL, а не CASCADE: удаление диалога не имеет права уносить
    # телефон карточки. Потерять происхождение — потерять уверенность; потерять
    # сам номер — потерять клиента.
    op.create_foreign_key(
        "fk_clients_phone_conversation_id_conversations",
        "clients",
        "conversations",
        ["phone_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_clients_phone_conversation_id_conversations", "clients", type_="foreignkey"
    )
    op.drop_column("clients", "phone_conversation_id")
