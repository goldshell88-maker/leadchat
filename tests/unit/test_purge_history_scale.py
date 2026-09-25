"""Стирание переписки не должно упираться в размер канала.

ЧТО ЗДЕСЬ ЗАПЕРТО. `purge_history` собирала все id диалогов канала в память и
подставляла их в `IN (...)` — по параметру на диалог. У протокола PostgreSQL
потолок 32767 параметров на запрос: канал с бо́льшим числом диалогов отключить
со стиранием было нельзя вообще. Запрос падал, транзакция откатывалась, а
вебхук к этому моменту уже был снят — канал оставался в промежуточном
состоянии: снаружи молчит, внутри числится работающим.

ПОЧЕМУ ТЕСТ СЧИТАЕТ ПАРАМЕТРЫ, А НЕ ЗАВОДИТ 32768 ДИАЛОГОВ. Настоящее
воспроизведение — это десятки тысяч строк на каждый прогон и всё равно только
на PostgreSQL: SQLite такого потолка не знает и падение бы скрыл. Считать
параметры честнее и дешевле — ломается ровно то, что ломалось на проде:
запрос, который растёт вместе с каналом. Проверка сформулирована как
«у маленького и у большого канала запросы одинакового размера», поэтому она
не зависит ни от выбранного числа диалогов, ни от предела конкретной СУБД.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.models import AvitoAccount, Client, Conversation, Message
from app.services import avito_accounts as svc

pytestmark = pytest.mark.anyio

# Потолок протокола PostgreSQL (asyncpg отдаёт его как есть). Запрос,
# который его достаёт, не выполняется — он падает целиком.
PG_MAX_QUERY_PARAMS = 32767


async def _seed_channel(
    sessionmaker: async_sessionmaker[AsyncSession], *, conversations: int, avito_user_id: int
) -> uuid.UUID:
    """Канал с заданным числом диалогов, в каждом по сообщению."""
    now = datetime.now(UTC)
    async with sessionmaker() as session:
        account = AvitoAccount(
            title=f"purge-scale-{avito_user_id}",
            avito_user_id=avito_user_id,
            access_token_enc=b"enc-access",
            refresh_token_enc=b"enc-refresh",
            token_expires_at=now,
            status="active",
            webhook_secret="whsec-test",
        )
        session.add(account)
        await session.flush()
        for _ in range(conversations):
            client_row = Client(channel="avito", external_id=f"ext-{uuid.uuid4()}")
            session.add(client_row)
            await session.flush()
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{uuid.uuid4()}",
                account_id=account.id,
                client_id=client_row.id,
                status="closed",
            )
            session.add(conv)
            await session.flush()
            session.add(
                Message(
                    conversation_id=conv.id,
                    direction="in",
                    sender_type="client",
                    body="переписка перед стиранием",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=now,
                )
            )
        await session.commit()
        return account.id


async def _purge_counting_params(
    engine: AsyncEngine, db: AsyncSession, account_id: uuid.UUID
) -> tuple[int, int]:
    """Стереть переписку канала. Отдаёт (удалено диалогов, пик параметров)."""
    peak = 0

    def _record(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        nonlocal peak
        peak = max(peak, len(parameters) if parameters is not None else 0)

    sa.event.listen(engine.sync_engine, "before_cursor_execute", _record)
    try:
        removed = await svc.purge_history(db, account_id)
        await db.commit()
    finally:
        sa.event.remove(engine.sync_engine, "before_cursor_execute", _record)
    return removed, peak


async def test_purge_query_size_does_not_grow_with_the_channel(
    engine: AsyncEngine, db: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Запросы стирания одинаковы для канала из 3 диалогов и из 60.

    Если размер запроса растёт вместе с каналом, то где-то между ними лежит
    канал, который отключить уже невозможно, — и это будет самый нужный
    компании канал, потому что он самый большой.
    """
    small = await _seed_channel(db_sessionmaker, conversations=3, avito_user_id=990001)
    large = await _seed_channel(db_sessionmaker, conversations=60, avito_user_id=990002)

    removed_small, peak_small = await _purge_counting_params(engine, db, small)
    removed_large, peak_large = await _purge_counting_params(engine, db, large)

    # Стирание работает — иначе «параметров мало» ничего не стоит.
    assert (removed_small, removed_large) == (3, 60)
    for account_id in (small, large):
        left = await db.scalar(
            sa.select(sa.func.count())
            .select_from(Conversation)
            .where(Conversation.account_id == account_id)
        )
        assert left == 0
    left_messages = await db.scalar(sa.select(sa.func.count()).select_from(Message))
    assert left_messages == 0

    # Главное: число диалогов в запрос не попадает.
    assert peak_large == peak_small, (
        f"размер запроса зависит от канала: {peak_small} параметров на 3 диалога "
        f"против {peak_large} на 60 — на {PG_MAX_QUERY_PARAMS} параметрах "
        "отключение канала перестанет работать совсем"
    )
    assert peak_large < PG_MAX_QUERY_PARAMS


async def test_purge_of_an_empty_channel_is_a_no_op(
    engine: AsyncEngine, db: AsyncSession, db_sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Пустой канал стирается без ошибки и отдаёт ноль.

    Это обычный случай: промахнулись аккаунтом при подключении и сразу удаляют.
    """
    empty = await _seed_channel(db_sessionmaker, conversations=0, avito_user_id=990003)

    removed, _ = await _purge_counting_params(engine, db, empty)

    assert removed == 0
