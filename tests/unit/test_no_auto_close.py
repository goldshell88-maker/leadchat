"""Молчание клиента не закрывает диалог само — ветка таймаута ведёт к человеку.

РЕШЕНИЕ ВЛАДЕЛЬЦА, ЗАПИСАННОЕ В ЗАГОТОВКЕ: «автоматического закрытия по таймауту НЕТ,
диалог живёт, пока менеджер не закроет его руками». В боевом сценарии, собранном в
панели, последним шагом стоял `close_silent` типа `close`.

ДАННЫЕ БОЯ НА 26.08 (2566 смен статуса): бот закрывал диалог ЧЕТЫРЕ раза за всё время —
выключение ничего не ломает; при этом СИСТЕМА 517 раз переоткрывала закрытый диалог,
потому что клиент написал снова. Закрытие клиента не останавливает.
"""

from __future__ import annotations

import importlib.util
import pathlib

from app.bots.scenarios import default_scenario

МИГРАЦИЯ = (
    pathlib.Path(__file__).resolve().parents[2] / "app/db/migrations/versions/0053_no_auto_close.py"
)


def _модуль():
    spec = importlib.util.spec_from_file_location("m0053", МИГРАЦИЯ)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


def test_в_заготовке_автозакрытия_нет() -> None:
    шаги = default_scenario()["steps"]
    закрытия = [s for s in шаги if s.get("type") == "close"]
    assert not закрытия, закрытия


def test_боевая_ветка_таймаута_переводится_к_человеку() -> None:
    """Ровно та форма, что лежит в боевой базе."""
    боевой = {
        "steps": [
            {
                "id": "wait_client_2",
                "type": "ask",
                "params": {"timeout": "24h"},
                "next": "ask_leadbot",
                "on_timeout": "close_silent",
            },
            {"id": "close_silent", "type": "close", "params": {}},
        ]
    }
    n = _модуль()._починить(боевой)
    assert n == 1
    ждущий = боевой["steps"][0]
    assert ждущий["on_timeout"] == "handoff_no_reply"
    добавлен = [s for s in боевой["steps"] if s["id"] == "handoff_no_reply"]
    assert добавлен and добавлен[0]["type"] == "handoff"
    # сам шаг close остаётся: админ вправе собрать им свою ветку осознанно
    assert any(s["id"] == "close_silent" for s in боевой["steps"])


def test_прощание_с_текстом_не_трогаем() -> None:
    """`close` с текстом — это прощание, написанное админом, а не молчание клиента."""
    сценарий = {
        "steps": [
            {"id": "ask", "type": "ask", "on_timeout": "bye"},
            {"id": "bye", "type": "close", "params": {"text": "Спасибо, что обратились"}},
        ]
    }
    assert _модуль()._починить(сценарий) == 0
    assert сценарий["steps"][0]["on_timeout"] == "bye"


def test_повторный_прогон_ничего_не_меняет() -> None:
    сценарий = {
        "steps": [
            {"id": "w", "type": "ask", "on_timeout": "close_silent"},
            {"id": "close_silent", "type": "close", "params": {}},
        ]
    }
    m = _модуль()
    assert m._починить(сценарий) == 1
    assert m._починить(сценарий) == 0
    assert sum(1 for s in сценарий["steps"] if s["id"] == "handoff_no_reply") == 1
