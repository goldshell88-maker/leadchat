"""Схема после `upgrade head`: индексы журнала аудита и имена CHECK-ограничений.

ПОЧЕМУ ЭТО ПРОВЕРЯЕТСЯ НА НАСТОЯЩЕЙ БАЗЕ И ПО КАТАЛОГУ. Юнит-набор строит
схему из метаданных моделей на SQLite и alembic не запускает вовсе — то есть
не знает ни про индексы, заведённые голым DDL, ни про имена, которые
`naming_convention` подставил в момент выполнения миграции. Оба дефекта живут
именно в разрыве между «что написано в модели» и «что легло в базу», поэтому
единственное место, где их видно, — pg_indexes/pg_constraint после
`alembic upgrade head` (набор поднимает базу с нуля, см. conftest).

ПОЧЕМУ НЕ EXPLAIN. Проверять индекс планом честнее по духу, но на тестовой
базе нечестно по факту: на пустой таблице планировщик возьмёт seq scan при
любых индексах, а насыпать в общий `audit_log` тысячи строк нельзя — из него
читают тесты статистики в этой же сессии. Здесь проверяется наличие ровно того
ключа, под который написан `ORDER BY` ручки; что этот ключ нужен — объяснено в
шапке миграции 0034.
"""

import re
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

import app.models  # noqa: F401 — регистрирует таблицы на metadata
from app.models.base import Base
from tests.integration.conftest import requires_docker

pytestmark = requires_docker


@pytest.fixture
async def pg_engine(pg_async_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield engine
    await engine.dispose()


async def _index_defs(engine: AsyncEngine, table: str) -> dict[str, str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t"),
            {"t": table},
        )
        return {row.indexname: row.indexdef for row in rows}


async def _check_names(engine: AsyncEngine, table: str) -> set[str]:
    async with engine.connect() as conn:
        rows = await conn.execute(
            text(
                "SELECT conname FROM pg_constraint "
                "WHERE contype = 'c' AND conrelid = CAST(:t AS regclass)"
            ),
            {"t": table},
        )
        return set(rows.scalars())


def _model_check_names(table: str) -> set[str]:
    """Имена CHECK так, как их построит `naming_convention` из модели.

    Ровно то, что подставит `drop_constraint` следующей миграции, — поэтому
    сравнивать надо именно с этим набором, а не с текстом миграции.
    """
    return {
        str(c.name)
        for c in Base.metadata.tables[table].constraints
        if isinstance(c, sa.CheckConstraint)
    }


async def test_journal_order_is_backed_by_an_index(pg_engine):
    """`ORDER BY created_at DESC, id DESC` — порядок ленты `GET /audit-log`.

    До 0034 в `audit_log` было два индекса, и оба вели столбцом `action` или
    `entity`, которых в этом запросе может не быть вовсе: каждое открытие
    журнала читало и сортировало всю таблицу ради пятидесяти строк.
    """
    defs = await _index_defs(pg_engine, "audit_log")
    assert any(re.search(r"USING btree \(created_at DESC, id DESC\)", d) for d in defs.values()), (
        f"нет индекса под порядок журнала, есть только: {sorted(defs)}"
    )


async def test_filter_by_employee_is_backed_by_an_index(pg_engine):
    """Фильтр «по сотруднику» + тот же порядок внутри него.

    Ведущий столбец обязан быть `user_id`: индекс, где он второй, под этот
    фильтр не работает.
    """
    defs = await _index_defs(pg_engine, "audit_log")
    assert any(re.search(r"USING btree \(user_id\b", d) for d in defs.values()), (
        f"нет индекса под фильтр по сотруднику, есть только: {sorted(defs)}"
    )


@pytest.mark.parametrize("table", ["users", "notifications"])
async def test_check_constraint_names_match_the_models(pg_engine, table: str):
    """Имена CHECK в базе совпадают с теми, что строит модель.

    В 0001 и 0006 в `name=` передали уже полное имя, и `naming_convention`
    приписал приставку второй раз: в базе лежало
    `ck_notifications_ck_notifications_severity`. Пока никто не трогает
    ограничение — тихо; первая же миграция, снимающая его по имени из модели,
    падает `DROP CONSTRAINT`-ом в никуда посреди выката.
    """
    assert await _check_names(pg_engine, table) == _model_check_names(table)
