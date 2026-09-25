"""apply_inbound_event + webhook gateway (07 §1.1; owner decisions 1–3).

Covers: echo drop, duplicate idempotency, closed→new reopen with the audit
pair, WS envelope shape (01 §11.3), phone capture (first fill only),
backfill creating closed conversations, and the gateway contract
(403 secret / 413 body / XADD + instant 200).

The ``db`` session is used ONLY by apply_inbound_event (it opens its own
``db.begin()``); assertions read through fresh sessions to keep transaction
boundaries clean.
"""

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import AuditLog, Client, Conversation, Message, Notification
from app.services import app_settings, inbox
from app.services.inbound import apply_inbound_event, extract_phone
from tests.unit.conftest import drain_events

try:  # the real adapter event when the OAuth zone is present
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

AVITO_USER_ID = 111222333
T0 = datetime(2026, 8, 4, 10, 0, 0, tzinfo=UTC)


def make_event(**kw) -> InboundEvent:
    defaults = {
        "external_chat_id": "chat-1",
        "external_message_id": "am-1",
        "author_id": 999001,
        "account_user_id": AVITO_USER_ID,
        "text": "Здравствуйте! Экран разбит, почём?",
        "created_at": T0,
        "client_name": "Иван Петров",
        "item_title": "Ремонт iPhone 13",
        "item_url": "https://avito.ru/item/1",
        "item_price": "от 1500 ₽",
    }
    defaults.update(kw)
    return InboundEvent(**defaults)


def _utc(value: datetime) -> datetime:
    """SQLite отдаёт время без зоны — сравниваем по UTC, а не по наличию tzinfo."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


@pytest.fixture
def read(db_sessionmaker):
    """Fresh-session reader: read(select(...)) -> list of scalars."""

    async def _read(stmt):
        async with db_sessionmaker() as session:
            return list((await session.execute(stmt)).scalars())

    return _read


async def test_external_outgoing_is_stored(db, redis, account, read):
    """17.08 (параллельная работа с Jivo) — разворот решения №1.

    Исходящее с аккаунта, которого нет в базе (ответ оператора из Jivo или
    приложения Авито), ВСТАВЛЯЕТСЯ как direction='out': без него переписка
    однобокая, а статус врёт «ждёт ответа». Диалог, если его не было,
    создаётся закрытым — очередь и боты историю не видят.
    """
    inserted = await apply_inbound_event(db, redis, account, make_event(author_id=AVITO_USER_ID))
    assert inserted is True
    msgs = await read(select(Message))
    assert len(msgs) == 1 and msgs[0].direction == "out"
    convs = await read(select(Conversation))
    assert len(convs) == 1 and convs[0].status == "closed"
    # клиент — заглушка «сам чат», а НЕ карточка аккаунта: иначе все чаты,
    # начинающиеся с нашего сообщения, слипались бы в «клиента-Тимофея»
    clients = await read(select(Client))
    assert [c.external_id for c in clients] == ["chat:chat-1"]


async def test_own_echo_is_deduped(db, redis, account, read):
    """Своё эхо (external_message_id уже записан доставкой LeadChat) — дубль."""
    first = await apply_inbound_event(db, redis, account, make_event(author_id=AVITO_USER_ID))
    assert first is True
    again = await apply_inbound_event(db, redis, account, make_event(author_id=AVITO_USER_ID))
    assert again is False
    assert len(await read(select(Message))) == 1


async def test_duplicate_is_idempotent(db, redis, account, read):
    """INT-2 в миниатюре: один и тот же external_message_id — одна строка,
    одно событие, unread_count не задваивается."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    assert await apply_inbound_event(db, redis, account, make_event()) is True
    assert await apply_inbound_event(db, redis, account, make_event()) is False

    assert len(await read(select(Message))) == 1
    (conv,) = await read(select(Conversation))
    assert conv.unread_count == 1
    # Два кадра на ПЕРВОЕ сообщение (message:new + inbox:new — диалог встал в
    # очередь) и ни одного на дубль: ретрай вебхука не должен ни звенеть
    # операторам, ни вставлять вторую строку в очередь.
    types = [e["type"] for e in await drain_events(pubsub)]
    assert types == ["message:new", "inbox:new"]


