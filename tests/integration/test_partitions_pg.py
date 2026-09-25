"""INT-10 (07 §1.2): партиции messages. Миграция 0002 создаёт текущий и
следующий месяц; scheduler-job ensure_message_partitions идемпотентно
обеспечивает их и дальше — вставка в следующий месяц не падает."""

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from app.scheduler import partitions as partitions_mod
from app.scheduler.partitions import (
    MONTHS_AHEAD,
    MONTHS_BACK,
    PartitionCoverage,
    ensure_message_partitions,
    partition_name,
)
from tests.integration.conftest import requires_docker

pytestmark = requires_docker


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


async def _partitions(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT c.relname FROM pg_inherits i "
                "JOIN pg_class c ON c.oid = i.inhrelid "
                "JOIN pg_class p ON p.oid = i.inhparent WHERE p.relname = 'messages'"
            )
        )
        return set(rows.scalars())


async def test_migration_created_initial_partitions(pg_engine):
    """0002: текущий + следующий месяц существуют сразу после `upgrade head`."""
    today = datetime.now(UTC).date().replace(day=1)
    next_month = (
        today.replace(year=today.year + 1, month=1)
        if today.month == 12
        else today.replace(month=today.month + 1)
    )
    partitions = await _partitions(pg_engine)
    assert partition_name(today) in partitions
    assert partition_name(next_month) in partitions


async def test_ensure_partitions_idempotent_and_insert_next_month(pg_engine):
    """Job идемпотентен; INSERT с created_at в следующем месяце проходит."""
    ensured_first = await ensure_message_partitions(pg_engine)
    ensured_second = await ensure_message_partitions(pg_engine)  # повторный прогон
    assert ensured_first == ensured_second
    # Два года назад + текущий + месяц вперёд. Раньше было ровно два месяца, и
    # первое же сообщение истории старше текущего месяца роняло загрузку (#24).
    assert len(ensured_first) == MONTHS_BACK + MONTHS_AHEAD + 1

    next_month_ts = (datetime.now(UTC).replace(day=1) + timedelta(days=35)).replace(day=15)
    async with pg_engine.begin() as conn:
        account_id, client_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO avito_accounts (id, title, avito_user_id, access_token_enc, "
                "refresh_token_enc, token_expires_at, webhook_secret) VALUES "
                "(:id, 'p', :uid, '\\x00', '\\x00', now(), 's')"
            ),
            {"id": account_id, "uid": 777000123},
        )
        await conn.execute(
            text("INSERT INTO clients (id, channel, external_id) VALUES (:id, 'avito', :ext)"),
            {"id": client_id, "ext": f"p-{uuid.uuid4().hex[:8]}"},
        )
        await conn.execute(
            text(
                "INSERT INTO conversations (id, channel, external_chat_id, account_id, "
                "client_id, status) VALUES (:id, 'avito', :chat, :acc, :cl, 'new')"
            ),
            {
                "id": conv_id,
                "chat": f"p-{uuid.uuid4().hex[:8]}",
                "acc": account_id,
                "cl": client_id,
            },
        )
        await conn.execute(
            text(
                "INSERT INTO messages (id, conversation_id, direction, sender_type, body, "
                "created_at) VALUES (:id, :conv, 'in', 'client', 'из будущего месяца', :ts)"
            ),
            {"id": uuid.uuid4(), "conv": conv_id, "ts": next_month_ts},
        )
        inserted = (
            await conn.execute(
                text("SELECT count(*) FROM messages WHERE body = 'из будущего месяца'")
            )
        ).scalar_one()
    assert inserted == 1


# =============================================================================
# #24 — история Авито за год
# =============================================================================


async def test_year_old_history_has_somewhere_to_land(pg_engine):
    """Главная проверка задачи #24: сообщение годовой давности вставляется.

    Именно на нём падала загрузка боевого аккаунта. Проверяем не наличие
    партиции, а сам INSERT: партиция может существовать, но с неверными
    границами, и разницу видно только по факту записи.
    """
    await ensure_message_partitions(pg_engine)
    year_ago = datetime.now(UTC) - timedelta(days=365)

    async with pg_engine.begin() as conn:
        conv_id = await _seed_conversation(conn)
        await conn.execute(
            text(
                "INSERT INTO messages (id, conversation_id, direction, sender_type, body, "
                "attachments, delivery_status, created_at) VALUES "
                "(:id, :conv, 'in', 'client', 'из прошлого года', '[]', 'delivered', :ts)"
            ),
            {"id": uuid.uuid4(), "conv": conv_id, "ts": year_ago},
        )
        stored = await conn.execute(
            text("SELECT count(*) FROM messages WHERE created_at = :ts"), {"ts": year_ago}
        )
        assert stored.scalar_one() == 1


async def test_coverage_creates_partition_beyond_the_window(pg_engine):
    """Второй слой: чат старше окна тоже должен лечь.

    Два года — щедро, но не бесконечно. Попадётся переписка пятилетней
    давности, и первый слой промахнётся; точное покрытие по факту данных для
    этого и заведено.
    """
    long_ago = datetime.now(UTC) - timedelta(days=5 * 365)
    assert partition_name(long_ago.date()) not in await _partitions(pg_engine)

    coverage = PartitionCoverage(pg_engine)
    await coverage.ensure(long_ago)

    assert partition_name(long_ago.date()) in await _partitions(pg_engine)

    async with pg_engine.begin() as conn:
        conv_id = await _seed_conversation(conn)
        await conn.execute(
            text(
                "INSERT INTO messages (id, conversation_id, direction, sender_type, body, "
                "attachments, delivery_status, created_at) VALUES "
                "(:id, :conv, 'in', 'client', 'очень старое', '[]', 'delivered', :ts)"
            ),
            {"id": uuid.uuid4(), "conv": conv_id, "ts": long_ago},
        )


