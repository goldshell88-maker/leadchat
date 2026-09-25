"""Валидатор сценария бота — 02 §1.4 (JSON Schema) и §5.2 (семантика графа).

Главный инвариант всего спринта: **сценарий, который не сохранить, не должен
попасть в прод**, а сценарий из документации — обязан сохраняться. Поэтому
здесь два вида тестов: таблица кодов §5.2 (по одному кейсу на код) и два
эталона — дефолтный «Первичный приём» и пример 02 §1.5 — которые должны быть
валидны всегда.
"""

import copy
import json
from typing import Any

import pytest

from app.bots.scenarios import default_scenario
from app.bots.validator import (
    ERROR,
    SCENARIO_SCHEMA,
    SUPPORTED_KEYWORDS,
    WARNING,
    Issue,
    errors_only,
    first_error_message,
    has_errors,
    issues_details,
    validate_scenario,
)

KB = "Замена экрана iPhone 13 — от 8900 ₽, срок 1–2 часа."


def codes(issues: list[Issue]) -> list[str]:
    return [i.code for i in issues]


def codes_of(scenario: Any, knowledge_base: str = KB) -> list[str]:
    return codes(validate_scenario(scenario, knowledge_base=knowledge_base))


def one(issues: list[Issue], code: str) -> Issue:
    found = [i for i in issues if i.code == code]
    assert found, f"ожидался код {code}, получены: {codes(issues)}"
    return found[0]


# --- эталоны -----------------------------------------------------------------


def test_default_scenario_is_valid_against_its_own_schema():
    """Дефолтный «Первичный приём» обязан быть валиден — его получает каждый
    новый бот (02 §5.1), и сломанный дефолт означал бы, что продукт нельзя
    даже начать использовать."""
    issues = validate_scenario(default_scenario(), knowledge_base=KB)
    assert issues == [], [i.message for i in issues]


def test_default_scenario_never_closes_the_dialog_by_timeout():
    """Решение владельца: автозакрытия по таймауту НЕТ — диалог живёт, пока
    менеджер не закроет вручную. Ветка молчания клиента ведёт в handoff."""
    scenario = default_scenario()
    steps = {s["id"]: s for s in scenario["steps"]}
    assert not [s for s in steps.values() if s["type"] == "close"]
    # ⚠ 15.08: on_timeout ведёт сперва на ПИНГ (регламент: 5 минут тишины →
    # уточняющий вопрос), но конец цепочки — по-прежнему handoff, не close:
    # решение владельца №2 (нет автозакрытий) в силе.
    ask_problem = steps["ask_problem"]
    ping = steps[ask_problem["on_timeout"]]
    assert ping["type"] == "send"  # пинг молчащему
    retry = steps[ping["next"]]
    assert steps[retry["on_timeout"]]["type"] == "handoff"
    # ночная ветка тоже: пинг, затем молчание = передать оператору, а не закрыть
    ping_phone = steps[steps["ask_phone"]["on_timeout"]]
    assert ping_phone["type"] == "send"
    retry_phone = steps[ping_phone["next"]]
    assert steps[retry_phone["on_timeout"]]["type"] == "handoff"


def test_documentation_example_is_valid():
    """Пример 02 §1.5 (с шагом `close`) — тоже валидная конфигурация.

    Он отличается от дефолта только веткой таймаута; сам движок шаг `close`
    поддерживает, и валидатор не должен его запрещать.
    """
    scenario = default_scenario()
    steps = {s["id"]: s for s in scenario["steps"]}
    # 15.08: ветка молчания стала цепочкой «пинг → повторное ожидание», поэтому
    # close ставится в её КОНЕЦ (ask_problem_2), а не на первый таймаут — иначе
    # пинг-шаги повисают недостижимыми
    steps["ask_problem_2"]["on_timeout"] = "close_silent"
    scenario["steps"] = [s for s in scenario["steps"] if s["id"] != "handoff_no_reply"]
    scenario["steps"].append(
        {"id": "close_silent", "type": "close", "params": {"text": None, "silent": True}}
    )
    assert validate_scenario(scenario, knowledge_base=KB) == []


def test_scenario_survives_json_round_trip():
    """Сценарий хранится в JSONB — валидатор обязан видеть то же самое."""
    scenario = json.loads(json.dumps(default_scenario()))
    assert validate_scenario(scenario, knowledge_base=KB) == []


# --- слой 1: JSON Schema (02 §1.4) ------------------------------------------


