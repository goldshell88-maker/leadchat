"""Управление диалогами — спринт 3 (01 §5.4 статус, §5.5 назначение,
§5.6 история клиента, §3.1 /users/assignable), плюс поиск §5.1.

Проверяем именно бизнес-эффекты, а не только коды ответов: системная запись
в ленте, строки `audit_log` по контракту 06 §0.3, WS-события и флажок ⚑.
"""

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from app.models import AuditLog, Client, Conversation, Message, User
from app.services import inbox
from app.ws.hub import INBOX_CLAIMED, INBOX_RELEASED
from tests.unit.conftest import drain_events

T0 = datetime(2026, 8, 4, 9, 0, 0, tzinfo=UTC)


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def seed(db_sessionmaker, make_avito_account, users_by_role):
    """Один активный диалог + прошлый закрытый диалог того же клиента."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        cl = Client(
            channel="avito",
            external_id="7001",
            name="Иван Петров",
            phone="+79261234567",
            avito_rating=4.9,
        )
        other = Client(channel="avito", external_id="7002", name="Мария Соколова")
        s.add_all([cl, other])
        await s.flush()

        current = Conversation(
            channel="avito",
            external_chat_id="chat-current",
            account_id=account.id,
            client_id=cl.id,
            status="new",
            unread_count=1,
            last_message_at=T0 + timedelta(minutes=10),
            item_title="Ремонт iPhone 13",
            item_url="https://avito.ru/item/1",
            item_price="от 1500 ₽",
        )
        past = Conversation(
            channel="avito",
            external_chat_id="chat-past",
            account_id=account.id,
            client_id=cl.id,
            status="closed",
            assignee_id=users_by_role["manager"].id,
            last_message_at=T0 - timedelta(days=60),
            item_title="Ремонт MacBook Air",
        )
        foreign = Conversation(  # диалог другого клиента — в историю попасть не должен
            channel="avito",
            external_chat_id="chat-foreign",
            account_id=account.id,
            client_id=other.id,
            status="new",
            last_message_at=T0,
        )
        s.add_all([current, past, foreign])
        await s.flush()

        s.add(
            Message(
                conversation_id=current.id,
                external_message_id="am-cur-1",
                direction="in",
                sender_type="client",
                body="Экран разбит, почём замена? Мой номер 8 926 123-45-67",
                attachments=[],
                delivery_status="delivered",
                created_at=T0 + timedelta(minutes=10),
            )
        )
        for n in range(3):
            s.add(
                Message(
                    conversation_id=past.id,
                    external_message_id=f"am-past-{n}",
                    direction="in" if n % 2 == 0 else "out",
                    sender_type="client" if n % 2 == 0 else "operator",
                    body=f"старое сообщение {n}",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=T0 - timedelta(days=60, minutes=n),
                )
            )
        await s.commit()
        return SimpleNamespace(
            account=account,
            client_id=cl.id,
            current=current.id,
            past=past.id,
            foreign=foreign.id,
        )


async def audit_rows(db_sessionmaker, action: str) -> list[AuditLog]:
    async with db_sessionmaker() as s:
        return list((await s.execute(select(AuditLog).where(AuditLog.action == action))).scalars())


def details(row: AuditLog) -> dict[str, Any]:
    """`details` в модели nullable — в этих action'ах он обязан быть (06 §0.3)."""
    assert row.details is not None
    return row.details


async def feed_bodies(client, tokens, conversation_id, role="manager"):
    r = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages", headers=auth(tokens, role)
    )
    assert r.status_code == 200, r.text
    return r.json()["items"]


# --- статус (01 §5.4) --------------------------------------------------------


