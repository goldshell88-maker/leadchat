"""Alembic env: async engine, runtime switch to the asyncpg driver.

URL resolution order:
1. ``sqlalchemy.url`` set programmatically (tests / -x overrides);
2. ``DATABASE_URL`` env var;
3. ``app.core.config.settings`` (imported lazily — full env required).
"""

import asyncio
import os
import re
from logging.config import fileConfig

import sqlalchemy as sa
from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401 — registers every table on the metadata
from app.models.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

# Objects Alembic must not manage (08 §1.4, autogenerate rule 2):
# monthly partitions of messages (scheduler's job), stats MV, and the
# DB-maintained generated column `search`.
_EXCLUDED_TABLES = re.compile(r"^(messages_y\d+.*|mv_conversation_stats)$")


def include_object(obj, name, type_, reflected, compare_to):  # noqa: ANN001
    if type_ == "table" and name is not None and _EXCLUDED_TABLES.match(name):
        return False
    if type_ == "column" and name == "search" and obj.table.name == "messages":
        return False
    return True


def _to_async(url: str) -> str:
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


def _database_url() -> str:
    url = config.get_main_option("sqlalchemy.url")
    if not url:
        url = os.environ.get("DATABASE_URL", "")
    if not url:
        from app.core.config import settings

        url = settings.database_url
    return _to_async(url)


def _configure(connection: Connection | None = None, url: str | None = None) -> None:
    context.configure(
        connection=connection,
        url=url,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        include_object=include_object,
    )


def run_migrations_offline() -> None:
    _configure(url=_database_url())
    with context.begin_transaction():
        context.run_migrations()


def _run_sync_migrations(connection: Connection) -> None:
    # ⚠ БЕЗ ЭТОГО НАКАТ МОГ МОЛЧА ОСТАНОВИТЬ ВСЮ СИСТЕМУ. Выкатка гонит миграции
    # ДО подмены кода (ship.sh, шаг 4), то есть старый код в этот момент живёт
    # и держит транзакции. `ALTER TABLE` берёт ACCESS EXCLUSIVE; наткнувшись на
    # чужую живую транзакцию, он встаёт в очередь БЕЗ СРОКА — умолчание
    # PostgreSQL `lock_timeout = 0` значит «ждать вечно», — а за ним в ту же
    # очередь встают ВСЕ последующие запросы к таблице, включая чтения. Пять
    # минут висящей выгрузки — пять минут стоящего канала, и в терминале это
    # выглядит просто как «скрипт задумался на шаге 4».
    #
    # Три секунды — из docs/31 §12: за это время живая транзакция обычная
    # завершается, а зависшая всё равно не завершится никогда. Отказ по
    # таймауту БЕЗОПАСЕН: цепочка миграций идёт одной транзакцией, при падении
    # схема целиком откатывается на прежнюю, и накат просто повторяют, когда
    # держатель замка найден и снят (`pg_stat_activity`).
    #
    # ⚠ `SET LOCAL` СТРОГО ВНУТРИ ТРАНЗАКЦИИ АЛЕМБИКА — И ЭТО НЕ СТИЛЬ, А ШРАМ.
    # Первая редакция ставила `SET` ДО `begin_transaction()`. SQLAlchemy на
    # первом же `execute` молча открывает ВНЕШНЮЮ транзакцию; alembic коммитит
    # свою, внутреннюю, а внешняя при закрытии соединения ОТКАТЫВАЕТСЯ — и
    # уносит с собой всю только что накаченную схему. В журнале при этом
    # напечатаны все «Running upgrade …» до единого, upgrade завершается без
    # ошибки, и «✓ схема на head» в выкатке было бы враньём. Поймано
    # интеграционным тестом `test_migrations_created_full_schema`: он один
    # проверяет не «команда прошла», а «таблицы существуют».
    _configure(connection=connection)
    with context.begin_transaction():
        connection.execute(sa.text("SET LOCAL lock_timeout = '3s'"))
        context.run_migrations()


async def _run_async_migrations() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run_sync_migrations)
    await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(_run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