def test_schema_uses_only_supported_keywords():
    """Реестр-страж: расширили схему — расширьте и мини-валидатор.

    Незнакомое ключевое слово молча НЕ проверялось бы, и «валидный» сценарий
    уехал бы в прод — поэтому обход схемы обязателен.
    """

    def walk(node: Any, path: str) -> list[str]:
        bad: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("properties", "$defs"):
                    for name, sub in value.items():
                        bad += walk(sub, f"{path}.{key}.{name}")
                    continue
                if key not in SUPPORTED_KEYWORDS:
                    bad.append(f"{path}.{key}")
                    continue
                if key in ("oneOf", "allOf"):
                    for index, sub in enumerate(value):
                        bad += walk(sub, f"{path}.{key}[{index}]")
                elif key in ("items", "if", "then", "not"):
                    bad += walk(value, f"{path}.{key}")
        return bad

    unsupported = walk(SCENARIO_SCHEMA, "$")
    assert not unsupported, f"мини-валидатор не знает: {unsupported}"


@pytest.mark.parametrize(
    "mutate,expect_field",
    [
        (lambda s: s.pop("version"), "version"),
        (lambda s: s.update(version=2), "version"),
        (lambda s: s.pop("steps"), "steps"),
        (lambda s: s.update(steps=[]), "steps"),
        (lambda s: s.update(unexpected="x"), "unexpected"),
        (lambda s: s.update(settings={"max_steps_total": 5}), "settings.max_steps_total"),
        (lambda s: s.update(settings={"unknown": 1}), "settings.unknown"),
    ],
)
def test_schema_rejects_broken_envelope(mutate, expect_field):
    scenario = default_scenario()
    mutate(scenario)
    issues = validate_scenario(scenario, knowledge_base=KB)
    assert has_errors(issues)
    schema_issue = one(issues, "schema_invalid")
    assert any(i.field == expect_field for i in issues if i.code == "schema_invalid"), (
        f"{expect_field} не найден среди {[i.field for i in issues]}"
    )
    assert schema_issue.level == ERROR


@pytest.mark.parametrize(
    "patch,description",
    [
        ({"type": "sing"}, "неизвестный тип шага"),
        ({"params": {"text": ""}}, "пустой текст"),
        ({"params": {"text": "x" * 1001}}, "текст длиннее 1000"),
        ({"params": {"text": "ok", "extra": 1}}, "лишний параметр"),
        ({"params": {}}, "нет обязательного text"),
        ({"id": "Greet"}, "id не по паттерну"),
        ({"next": None}, "next обязателен у send"),
    ],
)
def test_schema_rejects_broken_send_step(patch, description):
    scenario = default_scenario()
    scenario["steps"][0].update(patch)
    issues = validate_scenario(scenario, knowledge_base=KB)
    assert has_errors(issues), description
    assert "schema_invalid" in codes(issues), description


def test_schema_binds_error_to_the_step_id():
    """`422` рисуется списком с подсветкой карточки (11 §5.3) — нужен step_id."""
    scenario = default_scenario()
    scenario["steps"][1]["params"]["var"] = "Телефон"  # только латиница, 02 §1.3
    issue = one(validate_scenario(scenario, knowledge_base=KB), "schema_invalid")
    assert issue.step_id == "ask_problem"
    assert issue.field == "steps[1].params.var"


def test_schema_rejects_next_on_terminal_steps():
    scenario = default_scenario()
    handoff = next(s for s in scenario["steps"] if s["type"] == "handoff")
    handoff["next"] = "greet"
    assert "schema_invalid" in codes_of(scenario)


@pytest.mark.parametrize("timeout", ["24", "2 h", 30, 999999999, "24hh"])
def test_schema_rejects_bad_timeout(timeout):
    scenario = default_scenario()
    scenario["steps"][1]["params"]["timeout"] = timeout
    assert "schema_invalid" in codes_of(scenario)


@pytest.mark.parametrize("timeout", ["30m", "2h", "3d", 60, 604800, None])
def test_schema_accepts_documented_timeouts(timeout):
    scenario = default_scenario()
    scenario["steps"][1]["params"]["timeout"] = timeout
    assert "schema_invalid" not in codes_of(scenario)


def test_schema_checks_condition_variants():
    scenario = default_scenario()
    check = next(s for s in scenario["steps"] if s["id"] == "check_hours")
    check["params"]["conditions"][0]["if"] = {"kind": "work_hours", "from": "25:00", "to": "20:00"}
    assert "schema_invalid" in codes_of(scenario)

    check["params"]["conditions"][0]["if"] = {"kind": "var_exists", "var": "phone"}
    assert "schema_invalid" not in codes_of(scenario)

    check["params"]["conditions"][0]["if"] = {"kind": "нечто", "var": "phone"}
    assert "schema_invalid" in codes_of(scenario)


