"""Бот не повторяет собственную реплику слово в слово (правка 21.08).

ЧТО ВИДНО НА БОЕВОЙ. В выгрузке живых подсказок 5 из 924 — дословный повтор того,
что бот уже сказал в этом же диалоге, включая пинг «Ну что, подскажете? Сразу
сориентирую по времени» ЧЕТЫРЕ раза подряд в одном диалоге. Для оператора это шум,
для клиента в авто-режиме — двойная отправка одного и того же.

ГДЕ ГАРД. `send_bot_message` — единственная точка исходящих бота (02 §2.3), и режим
подсказки перехватывается там же. Значит и защита от повтора обязана стоять здесь:
любой новый шаг сценария получает её даром, ничего не забыв.

ЧТО ИМЕННО СЧИТАЕТСЯ ПОВТОРОМ. Дословное совпадение с предыдущей репликой бота,
ПОСЛЕ КОТОРОЙ КЛИЕНТ НИЧЕГО НЕ НАПИСАЛ. Оба условия обязательны:

  • не «похожее», а то же самое — похожие формулировки бот варьирует нарочно
    (несвязываемость каналов), и резать их нельзя;
  • не «когда-либо раньше», а именно без ответа клиента между — иначе гард съел бы
    законный повтор: клиент, вернувшийся в закрытый диалог, начинает сценарий с
    нуля, и второе «Здравствуйте!» ему полагается (02 §1.3).
"""

from __future__ import annotations

import uuid

import pytest

from app.bots.engine import ScenarioEngine


class _Row:
    def __init__(self, body, direction="out", sender="bot"):
        self.body = body
        self.direction = direction
        self.sender_type = sender
        self.id = uuid.uuid4()


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row

    def scalar(self):
        return self._row


class _Db:
    def __init__(self, last=None):
        self.last = last

    async def execute(self, *_a, **_k):
        return _Result(self.last)


class _Conv:
    def __init__(self):
        self.id = uuid.uuid4()


class _Bot:
    def __init__(self, mode):
        self.mode = mode


def _engine(last_bot_text, *, suggest):
    eng = ScenarioEngine.__new__(ScenarioEngine)
    # в режиме подсказки последняя реплика бота лежит заметкой с префиксом
    хранимое = ("Подсказка бота: " + last_bot_text) if suggest else last_bot_text
    eng.db = _Db(_Row(хранимое, direction="note" if suggest else "out"))
    eng.conv = _Conv()
    eng.bot = _Bot("suggest" if suggest else "auto")
    return eng


@pytest.mark.asyncio
async def test_клиент_ответил_между_повтор_законен():
    """Между двумя одинаковыми репликами есть слово клиента — это не даблпост."""
    eng = ScenarioEngine.__new__(ScenarioEngine)
    eng.db = _Db(_Row("А ещё вопрос", direction="in", sender="client"))
    eng.conv = _Conv()
    eng.bot = _Bot("auto")
    assert await eng.last_bot_text() == ""


@pytest.mark.asyncio
async def test_повтор_подсказки_не_кладётся_заметкой():
    eng = _engine("Ну что, подскажете? Сразу сориентирую по времени", suggest=True)
    assert await eng.last_bot_text() == "Ну что, подскажете? Сразу сориентирую по времени"
    assert eng.is_double_post(
        "Ну что, подскажете? Сразу сориентирую по времени",
        "Ну что, подскажете? Сразу сориентирую по времени",
    )


@pytest.mark.asyncio
async def test_пробелы_и_регистр_не_спасают_повтор():
    assert ScenarioEngine.is_double_post("Здравствуйте,  соберу шкаф", "здравствуйте, соберу шкаф")
    assert ScenarioEngine.is_double_post("Куда подъехать?\n\n", "Куда подъехать?")


def test_разные_реплики_проходят():
    assert not ScenarioEngine.is_double_post("Куда подъехать?", "Когда вам удобно?")
    assert not ScenarioEngine.is_double_post("Соберу шкаф. Схема есть?", "Соберу шкаф")
    assert not ScenarioEngine.is_double_post("Куда подъехать?", "")
    assert not ScenarioEngine.is_double_post("", "")
