"""Наблюдатель и внутренние заметки (DESIGN §5.1, 01 §6.1/§6.4, §13).

Заметка (`direction='note'`) — внутренняя переписка сотрудников. Роль
observer не получает её **от сервера**: ни в ленте (`GET
/conversations/{id}/messages`), ни в курсорах и счётчиках `has_more`, ни в
превью строки списка, ни WS-кадром `message:new`. Скрытие в UI барьером не
считается (03 §5.1: «UI-скрытие — это UX, а не безопасность»).
"""

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from app.models import Message
from app.ws.hub import Hub
from tests.unit.test_ws import attach

NOTE_BODY = "Внутренняя заметка: клиент торгуется, ниже 12 000 не опускаемся"
ROLES_WITH_NOTES = ("admin", "head", "manager")


@pytest.fixture
async def conversation_with_a_note(db_sessionmaker, seed_conversation, users_by_role):
    """Лента: входящее клиента → заметка менеджера → ответ клиенту."""
    manager = users_by_role["manager"]
    base = datetime.now(UTC)
    async with db_sessionmaker() as session:
        session.add(
            Message(
                conversation_id=seed_conversation.conversation_id,
                direction="note",
                sender_type="operator",
                sender_user_id=manager.id,
                body=NOTE_BODY,
                attachments=[],
                delivery_status="delivered",
                created_at=base + timedelta(seconds=1),
            )
        )
        session.add(
            Message(
                conversation_id=seed_conversation.conversation_id,
                direction="out",
                sender_type="operator",
                sender_user_id=manager.id,
                body="Здравствуйте! Замена экрана — 12 000 ₽",
                attachments=[],
                delivery_status="delivered",
                created_at=base + timedelta(seconds=2),
            )
        )
        await session.commit()
    return seed_conversation


async def _feed(client, token: str, conversation_id, **params) -> dict:
    r = await client.get(
        f"/api/v1/conversations/{conversation_id}/messages",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )
    assert r.status_code == 200, r.text
    return r.json()


# --- лента -------------------------------------------------------------------


@pytest.mark.parametrize("role", ROLES_WITH_NOTES)
async def test_notes_are_visible_to_roles_with_notes_read(
    client, tokens, conversation_with_a_note, role
):
    body = await _feed(client, tokens[role], conversation_with_a_note.conversation_id)
    directions = [m["direction"] for m in body["items"]]
    assert directions == ["in", "note", "out"]
    assert any(m["body"] == NOTE_BODY for m in body["items"])


async def test_observer_never_receives_notes_in_the_feed(client, tokens, conversation_with_a_note):
    body = await _feed(client, tokens["observer"], conversation_with_a_note.conversation_id)
    directions = [m["direction"] for m in body["items"]]
    assert directions == ["in", "out"], "observer получил заметку — это утечка (01 §6.1)"
    assert NOTE_BODY not in str(body)


async def test_observer_pagination_never_leaks_notes_through_cursors(
    client, tokens, db_sessionmaker, seed_conversation
):
    """Заметка не должна «просвечивать» ни в has_more, ни в курсорах.

    Лента: in → note → note → out. Для observer заметок нет вовсе, значит
    страница из одного элемента, взятая с конца, обязана иметь ровно один
    элемент «до» — иначе `has_more_before` считался бы по чужим строкам.
    """
    base = datetime.now(UTC)
    async with db_sessionmaker() as session:
        for offset, direction in ((1, "note"), (2, "note"), (3, "out")):
            session.add(
                Message(
                    conversation_id=seed_conversation.conversation_id,
                    direction=direction,
                    sender_type="operator",
                    body=f"{direction}-{offset}",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=base + timedelta(seconds=offset),
                )
            )
        await session.commit()

    tail = await _feed(client, tokens["observer"], seed_conversation.conversation_id, limit=1)
    assert [m["direction"] for m in tail["items"]] == ["out"]
    assert tail["page"]["has_more_before"] is True
    assert tail["page"]["has_more_after"] is False

    head_page = await _feed(
        client,
        tokens["observer"],
        seed_conversation.conversation_id,
        before=tail["page"]["prev_cursor"],
        limit=10,
    )
    assert [m["direction"] for m in head_page["items"]] == ["in"]
    assert head_page["page"]["has_more_before"] is False

    # у роли с notes:read те же курсоры отдают полную ленту
    manager_page = await _feed(
        client,
        tokens["manager"],
        seed_conversation.conversation_id,
        before=tail["page"]["prev_cursor"],
        limit=10,
    )
    assert [m["direction"] for m in manager_page["items"]] == ["in", "note", "note"]


async def test_observer_gap_fill_after_reconnect_never_returns_notes(
    client, tokens, conversation_with_a_note
):
    """`?after=` — догрузка пропущенного после обрыва WS (01 §11.5, 03 §6.4).

    Курсор наблюдателя указывает на входящее клиента; между ним и хвостом
    ленты лежит заметка. Догрузка обязана вернуть только «out» — иначе
    заметка приезжает тем самым запросом, которым фронт лечит разрыв.
    """
    conv_id = conversation_with_a_note.conversation_id
    first_page = await _feed(client, tokens["observer"], conv_id)
    assert [m["direction"] for m in first_page["items"]] == ["in", "out"]
    from_incoming = first_page["page"]["prev_cursor"]  # курсор входящего клиента

    gap = await _feed(client, tokens["observer"], conv_id, after=from_incoming)
    # Про состав страницы утверждаем ровно одно — заметки в ней нет. Сама
    # граница курсора нарочно не фиксируется: `encode_cursor` округляет время
    # до миллисекунд, поэтому пограничное сообщение может приехать повторно
    # (см. cross-boundary про app/services/conversations.py) — это вопрос
    # дублей, а не прав, и тест не должен цементировать текущее поведение.
    assert "note" not in [m["direction"] for m in gap["items"]]
    assert NOTE_BODY not in str(gap)

    # тот же курсор у роли с notes:read отдаёт и заметку — значит, дело в праве,
    # а не в том, что запрос случайно ничего не нашёл
    manager_gap = await _feed(client, tokens["manager"], conv_id, after=from_incoming)
    assert "note" in [m["direction"] for m in manager_gap["items"]]
    assert NOTE_BODY in str(manager_gap)


