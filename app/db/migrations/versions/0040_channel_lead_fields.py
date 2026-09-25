"""Автозаявки: источник, партнёр и ссылка на отзыв — у каждого канала свои.

ЗАЧЕМ. Заявка в лид-центре несёт три вещи, которых у нас до сих пор не было, и
все три свои у каждого аккаунта Авито (решение владельца от 13 августа):

  * **источник** («В95») — уходит в поле «Комментарий Партнера». Это наша
    собственная пометка, присвоенная нами же: по ней в лид-центре видно, с
    какого аккаунта пришёл клиент;
  * **номер партнёра** («7», «723») — по нему расширение находит внутренний id
    (они почти нигде не совпадают), и по нему же решается кнопка «Отзыв»;
  * **ссылка на отзыв** — короткая ссылка на аккаунт Авито, куда клиента просят
    написать отзыв. Уходит последней строкой комментария.

ПОЧЕМУ ССЫЛКА ХРАНИТСЯ, А НЕ СОБИРАЕТСЯ. Владелец сокращает ссылки сам и
вписывает готовую один раз при привязке канала. Сокращатель — чужой сервис;
поставь создание заявки в зависимость от его доступности, и падение стороннего
сайта начнёт останавливать заявки.

ПОЧЕМУ ВСЁ ТРИ ПУСТЫ ПО УМОЛЧАНИЮ И НЕ МЕШАЮТ. Канал без источника заявки
отдаёт — просто без пометки; канал без ссылки отдаёт заявку без строки отзыва.
Придержать лид из-за отсутствия НЕобязательного поля значило бы потерять заявку
ради аккуратности.

Revision ID: 0040
Revises: 0039
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0040"
down_revision: str = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("avito_accounts", sa.Column("lead_origin", sa.Text(), nullable=True))
    op.add_column("avito_accounts", sa.Column("lead_partner_number", sa.Text(), nullable=True))
    op.add_column("avito_accounts", sa.Column("review_url", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("avito_accounts", "review_url")
    op.drop_column("avito_accounts", "lead_partner_number")
    op.drop_column("avito_accounts", "lead_origin")
