"""Поиск диалога по внешнему чату обязан попадать в индекс.

Единственный индекс с `external_chat_id` — `uq_conversations_channel_external_
chat_id (channel, external_chat_id)` (миграция 0001). Его ведущая колонка —
`channel`: без неё seek по индексу невозможен, и базе остаётся полный проход по
454 тысячам диалогов.

Промах тихий по построению: ответ верный, ошибки нет, тест не падает — растёт
только время. Ровно это чинила миграция 0051 для карточек клиентов, и ровно это
повторилось в `_apply_external_outgoing`, куда приходит КАЖДЫЙ ответ, написанный
человеком в приложении Авито (по замеру боя 23.08 — единственный способ, которым
сейчас отвечают), и каждое историческое исходящее из сверки.

Стережём по дереву, а не по строке: разбор `ast` видит структуру запроса и
переживает и `ruff format`, и перенос условий.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from app.services import avito_accounts as acc_mod
from app.services import inbound as inbound_mod
from app.workers import reconciliation as rec_mod

#: Каждая функция, которая ищет `Conversation` по `external_chat_id`.
#: Добавляя сюда новую, проверь: `channel` в её запросе обязателен.
ИСКАТЕЛИ = [
    inbound_mod._apply_external_outgoing,
    inbound_mod._upsert_conversation,
    inbound_mod._apply_avito_system_event,
    acc_mod._get_or_create_conversation,
    rec_mod.reconcile_account,
]


def _условия_запросов(функция: object) -> list[set[str]]:
    """Наборы колонок `Conversation.*`, сравниваемых в каждом `select(...).where(...)`."""
    дерево = ast.parse(textwrap.dedent(inspect.getsource(функция)))
    наборы: list[set[str]] = []
    for узел in ast.walk(дерево):
        if not (isinstance(узел, ast.Call) and isinstance(узел.func, ast.Attribute)):
            continue
        if узел.func.attr != "where":
            continue
        колонки: set[str] = set()
        for условие in узел.args:
            for внутри in ast.walk(условие):
                if (
                    isinstance(внутри, ast.Attribute)
                    and isinstance(внутри.value, ast.Name)
                    and внутри.value.id == "Conversation"
                ):
                    колонки.add(внутри.attr)
        if "external_chat_id" in колонки:
            наборы.append(колонки)
    return наборы


@pytest.mark.parametrize("функция", ИСКАТЕЛИ, ids=lambda f: f.__name__)
def test_поиск_по_внешнему_чату_ограничивает_канал(функция: object) -> None:
    наборы = _условия_запросов(функция)
    assert наборы, f"{функция.__name__} больше не ищет диалог по external_chat_id — обнови список"
    for колонки in наборы:
        assert "channel" in колонки, (
            f"{функция.__name__}: запрос по external_chat_id без `channel` — ведущая колонка "
            "единственного подходящего индекса не ограничена, база пойдёт полным проходом"
        )


def test_индекс_с_этой_парой_действительно_есть() -> None:
    """Сторож бесполезен, если индекс однажды снимут миграцией.

    Проверяем саму модель: пара `(channel, external_chat_id)` обязана быть
    уникальной — именно это ограничение и создаёт индекс, ради которого выше
    требуется `channel`.
    """
    from app.models import Conversation

    пары = {
        tuple(getattr(c, "name", c) for c in ограничение.columns)
        for ограничение in Conversation.__table__.constraints
        if hasattr(ограничение, "columns") and len(ограничение.columns) == 2
    }
    индексы = {tuple(c.name for c in i.columns) for i in Conversation.__table__.indexes}
    assert ("channel", "external_chat_id") in пары | индексы, (
        "пары (channel, external_chat_id) больше нет — требование `channel` выше потеряло смысл"
    )