async def test_status_change_writes_system_message_audit_and_ws(
    client, tokens, seed, db_sessionmaker, redis, users_by_role
):
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    r = await client.patch(
        f"/api/v1/conversations/{seed.current}/status",
        json={"status": "in_progress"},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "in_progress"
    # «кто взял — тот и ведёт»: у неназначенного диалога ответственным стал автор
    assert body["assignee"]["id"] == str(users_by_role["manager"].id)

    items = await feed_bodies(client, tokens, seed.current)
    system = [m for m in items if m["direction"] == "system"]
    assert len(system) == 1
    assert system[0]["sender_type"] == "system"
    assert system[0]["body"].startswith("Статус: Новый → В работе.")
    assert "взял(а) в работу" in system[0]["body"]

    changed = await audit_rows(db_sessionmaker, "conversation.status_changed")
    assert len(changed) == 1
    assert details(changed[0])["from"] == "new"
    assert details(changed[0])["to"] == "in_progress"
    assert details(changed[0])["by"] == "operator"
    assert changed[0].user_id == users_by_role["manager"].id
    # автозахват пишет и «назначено» (06 §0.3) — иначе метрика «принято» слепа
    assigned = await audit_rows(db_sessionmaker, "conversation.assigned")
    assert [details(a)["by"] for a in assigned] == ["self"]

    events = await drain_events(pubsub)
    by_type = {e["type"]: e for e in events}
    assert set(by_type) == {"message:new", "conversation:updated"}
    assert by_type["message:new"]["data"]["message"]["direction"] == "system"
    assert by_type["conversation:updated"]["data"]["patch"]["status"] == "in_progress"


async def test_status_same_value_rejected(client, tokens, seed):
    r = await client.patch(
        f"/api/v1/conversations/{seed.current}/status",
        json={"status": "new"},
        headers=auth(tokens),
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "same_status"


async def test_closed_to_new_by_hand_rejected_but_back_to_work_allowed(client, tokens, seed):
    """01 §5.4: закрытый в «Новые» возвращает только клиент своим сообщением."""
    r = await client.patch(
        f"/api/v1/conversations/{seed.past}/status",
        json={"status": "new"},
        headers=auth(tokens),
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "manual_reopen_forbidden"

    r2 = await client.patch(
        f"/api/v1/conversations/{seed.past}/status",
        json={"status": "in_progress"},
        headers=auth(tokens),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["status"] == "in_progress"


async def test_status_bad_enum_is_validation_error(client, tokens, seed):
    r = await client.patch(
        f"/api/v1/conversations/{seed.current}/status",
        json={"status": "archived"},
        headers=auth(tokens),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "validation_error"


async def test_head_may_change_status_but_observer_may_not(client, tokens, seed):
    """Руководитель управляет диалогами (отправка — другое право, 01 §12)."""
    r = await client.patch(
        f"/api/v1/conversations/{seed.current}/status",
        json={"status": "closed"},
        headers=auth(tokens, "head"),
    )
    assert r.status_code == 200, r.text
    # head не умеет отвечать клиенту — «взять в работу» на него не срабатывает
    assert r.json()["assignee"] is None

    r2 = await client.patch(
        f"/api/v1/conversations/{seed.current}/status",
        json={"status": "in_progress"},
        headers=auth(tokens, "observer"),
    )
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == "forbidden"


# --- назначение и передача (01 §5.5) -----------------------------------------


async def test_transfer_creates_system_message_audit_and_assigned_event(
    client, tokens, seed, db_sessionmaker, redis, users_by_role, make_user, в_сети
):
    receiver = await make_user(
        "petr@leadchat.test", role="manager", full_name="Пётр Ковалёв", department="ОКК"
    )
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(receiver)
    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(receiver)
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(receiver.id), "comment": "торгуется, дай скидку до 10%"},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    payload = r.json()
    assert payload["conversation"]["assignee"]["full_name"] == "Пётр Ковалёв"
    assert payload["conversation"]["status"] == "in_progress"
    system = payload["system_message"]
    assert system["direction"] == "system"
    assert system["body"] == (
        "Диалог передан: Test User → Пётр Ковалёв. Комментарий: торгуется, дай скидку до 10%"
    )

    feed = await feed_bodies(client, tokens, seed.current)
    assert system["id"] in {m["id"] for m in feed}

    assigned = await audit_rows(db_sessionmaker, "conversation.assigned")
    assert len(assigned) == 1
    assert assigned[0].details == {
        "assignee_id": str(receiver.id),
        "prev_assignee_id": None,
        "by": "transfer",
        "comment": True,
    }
    assert assigned[0].user_id == users_by_role["manager"].id
    # смена new → in_progress фиксируется отдельной строкой (06 §0.3)
    assert len(await audit_rows(db_sessionmaker, "conversation.status_changed")) == 1

    by_type = {e["type"]: e for e in await drain_events(pubsub)}
    assert set(by_type) == {"message:new", "conversation:assigned", "conversation:updated"}
    data = by_type["conversation:assigned"]["data"]
    # Отдел приезжает вместе с именем (04.09): кадр рисует строку списка и
    # шапку ленты у всех тринадцати, и подписать человека там надо так же,
    # как в ответе ручки — иначе один Пётр выглядит как двое.
    assert data["assignee"] == {
        "id": str(receiver.id),
        "full_name": "Пётр Ковалёв",
        "department": "ОКК",
    }
    assert data["assigned_by"]["id"] == str(users_by_role["manager"].id)
    assert data["comment"] == "торгуется, дай скидку до 10%"
    # is_for_you подставляет хаб каждому получателю — в Pub/Sub его нет
    assert "is_for_you" not in data


async def test_self_assign_and_unassign_wording(client, tokens, seed, users_by_role):
    me = users_by_role["manager"]
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(me.id)},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assert r.json()["system_message"]["body"] == "Диалог взят в работу: Test User"

    r2 = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": None, "comment": "верну в общую очередь"},
        headers=auth(tokens),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["conversation"]["status"] == "new"  # 01 §5.5
    assert r2.json()["conversation"]["assignee"] is None
    assert r2.json()["system_message"]["body"] == (
        "Диалог возвращён в «Новые». Test User. Комментарий: верну в общую очередь"
    )


async def test_закрытие_отпускает_метку_очереди(client, tokens, seed, db_sessionmaker):
    """⚠ АУДИТ 30.08: закрытие снимало ожидание, непрочитанные, передачу и
    закрепления — но не `offered_at`.

    Миграция 0007 обещает обратное («закрытым offered_at не ставим вовсе»), и
    под это обещание построены частичные индексы очереди. На бою так накопилось
    2406 закрытых диалогов с меткой: принятие руками её намеренно не снимает,
    значит носит каждый разобранный диалог. Живая очередь фильтруется полным
    условием и не врала — опасность в путях, возвращающих диалог в очередь без
    `enter_queue`: они втаскивали ожидание недельной давности, и такой диалог
    вставал ПЕРВЫМ в списке, отсортированном по `offered_at`.
    """
    # Боевой путь: диалог предложили очереди, человек его ПРИНЯЛ (принятие
    # метку намеренно не снимает — в отличие от автораздачи) и теперь закрывает.
    async with db_sessionmaker() as s2:
        conv = await s2.get(Conversation, seed.current)
        me = (
            await s2.execute(select(Conversation).where(Conversation.id == seed.past))
        ).scalar_one()
        conv.offered_at = T0
        conv.escalated_at = T0
        conv.assignee_id = me.assignee_id
        conv.claimed_by_id = me.assignee_id
        conv.status = "in_progress"
        await s2.commit()

    r = await client.patch(
        f"/api/v1/conversations/{seed.current}/status",
        json={"status": "closed"},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s2:
        conv = await s2.get(Conversation, seed.current)
        assert conv.offered_at is None, (
            "метка очереди осталась на закрытом — инвариант миграции 0007 нарушен"
        )
        assert conv.escalated_at is None, "метка эскалации пережила закрытие"


async def test_закрытие_глушит_бота(client, tokens, seed, db_sessionmaker):
    """⚠ АУДИТ 30.08: закрытие не трогало bot_active и ожидание бота.

    Диалог, который ведёт бот, в очереди не значится, поэтому закрыть его может
    любой оператор («спам, закрываю»). Дальше отложенный дедлайн `ask`
    просыпался через 25 минут, видел `bot_active=True` и совпавший токен — а
    последней строкой ленты была системная запись о закрытии, то есть заслон
    «клиент уже ответил» её не считал ответом. Клиент ЗАКРЫТОГО диалога получал
    дожим «Ну что, подскажете?», либо ветка передачи воскрешала обращение.

    Глушим `mute`, а не просто снимаем признак: закрытие переоткрываемо, и без
    этого вернувшийся клиент попал бы на второй круг сценария.
    """
    async with db_sessionmaker() as s2:
        conv = await s2.get(Conversation, seed.current)
        past = await s2.get(Conversation, seed.past)
        conv.assignee_id = past.assignee_id
        conv.status = "in_progress"
        conv.bot_active = True
        await s2.commit()

    r = await client.patch(
        f"/api/v1/conversations/{seed.current}/status",
        json={"status": "closed"},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s2:
        conv = await s2.get(Conversation, seed.current)
        assert conv.bot_active is False, (
            "бот остался активным на закрытом — дедлайн разбудит его через 25 минут"
        )
        assert (conv.bot_vars or {}).get("muted"), (
            "бот не заглушён навсегда: вернувшийся клиент попадёт на второй круг сценария"
        )


async def test_назначение_не_воскрешает_закрытый(client, tokens, seed, users_by_role):
    """⚠ АУДИТ 30.08: статус ставился безусловно, «closed» никто не смотрел.

    Снятие ответственного у закрытого давало переход closed → new, который
    матрица переходов запрещает («возвращает только воркер входящих»), и следом
    `return_to_queue`. У всех диспетчеров живьём появлялась строка клиента,
    который давно не писал, — первой в очереди, — и сторож начинал лестницу
    эскалаций админам.
    """
    r = await client.post(
        f"/api/v1/conversations/{seed.past}/assign",
        json={"assignee_id": None},
        headers=auth(tokens),
    )
    assert r.status_code == 422, f"закрытый диалог воскрешён назначением (ответ {r.status_code})"
    assert r.json()["error"]["code"] == "conversation_closed"

    r2 = await client.post(
        f"/api/v1/conversations/{seed.past}/assign",
        json={"assignee_id": str(users_by_role["manager"].id)},
        headers=auth(tokens),
    )
    assert r2.status_code == 422, "закрытый диалог молча переоткрыт «взять себе»"


async def test_assigning_a_waiting_dialog_clears_it_from_everyone_queue(
    client, tokens, seed, users_by_role, redis, db_sessionmaker, в_сети
):
    """Руководитель разбирает затор передачей — очередь обязана это увидеть.

    Передача диалога из очереди для остальных операторов — то же событие, что
    чужое «Принять»: строка исчезает. Без кадра она осталась бы в двенадцати
    очередях, и следующий клик по «Принять» вернул бы 409 по диалогу, в
    котором уже работает Анна.
    """
    async with db_sessionmaker() as s, s.begin():
        conv = await s.get(Conversation, seed.current)
        conv.offered_at = T0

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    manager = users_by_role["manager"]
    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(manager)
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(manager.id)},
        headers=auth(tokens, "head"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["conversation"]["in_inbox"] is False

    events = await drain_events(pubsub)
    claimed = next(e for e in events if e["type"] == INBOX_CLAIMED)
    assert claimed["data"]["claimed_by"]["id"] == str(manager.id)
    assert claimed["data"]["conversation_patch"]["in_inbox"] is False
    # Дельта строки списка несёт тот же признак — на случай, если очередь
    # у получателя кадра не открыта.
    updated = next(e for e in events if e["type"] == "conversation:updated")
    assert updated["data"]["patch"]["in_inbox"] is False


async def test_unassigning_puts_the_dialog_back_into_everyone_queue(
    client, tokens, seed, users_by_role, redis, db_sessionmaker, в_сети
):
    """Обратное действие: сняли ответственного — строка появилась у всех."""
    manager = users_by_role["manager"]
    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(manager)
    await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(manager.id)},
        headers=auth(tokens, "head"),
    )
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": None},
        headers=auth(tokens, "head"),
    )
    assert r.status_code == 200, r.text
    assert r.json()["conversation"]["in_inbox"] is True

    released = next(e for e in await drain_events(pubsub) if e["type"] == INBOX_RELEASED)
    # Диалог едет ЦЕЛИКОМ: у подключившегося после передачи строки нет вовсе.
    assert released["data"]["conversation"]["id"] == str(seed.current)
    assert released["data"]["conversation"]["in_inbox"] is True
    assert released["data"]["released_by"]["full_name"] == users_by_role["head"].full_name


async def test_unassign_returns_the_dialog_to_the_queue(
    client, tokens, seed, users_by_role, db_sessionmaker
):
    """Снять ответственного — значит вернуть диалог В ОЧЕРЕДЬ, а не обнулить поле.

    Без этого диалог, который руководитель снял с уехавшего менеджера, остаётся
    с `claimed_by_id` этого менеджера, в очередь не попадает и висит в «Новых»
    невидимым: он есть, он ничей, и взять его не может никто — ровно то
    состояние, ради которого 7.1 и делалась.
    """
    me = users_by_role["manager"]
    await client.post(
        f"/api/v1/conversations/{seed.current}/claim", headers=auth(tokens), json=None
    )
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.current)
        assert conv.claimed_by_id == me.id

    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": None},
        headers=auth(tokens, "head"),  # руководитель разбирает затор (01 §5.5)
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.current)
    assert conv.assignee_id is None
    assert conv.claimed_by_id is None, "принявший остался — диалог не вернётся в очередь"
    assert conv.claimed_at is None
    assert conv.offered_at is not None
    assert inbox.is_waiting(conv)

    # И он действительно виден в очереди оператору, а не только «в базе чисто».
    r2 = await client.get("/api/v1/inbox", headers=auth(tokens))
    assert [i["id"] for i in r2.json()["items"]] == [str(seed.current)]


