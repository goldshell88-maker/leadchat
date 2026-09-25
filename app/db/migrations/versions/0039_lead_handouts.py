"""Автозаявки: куда канал заводит заявки и что мы уже отдали расширению.

ЗАЧЕМ. У владельца есть расширение «Автозаявки» — оно само ходит за лидами и
заводит заявки в трёх лид-центрах (БТ / КП / МНЧ). Со стороны LeadChat нужны две
ручки: отдать лиды и принять результат. Эта миграция готовит под них базу.

ЧТО СЧИТАЕТСЯ ЛИДОМ (решение владельца от 13 августа): диалог, которому ЧЕЛОВЕК
поставил итог «Выезд», и у клиента известен телефон. Не «любой диалог с
телефоном»: в лид-центр поехали бы заявки на тех, кто спросил цену и пропал, а
каждая лишняя — это возможный выезд мастера впустую.

ДВЕ ЧАСТИ.

  1. `avito_accounts.lead_src_key` — в какой из трёх лид-центров уходят заявки
     этого канала. Девять аккаунтов заказчика могут вести разные направления,
     и общего ответа тут нет. Канал без выбора заявки НЕ отдаёт — молчаливая
     отправка «куда-нибудь» означала бы заявку в чужой бизнес.

  2. `lead_handouts` — что отдали, когда и чем кончилось. Без этой таблицы
     расширение получало бы один и тот же диалог на каждом опросе, а мы бы не
     знали, создалась заявка или нет.

Revision ID: 0039
Revises: 0038
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0039"
down_revision: str = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "lead_handouts"

#: Три лид-центра владельца. Значения — те же коды, что понимает расширение
#: (`srcKey` в его протоколе), и переименовать их в «красивые» нельзя: по ним
#: расширение выбирает домен, куда идти.
SRC_KEYS = ("bt", "kp", "mnc")

#: Чем кончилась попытка завести заявку — словами расширения. Список закрыт,
#: потому что по нему на экране разбирают, что случилось; чужое значение здесь
#: означало бы строку «непонятно что» вместо ответа.
DECISIONS = (
    "new",  # клиента не было — заведены и клиент, и заявка
    "existing-customer",  # клиент был, живых заявок нет — заявка на него
    "blocked",  # по номеру уже висит живая заявка, ничего не создано
    "recovered",  # связь оборвалась, но проверка показала: заявка создана
    "error",  # лид-центр отклонил
)


def upgrade() -> None:
    op.add_column("avito_accounts", sa.Column("lead_src_key", sa.Text(), nullable=True))
    op.create_check_constraint(
        "lead_src_key",
        "avito_accounts",
        "lead_src_key IS NULL OR lead_src_key IN (" + ",".join(f"'{v}'" for v in SRC_KEYS) + ")",
    )

    op.create_table(
        TABLE,
        sa.Column("id", sa.Uuid(), nullable=False),
        # ОДНА ЗАПИСЬ НА ДИАЛОГ, и это стережёт база. Диалог — он же лид: у
        # одного обращения не бывает двух заявок, а если бы бывало, дубль в
        # лид-центре стоил бы второго выезда мастера к тому же человеку.
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("handed_at", sa.DateTime(timezone=True), nullable=False),
        # Куда отдавали. Копия из канала на момент выдачи: канал могут
        # переназначить, а знать надо, куда заявка ушла НА САМОМ ДЕЛЕ.
        sa.Column("src_key", sa.Text(), nullable=False),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.Text(), nullable=True),
        # Номер заявки в лид-центре — текстом: это чужой идентификатор, и
        # закладываться на то, что он навсегда останется числом, незачем.
        sa.Column("request_id", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{TABLE}"),
        sa.UniqueConstraint("conversation_id", name=f"uq_{TABLE}_conversation_id"),
        sa.CheckConstraint(
            "src_key IN (" + ",".join(f"'{v}'" for v in SRC_KEYS) + ")",
            name="src_key",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN (" + ",".join(f"'{v}'" for v in DECISIONS) + ")",
            name="decision",
        ),
        # Диалог могли удалить уборкой старых — запись о заявке остаётся: она
        # про то, что мы отдали наружу, и по ней сверяют с лид-центром.
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=f"fk_{TABLE}_conversation_id_conversations",
            ondelete="CASCADE",
        ),
    )
    # Главный запрос экрана — «последние выдачи, свежие сверху»; главный запрос
    # выдачи — «что ещё не подтверждено». Второй индекс частичный: неподтверждённых
    # всегда единицы, и читать ради них всю таблицу незачем.
    op.create_index(f"ix_{TABLE}_handed_at", TABLE, [sa.text("handed_at DESC")])
    op.create_index(
        f"ix_{TABLE}_unacked",
        TABLE,
        ["handed_at"],
        postgresql_where=sa.text("acked_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(f"ix_{TABLE}_unacked", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_handed_at", table_name=TABLE)
    op.drop_table(TABLE)
    # ⚠ ИМЯ БЕЗ ПРИСТАВКИ. Соглашение из `app/models/base.py` добавляет
    # `ck_avito_accounts_` само; впиши её здесь руками — и получится
    # `ck_avito_accounts_ck_avito_accounts_lead_src_key`, то есть DROP не
    # найдёт цели и откат упадёт. Ровно так это и поймали три интеграционных
    # теста, которые гоняют upgrade → downgrade → upgrade.
    op.drop_constraint("lead_src_key", "avito_accounts", type_="check")
    op.drop_column("avito_accounts", "lead_src_key")
