"""Позвать коллегу в диалог, не отдавая его (docs/19).

Проверяется не «добавилась ли строка». Проверяются две вещи, ради которых
приглашение и существует, и обе легко потерять:

1. ОТВЕТСТВЕННЫЙ НЕ МЕНЯЕТСЯ. Это главное отличие от передачи. Ошибись здесь
   — и «позвать посмотреть» тихо превратится в «отдать», а спрос перейдёт на
   человека, который его не брал.
2. ДИАЛОГ ПОПАДАЕТ В «МОИ» ПОЗВАННОГО. Без этого приглашение бесполезно на
   второй день: вкладка «Все» показывает всё и так, а среди четырёхсот тысяч
   диалогов «показывает» не значит «увидит».
"""

import pytest
import sqlalchemy as sa

from app.core.errors import ApiError
from app.models import Client, Conversation, ConversationParticipant
from app.services import conversations as convs
from app.services import participants

pytestmark = pytest.mark.anyio


@pytest.fixture
async def trio(make_user):
    owner = await make_user("owner@leadchat.test", role="manager", full_name="Анна Ведущая")
    helper = await make_user("helper@leadchat.test", role="manager", full_name="Мастер Борис")
    return owner, helper


@pytest.fixture
async def conv(db_sessionmaker, make_avito_account, trio):
    owner, _ = trio
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="pt-1", name="Клиент")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="pt-chat-1",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=owner.id,
        )
        s.add(row)
        await s.commit()
        await s.refresh(row)
        return row


async def test_the_owner_stays_the_owner(db_sessionmaker, conv, trio):
    """ГЛАВНОЕ: позвали — ответственный не изменился.

    Иначе «позвать посмотреть» тихо превращается в «отдать», и спрос
    переходит на человека, который диалог не брал.
    """
    owner, helper = trio
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        await participants.invite(s, row, who=helper, actor=owner, reason="Твой район")
        await s.commit()

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert row.assignee_id == owner.id, "ответственный прежний"
        assert set(await participants.participant_ids(s, conv.id)) == {helper.id}


async def test_the_dialog_appears_in_my_list(db_sessionmaker, conv, trio):
    """Диалог попадает в «Мои» позванного.

    Без этого приглашение бесполезно на второй день: вкладка «Все» показывает
    всё и так, но среди четырёхсот тысяч диалогов «показывает» не значит
    «увидит».
    """
    owner, helper = trio
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        await participants.invite(s, row, who=helper, actor=owner)
        await s.commit()

    async with db_sessionmaker() as s:
        found = (
            (await s.execute(sa.select(Conversation).where(participants.mine_condition(helper.id))))
            .scalars()
            .all()
        )
    assert [c.id for c in found] == [conv.id]


async def test_my_list_still_shows_my_own(db_sessionmaker, conv, trio):
    """И своё никуда не делось: условие расширено, а не подменено."""
    owner, _ = trio
    async with db_sessionmaker() as s:
        found = (
            (await s.execute(sa.select(Conversation).where(participants.mine_condition(owner.id))))
            .scalars()
            .all()
        )
    assert [c.id for c in found] == [conv.id]


async def test_inviting_the_owner_is_refused(db_sessionmaker, conv, trio):
    """Позвать того, кто и так ведёт диалог, — бессмыслица, а не действие."""
    owner, _ = trio
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        with pytest.raises(ApiError) as err:
            await participants.invite(s, row, who=owner, actor=owner)
        assert err.value.status == 422


async def test_inviting_twice_is_harmless(db_sessionmaker, conv, trio):
    """Двое могли позвать одного и того же — это не ошибка и не дубль."""
    owner, helper = trio
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        await participants.invite(s, row, who=helper, actor=owner)
        await participants.invite(s, row, who=helper, actor=owner)
        await s.commit()

    async with db_sessionmaker() as s:
        n = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(ConversationParticipant)
                .where(ConversationParticipant.conversation_id == conv.id)
            )
        ).scalar_one()
    assert n == 1


async def test_leaving_removes_from_my_list(db_sessionmaker, conv, trio):
    owner, helper = trio
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        await participants.invite(s, row, who=helper, actor=owner)
        await s.commit()

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        assert await participants.leave(s, row, user_id=helper.id) is True
        await s.commit()

    async with db_sessionmaker() as s:
        found = (
            (await s.execute(sa.select(Conversation).where(participants.mine_condition(helper.id))))
            .scalars()
            .all()
        )
    assert found == []


