"""Адрес клиента: колонки карточки и таблица распознанных адресов.

⚠ РАСПОЗНАННЫЙ АДРЕС В КАРТОЧКУ НЕ ПИШЕТСЯ НИКОГДА. Решение владельца от 12
августа про телефон здесь действует с ещё большим основанием: ошибка в номере
стоит звонка постороннему, ошибка в адресе — выезда мастера не туда, то есть
потерянного дня бригады и клиента, который ждал впустую. Поэтому `clients.address`
заполняет только человек, а всё вычитанное живёт отдельной таблицей со статусом.

ЗАЧЕМ ТАБЛИЦА, А НЕ ПАРА КОЛОНОК. Ровно те же три причины, что у телефона
(`client_phone_candidates`, миграция 0037): адресов у человека законно несколько
(свой и мамин), отклонённое обязано помниться, а у находки есть происхождение —
из какого сообщения и когда.

⚠ КЛЮЧ УНИКАЛЬНОСТИ — «улица, дом» БЕЗ КВАРТИРЫ. Замер боя: в 23,5 % адресных
диалогов адрес размазан по нескольким репликам («Ленина 5» → «кв 3, второй
подъезд»). С квартирой в ключе каждый обрывок стал бы отдельным вопросом
оператору, а пропускная способность такой очереди уже измерена на телефоне:
5 534 предложения за 30 дней и 24 решения. Части дописываются в ту же строку.

ЗАМЕР, ОПРАВДЫВАЮЩИЙ ТАБЛИЦУ (бой, 30 дней, разбор `address_parse`): 107 828
входящих от клиентов, адрес нашёлся 6 396 раз, из них уровень A — 3 613 находок
в 3 355 диалогах. Части: квартира 1 888, подъезд 871, этаж 649, домофон 108.

ЧЕГО ЗДЕСЬ НЕТ. Правки существующих строк: обе колонки карточки заводятся
пустыми, таблица — пустой. Заполнять их будет приём входящих и решения
операторов, а разовый пересчёт по истории — отдельной командой и отдельным
решением.
"""

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0070"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("clients", sa.Column("address", sa.Text(), nullable=True))
    op.add_column("clients", sa.Column("address_conversation_id", sa.Uuid(), nullable=True))
    op.add_column("clients", sa.Column("address_set_by_id", sa.Uuid(), nullable=True))
    op.add_column("clients", sa.Column("address_set_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "fk_clients_address_conversation_id_conversations",
        "clients",
        "conversations",
        ["address_conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_clients_address_set_by_id_users",
        "clients",
        "users",
        ["address_set_by_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.create_table(
        "client_address_candidates",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("message_id", sa.Uuid(), nullable=True),
        sa.Column("message_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("street", sa.Text(), nullable=False),
        sa.Column("house", sa.Text(), nullable=False),
        sa.Column("office", sa.Text(), nullable=True),
        sa.Column("entrance", sa.Text(), nullable=True),
        sa.Column("floor", sa.Text(), nullable=True),
        sa.Column("intercom", sa.Text(), nullable=True),
        sa.Column("raw", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_by_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(
            ["client_id"],
            ["clients.id"],
            name="fk_client_address_candidates_client_id_clients",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_client_address_candidates_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_id"],
            ["users.id"],
            name="fk_client_address_candidates_resolved_by_id_users",
            ondelete="SET NULL",
        ),
        sa.UniqueConstraint(
            "client_id", "value", name="uq_client_address_candidates_client_id_value"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'accepted', 'rejected')",
            name="ck_client_address_candidates_status",
        ),
        sa.CheckConstraint("level IN ('A', 'B', 'C')", name="ck_client_address_candidates_level"),
    )
    # Дочерняя сторона каскада от диалога: без индекса удаление диалога ищет
    # свои строки перебором всей таблицы.
    op.create_index(
        "ix_client_address_candidates_conversation",
        "client_address_candidates",
        ["conversation_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_client_address_candidates_conversation", table_name="client_address_candidates"
    )
    op.drop_table("client_address_candidates")
    op.drop_constraint("fk_clients_address_set_by_id_users", "clients", type_="foreignkey")
    op.drop_constraint(
        "fk_clients_address_conversation_id_conversations", "clients", type_="foreignkey"
    )
    op.drop_column("clients", "address_set_at")
    op.drop_column("clients", "address_set_by_id")
    op.drop_column("clients", "address_conversation_id")
    op.drop_column("clients", "address")
