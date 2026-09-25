"""WS-контур (01 §11, 08 §5): одноразовый тикет, фильтрация Hub'а по правам,
клиентские кадры (ping/subscribe/typing), control:revoked, персонализация."""

import json
import uuid
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.rbac import ROLE_PERMISSIONS
from app.ws.hub import Hub, handle_client_frame
from tests.unit.conftest import drain_events


class StubWS:
    def __init__(self):
        self.sent: list[dict] = []
        self.closed: int | None = None

    async def send_text(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.closed = code


def stub_user(role: str, user_id: uuid.UUID | None = None) -> SimpleNamespace:
    return SimpleNamespace(id=user_id or uuid.uuid4(), full_name=f"Тест {role.title()}", role=role)


@pytest.fixture
def hub(redis) -> Hub:
    return Hub(redis)


def attach(hub: Hub, role: str, user_id: uuid.UUID | None = None):
    ws = StubWS()
    session = hub.attach(ws, stub_user(role, user_id))
    return ws, session


# --- тикет (01 §11.1) --------------------------------------------------------


async def test_ws_ticket_is_one_time(client, tokens, users_by_role, redis):
    r = await client.post(
        "/api/v1/ws/ticket", headers={"Authorization": f"Bearer {tokens['manager']}"}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ticket"].startswith("wst_")
    assert body["expires_in"] == 60

    key = f"ws_ticket:{body['ticket']}"
    ttl = await redis.ttl(key)
    assert 0 < ttl <= 60
    # GETDEL — строго одноразовый; в значении — кому и когда выдан.
    user_id, _, issued = (await redis.getdel(key)).partition("|")
    assert user_id == str(users_by_role["manager"].id)
    assert issued.isdigit()
    assert await redis.getdel(key) is None


async def test_ws_ticket_requires_auth(client):
    r = await client.post("/api/v1/ws/ticket")
    assert r.status_code == 401


# --- фильтрация по правам (08 §5.3/5.4) -------------------------------------


def evt(type_: str, data: dict, meta: dict | None = None) -> dict:
    e = {"type": type_, "ts": "2026-08-04T10:00:00.000Z", "data": data}
    if meta:
        e["meta"] = meta
    return e


async def test_note_not_delivered_to_observer(hub):
    admin_ws, _ = attach(hub, "admin")
    observer_ws, _ = attach(hub, "observer")
    await hub.dispatch(
        evt("message:new", {"conversation_id": "c1", "message": {"direction": "note"}})
    )
    assert len(admin_ws.sent) == 1
    assert observer_ws.sent == []  # заметки observer'у не отдаются (01 §11.3)


@pytest.mark.parametrize("role", ["admin", "head", "manager"])
async def test_notes_go_to_every_role_holding_notes_read(hub, role):
    """Критерий выдачи заметки — матрица прав, а не имя роли.

    Отсечка по строке `role == "observer"` пропустила бы новую роль без
    `notes:read` (и сломалась бы от переименования observer'а), хотя
    HTTP-лента такую роль отсекает через has_permission (01 §6.1/§11.3).
    """
    assert "notes:read" in ROLE_PERMISSIONS[role]
    ws, _ = attach(hub, role)
    await hub.dispatch(
        evt("message:new", {"conversation_id": "c1", "message": {"direction": "note"}})
    )
    assert len(ws.sent) == 1


async def test_note_filter_follows_the_permission_matrix_not_the_role_name(hub):
    """Роль без `notes:read` заметок не получает, даже если она не observer."""
    ws, session = attach(hub, "observer")
    session.role = "trainee"  # гипотетическая новая роль: прав в матрице нет
    await hub.dispatch(
        evt("message:new", {"conversation_id": "c1", "message": {"direction": "note"}})
    )
    assert ws.sent == []


async def test_meta_only_user_reaches_only_that_users_sessions(hub):
    """Персональное состояние (read-маркер 01 §5.3) — только своим вкладкам."""
    me = uuid.uuid4()
    tab_one, _ = attach(hub, "manager", me)
    tab_two, _ = attach(hub, "manager", me)
    colleague_ws, _ = attach(hub, "manager")
    await hub.dispatch(
        evt(
            "conversation:updated",
            {"conversation_id": "c1", "patch": {"unread_count": 0}},
            meta={"only_user": str(me)},
        )
    )
    assert len(tab_one.sent) == 1
    assert len(tab_two.sent) == 1
    assert colleague_ws.sent == []  # чужой бейдж гасить нельзя


async def test_regular_message_delivered_to_all_roles(hub):
    sockets = [attach(hub, role)[0] for role in ("admin", "head", "manager", "observer")]
    await hub.dispatch(
        evt("message:new", {"conversation_id": "c1", "message": {"direction": "in"}})
    )
    assert all(len(ws.sent) == 1 for ws in sockets)


async def test_needs_reauth_only_for_admin(hub):
    admin_ws, _ = attach(hub, "admin")
    manager_ws, _ = attach(hub, "manager")
    await hub.dispatch(evt("account:needs_reauth", {"account_id": "a1"}))
    assert len(admin_ws.sent) == 1
    assert manager_ws.sent == []


async def test_meta_audience_admin(hub):
    admin_ws, _ = attach(hub, "admin")
    head_ws, _ = attach(hub, "head")
    await hub.dispatch(evt("notify", {"title": "История загружена"}, meta={"audience": "admin"}))
    assert len(admin_ws.sent) == 1
    assert head_ws.sent == []
    # meta — служебное поле, клиенту не уходит (01 §11.2)
    assert "meta" not in admin_ws.sent[0]


async def test_meta_exclude_user(hub):
    author_id = uuid.uuid4()
    author_ws, _ = attach(hub, "manager", author_id)
    other_ws, _ = attach(hub, "manager")
    await hub.dispatch(
        evt("typing", {"conversation_id": None}, meta={"exclude_user": str(author_id)})
    )
    assert author_ws.sent == []  # отправителю не возвращаем


async def test_typing_only_for_subscribed(hub):
    conv_id = uuid.uuid4()
    subscribed_ws, subscribed = attach(hub, "manager")
    subscribed.conversation_id = conv_id
    other_ws, _ = attach(hub, "manager")
    await hub.dispatch(evt("typing", {"conversation_id": str(conv_id), "source": "operator"}))
    assert len(subscribed_ws.sent) == 1
    assert other_ws.sent == []


async def test_control_revoked_closes_sessions(hub):
    user_id = uuid.uuid4()
    target_ws, _ = attach(hub, "manager", user_id)
    bystander_ws, _ = attach(hub, "manager")
    await hub.dispatch(evt("control:revoked", {"user_id": str(user_id), "code": 4403}))
    assert target_ws.closed == 4403
    assert bystander_ws.closed is None
    assert bystander_ws.sent == []  # control:* клиентам не транслируется


async def test_assigned_personalized_is_for_you(hub):
    assignee_id = uuid.uuid4()
    assignee_ws, _ = attach(hub, "manager", assignee_id)
    other_ws, _ = attach(hub, "manager")
    await hub.dispatch(
        evt(
            "conversation:assigned",
            {"conversation_id": "c1", "assignee": {"id": str(assignee_id)}},
        )
    )
    assert assignee_ws.sent[0]["data"]["is_for_you"] is True
    assert other_ws.sent[0]["data"]["is_for_you"] is False


# --- клиентские кадры (01 §11.4/11.5) ---------------------------------------


async def test_ping_pong_and_presence(hub, redis):
    ws, session = attach(hub, "manager")
    await handle_client_frame(session, json.dumps({"type": "ping", "data": {"n": 42}}), redis)
    assert ws.sent[-1]["type"] == "pong"
    assert ws.sent[-1]["data"] == {"n": 42}
    assert ws.sent[-1]["ts"].endswith("Z")
    # presence продлён (01 §11.6)
    assert await redis.zcard(f"presence:conns:{session.user_id}") == 1
    status = await redis.get(f"presence:{session.user_id}")
    assert status == "online"
    # Срок берём из настройки, а не числом: 27.08 он вырос с 90 до 210 секунд
    # вместе с таймаутом сторожа тишины, и зашитая девяностка сделала бы тест
    # сторожем СТАРОГО значения — он и упал первым, когда настройку подняли.
    assert 0 < await redis.ttl(f"presence:{session.user_id}") <= settings.ws_presence_ttl_seconds


async def test_subscribe_sets_conversation(hub, redis):
    _, session = attach(hub, "manager")
    conv_id = uuid.uuid4()
    await handle_client_frame(
        session,
        json.dumps({"type": "subscribe", "data": {"conversation_id": str(conv_id)}}),
        redis,
    )
    assert session.conversation_id == conv_id
    await handle_client_frame(
        session, json.dumps({"type": "subscribe", "data": {"conversation_id": None}}), redis
    )
    assert session.conversation_id is None


async def test_typing_frame_republished_without_author(hub, redis):
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    _, session = attach(hub, "manager")
    conv_id = str(uuid.uuid4())
    await handle_client_frame(
        session, json.dumps({"type": "typing", "data": {"conversation_id": conv_id}}), redis
    )
    (published,) = await drain_events(pubsub)
    assert published["type"] == "typing"
    assert published["data"]["conversation_id"] == conv_id
    assert published["meta"]["exclude_user"] == str(session.user_id)


@pytest.mark.parametrize("raw", ["not json", "{}", json.dumps({"type": "unknown"})])
async def test_bad_frames_answered_not_dropped(hub, redis, raw):
    ws, session = attach(hub, "manager")
    await handle_client_frame(session, raw, redis)
    assert ws.sent[-1] == {"type": "error", "data": {"code": "bad_frame"}}
    assert ws.closed is None  # сокет не рвём (01 §11.4)
