"""Отправка и неотправленное: три дыры, найденные проверкой 24.09.

* сбой публикации кадра после commit'а давал 500 и оставлял ответ «pending»
  навсегда, без задачи доставки;
* красную метку «ответ не дошёл» на реплике бота не мог снять никто;
* ответы «Повторить» и «Снять» приходили без цитаты — пузырь писал
  «Сообщение удалено» вместо фразы клиента.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import httpx
import pytest
import sqlalchemy as sa
from fastapi import FastAPI
from redis.exceptions import ConnectionError as RedisConnectionError

from app.models import Message
from app.services import messages as msgs
from tests.unit.test_send_message import (
    ARQ_QUEUE,
    _make_failed_message,
    auth,
    body,
    fetch_messages,
)

pytestmark = pytest.mark.anyio


@pytest.fixture
async def api(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


async def test_a_frame_failure_after_commit_still_queues_the_delivery(
    api,
    tokens,
    seed_conversation,
    db_sessionmaker,
    redis,
    monkeypatch,  # noqa: F811
):
    async def broken(*_a, **_kw):
        raise RedisConnectionError("Redis моргнул")

    monkeypatch.setattr(msgs, "publish_message_new", broken)

    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        json=body(),
        headers=auth(tokens),
    )

    assert r.status_code == 201, r.text
    (row,) = await fetch_messages(db_sessionmaker, seed_conversation.conversation_id)
    assert row.delivery_status == "pending"
    assert await redis.zcard(ARQ_QUEUE) == 1, "задача доставки обязана встать и без кадра"


async def test_the_frame_goes_out_before_the_delivery_job(
    api,
    tokens,
    seed_conversation,
    monkeypatch,  # noqa: F811
):
    """08 §8.1 п.4: статус быстрого воркера не обгоняет `message:new`.

    Иначе экран коллеги получает статус сообщения, которого ещё не знает, и
    сообщение приходит следом «Отправляется» — и таким остаётся.
    """
    order: list[str] = []
    real_publish = msgs.publish_message_new
    real_enqueue = msgs.enqueue_deliver

    async def publish(*a, **kw):
        order.append("frame")
        return await real_publish(*a, **kw)

    async def enqueue(*a, **kw):
        order.append("queue")
        return await real_enqueue(*a, **kw)

    monkeypatch.setattr(msgs, "publish_message_new", publish)
    monkeypatch.setattr(msgs, "enqueue_deliver", enqueue)

    r = await api.post(
        f"/api/v1/conversations/{seed_conversation.conversation_id}/messages",
        json=body(),
        headers=auth(tokens),
    )

    assert r.status_code == 201, r.text
    assert order == ["frame", "queue"]


async def _failed_bot_reply(db_sessionmaker, conv_id) -> Message:
    async with db_sessionmaker() as s:
        msg = Message(
            conversation_id=conv_id,
            direction="out",
            sender_type="bot",
            sender_user_id=None,
            body="Здравствуйте! Подскажите модель",
            attachments=[],
            delivery_status="failed",
            created_at=datetime.now(UTC),
        )
        s.add(msg)
        await s.commit()
        await s.refresh(msg)
        return msg


async def test_an_operator_can_dismiss_an_undelivered_bot_reply(
    api,
    tokens,
    seed_conversation,
    db_sessionmaker,  # noqa: F811
):
    msg = await _failed_bot_reply(db_sessionmaker, seed_conversation.conversation_id)

    r = await api.post(f"/api/v1/messages/{msg.id}/dismiss", headers=auth(tokens))

    assert r.status_code == 200, r.text
    assert r.json()["delivery_status"] == "dismissed"


async def test_an_operators_message_is_still_only_the_authors(
    api,
    tokens,
    seed_conversation,
    db_sessionmaker,
    users_by_role,  # noqa: F811
):
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["admin"].id
    )

    r = await api.post(f"/api/v1/messages/{msg.id}/dismiss", headers=auth(tokens))

    assert r.status_code == 403


async def test_retry_answers_with_the_quote(
    api,
    tokens,
    seed_conversation,
    db_sessionmaker,
    users_by_role,  # noqa: F811
):
    msg = await _make_failed_message(
        db_sessionmaker, seed_conversation.conversation_id, users_by_role["manager"].id
    )
    async with db_sessionmaker() as s:
        source = (
            await s.execute(sa.select(Message).where(Message.id == seed_conversation.message_id))
        ).scalar_one()
        row = await s.get(Message, (msg.id, msg.created_at))
        row.reply_to_id = source.id
        row.reply_to_created_at = source.created_at
        await s.commit()

    r = await api.post(f"/api/v1/messages/{msg.id}/retry", headers=auth(tokens))

    assert r.status_code == 200, r.text
    assert r.json()["reply_to"] is not None, "без цитаты пузырь пишет «Сообщение удалено»"
    assert r.json()["reply_to"]["id"] == str(seed_conversation.message_id)


def test_the_author_rule_in_one_place() -> None:
    from app.models import User

    бот = Message(sender_type="bot", sender_user_id=None)
    чужое = Message(sender_type="operator", sender_user_id=uuid.uuid4())
    менеджер = User(id=uuid.uuid4(), role="manager")

    assert msgs.may_fix_undelivered(бот, менеджер)
    assert not msgs.may_fix_undelivered(чужое, менеджер)
