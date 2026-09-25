"""Чёрный список клиентов (аудит 7 августа, docs/19).

Главное, что проверяется, — НЕ «перестаёт ли диалог попадать в очередь». Это
просто. Проверяется противоположное: что сообщения помеченного клиента
ВСЁ РАВНО ПРИХОДЯТ И СОХРАНЯЮТСЯ.

Разница между «не требует внимания» и «не принимаем» — это разница между
сэкономленным временем и потерянным заказом. Среди сотни «надоел» однажды
окажется человек, который в прошлый раз был не в духе, а сегодня готов
платить. Ошибка здесь необратима: сообщения, которое не сохранили, потом не
найти ничем.
"""

import pytest
import sqlalchemy as sa

from app.models import Client, Conversation, Message
from app.services import inbox as inbox_svc
from tests.unit.test_inbound import make_event

pytestmark = pytest.mark.anyio


@pytest.fixture
async def blocked_client(db_sessionmaker, make_user):
    admin = await make_user("blocker@leadchat.test", role="admin")
    async with db_sessionmaker() as s:
        cl = Client(
            channel="avito",
            external_id="blk-1",
            name="Надоедливый",
            blocked_at=sa.func.now(),
            blocked_by_id=admin.id,
            blocked_reason="Пишет каждый день, ничего не заказывает",
        )
        s.add(cl)
        await s.commit()
        await s.refresh(cl)
        return cl


async def test_the_message_is_still_received_and_stored(
    db_sessionmaker, redis, make_avito_account, blocked_client
):
    """САМОЕ ВАЖНОЕ: сообщение помеченного сохраняется.

    Пометка экономит время команды, а не теряет переписку. Выйди обработчик
    раньше сохранения — среди «надоел» однажды потерялся бы настоящий заказ,
    и найти его потом было бы нечем.
    """
    from app.services.inbound import apply_inbound_event

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        inserted = await apply_inbound_event(
            s,
            redis,
            account,
            make_event(external_chat_id="blk-chat", author_id=blocked_client.external_id),
        )
        await s.commit()

    assert inserted is True, "входящее принято, а не отброшено"
    async with db_sessionmaker() as s:
        conv = (
            await s.execute(
                sa.select(Conversation).where(Conversation.external_chat_id == "blk-chat")
            )
        ).scalar_one()
        msgs = (
            (await s.execute(sa.select(Message).where(Message.conversation_id == conv.id)))
            .scalars()
            .all()
        )
    assert len(msgs) == 1, "сообщение сохранено"
    assert conv.unread_count == 1, "счётчик непрочитанных работает как обычно"


async def test_the_dialog_does_not_ask_for_attention(
    db_sessionmaker, redis, make_avito_account, blocked_client
):
    """Но в очередь не встаёт: именно это пометка и убирает.

    Иначе диалог звенел бы у тринадцати человек, кто-то открывал бы его,
    тратил минуту и закрывал. Каждый день.
    """
    from app.services.inbound import apply_inbound_event

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        await apply_inbound_event(
            s,
            redis,
            account,
            make_event(external_chat_id="blk-chat-2", author_id=blocked_client.external_id),
        )
        await s.commit()

    async with db_sessionmaker() as s:
        conv = (
            await s.execute(
                sa.select(Conversation).where(Conversation.external_chat_id == "blk-chat-2")
            )
        ).scalar_one()
        assert inbox_svc.is_waiting(conv) is False, "внимания не требует"
        assert conv.offered_at is None
        assert conv.assignee_id is None, "и никому не назначен: он его не брал"


async def test_an_ordinary_client_is_untouched(db_sessionmaker, redis, make_avito_account):
    """Обычный клиент ведёт себя ровно как раньше — пометка не задевает всех."""
    from app.services.inbound import apply_inbound_event

    account = await make_avito_account()
    async with db_sessionmaker() as s:
        await apply_inbound_event(
            s, redis, account, make_event(external_chat_id="ok-chat", author_id="ordinary-1")
        )
        await s.commit()

    async with db_sessionmaker() as s:
        conv = (
            await s.execute(
                sa.select(Conversation).where(Conversation.external_chat_id == "ok-chat")
            )
        ).scalar_one()
        assert inbox_svc.is_waiting(conv) is True


async def test_blocking_and_unblocking_through_the_api(client, tokens, db_sessionmaker):
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="api-blk", name="Клиент")
        s.add(cl)
        await s.commit()
        cid = cl.id

    headers = {"Authorization": f"Bearer {tokens['admin']}"}
    r = await client.post(f"/api/v1/clients/{cid}/block", headers=headers, json={"reason": "Спам"})
    assert r.status_code == 200, r.text
    assert r.json()["blocked"] is True
    assert r.json()["blocked_reason"] == "Спам"
    # Кто пометил — видно: через полгода никто не помнит, кого и почему занесли.
    assert r.json()["blocked_by"]["full_name"]

    r = await client.get("/api/v1/clients/blocked", headers=headers)
    assert r.status_code == 200
    assert [c["id"] for c in r.json()["items"]] == [str(cid)]

    r = await client.post(f"/api/v1/clients/{cid}/unblock", headers=headers)
    assert r.json()["blocked"] is False

    r = await client.get("/api/v1/clients/blocked", headers=headers)
    assert r.json()["items"] == []


async def test_double_click_does_not_litter_the_journal(client, tokens, db_sessionmaker):
    """Повторная пометка — не ошибка, но и не событие."""
    from app.models import AuditLog

    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="api-blk-2", name="Клиент")
        s.add(cl)
        await s.commit()
        cid = cl.id

    headers = {"Authorization": f"Bearer {tokens['admin']}"}
    await client.post(f"/api/v1/clients/{cid}/block", headers=headers, json={})
    await client.post(f"/api/v1/clients/{cid}/block", headers=headers, json={})

    async with db_sessionmaker() as s:
        n = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "client.blocked")
            )
        ).scalar_one()
    assert n == 1


async def test_an_operator_cannot_block(client, tokens, db_sessionmaker):
    """Наблюдателю пометка недоступна: он вообще ничего не меняет."""
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="api-blk-3", name="Клиент")
        s.add(cl)
        await s.commit()
        cid = cl.id

    r = await client.post(
        f"/api/v1/clients/{cid}/block",
        headers={"Authorization": f"Bearer {tokens['observer']}"},
        json={},
    )
    assert r.status_code == 403
