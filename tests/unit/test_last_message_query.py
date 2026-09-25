"""Последнее сообщение диалога: проверка выборки (аудит 22.08, шаг 4).

ЗАЧЕМ ЭТОТ ФАЙЛ СУЩЕСТВУЕТ. `_load_related` строит превью строки списка — то,
что тринадцать диспетчеров видят весь день, — и отдельного покрытия у неё не
было: последнее сообщение, отсечение заметок, диалог без сообщений.

ИСТОРИЯ ОДНОГО ОПРОВЕРГНУТОГО ВЫВОДА, И ОНА ЗДЕСЬ НЕ РАДИ КРАСОТЫ. Аудит начался
с замера на боевой базе: запрос последнего сообщения планировался 44 мс при
исполнении 5 мс. Дальше три догадки о причине были проверены опытом и отпали —
число партиций (простой count по всем 26 планируется за 1.4 мс), число индексов
(проверено на отдельной схеме, разница 0.1 мс), подзапрос по `conversations`
(3.7 мс). Осталась четвёртая: окно над партиционированной таблицей. Замер
`DISTINCT ON` дал 1.5 мс — восемнадцатикратный выигрыш, и правка была написана.

Она НЕ выкачена, потому что честное сравнение её убило: 1.5 мс получались только
потому, что обе формулировки мерились в одной сессии psql и вторая доставалась
прогретому каталогу. В свежем соединении обе стоят одинаково (~40 мс), а во
ВТОРОМ запросе той же сессии обе стоят 1.2 мс. Приложение держит пул с
`pool_recycle`, соединения живут часами — значит эти 40 мс прод платит несколько
раз при прогреве пула, а не на каждый показ списка.

Вывод: дефекта нет, менять формулировку не за что. Тесты ниже остались как
покрытие, которого не хватало, а этот текст — чтобы следующий, кто увидит
«44 мс» в плане, не прошёл тот же круг заново.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import AvitoAccount, Client, Conversation, Message
from app.services import conversations as convs

T0 = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


async def _мир(db):
    acc = AvitoAccount(
        id=uuid.uuid4(),
        title="Канал",
        avito_user_id=1,
        status="active",
        webhook_secret="s",
        access_token_enc=b"enc",
        refresh_token_enc=b"enc",
        token_expires_at=T0 + timedelta(days=1),
    )
    cl = Client(id=uuid.uuid4(), name="Клиент", external_id="u-1")
    db.add_all([acc, cl])
    await db.flush()
    return acc, cl


def _диалог(acc, cl, n):
    return Conversation(
        id=uuid.uuid4(),
        account_id=acc.id,
        client_id=cl.id,
        external_chat_id=f"chat-{n}",
        status="new",
        last_message_at=T0 - timedelta(minutes=n),
    )


def _сообщение(conv, тело, когда, direction="in", sender="client"):
    return Message(
        id=uuid.uuid4(),
        conversation_id=conv.id,
        direction=direction,
        sender_type=sender,
        body=тело,
        attachments=[],
        delivery_status="delivered",
        created_at=когда,
    )


@pytest.mark.asyncio
async def test_берётся_именно_последнее_сообщение(db):
    acc, cl = await _мир(db)
    a, b = _диалог(acc, cl, 1), _диалог(acc, cl, 2)
    db.add_all([a, b])
    await db.flush()
    db.add_all(
        [
            _сообщение(a, "первое", T0 - timedelta(hours=3)),
            _сообщение(a, "ПОСЛЕДНЕЕ у А", T0 - timedelta(minutes=5)),
            _сообщение(b, "единственное у Б", T0 - timedelta(hours=1)),
        ]
    )
    await db.flush()

    related = await convs._load_related(db, [a, b])
    assert related["last_messages"][a.id].body == "ПОСЛЕДНЕЕ у А"
    assert related["last_messages"][b.id].body == "единственное у Б"


@pytest.mark.asyncio
async def test_заметка_не_становится_превью(db):
    """Внутренняя заметка видна только сотруднику и превью строки не задаёт."""
    acc, cl = await _мир(db)
    a = _диалог(acc, cl, 1)
    db.add(a)
    await db.flush()
    db.add_all(
        [
            _сообщение(a, "слово клиента", T0 - timedelta(minutes=30)),
            _сообщение(
                a,
                "заметка оператора",
                T0 - timedelta(minutes=1),
                direction="note",
                sender="operator",
            ),
        ]
    )
    await db.flush()

    related = await convs._load_related(db, [a])
    assert related["last_messages"][a.id].body == "слово клиента"


@pytest.mark.asyncio
async def test_диалог_без_сообщений_не_ломает_выборку(db):
    acc, cl = await _мир(db)
    пустой = _диалог(acc, cl, 9)
    db.add(пустой)
    await db.flush()

    related = await convs._load_related(db, [пустой])
    assert пустой.id not in related["last_messages"]
