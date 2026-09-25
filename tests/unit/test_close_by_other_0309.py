"""Чужое закрытие диалога не должно мешать тому, кто его вёл.

⚠ ПРОСЬБА ВЛАДЕЛЬЦА 03.09, ДОСЛОВНО: «Сделай так, чтобы другой человек мог
спокойно у меня его закрыть и чтобы это никак не помешало другому человеку».

Разведка перебрала весь список «чем можно помешать», и почти всё оказалось уже
тихим: звука нет, с экрана не уводят, ленту не дёргает, статистику не отнимают,
ответственного не снимают. Настоящих помех было две — у хозяина забирали поле
из-под руки (это половина фронта) и он не узнавал, что случилось. Вторая
половина проверяется здесь.
"""

import uuid

import pytest
import sqlalchemy as sa

from app.models import Client, Conversation, Notification


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def мой(db_sessionmaker, make_avito_account, users_by_role):
    """Диалог в работе у менеджера."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="cb-1", name="Клиент")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="cb-chat-1",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=users_by_role["manager"].id,
        )
        s.add(row)
        await s.commit()
        return row.id


async def уведомления(db_sessionmaker, кому: uuid.UUID) -> list[Notification]:
    async with db_sessionmaker() as s:
        return list(
            (await s.execute(sa.select(Notification).where(Notification.recipient_id == кому)))
            .scalars()
            .all()
        )


async def test_the_owner_learns_who_closed_their_dialog(
    client, tokens, мой, db_sessionmaker, users_by_role
):
    """Закрыл коллега — хозяин узнаёт об этом, а не гадает.

    ⚠ БЕЗ ЭТОГО ЧУЖОЕ ЗАКРЫТИЕ ЧИТАЕТСЯ КАК ПОЛОМКА. Кадры о закрытии веерные и
    одинаковые для всех: отличить «закрыл я» от «закрыл коллега» по ним нельзя.
    Хозяин видит только, что поле заперлось, диалог ушёл из «Моих», а признак
    «в работе у вас» погас.
    """
    r = await client.patch(
        f"/api/v1/conversations/{мой}/status",
        json={"status": "closed"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text

    строки = await уведомления(db_sessionmaker, users_by_role["manager"].id)
    assert len(строки) == 1, "хозяин не узнал, что его диалог закрыли"
    assert строки[0].kind == "conversation.closed_by_other"
    assert строки[0].entity_id == str(мой)
    # Без звука и без красной плашки: звонить на всю комнату из тринадцати
    # человек ради чужого закрытия дороже пользы.
    assert строки[0].severity == "info"


async def test_closing_my_own_dialog_is_silent(client, tokens, мой, db_sessionmaker, users_by_role):
    """Своё закрытие молчит.

    ⚠ ОБРАТНАЯ ПОЛОВИНА, И ОНА ВАЖНЕЕ ПРЯМОЙ. Закрытий за месяц 14 655, из них
    чужих — 16. Сообщай мы обо всех, человек получал бы уведомление на каждое
    своё закрытие, и центр уведомлений превратился бы в мусорку за смену.
    """
    r = await client.patch(
        f"/api/v1/conversations/{мой}/status",
        json={"status": "closed"},
        headers=auth(tokens, "manager"),
    )
    assert r.status_code == 200, r.text

    assert await уведомления(db_sessionmaker, users_by_role["manager"].id) == []


async def test_closing_a_nobodys_dialog_notifies_nobody(
    client, tokens, db_sessionmaker, make_avito_account, users_by_role
):
    """У ничьего диалога адресата нет — и сообщать некому."""
    account = await make_avito_account(avito_user_id=555000111, title="Ничей")
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="cb-2", name="Клиент 2")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="cb-chat-2",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
        )
        s.add(row)
        await s.commit()
        ничей = row.id

    r = await client.patch(
        f"/api/v1/conversations/{ничей}/status",
        json={"status": "closed"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text

    async with db_sessionmaker() as s:
        всего = (await s.execute(sa.select(sa.func.count()).select_from(Notification))).scalar_one()
    assert всего == 0


async def test_other_status_changes_are_silent(client, tokens, мой, db_sessionmaker, users_by_role):
    """Сообщаем только о ЗАКРЫТИИ, а не о всякой смене статуса.

    Возврат чужого диалога в работу — обычное дело, и уведомление о нём было бы
    новой помехой вместо снятой. (Диалог при этом достаётся нажавшему — это
    отдельное правило, закреплённое своими тестами.)
    """
    # «Ждёт клиента» сервер не даёт поставить, пока клиенту не ответили
    # («ждать ему нечего»), поэтому берём другой законный переход.
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, мой)
        conv.status = "closed"
        await s.commit()

    r = await client.patch(
        f"/api/v1/conversations/{мой}/status",
        json={"status": "in_progress"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text

    assert await уведомления(db_sessionmaker, users_by_role["manager"].id) == []


async def test_the_owner_hears_it_live_not_only_on_the_next_poll(
    client, tokens, мой, redis, users_by_role
):
    """Строка в базе без кадра видна только через минуту опроса колокольчика."""
    from app.ws.events import EVENTS_CHANNEL
    from tests.unit.conftest import drain_events

    pubsub = redis.pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)

    r = await client.patch(
        f"/api/v1/conversations/{мой}/status",
        json={"status": "closed"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code == 200, r.text

    notify = [e for e in await drain_events(pubsub) if e.get("type") == "notify"]
    assert [e["data"]["kind"] for e in notify] == ["conversation.closed_by_other"]
