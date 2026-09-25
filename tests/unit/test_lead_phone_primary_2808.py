"""В заявку уезжает ОСНОВНОЙ номер, а не «второй».

⚠ «ПРИНЯТ» И «ОСНОВНОЙ» — РАЗНЫЕ ВЕЩИ, И ЭТО СТОИЛО ЧУЖОГО ЗВОНКА.
Статус `accepted` ставят ОБЕ кнопки разбора распознанного номера: и «Заменить»,
и «Добавить». А «Добавить» по собственному докстрингу `resolve_phone_candidate`
означает «телефон его, но ОСНОВНОЙ ДРУГОЙ» — так помечают второй номер, тот
самый «звоните жене». Карточка показывает его отдельно и с пометкой
`"primary": False`, а `clients.phone` не трогается вовсе.

`lead_phone` этого различия не знала: первым путём брала ЛЮБОГО принятого
кандидата этой переписки. В лид-центр уезжал номер жены вместо номера клиента,
и ошибка оставалась тихой до самого звонка — в карточке номер правильный, в
заявке другой.

Тест ходит боевым путём: кладёт кандидата через `record_phone_candidate`,
нажимает кнопку через `resolve_phone_candidate` и спрашивает `lead_phone`. Так
он ломается и тогда, когда разъедутся кнопка и выдача.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from app.models import Client, ClientPhoneCandidate, Conversation, User
from app.services import clients as clients_svc
from app.services import leads as leads_svc
from app.services import phone_parse

pytestmark = pytest.mark.anyio

ОСНОВНОЙ = "+79151112233"
ВТОРОЙ = "+79157778899"


async def _кандидат(db: Any, conv, номер: str, msg_id) -> ClientPhoneCandidate:
    client_row = await db.get(Client, conv.client_id)
    await clients_svc.record_phone_candidate(
        db,
        client=client_row,
        conversation_id=conv.conversation_id,
        message_id=msg_id,
        message_at=datetime.now(UTC),
        found=phone_parse.Found(value=номер, raw=номер, start=0, end=len(номер)),
        now=datetime.now(UTC),
    )
    await db.flush()
    return (
        await db.execute(sa.select(ClientPhoneCandidate).where(ClientPhoneCandidate.phone == номер))
    ).scalar_one()


async def _телефон_заявки(db_sessionmaker: Any, conv_id) -> str | None:
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, conv_id)
        client_row = await db.get(Client, conv.client_id)
        return await leads_svc.lead_phone(db, conv, client_row)


async def test_второй_номер_в_заявку_не_уезжает(
    db_sessionmaker: Any, seed_conversation, users_by_role: dict[str, User]
) -> None:
    """«Добавить» = «телефон его, но основной другой». В заявку он не идёт."""
    async with db_sessionmaker() as db:
        client_row = await db.get(Client, seed_conversation.client_id)
        client_row.phone = None
        кандидат = await _кандидат(db, seed_conversation, ВТОРОЙ, seed_conversation.message_id)
        await clients_svc.resolve_phone_candidate(
            db, candidate=кандидат, decision="add", actor=users_by_role["manager"]
        )
        await db.commit()

    assert await _телефон_заявки(db_sessionmaker, seed_conversation.conversation_id) is None, (
        "в лид-центр уехал номер, про который человек сказал «основной другой»"
    )


async def test_заменённый_номер_в_заявку_уезжает(
    db_sessionmaker: Any, seed_conversation, users_by_role: dict[str, User]
) -> None:
    """Обратная сторона: «Заменить» делает номер основным — его и отдаём."""
    async with db_sessionmaker() as db:
        client_row = await db.get(Client, seed_conversation.client_id)
        client_row.phone = None
        кандидат = await _кандидат(db, seed_conversation, ОСНОВНОЙ, seed_conversation.message_id)
        await clients_svc.resolve_phone_candidate(
            db, candidate=кандидат, decision="replace", actor=users_by_role["manager"]
        )
        await db.commit()

    assert await _телефон_заявки(db_sessionmaker, seed_conversation.conversation_id) == ОСНОВНОЙ


async def test_второй_номер_не_вытесняет_основной(
    db_sessionmaker: Any, seed_conversation, users_by_role: dict[str, User]
) -> None:
    """Самый дорогой случай: основной есть, а «второй» решён ПОЗЖЕ него.

    Выборка кандидата шла по `resolved_at DESC`, то есть побеждал последний
    решённый — и в заявку уезжал именно второй номер, хотя основной стоит в
    карточке рядом.
    """
    async with db_sessionmaker() as db:
        client_row = await db.get(Client, seed_conversation.client_id)
        client_row.phone = None
        первый = await _кандидат(db, seed_conversation, ОСНОВНОЙ, seed_conversation.message_id)
        await clients_svc.resolve_phone_candidate(
            db, candidate=первый, decision="replace", actor=users_by_role["manager"]
        )
        второй = await _кандидат(db, seed_conversation, ВТОРОЙ, seed_conversation.message_id)
        await clients_svc.resolve_phone_candidate(
            db, candidate=второй, decision="add", actor=users_by_role["manager"]
        )
        await db.commit()

    assert await _телефон_заявки(db_sessionmaker, seed_conversation.conversation_id) == ОСНОВНОЙ, (
        "в заявку уехал второй номер, потому что решение по нему принято последним"
    )
