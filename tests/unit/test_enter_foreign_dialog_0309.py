"""Свободный вход в чужой рабочий диалог (просьба владельца 03.09).

⚠ ДОСЛОВНО: «сделай так, чтобы можно было спокойно заходить в чужой диалог,
который в работе у другого человека, и он появлялся так же у тебя в „Мои".
Сделай только индикацию, что диалог в работе у … Чтобы оба человека понимали,
что у кого в работе».

Механизм «диалог попадает в „Мои"» уже был — участники (docs/19). Не хватало
различия «позвали помочь» и «зашёл посмотреть»: от него зависят права, долг и
срок жизни участия. Здесь проверяется именно оно.
"""

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Client, Conversation, ConversationParticipant, Message
from app.services import participants

T0 = datetime(2026, 9, 3, 9, 0, 0, tzinfo=UTC)


def auth(tokens, role="manager"):
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def чужой(db_sessionmaker, make_avito_account, users_by_role):
    """Диалог в работе у администратора; менеджер к нему отношения не имеет."""
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="ent-1", name="Клиент")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="ent-chat-1",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=users_by_role["admin"].id,
            last_message_at=T0,
        )
        s.add(row)
        await s.commit()
        return row.id


@pytest.fixture
async def свой(db_sessionmaker, make_avito_account, users_by_role):
    """Диалог, который ведёт сам менеджер."""
    account = await make_avito_account(avito_user_id=987654321, title="Свой")
    async with db_sessionmaker() as s:
        cl = Client(channel="avito", external_id="ent-2", name="Клиент 2")
        s.add(cl)
        await s.flush()
        row = Conversation(
            channel="avito",
            external_chat_id="ent-chat-2",
            account_id=account.id,
            client_id=cl.id,
            status="in_progress",
            assignee_id=users_by_role["manager"].id,
            last_message_at=T0,
        )
        s.add(row)
        await s.commit()
        return row.id


async def мои(client, tokens, role="manager") -> set[str]:
    r = await client.get("/api/v1/conversations?tab=mine&limit=200", headers=auth(tokens, role))
    assert r.status_code == 200, r.text
    return {i["id"] for i in r.json()["items"]}


async def test_entering_puts_the_dialog_into_my_list(client, tokens, чужой):
    """Зашёл в чужой рабочий диалог — он появился в «Моих»."""
    assert str(чужой) not in await мои(client, tokens)

    r = await client.post(f"/api/v1/conversations/{чужой}/enter", headers=auth(tokens))
    assert r.status_code == 200, r.text
    assert r.json()["entered"] is True

    assert str(чужой) in await мои(client, tokens), "чужой диалог не появился в «Моих»"


async def test_entering_is_silent(client, tokens, чужой, db_sessionmaker):
    """Вход НИЧЕГО не шлёт: ни строки в ленте, ни уведомления, ни журнала.

    ⚠ ЕСЛИ ПУСТИТЬ ВХОД ЧЕРЕЗ «ПОЗВАТЬ», каждое открытие чужого диалога дало бы
    человеку уведомление ОТ САМОГО СЕБЯ и строку в ленте «Иванов позвал(а) в
    диалог: Иванов», а кадр о новом сообщении улетел бы всем тринадцати. Ровно
    из-за такого шума 02.09 человек выключил звук.
    """
    async with db_sessionmaker() as s:
        было = (
            await s.execute(
                sa.select(sa.func.count()).select_from(Message).where(Message.direction == "system")
            )
        ).scalar_one()

    await client.post(f"/api/v1/conversations/{чужой}/enter", headers=auth(tokens))

    async with db_sessionmaker() as s:
        стало = (
            await s.execute(
                sa.select(sa.func.count()).select_from(Message).where(Message.direction == "system")
            )
        ).scalar_one()
        assert стало == было, "вход написал в ленту"

        записи = (
            (await s.execute(sa.select(AuditLog).where(AuditLog.entity_id == str(чужой))))
            .scalars()
            .all()
        )
        assert not [a for a in записи if "participant" in a.action], "вход попал в журнал"

    уведомления = await client.get("/api/v1/notifications", headers=auth(tokens))
    assert уведомления.json()["items"] == [], "человек получил уведомление сам от себя"


async def test_entering_does_not_grant_the_rights_of_an_owner(
    client, tokens, чужой, db_sessionmaker, users_by_role
):
    """«Зашёл посмотреть» не даёт прав «своего»: закрепить чужой диалог нельзя.

    Позванному это можно — его позвали помогать. Разница между двумя видами
    участия здесь и работает.
    """
    await client.post(f"/api/v1/conversations/{чужой}/enter", headers=auth(tokens))

    r = await client.post(f"/api/v1/conversations/{чужой}/pin", headers=auth(tokens))
    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "not_mine"


