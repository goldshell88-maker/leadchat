"""Старое эхо из сверки не имеет права выбросить ждущего клиента из очереди.

⚠ ЧЕМ ЭТО КОНЧАЛОСЬ. Канал теряет вебхуки на десять минут — ровно тот случай,
ради которого сверка и написана. 10:00 оператор отвечает клиенту из мобильного
приложения Авито; 10:03 клиент пишет «а когда приедете?». Сверка приходит через
пять минут и отдаёт историю СВЕЖИМИ ВПЕРЁД (так отвечает Авито, так же устроен
имитатор). Сначала применяется сообщение клиента — диалог встаёт в очередь, у
диспетчеров звенит. Следом применяется эхо 10:00 — и `leave_queue` снимает
`offered_at` и через `clear_awaiting` гасит `awaiting_since`.

После этого обращение невидимо ЦЕЛИКОМ: очередь требует `offered_at`, сторож
«клиент ждёт» требует ответственного, а его нет. Ни ошибки на экране, ни строки
в журнале — клиент просто перестаёт существовать для системы, пока не напишет
в третий раз.

Соседняя ветка (гашение `awaiting_since`) от этого защищена сравнением с
порядком с 17.08; ветке выхода из очереди, добавленной 19.08, защиты не
досталось — а гасит она в том числе то самое поле.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.integrations.avito.adapter import AvitoAdapter
from app.models import Client, Conversation
from app.services.inbound import apply_inbound_event
from tests.unit.conftest import drain_events

pytestmark = pytest.mark.anyio

AVITO_USER_ID = 222333444
CHAT_ID = "u2i-stale-echo"


def _сообщение(*, author_id: int, text: str, at: datetime, mid: str) -> dict:
    return {
        "id": f"env-{mid}",
        "version": "v3.0.0",
        "timestamp": int(at.timestamp()),
        "payload": {
            "type": "message",
            "value": {
                "id": mid,
                "chat_id": CHAT_ID,
                "author_id": author_id,
                "user_id": AVITO_USER_ID,
                "created": int(at.timestamp()),
                "content": {"text": text},
            },
        },
    }


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


@pytest.fixture
async def диалог(db_sessionmaker, account):
    """Диалог, стоящий в очереди: ничей, никем не принят, бот молчит."""
    client_id, conv_id = uuid.uuid4(), uuid.uuid4()
    async with db_sessionmaker() as s, s.begin():
        s.add(Client(id=client_id, channel="avito", external_id="777001", name="Иван"))
        s.add(
            Conversation(
                id=conv_id,
                channel="avito",
                external_chat_id=CHAT_ID,
                account_id=account.id,
                client_id=client_id,
                status="new",
                bot_active=False,
                bot_vars={},
                tags=[],
                unread_count=0,
                declined_by=[],
            )
        )
    return conv_id


async def _применить(db, redis, account, конверт: dict) -> bool:
    return await apply_inbound_event(db, redis, account, AvitoAdapter.parse_webhook(конверт))


async def _перечитать(db_sessionmaker, conv_id) -> Conversation:
    async with db_sessionmaker() as s:
        return (
            await s.execute(select(Conversation).where(Conversation.id == conv_id))
        ).scalar_one()


async def test_эхо_старше_вопроса_клиента_не_гасит_очередь(
    db, redis, account, диалог, db_sessionmaker
):
    """Порядок применения задаёт сверка, а не время события."""
    сейчас = datetime.now(UTC)
    ответ_оператора = сейчас - timedelta(minutes=5)
    вопрос_клиента = сейчас - timedelta(minutes=2)

    # Сверка отдаёт историю свежими вперёд: сначала вопрос клиента...
    await _применить(
        db,
        redis,
        account,
        _сообщение(author_id=777001, text="а когда приедете?", at=вопрос_клиента, mid="am-client"),
    )
    ждал = await _перечитать(db_sessionmaker, диалог)
    assert ждал.offered_at is not None, "клиент написал — диалог обязан встать в очередь"

    # ...и только потом более СТАРОЕ эхо ответа из приложения Авито.
    await _применить(
        db,
        redis,
        account,
        _сообщение(
            author_id=AVITO_USER_ID, text="Приедем завтра", at=ответ_оператора, mid="am-echo"
        ),
    )

    после = await _перечитать(db_sessionmaker, диалог)
    assert после.offered_at is not None, (
        "старое эхо выбросило ждущего клиента из очереди — обращение стало невидимым "
        "и для «Входящих», и для сторожей"
    )


async def test_свежее_эхо_очередь_гасит_как_и_раньше(db, redis, account, диалог, db_sessionmaker):
    """Правка не должна отменить решение владельца от 19.08 (находка L-008).

    Ответили снаружи ПОСЛЕ вопроса клиента — диалог больше не требует нашего
    хода и из очереди уходит.
    """
    сейчас = datetime.now(UTC)
    await _применить(
        db,
        redis,
        account,
        _сообщение(
            author_id=777001,
            text="здравствуйте",
            at=сейчас - timedelta(minutes=5),
            mid="am-client-2",
        ),
    )
    await _применить(
        db,
        redis,
        account,
        _сообщение(
            author_id=AVITO_USER_ID,
            text="Приедем завтра",
            at=сейчас - timedelta(minutes=1),
            mid="am-echo-2",
        ),
    )

    после = await _перечитать(db_sessionmaker, диалог)
    assert после.offered_at is None, "ответили снаружи — диалог не должен ждать разбора"


# --------------------------------------------- ответ из приложения Авито


async def test_ответ_из_приложения_переводит_диалог_из_новых(
    db, redis, account, диалог, db_sessionmaker
):
    """⚠ ЗАМЕР БОЯ 28.08: 522 ОТВЕЧЕННЫХ ДИАЛОГА ВИСЯТ В «НОВЫХ».

    `ensure_in_progress` — единственный вход для всех автоматических переходов
    «кто-то взялся за диалог»: его зовут первый ответ оператора из LeadChat,
    автораздача, принятие из очереди и принятие передачи. Путь «ответили в
    приложении Авито» не звал его никогда.

    А по замеру боя 23.08 отвечают сегодня ИМЕННО оттуда — то есть мимо
    перехода проходят практически все ответы. На бою это дало 522 диалога, где
    последнее видимое сообщение НАШЕ, в статусе «Новый», против 7 правильно
    переведённых: оператор открывает «Новые» и видит полтысячи обращений, на
    которые уже ответил, и отличить их там нечем.

    ЧТО ЛОМАЛИ: убрали вызов `ensure_in_progress` — тест краснеет на статусе.
    """
    сейчас = datetime.now(UTC)
    await _применить(
        db,
        redis,
        account,
        _сообщение(
            author_id=777001,
            text="телевизор не включается",
            at=сейчас - timedelta(minutes=4),
            mid="am-c1",
        ),
    )
    до = await _перечитать(db_sessionmaker, диалог)
    assert до.status == "new"

    # Оператор отвечает из мобильного приложения Авито — к нам это приходит эхом.
    await _применить(
        db,
        redis,
        account,
        _сообщение(
            author_id=AVITO_USER_ID,
            text="Здравствуйте, от 1000 рублей",
            at=сейчас - timedelta(minutes=1),
            mid="am-echo-status",
        ),
    )

    после = await _перечитать(db_sessionmaker, диалог)
    assert после.status == "in_progress", (
        "ответили из приложения Авито, а диалог остался «Новым» — он так и висит "
        "во вкладке «Новые» вместе с теми, на которые никто не отвечал"
    )
    # И ожидание снято: ответили же.
    assert после.awaiting_since is None


async def test_импорт_истории_старые_диалоги_не_будит(db, redis, account, диалог, db_sessionmaker):
    """Обратная сторона: годовалая переписка обязана остаться как есть.

    Условие `_fresh` здесь то же, что у соседних веток. Без него разбор истории
    поднял бы полтысячи закрытых диалогов в «В работе» разом.
    """
    старое = datetime.now(UTC) - timedelta(days=200)
    await _применить(
        db,
        redis,
        account,
        _сообщение(author_id=AVITO_USER_ID, text="старый ответ", at=старое, mid="am-old"),
    )
    после = await _перечитать(db_sessionmaker, диалог)
    assert после.status == "new", "импорт истории разбудил старый диалог"


async def test_кадр_ответа_гасит_шкалу_ожидания(db, redis, account, диалог, db_sessionmaker):
    """⚠ ШКАЛА «ЖДЁТ 19 МИН» РОСЛА НА ДИАЛОГЕ, ГДЕ В ПРЕВЬЮ «ВЫ: …».

    Отметку «клиент ждёт» ответ снимает — на сервере. Но в патч строки списка
    она не попадала, а строка на клиенте обновляется ИМЕННО ЭТИМ ПАТЧОМ:
    превью и время менялись, а якорь ожидания оставался прежним — тем, что
    лежал там ДО ответа. Часы тикают от якоря, и оранжевая шкала продолжала
    расти на отвеченном диалоге. Гасло это только полной перезагрузкой списка,
    а список сам не перезапрашивается: `staleTime` тридцать секунд и
    `refetchOnWindowFocus` выключен.

    ЧТО ЛОМАЛИ: убрали `waiting_since` из патча — тест краснеет.
    """
    сейчас = datetime.now(UTC)
    await _применить(
        db,
        redis,
        account,
        _сообщение(
            author_id=777001, text="не включается", at=сейчас - timedelta(minutes=4), mid="am-c9"
        ),
    )

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await _применить(
        db,
        redis,
        account,
        _сообщение(
            author_id=AVITO_USER_ID,
            text="Здравствуйте, от 1000 рублей",
            at=сейчас - timedelta(minutes=1),
            mid="am-echo-wait",
        ),
    )

    кадры = await drain_events(pubsub)
    ответ = next(e for e in кадры if e["type"] == "message:new")
    патч = ответ["data"]["conversation_patch"]
    assert "waiting_since" in патч, (
        "патч строки не несёт якорь ожидания — шкала продолжит расти на отвеченном"
    )
    assert патч["waiting_since"] is None, "ответили, а строка всё ещё «клиент ждёт»"
    assert патч["status"] == "in_progress"
