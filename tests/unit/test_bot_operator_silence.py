"""Страж правила владельца 17.08: человек писал — бот молчит навсегда.

Правило живёт в двух рубежах (`bot_entry_block` → "operator_replied" на входе
и повторный guard в каждом тике `run_bot_step` → mute). Тест закрепляет, что
ЛЮБОЙ человеческий след блокирует бота — включая ответ из Jivo/приложения
Авито (эхо-зеркало 17.08 кладёт его operator'ом с пустым sender_user_id) и
операторские сообщения из загруженной истории.
"""

import uuid
from datetime import UTC, datetime

from app.bots.runtime import bot_entry_block, has_operator_messages
from app.models import AvitoAccount, Bot, Client, Conversation, Message


async def _conv_with(db_sessionmaker, account, *, sender_type: str | None):
    async with db_sessionmaker() as db:
        bot = Bot(id=uuid.uuid4(), name="Бот", scenario={"entry": "s", "steps": []}, mode="auto")
        db.add(bot)
        client = Client(id=uuid.uuid4(), channel="avito", external_id=f"c-{uuid.uuid4()}")
        db.add(client)
        await db.flush()
        acc = await db.get(AvitoAccount, account.id)
        acc.bot_id = bot.id
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4()}",
            account_id=account.id,
            client_id=client.id,
            status="new",
            status_since=datetime.now(UTC),
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=0,
            declined_by=[],
        )
        db.add(conv)
        await db.flush()
        if sender_type is not None:
            db.add(
                Message(
                    id=uuid.uuid4(),
                    conversation_id=conv.id,
                    direction="out",
                    sender_type=sender_type,
                    # sender_user_id НАМЕРЕННО пуст: так выглядит ответ из
                    # Jivo/приложения Авито и исходящее из истории
                    sender_user_id=None,
                    body="Здравствуйте, подскажу",
                    delivery_status="delivered",
                    created_at=datetime.now(UTC),
                )
            )
        await db.commit()
        return db, conv


async def test_jivo_reply_blocks_bot_forever(db_sessionmaker, make_avito_account):
    account = await make_avito_account()
    _, conv = await _conv_with(db_sessionmaker, account, sender_type="operator")
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, conv.id)
        assert await has_operator_messages(db, conv) is True
        assert await bot_entry_block(db, conv) == "operator_replied"


async def test_bots_own_words_do_not_silence_it(db_sessionmaker, make_avito_account):
    account = await make_avito_account()
    _, conv = await _conv_with(db_sessionmaker, account, sender_type="bot")
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, conv.id)
        assert await has_operator_messages(db, conv) is False
        assert await bot_entry_block(db, conv) != "operator_replied"
