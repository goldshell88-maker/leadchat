"""Один выбор диалекта на весь бэкенд: ``INSERT ... ON CONFLICT``.

Postgres в бою, SQLite в юнит-тестах — конструктор запроса у них разный, и до
сегодня выбор был скопирован в приём входящих (`inbound._dialect_insert`).
Копия ровно одна, но копий такого рода не бывает по одной: следующему, кому
понадобится вставка «или ничего», придётся списывать её оттуда же. Здесь она
живёт одна и называется своим именем.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


def insert(db: AsyncSession) -> Any:
    """Конструктор ``INSERT``, умеющий ``on_conflict_do_nothing``.

    Тип возвращаемого — `Any`: у двух диалектов это два РАЗНЫХ класса
    (`postgresql.Insert` и `sqlite.Insert`), общего предка с
    `on_conflict_do_nothing` у них нет, и честного общего типа не существует.
    """
    if db.get_bind().dialect.name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        return pg_insert
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    return sqlite_insert
