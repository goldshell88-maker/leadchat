"""templates (quick replies) — DESIGN §4.4."""

import uuid

from sqlalchemy import ForeignKey, Integer, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Template(Base):
    __tablename__ = "templates"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id")
    )  # NULL = shared
    title: Mapped[str] = mapped_column(Text, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    folder: Mapped[str | None] = mapped_column(Text)
    #: Сколько раз заготовку применили. Двигает её вверх в подсказке.
    #:
    #: ⚠ ОБЩИЙ, А НЕ ПЕРСОНАЛЬНЫЙ — осознанное упрощение: тринадцать диспетчеров
    #: одной компании отвечают на одни и те же вопросы, и «ходовое у команды»
    #: им ближе, чем «ходовое лично у меня» на пустой истории.
    #:
    #: Заготовки, выращенные из истории диалогов, стартуют не с нуля: у них
    #: стоит число, сколько раз фразу набрали руками до появления заготовки.
    #: Иначе первую неделю подсказка сортировала бы по алфавиту — то есть была
    #: бы бесполезна ровно тогда, когда к ней привыкают.
    used_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
