"""Задачи проверки по карте и автозаписи: вердикт в строку, адрес в карточку.

Карта подменена записанным ответом — сеть здесь не ходит. Стережётся то, что
владелец просил как «полностью автоматически», и все сторожа, которые не дают
автоматике поставить в карточку чужой дом.

ДИВЕРСИИ (каждая обязана краснеть): снять `WHERE address IS NULL` у условной
записи → «оператор успел раньше» перезаписывается; снять «ровно одна exact в
диалоге» → две строки, и автозапись пишет первую; снять память об отказе →
стёртый адрес возвращается; снять свежесть → вчерашняя строка пишется;
снять замок темпа → два запроса в одну секунду.
"""

import asyncio
import dataclasses
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from arq import Retry

from app.models import AuditLog, Client, ClientAddressCandidate, Conversation, Message
from app.models.client import CANDIDATE_ACCEPTED, CANDIDATE_PENDING, CANDIDATE_REJECTED
from app.services import address_parse, app_settings
from app.services import clients as clients_svc
from app.services import geocode as g
from app.services.geocode_queue import enqueue_geocode
from app.workers import geocode as worker

pytestmark = pytest.mark.anyio

ДОМ = g.GeoHit(
    street="Звенигородская улица",
    house="1",
    settlement="Заречный",
    city="Орск",
    region="Оренбургская область",
    lat=51.2101234,
    lon=58.5012345,
    house_level=True,
)
ФОРМАТ = "Звенигородская улица, 1, посёлок Заречный, Орск"


