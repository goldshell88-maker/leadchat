"""Сторож освобождения закрывает и возвращает диалог по общим правилам.

Раньше автозакрытие шло мимо уборки закрытия: закрепления оставались и
съедали предел, метки очереди не снимались, а в ленте не было ни строки о том,
кто и почему закрыл диалог. Вернувшийся хозяин находил свой диалог закрытым
без объяснений.
"""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.models import AuditLog, ConversationPin, Message
from app.services import inbox as inbox_svc
from tests.unit.test_release_unavailable_0309 import (
    _пометить_недоступным,
    перечитать,
    прогон,
    сделать_диалог,
)

pytestmark = pytest.mark.anyio


async def _лента(db_sessionmaker, conv_id) -> list[str]:
    async with db_sessionmaker() as s:
        return [
            m.body or ""
            for m in (
                await s.execute(
                    sa.select(Message).where(
                        Message.conversation_id == conv_id, Message.direction == "system"
                    )
                )
            ).scalars()
        ]


async def test_an_auto_closed_dialog_is_cleaned_up_and_explained(
    db_sessionmaker, redis, make_user, make_avito_account
):
    account = await make_avito_account()
    ушёл = await make_user("gone-close@leadchat.test", role="manager", full_name="Иван Ушедший")
    сейчас = datetime.now(UTC)
    conv = await сделать_диалог(
        db_sessionmaker, account.id, key="close-clean", assignee_id=ушёл.id, awaiting_since=None
    )
    async with db_sessionmaker() as s:
        s.add(ConversationPin(conversation_id=conv.id, user_id=ушёл.id))
        row = await s.get(type(conv), conv.id)
        row.offered_at = сейчас
        row.unread_count = 3
        await s.commit()
    await _пометить_недоступным(redis, ушёл.id, минут_назад=20, now=сейчас)

    кадры = await прогон(db_sessionmaker, redis, now=сейчас)

    после = await перечитать(db_sessionmaker, conv.id)
    assert после.status == inbox_svc.CLOSED
    assert после.offered_at is None and после.unread_count == 0
    async with db_sessionmaker() as s:
        pins = (
            await s.execute(
                sa.select(ConversationPin).where(ConversationPin.conversation_id == conv.id)
            )
        ).all()
        audit = (
            await s.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == "conversation.status_changed",
                    AuditLog.entity_id == str(conv.id),
                )
            )
        ).scalar_one()
    assert pins == [], "закрепление закрытого диалога съедало бы предел семи"
    assert audit.details["from"] == "in_progress"
    assert audit.details["by"] == "system"
    assert any(
        "закрыт автоматически: Иван Ушедший" in b for b in await _лента(db_sessionmaker, conv.id)
    )
    assert кадры[0]["message"] is not None


async def test_a_returned_dialog_says_why_in_its_feed(
    db_sessionmaker, redis, make_user, make_avito_account
):
    account = await make_avito_account()
    ушёл = await make_user("gone-back@leadchat.test", role="manager", full_name="Пётр Ушедший")
    сейчас = datetime.now(UTC)
    conv = await сделать_диалог(
        db_sessionmaker, account.id, key="return-feed", assignee_id=ушёл.id, awaiting_since=сейчас
    )
    await _пометить_недоступным(redis, ушёл.id, минут_назад=20, now=сейчас)

    await прогон(db_sessionmaker, redis, now=сейчас)

    assert any(
        "возвращён во «Входящие»: Пётр Ушедший" in b for b in await _лента(db_sessionmaker, conv.id)
    )
