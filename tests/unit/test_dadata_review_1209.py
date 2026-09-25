"""Находки ревью DaData (12.09), каждая — воспроизведённая и закрытая.

1. DaData нашла дом с нужным номером в городе, но не один (ambiguous), OSM
   пуст — раньше её ответ выбрасывался, а поиск по области переклеивал те же
   дома в «в другом городе области» и тратил второй запрос.
2. Бан OSM (три 403 за час) ставил на паузу и DaData — независимого первого
   провайдера, который дом дал бы.
3. Режим `yandex` за потолком уходит в OSM; отказ OSM записывался и
   считался как отказ Яндекса.
4. Сторож типа улицы не знал сокращения ФИАС «кв-л»: «кв-л 15, д 15»
   подтверждал «15 мкр д 15» (Ангарск: есть и кварталы, и микрорайоны).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.integrations import gateway
from app.integrations.avito.listing_url import City
from app.models import Client, ClientAddressCandidate, Conversation
from app.services import address_parse, app_settings
from app.services import clients as clients_svc
from app.services import geocode as g
from app.workers import geocode as worker

pytestmark = pytest.mark.anyio

ОРСК = City("Орск", "Оренбургская область", "Asia/Yekaterinburg")


def дом(street: str, house: str, *, settlement: str | None = None, lat: float = 51.2) -> g.GeoHit:
    return g.GeoHit(
        street=street,
        house=house,
        settlement=settlement,
        city="Орск",
        region="Оренбургская область",
        lat=lat,
        lon=58.5,
        house_level=True,
    )


async def _строка(  # noqa: ANN001
    seed_conversation, db_sessionmaker, текст: str, slug: str = "orsk", *, про_адрес: bool = True
):
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        conv.item_city_slug = slug
        card = await s.get(Client, seed_conversation.client_id)
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=seed_conversation.message_id,
            message_at=datetime.now(UTC),
            found=address_parse.parse(текст, про_адрес=про_адрес),
            now=datetime.now(UTC),
        )
        await s.commit()
        return записано.candidate_id


def ctx(db_sessionmaker, redis) -> dict:  # noqa: ANN001
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}


@pytest.fixture(autouse=True)
def без_точки_города(monkeypatch):  # noqa: ANN001
    """Координаты города — отдельный поход к DaData; в тестах он выключен,
    кроме тех, что проверяют именно его (там подменяется явно)."""

    async def нет(city, region, **kw):  # noqa: ANN001
        return None

    monkeypatch.setattr(worker.dadata, "city_point", нет)


async def test_ambiguous_от_dadata_даёт_варианты_а_не_elsewhere(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """С 18.09 (автопривязка, правило `best_of`: пункт не назван — единственный
    дом в самом городе решает) та же строка при включённой настройке
    `address_geo.auto_decide` становится `exact` с хвостом `~approx` (стенд в
    `test_autobind_policy_1809.py`); здесь — диверсия выключателя: выключено →
    прежний `ambiguous` с двумя вариантами и без второго запроса по области."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None)
        await s.commit()
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")
    вызовы: list[str | None] = []

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        вызовы.append(query.city)
        return [дом("ул Ленина", "5"), дом("ул Ленина", "5", settlement="Заречный", lat=51.3)]

    async def osm_пусто(query, wait=None, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm_пусто)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_AMBIGUOUS
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row.geo_provider == "dadata"
        assert row.geo_variants is not None and len(row.geo_variants) == 2
    assert вызовы == ["Орск"], "второго запроса по области быть не должно"
    assert int(await redis.get(worker.dadata_calls_key()) or 0) == 1


async def test_бан_osm_не_останавливает_dadata(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")
    await redis.set("geo:blocked:nominatim", 3)

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        return [дом("ул Ленина", "5")]

    async def osm(query, wait=None, **kw):  # noqa: ANN001
        raise AssertionError("OSM под баном — стучать нельзя")

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT

    # Без DaData тот же бан по-прежнему ставит строку на паузу.
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    cid2 = await _строка(seed_conversation, db_sessionmaker, "ул Мира 7")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid2) == "paused"


async def test_отказ_osm_в_режиме_yandex_носит_имя_osm(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    monkeypatch.setitem(gateway.known_keys, "dadata", False)
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.ADDRESS_GEO_PROVIDER: "yandex",
                app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT: 1,
            },
            user_id=None,
        )
        await s.commit()
    await redis.set(worker.yandex_calls_key(), 1)  # потолок Яндекса выбран → OSM
    cid = await _строка(seed_conversation, db_sessionmaker, "ул Ленина 5")

    async def osm_403(query, wait=None, **kw):  # noqa: ANN001
        raise g.GeocodeError("nominatim", "blocked", 403)

    async def яндекс(query, **kw):  # noqa: ANN001
        raise AssertionError("потолок выбран — Яндекс не спрашивается")

    monkeypatch.setattr(worker.nominatim, "search", osm_403)
    monkeypatch.setattr(worker.yandex_geocoder, "search", яндекс)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_BLOCKED
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row.geo_provider == "nominatim"
    assert int(await redis.get("geo:blocked:nominatim") or 0) == 1
    assert await redis.get("geo:blocked:yandex") is None


