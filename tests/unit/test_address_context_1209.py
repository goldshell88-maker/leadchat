"""Адрес после вопроса оператора, варианты карты, дом в другом городе области (12.09).

Шесть скриншотов владельца за один час: «Уральская 24-31», «Рязанская 10»,
«Кольцова 15/3-11» — всё в ответ на «Подскажите, куда к вам подъехать», и всё
уровня C, то есть спрятано. Признак «оператор спросил» у разбора был с 09.09,
но его никто не передавал. Плюс «уточните посёлок» на четырёх строениях одного
дома и «7.кор4», терявший корпус.

ДИВЕРСИИ (каждая обязана краснеть): не передавать `про_адрес` → уровень C;
убрать группировку адресов → `ambiguous`; убрать поиск по области → нет
`elsewhere`; убрать «кор» из списка корпусов → дом «7».
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa

from app.integrations import gateway
from app.models import Client, ClientAddressCandidate, Conversation, Message
from app.services import address_parse, app_settings
from app.services import clients as clients_svc
from app.services import geocode as g
from app.services.inbound import apply_inbound_event
from app.workers import geocode as worker

try:
    from app.integrations.avito.adapter import InboundEvent
except ImportError:  # pragma: no cover
    from app.workers.inbound import FallbackInboundEvent as InboundEvent

pytestmark = pytest.mark.anyio

AVITO_USER_ID = 111222555
T0 = datetime(2026, 9, 12, 7, 0, 0, tzinfo=UTC)


def событие(текст: str, *, msg: str, when: datetime) -> InboundEvent:
    return InboundEvent(
        external_chat_id="chat-ctx",
        external_message_id=msg,
        author_id=999003,
        account_user_id=AVITO_USER_ID,
        text=текст,
        created_at=when,
        client_name="Артём",
        item_title="Услуги электрика",
        item_url="https://avito.ru/prokopevsk/item/3",
        item_price=None,
    )


@pytest.fixture
async def account(make_avito_account):
    return await make_avito_account(AVITO_USER_ID)


async def _исходящее(db_sessionmaker, текст: str, when: datetime) -> None:
    async with db_sessionmaker() as s:
        conv = (await s.execute(sa.select(Conversation))).scalars().one()
        s.add(
            Message(
                conversation_id=conv.id,
                external_message_id=f"out-{when.timestamp()}",
                direction="out",
                sender_type="user",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=when,
            )
        )
        await s.commit()


async def _строки(db_sessionmaker) -> list[ClientAddressCandidate]:
    async with db_sessionmaker() as s:
        return list((await s.execute(sa.select(ClientAddressCandidate))).scalars().all())


async def test_название_число_после_вопроса_оператора_это_уровень_B(
    db, redis, account, db_sessionmaker
):
    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="m1", when=T0))
    await _исходящее(
        db_sessionmaker,
        "Подскажите, куда к вам подъехать (квартира, подъезд, этаж) и ваш номер телефона",
        T0 + timedelta(minutes=1),
    )
    await apply_inbound_event(
        db,
        redis,
        account,
        событие("Уральская 24-31 89001112247", msg="m2", when=T0 + timedelta(minutes=2)),
    )
    (row,) = await _строки(db_sessionmaker)
    assert (row.level, row.value, row.office) == ("B", "Уральская, 24", "31")
    # И показывается оператору с умолчанием «AB».
    async with db_sessionmaker() as s:
        card = (await s.execute(sa.select(Client))).scalars().one()
        view = await clients_svc.identity_view(s, card)
    assert [c["value"] for c in view["address_candidates"]] == ["Уральская, 24"]


async def test_без_вопроса_оператора_остаётся_уровень_C_и_не_показывается(
    db, redis, account, db_sessionmaker
):
    """Отрицательная проверка: тот же текст, но оператор ничего не спрашивал."""
    await apply_inbound_event(db, redis, account, событие("Здравствуйте", msg="m1", when=T0))
    await _исходящее(db_sessionmaker, "Хорошо, когда вам удобно?", T0 + timedelta(minutes=1))
    await apply_inbound_event(
        db, redis, account, событие("Рязанская 10", msg="m2", when=T0 + timedelta(minutes=2))
    )
    (row,) = await _строки(db_sessionmaker)
    assert row.level == "C"
    async with db_sessionmaker() as s:
        card = (await s.execute(sa.select(Client))).scalars().one()
        view = await clients_svc.identity_view(s, card)
    assert view["address_candidates"] == []
    # …но если карта потом подтвердит дом — строка покажется и без уровня.
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(ClientAddressCandidate).values(geo_status=g.GEO_EXACT, geo_formatted="x")
        )
        await s.commit()
        card = (await s.execute(sa.select(Client))).scalars().one()
        view = await clients_svc.identity_view(s, card)
    assert [c["value"] for c in view["address_candidates"]] == ["Рязанская, 10"]


def test_корпус_через_точку_не_теряется() -> None:
    f = address_parse.parse("Залесный, ул Василия Жуковского 7.кор4,")
    assert f is not None and g.house_key(f.house) == "7к4"


ДОМ = g.GeoHit(
    street="Большая Санкт-Петербургская улица",
    house="27",
    settlement="Черепичный посёлок",
    city="Великий Новгород",
    region="Новгородская область",
    lat=58.5174,
    lon=31.2939,
    house_level=True,
    settlement_kind="district",
)


def test_четыре_строения_одного_дома_это_один_адрес() -> None:
    """⚠ ДИВЕРСИЯ: вернуть сравнение по точкам — `ambiguous`."""
    from app.integrations.avito.listing_url import City

    city = City(name="Великий Новгород", region="Новгородская область", tz="Europe/Moscow")
    parsed = g.Parsed(street="Большая Санкт-Петербургская улица", house="27")
    hits = [
        ДОМ,
        dataclasses.replace(ДОМ, lat=58.5188, lon=31.2943, city="городской округ Великий Новгород"),
        dataclasses.replace(ДОМ, lat=58.5148, lon=31.2925),
        dataclasses.replace(ДОМ, house="27, стр. 18", lat=58.5170, lon=31.2957),
    ]
    статус, hit = g.verdict(parsed, city, hits)
    assert статус == g.GEO_EXACT and hit is ДОМ


def test_разные_адреса_дают_варианты_а_не_вопрос_о_посёлке() -> None:
    from app.integrations.avito.listing_url import City

    city = City(name="Великий Новгород", region="Новгородская область", tz="Europe/Moscow")
    parsed = g.Parsed(street="Ленина", house="5")
    hits = [
        dataclasses.replace(ДОМ, street="улица Ленина", house="5", settlement="Кречевицы"),
        dataclasses.replace(ДОМ, street="улица Ленина", house="5", settlement="Панковка", lat=58.6),
    ]
    assert g.verdict(parsed, city, hits)[0] == g.GEO_AMBIGUOUS
    варианты = g.variants(parsed, city, hits)
    assert len(варианты) == 2 and варианты[0]["formatted"] != варианты[1]["formatted"]


@pytest.fixture
async def хабаровск(seed_conversation, db_sessionmaker):
    """Объявление в Хабаровске, клиент написал «Кольцова 15/3»."""
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "habarovsk"
        card = await s.get(Client, seed_conversation.client_id)
        card.address = None
        found = address_parse.parse("ул Кольцова 15/3")
        assert found is not None
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
        seed_conversation.candidate_id = записано.candidate_id
    return seed_conversation


async def test_дом_в_другом_городе_области_показывается_вариантом(
    хабаровск, db_sessionmaker, redis, monkeypatch
):
    """OSM и Яндекс в Хабаровске дома не нашли; по области Яндекс находит
    Комсомольск-на-Амуре — строка получает `elsewhere` и вариант с городом.
    ⚠ ДИВЕРСИЯ: убрать поиск по области — `house_missing`, вариантов нет."""

    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        return []

    запросы: list[g.Query] = []

    async def yandex(query, **kw):  # noqa: ANN001
        запросы.append(query)
        if query.city is not None:
            return []
        return [
            g.GeoHit(
                street="улица Кольцова",
                house="15/3",
                settlement=None,
                city="Комсомольск-на-Амуре",
                region="Хабаровский край",
                lat=50.55,
                lon=137.0,
                house_level=True,
            )
        ]

    monkeypatch.setattr(worker.nominatim, "search", osm)
    monkeypatch.setattr(worker.yandex_geocoder, "search", yandex)
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.ADDRESS_GEO_PROVIDER: "osm_then_yandex",
                app_settings.ADDRESS_GEO_SUGGEST_ENABLED: False,
            },
            user_id=None,
        )
        await s.commit()
    итог = await worker.geocode_candidate(
        {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1},
        хабаровск.candidate_id,
    )
    assert итог == g.GEO_ELSEWHERE
    assert [q.city for q in запросы] == ["Хабаровск", None]
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, хабаровск.candidate_id)
        assert row.geo_status == g.GEO_ELSEWHERE
        assert [v["city"] for v in row.geo_variants] == ["Комсомольск-на-Амуре"]
        assert row.geo_variants[0]["formatted"] == "улица Кольцова, 15/3, Комсомольск-на-Амуре"
        # Сам в карточку не ложится: автозапись только у `exact`.
        card = await s.get(Client, хабаровск.client_id)
        assert card.address is None


async def test_оператор_выбирает_вариант_и_он_становится_адресом(
    хабаровск, db_sessionmaker, users_by_role
):
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, хабаровск.candidate_id)
        row.geo_status = g.GEO_ELSEWHERE
        row.geo_variants = [
            {
                "formatted": "улица Кольцова, 15/3, Комсомольск-на-Амуре",
                "lat": 50.55,
                "lon": 137.0,
                "city": "Комсомольск-на-Амуре",
            }
        ]
        await s.commit()
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, хабаровск.candidate_id)
        card = await s.get(Client, хабаровск.client_id)
        итог = await clients_svc.resolve_address_candidate(
            s,
            candidate=row,
            client=card,
            decision="replace",
            actor=users_by_role["manager"],
            variant=0,
        )
        await s.commit()
    assert итог["address"] == "улица Кольцова, 15/3, Комсомольск-на-Амуре"
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, хабаровск.candidate_id)
        assert (row.geo_status, row.geo_lat) == (g.GEO_EXACT, 50.55)


def test_порядковое_числительное_в_названии_улицы_не_мешает() -> None:
    """«177 квартал 13» ↔ «177-й квартал» (Ангарск, 12.09); «3 линия» ↔ «3-я линия».
    ⚠ ДИВЕРСИЯ: убрать `_ПОРЯДКОВОЕ` из `_слова` — краснеет."""
    assert g._улица_не_шире("177-й квартал", "177 квартал")
    assert g._улица_не_шире("3-я линия", "3 линия")
    assert not g._улица_не_шире("178-й квартал", "177 квартал")


async def test_город_появился_позже_адреса_и_строка_идёт_на_карту(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """Бой 12.09 (Хабаровск): строка проверялась в ту же секунду, что распознана,
    объявление приезжало обогащением мгновением позже — «город неизвестен»
    оставалось навсегда. ⚠ ДИВЕРСИЯ: убрать постановку из `enrich_client` и
    `no_city` из починки — краснеет."""
    from app.integrations.avito.adapter import ChatInfo
    from app.scheduler.jobs import geo_repair
    from app.services import client_enrich

    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_title = None
        conv.item_url = None
        conv.item_city_slug = None
        card = await s.get(Client, seed_conversation.client_id)
        found = address_parse.parse("ул Энтузиастов 67")
        assert found is not None
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
    # Карта спрошена до объявления — вердикт «город неизвестен».
    итог = await worker.geocode_candidate(
        {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}, cid
    )
    assert итог == g.GEO_NO_CITY

    # Обогащение принесло объявление с городом → строка снова в очереди карты.
    async def _fake_fetch(*_a, **_kw):  # noqa: ANN001
        return ChatInfo(
            external_chat_id="x",
            client_external_id="999001",
            client_name="Иван Петров",
            item_title="Сантехнические работы",
            item_url="https://www.avito.ru/habarovsk/predlozheniya_uslug/santehnika_1234567890",
            item_price=None,
            unread_count=0,
            has_unread=False,
            last_message_at=None,
        )

    monkeypatch.setattr(client_enrich, "_fetch_chat_info", _fake_fetch)
    await client_enrich.enrich_client(
        {"db_session_factory": db_sessionmaker, "redis": redis}, seed_conversation.conversation_id
    )
    assert await redis.exists(f"arq:job:geocode:{cid}:city")

    # ⚠ И ЗАДАЧА ОБЯЗАНА ПЕРЕПИСАТЬ «город неизвестен», а не выйти по «уже
    # решено» (бой 12.09, Чита: постановка была, задача выходила за 0.00 с).
    async def карта(query, wait=None, **kw):  # noqa: ANN001
        return [
            g.GeoHit(
                street="улица Энтузиастов",
                house="67",
                settlement=None,
                city="Хабаровск",
                region="Хабаровский край",
                lat=48.5,
                lon=135.1,
                house_level=True,
            )
        ]

    monkeypatch.setattr(worker.nominatim, "search", карта)
    assert (
        await worker.geocode_candidate(
            {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}, cid
        )
        == g.GEO_EXACT
    )
    async with db_sessionmaker() as s:
        assert (await s.get(ClientAddressCandidate, cid)).geo_status == g.GEO_EXACT
        await s.execute(
            sa.update(ClientAddressCandidate)
            .where(ClientAddressCandidate.id == cid)
            .values(geo_status=g.GEO_NO_CITY)
        )
        await s.commit()

    # И починка планировщика тоже не проходит мимо «город неизвестен».
    await redis.delete(f"arq:job:geocode:{cid}:city")
    monkeypatch.setattr(geo_repair.redis_mod, "get_client", lambda: redis)
    monkeypatch.setattr(geo_repair.db_mod, "session_scope", db_sessionmaker)
    async with db_sessionmaker() as s:
        await s.execute(
            sa.update(ClientAddressCandidate)
            .where(ClientAddressCandidate.id == cid)
            .values(geo_checked_at=datetime.now(UTC) - timedelta(hours=1))
        )
        await s.commit()
    assert await geo_repair.repair_geocodes() == 1
