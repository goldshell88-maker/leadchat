"""Забрать диалог у бота, ничего не написав клиенту (запрос владельца 29.08).

До этой правки бота глушил ровно один путь — отправка сообщения (02 §2.6).
Чтобы вмешаться в диалог, оператор был обязан что-то написать клиенту, даже
когда хотел просто прочитать переписку, дособрать данные или позвонить.
Единственной альтернативой было выключить бота ЦЕЛИКОМ, на всех диалогах
сразу (`POST /bots/{id}/disable`), что несоразмерно задаче.

Ручка делает то же, что делает ответ, минус сам ответ: диалог назначается на
того, кто забрал («кто взял — тот и ведёт», 01 §6.2), и бот замолкает навсегда.
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bots.state import BotState
from app.models import AuditLog, AvitoAccount, Client, Conversation, Message, User
from app.services.messages import take_over_from_bot

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def seed(db_sessionmaker: async_sessionmaker[AsyncSession]) -> SimpleNamespace:
    """Аккаунт, клиент, оператор и диалог, который прямо сейчас ведёт бот."""
    async with db_sessionmaker() as s:
        account = AvitoAccount(
            title="LP-Тест",
            avito_user_id=555000222,
            access_token_enc=b"a",
            refresh_token_enc=b"r",
            token_expires_at=datetime.now(UTC) + timedelta(days=1),
            status="active",
            webhook_secret="whsec",
        )
        client = Client(channel="avito", external_id="c-take", name="Ирина")
        user = User(
            email="op@example.com",
            full_name="Оператор",
            password_hash="x",
            role="manager",
        )
        s.add_all([account, client, user])
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
        return SimpleNamespace(conversation_id=conv.id, user_id=user.id, account_id=account.id)


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


async def _взять(db_sessionmaker, seed) -> tuple[bool, bool, bool]:
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(select(Conversation).where(Conversation.id == seed.conversation_id))
        ).scalar_one()
        user = (await s.execute(select(User).where(User.id == seed.user_id))).scalar_one()
        итог = await take_over_from_bot(s, conv, user)
        await s.commit()
        return итог


async def test_бот_замолкает(db_sessionmaker, seed, load_conv):
    await _взять(db_sessionmaker, seed)
    conv = await load_conv(seed.conversation_id)
    assert conv.bot_active is False


async def test_бот_замолкает_навсегда(db_sessionmaker, seed, load_conv):
    """`muted` живёт в bot_vars: клиент вернётся — бот не оживёт (02 §2.6)."""
    await _взять(db_sessionmaker, seed)
    conv = await load_conv(seed.conversation_id)
    assert BotState.from_conv(conv).muted is True


async def test_диалог_назначается_на_того_кто_забрал(db_sessionmaker, seed, load_conv):
    """«Кто взял — тот и ведёт» (01 §6.2): забрать = принять, как и ответ."""
    await _взять(db_sessionmaker, seed)
    conv = await load_conv(seed.conversation_id)
    assert conv.assignee_id == seed.user_id


async def test_клиенту_ничего_не_отправляется(db_sessionmaker, seed):
    """Главное свойство: забрать можно МОЛЧА. Ни одного исходящего сообщения."""
    await _взять(db_sessionmaker, seed)
    async with db_sessionmaker() as s:
        исходящие = list(
            (
                await s.execute(
                    select(Message).where(
                        Message.conversation_id == seed.conversation_id,
                        Message.direction == "out",
                    )
                )
            ).scalars()
        )
    assert исходящие == []


async def test_в_журнале_есть_и_мьют_и_назначение(db_sessionmaker, seed):
    await _взять(db_sessionmaker, seed)
    async with db_sessionmaker() as s:
        действия = {
            a.action
            for a in (
                await s.execute(
                    select(AuditLog).where(AuditLog.entity_id == str(seed.conversation_id))
                )
            ).scalars()
        }
    assert "bot.muted" in действия
    assert "conversation.assigned" in действия


async def test_повторное_нажатие_не_ошибка(db_sessionmaker, seed, load_conv):
    """Кнопка одна и та же; «бот и так молчал» — нормальный исход, не сбой."""
    первый = await _взять(db_sessionmaker, seed)
    второй = await _взять(db_sessionmaker, seed)
    assert первый[2] is True  # в первый раз бот действительно вёл диалог
    assert второй[2] is False  # во второй — уже нет, и это не ошибка
    conv = await load_conv(seed.conversation_id)
    assert conv.bot_active is False


async def test_чужой_канал_забрать_нельзя(db_sessionmaker, seed, load_conv):
    """⚠ АУДИТ 30.08: у этой ручки не было замка на канал.

    28.08 путь «ответил — значит принял» получил стража
    `_assert_may_take_this_channel`: ничейный диалог берётся только на СВОЁМ
    канале — список «Все» по каналам не сужается, и чужой диалог открывается
    обычным нажатием. Эта ручка, добавленная 29.08, делает ровно то же
    присвоение, но звала `_auto_assign` напрямую, в обход стража: менеджер
    чужого канала жал «Забрать у бота» и становился хозяином обращения.
    """
    from app.core.errors import ApiError
    from app.models.account_operator import AccountOperator

    # Канал закреплён за ДРУГИМ оператором — значит для нашего он чужой.
    async with db_sessionmaker() as s:
        другой = User(
            email="foreign@example.com",
            full_name="Чужой",
            password_hash="x",
            role="manager",
        )
        s.add(другой)
        await s.flush()
        s.add(AccountOperator(account_id=seed.account_id, user_id=другой.id))
        await s.commit()

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation_id)
        user = await s.get(User, seed.user_id)
        with pytest.raises(ApiError) as поймано:
            await take_over_from_bot(s, conv, user)
    assert поймано.value.code == "forbidden", "чужой канал забрали без единой ошибки"

    conv = await load_conv(seed.conversation_id)
    assert conv.assignee_id is None, "диалог чужого канала всё-таки присвоен"
    assert conv.bot_active is True, "бот заглушён в чужом диалоге"


async def test_чужого_ответственного_не_перебиваем(db_sessionmaker, seed, load_conv):
    """Диалог уже за коллегой — забираем бота, но ответственного не меняем."""
    async with db_sessionmaker() as s:
        другой = User(
            email="other@example.com",
            full_name="Коллега",
            password_hash="x",
            role="manager",
        )
        s.add(другой)
        await s.flush()
        conv = (
            await s.execute(select(Conversation).where(Conversation.id == seed.conversation_id))
        ).scalar_one()
        conv.assignee_id = другой.id
        чужой_id = другой.id
        await s.commit()

    await _взять(db_sessionmaker, seed)
    conv = await load_conv(seed.conversation_id)
    assert conv.assignee_id == чужой_id
    assert conv.bot_active is False
