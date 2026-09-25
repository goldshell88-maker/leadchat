"""Подсказки на ЛЮБОМ ходу диалога (решение владельца 21.08).

ЗАЧЕМ. В режиме подсказок бот пишет ЗАМЕТКУ, которую видит только сотрудник —
клиенту не уходит ничего. Значит все запреты входа, защищающие клиента от бота
(«оператор уже ответил», «диалог не новый», «менеджер вмешался», «передан человеку»),
для подсказки смысла не имеют: они лишь лишают диспетчера помощи ровно там, где она
нужнее всего — в середине живого разговора. Замер 20.08: из 445 диалогов с подсказками
бот замолкал после первой же реплики оператора.

ЧТО ОСТАЁТСЯ В СИЛЕ и в режиме подсказок: выключенный бот, канал без бота,
отсутствие диалога, расписание (это деньги: каждый ход — платный вызов шлюза).

В АВТО-режиме не меняется НИЧЕГО: там бот говорит клиенту, и все прежние запреты
обязаны держаться — иначе бот заговорит поверх оператора.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from app.bots.runtime import bot_entry_block
from app.bots.state import BotState


class _FakeConv:
    def __init__(self, *, status="new", bot_active=False, bot_vars=None):
        self.id = uuid.uuid4()
        self.status = status
        self.bot_active = bot_active
        self.bot_vars = bot_vars or {}
        self.account_id = uuid.uuid4()


class _FakeBot:
    def __init__(self, *, mode="suggest", is_enabled=True):
        self.id = uuid.uuid4()
        self.mode = mode
        self.is_enabled = is_enabled
        self.schedule = None


def _muted_conv():
    conv = _FakeConv(status="in_progress")
    state = BotState.from_conv(conv)
    state.mute()
    conv.bot_vars = state.dump()
    return conv


@pytest.mark.asyncio
async def test_подсказка_после_ответа_оператора(monkeypatch):
    """Оператор ответил — в режиме подсказок бот продолжает подсказывать."""
    import app.bots.runtime as rt

    async def _yes(*_a, **_k):
        return True

    monkeypatch.setattr(rt, "has_operator_messages", _yes)
    conv = _FakeConv(status="new")
    assert await bot_entry_block(None, conv, bot=_FakeBot(mode="suggest")) is None


@pytest.mark.asyncio
async def test_подсказка_в_середине_диалога(monkeypatch):
    """Статус уже не «новый» — подсказка всё равно полагается."""
    import app.bots.runtime as rt

    async def _no(*_a, **_k):
        return False

    monkeypatch.setattr(rt, "has_operator_messages", _no)
    for статус in ("in_progress", "waiting_client", "postponed", "closed"):
        conv = _FakeConv(status=статус)
        assert await bot_entry_block(None, conv, bot=_FakeBot(mode="suggest")) is None, статус


@pytest.mark.asyncio
async def test_подсказка_после_передачи_и_мьюта(monkeypatch):
    """Передача человеку и «менеджер вмешался» больше не глушат подсказки."""
    import app.bots.runtime as rt

    async def _no(*_a, **_k):
        return False

    monkeypatch.setattr(rt, "has_operator_messages", _no)
    assert await bot_entry_block(None, _muted_conv(), bot=_FakeBot(mode="suggest")) is None


@pytest.mark.asyncio
async def test_авто_режим_не_изменился(monkeypatch):
    """АВТО: клиенту говорит бот — все прежние запреты держатся."""
    import app.bots.runtime as rt

    async def _yes(*_a, **_k):
        return True

    monkeypatch.setattr(rt, "has_operator_messages", _yes)
    conv = _FakeConv(status="new")
    assert await bot_entry_block(None, conv, bot=_FakeBot(mode="auto")) == "operator_replied"

    async def _no(*_a, **_k):
        return False

    monkeypatch.setattr(rt, "has_operator_messages", _no)
    assert (
        await bot_entry_block(None, _FakeConv(status="in_progress"), bot=_FakeBot(mode="auto"))
        == "not_new"
    )
    assert await bot_entry_block(None, _muted_conv(), bot=_FakeBot(mode="auto")) == "muted"


@pytest.mark.asyncio
async def test_выключатели_держатся_и_в_подсказках(monkeypatch):
    """Выключенный бот и отсутствие диалога — запрет в любом режиме."""
    import app.bots.runtime as rt

    async def _no(*_a, **_k):
        return False

    monkeypatch.setattr(rt, "has_operator_messages", _no)
    assert await bot_entry_block(None, None, bot=_FakeBot()) == "no_conversation"
    выкл = await bot_entry_block(None, _FakeConv(), bot=_FakeBot(is_enabled=False))
    assert выкл == "bot_disabled"


@pytest.mark.asyncio
async def test_расписание_держится_в_подсказках(monkeypatch):
    """Расписание — деньги: вне окна платных вызовов нет и в режиме подсказок."""
    import app.bots.runtime as rt

    async def _no(*_a, **_k):
        return False

    monkeypatch.setattr(rt, "has_operator_messages", _no)
    monkeypatch.setattr(rt, "is_bot_scheduled_now", lambda *_a, **_k: False)
    conv = _FakeConv(status="in_progress")
    ночь = datetime(2026, 8, 21, 3, 0, tzinfo=UTC)
    блок = await bot_entry_block(None, conv, bot=_FakeBot(mode="suggest"), now=ночь)
    assert блок == "not_scheduled"


# ── куда входит подсказка: приветствие или сразу черновик ────────────────────
class _FakeStep:
    def __init__(self, sid, stype):
        self.id, self.type = sid, stype


class _FakeScenario:
    def __init__(self, steps):
        self._steps = {s.id: s for s in steps}
        self.order = tuple(s.id for s in steps)
        self.entry = steps[0].id if steps else None

    def step(self, sid):
        return self._steps.get(sid)


СЦЕНАРИЙ = _FakeScenario(
    [
        _FakeStep("greet", "send"),
        _FakeStep("ask_problem", "ask"),
        _FakeStep("ai_draft", "ai_answer"),
        _FakeStep("hand", "handoff"),
    ]
)


def test_свежий_бот_в_новом_диалоге_идёт_сценарием():
    """Первое касание — приветствие уместно, прежняя дорога сохраняется."""
    from app.bots.engine import suggest_entry_step

    assert suggest_entry_step(СЦЕНАРИЙ, fresh=True, conv_status="new") is None


def test_разговор_идёт_сразу_черновик():
    """Оператор уже работает / диалог не новый / бот раньше передавал —
    подсказка обязана быть ответом по контексту, а не «здравствуйте»."""
    from app.bots.engine import suggest_entry_step

    assert suggest_entry_step(СЦЕНАРИЙ, fresh=True, conv_status="in_progress") == "ai_draft"
    assert suggest_entry_step(СЦЕНАРИЙ, fresh=False, conv_status="new") == "ai_draft"
    assert suggest_entry_step(СЦЕНАРИЙ, fresh=False, conv_status="closed") == "ai_draft"


def test_сценарий_без_ai_шага_идёт_прежней_дорогой():
    from app.bots.engine import suggest_entry_step

    пустой = _FakeScenario([_FakeStep("greet", "send"), _FakeStep("hand", "handoff")])
    assert suggest_entry_step(пустой, fresh=False, conv_status="in_progress") is None
