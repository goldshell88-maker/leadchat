"""Ключи Авито отделены от людей: у человека их законно несколько.

ЗАЧЕМ. Сегодня строка `clients` — одновременно «человек» и «ключ Авито», склеенные
ограничением `UNIQUE(channel, external_id)`. Отсюда всё, на что жалуется владелец:

* сообщение без автора (служебное событие Авито) заводит карточку-заглушку
  `chat:<id чата>` — «личность неизвестна». Приписать ей потом настоящий
  идентификатор НЕЛЬЗЯ: строка одна, ключ один, ограничение не пустит. Карточка
  остаётся «Клиентом» навсегда, и её объединяют руками;
* на снимке карточки такие заглушки печатаются вперемешку с настоящим
  идентификатором, и оператор не может понять, что два ключа из трёх не значат
  ничего.

ЧТО ДАЁТ ТАБЛИЦА. У человека законно несколько ключей — по одному на каждый наш
аккаунт плюс раскрытые заглушки. Приписать ключ становится ДОБАВЛЕНИЕМ СТРОКИ, а не
переименованием, и упирается оно уже ни во что.

⚠ КЛЮЧ УНИКАЛЕН ВМЕСТЕ С АККАУНТОМ, И ЭТО ГЛАВНОЕ РЕШЕНИЕ ЭТОЙ МИГРАЦИИ.
`UNIQUE(channel, kind, value, account_id)`, а не без аккаунта. Допущение «author_id
Авито общий для всех наших аккаунтов» никогда не проверялось: в спецификации у
`Chat.users[].id` стоит «Обратите внимание на хэширование» — предупреждение без
объяснения. Если идентификатор свой у каждого аккаунта, ключ без `account_id`
соединил бы РАЗНЫХ людей. Опыт описан в docs/44 §5; после него `account_id` уходит из
ключа одной строкой, и межканальная склейка становится законной, а не молчаливой.

⚠ `account_id` NULL — законное значение, а не пробел: так помечены ключи, для которых
аккаунт неизвестен (строки, перенесённые этой миграцией). NULL в Postgres не участвует
в UNIQUE, поэтому перенесённые ключи ограничение не нарушают и друг другу не мешают.

СОВМЕСТИМОСТЬ. `clients.external_id` остаётся на месте и по-прежнему единственный, по
кому ищет приём сообщений: старый код о таблице не знает и работать не перестаёт.
Миграция только добавляет.
"""

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "client_identities",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "client_id",
            sa.Uuid(),
            sa.ForeignKey("clients.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("channel", sa.Text(), nullable=False, server_default="avito"),
        # 'avito_user' — настоящий идентификатор человека у Авито.
        # 'chat_stub'  — наша заглушка «личность неизвестна», ключом человека не является.
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Uuid(), sa.ForeignKey("avito_accounts.id", ondelete="SET NULL")),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        # Заглушка раскрыта: личность выяснена, ключ больше не показываем как
        # идентификатор человека. NULL у настоящих ключей и у нераскрытых заглушек.
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("kind IN ('avito_user', 'chat_stub')", name="kind"),
        sa.UniqueConstraint("channel", "kind", "value", "account_id", name="key"),
    )
    # Поиск идёт по паре «вид + значение» на горячем пути приёма сообщения.
    op.create_index(
        "ix_client_identities_lookup", "client_identities", ["channel", "kind", "value"]
    )
    op.create_index("ix_client_identities_client", "client_identities", ["client_id"])

    # Перенос: по одной строке на каждую существующую карточку. Заглушки узнаются
    # по форме ключа `chat:<id>` — она выбрана так, чтобы не совпасть с числовым
    # идентификатором Авито ни при каких данных.
    op.execute(
        """
        -- `first_seen_at` — now(): в `clients` нет колонки с моментом заведения
        -- (проверено по схеме), а выдумывать дату «когда впервые увидели ключ» из
        -- ничего значило бы записать в базу неправду. Перенесённые строки честно
        -- помечены моментом переноса.
        INSERT INTO client_identities (id, client_id, channel, kind, value, first_seen_at)
        SELECT gen_random_uuid(),
               c.id,
               c.channel,
               CASE WHEN c.external_id LIKE 'chat:%' THEN 'chat_stub' ELSE 'avito_user' END,
               CASE WHEN c.external_id LIKE 'chat:%'
                    THEN substring(c.external_id from 6)
                    ELSE c.external_id END,
               now()
        FROM clients c
        """
    )


def downgrade() -> None:
    op.drop_index("ix_client_identities_client", table_name="client_identities")
    op.drop_index("ix_client_identities_lookup", table_name="client_identities")
    op.drop_table("client_identities")
