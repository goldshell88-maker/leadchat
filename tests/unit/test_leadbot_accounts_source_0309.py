"""Список каналов лид-бота несёт источник (просьба владельца 03.09).

⚠ ПРОСЬБА ДОСЛОВНО: «источники должны стоять везде». Показан был список
каналов лид-бота — два десятка строк вида «Александр КП», «Александр МНЧ»,
по которым не отличить, к какому источнику подключаешь бота.

⚠ ЗДЕСЬ ПРОБЕЛ БЫЛ НЕ В ВЁРСТКЕ, А В ДАННЫХ. В остальных списках источник уже
приходил (`lead_origin` есть и в справочнике каналов, и в строке диалога), и
хватало одной подписи на фронте. Этот ответ собирается отдельно и нёс только
`id`, `title` и признак занятости — показывать было нечего.
"""

from __future__ import annotations

import pytest

from app.services import leadbot_admin

pytestmark = pytest.mark.anyio


async def test_канал_отдаётся_вместе_с_источником(db_sessionmaker, make_avito_account):
    канал = await make_avito_account(920001)
    async with db_sessionmaker() as s:
        строка = await s.get(type(канал), канал.id)
        строка.lead_origin = "В95"
        await s.commit()

    async with db_sessionmaker() as s:
        свод = await leadbot_admin.overview(s)

    наш = next(a for a in свод["accounts"] if a["id"] == str(канал.id))
    assert наш["lead_origin"] == "В95", (
        "источника нет в ответе — на экране его взять неоткуда, "
        "и подпись соберётся из одного названия"
    )


async def test_канал_без_источника_отдаёт_пусто(db_sessionmaker, make_avito_account):
    """Пусто — это ответ, а не пропуск: подпись останется прежней."""
    канал = await make_avito_account(920002)
    async with db_sessionmaker() as s:
        свод = await leadbot_admin.overview(s)

    наш = next(a for a in свод["accounts"] if a["id"] == str(канал.id))
    assert "lead_origin" in наш and наш["lead_origin"] is None