def test_кв_л_это_квартал_а_не_микрорайон() -> None:
    ангарск = City("Ангарск", "Иркутская область", "Asia/Irkutsk")
    квартал = g.GeoHit(
        street="кв-л 15",
        house="15",
        settlement=None,
        city="Ангарск",
        region="Иркутская обл",
        lat=52.5,
        lon=103.8,
        house_level=True,
    )
    assert g.verdict(g.Parsed(street="15 мкр", house="15"), ангарск, [квартал])[0] == (
        g.GEO_STREET_MISMATCH
    )
    assert g.verdict(g.Parsed(street="15 квартал", house="15"), ангарск, [квартал])[0] == (
        g.GEO_EXACT
    )


# --- «Малиновка, пер Тихий 2»: названный пункт важнее города объявления ---------

КОСТРОМА = City("Кострома", "Костромская область", "Europe/Moscow")


def _тихий(city: str, lat: float) -> g.GeoHit:
    return g.GeoHit(
        street="пер Тихий",
        house="2",
        settlement=None,
        city=city,
        region="Костромская обл",
        lat=lat,
        lon=41.0,
        house_level=True,
    )


def test_вердикт_названный_пункт_в_области_даёт_exact() -> None:
    hits = [_тихий("Середняя", 57.7), _тихий("Малиновка", 57.8)]
    p = g.Parsed(street="пер тихий", house="2", settlement="Малиновка")
    статус, hit = g.verdict(p, КОСТРОМА, hits)
    assert статус == g.GEO_EXACT and hit is not None and hit.lat == 57.8
    assert g.format_address(hit, p) == "пер Тихий, 2, Малиновка"
    # Без пункта — те же дома лишь варианты; пункт не нашёлся — не дом в городе.
    assert g.verdict(g.Parsed(street="пер тихий", house="2"), КОСТРОМА, hits)[0] == (
        g.GEO_CITY_MISMATCH
    )
    в_городе = [_тихий("Кострома", 57.9)]
    assert g.verdict(p, КОСТРОМА, в_городе)[0] == g.GEO_SETTLEMENT_MISMATCH


