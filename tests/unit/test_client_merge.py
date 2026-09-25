"""Ручное объединение и разъединение карточек клиента (правка 9 от 12 августа).

ОТКУДА ЗАДАЧА. Автоматическая склейка держится на `author_id` Авито и на
телефоне. Телефон известен у 3 обращений из 37; про `author_id` Авито нигде не
обещает, что он общий для разных наших аккаунтов. Значит в подавляющем
большинстве случаев решает человек, читающий переписку, — и ему нужен и способ
свести, и способ развести обратно.

ЧТО ЗДЕСЬ ОХРАНЯЕТСЯ, ПО ВАЖНОСТИ:
1. Объединение НЕ РАЗРУШАЕТ данные: телефоны и идентификаторы обеих карточек
   остаются и показываются списком.
2. «Разъединить» возвращает ТОЧНО исходное состояние.
3. Молча ничего не склеивается: совпадение имени или идентификатора — только
   подсказка. Ровно из-за молчаливой склейки 11 августа под одним именем
   собрались восемь человек из разных городов.
"""

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from app.models import AuditLog, AvitoAccount, Client, Conversation

CLIENT_COLUMNS = [c.name for c in Client.__table__.columns]


@pytest.fixture
async def account(make_avito_account) -> AvitoAccount:
    return await make_avito_account()


@pytest.fixture
def make_client(db_sessionmaker):
    async def _make(external_id: str, **kw: Any) -> Client:
        async with db_sessionmaker() as session:
            row = Client(channel="avito", external_id=external_id, **kw)
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    return _make


@pytest.fixture
def make_conversation(db_sessionmaker, account):
    async def _make(client_id: uuid.UUID) -> Conversation:
        async with db_sessionmaker() as session:
            conv = Conversation(
                channel="avito",
                external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
                account_id=account.id,
                client_id=client_id,
                status="new",
                last_message_at=datetime.now(UTC),
            )
            session.add(conv)
            await session.commit()
            await session.refresh(conv)
            return conv

    return _make


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _merge(client_http, token, target_id, source_id):
    return await client_http.post(
        f"/api/v1/clients/{target_id}/merge",
        json={"source_id": str(source_id)},
        headers=_auth(token),
    )


async def _unmerge(client_http, token, target_id, source_id):
    return await client_http.post(
        f"/api/v1/clients/{target_id}/unmerge",
        json={"source_id": str(source_id)},
        headers=_auth(token),
    )


async def _snapshot_rows(db_sessionmaker) -> dict[str, dict[str, Any]]:
    """Все строки `clients` целиком — чтобы сравнивать состояние ДО и ПОСЛЕ.

    Именно ВСЕ колонки, а не выбранные: забытое поле — это карточка, которая
    после «объединить + разъединить» отличается от исходной, и никто этого не
    заметит, пока не начнут звонить.
    """
    async with db_sessionmaker() as session:
        rows = (await session.execute(sa.select(Client))).scalars().all()
        return {str(r.id): {c: getattr(r, c) for c in CLIENT_COLUMNS} for r in rows}


async def _conversation_owners(db_sessionmaker) -> dict[str, str]:
    async with db_sessionmaker() as session:
        rows = (await session.execute(sa.select(Conversation.id, Conversation.client_id))).all()
        return {str(r.id): str(r.client_id) for r in rows}


# --- объединение --------------------------------------------------------------


