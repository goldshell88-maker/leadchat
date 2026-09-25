"""Двое в одном диалоге (SCEN-48): регистр зрителей и рассылка состава.

Проверяется то, чего раньше не было ЦЕЛИКОМ: сервер кадр `subscribe` принимал
и запоминал в сессии, но никому о нём не рассказывал. Здесь — что состав
зрителей собирается, переживает вторую вкладку, уходит вместе с сокетом и
доезжает только до тех, у кого этот диалог открыт.
"""

import asyncio
import json
import time
import uuid

import pytest

from app.ws import viewers as viewers_registry
from app.ws.hub import Hub, handle_client_frame
from tests.unit.conftest import drain_events


class StubWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed: int | None = None

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.closed = code


class _User:
    def __init__(self, role: str, name: str, user_id: uuid.UUID) -> None:
        self.id = user_id
        self.full_name = name
        self.role = role


@pytest.fixture
def hub(redis) -> Hub:
    return Hub(redis)


def attach(hub: Hub, name: str, user_id: uuid.UUID | None = None):
    ws = StubWS()
    session = hub.attach(ws, _User("manager", name, user_id or uuid.uuid4()))
    return ws, session


async def subscribe(session, conv_id, redis) -> None:
    cid = str(conv_id) if conv_id is not None else None
    await handle_client_frame(
        session, json.dumps({"type": "subscribe", "data": {"conversation_id": cid}}), redis
    )


async def age(redis, conv_id, *, user_id, full_name, conn_id, seconds: int | None = None) -> None:
    """Состарить запись зрителя: как будто подтверждений не было `seconds`.

    Правим ВЕС записи, а не системное время: подмена `time.time` действует и на
    сам Redis, и тогда тест проходит от того, что истёк весь ключ, — то есть
    молчит о подметании, ради которого написан. Поймано поломкой.
    """
    stale = time.time() - (seconds if seconds is not None else viewers_registry._ttl() + 1)
    member = viewers_registry._member(user_id, full_name, conn_id)
    await redis.zadd(f"viewers:{conv_id}", {member: stale})


# --- регистр (app/ws/viewers.py) --------------------------------------------


async def test_two_operators_in_one_dialog_are_both_visible(redis):
    """Ради этого всё и написано: диалог открыт двумя, и это видно."""
    conv = uuid.uuid4()
    petr, anna = uuid.uuid4(), uuid.uuid4()
    await viewers_registry.touch(redis, conv, user_id=petr, full_name="Пётр", conn_id="c1")
    await viewers_registry.touch(redis, conv, user_id=anna, full_name="Анна", conn_id="c2")

    people = await viewers_registry.viewers(redis, conv)
    assert [p["full_name"] for p in people] == ["Пётр", "Анна"]  # порядок прихода
    assert {p["id"] for p in people} == {str(petr), str(anna)}


async def test_second_tab_of_one_person_is_still_one_viewer(redis):
    """Две вкладки одного диспетчера — не двое в диалоге."""
    conv, petr = uuid.uuid4(), uuid.uuid4()
    await viewers_registry.touch(redis, conv, user_id=petr, full_name="Пётр", conn_id="c1")
    await viewers_registry.touch(redis, conv, user_id=petr, full_name="Пётр", conn_id="c2")
    assert len(await viewers_registry.viewers(redis, conv)) == 1


async def test_closing_one_of_two_tabs_keeps_the_person_in_the_list(redis):
    """Закрыл одну вкладку из двух — из диалога не вышел.

    Ключ уникальности члена ZSET'а обязан включать conn_id: схлопни его до
    пользователя, и закрытие второй вкладки убирало бы человека из зрителей,
    хотя диалог у него открыт.
    """
    conv, petr = uuid.uuid4(), uuid.uuid4()
    await viewers_registry.touch(redis, conv, user_id=petr, full_name="Пётр", conn_id="c1")
    await viewers_registry.touch(redis, conv, user_id=petr, full_name="Пётр", conn_id="c2")
    await viewers_registry.leave(redis, conv, user_id=petr, full_name="Пётр", conn_id="c2")
    assert [p["full_name"] for p in await viewers_registry.viewers(redis, conv)] == ["Пётр"]


