"""Адрес в карточке: ручной ввод, решения по распознанному, подпись и журнал.

⚠ ВОПРОС ВЛАДЕЛЬЦА 09.09, ДОСЛОВНО: «И куда привязывается адрес, не могу найти».
Ответ на тот момент был «никуда»: запись в таблицу выкачена, а экрана и ручек у
адреса не было вовсе. Здесь охраняется путь, которым адрес попадает в карточку —
и то, что путь этот РОВНО ОДИН на каждый исход.

ЧЕМ АДРЕС ОТЛИЧАЕТСЯ ОТ ТЕЛЕФОНА, И ПОЧЕМУ ЭТО ВАЖНО ИМЕННО ЗДЕСЬ. Цена ошибки
у номера — звонок постороннему; у адреса — бригада, уехавшая не туда, то есть
потерянный день и клиент, прождавший впустую. С 18.09 адрес пишет автоматика
по степени строки (`clients.candidate_grade`: точка дома, приблизительная
точка, текст без точки — `tests/unit/test_autobind_card_1809.py`); человек
только меняет («изменить», PUT) или отказывает строке («Не адрес»), и его
правку автоматика не трогает. `replace`/`add` остаются ручками для CLI и
тестов. Каждое решение человека обязано быть названо в журнале: через месяц
вопросы «почему в карточке этот адрес» и «почему его там нет» одинаково
законны.

⚠ ОТДЕЛЬНО СТЕРЕЖЁТСЯ ПОЛНОТА ЖУРНАЛА. У телефона строки пишутся на все три
исхода с 12 августа, у адреса «добавить» и «не адрес» сначала не писались вовсе:
оба решения карточку не меняют, и без записи выглядят как бездействие. Это тот
самый класс «два пути делают одно дело по-разному», которым в этом проекте
кончалась не одна беда.
"""

import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from app.models import AuditLog, Client, ClientAddressCandidate
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_PENDING, CANDIDATE_REJECTED
from app.services import address_parse, app_settings
from app.services import clients as clients_svc

pytestmark = pytest.mark.anyio

#: Ровно так адрес пишут в переписке: без «улица», с домом через пробел.
СООБЩЕНИЕ = "Приезжайте на ул. Ленина 5, кв 3"
#: Канон разбора — «улица, дом» с типом, ровно как его строит `Found.value`.
АДРЕС = "ул. Ленина, 5"
ДРУГОЙ = "Мира, 12"


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def url_адрес(client_id) -> str:
    return f"/api/v1/clients/{client_id}/address"


def url_решение(client_id, candidate_id) -> str:
    return f"/api/v1/clients/{client_id}/address-candidates/{candidate_id}/resolve"


@pytest.fixture
async def seeded(seed_conversation, db_sessionmaker):
    """Карточка без адреса и одно распознанное предложение, ждущее решения.

    Предложение кладётся боевым путём — разбором текста и
    `record_address_candidate`, — а не вставкой строки: набор обязан краснеть и
    тогда, когда разъедутся разбор и запись.
    """
    async with db_sessionmaker() as session:
        card = await session.get(Client, seed_conversation.client_id)
        assert card is not None
        card.address = None
        found = address_parse.parse(СООБЩЕНИЕ)
        assert found is not None and found.value == АДРЕС, "разбор перестал брать образец"
        await clients_svc.record_address_candidate(
            session,
            client=card,
            conversation_id=seed_conversation.conversation_id,
            message_id=seed_conversation.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        await session.commit()
        row = (await session.execute(sa.select(ClientAddressCandidate))).scalar_one()
        seed_conversation.candidate_id = row.id
    return seed_conversation


async def _карточка(db_sessionmaker, client_id) -> Client:
    async with db_sessionmaker() as session:
        card = await session.get(Client, client_id)
        assert card is not None
        return card


async def _строки(db_sessionmaker, client_id) -> dict[str, str]:
    """Все предложения адреса этой карточки: значение → состояние."""
    async with db_sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    sa.select(ClientAddressCandidate).where(
                        ClientAddressCandidate.client_id == client_id
                    )
                )
            )
            .scalars()
            .all()
        )
    return {r.value: r.status for r in rows}


async def _действия(db_sessionmaker) -> list[str]:
    async with db_sessionmaker() as session:
        return list((await session.execute(sa.select(AuditLog.action))).scalars().all())


async def _личность(client, tokens, client_id) -> dict:
    res = await client.get(f"/api/v1/clients/{client_id}/identity", headers=hdr(tokens["manager"]))
    assert res.status_code == 200, res.text
    return res.json()


