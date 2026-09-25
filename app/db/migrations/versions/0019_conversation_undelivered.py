"""Неотправленный ответ виден в строке диалога, а не только в открытой ленте.

ЧТО БЫЛО. Отправка упала — карточка в левом списке выглядела как успешно
отвеченная: «Вы: перезвоню в течение часа», ничего красного. Красный крест
существовал, но только внутри открытого диалога. Оператор уходил к следующему
клиенту и не возвращался, а клиент ждал ответа, которого не было.

ПОЧЕМУ КОЛОНКА, А НЕ ЗАПРОС НА ЧТЕНИИ. Признак «в диалоге есть неотправленное»
выводится подзапросом по `messages`, но список диалогов — самый частый запрос
системы, и такой подзапрос стоил бы отдельного похода в таблицу сообщений на
КАЖДУЮ строку каждого списка. На четырёхстах тысячах диалогов это неприемлемо.
Тот же довод уже записан у `awaiting_since` в модели: поле пишется один раз
там, где событие происходит, — на провале доставки, повторе и удалении, то
есть на редких путях.

Хранится момент, а не «да/нет»: по нему видно, сколько ответ уже висит
неотправленным, и это же значение можно показать человеку.

Индекс частичный: строк с непустым значением единицы на всю базу, а «покажи
все диалоги, где ответ не ушёл» — первый вопрос администратора, когда у канала
кончился токен.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations", sa.Column("undelivered_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_conversations_undelivered",
        "conversations",
        ["undelivered_at"],
        postgresql_where=sa.text("undelivered_at IS NOT NULL"),
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_index("ix_conversations_undelivered", table_name="conversations")
    op.drop_column("conversations", "undelivered_at")