@pytest.mark.parametrize(
    "role,reason", [("head", "assignee_cannot_chat"), ("observer", "assignee_cannot_chat")]
)
async def test_assign_to_non_chatting_role_rejected(
    client, tokens, seed, users_by_role, role, reason
):
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(users_by_role[role].id)},
        headers=auth(tokens),
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == reason


async def test_assign_inactive_and_unknown_and_same(client, tokens, seed, make_user, users_by_role):
    sleeping = await make_user("sleep@leadchat.test", role="manager", is_active=False)
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(sleeping.id)},
        headers=auth(tokens),
    )
    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "assignee_inactive"

    r2 = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(uuid.uuid4())},
        headers=auth(tokens),
    )
    assert r2.status_code == 422
    assert r2.json()["error"]["details"]["reason"] == "assignee_not_found"

    r3 = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": None},
        headers=auth(tokens),
    )
    assert r3.status_code == 422  # диалог и так без ответственного
    assert r3.json()["error"]["details"]["reason"] == "same_assignee"


async def test_assign_unknown_conversation_is_404(client, tokens, seed, users_by_role):
    r = await client.post(
        f"/api/v1/conversations/{uuid.uuid4()}/assign",
        json={"assignee_id": str(users_by_role["manager"].id)},
        headers=auth(tokens),
    )
    assert r.status_code == 404


