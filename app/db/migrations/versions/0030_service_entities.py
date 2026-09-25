"""Явный признак «служебная запись» у пользователя и у аккаунта Авито.

ЧТО БЫЛО. Служебные сущности регрессионного smoke (07 §6) — пользователь-робот,
аккаунт-заглушка и диалог `SMOKE-CONV` — жили в боевой базе неотличимо от
настоящих. Отличить их можно было ровно одним способом: угадать по строке.
Пользователя прятали по домену адреса (`config.is_service_email`, суффикс
`.local`), аккаунт и диалог не прятали нигде.

ЧЕМ ЭТО ОБЕРНУЛОСЬ НА БОЮ (замер 12 августа):

* в «Разборе диалогов» стоит строка с клиентом «SMOKE (служебный)» и чатом
  `SMOKE-CONV` — руководитель считает по ней как по обращению;
* угадывание по домену бьёт мимо в обе стороны: на `.local` у заказчика живут
  НАСТОЯЩИЕ администраторы (`admin@leadpartner.local`,
  `dev-admin@leadchat.local`), и список сотрудников не показывает их вовсе.

ЧТО ДЕЛАЕТ МИГРАЦИЯ. Две булевы колонки со значением по умолчанию `false`:
`users.is_service` и `avito_accounts.is_service`. Признак ставит тот, кто
запись создаёт (`app/cli.py seed-smoke`, идёт на каждом деплое), а читают его
те, кто строит отчёт.

ЗАПОЛНЕНИЕ — ТОЧЕЧНОЕ, А НЕ ПО ЭВРИСТИКЕ. Мы намеренно НЕ повторяем здесь
правило «адрес кончается на .local»: именно оно и прячет двух живых
администраторов. Помечается ровно то, что создаёт seed-smoke, — робот по его
адресу и заглушка по её зарезервированному `avito_user_id`. Ошибиться в другую
сторону (не пометить служебное) дёшево: следующий же прогон seed-smoke пометит,
он идемпотентен. Ошибиться в эту (пометить человека) — значит стереть его из
отчётов молча.

Адрес робота и `avito_user_id` заглушки берутся из настроек, а не зашиты
числом: `SMOKE_USER_EMAIL` и `SMOKE_AVITO_USER_ID` настраиваются, и миграция
обязана красить ровно ту строку, которую заведёт команда на этом же стенде.

ОБЪЁМ И БЛОКИРОВКИ. `ADD COLUMN` с константным DEFAULT в PostgreSQL 11+ —
операция на метаданных: таблица не переписывается, блокировка на доли секунды.
`users` и `avito_accounts` — десятки строк, а не сотни тысяч.

ОБРАТИМОСТЬ. Полная: колонки новые, кроме нас их никто не заполняет, при
откате теряется только сам признак, а он восстанавливается прогоном seed-smoke.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.core.config import settings

revision: str = "0030"
down_revision: str = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for table in ("users", "avito_accounts"):
        op.add_column(
            table,
            sa.Column(
                "is_service",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )

    bind = op.get_bind()
    bind.execute(
        sa.text("UPDATE users SET is_service = true WHERE lower(email) = lower(:email)"),
        {"email": settings.smoke_user_email},
    )
    bind.execute(
        sa.text("UPDATE avito_accounts SET is_service = true WHERE avito_user_id = :uid"),
        {"uid": settings.smoke_avito_user_id},
    )


def downgrade() -> None:
    op.drop_column("avito_accounts", "is_service")
    op.drop_column("users", "is_service")
