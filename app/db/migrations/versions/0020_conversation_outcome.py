"""Чем закончилось обращение: выезд, отказ, спам (план работ 21, C1).

ЕДИНСТВЕННЫЙ ПУНКТ ПЛАНА, КОТОРЫЙ НЕЛЬЗЯ ДОДЕЛАТЬ ПОТОМ. Система нигде не
пишет, чем закончился разговор. Через месяц владелец спросит: сколько из
обращений стали выездами, какой канал окупается, кто из тринадцати приносит
деньги, а кто разговаривает. Ответа не будет — и задним числом его не собрать,
потому что переписка отвечает на вопрос «о чём говорили», но не на вопрос
«чем кончилось». Всё остальное в плане можно доделать после запуска; это —
нет, и потому оно идёт вперёд косметики.

СПРАВОЧНИК ЗАКРЫТЫЙ И КОРОТКИЙ — пять значений, согласованы с владельцем:
выезд назначен, отказ, не наш профиль, спам, нет ответа. Свободный текст здесь
был бы бесполезен: через месяц в нём окажется двести написаний одного и того
же, и сводку по нему не построить. Расширять справочник проще, чем разбирать
свалку.

СУММА НЕОБЯЗАТЕЛЬНА и хранится в копейках целым числом. Дробные рубли в
плавающей точке дают «1999.9999999» в отчёте за квартал; целые копейки не
дают. Заполняется она только у выездов и только когда известна — принуждать
диспетчера к цифре, которой он не знает, значит получить выдуманную.

Колонки NULL по построению: у всех уже накопленных диалогов результата нет и
взяться ему неоткуда. Пустое значение здесь честно означает «не спрашивали».
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020"
down_revision: str = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("outcome", sa.Text(), nullable=True))
    op.add_column("conversations", sa.Column("outcome_amount", sa.BigInteger(), nullable=True))
    op.add_column(
        "conversations", sa.Column("outcome_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "conversations",
        sa.Column(
            "outcome_by_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", name="fk_conversations_outcome_by_id_users"),
            nullable=True,
        ),
    )
    # Частичный индекс: строк с результатом со временем станет много, но
    # выборки по нему всегда идут «где результат проставлен».
    op.create_index(
        "ix_conversations_outcome",
        "conversations",
        ["outcome"],
        postgresql_where=sa.text("outcome IS NOT NULL"),
    )


def downgrade() -> None:
    # dev-only (08 §1.4 правило 5: в проде downgrade не применяется).
    op.drop_index("ix_conversations_outcome", table_name="conversations")
    op.drop_column("conversations", "outcome_by_id")
    op.drop_column("conversations", "outcome_at")
    op.drop_column("conversations", "outcome_amount")
    op.drop_column("conversations", "outcome")