async def test_the_debt_counts_only_what_i_answer_for(client, tokens, чужой, db_sessionmaker):
    """Число «ждут вашего ответа» не растёт от того, что я куда-то зашёл.

    ⚠ ИНАЧЕ ПИЛЮЛЯ «ЖДУТ ВАШЕГО ОТВЕТА» ВЫРОДИЛАСЬ БЫ В «сколько диалогов я
    сегодня открыл»: один чужой ждущий диалог зачёлся бы в долг и хозяину, и
    каждому заглянувшему.
    """
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, чужой)
        conv.awaiting_since = conv.last_message_at
        await s.commit()

    до = (await client.get("/api/v1/conversations/counts", headers=auth(tokens))).json()
    await client.post(f"/api/v1/conversations/{чужой}/enter", headers=auth(tokens))
    после = (await client.get("/api/v1/conversations/counts", headers=auth(tokens))).json()

    assert после["mine"] == до["mine"] + 1, "диалог не попал в «Мои»"
    assert после["mine_waiting"] == до["mine_waiting"], "чужой долг записан на меня"


async def test_entering_my_own_dialog_writes_nothing(client, tokens, свой, db_sessionmaker):
    """У ответственного входа нет: строка участника рядом с ответственностью —
    второе имя одному и тому же."""
    r = await client.post(f"/api/v1/conversations/{свой}/enter", headers=auth(tokens))
    assert r.status_code == 200, r.text
    assert r.json()["entered"] is False

    async with db_sessionmaker() as s:
        строки = (
            (
                await s.execute(
                    sa.select(ConversationParticipant).where(
                        ConversationParticipant.conversation_id == свой
                    )
                )
            )
            .scalars()
            .all()
        )
        assert строки == []


async def test_when_the_owner_leaves_the_dialog_leaves_my_list_too(
    client, tokens, чужой, db_sessionmaker
):
    """Ушёл ответственный — диалог ушёл и из «Моих» у заглянувшего.

    ⚠ ИНАЧЕ ДИАЛОГ ОКАЗАЛСЯ БЫ ОДНОВРЕМЕННО НИЧЬИМ И «МОИМ». Сторож отнимает
    ответственного у отсутствующего и возвращает диалог в общую очередь.
    Зашедший не взял бы его из очереди, считая своим, а очередь звала бы
    остальных — ровно та пара «двое пишут одному клиенту», против которой
    сделана вся очередь.

    Держится это условием в `mine_condition`, а не уборкой строк: снимать
    участие крючками пришлось бы в пяти местах, где диалог теряет
    ответственного.
    """
    await client.post(f"/api/v1/conversations/{чужой}/enter", headers=auth(tokens))
    assert str(чужой) in await мои(client, tokens)

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, чужой)
        conv.assignee_id = None
        await s.commit()

    assert str(чужой) not in await мои(client, tokens), "ничей диалог остался в «Моих»"


async def test_an_invitation_survives_the_owner_leaving(
    client, tokens, чужой, db_sessionmaker, users_by_role
):
    """А приглашение так не гаснет: позвали — значит позвали.

    Обратная половина правила. Без неё «зашёл» и «позвали» снова склеились бы в
    одно, и позванный терял бы диалог из списка ровно тогда, когда помощь и
    нужна: ответственный ушёл, разбираться остался он.
    """
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, чужой)
        await participants.invite(
            s, conv, who=users_by_role["manager"], actor=users_by_role["admin"]
        )
        conv.assignee_id = None
        await s.commit()

    assert str(чужой) in await мои(client, tokens), "позванный потерял диалог из «Моих»"


async def статус(client, tokens, conv_id, role="manager") -> str:
    r = await client.get(f"/api/v1/conversations/{conv_id}", headers=auth(tokens, role))
    assert r.status_code == 200, r.text
    return r.json()["status"]


async def test_a_guest_cannot_close_someone_elses_dialog(client, tokens, чужой):
    """Зашёл сам — закрыть чужой диалог нельзя (жалоба владельца 04.09).

    ⚠ ДОСЛОВНО (имя заменено): «я когда закрываю диалог у себя, он так же
    закрывается у Петрова Ивана». Диалог у гостя стоит в «Моих» рядом со своими,
    и «Закрыть» читается как «убрать у себя» — а закрывает разговор клиенту. В
    бою это стоило живого диалога: закрыт в 14:38, клиент написал в 14:42.

    Убирает у себя «Выйти»; закрытие остаётся действием хозяина.
    """
    await client.post(f"/api/v1/conversations/{чужой}/enter", headers=auth(tokens))

    r = await client.patch(
        f"/api/v1/conversations/{чужой}/status",
        json={"status": "closed"},
        headers=auth(tokens),
    )
    assert r.status_code == 403, r.text
    assert r.json()["error"]["details"]["reason"] == "guest_cannot_close"
    assert await статус(client, tokens, чужой) == "in_progress", "диалог всё-таки закрылся"


