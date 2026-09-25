"""Эхо первой части длинного ответа — своё, а не «ответили из другого приложения» (24.09).

Доставка режет ответ длиннее 1000 знаков на части. Эхо первой части приходит
раньше, чем её id попадает в множество «свои», а сверка по тексту искала
сообщение ЦЕЛИКОМ: в бою прайс на 1 032 знака лёг в ленту вторым исходящим на
998 знаков без автора, и бот снимался со словами «Ответили из другого
приложения».

ДИВЕРСИЯ: убрать вызов `_своё_по_части` — краснеет тест первой части.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.integrations.avito.adapter import AvitoAdapter
from app.models import Client, Conversation, Message
from app.services.avito_text import split_text
from app.services.inbound import apply_inbound_event

pytestmark = pytest.mark.anyio

AVITO_USER_ID = 222333555
CHAT_ID = "u2i-long-echo"
LONG = ("Замена экрана с гарантией полгода, выезд мастера в день обращения. " * 18).strip()


def _echo(*, text: str, at: datetime, mid: str) -> dict:
    return {
        "id": f"env-{mid}",
        "version": "v3.0.0",
        "timestamp": int(at.timestamp()),
        "payload": {
            "type": "message",
            "value": {
                "id": mid,
                "chat_id": CHAT_ID,
                "author_id": AVITO_USER_ID,
                "user_id": AVITO_USER_ID,
                "created": int(at.timestamp()),
                "content": {"text": text},
            },
        },
    }


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


@pytest.fixture
async def dialog(db_sessionmaker, account):
    """Диалог у бота, в нём только что ушёл наш длинный ответ."""
    client_id, conv_id = uuid.uuid4(), uuid.uuid4()
    async with db_sessionmaker() as s, s.begin():
        s.add(Client(id=client_id, channel="avito", external_id="777002", name="Иван"))
        s.add(
            Conversation(
                id=conv_id,
                channel="avito",
                external_chat_id=CHAT_ID,
                account_id=account.id,
                client_id=client_id,
                status="in_progress",
                bot_active=True,
                bot_vars={},
                tags=[],
                unread_count=0,
                declined_by=[],
            )
        )
        s.add(
            Message(
                conversation_id=conv_id,
                direction="out",
                sender_type="bot",
                body=LONG,
                attachments=[],
                delivery_status="pending",
                created_at=datetime.now(UTC),
            )
        )
    return conv_id


async def _outgoing(db_sessionmaker, conv_id) -> int:
    async with db_sessionmaker() as s:
        return (
            await s.execute(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id == conv_id, Message.direction == "out")
            )
        ).scalar_one()


async def test_the_first_part_echo_is_recognised_as_ours(
    db, redis, account, dialog, db_sessionmaker
):
    parts = split_text(LONG)
    assert len(parts) == 2 and len(LONG) > 1000

    await apply_inbound_event(
        db,
        redis,
        account,
        AvitoAdapter.parse_webhook(_echo(text=parts[0], at=datetime.now(UTC), mid="part-1")),
    )

    assert await _outgoing(db_sessionmaker, dialog) == 1, "эхо части легло в ленту вторым"
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, dialog)
    assert conv is not None and conv.bot_active, "бот снят своим же эхом"


async def test_a_real_outside_answer_is_still_inserted(db, redis, account, dialog, db_sessionmaker):
    """Ответ из приложения Авито, не совпавший ни с одной частью, — чужой, как раньше."""
    await apply_inbound_event(
        db,
        redis,
        account,
        AvitoAdapter.parse_webhook(
            _echo(text="Перезвоню через час", at=datetime.now(UTC), mid="outside-1")
        ),
    )

    assert await _outgoing(db_sessionmaker, dialog) == 2


async def test_an_old_long_answer_does_not_swallow_a_new_echo(
    db, redis, account, dialog, db_sessionmaker
):
    """Окно — то же, что у сверки по тексту: часть давнего ответа уже не «своя»."""
    parts = split_text(LONG)
    await apply_inbound_event(
        db,
        redis,
        account,
        AvitoAdapter.parse_webhook(
            _echo(text=parts[0], at=datetime.now(UTC) + timedelta(minutes=10), mid="late-1")
        ),
    )

    assert await _outgoing(db_sessionmaker, dialog) == 2