async def test_the_reason_is_visible(db_sessionmaker, conv, trio):
    """Зачем позвали — видно.

    Без причины приглашение читается как «посмотри зачем-то», и человек
    открывает диалог, чтобы выяснить, что от него хотели.
    """
    owner, helper = trio
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        await participants.invite(
            s, row, who=helper, actor=owner, reason="Скажи, чинится ли эта модель"
        )
        await s.commit()

    async with db_sessionmaker() as s:
        view = await participants.view(s, conv.id)
    assert len(view) == 1
    assert view[0]["full_name"] == "Мастер Борис"
    assert view[0]["reason"] == "Скажи, чинится ли эта модель"


async def test_the_api_invites_and_notifies(
    client, tokens, users_by_role, db_sessionmaker, make_avito_account
):
    """Через ручку: позванный получает уведомление, ответственный не меняется."""
    from app.models.notification import Notification

    admin = users_by_role["admin"]
    manager = users_by_role["manager"]
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="pt-api", name="Клиент")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="pt-chat-api",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=admin.id,
        )
        s.add(row)
        await s.commit()
        conv_id = row.id

    r = await client.post(
        f"/api/v1/conversations/{conv_id}/participants",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
        json={"user_id": str(manager.id), "reason": "Посмотри, пожалуйста"},
    )
    assert r.status_code == 200, r.text
    assert [p["id"] for p in r.json()["participants"]] == [str(manager.id)]

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv_id)
        assert row.assignee_id == admin.id, "ответственный не изменился"
        notes = (
            (
                await s.execute(
                    sa.select(Notification).where(Notification.recipient_id == manager.id)
                )
            )
            .scalars()
            .all()
        )
    # Уведомление обязательно: без него зовут в пустоту. Свой вид, а не «Вам
    # передали»: иначе приглашение склеивалось с вестью о передаче.
    assert len(notes) == 1
    assert notes[0].kind == "conversation.invited"
    assert notes[0].title == "Вас позвали в диалог"
    assert "Посмотри" in (notes[0].body or "")


@pytest.mark.parametrize(
    "role,allowed",
    [("admin", True), ("manager", True), ("head", False), ("observer", False)],
)
async def test_inviting_needs_the_right_to_answer(
    client, tokens, users_by_role, db_sessionmaker, make_avito_account, role, allowed
):
    """Право то же, что и на ответ клиенту: `messages:send`.

    Зовёт тот, кто ведёт переписку, — admin и manager. Руководитель и
    наблюдатель смотрят, а не расставляют людей по диалогам; для этого у них
    есть назначение (`conversations:manage`).

    В общей матрице RBAC этих двух ручек нет: у POST ALLOW-ветка упёрлась бы
    в 400 на пустом теле, а в пути DELETE стоит `{user_id}`, который матрица
    не подставляет. Поэтому право заперто здесь.
    """
    account = await make_avito_account(avito_user_id=444555666)
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="pt-rbac", name="Клиент")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="pt-chat-rbac",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=users_by_role["admin"].id,
        )
        s.add(row)
        await s.commit()
        conv_id = row.id

    target = users_by_role["manager"].id
    calls = [
        ("POST", f"/api/v1/conversations/{conv_id}/participants", {"user_id": str(target)}),
        ("DELETE", f"/api/v1/conversations/{conv_id}/participants/{target}", None),
    ]
    for method, url, body in calls:
        r = await client.request(
            method, url, headers={"Authorization": f"Bearer {tokens[role]}"}, json=body
        )
        if allowed:
            assert r.status_code < 400, f"{method} {url}: {r.text}"
        else:
            assert r.status_code == 403, f"{method} {url}: {r.text}"

        anon = await client.request(method, url, json=body)
        assert anon.status_code == 401, f"{method} {url} без токена: {anon.text}"


async def test_the_detail_shows_who_was_invited(db_sessionmaker, conv, trio):
    """Кто ещё здесь — видно при открытии диалога."""
    owner, helper = trio
    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        await participants.invite(s, row, who=helper, actor=owner)
        await s.commit()

    async with db_sessionmaker() as s:
        row = await s.get(Conversation, conv.id)
        detail = await convs.conversation_detail(s, row)
    assert [p["full_name"] for p in detail["participants"]] == ["Мастер Борис"]
