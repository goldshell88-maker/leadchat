"""Историческая дверь разбирает адрес и телефон (N29, 19.09).

ЧТО СТЕРЕЖЁТ. `_backfill_chat` → `_insert_history_message` кладёт реплики клиента в
ленту мимо `apply_inbound_event`: ни строки адреса, ни номера. Замер 27 773 диалогов
за 30 дней: 47 диалогов/мес с настоящим адресом и без единой строки — по гипотезе
разбора их реплики пришли догрузкой истории. Теперь после вставки ставится задача
`catchup_conversation_cards`, и она перечитывает хвост диалога тем же путём, что
живой приём, с `now` = время реплики.

Стенд истории: Авито подменён записанными ответами (`svc._avito_call`), сеть не
ходит; заказ истории через новый пул ARQ (`enqueue_conversation_history`) заглушен;
кадры в сокет собираются в список. Тексты вымышленные, номера +7 900, геоточка —
«Калужская область, Калуга, улица Ленина, 5» (как в test_address_stend_1409).
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from app.integrations.avito.adapter import InboundEvent
from app.models import Client, ClientAddressCandidate, ClientPhoneCandidate, Conversation, Message
from app.services import app_settings
from app.services import avito_accounts as svc
from app.services import inbound as inbound_svc
from app.services.inbound import apply_inbound_event

pytestmark = pytest.mark.anyio

ACCOUNT_UID = 771900
CLIENT_UID = 999901
NOW = datetime.now(UTC).replace(microsecond=0)
ГЕОТОЧКА_АДРЕС = "Калужская область, Калуга, улица Ленина, 5"


def _ctx(db_sessionmaker, redis) -> dict[str, Any]:
    return {"db_session_factory": db_sessionmaker, "redis": redis}


def _raw_chat(chat_id: str, *, unread: bool = False) -> dict[str, Any]:
    return {
        "id": chat_id,
        "users": [{"id": ACCOUNT_UID, "name": "Мы"}, {"id": CLIENT_UID, "name": "Клиент"}],
        "context": {"type": "item", "value": {"title": "Ремонт стиральных машин"}},
        "has_unread": unread,
        "updated": int(NOW.timestamp()),
    }


def _raw_msg(
    msg_id: str, text: str, *, created: datetime, author_id: int = CLIENT_UID
) -> dict[str, Any]:
    return {
        "id": msg_id,
        "author_id": author_id,
        "created": int(created.timestamp()),
        "content": {"text": text},
    }


def _raw_geo(msg_id: str, *, created: datetime, author_id: int = CLIENT_UID) -> dict[str, Any]:
    """Сообщение-геоточка истории, как отдаёт GET …/messages/ (adapter `_extract_attachments`):
    `type=location`, координаты и адрес в `content.location`."""
    return {
        "id": msg_id,
        "author_id": author_id,
        "created": int(created.timestamp()),
        "type": "location",
        "content": {
            "location": {"kind": "street", "lat": 54.5, "lon": 36.25, "text": ГЕОТОЧКА_АДРЕС}
        },
    }


def _геоточка_вложение(*, координаты: bool = True, адрес: str = ГЕОТОЧКА_АДРЕС) -> dict[str, Any]:
    """Вложение геоточки в том виде, в каком его хранит лента (см. test_address_stend_1409:1392)."""
    item: dict[str, Any] = {
        "media_id": "avito_location_n29",
        "kind": "file",
        "name": "Геопозиция",
        "size": None,
        "avito_type": "location",
        "address": адрес,
    }
    if координаты:
        item["lat"], item["lon"] = 54.5, 36.25
    return item


def _msg(
    conv: Conversation,
    ext: str,
    body: str | None,
    when: datetime,
    *,
    attachments: list[Any] | None = None,
) -> Message:
    return Message(
        conversation_id=conv.id,
        external_message_id=ext,
        direction="in",
        sender_type="client",
        body=body,
        attachments=attachments or [],
        delivery_status="delivered",
        created_at=when,
    )


def _api(raw_chat: dict[str, Any], raw_messages: list[dict[str, Any]]):
    """`get_chat` → карточка чата, `get_chat_messages` → страница истории по смещению."""

    async def fake_call(fn, _account, _db, _redis, _limiter, *args, **kwargs):
        if fn.__name__ == "get_chat":
            return raw_chat
        offset = int(kwargs.get("offset", 0))
        limit = int(kwargs.get("limit", 100))
        return raw_messages[offset : offset + limit]

    return fake_call


def _live(text: str, *, msg: str, when: datetime, chat: str = "chat-n29") -> InboundEvent:
    return InboundEvent(
        external_chat_id=chat,
        external_message_id=msg,
        author_id=CLIENT_UID,
        account_user_id=ACCOUNT_UID,
        text=text,
        created_at=when,
        client_name="Клиент",
    )


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(ACCOUNT_UID)


@pytest.fixture
def без_сети(monkeypatch) -> list[tuple[str, dict[str, Any]]]:
    async def _noop(*args, **kwargs):
        return None

    monkeypatch.setattr(svc, "enqueue_conversation_history", _noop)
    кадры: list[tuple[str, dict[str, Any]]] = []

    async def fake_publish(_redis, kind, payload, **_kw):
        кадры.append((kind, payload))

    monkeypatch.setattr(inbound_svc, "publish_event", fake_publish)
    return кадры


async def _догрузить(
    monkeypatch,
    db_sessionmaker,
    redis,
    account,
    chat_id: str,
    raw_messages,
    *,
    unread: bool = False,
) -> None:
    """История одного чата — той же дверью, что массовый импорт (`_backfill_chat`)."""
    monkeypatch.setattr(svc, "_avito_call", _api(_raw_chat(chat_id, unread=unread), raw_messages))
    await svc.backfill_conversation(_ctx(db_sessionmaker, redis), account.id, chat_id)


async def _догнать(db_sessionmaker, redis, conv_id: uuid.UUID, since: datetime) -> str:
    from app.workers import cards_catchup as worker

    return await worker.catchup_conversation_cards(
        _ctx(db_sessionmaker, redis), conv_id, since.isoformat()
    )


async def _conv(db_sessionmaker, chat_id: str) -> Conversation:
    async with db_sessionmaker() as s:
        return (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == chat_id))
        ).scalar_one()


async def _строки(db_sessionmaker) -> list[ClientAddressCandidate]:
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    sa.select(ClientAddressCandidate).order_by(ClientAddressCandidate.detected_at)
                )
            )
            .scalars()
            .all()
        )


async def _ключи(redis, prefix: str) -> list[str]:
    return list(await redis.keys(f"arq:job:{prefix}*"))


# ── задача 1: имя задачи, окно, постановка ───────────────────────────────────


def test_нижняя_граница_догона_чистой_функцией():
    """Самая ранняя вставленная реплика клиента В ОКНЕ; старше окна — не считается."""
    from app.services import cards_catchup as cc

    now = NOW
    assert cc.earliest_in_window([], now=now) is None
    assert cc.earliest_in_window([now - timedelta(days=40)], now=now) is None
    assert cc.earliest_in_window(
        [now - timedelta(days=40), now - timedelta(days=2), now - timedelta(days=1)], now=now
    ) == now - timedelta(days=2)
    assert cc.job_id("abc") == "cardcatch:abc"


async def test_постановка_догона_дедуп_по_диалогу(redis):
    from app.services import cards_catchup as cc

    conv_id = uuid.uuid4()
    assert await cc.enqueue_cards_catchup(redis, conv_id, since=NOW) is True
    assert await redis.exists(f"arq:job:cardcatch:{conv_id}")
    assert await cc.enqueue_cards_catchup(redis, conv_id, since=NOW) is False, "вторая — бесплатна"


async def test_отказ_redis_при_постановке_не_бросает_и_называет_причину(redis, monkeypatch):
    """Постановка догона не стоит сорванной загрузки чата: отказ Redis — False и warning
    с именем класса ошибки (ревью 19.09: без него таймаут, разрыв и ошибка сериализации
    по журналу неразличимы)."""
    import structlog
    from arq.connections import ArqRedis

    from app.services import cards_catchup as cc

    async def падает(*args: Any, **kwargs: Any) -> Any:
        raise ConnectionError("redis ушёл")

    monkeypatch.setattr(ArqRedis, "enqueue_job", падает)
    with structlog.testing.capture_logs() as логи:
        assert await cc.enqueue_cards_catchup(redis, uuid.uuid4(), since=NOW) is False
    (запись,) = [л for л in логи if л["event"] == "cards_catchup.enqueue_failed"]
    assert (запись["log_level"], запись["error"]) == ("warning", "ConnectionError")


# ── задача 2: обёртка живого пути ────────────────────────────────────────────


async def test_обёртка_идёт_тем_же_путём_телефон_потом_адрес(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """Порядок как в `apply_inbound_event`: сначала телефон, потом адрес; задач не ставит.
    Порядок держит шпион на одном сообщении с обоими (ревью 19.09): по кортежам двух
    разных сообщений перестановка вызовов в обёртке была бы невидима."""
    T = NOW - timedelta(days=1)
    await apply_inbound_event(db, redis, account, _live("здравствуйте", msg="m-0", when=T))
    conv_id = (await _conv(db_sessionmaker, "chat-n29")).id
    вызовы: list[str] = []
    исходный_телефон = inbound_svc._maybe_extract_phone
    исходный_адрес = inbound_svc._maybe_extract_address

    async def шпион_телефона(*args: Any, **kwargs: Any) -> Any:
        вызовы.append("phone")
        return await исходный_телефон(*args, **kwargs)

    async def шпион_адреса(*args: Any, **kwargs: Any) -> Any:
        вызовы.append("address")
        return await исходный_адрес(*args, **kwargs)

    monkeypatch.setattr(inbound_svc, "_maybe_extract_phone", шпион_телефона)
    monkeypatch.setattr(inbound_svc, "_maybe_extract_address", шпион_адреса)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv is not None
        client = await s.get(Client, conv.client_id)
        assert client is not None

        телефон = _msg(conv, "m-x1", "мой номер 8 900 111-22-46", T + timedelta(minutes=1))
        адрес = _msg(conv, "m-x2", "Приезжайте на ул. Ленина 5", T + timedelta(minutes=2))
        оба = _msg(conv, "m-x3", "ул. Ленина 5, номер 8 900 111-22-44", T + timedelta(minutes=3))
        s.add_all([телефон, адрес, оба])
        await s.flush()
        assert await inbound_svc.replay_card_extraction(
            s, conv, client, телефон, now=T + timedelta(minutes=1)
        ) == ("phone_suggested", None, False)  # автозапись телефона по умолчанию выключена
        assert await inbound_svc.replay_card_extraction(
            s, conv, client, адрес, now=T + timedelta(minutes=2)
        ) == (None, "address_suggested", False)
        вызовы.clear()
        assert await inbound_svc.replay_card_extraction(
            s, conv, client, оба, now=T + timedelta(minutes=3)
        ) == ("phone_suggested", "address_refined", False)  # адрес тот же — строка одна
        assert вызовы == ["phone", "address"]
        await s.commit()
    (строка,) = await _строки(db_sessionmaker)
    assert строка.detected_at.replace(tzinfo=UTC) == T + timedelta(minutes=2)
    for prefix in ("geocode:", "addr-fill:", "addr-ask:", "merge:", "llm-addr:"):
        assert await _ключи(redis, prefix) == [], f"обёртка поставила задачу {prefix}"


async def test_обёртка_отмечает_геоточку_с_координатами(
    db, redis, account, db_sessionmaker, без_сети
):
    """Точка Авито с координатами — строка `exact` без карты и признак для автозаписи;
    контрпример: точка без координат (до 15.09) — `pending` и признака нет."""
    T = NOW - timedelta(days=1)
    await apply_inbound_event(db, redis, account, _live("здравствуйте", msg="m-0", when=T))
    conv_id = (await _conv(db_sessionmaker, "chat-n29")).id
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, conv_id)
        assert conv is not None
        client = await s.get(Client, conv.client_id)
        assert client is not None
        точка = _msg(
            conv, "m-geo", None, T + timedelta(minutes=1), attachments=[_геоточка_вложение()]
        )
        старая = _msg(
            conv,
            "m-geo-old",
            None,
            T + timedelta(minutes=2),
            attachments=[
                _геоточка_вложение(
                    координаты=False, адрес="Калужская область, Калуга, улица Мира, 7"
                )
            ],
        )
        s.add_all([точка, старая])
        await s.flush()
        assert await inbound_svc.replay_card_extraction(
            s, conv, client, точка, now=T + timedelta(minutes=1)
        ) == (None, "address_suggested", True)
        assert await inbound_svc.replay_card_extraction(
            s, conv, client, старая, now=T + timedelta(minutes=2)
        ) == (None, "address_suggested", False)
        await s.commit()
    строки = {r.value: r.geo_status for r in await _строки(db_sessionmaker)}
    assert строки == {"улица Ленина, 5": "exact", "улица Мира, 7": "pending"}
    assert await _ключи(redis, "addr-fill:") == [], "обёртка сама автозапись не ставит"


# ── задача 3: сторож порядка в `clients.record_address_candidate` ────────────


async def test_строка_не_едет_к_старому_упоминанию_и_части_не_перебиваются(db, account):
    """Догон и сверка приносят реплики задним числом: строка живого диалога B (T)
    не уезжает в старый диалог A (T−2д) и «кв 3» не перебивает «кв 7».
    Контрпример: упоминание ПОЗЖЕ строки — едет и дописывает (ревью 13.09)."""
    from app.services import address_parse, clients

    client = Client(channel="avito", external_id="n29-guard", name="Клиент")
    db.add(client)
    await db.flush()
    conv_a = Conversation(
        channel="avito",
        external_chat_id="n29-a",
        account_id=account.id,
        client_id=client.id,
        status="closed",
    )
    conv_b = Conversation(
        channel="avito",
        external_chat_id="n29-b",
        account_id=account.id,
        client_id=client.id,
        status="new",
    )
    db.add_all([conv_a, conv_b])
    await db.flush()
    T = NOW
    found_b = address_parse.parse("ул. Ленина 5, кв 7")
    found_a = address_parse.parse("ул. Ленина 5, кв 3")
    assert found_b is not None and found_a is not None
    await clients.record_address_candidate(
        db,
        client=client,
        conversation_id=conv_b.id,
        message_id=uuid.uuid4(),
        message_at=T,
        found=found_b,
        now=T,
    )
    await clients.record_address_candidate(
        db,
        client=client,
        conversation_id=conv_a.id,
        message_id=uuid.uuid4(),
        message_at=T - timedelta(days=2),
        found=found_a,
        now=T - timedelta(days=2),
    )
    await db.commit()
    (строка,) = (await db.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert (строка.conversation_id, строка.office) == (conv_b.id, "7")

    await clients.record_address_candidate(
        db,
        client=client,
        conversation_id=conv_a.id,
        message_id=uuid.uuid4(),
        message_at=T + timedelta(hours=1),
        found=found_a,
        now=T + timedelta(hours=1),
    )
    await db.commit()
    (строка,) = (await db.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert (строка.conversation_id, строка.office) == (conv_a.id, "3")


# ── задача 4: воркер догона и регистрация ────────────────────────────────────


def test_задача_зарегистрирована_и_не_хранит_результат():
    """Без строки в `WorkerSettings.functions` задача висит в pending; без
    `keep_result=0` вторая догрузка того же диалога в течение часа осталась бы без
    догона: `enqueue_job` с тем же `_job_id` возвращает None, пока жив `arq:result`
    (замок вопроса клиенту читает только очередь/полёт — он тут ни при чём)."""
    from app.workers.main import WorkerSettings, registered_job_names

    assert "catchup_conversation_cards" in registered_job_names()
    (f,) = [
        f
        for f in WorkerSettings.functions
        if getattr(f, "name", "") == "catchup_conversation_cards"
    ]
    assert f.keep_result_s == 0 and f.max_tries == 1  # поля `arq.worker.Function`


async def test_догон_перечитывает_хвост_включая_живую_реплику(
    db, redis, account, db_sessionmaker, без_сети
):
    """«кв 7» живой реплики при приёме строки не нашла (адрес ещё не приехал историей);
    догон хвоста обязан её дописать. `detected_at` строки — время реплики истории.
    Две реплики в проходе — сторож составного ключа: `db.get(Message, id)` упал бы на
    первой же (`InvalidRequestError`), и `messages` не дошёл бы до 2."""
    from app.workers import cards_catchup as worker

    T = NOW
    await apply_inbound_event(db, redis, account, _live("кв 7", msg="m-live", when=T))
    conv = await _conv(db_sessionmaker, "chat-n29")
    async with db_sessionmaker() as s:
        s.add(_msg(conv, "h-1", "ул. Ленина 5, кв 3", T - timedelta(days=3)))
        await s.commit()
    async with db_sessionmaker() as s, app_settings.one_pass():
        итог = await worker.replay_conversation(s, conv.id, since=T - timedelta(days=3))
    assert итог is not None
    assert (итог.messages, итог.with_address, итог.geopoint_ready) == (2, 2, False)
    (строка,) = await _строки(db_sessionmaker)
    assert строка.office == "7"
    assert строка.detected_at.replace(tzinfo=UTC) == T - timedelta(days=3)
    # Повтор идемпотентен — через саму задачу.
    assert await _догнать(db_sessionmaker, redis, conv.id, T - timedelta(days=3)) == "done"
    (строка2,) = await _строки(db_sessionmaker)
    assert (строка2.id, строка2.office) == (строка.id, "7")


async def test_сбой_одной_реплики_не_уносит_остальные(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    T = NOW
    await apply_inbound_event(
        db, redis, account, _live("здравствуйте", msg="m-0", when=T - timedelta(minutes=9))
    )
    conv = await _conv(db_sessionmaker, "chat-n29")
    async with db_sessionmaker() as s:
        s.add_all(
            [
                _msg(conv, "h-1", "сломай меня", T - timedelta(minutes=5)),
                _msg(conv, "h-2", "ул. Ленина 5", T - timedelta(minutes=4)),
            ]
        )
        await s.commit()
    original = inbound_svc.replay_card_extraction
    упало = {"n": 0}

    async def flaky(db_, conv_, client_, msg, *, now):
        if (msg.body or "").startswith("сломай"):
            упало["n"] += 1
            raise RuntimeError("разбор упал")
        return await original(db_, conv_, client_, msg, now=now)

    monkeypatch.setattr(inbound_svc, "replay_card_extraction", flaky)
    assert await _догнать(db_sessionmaker, redis, conv.id, T - timedelta(minutes=5)) == "done"
    assert упало["n"] == 1
    assert [r.value for r in await _строки(db_sessionmaker)] == ["ул. Ленина, 5"]


async def test_сбой_соединения_в_хвосте_уходит_наверх(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """Изолируется сбой РАЗБОРА, не соединения: `OperationalError` на первой
    реплике хвоста не глотается как `message_failed` — задача падает целиком
    (`max_tries=1`: след в Sentry вместо «done» без строк), вторая реплика не
    разбирается на том же мёртвом соединении. Задача голоса на то же исключение
    просит `Retry` (`test_voice_card_1909`). Ревью 19.09, C2/C4.

    ⚠ ДИВЕРСИЯ: убрать `except (OperationalError, InterfaceError): raise` в
    `replay_conversation` — исход "done", строка от второй реплики есть,
    `pytest.raises` краснеет.
    """
    import structlog
    from sqlalchemy.exc import OperationalError

    T = NOW
    await apply_inbound_event(
        db, redis, account, _live("здравствуйте", msg="m-0", when=T - timedelta(minutes=9))
    )
    conv = await _conv(db_sessionmaker, "chat-n29")
    async with db_sessionmaker() as s:
        s.add_all(
            [
                _msg(conv, "h-1", "оборви меня", T - timedelta(minutes=5)),
                _msg(conv, "h-2", "ул. Ленина 5", T - timedelta(minutes=4)),
            ]
        )
        await s.commit()
    original = inbound_svc.replay_card_extraction

    async def обрыв(db_, conv_, client_, msg, *, now):
        if (msg.body or "").startswith("оборви"):
            raise OperationalError("SELECT 1", None, ConnectionResetError("обрыв"))
        return await original(db_, conv_, client_, msg, now=now)

    monkeypatch.setattr(inbound_svc, "replay_card_extraction", обрыв)
    with structlog.testing.capture_logs() as логи, pytest.raises(OperationalError):
        await _догнать(db_sessionmaker, redis, conv.id, T - timedelta(minutes=5))
    assert [л["event"] for л in логи if л["event"].startswith("cards_catchup.")] == []
    assert await _строки(db_sessionmaker) == []


async def test_догон_истории_не_считает_ворота_модели(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """Копилка следствий у задачи истории закрыта (`со_следствиями=False`):
    ворота модели-читателя (`inbound.llm_read_wanted` — запрос к ленте на
    каждую реплику) не считаются там, где их никто не ставит; `geocode_ids` и
    `llm_reads` в итоге пусты. У задачи голоса копилка открыта — там ворота
    считаются и задача `llm-addr` ставится (`test_voice_card_1909`).
    Ревью 19.09, D1.

    ⚠ ДИВЕРСИЯ: в `replay_conversation` звать `replay_card_extraction_full`
    независимо от флага — шпион видит вызов, тест краснеет.
    """
    from app.workers import cards_catchup as worker

    T = NOW
    await apply_inbound_event(
        db, redis, account, _live("здравствуйте", msg="m-0", when=T - timedelta(minutes=9))
    )
    conv = await _conv(db_sessionmaker, "chat-n29")
    async with db_sessionmaker() as s:
        s.add_all(
            [
                _msg(conv, "h-1", "Пушки на 10", T - timedelta(minutes=5)),
                _msg(conv, "h-2", "ул. Ленина 5", T - timedelta(minutes=4)),
            ]
        )
        await s.commit()
    ворота: list[Any] = []
    исходные = inbound_svc.llm_read_wanted

    async def шпион(*args: Any, **kwargs: Any) -> bool:
        ворота.append(kwargs.get("речь"))
        return await исходные(*args, **kwargs)

    monkeypatch.setattr(inbound_svc, "llm_read_wanted", шпион)
    async with db_sessionmaker() as s, app_settings.one_pass():
        итог = await worker.replay_conversation(s, conv.id, since=T - timedelta(minutes=5))
    assert итог is not None
    assert (итог.messages, итог.with_address) == (2, 1)
    assert ворота == [], "ворота модели считаются только при открытой копилке"
    assert (итог.geocode_ids, итог.llm_reads) == ((), ())
    assert await _догнать(db_sessionmaker, redis, conv.id, T - timedelta(minutes=5)) == "done"
    assert ворота == []


async def test_служебная_запись_и_пустое_пропускаются(
    db, redis, account, db_sessionmaker, без_сети
):
    T = NOW
    await apply_inbound_event(
        db, redis, account, _live("здравствуйте", msg="m-0", when=T - timedelta(minutes=9))
    )
    conv = await _conv(db_sessionmaker, "chat-n29")
    async with db_sessionmaker() as s:
        s.add(
            _msg(
                conv,
                "h-sys",
                "[Системное сообщение] Пользователь создал чат, но пока ничего не написал",
                T - timedelta(minutes=5),
            )
        )
        await s.commit()
    assert await _догнать(db_sessionmaker, redis, conv.id, T - timedelta(minutes=5)) == "empty"
    assert await _строки(db_sessionmaker) == []
    assert await _догнать(db_sessionmaker, redis, uuid.uuid4(), T) == "gone"


async def test_догон_ставит_одну_автозапись_только_по_геоточке(
    db, redis, account, db_sessionmaker, без_сети
):
    """Строка `exact` от точки Авито в починку карты не попадает — автозапись ставит
    догон, один раз на диалог. Контрпример — текстовый адрес: `addr-fill:` нет."""
    T = NOW
    await apply_inbound_event(
        db, redis, account, _live("здравствуйте", msg="m-0", when=T - timedelta(minutes=9))
    )
    conv = await _conv(db_sessionmaker, "chat-n29")
    async with db_sessionmaker() as s:
        s.add(_msg(conv, "h-1", "Приезжайте на ул. Мира 7", T - timedelta(minutes=6)))
        await s.commit()
    assert await _догнать(db_sessionmaker, redis, conv.id, T - timedelta(minutes=6)) == "done"
    assert await _ключи(redis, "addr-fill:") == [], "текст — через карту, не напрямую"

    async with db_sessionmaker() as s:
        s.add_all(
            [
                _msg(
                    conv,
                    "h-geo",
                    None,
                    T - timedelta(minutes=5),
                    attachments=[_геоточка_вложение()],
                ),
                _msg(
                    conv,
                    "h-geo-2",
                    None,
                    T - timedelta(minutes=4),
                    attachments=[_геоточка_вложение()],
                ),
            ]
        )
        await s.commit()
    assert await _догнать(db_sessionmaker, redis, conv.id, T - timedelta(minutes=6)) == "done"
    assert await _ключи(redis, "addr-fill:") == [f"arq:job:addr-fill:{conv.id}"]
    assert {r.value: r.geo_status for r in await _строки(db_sessionmaker)} == {
        "ул. Мира, 7": "pending",
        "улица Ленина, 5": "exact",
    }


# ── задача 5: постановка догона из `_backfill_chat` (стенд истории) ──────────


async def test_ответ_на_вопрос_оператора_из_истории_рождает_строку(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """Боевой класс H_no_rows: клиент назвал адрес ДО первого дошедшего вебхука.
    Диалог создан живой репликой, «что было раньше» приезжает `backfill_conversation`.
    Порядок сообщений Авито нарочно от новых к старым — он нигде не обещан.
    В хвосте — исходящее оператора с НАШИМ адресом и номером (ревью 19.09): фильтр
    `direction='in' AND sender_type='client'` — единственное, что не даёт догону
    записать клиенту наш телефон и адрес; без него здесь были бы вторая строка и номер."""
    T = NOW
    await apply_inbound_event(
        db, redis, account, _live("когда сможете приехать?", msg="m-live", when=T)
    )
    история = [
        _raw_msg("m-live", "когда сможете приехать?", created=T),
        _raw_msg(
            "h-4",
            "Наш адрес ул. Пушкина 10, звоните 8 900 111-22-44",
            created=T - timedelta(minutes=6),
            author_id=ACCOUNT_UID,
        ),
        _raw_msg("h-3", "кв 3, 2 подъезд", created=T - timedelta(minutes=7)),
        _raw_msg("h-2", "садовая 12", created=T - timedelta(minutes=8)),
        _raw_msg(
            "h-1", "Куда к вам подъехать?", created=T - timedelta(minutes=10), author_id=ACCOUNT_UID
        ),
    ]
    await _догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-n29", история)
    conv = await _conv(db_sessionmaker, "chat-n29")
    assert await redis.exists(f"arq:job:cardcatch:{conv.id}"), "догон не заказан"
    assert await _строки(db_sessionmaker) == [], "дверь сама строк не заводит — это делает задача"

    assert await _догнать(db_sessionmaker, redis, conv.id, T - timedelta(minutes=8)) == "done"

    (строка,) = await _строки(db_sessionmaker)
    assert (строка.house, строка.level, строка.office, строка.entrance) == ("12", "B", "3", "2")
    assert "Пушкина" not in строка.value, "адрес оператора попал в строку клиента"
    async with db_sessionmaker() as s:
        client = await s.get(Client, conv.client_id)
        assert client is not None and client.phone is None
        assert (
            await s.execute(sa.select(sa.func.count()).select_from(ClientPhoneCandidate))
        ).scalar_one() == 0, "номер оператора попал в предложения клиенту"
    assert строка.geo_status == "pending"
    assert строка.detected_at.replace(tzinfo=UTC) == T - timedelta(minutes=8)
    # Текстовая строка — через карту: ни одной задачи, в том числе автозаписи.
    for prefix in ("geocode:", "addr-fill:", "addr-ask:", "merge:", "llm-addr:"):
        assert await _ключи(redis, prefix) == [], f"догон поставил задачу {prefix}"
    assert [p["reason"] for k, p in без_сети if k == "client:updated"] == ["address_suggested"]


async def test_геоточка_из_истории_строка_exact_и_одна_автозапись(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """Точка Авито, присланная до первого вебхука: строка `exact` (карта — Авито) и
    ровно одна `addr-fill:{conv}` — иначе карточка пустая навсегда (`geo_repair`
    строки `exact` не берёт). Контрпример — предыдущий тест: история без точки,
    `addr-fill:` нет."""
    T = NOW
    await apply_inbound_event(db, redis, account, _live("когда приедете?", msg="m-live", when=T))
    история = [
        _raw_msg("m-live", "когда приедете?", created=T),
        _raw_geo("h-geo", created=T - timedelta(minutes=5)),
        _raw_msg(
            "h-1", "Куда к вам подъехать?", created=T - timedelta(minutes=6), author_id=ACCOUNT_UID
        ),
    ]
    await _догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-n29", история)
    conv = await _conv(db_sessionmaker, "chat-n29")
    assert await _догнать(db_sessionmaker, redis, conv.id, T - timedelta(minutes=5)) == "done"
    (строка,) = await _строки(db_sessionmaker)
    assert (строка.value, строка.geo_status, строка.geo_provider) == (
        "улица Ленина, 5",
        "exact",
        "avito",
    )
    assert await _ключи(redis, "addr-fill:") == [f"arq:job:addr-fill:{conv.id}"]
    assert await _ключи(redis, "geocode:") == []


async def test_импорт_закрытого_чата_телефон_в_карточку_без_кадра(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """Чат, которого у нас не было: диалог создаётся закрытым; догон кладёт номер
    (автозапись включена) и адрес строкой; кадра закрытому — нет; объединение не ставится."""
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.PHONE_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()
    история = [
        _raw_msg("h-1", "мой номер 8 900 111-22-46", created=NOW - timedelta(days=3)),
        _raw_msg("h-2", "Приезжайте на ул. Ленина 5", created=NOW - timedelta(days=3, minutes=-1)),
    ]
    await _догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-import", история)
    conv = await _conv(db_sessionmaker, "chat-import")
    assert conv.status == "closed"
    assert await _догнать(db_sessionmaker, redis, conv.id, NOW - timedelta(days=3)) == "done"
    async with db_sessionmaker() as s:
        client = await s.get(Client, conv.client_id)
    assert client is not None and client.phone == "+79001112246"
    assert [r.value for r in await _строки(db_sessionmaker)] == ["ул. Ленина, 5"]
    assert [k for k, _ in без_сети if k == "client:updated"] == []
    assert await _ключи(redis, "merge:") == []


async def test_окно_тридцать_дней_и_повтор_без_новых_реплик(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """`backfill_conversation` грузит всё (`HISTORY_ALL`, floor=None): 40-дневная реплика
    вставляется, но догон по ней не ставится."""
    старое = [_raw_msg("h-old", "ул. Ленина 5", created=NOW - timedelta(days=40))]
    await _догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-old", старое)
    conv = await _conv(db_sessionmaker, "chat-old")
    assert not await redis.exists(f"arq:job:cardcatch:{conv.id}"), "старше окна — не догоняем"

    смесь = старое + [
        _raw_msg("h-new", "Приезжайте на ул. Мира 7", created=NOW - timedelta(days=2))
    ]
    await _догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-old", смесь)
    assert await redis.exists(f"arq:job:cardcatch:{conv.id}")
    await redis.delete(f"arq:job:cardcatch:{conv.id}")
    await redis.zrem("arq:queue", f"cardcatch:{conv.id}")
    # Третий заход по тому же чату ничего не вставляет — и ничего не ставит.
    await _догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-old", смесь)
    assert not await redis.exists(f"arq:job:cardcatch:{conv.id}")


async def test_чат_из_наших_и_служебных_догон_не_ставит(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    история = [
        _raw_msg(
            "h-1",
            "Добрый день! Чем помочь?",
            created=NOW - timedelta(days=1),
            author_id=ACCOUNT_UID,
        ),
        _raw_msg(
            "h-2",
            "[Системное сообщение] Пользователь создал чат, но пока ничего не написал",
            created=NOW - timedelta(days=1),
        ),
    ]
    await _догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-empty", история)
    conv = await _conv(db_sessionmaker, "chat-empty")
    assert not await redis.exists(f"arq:job:cardcatch:{conv.id}")


# ── задача 6: вопрос клиенту ждёт догон ──────────────────────────────────────


async def test_вопрос_клиенту_ждёт_догон_карточки(redis):
    """`cardcatch:{conv}` в полёте — переходный замок, как `convhist`: строки адреса
    появятся после догона, спрашивать раньше — спросить того, кто уже ответил."""
    from app.services import cards_catchup as cc
    from app.workers import address_ask as ask_worker

    conv = SimpleNamespace(id=uuid.uuid4(), account_id=uuid.uuid4())
    assert await ask_worker._история_едет(redis, conv) is False
    await cc.enqueue_cards_catchup(redis, conv.id, since=NOW)
    assert await ask_worker._история_едет(redis, conv) is True


# ── ревью 19.09: снимок между транзакциями, сторож вердикта, история в аудите ──

ТЕКСТ_КАРТЫ = "ул Ленина, 5, Калуга"


async def _стенд_двух_реплик(
    db, redis, account, db_sessionmaker, *, T: datetime
) -> tuple[Conversation, ClientAddressCandidate]:
    """Живая «ул. Ленина 5» (T−8) со строкой, подтверждённой картой (`exact`), и «кв 7»
    историей (T−7): догону от T−8 достаются две реплики — две транзакции."""
    await apply_inbound_event(
        db, redis, account, _live("ул. Ленина 5", msg="m-1", when=T - timedelta(minutes=8))
    )
    conv = await _conv(db_sessionmaker, "chat-n29")
    async with db_sessionmaker() as s:
        (строка,) = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
        строка.geo_status = "exact"
        строка.geo_lat, строка.geo_lon = 54.5, 36.25
        строка.geo_formatted = ТЕКСТ_КАРТЫ
        строка.geo_provider = "dadata"
        s.add(_msg(conv, "h-kv", "кв 7", T - timedelta(minutes=7)))
        await s.commit()
    return conv, строка


async def _догон_с_чужой_записью_между_репликами(
    monkeypatch, db_sessionmaker, conv: Conversation, *, since: datetime, запись: dict[str, Any]
) -> None:
    """Чужая запись в карточку между двумя репликами догона: UPDATE мимо identity map
    (`synchronize_session=False`) в транзакции первой реплики — в базе она с первым
    commit'ом, а ORM-объект `client` о ней не знает, как о записи из другого процесса."""
    from app.workers import cards_catchup as worker

    исходная = inbound_svc.replay_card_extraction
    сделано = {"n": 0}

    async def с_чужой_записью(db_, conv_, client_, msg, *, now):
        if сделано["n"] == 0:
            сделано["n"] = 1
            await db_.execute(
                sa.update(Client)
                .where(Client.id == client_.id)
                .values(**запись)
                .execution_options(synchronize_session=False)
            )
        return await исходная(db_, conv_, client_, msg, now=now)

    monkeypatch.setattr(inbound_svc, "replay_card_extraction", с_чужой_записью)
    async with db_sessionmaker() as s, app_settings.one_pass():
        итог = await worker.replay_conversation(s, conv.id, since=since)
    assert итог is not None and итог.messages == 2 and сделано["n"] == 1


