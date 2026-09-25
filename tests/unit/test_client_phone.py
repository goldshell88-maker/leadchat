"""Телефон клиента, введённый руками (правка 9 от 12 августа).

ЗАЧЕМ ЭТИ ТЕСТЫ. Карточка показывала «телефон не указан» неизменяемой строкой,
хотя телефон известен у 3 обращений из 37: люди диктуют номер голосом, пишут
его в объявлении или называют мастеру. Всё, что охраняется здесь, — про то,
чтобы записанный руками номер нельзя было ни исказить, ни выдать за
вычитанный из переписки.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Client, Conversation
from app.services.clients import normalize_phone
from app.services.inbound import extract_phone

# --- нормализация -------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+79001112253", "+79001112253"),
        ("89001112253", "+79001112253"),
        ("79001112253", "+79001112253"),
        ("9001112253", "+79001112253"),
        ("+7 900 111-22-53", "+79001112253"),
        ("8 (900) 111 22 53", "+79001112253"),
        # Городской с кодом города — законный рабочий телефон клиента, и
        # `extract_phone` принимает его так же. Отвергнуть его здесь значило бы
        # развести две нормализации.
        ("8 (495) 123-45-67", "+74951234567"),
        ("  +7 900 111 22 53  ", "+79001112253"),
    ],
)
def test_normalizes_every_way_a_human_writes_a_number(raw: str, expected: str):
    assert normalize_phone(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "не знаю",
        "8900111225",  # девять цифр после восьмёрки — не хватает одной
        "890011122537",  # лишняя цифра
        # Десять цифр НЕ с девятки: неотличимо от мобильного, у которого
        # потеряли первую цифру. Дописать `+7` — показать не тот номер.
        "4951234567",
        "+380671234567",  # иностранный
        "89001112253 или 89001112254",  # два номера в одном поле
        "8 (900) 111-22-53 доб. 5",
    ],
)
def test_rejects_everything_that_is_not_one_whole_russian_number(raw: str):
    """Молча взятый «первый попавшийся» номер хуже отказа.

    Оператор увидел бы сохранённое значение, решил, что система его поняла, и
    позвонил по половине введённого.
    """
    assert normalize_phone(raw) is None


def test_manual_and_regex_normalization_agree():
    """Ручной ввод и разбор переписки обязаны давать ОДНУ строку.

    Разойдись они — номер, вычитанный из сообщения, и тот же номер, введённый
    руками, легли бы в базу двумя разными значениями и перестали бы совпадать
    при поиске двойников. Тогда объединение карточек по телефону не сработало
    бы ровно там, где данных больше всего.
    """
    for raw in ("+7 900 111-22-53", "89001112253", "9001112253"):
        assert normalize_phone(raw) == extract_phone(raw)


# --- ручка --------------------------------------------------------------------


@pytest.fixture
async def seeded(seed_conversation, db_sessionmaker):
    async with db_sessionmaker() as session:
        client = await session.get(Client, seed_conversation.client_id)
        assert client is not None
        client.phone = None
        await session.commit()
    return seed_conversation


async def _put_phone(client_http, token: str, client_id, body: dict):
    return await client_http.put(
        f"/api/v1/clients/{client_id}/phone",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )


async def test_manual_phone_is_saved_normalized(client, tokens, seeded, db_sessionmaker):
    resp = await _put_phone(
        client,
        tokens["manager"],
        seeded.client_id,
        {"phone": "8 (900) 111-22-53", "conversation_id": str(seeded.conversation_id)},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone"] == "+79001112253"
    async with db_sessionmaker() as session:
        row = await session.get(Client, seeded.client_id)
        assert row is not None
        assert row.phone == "+79001112253"


async def test_manual_phone_records_who_and_when(
    client, tokens, users_by_role, seeded, db_sessionmaker
):
    """Без автора карточка выдавала бы ручной номер за вычитанный из переписки.

    Подпись «(из диалога)» стоит прямо под номером; если оператор записал его
    со слов клиента, эта подпись — неправда, и спросить через месяц будет не с
    кого.
    """
    await _put_phone(client, tokens["manager"], seeded.client_id, {"phone": "+79001112253"})
    async with db_sessionmaker() as session:
        row = await session.get(Client, seeded.client_id)
        assert row is not None
        assert row.phone_set_by_id == users_by_role["manager"].id
        assert row.phone_set_at is not None
        # Происхождение «с какого нашего канала пришёл номер» у ручного ввода
        # отсутствует, и подставлять сюда аккаунт открытого диалога нельзя:
        # это дало бы автоматике основание подтвердить межканальную склейку
        # номером, которого Авито не присылал.
        assert row.phone_account_id is None


async def test_first_manual_phone_feeds_the_collected_phones_metric(
    client, tokens, seeded, db_sessionmaker
):
    """Первое заполнение = `client.phone_captured` с `source='manual'`.

    Метрика «собрано телефонов» (06 §1.4) уже имеет колонку `by_manual`, и до
    сегодня она всегда была нулевой — ручного ввода в системе не существовало.
    """
    await _put_phone(
        client,
        tokens["manager"],
        seeded.client_id,
        {"phone": "+79001112253", "conversation_id": str(seeded.conversation_id)},
    )
    async with db_sessionmaker() as session:
        rows = list(
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action == "client.phone_captured")
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].details["source"] == "manual"
    assert rows[0].details["conversation_id"] == str(seeded.conversation_id)


async def test_correcting_a_known_phone_is_a_different_event(
    client, tokens, seeded, db_sessionmaker
):
    """Исправление не должно накручивать «собрано телефонов».

    Одна опечатка, поправленная трижды, дала бы «собрано 4 телефона» из
    одного — и квартальный отчёт показал бы работу, которой не было.
    """
    await _put_phone(client, tokens["manager"], seeded.client_id, {"phone": "+79001112253"})
    resp = await _put_phone(client, tokens["manager"], seeded.client_id, {"phone": "+79001112255"})
    assert resp.status_code == 200, resp.text
    async with db_sessionmaker() as session:
        actions = list((await session.execute(sa.select(AuditLog.action))).scalars().all())
        edited = list(
            (
                await session.execute(
                    sa.select(AuditLog).where(AuditLog.action == "client.phone_edited")
                )
            )
            .scalars()
            .all()
        )
    assert actions.count("client.phone_captured") == 1
    assert actions.count("client.phone_edited") == 1
    assert edited[0].details["previous"] == "+79001112253"


async def test_saving_the_same_number_twice_writes_nothing(client, tokens, seeded, db_sessionmaker):
    """Двойной клик по «Сохранить» не должен оставлять след в журнале."""
    await _put_phone(client, tokens["manager"], seeded.client_id, {"phone": "+79001112253"})
    resp = await _put_phone(
        client, tokens["manager"], seeded.client_id, {"phone": "8 900 111-22-53"}
    )
    assert resp.json()["changed"] is False
    async with db_sessionmaker() as session:
        actions = list((await session.execute(sa.select(AuditLog.action))).scalars().all())
    assert actions.count("client.phone_captured") == 1
    assert actions.count("client.phone_edited") == 0


async def test_garbage_is_refused_with_a_human_message(client, tokens, seeded):
    resp = await _put_phone(client, tokens["manager"], seeded.client_id, {"phone": "не знаю"})
    assert resp.status_code == 422
    body = resp.json()["error"]
    assert body["code"] == "invalid_phone"
    # Текст показывается человеку как есть (01 §1.3) — он обязан говорить, что
    # делать, а не «validation error».
    assert "+7" in body["message"]


async def test_twin_card_is_reported_but_does_not_block_saving(
    client, tokens, seeded, db_sessionmaker
):
    """Один человек законно имеет по карточке на каждый наш аккаунт.

    Отказ «такой номер уже есть» заставил бы оператора выбирать между правдой и
    возможностью сохранить.
    """
    async with db_sessionmaker() as session:
        twin = Client(channel="avito", external_id="777042", name="Оля", phone="+79001112253")
        session.add(twin)
        await session.commit()
        twin_id = twin.id
    resp = await _put_phone(
        client, tokens["manager"], seeded.client_id, {"phone": "8 900 111-22-53"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["phone"] == "+79001112253"
    assert [t["id"] for t in body["twins"]] == [str(twin_id)]


async def test_twin_that_is_already_merged_is_not_offered(client, tokens, seeded, db_sessionmaker):
    """Карточку, уже объединённую в третью, предлагать нельзя.

    Это предложение цепочки A -> B -> C, а её «Разъединить» распутать не
    сможет: снимок в журнале описывает ОДНУ операцию, а не дерево.
    """
    async with db_sessionmaker() as session:
        winner = Client(channel="avito", external_id="777500", name="Оля")
        session.add(winner)
        await session.flush()
        merged = Client(
            channel="avito",
            external_id="777042",
            name="Оля Н.",
            phone="+79001112253",
            merged_into_id=winner.id,
            merged_at=datetime.now(UTC),
        )
        session.add(merged)
        await session.commit()
    resp = await _put_phone(client, tokens["manager"], seeded.client_id, {"phone": "+79001112253"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["twins"] == []


async def test_conversation_of_another_client_is_refused(
    client, tokens, seeded, db_sessionmaker, make_avito_account
):
    """`conversation_id` едет в метрику как источник аккаунта — чужой подставлять нельзя."""
    account = await make_avito_account(avito_user_id=222333444, webhook_secret="whsec-2")
    async with db_sessionmaker() as session:
        other_client = Client(channel="avito", external_id="777099")
        session.add(other_client)
        await session.flush()
        other_conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=other_client.id,
            status="new",
            last_message_at=datetime.now(UTC),
        )
        session.add(other_conv)
        await session.commit()
        other_conv_id = other_conv.id
    resp = await _put_phone(
        client,
        tokens["manager"],
        seeded.client_id,
        {"phone": "+79001112253", "conversation_id": str(other_conv_id)},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "conversation_mismatch"


async def test_observer_cannot_edit_the_phone(client, tokens, seeded):
    """У наблюдателя нет `conversations:manage` — карточка для него только чтение."""
    resp = await _put_phone(client, tokens["observer"], seeded.client_id, {"phone": "+79001112253"})
    assert resp.status_code == 403


async def test_manual_phone_reaches_colleagues_open_cards(client, tokens, seeded, redis):
    """Без `client:updated` у коллеги менялась только цифра в строке списка, а
    карточка держала старый номер — и её «изменить» откатывало исправление."""
    from app.ws.events import EVENTS_CHANNEL
    from tests.unit.conftest import drain_events

    pubsub = redis.pubsub()
    await pubsub.subscribe(EVENTS_CHANNEL)

    resp = await _put_phone(
        client,
        tokens["manager"],
        seeded.client_id,
        {"phone": "+79001112240", "conversation_id": str(seeded.conversation_id)},
    )
    assert resp.status_code == 200, resp.text

    updated = [e for e in await drain_events(pubsub) if e.get("type") == "client:updated"]
    assert [e["data"]["client_id"] for e in updated] == [str(seeded.client_id)]
    assert updated[0]["data"]["conversation_id"] == str(seeded.conversation_id)
