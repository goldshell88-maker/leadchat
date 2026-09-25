"""``repair-empty-messages``: переименование «неподдерживаемых» по сырцу.

⚠ ЗАМЕР БОЯ 15 АВГУСТА, ради которого второй проход и появился: из 30
«сообщений неподдерживаемого вида» 14 оказались `appCall` — клиент ЗВОНИЛ
через приложение Авито, а лента предлагала «открыть диалог в Авито».
Диспетчер не перезванивал, потому что не знал, что был звонок.

Тест интеграционный, а не unit, по необходимости: переименование ищет вид в
сохранённом конверте JSONB-операторами (`payload->'payload'->>'…'`), которых
SQLite не умеет. Гонять его на подменной базе значило бы проверять не тот SQL,
что пойдёт в бой.
"""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from app.cli import apply_repair_empty
from app.integrations.avito.adapter import UNSUPPORTED_MESSAGE_TEXT
from app.models import AvitoAccount, Client, Conversation, Message, WebhookRawLog

pytestmark = pytest.mark.anyio


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


@pytest.fixture
def sessionmaker(pg_engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(pg_engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
async def _clean(pg_engine: AsyncEngine) -> None:
    first = datetime.now(UTC).date().replace(day=1)
    cursor = (first - timedelta(days=1)).replace(day=1)
    async with pg_engine.begin() as conn:
        for _ in range(3):
            end = (cursor + timedelta(days=32)).replace(day=1)
            await conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS messages_y{cursor.year:04d}m{cursor.month:02d} "
                    f"PARTITION OF messages FOR VALUES FROM ('{cursor}') TO ('{end}')"
                )
            )
            cursor = end
        await conn.execute(
            text(
                "TRUNCATE webhook_raw_log, messages, conversations, clients, avito_accounts CASCADE"
            )
        )


def _raw(message_id: str, kind: str) -> dict:
    """Конверт вебхука ровно той формы, что лежит на бою (v3.0.0)."""
    return {
        "id": str(uuid.uuid4()),
        "version": "v3.0.0",
        "payload": {
            "type": "message",
            "value": {"id": message_id, "type": kind, "content": {}, "chat_id": "u2i-x"},
        },
    }


async def _body_of(sessionmaker, external_id: str) -> str | None:
    async with sessionmaker() as s:
        row = (
            await s.execute(select(Message).where(Message.external_message_id == external_id))
        ).scalar_one()
        return row.body


async def _seed(sessionmaker, *, body: str | None, external_id: str) -> uuid.UUID:
    async with sessionmaker() as s:
        account = AvitoAccount(
            title="Тест",
            avito_user_id=111,
            access_token_enc=b"x",
            refresh_token_enc=b"x",
            token_expires_at=datetime.now(UTC),
            webhook_secret="s",
        )
        s.add(account)
        await s.flush()
        cl = Client(channel="avito", external_id=f"c-{external_id}", name="Клиент")
        s.add(cl)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{external_id}",
            account_id=account.id,
            client_id=cl.id,
            status="new",
        )
        s.add(conv)
        await s.flush()
        m = Message(
            conversation_id=conv.id,
            external_message_id=external_id,
            direction="in",
            sender_type="client",
            body=body,
            attachments=[],
            delivery_status="delivered",
        )
        s.add(m)
        await s.flush()
        mid = m.id
        await s.commit()
        return mid


async def test_a_call_hidden_behind_the_placeholder_gets_its_real_name(sessionmaker):
    """Заглушка со звонком за спиной становится «Клиент звонил…» — по сырцу."""
    await _seed(sessionmaker, body=UNSUPPORTED_MESSAGE_TEXT, external_id="call-1")
    async with sessionmaker() as s:
        s.add(WebhookRawLog(stream_id="1-1", payload=_raw("call-1", "appCall"), processed=True))
        await s.commit()

    async with sessionmaker() as s:
        _, renamed = await apply_repair_empty(s, dry_run=False)
    assert renamed == 1

    assert await _body_of(sessionmaker, "call-1") == "Клиент звонил через приложение Авито"


async def test_history_without_a_raw_envelope_is_left_honest(sessionmaker):
    """Строка без сохранённого конверта остаётся «неподдерживаемой».

    Выдумывать вид задним числом нельзя: у истории, загруженной до включения
    сырца, конверта нет, и честное «не знаем» лучше уверенной догадки.
    """
    await _seed(sessionmaker, body=UNSUPPORTED_MESSAGE_TEXT, external_id="old-1")

    async with sessionmaker() as s:
        _, renamed = await apply_repair_empty(s, dry_run=False)
    assert renamed == 0

    assert await _body_of(sessionmaker, "old-1") == UNSUPPORTED_MESSAGE_TEXT


async def test_dry_run_counts_but_changes_nothing(sessionmaker):
    """Сухой прогон называет число и не трогает базу — сначала посмотреть."""
    await _seed(sessionmaker, body=UNSUPPORTED_MESSAGE_TEXT, external_id="call-2")
    async with sessionmaker() as s:
        s.add(WebhookRawLog(stream_id="1-2", payload=_raw("call-2", "appCall"), processed=True))
        await s.commit()

    async with sessionmaker() as s:
        _, renamed = await apply_repair_empty(s, dry_run=True)
    assert renamed == 1, "сухой прогон обязан назвать, сколько переименует"

    assert await _body_of(sessionmaker, "call-2") == UNSUPPORTED_MESSAGE_TEXT, (
        "сухой прогон не имеет права менять"
    )
