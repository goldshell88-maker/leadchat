"""Принятый человеком диалог закрыт для авто-бота (боевой дефект 30.08).

Слово владельца: «бот иногда входит в диалоги, которые приняли операторы, но не
успели ответить». Дыра была двойная: bot_entry_block проверял статус и «оператор
ОТВЕТИЛ» (has_operator_messages), а само ПРИНЯТИЕ — claim из очереди или
назначение — не проверял никто; ранний выход `bot_active` к тому же пускал бота
ПРОДОЛЖАТЬ сценарий в уже принятом диалоге.

Правило: принял человек — ведёт человек. Режим ПОДСКАЗОК под заслон не попадает:
подсказка — заметка сотруднику, в принятом диалоге она и нужна (решение 21.08).
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bots.runtime import bot_entry_block
from app.models import AvitoAccount, Bot, Client, Conversation, User

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def seed(db_sessionmaker: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    async with db_sessionmaker() as s:
        account = AvitoAccount(
            title="LP-Тест",
            avito_user_id=555000333,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=datetime.now(UTC) + timedelta(days=1),
            status="active",
            webhook_secret="whsec",
        )
        client = Client(channel="avito", external_id="c-cg", name="Клиент")
        user = User(
            email="op-cg@example.com", full_name="Оператор", password_hash="x", role="manager"
        )
        s.add_all([account, client, user])
        await s.flush()
        bot = Bot(name="Лид-бот", is_enabled=True, mode="auto", scenario={"steps": []})
        s.add(bot)
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
        return SimpleNamespace(
            conv_id=conv.id, user_id=user.id, bot_id=bot.id, account_id=account.id
        )


async def _block(db_sessionmaker, seed, **conv_updates):
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(select(Conversation).where(Conversation.id == seed.conv_id))
        ).scalar_one()
        for k, v in conv_updates.items():
            setattr(conv, k, v)
        await s.flush()
        bot = (await s.execute(select(Bot).where(Bot.id == seed.bot_id))).scalar_one()
        return await bot_entry_block(s, conv, bot=bot)


async def test_принятый_диалог_закрыт_для_бота(db_sessionmaker, seed):
    """Кнопка «Принять»: claimed_by ставится, ответа ещё нет — бот не входит."""
    assert await _block(db_sessionmaker, seed, claimed_by_id=seed.user_id) == "claimed_by_human"


async def test_назначенный_диалог_закрыт_для_бота(db_sessionmaker, seed):
    assert await _block(db_sessionmaker, seed, assignee_id=seed.user_id) == "claimed_by_human"


async def test_bot_active_не_спасает_в_принятом(db_sessionmaker, seed):
    """Главная половина дыры: бот ЖДАЛ ответа (bot_active), оператор принял —
    продолжать сценарий нельзя."""
    assert (
        await _block(db_sessionmaker, seed, bot_active=True, claimed_by_id=seed.user_id)
        == "claimed_by_human"
    )


async def test_непринятый_диалог_бот_ведёт_как_раньше(db_sessionmaker, seed):
    assert await _block(db_sessionmaker, seed) is None


async def test_предложенный_но_не_принятый_бот_ведёт(db_sessionmaker, seed):
    """offered_at — диалог лишь ПРЕДЛОЖЕН очередью; пока никто не нажал
    «Принять», бот работает."""
    assert await _block(db_sessionmaker, seed, offered_at=datetime.now(UTC)) is None


async def test_подсказки_живут_в_принятом_диалоге(db_sessionmaker, seed):
    """Режим suggest: подсказка — заметка сотруднику, в принятом диалоге она
    и нужна (решение 21.08). Заслон её не трогает."""
    async with db_sessionmaker() as s:
        bot = (await s.execute(select(Bot).where(Bot.id == seed.bot_id))).scalar_one()
        bot.mode = "suggest"
        await s.commit()
    assert await _block(db_sessionmaker, seed, claimed_by_id=seed.user_id) is None
