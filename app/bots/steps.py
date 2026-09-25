"""Сценарий и его шаги: разбор, подстановка переменных, валидация ответов.

Чистый модуль без БД, Redis и времени «изнутри»: всё, что зависит от текущего
момента, принимает ``now`` аргументом. Это позволяет одинаково исполнять
сценарий и в воркере, и в песочнице редактора (02 §5.3), и в юнит-тестах без
``time_machine``.

Что здесь:

* :class:`Scenario` / :class:`Step` — типизированное чтение ``bots.scenario``
  (формат 02 §1.1–1.3). Разбор терпимый: мусорные шаги пропускаются с
  warning'ом, а не роняют тик — валидатор графа (02 §5.2) живёт на входе
  ``PUT /bots/{id}``, рантайм обязан доработать даже кривую запись.
* :func:`render` / :func:`render_text` — подстановка ``{client_name}`` и
  остальных (02 §1.2). Пустое значение уносит с собой весь оборот, чтобы
  клиенту не уезжало «по объявлению «».».
* :func:`validate_answer`, :func:`match_menu_option` — ответы клиента на
  ``ask``/``menu`` (02 §1.3).
* :func:`detect_human_request` — первый эшелон условия handoff №1 (02 §4).
* :func:`mask_phones` — ПРИВАТНОСТЬ: перед отправкой текста в модель телефоны
  заменяются плейсхолдером ``{PHONE}``, потому что запрос уходит за границу
  (через шлюз Амстердама, docs/46). Извлечённый регуляркой номер живёт
  локально в ``bot_vars.vars`` и в карточке клиента, в промпт не попадает.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import structlog

from app.bots.schedule import in_work_hours
from app.services.inbound import extract_phone as extract_phone

log = structlog.get_logger("app.bots.steps")

STEP_TYPES: tuple[str, ...] = (
    "send",
    "ask",
    "menu",
    "condition",
    "ai_answer",
    "handoff",
    "close",
    "tag",
    "note",
)
STEP_ID_RE = re.compile(r"^[a-z0-9_]{1,64}$")

# Значение переменной, попадающей в bot_vars.vars (02 §1.3: «обрезанный до 2000»).
MAX_VAR_LENGTH = 2000
MAX_TIMEOUT_SECONDS = 604_800  # 7 суток — верхняя граница схемы 02 §1.4


# --------------------------------------------------------------------- лимиты


@dataclass(frozen=True, slots=True)
class Limits:
    """Предохранители 02 §2.7. Дефолты — из документа, границы — из схемы §1.4."""

    max_steps_total: int = 100
    max_steps_per_tick: int = 20
    max_bot_messages_row: int = 5
    max_offscript_messages: int = 2

    @classmethod
    def from_settings(cls, raw: Any) -> Limits:
        if not isinstance(raw, dict):
            return cls()
        default = cls()

        def pick(name: str, low: int, high: int) -> int:
            value = raw.get(name, getattr(default, name))
            if isinstance(value, bool) or not isinstance(value, int):
                return int(getattr(default, name))
            return max(low, min(high, value))

        return cls(
            max_steps_total=pick("max_steps_total", 10, 500),
            max_steps_per_tick=pick("max_steps_per_tick", 5, 50),
            max_bot_messages_row=pick("max_bot_messages_row", 2, 10),
            max_offscript_messages=pick("max_offscript_messages", 1, 5),
        )


# ---------------------------------------------------------------------- шаги


@dataclass(frozen=True, slots=True)
class Step:
    """Один шаг сценария (02 §1.3). ``params`` не типизируем: у девяти типов
    девять наборов полей, и рантайм читает их через ``.get`` с дефолтами."""

    id: str
    type: str
    params: dict[str, Any]
    next: str | None = None
    on_timeout: str | None = None
    on_invalid: str | None = None
    on_no_match: str | None = None
    on_low_confidence: str | None = None

    @classmethod
    def from_dict(cls, raw: Any) -> Step | None:
        if not isinstance(raw, dict):
            return None
        sid, stype = raw.get("id"), raw.get("type")
        if not isinstance(sid, str) or not isinstance(stype, str):
            return None
        if stype not in STEP_TYPES:
            log.warning("bot.scenario_unknown_step_type", step_id=sid, step_type=stype)
            return None
        params = raw.get("params")
        return cls(
            id=sid,
            type=stype,
            params=params if isinstance(params, dict) else {},
            next=_ref(raw.get("next")),
            on_timeout=_ref(raw.get("on_timeout")),
            on_invalid=_ref(raw.get("on_invalid")),
            on_no_match=_ref(raw.get("on_no_match")),
            on_low_confidence=_ref(raw.get("on_low_confidence")),
        )


def _ref(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


@dataclass(slots=True)
class Scenario:
    """``bots.scenario`` целиком (02 §1.1)."""

    version: int
    revision: int
    entry: str | None
    limits: Limits
    steps: dict[str, Step]
    order: tuple[str, ...]
    raw: dict[str, Any]

    def step(self, step_id: str | None) -> Step | None:
        if not step_id:
            return None
        return self.steps.get(step_id)

    def has(self, step_id: str | None) -> bool:
        return self.step(step_id) is not None

    @classmethod
    def from_dict(cls, raw: Any) -> Scenario:
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        steps: dict[str, Step] = {}
        order: list[str] = []
        for item in data.get("steps") or []:
            step = Step.from_dict(item)
            if step is None:
                log.warning("bot.scenario_bad_step", step=item if isinstance(item, dict) else None)
                continue
            if step.id in steps:  # duplicate_id — ошибка валидатора; рантайм берёт первый
                log.warning("bot.scenario_duplicate_step", step_id=step.id)
                continue
            steps[step.id] = step
            order.append(step.id)
        entry = data.get("entry")
        version = data.get("version")
        revision = data.get("revision")
        return cls(
            version=version if isinstance(version, int) else 1,
            revision=revision if isinstance(revision, int) else 0,
            entry=entry if isinstance(entry, str) and entry else None,
            limits=Limits.from_settings(data.get("settings")),
            steps=steps,
            order=tuple(order),
            raw=data,
        )


# ------------------------------------------------------- подстановка (02 §1.2)

PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# «, {client_name}» при пустом значении вырезается целиком: «Здравствуйте,
# {client_name}!» -> «Здравствуйте!», а не «Здравствуйте, !» (02 §1.2).
ADDRESS_RE = re.compile(r",[ \t]*\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Границы фрагмента для правила «пустая подстановка уносит свой оборот»:
# конец предложения (вместе с пробелами после него) или перевод строки.
# Разделитель уезжает ВМЕСТЕ со своим фрагментом, поэтому после выброса не
# остаётся ни двойных пробелов, ни висящих переводов строки.
FRAGMENT_RE = re.compile(r"[^.!?…\n]*(?:[.!?…]+[ \t]*|\n+|$)")

SYSTEM_PLACEHOLDERS: tuple[str, ...] = (
    "client_name",
    "item_title",
    "item_price",
    "account_title",
)


def _is_blank(value: Any) -> bool:
    return value is None or str(value).strip() == ""


@dataclass(frozen=True, slots=True)
class Rendered:
    """Результат подстановки: текст плюс то, что пришлось из него выбросить."""

    text: str
    #: Плейсхолдеры, ради пустоты которых выброшен целый оборот.
    dropped: tuple[str, ...] = ()

    @property
    def lost_everything(self) -> bool:
        """Шаблон был не пуст, а после подстановки не осталось ничего."""
        return bool(self.dropped) and not self.text


def render(template: str | None, values: dict[str, Any]) -> Rendered:
    """Подставить переменные в текст шага (02 §1.2).

    ПОЧЕМУ ЗДЕСЬ ВЫРЕЗАЮТСЯ ЦЕЛЫЕ ОБОРОТЫ. Пустое значение раньше подставлялось
    как пустая строка, и клиенту уезжало «Вы пишете по объявлению «».» —
    обрывок, по которому сразу видно, что с ним говорит сломанный робот. Правило
    «вырезать запятую-обращение» (ADDRESS_RE) спасало ровно один оборот из всех,
    «Здравствуйте, {client_name}!», а кавычки, скобки и предлог перед
    подстановкой оставались на экране.

    Поэтому правило общее: фрагмент (предложение или строка), внутри которого
    подстановка оказалась пустой, уходит целиком. «Здравствуйте, Иван! Вы пишете
    по объявлению «{item_title}». Что случилось?» без объявления превращается в
    «Здравствуйте, Иван! Что случилось?» — короче, но по-человечески.

    Обращение обрабатывается ПЕРВЫМ и по-прежнему точечно: иначе пустое имя
    уносило бы «Здравствуйте!» вместе со всем приветствием.

    Неизвестный плейсхолдер -> пустая строка + warning (валидатор ловит это
    ещё при сохранении, код рантайма обязан просто не сломаться).
    """
    if not template:
        return Rendered("")

    def blank(name: str) -> bool:
        # Неизвестное имя — тоже пусто: подставить его всё равно нечем.
        return name not in values or _is_blank(values[name])

    def cut_address(m: re.Match[str]) -> str:
        # запятая-обращение уходит вместе с пустым значением
        return "" if blank(m.group(1)) else m.group(0)

    def substitute(m: re.Match[str]) -> str:
        name = m.group(1)
        if name not in values:
            log.warning("bot.render_unknown_placeholder", placeholder=name)
            return ""
        value = values[name]
        return "" if value is None else str(value)

    out = ADDRESS_RE.sub(cut_address, template)

    kept: list[str] = []
    dropped: list[str] = []
    for fragment in FRAGMENT_RE.findall(out):
        if not fragment:
            continue
        empty = [name for name in PLACEHOLDER_RE.findall(fragment) if blank(name)]
        if empty:
            dropped += empty
            continue
        kept.append(fragment)

    if not dropped:  # обычный путь: текст не трогали вовсе
        return Rendered(PLACEHOLDER_RE.sub(substitute, out))

    log.warning("bot.render_dropped_fragment", placeholders=sorted(set(dropped)))
    return Rendered(
        PLACEHOLDER_RE.sub(substitute, "".join(kept)).strip(),
        tuple(dict.fromkeys(dropped)),
    )


def render_text(template: str | None, values: dict[str, Any]) -> str:
    """`render` для тех, кому нужен только текст."""
    return render(template, values).text


# ------------------------------------------------------------------ телефоны

# Тот же кандидат, что в ``services.inbound`` (DESIGN 3.3/8.3): 10–11 цифр с
# любыми разделителями. Нормализацию и извлечение делает сам ``extract_phone``
# — здесь регулярка нужна только для маскирования перед отправкой в модель.
PHONE_CANDIDATE_RE = re.compile(r"\+?\d(?:[\s\-().]*\d){9,10}")
PHONE_PLACEHOLDER = "{PHONE}"


# ЗДЕСЬ ЖИЛ ВТОРОЙ `normalize_phone` — обёртка в одну строку над `extract_phone`.
# Снят 23.08: за всё время его не позвал никто, а нормализация телефона в системе
# и так одна (`app/services/phone_parse`), к ней ведут и `services.clients` и
# `inbound`. Второе имя для одного действия — приглашение к расхождению.


def mask_phones(text: str | None, placeholder: str = PHONE_PLACEHOLDER) -> str:
    """ПРИВАТНОСТЬ: убрать телефоны из текста перед отправкой в модель.

    Запрос к Claude уходит за границу (прокси в Нидерландах,
    через шлюз Амстердама), поэтому персональные данные в промпт не попадают.
    Номер, извлечённый регуляркой, сохраняется локально — в ``bot_vars.vars``
    и в карточке клиента.
    """
    if not text:
        return ""

    def repl(m: re.Match[str]) -> str:
        return placeholder if extract_phone(m.group()) else m.group()

    return PHONE_CANDIDATE_RE.sub(repl, text)


# ------------------------------------------------------------------ таймауты

_TIMEOUT_RE = re.compile(r"^(\d+)\s*([smhd])$", re.I)
_TIMEOUT_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_timeout(value: Any) -> timedelta | None:
    """«24h» | «30m» | 3600 | None -> timedelta | None (02 §1.4)."""
    if value is None or isinstance(value, bool):
        return None
    seconds: int | None = None
    if isinstance(value, int | float):
        seconds = int(value)
    elif isinstance(value, str):
        m = _TIMEOUT_RE.match(value.strip())
        if m:
            seconds = int(m.group(1)) * _TIMEOUT_UNITS[m.group(2).lower()]
        else:
            log.warning("bot.bad_timeout", value=value)
    if seconds is None or seconds <= 0:
        return None
    return timedelta(seconds=min(seconds, MAX_TIMEOUT_SECONDS))


# --------------------------------------------------- валидация ответов (ask)


def _valid_any(text: str) -> bool:
    return len(text.strip()) > 0


def _valid_number(text: str) -> bool:
    return re.search(r"\d+([.,]\d+)?", text) is not None


VALIDATORS = {
    "any": _valid_any,
    "phone": lambda t: extract_phone(t) is not None,
    "number": _valid_number,
}


def validate_answer(params: dict[str, Any], text: str | None) -> tuple[bool, str | None]:
    """Проверить ответ клиента на шаге ``ask`` (02 §1.3).

    Возвращает ``(ok, value)``: для ``phone`` значение нормализовано
    (``+7XXXXXXXXXX``), иначе — исходный текст, обрезанный до 2000 символов.
    """
    raw = (text or "").strip()
    rule = params.get("validate") or "any"

    if isinstance(rule, dict):
        pattern = rule.get("regex")
        if isinstance(pattern, str) and pattern:
            try:
                ok = re.search(pattern, raw, re.I) is not None
            except re.error as exc:  # bad_regex — ошибка валидатора; тик не роняем
                log.warning("bot.bad_regex", regex=pattern, error=str(exc))
                ok = _valid_any(raw)
            return (ok, raw[:MAX_VAR_LENGTH] if ok else None)
        rule = "any"

    if rule == "phone":
        phone = extract_phone(raw)
        return (phone is not None, phone)

    check = VALIDATORS.get(rule if isinstance(rule, str) else "any", _valid_any)
    ok = check(raw)
    return (ok, raw[:MAX_VAR_LENGTH] if ok else None)


# ----------------------------------------------------------- матчинг меню


def normalize_text(text: str | None) -> str:
    """lower + ё->е — общая нормализация для эвристик по русскому тексту."""
    return (text or "").lower().replace("ё", "е")


_TRIM_PUNCT = " .!?,;:)("


def match_menu_option(
    options: list[dict[str, Any]] | None, text: str | None
) -> dict[str, Any] | None:
    """Номер варианта ИЛИ ключевое слово (02 §1.3).

    Равенство — по очищенному от хвостовой пунктуации ответу («1.» = «1»),
    вхождение подстрокой — только для ключей длиной ≥3 (иначе «1» нашлась бы
    внутри «стиралка 1000 оборотов»). Порядок вариантов решает: первый
    совпавший побеждает.
    """
    answer = normalize_text(text).strip()
    trimmed = answer.strip(_TRIM_PUNCT)
    for option in options or []:
        if not isinstance(option, dict):
            continue
        for key in option.get("match") or []:
            needle = normalize_text(str(key)).strip()
            if not needle:
                continue
            if trimmed == needle or answer == needle:
                return option
            if len(needle) >= 3 and needle in answer:
                return option
    return None


# ------------------------------------------- условие handoff №1 (02 §4, эшелон «а»)

HUMAN_WORDS: tuple[str, ...] = (
    "оператор",
    "менеджер",
    "человек",
    "живой",
    "мастера позов",
    "позовите",
    "соедините",
    "хватит бот",
    "не бот",
)


def detect_human_request(text: str | None) -> bool:
    """Словарная эвристика «позовите человека» — мгновенно, без модели.

    Второй эшелон (перефразировки) — классификатор на Haiku (02 §3.4).
    """
    normalized = normalize_text(text)
    if not normalized:
        return False
    return any(word in normalized for word in HUMAN_WORDS)


# ------------------------------------------------------- условия шага condition


def evaluate_condition(
    cond: Any,
    *,
    values: dict[str, Any],
    last_text: str | None,
    now: datetime | None = None,
) -> bool:
    """Одно условие шага ``condition`` (таблица видов — 02 §1.3)."""
    if not isinstance(cond, dict):
        return False
    kind = cond.get("kind")

    if kind == "work_hours":
        return in_work_hours(cond, now)

    var = cond.get("var")
    var = var if isinstance(var, str) else ""

    if kind == "var_exists":
        return not _is_blank(values.get(var))

    if kind == "var_equals":
        actual = values.get(var)
        if actual is None:
            return False
        return str(actual).strip().casefold() == str(cond.get("value", "")).strip().casefold()

    if kind == "text_contains":
        haystack = normalize_text(last_text)
        if not haystack:
            return False
        return any(
            normalize_text(str(kw)) in haystack for kw in (cond.get("keywords") or []) if str(kw)
        )

    if kind == "text_matches":
        pattern = cond.get("regex")
        if not isinstance(pattern, str) or not pattern:
            return False
        try:
            return re.search(pattern, last_text or "", re.I) is not None
        except re.error as exc:
            log.warning("bot.bad_regex", regex=pattern, error=str(exc))
            return False

    log.warning("bot.unknown_condition_kind", kind=kind)
    return False


def pick_condition_branch(
    step: Step,
    *,
    values: dict[str, Any],
    last_text: str | None,
    now: datetime | None = None,
) -> str | None:
    """Первое истинное условие -> его ``next``; иначе ``else`` (02 §1.3)."""
    for item in step.params.get("conditions") or []:
        if not isinstance(item, dict):
            continue
        if evaluate_condition(item.get("if"), values=values, last_text=last_text, now=now):
            return _ref(item.get("next"))
    return _ref(step.params.get("else"))
