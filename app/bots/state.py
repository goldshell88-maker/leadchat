"""``conversations.bot_vars`` — типизированное состояние движка (02 §2.1).

Всё состояние бота в диалоге живёт в двух колонках ``conversations``:
``bot_active`` (флаг для чужих подсистем) и ``bot_vars`` (приватное состояние
движка). Здесь — модели чтения-записи ``bot_vars`` и контейнер отложенных
эффектов тика.

Три инварианта, ради которых существует этот модуль:

1. **Чтение никогда не падает.** ``bot_vars`` пишут не только мы: спринт 3
   ставит туда ``{"muted": true, "waiting": null}`` прямо из отправки
   сообщения оператором (02 §2.6). Любая форма, включая пустой дикт и
   легаси-раскладку, читается в :class:`BotState` без исключений — иначе
   диалог зависнет навсегда.
2. **Неизвестные ключи переживают round-trip** (``extra``): параллельные зоны
   (песочница, редактор) могут дописывать своё, и тик бота не должен их
   стирать.
3. **``muted`` не сбрасывается никогда** — ни при переоткрытии диалога, ни при
   старте нового сценария (решение владельца №3).

Отложенные эффекты (:class:`Outbox`) — публикация в Pub/Sub и постановка
ARQ-задач строго ПОСЛЕ commit'а (08 §8.1): движок только копит их, отправляет
:func:`app.bots.runtime.flush_outbox`. Тот же список — источник событий
трассировки для песочницы (02 §5.3).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

log = structlog.get_logger("app.bots.state")


def utcnow() -> datetime:
    return datetime.now(UTC)


def utcnow_iso(now: datetime | None = None) -> str:
    moment = now or utcnow()
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def new_token() -> str:
    """Токен ожидания: отложенная задача таймаута сверяет его (02 §2.4)."""
    return secrets.token_hex(4)


# ------------------------------------------------------------------- waiting


@dataclass(slots=True)
class Waiting:
    """Состояние «ждём ответа клиента» на шаге ``ask``/``menu`` (02 §2.1)."""

    kind: str = "ask"  # ask | menu
    var: str | None = None
    token: str = ""
    deadline: str | None = None
    attempts: int = 0
    step_id: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> Waiting | None:
        if not isinstance(raw, dict) or not raw:
            return None
        kind = raw.get("kind")
        token = raw.get("token")
        if not isinstance(token, str) or not token:
            # Легаси/битая запись: без токена отложенная задача никогда не
            # совпадёт, ждать будем только ответа клиента. Диалог не теряем.
            log.warning("bot.waiting_without_token", waiting=raw)
            token = ""
        attempts = raw.get("attempts")
        return cls(
            kind=kind if kind in ("ask", "menu") else "ask",
            var=raw.get("var") if isinstance(raw.get("var"), str) else None,
            token=token,
            deadline=raw.get("deadline") if isinstance(raw.get("deadline"), str) else None,
            attempts=attempts
            if isinstance(attempts, int) and not isinstance(attempts, bool)
            else 0,
            # ``step_id`` — алиас из задания спринта; истина о текущем шаге
            # всё равно в ``BotState.step`` (02 §2.1).
            step_id=raw.get("step_id") if isinstance(raw.get("step_id"), str) else None,
        )

    def dump(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "var": self.var,
            "token": self.token,
            "deadline": self.deadline,
            "attempts": self.attempts,
            "step_id": self.step_id,
        }

    def expired(self, now: datetime | None = None) -> bool:
        deadline = parse_iso(self.deadline)
        return deadline is not None and deadline <= (now or utcnow())


# ------------------------------------------------------------------ счётчики


@dataclass(slots=True)
class Counters:
    """Счётчики предохранителей (02 §2.1/§2.7)."""

    steps_total: int = 0  # шагов за всю жизнь диалога
    bot_msgs_row: int = 0  # сообщений бота подряд без ответа клиента
    offscript_msgs: int = 0  # входящих «мимо сценария» подряд
    attempts: int = 0  # невалидных ответов за всю жизнь диалога
    ai_calls: int = 0

    @classmethod
    def from_dict(cls, raw: Any) -> Counters:
        data = raw if isinstance(raw, dict) else {}

        def pick(name: str) -> int:
            value = data.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                return 0
            return value

        return cls(
            steps_total=pick("steps_total"),
            bot_msgs_row=pick("bot_msgs_row"),
            offscript_msgs=pick("offscript_msgs"),
            attempts=pick("attempts"),
            ai_calls=pick("ai_calls"),
        )

    def dump(self) -> dict[str, int]:
        return {
            "steps_total": self.steps_total,
            "bot_msgs_row": self.bot_msgs_row,
            "offscript_msgs": self.offscript_msgs,
            "attempts": self.attempts,
            "ai_calls": self.ai_calls,
        }


@dataclass(slots=True)
class HandoffInfo:
    reason: str
    at: str
    step: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> HandoffInfo | None:
        if isinstance(raw, str) and raw:  # легаси: в bot_vars лежала одна причина
            return cls(reason=raw, at=utcnow_iso())
        if not isinstance(raw, dict) or not raw:
            return None
        reason = raw.get("reason")
        if not isinstance(reason, str) or not reason:
            return None
        at = raw.get("at")
        return cls(
            reason=reason,
            at=at if isinstance(at, str) else utcnow_iso(),
            step=raw.get("step") if isinstance(raw.get("step"), str) else None,
        )

    def dump(self) -> dict[str, Any]:
        return {"reason": self.reason, "at": self.at, "step": self.step}


# --------------------------------------------------------------------- state

# Ключи, которые движок знает; всё остальное уезжает в ``extra`` без потерь.
KNOWN_KEYS: frozenset[str] = frozenset(
    {
        "bot_id",
        # ⚠ ОСТАЁТСЯ В СПИСКЕ, ХОТЯ ПОЛЯ БОЛЬШЕ НЕТ — читаем, не пишем.
        # У живых диалогов в `bot_vars` лежит `scenario_revision: N`, записанный
        # до 23.08. Убери ключ отсюда — и `from_dict` ниже примет его за старую
        # раскладку и мигрирует В ПЕРЕМЕННЫЕ БОТА, а те идут и в подстановку
        # шаблонов, и в сводку оператору при передаче диалога. У каждого живого
        # диалога появилась бы переменная «scenario_revision = 3», видимая
        # человеку. Ключ уйдёт из базы сам: `to_dict` его больше не пишет.
        "scenario_revision",
        "step",
        "current_step",  # алиас из задания спринта — читаем, не пишем
        "waiting",
        "vars",
        "counters",
        "muted",
        "handoff",
        "started_at",
        "last_step_at",
        "closed_at",
        "seen_in",
    }
)

#: Сколько последних разобранных входящих помнит состояние: больше серии
#: (`runtime.SERIES_MAX_MESSAGES`) с запасом.
SEEN_IN_MAX = 30


@dataclass(slots=True)
class BotState:
    """Разобранный ``bot_vars`` (02 §2.1). ``next_step`` — транзиент тика."""

    bot_id: str | None = None
    # ЗДЕСЬ ЖИЛО `scenario_revision` — номер редакции сценария, который писался
    # на КАЖДОЕ входящее и не читался никем: ни одного сравнения, ни одного
    # ветвления во всём дереве. Сторож, которого нет.
    #
    # Настоящая защита от правки сценария под ногами работает и без него:
    # `engine` в трёх местах проверяет `self.scenario.step(...) is None` и уводит
    # диалог в передачу с причиной `scenario_changed`. Номер редакции к этому
    # решению не привлекался.
    #
    # Хуже, чем бесполезность: `start()` перезаписывал сохранённое значение
    # второй строкой `on_incoming`, то есть свежая редакция затирала прежнюю
    # раньше, чем кто-либо мог бы их сравнить. Даже задумай кто-то сторож — этот
    # порядок сделал бы его слепым.
    step: str | None = None
    waiting: Waiting | None = None
    vars: dict[str, Any] = field(default_factory=dict)
    counters: Counters = field(default_factory=Counters)
    muted: bool = False
    handoff: HandoffInfo | None = None
    started_at: str | None = None
    last_step_at: str | None = None
    # Момент, когда сценарий закончился шагом ``close``. Сам по себе состояние
    # не блокирует: вернувшийся клиент получает сценарий с чистого листа
    # (02 §1.3), а поле остаётся следом для журнала и отладки.
    closed_at: str | None = None
    #: Входящие, которые тик уже разобрал (id, последние :data:`SEEN_IN_MAX`).
    #: Серия клиента — то, чего здесь нет (проверка 24.09): граница «после
    #: реплики бота» не годилась — подсказка пишется заметкой, а время
    #: сообщения Авито идёт целыми секундами и бывает раньше ответа бота.
    seen_in: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)
    next_step: str | None = None  # в bot_vars НЕ попадает

    # --- чтение ---------------------------------------------------------

    @classmethod
    def from_dict(cls, raw: Any) -> BotState:
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        values = data.get("vars")
        vars_: dict[str, Any] = dict(values) if isinstance(values, dict) else {}
        extra: dict[str, Any] = {}
        for key, value in data.items():
            if key in KNOWN_KEYS:
                continue
            # Миграция старой раскладки: скаляр верхнего уровня — это
            # собранная ботом переменная (до появления вложенного "vars").
            if isinstance(value, str | int | float | bool) and key not in vars_:
                vars_[key] = value
                log.info("bot.state_migrated_var", key=key)
            else:
                extra[key] = value

        step = data.get("step")
        if not isinstance(step, str) or not step:
            legacy = data.get("current_step")  # алиас задания спринта
            step = legacy if isinstance(legacy, str) and legacy else None

        bot_id = data.get("bot_id")
        return cls(
            bot_id=str(bot_id) if bot_id else None,
            step=step,
            waiting=Waiting.from_dict(data.get("waiting")),
            vars=vars_,
            counters=Counters.from_dict(data.get("counters")),
            muted=bool(data.get("muted")),
            handoff=HandoffInfo.from_dict(data.get("handoff")),
            started_at=data.get("started_at") if isinstance(data.get("started_at"), str) else None,
            last_step_at=(
                data.get("last_step_at") if isinstance(data.get("last_step_at"), str) else None
            ),
            closed_at=data.get("closed_at") if isinstance(data.get("closed_at"), str) else None,
            seen_in=[x for x in data.get("seen_in") or [] if isinstance(x, str)][-SEEN_IN_MAX:],
            extra=extra,
        )

    @classmethod
    def from_conv(cls, conv: Any) -> BotState:
        return cls.from_dict(getattr(conv, "bot_vars", None))

    # --- запись ---------------------------------------------------------

    def dump(self) -> dict[str, Any]:
        """JSON для ``conversations.bot_vars``.

        Имя поля текущего шага — ``step`` (нормативный JSON 02 §2.1);
        ``current_step`` принимается на чтении как алиас, но не пишется, чтобы
        в колонке не жили два источника истины (её отдаёт наружу
        ``GET /conversations/{id}``, 01 §5.2).
        """
        out: dict[str, Any] = dict(self.extra)
        out.update(
            {
                "bot_id": self.bot_id,
                "step": self.step,
                "waiting": self.waiting.dump() if self.waiting else None,
                "vars": dict(self.vars),
                "counters": self.counters.dump(),
                "muted": self.muted,
                "handoff": self.handoff.dump() if self.handoff else None,
                "started_at": self.started_at,
                "last_step_at": self.last_step_at,
                "closed_at": self.closed_at,
                "seen_in": list(self.seen_in),
            }
        )
        return out

    def remember_seen(self, message_ids: Iterable[str]) -> None:
        """Отметить входящие разобранными: следующий тик их в серию не возьмёт."""
        fresh = [m for m in message_ids if m not in self.seen_in]
        self.seen_in = [*self.seen_in, *fresh][-SEEN_IN_MAX:]

    # --- жизненный цикл --------------------------------------------------

    def is_fresh(self) -> bool:
        """Сценарий ещё не начинался (первый тик в этом диалоге)."""
        return self.step is None and self.waiting is None and self.handoff is None

    def start(
        self,
        *,
        bot_id: Any = None,
        now: datetime | None = None,
    ) -> None:
        """Отметить начало сценария; повторный вызов ничего не ломает."""
        if bot_id is not None:
            self.bot_id = str(bot_id)
        if self.started_at is None:
            self.started_at = utcnow_iso(now)

    def finish_closed(self, step_id: str | None = None, now: datetime | None = None) -> None:
        """Сценарий дошёл до ``close``: сбрасываем позицию до чистого листа.

        02 §1.3: если клиент напишет снова, воркер входящих переоткроет диалог,
        и бот стартует ЗАНОВО. Поэтому после закрытия в состоянии не должно
        остаться ни текущего шага, ни счётчиков прошлого прохода. Что
        сохраняем осознанно: ``muted`` (сбросу не подлежит никогда),
        собранные ``vars`` (телефон и проблема клиента не устаревают от того,
        что диалог закрыли, и их видно в карточке) и ``bot_id``.
        """
        self.closed_at = utcnow_iso(now)
        self.last_step_at = self.closed_at
        self.step = None
        self.waiting = None
        self.handoff = None
        self.started_at = None
        self.counters = Counters()
        self.next_step = None
        if step_id:
            self.vars["_closed_by_step"] = step_id

    def touch(self, now: datetime | None = None) -> None:
        self.last_step_at = utcnow_iso(now)

    def begin_waiting(
        self,
        *,
        kind: str,
        step_id: str,
        var: str | None,
        timeout: timedelta | None,
        now: datetime | None = None,
    ) -> Waiting:
        """Новое ожидание + НОВЫЙ токен (02 §2.4): старая отложенная задача
        при срабатывании увидит чужой токен и умрёт."""
        moment = now or utcnow()
        waiting = Waiting(
            kind=kind,
            var=var,
            token=new_token(),
            deadline=utcnow_iso(moment + timeout) if timeout else None,
            attempts=0,
            step_id=step_id,
        )
        self.waiting = waiting
        return waiting

    def stop_waiting(self) -> None:
        self.waiting = None

    def handoff_done(self) -> bool:
        return self.handoff is not None

    def set_handoff(self, *, reason: str, step: str | None, now: datetime | None = None) -> None:
        self.handoff = HandoffInfo(reason=reason, at=utcnow_iso(now), step=step)

    def mute(self) -> None:
        """Оператор вмешался — навсегда (решение владельца №3, 02 §2.6)."""
        self.muted = True
        self.waiting = None


# --------------------------------------------------------------------- outbox


@dataclass(slots=True)
class PendingEvent:
    """Кадр Pub/Sub, ждущий commit'а (08 §8.1)."""

    type: str
    data: dict[str, Any]


