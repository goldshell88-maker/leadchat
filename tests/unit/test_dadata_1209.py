"""DaData «Подсказки» как первая карта (12.09): разбор ответа, запрос, место в цепочке.

Образец ответа — по форме живого, снятого 12.09 по Хабаровскому краю (поля
обрезаны до используемых; адрес «Садовая 15/3» и точки вымышленные).
Сравнение на 300 строках боя: DaData одна даёт 185 exact против 171 у цепочки
OSM→Яндекс, но теряет 10 там, где OSM находит, — поэтому DaData ПЕРВОЙ, а
цепочка — за ней.

ДИВЕРСИИ: убрать DaData из начала цепочки → OSM спрашивается при exact у
DaData; считать `qc_geo=1` догадкой → Казань «3 к 5» снова house_mismatch.

С 16.09 (docs/46) ключ, тело запроса и разбор ответа живут в шлюзе: тесты
заголовка/тела/статусов уехали в `gateway/tests/test_gw_dadata.py`, разбор
берётся из `leadchat_gateway.providers.dadata`, обёртка — в
`test_dadata_шлюз.py`.
"""

from __future__ import annotations

import pytest
from leadchat_gateway.providers import dadata as gw_dadata

from app.integrations import dadata, gateway
from app.integrations.avito.listing_url import City
from app.services import app_settings
from app.services import geocode as g

pytestmark = pytest.mark.anyio

ОТВЕТ = {
    "suggestions": [
        {
            "value": "Хабаровский край, г Комсомольск-на-Амуре, ул Садовая, д 15 к 3",
            "data": {
                "region_with_type": "Хабаровский край",
                "city": "Комсомольск-на-Амуре",
                "settlement": None,
                "settlement_with_type": None,
                "street_with_type": "ул Садовая",
                "house": "15",
                "block_type": "к",
                "block": "3",
                "geo_lat": "50.55",
                "geo_lon": "137.01",
                "qc_geo": "0",
                "fias_level": "8",
            },
        },
        {
            "value": "…, кв 1",
            "data": {
                "region_with_type": "Хабаровский край",
                "city": "Комсомольск-на-Амуре",
                "street_with_type": "ул Садовая",
                "house": "15",
                "block_type": "к",
                "block": "3",
                "geo_lat": "50.55",
                "geo_lon": "137.01",
                "qc_geo": "0",
                "fias_level": "9",
            },
        },
        # Микрорайон без улицы — пункт и есть «улица».
        {
            "value": "Иркутская обл, г Ангарск, мкр 12, д 7",
            "data": {
                "region_with_type": "Иркутская обл",
                "city": "Ангарск",
                "settlement": "12",
                "settlement_with_type": "мкр 12",
                "street_with_type": None,
                "house": "7",
                "geo_lat": "52.51",
                "geo_lon": "103.87",
                "qc_geo": "1",
                "fias_level": "8",
            },
        },
        # Корпус, цифра которого входит в номер дома: «д 12 к 2» ≠ «д 12».
        {
            "value": "Оренбургская обл, г Орск, ул Ленина, д 12 к 2",
            "data": {
                "region_with_type": "Оренбургская обл",
                "city": "Орск",
                "street_with_type": "ул Ленина",
                "house": "12",
                "block_type": "к",
                "block": "2",
                "geo_lat": "51.2",
                "geo_lon": "58.5",
                "qc_geo": "0",
                "fias_level": "8",
            },
        },
        # Дом деревни: в справочнике есть (fias_level 8), точка — центр пункта.
        {
            "value": "Вологодская обл, Череповецкий р-н, д Новое Заречье, ул Липовая, д 6",
            "data": {
                "region_with_type": "Вологодская обл",
                "city": None,
                "settlement": "Новое Заречье",
                "settlement_with_type": "д Новое Заречье",
                "street_with_type": "ул Липовая",
                "house": "6",
                "geo_lat": "59.3",
                "geo_lon": "37.6",
                "qc_geo": "3",
                "fias_level": "8",
            },
        },
        # Дома в справочнике нет — не дом, какими бы ни были координаты.
        {
            "value": "д Новое Заречье, ул Липовая, д 66",
            "data": {
                "region_with_type": "Вологодская обл",
                "settlement": "Новое Заречье",
                "settlement_with_type": "д Новое Заречье",
                "street_with_type": "ул Липовая",
                "house": "66",
                "geo_lat": "59.3",
                "geo_lon": "37.6",
                "qc_geo": "0",
                "fias_level": "-1",
            },
        },
        # Улица без дома — не дом.
        {
            "value": "ул Садовая",
            "data": {"street_with_type": "ул Садовая", "house": None, "fias_level": "7"},
        },
    ]
}


