"""Режим работы бота: автоответ или подсказка оператору.

Зачем. До сих пор бот, если он включён, отвечал клиенту сам — третьего не было дано.
Для выхода на живые аккаунты это слишком резко: сначала нужно посмотреть, ЧТО он пишет,
и только потом отдавать ему слово. Режим «подсказка» даёт ровно это — бот считает ответ
и кладёт его заметкой в диалог, клиент ничего не получает, отвечает человек.

⚠ НОМЕР. Изначально это была 0035; пока правка лежала в стороне, номер занял
`0035_bots_ai_provider` (соседняя работа над тем же деревом). Две ревизии с одним
`down_revision` дают alembic ДВЕ головы и `upgrade head`, который не знает, куда идти.
Поэтому здесь 0036 поверх 0035 — колонки разные, порядок между ними не важен.

Значение по умолчанию — `suggest`, самое безопасное. Существующие боты (их на момент
миграции ноль) и все новые начинают с подсказки; переключение на автоответ — осознанное
действие руками.

Revision ID: 0036
Revises: 0035
"""

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str = "0035"
branch_labels = None
depends_on = None

#: КОРОТКОЕ имя ограничения — то, что подставляется в `naming_convention`
#: (`ck_%(table_name)s_%(constraint_name)s` из app/models/base.py). В базе оно станет
#: `ck_bots_mode`. Полное имя писать здесь нельзя: и `create_check_constraint`, и
#: `drop_constraint` прогоняют аргумент через соглашение, и вышло бы
#: `ck_bots_ck_bots_mode` (ровно эта ошибка уже была в 0027).
CK_MODE = "mode"

CONDITION = "mode in ('suggest', 'auto')"


def upgrade() -> None:
    op.add_column(
        "bots",
        sa.Column("mode", sa.Text(), nullable=False, server_default="suggest"),
    )
    # Значения ограничены на уровне базы: режим читается движком на каждом ходу,
    # и опечатка в нём означала бы молча неотправленные (или наоборот отправленные) реплики.
    op.create_check_constraint(CK_MODE, "bots", sa.text(CONDITION))


def downgrade() -> None:
    op.drop_constraint(CK_MODE, "bots", type_="check")
    op.drop_column("bots", "mode")
