"""Кадры исхода передачи говорят, чем кончилось предложение (проверка 24.09).

Живая лента писала «Анна передал диалог — теперь ведёт Пётр» уже в момент
ПРЕДЛОЖЕНИЯ и молчала о принятии, отказе, отмене и истечении: по заплатке
`transfer: null` их не различить. Кадр `conversation:updated` теперь несёт
`transfer_outcome` и того, кто действовал.
"""

from __future__ import annotations

import uuid

import pytest

from app.models import Client, Conversation
from app.ws.events import EVENTS_CHANNEL
from tests.unit.conftest import drain_events
from tests.unit.test_transfer_lifecycle import _auth, _offer

pytestmark = pytest.mark.anyio


@pytest.fixture
async def team(make_user):  # noqa: ANN001
    return {
        "owner": await make_user(
            "owner-of@leadchat.test", role="manager", full_name="Анна Ведущая"
        ),
        "taker": await make_user(
            "taker-of@leadchat.test", role="manager", full_name="Борис Берущий"
        ),
    }


@pytest.fixture
async def conv_id(db_sessionmaker, make_avito_account, team) -> uuid.UUID:  # noqa: ANN001
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        client = Client(channel="avito", external_id=f"of-{uuid.uuid4().hex[:6]}", name="Клиент")
        s.add(client)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id=f"of-chat-{uuid.uuid4().hex[:6]}",
            account_id=account.id,
            client_id=client.id,
            status="in_progress",
            assignee_id=team["owner"].id,
        )
        s.add(row)
        await s.commit()
        return row.id


async def _outcome_frames(redis, client, url, user) -> list[dict]:  # noqa: ANN001
    pubsub = redis.pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)
    r = await client.post(url, headers=_auth(user))
    assert r.status_code == 200, r.text
    events = await drain_events(pubsub)
    return [
        e["data"]
        for e in events
        if e.get("type") == "conversation:updated" and "transfer_outcome" in e["data"]
    ]


@pytest.mark.parametrize(
    ("action", "who", "outcome"),
    [
        ("accept", "taker", "accepted"),
        ("decline", "taker", "declined"),
        ("cancel", "owner", "cancelled"),
    ],
)
async def test_the_outcome_frame_names_what_happened_and_who_did_it(
    client,
    redis,
    team,
    conv_id,
    в_сети,
    action,
    who,
    outcome,  # noqa: F811
) -> None:
    await _offer(client, conv_id, by=team["owner"], to=team["taker"], в_сети=в_сети)

    frames = await _outcome_frames(
        redis, client, f"/api/v1/conversations/{conv_id}/transfer/{action}", team[who]
    )

    assert [f["transfer_outcome"] for f in frames] == [outcome]
    assert frames[0]["transfer_actor"] == {
        "id": str(team[who].id),
        "full_name": team[who].full_name,
    }