def test_schema_validates_menu_options():
    scenario = _menu_scenario()
    scenario["steps"][1]["params"]["options"] = []
    assert "schema_invalid" in codes_of(scenario)


def test_non_object_scenario_is_rejected_without_crashing():
    garbage: list[Any] = [None, [], "scenario", 42]
    for item in garbage:
        issues = validate_scenario(item)
        assert has_errors(issues)
        assert codes(issues) == ["scenario_not_object"]


# --- слой 2: семантика графа (02 §5.2) --------------------------------------


def test_broken_ref_is_an_error_and_names_the_target():
    scenario = default_scenario()
    scenario["steps"][0]["next"] = "ask_problm"
    issues = validate_scenario(scenario, knowledge_base=KB)
    issue = one(issues, "broken_ref")
    assert issue.level == ERROR
    assert issue.step_id == "greet" and issue.field == "next" and issue.ref == "ask_problm"
    assert "ask_problm" in issue.message


def test_broken_ref_is_found_in_every_kind_of_link():
    """Ссылки живут в шести местах (02 §1.1) — валидатор обязан знать все."""
    scenario = _menu_scenario()
    steps = {s["id"]: s for s in scenario["steps"]}
    steps["pick"]["params"]["options"][0]["next"] = "nowhere1"
    steps["pick"]["on_no_match"] = "nowhere2"
    steps["pick"]["on_timeout"] = "nowhere3"
    steps["branch"]["params"]["conditions"][0]["next"] = "nowhere4"
    steps["branch"]["params"]["else"] = "nowhere5"
    issues = validate_scenario(scenario, knowledge_base=KB)
    fields = {i.field for i in issues if i.code == "broken_ref"}
    assert fields == {
        "params.options[0].next",
        "on_no_match",
        "on_timeout",
        "params.conditions[0].next",
        "params.else",
    }


def test_entry_missing_is_an_error():
    scenario = default_scenario()
    scenario["entry"] = "nowhere"
    issue = one(validate_scenario(scenario, knowledge_base=KB), "entry_missing")
    assert issue.level == ERROR and issue.field == "entry"


def test_duplicate_id_is_an_error():
    scenario = default_scenario()
    scenario["steps"].append(
        {"id": "greet", "type": "send", "params": {"text": "дубль"}, "next": "greet"}
    )
    issue = one(validate_scenario(scenario, knowledge_base=KB), "duplicate_id")
    assert issue.level == ERROR and issue.step_id == "greet"


def test_unreachable_step_is_only_a_warning():
    """Черновики бывают с «висящими» шагами — сохранить можно (02 §5.2)."""
    scenario = default_scenario()
    scenario["steps"].append({"id": "orphan", "type": "handoff", "params": {"reason": "scenario"}})
    issues = validate_scenario(scenario, knowledge_base=KB)
    issue = one(issues, "unreachable_step")
    assert issue.level == WARNING and issue.step_id == "orphan"
    assert not has_errors(issues)


def test_no_terminal_path_is_an_error():
    """«Повисший» сценарий запрещён: из шага обязан быть путь в handoff/close."""
    scenario = {
        "version": 1,
        "entry": "a",
        "steps": [
            {"id": "a", "type": "send", "params": {"text": "раз"}, "next": "b"},
            {"id": "b", "type": "ask", "params": {"var": "x", "timeout": "24h"}, "next": "a"},
        ],
    }
    issues = validate_scenario(scenario, knowledge_base=KB)
    assert {i.step_id for i in issues if i.code == "no_terminal_path"} == {"a", "b"}
    assert has_errors(issues)


def test_static_loop_is_an_error_and_shows_the_cycle():
    """Цикл без ask/menu крутится внутри одного тика — ловим статически."""
    branch = {
        "id": "b",
        "type": "condition",
        "params": {
            "conditions": [{"if": {"kind": "var_exists", "var": "phone"}, "next": "end"}],
            "else": "a",  # обратное ребро: цикл a → b → a без единого вопроса
        },
    }
    scenario = {
        "version": 1,
        "entry": "a",
        "steps": [
            {"id": "a", "type": "tag", "params": {"tags": ["x"]}, "next": "b"},
            branch,
            {"id": "end", "type": "handoff", "params": {}},
        ],
    }
    issues = validate_scenario(scenario, knowledge_base=KB)
    issue = one(issues, "static_loop")
    assert issue.level == ERROR
    assert "a → b → a" in issue.message