def test_разбор_ответа_дом_корпус_микрорайон() -> None:
    hits = [g.GeoHit(**h.model_dump()) for h in gw_dadata.parse_response(ОТВЕТ)]
    assert [(h.street, h.house, h.city, h.house_level, h.interpolated) for h in hits] == [
        ("ул Садовая", "15 к 3", "Комсомольск-на-Амуре", True, False),
        ("ул Садовая", "15 к 3", "Комсомольск-на-Амуре", True, False),
        ("мкр 12", "7", "Ангарск", True, False),
        ("ул Ленина", "12 к 2", "Орск", True, False),
        ("ул Липовая", "6", "Новое Заречье", True, False),
        ("ул Липовая", "66", "Новое Заречье", False, False),
    ]
    # Пункт стал «улицей», но остался и пунктом (по нему сверяется названное);
    # в строке адреса он не печатается дважды.
    assert hits[2].settlement == "12"
    assert g.format_address(hits[2], g.Parsed(street="12 мкр", house="7")) == "мкр 12, 7, Ангарск"
    assert g.house_key(hits[0].house) == g.house_key("15/3") == "15к3"
    assert g.house_key(hits[3].house) == g.house_key("12к2") == "12к2"
    # Клиент написал «12», в справочнике только корпуса — это НЕ тот дом.
    орск = City("Орск", "Оренбургская область", "Asia/Yekaterinburg")
    статус, _ = g.verdict(g.Parsed(street="Ленина", house="12", level="A"), орск, hits[3:])
    assert статус == g.GEO_HOUSE_MISMATCH


def test_запрос_город_или_область_и_корпус_словом(monkeypatch) -> None:  # noqa: ANN001
    q = g.Query(
        region="Хабаровский край",
        city="Хабаровск",
        settlement=None,
        street="Садовая",
        house="15/3",
    )
    assert gw_dadata.locations_for(region=q.region, city=q.city) == [{"city": "Хабаровск"}]
    assert gw_dadata.locations_for(region="Хабаровский край", city=None) == [
        {"region": "Хабаровский"}
    ]
    assert gw_dadata.locations_for(region="Республика Татарстан", city=None) == [
        {"region": "Татарстан"}
    ]
    assert gw_dadata.locations_for(region="Москва и область", city=None) == [
        {"region_iso_code": "RU-MOW"},
        {"region_iso_code": "RU-MOS"},
    ]
    assert gw_dadata.locations_for(region="ХМАО — Югра", city=None) == [
        {"region_iso_code": "RU-KHM"}
    ]
    assert dadata.house_for_query("3.кор5") == "3 к 5"
    assert dadata.house_for_query("15/3") == "15/3"


# --- место в цепочке ------------------------------------------------------------


ХАБАРОВСК = City("Хабаровск", "Хабаровский край", "Asia/Vladivostok")


async def test_dadata_первой_и_osm_не_спрашивается_при_exact(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    from datetime import UTC, datetime

    from app.models import Client, ClientAddressCandidate, Conversation
    from app.services import address_parse
    from app.services import clients as clients_svc
    from app.workers import geocode as worker

    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = "habarovsk"
        card = await s.get(Client, seed_conversation.client_id)
        found = address_parse.parse("ДОС 54", про_адрес=True)
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

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        return [
            g.GeoHit(
                street="кв-л ДОС",
                house="54",
                settlement=None,
                city="Хабаровск",
                region="Хабаровский край",
                lat=48.47,
                lon=135.13,
                house_level=True,
            )
        ]

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        raise AssertionError("DaData подтвердила дом — OSM спрашивать незачем")

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm)
    итог = await worker.geocode_candidate(
        {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}, cid
    )
    assert итог == g.GEO_EXACT
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert (row.geo_provider, row.geo_formatted) == ("dadata", "кв-л ДОС, 54, Хабаровск")
    assert int(await redis.get(worker.dadata_calls_key()) or 0) == 1

    # Выключатель: DaData не спрашивается, цепочка идёт с OSM.
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_DADATA_ENABLED: False}, user_id=None
        )
        await s.execute(sa_update_pending(cid))
        await s.commit()

    async def dadata_forbidden(query, **kw):  # noqa: ANN001
        raise AssertionError("DaData выключена в настройках")

    async def osm_ok(query, wait=None, **kw):  # noqa: ANN001
        return [
            g.GeoHit(
                street="ДОС квартал (Большой Аэродром)",
                house="54",
                settlement=None,
                city="Хабаровск",
                region="Хабаровский край",
                lat=48.47,
                lon=135.13,
                house_level=True,
            )
        ]

    monkeypatch.setattr(worker.dadata, "search", dadata_forbidden)
    monkeypatch.setattr(worker.nominatim, "search", osm_ok)
    assert (
        await worker.geocode_candidate(
            {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}, cid
        )
        == g.GEO_EXACT
    )


def sa_update_pending(cid):  # noqa: ANN001
    import sqlalchemy as sa

    from app.models import ClientAddressCandidate

    return (
        sa.update(ClientAddressCandidate)
        .where(ClientAddressCandidate.id == cid)
        .values(geo_status="pending", geo_attempts=0)
    )