async def test_observer_cannot_assign(client, tokens, seed, users_by_role):
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(users_by_role["manager"].id)},
        headers=auth(tokens, "observer"),
    )
    assert r.status_code == 403


@pytest.mark.parametrize("role", ["admin", "head", "manager"])
async def test_manage_endpoints_allowed_for_admin_head_manager(
    client, tokens, seed, users_by_role, role, в_сети
):
    """Полная ALLOW-ветка матрицы 01 §13 для двух управляющих ручек.

    В `tests/unit/test_rbac.py` эти строки жить не могут (ALLOW-запрос без тела
    вернул бы 400) — поэтому они здесь, в COVERED_ELSEWHERE-стиле.
    """
    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(users_by_role["manager"])
    assign = await client.post(
        f"/api/v1/conversations/{seed.foreign}/assign",
        json={"assignee_id": str(users_by_role["manager"].id)},
        headers=auth(tokens, role),
    )
    assert assign.status_code == 200, assign.text

    status = await client.patch(
        f"/api/v1/conversations/{seed.foreign}/status",
        json={"status": "closed"},
        headers=auth(tokens, role),
    )
    assert status.status_code == 200, status.text


async def test_anonymous_cannot_manage(client, seed, users_by_role):
    assert (
        await client.patch(
            f"/api/v1/conversations/{seed.current}/status", json={"status": "closed"}
        )
    ).status_code == 401
    assert (
        await client.post(
            f"/api/v1/conversations/{seed.current}/assign",
            json={"assignee_id": str(users_by_role["manager"].id)},
        )
    ).status_code == 401