async def test_leave_finds_its_member(redis):
    """`leave` обязан попасть в свою запись — иначе зритель висит призраком."""
    conv, petr = uuid.uuid4(), uuid.uuid4()
    await viewers_registry.touch(redis, conv, user_id=petr, full_name="Пётр", conn_id="c1")
    await viewers_registry.leave(redis, conv, user_id=petr, full_name="Пётр", conn_id="c1")
    assert await viewers_registry.viewers(redis, conv) == []


async def test_stale_viewer_is_swept_on_read(redis):
    """Сокет умер молча (NAT, снятый процесс) — зритель не вечен.

    Просрочен ровно один из двоих: иначе тест прошёл бы и от исчезновения
    всего ключа целиком, ничего не сказав про само подметание.
    """
    conv, petr, anna = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await viewers_registry.touch(redis, conv, user_id=petr, full_name="Пётр", conn_id="c1")
    await viewers_registry.touch(redis, conv, user_id=anna, full_name="Анна", conn_id="c2")
    await age(redis, conv, user_id=anna, full_name="Анна", conn_id="c2")

    assert [p["full_name"] for p in await viewers_registry.viewers(redis, conv)] == ["Пётр"]


async def test_key_has_a_ttl(redis):
    """Брошенный диалог не остаётся в Redis навсегда."""
    conv = uuid.uuid4()
    await viewers_registry.touch(redis, conv, user_id=uuid.uuid4(), full_name="Пётр", conn_id="c1")
    assert 0 < await redis.ttl(f"viewers:{conv}") <= viewers_registry._ttl()


# --- кадр subscribe и рассылка состава --------------------------------------


async def test_subscribe_publishes_the_whole_roster(hub, redis):
    """Пришедший вторым узнаёт, что диалог уже открыт коллегой."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    conv = uuid.uuid4()
    _, petr = attach(hub, "Пётр")
    _, anna = attach(hub, "Анна")

    await subscribe(petr, conv, redis)
    await subscribe(anna, conv, redis)
    events = [e for e in await drain_events(pubsub) if e["type"] == "conversation:viewers"]

    assert len(events) == 2
    assert [v["full_name"] for v in events[0]["data"]["viewers"]] == ["Пётр"]
    # Второй кадр — состав ЦЕЛИКОМ, а не «пришла Анна»: собранный из приращений
    # список разъехался бы с правдой после первого же потерянного кадра.
    assert [v["full_name"] for v in events[1]["data"]["viewers"]] == ["Пётр", "Анна"]
    assert events[1]["data"]["conversation_id"] == str(conv)


async def test_subscribe_still_sets_the_session_conversation(hub, redis):
    """Прежнее поведение кадра (01 §11.4) никуда не делось."""
    _, session = attach(hub, "Пётр")
    conv = uuid.uuid4()
    await subscribe(session, conv, redis)
    assert session.conversation_id == conv
    await subscribe(session, None, redis)
    assert session.conversation_id is None


async def test_leaving_a_dialog_republishes_the_roster_to_those_left(hub, redis):
    """Ушёл — состав у оставшихся обновился, и в нём его больше нет."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    conv = uuid.uuid4()
    _, petr = attach(hub, "Пётр")
    _, anna = attach(hub, "Анна")
    await subscribe(petr, conv, redis)
    await subscribe(anna, conv, redis)
    await drain_events(pubsub)

    await subscribe(anna, None, redis)  # закрыла диалог
    (event,) = [e for e in await drain_events(pubsub) if e["type"] == "conversation:viewers"]
    assert [v["full_name"] for v in event["data"]["viewers"]] == ["Пётр"]


