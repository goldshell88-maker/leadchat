"""Находки параллельного ревью пачки 9б (проверка 24.09) — каждая с воспроизведением.

* Тик не смотрел на совершённую передачу: запоздавший тик (или передача,
  закоммиченная классификатором, пока модель думала) отвечал клиенту после
  «передаю мастеру» и снова прятал диалог.
* Упавший между транзакциями тик оставлял `bot_active` без ожидания — такой
  диалог не видел ни один сторож.
* В подсказке серия не заканчивалась подсказкой (она — заметка): одно давнее
  «менеджер» глушило все следующие подсказки. Время сообщения Авито идёт
  целыми секундами и бывает раньше ответа бота — такое сообщение выпадало.
* Цепочка из четырёх шагов ИИ оставляла диалог без ожидания; классификатор
  терялся, если вторую транзакцию останавливали ворота; подсказка ложилась
  после ответа оператора; сценарий-подсказчик без ИИ не продвигался; вне
  расписания оставшиеся метки подсказки не снимались.

ДИВЕРСИИ (каждая краснит свой тест): проверка передачи в `_tick_guards`;
ветка `bot_stalled` сторожа; `seen_in` в серии; `_stop_ai_chain`; сохранение
текста классификатора в `_tick`; сверка последней реплики клиенту во второй
транзакции; ожидание без срока в `_begin_waiting`; уборка меток до
расписания.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from app.bots import handoff as handoff_mod
from app.bots.runtime import bot_step
from app.bots.state import BotState, Outbox
from app.models import AuditLog, Conversation, Message, User
from app.scheduler.jobs import bot_deadlines
from tests.unit import test_bot_engine as engine_fixtures
from tests.unit.test_bot_engine import StubAI, bot_messages, load_conv, notes

make_bot = engine_fixtures.make_bot
make_world = engine_fixtures.make_world
world_ctx = engine_fixtures.world_ctx
chat = engine_fixtures.chat

pytestmark = pytest.mark.anyio

ANSWER = {"reply": "Замена экрана — от 8900 ₽", "confidence": 0.95, "needs_operator": False}
AI_THEN_WAIT = {
    "version": 1,
    "revision": 1,
    "entry": "ai",
    "steps": [
        {"id": "ai", "type": "ai_answer", "params": {}, "next": "wait"},
        {"id": "wait", "type": "ask", "params": {"text": None, "timeout": "25m"}, "next": "ai"},
    ],
}
MENU_ONLY = {
    "version": 1,
    "revision": 1,
    "entry": "pick",
    "steps": [
        {
            "id": "pick",
            "type": "menu",
            "params": {
                "text": "Что сломалось?\n1. Телефон\n2. Ноутбук",
                "var": "device",
                "options": [
                    {"id": "phone", "label": "Телефон", "match": ["1"], "next": "done"},
                    {"id": "laptop", "label": "Ноутбук", "match": ["2"], "next": "done"},
                ],
                "timeout": "25m",
            },
        },
        {"id": "done", "type": "handoff", "params": {"reason": "scenario"}},
    ],
}


class ThinkingAI(StubAI):
    """Модель, пока думает, даёт миру поменяться (`meanwhile`)."""

    def __init__(self, meanwhile: Any = None, **kw: Any) -> None:
        super().__init__(**({"answer": ANSWER} | kw))
        self.meanwhile = meanwhile

    async def ai_answer(self, bot, dialog, item_title, **kw):  # noqa: ANN001
        if self.meanwhile is not None:
            meanwhile, self.meanwhile = self.meanwhile, None
            await meanwhile()
        return await super().ai_answer(bot, dialog, item_title, **kw)


async def _client_writes(db_sessionmaker: Any, conv_id: uuid.UUID, text: str, at: datetime) -> None:
    async with db_sessionmaker() as s, s.begin():
        s.add(
            Message(
                conversation_id=conv_id,
                external_message_id=f"m-{uuid.uuid4().hex[:8]}",
                direction="in",
                sender_type="client",
                body=text,
                attachments=[],
                delivery_status="delivered",
                created_at=at,
            )
        )


async def _operator(db_sessionmaker: Any) -> User:
    async with db_sessionmaker() as s, s.begin():
        user = User(
            email=f"op-{uuid.uuid4().hex[:6]}@leadchat.test",
            full_name="Оператор",
            password_hash="x",
            role="manager",
        )
        s.add(user)
    return user


async def _reasons(db_sessionmaker: Any, conv_id: uuid.UUID) -> list[str]:
    async with db_sessionmaker() as s:
        rows = (
            await s.execute(
                select(AuditLog.details).where(
                    AuditLog.action == "bot.handoff", AuditLog.entity_id == str(conv_id)
                )
            )
        ).scalars()
        return [d["reason"] for d in rows]


# ----------------------------------------------------------------- передача


async def test_a_tick_queued_before_the_handoff_does_not_answer(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    ctx = world_ctx(ai=ThinkingAI())
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        state = BotState.from_conv(conv)
        state.step = "ai"
        state.set_handoff(reason="ai_low_confidence", step="ai")
        conv.bot_vars = state.dump()
    await _client_writes(
        db_sessionmaker, world.conversation_id, "И адрес: ул. Садовая 12", ctx["bot_now"]()
    )

    assert await bot_step(ctx, world.conversation_id, "И адрес: ул. Садовая 12") == "handoff_done"

    assert await bot_messages(db_sessionmaker, world.conversation_id) == []
    assert (await load_conv(db_sessionmaker, world.conversation_id)).bot_active is False


async def test_a_handoff_committed_while_the_model_thinks_holds(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    ctx = world_ctx(ai=None)

    async def classifier_hands_off() -> None:
        async with db_sessionmaker() as s, s.begin():
            conv = await s.get(Conversation, world.conversation_id)
            assert conv is not None
            state = BotState.from_conv(conv)
            await handoff_mod.do_handoff(s, conv, state, Outbox(), reason="negative")
            conv.bot_vars = state.dump()

    ctx["bot_ai"] = ThinkingAI(meanwhile=classifier_hands_off)
    await _client_writes(db_sessionmaker, world.conversation_id, "Сколько стоит?", ctx["bot_now"]())

    assert await bot_step(ctx, world.conversation_id, "Сколько стоит?") == "handoff_done"

    assert await bot_messages(db_sessionmaker, world.conversation_id) == []
    assert (await load_conv(db_sessionmaker, world.conversation_id)).bot_active is False


# ----------------------------------------------------- сторож застрявшего бота


async def test_a_bot_that_stopped_mid_scenario_is_handed_to_people(
    db_sessionmaker: Any, make_world: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    now = datetime.now(UTC)
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        state = BotState.from_conv(conv)
        state.step = "ai"
        state.touch(now - bot_deadlines.STALL - timedelta(minutes=1))
        conv.bot_vars = state.dump()
        conv.bot_active = True

    async with db_sessionmaker() as s, s.begin():
        _, picked = await bot_deadlines.sweep_in_session(s, now=now)

    assert picked == 1
    assert await _reasons(db_sessionmaker, world.conversation_id) == ["bot_stalled"]
    assert (await load_conv(db_sessionmaker, world.conversation_id)).bot_active is False


async def test_a_bot_between_its_two_transactions_is_left_alone(
    db_sessionmaker: Any, make_world: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    now = datetime.now(UTC)
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        state = BotState.from_conv(conv)
        state.step = "ai"
        state.touch(now - timedelta(seconds=20))
        conv.bot_vars = state.dump()
        conv.bot_active = True

    async with db_sessionmaker() as s, s.begin():
        _, picked = await bot_deadlines.sweep_in_session(s, now=now)

    assert picked == 0


# ------------------------------------------------------------------- серия


async def test_one_old_request_for_a_person_does_not_silence_suggestions(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any
) -> None:
    from app.services import leadbot_admin

    bot = await make_bot(leadbot_admin.default_scenario(), mode="suggest")
    world = await make_world(bot=bot)
    ai = ThinkingAI(
        answer={"reply": "Подскажите модель", "confidence": 0.9, "needs_operator": False}
    )
    ctx = world_ctx(ai=ai)
    for text in ("Здравствуйте, соедините с менеджером", "Не сливает воду", "Когда приедете?"):
        await _client_writes(db_sessionmaker, world.conversation_id, text, ctx["bot_now"]())
        assert await bot_step(ctx, world.conversation_id, text) == "ok"

    suggestions = [
        n for n in await notes(db_sessionmaker, world.conversation_id) if "Подсказка" in n
    ]
    assert len(suggestions) == 2, "после «менеджера» подсказки прекратились"


async def test_a_message_stamped_before_the_bots_reply_still_counts(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    """Время Авито — целые секунды: сообщение клиента бывает «раньше» ответа
    бота, хотя пришло позже. Из серии оно не выпадает."""
    world = await make_world(AI_THEN_WAIT)
    ctx = world_ctx(ai=ThinkingAI())
    await _client_writes(db_sessionmaker, world.conversation_id, "Сколько стоит?", ctx["bot_now"]())
    assert await bot_step(ctx, world.conversation_id, "Сколько стоит?") == "ok"
    async with db_sessionmaker() as s:
        reply_at = await s.scalar(
            select(Message.created_at).where(
                Message.conversation_id == world.conversation_id, Message.sender_type == "bot"
            )
        )
    await _client_writes(
        db_sessionmaker,
        world.conversation_id,
        "позовите оператора",
        reply_at - timedelta(milliseconds=300),
    )
    await _client_writes(db_sessionmaker, world.conversation_id, "?", ctx["bot_now"]())

    assert await bot_step(ctx, world.conversation_id, "позовите оператора") == "ok"

    assert await _reasons(db_sessionmaker, world.conversation_id) == ["client_request"]


async def test_a_second_tick_for_an_answered_series_does_nothing(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    ai = ThinkingAI()
    ctx = world_ctx(ai=ai)
    await _client_writes(db_sessionmaker, world.conversation_id, "Сколько стоит?", ctx["bot_now"]())
    assert await bot_step(ctx, world.conversation_id, "Сколько стоит?") == "ok"

    assert await bot_step(ctx, world.conversation_id, "Сколько стоит?") == "seen"

    assert len(await bot_messages(db_sessionmaker, world.conversation_id)) == 1
    assert len(ai.dialogs) == 1


# ---------------------------------------------------------- цепочка шагов ИИ


async def test_a_chain_of_ai_steps_longer_than_a_tick_hands_off(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    chain = {
        "version": 1,
        "revision": 1,
        "entry": "ai1",
        "steps": [
            {"id": "ai1", "type": "ai_answer", "params": {}, "next": "ai2"},
            {"id": "ai2", "type": "ai_answer", "params": {}, "next": "ai3"},
            {"id": "ai3", "type": "ai_answer", "params": {}, "next": "ai4"},
            {"id": "ai4", "type": "ai_answer", "params": {}, "next": "wait"},
            {
                "id": "wait",
                "type": "ask",
                "params": {"text": None, "timeout": "25m"},
                "next": "ai1",
            },
        ],
    }
    world = await make_world(chain)
    ctx = world_ctx(ai=ThinkingAI())
    await _client_writes(db_sessionmaker, world.conversation_id, "Сколько стоит?", ctx["bot_now"]())

    assert await bot_step(ctx, world.conversation_id, "Сколько стоит?") == "loop_protection"

    assert await _reasons(db_sessionmaker, world.conversation_id) == ["loop_protection"]
    assert (await load_conv(db_sessionmaker, world.conversation_id)).bot_active is False


# ------------------------------------------------------------ классификатор


async def test_the_classifier_runs_even_when_the_second_transaction_is_stopped(
    db_sessionmaker: Any, make_world: Any, world_ctx: Any
) -> None:
    world = await make_world(AI_THEN_WAIT)
    operator = await _operator(db_sessionmaker)
    ctx = world_ctx(ai=None)

    async def operator_claims() -> None:
        async with db_sessionmaker() as s, s.begin():
            conv = await s.get(Conversation, world.conversation_id)
            assert conv is not None
            conv.claimed_by_id = conv.assignee_id = operator.id
            conv.status = "in_progress"

    ai = ThinkingAI(meanwhile=operator_claims, classify={"sentiment": "negative"})
    ctx["bot_ai"] = ai
    await _client_writes(db_sessionmaker, world.conversation_id, "Безобразие!", ctx["bot_now"]())

    assert await bot_step(ctx, world.conversation_id, "Безобразие!") == "claimed"

    assert ai.classified, "классификатор не вызван"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert handoff_mod.NEGATIVE_TAG in (conv.tags or [])


# ------------------------------------------------------------------ подсказка


async def test_a_suggestion_is_dropped_when_the_operator_answered_meanwhile(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any
) -> None:
    bot = await make_bot(AI_THEN_WAIT, mode="suggest")
    world = await make_world(bot=bot)
    operator = await _operator(db_sessionmaker)
    ctx = world_ctx(ai=None)

    async def operator_answers() -> None:
        async with db_sessionmaker() as s, s.begin():
            s.add(
                Message(
                    conversation_id=world.conversation_id,
                    external_message_id=f"op-{uuid.uuid4().hex[:8]}",
                    direction="out",
                    sender_type="operator",
                    sender_user_id=operator.id,
                    body="Диагностика бесплатно",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=ctx["bot_now"](),
                )
            )

    ctx["bot_ai"] = ThinkingAI(
        meanwhile=operator_answers,
        answer={"reply": "Диагностика бесплатно", "confidence": 0.9, "needs_operator": False},
    )
    await _client_writes(
        db_sessionmaker, world.conversation_id, "Сколько стоит диагностика?", ctx["bot_now"]()
    )

    assert await bot_step(ctx, world.conversation_id, "Сколько стоит диагностика?") == "stale"

    assert not [n for n in await notes(db_sessionmaker, world.conversation_id) if "Подсказка" in n]


async def test_a_scripted_suggestion_follows_the_clients_answers(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any
) -> None:
    """Сценарий без шага ИИ в подсказке — подсказчик для оператора: ответ
    клиента попадает в меню, диалог не прячется и не торопится сроком."""
    bot = await make_bot(MENU_ONLY, mode="suggest")
    world = await make_world(bot=bot)
    ctx = world_ctx(ai=StubAI())
    client = engine_fixtures.Chat(ctx, world, db_sessionmaker)
    assert await client.says("Здравствуйте") == "ok"
    conv = await load_conv(db_sessionmaker, world.conversation_id)
    waiting = BotState.from_conv(conv).waiting
    assert conv.bot_active is False and waiting is not None and waiting.deadline is None

    assert await client.says("2") == "ok"

    state = BotState.from_conv(await load_conv(db_sessionmaker, world.conversation_id))
    assert state.vars.get("device") == "laptop"
    assert await _reasons(db_sessionmaker, world.conversation_id) == ["scenario"]


async def test_leftover_suggestion_marks_are_cleared_outside_the_schedule(
    db_sessionmaker: Any, make_bot: Any, make_world: Any, world_ctx: Any
) -> None:
    schedule = {
        "always": False,
        "timezone": "Europe/Moscow",
        "intervals": [{"days": ["sun"], "start": "03:00", "end": "03:01"}],
    }
    bot = await make_bot(AI_THEN_WAIT, mode="suggest", schedule=schedule)
    world = await make_world(bot=bot)
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, world.conversation_id)
        assert conv is not None
        state = BotState.from_conv(conv)
        state.step = "wait"
        state.begin_waiting(kind="ask", step_id="wait", var=None, timeout=timedelta(minutes=25))
        conv.bot_vars = state.dump()
        conv.bot_active = True
    ctx = world_ctx(ai=StubAI())
    await _client_writes(db_sessionmaker, world.conversation_id, "Алло?", ctx["bot_now"]())

    assert await bot_step(ctx, world.conversation_id, "Алло?") == "not_scheduled"

    conv = await load_conv(db_sessionmaker, world.conversation_id)
    assert conv.bot_active is False and BotState.from_conv(conv).waiting is None
