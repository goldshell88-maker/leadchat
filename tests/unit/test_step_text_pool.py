"""Формулировка шага берётся из пула и уникальна между диалогами.

ТРЕБОВАНИЕ ВЛАДЕЛЬЦА 26.08: «формулировка должна быть уникальной всегда и подходить к
контексту». Шаг `ping_problem` держал ОДНУ зашитую строку — её слово в слово получали
все, кто замолчал. Тот же разбор, что у зерна вариативности в лид-боте: уникальность
держится не текстом, а выбором из пула по САМОМУ ДИАЛОГУ.

ДВА СВОЙСТВА, И ОБА ОБЯЗАТЕЛЬНЫ:
  · между диалогами формулировки расходятся — это и есть уникальность;
  · внутри одного диалога и шага формулировка НЕ скачет: повторный проход не должен
    читаться как сбой («он мне это уже писал, только другими словами»).

ЧЕГО ПРИЁМ НЕ ДАЁТ, СКАЗАНО ЧЕСТНО: пул подбирает формулировку под ШАГ, а не под
конкретную реплику клиента. Шаг сам по себе и есть контекст — «клиент не описал
поломку», и все варианты спрашивают именно о ней.
"""

from __future__ import annotations

import json
import pathlib
import uuid
from types import SimpleNamespace

import pytest

from app.bots.engine import ScenarioEngine
from app.bots.steps import Step

КОРЕНЬ = pathlib.Path(__file__).resolve().parents[2]
ПУЛ = [f"вариант {i}" for i in range(8)]


def _шаг() -> Step:
    return Step(id="ping_problem", type="send", params={"text": list(ПУЛ)}, next=None)


def _движок(conv_id: uuid.UUID) -> ScenarioEngine:
    """Нам нужен только выбор текста — остальной движок не поднимаем."""
    движок = ScenarioEngine.__new__(ScenarioEngine)
    движок.conv = SimpleNamespace(id=conv_id)
    return движок


def test_разные_диалоги_получают_разные_формулировки() -> None:
    выбор = {_движок(uuid.uuid4())._текст_шага(_шаг()) for _ in range(60)}
    assert len(выбор) > 1, "все диалоги получают одну формулировку — пул не работает"


def test_внутри_диалога_формулировка_не_скачет() -> None:
    conv = uuid.uuid4()
    первый = _движок(conv)._текст_шага(_шаг())
    for _ in range(10):
        assert _движок(conv)._текст_шага(_шаг()) == первый


def test_обычная_строка_остаётся_строкой() -> None:
    """Шаги с одним текстом должны работать ровно как раньше."""
    шаг = Step(id="s", type="send", params={"text": "просто текст"}, next=None)
    assert _движок(uuid.uuid4())._текст_шага(шаг) == "просто текст"


def test_пустой_пул_не_роняет_шаг() -> None:
    шаг = Step(id="s", type="send", params={"text": ["", "   "]}, next=None)
    assert _движок(uuid.uuid4())._текст_шага(шаг) is None


def test_выбор_только_из_пула() -> None:
    for _ in range(30):
        assert _движок(uuid.uuid4())._текст_шага(_шаг()) in ПУЛ


def test_шаг_отправки_действительно_зовёт_выбор() -> None:
    """Проверки выше дёргают выбор напрямую и зелены с вырезанным вызовом из `exec_send`.

    На этих граблях за день я стоял четыре раза: правка написана, а к делу не подключена.
    """
    import ast as _ast

    исходник = (КОРЕНЬ / "app" / "bots" / "engine.py").read_text("utf-8")
    дерево = _ast.parse(исходник)
    тело = ""
    for узел in _ast.walk(дерево):
        if isinstance(узел, _ast.AsyncFunctionDef) and узел.name == "exec_send":
            тело = _ast.get_source_segment(исходник, узел) or ""
    assert тело, "шаг exec_send не найден"
    assert "self._текст_шага(step)" in тело, (
        "exec_send берёт текст мимо выбора из пула — уникальность держаться не будет"
    )


@pytest.mark.parametrize("файл", ["primary_intake.json"])
def test_заготовка_держит_пул_а_не_строку(файл: str) -> None:
    """Иначе новый бот снова родится с одной формулировкой на всех."""
    данные = json.loads((КОРЕНЬ / "app" / "bots" / "scenarios" / файл).read_text("utf-8"))
    пулы = []

    def обойти(узел: object) -> None:
        if isinstance(узел, dict):
            if узел.get("id") == "ping_problem":
                пулы.append((узел.get("params") or {}).get("text"))
            for v in узел.values():
                обойти(v)
        elif isinstance(узел, list):
            for v in узел:
                обойти(v)

    обойти(данные)
    assert пулы, "шаг ping_problem пропал из заготовки"
    текст = пулы[0]
    assert isinstance(текст, list) and len(текст) >= 6, (
        "дожим молчания снова одна строка на всех клиентов"
    )