# --- ручной ввод --------------------------------------------------------------


async def test_адрес_руками_встаёт_в_карточку(client, tokens, seeded, db_sessionmaker):
    """Главный ответ на вопрос владельца: адрес привязывается К КАРТОЧКЕ клиента.

    Не к диалогу: у одного человека диалогов бывает девять, и адрес выезда у
    него при этом один.
    """
    res = await client.put(
        url_адрес(seeded.client_id),
        json={"address": "Ленина 5, кв 3", "conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    assert res.json() == {"address": "Ленина 5, кв 3", "changed": True}

    card = await _карточка(db_sessionmaker, seeded.client_id)
    assert card.address == "Ленина 5, кв 3"
    # Буквы оператора не переписываем: канона у адреса нет, и приводить «Ленина
    # 5» к «Ленина, 5» значило бы спорить с тем, кто только что говорил с
    # клиентом.
    assert card.address_set_at is not None
    assert card.address_conversation_id == seeded.conversation_id

    личность = await _личность(client, tokens, seeded.client_id)
    assert личность["address"] == "Ленина 5, кв 3"
    assert личность["address_source"] == "manual"
    assert "client.address_captured" in await _действия(db_sessionmaker)


async def test_правка_адреса_это_другое_событие(client, tokens, seeded, db_sessionmaker):
    """Вторая правка — `edited`, а не второй `captured`.

    Иначе метрика «собрано адресов» посчитала бы одну опечатку, поправленную
    трижды, за три собранных адреса — ровно та беда, от которой у телефона
    события разведены.
    """
    for текст in ("Ленина 5", "Ленина 5, кв 3"):
        res = await client.put(
            url_адрес(seeded.client_id),
            json={"address": текст, "conversation_id": str(seeded.conversation_id)},
            headers=hdr(tokens["manager"]),
        )
        assert res.status_code == 200, res.text

    действия = await _действия(db_sessionmaker)
    assert действия.count("client.address_captured") == 1
    assert действия.count("client.address_edited") == 1


async def test_повтор_того_же_адреса_журнал_не_плодит(client, tokens, seeded, db_sessionmaker):
    """Нажали дважды подряд — записи не появилось: `changed=false`.

    Кнопка приезжает и из устаревшего кадра карточки, и падать на этом нельзя,
    и плодить строку журнала — тоже.
    """
    тело = {"address": "Ленина 5", "conversation_id": str(seeded.conversation_id)}
    first = await client.put(url_адрес(seeded.client_id), json=тело, headers=hdr(tokens["manager"]))
    second = await client.put(
        url_адрес(seeded.client_id), json=тело, headers=hdr(tokens["manager"])
    )
    assert first.json()["changed"] is True
    assert second.json()["changed"] is False
    assert (await _действия(db_sessionmaker)).count("client.address_captured") == 1


async def test_пустая_строка_стирает_адрес(client, tokens, seeded, db_sessionmaker):
    """Стереть — законное действие: клиент передумал и назвал другой адрес.

    Вместе с адресом обязаны уйти и признаки происхождения, иначе карточка
    подпишет пустоту словами «внесён вручную».
    """
    await client.put(
        url_адрес(seeded.client_id),
        json={"address": "Ленина 5", "conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    res = await client.put(
        url_адрес(seeded.client_id),
        json={"address": "", "conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["address"] is None

    card = await _карточка(db_sessionmaker, seeded.client_id)
    assert card.address is None
    assert card.address_set_at is None
    assert card.address_set_by_id is None
    assert (await _личность(client, tokens, seeded.client_id))["address_source"] == "none"


async def test_чужой_диалог_в_теле_отвергается(client, tokens, seeded, db_sessionmaker):
    """Диалог другого клиента подставить нельзя — иначе адрес получит ложное «где».

    Подпись «из диалога такого-то» — это доказательство, по которому оператор
    проверяет догадку. Доказательство от постороннего человека хуже, чем его
    отсутствие.
    """
    async with db_sessionmaker() as session:
        чужой = Client(id=uuid.uuid4(), external_id=f"u{uuid.uuid4().hex[:8]}")
        session.add(чужой)
        await session.commit()
        чужой_id = чужой.id

    res = await client.put(
        url_адрес(чужой_id),
        json={"address": "Ленина 5", "conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 422, res.text
    assert res.json()["error"]["code"] == "conversation_mismatch"
    assert (await _карточка(db_sessionmaker, чужой_id)).address is None


async def test_слишком_длинная_строка_не_проходит(client, tokens, seeded):
    """Триста символов — это уже не адрес, а вставленная переписка целиком.

    Отвечает 400: длину ловит схема запроса, а не служба, и общий обработчик
    ошибок схемы в этом проекте отвечает именно так.
    """
    res = await client.put(
        url_адрес(seeded.client_id),
        json={"address": "Ленина " + "5" * 400},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 400, res.text
    assert res.json()["error"]["details"]["fields"][0]["field"] == "address"


# --- решения по распознанному -------------------------------------------------


async def test_заменить_ставит_распознанный_адрес(client, tokens, seeded, db_sessionmaker):
    """«Это адрес выезда» — строка встаёт в карточку с подписью «из диалога».

    Отметок ручного ввода не ставим: подпись «со слов оператора» под тем, что
    вычитано из переписки, ручается за догадку чужим авторитетом.
    """
    res = await client.post(
        url_решение(seeded.client_id, seeded.candidate_id),
        json={"decision": "replace"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    # ⚠ С КВАРТИРОЙ (правка 11.09). До неё кнопка писала «улица, дом» и теряла
    # «кв 3», названную клиентом в той же фразе, — мастер приезжал к подъезду.
    # Текст собирает один сборщик на кнопку и на автозапись.
    assert res.json()["address"] == f"{АДРЕС}, кв 3"
    assert res.json()["candidate"]["status"] == "accepted"

    card = await _карточка(db_sessionmaker, seeded.client_id)
    assert card.address == f"{АДРЕС}, кв 3"
    assert card.address_set_at is None
    assert card.address_conversation_id == seeded.conversation_id
    # Происхождение — по связи со строкой, а не по тексту.
    assert card.address_candidate_id == seeded.candidate_id

    личность = await _личность(client, tokens, seeded.client_id)
    assert личность["address_source"] == "dialog"
    # Принятое и ставшее основным не обязано повторяться вторым списком.
    assert личность["addresses"] == []
    assert личность["address_candidates"] == []
    assert "client.address_captured" in await _действия(db_sessionmaker)


async def test_добавить_карточку_не_трогает_но_пишется(client, tokens, seeded, db_sessionmaker):
    """«Адрес его, но выезд по другому» — безопасный ответ для второго адреса.

    Карточка не меняется, и ровно поэтому решение обязано быть в журнале: иначе
    оно неотличимо от бездействия, а второй адрес человека — вещь, о которой
    через месяц спрашивают.
    """
    await client.put(
        url_адрес(seeded.client_id),
        json={"address": ДРУГОЙ, "conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    res = await client.post(
        url_решение(seeded.client_id, seeded.candidate_id),
        json={"decision": "add"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["address"] == ДРУГОЙ

    assert (await _карточка(db_sessionmaker, seeded.client_id)).address == ДРУГОЙ
    assert await _строки(db_sessionmaker, seeded.client_id) == {АДРЕС: CANDIDATE_ACCEPTED}

    личность = await _личность(client, tokens, seeded.client_id)
    assert [a["value"] for a in личность["addresses"]] == [АДРЕС]
    assert личность["address_candidates"] == []
    assert "client.address_candidate_accepted" in await _действия(db_sessionmaker)


async def test_не_адрес_помнится_и_пишется(client, tokens, seeded, db_sessionmaker):
    """«Не адрес» — строка остаётся с пометкой, и решение названо в журнале.

    Без пометки то же место предлагалось бы заново после каждого следующего
    сообщения; без строки журнала на вопрос «кто сказал, что это не адрес»
    ответить нечем.
    """
    res = await client.post(
        url_решение(seeded.client_id, seeded.candidate_id),
        json={"decision": "reject"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    assert res.json()["address"] is None

    assert await _строки(db_sessionmaker, seeded.client_id) == {АДРЕС: CANDIDATE_REJECTED}
    личность = await _личность(client, tokens, seeded.client_id)
    assert личность["address_candidates"] == []
    assert личность["addresses"] == []
    assert "client.address_candidate_rejected" in await _действия(db_sessionmaker)


async def test_повторное_решение_отвергается(client, tokens, seeded):
    """Второе решение по той же строке — 409, а не молчаливая перезапись.

    Два диспетчера смотрят в одну карточку; проигравший обязан узнать, что
    решение уже принято, а не увидеть свой ответ поверх чужого.
    """
    for _ in range(1):
        первый = await client.post(
            url_решение(seeded.client_id, seeded.candidate_id),
            json={"decision": "add"},
            headers=hdr(tokens["manager"]),
        )
    второй = await client.post(
        url_решение(seeded.client_id, seeded.candidate_id),
        json={"decision": "reject"},
        headers=hdr(tokens["manager"]),
    )
    assert первый.status_code == 200, первый.text
    assert второй.status_code == 409, второй.text
    assert второй.json()["error"]["code"] == "already_resolved"


async def test_негодное_слово_решением_не_считается(client, tokens, seeded, db_sessionmaker):
    """Три исхода — это три исхода: четвёртого экран не рисует."""
    res = await client.post(
        url_решение(seeded.client_id, seeded.candidate_id),
        json={"decision": "maybe"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 422, res.text
    assert await _строки(db_sessionmaker, seeded.client_id) == {АДРЕС: CANDIDATE_PENDING}


async def test_предложение_чужой_карточки_не_найдено(client, tokens, seeded, db_sessionmaker):
    """Идентификатор в адресе не даёт решать за чужую карточку."""
    async with db_sessionmaker() as session:
        чужой = Client(id=uuid.uuid4(), external_id=f"u{uuid.uuid4().hex[:8]}")
        session.add(чужой)
        await session.commit()
        чужой_id = чужой.id

    res = await client.post(
        url_решение(чужой_id, seeded.candidate_id),
        json={"decision": "replace"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 404, res.text
    assert await _строки(db_sessionmaker, seeded.client_id) == {АДРЕС: CANDIDATE_PENDING}


# --- что показываем -----------------------------------------------------------


async def test_уровень_ниже_разрешённого_не_показывается(client, tokens, seeded, db_sessionmaker):
    """Показываем не всё распознанное, а разрешённые руководителем уровни.

    Уровень C — широкий невод: 2 619 находок за 30 дней, половина ложных по
    замеру источника. Записываем всё (иначе качество не померить), показываем
    названное настройкой.
    """
    async with db_sessionmaker() as session:
        row = await session.get(ClientAddressCandidate, seeded.candidate_id)
        assert row is not None
        row.level = "C"
        await session.commit()

    assert (await _личность(client, tokens, seeded.client_id))["address_candidates"] == []

    async with db_sessionmaker() as session:
        await app_settings.set_many(
            session, {app_settings.ADDRESS_DETECT_LEVELS: "AC"}, user_id=None
        )
        await session.commit()

    показаны = (await _личность(client, tokens, seeded.client_id))["address_candidates"]
    assert [c["value"] for c in показаны] == [АДРЕС]
    # Цитата обязана ехать вместе с предложением: без неё оператор либо примет
    # догадку не глядя, либо перестанет её замечать.
    assert показаны[0]["raw"] in СООБЩЕНИЕ
    assert показаны[0]["parts"] == {"office": "3"}


async def test_ручной_ввод_гасит_совпавшее_предложение(client, tokens, seeded, db_sessionmaker):
    """Вписали руками то же самое — спрашивать больше не о чем.

    Иначе карточка предлагала бы подтвердить адрес, который в ней уже стоит, и
    оператор приучался бы нажимать «подтвердить», не читая.
    """
    res = await client.put(
        url_адрес(seeded.client_id),
        json={"address": АДРЕС, "conversation_id": str(seeded.conversation_id)},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text

    assert await _строки(db_sessionmaker, seeded.client_id) == {АДРЕС: CANDIDATE_ACCEPTED}
    личность = await _личность(client, tokens, seeded.client_id)
    assert личность["address_candidates"] == []
    # Основной адрес вторым списком не дублируется.
    assert личность["addresses"] == []


async def test_принятая_строка_видна_в_прочих_адресах_и_при_пустой_карточке(
    client, tokens, seeded, db_sessionmaker
):
    """«Добавить» в пустую карточку: строка принята, адреса в поле нет — и она
    обязана быть в «ещё адреса». `None not in (value, None)` прятала её
    (ревью 11.09)."""
    res = await client.post(
        url_решение(seeded.client_id, seeded.candidate_id),
        json={"decision": "add"},
        headers=hdr(tokens["manager"]),
    )
    assert res.status_code == 200, res.text
    личность = await _личность(client, tokens, seeded.client_id)
    assert личность["address"] is None
    assert [a["value"] for a in личность["addresses"]] == [АДРЕС]


async def test_карта_выключена_под_предложением_нет_вечного_проверяем(
    client, tokens, seeded, db_sessionmaker
):
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_ENABLED: False}, user_id=None)
        await s.commit()
    личность = await _личность(client, tokens, seeded.client_id)
    (предложение,) = личность["address_candidates"]
    assert предложение["geo"] is None
