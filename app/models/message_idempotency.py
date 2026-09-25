"""Второй эшелон защиты от дублей исходящих (#25).

Пара «диалог + идентификатор от клиента» — первичный ключ. Строка пишется в
ТОЙ ЖЕ транзакции, что и само сообщение, поэтому вторая попытка с тем же
идентификатором упирается в нарушение ключа независимо от того, что
происходит с Redis и в какой момент она подоспела.

ПОЧЕМУ ОТДЕЛЬНАЯ ТАБЛИЦА, А НЕ ИНДЕКС НА `messages`. Такой индекс там уже был
и не работал: `messages` партиционирована по времени, а уникальный индекс
партиционированной таблицы обязан включать ключ партиционирования. У повторной
попытки время создания другое — тройка снова уникальна, и дубль проходит
насквозь. Именно партиционирование и обессмыслило прежнюю защиту, поэтому эта
таблица НЕ партиционирована.

Строки живут дольше, чем ключ в Redis (сутки), и это правильно: истёкший ключ
не должен открывать дорогу дублю. Чистить их можно ночным заданием по
`created_at`, когда таблица вырастет, — но не раньше, чем появится причина.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class MessageIdempotency(Base):
    __tablename__ = "message_idempotency"

    conversation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    client_message_id: Mapped[str] = mapped_column(Text, primary_key=True)
    #: Какое сообщение победило. По нему повтор возвращает то же самое, а не
    #: просто отказ: для клиента повторная отправка обязана выглядеть успешной.
    message_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
