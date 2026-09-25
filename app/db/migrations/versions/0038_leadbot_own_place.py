"""Лид-бот как отдельная сущность: системная запись и журнал его работы.

Решение владельца от 12 августа: «сам LeadBot это не совсем бот, я хочу чтобы у
него была своя собственная вкладка и настройки». До сих пор он настраивался
внутри редактора ботов — выбором «мозга» у обычного бота (`bots.ai_provider`,
миграция 0035). Это и правда неверно по смыслу: лид-бот — отдельный продукт на
своём сервере, со своим регламентом, своими ценами и своей ценой вызова.

ЧТО МЕНЯЕТСЯ СНАРУЖИ И ЧТО ОСТАЁТСЯ ВНУТРИ. Снаружи у лид-бота свой раздел
настроек; в списке ботов его больше нет. Внутри он по-прежнему отвечает на шаге
`ai_answer` сценария — и это не компромисс, а сознательный выбор: в движке
живут расписание, замолкание при ответе менеджера, handoff, лимиты и защита
«недоступность ИИ не блокирует доставку». Вынеси лид-бота из движка «ради
чистоты» — всё это пришлось бы написать заново, и каждая потерянная мелочь
означала бы сообщение, которого клиент не дождался.

Поэтому появляется `bots.is_system`: запись лид-бота есть в таблице, но её не
показывает список ботов и не открывает редактор. Признак, а не отдельная
таблица, потому что движку она обязана быть обычным ботом — иначе развилка «а
это точно бот?» расползётся по всему движку.

ДВЕ ЧАСТИ:

  1. `bots.is_system` — запись, которой нет в интерфейсе ботов;
  2. `leadbot_calls` — журнал обращений. Он и есть ответ на «чтобы было всё
     видно»: сегодня лид-бот отдаёт в `meta` слой, который ответил, эскалацию с
     причиной и сроком, готовность заявки, предупреждения и время — а LeadChat
     часть пишет в поток логов и теряет. Логи читает один человек в проекте
     через ssh; журнал читают из интерфейса.

Revision ID: 0038
Revises: 0037
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from app.models.types import JSONB

revision: str = "0038"
down_revision: str = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CALLS = "leadbot_calls"

#: Имена ограничений — ровно те, что даёт соглашение из `app/models/base.py`.
#: Расхождение здесь даёт `DROP CONSTRAINT`, не находящий цели (см. 0027).
CK_OUTCOME = f"ck_{CALLS}_outcome"

#: Чем кончилось обращение к лид-боту — с точки зрения КЛИЕНТА, а не HTTP.
#: Двухсотый ответ, выброшенный из-за низкой уверенности, для клиента такое же
#: молчание, как таймаут, и складывать их в одно «успешно» значит потерять
#: единственное, что здесь важно.
OUTCOMES = (
    "sent",  # текст ушёл клиенту
    "hint",  # текст лёг подсказкой оператору, клиент не получил ничего
    "bridge",  # текст ушёл клиенту, следом диалог передан человеку
    "handoff",  # текста не было, диалог передан человеку
    "low_confidence",  # ответ был, но уверенность ниже порога — выброшен целиком
    "unavailable",  # лид-бот не ответил: таймаут, сеть, отказ, чужой формат
)


def upgrade() -> None:
    # --- 1. Системная запись бота ------------------------------------------
    op.add_column(
        "bots",
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # --- 2. Журнал работы ---------------------------------------------------
    op.create_table(
        CALLS,
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("account_id", sa.Uuid(), nullable=True),
        # Сквозной идентификатор: его же кладёт в свой ответ лид-бот
        # (`meta.request_id`). По нему одну и ту же работу видно с обеих сторон —
        # без него сопоставлять пришлось бы по времени и тексту.
        sa.Column("request_id", sa.Text(), nullable=True),
        # Вопрос и ответ хранятся ОБРЕЗАННЫМИ (см. `app/services/leadbot_log.py`).
        # Журнал отвечает на «почему бот ответил так», а не заменяет переписку:
        # она уже лежит в `messages`, и вторая её копия — это второе место, где
        # переписка клиентов способна утечь.
        sa.Column("question", sa.Text(), nullable=True),
        sa.Column("reply", sa.Text(), nullable=True),
        # Кто именно ответил на той стороне: «роутер» (детерминированное
        # правило регламента), «модель», «недоступен». Главный вопрос владельца
        # к любому ответу — «это регламент или он сам придумал».
        sa.Column("layer", sa.Text(), nullable=True),
        sa.Column("flag", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=True),
        sa.Column("needs_operator", sa.Boolean(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("escalation_reason", sa.Text(), nullable=True),
        sa.Column("escalation_label", sa.Text(), nullable=True),
        sa.Column("escalation_deadline_min", sa.Integer(), nullable=True),
        sa.Column("lead_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("warnings", JSONB, nullable=False, server_default="[]"),
        sa.Column("ms", sa.Integer(), nullable=True),
        # Причина отказа человеческими словами. Пусто у удачного обращения.
        sa.Column("error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=f"pk_{CALLS}"),
        sa.CheckConstraint(
            "outcome IN (" + ",".join(f"'{v}'" for v in OUTCOMES) + ")",
            name=CK_OUTCOME,
        ),
        # Диалог мог быть удалён (чистка старых), а запись журнала — остаться:
        # она про работу лид-бота, а не про переписку. Поэтому SET NULL, а не
        # CASCADE: иначе вместе с уборкой диалогов из журнала исчезла бы
        # половина истории решений, и «сколько раз бот ошибся в июле» перестало
        # бы иметь ответ.
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=f"fk_{CALLS}_conversation_id_conversations",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["account_id"],
            ["avito_accounts.id"],
            name=f"fk_{CALLS}_account_id_avito_accounts",
            ondelete="SET NULL",
        ),
    )
    # Экран журнала — это «последние N, свежие сверху», с отбором по каналу и по
    # тому, чем кончилось. Один индекс по времени закрывает главный порядок;
    # частичный по неудачам — вопрос «покажи только то, где что-то пошло не так»,
    # который задают чаще всего и на который иначе пришлось бы читать всю таблицу.
    op.create_index(f"ix_{CALLS}_created_at", CALLS, [sa.text("created_at DESC")])
    op.create_index(
        f"ix_{CALLS}_trouble",
        CALLS,
        [sa.text("created_at DESC")],
        postgresql_where=sa.text("outcome IN ('unavailable','low_confidence')"),
    )
    op.create_index(f"ix_{CALLS}_conversation_id", CALLS, ["conversation_id"])


def downgrade() -> None:
    op.drop_index(f"ix_{CALLS}_conversation_id", table_name=CALLS)
    op.drop_index(f"ix_{CALLS}_trouble", table_name=CALLS)
    op.drop_index(f"ix_{CALLS}_created_at", table_name=CALLS)
    op.drop_table(CALLS)
    op.drop_column("bots", "is_system")
