"""Схемы API ботов — 01 §8, форматы `scenario`/`schedule` — 02 §1.1–1.4, §2.5.

Разделение ответственности осознанное:

* **Pydantic** проверяет «обёртку» бота — имя, расписание, лимит базы знаний,
  типы полей. Нарушение — обычный `400 validation_error` (01 §1.3).
* **`app.bots.validator`** проверяет сценарий — JSON Schema + семантика графа.
  Нарушение — `422 bot_scenario_invalid` со списком `{step_id, field, code,
  message}` (01 §8.3), который редактор рисует под панелью действий (11 §5.3).

Поэтому `scenario` здесь — просто `dict`: если положить его в pydantic-модель,
ошибки сценария вернутся в чужом формате и редактор не сможет подсветить шаги.
"""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MAX_NAME = 200
MAX_KNOWLEDGE_BASE = 20_000  # 02 §5.1: счётчик символов в редакторе
MAX_SANDBOX_MESSAGE = 4000

Day = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAYS: tuple[Day, ...] = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
HHMM = Annotated[str, Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")]

MSK = timezone(timedelta(hours=3))


# --- расписание (02 §2.5) ----------------------------------------------------


class ScheduleInterval(BaseModel):
    """Интервал активности. `start > end` — интервал через полночь (20:00–10:00)."""

    model_config = ConfigDict(extra="forbid")

    days: list[Day] = Field(default_factory=lambda: list(DAYS), min_length=1)
    start: HHMM
    end: HHMM

    @field_validator("days")
    @classmethod
    def _unique_days(cls, value: list[Day]) -> list[Day]:
        # порядок дней нормализуем — расписание сравнивается как JSON
        return [d for d in DAYS if d in set(value)]

    @model_validator(mode="after")
    def _not_empty_interval(self) -> "ScheduleInterval":
        if self.start == self.end:
            raise ValueError("Начало и конец интервала совпадают — интервал пустой")
        return self


class Schedule(BaseModel):
    """`bots.schedule`. Дефолт из DDL — круглосуточно."""

    model_config = ConfigDict(extra="forbid")

    always: bool = True
    timezone: str = "Europe/Moscow"
    intervals: list[ScheduleInterval] = Field(default_factory=list)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Неизвестная таймзона: {value}") from exc
        return value

    @model_validator(mode="after")
    def _intervals_required(self) -> "Schedule":
        if not self.always and not self.intervals:
            raise ValueError("Без интервалов расписание не включит бота никогда")
        return self


# --- боты --------------------------------------------------------------------


class BotAccountOut(BaseModel):
    id: uuid.UUID
    title: str


class BotListItemOut(BaseModel):
    """Строка списка ботов (01 §8.1, экран 11 §5.1)."""

    id: uuid.UUID
    name: str
    is_enabled: bool
    schedule: dict[str, Any]
    accounts: list[BotAccountOut]
    scenario_steps_count: int
    knowledge_base_present: bool
    # Оба переключателя видны прямо в списке. «Включён», «кто думает» и «дойдёт ли до
    # клиента» — три разные вещи, и в списке из десятка ботов это первое, что нужно
    # знать про каждого: заходить в редактор ради этого никто не станет.
    ai_provider: Literal["claude", "leadbot"] = "claude"
    mode: Literal["suggest", "auto"] = "suggest"
    # Колонка «Диалогов/7д» (11 §5.1): сколько диалогов бот вёл за последние
    # 7 суток. Считается по `bot_vars.bot_id` — единственному следу бота в
    # диалоге, который переживает и handoff, и закрытие.
    conversations_7d: int = 0


class PageOut(BaseModel):
    limit: int
    offset: int
    total: int


class BotsPageOut(BaseModel):
    items: list[BotListItemOut]
    page: PageOut


class BotDetailOut(BaseModel):
    """Деталь бота (01 §8.2) — она же ответ POST/PUT/enable/disable."""

    id: uuid.UUID
    name: str
    is_enabled: bool
    schedule: dict[str, Any]
    scenario: dict[str, Any]
    knowledge_base: str | None
    # Кто придумывает ответ на шаге `ai_answer`: наш Claude или лид-бот владельца
    # (миграция 0035, разбор — в app/bots/provider.py).
    #
    # ⚠ У БОТОВ, КОТОРЫЕ ОТДАЁТ ЭТА РУЧКА, ЗДЕСЬ ВСЕГДА `claude`. Значение
    # `leadbot` носит одна системная запись, а её список ботов не показывает и
    # редактор не открывает (миграция 0038, `app/services/leadbot_admin.py`):
    # с 12 августа лид-бот — отдельный раздел, а не выбор мозга у бота.
    ai_provider: Literal["claude", "leadbot"] = "claude"
    # Что делать с придуманным: «suggest» — заметка оператору, клиент ничего не
    # получает; «auto» — отправить клиенту (миграция 0036).
    mode: Literal["suggest", "auto"] = "suggest"
    accounts: list[BotAccountOut]


class BotWrite(BaseModel):
    """Тело `POST /bots` и `PUT /bots/{id}` (01 §8.3).

    PUT — полная замена: PATCH не даём, сценарий — атомарный документ.
    `scenario` не задан при создании → берётся дефолтный «Первичный приём»
    (02 §5.1: «Создать бота» создаёт копию дефолтного сценария).
    `is_enabled` читает только POST: в PUT включение не меняется, для него
    есть `enable`/`disable` (01 §8.4, проверка 24.09).
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_NAME)
    is_enabled: bool = True
    schedule: Schedule = Field(default_factory=Schedule)
    scenario: dict[str, Any] | None = None
    knowledge_base: str = Field(default="", max_length=MAX_KNOWLEDGE_BASE)
    # ⚠ ОБА УМОЛЧАНИЯ — «КАК БЫЛО», И ЭТО НЕ СИММЕТРИЯ РАДИ КРАСОТЫ.
    # Поставщик: подключение лид-бота не имеет права само собой сменить мозг
    # работающему боту — у него другой регламент и другие цены, и подмену заметили бы
    # по ответам клиентам, а не по настройке.
    # Режим: на живой аккаунт бот выходит подсказкой, и только когда видно, что он
    # пишет, ему отдают слово. Оба переключаются руками, по одному боту.
    ai_provider: Literal["claude", "leadbot"] = "claude"
    mode: Literal["suggest", "auto"] = "suggest"

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Имя бота не может быть пустым")
        return value


class BotAccountsWrite(BaseModel):
    """`PUT /bots/{id}/accounts` — полный список аккаунтов бота (01 §8.5)."""

    model_config = ConfigDict(extra="forbid")

    account_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)

    @field_validator("account_ids")
    @classmethod
    def _unique(cls, value: list[uuid.UUID]) -> list[uuid.UUID]:
        seen: list[uuid.UUID] = []
        for item in value:
            if item not in seen:
                seen.append(item)
        return seen


class BotAccountsOut(BaseModel):
    accounts: list[BotAccountOut]


# --- валидация сценария ------------------------------------------------------


class ScenarioIssueOut(BaseModel):
    """Находка валидатора — контракт списка ошибок редактора (11 §5.3)."""

    step_id: str | None
    field: str | None
    code: str
    level: Literal["error", "warning"]
    message: str
    ref: str | None = None


# --- песочница (01 §8.6, 02 §5.3) -------------------------------------------


class SandboxStartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scenario: dict[str, Any]
    knowledge_base: str = Field(default="", max_length=MAX_KNOWLEDGE_BASE)
    schedule: Schedule = Field(default_factory=Schedule)
    client_name: str | None = Field(default=None, max_length=200)
    item_title: str | None = Field(default=None, max_length=500)
    # «Время: задать» — влияет и на work_hours шага condition, и на расписание.
    now_override: datetime | None = None
    ai_mode: Literal["real", "stub"] = "stub"

    @field_validator("now_override")
    @classmethod
    def _aware(cls, value: datetime | None) -> datetime | None:
        # Наивное время трактуем как московское: все бизнес-определения
        # времени в проекте — по Москве (06 §0.1), и админ вводит его же.
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=MSK)
        return value


class SandboxMessageIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=MAX_SANDBOX_MESSAGE)


class SandboxTickOut(BaseModel):
    """Ответ любого шага песочницы: что сделал бот + состояние + трасса."""

    events: list[dict[str, Any]]
    state: dict[str, Any]
    trace: list[str]


class SandboxStartOut(SandboxTickOut):
    session_id: str