async def test_transferred_flag_lights_for_receiver_and_goes_out_on_open(
    client, tokens, seed, make_user, в_сети
):
    """⚑ «передан вам» (01 §5.1, 11 §2.1) — пер-юзерное состояние."""
    from app.core.security import create_access_token

    receiver = await make_user("anna@leadchat.test", role="manager", full_name="Анна Смирнова")
    receiver_auth = {
        "Authorization": f"Bearer {create_access_token(user_id=str(receiver.id), role='manager')}"
    }

    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(receiver)
    await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(receiver.id)},
        headers=auth(tokens),
    )

    listing = (await client.get("/api/v1/conversations", headers=receiver_auth)).json()
    row = next(i for i in listing["items"] if i["id"] == str(seed.current))
    assert row["transferred_to_me"] is True
    # у передавшего флажка нет — это не его диалог
    mine = (await client.get("/api/v1/conversations", headers=auth(tokens))).json()
    assert (
        next(i for i in mine["items"] if i["id"] == str(seed.current))["transferred_to_me"] is False
    )

    detail = (
        await client.get(f"/api/v1/conversations/{seed.current}", headers=receiver_auth)
    ).json()
    assert detail["transferred_to_me"] is True  # флаг ещё виден в ответе на открытие

    after = (await client.get("/api/v1/conversations", headers=receiver_auth)).json()
    assert (
        next(i for i in after["items"] if i["id"] == str(seed.current))["transferred_to_me"]
        is False
    )


# --- /users/assignable (01 §3.1) ---------------------------------------------


async def test_assignable_users_lists_only_answering_roles(
    client, tokens, users_by_role, make_user, redis
):
    offline_one = await make_user("off@leadchat.test", role="manager", full_name="Олег Иванов")
    await make_user("gone@leadchat.test", role="manager", is_active=False, full_name="Ушедший")
    await redis.set(f"presence:{users_by_role['manager'].id}", "online")

    r = await client.get("/api/v1/users/assignable", headers=auth(tokens))
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    assert {i["role"] for i in items} == {"admin", "manager"}  # head/observer отвечать не могут
    names = [i["full_name"] for i in items]
    assert "Ушедший" not in names  # только активные
    assert names == sorted(names)  # стабильный порядок для модалки

    online = {i["id"]: i["is_online"] for i in items}  # по id: тёзки в базе реальны
    assert online[str(users_by_role["manager"].id)] is True  # presence:{user_id} есть
    assert online[str(users_by_role["admin"].id)] is False
    assert online[str(offline_one.id)] is False


async def test_assignable_users_can_include_deactivated(client, tokens, make_user):
    """11 §2.5.2: руководителю нужен уволенный в фильтре «Менеджер ▾».

    Деактивация диалоги не переназначает (01 §3.5), поэтому осиротевшие
    диалоги ищутся именно по отключённому сотруднику. Целью назначения он всё
    равно не станет — `422 assignee_inactive` (01 §5.5).
    """
    gone = await make_user(
        "fired@leadchat.test", role="manager", is_active=False, full_name="Ушедший Сотрудник"
    )
    default = (await client.get("/api/v1/users/assignable", headers=auth(tokens))).json()["items"]
    assert str(gone.id) not in {i["id"] for i in default}
    assert all(i["is_active"] is True for i in default)  # поведение по умолчанию прежнее

    with_inactive = (
        await client.get(
            "/api/v1/users/assignable", params={"include_inactive": "true"}, headers=auth(tokens)
        )
    ).json()["items"]
    row = next(i for i in with_inactive if i["id"] == str(gone.id))
    assert row["is_active"] is False  # фронт рисует пометку «отключён»


async def test_assignable_users_hide_the_smoke_robot(client, tokens, make_user):
    """Служебный smoke-пользователь — не сотрудник. Признак — колонка, не домен.

    Переучен 15 августа: раньше робот заводился голым доменом `.local`, и тест
    закреплял правило, признанное неверным ещё 12 августа, — по домену списки
    прятали и настоящих людей. Обратная сторона (человек на `.local` ВИДЕН)
    заперта в test_operator_pool.py.
    """
    robot = await make_user(
        "smoke@leadpartner.local", role="manager", full_name="Smoke Robot", is_service=True
    )
    for params in ({}, {"include_inactive": "true"}):
        items = (
            await client.get("/api/v1/users/assignable", params=params, headers=auth(tokens))
        ).json()["items"]
        assert str(robot.id) not in {i["id"] for i in items}


async def test_assignable_users_rbac(client, tokens):
    assert (
        await client.get("/api/v1/users/assignable", headers=auth(tokens, "head"))
    ).status_code == 200
    r = await client.get("/api/v1/users/assignable", headers=auth(tokens, "observer"))
    assert r.status_code == 403
    assert (await client.get("/api/v1/users/assignable")).status_code == 401


# --- деталь и история клиента (01 §5.2, §5.6) --------------------------------


