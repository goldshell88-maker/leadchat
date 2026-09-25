"""Закрытие диалога и висящее предложение передачи (SCEN-12, major).

ЧТО БЫЛО. `change_status` при закрытии честно гасил ожидание клиента и
автораздачу, но предложение передачи не трогал. Оно жило своей жизнью ещё
пятнадцать минут: у получателя на ЗАКРЫТОМ диалоге оставалась рабочая кнопка
«Принять», а `transfer.accept` статуса не проверял вовсе. Нажав её, человек
становился ответственным за закрытое обращение и уходил в уверенности, что
взял работу, — при этом диалог не появлялся ни в очереди, ни в «Моих».

ДВА РУБЕЖА, И ОБА НУЖНЫ. Снятие при закрытии убирает кнопку с экрана. Проверка
в `accept` закрывает то, чего снятие не покрывает: предложения, повисшие ДО
этой правки, и гонку «закрыли ровно в тот момент, когда коллега жал „Принять“».

Проверка ломанием: уберите `transfer_svc.clear(conv)` из ветки закрытия —
падает `test_closing_clears_pending_transfer`; уберите проверку статуса в
`transfer.accept` — падает `test_accepting_a_closed_dialog_is_refused`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa

from app.core.errors import ApiError
from app.models import Conversation, User
from app.services import conversations as conv_svc
from app.services import transfer as transfer_svc


@pytest.fixture
async def pair(db_sessionmaker: Any) -> tuple[User, User]:
    """Двое, между которыми ходит диалог."""
    async with db_sessionmaker() as db:
        giver = User(
            id=uuid.uuid4(),
            email="giver@leadpartner.ru",
            full_name="Передающий",
            role="manager",
            password_hash="x",
            is_active=True,
        )
        taker = User(
            id=uuid.uuid4(),
            email="taker@leadpartner.ru",
            full_name="Принимающий",
            role="manager",
            password_hash="x",
            is_active=True,
        )
        db.add_all([giver, taker])
        await db.commit()
        await db.refresh(giver)
        await db.refresh(taker)
        return giver, taker


@pytest.fixture
async def conv(db_sessionmaker: Any, make_avito_account: Any) -> Conversation:
    from app.models import Client

    account = await make_avito_account(avito_user_id=555666777)
    async with db_sessionmaker() as db:
        client = Client(id=uuid.uuid4(), channel="avito", external_id="c-1", name="Клиент")
        db.add(client)
        row = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id="chat-close-transfer",
            account_id=account.id,
            client_id=client.id,
            status="in_progress",
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=0,
            declined_by=[],
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row


async def test_closing_clears_pending_transfer(
    db_sessionmaker: Any, conv: Conversation, pair: tuple[User, User]
) -> None:
    giver, taker = pair
    async with db_sessionmaker() as db:
        row = await db.get(Conversation, conv.id)
        assert row is not None
        row.assignee_id = giver.id
        transfer_svc.offer(row, to=taker, actor=giver)
        await db.commit()
        assert transfer_svc.is_pending(row), "исходное состояние: предложение висит"

        await conv_svc.change_status(db, row, new_status="closed", actor=giver)
        await db.commit()

    async with db_sessionmaker() as db:
        row = await db.get(Conversation, conv.id)
        assert row is not None
        assert row.status == "closed"
        assert row.transfer_to_id is None, "закрыли — предлагать нечего"
        assert row.transfer_by_id is None
        assert row.transfer_at is None


async def test_accepting_a_closed_dialog_is_refused(
    db_sessionmaker: Any, conv: Conversation, pair: tuple[User, User]
) -> None:
    """Второй рубеж: предложение повисло с прошлых времён, диалог уже закрыт."""
    giver, taker = pair
    async with db_sessionmaker() as db:
        row = await db.get(Conversation, conv.id)
        assert row is not None
        row.assignee_id = giver.id
        transfer_svc.offer(row, to=taker, actor=giver)
        # Закрываем в обход сервиса — так выглядит диалог, закрытый ДО правки.
        await db.execute(
            sa.update(Conversation).where(Conversation.id == row.id).values(status="closed")
        )
        await db.commit()

    async with db_sessionmaker() as db:
        row = await db.get(Conversation, conv.id)
        assert row is not None
        with pytest.raises(ApiError) as exc:
            transfer_svc.accept(row, actor=taker)
        assert exc.value.details == {"reason": "conversation_closed"}
        # И предложение снято, чтобы кнопка не осталась висеть навсегда.
        assert row.transfer_to_id is None
        assert row.assignee_id == giver.id, "ответственный не меняется"


async def test_accepting_an_open_dialog_still_works(
    db_sessionmaker: Any, conv: Conversation, pair: tuple[User, User]
) -> None:
    """Обычная передача не сломана: открытый диалог принимается как раньше."""
    giver, taker = pair
    async with db_sessionmaker() as db:
        row = await db.get(Conversation, conv.id)
        assert row is not None
        row.assignee_id = giver.id
        transfer_svc.offer(row, to=taker, actor=giver)
        await db.commit()

    async with db_sessionmaker() as db:
        row = await db.get(Conversation, conv.id)
        assert row is not None
        previous = transfer_svc.accept(row, actor=taker)
        await db.commit()
        assert previous == giver.id
        assert row.assignee_id == taker.id
        assert row.transfer_to_id is None
