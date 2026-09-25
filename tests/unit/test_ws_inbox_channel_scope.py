"""Кадры очереди уезжают операторам СВОЕГО канала (7.2, разбор Jivo 15 §2.2).

`GET /inbox` очередь по каналам сужает (`inbox.visible_queue_condition`), а
живая лента до этой правки — нет: `inbox:new` и `inbox:released` уходили всем
подключённым. Оператор канала «Парт - 723 БЕЛЫЙ» слышал звук на обращения
канала «! Парт - 7 / Ист - В43 МНЧ !», жал «Принять» и получал 403, а после
перезагрузки страницы чужие строки пропадали — фильтр выглядел сломанным.

Здесь проверяется вторая половина правила из шапки
`app/services/account_operators.py`: канал БЕЗ назначенных операторов открыт
ВСЕМ, а не никому. Если прочитать её наоборот, очередь замолчит у всех
тринадцати без единой ошибки в логах.
"""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.ws.hub import (
    INBOX_ELIGIBLE_KEY,
    INBOX_NEW,
    INBOX_RELEASED,
    Hub,
    publish_inbox_new,
    sees_every_channel,
)
from tests.unit.conftest import drain_events

QUEUE_EVENTS = (INBOX_NEW, INBOX_RELEASED)


class StubWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:  # pragma: no cover — не нужен
        pass


@pytest.fixture
def hub(redis) -> Hub:
    return Hub(redis)


def attach(hub: Hub, role: str, user_id: uuid.UUID | None = None):
    ws = StubWS()
    user = SimpleNamespace(id=user_id or uuid.uuid4(), full_name=f"Тест {role}", role=role)
    return ws, hub.attach(ws, user)


def queue_evt(type_: str, eligible: list[str] | None = None) -> dict:
    data: dict = {"conversation_id": "c1", "conversation": {"id": "c1"}}
    if eligible is not None:
        data[INBOX_ELIGIBLE_KEY] = eligible
    return {"type": type_, "ts": "2026-08-12T09:00:00.000Z", "data": data}


# --- собственно фильтр -------------------------------------------------------


@pytest.mark.parametrize("event_type", QUEUE_EVENTS)
async def test_queue_frame_skips_operator_of_another_channel(hub, event_type):
    """Оператору чужого канала кадр не уезжает вовсе.

    Не «уезжает без кнопки Принять»: строка очереди едет целиком и встаёт в
    чужой список ровно так же, как своя.
    """
    mine = uuid.uuid4()
    my_ws, _ = attach(hub, "manager", mine)
    stranger_ws, _ = attach(hub, "manager")

    await hub.dispatch(queue_evt(event_type, [str(mine)]))

    assert len(my_ws.sent) == 1, f"{event_type} не доехал до оператора своего канала"
    assert stranger_ws.sent == [], f"{event_type} уехал оператору чужого канала"


@pytest.mark.parametrize("event_type", QUEUE_EVENTS)
@pytest.mark.parametrize("eligible", [None, []], ids=["ключа нет", "пустой список"])
async def test_channel_without_operators_is_open_to_everyone(hub, event_type, eligible):
    """Правило совместимости: никого не назначили — значит канал ведут все.

    Обратное прочтение выключило бы очередь у всей смены молча, без ошибки.
    Отсутствие ключа покрыто той же проверкой намеренно: публикатор, который
    список ещё не проставил, обязан работать как раньше.
    """
    ws, _ = attach(hub, "manager")
    await hub.dispatch(queue_evt(event_type, eligible))
    assert len(ws.sent) == 1


@pytest.mark.parametrize("event_type", QUEUE_EVENTS)
@pytest.mark.parametrize("role", ["admin", "head", "observer"])
async def test_supervision_keeps_the_whole_queue(hub, event_type, role):
    """Администратор, руководитель и наблюдатель видят очередь целиком.

    Это не поблажка: администратор разбирает затор и получает эскалацию
    «отказались все», а руководитель с наблюдателем диалоги из очереди не
    берут вовсе — сузить очередь надзору значит ослепить его ради фильтра,
    который ему ничего не экономит (`account_operators.sees_all_channels`).
    """
    assert sees_every_channel(role)
    ws, _ = attach(hub, role, uuid.uuid4())
    await hub.dispatch(queue_evt(event_type, [str(uuid.uuid4())]))
    assert len(ws.sent) == 1, f"{role} потерял кадр {event_type} чужого канала"


async def test_manager_is_the_only_role_the_channel_narrows(hub):
    """Ровно одна роль под фильтром — та, у кого очередь личная и есть."""
    assert not sees_every_channel("manager")


@pytest.mark.parametrize("event_type", QUEUE_EVENTS)
async def test_eligible_list_never_reaches_the_browser(hub, event_type):
    """Список допущенных — адресация, а не содержимое строки.

    Отдать его браузеру значило бы раздать всей смене состав операторов
    каждого канала: то, что показывает экран назначения, доступный одним
    администраторам.
    """
    mine = uuid.uuid4()
    ws, _ = attach(hub, "manager", mine)
    await hub.dispatch(queue_evt(event_type, [str(mine), str(uuid.uuid4())]))

    (frame,) = ws.sent
    assert INBOX_ELIGIBLE_KEY not in frame["data"]
    assert "meta" not in frame
    # Остальное содержимое кадра на месте, включая персонализацию (15 §2.1).
    assert frame["data"]["conversation"] == {"id": "c1"}
    assert frame["data"]["can_claim"] is True


# --- публикатор --------------------------------------------------------------


async def test_publish_inbox_new_carries_the_channel(redis):
    """`publish_inbox_new` кладёт канал в кадр — отсортированным списком.

    Порядок фиксирован намеренно: кадр обязан быть воспроизводимым, иначе
    один и тот же диалог даёт разный JSON от прогона к прогону.
    """
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    first, second = "b" * 32, "a" * 32

    await publish_inbox_new(redis, {"id": "c1"}, eligible_operator_ids={first, second})

    (frame,) = await drain_events(pubsub)
    assert frame["type"] == INBOX_NEW
    assert frame["data"][INBOX_ELIGIBLE_KEY] == [second, first]


async def test_publish_inbox_new_without_channel_stays_open(redis):
    """Публикатор канал не передал — ключа в кадре нет, очередь у всех."""
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    await publish_inbox_new(redis, {"id": "c1"})

    (frame,) = await drain_events(pubsub)
    assert INBOX_ELIGIBLE_KEY not in frame["data"]
