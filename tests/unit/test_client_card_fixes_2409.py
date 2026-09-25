"""Карточка клиента: пометка «нежелательный» и сбой кадра после сохранения.

* Окно пометки обещает «диалог не встанет в очередь и не будет звенеть у
  команды», а уже ждущее обращение оставалось во «Входящих» у всех — и закрыть
  его менеджер не мог.
* Сбой публикации кадра после commit'а отдавал 500 на имени, телефоне и
  пометке, хотя правка уже была в базе.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.security import create_access_token
from app.models import Conversation
from app.ws.events import EVENTS_CHANNEL

pytestmark = pytest.mark.anyio


def _auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id=str(user.id), role=user.role)}"}


async def test_blocking_takes_the_waiting_dialog_out_of_the_queue(
    client, db_sessionmaker, redis, seed_conversation, users_by_role
):
    from tests.unit.conftest import drain_events

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.offered_at = datetime.now(UTC)
        conv.awaiting_since = datetime.now(UTC)
        await s.commit()
    pubsub = redis.pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)

    r = await client.post(
        f"/api/v1/clients/{seed_conversation.client_id}/block",
        json={"reason": "спам"},
        headers=_auth(users_by_role["manager"]),
    )

    assert r.status_code == 200, r.text
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
    assert conv.offered_at is None and conv.awaiting_since is None
    claimed = [e for e in await drain_events(pubsub) if e.get("type") == "inbox:claimed"]
    assert [e["data"]["conversation_id"] for e in claimed] == [
        str(seed_conversation.conversation_id)
    ]


async def test_a_frame_failure_does_not_turn_a_saved_name_into_an_error(
    client, redis, seed_conversation, users_by_role, monkeypatch
):
    async def broken(*_a, **_kw):
        raise RedisConnectionError("Redis моргнул")

    # Ломаем саму публикацию: так ловится любой путь кадра, а не одна функция.
    monkeypatch.setattr(redis, "publish", broken)

    r = await client.put(
        f"/api/v1/clients/{seed_conversation.client_id}/name",
        json={"name": "Иван"},
        headers=_auth(users_by_role["manager"]),
    )

    assert r.status_code == 200, r.text


async def test_replace_on_a_merged_cards_number_changes_the_open_card(
    client, db_sessionmaker, make_avito_account, users_by_role
):
    """«Заменить» писало номер в скрытую присоединённую карточку, а открытая
    оставалась со старым; тост при этом говорил «Телефон карточки заменён»."""
    from app.models import Client, ClientPhoneCandidate

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        winner = Client(channel="avito", external_id="w-2409", name="Ольга", phone="+79001112241")
        s.add(winner)
        await s.flush()
        loser = Client(channel="avito", external_id="l-2409", name="Оля", merged_into_id=winner.id)
        s.add(loser)
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id="chat-l-2409",
            account_id=account.id,
            client_id=winner.id,
            origin_client_id=loser.id,
            status="in_progress",
        )
        s.add(conv)
        await s.flush()
        candidate = ClientPhoneCandidate(
            client_id=loser.id,
            conversation_id=conv.id,
            phone="+79001112242",
            raw="8 900 111 22 42",
            detected_at=datetime.now(UTC),
        )
        s.add(candidate)
        await s.commit()
        winner_id, loser_id, candidate_id = winner.id, loser.id, candidate.id

    r = await client.post(
        f"/api/v1/clients/{winner_id}/phone-candidates/{candidate_id}/resolve",
        json={"decision": "replace"},
        headers=_auth(users_by_role["manager"]),
    )

    assert r.status_code == 200, r.text
    async with db_sessionmaker() as s:
        assert (await s.get(Client, winner_id)).phone == "+79001112242"
        assert (await s.get(Client, loser_id)).phone is None


async def test_a_suggestion_into_a_group_says_so_and_merges_the_other_way(
    client, db_sessionmaker, users_by_role
):
    """В карточку с группой другую влить нельзя (422) — подсказка обязана
    сказать об этом, и объединение идёт в обратную сторону."""
    from app.models import Client
    from app.services import clients as clients_svc

    async with db_sessionmaker() as s:
        group = Client(channel="avito", external_id="g-2409", name="Ольга", phone="+79001112243")
        s.add(group)
        await s.flush()
        s.add(Client(channel="avito", external_id="gc-2409", merged_into_id=group.id))
        mine = Client(channel="avito", external_id="m-2409", name="Оля", phone="+79001112243")
        s.add(mine)
        await s.commit()
        group_id, mine_id = group.id, mine.id

    async with db_sessionmaker() as s:
        (hint,) = await clients_svc.merge_candidates(s, await s.get(Client, mine_id))
    assert hint["id"] == str(group_id)
    assert hint["has_group"] is True and hint["mine_has_group"] is False

    r = await client.post(
        f"/api/v1/clients/{group_id}/merge",
        json={"source_id": str(mine_id)},
        headers=_auth(users_by_role["manager"]),
    )
    assert r.status_code == 200, r.text


async def test_an_observer_reads_the_card_but_cannot_edit_it(
    client, seed_conversation, users_by_role
):
    """Адрес выезда лежит только в личности клиента, а её отдавали лишь с правом
    править: наблюдатель не видел адреса вовсе."""
    observer = users_by_role["observer"]

    read = await client.get(
        f"/api/v1/clients/{seed_conversation.client_id}/identity", headers=_auth(observer)
    )
    edit = await client.put(
        f"/api/v1/clients/{seed_conversation.client_id}/name",
        json={"name": "Кто-то"},
        headers=_auth(observer),
    )

    assert read.status_code == 200, read.text
    assert edit.status_code == 403
