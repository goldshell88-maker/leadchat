"""avito_accounts — DESIGN §4.4."""

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, LargeBinary, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class AvitoAccount(Base):
    __tablename__ = "avito_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(Text, nullable=False)  # e.g. "LP-Москва"
    avito_user_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False)
    access_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)  # AES-256-GCM
    refresh_token_enc: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default="active"
    )  # active | needs_reauth | disabled
    webhook_secret: Mapped[str] = mapped_column(Text, nullable=False)
    # Аккаунт-заглушка регрессионного smoke (07 §6), а не канал заказчика.
    #
    # Через него никогда не уходит запрос в Авито (`status='disabled'`), но
    # диалог у него настоящий: `conversations.account_id` — NOT NULL, и
    # служебному диалогу `SMOKE-CONV` нужен аккаунт. Из-за этого он и попадал
    # в отчёты наравне с боевыми каналами: в «Разборе диалогов» строкой
    # «SMOKE (служебный)» и клиентом `SMOKE-CONV`.
    #
    # Признак ставит `app/cli.py seed-smoke` — тот же, кто аккаунт и заводит.
    # Отличать заглушку по названию или по `avito_user_id=1` значило бы
    # размазать одно правило по всем читателям.
    #: В какой лид-центр уходят заявки этого канала: `bt` / `kp` / `mnc`
    #: (миграция 0039). `None` — не выбран, и тогда канал заявки НЕ отдаёт:
    #: молчаливая отправка «куда-нибудь» означала бы заявку в чужой бизнес.
    #: Девять аккаунтов заказчика могут вести разные направления, общего
    #: ответа тут нет — выбирает человек на карточке канала.
    lead_src_key: Mapped[str | None] = mapped_column(Text)
    #: Источник («В95») — наша пометка, уходит в «Комментарий Партнера» заявки.
    #: Свой у каждого аккаунта: по нему в лид-центре видно, откуда клиент.
    lead_origin: Mapped[str | None] = mapped_column(Text)
    #: Номер партнёра, КАК ЕГО ЗНАЮТ ЛЮДИ («7», «723»), а не внутренний id: они
    #: почти нигде не совпадают, перевод делает расширение по справочнику живой
    #: формы. По этому же номеру решается кнопка «Отзыв».
    lead_partner_number: Mapped[str | None] = mapped_column(Text)
    #: Короткая ссылка на отзыв об этом аккаунте Авито. Вписывается руками один
    #: раз: сокращатель — чужой сервис, и ставить создание заявки в зависимость
    #: от его доступности незачем.
    review_url: Mapped[str | None] = mapped_column(Text)
    is_service: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Свои ключи приложения Авито — путь подключения «только ID и секрет»
    # (миграция 0018). Пусто — аккаунт подключён через согласие и обновляется
    # refresh-токеном; заполнено — переполучаем доступ этими ключами.
    client_id: Mapped[str | None] = mapped_column(Text)
    client_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    bot_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, ForeignKey("bots.id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: Когда история канала была ЗАГРУЖЕНА ЦЕЛИКОМ, и на какую глубину.
    #:
    #: Пусто — полной загрузки не было. По одному лишь Redis это не узнать: ход
    #: загрузки по завершении стирается, а множество разобранных чатов истекает
    #: через неделю, так что «загружено» и «не начиналось» выглядят одинаково.
    #: Сторожу догрузки (`scheduler.enqueue_backfill_supervise`) различать их
    #: обязательно: иначе он либо крутит законченную загрузку по кругу, либо не
    #: трогает незапущенную.
    history_loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history_loaded_depth: Mapped[str | None] = mapped_column(Text)