@pytest.fixture
async def seeded(seed_conversation, db_sessionmaker):
    """Диалог из Орска с одной распознанной строкой уровня A без вердикта."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        # Реплика строки — сам адрес, как на живом пути: автозапись с 19.09
        # перечитывает реплику строки нынешним разбором и речь в карточку не
        # кладёт («Экран разбит, почём?» с домом «1» — речь).
        сообщение = (
            await s.execute(sa.select(Message).where(Message.id == seed_conversation.message_id))
        ).scalar_one()
        сообщение.body = "п заречный ул звенигородская д 1, кв 3"
        found = address_parse.parse(сообщение.body)
        assert found is not None and found.settlement
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=seed_conversation.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        assert записано.впервые and записано.candidate_id is not None
        await s.commit()
        seed_conversation.candidate_id = записано.candidate_id
    return seed_conversation


def ctx(db_sessionmaker, redis, *, job_try: int = 1) -> dict:
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": job_try}


async def строка(db_sessionmaker, cid) -> ClientAddressCandidate:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row is not None
        return row


async def карточка(db_sessionmaker, client_id) -> Client:
    async with db_sessionmaker() as s:
        card = await s.get(Client, client_id)
        assert card is not None
        return card


@pytest.fixture
def карта(monkeypatch):
    """Карта отвечает записанным домом; запросы считаются."""
    вызовы: list[g.Query] = []

    async def search(query, wait=None, **kw):  # noqa: ANN001
        вызовы.append(query)
        if wait is not None:  # как настоящий поиск: замок темпа перед запросом
            await wait()
        return [ДОМ]

    monkeypatch.setattr(worker.nominatim, "search", search)
    return вызовы


# --- проверка строки ----------------------------------------------------------


async def test_вердикт_ложится_в_строку_и_зовёт_автозапись(seeded, db_sessionmaker, redis, карта):
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
    assert итог == g.GEO_EXACT

    row = await строка(db_sessionmaker, seeded.candidate_id)
    assert row.geo_status == g.GEO_EXACT
    assert row.geo_formatted == ФОРМАТ
    assert (row.geo_lat, row.geo_lon) == (ДОМ.lat, ДОМ.lon)
    assert row.geo_provider == "nominatim"
    assert row.geo_attempts == 1
    # Запрос собран из компонентов с городом объявления.
    assert карта == [
        g.Query(
            region="Оренбургская область",
            city="Орск",
            settlement="Заречный",
            street="ул звенигородская",
            house="1",
        )
    ]
    # Кадр «перечитайте карточку» ушёл, автозапись поставлена в очередь через окно тишины.
    from tests.unit.conftest import drain_events

    кадры = await drain_events(pubsub)
    assert any(e.get("type") == "client:updated" for e in кадры)
    assert await redis.exists(f"arq:job:addr-fill:{seeded.conversation_id}")
    # Карточка ЕЩЁ пуста: автозапись — отдельная задача, а не побочный эффект.
    assert (await карточка(db_sessionmaker, seeded.client_id)).address is None


async def test_повторная_проверка_решённой_строки_не_ходит_к_карте(
    seeded, db_sessionmaker, redis, карта
):
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == "already"
    )
    assert len(карта) == 1


async def test_без_города_вердикт_no_city_и_карта_не_спрашивается(
    seeded, db_sessionmaker, redis, карта
):
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seeded.conversation_id)
        conv.item_city_slug = None
        await s.commit()
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_NO_CITY
    )
    assert карта == []
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_status == g.GEO_NO_CITY


async def test_выключатель_останавливает_походы_к_карте(seeded, db_sessionmaker, redis, карта):
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_ENABLED: False}, user_id=None)
        await s.commit()
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == "disabled"
    )
    assert карта == []
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_status == g.GEO_PENDING


async def test_удалённая_строка_это_не_сбой(db_sessionmaker, redis, карта):
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), uuid.uuid4()) == "gone"


async def test_сеть_просит_повтор_а_на_последней_попытке_пишет_error(
    seeded, db_sessionmaker, redis, monkeypatch
):
    async def падает(query, **kw):  # noqa: ANN001
        raise g.GeocodeError("nominatim", "network")

    monkeypatch.setattr(worker.nominatim, "search", падает)
    with pytest.raises(Retry):
        await worker.geocode_candidate(ctx(db_sessionmaker, redis, job_try=1), seeded.candidate_id)
    # Повтор — без записи: попытка считается один раз на жизнь задачи, иначе
    # час недоступности карты сжигал бы весь потолок починки (ревью 11.09).
    row = await строка(db_sessionmaker, seeded.candidate_id)
    assert (row.geo_status, row.geo_attempts) == (g.GEO_PENDING, 0)

    итог = await worker.geocode_candidate(
        ctx(db_sessionmaker, redis, job_try=worker.MAX_TRIES), seeded.candidate_id
    )
    assert итог == g.GEO_ERROR
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_attempts == 1


async def test_блокировка_считается_и_включает_предохранитель(
    seeded, db_sessionmaker, redis, monkeypatch
):
    вызовов = 0

    async def считающий_запрет(query, **kw):  # noqa: ANN001
        nonlocal вызовов
        вызовов += 1
        raise g.GeocodeError("nominatim", "blocked", 403)

    monkeypatch.setattr(worker.nominatim, "search", считающий_запрет)
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
    assert итог == g.GEO_BLOCKED
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_status == g.GEO_BLOCKED
    assert int(await redis.get("geo:blocked:nominatim")) == 1
    # Бан — свойство провайдера: после порога блокировок за час к карте не
    # стучим вовсе, строки остаются ждать (предохранитель, ревью 11.09).
    await redis.set("geo:blocked:nominatim", worker.BLOCKED_THRESHOLD, ex=3600)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id) == "paused"
    )
    assert вызовов == 1


async def test_замок_темпа_не_пускает_второй_запрос_в_ту_же_секунду(redis):
    await worker._ждать_темпа(redis)
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(worker._ждать_темпа(redis), timeout=0.4)


# --- автозапись ---------------------------------------------------------------


async def проверить(seeded, db_sessionmaker, redis, карта):
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_status == g.GEO_EXACT


async def test_автозапись_кладёт_строку_карты_с_частями_в_пустую_карточку(
    seeded, db_sessionmaker, redis, карта
):
    await проверить(seeded, db_sessionmaker, redis, карта)
    pubsub = redis.pubsub()
    await pubsub.subscribe("events")

    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id)
        == "filled"
    )

    card = await карточка(db_sessionmaker, seeded.client_id)
    assert card.address == f"{ФОРМАТ}, кв 3"
    assert card.address_candidate_id == seeded.candidate_id
    assert card.address_conversation_id == seeded.conversation_id
    assert card.address_set_by_id is None and card.address_set_at is None
    row = await строка(db_sessionmaker, seeded.candidate_id)
    assert row.status == CANDIDATE_ACCEPTED and row.resolved_by_id is None
    async with db_sessionmaker() as s:
        журнал = (
            (
                await s.execute(
                    sa.select(AuditLog).where(AuditLog.action == "client.address_captured")
                )
            )
            .scalars()
            .all()
        )
    assert len(журнал) == 1
    assert журнал[0].user_id is None
    assert журнал[0].details["source"] == "geocoder"
    from tests.unit.conftest import drain_events

    assert any(e.get("type") == "client:updated" for e in await drain_events(pubsub))
    # Карточка подписывает адрес как поставленный автоматикой и хранит цитату.
    async with db_sessionmaker() as s:
        личность = await clients_svc.identity_view(s, await s.get(Client, seeded.client_id))
    assert личность["address_source"] == "auto"
    assert личность["address_evidence"]["raw"] == "п заречный ул звенигородская д 1"
    assert личность["address_geo"]["status"] == g.GEO_EXACT
    assert личность["address_candidates"] == [] and личность["addresses"] == []


async def test_кнопка_подтвердить_даёт_тот_же_текст_что_автозапись(
    seeded, db_sessionmaker, redis, карта, tokens, client
):
    await проверить(seeded, db_sessionmaker, redis, карта)
    res = await client.post(
        f"/api/v1/clients/{seeded.client_id}/address-candidates/{seeded.candidate_id}/resolve",
        json={"decision": "replace"},
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert res.status_code == 200, res.text
    assert res.json()["address"] == f"{ФОРМАТ}, кв 3"
    async with db_sessionmaker() as s:
        личность = await clients_svc.identity_view(s, await s.get(Client, seeded.client_id))
    assert личность["address_source"] == "dialog"


async def test_оператор_успел_раньше_автозапись_не_трогает_его_текст(
    seeded, db_sessionmaker, redis, карта
):
    await проверить(seeded, db_sessionmaker, redis, карта)
    async with db_sessionmaker() as s:
        card = await s.get(Client, seeded.client_id)
        card.address = "Свой адрес, 7"
        card.address_set_at = datetime.now(UTC)
        await s.commit()
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id) == "skip"
    )
    assert (await карточка(db_sessionmaker, seeded.client_id)).address == "Свой адрес, 7"
    assert (await строка(db_sessionmaker, seeded.candidate_id)).status == CANDIDATE_PENDING


async def test_две_подтверждённые_строки_в_диалоге_пишется_последняя_названная(
    seeded, db_sessionmaker, redis, карта
):
    """С 18.09 два разных подтверждённых дома в одном диалоге не тупик:
    без вопроса об адресе автозапись берёт последний названный, первая
    строка остаётся `pending` и видна на экране «Также назван»."""
    await проверить(seeded, db_sessionmaker, redis, карта)
    async with db_sessionmaker() as s:
        card = await s.get(Client, seeded.client_id)
        found = address_parse.parse("ул Мира 7")
        записано2 = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=seeded.conversation_id,
            message_id=None,
            message_at=datetime.now(UTC) + timedelta(minutes=5),
            found=found,
            now=datetime.now(UTC),
        )
        row2 = await s.get(ClientAddressCandidate, записано2.candidate_id)
        row2.geo_status = g.GEO_EXACT
        row2.geo_formatted = "улица Мира, 7, Орск"
        row2.geo_lat, row2.geo_lon = 51.23, 58.47  # инвариант exact ⇒ lat/lon
        await s.commit()
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id)
        == "filled"
    )
    card = await карточка(db_sessionmaker, seeded.client_id)
    assert card.address_candidate_id == записано2.candidate_id
    assert card.address == "улица Мира, 7, Орск"
    assert (await строка(db_sessionmaker, seeded.candidate_id)).status == CANDIDATE_PENDING
    async with db_sessionmaker() as s:
        личность = await clients_svc.identity_view(s, await s.get(Client, seeded.client_id))
    assert [c["id"] for c in личность["address_candidates"]] == [str(seeded.candidate_id)]


async def test_стёртый_оператором_адрес_не_возвращается(
    seeded, db_sessionmaker, redis, карта, tokens, client
):
    """Петля: автозапись → оператор стёр → «кв 3» → карта снова exact → автозапись."""
    await проверить(seeded, db_sessionmaker, redis, карта)
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id)
        == "filled"
    )
    res = await client.put(
        f"/api/v1/clients/{seeded.client_id}/address",
        json={"address": "", "conversation_id": str(seeded.conversation_id)},
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert res.status_code == 200, res.text
    row = await строка(db_sessionmaker, seeded.candidate_id)
    assert row.status == CANDIDATE_REJECTED
    assert (await карточка(db_sessionmaker, seeded.client_id)).address_candidate_id is None
    # Вторая попытка автоматики — та же строка, теперь отклонённая.
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id) == "skip"
    )
    assert (await карточка(db_sessionmaker, seeded.client_id)).address is None


async def test_вчерашняя_строка_в_пустую_карточку_пишется_а_поверх_места_нет(
    seeded, db_sessionmaker, redis, карта
):
    """С 13.09 в ПУСТУЮ карточку автозапись кладёт подтверждённый адрес любой
    давности (926 заявок с exact и пустой карточкой); поверх записанного
    автоматикой места — только свежую строку."""
    await проверить(seeded, db_sessionmaker, redis, карта)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.detected_at = datetime.now(UTC) - timedelta(hours=25)
        await s.commit()
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id)
        == "filled"
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.status = "pending"
        card = await s.get(Client, seeded.client_id)
        место = ClientAddressCandidate(
            client_id=card.id,
            conversation_id=seeded.conversation_id,
            message_id=row.message_id,
            message_at=row.message_at,
            value="деревня Ивановка",
            street="",
            house="",
            raw="д. Ивановка",
            level="A",
            kind="place",
            settlement="Ивановка",
            settlement_type="деревня",
            status="accepted",
            detected_at=datetime.now(UTC) - timedelta(hours=26),
            geo_status="exact",
        )
        s.add(место)
        await s.flush()
        card.address = "деревня Ивановка"
        card.address_candidate_id = место.id
        card.address_set_by_id = None
        await s.commit()
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id) == "skip"
    )


async def test_уровень_C_похожий_на_дату_автоматикой_не_пишется(
    seeded, db_sessionmaker, redis, карта
):
    """С 13.09 уровень C пишется, когда карта подтвердила дом; исключение —
    «Октября 7»: одно слово-месяц улицей не считаем."""
    await проверить(seeded, db_sessionmaker, redis, карта)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.level = "C"
        row.street = "Октября"
        await s.commit()
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id) == "skip"
    )


async def test_присоединённая_карточка_автоматикой_не_пишется(
    seeded, db_sessionmaker, redis, карта
):
    await проверить(seeded, db_sessionmaker, redis, карта)
    async with db_sessionmaker() as s:
        другой = Client(channel="avito", external_id="merge-target", name="Победитель")
        s.add(другой)
        await s.flush()
        card = await s.get(Client, seeded.client_id)
        card.merged_into_id = другой.id
        await s.commit()
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id) == "skip"
    )


async def test_выключатель_автозаписи(seeded, db_sessionmaker, redis, карта):
    await проверить(seeded, db_sessionmaker, redis, карта)
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_AUTOFILL: False}, user_id=None)
        await s.commit()
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id)
        == "disabled"
    )
    assert (await карточка(db_sessionmaker, seeded.client_id)).address is None


async def test_карта_не_подтвердила_автозапись_молчит(seeded, db_sessionmaker, redis, monkeypatch):
    async def чужой_дом(query, **kw):  # noqa: ANN001
        return [
            g.GeoHit(
                street="улица 40 лет Октября",
                house="1",
                settlement="Заречный",
                city="Орск",
                region="Оренбургская область",
                lat=51.0,
                lon=58.0,
                house_level=True,
            )
        ]

    monkeypatch.setattr(worker.nominatim, "search", чужой_дом)
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_status == g.GEO_STREET_MISMATCH
    assert not await redis.exists(f"arq:job:addr-fill:{seeded.conversation_id}")
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id) == "skip"
    )


# --- постановка ---------------------------------------------------------------


async def test_постановка_дедуплицируется_по_строке(redis):
    cid = uuid.uuid4()
    assert await enqueue_geocode(redis, cid) is True
    assert await enqueue_geocode(redis, cid) is False
    assert await redis.zcard("arq:queue") == 1


async def test_отказ_помнится_и_по_строке_карты_при_другом_написании(
    seeded, db_sessionmaker, redis, карта, tokens, client
):
    """Стёрли «ул звенигородская 1» → клиент пишет «Звенигородская 1» — это
    другой ключ, новая строка, тот же дом на карте. Автоматика обязана
    вспомнить отказ по строке карты, а не только по ключу."""
    await проверить(seeded, db_sessionmaker, redis, карта)
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id)
        == "filled"
    )
    res = await client.put(
        f"/api/v1/clients/{seeded.client_id}/address",
        json={"address": "", "conversation_id": str(seeded.conversation_id)},
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert res.status_code == 200, res.text

    async with db_sessionmaker() as s:
        card = await s.get(Client, seeded.client_id)
        found = address_parse.parse("Звенигородская 1, кв 3")
        assert found is not None and found.value != "ул звенигородская, 1"
        записано2 = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=seeded.conversation_id,
            message_id=None,
            message_at=None,
            found=found,
            now=datetime.now(UTC),
        )
        assert записано2.впервые
        await s.commit()
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), записано2.candidate_id)
    row2 = await строка(db_sessionmaker, записано2.candidate_id)
    # Без «п» в тексте строка карты другая («Заречный» без типа) — но дом тот же.
    первая = await строка(db_sessionmaker, seeded.candidate_id)
    assert row2.geo_status == g.GEO_EXACT and row2.geo_formatted != первая.geo_formatted
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id) == "skip"
    )
    assert (await карточка(db_sessionmaker, seeded.client_id)).address is None


# --- находки ревью 11.09: гонки, замок, запись после commit'а ------------------


async def test_посёлок_дописан_пока_задача_в_пути_вердикт_без_него_не_ложится(
    seeded, db_sessionmaker, redis, monkeypatch
):
    """Строка изменилась под задачей — её вердикт устарел (ревью 11.09, критично).

    Первая редакция писала `WHERE geo_status IN (pending, error)` и клала exact
    без посёлка поверх строки, в которую посёлок дописали за секунду до; повтор
    проверки ARQ отбрасывал (имя задачи занято до конца выполнения), и
    автозапись через 90 с ставила первую из четырёх улиц Ленина.
    """
    seeded_row = await строка(db_sessionmaker, seeded.candidate_id)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.settlement = None
        row.settlement_type = None
        await s.commit()

    async def карта_с_гонкой(query, **kw):  # noqa: ANN001
        # Пока «ходим к карте», клиент уточняет посёлок — в ту же строку.
        async with db_sessionmaker() as s:
            card = await s.get(Client, seeded.client_id)
            found = address_parse.parse("п заречный ул звенигородская д 1")
            записано = await clients_svc.record_address_candidate(
                s,
                client=card,
                conversation_id=seeded.conversation_id,
                message_id=None,
                message_at=None,
                found=found,
                now=datetime.now(UTC),
            )
            assert not записано.впервые and записано.перепроверить
            await s.commit()
        return [ДОМ]

    monkeypatch.setattr(worker.nominatim, "search", карта_с_гонкой)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id) == "stale"
    )
    row = await строка(db_sessionmaker, seeded.candidate_id)
    assert row.geo_status == g.GEO_PENDING and row.settlement == "заречный"
    assert row.geo_formatted is None
    # Повтор поставлен под вторым именем: своё занято до конца выполнения.
    assert await redis.exists(f"arq:job:geocode:{seeded.candidate_id}:again")
    assert not await redis.exists(f"arq:job:addr-fill:{seeded.conversation_id}")
    assert seeded_row.value == row.value


def _фабрика_с_оператором(real_factory, client_id, fired):
    """Сессия задачи, в которой перед её `UPDATE clients` успевает оператор."""

    def factory():
        s = real_factory()
        real_exec = s.execute

        async def execute(stmt, *a, **kw):
            if isinstance(stmt, sa.sql.Update) and stmt.table.name == "clients":
                fired["n"] += 1
                # Оператор — в СВОЕЙ сессии и со своим commit'ом: его запись
                # не откатится вместе с транзакцией задачи.
                async with real_factory() as своя:
                    await своя.execute(
                        sa.text("UPDATE clients SET address=:a, address_set_at=:t WHERE id=:id"),
                        {
                            "a": "Свой адрес, 7",
                            "t": datetime.now(UTC).isoformat(),
                            "id": client_id.hex,
                        },
                    )
                    await своя.commit()
            return await real_exec(stmt, *a, **kw)

        s.execute = execute
        return s

    return factory


async def test_гонка_с_оператором_между_чтением_и_записью(seeded, db_sessionmaker, redis, карта):
    """⚠ ДИВЕРСИЯ: снять `Client.address.is_(None)` у условной записи — краснеет.

    Проверка «оператор успел раньше» выше упирается в ранний выход и условный
    UPDATE не трогает (ревью 11.09). Здесь оператор пишет МЕЖДУ чтением и
    записью — и гонку решает база, а не порядок чтения.
    """
    await проверить(seeded, db_sessionmaker, redis, карта)
    fired = {"n": 0}
    factory = _фабрика_с_оператором(db_sessionmaker, seeded.client_id, fired)
    итог = await worker.autofill_address(
        {"db_session_factory": factory, "redis": redis, "job_try": 1}, seeded.conversation_id
    )
    assert fired["n"] == 1, "оператор не вклинился — проверка мерила бы не то"
    assert итог == "raced"
    assert (await карточка(db_sessionmaker, seeded.client_id)).address == "Свой адрес, 7"
    async with db_sessionmaker() as s:
        n = (
            await s.execute(
                sa.select(sa.func.count())
                .select_from(AuditLog)
                .where(AuditLog.action == "client.address_captured")
            )
        ).scalar_one()
    assert n == 0
    assert (await строка(db_sessionmaker, seeded.candidate_id)).status == CANDIDATE_PENDING


async def test_поход_к_карте_идёт_через_замок_темпа(seeded, db_sessionmaker, redis, карта):
    """⚠ ДИВЕРСИЯ: убрать `wait=темп` из `_search` — краснеет (ревью 11.09).

    Живой остаток: до задачи ключа нет, после — есть. Замок стережётся на
    пути задачи, а не вызовом функции напрямую.
    """
    assert not await redis.exists(worker.RATE_LOCK_KEY)
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
    assert await redis.exists(worker.RATE_LOCK_KEY), "к карте сходили без замка темпа"


async def test_отказ_помнится_и_в_следующем_диалоге_того_же_клиента(
    seeded, db_sessionmaker, redis, карта, tokens, client
):
    """Стёрли вчера в одном диалоге — сегодня из другого диалога дом не возвращается."""
    await проверить(seeded, db_sessionmaker, redis, карта)
    assert (
        await worker.autofill_address(ctx(db_sessionmaker, redis), seeded.conversation_id)
        == "filled"
    )
    res = await client.put(
        f"/api/v1/clients/{seeded.client_id}/address",
        json={"address": "", "conversation_id": str(seeded.conversation_id)},
        headers={"Authorization": f"Bearer {tokens['manager']}"},
    )
    assert res.status_code == 200, res.text

    async with db_sessionmaker() as s:
        conv2 = Conversation(
            channel="avito",
            external_chat_id=f"chat-{uuid.uuid4().hex[:8]}",
            account_id=seeded.account.id,
            client_id=seeded.client_id,
            status="new",
            unread_count=0,
            last_message_at=datetime.now(UTC),
            item_city_slug="orsk",
        )
        s.add(conv2)
        await s.flush()
        card = await s.get(Client, seeded.client_id)
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv2.id,
            message_id=None,
            message_at=None,
            found=address_parse.parse("ул. звенигородская, д. 1"),
            now=datetime.now(UTC),
        )
        assert записано.впервые
        conv2_id = conv2.id
        await s.commit()
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), записано.candidate_id)
    assert (await строка(db_sessionmaker, записано.candidate_id)).geo_status == g.GEO_EXACT
    assert await worker.autofill_address(ctx(db_sessionmaker, redis), conv2_id) == "skip"
    assert (await карточка(db_sessionmaker, seeded.client_id)).address is None


# --- цепочка «OSM → Яндекс» с дневным потолком (просьба владельца 11.09) ------


@pytest.fixture
def яндекс(monkeypatch):
    """Ключ есть (на шлюзе, docs/46), Яндекс отвечает записанным домом; вызовы считаются."""
    from app.integrations import gateway

    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    вызовы: list[g.Query] = []

    async def search(query, **kw):  # noqa: ANN001
        вызовы.append(query)
        # Счётчик потолка зовёт сама интеграция при настоящем походе (кэш 13.09).
        if kw.get("on_request"):
            await kw["on_request"]()
        return [dataclasses.replace(ДОМ, settlement="посёлок Заречный")]

    monkeypatch.setattr(worker.yandex_geocoder, "search", search)
    return вызовы


async def _режим(db_sessionmaker, режим: str, потолок: int | None = 900) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.ADDRESS_GEO_PROVIDER: режим,
                app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT: потолок,
            },
            user_id=None,
        )
        await s.commit()


async def test_osm_нашёл_яндекс_не_спрашивается(seeded, db_sessionmaker, redis, карта, яндекс):
    await _режим(db_sessionmaker, "osm_then_yandex")
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_EXACT
    )
    assert яндекс == []
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_provider == "nominatim"
    assert await worker.yandex_calls_today(redis) == 0


async def test_osm_не_нашёл_яндекс_добирает_и_считается(
    seeded, db_sessionmaker, redis, monkeypatch, яндекс
):
    async def пусто(query, wait=None, **kw):  # noqa: ANN001
        if wait:
            await wait()
        return []

    monkeypatch.setattr(worker.nominatim, "search", пусто)
    await _режим(db_sessionmaker, "osm_then_yandex")
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_EXACT
    )
    assert len(яндекс) == 1
    row = await строка(db_sessionmaker, seeded.candidate_id)
    assert row.geo_provider == "yandex"
    # Тип пункта Яндекс отдаёт сам — не дублируется словом клиента.
    assert row.geo_formatted == "Звенигородская улица, 1, посёлок Заречный, Орск"
    assert await worker.yandex_calls_today(redis) == 1


async def test_потолок_яндекса_держит_бесплатную_тысячу(
    seeded, db_sessionmaker, redis, monkeypatch, яндекс
):
    """⚠ ДИВЕРСИЯ: снять проверку потолка в `_яндекс_доступен` — краснеет."""

    async def пусто(query, wait=None, **kw):  # noqa: ANN001
        if wait:
            await wait()
        return []

    monkeypatch.setattr(worker.nominatim, "search", пусто)
    await _режим(db_sessionmaker, "osm_then_yandex", потолок=5)
    await redis.set(worker.yandex_calls_key(), 5)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_NOT_FOUND
    )
    assert яндекс == []
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_provider == "nominatim"


async def test_режим_yandex_первым_а_за_потолком_osm(seeded, db_sessionmaker, redis, карта, яндекс):
    await _режим(db_sessionmaker, "yandex", потолок=1)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_EXACT
    )
    assert len(яндекс) == 1 and карта == []
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_provider == "yandex"
    # Потолок выбран — вторая строка идёт в OSM, а не встаёт.
    async with db_sessionmaker() as s:
        card = await s.get(Client, seeded.client_id)
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=seeded.conversation_id,
            message_id=None,
            message_at=None,
            found=address_parse.parse("ул Мира 7"),
            now=datetime.now(UTC),
        )
        await s.commit()
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), записано.candidate_id)
    assert len(яндекс) == 1 and len(карта) == 1


async def test_яндекс_упал_остаёмся_с_вердиктом_osm(seeded, db_sessionmaker, redis, monkeypatch):
    from app.integrations import gateway

    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)

    async def пусто(query, wait=None, **kw):  # noqa: ANN001
        if wait:
            await wait()
        return []

    async def падает(query, **kw):  # noqa: ANN001
        raise g.GeocodeError("yandex", "network", 503)

    monkeypatch.setattr(worker.nominatim, "search", пусто)
    monkeypatch.setattr(worker.yandex_geocoder, "search", падает)
    await _режим(db_sessionmaker, "osm_then_yandex")
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_NOT_FOUND
    )
    assert (await строка(db_sessionmaker, seeded.candidate_id)).geo_provider == "nominatim"


# --- Геосаджест: опечатка и сокращение (просьба владельца 11.09) --------------


@pytest.fixture
def саджест(monkeypatch):
    from app.integrations import gateway, yandex_suggest

    monkeypatch.setitem(gateway.known_keys, "yandex_suggest", True)
    вызовы: list[g.Query] = []

    async def suggest(query, **kw):  # noqa: ANN001
        вызовы.append(query)
        if kw.get("on_request"):
            await kw["on_request"]()
        return [
            yandex_suggest.Suggested(
                street="Звенигородская улица",
                house="1",
                city="Орск",
                settlement="посёлок Заречный",
                region="Оренбургская область",
                formatted=None,
            )
        ]

    monkeypatch.setattr(worker.yandex_suggest, "suggest", suggest)
    return вызовы


@pytest.fixture
async def с_опечаткой(seed_conversation, db_sessionmaker):
    """Клиент написал «Звенигародская» — карта такой улицы не знает."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        found = address_parse.parse("ул Звенигародская 1")
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=None,
            message_at=None,
            found=found,
            now=datetime.now(UTC),
        )
        await s.commit()
        seed_conversation.candidate_id = записано.candidate_id
    return seed_conversation