async def test_leaving_removes_the_dialog_only_from_my_list(client, tokens, чужой, users_by_role):
    """«Выйти» убирает диалог у гостя и не трогает хозяина."""
    await client.post(f"/api/v1/conversations/{чужой}/enter", headers=auth(tokens))
    assert str(чужой) in await мои(client, tokens)

    я = users_by_role["manager"].id
    r = await client.delete(f"/api/v1/conversations/{чужой}/participants/{я}", headers=auth(tokens))
    assert r.status_code in (200, 204), r.text

    assert str(чужой) not in await мои(client, tokens)
    assert await статус(client, tokens, чужой) == "in_progress", "выход закрыл диалог хозяину"
    assert str(чужой) in await мои(client, tokens, "admin"), "диалог пропал у хозяина"


async def test_an_invited_colleague_still_closes_the_dialog(client, tokens, чужой, users_by_role):
    """Позванного запрет не касается: его позвали помогать, права он получил.

    ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА ЗДЕСЬ ОБЯЗАТЕЛЬНА. Без неё запрет «гость не
    закрывает» с тем же успехом мог бы читаться как «участник не закрывает» —
    и тихо отнял бы у позванного коллеги то, ради чего его звали.
    """
    r = await client.post(
        f"/api/v1/conversations/{чужой}/participants",
        json={"user_id": str(users_by_role["manager"].id), "reason": "нужен мастер"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code in (200, 201), r.text

    r = await client.patch(
        f"/api/v1/conversations/{чужой}/status",
        json={"status": "closed"},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assert await статус(client, tokens, чужой, "admin") == "closed"


async def test_a_colleague_who_never_entered_still_closes_the_dialog(client, tokens, чужой):
    """Запрет узкий: он про участие, а не про чужие диалоги вообще.

    Закрывать чужой диалог из «Все» и «Разбора» приходилось 19 раз за 30 дней —
    администратору чаще всех. Эту дорогу мы не трогаем.
    """
    r = await client.patch(
        f"/api/v1/conversations/{чужой}/status",
        json={"status": "closed"},
        headers=auth(tokens),
    )
    assert r.status_code == 200, r.text
    assert await статус(client, tokens, чужой, "admin") == "closed"


async def test_leaving_after_a_silent_entry_is_silent_too(
    client, tokens, чужой, users_by_role, db_sessionmaker
):
    """Зашёл тихо — тихо и вышел (04.09).

    ⚠ ИНАЧЕ ХОЗЯИН ЧИТАЕТ НОВОСТЬ О СОБЫТИИ, НАЧАЛА КОТОРОГО НЕ ВИДЕЛ. Вход не
    пишет в ленту ни строки, а выход писал: «Иванов вышел(а) из диалога» — при
    том что о приходе Иванова никто не объявлял.
    """
    await client.post(f"/api/v1/conversations/{чужой}/enter", headers=auth(tokens))
    async with db_sessionmaker() as s:
        было = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(Message)
                .where(Message.conversation_id == чужой, Message.direction == "system")
            )
        ).scalar_one()

    я = users_by_role["manager"].id
    r = await client.delete(f"/api/v1/conversations/{чужой}/participants/{я}", headers=auth(tokens))
    assert r.status_code in (200, 204), r.text

    async with db_sessionmaker() as s:
        стало = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(Message)
                .where(Message.conversation_id == чужой, Message.direction == "system")
            )
        ).scalar_one()
    assert стало == было, "выход зашедшего написал в ленту"


async def test_removing_an_invited_colleague_is_still_announced(
    client, tokens, чужой, users_by_role, db_sessionmaker
):
    """Позванного объявляли — и уход из приглашения объявляют тоже.

    ⚠ ОТРИЦАТЕЛЬНАЯ ПРОВЕРКА К ПРЕДЫДУЩЕЙ: «тихо» не должно расползтись на всё
    участие сразу, иначе из ленты пропадёт след настоящего события.
    """
    r = await client.post(
        f"/api/v1/conversations/{чужой}/participants",
        json={"user_id": str(users_by_role["manager"].id), "reason": "нужен мастер"},
        headers=auth(tokens, "admin"),
    )
    assert r.status_code in (200, 201), r.text

    async with db_sessionmaker() as s:
        было = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(Message)
                .where(Message.conversation_id == чужой, Message.direction == "system")
            )
        ).scalar_one()

    я = users_by_role["manager"].id
    await client.delete(f"/api/v1/conversations/{чужой}/participants/{я}", headers=auth(tokens))

    async with db_sessionmaker() as s:
        стало = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(Message)
                .where(Message.conversation_id == чужой, Message.direction == "system")
            )
        ).scalar_one()
    assert стало == было + 1, "уход позванного пропал из ленты"