async def test_event_envelope_shape(db, redis, account, read):
    """Конверт 01 §11.3: {type, ts, data{conversation_id, message, conversation_patch}}."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await apply_inbound_event(db, redis, account, make_event())

    evt, _queue_frame = await drain_events(pubsub)
    assert evt["type"] == "message:new"
    assert evt["ts"].endswith("Z")
    data = evt["data"]
    (conv,) = await read(select(Conversation))
    assert data["conversation_id"] == str(conv.id)
    message = data["message"]
    assert message["direction"] == "in"
    assert message["sender_type"] == "client"
    assert message["body"] == "Здравствуйте! Экран разбит, почём?"
    assert message["delivery_status"] == "delivered"
    assert message["client_message_id"] is None
    assert data["conversation_patch"] == {
        # ⚠ ХОЗЯИН ДИАЛОГА ДОБАВЛЕН 02.09, и сравнение остаётся ТОЧНЫМ.
        #
        # Поле нужно звуку: он звенит только по СВОИМ диалогам, а из кэша
        # хозяин известен не всегда. Пропади поле — экран замолчит и по своим
        # тоже, то есть вернётся беда «без звука я не реагирую на сообщения».
        #
        # Сравнение словарей целиком, а не по ключам, здесь намеренное: лишнее
        # поле в кадре так же заметно, как и пропавшее. Оба уезжают тринадцати
        # вкладкам на каждое входящее.
        "assignee_id": None,
        "unread_delta": 1,
        "last_message_at": "2026-08-04T10:00:00.000Z",
        "status": "new",
        # Якорь ожидания едет в этом же кадре: клиент написал — оранжевая шкала
        # обязана зажечься сразу, а не после полной перезагрузки списка. Здесь
        # это первое сообщение диалога, поэтому якорь равен его времени.
        "waiting_since": "2026-08-04T10:00:00.000Z",
    }


async def test_closed_conversation_reopens(
    db, db_sessionmaker, redis, account, users_by_role, read
):
    """INT-4: клиент вернулся — closed -> new, assignee снят, ДВА audit-события."""
    await apply_inbound_event(db, redis, account, make_event())
    async with db_sessionmaker() as session, session.begin():
        conv = (await session.execute(select(Conversation))).scalar_one()
        conv.status = "closed"
        conv.assignee_id = users_by_role["manager"].id

    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(external_message_id="am-2", created_at=T0 + timedelta(minutes=5)),
    )
    (conv,) = await read(select(Conversation))
    assert conv.status == "new"
    assert conv.assignee_id is None
    actions = await read(select(AuditLog.action))
    assert "conversation.status_changed" in actions
    assert "conversation.reopened" in actions


class TestTheClosingOperatorIsTold:
    """«Клиент вернулся в закрытый диалог» — уведомление, которого не было (SCEN-17).

    Вид `conversation.reopened` числился в каталоге центра уведомлений, имел
    иконку и подпись во фронте — и не создавался НИ ОДНОЙ строкой боевого
    кода. Здесь, в единственном месте, где клиент и правда возвращается,
    писалась только запись журнала аудита: её читает разбор постфактум, а
    человека она не зовёт никогда.

    Диалог при этом уходит в общую очередь, и это сказано всем. Не сказано
    ровно одно и ровно одному: тому, кто этот разговор вёл и закрыл.
    """

    async def test_the_previous_owner_gets_a_personal_notification(
        self, db, db_sessionmaker, redis, account, users_by_role, read
    ):
        """Главная проверка: строка адресована закрывшему, а не рассылке."""
        await apply_inbound_event(db, redis, account, make_event())
        async with db_sessionmaker() as session, session.begin():
            conv = (await session.execute(select(Conversation))).scalar_one()
            conv.status = "closed"
            conv.assignee_id = users_by_role["manager"].id

        await apply_inbound_event(
            db,
            redis,
            account,
            make_event(external_message_id="am-2", created_at=T0 + timedelta(minutes=5)),
        )

        rows = await read(select(Notification).where(Notification.kind == "conversation.reopened"))
        assert rows, (
            "клиент вернулся в закрытый диалог, а уведомления нет — "
            "вид объявлен в каталоге и не создаётся нигде"
        )
        (row,) = rows
        assert row.recipient_id == users_by_role["manager"].id
        assert row.audience is None, "это личная новость, а не рассылка администраторам"
        (conv,) = await read(select(Conversation))
        assert row.entity_type == "conversation"
        assert row.entity_id == str(conv.id)
        assert "Иван Петров" in (row.body or ""), "в тексте должно быть имя вернувшегося клиента"

    async def test_the_frame_reaches_the_browser(
        self, db, db_sessionmaker, redis, account, users_by_role
    ):
        """Кадр уходит В СОКЕТ — иначе строка ждёт перезагрузки страницы.

        Порядок важен: сперва сообщение и строка очереди (их видят все),
        последним — личное уведомление закрывшему.
        """
        await apply_inbound_event(db, redis, account, make_event())
        async with db_sessionmaker() as session, session.begin():
            conv = (await session.execute(select(Conversation))).scalar_one()
            conv.status = "closed"
            conv.assignee_id = users_by_role["manager"].id

        pubsub = redis.pubsub()
        await pubsub.subscribe("events")
        await apply_inbound_event(
            db,
            redis,
            account,
            make_event(external_message_id="am-2", created_at=T0 + timedelta(minutes=5)),
        )

        events = await drain_events(pubsub)
        notices = [e for e in events if e["type"] == "notify"]
        assert notices, [e["type"] for e in events]
        notice = notices[0]
        assert notice["data"]["kind"] == "conversation.reopened"
        # Адресный кадр: хаб раздаёт его одному человеку, а не всей смене.
        assert notice.get("meta", {}).get("only_user") == str(users_by_role["manager"].id)

    async def test_a_dialog_without_an_owner_notifies_nobody(
        self, db, db_sessionmaker, redis, account, read
    ):
        """Закрыт из очереди, хозяина не было — адресата нет, и молчание верно.

        Подменять его рассылкой администраторам нельзя: это чужая работа и
        чистый шум. Проверка заодно запирает падение — `notify` без адреса
        честно бросает ValueError, и такой вызов уронил бы приём сообщения.
        """
        await apply_inbound_event(db, redis, account, make_event())
        async with db_sessionmaker() as session, session.begin():
            conv = (await session.execute(select(Conversation))).scalar_one()
            conv.status = "closed"
            conv.assignee_id = None

        assert await apply_inbound_event(
            db,
            redis,
            account,
            make_event(external_message_id="am-2", created_at=T0 + timedelta(minutes=5)),
        )

        (conv,) = await read(select(Conversation))
        assert conv.status == "new", "сообщение принято, диалог поднят"
        assert await read(select(Notification)) == []

    async def test_history_import_wakes_nobody(self, db, redis, account, read):
        """Сверка и импорт истории уведомлений не порождают.

        Иначе первая же догрузка старой переписки высыпала бы оператору
        десятки «клиент вернулся» про разговоры годичной давности.
        """
        await apply_inbound_event(db, redis, account, make_event(), publish=False, backfill=True)
        await apply_inbound_event(
            db,
            redis,
            account,
            make_event(external_message_id="am-2", created_at=T0 + timedelta(minutes=5)),
            publish=False,
            backfill=True,
        )

        assert await read(select(Notification)) == []


async def test_backfill_creates_closed_and_never_reopens(db, redis, account, read):
    """Решение №3: история — status='closed'; backfill не переоткрывает."""
    assert await apply_inbound_event(db, redis, account, make_event(), publish=False, backfill=True)
    (conv,) = await read(select(Conversation))
    assert conv.status == "closed"

    assert await apply_inbound_event(
        db,
        redis,
        account,
        make_event(external_message_id="am-2", created_at=T0 + timedelta(minutes=1)),
        publish=False,
        backfill=True,
    )
    (conv,) = await read(select(Conversation))
    assert conv.status == "closed"  # не «Новые» — история остаётся архивом
    assert "conversation.reopened" not in await read(select(AuditLog.action))


# --- очередь «Входящие» (план 7.1) -------------------------------------------
#
# Здесь проходит ЕДИНСТВЕННЫЙ путь, которым диалог из Авито попадает в систему,
# — значит и единственный, которым он попадает в очередь. Если эти четыре теста
# зелёные, вкладка «Входящие» наполняется в бою; если их убрать, очередь молча
# опустеет после первого же деплоя, и заметит это оператор, а не CI.


async def test_new_dialog_enters_the_queue_and_rings(db, redis, account, read):
    """Новый диалог: `offered_at` в базе + кадр `inbox:new` со строкой очереди."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await apply_inbound_event(db, redis, account, make_event())

    (conv,) = await read(select(Conversation))
    assert conv.offered_at is not None, "новый диалог не встал в очередь"
    assert conv.claimed_by_id is None and conv.assignee_id is None
    assert list(conv.declined_by or []) == []
    assert inbox.is_waiting(conv)

    frame = next(e for e in await drain_events(pubsub) if e["type"] == "inbox:new")
    row = frame["data"]["conversation"]
    assert frame["data"]["conversation_id"] == str(conv.id)
    # Строка очереди — целиком: подключившемуся оператору вставлять больше нечего.
    assert row["id"] == str(conv.id)
    assert row["client"]["name"] == "Иван Петров"
    assert row["item"]["title"] == "Ремонт iPhone 13"
    assert row["last_message"]["body"] == "Здравствуйте! Экран разбит, почём?"
    assert row["in_inbox"] is True
    assert row["waiting_seconds"] is not None
    assert row["declined_count"] == 0 and row["escalated"] is False