async def test_coverage_asks_the_database_once_per_month(pg_engine, monkeypatch):
    """Повторный вызов для того же месяца не идёт в базу.

    Не экономия запросов, а блокировка: CREATE TABLE ... PARTITION OF берёт
    тяжёлый лок на родительской таблице, и вызов на каждое из десятков тысяч
    сообщений держал бы приём новых в очереди весь прогон.
    """
    coverage = PartitionCoverage(pg_engine)
    moment = datetime.now(UTC) - timedelta(days=4 * 365)

    calls: list[list] = []
    original = partitions_mod._create

    async def counting_create(engine, months):
        months = list(months)
        calls.append(months)
        return await original(engine, months)

    monkeypatch.setattr(partitions_mod, "_create", counting_create)

    await coverage.ensure(moment)
    await coverage.ensure(moment)  # тот же месяц, другое число
    await coverage.ensure(moment.replace(day=1))

    assert len(calls) == 1, "один месяц — один поход в базу за DDL"

    # А другой месяц — новый поход: память не должна затыкать настоящую работу.
    await coverage.ensure(moment - timedelta(days=40))
    assert len(calls) == 2


async def _seed_conversation(conn) -> uuid.UUID:
    """Минимальный диалог: у messages внешний ключ на conversations."""
    account_id, client_id, conv_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await conn.execute(
        text(
            "INSERT INTO avito_accounts (id, title, avito_user_id, access_token_enc, "
            "refresh_token_enc, token_expires_at, status, webhook_secret) VALUES "
            "(:id, 'part-test', :uid, '\\x61', '\\x72', now() + interval '1 day', 'active', 's')"
        ),
        {"id": account_id, "uid": int(uuid.uuid4().int % 10**9)},
    )
    await conn.execute(
        text(
            "INSERT INTO clients (id, channel, external_id, name) "
            "VALUES (:id, 'avito', :ext, 'Клиент')"
        ),
        {"id": client_id, "ext": f"part-{client_id}"},
    )
    await conn.execute(
        text(
            "INSERT INTO conversations (id, channel, external_chat_id, account_id, client_id, "
            "status) VALUES (:id, 'avito', :ext, :acc, :cli, 'closed')"
        ),
        {"id": conv_id, "ext": f"part-{conv_id}", "acc": account_id, "cli": client_id},
    )
    return conv_id


# =============================================================================
# Стирание переписки канала — на НАСТОЯЩЕМ PostgreSQL
# =============================================================================


async def test_purge_history_really_deletes_on_postgres(pg_engine):
    """Отключение канала стирает переписку — и не падает на внешнем ключе.

    ПОЧЕМУ ЭТОТ ТЕСТ ЗДЕСЬ, А НЕ В ЮНИТАХ. В `purge_history` было написано
    «сообщения уходят каскадом за диалогами». Это предположение, и оно
    оказалось неверным: у `conversation_participants` и `conversation_pins`
    каскад есть, у `messages` — правило NO ACTION. На проде отключение канала
    падало с «Внутренняя ошибка сервера», и человек не мог ни отключить канал,
    ни удалить его.

    Юнит-тесты этого поймать не могли: они идут на SQLite, а он по умолчанию
    внешние ключи не проверяет вовсе — удаление проходило и «подтверждало»
    ошибочное допущение. Такие вещи видны только на настоящей базе.
    """
    import uuid as _uuid

    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.models import Conversation, Message
    from app.services import avito_accounts as svc

    await ensure_message_partitions(pg_engine)
    factory = async_sessionmaker(pg_engine, expire_on_commit=False)

    async with factory() as db:
        conv_id = await _seed_conversation_orm(db)
        account_id = (await db.get(Conversation, conv_id)).account_id
        db.add(
            Message(
                conversation_id=conv_id,
                direction="in",
                sender_type="client",
                body="перед стиранием",
                attachments=[],
                delivery_status="delivered",
                created_at=datetime.now(UTC),
            )
        )
        await db.commit()

        removed = await svc.purge_history(db, account_id)
        await db.commit()

        assert removed == 1
        left_convs = await db.scalar(
            text("SELECT count(*) FROM conversations WHERE account_id = :a"), {"a": account_id}
        )
        left_msgs = await db.scalar(
            text("SELECT count(*) FROM messages WHERE conversation_id = :c"), {"c": conv_id}
        )
        assert (left_convs, left_msgs) == (0, 0)
        assert _uuid.UUID(str(conv_id))  # id остаётся корректным — просто строки нет


async def _seed_conversation_orm(db) -> "uuid.UUID":
    """Диалог через ORM — нужен account_id, чтобы стирать по нему."""
    from datetime import timedelta

    from app.models import AvitoAccount, Client, Conversation

    account = AvitoAccount(
        title="purge-test",
        avito_user_id=int(uuid.uuid4().int % 10**9),
        access_token_enc=b"a",
        refresh_token_enc=b"r",
        token_expires_at=datetime.now(UTC) + timedelta(days=1),
        status="active",
        webhook_secret="s",
    )
    client_row = Client(channel="avito", external_id=f"purge-{uuid.uuid4()}", name="Клиент")
    db.add_all([account, client_row])
    await db.flush()
    conv = Conversation(
        channel="avito",
        external_chat_id=f"purge-{uuid.uuid4()}",
        account_id=account.id,
        client_id=client_row.id,
        status="closed",
    )
    db.add(conv)
    await db.flush()
    return conv.id
