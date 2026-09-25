"""Передача диалога от предложения до развязки: кто принимает, кто что узнаёт.

Проверяется через HTTP и сторожа, как это видит человек: админ передаёт чужой
диалог, получатель принимает, отказывается или молчит, предложение
переадресуют. Жалоба владельца 24.09 «когда передаёшь, тебе диалог не
приходит» складывалась именно из этих мест.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.core.security import create_access_token
from app.models import Client, Conversation
from app.models.notification import Notification
from app.services import conversations as convs
from app.services import transfer as transfer_svc
from app.ws.events import EVENTS_CHANNEL

pytestmark = pytest.mark.anyio


@pytest.fixture
async def team(make_user, users_by_role):
    owner = await make_user("owner-tl@leadchat.test", role="manager", full_name="Анна Ведущая")
    taker = await make_user("taker-tl@leadchat.test", role="manager", full_name="Борис Берущий")
    third = await make_user("third-tl@leadchat.test", role="manager", full_name="Вера Третья")
    return {"owner": owner, "taker": taker, "third": third, "admin": users_by_role["admin"]}


def _auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id=str(user.id), role=user.role)}"}


@pytest.fixture
async def conv_id(db_sessionmaker, make_avito_account, team) -> uuid.UUID:
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id=f"tl-{uuid.uuid4().hex[:6]}", name="Клиент")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id=f"tl-chat-{uuid.uuid4().hex[:6]}",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=team["owner"].id,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _offer(client, conv_id, *, by, to, в_сети):
    await в_сети(to)
    r = await client.post(
        f"/api/v1/conversations/{conv_id}/assign",
        headers=_auth(by),
        json={"assignee_id": str(to.id)},
    )
    assert r.status_code == 200, r.text
    return r


async def _notes(db_sessionmaker, conv_id, kind: str) -> list[Notification]:
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    sa.select(Notification).where(
                        Notification.kind == kind, Notification.entity_id == str(conv_id)
                    )
                )
            ).scalars()
        )


async def test_an_offer_made_by_an_admin_can_be_accepted(
    client, db_sessionmaker, team, conv_id, в_сети
):
    """Админ передаёт диалог менеджера: получатель принимает и становится хозяином.

    Принятие сверяло владельца с предлагавшим и на такой передаче всегда
    отвечало «предложение устарело» — в бою 0 принятых из 8 за месяц.
    """
    await _offer(client, conv_id, by=team["admin"], to=team["taker"], в_сети=в_сети)

    r = await client.post(
        f"/api/v1/conversations/{conv_id}/transfer/accept", headers=_auth(team["taker"])
    )

    assert r.status_code == 200, r.text
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
    assert row.assignee_id == team["taker"].id
    assert row.transfer_to_id is None


async def test_a_stale_offer_is_removed_by_the_refusal(
    client, db_sessionmaker, team, conv_id, в_сети
):
    """Диалог забрали, пока предложение висело: отказ в принятии снимает его
    сразу, а не оставляет кнопку, которая отказывает до таймаута."""
    await _offer(client, conv_id, by=team["owner"], to=team["taker"], в_сети=в_сети)
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == conv_id)
            .values(assignee_id=team["third"].id)
        )
        await s.commit()

    r = await client.post(
        f"/api/v1/conversations/{conv_id}/transfer/accept", headers=_auth(team["taker"])
    )

    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "owner_changed"
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
    assert row.transfer_to_id is None, "устаревшее предложение обязано сняться"
    assert row.assignee_id == team["third"].id


async def test_the_offer_frames_carry_the_transfer(client, redis, team, conv_id, в_сети):
    """У получателя с открытым диалогом полоса «Принять» рисуется по `transfer`
    в кадре: без него в детали оставалось «Войти в диалог»."""
    pubsub = redis.pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)
    await _offer(client, conv_id, by=team["owner"], to=team["taker"], в_сети=в_сети)

    from tests.unit.conftest import drain_events

    events = await drain_events(pubsub)
    updated = [e for e in events if e.get("type") == "conversation:updated"]
    assigned = [e for e in events if e.get("type") == "conversation:assigned"]
    assert updated and updated[-1]["data"]["patch"]["transfer"]["to"]["id"] == str(team["taker"].id)
    assert assigned and assigned[-1]["data"]["offer"] is True


async def test_a_closed_dialog_cannot_be_offered(client, db_sessionmaker, team, conv_id, в_сети):
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation).where(Conversation.id == conv_id).values(status="closed")
        )
        await s.commit()
    await в_сети(team["taker"])

    r = await client.post(
        f"/api/v1/conversations/{conv_id}/assign",
        headers=_auth(team["owner"]),
        json={"assignee_id": str(team["taker"].id)},
    )

    assert r.status_code == 422
    async with db_sessionmaker() as s:
        assert (await s.get(Conversation, conv_id)).transfer_to_id is None
    assert await _notes(db_sessionmaker, conv_id, transfer_svc.OFFERED) == []


async def test_closing_waits_for_the_answer_to_a_transfer(
    client, db_sessionmaker, team, conv_id, в_сети
):
    """Закрытие молча отменяло передачу: получатель оставался с «Принять»."""
    await _offer(client, conv_id, by=team["owner"], to=team["taker"], в_сети=в_сети)

    r = await client.patch(
        f"/api/v1/conversations/{conv_id}/status",
        headers=_auth(team["owner"]),
        json={"status": "closed"},
    )

    assert r.status_code == 422
    assert r.json()["error"]["details"]["reason"] == "transfer_pending"
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
    assert row.status != "closed" and row.transfer_to_id == team["taker"].id


async def test_a_redirected_offer_tells_the_first_recipient(
    client, db_sessionmaker, redis, team, conv_id, в_сети
):
    await _offer(client, conv_id, by=team["owner"], to=team["taker"], в_сети=в_сети)
    assert str(conv_id) in await redis.smembers(
        convs.TRANSFERRED_KEY.format(user_id=team["taker"].id)
    )
    await _offer(client, conv_id, by=team["owner"], to=team["third"], в_сети=в_сети)

    withdrawn = await _notes(db_sessionmaker, conv_id, transfer_svc.WITHDRAWN)
    assert [n.recipient_id for n in withdrawn] == [team["taker"].id]
    assert "переадресовали" in withdrawn[0].title
    offered = await _notes(db_sessionmaker, conv_id, transfer_svc.OFFERED)
    assert {n.recipient_id for n in offered} == {team["taker"].id, team["third"].id}
    first = next(n for n in offered if n.recipient_id == team["taker"].id)
    assert first.read_at is not None, "«Вам передали» первого получателя погашено"
    flagged = await redis.smembers(convs.TRANSFERRED_KEY.format(user_id=team["taker"].id))
    assert str(conv_id) not in flagged


async def test_a_refusal_reaches_both_the_owner_and_the_admin_who_offered(
    client, db_sessionmaker, team, conv_id, в_сети
):
    await _offer(client, conv_id, by=team["admin"], to=team["taker"], в_сети=в_сети)

    r = await client.post(
        f"/api/v1/conversations/{conv_id}/transfer/decline", headers=_auth(team["taker"])
    )

    assert r.status_code == 200, r.text
    by_recipient = {
        n.recipient_id: n
        for n in await _notes(db_sessionmaker, conv_id, "conversation.transfer_declined")
    }
    assert set(by_recipient) == {team["owner"].id, team["admin"].id}
    assert "остался у вас" in by_recipient[team["owner"].id].title
    assert "Анна Ведущая" in (by_recipient[team["admin"].id].body or "")


async def test_accepting_settles_the_old_owners_reminder(
    client, db_sessionmaker, team, conv_id, в_сети
):
    from app.services import notifications

    async with db_sessionmaker() as s:
        await notifications.notify(
            s,
            kind="conversation.awaiting_you",
            recipient_id=team["owner"].id,
            body="Клиент ждёт ответа",
            entity_type="conversation",
            entity_id=str(conv_id),
        )
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == conv_id)
            .values(auto_assigned_at=datetime.now(UTC))
        )
        await s.commit()
    await _offer(client, conv_id, by=team["owner"], to=team["taker"], в_сети=в_сети)

    await client.post(
        f"/api/v1/conversations/{conv_id}/transfer/accept", headers=_auth(team["taker"])
    )

    (reminder,) = await _notes(db_sessionmaker, conv_id, "conversation.awaiting_you")
    assert reminder.read_at is not None
    async with db_sessionmaker() as s:
        assert (await s.get(Conversation, conv_id)).auto_assigned_at is None


async def _expire(db_sessionmaker, redis, monkeypatch, conv_id) -> None:
    from app.scheduler.jobs import reclaim

    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == conv_id)
            .values(
                transfer_at=datetime.now(UTC)
                - timedelta(minutes=transfer_svc.TRANSFER_TIMEOUT_MINUTES + 1)
            )
        )
        await s.commit()
    monkeypatch.setattr(reclaim.db_mod, "session_scope", db_sessionmaker)
    monkeypatch.setattr(reclaim.redis_mod, "get_client", lambda: redis)
    assert await reclaim.expire_transfers() == 1


async def test_an_expired_offer_is_reported_to_everyone_involved(
    client, db_sessionmaker, redis, monkeypatch, team, conv_id, в_сети
):
    await _offer(client, conv_id, by=team["admin"], to=team["taker"], в_сети=в_сети)
    flag_key = convs.TRANSFERRED_KEY.format(user_id=team["taker"].id)
    assert str(conv_id) in await redis.smembers(flag_key)

    await _expire(db_sessionmaker, redis, monkeypatch, conv_id)

    expired = await _notes(db_sessionmaker, conv_id, "conversation.transfer_expired")
    assert {n.recipient_id for n in expired} == {team["owner"].id, team["admin"].id}
    withdrawn = await _notes(db_sessionmaker, conv_id, transfer_svc.WITHDRAWN)
    assert [n.recipient_id for n in withdrawn] == [team["taker"].id]
    assert str(conv_id) not in await redis.smembers(flag_key), "⚑ после истечения гаснет"


async def test_a_second_expired_offer_is_a_new_line_not_a_silent_repeat(
    client, db_sessionmaker, redis, monkeypatch, team, conv_id, в_сети
):
    """Повторное «Передачу не приняли» по тому же диалогу ложилось в
    непрочитанную первую строку и не доходило вовсе."""
    await _offer(client, conv_id, by=team["owner"], to=team["taker"], в_сети=в_сети)
    await _expire(db_sessionmaker, redis, monkeypatch, conv_id)
    await _offer(client, conv_id, by=team["owner"], to=team["third"], в_сети=в_сети)
    await _expire(db_sessionmaker, redis, monkeypatch, conv_id)

    expired = await _notes(db_sessionmaker, conv_id, "conversation.transfer_expired")
    assert len(expired) == 2


async def test_the_waiting_client_watchdog_leaves_a_pending_offer_alone(
    client, db_sessionmaker, redis, monkeypatch, team, conv_id, в_сети
):
    """Сторож «клиент ждёт» снимал висящую передачу вместе с возвратом в очередь."""
    from app.scheduler.jobs import awaiting

    await _offer(client, conv_id, by=team["owner"], to=team["taker"], в_сети=в_сети)
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(Conversation)
            .where(Conversation.id == conv_id)
            .values(awaiting_since=datetime.now(UTC) - timedelta(minutes=40))
        )
        await s.commit()
    monkeypatch.setattr(awaiting, "session_scope", db_sessionmaker)
    monkeypatch.setattr(awaiting.redis_mod, "get_client", lambda: redis)

    await awaiting.check_awaiting()

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
    assert row.transfer_to_id == team["taker"].id
    assert row.assignee_id == team["owner"].id


async def test_the_offer_row_is_in_the_recipients_view_of_the_dialog(
    db_sessionmaker, team, conv_id
):
    """`transfer_from_id` пишется предложением и снимается вместе с ним."""
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        await convs.assign_conversation(s, row, assignee=team["taker"], actor=team["admin"])
        assert row.transfer_from_id == team["owner"].id
        transfer_svc.clear(row)
        assert row.transfer_from_id is None


def test_notice_keys_differ_per_offer() -> None:
    conv = uuid.uuid4()
    first = transfer_svc.notice_key("k", conv, datetime(2026, 9, 24, 10, 0, tzinfo=UTC))
    second = transfer_svc.notice_key("k", conv, datetime(2026, 9, 24, 10, 20, tzinfo=UTC))
    assert first != second
    assert json.dumps(first)


async def test_the_offer_row_in_the_recipients_inbox_names_everyone(client, team, conv_id, в_сети):
    """Строка предложения во «Входящих» выглядела ничейной: без ответственного,
    а в полосе «— передаёт вам диалог»."""
    await _offer(client, conv_id, by=team["owner"], to=team["taker"], в_сети=в_сети)

    r = await client.get("/api/v1/inbox", headers=_auth(team["taker"]))

    assert r.status_code == 200, r.text
    (row,) = [i for i in r.json()["items"] if i["id"] == str(conv_id)]
    assert row["assignee"]["id"] == str(team["owner"].id)
    assert row["transfer"]["by"]["full_name"] == "Анна Ведущая"
    assert row["transfer"]["to"]["full_name"] == "Борис Берущий"
    assert row["transferred_to_me"] is True
