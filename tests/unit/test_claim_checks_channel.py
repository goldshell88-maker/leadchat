"""Принять диалог чужого канала нельзя — и страж РЕАЛЬНО стоит на дороге.

НАЙДЕНО 12 августа обходом кода. `assert_can_take_account` существовал,
объяснял в докстринге, зачем он нужен, и был покрыт восемью тестами — а
вызывался ТОЛЬКО ИЗ НИХ. Ни одной боевой строки. Тесты звали функцию напрямую
и оставались зелёными: они проверяли сам страж, но не то, что он поставлен.

Цена пропуска написана в его же докстринге: «Очередь уже отфильтрована, но
прятать кнопку недостаточно: id диалога виден в ссылке, в кадре `inbox:new` и
в списке "Все", а принятие — обычный POST». У заказчика девять каналов и
тринадцать диспетчеров, назначенных по каналам; любой мог принять чужой диалог
по прямой ссылке.

ЭТОТ ТЕСТ ХОДИТ ЧЕРЕЗ `inbox.claim`, А НЕ ЗОВЁТ СТРАЖА. В этом вся разница:
проверяется путь, которым ходит человек, а не наличие функции в модуле.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from app.core.errors import ApiError
from app.models import AccountOperator, Client, Conversation, User
from app.services import inbox as inbox_svc


async def _оператор(db: Any, email: str) -> User:
    user = User(
        id=uuid.uuid4(),
        email=email,
        full_name="Диспетчер",
        role="manager",
        password_hash="x",
        is_active=True,
        handles_conversations=True,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
async def стенд(db_sessionmaker: Any, make_avito_account: Any) -> dict[str, Any]:
    account = await make_avito_account(avito_user_id=770011)
    async with db_sessionmaker() as db:
        свой = await _оператор(db, "svoy@leadpartner.ru")
        чужой = await _оператор(db, "chuzhoy@leadpartner.ru")
        # Канал закреплён за «своим»: пока у канала есть хоть один назначенный
        # оператор, остальные к нему не допускаются (правило совместимости —
        # канал без назначенных открыт всем).
        db.add(AccountOperator(account_id=account.id, user_id=свой.id))

        client = Client(id=uuid.uuid4(), channel="avito", external_id="777900", name="Клиент")
        db.add(client)
        conv = Conversation(
            id=uuid.uuid4(),
            channel="avito",
            external_chat_id="chat-chuzhoy-kanal",
            account_id=account.id,
            client_id=client.id,
            status="new",
            bot_active=False,
            bot_vars={},
            tags=[],
            unread_count=1,
            declined_by=[],
            offered_at=datetime.now(UTC),
        )
        db.add(conv)
        await db.commit()
        return {"conv": conv.id, "свой": свой, "чужой": чужой}


async def test_чужой_канал_принять_нельзя(db_sessionmaker: Any, стенд: dict[str, Any]) -> None:
    async with db_sessionmaker() as db:
        with pytest.raises(ApiError) as отказ:
            await inbox_svc.claim(db, стенд["conv"], стенд["чужой"])

    assert отказ.value.status == 403
    # Причина названа машиночитаемо: интерфейс отличает «канал не ваш» от
    # «роль не отвечает клиентам» и говорит человеку разное.
    assert отказ.value.details.get("reason") == "channel_not_assigned"

    # И диалог остался нетронутым: отказ идёт ДО захвата строки.
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, стенд["conv"])
        assert conv is not None
        assert conv.assignee_id is None
        assert conv.status == "new"


async def test_свой_канал_принять_можно(db_sessionmaker: Any, стенд: dict[str, Any]) -> None:
    """Обратная сторона: страж не должен запирать назначенного оператора."""
    async with db_sessionmaker() as db:
        await inbox_svc.claim(db, стенд["conv"], стенд["свой"])
        await db.commit()

    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, стенд["conv"])
        assert conv is not None
        assert conv.assignee_id == стенд["свой"].id
        assert conv.status == "in_progress"


# --------------------------------------------------- вторая дверь: «ответил»


async def _отправить(db: Any, redis: Any, conv_id: uuid.UUID, user: User) -> Any:
    from app.services.messages import create_outbound_message

    return await create_outbound_message(
        db,
        redis,
        conversation_id=conv_id,
        user=user,
        text="Здравствуйте, приедем завтра",
        client_message_id=str(uuid.uuid4()),
    )


async def test_чужой_канал_нельзя_взять_и_ответом(
    db_sessionmaker: Any, redis: Any, стенд: dict[str, Any]
) -> None:
    """⚠ ВТОРАЯ ДВЕРЬ В ВЛАДЕНИЕ, И У НЕЁ НЕ БЫЛО ЗАМКА (28.08).

    Кнопка «Принять» спрашивает «этот канал ваш?». Путь «ответил — значит
    принял» (01 §6.2) назначает ответственного ТЕМИ ЖЕ полями и вообще без
    этого вопроса: во всём приложении страж звался ровно из одного места.
    Список «Все» по каналам не сужается, так что чужой диалог виден и
    открывается обычным нажатием — менеджер отвечал клиенту чужого канала и
    становился хозяином обращения без единой ошибки на экране.

    ЧТО ЛОМАЛИ: убрали вызов `_assert_may_take_this_channel` из
    `create_outbound_message` — тест краснеет на «диалог остался ничейным».
    """
    async with db_sessionmaker() as db:
        with pytest.raises(ApiError) as отказ:
            await _отправить(db, redis, стенд["conv"], стенд["чужой"])

    assert отказ.value.status == 403
    assert отказ.value.details.get("reason") == "channel_not_assigned"

    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, стенд["conv"])
        assert conv is not None
        assert conv.assignee_id is None, "чужой канал — а диалог достался отвечавшему"


async def test_свой_канал_ответом_берётся_как_и_прежде(
    db_sessionmaker: Any, redis: Any, стенд: dict[str, Any]
) -> None:
    """Обратная сторона: назначенному оператору путь «ответил — принял» открыт."""
    async with db_sessionmaker() as db:
        await _отправить(db, redis, стенд["conv"], стенд["свой"])
        await db.commit()

    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, стенд["conv"])
        assert conv is not None
        assert conv.assignee_id == стенд["свой"].id


async def test_ответ_в_чужой_диалог_не_запрещён(
    db_sessionmaker: Any, redis: Any, стенд: dict[str, Any]
) -> None:
    """⚠ ПРАВКА НЕ ИМЕЕТ ПРАВА ЗАПЕРЕТЬ ЖИВУЮ РАБОТУ.

    «Диспетчер подхватывает клиента коллеги, ушедшего на обед, — это нормальная
    работа» (`services/messages.py`, разбор SCEN-48/49). Спрашиваем про канал
    только у НИЧЕЙНОГО диалога: у диалога с хозяином присваивать нечего, а
    отняв право дописать, мы бросили бы клиента посреди разговора.
    """
    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, стенд["conv"])
        assert conv is not None
        conv.assignee_id = стенд["свой"].id  # хозяин есть, и это не отвечающий
        await db.commit()

    async with db_sessionmaker() as db:
        created = await _отправить(db, redis, стенд["conv"], стенд["чужой"])
        await db.commit()
    assert created.message is not None, "ответ в чужой диалог запрещать нечем и не нужно"

    async with db_sessionmaker() as db:
        conv = await db.get(Conversation, стенд["conv"])
        assert conv is not None
        assert conv.assignee_id == стенд["свой"].id, "ответственный не меняется"