async def test_second_message_does_not_queue_the_dialog_twice(db, redis, account, read):
    """Второе сообщение того же клиента — не второй вход в очередь.

    Иначе каждое сообщение болтливого клиента переставляло бы ему время
    ожидания (он бы навсегда уехал в хвост очереди) и звенело бы всей смене.
    """
    await apply_inbound_event(db, redis, account, make_event())
    (first,) = await read(select(Conversation))
    offered = first.offered_at

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(external_message_id="am-2", created_at=T0 + timedelta(minutes=3)),
    )
    (conv,) = await read(select(Conversation))
    assert conv.offered_at == offered
    assert [e["type"] for e in await drain_events(pubsub)] == ["message:new"]


async def test_backfill_never_touches_the_queue(db, redis, account, read):
    """Решение владельца №3: история в очередь не идёт — ни строкой, ни кадром.

    Это про день включения 7.1: бэкофилл тянет из Авито тысячи старых чатов, и
    если каждый встанет в очередь, живой клиент в ней просто потеряется.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await apply_inbound_event(db, redis, account, make_event(), backfill=True)

    (conv,) = await read(select(Conversation))
    assert conv.offered_at is None
    assert not inbox.is_waiting(conv)
    # `message:new` бэкофилл в этом вызове публикует (боевой вызов зовёт его с
    # publish=False), а кадра очереди нет — и это ровно то, что проверяется.
    assert [e["type"] for e in await drain_events(pubsub)] == ["message:new"]


async def test_returning_client_starts_a_new_wait_and_forgets_old_declines(
    db, db_sessionmaker, redis, account, users_by_role, read
):
    """Клиент вернулся — ожидание НОВОЕ, и вчерашний отказ его больше не прячет.

    Иначе диалог вернувшегося клиента был бы невидим ровно для тех, кто уже
    один раз решил им не заниматься.
    """
    await apply_inbound_event(db, redis, account, make_event())
    manager = users_by_role["manager"]
    async with db_sessionmaker() as session, session.begin():
        conv = (await session.execute(select(Conversation))).scalar_one()
        conv.status = "closed"
        conv.assignee_id = manager.id
        conv.claimed_by_id = manager.id
        conv.claimed_at = T0
        conv.declined_by = [str(manager.id)]
        conv.escalated_at = T0
        conv.offered_at = T0 - timedelta(days=30)

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    back = T0 + timedelta(days=30)
    await apply_inbound_event(
        db, redis, account, make_event(external_message_id="am-2", created_at=back)
    )

    (conv,) = await read(select(Conversation))
    assert conv.status == "new"
    assert _utc(conv.offered_at) == back, "ожидание считается с возврата, а не с прошлого раза"
    assert conv.claimed_by_id is None and conv.claimed_at is None
    assert list(conv.declined_by or []) == [], "вчерашний отказ прячет сегодняшнего клиента"
    assert conv.escalated_at is None
    assert inbox.is_waiting(conv)

    frame = next(e for e in await drain_events(pubsub) if e["type"] == "inbox:new")
    assert frame["data"]["conversation"]["waiting_seconds"] == 0

    async with db_sessionmaker() as session:
        items, total = await inbox.list_inbox(session, manager, now=back)
    assert total == 1 and items[0]["id"] == str(conv.id)


async def test_phone_captured_once(db, redis, account, read):
    """INT-7: телефон -> clients.phone + audit; подтверждённый не перезатирается.

    ЗАПИСЬ В КАРТОЧКУ ТЕПЕРЬ ВКЛЮЧАЕТСЯ ЯВНО, и это правка 10 от 12 августа, а
    не поломка теста. Раньше номер, найденный в тексте, молча уезжал в карточку;
    владелец это запретил: «тихая запись чужого номера в карточку хуже, чем
    несделанная работа», — потому что разбор ошибается на цифрах, телефоном не
    являющихся, а номер оттуда набирают и диктуют мастеру вслух. По умолчанию
    распознанное предлагается оператору (это проверяет
    `tests/unit/test_phone_from_text.py`); здесь охраняется ровно прежнее
    правило INT-7 — первый номер не перезаписывается вторым, — и для этого
    запись нужно включить.
    """
    from app.services import app_settings

    await app_settings.set_many(db, {app_settings.PHONE_DETECT_AUTOFILL: True}, user_id=None)
    await db.commit()

    await apply_inbound_event(db, redis, account, make_event(text="звоните 8 900 111 22 51"))
    (client_row,) = await read(select(Client))
    assert client_row.phone == "+79001112251"
    assert (await read(select(AuditLog.action))).count("client.phone_captured") == 1

    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(
            external_message_id="am-2",
            created_at=T0 + timedelta(minutes=2),
            text="лучше на +7 900 111 22 52",
        ),
    )
    (client_row,) = await read(select(Client))
    assert client_row.phone == "+79001112251"  # первый номер сохранён
    assert (await read(select(AuditLog.action))).count("client.phone_captured") == 1


# --- ленивое имя клиента (хвост спринта 2, пункт «а») ------------------------


async def enrich_jobs(redis) -> list[str]:
    """Ключи поставленных задач ``enrich_client`` (дедуп по ``_job_id``)."""
    return sorted(k for k in await redis.keys("arq:job:enrich:*"))


async def test_new_client_without_name_enqueues_enrich(db, redis, account, read):
    """Вебхук v3 имени не несёт: после commit'а ставится задача обогащения."""
    await apply_inbound_event(db, redis, account, make_event(client_name=None))

    (conv,) = await read(select(Conversation))
    (client_row,) = await read(select(Client))
    assert client_row.name is None
    assert await enrich_jobs(redis) == [f"arq:job:enrich:{conv.id}"]


