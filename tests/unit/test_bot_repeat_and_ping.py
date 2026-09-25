"""Две боевые жалобы 26.08 одним файлом: дубль подсказки и дожим в режиме подсказки.

СНИМОК ВЛАДЕЛЬЦА. В ленте одного диалога подряд стояли:

    00:05  Подсказка бота: добрый вечер, да, помогу с кроватью…
    00:06  Подсказка бота: Ну что, подскажете? Сразу сориентирую по мастеру и времени
    00:06  Подсказка бота: добрый вечер, да, помогу с кроватью…   ← дословная копия

Отсюда ровно два дефекта, и оба проверяются здесь.

1. ГАРД ПОВТОРА СРАВНИВАЛ ТОЛЬКО С ПРЕДЫДУЩЕЙ РЕПЛИКОЙ. Между двумя копиями
   вклинился дожим — и копия прошла. Свойство, которое нужно защищать, другое: пока
   клиент молчит, всё сказанное остаётся для него одинаково новым, поэтому повтором
   считается совпадение с ЛЮБОЙ своей репликой после последнего слова клиента.

2. ДОЖИМ В РЕЖИМЕ ПОДСКАЗКИ БЕССМЫСЛЕН. Клиенту не ушло ни слова: подсказки лежат
   заметками для смены. `ask` ждёт ответа на вопрос, которого клиент не видел, а по
   таймауту в ленту падает «Ну что, подскажете?» — обращение к человеку, у которого
   ничего не спрашивали. В режиме подсказки таймаут ведёт в очередь, к человеку.
"""

from __future__ import annotations

import uuid

import pytest

from app.bots.engine import ScenarioEngine


class _Row:
    def __init__(self, body, direction="out", sender="bot"):
        self.body, self.direction, self.sender_type = body, direction, sender
        self.id = uuid.uuid4()


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return list(self._rows)

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _Db:
    """Отдаёт ленту сверху вниз — так же, как запрос с ORDER BY … DESC."""

    def __init__(self, rows):
        self.rows = rows

    async def execute(self, *_a, **_k):
        return _Result(self.rows)


class _Conv:
    def __init__(self):
        self.id = uuid.uuid4()


class _Bot:
    def __init__(self, mode):
        self.mode = mode


def _engine(rows, *, suggest):
    eng = ScenarioEngine.__new__(ScenarioEngine)
    eng.db, eng.conv = _Db(rows), _Conv()
    eng.bot = _Bot("suggest" if suggest else "auto")
    return eng


def _подсказка(text):
    return _Row("Подсказка бота: " + text, direction="note")


ПРИВЕТ = "добрый вечер, да, помогу с кроватью. Что именно случилось?"
ДОЖИМ = "Ну что, подскажете? Сразу сориентирую по мастеру и времени"


@pytest.mark.asyncio
async def test_копия_через_дожим_не_проходит():
    """Ровно случай со снимка: между двумя копиями стоит другая реплика бота."""
    eng = _engine([_подсказка(ДОЖИМ), _подсказка(ПРИВЕТ)], suggest=True)
    assert await eng.own_texts_since_client() == [ДОЖИМ, ПРИВЕТ]
    assert await eng.is_repeat_since_client(ПРИВЕТ)


@pytest.mark.asyncio
async def test_слово_клиента_обрывает_счёт():
    """Клиент написал — прежние реплики бота повтором больше не считаются."""
    eng = _engine(
        [_Row("а сколько будет стоить", direction="in", sender="client"), _подсказка(ПРИВЕТ)],
        suggest=True,
    )
    assert await eng.own_texts_since_client() == []
    assert not await eng.is_repeat_since_client(ПРИВЕТ)


ПОЧТИ_1 = (
    "Понимаю, цену скажу до начала работ, решать вам. "
    "Кстати, я на выезд работаю, мастерской нет. Куда подъехать?"
)
ПОЧТИ_2 = (
    "Понимаю, цену скажу до начала работ, решать вам. "
    "Только я выездной мастер, мастерской нет, сам к вам подъеду. Куда, электросталь?"
)


@pytest.mark.asyncio
async def test_та_же_мысль_другими_словами_не_проходит():
    """Снимок владельца 26.08: два ответа подряд с разницей в минуту, смысл один."""
    eng = _engine([_подсказка(ПОЧТИ_1)], suggest=True)
    assert await eng.is_repeat_since_client(ПОЧТИ_2)


def test_короткие_реплики_сравниваются_только_дословно():
    """На трёх словах Жаккар шумит: «Хорошо» и «Хорошо, наберу» — не повтор."""
    assert not ScenarioEngine.is_near_duplicate("Хорошо", "Хорошо, наберу")
    assert not ScenarioEngine.is_near_duplicate("Да, помогу", "Да, сделаю")


def test_одинаковая_первая_фраза_это_повтор():
    """Точнее Жаккара и ловит ровно случай со снимка."""
    assert ScenarioEngine.is_near_duplicate(ПОЧТИ_1, ПОЧТИ_2)
    assert ScenarioEngine._первая_фраза(ПОЧТИ_1) == ScenarioEngine._первая_фраза(ПОЧТИ_2)


def test_разные_по_смыслу_реплики_проходят():
    """Порог не должен есть законную работу: это разные шаги воронки."""
    assert not ScenarioEngine.is_near_duplicate(
        "Могу сегодня к 19:00, удобно вам будет? Куда подъехать?",
        "Здравствуйте, да, помогу с телевизором. Что именно случилось?",
    )


@pytest.mark.asyncio
async def test_новый_текст_проходит():
    eng = _engine([_подсказка(ПРИВЕТ)], suggest=True)
    assert not await eng.is_repeat_since_client("Куда подъехать, адрес подскажете?")


# --------------------------------------------------------------- дожим


class _Outbox:
    def __init__(self):
        self.notes = []

    def note(self, kind, **kw):
        self.notes.append(kind)


class _Waiting:
    step_id, token, attempts = "ask_problem", "t1", 0


class _State:
    def __init__(self):
        self.waiting, self.step = _Waiting(), "ask_problem"

    def stop_waiting(self):
        self.waiting = None


class _Step:
    id, on_timeout = "ask_problem", "ping_problem"


class _Scenario:
    def step(self, _id):
        return _Step()


def _таймаут_движок(*, suggest):
    eng = ScenarioEngine.__new__(ScenarioEngine)
    eng.bot = _Bot("suggest" if suggest else "auto")
    eng.state, eng.outbox, eng.scenario = _State(), _Outbox(), _Scenario()
    eng.переходы, eng.передачи = [], []
    eng._goto = eng.переходы.append

    async def _handoff(reason):
        eng.передачи.append(reason)

    eng.do_handoff = _handoff
    return eng


@pytest.mark.asyncio
async def test_подсказка_по_таймауту_идёт_к_человеку():
    eng = _таймаут_движок(suggest=True)
    await eng.on_timeout("t1")
    assert eng.передачи == ["ask_timeout"], "в режиме подсказки дожима быть не должно"
    assert eng.переходы == []


@pytest.mark.asyncio
async def test_в_авто_режиме_дожим_остаётся():
    """Гард узкий: в авто-режиме бот действительно пишет клиенту, и дожим измерен."""
    eng = _таймаут_движок(suggest=False)
    await eng.on_timeout("t1")
    assert eng.переходы == ["ping_problem"]
    assert eng.передачи == []
