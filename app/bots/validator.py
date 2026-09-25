"""Валидация сценария бота — два слоя (02 §1.4 и §5.2).

**Слой 1 — JSON Schema** (draft 2020-12, 02 §1.4): типы шагов, обязательные
поля, лимиты длин, форматы времени и regex. Схема лежит здесь константой
:data:`SCENARIO_SCHEMA` — она же источник для файла ``scenario.schema.json``
и для генерации типов на фронте (02 §5.2 требует одинаковых правил на клиенте
и на сервере).

**Слой 2 — семантика графа** (:func:`validate_scenario`): ссылочная целостность,
достижимость, статические циклы, дубли id, неизвестные переменные. JSON Schema
такого не умеет — это отдельный проход по графу.

Уровни (02 §5.2): ``error`` блокирует сохранение, ``warning`` — нет
(«Сохранить с предупреждениями»). Коды машиночитаемы и всегда сопровождаются
`step_id` проблемного шага — фронт по ним скроллит к карточке (11 §5.3).

Почему свой мини-валидатор JSON Schema, а не `jsonschema`: в зависимостях
бэкенда его нет, а тянуть пакет ради одной схемы фиксированной формы дороже,
чем 150 строк на используемое подмножество ключевых слов (полный список —
:data:`SUPPORTED_KEYWORDS`; неизвестное слово в схеме роняет тест
`test_bot_validator.py::test_schema_uses_only_supported_keywords`, то есть
расширение схемы не пройдёт мимо валидатора молча).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# --- 1. JSON Schema сценария (02 §1.4, дословно) -----------------------------

SCENARIO_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://chat.partner-lead-centre.ru/schemas/bot-scenario-v1.json",
    "title": "LeadChat bot scenario v1",
    "type": "object",
    "required": ["version", "entry", "steps"],
    "additionalProperties": False,
    "properties": {
        "version": {"const": 1},
        "revision": {"type": "integer", "minimum": 0},
        "entry": {"$ref": "#/$defs/stepId"},
        "settings": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "max_steps_total": {"type": "integer", "minimum": 10, "maximum": 500},
                "max_steps_per_tick": {"type": "integer", "minimum": 5, "maximum": 50},
                "max_bot_messages_row": {"type": "integer", "minimum": 2, "maximum": 10},
                "max_offscript_messages": {"type": "integer", "minimum": 1, "maximum": 5},
            },
        },
        "steps": {
            "type": "array",
            "minItems": 1,
            "maxItems": 200,
            "items": {"$ref": "#/$defs/step"},
        },
    },
    "$defs": {
        "stepId": {"type": "string", "pattern": r"^[a-z0-9_]{1,64}$"},
        "stepRef": {"oneOf": [{"$ref": "#/$defs/stepId"}, {"type": "null"}]},
        "text": {"type": "string", "minLength": 1, "maxLength": 1000},
        # ⚠ ПУЛ ФОРМУЛИРОВОК ДЛЯ ШАГА `send` (требование владельца 26.08: «формулировка
        # должна быть уникальной всегда»). Одна зашитая строка на всех клиентов —
        # гарантированный повтор; движок берёт из списка один вариант, устойчиво по паре
        # «диалог + шаг». Разрешаем ТОЛЬКО у `send`: у `ask` и `menu` текст участвует в
        # разборе ответа клиента, и пул там потребовал бы своей логики сопоставления,
        # а у `note` заметку читает смена — ей разнообразие ни к чему.
        "textPool": {
            "oneOf": [
                {"$ref": "#/$defs/text"},
                {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 20,
                    "items": {"$ref": "#/$defs/text"},
                },
            ]
        },
        "timeout": {
            "oneOf": [
                {"type": "string", "pattern": r"^\d+(m|h|d)$"},
                {"type": "integer", "minimum": 60, "maximum": 604800},
                {"type": "null"},
            ]
        },
        "validate": {
            "oneOf": [
                {"enum": ["any", "phone", "number"]},
                {
                    "type": "object",
                    "required": ["regex"],
                    "additionalProperties": False,
                    "properties": {"regex": {"type": "string", "maxLength": 200}},
                },
            ]
        },
        "condition": {
            "type": "object",
            "required": ["kind"],
            "oneOf": [
                {
                    "properties": {
                        "kind": {"const": "work_hours"},
                        "from": {"type": "string", "pattern": r"^([01]\d|2[0-3]):[0-5]\d$"},
                        "to": {"type": "string", "pattern": r"^([01]\d|2[0-3]):[0-5]\d$"},
                        "timezone": {"type": "string", "default": "Europe/Moscow"},
                        "days": {
                            "type": "array",
                            "items": {"enum": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                        },
                    },
                    "required": ["kind", "from", "to"],
                    "additionalProperties": False,
                },
                {
                    "properties": {"kind": {"const": "var_exists"}, "var": {"type": "string"}},
                    "required": ["kind", "var"],
                    "additionalProperties": False,
                },
                {
                    "properties": {
                        "kind": {"const": "var_equals"},
                        "var": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "required": ["kind", "var", "value"],
                    "additionalProperties": False,
                },
                {
                    "properties": {
                        "kind": {"const": "text_contains"},
                        "keywords": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                    },
                    "required": ["kind", "keywords"],
                    "additionalProperties": False,
                },
                {
                    "properties": {
                        "kind": {"const": "text_matches"},
                        "regex": {"type": "string", "maxLength": 200},
                    },
                    "required": ["kind", "regex"],
                    "additionalProperties": False,
                },
            ],
        },
        "step": {
            "type": "object",
            "required": ["id", "type", "params"],
            "properties": {
                "id": {"$ref": "#/$defs/stepId"},
                "type": {
                    "enum": [
                        "send",
                        "ask",
                        "menu",
                        "condition",
                        "ai_answer",
                        "handoff",
                        "close",
                        "tag",
                        "note",
                    ]
                },
            },
            "allOf": [
                {
                    "if": {"properties": {"type": {"const": "send"}}},
                    "then": {
                        "required": ["next"],
                        "properties": {
                            "next": {"$ref": "#/$defs/stepId"},
                            "params": {
                                "type": "object",
                                "required": ["text"],
                                "additionalProperties": False,
                                "properties": {"text": {"$ref": "#/$defs/textPool"}},
                            },
                        },
                    },
                },
                {
                    "if": {"properties": {"type": {"const": "ask"}}},
                    "then": {
                        "required": ["next"],
                        "properties": {
                            "next": {"$ref": "#/$defs/stepId"},
                            "on_timeout": {"$ref": "#/$defs/stepRef"},
                            "on_invalid": {"$ref": "#/$defs/stepRef"},
                            "params": {
                                "type": "object",
                                "required": ["var"],
                                "additionalProperties": False,
                                "properties": {
                                    "text": {"oneOf": [{"$ref": "#/$defs/text"}, {"type": "null"}]},
                                    # ⚠ ВЕДУЩЕЕ ПОДЧЁРКИВАНИЕ РАЗРЕШЕНО (31.08). Схема требовала
                                    # начинать с буквы, а `leadbot_admin.default_scenario` сам
                                    # создаёт шаг с `var: "_client_said"` — то есть панель
                                    # генерировала сценарий, который её же проверка объявляла
                                    # неверным. Подчёркивание тут значит «служебная переменная,
                                    # человек её не вводит», и это соглашение стоит сохранить.
                                    "var": {
                                        "type": "string",
                                        "pattern": r"^_?[a-z][a-z0-9_]{0,31}$",
                                    },
                                    "validate": {"$ref": "#/$defs/validate", "default": "any"},
                                    "retry_text": {
                                        "oneOf": [{"$ref": "#/$defs/text"}, {"type": "null"}]
                                    },
                                    "max_attempts": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 5,
                                        "default": 2,
                                    },
                                    "timeout": {"$ref": "#/$defs/timeout", "default": "24h"},
                                },
                            },
                        },
                    },
                },
                {
                    "if": {"properties": {"type": {"const": "menu"}}},
                    "then": {
                        "properties": {
                            "on_no_match": {"$ref": "#/$defs/stepRef"},
                            # ⚠ `on_invalid` У МЕНЮ ОБЪЯВЛЕН, ПОТОМУ ЧТО ДВИЖОК ПО
                            # НЕМУ ХОДИТ: `engine.py:547` берёт `on_no_match or
                            # on_invalid`, `:569` — наоборот. Схема о ключе не
                            # знала, а обход графа его не собирал, значит ссылка
                            # на несуществующий шаг проходила сохранение молча и
                            # срабатывала уже на живом клиенте: `_goto` писал
                            # `bot.broken_ref` и уводил диалог в передачу с
                            # причиной «сценарий изменился» — неправдой.
                            "on_invalid": {"$ref": "#/$defs/stepRef"},
                            "on_timeout": {"$ref": "#/$defs/stepRef"},
                            "params": {
                                "type": "object",
                                "required": ["text", "options"],
                                "additionalProperties": False,
                                "properties": {
                                    "text": {"$ref": "#/$defs/text"},
                                    "var": {"type": "string"},
                                    "options": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 10,
                                        "items": {
                                            "type": "object",
                                            "required": ["id", "label", "match", "next"],
                                            "additionalProperties": False,
                                            "properties": {
                                                "id": {"$ref": "#/$defs/stepId"},
                                                "label": {"type": "string", "maxLength": 100},
                                                "match": {
                                                    "type": "array",
                                                    "minItems": 1,
                                                    "items": {"type": "string"},
                                                },
                                                "next": {"$ref": "#/$defs/stepId"},
                                            },
                                        },
                                    },
                                    "retry_text": {
                                        "oneOf": [{"$ref": "#/$defs/text"}, {"type": "null"}]
                                    },
                                    "max_attempts": {
                                        "type": "integer",
                                        "minimum": 1,
                                        "maximum": 5,
                                        "default": 2,
                                    },
                                    "timeout": {"$ref": "#/$defs/timeout", "default": "24h"},
                                },
                            },
                        },
                    },
                },
                {
                    "if": {"properties": {"type": {"const": "condition"}}},
                    "then": {
                        "properties": {
                            "params": {
                                "type": "object",
                                "required": ["conditions", "else"],
                                "additionalProperties": False,
                                "properties": {
                                    "conditions": {
                                        "type": "array",
                                        "minItems": 1,
                                        "maxItems": 10,
                                        "items": {
                                            "type": "object",
                                            "required": ["if", "next"],
                                            "additionalProperties": False,
                                            "properties": {
                                                "if": {"$ref": "#/$defs/condition"},
                                                "next": {"$ref": "#/$defs/stepId"},
                                            },
                                        },
                                    },
                                    "else": {"$ref": "#/$defs/stepId"},
                                },
                            },
                        },
                    },
                },
                {
                    "if": {"properties": {"type": {"const": "ai_answer"}}},
                    "then": {
                        "required": ["next"],
                        "properties": {
                            "next": {"$ref": "#/$defs/stepId"},
                            "on_low_confidence": {"$ref": "#/$defs/stepRef"},
                            "params": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "confidence_threshold": {
                                        "type": "number",
                                        "minimum": 0,
                                        "maximum": 1,
                                        "default": 0.6,
                                    },
                                    "max_reply_len": {
                                        "type": "integer",
                                        "minimum": 100,
                                        "maximum": 1000,
                                        "default": 800,
                                    },
                                    "context_messages": {
                                        "type": "integer",
                                        "minimum": 2,
                                        # ⚠ ПОТОЛОК ТОТ ЖЕ, ЧТО У РАЗДЕЛА ЛИД-БОТА (31.08).
                                        # Здесь стояло 30, а `leadbot_admin.MAX_CONTEXT_MESSAGES`
                                        # разрешает 100, и раздел настроек это значение
                                        # принимает — боевая запись лид-бота живёт со 100 и
                                        # НЕ проходит собственный валидатор. Два источника
                                        # правды на одно поле: проверка говорит «нельзя»,
                                        # а сохранение разрешает. Свожу к одному.
                                        "maximum": 100,
                                        "default": 10,
                                    },
                                },
                            },
                        },
                    },
                },
                {
                    "if": {"properties": {"type": {"const": "handoff"}}},
                    "then": {
                        "properties": {
                            "params": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "reason": {"type": "string", "default": "scenario"},
                                    "comment": {
                                        "oneOf": [{"$ref": "#/$defs/text"}, {"type": "null"}]
                                    },
                                    "tags": {
                                        "type": "array",
                                        "items": {"type": "string", "maxLength": 50},
                                    },
                                },
                            },
                        },
                        "not": {"required": ["next"]},
                    },
                },
                {
                    "if": {"properties": {"type": {"const": "close"}}},
                    "then": {
                        "properties": {
                            "params": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "text": {"oneOf": [{"$ref": "#/$defs/text"}, {"type": "null"}]},
                                    "silent": {"type": "boolean", "default": False},
                                    # Итог диалога (15.08, «авто создание нужно
                                    # только для бота»): закрытие с "visit"
                                    # делает диалог заявкой в лид-центр.
                                    # Словарь тот же, что в CHECK базы.
                                    "outcome": {
                                        "enum": [
                                            "visit",
                                            "declined",
                                            "not_our_profile",
                                            "spam",
                                            "no_reply",
                                        ]
                                    },
                                },
                            },
                        },
                        "not": {"required": ["next"]},
                    },
                },
                {
                    "if": {"properties": {"type": {"const": "tag"}}},
                    "then": {
                        "required": ["next"],
                        "properties": {
                            "next": {"$ref": "#/$defs/stepId"},
                            "params": {
                                "type": "object",
                                "required": ["tags"],
                                "additionalProperties": False,
                                "properties": {
                                    "tags": {
                                        "type": "array",
                                        "minItems": 1,
                                        "items": {"type": "string", "maxLength": 50},
                                    }
                                },
                            },
                        },
                    },
                },
                {
                    "if": {"properties": {"type": {"const": "note"}}},
                    "then": {
                        "required": ["next"],
                        "properties": {
                            "next": {"$ref": "#/$defs/stepId"},
                            "params": {
                                "type": "object",
                                "required": ["text"],
                                "additionalProperties": False,
                                "properties": {"text": {"$ref": "#/$defs/text"}},
                            },
                        },
                    },
                },
            ],
        },
    },
}


# --- 2. Мини-валидатор JSON Schema (подмножество draft 2020-12) --------------

# Ключевые слова, которые понимает :func:`_check`. Всё, что встретится в схеме
# сверх этого списка, — не проверяется, и это ошибка разработчика схемы:
# тест реестра (tests/unit/test_bot_validator.py) обходит SCENARIO_SCHEMA и
# валит CI, если появилось неподдержанное слово.
SUPPORTED_KEYWORDS: frozenset[str] = frozenset(
    {
        "$schema",
        "$id",
        "$ref",
        "$defs",
        "title",
        "description",
        "default",
        "type",
        "const",
        "enum",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "pattern",
        "minimum",
        "maximum",
        "oneOf",
        "allOf",
        "if",
        "then",
        "not",
    }
)

_TYPE_NAMES = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "null": type(None),
}


@dataclass(frozen=True)
class SchemaError:
    """Нарушение JSON Schema: путь до узла + человекочитаемое сообщение."""

    path: str
    message: str


def _type_ok(value: Any, expected: str) -> bool:
    if expected == "integer":
        # bool — подкласс int в Python, но не integer в JSON Schema
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, int | float) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    python_type = _TYPE_NAMES.get(expected)
    if python_type is None:  # pragma: no cover — ловится тестом реестра
        raise ValueError(f"неизвестный тип в схеме: {expected}")
    return isinstance(value, python_type)


def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    """Только локальные указатели вида ``#/$defs/name`` — других в схеме нет."""
    if not ref.startswith("#/"):  # pragma: no cover
        raise ValueError(f"неподдерживаемый $ref: {ref}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def _join(path: str, part: str) -> str:
    return f"{path}.{part}" if path else part


def _matches(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> bool:
    return not _check(value, schema, "", root)


def _check(
    value: Any, schema: dict[str, Any], path: str, root: dict[str, Any]
) -> list[SchemaError]:
    """Проверить значение по схеме; вернуть все найденные нарушения."""
    errors: list[SchemaError] = []

    if "$ref" in schema:
        errors += _check(value, _resolve(schema["$ref"], root), path, root)

    if "type" in schema and not _type_ok(value, schema["type"]):
        return [*errors, SchemaError(path, f"ожидается тип {schema['type']}")]

    if "const" in schema and value != schema["const"]:
        errors.append(SchemaError(path, f"допустимо только значение {schema['const']!r}"))

    if "enum" in schema and value not in schema["enum"]:
        allowed = ", ".join(repr(v) for v in schema["enum"])
        errors.append(SchemaError(path, f"допустимые значения: {allowed}"))

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(SchemaError(path, f"минимальная длина — {schema['minLength']}"))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(SchemaError(path, f"максимальная длина — {schema['maxLength']}"))
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(SchemaError(path, f"не соответствует формату {schema['pattern']}"))

    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(SchemaError(path, f"минимальное значение — {schema['minimum']}"))
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(SchemaError(path, f"максимальное значение — {schema['maximum']}"))

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(SchemaError(path, f"минимум элементов — {schema['minItems']}"))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(SchemaError(path, f"максимум элементов — {schema['maxItems']}"))
        if "items" in schema:
            for index, item in enumerate(value):
                errors += _check(item, schema["items"], f"{path}[{index}]", root)

    if isinstance(value, dict):
        for name in schema.get("required", ()):
            if name not in value:
                errors.append(SchemaError(_join(path, name), "обязательное поле"))
        props: dict[str, Any] = schema.get("properties", {})
        for name, sub in props.items():
            if name in value:
                errors += _check(value[name], sub, _join(path, name), root)
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    errors.append(SchemaError(_join(path, name), "неизвестное поле"))

    if "oneOf" in schema:
        variants = [_check(value, sub, path, root) for sub in schema["oneOf"]]
        if all(variants):
            # Показываем самую «близкую» ветку — иначе сообщение бесполезно.
            best = min(variants, key=len)
            errors += best or [SchemaError(path, "не подходит ни под один вариант")]

    for sub in schema.get("allOf", ()):
        errors += _check(value, sub, path, root)

    if "if" in schema:
        if _matches(value, schema["if"], root):
            if "then" in schema:
                errors += _check(value, schema["then"], path, root)
        # `else` в схеме сценария не используется — намеренно не поддерживаем

    if "not" in schema and _matches(value, schema["not"], root):
        forbidden = ", ".join(schema["not"].get("required", ())) or "это значение"
        errors.append(SchemaError(path, f"недопустимо: {forbidden}"))

    return errors


def validate_schema(scenario: Any) -> list[SchemaError]:
    """Слой 1: проверка сценария по :data:`SCENARIO_SCHEMA` (02 §1.4)."""
    return _check(scenario, SCENARIO_SCHEMA, "", SCENARIO_SCHEMA)


# --- 3. Семантика графа (02 §5.2) --------------------------------------------

ERROR = "error"
WARNING = "warning"

TERMINAL_TYPES: frozenset[str] = frozenset({"handoff", "close"})
WAITING_TYPES: frozenset[str] = frozenset({"ask", "menu"})

# Системные плейсхолдеры (02 §1.2). Латиница — словарь ботов; русский словарь
# быстрых ответов ({имя}/{менеджер}) сюда не входит намеренно.
SYSTEM_VARS: frozenset[str] = frozenset(
    {"client_name", "item_title", "item_price", "account_title"}
)
# Переменные, которые движок заполняет сам, без шага ask (02 §3.5 эшелон 1
# и exec_ai_answer): телефон вынимается регуляркой из любого входящего.
AUTO_VARS: frozenset[str] = frozenset({"phone", "_ai_confidence"})


def _dict(value: Any) -> dict[str, Any]:
    """Терпимое чтение: не-объект эквивалентен пустому (схема уже отругалась)."""
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


_PLACEHOLDER_RE = re.compile(r"\{([^{}\s]{1,64})\}")

_ISSUE_MESSAGES = {
    "duplicate_id": "Дубль id шага",
    "entry_missing": "Стартовый шаг entry не найден среди steps",
    "broken_ref": "Ссылка на несуществующий шаг",
    "unreachable_step": "Шаг недостижим из стартового",
    "no_terminal_path": "Из шага нет пути к handoff или close — сценарий повиснет",
    "static_loop": "Цикл без вопроса клиенту — бот зациклится сам на себе",
    "var_undefined": "Переменная не объявлена ни одним ask/menu до этого шага",
    "var_shadowed": "Два шага пишут в одну переменную",
    "bad_regex": "Регулярное выражение не компилируется",
    "menu_dup_match": "Ключевое слово встречается в нескольких вариантах меню",
    "menu_dup_option": "Два варианта меню с одним id — второй недостижим и неотличим в переменной",
    "ai_without_kb": "Шаг ai_answer при пустой базе знаний",
    "ask_no_timeout": "Шаг ждёт ответ бесконечно (timeout: null)",
    "first_step_asks": "Сценарий стартует молчаливым ожиданием",
    "schema_invalid": "Сценарий не соответствует схеме",
    "scenario_not_object": "Сценарий должен быть JSON-объектом",
}


@dataclass(frozen=True)
class Issue:
    """Одна находка валидатора — контракт `422` (01 §8.3) и списка ошибок (11 §5.3)."""

    code: str
    level: str  # error | warning
    message: str
    step_id: str | None = None
    field: str | None = None
    ref: str | None = None

    @property
    def is_error(self) -> bool:
        return self.level == ERROR

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "step_id": self.step_id,
            "field": self.field,
            "code": self.code,
            "level": self.level,
            "message": self.message,
        }
        if self.ref is not None:
            data["ref"] = self.ref
        return data


def _issue(
    code: str,
    level: str,
    *,
    step_id: str | None = None,
    field: str | None = None,
    ref: str | None = None,
    detail: str | None = None,
) -> Issue:
    base = _ISSUE_MESSAGES.get(code, code)
    prefix = f"Шаг {step_id}: " if step_id else ""
    message = f"{prefix}{base}" + (f" — {detail}" if detail else "")
    return Issue(code=code, level=level, message=message, step_id=step_id, field=field, ref=ref)


def _step_ids_by_index(scenario: Any) -> list[str | None]:
    steps = scenario.get("steps") if isinstance(scenario, dict) else None
    if not isinstance(steps, list):
        return []
    return [s.get("id") if isinstance(s, dict) else None for s in steps]


def _schema_issues(scenario: Any) -> list[Issue]:
    """Ошибки слоя 1, привязанные к шагу: путь ``steps[3].params.text`` → id шага."""
    ids = _step_ids_by_index(scenario)
    issues: list[Issue] = []
    for err in validate_schema(scenario):
        step_id: str | None = None
        match = re.match(r"^steps\[(\d+)\]", err.path)
        if match:
            index = int(match.group(1))
            if 0 <= index < len(ids):
                step_id = ids[index]
        issues.append(
            Issue(
                code="schema_invalid",
                level=ERROR,
                message=(f"Шаг {step_id}: " if step_id else "")
                + f"{err.path or 'сценарий'}: {err.message}",
                step_id=step_id,
                field=err.path or None,
            )
        )
    return issues


def _edges(step: dict[str, Any]) -> list[tuple[str, Any]]:
    """Все исходящие ссылки шага: ``[(поле, значение), ...]`` (02 §1.1)."""
    stype = step.get("type")
    params = _dict(step.get("params"))
    out: list[tuple[str, Any]] = []
    if stype in ("send", "ask", "ai_answer", "tag", "note"):
        out.append(("next", step.get("next")))
    if stype == "ask":
        out += [("on_timeout", step.get("on_timeout")), ("on_invalid", step.get("on_invalid"))]
    if stype == "menu":
        options = _list(params.get("options"))
        for index, option in enumerate(options):
            if isinstance(option, dict):
                out.append((f"params.options[{index}].next", option.get("next")))
        out += [
            ("on_no_match", step.get("on_no_match")),
            # Тот же ключ, по которому ходит движок (engine.py:547, :569).
            ("on_invalid", step.get("on_invalid")),
            ("on_timeout", step.get("on_timeout")),
        ]
    if stype == "condition":
        conditions = _list(params.get("conditions"))
        for index, cond in enumerate(conditions):
            if isinstance(cond, dict):
                out.append((f"params.conditions[{index}].next", cond.get("next")))
        out.append(("params.else", params.get("else")))
    if stype == "ai_answer":
        out.append(("on_low_confidence", step.get("on_low_confidence")))
    return [(field, target) for field, target in out if target is not None]


def _texts(step: dict[str, Any]) -> list[tuple[str, str]]:
    """Тексты шага, в которых допустима подстановка переменных (02 §1.2)."""
    params = _dict(step.get("params"))
    out = []
    for name in ("text", "retry_text", "comment"):
        value = params.get(name)
        if isinstance(value, str):
            out.append((f"params.{name}", value))
    return out


def _declared_var(step: dict[str, Any]) -> str | None:
    if step.get("type") not in WAITING_TYPES:
        return None
    params = _dict(step.get("params"))
    var = params.get("var")
    return var if isinstance(var, str) and var else None


def _find_static_loops(
    steps: dict[str, dict[str, Any]], graph: dict[str, list[tuple[str, str]]]
) -> list[list[str]]:
    """Циклы среди «бесплатных» шагов — тех, что не ждут ответа клиента.

    Любой цикл, проходящий через ``ask``/``menu``, обязан использовать их
    исходящее ребро, то есть требует сообщения клиента, — такие циклы ловит
    рантайм-лимит `max_steps_total` (02 §2.7), а не валидатор. Здесь ищем
    гарантированное зацикливание внутри одного тика.
    """
    free = {sid for sid, step in steps.items() if step.get("type") not in WAITING_TYPES}
    loops: list[list[str]] = []
    seen: set[frozenset[str]] = set()
    color: dict[str, int] = {}  # 0 — в работе, 1 — закрыт

    def dfs(node: str, stack: list[str]) -> None:
        color[node] = 0
        stack.append(node)
        for _, target in graph.get(node, ()):
            if target not in free:
                continue
            state = color.get(target)
            if state is None:
                dfs(target, stack)
            elif state == 0:  # обратное ребро — цикл
                cycle = stack[stack.index(target) :]
                key = frozenset(cycle)
                if key not in seen:
                    seen.add(key)
                    loops.append(cycle)
        stack.pop()
        color[node] = 1

    for sid in steps:
        if sid in free and sid not in color:
            dfs(sid, [])
    return loops


def validate_scenario(scenario: Any, *, knowledge_base: str | None = None) -> list[Issue]:
    """Полная валидация сценария: слой 1 (схема) + слой 2 (граф), 02 §5.2.

    Возвращает список находок; ``error`` блокирует сохранение, ``warning`` — нет.
    Порядок стабилен: сначала ошибки схемы, затем структурные, затем локальные
    проверки в порядке шагов — фронт рендерит список как есть (11 §5.3).
    """
    if not isinstance(scenario, dict):
        return [_issue("scenario_not_object", ERROR)]

    issues: list[Issue] = _schema_issues(scenario)

    raw_steps = scenario.get("steps")
    if not isinstance(raw_steps, list) or not raw_steps:
        return issues  # без списка шагов семантику проверять не на чем

    # --- дубли id -------------------------------------------------------
    steps: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for raw in raw_steps:
        if not isinstance(raw, dict):
            continue
        sid = raw.get("id")
        if not isinstance(sid, str) or not sid:
            continue
        if sid in steps:
            issues.append(_issue("duplicate_id", ERROR, step_id=sid, field="id"))
            continue  # дальше работаем с первым вхождением
        steps[sid] = raw
        order.append(sid)

    # --- entry ----------------------------------------------------------
    entry = scenario.get("entry")
    if not isinstance(entry, str) or entry not in steps:
        issues.append(
            _issue(
                "entry_missing", ERROR, field="entry", ref=entry if isinstance(entry, str) else None
            )
        )

    # --- ссылочная целостность -----------------------------------------
    graph: dict[str, list[tuple[str, str]]] = {}
    for sid in order:
        edges: list[tuple[str, str]] = []
        for field, target in _edges(steps[sid]):
            if not isinstance(target, str) or target not in steps:
                issues.append(
                    _issue(
                        "broken_ref",
                        ERROR,
                        step_id=sid,
                        field=field,
                        ref=target if isinstance(target, str) else None,
                        detail=f"{field} → {target!r}",
                    )
                )
                continue
            edges.append((field, target))
        graph[sid] = edges

    # --- достижимость из entry -----------------------------------------
    reachable: set[str] = set()
    if isinstance(entry, str) and entry in steps:
        queue = [entry]
        reachable.add(entry)
        while queue:
            current = queue.pop()
            for _, target in graph.get(current, ()):
                if target not in reachable:
                    reachable.add(target)
                    queue.append(target)
    for sid in order:
        if sid not in reachable:
            issues.append(_issue("unreachable_step", WARNING, step_id=sid))

    # --- путь к терминальному шагу --------------------------------------
    # Считаем по ЯВНЫМ рёбрам: дефолтный handoff у пустых on_timeout/on_invalid
    # существует всегда, и с ним проверка выродилась бы в «всегда ок», хотя
    # «повисший» спроектированный путь — именно то, что мы обязаны запретить.
    reverse: dict[str, list[str]] = {sid: [] for sid in order}
    for sid in order:
        for _, target in graph[sid]:
            reverse[target].append(sid)
    can_finish = {sid for sid in order if steps[sid].get("type") in TERMINAL_TYPES}
    stack = list(can_finish)
    while stack:
        current = stack.pop()
        for source in reverse.get(current, ()):
            if source not in can_finish:
                can_finish.add(source)
                stack.append(source)
    for sid in order:
        if sid in reachable and sid not in can_finish:
            issues.append(_issue("no_terminal_path", ERROR, step_id=sid))

    # --- статические циклы ----------------------------------------------
    for cycle in _find_static_loops(steps, graph):
        issues.append(
            _issue(
                "static_loop",
                ERROR,
                step_id=cycle[0],
                detail=" → ".join([*cycle, cycle[0]]),
            )
        )

    # --- переменные ------------------------------------------------------
    # may-анализ: какие переменные объявлены хотя бы на одном пути entry → шаг.
    available: dict[str, set[str]] = {sid: set() for sid in order}
    if isinstance(entry, str) and entry in steps:
        changed = True
        while changed:
            changed = False
            for sid in order:
                if sid not in reachable:
                    continue
                out = set(available[sid])
                declared = _declared_var(steps[sid])
                if declared:
                    out.add(declared)
                for _, target in graph[sid]:
                    if not out <= available[target]:
                        available[target] |= out
                        changed = True

    writers: dict[str, str] = {}
    for sid in order:
        step = steps[sid]
        declared = _declared_var(step)
        if declared:
            if declared in writers:
                issues.append(
                    _issue(
                        "var_shadowed",
                        WARNING,
                        step_id=sid,
                        field="params.var",
                        detail=f"{declared} уже пишет шаг {writers[declared]}",
                    )
                )
            else:
                writers[declared] = sid

        for field, text in _texts(step):
            if sid not in reachable:
                continue  # о недостижимом шаге уже предупредили — не шумим дважды
            known = SYSTEM_VARS | AUTO_VARS | available[sid]
            for name in _PLACEHOLDER_RE.findall(text):
                if name not in known:
                    issues.append(
                        _issue(
                            "var_undefined",
                            WARNING,
                            step_id=sid,
                            field=field,
                            ref=name,
                            detail=f"{{{name}}}",
                        )
                    )

        # --- regex -------------------------------------------------------
        params = _dict(step.get("params"))
        regexes: list[tuple[str, Any]] = []
        validate = params.get("validate")
        if isinstance(validate, dict):
            regexes.append(("params.validate.regex", validate.get("regex")))
        if step.get("type") == "condition":
            for index, cond in enumerate(_list(params.get("conditions"))):
                if isinstance(cond, dict) and isinstance(cond.get("if"), dict):
                    if cond["if"].get("kind") == "text_matches":
                        regexes.append(
                            (f"params.conditions[{index}].if.regex", cond["if"].get("regex"))
                        )
        for field, pattern in regexes:
            if not isinstance(pattern, str):
                continue
            if len(pattern) > 200:
                issues.append(
                    _issue(
                        "bad_regex", ERROR, step_id=sid, field=field, detail="длиннее 200 символов"
                    )
                )
                continue
            try:
                re.compile(pattern)
            except re.error as exc:
                issues.append(_issue("bad_regex", ERROR, step_id=sid, field=field, detail=str(exc)))

        # --- меню: дубли id вариантов и ключевых слов -----------------------
        if step.get("type") == "menu":
            seen_keywords: dict[str, str] = {}
            seen_options: set[str] = set()
            options = _list(params.get("options"))
            for option in options:
                if not isinstance(option, dict):
                    continue
                option_id = str(option.get("id"))
                # Ошибка, а не предупреждение (проверка 24.09): совпадение ищется до
                # первого варианта, а в переменную пишется id — два варианта с одним
                # id неразличимы ни для движка, ни для условия `var_equals`.
                if option_id in seen_options:
                    issues.append(
                        _issue(
                            "menu_dup_option",
                            ERROR,
                            step_id=sid,
                            field="params.options",
                            ref=option_id,
                            detail=f"«{option_id}»",
                        )
                    )
                seen_options.add(option_id)
                for keyword in _list(option.get("match")):
                    if not isinstance(keyword, str):
                        continue
                    key = keyword.strip().lower()
                    if key in seen_keywords:
                        issues.append(
                            _issue(
                                "menu_dup_match",
                                WARNING,
                                step_id=sid,
                                field="params.options",
                                ref=key,
                                detail=f"«{key}» в вариантах {seen_keywords[key]} и {option_id}",
                            )
                        )
                    else:
                        seen_keywords[key] = option_id

        # --- локальные предупреждения ------------------------------------
        if step.get("type") == "ai_answer" and not (knowledge_base or "").strip():
            issues.append(_issue("ai_without_kb", WARNING, step_id=sid))
        if step.get("type") in WAITING_TYPES and "timeout" in params and params["timeout"] is None:
            issues.append(_issue("ask_no_timeout", WARNING, step_id=sid, field="params.timeout"))

    if isinstance(entry, str) and entry in steps:
        entry_step = steps[entry]
        entry_params = _dict(entry_step.get("params"))
        if entry_step.get("type") == "ask" and not entry_params.get("text"):
            issues.append(_issue("first_step_asks", WARNING, step_id=entry))

    return issues


# --- 4. Помощники для API ----------------------------------------------------


def errors_only(issues: list[Issue]) -> list[Issue]:
    return [i for i in issues if i.is_error]


def has_errors(issues: list[Issue]) -> bool:
    return any(i.is_error for i in issues)


def issues_details(issues: list[Issue]) -> dict[str, Any]:
    """`details` для `422 bot_scenario_invalid` (01 §8.3 + 11 §5.3).

    Первая ошибка разложена по полям `step_id`/`reason`/`ref` — как в примере
    01 §8.3; полный список уходит в `issues`, из него редактор рисует
    список под панелью действий.
    """
    errors = errors_only(issues)
    head = errors[0] if errors else None
    details: dict[str, Any] = {"issues": [i.as_dict() for i in issues]}
    if head is not None:
        details["step_id"] = head.step_id
        details["reason"] = head.code
        if head.ref is not None:
            details["ref"] = head.ref
    return details


def first_error_message(issues: list[Issue]) -> str:
    errors = errors_only(issues)
    return errors[0].message if errors else "Сценарий не прошёл валидацию"