@dataclass(slots=True)
class PendingJob:
    """ARQ-задача, ждущая commit'а: воркер не должен прочитать то, чего нет."""

    name: str
    args: tuple[Any, ...] = ()
    defer_by: timedelta | None = None
    job_id: str | None = None


@dataclass(slots=True)
class Outbox:
    """Отложенные эффекты одного тика + трассировка для песочницы (02 §5.3)."""

    events: list[PendingEvent] = field(default_factory=list)
    jobs: list[PendingJob] = field(default_factory=list)
    trace: list[dict[str, Any]] = field(default_factory=list)
    #: Итоги :func:`app.services.notifications.notify`, ждущие commit'а.
    #:
    #: Запись центра уведомлений кладётся в ТУ ЖЕ транзакцию, что и сама
    #: передача диалога, а кадр в браузер обязан уйти строго после commit'а —
    #: иначе человек получит тост о диалоге, которого ещё нет. Раньше итог
    #: `notify` просто выбрасывался, и строка ложилась в базу немой: тоста нет,
    #: колокольчик не обновляется (запрос списка идёт без `refetchInterval`, а
    #: `refetchOnWindowFocus` выключен) — руководитель узнавал об «AI недоступен»
    #: только перезагрузив вкладку.
    #:
    #: Тип намеренно широкий: `state.py` — низ зоны бота, и тянуть сюда
    #: `services.notifications` значило бы завести цикл импортов.
    notifications: list[Any] = field(default_factory=list)

    def event(self, type_: str, data: dict[str, Any]) -> None:
        self.events.append(PendingEvent(type=type_, data=data))

    def notification(self, result: Any) -> None:
        """Уведомление записано в транзакции — доставить после commit'а."""
        if result is not None:
            self.notifications.append(result)

    def job(
        self,
        name: str,
        *args: Any,
        defer_by: timedelta | None = None,
        job_id: str | None = None,
    ) -> None:
        self.jobs.append(PendingJob(name=name, args=args, defer_by=defer_by, job_id=job_id))

    def note(self, kind: str, /, **fields: Any) -> None:
        """Строка трассировки («→ шаг check_hours», «✨ confidence 0.42»).

        ``kind`` — позиционный: он и есть тип события песочницы (02 §5.3), и
        затереть его полем нельзя.
        """
        entry: dict[str, Any] = {"kind": kind}
        entry.update(fields)
        entry["kind"] = kind
        self.trace.append(entry)

    def clear(self) -> None:
        self.events.clear()
        self.jobs.clear()
        self.notifications.clear()
