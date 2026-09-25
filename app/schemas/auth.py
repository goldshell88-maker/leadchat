"""Request/response models for auth endpoints (01 §2)."""

import uuid
from typing import Annotated

from pydantic import BaseModel, Field, field_validator

from app.services.user_ref import normalize_department

# NOTE: email is a plain str on purpose: strict EmailStr (email-validator)
# rejects special-use TLDs, while internal accounts like smoke@leadchat.local
# (08 §7 seed-smoke) must be able to log in. Format is enforced where users
# are created, not at login.


class LoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str
    remember: bool = False


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    #: Свой отдел — нужен ЛЕНТЕ, а не самому человеку (04.09).
    #:
    #: Собственная реплика рисуется в ленте до ответа сервера
    #: (`useSendMessage` кладёт её оптимистично), и подпись автора берётся
    #: из сессии. Без отдела здесь строка успевала бы показать «Иванов», а
    #: через полсекунды смениться на «Иванов (ОКК)» — мигание там, где
    #: человек смотрит на только что отправленный текст.
    department: str | None = None

    model_config = {"from_attributes": True}

    # Чистка стоит в МОДЕЛИ, а не в ручках: вход собирает эту же модель через
    # `model_validate(user)` прямо из строки БД, и отдел «Диспетчер  МНЧ» с
    # двумя пробелами уехал бы мимо любой чистки в обработчике.
    @field_validator("department")
    @classmethod
    def _чистый_отдел(cls, значение: str | None) -> str | None:
        return normalize_department(значение)


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut


class MeOut(UserOut):
    permissions: list[str]
    #: Личные горячие клавиши: действие → сочетания. Пусто — всё по умолчанию.
    #: Хранятся ТОЛЬКО отличия, поэтому правка умолчаний доезжает до тех, кто это
    #: действие не трогал, а не застревает у каждого своей копией.
    hotkeys: dict[str, list[str]] = {}


#: Имя действия: короткий код вроде `thread.reply`.
#:
#: Псевдонимы объявлены СНАРУЖИ модели намеренно: внутри тела класса Pydantic
#: принимает их за необъявленные поля и отказывается собирать модель.
_Действие = Annotated[str, Field(max_length=40)]
#: Каноническая запись сочетания: `Mod+Shift+KeyT`, `Alt+Digit0`, `Escape`.
_Сочетание = Annotated[str, Field(max_length=40)]


class HotkeysRequest(BaseModel):
    """Тело `PUT /auth/me/hotkeys`. Пустой объект — «вернуть всё как было».

    ⚠ ПОТОЛОК РАЗМЕРА ОБЯЗАТЕЛЕН. Поле уходит в JSONB и читается на КАЖДЫЙ вход в
    систему вместе с профилем; без ограничения сюда однажды приедет мегабайт, и его
    будут возить туда-сюда все тринадцать человек каждое утро. Восемнадцать действий
    по несколько сочетаний в реальность помещаются с большим запасом.

    ⚠ И ОГРАНИЧИВАТЬ НАДО СОДЕРЖИМОЕ, А НЕ ТОЛЬКО ЧИСЛО ПАР (28.08). Здесь стоял
    один `max_length=64` на самом словаре, а в Pydantic это число ПАР, не байт:
    ключи, сами сочетания и длина списков не ограничивались ничем. Проверено на
    живой схеме — тело в пять мегабайт принималось и уезжало в JSONB. Обещание в
    абзаце выше не выполнялось, а сторож в тестах проверял ровно то же, что и
    докстрока, но только количество, и оставался зелёным.

    Границы взяты с запасом от реальности: имя действия — короткий код
    (`thread.reply`), сочетание — каноническая запись вида `Mod+Shift+KeyT`,
    и больше четырёх записей на одно действие человеку не нужно.
    """

    hotkeys: dict[_Действие, Annotated[list[_Сочетание], Field(max_length=4)]] = Field(
        default_factory=dict, max_length=64
    )


class InviteInfoOut(BaseModel):
    email: str
    full_name: str


class InviteAcceptRequest(BaseModel):
    token: str
    password: str = Field(min_length=10)


class ChangePasswordRequest(BaseModel):
    """Смена СВОЕГО пароля (требование заказчика от 7 августа).

    Текущий пароль спрашивается обязательно, и это не формальность: сессия
    живёт долго, а незапертый ноутбук — обычное дело в офисе. Без проверки
    любой, кто подошёл к чужому столу, менял бы пароль коллеге и запирал его
    из системы.
    """

    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=10)


class ChangeNameRequest(BaseModel):
    """Своё имя. Оно видно клиенту в подписи сообщения и коллегам в списке —
    поэтому пустое и однобуквенное не принимаем."""

    full_name: str = Field(min_length=2, max_length=120)