async def test_detail_carries_client_card_fields(client, tokens, seed):
    detail = (
        await client.get(f"/api/v1/conversations/{seed.current}", headers=auth(tokens))
    ).json()
    assert detail["client"] == {
        "id": str(seed.client_id),
        "name": "Иван Петров",
        # Идентификатор клиента на Авито — для блока «Клиент на Авито» с
        # кнопкой копирования (требование владельца 5 от 11 августа).
        "external_id": "7001",
        # ⚠ ЗДЕСЬ СТОЯЛО «ссылки на профиль Авито не даёт: собрать её не из
        # чего». Это было верно на день написания и опровергнуто 02.09: Авито
        # присылает `public_user_profile` с ключами
        # `{avatar, item_id, url, user_id}`, и ссылка доехала до 186 карточек из
        # 186, про которые успели спросить.
        #
        # У ЭТОГО клиента она пуста по другой причине — про него ещё не
        # спрашивали, что и говорит соседнее поле. Ссылка НЕ выводится из
        # `external_id` («7001» здесь заведомо не настоящий ключ профиля): она
        # только читается из того, что прислал Авито, — см.
        # `test_client_profile_url_is_never_invented`.
        "profile_url": None,
        # Спрашивали ли мы про профиль. Отличает «ещё не знаем» от «Авито не
        # дал»: карточка обязана говорить об этом разное, первое пройдёт само
        # через секунду после открытия диалога, второе не пройдёт никогда.
        "profile_checked": False,
        "phone": "+79261234567",
        # Фото из Авито (15.08): у заготовки его нет — null, интерфейс рисует
        # инициалы. Ключ обязан присутствовать: карточка читает его без «?.».
        "avatar_url": None,
        "avito_rating": 4.9,
        # Признак чёрного списка едет в КАЖДОМ диалоге (docs/19): оператор
        # должен видеть пометку сразу при открытии, а не догружать по кнопке.
        "blocked": False,
        "blocked_reason": None,
        # Межканальная склейка (требование владельца 6 от 11 августа). У этого
        # клиента оба диалога на одном аккаунте, поэтому склейки нет — и поле
        # честно `null`, а не «предположительно».
        "link_confidence": None,
        "link_phone_conflict": False,
    }
    assert detail["item"] == {
        "title": "Ремонт iPhone 13",
        "url": "https://avito.ru/item/1",
        "price": "от 1500 ₽",
        # Город объявления (требования 3-4 от 11 августа). Здесь его нет:
        # ссылка вида /item/1 города не содержит, и выдумывать его нельзя —
        # у диалога честно «город не определён».
        "city_slug": None,
        "city_name": None,
        "city_region": None,
        "city_tz": None,
    }
    assert detail["status"] == "new"
    assert detail["assignee"] is None
    assert detail["tags"] == []
    assert detail["first_client_at"] is not None
    assert detail["client_conversations_count"] == 2  # текущий + прошлый
    assert detail["external_chat_id"] == "chat-current"


async def test_client_history_returns_past_dialogs_only(client, tokens, seed, users_by_role):
    r = await client.get(
        f"/api/v1/conversations/{seed.current}/client-history", headers=auth(tokens)
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["client"] == {"id": str(seed.client_id), "name": "Иван Петров"}
    assert [i["id"] for i in body["items"]] == [str(seed.past)]  # без текущего и без чужого
    past = body["items"][0]
    assert past["status"] == "closed"
    assert past["item"] == {"title": "Ремонт MacBook Air"}
    # Канал прошлого обращения — с идентификатором рядом с названием: названия
    # каналов у заказчика человеческие («Тимофей», «Дамир») и повторяются, так
    # что «тот же канал, что открыт сейчас» интерфейс считает по id.
    assert past["account"] == {"id": str(seed.account.id), "title": "LP-Test"}
    # Ответственный прошлого обращения — общая ссылка на человека (04.09):
    # та же сборка, что в шапке ленты, вместе с отделом. Отдел у seed-строки
    # не заполнен — и тогда это честный `null`, а не пустая строка.
    assert past["assignee"] == {
        "id": str(users_by_role["manager"].id),
        "full_name": "Test User",
        "department": None,
    }
    assert past["messages_count"] == 3
    assert past["last_message_at"] is not None
    # Оба диалога этого клиента — на одном канале, поэтому в сводке один канал
    # и никакой склейки.
    assert body["summary"] == {
        "channels": [{"id": str(seed.account.id), "title": "LP-Test"}],
        "link_confidence": None,
        "link_phone_conflict": False,
        "cross_account_since": None,
    }


async def test_client_history_available_to_observer(client, tokens, seed):
    r = await client.get(
        f"/api/v1/conversations/{seed.current}/client-history", headers=auth(tokens, "observer")
    )
    assert r.status_code == 200  # чтение доступно всем ролям (01 §13)


# --- поиск (01 §5.1) ---------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    ["+79261234567", "8 926 123-45-67", "8(926)123 45 67", "9261234567", "1234567"],
)
async def test_search_by_phone_in_any_format(client, tokens, seed, query):
    r = await client.get("/api/v1/conversations", params={"q": query}, headers=auth(tokens))
    assert r.status_code == 200, r.text
    assert str(seed.current) in [i["id"] for i in r.json()["items"]]


