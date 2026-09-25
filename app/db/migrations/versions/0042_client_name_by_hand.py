"""Имя клиента можно править руками — и это надо отличать от имени из Авито.

ТРЕБОВАНИЕ ЗАКАЗЧИКА ОТ 13 АВГУСТА: «сделай так, чтобы можно было редактировать ИМЯ
клиента». До сих пор имя приходило только из профиля Авито, и правилось ровно одним
способом — «дописать, если пусто».

ЗАЧЕМ ДВЕ КОЛОНКИ, А НЕ ПРОСТО РАЗРЕШИТЬ ЗАПИСЬ В `name`. Потому что без них правка
диспетчера молча теряется, причём в трёх местах сразу, и все три — обычная работа:

  * **объединение карточек** (`clients._pick_name`) выбирает имя по правилу «у кого
    есть, того и берём», рассчитанному на то, что ОБА имени пришли из Авито. Диспетчер
    вписал имя со слов клиента, объединил с карточкой того же человека с другого
    аккаунта — и получил обратно авитошное «Ак»;
  * **разъединение** восстанавливает поля по снимку из журнала и обязано вернуть
    ТОЧНО исходное состояние — то есть затрёт переименование, сделанное после
    объединения;
  * **дозагрузка из Авито** (`client_enrich`) решает «писать или нет» по пустоте поля.
    Пустота, поставленная человеком нарочно, и пустота «ещё не узнали» для неё одно и
    то же — очищенное имя вернулось бы само, и диспетчер решил бы, что кнопка не
    работает.

Все три случая различаются одним признаком: «это имя ввёл человек». Ровно так уже
сделано у телефона — `phone_set_by_id` / `phone_set_at` (миграция 0031), и здесь
повторяется тот же приём, а не изобретается второй.

ПОЧЕМУ NULLABLE И БЕЗ ЗАПОЛНЕНИЯ. NULL значит «имя не трогали руками» — а таково всё,
что есть в базе на момент выкатки: ручного ввода имени в системе не существовало.
Заполнять нечем и незачем.

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-13
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clients",
        sa.Column("name_set_by_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column("clients", sa.Column("name_set_at", sa.DateTime(timezone=True), nullable=True))
    # ON DELETE SET NULL — как у телефона: уволенный сотрудник не должен уносить с собой
    # ни карточку клиента, ни сам факт, что имя ввели руками.
    op.create_foreign_key(
        "fk_clients_name_set_by",
        "clients",
        "users",
        ["name_set_by_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_constraint("fk_clients_name_set_by", "clients", type_="foreignkey")
    op.drop_column("clients", "name_set_at")
    op.drop_column("clients", "name_set_by_id")
