"""ЗАВИСШАЯ ПЕРЕДАЧА: ДИАЛОГ, КОТОРЫЙ НЕЛЬЗЯ НИ ПРИНЯТЬ, НИ НАЙТИ.

⚠ БОЕВОЙ СЛУЧАЙ 28.08, слова владельца: «зависла передача диалога, пришлось мне
вручную закрыть диалог и найти его заново… я возвращал его во входящие, и он его
вернул так, при этом его нельзя было принять и пришлось закрывать».

ЧТО ПРОИСХОДИЛО. Передача двухфазная: пока получатель не принял, ответственный
не меняется. Возврат диалога в общую очередь снимал ответственного — а
ПРЕДЛОЖЕНИЕ оставлял. Диалог оказывался разом и ничьим (в очереди), и обещанным
конкретному человеку. На экране это давало тупик: плашка «Иванов Иван →
Петров Пётр: ждёт подтверждения» выигрывала у полосы очереди, кнопки
«Принять» не было вовсе, и единственным выходом оставалось закрыть диалог.

ВТОРАЯ ПОЛОВИНА ЖАЛОБЫ — «непонятно, когда и как тебе передают диалог».
Предложенный диалог получателю негде было увидеть: в «Моих» его нет
(ответственный чужой), в очереди нет (она про ничьи). Единственным следом
оставался колокольчик.
"""

import uuid

import pytest

from app.models import Client, Conversation
from app.services import inbox as inbox_svc
from app.services import transfer as transfer_svc

pytestmark = pytest.mark.anyio


def auth(tokens: dict[str, str], role: str = "manager") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


@pytest.fixture
async def seed(db_sessionmaker, make_avito_account):
    account = await make_avito_account()
    async with db_sessionmaker() as s:
        клиент = Client(channel="avito", external_id="9301", name="Анна Смирнова")
        s.add(клиент)
        await s.flush()
        диалог = Conversation(
            channel="avito",
            external_chat_id="chat-stuck",
            account_id=account.id,
            client_id=клиент.id,
            status="new",
        )
        s.add(диалог)
        await s.commit()
        return диалог.id


def _с_предложением(conv: Conversation, *, кому: uuid.UUID, от: uuid.UUID) -> None:
    conv.transfer_to_id = кому
    conv.transfer_by_id = от
    conv.transfer_comment = "разберись, пожалуйста"


async def test_return_to_queue_takes_the_pending_offer_with_it(
    db_sessionmaker, seed, users_by_role
) -> None:
    """Диалог уехал в общую очередь — предложение конкретному человеку снято.

    Иначе диалог разом ничей и обещанный: ровно то состояние, из которого на
    бою не было выхода.
    """
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed)
        conv.assignee_id = users_by_role["manager"].id
        _с_предложением(conv, кому=users_by_role["admin"].id, от=users_by_role["manager"].id)
        await s.commit()

        conv = await s.get(Conversation, seed)
        inbox_svc.return_to_queue(conv)
        await s.commit()

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed)
        assert conv.assignee_id is None
        assert not transfer_svc.is_pending(conv), (
            "предложение пережило возврат в очередь — диалог снова станет неберущимся"
        )
        assert conv.transfer_comment is None, (
            "комментарий остался объяснением к предложению, которого больше нет"
        )


async def test_a_queued_dialog_with_a_stuck_offer_can_still_be_claimed(
    client, tokens, db_sessionmaker, seed, users_by_role
) -> None:
    """ВТОРОЙ ЗАМОК: даже если предложение как-то уцелело, диалог берётся.

    Строки, застрявшие ДО этой выкатки, живут в базе, и чинить их вручную
    владелец не обязан. Сервер и раньше давал принять такой диалог — тупик был
    на экране (порядок плашек в `ThreadFooter`), — и эта проверка держит
    серверную половину: очередь остаётся очередью.
    """
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed)
        inbox_svc.enter_queue(conv)
        _с_предложением(conv, кому=users_by_role["admin"].id, от=users_by_role["admin"].id)
        await s.commit()

    r = await client.post(f"/api/v1/conversations/{seed}/claim", headers=auth(tokens))
    assert r.status_code == 200, r.text


async def test_the_recipient_sees_the_offered_dialog_in_my_tab(
    client, tokens, db_sessionmaker, seed, users_by_role
) -> None:
    """«Непонятно, когда и как тебе передают диалог» — теперь понятно ГДЕ.

    Предложенный диалог стоит во вкладке «Мои» получателя: там, куда он и так
    смотрит. Ответственный при этом по-прежнему прежний — список не решает за
    человека, он даёт решить.
    """
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed)
        conv.assignee_id = users_by_role["admin"].id
        _с_предложением(conv, кому=users_by_role["manager"].id, от=users_by_role["admin"].id)
        await s.commit()

    r = await client.get("/api/v1/conversations?tab=my", headers=auth(tokens, "manager"))
    assert r.status_code == 200, r.text
    ids = [i["id"] for i in r.json()["items"]]
    assert str(seed) in ids, (
        "переданный диалог не виден получателю нигде — про передачу он узнает "
        "только из колокольчика, а его закрывают не глядя"
    )


async def test_the_giver_still_sees_it_too(
    client, tokens, db_sessionmaker, seed, users_by_role
) -> None:
    """И у передающего диалог остаётся: ответственный не менялся.

    Пропади он у отдающего — вышло бы худшее из возможного: клиент за никем,
    пока предложение висит.
    """
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed)
        conv.assignee_id = users_by_role["manager"].id
        _с_предложением(conv, кому=users_by_role["admin"].id, от=users_by_role["manager"].id)
        await s.commit()

    r = await client.get("/api/v1/conversations?tab=my", headers=auth(tokens))
    assert str(seed) in [i["id"] for i in r.json()["items"]]