async def test_note_created_through_the_api_is_hidden_from_observer(
    client, tokens, seed_conversation
):
    """Заметку пишет руководитель (ему это можно), observer её не видит."""
    r = await client.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        headers={"Authorization": f"Bearer {tokens['head']}"},
        json={"text": NOTE_BODY, "client_message_id": str(uuid.uuid4())},
    )
    assert r.status_code == 201, r.text
    assert r.json()["direction"] == "note"

    seen = await _feed(client, tokens["manager"], seed_conversation.conversation_id)
    assert [m["direction"] for m in seen["items"]] == ["in", "note"]

    hidden = await _feed(client, tokens["observer"], seed_conversation.conversation_id)
    assert [m["direction"] for m in hidden["items"]] == ["in"]


async def test_observer_cannot_write_a_note(client, tokens, seed_conversation):
    r = await client.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/notes",
        headers={"Authorization": f"Bearer {tokens['observer']}"},
        json={"text": "нельзя", "client_message_id": str(uuid.uuid4())},
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["code"] == "forbidden"


# --- список диалогов ---------------------------------------------------------


@pytest.mark.parametrize("role", ("observer", "manager"))
async def test_note_never_becomes_the_list_preview(
    client, tokens, conversation_with_a_note, db_sessionmaker, role
):
    """Превью строки списка — только переписка с клиентом (01 §5.1).

    Даже когда заметка — самое свежее сообщение диалога: иначе её текст
    прочитал бы observer прямо в списке, не открывая диалог.
    """
    async with db_sessionmaker() as session:
        session.add(
            Message(
                conversation_id=conversation_with_a_note.conversation_id,
                direction="note",
                sender_type="operator",
                body="Последняя заметка",
                attachments=[],
                delivery_status="delivered",
                created_at=datetime.now(UTC) + timedelta(seconds=10),
            )
        )
        await session.commit()

    r = await client.get(
        "/api/v1/conversations", headers={"Authorization": f"Bearer {tokens[role]}"}
    )
    assert r.status_code == 200, r.text
    row = r.json()["items"][0]
    assert row["last_message"]["direction"] in ("in", "out")
    assert "заметка" not in row["last_message"]["body"].lower()


@pytest.mark.parametrize("role", ("observer", "manager"))
async def test_search_never_matches_a_note(client, tokens, conversation_with_a_note, role):
    """Поиск `q=` идёт только по переписке с клиентом (01 §5.1).

    Иначе заметку можно читать по словам: подобрал слово — диалог нашёлся,
    не подобрал — нет. Правило общее для всех ролей, а не только для observer:
    выдача поиска не должна зависеть от того, кто спрашивает.
    """
    headers = {"Authorization": f"Bearer {tokens[role]}"}

    hit = await client.get("/api/v1/conversations", headers=headers, params={"q": "Экран"})
    assert hit.status_code == 200, hit.text
    assert len(hit.json()["items"]) == 1, "поиск по тексту клиента сломан — тест стал бы пустым"

    miss = await client.get("/api/v1/conversations", headers=headers, params={"q": "торгуется"})
    assert miss.status_code == 200, miss.text
    assert miss.json()["items"] == [], "диалог найден по тексту заметки — утечка (01 §6.1)"


async def test_conversation_detail_does_not_leak_a_note_to_observer(
    client, tokens, conversation_with_a_note
):
    r = await client.get(
        f"/api/v1/conversations/{conversation_with_a_note.conversation_id}",
        headers={"Authorization": f"Bearer {tokens['observer']}"},
    )
    assert r.status_code == 200, r.text
    assert NOTE_BODY not in r.text


# --- WS (08 §5.3) ------------------------------------------------------------


async def test_hub_does_not_deliver_note_events_to_observer_sessions(redis):
    hub = Hub(redis)
    watchers = {role: attach(hub, role) for role in (*ROLES_WITH_NOTES, "observer")}

    conversation_id = str(uuid.uuid4())

    def message_new(direction: str, body: str) -> dict:
        return {
            "type": "message:new",
            "ts": "2026-08-04T10:00:00.000Z",
            "data": {
                "conversation_id": conversation_id,
                "message": {"direction": direction, "body": body},
            },
        }

    await hub.dispatch(message_new("note", NOTE_BODY))
    await hub.dispatch(message_new("in", "Здравствуйте"))

    ws_observer, _ = watchers["observer"]
    assert [f["data"]["message"]["direction"] for f in ws_observer.sent] == ["in"]
    assert NOTE_BODY not in str(ws_observer.sent)
    for role in ROLES_WITH_NOTES:
        ws, _ = watchers[role]
        assert [f["data"]["message"]["direction"] for f in ws.sent] == ["note", "in"]