async def test_short_digit_query_does_not_match_every_phone(client, tokens, seed):
    """«iPhone 13» не должен превращаться в LIKE '%13%' по телефонам."""
    r = await client.get("/api/v1/conversations", params={"q": "13"}, headers=auth(tokens))
    assert [i["id"] for i in r.json()["items"]] == []


async def test_search_by_name_and_message_text(client, tokens, seed):
    by_name = await client.get("/api/v1/conversations", params={"q": "Иван"}, headers=auth(tokens))
    assert [i["id"] for i in by_name.json()["items"]] == [str(seed.current)]

    by_text = await client.get(
        "/api/v1/conversations", params={"q": "разбит"}, headers=auth(tokens)
    )
    assert str(seed.current) in [i["id"] for i in by_text.json()["items"]]


async def test_search_combines_with_tab_and_pagination(client, tokens, seed):
    # tab=closed + текст старого диалога
    closed = await client.get(
        "/api/v1/conversations",
        params={"q": "старое", "tab": "closed"},
        headers=auth(tokens),
    )
    assert [i["id"] for i in closed.json()["items"]] == [str(seed.past)]
    # тот же запрос во вкладке «Все» (не закрытые) ничего не находит
    active = await client.get(
        "/api/v1/conversations", params={"q": "старое", "tab": "all"}, headers=auth(tokens)
    )
    assert active.json()["items"] == []
    assert active.json()["page"]["total"] == 0

    page = await client.get(
        "/api/v1/conversations",
        params={"tab": "all", "limit": 1, "offset": 1},
        headers=auth(tokens),
    )
    body = page.json()
    assert body["page"] == {"limit": 1, "offset": 1, "total": 2}  # current + foreign
    assert len(body["items"]) == 1


async def test_tab_mine_excludes_closed(client, tokens, seed, users_by_role):
    """01 §5.1: «Мои» — назначенные мне и НЕ закрытые (past назначен мне и закрыт)."""
    r = await client.get("/api/v1/conversations", params={"tab": "mine"}, headers=auth(tokens))
    assert [i["id"] for i in r.json()["items"]] == []

    await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(users_by_role["manager"].id)},
        headers=auth(tokens),
    )
    r2 = await client.get("/api/v1/conversations", params={"tab": "mine"}, headers=auth(tokens))
    assert [i["id"] for i in r2.json()["items"]] == [str(seed.current)]


async def test_system_message_does_not_become_row_preview(client, tokens, seed, users_by_role):
    """Служебная запись не выталкивает переписку из превью строки списка."""
    before = (await client.get("/api/v1/conversations", headers=auth(tokens))).json()
    row_before = next(i for i in before["items"] if i["id"] == str(seed.current))

    await client.patch(
        f"/api/v1/conversations/{seed.current}/status",
        json={"status": "in_progress"},
        headers=auth(tokens),
    )
    after = (await client.get("/api/v1/conversations", headers=auth(tokens))).json()
    row_after = next(i for i in after["items"] if i["id"] == str(seed.current))
    assert row_after["last_message"] == row_before["last_message"]
    assert row_after["last_message_at"] == row_before["last_message_at"]


# --- `details.by` у conversation.assigned (06 §0.3) --------------------------


async def test_head_reassignment_is_logged_as_head_not_transfer(
    client, tokens, users_by_role, db_sessionmaker, seed, в_сети
):
    """Словарь `by` — self|transfer|head|auto.

    Раньше код знал только self/transfer, и переназначение руководителем
    попадало в журнал как «Диалог передан коллеге» вместо «Диалог переназначен
    руководителем». На метрики не влияет (статистика читает `details->>'by'`
    только у status_changed), но журнал показывал неправду.
    """
    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(users_by_role["manager"])
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(users_by_role["manager"].id)},
        headers=auth(tokens, "head"),
    )
    assert r.status_code == 200, r.text
    assigned = await audit_rows(db_sessionmaker, "conversation.assigned")
    assert [details(a)["by"] for a in assigned] == ["head"]


async def test_manager_transfer_to_a_colleague_is_still_transfer(
    client, tokens, users_by_role, make_user, db_sessionmaker, seed, в_сети
):
    receiver = await make_user("colleague@leadchat.test", role="manager", full_name="Коллега")
    # Получатель в сети: передавать в офлайн сервер не даёт (28.08).
    await в_сети(receiver)
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(receiver.id)},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assigned = await audit_rows(db_sessionmaker, "conversation.assigned")
    assert [details(a)["by"] for a in assigned] == ["transfer"]