async def test_merge_moves_conversations_to_the_winner(
    client, tokens, make_client, make_conversation, db_sessionmaker
):
    winner = await make_client("777001", name="Анна Смирнова", phone="+79001112250")
    loser = await make_client("777042", name="Оля")
    a = await make_conversation(winner.id)
    b = await make_conversation(loser.id)
    c = await make_conversation(loser.id)

    resp = await _merge(client, tokens["manager"], winner.id, loser.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved_conversations"] == 2

    owners = await _conversation_owners(db_sessionmaker)
    assert owners[str(a.id)] == str(winner.id)
    assert owners[str(b.id)] == str(winner.id)
    assert owners[str(c.id)] == str(winner.id)


async def test_merge_keeps_both_phones_and_both_avito_ids(
    client, tokens, make_client, make_conversation
):
    """Объединение НЕ разрушает данные.

    У одного человека законно несколько идентификаторов Авито — по одному на
    каждый наш аккаунт, — и второй телефон («звоните жене») в ремонте техники
    обычное дело. Потерять их значит потерять единственный способ дозвониться.
    """
    winner = await make_client("777001", name="Ольга", phone="+79001112250")
    loser = await make_client("777042", name="Аня Смирнова", phone="+79001112251")
    await _merge(client, tokens["manager"], winner.id, loser.id)

    resp = await client.get(
        f"/api/v1/clients/{winner.id}/identity", headers=_auth(tokens["manager"])
    )
    body = resp.json()
    assert [p["value"] for p in body["phones"]] == ["+79001112250", "+79001112251"]
    assert [i["value"] for i in body["avito_ids"]] == ["777001", "777042"]
    assert [m["id"] for m in body["merged_from"]] == [str(loser.id)]


async def test_name_comes_from_the_card_with_a_phone(client, tokens, make_client):
    """Имя берётся из карточки с подтверждённым телефоном.

    За телефоном стоит разговор с человеком, за именем профиля Авито — анкета,
    которую заводили один раз и могли назвать как угодно («Продам всё», «Ак»).
    """
    winner = await make_client("777001", name="Ак")
    loser = await make_client("777042", name="Анна Смирнова", phone="+79001112250")
    await _merge(client, tokens["manager"], winner.id, loser.id)
    resp = await client.get(
        f"/api/v1/clients/{winner.id}/identity", headers=_auth(tokens["manager"])
    )
    assert resp.json()["name"] == "Анна Смирнова"


async def test_winner_phone_is_never_overwritten(client, tokens, make_client, db_sessionmaker):
    """Номер, по которому уже звонили, не затирается номером из чужой карточки."""
    winner = await make_client("777001", name="Ольга", phone="+79001112250")
    loser = await make_client("777042", name="Оля", phone="+79001112251")
    await _merge(client, tokens["manager"], winner.id, loser.id)
    async with db_sessionmaker() as session:
        row = await session.get(Client, winner.id)
        assert row is not None
        assert row.phone == "+79001112250"


async def test_confidence_is_confirmed_only_when_phones_matched_before_merge(
    client, tokens, make_client
):
    """«Подтверждено» — это совпавшие телефоны ДО объединения, а не после.

    После объединения телефон победителя мог приехать от проигравшего, и
    сравнение живых полей давало бы «подтверждено» у КАЖДОЙ ручной склейки,
    включая ошибочную. Поэтому мера доверия читается из журнала.
    """
    same_a = await make_client("777001", name="Ольга", phone="+79001112250")
    # Оба номера в базе уже нормализованы: и `extract_phone`, и ручной ввод
    # кладут в `clients.phone` только форму `+7XXXXXXXXXX`.
    same_b = await make_client("777042", name="Оля", phone="+79001112250")
    await _merge(client, tokens["manager"], same_a.id, same_b.id)
    body = (
        await client.get(f"/api/v1/clients/{same_a.id}/identity", headers=_auth(tokens["manager"]))
    ).json()
    assert body["merged_from"][0]["confidence"] == "confirmed"

    empty = await make_client("777100", name="Иван")
    named = await make_client("777101", name="Иван", phone="+79001112252")
    await _merge(client, tokens["manager"], empty.id, named.id)
    body2 = (
        await client.get(f"/api/v1/clients/{empty.id}/identity", headers=_auth(tokens["manager"]))
    ).json()
    # Телефон переехал в пустое место — но совпадением это не было.
    assert body2["phone"] == "+79001112252"
    assert body2["merged_from"][0]["confidence"] == "assumed"


# --- разъединение -------------------------------------------------------------


async def test_unmerge_restores_exact_state(
    client, tokens, make_client, make_conversation, db_sessionmaker
):
    """«Разъединить» обязано вернуть ТОЧНО исходное состояние.

    Сравниваются ВСЕ колонки обеих карточек и владельцы всех диалогов. Если
    объединение однажды начнёт трогать поле, которого нет в снимке журнала,
    этот тест упадёт раньше, чем ошибку увидит диспетчер.
    """
    winner = await make_client("777001", name="Ак")
    loser = await make_client("777042", name="Анна Смирнова", phone="+79001112250")
    await make_conversation(winner.id)
    await make_conversation(loser.id)
    await make_conversation(loser.id)

    before_clients = await _snapshot_rows(db_sessionmaker)
    before_owners = await _conversation_owners(db_sessionmaker)

    assert (await _merge(client, tokens["manager"], winner.id, loser.id)).status_code == 200
    resp = await _unmerge(client, tokens["manager"], winner.id, loser.id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["moved_conversations"] == 2

    assert await _snapshot_rows(db_sessionmaker) == before_clients
    assert await _conversation_owners(db_sessionmaker) == before_owners


async def test_unmerge_leaves_conversations_that_appeared_after_the_merge(
    client, tokens, make_client, make_conversation, db_sessionmaker
):
    """Диалог, пришедший победителю ПОСЛЕ объединения, остаётся у победителя.

    Вернуть ему чужой новый диалог было бы не откатом, а второй ошибкой:
    исходное состояние про него ничего не говорит.
    """
    winner = await make_client("777001", name="Ольга")
    loser = await make_client("777042", name="Оля")
    old = await make_conversation(loser.id)
    await _merge(client, tokens["manager"], winner.id, loser.id)
    fresh = await make_conversation(winner.id)

    await _unmerge(client, tokens["manager"], winner.id, loser.id)
    owners = await _conversation_owners(db_sessionmaker)
    assert owners[str(old.id)] == str(loser.id)
    assert owners[str(fresh.id)] == str(winner.id)


async def test_unmerge_keeps_the_phone_typed_after_the_merge(
    client, tokens, make_client, db_sessionmaker
):
    """Номер, вписанный ПОСЛЕ объединения, разъединение не стирает.

    ⚠ ЗЕРКАЛО ПРОВЕРКИ ПРО ДИАЛОГИ, КОТОРОГО НЕ БЫЛО (28.08). Для диалогов
    асимметрия обработана давно — «появившиеся у победителя после объединения
    остаются у него». Для ПОЛЕЙ карточки её не было: `winner.name` и
    `winner.phone` восстанавливались из снимка безусловно.

    Между объединением и разъединением карточку правят свободно: ни `set_name`,
    ни `set_phone`, ни `PUT /clients/{id}/phone` не смотрят на `merged_into_id`.
    Типичный случай — карточки объединили, клиент продиктовал номер, диспетчер
    вписал его победителю; через день выяснилось, что люди разные, и другой
    диспетчер нажал «Разъединить». Номер, по которому собирались звонить,
    исчезал с ОБЕИХ карточек: у победителя откатывался в снимок, у проигравшего
    его никогда и не было. Ни ошибки, ни следа — в журнале обычное разъединение.

    ЧТО ЛОМАЛИ: вернули безусловное восстановление — тест краснеет на «номер
    пережил разъединение».
    """
    winner = await make_client("777001", name="Ольга")
    loser = await make_client("777042", name="Оля")
    assert (await _merge(client, tokens["manager"], winner.id, loser.id)).status_code == 200

    # После объединения клиент продиктовал номер, диспетчер вписал его руками.
    вписан = await client.put(
        f"/api/v1/clients/{winner.id}/phone",
        json={"phone": "+79001112253"},
        headers=_auth(tokens["manager"]),
    )
    assert вписан.status_code in (200, 201), вписан.text

    assert (await _unmerge(client, tokens["manager"], winner.id, loser.id)).status_code == 200

    async with db_sessionmaker() as db:
        карточка = await db.get(Client, winner.id)
        assert карточка is not None
        assert карточка.phone == "+79001112253", (
            "разъединение стёрло номер, вписанный после объединения"
        )


async def test_unmerge_still_rolls_back_untouched_fields(
    client, tokens, make_client, db_sessionmaker
):
    """Обратная сторона: нетронутое поле откатывается, как и прежде.

    Правка не имеет права превратить откат в бездействие: имя, которое принесло
    само объединение, обязано уйти обратно.
    """
    winner = await make_client("777001", name="Ак")
    loser = await make_client("777042", name="Анна Смирнова", phone="+79001112250")
    assert (await _merge(client, tokens["manager"], winner.id, loser.id)).status_code == 200
    assert (await _unmerge(client, tokens["manager"], winner.id, loser.id)).status_code == 200

    async with db_sessionmaker() as db:
        карточка = await db.get(Client, winner.id)
        assert карточка is not None
        assert карточка.name == "Ак"
        assert карточка.phone is None


async def test_unmerge_without_a_journal_record_is_refused(
    client, tokens, make_client, db_sessionmaker
):
    """Снимка нет — отката нет.

    Угадать, что имя пришло от проигравшей карточки, а не стояло у победителя
    изначально, нельзя, и отгадка здесь стоит перепутанного имени в боевой
    карточке.
    """
    winner = await make_client("777001", name="Ольга")
    loser = await make_client("777042", name="Оля")
    await _merge(client, tokens["manager"], winner.id, loser.id)
    async with db_sessionmaker() as session:
        await session.execute(sa.delete(AuditLog).where(AuditLog.action == "client.merged"))
        await session.commit()

    resp = await _unmerge(client, tokens["manager"], winner.id, loser.id)
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "merge_record_missing"


async def test_unmerge_from_the_wrong_card_is_refused(client, tokens, make_client):
    winner = await make_client("777001", name="Ольга")
    loser = await make_client("777042", name="Оля")
    stranger = await make_client("777099", name="Иван")
    await _merge(client, tokens["manager"], winner.id, loser.id)

    resp = await _unmerge(client, tokens["manager"], stranger.id, loser.id)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "not_merged_into_target"


# --- запреты ------------------------------------------------------------------


async def test_chains_are_refused_in_both_directions(client, tokens, make_client):
    """A -> B -> C неразъединяемо: снимок в журнале описывает ОДНУ операцию."""
    a = await make_client("777001", name="Один")
    b = await make_client("777042", name="Два")
    c = await make_client("777099", name="Три")
    await _merge(client, tokens["manager"], a.id, b.id)

    already = await _merge(client, tokens["manager"], c.id, b.id)
    assert already.status_code == 422
    assert already.json()["error"]["code"] == "already_merged"

    into_merged = await _merge(client, tokens["manager"], b.id, c.id)
    assert into_merged.status_code == 422
    assert into_merged.json()["error"]["code"] == "target_is_merged"

    # ⚠ ТРЕТИЙ СПОСОБ ПОСТРОИТЬ ЦЕПОЧКУ, И ЕГО ПРОВЕРКИ НЕ ЛОВИЛИ (найдено 14.08).
    #
    # `a` — победитель: в него объединили `b`, и собственное поле у него пустое.
    # Объединяя ТЕПЕРЬ `a` в `c`, обе прежние проверки проходят: у `a` поле пустое
    # («не объединён»), у `c` тоже. Получается b -> a -> c.
    #
    # ЦЕНА. `unmerge(b)` берёт победителя из `b.merged_into_id` — это `a`, — а диалоги
    # уже уехали к `c`. UPDATE не находит ни строки, карточка разъединяется ПУСТОЙ и
    # рапортует «Диалогов возвращено: 0». «Разъединить» молча не возвращает исходное,
    # а на этом обещании держится доверие к объединению — тем более к автоматическому.
    with_group = await _merge(client, tokens["manager"], c.id, a.id)
    assert with_group.status_code == 422, (
        "цепочку построили в обход: карточку-победителя объединили дальше"
    )
    assert with_group.json()["error"]["code"] == "loser_has_merged"


async def test_card_cannot_be_merged_into_itself(client, tokens, make_client):
    one = await make_client("777001", name="Ольга")
    resp = await _merge(client, tokens["manager"], one.id, one.id)
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "same_client"


async def test_observer_cannot_merge(client, tokens, make_client):
    winner = await make_client("777001", name="Ольга")
    loser = await make_client("777042", name="Оля")
    assert (await _merge(client, tokens["observer"], winner.id, loser.id)).status_code == 403


# --- подсказки ----------------------------------------------------------------


async def test_candidates_rank_phone_as_confirmed_and_name_as_a_guess(client, tokens, make_client):
    """Подсказка обязана нести причину.

    Без неё оператор не отличит совпавший телефон (сильный довод) от
    совпавшего имени — а «Иван» в ремонте техники это каждый десятый. Ровно на
    имени 11 августа под одной карточкой собрались восемь человек из разных
    городов.
    """
    me = await make_client("777001", name="Иван Кузнецов", phone="+79001112250")
    by_phone = await make_client("777042", name="И. Петров", phone="+79001112250")
    # 17.08: тёзка — довод, только когда он с телефоном (объединение с
    # пустышкой не даёт ничего) и когда тёзок мало (толпа «Иванов» — спам).
    # 12.09: и только по ПОЛНОМУ имени (два слова): одно слово «Иван» при
    # 34 аккаунтах — почти случайное совпадение (просьба владельца).
    by_name = await make_client("777099", name="иван кузнецов", phone="+79001112254")
    await make_client("777100", name="Пётр", phone="+79001112252")

    resp = await client.get(
        f"/api/v1/clients/{me.id}/merge-candidates", headers=_auth(tokens["manager"])
    )
    items = resp.json()["items"]
    found = {item["id"]: (item["reason"], item["confidence"]) for item in items}
    assert found[str(by_phone.id)] == ("phone", "confirmed")
    assert found[str(by_name.id)] == ("name", "assumed")
    assert len(found) == 2


async def test_одно_слово_имени_не_подсказка(client, tokens, make_client):
    """«Леся» с двумя другими «Лесями» — не «возможно, тот же человек» (12.09).
    ⚠ ДИВЕРСИЯ: убрать условие «два слова» — краснеет."""
    me = await make_client("777201", name="Леся")
    await make_client("777202", name="Леся", phone="+79001112257")
    await make_client("777203", name="леся", phone="+79001112258")
    resp = await client.get(
        f"/api/v1/clients/{me.id}/merge-candidates", headers=_auth(tokens["manager"])
    )
    assert resp.json()["items"] == []


async def test_namesakes_without_phone_or_in_crowds_are_not_spam(client, tokens, make_client):
    """Жалоба владельца 17.08: карточка предлагала ВСЕХ тёзок подряд.

    Пустышка без телефона — не кандидат (объединять не с чем); три и больше
    тёзок — тоже не кандидаты (само количество говорит «имя массовое»).
    """
    me = await make_client("888001", name="Ольга", phone="+79001112243")
    await make_client("888002", name="Ольга")  # пустышка — вон
    await make_client("888003", name="ольга")  # пустышка — вон
    await make_client("888004", name="Ольга")  # пустышка — вон

    resp = await client.get(
        f"/api/v1/clients/{me.id}/merge-candidates", headers=_auth(tokens["manager"])
    )
    assert resp.json()["items"] == []

    # трое тёзок С телефонами — всё равно толпа, подсказка молчит
    me2 = await make_client("888011", name="Игорь", phone="+79001112244")
    for i in (45, 46, 47):
        await make_client(f"8880{i}", name="Игорь", phone=f"+790011122{i}")
    resp = await client.get(
        f"/api/v1/clients/{me2.id}/merge-candidates", headers=_auth(tokens["manager"])
    )
    assert resp.json()["items"] == []


async def test_candidates_never_merge_anything_by_themselves(
    client, tokens, make_client, make_conversation, db_sessionmaker
):
    """Подсказка — это подсказка. Даже совпавший телефон не склеивает молча."""
    me = await make_client("777001", name="Иван", phone="+79001112250")
    twin = await make_client("777042", name="И. Петров", phone="+79001112250")
    conv = await make_conversation(twin.id)

    await client.get(f"/api/v1/clients/{me.id}/merge-candidates", headers=_auth(tokens["manager"]))
    owners = await _conversation_owners(db_sessionmaker)
    assert owners[str(conv.id)] == str(twin.id)
    async with db_sessionmaker() as session:
        row = await session.get(Client, twin.id)
        assert row is not None
        assert row.merged_into_id is None


async def test_candidates_skip_already_merged_cards(client, tokens, make_client):
    """Предложить объединиться с уже объединённой — предложить цепочку."""
    me = await make_client("777001", name="Иван", phone="+79001112250")
    twin = await make_client("777042", name="Иван", phone="+79001112250")
    third = await make_client("777099", name="Иван")
    await _merge(client, tokens["manager"], third.id, twin.id)

    resp = await client.get(
        f"/api/v1/clients/{me.id}/merge-candidates", headers=_auth(tokens["manager"])
    )
    assert [i["id"] for i in resp.json()["items"]] == [str(third.id)]


async def test_short_names_are_not_a_hint(client, tokens, make_client):
    """«Ак» или «-» склеили бы посторонних, поэтому короткие имена не ищутся."""
    me = await make_client("777001", name="Ак")
    await make_client("777042", name="Ак")
    resp = await client.get(
        f"/api/v1/clients/{me.id}/merge-candidates", headers=_auth(tokens["manager"])
    )
    assert resp.json()["items"] == []


# --- журнал -------------------------------------------------------------------


async def test_journal_keeps_both_cards_and_the_moved_conversations(
    client, tokens, make_client, make_conversation, db_sessionmaker
):
    """Без обеих исходных карточек в журнале откат невозможен, а вопрос
    «почему у этого человека вдруг девять диалогов» останется без ответа."""
    winner = await make_client("777001", name="Ак")
    loser = await make_client("777042", name="Ольга", phone="+79001112250")
    moved = await make_conversation(loser.id)
    await _merge(client, tokens["manager"], winner.id, loser.id)

    async with db_sessionmaker() as session:
        row = (
            await session.execute(sa.select(AuditLog).where(AuditLog.action == "client.merged"))
        ).scalar_one()
    assert row.entity_id == str(winner.id)
    assert row.details["source_id"] == str(loser.id)
    assert row.details["conversation_ids"] == [str(moved.id)]
    assert row.details["before"]["winner"]["name"] == "Ак"
    assert row.details["before"]["winner"]["phone"] is None


# --- распознанные номера при объединении (аудит 19.08, находка L-013) ---------


async def test_merge_does_not_move_or_destroy_phone_candidates(
    client, tokens, make_client, make_conversation, db_sessionmaker
):
    """Объединение НЕ трогает распознанные номера — их некуда было бы возвращать.

    ЧТО БЫЛО. Кандидаты проигравшей карточки переносились на победителя, а
    совпавшие по номеру УДАЛЯЛИСЬ насовсем (UNIQUE(client_id, phone)).
    Разъединение их не восстанавливало: снимка не было. Докстринг
    `unmerge_clients` при этом обещал вернуть «ТОЧНО в исходное состояние» —
    код противоречил собственному комментарию. На бою механизм рабочий:
    16 объединений и 14 разъединений в журнале.

    ЧТО СТАЛО (решение владельца 19.08). Данные не двигаются вовсе, а
    победитель читает номера присоединённых карточек по связи. Тогда
    разъединение честно само собой: возвращать нечего.
    """
    from app.models.client import ClientPhoneCandidate
    from app.services import clients as clients_svc

    winner = await make_client("778001", name="Ольга")
    loser = await make_client("778042", name="Оля")
    conv = await make_conversation(loser.id)

    async with db_sessionmaker() as session:
        session.add_all(
            [
                ClientPhoneCandidate(
                    id=uuid.uuid4(),
                    client_id=winner.id,
                    conversation_id=conv.id,
                    phone="+79001112255",
                    raw="+79001112255",
                    message_id=uuid.uuid4(),
                    message_at=datetime.now(UTC),
                    status="pending",
                    detected_at=datetime.now(UTC),
                ),
                ClientPhoneCandidate(  # тот же номер, что у победителя — раньше УДАЛЯЛСЯ
                    id=uuid.uuid4(),
                    client_id=loser.id,
                    conversation_id=conv.id,
                    phone="+79001112255",
                    raw="+79001112255",
                    message_id=uuid.uuid4(),
                    message_at=datetime.now(UTC),
                    status="pending",
                    detected_at=datetime.now(UTC),
                ),
                ClientPhoneCandidate(  # свой номер проигравшего — раньше ПЕРЕЕЗЖАЛ
                    id=uuid.uuid4(),
                    client_id=loser.id,
                    conversation_id=conv.id,
                    phone="+79001112256",
                    raw="+79001112256",
                    message_id=uuid.uuid4(),
                    message_at=datetime.now(UTC),
                    status="pending",
                    detected_at=datetime.now(UTC),
                ),
            ]
        )
        await session.commit()

    resp = await _merge(client, tokens["manager"], winner.id, loser.id)
    assert resp.status_code == 200, resp.text

    async with db_sessionmaker() as session:
        строки = list(
            (
                await session.execute(
                    sa.select(ClientPhoneCandidate).order_by(ClientPhoneCandidate.phone)
                )
            ).scalars()
        )
        assert len(строки) == 3, "объединение уничтожило распознанный номер"
        по_карточкам = {(str(r.client_id), r.phone) for r in строки}
        assert (str(loser.id), "+79001112255") in по_карточкам, "номер переехал с карточки"
        assert (str(loser.id), "+79001112256") in по_карточкам, "номер переехал с карточки"

        # победитель всё равно ВИДИТ оба номера — чтение ходит по связи
        видит = await clients_svc.phone_candidates(session, winner.id)
        assert {r.phone for r in видит} == {"+79001112255", "+79001112256"}
        assert len(видит) == 3, "победитель обязан видеть и свои, и присоединённые номера"


async def test_unmerge_returns_phone_candidates_untouched(
    client, tokens, make_client, make_conversation, db_sessionmaker
):
    """Разъединение возвращает карточке её номера — потому что они и не уходили."""
    from app.models.client import ClientPhoneCandidate
    from app.services import clients as clients_svc

    winner = await make_client("779001", name="Ольга")
    loser = await make_client("779042", name="Оля")
    conv = await make_conversation(loser.id)
    async with db_sessionmaker() as session:
        session.add(
            ClientPhoneCandidate(
                id=uuid.uuid4(),
                client_id=loser.id,
                conversation_id=conv.id,
                phone="+79001112259",
                raw="+79001112259",
                message_id=uuid.uuid4(),
                message_at=datetime.now(UTC),
                status="pending",
                detected_at=datetime.now(UTC),
            )
        )
        await session.commit()

    assert (await _merge(client, tokens["manager"], winner.id, loser.id)).status_code == 200
    assert (await _unmerge(client, tokens["manager"], winner.id, loser.id)).status_code == 200

    async with db_sessionmaker() as session:
        свои = await clients_svc.phone_candidates(session, loser.id)
        assert [r.phone for r in свои] == ["+79001112259"], (
            "после разъединения карточка потеряла распознанный номер"
        )
        чужие = await clients_svc.phone_candidates(session, winner.id)
        assert чужие == [], "у победителя не должно остаться чужих номеров"


# --- кандидаты присоединённой карточки (владелец 18.09) ------------------------


async def test_адрес_присоединённой_карточки_подтверждается_с_победителя(
    client, tokens, make_client, make_conversation, db_sessionmaker
):
    """Ангарск: карточку объединила автоматика по телефону, а «Подтвердить» на
    адресе присоединённой карточки отвечало «Предложение не найдено» — строка
    осталась у проигравшей (кандидаты при объединении не переезжают, 19.08),
    а показ собирает их со всей группы. Решение — с той же карточки."""
    from app.services import address_parse
    from app.services import clients as clients_svc

    winner = await make_client("777001", name="Павел", phone="+79001112250")
    loser = await make_client("777042", name="Ангарск")
    conv = await make_conversation(loser.id)
    async with db_sessionmaker() as session:
        card = await session.get(Client, loser.id)
        found = address_parse.parse("85-й квартал, 17")
        assert found is not None
        записано = await clients_svc.record_address_candidate(
            session,
            client=card,
            conversation_id=conv.id,
            message_id=None,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        await session.commit()
        cid = записано.candidate_id
    assert (await _merge(client, tokens["manager"], winner.id, loser.id)).status_code == 200
    # Показ победителя видит строку присоединённой карточки…
    identity = await client.get(
        f"/api/v1/clients/{winner.id}/identity", headers=_auth(tokens["manager"])
    )
    assert identity.status_code == 200
    assert any(str(cid) == c["id"] for c in identity.json().get("address_candidates", []))
    # …и решение по ней принимается с победителя, а не отвечает 404.
    res = await client.post(
        f"/api/v1/clients/{winner.id}/address-candidates/{cid}/resolve",
        json={"decision": "replace"},
        headers=_auth(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    async with db_sessionmaker() as session:
        card = await session.get(Client, winner.id)
        assert card.address and "17" in card.address and card.address_candidate_id == cid
    # Чужая карточка (не в группе) по-прежнему 404.
    stranger = await make_client("777099", name="Чужой")
    res = await client.post(
        f"/api/v1/clients/{stranger.id}/address-candidates/{cid}/resolve",
        json={"decision": "reject"},
        headers=_auth(tokens["manager"]),
    )
    assert res.status_code == 404


async def test_unmerge_takes_back_the_phone_found_in_the_losers_dialog(
    client, tokens, make_client, make_conversation, db_sessionmaker
):
    """Номер, пойманный в диалоге проигравшей уже после объединения, — её.

    Оставался у победителя с подписью «(из диалога)» и ссылкой на чужой
    диалог, а у проигравшей номера не было.
    """
    from app.models import ClientPhoneCandidate

    winner = await make_client("777001", name="Ольга")
    loser = await make_client("777042", name="Оля")
    conv = await make_conversation(loser.id)
    assert (await _merge(client, tokens["manager"], winner.id, loser.id)).status_code == 200
    # Автоматика поймала номер в переехавшем диалоге и поставила его победителю.
    async with db_sessionmaker() as s:
        w = await s.get(Client, winner.id)
        w.phone = "+79001112241"
        w.phone_conversation_id = conv.id
        s.add(
            ClientPhoneCandidate(
                client_id=winner.id,
                conversation_id=conv.id,
                phone="+79001112241",
                raw="8 900 111 22 41",
                detected_at=datetime.now(UTC),
            )
        )
        await s.commit()

    resp = await _unmerge(client, tokens["manager"], winner.id, loser.id)
    assert resp.status_code == 200, resp.text

    async with db_sessionmaker() as s:
        w = await s.get(Client, winner.id)
        l_ = await s.get(Client, loser.id)
        owners = [c.client_id for c in (await s.execute(sa.select(ClientPhoneCandidate))).scalars()]
    assert w.phone is None and w.phone_conversation_id is None
    assert l_.phone == "+79001112241" and l_.phone_conversation_id == conv.id
    assert owners == [loser.id]