def _карта_только_по_верной_улице(monkeypatch, вызовы: list[g.Query]):
    """OSM отвечает домом только на правильно написанную улицу."""

    async def search(query, wait=None, **kw):  # noqa: ANN001
        вызовы.append(query)
        if wait:
            await wait()
        return [ДОМ] if "звенигородская" in query.street.lower() else []

    monkeypatch.setattr(worker.nominatim, "search", search)


async def test_опечатка_чинится_подсказкой_и_карта_подтверждает(
    с_опечаткой, db_sessionmaker, redis, monkeypatch, саджест
):
    вызовы: list[g.Query] = []
    _карта_только_по_верной_улице(monkeypatch, вызовы)
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), с_опечаткой.candidate_id)
    assert итог == g.GEO_EXACT
    row = await строка(db_sessionmaker, с_опечаткой.candidate_id)
    assert row.geo_provider == "suggest+nominatim"
    assert row.geo_formatted == "Звенигородская улица, 1, Заречный, Орск"
    # Саджест спросили один раз, к карте сходили дважды: с опечаткой и с поправкой.
    assert len(саджест) == 1 and len(вызовы) == 2
    assert вызовы[1].street == "Звенигородская улица"
    assert await worker.suggest_calls_today(redis) == 1


async def test_подсказка_не_спрашивается_когда_карта_и_так_нашла(
    seeded, db_sessionmaker, redis, карта, саджест
):
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id)
        == g.GEO_EXACT
    )
    assert саджест == []


