"""Передача диалога менеджеру и глушение — 02 §4 и §2.6 (07 §1.1).

Проверяется процедура как таковая: что она делает со строкой диалога, что
кладёт в ленту, что пишет в журнал (контракт 06 §0.3) и что публикует. Сами
шесть условий, которые её вызывают, — в `test_bot_engine.py`.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bots.handoff import (
    HANDOFF_REASONS,
    NEGATIVE_TAG,
    add_conversation_tags,
    do_handoff,
    handoff_summary,
    handoff_system_text,
    reason_label,
    reason_tags,
)
from app.bots.runtime import mute_bot
from app.bots.state import BotState, Outbox
from app.models import AuditLog, AvitoAccount, Client, Conversation, Message, User
from app.services import inbox
from app.services.notifications import as_utc  # SQLite отдаёт наивные метки (07 §1.1)
from app.ws.hub import INBOX_NEW

# Реестр причин из 02 §4 — он же словарь `details.reason` в 06 §0.3.
#: Причины, которые называет САМ движок (02 §4, 06 §0.3).
DOC_REASONS = {
    "client_request",
    "ai_low_confidence",
    "negative",
    "offscript",
    "scenario",
    "ask_timeout",
    "ask_invalid",
    "loop_protection",
    "ai_unavailable",
    "scenario_changed",
    # ⚠ 26.08: окончательный отказ заканчивает работу бота. Клиент спросил замену
    # матрицы, бот верно отказал — и через пять минут сам же дожал «Ну что, расскажете,
    # что случилось?»: шаг ушёл на `next`, тот оказался вопросом, и его таймаут отработал
    # как обычно. Спрашивать после отказа нечего, тема закрыта.
    "refused",
    # Проверка 24.09: бота выключили или сняли с канала, пока он вёл диалог, —
    # диалог возвращается людям, а не висит скрытым из очереди. То же при
    # переводе в подсказки: подсказка диалог не ведёт.
    "bot_disabled",
    "bot_to_suggest",
    # Тик оборвался между двумя транзакциями: бот ведёт, но ничего не ждёт.
    "bot_stalled",
}
#: ⚠ ПРИЧИНЫ БЭКЕНДА — ОТДЕЛЬНЫЙ НАБОР, И РЕЕСТР ОБЯЗАН ЗНАТЬ ОБА (26.08).
#: До этого любая передача от `ai_answer` называлась «AI не уверен в ответе», даже когда
#: лид-бот прямо сказал, почему зовёт человека. На снимках владельца в очереди висело
#: «AI не уверен в ответе», а рядом заметкой — честное «клиент звонил» и «тема по
#: согласованию». Оператор сортирует очередь по причине, значит она обязана быть правдой.
#: Список — словарь лид-бота (brain/escalation.py::REASONS).
BACKEND_REASONS = {
    "claim",
    "prior_master",
    "cancel",
    "status",
    "reschedule",
    "no_show",
    "discontent",
    "unknown_topic",
    "soglasovanie",
    "call_request",
    "call_made",
    "model_silent",
    "blocklist",
    "multi_account",
    "troll_exit",
    "offtopic",
    "model_handoff",
    "voice",
    "not_primary",
    "self_repeat",
}


# ---------------------------------------------------------------- фикстуры


@pytest.fixture
async def conv_seed(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> SimpleNamespace:
    """Аккаунт + клиент + диалог, которым «владеет» бот."""
    async with db_sessionmaker() as s:
        account = AvitoAccount(
            title="LP-Тест",
            avito_user_id=555000111,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=datetime.now(UTC) + timedelta(days=1),
            status="active",
            webhook_secret="whsec",
        )
        client = Client(channel="avito", external_id="c-1", name="Иван")
        s.add_all([account, client])
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client.id,
            status="new",
            bot_active=True,
            bot_vars={},
            tags=[],
        )
        s.add(conv)
        await s.commit()
        return SimpleNamespace(account_id=account.id, client_id=client.id, conversation_id=conv.id)


@pytest.fixture
def load_conv(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[[uuid.UUID], Awaitable[Conversation]]:
    async def _load(conv_id: uuid.UUID) -> Conversation:
        async with db_sessionmaker() as s:
            return (
                await s.execute(select(Conversation).where(Conversation.id == conv_id))
            ).scalar_one()

    return _load


async def messages_of(db_sessionmaker, conv_id, direction: str) -> list[Message]:
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    select(Message).where(
                        Message.conversation_id == conv_id, Message.direction == direction
                    )
                )
            ).scalars()
        )


def body_of(msg: Message) -> str:
    assert msg.body is not None
    return msg.body


async def audit_of(db_sessionmaker, action: str) -> list[AuditLog]:
    async with db_sessionmaker() as s:
        return list((await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


# ------------------------------------------------------------ реестр причин


def test_reason_registry_matches_the_documented_one():
    """Реестр реализован полностью и без самодеятельности (02 §4, 06 §0.3)."""
    assert set(HANDOFF_REASONS) == DOC_REASONS | BACKEND_REASONS


def test_причина_бэкенда_имеет_человеческое_название():
    """Иначе в очереди у оператора окажется голый машинный код вроде «call_made»."""
    for r in BACKEND_REASONS:
        assert HANDOFF_REASONS[r] != r, r
        assert len(HANDOFF_REASONS[r]) > 8, r


def test_negative_is_the_only_auto_tagged_reason():
    assert reason_tags("negative") == [NEGATIVE_TAG]
    assert all(reason_tags(r) == [] for r in (DOC_REASONS | BACKEND_REASONS) - {"negative"})


def test_reason_label_falls_back_to_the_raw_value():
    assert reason_label("client_request") != "client_request"
    assert reason_label("невиданная_причина") == "невиданная_причина"


# ---------------------------------------------------------------- процедура


async def test_do_handoff_returns_dialog_to_the_common_queue(db_sessionmaker, conv_seed, load_conv):
    """02 §4: бот снят, статус `new`, ответственного нет, ревизия в bot_vars."""
    outbox = Outbox()
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        conv.assignee_id = None
        state = BotState.from_conv(conv)
        state.step = "handoff_night"
        state.vars.update({"problem": "Разбит экран", "phone": "+79161234567"})
        assert await do_handoff(
            s,
            conv,
            state,
            outbox,
            reason="scenario",
            bot_id=uuid.uuid4(),
            comment="Ночной диалог",
            tags=["ночной-лид"],
        )
        conv.bot_vars = state.dump()

    conv = await load_conv(conv_seed.conversation_id)
    assert conv.bot_active is False
    assert conv.status == "new"
    assert conv.assignee_id is None
    assert "ночной-лид" in conv.tags
    assert conv.bot_vars["handoff"]["reason"] == "scenario"
    assert conv.bot_vars["handoff"]["step"] == "handoff_night"
    assert conv.bot_vars["waiting"] is None


async def test_do_handoff_writes_summary_note_and_system_message(db_sessionmaker, conv_seed):
    outbox = Outbox()
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        state = BotState.from_conv(conv)
        state.step = "handoff_night"
        state.vars.update({"problem": "Разбит экран", "phone": "+79161234567"})
        await do_handoff(s, conv, state, outbox, reason="ask_timeout", comment="Комментарий шага")
        conv.bot_vars = state.dump()

    notes = await messages_of(db_sessionmaker, conv_seed.conversation_id, "note")
    system = await messages_of(db_sessionmaker, conv_seed.conversation_id, "system")
    assert len(notes) == 1 and len(system) == 1
    # DESIGN §4.3: менеджер видит собранные ботом переменные
    assert "Телефон: +79161234567" in body_of(notes[0])
    assert "Проблема: Разбит экран" in body_of(notes[0])
    assert "Комментарий шага" in body_of(notes[0])
    assert notes[0].sender_type == "bot"
    assert system[0].sender_type == "system"
    assert "передал диалог менеджеру" in body_of(system[0])  # словарь 10 §7.2


async def test_do_handoff_writes_audit_by_the_contract(db_sessionmaker, conv_seed):
    bot_id = uuid.uuid4()
    outbox = Outbox()
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        state = BotState.from_conv(conv)
        state.step = "ai_draft"
        await do_handoff(s, conv, state, outbox, reason="ai_low_confidence", bot_id=bot_id)
        conv.bot_vars = state.dump()

    rows = await audit_of(db_sessionmaker, "bot.handoff")
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id is None  # действие бота, а не человека (06 §0.3)
    assert row.entity == "conversation"
    assert row.entity_id == str(conv_seed.conversation_id)
    assert row.details == {
        "reason": "ai_low_confidence",
        "step": "ai_draft",
        "bot_id": str(bot_id),
    }


async def test_do_handoff_publishes_conversation_updated(db_sessionmaker, conv_seed):
    outbox = Outbox()
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        state = BotState.from_conv(conv)
        await do_handoff(s, conv, state, outbox, reason="negative")
        conv.bot_vars = state.dump()

    kinds = [event.type for event in outbox.events]
    assert kinds.count("message:new") == 2  # заметка-сводка + системная запись
    updated = [e for e in outbox.events if e.type == "conversation:updated"]
    assert len(updated) == 1
    patch = updated[0].data["patch"]
    assert patch["status"] == "new"
    assert patch["bot_active"] is False
    assert NEGATIVE_TAG in patch["tags"]  # негатив поднимает диалог в топ (01 §5.1)


async def test_do_handoff_puts_the_dialog_into_the_inbox_queue(
    db_sessionmaker, conv_seed, load_conv
):
    """7.1: диалог, отданный ботом, обязан ждать в очереди — с кнопкой «Принять».

    «Вернуть в общую очередь» в 02 §4 до 7.1 значило `status='new'`. Теперь
    очередь — это `offered_at`: без него диалог лежит в «Новых» и не виден во
    «Входящих» никому, то есть бот отдал клиента в никуда.
    """
    moment = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
    outbox = Outbox()
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        # Диалог уже стоял в очереди с прихода клиента, и от него успели
        # отказаться — после передачи это ожидание должно начаться заново.
        conv.offered_at = moment - timedelta(hours=2)
        conv.declined_by = [str(uuid.uuid4())]
        conv.escalated_at = moment - timedelta(hours=1)
        state = BotState.from_conv(conv)
        await do_handoff(s, conv, state, outbox, reason="client_request", now=moment)
        conv.bot_vars = state.dump()

    conv = await load_conv(conv_seed.conversation_id)
    assert inbox.is_waiting(conv)
    assert as_utc(conv.offered_at) == moment, "ожидание считается с передачи, а не с начала бота"
    assert list(conv.declined_by or []) == [], "отказы бот-диалога тянутся в новое ожидание"
    assert conv.escalated_at is None

    frame = next(e for e in outbox.events if e.type == INBOX_NEW)
    row = frame.data["conversation"]
    assert row["id"] == str(conv.id)
    assert row["in_inbox"] is True
    assert row["client"]["name"] == "Иван"  # строка очереди — целиком, а не id
    assert row["waiting_seconds"] == 0
    patch = next(e for e in outbox.events if e.type == "conversation:updated").data["patch"]
    assert patch["in_inbox"] is True


async def test_do_handoff_is_idempotent(db_sessionmaker, conv_seed):
    outbox = Outbox()
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        state = BotState.from_conv(conv)
        assert await do_handoff(s, conv, state, outbox, reason="scenario") is True
        # второй вызов (напр. реакция классификатора после тика) — no-op
        assert await do_handoff(s, conv, state, outbox, reason="negative") is False
        conv.bot_vars = state.dump()

    assert len(await messages_of(db_sessionmaker, conv_seed.conversation_id, "note")) == 1
    assert len(await audit_of(db_sessionmaker, "bot.handoff")) == 1
    conv_rows = await messages_of(db_sessionmaker, conv_seed.conversation_id, "system")
    assert len(conv_rows) == 1


async def test_unknown_reason_still_hands_the_dialog_over(db_sessionmaker, conv_seed, load_conv):
    """Опечатка в reason не должна оставить клиента у молчащего бота."""
    outbox = Outbox()
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        state = BotState.from_conv(conv)
        assert await do_handoff(s, conv, state, outbox, reason="странная_причина") is True
        conv.bot_vars = state.dump()
    conv = await load_conv(conv_seed.conversation_id)
    assert conv.bot_active is False and conv.status == "new"


# ------------------------------------------------------------------ сводка


def test_summary_lists_collected_vars_and_hides_technical_ones():
    text = handoff_summary(
        "scenario",
        "Клиент описал проблему",
        {"problem": "Не включается", "phone": "+79990001122", "_ai_confidence": 0.42},
        {"device_model": "iPhone 13", "problem": "перетереть нельзя"},
        step="handoff_day",
    )
    assert "Проблема: Не включается" in text
    assert "Телефон: +79990001122" in text
    assert "Модель: iPhone 13" in text  # AI-извлечение дополняет
    assert "перетереть нельзя" not in text  # но не перетирает собранное `ask`-ом
    assert "_ai_confidence" not in text
    assert "handoff_day" in text


def test_summary_without_vars_is_still_readable():
    text = handoff_summary("ai_unavailable", None, {}, None)
    assert "AI недоступен" in text
    assert "Собрано ботом" not in text


def test_system_text_names_the_reason():
    assert "негатив" in handoff_system_text("negative")


# -------------------------------------------------------------------- теги


def test_add_conversation_tags_dedupes_and_preserves_order():
    conv = cast(Conversation, SimpleNamespace(tags=["первичный-приём"]))
    assert add_conversation_tags(conv, ["негатив", "первичный-приём", " "]) == ["негатив"]
    assert conv.tags == ["первичный-приём", "негатив"]
    assert add_conversation_tags(conv, ["негатив"]) == []


# -------------------------------------------------------- mute (02 §2.6)


async def test_mute_bot_silences_forever(db_sessionmaker, conv_seed, load_conv):
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        state = BotState.from_conv(conv)
        state.begin_waiting(kind="ask", step_id="ask_phone", var="phone", timeout=None)
        conv.bot_vars = state.dump()
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        user = User(
            email="op@leadchat.test",
            password_hash="x",
            full_name="Оператор",
            role="manager",
        )
        s.add(user)
        await s.flush()
        assert await mute_bot(s, conv, by_user=user) is True

    conv = await load_conv(conv_seed.conversation_id)
    assert conv.bot_active is False
    assert conv.bot_vars["muted"] is True
    # ожидающий bot_ask_timeout самоаннулируется: токена в bot_vars больше нет
    assert conv.bot_vars["waiting"] is None
    rows = await audit_of(db_sessionmaker, "bot.muted")
    assert len(rows) == 1 and rows[0].entity_id == str(conv_seed.conversation_id)


async def test_mute_bot_is_noop_when_already_muted(db_sessionmaker, conv_seed):
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        assert await mute_bot(s, conv) is True
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        assert await mute_bot(s, conv) is False
    assert len(await audit_of(db_sessionmaker, "bot.muted")) == 1


async def test_mute_bot_does_not_log_when_no_bot_was_running(db_sessionmaker, conv_seed):
    """Журнал не должен заполняться событиями о ботах, которых не было."""
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        conv.bot_active = False
        assert await mute_bot(s, conv) is False
    assert await audit_of(db_sessionmaker, "bot.muted") == []


async def test_muted_flag_survives_reopen(db_sessionmaker, conv_seed, load_conv):
    """Клиент вернулся, статус снова `new` — `muted` не сбрасывается ничем."""
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, conv_seed.conversation_id)
        await mute_bot(s, conv)
    async with db_sessionmaker() as s, s.begin():  # эмуляция reopen из DESIGN §8.3
        conv = await s.get(Conversation, conv_seed.conversation_id)
        conv.status = "new"
    conv = await load_conv(conv_seed.conversation_id)
    assert BotState.from_conv(conv).muted is True
