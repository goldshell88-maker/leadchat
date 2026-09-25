"""Бот не закрывает диалог, который ведёт человек (аудит 30.08).

⚠ ПОЧЕМУ НАСТОЯЩИЙ ВЫЗОВ, А НЕ РАЗБОР ИСХОДНИКА. Соседний
`test_bot_closes_dialog.py` сторожит эту же ветку через `ast` — то есть
проверяет, что в тексте функции есть нужные строки. Такой сторож не отличает
«ветка написана» от «ветка выполняется в том диалоге, где не должна»: ровно
поэтому дефект и дожил до аудита. Здесь функция вызывается по-настоящему.

ЧТО ЛОМАЛОСЬ. Решение владельца 29.08 («заявка собрана — закрываем»)
принималось про АВТО-режим, где диалог ведёт бот и больше никто. Но подсказки
работают и в операторских диалогах. Оператор ведёт клиента, тот присылает
телефон и адрес, лид-бот честно отдаёт `lead_ready` — и диалог ЗАКРЫВАЛСЯ у
оператора под руками, с исходом «выезд», из которого родилась бы вторая
автозаявка рядом с той, что заводит человек. Напишет клиент ещё раз — диалог
переоткроется НИЧЬИМ в общей очереди, и оператор молча теряет клиента.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.bots.engine import ScenarioEngine
from app.bots.state import BotState
from app.models import Conversation

pytestmark = pytest.mark.anyio

СЕЙЧАС = datetime(2026, 8, 30, 12, 0, 0, tzinfo=UTC)


class ЗаписнаяДБ:
    """Сессия-заглушка: движку здесь нужна только возможность что-то добавить."""

    def __init__(self) -> None:
        self.added: list[Any] = []

    def add(self, obj: Any) -> None:
        self.added.append(obj)

    async def flush(self) -> None:  # pragma: no cover — вызывается не всегда
        return None

    async def execute(self, *_a: Any, **_kw: Any) -> Any:  # pragma: no cover
        raise AssertionError("движок не должен ходить в базу на этом пути")


def движок(*, режим: str, хозяин: uuid.UUID | None) -> tuple[ScenarioEngine, Conversation]:
    conv = Conversation(
        id=uuid.uuid4(),
        channel="avito",
        external_chat_id="chat-1",
        account_id=uuid.uuid4(),
        client_id=uuid.uuid4(),
        status="in_progress",
        assignee_id=хозяин,
        bot_active=True,
    )
    eng = ScenarioEngine(
        bot=SimpleNamespace(id=uuid.uuid4(), mode=режим, name="Бот"),
        conv=conv,
        state=BotState.from_conv(conv),
        db=ЗаписнаяДБ(),
        now=СЕЙЧАС,
        scenario={"steps": []},
    )
    заметки: list[str] = []

    async def записать(text: str) -> None:
        заметки.append(text)

    eng.add_note = записать  # type: ignore[method-assign]
    eng.заметки = заметки  # type: ignore[attr-defined]
    return eng, conv


async def test_в_подсказке_диалог_не_закрывается() -> None:
    """⚠ ГЛАВНАЯ ПРОВЕРКА: подсказка ничего не решает за человека."""
    eng, conv = движок(режим="suggest", хозяин=None)

    await eng.close_by_leadbot(reason="lead_ready", outcome="visit", note="🤖 Заявка собрана")

    assert conv.status == "in_progress", "бот закрыл диалог в режиме подсказки"
    assert conv.bot_active is True, "бот заглушил себя там, где только подсказывал"
    assert eng.заметки, "заметка пропала — польза подсказки потеряна"


async def test_чужой_диалог_не_закрывается_и_в_авторежиме() -> None:
    """Диалог мог достаться человеку и в авто-режиме — передачей или «забрать у бота»."""
    eng, conv = движок(режим="auto", хозяин=uuid.uuid4())

    await eng.close_by_leadbot(reason="lead_ready", outcome="visit", note="🤖 Заявка собрана")

    assert conv.status == "in_progress", "бот закрыл диалог, который ведёт человек"
    assert eng.заметки, "заметка не оставлена"


async def test_свой_диалог_в_авторежиме_закрывается_как_прежде() -> None:
    """Решение владельца 29.08 в силе: там, где бот ведёт сам, он и закрывает."""
    eng, conv = движок(режим="auto", хозяин=None)
    eng._apply_close_outcome_called = False  # type: ignore[attr-defined]

    async def без_итога(_outcome: str) -> None:
        eng._apply_close_outcome_called = True  # type: ignore[attr-defined]

    eng._apply_close_outcome_by_name = без_итога  # type: ignore[attr-defined]
    try:
        await eng.close_by_leadbot(reason="lead_ready", outcome="visit", note="🤖 Заявка собрана")
    except Exception:  # noqa: BLE001 — дальше идёт запись в базу, её здесь нет
        pass

    assert conv.status == "closed", (
        "бот перестал закрывать собственные диалоги — решение владельца 29.08 отменено"
    )
