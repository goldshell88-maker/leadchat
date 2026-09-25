"""Владелец диалога в момент предложения передачи: `conversations.transfer_from_id`.

Передать чужой диалог может не только его хозяин, но и администратор,
руководитель или коллега с `conversations:manage`. Принятие сверяло нынешнего
владельца с тем, КТО предлагал (`transfer_by_id`), и у такой передачи всегда
отвечало «предложение устарело». Сверять нужно с тем, кто владел диалогом,
когда его предложили.

Висящие предложения получают нынешнего владельца: пока предложение живо,
владелец не менялся (смена владельца снимает предложение).

Expand-safe: колонка NULL-допустимая, старый код её не читает.
"""

import sqlalchemy as sa
from alembic import op

revision = "0085"
down_revision = "0084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("transfer_from_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=True),
    )
    op.execute(
        "UPDATE conversations SET transfer_from_id = assignee_id WHERE transfer_to_id IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("conversations", "transfer_from_id")
