"""Канал больше протокольного потолка PostgreSQL всё равно отключается.

ЧТО ЛОМАЛОСЬ. `purge_history` вычитывала все id диалогов канала и подставляла
их в `IN (...)` — по параметру на диалог. У протокола PostgreSQL потолок 32767
параметров на запрос, и он жёсткий: у канала с бо́льшим числом диалогов запрос
не выполнялся вовсе. «Отключить и стереть» падало с «Внутренней ошибкой
сервера», а вебхук к этому моменту был уже снят — канал оставался в
промежуточном состоянии: обращения не приходят, переписка на месте, в списке
он числится работающим. Ровно на боевых каналах, ради которых эта кнопка и
нужна.

ПОЧЕМУ НА НАСТОЯЩЕМ POSTGRESQL И НИГДЕ БОЛЬШЕ. Потолок принадлежит протоколу,
а не нашему коду: SQLite, на котором идут юниты, о нём не знает и падение
скрывает. В юнитах заперта форма запроса
(`tests/unit/test_purge_history_scale.py` — размер запроса не растёт вместе с
каналом), а здесь — сам факт: тридцать две тысячи диалогов стираются.

Тест намеренно дорогой (одна вставка на 32 768 строк) и намеренно один.
"""

import uuid
from collections.abc import AsyncIterator

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.integration.conftest import requires_docker

pytestmark = requires_docker

# Предел числа параметров в одном запросе на стороне протокола PostgreSQL.
# Берём на один диалог больше — это и есть первый канал, который до правки
# отключить было нельзя.
PG_MAX_QUERY_PARAMS = 32767
CONVERSATIONS = PG_MAX_QUERY_PARAMS + 1


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


async def test_channel_over_the_parameter_ceiling_can_still_be_purged(
    pg_engine: AsyncEngine,
) -> None:
    """32 768 диалогов канала стираются одним вызовом, без ошибки протокола."""
    from app.services import avito_accounts as svc

    account_id, client_id = uuid.uuid4(), uuid.uuid4()
    tag = uuid.uuid4().hex[:8]

    async with pg_engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO avito_accounts (id, title, avito_user_id, access_token_enc, "
                "refresh_token_enc, token_expires_at, status, webhook_secret) VALUES "
                "(:id, 'scale-test', :uid, '\\x61', '\\x72', now() + interval '1 day', "
                "'active', 's')"
            ),
            {"id": account_id, "uid": int(uuid.uuid4().int % 10**9)},
        )
        await conn.execute(
            text("INSERT INTO clients (id, channel, external_id) VALUES (:id, 'avito', :ext)"),
            {"id": client_id, "ext": f"scale-{tag}"},
        )
        # Одной вставкой: 32 768 отдельных INSERT'ов сделали бы тест
        # неподъёмным, а проверяем мы не скорость записи, а стирание.
        await conn.execute(
            text(
                "INSERT INTO conversations (id, channel, external_chat_id, account_id, "
                "client_id, status) SELECT gen_random_uuid(), 'avito', "
                "'scale-' || :tag || '-' || g, :acc, :cli, 'closed' "
                "FROM generate_series(1, :n) g"
            ),
            {"tag": tag, "acc": account_id, "cli": client_id, "n": CONVERSATIONS},
        )
        # По сообщению в каждый диалог: до правки на потолок упирались ОБА
        # запроса, и первым — как раз удаление сообщений.
        await conn.execute(
            text(
                "INSERT INTO messages (id, conversation_id, direction, sender_type, body, "
                "created_at) SELECT gen_random_uuid(), c.id, 'in', 'client', "
                "'перед стиранием', now() FROM conversations c WHERE c.account_id = :acc"
            ),
            {"acc": account_id},
        )

    factory = async_sessionmaker(pg_engine, expire_on_commit=False)
    async with factory() as db:
        removed = await svc.purge_history(db, account_id)
        await db.commit()

    assert removed == CONVERSATIONS

    async with pg_engine.begin() as conn:
        left_convs = (
            await conn.execute(
                text("SELECT count(*) FROM conversations WHERE account_id = :a"),
                {"a": account_id},
            )
        ).scalar_one()
        left_msgs = (
            await conn.execute(text("SELECT count(*) FROM messages WHERE body = 'перед стиранием'"))
        ).scalar_one()
        assert (left_convs, left_msgs) == (0, 0)
        # За собой убираем: база одна на весь набор интеграционных тестов.
        await conn.execute(text("DELETE FROM avito_accounts WHERE id = :a"), {"a": account_id})
        await conn.execute(text("DELETE FROM clients WHERE id = :c"), {"c": client_id})
