"""Схемы управления сотрудниками — 01 §3.1–3.5, экран «Команда» 11 §4.2.

Три решения, которые видно прямо в наборе моделей:

* **`email` — обычная строка с собственной регуляркой, а не `EmailStr`.**
  Причина та же, что в `schemas/auth.py`: `email-validator` отвергает
  служебные TLD, а smoke-пользователь живёт на `…​.local` (07 §6). Формат
  проверяем там, где сотрудник создаётся, — здесь.
* **Время отдаём строкой в ISO-8601 с `Z`** (01 §1.5). `datetime` из SQLite
  приезжает наивным, из Postgres — с зоной; сериализуй его pydantic сам —
  и формат `created_at` тихо разъехался бы между юнитами и продом.
* **Мутации возвращают конверт `{"user": {...}}`**, как 201 у приглашения
  (01 §3.2). Один разбор ответа на все пять ручек вместо двух форматов.
"""

import re
import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator

Role = Literal["admin", "head", "manager", "observer"]

# Достаточно строгая, чтобы отсечь опечатку в адресе, и достаточно мягкая,
# чтобы не спорить с реальными доменами. Полная проверка адреса возможна
# только доставкой письма, а почтового сервиса в MVP нет (01 §3.2).
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

MAX_EMAIL = 320
MAX_FULL_NAME = 200
# Отдел — короткая подпись в списке, а не описание. У заказчика самое длинное
# название — «СТАРШИЕ - ЧАТЫ».
MAX_DEPARTMENT = 100
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


class UserAdminOut(BaseModel):
    """Строка таблицы «Сотрудники» (01 §3.1, экран 11 §4.2)."""

    id: uuid.UUID
    email: str
    full_name: str
    role: str
    is_active: bool
    # 🟢/⚪ в таблице. У отключённого всегда false: presence живёт с TTL и
    # ещё минуту после обрыва сокета показывал бы уволенного «в сети».
    is_online: bool
    # «на месте» | «отошёл» | null (не в сети). Отдельно от `is_online`:
    # отошедший остаётся видимым и доступным для передачи, но раздачу
    # обращений мимо него система ведёт сама (#34).
    presence: str | None = None
    # «ждёт пароля 🔗» — приглашён, но ссылку ещё не использовал (01 §3.1).
    invite_pending: bool
    # Ведёт ли человек диалоги (7.4) — отдельно от роли. Администратор может
    # администрировать и не работать с обращениями: у заказчика в Jivo так
    # настроен целый отдел «СТАРШИЕ - ЧАТЫ».
    handles_conversations: bool
    department: str | None
    # Личный цвет (#RRGGBB, 17.08). ⚠ БЕЗ ЭТОЙ СТРОКИ ЦВЕТ БЫЛ МЁРТВ: pydantic
    # с extra='ignore' молча выбрасывал ключ из ответа, и «Команда» показывала
    # несохранённый цвет, хотя в базе он лежал. Каждое новое поле обязано
    # появляться в схеме ОТВЕТА, а не только в модели и ручке (аудит 18.08).
    color: str | None = None
    created_at: str


class PageOut(BaseModel):
    limit: int
    offset: int
    total: int


class UsersPageOut(BaseModel):
    items: list[UserAdminOut]
    page: PageOut


class UserEnvelopeOut(BaseModel):
    user: UserAdminOut


class InviteIssuedOut(UserEnvelopeOut):
    """Ответ приглашения / перевыпуска ссылки / сброса пароля (01 §3.2–3.3).

    `invite_url` показывается администратору ОДИН раз — почтового сервиса
    в MVP нет, ссылку передаёт человек человеку (11 §4.2).
    """

    invite_url: str
    invite_expires_at: str


class UserInviteIn(BaseModel):
    email: str = Field(min_length=3, max_length=MAX_EMAIL)
    full_name: str = Field(min_length=1, max_length=MAX_FULL_NAME)
    role: Role

    @model_validator(mode="after")
    def _check(self) -> "UserInviteIn":
        self.email = self.email.strip().lower()
        self.full_name = self.full_name.strip()
        if not EMAIL_RE.match(self.email):
            raise ValueError("Некорректный адрес почты")
        if not self.full_name:
            raise ValueError("Имя не может быть пустым")
        return self


class UserPatchIn(BaseModel):
    """Имя, почта и/или роль (01 §3.4). Пустое тело — `400 validation_error`:
    молча отвечать 200 на запрос, который ничего не меняет, — враньё."""

    full_name: str | None = Field(default=None, min_length=1, max_length=MAX_FULL_NAME)
    # ⚠ ПОЧТА — ЭТО ЛОГИН, А НЕ ПРОСТО ПОЛЕ КАРТОЧКИ. Менять её умеет только
    # администратор, и после смены человек входит уже по новому адресу. Правила
    # те же, что при приглашении: обрезаем, приводим к нижнему регистру и
    # сверяем с той же `EMAIL_RE` — иначе «корректный адрес» значил бы разное
    # на создании и на правке.
    email: str | None = Field(default=None, min_length=3, max_length=MAX_EMAIL)
    role: Role | None = None
    handles_conversations: bool | None = None
    # Пустая строка — «убрать отдел». Отличима от «не меняли» (None), потому
    # что снять отдел человек должен уметь так же, как и поставить.
    department: str | None = Field(default=None, max_length=MAX_DEPARTMENT)
    # Пустая строка — «снять цвет»; иначе строго #RRGGBB.
    color: str | None = Field(default=None, pattern=r"^$|^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def _check(self) -> "UserPatchIn":
        if self.full_name is not None:
            self.full_name = self.full_name.strip()
            if not self.full_name:
                raise ValueError("Имя не может быть пустым")
        if self.email is not None:
            self.email = self.email.strip().lower()
            if not EMAIL_RE.match(self.email):
                raise ValueError("Некорректный адрес почты")
        if self.department is not None:
            self.department = self.department.strip()
        if (
            self.full_name is None
            and self.role is None
            and self.handles_conversations is None
            and self.department is None
            # ⚠ 17.08: без цвета в этом списке запрос «поменять только цвет»
            # отклонялся как пустой — «сервер не принял значение». Каждое
            # новое поле обязано появляться и здесь.
            and self.color is None
            # почта — такое же изменяемое поле, как и остальные выше
            and self.email is None
        ):
            raise ValueError("Нужно передать хотя бы одно изменяемое поле")
        return self


class SetPasswordIn(BaseModel):
    """Пароль, который администратор задаёт сотруднику напрямую.

    ⚠ ДЛИНА ЗДЕСЬ НЕ ОГРАНИЧЕНА СНИЗУ — РЕШЕНИЕ ВЛАДЕЛЬЦА 27.08. Раньше стояло
    десять символов, как при приёме приглашения. Разница между двумя случаями в
    том, кто набирает пароль: приглашение сотрудник заполняет сам и живёт с этим
    паролем дальше, а здесь администратор задаёт временный вход руками — чаще
    всего чтобы человек попал в панель прямо сейчас. Планка там оставлена
    (`auth.py`: и приём приглашения, и смена своего пароля), здесь снята.

    Пустую строку всё равно не принимаем: пароль из нуля знаков — это не
    «простой пароль», а вход без пароля, и включать такое молча нельзя.
    """

    password: str = Field(min_length=1, max_length=128)