def test_loop_through_ask_is_not_a_static_loop():
    """Цикл через вопрос клиенту — законен: его сдерживает `max_steps_total`
    (02 §2.7), запрещать такие сценарии валидатором нельзя."""
    scenario = {
        "version": 1,
        "entry": "ask_again",
        "steps": [
            {
                "id": "ask_again",
                "type": "ask",
                "params": {"text": "Ваш номер?", "var": "phone", "timeout": "2h"},
                "next": "ask_again",
                "on_timeout": "bye",
                "on_invalid": "bye",
            },
            {"id": "bye", "type": "handoff", "params": {}},
        ],
    }
    assert "static_loop" not in codes_of(scenario)


def test_var_undefined_is_a_warning_and_knows_system_names():
    scenario = default_scenario()
    steps = {s["id"]: s for s in scenario["steps"]}
    steps["note_contact"]["params"]["text"] = (
        "{client_name} {item_title} {item_price} {account_title} {problem} {phone} {device_model}"
    )
    issues = validate_scenario(scenario, knowledge_base=KB)
    issue = one(issues, "var_undefined")
    assert issue.level == WARNING and issue.ref == "device_model"
    assert len([i for i in issues if i.code == "var_undefined"]) == 1


def test_var_undefined_catches_the_russian_dictionary_of_quick_replies():
    """`{имя}` — словарь быстрых ответов операторов (01 §7), не ботов (02 §1.2).
    Смешивать их нельзя, и валидатор обязан это заметить."""
    scenario = default_scenario()
    scenario["steps"][0]["params"]["text"] = "Здравствуйте, {имя}!"
    assert one(validate_scenario(scenario, knowledge_base=KB), "var_undefined").ref == "имя"


def test_var_declared_earlier_on_the_path_is_known():
    scenario = default_scenario()
    steps = {s["id"]: s for s in scenario["steps"]}
    steps["night_msg"]["params"]["text"] = "Проблема «{problem}» ясна, оставьте телефон ✔"
    assert "var_undefined" not in codes_of(scenario)


def test_var_shadowed_is_a_warning():
    scenario = default_scenario()
    scenario["steps"].insert(
        1,
        {
            "id": "ask_again",
            "type": "ask",
            "params": {"text": "И ещё раз?", "var": "problem", "timeout": "1h"},
            "next": "ask_problem",
            "on_timeout": None,
            "on_invalid": None,
        },
    )
    issue = one(validate_scenario(scenario, knowledge_base=KB), "var_shadowed")
    assert issue.level == WARNING


@pytest.mark.parametrize("bad", ["[unclosed", "(?P<", "*"])
def test_bad_regex_is_an_error(bad):
    scenario = default_scenario()
    scenario["steps"][1]["params"]["validate"] = {"regex": bad}
    issue = one(validate_scenario(scenario, knowledge_base=KB), "bad_regex")
    assert issue.level == ERROR and issue.step_id == "ask_problem"


def test_bad_regex_in_condition_is_an_error():
    scenario = default_scenario()
    check = next(s for s in scenario["steps"] if s["id"] == "check_hours")
    check["params"]["conditions"][0]["if"] = {"kind": "text_matches", "regex": "([a-z"}
    issue = one(validate_scenario(scenario, knowledge_base=KB), "bad_regex")
    assert issue.field == "params.conditions[0].if.regex"


def test_menu_dup_match_is_a_warning():
    scenario = _menu_scenario()
    steps = {s["id"]: s for s in scenario["steps"]}
    steps["pick"]["params"]["options"][1]["match"].append("Телефон")
    issue = one(validate_scenario(scenario, knowledge_base=KB), "menu_dup_match")
    assert issue.level == WARNING and issue.ref == "телефон"


def test_menu_dup_option_id_is_an_error():
    """Проверка 24.09: «Добавить вариант» после удаления давал второй `opt_2` —
    совпадение ищется до первого варианта, и второй становился недостижимым."""
    scenario = _menu_scenario()
    steps = {s["id"]: s for s in scenario["steps"]}
    steps["pick"]["params"]["options"][1]["id"] = "mobile"
    issue = one(validate_scenario(scenario, knowledge_base=KB), "menu_dup_option")
    assert issue.level == ERROR and issue.ref == "mobile"


def test_ai_without_kb_is_a_warning_only_without_knowledge_base():
    assert "ai_without_kb" in codes_of(default_scenario(), knowledge_base="")
    assert "ai_without_kb" in codes_of(default_scenario(), knowledge_base="   ")
    assert "ai_without_kb" not in codes_of(default_scenario(), knowledge_base=KB)


def test_ask_no_timeout_is_a_warning():
    scenario = default_scenario()
    scenario["steps"][1]["params"]["timeout"] = None
    issue = one(validate_scenario(scenario, knowledge_base=KB), "ask_no_timeout")
    assert issue.level == WARNING and issue.field == "params.timeout"


