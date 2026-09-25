"""Режим работы бота: автоответ или подсказка оператору (миграция 0035).

Зачем отдельным файлом. До сих пор включённый бот отвечал клиенту сам — третьего не
было дано, и это слишком резко для выхода на живой аккаунт. Режим «подсказка» даёт
промежуточный шаг: бот считает ответ и кладёт его заметкой в диалог, клиент не получает
ничего, отвечает человек. Умолчание в базе — именно «подсказка», самое безопасное.

Стенд тот же, что у тестов движка (аккаунт + бот + диалог + «клиент пишет»), поэтому
фикстуры импортируются, а не копируются: проверяем боевой путь, а не свою модель его работы.
"""

# ruff: noqa: F811 — фикстуры движка «переопределяются» аргументами тестов, так и задумано
from typing import Literal

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from app.bots.state import BotState
from app.models import Bot, Conversation, Message
from app.schemas.bots import BotWrite
from tests.unit.test_bot_engine import (  # noqa: F401 — chat/make_* нужны как фикстуры
    DAY,
    StubAI,
    bot_messages,
    chat,
    make_bot,
    make_world,
    notes,
    world_ctx,
)

PREFIX = "Подсказка бота: "


async def suggestions(db_sessionmaker, conv_id) -> list[str]:
    """Заметки-подсказки, без прочих служебных заметок диалога."""
    return [n for n in await notes(db_sessionmaker, conv_id) if n.startswith(PREFIX)]


async def counters_row(db_sessionmaker, conv_id) -> int:
    async with db_sessionmaker() as s:
        rows = await s.execute(select(Conversation).where(Conversation.id == conv_id))
        conv = rows.scalar_one()
    return BotState.from_conv(conv).counters.bot_msgs_row


# ------------------------------------------------------------------ умолчание


async def test_the_two_switches_are_independent(db_sessionmaker):
    """«Кто думает» и «что делать с ответом» — разные вопросы, и путать их дорого.

    Лид-бот в подсказке — главный режим первой недели: чужой мозг считает ответ,
    клиент не получает ничего, человек читает и решает. Если бы одно поле означало
    оба ответа, такого сочетания просто не существовало бы."""
    async with db_sessionmaker() as s:
        bot = Bot(
            name="Лид-бот подсказкой",
            schedule={"always": True},
            scenario={"version": 1},
            ai_provider="leadbot",
            mode="suggest",
        )
        s.add(bot)
        await s.commit()
        await s.refresh(bot)
        assert (bot.ai_provider, bot.mode) == ("leadbot", "suggest")