async def test_enrich_is_asked_for_the_profile_even_when_the_name_came(db, redis, account, read):
    """⚠ ЭТОТ ТЕСТ ПОМЕНЯЛ ЗНАК 02.09, И ЭТО НАМЕРЕННО.

    Он назывался `test_event_with_client_name_does_not_enqueue_enrich` и
    утверждал: имя пришло в событии — тянуть нечего. Сегодня тянуть есть что:
    у постановки задачи появился третий повод — «про ссылку на профиль клиента
    ещё не спрашивали» (просьба владельца 02.09).

    Без этого повода правка не работала бы НИ У ОДНОГО клиента, заведённого
    раньше: у них известны и имя, и объявление, значит прежние два повода
    молчат, задача не встаёт, и кнопка не появляется. «Написано, но не
    подключено» в чистом виде.

    Прежнее правило при этом не потеряно, а проверяется соседним тестом ниже:
    повод гаснет навсегда после первого вопроса, поэтому цена — один поход на
    клиента, а не на сообщение.
    """
    await apply_inbound_event(db, redis, account, make_event())
    (conv,) = await read(select(Conversation))
    assert await enrich_jobs(redis) == [f"arq:job:enrich:{conv.id}"]


async def test_enrich_is_not_asked_again_once_the_profile_was_checked(
    db, redis, account, read, db_sessionmaker
):
    """Спросили однажды — повода больше нет. Иначе это налог на каждое сообщение."""
    from app.models import Client as C

    await apply_inbound_event(db, redis, account, make_event())
    async with db_sessionmaker() as s:
        (client_row,) = (await s.execute(select(C))).scalars().all()
        client_row.profile_checked_at = datetime(2026, 9, 2, tzinfo=UTC)
        await s.commit()
    # Чистим очередь: нас интересует, встанет ли задача ЗАНОВО.
    for job in await enrich_jobs(redis):
        await redis.delete(job)
    await redis.delete("arq:queue")

    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(external_message_id="am-2", created_at=T0 + timedelta(minutes=1)),
    )
    assert await enrich_jobs(redis) == [], "про профиль уже спрашивали — повода нет"