def test_first_step_asks_is_a_warning():
    scenario = default_scenario()
    scenario["entry"] = "ask_problem"  # ask без text: бот стартует молча
    issues = validate_scenario(scenario, knowledge_base=KB)
    assert one(issues, "first_step_asks").level == WARNING


# --- контракт ответа `422` (01 §8.3, 11 §5.3) -------------------------------


def test_issue_dict_carries_the_editor_contract():
    """`Issue` и схема ответа API — один и тот же контракт (01 §8.3, 11 §5.3)."""
    from app.schemas.bots import ScenarioIssueOut

    scenario = default_scenario()
    scenario["steps"][0]["next"] = "nope"
    issues = validate_scenario(scenario, knowledge_base=KB)
    issue = one(issues, "broken_ref")
    assert set(issue.as_dict()) >= {"step_id", "field", "code", "level", "message"}
    for item in issues:  # каждая находка обязана пролезать в схему ответа
        assert ScenarioIssueOut(**item.as_dict()).code == item.code


def test_issues_details_head_matches_the_documented_example():
    scenario = default_scenario()
    # пример из 01 §8.3: ломаем ссылку у ask_phone (ищем по id — позиции плавают)
    _ask_phone = next(x for x in scenario["steps"] if x["id"] == "ask_phone")
    _ask_phone["next"] = "tag_contct"
    issues = validate_scenario(scenario, knowledge_base=KB)
    details = issues_details(issues)
    assert details["step_id"] == "ask_phone"
    assert details["reason"] == "broken_ref"
    assert details["ref"] == "tag_contct"
    assert [i["code"] for i in details["issues"]] == codes(issues)
    assert "ask_phone" in first_error_message(issues)


def test_warnings_alone_do_not_block_saving():
    issues = validate_scenario(default_scenario(), knowledge_base="")
    assert issues and not has_errors(issues)
    assert errors_only(issues) == []


def test_validation_is_deterministic():
    scenario = default_scenario()
    scenario["steps"][0]["next"] = "nope"
    first = [i.as_dict() for i in validate_scenario(scenario, knowledge_base=KB)]
    second = [i.as_dict() for i in validate_scenario(copy.deepcopy(scenario), knowledge_base=KB)]
    assert first == second


# --- фикстуры-сценарии -------------------------------------------------------


def _menu_scenario() -> dict[str, Any]:
    """Сценарий с `menu` и `condition` — типы, которых нет в дефолтном."""
    return {
        "version": 1,
        "entry": "pick",
        "steps": [
            {
                "id": "pick",
                "type": "menu",
                "params": {
                    "text": "Какая техника?\n1. Телефон\n2. Ноутбук",
                    "var": "device_kind",
                    "options": [
                        {
                            "id": "mobile",
                            "label": "Телефон",
                            "match": ["1", "телефон"],
                            "next": "branch",
                        },
                        {
                            "id": "laptop",
                            "label": "Ноутбук",
                            "match": ["2", "ноутбук"],
                            "next": "branch",
                        },
                    ],
                    "retry_text": "Ответьте цифрой 1–2 🙂",
                    "max_attempts": 2,
                    "timeout": "24h",
                },
                "on_no_match": None,
                "on_timeout": None,
            },
            {
                "id": "branch",
                "type": "condition",
                "params": {
                    "conditions": [{"if": {"kind": "var_exists", "var": "phone"}, "next": "done"}],
                    "else": "done",
                },
            },
            {"id": "done", "type": "handoff", "params": {"reason": "scenario"}},
        ],
    }


def test_menu_fixture_itself_is_valid():
    assert validate_scenario(_menu_scenario(), knowledge_base=KB) == []


def test_close_accepts_a_dictionary_outcome_and_rejects_a_typo():
    """Итог у `close` — из закрытого словаря, опечатка ловится ДО сохранения.

    Шаг появился 15 августа («авто создание нужно только для бота»): закрытие
    с итогом «visit» делает диалог заявкой в лид-центр. Словарь тот же, что в
    CHECK-ограничении базы; движок вторым рубежом молча пропустит опечатку из
    сценария, приехавшего мимо валидатора, — но редактор обязан остановить её
    здесь, пока сценарий правит человек.
    """
    scenario = {
        "version": 1,
        "entry": "bye",
        "steps": [{"id": "bye", "type": "close", "params": {"outcome": "visit"}}],
    }
    assert not has_errors(validate_scenario(scenario, knowledge_base=KB))

    scenario["steps"][0]["params"]["outcome"] = "vizit"
    assert has_errors(validate_scenario(scenario, knowledge_base=KB))
