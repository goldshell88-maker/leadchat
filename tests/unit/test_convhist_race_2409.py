"""Догрузка истории чата не гоняется с живым приёмом (проверка 24.09).

Приём создаёт диалог первым сообщением клиента и заказывает историю чата.
Пока задача ждала очереди, клиент успевал написать ещё, и история забирала это
сообщение исторической дверью: без непрочитанного, без ожидания ответа, без
кадра. Живой вебхук того же сообщения получал «дубль» и пропускал всё живое —
вопрос клиента выглядел отвеченным. В обратном порядке уникальный ключ ронял
всю историю чата с откатом.

Стенд: Авито подменён записанными ответами, тексты вымышленные.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from app.integrations.avito.adapter import InboundEvent
from app.models import Conversation, Message
from app.services import avito_accounts as svc
from app.services import inbound as inbound_svc
from app.services.inbound import apply_inbound_event

pytestmark = pytest.mark.anyio

ACCOUNT_UID = 772400
CLIENT_UID = 998800
CHAT = "chat-race"
T0 = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)


@pytest.fixture
async def account(make_avito_account):  # noqa: ANN001
    return await make_avito_account(ACCOUNT_UID)


@pytest.fixture
def ordered(monkeypatch) -> list[dict[str, Any]]:  # noqa: ANN001
    calls: list[dict[str, Any]] = []

    async def capture(*args: Any, **kwargs: Any) -> None:
        calls.append({"args": args, **kwargs})

    async def no_frames(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(svc, "enqueue_conversation_history", capture)
    monkeypatch.setattr(inbound_svc, "publish_event", no_frames)
    return calls


def _live(msg: str, text: str, when: datetime) -> InboundEvent:
    return InboundEvent(
        external_chat_id=CHAT,
        external_message_id=msg,
        author_id=CLIENT_UID,
        account_user_id=ACCOUNT_UID,
        text=text,
        created_at=when,
        client_name="Клиент",
    )


def _raw(msg: str, text: str, when: datetime) -> dict[str, Any]:
    return {
        "id": msg,
        "author_id": CLIENT_UID,
        "created": int(when.timestamp()),
        "content": {"text": text},
    }


def _avito(raw_messages: list[dict[str, Any]]):
    chat = {
        "id": CHAT,
        "users": [{"id": ACCOUNT_UID, "name": "Мы"}, {"id": CLIENT_UID, "name": "Клиент"}],
        "context": {"type": "item", "value": {"title": "Ремонт холодильников"}},
        "has_unread": True,
        "updated": int(T0.timestamp()),
    }

    async def fake_call(fn, _account, _db, _redis, _limiter, *args, **kwargs):  # noqa: ANN001
        if fn.__name__ == "get_chat":
            return chat
        offset = int(kwargs.get("offset", 0))
        return raw_messages[offset : offset + int(kwargs.get("limit", 100))]

    return fake_call


async def _ids(db_sessionmaker) -> set[str]:  # noqa: ANN001
    async with db_sessionmaker() as s:
        return set((await s.execute(sa.select(Message.external_message_id))).scalars().all())


async def test_history_leaves_the_newer_message_to_the_webhook(
    db, redis, account, db_sessionmaker, ordered, monkeypatch
) -> None:
    await apply_inbound_event(db, redis, account, _live("m-0", "Здравствуйте", T0))
    assert ordered and ordered[0]["live_since"] == T0

    # Клиент уже написал следующее, вебхук ещё в пути — а история его видит.
    monkeypatch.setattr(
        svc,
        "_avito_call",
        _avito(
            [
                _raw("h-old", "Прошлый ремонт в июле", T0 - timedelta(days=30)),
                _raw("m-0", "Здравствуйте", T0),
                _raw("m-1", "Не морозит морозилка, сколько стоит?", T0 + timedelta(seconds=1)),
            ]
        ),
    )
    await svc.backfill_conversation(
        {"db_session_factory": db_sessionmaker, "redis": redis},
        account.id,
        CHAT,
        T0.isoformat(),
    )

    assert await _ids(db_sessionmaker) == {"m-0", "h-old"}

    # Вебхук приходит следом и проводит сообщение как живое, а не как дубль.
    stored = await apply_inbound_event(
        db,
        redis,
        account,
        _live("m-1", "Не морозит морозилка, сколько стоит?", T0 + timedelta(seconds=1)),
    )
    assert stored is True
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(sa.select(Conversation).where(Conversation.external_chat_id == CHAT))
        ).scalar_one()
    assert conv.unread_count == 2


async def test_history_insert_survives_a_row_written_between_check_and_insert(
    db, redis, account, db_sessionmaker, ordered, monkeypatch
) -> None:
    await apply_inbound_event(db, redis, account, _live("m-0", "Здравствуйте", T0))
    conv = (
        await db.execute(sa.select(Conversation).where(Conversation.external_chat_id == CHAT))
    ).scalar_one()

    # Проверка «такого ещё нет» видит прошлое: живой приём записал строку сразу
    # после неё.
    async def stale_check(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr(db, "scalar", stale_check)
    event = svc.AvitoAdapter.normalize_history_message(
        _raw("m-0", "Здравствуйте", T0),
        chat=svc.AvitoAdapter.parse_chat(
            {
                "id": CHAT,
                "users": [{"id": ACCOUNT_UID}, {"id": CLIENT_UID}],
                "context": {"type": "item", "value": {"title": "Ремонт"}},
            },
            account_user_id=ACCOUNT_UID,
        ),
        account_user_id=ACCOUNT_UID,
    )

    assert await svc._insert_history_message(db, conv, account, event) is False