async def test_taking_a_dialog_for_yourself_is_self(
    client, tokens, users_by_role, db_sessionmaker, seed
):
    r = await client.post(
        f"/api/v1/conversations/{seed.current}/assign",
        json={"assignee_id": str(users_by_role["manager"].id)},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assigned = await audit_rows(db_sessionmaker, "conversation.assigned")
    assert [details(a)["by"] for a in assigned] == ["self"]


async def test_reopening_a_closed_dialog_gives_it_to_whoever_pressed(
    client, tokens, seed, db_sessionmaker, users_by_role
):
    """Закрытый диалог ничей: вернул в работу — значит взял себе.

    ⚠ ЖАЛОБЫ ВЛАДЕЛЬЦА 03.09, ДВЕ ПОДРЯД: «когда диалог закрыт, то он по факту
    не чей… сейчас из-за этого себе в работу нельзя забрать диалог» и, после
    первой правки, «взял в работу и снова так же в „Мои" диалог не появился».

    ⚠ ЖМЁТ АДМИНИСТРАТОР, А НЕ МЕНЕДЖЕР, И ЭТО ГЛАВНОЕ В ТЕСТЕ. Соседний
    `test_closed_to_new_by_hand_rejected_but_back_to_work_allowed` переоткрывает
    тот же диалог менеджером — то есть тем, кто в нём и записан ответственным
    (фикстура `seed`). У такого теста «ответственный остался прежним» и
    «ответственный стал нажавшим» дают ОДИН И ТОТ ЖЕ результат, поэтому он
    зеленел и до правки, и после. Различает их только чужой нажавший.

    Замер боя за 30 суток: 115 переоткрытий, 21 из них — чужого диалога.
    """
    admin = users_by_role["admin"]
    prev_owner = users_by_role["manager"]
    # ⚠ ИМЯ ПРЕЖНЕГО ХОЗЯИНА ДЕЛАЕМ РАЗЛИЧИМЫМ, И ЭТО НЕ УКРАШЕНИЕ. Фикстура
    # заводит ВСЕХ пользователей с одним `full_name` («Test User»), поэтому
    # проверка «имя прежнего хозяина есть в ленте» совпадала бы с именем самого
    # нажавшего и зеленела бы при полностью убранной приписке — диверсия это и
    # показала.
    async with db_sessionmaker() as s:
        владелец = await s.get(User, prev_owner.id)
        владелец.full_name = "Прежний Хозяин"
        await s.commit()

    r = await client.patch(
        f"/api/v1/conversations/{seed.past}/status",
        json={"status": "in_progress"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.past)
        assert conv.assignee_id == admin.id, "диалог не достался тому, кто вернул его в работу"
        # Держателем очереди диалог считает `claimed_by_id or assignee_id`: без
        # этой строки новый хозяин не смог бы вернуть в очередь свой же диалог.
        assert conv.claimed_by_id == admin.id

        # Переход владения виден в ЛЕНТЕ, а не только в аудите: прежний хозяин
        # открывает диалог и читает, куда делась его работа.
        rows = (
            (
                await s.execute(
                    select(Message)
                    .where(Message.conversation_id == seed.past, Message.direction == "system")
                    .order_by(Message.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        assert rows, "системной записи о смене статуса нет"
        assert "взял(а) в работу" in (rows[0].body or "")
        assert "(вёл(а) Прежний Хозяин)" in (rows[0].body or ""), (
            "в ленте не сказано, кто вёл диалог до этого"
        )

        # `prev_assignee_id` в аудите был литеральным None — правдой ровно до
        # перехода владения. Колонка «Принято» в статистике считает диалоги по
        # этому событию, поэтому оно обязано писаться при КАЖДОЙ смене хозяина.
        assigned = (
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.action == "conversation.assigned",
                        AuditLog.entity_id == str(seed.past),
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(assigned) == 1, "событие о смене владельца не записано"
        assert assigned[0].details["assignee_id"] == str(admin.id)
        assert assigned[0].details["prev_assignee_id"] == str(prev_owner.id)


async def test_reopening_by_head_leaves_the_dialog_with_its_owner(
    client, tokens, seed, db_sessionmaker, users_by_role
):
    """Руководитель отвечать клиентам не может — значит и владельцем не станет.

    Обратная половина правила. Без неё «переоткрытие отдаёт диалог нажавшему»
    молча назначило бы ответственным того, кому нечем отвечать: диалог оказался
    бы в «Моих» у человека без права писать клиенту.
    """
    r = await client.patch(
        f"/api/v1/conversations/{seed.past}/status",
        json={"status": "in_progress"},
        headers=auth(tokens, "head"),
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.past)
        assert conv.assignee_id == users_by_role["manager"].id, "руководитель забрал диалог себе"


async def test_status_change_of_a_live_dialog_never_moves_ownership(
    client, tokens, seed, db_sessionmaker, users_by_role
):
    """У НЕзакрытого диалога смена статуса ответственного не трогает.

    ⚠ ЭТО ГРАНИЦА ПРАВИЛА, А НЕ ПОВТОР СОСЕДНЕГО ТЕСТА. Убери из условия
    `previous == "closed"` — и перевод чужого живого диалога из «Ждёт клиента»
    обратно в работу отбирал бы его у коллеги, который сейчас за клавиатурой.
    """
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.current)
        conv.status = "waiting_client"
        conv.assignee_id = users_by_role["manager"].id
        await s.commit()

    r = await client.patch(
        f"/api/v1/conversations/{seed.current}/status",
        json={"status": "in_progress"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.current)
        assert conv.assignee_id == users_by_role["manager"].id, "живой диалог сменил хозяина"
