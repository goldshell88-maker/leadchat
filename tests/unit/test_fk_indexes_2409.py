"""Внешние ключи на то, что стирается пачками, стоят на индексе (24.09).

PostgreSQL не заводит индекс под внешний ключ сам, а проверять ключ при
удалении родителя обязан: без индекса каждое удаление — полный проход по
дочерней таблице. Диалоги и строки адресов удаляются тысячами за раз
(«Отключить и стереть» у канала), и у `clients` таких ключей нашлось два
(0086) — стирание упиралось в потолок запроса API уже после снятия подписки у
Авито. Сторож смотрит на модель: без объявления там следующий autogenerate
предложил бы индекс удалить.
"""

from __future__ import annotations

from sqlalchemy import UniqueConstraint

import app.models  # noqa: F401 — регистрирует все таблицы в метаданных
from app.models.base import Base

#: Родители, которых удаляют пачками: диалоги — при стирании истории канала,
#: строки адресов — каскадом вместе с диалогами.
BULK_DELETED = {"conversations", "client_address_candidates"}


def _leading_columns(table) -> set[str]:  # noqa: ANN001
    leading = {next(iter(table.primary_key.columns)).name} if table.primary_key.columns else set()
    leading |= {next(iter(ix.columns)).name for ix in table.indexes if ix.columns}
    leading |= {
        next(iter(c.columns)).name
        for c in table.constraints
        if isinstance(c, UniqueConstraint) and c.columns
    }
    return leading


def test_foreign_keys_to_bulk_deleted_parents_are_indexed() -> None:
    missing = [
        f"{table.name}.{column.name} → {fk.column.table.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        for fk in column.foreign_keys
        if fk.column.table.name in BULK_DELETED and column.name not in _leading_columns(table)
    ]

    assert missing == []