async def test_db_rejects_unknown_provider(db_sessionmaker):
    """Справочник поставщиков держит база: опечатка не превратится в тихий откат.

    `provider_of` при неизвестном значении честно возвращает Claude — и бот, который
    владелец считает переведённым на лид-бота, месяц отвечал бы чужим регламентом."""
    from sqlalchemy.exc import IntegrityError

    async with db_sessionmaker() as s:
        s.add(
            Bot(
                name="Кривой мозг",
                schedule={"always": True},
                scenario={"version": 1},
                ai_provider="лидбот",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


async def test_db_default_is_suggest(db_sessionmaker):
    """Умолчание самой базы (0035): забыть указать режим — безопасно, бот промолчит."""
    async with db_sessionmaker() as s:
        bot = Bot(name="Без режима", schedule={"always": True}, scenario={"version": 1})
        s.add(bot)
        await s.commit()
        await s.refresh(bot)
        assert bot.mode == "suggest"
        assert bot.ai_provider == "claude", "мозг по умолчанию тоже «как было»"


async def test_db_rejects_unknown_mode(db_sessionmaker):
    """Справочник режимов держит база: чужое значение не запишется ни через какой путь.

    Без этого опечатка в режиме означала бы бота, который молча не отправляет (или,
    наоборот, отправляет) — и заметить это можно было бы только по жалобе клиента."""
    from sqlalchemy.exc import IntegrityError

    async with db_sessionmaker() as s:
        s.add(
            Bot(
                name="Кривой режим",
                schedule={"always": True},
                scenario={"version": 1},
                mode="операторский",
            )
        )
        with pytest.raises(IntegrityError):
            await s.commit()


# ------------------------------------------------------------------ поведение


async def test_suggest_mode_writes_a_note_and_sends_nothing(
    db_sessionmaker, world_ctx, make_world, make_bot, chat
):
    """Главное свойство режима: клиент не получает НИЧЕГО, оператор видит текст."""
    world = await make_world(bot=await make_bot(mode="suggest"))

    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")

    assert await bot_messages(db_sessionmaker, world.conversation_id) == []
    hints = await suggestions(db_sessionmaker, world.conversation_id)
    assert hints, "оператор обязан увидеть предложенный ответ"
    assert hints[0].removeprefix(PREFIX).strip()


async def test_suggest_mode_does_not_enqueue_delivery(
    db_sessionmaker, redis, world_ctx, make_world, make_bot, chat
):
    """Ни исходящего сообщения, ни задачи доставки: до Авито подсказка не доезжает."""
    world = await make_world(bot=await make_bot(mode="suggest"))

    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")

    async with db_sessionmaker() as s:
        outgoing = (
            (
                await s.execute(
                    select(Message).where(
                        Message.conversation_id == world.conversation_id,
                        Message.direction == "out",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert outgoing == []
    assert [k async for k in redis.scan_iter("arq:job:deliver:*")] == []


async def test_auto_mode_still_talks_to_the_client(
    db_sessionmaker, world_ctx, make_world, make_bot, chat
):
    """Обратная сторона: в автоответе всё как раньше — текст уходит, подсказок нет."""
    world = await make_world(bot=await make_bot(mode="auto"))

    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")

    assert await bot_messages(db_sessionmaker, world.conversation_id)
    assert await suggestions(db_sessionmaker, world.conversation_id) == []


async def test_suggest_mode_respects_the_loop_guard(
    db_sessionmaker, world_ctx, make_world, make_bot, chat
):
    """Счётчик реплик подряд считает и подсказки.

    Иначе предохранитель зацикливания (02 §2.7) в этом режиме молча выключился бы, и
    зациклившийся сценарий засыпал бы диалог заметками — оператору это ничем не лучше,
    чем клиенту."""
    world = await make_world(bot=await make_bot(mode="suggest"))

    await chat(world_ctx(ai=StubAI(), now=DAY), world).says("Здравствуйте")

    assert await suggestions(db_sessionmaker, world.conversation_id)
    assert await counters_row(db_sessionmaker, world.conversation_id) >= 1


# ------------------------------------------------------------------ контракт API


@pytest.mark.parametrize("mode", ["suggest", "auto"])
def test_schema_accepts_both_modes(mode: Literal["suggest", "auto"]):
    assert BotWrite(name="Бот", scenario={"version": 1}, mode=mode).mode == mode


def test_schema_rejects_unknown_mode():
    """Опечатка в режиме обязана падать на входе, а не превращаться в тихое молчание бота."""
    with pytest.raises(ValidationError):
        BotWrite(name="Бот", scenario={"version": 1}, mode="авто")  # type: ignore[arg-type]


def test_schema_default_is_the_safe_one():
    body = BotWrite(name="Бот", scenario={"version": 1})
    assert (body.ai_provider, body.mode) == ("claude", "suggest")


@pytest.mark.parametrize("provider", ["claude", "leadbot"])
def test_schema_accepts_both_brains(provider: Literal["claude", "leadbot"]):
    body = BotWrite(name="Бот", scenario={"version": 1}, ai_provider=provider)
    assert body.ai_provider == provider


def test_schema_rejects_unknown_brain():
    with pytest.raises(ValidationError):
        BotWrite(name="Бот", scenario={"version": 1}, ai_provider="лид-бот")  # type: ignore[arg-type]


# ------------------------------------- черновик при низкой уверенности


async def test_suggest_mode_keeps_the_draft_when_the_bot_is_unsure(
    db_sessionmaker, world_ctx, make_world, make_bot, chat
):
    """Ответ был, уверенности не хватило — оператор всё равно видит черновик.

    ⚠ РАЗНЫЙ СЧЁТ В ДВУХ РЕЖИМАХ. В автоответе низкая уверенность значит «клиенту
    лучше молчание, чем сомнительный ответ», и это верно. В режиме подсказки клиенту
    и так не уходит ничего: выбрасывая черновик, мы наказываем не клиента, а
    диспетчера — он остаётся без помощи ровно там, где бот сам растерялся.
    Замер по живой выгрузке: 64 подсказки из 924 (6.9%) не показаны по этой причине.

    Передача человеку при этом НЕ отменяется: причина видна в ленте, решает человек.
    """
    ai = StubAI(
        answer={"reply": "Наверное, дело в матрице", "confidence": 0.2, "needs_operator": False}
    )
    world = await make_world(bot=await make_bot(mode="suggest"))
    client = chat(world_ctx(ai=ai, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Странный дефект, не пойму")

    assert await bot_messages(db_sessionmaker, world.conversation_id) == [], "клиенту — ничего"
    hints = await suggestions(db_sessionmaker, world.conversation_id)
    assert any("матрице" in h for h in hints), "черновик обязан дойти до оператора"


async def test_auto_mode_still_withholds_the_unsure_answer(
    db_sessionmaker, world_ctx, make_world, make_bot, chat
):
    """Обратная сторона: клиенту сомнительный ответ по-прежнему не уходит."""
    ai = StubAI(
        answer={"reply": "Наверное, дело в матрице", "confidence": 0.2, "needs_operator": False}
    )
    world = await make_world(bot=await make_bot(mode="auto"))
    client = chat(world_ctx(ai=ai, now=DAY), world)
    await client.says("Здравствуйте")
    await client.says("Странный дефект, не пойму")

    assert "матрице" not in " ".join(await bot_messages(db_sessionmaker, world.conversation_id))