async def test_resubscribe_to_the_same_dialog_is_quiet(hub, redis):
    """Повтор подписки после реконнекта состав не менял — и рассылать нечего."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    conv = uuid.uuid4()
    _, petr = attach(hub, "Пётр")
    await subscribe(petr, conv, redis)
    await drain_events(pubsub)

    await subscribe(petr, conv, redis)
    assert [e for e in await drain_events(pubsub) if e["type"] == "conversation:viewers"] == []
    # но запись жива — вкладка, пережившая обрыв, из зрителей не выпадает
    assert len(await viewers_registry.viewers(redis, conv)) == 1


async def test_ping_keeps_the_reader_in_the_roster(hub, redis):
    """Молча читающий переписку не должен исчезать из зрителей.

    Именно внимательный читатель — самый опасный сосед: он ничего не печатает,
    и ровно ему сообщать «диалог открыт вторым» нужнее всего.
    """
    conv = uuid.uuid4()
    _, petr = attach(hub, "Пётр")
    await subscribe(petr, conv, redis)
    await age(redis, conv, user_id=petr.user_id, full_name=petr.full_name, conn_id=petr.conn_id)

    await handle_client_frame(petr, json.dumps({"type": "ping", "data": {"n": 1}}), redis)

    assert len(await viewers_registry.viewers(redis, conv)) == 1


async def test_resubscribe_after_reconnect_refreshes_the_record(hub, redis):
    """Пережившая обрыв вкладка остаётся зрителем.

    После реконнекта браузер подписывается на тот же диалог заново. Кадр
    приходит «в ту же точку», рассылать по нему нечего — но запись обновить
    обязан, иначе внимательный читатель выпадет из зрителей по времени именно
    после обрыва связи.
    """
    conv = uuid.uuid4()
    _, petr = attach(hub, "Пётр")
    await subscribe(petr, conv, redis)
    await age(redis, conv, user_id=petr.user_id, full_name=petr.full_name, conn_id=petr.conn_id)

    await subscribe(petr, conv, redis)  # тот же диалог

    assert len(await viewers_registry.viewers(redis, conv)) == 1


async def test_disconnect_removes_the_viewer(hub, redis):
    """Сокет закрыт — зритель уходит сразу, а не через полторы минуты."""
    conv = uuid.uuid4()
    _, petr = attach(hub, "Пётр")
    _, anna = attach(hub, "Анна")
    await subscribe(petr, conv, redis)
    await subscribe(anna, conv, redis)

    hub.detach(anna)
    await asyncio.sleep(0)  # detach ставит фоновую задачу — даём ей шаг
    await asyncio.sleep(0)
    assert [v["full_name"] for v in await viewers_registry.viewers(redis, conv)] == ["Пётр"]


async def test_viewer_cleanup_survives_a_dead_redis(hub, redis, monkeypatch):
    """Redis отвалился в момент закрытия вкладки — уборка не бросает наружу.

    Задача фоновая, ждать её некому: необработанное исключение в ней означало
    бы трейсбек на КАЖДОЕ закрытие вкладки, пока Redis недоступен.
    """
    conv = uuid.uuid4()
    _, petr = attach(hub, "Пётр")
    await subscribe(petr, conv, redis)

    async def boom(*args, **kwargs):
        raise ConnectionError("redis упал")

    monkeypatch.setattr(viewers_registry, "leave", boom)

    await hub.forget_viewer(petr)
    assert petr.conversation_id is None


async def test_detach_without_an_open_dialog_spawns_nothing(hub, redis):
    """Отключение сессии без открытого диалога ничего не будит."""
    _, petr = attach(hub, "Пётр")
    hub.detach(petr)
    assert hub._tasks == set()


# --- адресация (Hub._allowed) ------------------------------------------------


async def test_roster_reaches_only_those_who_opened_the_dialog(hub):
    """Состав зрителей бесполезен тому, у кого диалог не открыт."""
    conv = uuid.uuid4()
    watcher_ws, watcher = attach(hub, "Пётр")
    watcher.conversation_id = conv
    idle_ws, _ = attach(hub, "Анна")
    other_ws, other = attach(hub, "Игорь")
    other.conversation_id = uuid.uuid4()

    await hub.dispatch(
        {
            "type": "conversation:viewers",
            "ts": "2026-08-12T10:00:00.000Z",
            "data": {"conversation_id": str(conv), "viewers": []},
        }
    )
    assert len(watcher_ws.sent) == 1
    assert idle_ws.sent == []
    assert other_ws.sent == []