async def test_воркер_дом_в_названном_пункте_области_это_адрес_а_не_вариант(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка(
        seed_conversation, db_sessionmaker, "Малиновка, пер тихий дом 2", "kostroma"
    )

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        if query.city:
            return [_тихий("Кострома", 57.9)]  # в городе есть свой Тихий, 2
        return [_тихий("Середняя", 57.7), _тихий("Малиновка", 57.8)]

    async def osm_пусто(query, wait=None, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm_пусто)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert (row.geo_provider, row.geo_formatted) == ("dadata", "пер Тихий, 2, Малиновка")
        assert row.settlement == "Малиновка" and not row.geo_variants


# --- пункт из соседней реплики: «Новое заозерье» + «Рябиновая 8» (Череповец) -----

ЧЕРЕПОВЕЦ = City("Череповец", "Вологодская область", "Europe/Moscow")


def test_подсказки_пункта_из_коротких_реплик() -> None:
    assert address_parse.settlement_hints(
        [
            "Новое заозерье",
            "Частный дом",
            "Стоит простой роутер и модем с симкой 4G, без усилителя",
            "Здравствуйте, плохой интернет",
            "п. Ударник",
            "Да",
            None,
            "Новое заозерье",
        ]
    ) == ["Новое заозерье", "Ударник"]


def _рябиновая(place: str, lat: float) -> g.GeoHit:
    return g.GeoHit(
        street="ул Рябиновая",
        house="8",
        settlement=None,
        city=place,
        region="Вологодская обл",
        lat=lat,
        lon=37.9,
        house_level=True,
    )


async def _реплика_клиента(db_sessionmaker, seed_conversation, текст: str, когда: datetime):  # noqa: ANN001
    from app.models import Message

    async with db_sessionmaker() as s:
        s.add(
            Message(
                conversation_id=seed_conversation.conversation_id,
                external_message_id=f"in-{когда.timestamp()}",
                direction="in",
                sender_type="client",
                body=текст,
                attachments=[],
                delivery_status="delivered",
                created_at=когда,
            )
        )
        await s.commit()


@pytest.mark.parametrize("с_подсказкой", [True, False])
async def test_пункт_из_соседней_реплики_выбирает_дом_по_области(
    seed_conversation, db_sessionmaker, redis, monkeypatch, с_подсказкой: bool
):
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    now = datetime.now(UTC)
    if с_подсказкой:
        await _реплика_клиента(
            db_sessionmaker, seed_conversation, "Новое заозерье", now - timedelta(minutes=3)
        )
    await _реплика_клиента(
        db_sessionmaker, seed_conversation, "Частный дом", now - timedelta(minutes=2)
    )
    cid = await _строка(
        seed_conversation, db_sessionmaker, "Рябиновая 8", "cherepovets", про_адрес=False
    )

    запросы: list[tuple[str | None, str | None, bool]] = []

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        запросы.append((query.city, query.settlement, kw.get("without_settlement_too", True)))
        if query.city:
            return []
        # По области без пункта — десятка чужих домов (бой: Вологда, Бабаево,
        # Ермаково); нужный находится только запросом с пунктом из реплики.
        if query.settlement is None:
            return [_рябиновая("Вологда", 59.2), _рябиновая("Бабаево", 59.4)]
        if query.settlement == "Новое заозерье":
            return [_рябиновая("Новое Заозерье", 59.3)]
        return []

    async def osm_пусто(query, wait=None, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm_пусто)
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row.level == "C"
        if с_подсказкой:
            assert итог == g.GEO_EXACT
            assert row.geo_formatted == "ул Рябиновая, 8, Новое Заозерье"
            assert not row.geo_variants
            # Город → область без пункта → область с пунктом, только с ним.
            assert запросы == [
                ("Череповец", None, True),
                (None, None, True),
                (None, "Новое заозерье", False),
            ]
        else:
            assert итог == g.GEO_ELSEWHERE
            assert len(row.geo_variants) == 2
            assert len(запросы) == 2, "без подсказок лишних запросов нет"
        # Уровень C, но карта нашла дом — оператор строку видит в обоих случаях.
        card = await s.get(Client, seed_conversation.client_id)
        view = await clients_svc.identity_view(s, card)
        assert [c["id"] for c in view["address_candidates"]] == [str(cid)]


async def test_круг_вокруг_города_находит_деревню_раньше_областного_центра(
    seed_conversation, db_sessionmaker, redis, monkeypatch
):
    """Без подсказок: по области DaData отдаёт Вологду и Бабаево, а в круге
    60 км вокруг Череповца — деревни округа. Варианты рядом идут первыми, а
    координаты города кладутся в кэш и второй раз не спрашиваются."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка(
        seed_conversation, db_sessionmaker, "Рябиновая 8", "cherepovets", про_адрес=False
    )
    точек = 0

    async def точка(city, region, **kw):  # noqa: ANN001
        nonlocal точек
        точек += 1
        await kw["on_request"]()
        assert (city, region) == ("Череповец", "Вологодская область")
        return (59.12, 37.9)

    async def dadata_search(query, **kw):  # noqa: ANN001
        await kw["on_request"]()
        if query.city:
            return []
        if kw.get("near") == (59.12, 37.9):
            return [_рябиновая("Новое Заозерье", 59.3), _рябиновая("Ванеево", 59.25)]
        return [_рябиновая("Вологда", 59.2), _рябиновая("Бабаево", 59.4)]

    async def osm_пусто(query, wait=None, **kw):  # noqa: ANN001
        return []

    monkeypatch.setattr(worker.dadata, "city_point", точка)
    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    monkeypatch.setattr(worker.nominatim, "search", osm_пусто)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert [v["city"] for v in row.geo_variants] == [
            "Новое Заозерье",
            "Ванеево",
            "Вологда",
            "Бабаево",
        ]
    assert точек == 1
    assert (
        await redis.get(
            worker.city_point_key(
                КОСТРОМА.__class__("Череповец", "Вологодская область", "Europe/Moscow")
            )
        )
        is not None
    )

    # Вторая строка того же города — координаты из кэша.
    cid2 = await _строка(
        seed_conversation, db_sessionmaker, "Лесная 3", "cherepovets", про_адрес=False
    )
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid2)
    assert точек == 1


# --- «Поселок Сосново дом 9» при объявлении во Фрязине (13.09) -------------------


def test_дом_по_посёлку_без_улицы_в_другом_городе_области_это_exact() -> None:
    фрязино = City("Фрязино", "Московская область", "Europe/Moscow")
    ответ = {
        "suggestions": [
            {
                "value": "Московская обл, г Щёлково, п Сосново, д 9",
                "data": {
                    "region_with_type": "Московская обл",
                    "city": "Щёлково",
                    "settlement": "Сосново",
                    "settlement_with_type": "поселок Сосново",
                    "street_with_type": None,
                    "house": "9",
                    "geo_lat": "55.9",
                    "geo_lon": "38.1",
                    "qc_geo": "0",
                    "fias_level": "8",
                },
            }
        ]
    }
    from leadchat_gateway.providers import dadata as gw_dadata

    hits = [g.GeoHit(**h.model_dump()) for h in gw_dadata.parse_response(ответ)]
    assert (hits[0].street, hits[0].settlement, hits[0].city) == (
        "поселок Сосново",
        "Сосново",
        "Щёлково",
    )
    found = address_parse.parse("Поселок Сосново дом 9")
    assert found is not None
    p = g.Parsed(
        street=found.street,
        house=found.house,
        settlement=found.settlement,
        settlement_type=found.settlement_type,
    )
    статус, hit = g.verdict(p, фрязино, hits)
    assert статус == g.GEO_EXACT and hit is not None
    assert g.format_address(hit, p) == "поселок Сосново, 9, Щёлково"
