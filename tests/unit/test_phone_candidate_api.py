"""Ручка решения по номеру, распознанному в переписке (правка 10 от 12 августа).

Правила трёх исходов проверяет `tests/unit/test_phone_from_text.py` — там же,
где живёт сам разбор. Здесь охраняется то, что видно только снаружи, со стороны
экрана:

* решение нельзя принять за ЧУЖУЮ карточку, подставив идентификатор в адрес;
* негодное слово, повторное решение и несуществующий номер отвечают тремя
  разными статусами — экран рисует на них три разных сообщения, а не одно
  «что-то пошло не так»;
* право то же, каким телефон правят руками: разреши одно и запрети другое —
  получится закрытая дверь при открытом окне;
* карточка получает то же поле `twins`, что и после ручного ввода, — иначе
  кнопка «Объединить» рисовалась бы двумя разъезжающимися кусками кода.

И отдельно — контракт карточки: `phone_source` с `phone_candidates` в ответе
`/identity`. Без них экран не отличит номер, набранный со слов клиента, от
вычитанного из текста, и подпись под телефоном станет догадкой.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Client, ClientPhoneCandidate, Conversation
from app.services import clients as clients_svc
from app.services import phone_parse

pytestmark = pytest.mark.anyio

НОМЕР = "+79001112240"
#: Ровно как человек написал его в боевом диалоге Анны Сергеевны.
СЫРОЙ = "+7(900)1112240"


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def url(client_id, candidate_id) -> str:
    return f"/api/v1/clients/{client_id}/phone-candidates/{candidate_id}/resolve"


@pytest.fixture
async def seeded(seed_conversation, db_sessionmaker):
    """Карточка без телефона и один распознанный номер, ждущий решения.

    Кандидат кладётся боевым путём (`record_phone_candidate`), а не вставкой
    строки: тест обязан ломаться и тогда, когда разъедутся ручка и запись.
    """
    async with db_sessionmaker() as session:
        client_row = await session.get(Client, seed_conversation.client_id)
        assert client_row is not None
        client_row.phone = None
        await clients_svc.record_phone_candidate(
            session,
            client=client_row,
            conversation_id=seed_conversation.conversation_id,
            message_id=seed_conversation.message_id,
            message_at=datetime.now(UTC),
            found=phone_parse.Found(value=НОМЕР, raw=СЫРОЙ, start=0, end=len(СЫРОЙ)),
            now=datetime.now(UTC),
        )
        await session.commit()
        candidate = (await session.execute(sa.select(ClientPhoneCandidate))).scalar_one()
        seed_conversation.candidate_id = candidate.id
    return seed_conversation


async def _actions(db_sessionmaker) -> list[str]:
    async with db_sessionmaker() as session:
        return list((await session.execute(sa.select(AuditLog.action))).scalars().all())


# --- три исхода ---------------------------------------------------------------


async def test_replace_puts_the_number_into_the_card(client, tokens, seeded, db_sessionmaker):
    """«Заменить» — это его основной телефон: номер встаёт в поле карточки."""
    res = await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "replace"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["phone"] == НОМЕР
    assert body["candidate"]["status"] == "accepted"

    async with db_sessionmaker() as session:
        row = await session.get(Client, seeded.client_id)
        assert row is not None
        assert row.phone == НОМЕР
        # Руками номер не вводили — человек только согласился. Подпись
        # «внесён вручную» под вычитанным из переписки номером была бы враньём
        # о происхождении цифр.
        assert row.phone_set_at is None


async def test_add_keeps_the_main_phone_untouched(client, tokens, seeded, db_sessionmaker):
    """«Добавить» — телефон его, но основной другой («звоните жене»).

    Поле карточки не трогается вовсе; номер виден списком в `/identity`. Иначе
    второй номер вытеснял бы первый, по которому уже звонили.
    """
    async with db_sessionmaker() as session:
        row = await session.get(Client, seeded.client_id)
        assert row is not None
        row.phone = "+79001112253"
        await session.commit()

    res = await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "add"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["phone"] == "+79001112253"

    res = await client.get(
        f"/api/v1/clients/{seeded.client_id}/identity", headers=hdr(tokens["manager"])
    )
    values = [p["value"] for p in res.json()["phones"]]
    assert values == ["+79001112253", НОМЕР]
    assert "client.phone_candidate_accepted" in await _actions(db_sessionmaker)


async def test_reject_leaves_the_card_alone_and_is_remembered(
    client, tokens, seeded, db_sessionmaker
):
    """«Отклонить» — это не телефон (код домофона, артикул, номер квартиры).

    Строка остаётся с пометкой: без неё тот же номер из того же сообщения
    предлагался бы заново, и люди перестали бы читать подсказки за неделю.
    """
    res = await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "reject"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["phone"] is None
    assert res.json()["candidate"]["status"] == "rejected"
    assert "client.phone_candidate_rejected" in await _actions(db_sessionmaker)

    res = await client.get(
        f"/api/v1/clients/{seeded.client_id}/identity", headers=hdr(tokens["manager"])
    )
    assert res.json()["phone_candidates"] == []


async def test_the_decision_is_never_anonymous(
    client, tokens, users_by_role, seeded, db_sessionmaker
):
    """Через месяц вопрос «кто вписал в карточку этот номер» обязан иметь ответ.

    У автоматической записи автора нет, и это видно по пустому
    `resolved_by_id`; у нажатой кнопки автор есть всегда.
    """
    await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "add"},
        headers=hdr(tokens["manager"]),
    )
    async with db_sessionmaker() as session:
        row = await session.get(ClientPhoneCandidate, seeded.candidate_id)
        assert row is not None
        assert row.resolved_by_id == users_by_role["manager"].id
        assert row.resolved_at is not None


# --- отказы -------------------------------------------------------------------


async def test_an_unknown_decision_is_refused_with_422(client, tokens, seeded):
    """Негодное решение — 422, а не 400 от разбора тела.

    Список слов держит одна функция (`clients.resolve_phone_candidate`).
    Объяви поле перечислением в схеме — тот же отказ приходил бы то 400, то
    422 в зависимости от присланного слова, и экран не смог бы отличить
    «так нельзя» от «повторите».
    """
    res = await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "удалить"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 422, res.text
    fields = res.json()["error"]["details"]["fields"]
    assert fields[0]["field"] == "decision"


async def test_a_second_decision_on_the_same_number_is_a_conflict(client, tokens, seeded):
    """Двойной клик не должен переигрывать уже принятое решение.

    409 — «это уже изменили без вас»: экран обязан перечитать карточку, а не
    молча повторить действие с другим исходом.
    """
    first = await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "reject"},
        headers=hdr(tokens["manager"]),
    )
    assert first.status_code == 200, first.text
    second = await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "replace"},
        headers=hdr(tokens["manager"]),
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "already_resolved"


async def test_an_unknown_candidate_is_not_found(client, tokens, seeded):
    res = await client.post(
        url(seeded.client_id, uuid.uuid4()),
        json={"decision": "add"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 404, res.text


async def test_a_candidate_of_another_card_is_not_found(
    client, tokens, seeded, db_sessionmaker, make_avito_account
):
    """Чужой кандидат в адресе — это номер из ЧУЖОЙ переписки в этой карточке.

    Открывший карточку Ольги подставил бы идентификатор кандидата Анны и
    вписал бы её телефон Ольге; выглядело бы это совершенно обычно, а звонок
    ушёл бы постороннему человеку. Отвечаем 404, а не 403: подтверждать
    существование чужой строки тому, кто спросил не про свою карточку, незачем.
    """
    account = await make_avito_account(avito_user_id=222333444, webhook_secret="whsec-2")
    async with db_sessionmaker() as session:
        other = Client(channel="avito", external_id="777099", name="Ольга")
        session.add(other)
        await session.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=other.id,
            status="new",
            last_message_at=datetime.now(UTC),
        )
        session.add(conv)
        await session.flush()
        await clients_svc.record_phone_candidate(
            session,
            client=other,
            conversation_id=conv.id,
            message_id=None,
            message_at=None,
            found=phone_parse.Found(value="+79001112257", raw="89001112257", start=0, end=11),
            now=datetime.now(UTC),
        )
        await session.commit()
        alien = (
            await session.execute(
                sa.select(ClientPhoneCandidate).where(ClientPhoneCandidate.client_id == other.id)
            )
        ).scalar_one()
        alien_id = alien.id

    res = await client.post(
        url(seeded.client_id, alien_id),
        json={"decision": "replace"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 404, res.text

    async with db_sessionmaker() as session:
        mine = await session.get(Client, seeded.client_id)
        assert mine is not None
        assert mine.phone is None


async def test_an_observer_cannot_decide(client, tokens, seeded):
    """Право то же, что у ручной правки телефона: у наблюдателя его нет."""
    res = await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "reject"},
        headers=hdr(tokens["observer"]),
    )
    assert res.status_code == 403, res.text


# --- двойник ------------------------------------------------------------------


async def test_a_twin_card_is_reported_the_same_way_as_after_manual_entry(
    client, tokens, seeded, db_sessionmaker
):
    """Подтверждённый номер — ровно тот случай, когда карточек у человека две.

    Форма ответа та же, что у `PUT /clients/{id}/phone`, и это не совпадение:
    строка «Возможно, это тот же человек» с кнопкой «Объединить» рисуется
    одним куском кода на оба случая.
    """
    async with db_sessionmaker() as session:
        twin = Client(channel="avito", external_id="777042", name="Анна", phone=НОМЕР)
        session.add(twin)
        await session.commit()
        twin_id = twin.id

    res = await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "replace"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    assert [t["id"] for t in res.json()["twins"]] == [str(twin_id)]


async def test_a_rejected_number_never_offers_a_merge(client, tokens, seeded, db_sessionmaker):
    """Оператор только что сказал «номер не его» — предлагать по нему нечего."""
    async with db_sessionmaker() as session:
        session.add(Client(channel="avito", external_id="777042", name="Анна", phone=НОМЕР))
        await session.commit()

    res = await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "reject"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["twins"] == []


# --- контракт карточки --------------------------------------------------------


async def test_the_card_ships_pending_candidates_and_the_origin_of_the_phone(
    client, tokens, seeded
):
    """`/identity` обязан отдавать и предложения, и происхождение номера.

    Без `phone_candidates` экрану нечего показать оператору — распознанный
    номер остался бы лежать в базе; без `phone_source` подпись под телефоном
    («внесён вручную» / «из диалога») стала бы догадкой интерфейса, а карточка
    ручалась бы за чужие цифры своим авторитетом.
    """
    res = await client.get(
        f"/api/v1/clients/{seeded.client_id}/identity", headers=hdr(tokens["manager"])
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["phone_source"] == "none"
    assert [c["phone"] for c in body["phone_candidates"]] == [НОМЕР]
    # `raw` и место разговора — чтобы оператор мог сверить нашу догадку с тем,
    # что клиент написал на самом деле. Предложение, которое нельзя проверить,
    # либо принимают не глядя, либо перестают замечать.
    assert body["phone_candidates"][0]["raw"] == СЫРОЙ
    assert body["phone_candidates"][0]["conversation_id"] == str(seeded.conversation_id)


async def test_the_origin_says_dialog_after_the_number_is_accepted(client, tokens, seeded):
    """Принятый из переписки номер подписывается «из диалога», а не «вручную»."""
    await client.post(
        url(seeded.client_id, seeded.candidate_id),
        json={"decision": "replace"},
        headers=hdr(tokens["manager"]),
    )
    res = await client.get(
        f"/api/v1/clients/{seeded.client_id}/identity", headers=hdr(tokens["manager"])
    )
    body = res.json()
    assert body["phone_source"] == "dialog"
    assert body["phone_manual"] is False
    # Решение принято — спрашивать больше не о чем.
    assert body["phone_candidates"] == []
