"""Список каналов не спрашивает базу по карточке (замер 31.08).

⚠ ЧИСЛА, ИЗ-ЗА КОТОРЫХ ЭТО НАЙДЕНО. Разбор скорости всех ручек на боевом:
`/api/v1/avito-accounts` — медиана 0,796 с, максимум 1,02 с, тогда как медиана
ВСЕХ ОСТАЛЬНЫХ ручек 26–150 мс. Самая медленная точка продукта, и единственная
медленная.

ПРИЧИНА. Карточка канала спрашивала «когда по каналу было последнее входящее»
сама, в цикле по странице. Запрос идёт по секционированной таблице сообщений;
на боевом плане одно только ПЛАНИРОВАНИЕ стоило 35 мс, выполнение — 103 мс. На
тридцати пяти каналах это больше секунды впустую. Соседние строки того же
маршрута прямо запрещают такое («один запрос на всю страницу, а не по запросу
на карточку: N+1 в списке — тот самый запрет 01 §5.1») — но эта ветка запрет
обходила.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.services import channel_health

pytestmark = pytest.mark.anyio


async def test_карта_готовится_одним_запросом(db_sessionmaker, make_avito_account):
    """Карта отвечает по всем каналам сразу и не падает на пустых."""
    первый = await make_avito_account(910001)
    второй = await make_avito_account(910002)

    async with db_sessionmaker() as s:
        карта = await channel_health.последние_входящие_по_каналам(s, [первый.id, второй.id])

    assert set(карта) == {первый.id, второй.id}, (
        "канал пропал из карты — карточка снова пойдёт в базу сама"
    )


async def test_пустой_список_в_базу_не_ходит(db_sessionmaker):
    async with db_sessionmaker() as s:
        assert await channel_health.последние_входящие_по_каналам(s, []) == {}


async def test_контекст_страницы_несёт_карту(db_sessionmaker, make_avito_account):
    """⚠ БЕЗ ЭТОГО ПРОВЕРКИ ВЫШЕ СТОРОЖИЛИ БЫ ФУНКЦИЮ, КОТОРУЮ НИКТО НЕ ЗОВЁТ.

    Механизм, написанный и не подключённый, — самый частый дефект в этом
    проекте; сегодня он ловился трижды.
    """
    канал = await make_avito_account(910003)

    async with db_sessionmaker() as s:
        с_картой = await channel_health.load_context(s, account_ids=[канал.id])
        без_карты = await channel_health.load_context(s)

    assert с_картой.последние_входящие is not None, "контекст страницы не готовит карту"
    assert канал.id in с_картой.последние_входящие
    assert без_карты.последние_входящие is None, (
        "одиночная карточка получила карту — лишний запрос там, где он не нужен"
    )


async def test_карточка_берёт_из_карты_а_не_из_базы(db_sessionmaker, make_avito_account, redis):
    """⚠ ГЛАВНАЯ ПРОВЕРКА: карта должна ИСПОЛЬЗОВАТЬСЯ, а не просто готовиться.

    Диверсия показала, что без неё остальные сторожат карту, которую никто не
    читает: маршрут её готовит, контекст несёт — а карточка по-прежнему идёт в
    базу, и секунда ожидания никуда не девается. Подкладываем в карту заведомо
    узнаваемое время и требуем, чтобы вернулось именно оно.
    """
    from datetime import UTC, datetime

    канал = await make_avito_account(910004)
    метка = datetime(2026, 8, 30, 12, 34, 56, tzinfo=UTC)

    async with db_sessionmaker() as s:
        # Контекст неизменяем намеренно — собираем нужный, а не правим готовый.
        основа = await channel_health.load_context(s, account_ids=[канал.id])
        ctx = dataclasses.replace(основа, последние_входящие={канал.id: метка})
        получено = await channel_health._last_event_at(s, redis, канал.id, ctx=ctx)

    assert получено == метка, (
        "карточка не читает готовую карту — запрос на канал остался, секунда тоже"
    )


def test_маршрут_просит_карту_на_всю_страницу():
    """Сторож подключения: маршрут обязан передавать список каналов."""
    import pathlib

    корень = pathlib.Path(__file__).resolve().parents[2]
    текст = (корень / "app" / "api" / "routes" / "avito_connect.py").read_text(encoding="utf-8")
    assert "load_context(db, account_ids=" in текст, (
        "список каналов снова спрашивает базу по карточке — вернулась секунда ожидания"
    )


async def test_молчащий_дольше_окна_канал_не_становится_безмолвным(
    db_sessionmaker, make_avito_account
):
    """⚠ ОКНО УСКОРЯЕТ, НО МЕНЯЕТ СМЫСЛ ПУСТОГО ОТВЕТА — ЗДЕСЬ ГРАНИЦА.

    Запрос карты обходил ВСЕ партиции `messages`; граница в 30 суток убирает
    большую их часть (замер на бою 03.09: 106,9 → 62,7 мс, 121 → 47 узлов
    плана). Но канал, молчащий сорок дней, в окно не попадает — и вернул бы
    `None`, а `None` читается потребителем как «входящих не было НИКОГДА».
    Мёртвый канал перестал бы значиться молчащим: ровно то, ради чего сторож
    и написан.

    Поэтому по «молчунам» идёт второй запрос, уже без границы. Тест держит
    именно его: убери запасной ход — и здесь будет `None`.
    """
    from datetime import UTC, datetime, timedelta

    from app.models import Client, Conversation, Message

    канал = await make_avito_account(910010)
    давно = datetime.now(UTC) - timedelta(days=channel_health.RHYTHM_WINDOW_DAYS + 10)

    async with db_sessionmaker() as s:
        клиент = Client(channel="avito", external_id="910010-old")
        s.add(клиент)
        await s.flush()
        диалог = Conversation(
            channel="avito",
            external_chat_id="910010-silent",
            account_id=канал.id,
            client_id=клиент.id,
            status="closed",
            last_message_at=давно,
        )
        s.add(диалог)
        await s.flush()
        s.add(
            Message(
                conversation_id=диалог.id,
                direction="in",
                sender_type="client",
                body="последнее, что от него было",
                attachments=[],
                created_at=давно,
            )
        )
        await s.commit()

    async with db_sessionmaker() as s:
        карта = await channel_health.последние_входящие_по_каналам(s, [канал.id])

    assert карта[канал.id] is not None, (
        "канал, молчащий дольше окна, отдан как «входящих не было никогда» — "
        "сторож тишины ослеп ровно на тех каналах, ради которых он есть"
    )
    assert карта[канал.id].date() == давно.date()