async def test_подсказка_за_потолком_не_спрашивается(
    с_опечаткой, db_sessionmaker, redis, monkeypatch, саджест
):
    _карта_только_по_верной_улице(monkeypatch, [])
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_SUGGEST_DAILY_LIMIT: 3}, user_id=None
        )
        await s.commit()
    await redis.set(worker.suggest_calls_key(), 3)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), с_опечаткой.candidate_id)
        == g.GEO_NOT_FOUND
    )
    assert саджест == []


async def test_выключатель_подсказки(с_опечаткой, db_sessionmaker, redis, monkeypatch, саджест):
    _карта_только_по_верной_улице(monkeypatch, [])
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_SUGGEST_ENABLED: False}, user_id=None
        )
        await s.commit()
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), с_опечаткой.candidate_id)
        == g.GEO_NOT_FOUND
    )
    assert саджест == []


async def test_подсказка_на_чужую_улицу_вердикт_не_пускает(
    с_опечаткой, db_sessionmaker, redis, monkeypatch
):
    """Саджест предложил «Советскую» на «Звенигародскую» — вердикт обязан отказать."""
    from app.integrations import gateway, yandex_suggest

    monkeypatch.setitem(gateway.known_keys, "yandex_suggest", True)

    async def suggest(query, **kw):  # noqa: ANN001
        if kw.get("on_request"):
            await kw["on_request"]()
        return [
            yandex_suggest.Suggested(
                street="Советская улица",
                house="1",
                city="Орск",
                settlement=None,
                region="Оренбургская область",
                formatted=None,
            )
        ]

    async def search(query, wait=None, **kw):  # noqa: ANN001
        if wait:
            await wait()
        return (
            [dataclasses.replace(ДОМ, street="Советская улица", settlement=None)]
            if "совет" in query.street.lower()
            else []
        )

    monkeypatch.setattr(worker.yandex_suggest, "suggest", suggest)
    monkeypatch.setattr(worker.nominatim, "search", search)
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), с_опечаткой.candidate_id)
        == g.GEO_NOT_FOUND
    )
    assert (await строка(db_sessionmaker, с_опечаткой.candidate_id)).geo_provider == "nominatim"


