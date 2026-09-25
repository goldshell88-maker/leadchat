"""Заявки сотрудников администратору (14 §2.2, §4 «Интерфейс программный»).

    POST /support/password-reset   без авторизации, со страницы входа
    POST /support/message          с авторизацией, из профиля

Обе ручки ничего не показывают сотруднику о состоянии системы и не отдают
данных: они только кладут заявку в центр уведомлений администратора.

Слово «заявка», а не «обращение», — по словарю 10 §7.2; подробности в шапке
``app/services/support.py``.

Права (DESIGN §5.1): у сброса пароля прав нет по построению — человек как раз
не может войти. Сообщение администратору доступно ЛЮБОЙ авторизованной роли,
включая observer: право писать администратору не выдаётся, оно есть у всех, кто
работает в системе.
"""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, get_redis
from app.core.errors import ACCOUNT_DISABLED_MESSAGE, ApiError
from app.models import User
from app.services.support import request_password_reset, submit_admin_message

router = APIRouter()

# Один и тот же ответ на заявку о сбросе пароля — независимо от того, есть
# такая учётная запись или нет (14 §2.2). Список сотрудников — это то, кто у
# нас работает; неавторизованная ручка не имеет права его подтверждать даже
# по одному адресу за раз.
PASSWORD_RESET_ANSWER = (
    "Заявка принята. Если такая учётная запись есть, администратор получит "
    "её и вышлет новую ссылку для входа."
)
MESSAGE_ANSWER = "Сообщение отправлено администратору."


class PasswordResetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # str, а не EmailStr — как в schemas/auth.py: строгий валидатор отвергает
    # служебные домены (.local), а формат мы здесь всё равно не проверяем
    # всерьёз: несуществующий адрес обязан получить тот же ответ, что и живой.
    email: str = Field(min_length=3, max_length=320)

    @field_validator("email")
    @classmethod
    def _looks_like_email(cls, value: str) -> str:
        value = value.strip()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            # Текст валидатора доезжает до человека как message ответа
            # (app/core/errors.py), поэтому он написан по образцу 10 §7.3:
            # сначала что не так, потом — какой вид нужен.
            raise ValueError("Похоже, это не адрес почты. Формат: имя@домен")
        return value


class AdminMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=3, max_length=200)
    text: str = Field(min_length=1, max_length=2000)

    @field_validator("subject", "text")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Заполните это поле — из одних пробелов заявки не бывает")
        return value


class SupportAcceptedOut(BaseModel):
    status: str = "accepted"
    message: str


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


@router.post("/support/password-reset", response_model=SupportAcceptedOut, status_code=202)
async def password_reset_request(
    body: PasswordResetIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> SupportAcceptedOut:
    """«Не помню пароль» → заявка администратору (14 §2.2).

    Ответ одинаковый всегда: и код, и текст. Единственное отличимое состояние —
    429 при исчерпанном лимите, и оно не зависит от существования учётки:
    счётчики тикают до похода в базу (см. ``request_password_reset``).
    """
    await request_password_reset(db, redis, email=body.email, ip=_client_ip(request))
    return SupportAcceptedOut(message=PASSWORD_RESET_ANSWER)


@router.post("/support/message", response_model=SupportAcceptedOut, status_code=202)
async def admin_message(
    body: AdminMessageIn,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> SupportAcceptedOut:
    """Форма «Написать администратору» из профиля: 5 сообщений в час."""
    if not user.is_active:  # страховка: get_current_user уже отсекает
        # Текст обязателен: без него сработал бы общий «нужны права выше
        # ваших», а права здесь ни при чём — учётку выключили.
        raise ApiError("forbidden", status=403, message=ACCOUNT_DISABLED_MESSAGE)
    await submit_admin_message(db, redis, user=user, subject=body.subject, text=body.text)
    return SupportAcceptedOut(message=MESSAGE_ANSWER)