async def test_enrich_enqueued_once_per_client(db, redis, account, read):
    """Второе сообщение того же клиента задачу не повторяет: строка уже наша,
    а дедуп по ``_job_id`` страхует от гонки двух вебхуков."""
    await apply_inbound_event(db, redis, account, make_event(client_name=None))
    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(
            client_name=None,
            external_message_id="am-2",
            created_at=T0 + timedelta(minutes=1),
        ),
    )
    (conv,) = await read(select(Conversation))
    assert await enrich_jobs(redis) == [f"arq:job:enrich:{conv.id}"]


@pytest.mark.parametrize(
    "text,expected",
    [
        ("звоните 8 900 111 22 51", "+79001112251"),
        ("+7 (900) 111-22-52", "+79001112252"),
        ("мой номер 9001112251", "+79001112251"),
        ("завтра в 10:30, цена 1500", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_phone(text, expected):
    assert extract_phone(text) == expected


# --- gateway (01 §10) --------------------------------------------------------


async def test_gateway_accepts_and_enqueues(client, redis, account):
    payload = {"payload": {"type": "message", "value": {"id": "am-1"}}}
    r = await client.post(f"/api/hooks/avito/{account.id}?secret=whsec-test", json=payload)
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    entries = await redis.xrange("webhooks:avito", "-", "+")
    assert len(entries) == 1
    _, fields = entries[0]
    assert fields["account_id"] == str(account.id)
    assert json.loads(fields["payload"]) == payload
    assert await redis.get(f"webhook_last:{account.id}") is not None


async def test_gateway_rejects_bad_secret(client, redis, account):
    r = await client.post(f"/api/hooks/avito/{account.id}?secret=WRONG", json={})
    assert r.status_code == 403
    r2 = await client.post(f"/api/hooks/avito/{account.id}", json={})  # секрета нет
    assert r2.status_code == 403
    r3 = await client.post(f"/api/hooks/avito/{uuid.uuid4()}?secret=whsec-test", json={})
    assert r3.status_code == 403  # неизвестный аккаунт неотличим от плохого секрета
    assert await redis.xlen("webhooks:avito") == 0


async def test_gateway_rejects_oversized_body(client, redis, account):
    body = b'{"x": "' + b"a" * 1_100_000 + b'"}'
    r = await client.post(
        f"/api/hooks/avito/{account.id}?secret=whsec-test",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 413
    assert await redis.xlen("webhooks:avito") == 0


async def test_distribution_takes_the_dialog_out_of_the_queue(
    db, redis, account, read, make_user, monkeypatch
):
    """Сквозная проверка крючка: включённое распределение назначает диалог
    ПРЯМО НА ПРИЁМЕ, и в очередь он не встаёт (docs/18).

    Отдельный тест нужен именно на крючок. Выбор получателя проверяется в
    tests/unit/test_distribution.py, но крючок может молча не сработать —
    и тогда все те проверки останутся зелёными, а распределения не будет.
    """
    from app.services import app_settings, inbox
    from app.ws.presence import _status_key

    # Настройки пишем в базу, а не подменяем заглушкой: путь до раздачи должен
    # быть тем же, что в проде, включая чтение из `app_settings`.
    await app_settings.set_many(
        db,
        {
            app_settings.DISTRIBUTION_ENABLED: True,
            app_settings.DISTRIBUTION_MAX_ACTIVE: 5,
        },
        user_id=None,
    )
    await db.commit()

    operator = await make_user("dispatcher@leadchat.test", role="manager", full_name="Диспетчер")
    await redis.set(_status_key(operator.id), "online")

    inserted = await apply_inbound_event(
        db, redis, account, make_event(external_chat_id="dist-chat", external_message_id="am-dist")
    )
    assert inserted is True

    conv = (await read(select(Conversation).where(Conversation.external_chat_id == "dist-chat")))[0]
    assert conv.assignee_id == operator.id, "диалог должен быть назначен на приёме"
    assert conv.offered_at is None, "назначенный диалог в очереди не стоит"
    assert inbox.is_waiting(conv) is False
    assert conv.status == "in_progress"


async def test_without_distribution_the_dialog_waits_in_the_queue_as_before(
    db, redis, account, read, make_user
):
    """Выключенное распределение НЕ меняет ничего — прежнее поведение целиком.

    Это свойство важнее самого распределения: если оно поведёт себя странно,
    администратор возвращает прежний порядок одним переключателем.
    """
    from app.services import inbox
    from app.ws.presence import _status_key

    operator = await make_user("idle@leadchat.test", role="manager")
    await redis.set(_status_key(operator.id), "online")

    await apply_inbound_event(
        db,
        redis,
        account,
        make_event(external_chat_id="queue-chat", external_message_id="am-queue"),
    )

    conv = (await read(select(Conversation).where(Conversation.external_chat_id == "queue-chat")))[
        0
    ]
    assert conv.assignee_id is None
    assert conv.offered_at is not None
    assert inbox.is_waiting(conv) is True


# ---------------------------------------------------------------------------
# Кадр «карточка клиента изменилась» уходит ПОСЛЕ записи в базу (02.09)
# ---------------------------------------------------------------------------


async def test_к_моменту_кадра_о_клиенте_телефон_уже_в_базе(db, db_sessionmaker, redis, account):
    """⚠ ЖАЛОБА ВЛАДЕЛЬЦА 02.09: «клиент указывает номер — сначала пишется „Ещё
    номера этого человека“, а после обновления страницы номер прописывается
    нормально».

    ЧТО БЫЛО. Разбор телефона публиковал кадр прямо по месту — то есть ВНУТРИ
    транзакции, до commit'а. Экран получал кадр, честно перезапрашивал карточку
    и читал из базы ЕЩЁ СТАРУЮ строку, без телефона. Второго повода перезапросить
    не было, и номер появлялся только после F5. Строка «Ещё номера этого
    человека» при этом появлялась — она собирается из кандидатов, и гонка там
    складывалась иначе. Отсюда и вид беды: половина карточки знает про номер,
    половина нет.

    ⚠ ПРАВИЛО ЗАПИСАНО В ПРОЕКТЕ ДАВНО (08 §8.1): строки собираются внутри
    транзакции, кадры публикуются строго после commit'а. Соседний кадр очереди
    так и сделан. Кадр клиента был единственным исключением.

    ⚠ ПОЧЕМУ НЕ ЛОВИЛОСЬ РАНЬШЕ. Прежний сторож проверял ФРОНТ: что кадр
    сбрасывает нужный ключ кэша. Он прав и остаётся зелёным. Но кадр, ушедший
    слишком рано, сбрасывает кэш ровно так же — и перезапрашивает старые данные.
    Проверять надо было не «сбросили ли», а «есть ли что читать к этому моменту».
    """
    # ⚠ АВТОЗАПИСЬ ВКЛЮЧАЕМ ЯВНО. В умолчаниях она выключена (решение владельца:
    # тихая запись чужого номера хуже несделанной работы), а в БОЮ включена.
    # Проверять надо боевое поведение — иначе телефон осел бы в кандидатах, и
    # проверка «есть ли что читать к моменту кадра» ничего бы не значила.
    async with db_sessionmaker() as настройки:
        await app_settings.set_many(
            настройки, {app_settings.PHONE_DETECT_AUTOFILL: True}, user_id=None
        )
        await настройки.commit()

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await apply_inbound_event(db, redis, account, make_event(text="Мой номер 8 900 111 22 40"))

    кадры = [e for e in await drain_events(pubsub) if e.get("type") == "client:updated"]
    assert кадры, "кадр о карточке клиента не ушёл — номер появится только после F5"

    # ⚠ ДРУГАЯ СЕССИЯ: она видит только закоммиченное. Уйди кадр до commit'а —
    # экран в этот момент прочитал бы ровно это, то есть пустой телефон.
    async with db_sessionmaker() as s:
        client = (await s.execute(select(Client))).scalars().first()
        assert client is not None
        assert client.phone == "+79001112240", (
            "кадр ушёл раньше записи: экран перезапросит карточку и получит старые данные"
        )


async def test_кадр_о_клиенте_несёт_диалог(db, db_sessionmaker, redis, account):
    """По этому id фронт сбрасывает ДЕТАЛЬ диалога, где и живёт телефон."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await apply_inbound_event(db, redis, account, make_event(text="звоните 89001112240"))

    кадр = next(e for e in await drain_events(pubsub) if e.get("type") == "client:updated")
    async with db_sessionmaker() as s:
        conv = (await s.execute(select(Conversation))).scalars().first()
        assert conv is not None
        assert кадр["data"]["conversation_id"] == str(conv.id), (
            "без верного id диалога фронт сбросит не тот кэш — телефон снова не появится"
        )


async def test_без_номера_кадра_о_клиенте_нет(db, redis, account):
    """Лишний кадр — тоже беда: тринадцать вкладок перезапросят карточку зря."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await apply_inbound_event(db, redis, account, make_event(text="Здравствуйте, сколько стоит?"))

    assert not [e for e in await drain_events(pubsub) if e.get("type") == "client:updated"], (
        "кадр ушёл на сообщении без номера — лишние запросы у всей смены"
    )


async def test_кадр_сообщения_несёт_хозяина_диалога(db, redis, account, users_by_role):
    """⚠ БЕЗ ЭТОГО ПОЛЯ ЗВУК ЗАМОЛЧИТ И ПО СВОИМ ДИАЛОГАМ.

    Обратная связь диспетчера 02.09: «звук выключил… 90% просто так оповещает,
    когда даже сообщений нет… крч не работопригодно». Замер подтвердил: за день
    521 входящее и 81 постановка в очередь — около шестисот сигналов на
    человека, а звенело на ЛЮБОЕ входящее в ЛЮБОМ диалоге.

    Теперь экран звенит только по СВОИМ. Чтобы отличить своё от чужого, ему
    нужен хозяин диалога В КАДРЕ: из кэша тот известен не всегда (строки может
    не быть вовсе), и неизвестность экран трактует как «чужое» — молчит.

    Значит пропажа этого поля означает не шум, а ТИШИНУ по своим диалогам, то
    есть ровно ту беду, от которой человек и страдал: «без звука я не реагирую
    на сообщения».
    """
    manager = users_by_role["manager"]
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await apply_inbound_event(db, redis, account, make_event(text="Здравствуйте"))

    кадр = next(e for e in await drain_events(pubsub) if e.get("type") == "message:new")
    заплатка = кадр["data"]["conversation_patch"]
    assert "assignee_id" in заплатка, (
        "кадр не несёт хозяина диалога — экран замолчит и по своим тоже"
    )
    # У нового диалога хозяина нет — и это честный `null`, а не отсутствие поля.
    assert заплатка["assignee_id"] is None
    assert manager is not None