async def test_автозапись_берёт_уровень_C_когда_карта_подтвердила_дом(
    seed_conversation, db_sessionmaker, redis, карта
):
    """Владелец 13.09 (Елец): «Тополиная д 14» назавтра после вопроса —
    уровень C, карта подтвердила, а адрес ждал кнопки. Теперь пишется сам;
    «Октября 7» (дата) — нет."""
    from datetime import UTC, datetime

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "orsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        # Реплика строки — сам адрес (сторож автозаписи 19.09 перечитывает её).
        сообщение = (
            await s.execute(sa.select(Message).where(Message.id == seed_conversation.message_id))
        ).scalar_one()
        сообщение.body = "Звенигородская 1"
        found = address_parse.parse(сообщение.body)
        assert found is not None and found.level == "C"
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=seed_conversation.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        await s.commit()
        cid = записано.candidate_id
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    assert worker._похоже_на_дату("Октября") and not worker._похоже_на_дату("40 лет Октября")


async def test_город_клиента_вместо_отсутствующего_города_объявления(
    seeded, db_sessionmaker, redis, карта
):
    """«Геленджик ул. Цветочная 7» при объявлении без города (бой 13.09): карта
    спрашивается с городом клиента, а не молчит «город неизвестен»."""
    from app.integrations.avito.listing_url import city_by_name

    assert city_by_name("геленджик") is not None and city_by_name("Москва и МО") is None
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seeded.conversation_id)
        conv.item_city_slug = None
        conv.item_url = "https://avito.ru/item/1"
        row = await s.get(ClientAddressCandidate, seeded.candidate_id)
        row.locality = "Орск"
        await s.commit()
    assert (
        await worker.geocode_candidate(ctx(db_sessionmaker, redis), seeded.candidate_id) == "exact"
    )


async def test_потолок_яндекса_занимается_до_похода_и_по_московским_суткам(redis) -> None:
    """Бой 13.09: Саджест заблокирован на 1 084 из 1 000 при нашем счётчике 845.
    Сутки — московские; запрос занимает место в потолке ДО похода."""
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    ночь = datetime(2026, 9, 12, 22, 30, tzinfo=UTC)  # 01:30 МСК 13.09
    assert worker.yandex_calls_key(ночь).endswith("20260913")
    assert worker.suggest_calls_key(ночь).endswith("20260913")
    assert datetime(2026, 9, 12, 20, 0, tzinfo=UTC).astimezone(ZoneInfo("Europe/Moscow")).day == 12
    ключ = worker.yandex_calls_key()
    await worker._занять(redis, ключ, 2, "yandex")
    await worker._занять(redis, ключ, 2, "yandex")
    with pytest.raises(g.GeocodeError) as exc:
        await worker._занять(redis, ключ, 2, "yandex")
    assert exc.value.kind == "limit" and int(await redis.get(ключ)) == 2