async def test_догон_не_перезаписывает_адрес_оператора_вписанный_между_репликами(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """До догона в поле — адрес автозаписи из строки. Между «ул. Ленина 5» и «кв 7»
    оператор вписал адрес руками (`address_set_at`, связь со строкой снята — как
    `set_address`). «кв 7» дописывается в строку, но текст оператора остаётся.
    ⚠ ДИВЕРСИЯ: убрать `db.refresh(client)` после commit'а в `replay_conversation` —
    `refresh_auto_address` судит по снимку (`address_set_at is None`, связь на месте)
    и кладёт «…, кв 7» поверх слов человека."""
    T = NOW
    conv, строка = await _стенд_двух_реплик(db, redis, account, db_sessionmaker, T=T)
    async with db_sessionmaker() as s:
        client = await s.get(Client, conv.client_id)
        assert client is not None
        client.address = ТЕКСТ_КАРТЫ
        client.address_candidate_id, client.address_value = строка.id, строка.value
        client.address_set_at = None
        await s.commit()
    await _догон_с_чужой_записью_между_репликами(
        monkeypatch,
        db_sessionmaker,
        conv,
        since=T - timedelta(minutes=8),
        запись={
            "address": "Текст оператора",
            "address_set_at": T - timedelta(minutes=7, seconds=30),
            "address_candidate_id": None,
            "address_value": None,
        },
    )
    async with db_sessionmaker() as s:
        client = await s.get(Client, conv.client_id)
        (строка2,) = (await s.execute(sa.select(ClientAddressCandidate))).scalars().all()
    assert client is not None and client.address == "Текст оператора"
    assert client.address_set_at is not None
    assert строка2.office == "7", "квартира в строку дописывается — спорит не с ней"


async def test_догон_дописывает_квартиру_в_адрес_автозаписи_легшей_между_репликами(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """Контрпример к предыдущему: до догона поле пустое, между репликами автозапись
    (другой процесс) положила строку в карточку — «кв 7» обязана дойти до текста
    карточки (класс жалобы 18.09, Хабаровск). ⚠ ДИВЕРСИЯ та же: без перечитки
    `refresh_auto_address` видит `address is None` и молчит."""
    T = NOW
    conv, строка = await _стенд_двух_реплик(db, redis, account, db_sessionmaker, T=T)
    await _догон_с_чужой_записью_между_репликами(
        monkeypatch,
        db_sessionmaker,
        conv,
        since=T - timedelta(minutes=8),
        запись={
            "address": ТЕКСТ_КАРТЫ,
            "address_candidate_id": строка.id,
            "address_value": строка.value,
            "address_set_at": None,
        },
    )
    async with db_sessionmaker() as s:
        client = await s.get(Client, conv.client_id)
    assert client is not None and client.address == f"{ТЕКСТ_КАРТЫ}, кв 7"


async def _решённая_строка(
    db, account, *, external_id: str, текст: str, T: datetime, resolved_by: uuid.UUID | None
) -> tuple[Client, Conversation, ClientAddressCandidate]:
    """Строка дома с вердиктом карты (`exact`, координаты, варианты); `resolved_by` —
    оператор выбрал её кнопкой (как `resolve_address_candidate`)."""
    from app.services import address_parse, clients

    client = Client(channel="avito", external_id=external_id, name="Клиент")
    db.add(client)
    await db.flush()
    conv = Conversation(
        channel="avito",
        external_chat_id=f"chat-{external_id}",
        account_id=account.id,
        client_id=client.id,
        status="new",
    )
    db.add(conv)
    await db.flush()
    found = address_parse.parse(текст)
    assert found is not None
    итог = await clients.record_address_candidate(
        db,
        client=client,
        conversation_id=conv.id,
        message_id=uuid.uuid4(),
        message_at=T,
        found=found,
        now=T,
    )
    строка = await db.get(ClientAddressCandidate, итог.candidate_id)
    assert строка is not None
    строка.geo_status = "exact"
    строка.geo_lat, строка.geo_lon = 54.5, 36.25
    строка.geo_formatted = ТЕКСТ_КАРТЫ
    строка.geo_provider = "dadata"
    строка.geo_variants = [{"formatted": ТЕКСТ_КАРТЫ, "lat": 54.5, "lon": 36.25}]
    строка.geo_attempts = 2
    if resolved_by is not None:
        строка.status = "accepted"
        строка.resolved_by_id, строка.resolved_at = resolved_by, T
    await db.commit()
    return client, conv, строка


def _вердикт(строка: ClientAddressCandidate) -> tuple[Any, ...]:
    return (
        строка.geo_status,
        строка.geo_lat,
        строка.geo_lon,
        строка.geo_variants,
        строка.geo_formatted,
        строка.geo_attempts,
        строка.resolved_by_id,
        строка.status,
    )


def _сброшен(итог: Any, строка: ClientAddressCandidate) -> bool:
    return (
        итог.перепроверить is True
        and строка.settlement == "Ударник"
        and (строка.geo_status, строка.geo_lat, строка.geo_variants, строка.geo_attempts)
        == ("pending", None, None, 0)
    )


async def test_старая_реплика_с_пунктом_не_сбрасывает_вердикт_решённый_человеком(
    db, account, users_by_role
):
    """Догон истории приносит реплику СТАРШЕ строки с тем же адресом и пунктом.
    Строка «Ленина 5», решённая оператором (`exact`, кнопка): пункт дописан, вердикт,
    координаты, варианты и автор решения не тронуты, перепроверки нет, тип улицы
    («ул.») ключ не меняет. То же — у решённой человеком от ПОЗДНЕЙ реплики и у
    нерешённой от старой. Контрпример: нерешённая строка + поздняя реплика — сброс,
    как и было (ревью 13.09: «посёлок, названный позже, меняет вердикт»).
    ⚠ ДИВЕРСИЯ: `вердикт_подвижен = True` — первые три части краснеют; `= False` —
    контрпример."""
    from app.services import address_parse, clients

    T = NOW
    оператор = users_by_role["manager"].id

    async def дописать(client, conv, *, текст: str, когда: datetime):
        found = address_parse.parse(текст)
        assert found is not None
        итог = await clients.record_address_candidate(
            db,
            client=client,
            conversation_id=conv.id,
            message_id=uuid.uuid4(),
            message_at=когда,
            found=found,
            now=когда,
        )
        await db.commit()
        return итог

    # 1. Решена оператором, реплика старше: пункт дописан, вердикт цел.
    client, conv, строка = await _решённая_строка(
        db, account, external_id="d3-op", текст="Ленина 5", T=T, resolved_by=оператор
    )
    было = _вердикт(строка)
    итог = await дописать(
        client, conv, текст="пос. Ударник, ул. Ленина 5", когда=T - timedelta(days=1)
    )
    await db.refresh(строка)
    assert (итог.впервые, итог.candidate_id, итог.перепроверить) == (False, строка.id, False)
    assert _вердикт(строка) == было
    assert (строка.settlement, строка.settlement_type) == ("Ударник", "посёлок")
    assert (строка.value, строка.street) == ("Ленина, 5", "Ленина"), (
        "ключ решённой строки не меняется"
    )

    # 2. Решена оператором, реплика ПОЗЖЕ: город дописан, вердикт цел.
    итог = await дописать(
        client, conv, текст="г. Шадринск, ул. Ленина 5", когда=T + timedelta(hours=1)
    )
    await db.refresh(строка)
    assert (итог.перепроверить, строка.locality, _вердикт(строка)) == (False, "Шадринск", было)

    # 3. Не решена, реплика старше: пункт дописан без сброса.
    client2, conv2, строка2 = await _решённая_строка(
        db, account, external_id="d3-old", текст="ул. Ленина 5", T=T, resolved_by=None
    )
    было2 = _вердикт(строка2)
    итог = await дописать(
        client2, conv2, текст="ул. Ленина 5, посёлок Ударник", когда=T - timedelta(days=1)
    )
    await db.refresh(строка2)
    assert (итог.перепроверить, строка2.settlement, _вердикт(строка2)) == (False, "Ударник", было2)

    # 4. Контрпример: не решена, реплика позже — сброс в очередь карты.
    client3, conv3, строка3 = await _решённая_строка(
        db, account, external_id="d3-live", текст="ул. Ленина 5", T=T, resolved_by=None
    )
    итог = await дописать(
        client3, conv3, текст="ул. Ленина 5, посёлок Ударник", когда=T + timedelta(minutes=5)
    )
    await db.refresh(строка3)
    assert _сброшен(итог, строка3)


def test_номер_из_истории_по_стенным_часам():
    """Порог `ИСТОРИЯ_СТАРШЕ` (сутки): реплика старше — история, младше и без времени — нет.
    Судить по `now` разбора нельзя: у догона `now` = время реплики."""
    from app.services import clients

    wall = NOW
    assert clients.номер_из_истории(wall - timedelta(days=25), wall=wall) is True
    assert clients.номер_из_истории(wall - timedelta(hours=25), wall=wall) is True
    assert clients.номер_из_истории(wall - timedelta(hours=23), wall=wall) is False
    assert clients.номер_из_истории(wall, wall=wall) is False
    assert clients.номер_из_истории(None, wall=wall) is False
    # Наивное время (SQLite) — тот же ответ.
    assert clients.номер_из_истории((wall - timedelta(days=2)).replace(tzinfo=None), wall=wall)


def датой(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(UTC).replace(microsecond=0)


async def test_номер_из_догона_истории_помечен_в_аудите_а_живой_нет(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """Догон 25-дневной реплики заполняет основной, и аудит `client.phone_captured`
    несёт `history=true` и время реплики — отчёт «собрано телефонов» (`stats._PHONES_SQL`)
    такие строки не считает. Живое входящее другого клиента — `history=false`.
    ⚠ ДИВЕРСИЯ: убрать `history` из details `absorb_phones` — KeyError."""
    from app.models import AuditLog

    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.PHONE_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()
    давно = NOW - timedelta(days=25)
    история = [_raw_msg("h-1", "мой номер 8 900 111-22-44", created=давно)]
    await _догрузить(monkeypatch, db_sessionmaker, redis, account, "chat-hist", история)
    conv = await _conv(db_sessionmaker, "chat-hist")
    assert await _догнать(db_sessionmaker, redis, conv.id, давно) == "done"

    живое = InboundEvent(
        external_chat_id="chat-live",
        external_message_id="m-live",
        author_id=CLIENT_UID + 1,
        account_user_id=ACCOUNT_UID,
        text="мой номер 8 900 111-22-55",
        created_at=NOW,
        client_name="Другой клиент",
    )
    await apply_inbound_event(db, redis, account, живое)

    async with db_sessionmaker() as s:
        rows = (
            (
                await s.execute(
                    sa.select(AuditLog)
                    .where(AuditLog.action == "client.phone_captured")
                    .order_by(AuditLog.created_at, AuditLog.id)
                )
            )
            .scalars()
            .all()
        )
    assert [r.details["source"] for r in rows] == ["regex", "regex"]
    по_диалогу = {r.details["conversation_id"]: r.details for r in rows}
    из_истории = по_диалогу[str(conv.id)]
    assert из_истории["history"] is True
    assert датой(из_истории["message_at"]) == давно
    живой = next(d for cid, d in по_диалогу.items() if cid != str(conv.id))
    assert живой["history"] is False


def test_отчёт_собрано_телефонов_исключает_историю_по_признаку_аудита():
    """Предикат отчёта и ключ аудита — один договор: `details.history` пишет
    `clients.absorb_phones`, читает `stats._PHONES_SQL`. SQL здесь не выполняется
    (unit-база — SQLite, `->>`/`::boolean` — Postgres); прогон на Postgres —
    `tests/integration/test_stats_pg.py` (у его посева ключа нет → строки считаются,
    как считались: `COALESCE(NULL, false)`)."""
    from app.services import stats

    sql = stats._PHONES_SQL
    assert "a.action = 'client.phone_captured'" in sql
    assert "AND NOT COALESCE((a.details->>'history')::boolean, false)" in sql


# ── задача 7: `cli backfill-cards` — той же обёрткой ─────────────────────────


async def test_backfill_cards_идёт_той_же_обёрткой(
    db, redis, account, db_sessionmaker, monkeypatch, без_сети
):
    """Разовый догон владельца и задача N29 — один путь (память dva-puti-raznyi-schet)."""
    from app.cli import run_backfill_cards

    await apply_inbound_event(
        db, redis, account, _live("ул. Ленина 5", msg="m-1", when=NOW - timedelta(days=1))
    )
    вызовы: list[uuid.UUID] = []
    original = inbound_svc.replay_card_extraction

    async def spy(db_, conv_, client_, msg, *, now):
        вызовы.append(msg.id)
        return await original(db_, conv_, client_, msg, now=now)

    monkeypatch.setattr(inbound_svc, "replay_card_extraction", spy)
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=7, dry_run=True)
    assert len(вызовы) == 1


async def test_backfill_cards_ставит_автозапись_по_геоточке_только_в_боевом_прогоне(
    db, redis, account, db_sessionmaker, без_сети
):
    """Геоточка в прошлом: сухой прогон ничего не ставит (строк после отката нет);
    боевой с Redis — одна `addr-fill:` на диалог. Контрпример — диалог с текстовым
    адресом: `addr-fill:` нет."""
    from app.cli import run_backfill_cards

    T = NOW - timedelta(days=2)
    await apply_inbound_event(db, redis, account, _live("здравствуйте", msg="m-0", when=T))
    await apply_inbound_event(
        db, redis, account, _live("ул. Мира 7", msg="m-t", when=T, chat="chat-text")
    )
    conv = await _conv(db_sessionmaker, "chat-n29")
    async with db_sessionmaker() as s:
        s.add(
            _msg(conv, "h-geo", None, T + timedelta(minutes=1), attachments=[_геоточка_вложение()])
        )
        await s.commit()
    await redis.delete(*(await redis.keys("arq:job:*")) or ["-"])
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=7, dry_run=True, redis=redis)
    assert await _ключи(redis, "addr-fill:") == []
    async with db_sessionmaker() as s:
        await run_backfill_cards(s, days=7, dry_run=False, redis=redis)
    assert await _ключи(redis, "addr-fill:") == [f"arq:job:addr-fill:{conv.id}"]
