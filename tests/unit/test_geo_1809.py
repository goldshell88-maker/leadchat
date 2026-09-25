"""Карта и точка по скринам владельца 18.09 (участок G).

Что было: DaData отдаёт дом ФИАС с точкой улицы или пункта (`qc_geo` 2/3), и
карточка получала точку за полтора километра от дома («Янтарная 9к1» —
центр мкр Зелёная Роща); пункт, названный клиентом, проигрывал городу
объявления («ДНТ Ангара тур ул. Луговая 3» — «есть в области, проверьте
вариант»; «Люберцы, д. Горелово, СНТ Заречное, 36» — деревня терялась в
скобках DaData); участок массива («СНТ Солнечный 47») — «нашла улицу, но не
дом»; «38-й комплекс, 7» и «кв-л 84/85, д 4» — сверка ключей.

Сеть здесь не ходит: карты подменены записанными ответами. Живой DaData
18.09 снят только по `value`/`fias_level`/`qc_geo`/координатам, раскладка
полей `data` восстановлена по образцам 13.09 («ДНТ Ромашка (село Нижнее
Заречье)», «мкр Южный (пгт Заречный)») — обе встречающиеся формы
(«СНТ Заречное (…)» и «Заречное (…)») проверяются.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
from leadchat_gateway.providers import dadata as gw_dadata

from app.integrations import gateway
from app.integrations.avito.listing_url import City
from app.models import Client, ClientAddressCandidate, Conversation, Message
from app.services import address_parse, app_settings
from app.services import clients as clients_svc
from app.services import geocode as g
from app.workers import geocode as worker

pytestmark = pytest.mark.anyio

СЕРТОЛОВО = City("Сертолово", "Ленинградская область", "Europe/Moscow")
УЛАН_УДЭ = City("Улан-Удэ", "Республика Бурятия", "Asia/Irkutsk")
ЛЮБЕРЦЫ = City("Люберцы", "Московская область", "Europe/Moscow")
ДОМОДЕДОВО = City("Домодедово", "Московская область", "Europe/Moscow")
ЧЕЛНЫ = City("Набережные Челны", "Республика Татарстан", "Europe/Moscow")
АНГАРСК = City("Ангарск", "Иркутская область", "Asia/Irkutsk")
САРАНСК = City("Саранск", "Республика Мордовия", "Europe/Moscow")


def дом(**kw: Any) -> g.GeoHit:
    база: dict[str, Any] = {
        "street": "ул Ленина",
        "house": "5",
        "settlement": None,
        "city": "Орск",
        "region": "Оренбургская область",
        "lat": 51.2,
        "lon": 58.5,
        "house_level": True,
    }
    база.update(kw)
    return g.GeoHit(**база)


def _dadata(ответ: dict) -> list[g.GeoHit]:
    """Разбор живёт в шлюзе; вердикту нужны dataclass-ы LeadChat."""
    return [g.GeoHit(**h.model_dump()) for h in gw_dadata.parse_response(ответ)]


#: «Янтарная 9к1» (Сертолово): DaData — дом ФИАС, точка мкр (qc_geo 3);
#: Яндекс — тот же дом, `precision=exact`.
ЯНТАРНАЯ_DADATA = _dadata(
    {
        "suggestions": [
            {
                "value": "Ленинградская обл, г Сертолово, мкр Зеленая Роща, ул Янтарная, 9 к 1",
                "data": {
                    "region_with_type": "Ленинградская обл",
                    "city": "Сертолово",
                    "settlement": "Зеленая Роща",
                    "settlement_with_type": "мкр Зеленая Роща",
                    "street_with_type": "ул Янтарная",
                    "house": "9",
                    "block_type": "к",
                    "block": "1",
                    "geo_lat": "60.185107",
                    "geo_lon": "30.134979",
                    "qc_geo": "3",
                    "fias_level": "8",
                },
            }
        ]
    }
)[0]
ЯНТАРНАЯ_ЯНДЕКС = g.GeoHit(
    street="Янтарная улица",
    house="9к1",
    settlement="микрорайон Зелёная Роща",
    city="Сертолово",
    region="Ленинградская область",
    lat=60.188775,
    lon=30.10929,
    house_level=True,
    settlement_kind="district",
)


# ── чистый вердикт ─────────────────────────────────────────────────────────────


def test_дом_фиас_с_точкой_пункта_остаётся_домом_но_точка_неточная() -> None:
    """Сторож вердикта не отвергает дом ФИАС из-за точности точки (деревни без
    координат домов — бой 12.09), а точность видна отдельно."""
    assert ЯНТАРНАЯ_DADATA.house_level and not ЯНТАРНАЯ_DADATA.precise
    parsed = g.Parsed(street="Янтарная", house="9к1", level="C")
    статус, hit = g.verdict(parsed, СЕРТОЛОВО, [ЯНТАРНАЯ_DADATA])
    assert статус == g.GEO_EXACT and hit is ЯНТАРНАЯ_DADATA


def test_точка_того_же_дома_у_яндекса_берётся_а_чужого_нет() -> None:
    точный = g.precise_point(ЯНТАРНАЯ_DADATA, СЕРТОЛОВО.region, [ЯНТАРНАЯ_ЯНДЕКС])
    assert точный is not None and точный.precise
    assert (точный.lat, точный.lon) == (ЯНТАРНАЯ_ЯНДЕКС.lat, ЯНТАРНАЯ_ЯНДЕКС.lon)
    # Строка адреса — по-прежнему от DaData: менялась только точка.
    assert (точный.street, точный.house, точный.city) == ("ул Янтарная", "9 к 1", "Сертолово")
    # ДИВЕРСИИ: другой номер, другая улица, другой город, точка не дома,
    # другая область — точку не берём.
    for чужой in (
        dataclasses.replace(ЯНТАРНАЯ_ЯНДЕКС, house="9"),
        dataclasses.replace(ЯНТАРНАЯ_ЯНДЕКС, street="Яблоневая улица"),
        dataclasses.replace(ЯНТАРНАЯ_ЯНДЕКС, city="Всеволожск", settlement=None),
        dataclasses.replace(ЯНТАРНАЯ_ЯНДЕКС, house_level=False),
        dataclasses.replace(ЯНТАРНАЯ_ЯНДЕКС, precise=False),
        dataclasses.replace(ЯНТАРНАЯ_ЯНДЕКС, region="Новгородская область"),
    ):
        assert g.precise_point(ЯНТАРНАЯ_DADATA, СЕРТОЛОВО.region, [чужой]) is None


def test_метка_приблизительной_точки_в_имени_провайдера() -> None:
    assert g.mark_approx("dadata") == "dadata~approx"
    assert g.mark_approx("dadata~approx") == "dadata~approx"
    assert g.point_is_approx("speller+dadata~approx") and not g.point_is_approx("dadata+yandex")
    assert not g.point_is_approx(None)


def test_пункт_клиента_с_пробелом_против_дефиса_карты_и_тип_массива() -> None:
    """«ДНТ Ангара тур» ↔ «ДНТ Ангара-Тур» (Улан-Удэ, 18.09); «СНТ Солнечный»
    ↔ «Солнечный»: тип массива — не слово имени."""
    assert g._пункт_назван("ангара тур", ["днт", "ангара-тур"])
    assert g._пункт_назван("ангара-тур", ["ангара", "тур"])
    assert g._пункт_назван("снт солнечный", ["солнечный"])
    assert not g._пункт_назван("снт солнечный", ["лесной"])
    assert not g._пункт_назван("снт", ["солнечный"])


АНГАРА_ТУР = _dadata(
    {
        "suggestions": [
            {
                "value": "Респ Бурятия, село Кедровка, тер ДНТ Ангара-Тур, ул Луговая, д 3",
                "data": {
                    "region_with_type": "Респ Бурятия",
                    "area_with_type": "Иволгинский р-н",
                    "city": None,
                    "settlement": "ДНТ Ангара-Тур (село Кедровка)",
                    "settlement_with_type": "тер ДНТ Ангара-Тур (село Кедровка)",
                    "street_with_type": "ул Луговая",
                    "house": "3",
                    "geo_lat": "51.85",
                    "geo_lon": "107.4",
                    "qc_geo": "3",
                    "fias_level": "8",
                },
            }
        ]
    }
)


def test_днт_названный_клиентом_главнее_города_объявления() -> None:
    found = address_parse.parse("ДНТ Ангара тур ул. Луговая 3.")
    assert found is not None and (found.settlement, found.settlement_type) == (
        "Ангара тур",
        "ДНТ",
    )
    parsed = g.Parsed(
        street=found.street,
        house=found.house,
        settlement=found.settlement,
        settlement_type=found.settlement_type,
    )
    статус, hit = g.verdict(parsed, УЛАН_УДЭ, АНГАРА_ТУР)
    assert статус == g.GEO_EXACT and hit is not None
    assert g.format_address(hit, parsed) == "ул Луговая, 3, ДНТ Ангара-Тур, Кедровка"
    # Без пункта тот же дом в Кедровке при объявлении в Улан-Удэ — не тот город.
    assert g.verdict(g.Parsed(street="ул Луговая", house="3"), УЛАН_УДЭ, АНГАРА_ТУР)[0] == (
        g.GEO_CITY_MISMATCH
    )


def _горелово(settlement: str) -> list[g.GeoHit]:
    return _dadata(
        {
            "suggestions": [
                {
                    "value": "Московская обл, г Люберцы, деревня Горелово, тер. СНТ Заречное, д 36",
                    "data": {
                        "region_with_type": "Московская обл",
                        "city": "Люберцы",
                        "settlement": settlement,
                        "settlement_with_type": "тер. СНТ Заречное (деревня Горелово)",
                        "street_with_type": None,
                        "house": "36",
                        "geo_lat": "55.6",
                        "geo_lon": "37.9",
                        "qc_geo": "3",
                        "fias_level": "8",
                    },
                }
            ]
        }
    )


@pytest.mark.parametrize(
    "settlement", ["СНТ Заречное (деревня Горелово)", "Заречное (деревня Горелово)"]
)
def test_деревня_из_скобок_не_теряется_когда_город_есть(settlement: str) -> None:
    """«Люберцы Д. Горелово Снт Заречное Д. 36»: клиент назвал и город, и
    деревню, и массив — карта нашла дом именно там: exact, не «другой город»."""
    hits = _горелово(settlement)
    assert (hits[0].settlement, hits[0].city, hits[0].area) == (
        "Горелово",
        "Люберцы",
        "СНТ Заречное",
    )
    # Сегодняшний разбор: массив — «улица».
    found = address_parse.parse("Люберцы Д. Горелово Снт Заречное Д. 36")
    assert found is not None and found.locality == "Люберцы" and found.settlement == "Горелово"
    сегодня = g.Parsed(
        street=found.street,
        house=found.house,
        settlement=found.settlement,
        settlement_type=found.settlement_type,
        locality=found.locality,
    )
    # Будущий разбор (участок P): пункт + массив + дом, улицы нет.
    завтра = g.Parsed(
        street="",
        house="36",
        settlement="Горелово",
        settlement_type="деревня",
        locality="Люберцы",
        area="СНТ Заречное",
    )
    for parsed in (сегодня, завтра):
        статус, hit = g.verdict(parsed, ЛЮБЕРЦЫ, hits)
        assert статус == g.GEO_EXACT and hit is not None, parsed
        assert g.format_address(hit, parsed) == "тер. СНТ Заречное, 36, деревня Горелово, Люберцы"
        assert g.build_query(parsed, ЛЮБЕРЦЫ) == g.Query(
            region="Московская область",
            city="Люберцы",
            settlement="Горелово",
            street=parsed.street or "СНТ Заречное",
            house="36",
        )
    # ДИВЕРСИЯ: дом 36 в той же деревне, но не в массиве — не тот дом.
    вне_массива = [dataclasses.replace(hits[0], street="ул Центральная", area=None)]
    assert g.verdict(завтра, ЛЮБЕРЦЫ, вне_массива)[0] == g.GEO_STREET_MISMATCH
    assert g.verdict(сегодня, ЛЮБЕРЦЫ, вне_массива)[0] == g.GEO_STREET_MISMATCH


СОЛНЕЧНЫЙ = _dadata(
    {
        "suggestions": [
            {
                "value": "Респ Бурятия, Заиграевский р-н, тер. СНТ Солнечный, д 47",
                "data": {
                    "region_with_type": "Респ Бурятия",
                    "area_with_type": "Заиграевский р-н",
                    "city": None,
                    "settlement": "Солнечный",
                    "settlement_with_type": "тер. СНТ Солнечный",
                    "street_with_type": None,
                    "house": "47",
                    "geo_lat": "51.9",
                    "geo_lon": "108.1",
                    "qc_geo": "3",
                    "fias_level": "65",
                },
            }
        ]
    }
)


def test_участок_массива_это_дом_с_приблизительной_точкой() -> None:
    """«Снт Солнечный 47»: участков у ФИАС нет (уровень 65), а мастер едет в
    массив и спрашивает номер — дом, точка массива, Яндекс потом уточнит."""
    assert СОЛНЕЧНЫЙ[0].house_level and not СОЛНЕЧНЫЙ[0].precise
    found = address_parse.parse("Снт Солнечный 47")
    assert found is not None
    сегодня = g.Parsed(
        street=found.street,
        house=found.house,
        settlement=found.settlement,
        settlement_type=found.settlement_type,
    )
    завтра = g.Parsed(street="", house="47", settlement="Солнечный", settlement_type="СНТ")
    for parsed in (сегодня, завтра):
        статус, hit = g.verdict(parsed, УЛАН_УДЭ, СОЛНЕЧНЫЙ)
        assert статус == g.GEO_EXACT and hit is not None, parsed
        assert g.format_address(hit, parsed) == "тер. СНТ Солнечный, 47, Солнечный"
        # В запрос массив идёт с типом: «Солнечный 47» карта примет за село.
        query = g.build_query(parsed, УЛАН_УДЭ)
        assert query is not None and query.settlement == "СНТ Солнечный"
    # Яндекс знает участок улицей массива («10-я улица, 47, СНТ Солнечный»):
    # массив назван местом — точка его.
    яндекс = g.GeoHit(
        street="10-я улица",
        house="47",
        settlement="садовое товарищество Солнечный",
        city="Заиграево",
        region="Республика Бурятия",
        lat=51.91,
        lon=108.12,
        house_level=True,
    )
    точный = g.precise_point(СОЛНЕЧНЫЙ[0], УЛАН_УДЭ.region, [яндекс])
    assert точный is not None and (точный.lat, точный.lon) == (51.91, 108.12)
    # А улица другого пункта с тем же номером — нет.
    assert (
        g.precise_point(
            СОЛНЕЧНЫЙ[0],
            УЛАН_УДЭ.region,
            [dataclasses.replace(яндекс, settlement=None, city="Улан-Удэ")],
        )
        is None
    )


def test_кп_новое_сосново_42() -> None:
    """«КП Новое Сосново, 42» (Домодедово): у DaData дом ФИАС с точкой села."""
    hits = _dadata(
        {
            "suggestions": [
                {
                    "value": "Московская обл, г Домодедово, село Сосново, КП Новое Сосново, 42",
                    "data": {
                        "region_with_type": "Московская обл",
                        "city": "Домодедово",
                        "settlement": "КП Новое Сосново (село Сосново)",
                        "settlement_with_type": "тер КП Новое Сосново (село Сосново)",
                        "street_with_type": None,
                        "house": "42",
                        "geo_lat": "55.39416",
                        "geo_lon": "37.729186",
                        "qc_geo": "3",
                        "fias_level": "8",
                    },
                }
            ]
        }
    )
    сегодня = g.Parsed(street="Новое Сосново", house="42", level="B")
    завтра = g.Parsed(street="", house="42", settlement="Новое Сосново", settlement_type="КП")
    for parsed in (сегодня, завтра):
        статус, hit = g.verdict(parsed, ДОМОДЕДОВО, hits)
        assert статус == g.GEO_EXACT and hit is not None and not hit.precise, parsed
    яндекс = g.GeoHit(
        street="территория КП Новое Сосново",
        house="42",
        settlement="село Сосново",
        city="Домодедово",
        region="Московская область",
        lat=55.395505,
        lon=37.716762,
        house_level=True,
    )
    точный = g.precise_point(hits[0], ДОМОДЕДОВО.region, [яндекс])
    assert точный is not None and (точный.lat, точный.lon) == (55.395505, 37.716762)


def test_челны_комплекс_и_нули_впереди() -> None:
    """«38/07» → «38-й комплекс», дом «7»: Яндекс знает, ключи сходятся."""
    parsed = g.Parsed(street="38-й комплекс", house="7")
    яндекс = g.GeoHit(
        street="38-й комплекс",
        house="7",
        settlement=None,
        city="Набережные Челны",
        region="Республика Татарстан",
        lat=55.7,
        lon=52.4,
        house_level=True,
    )
    assert g.verdict(parsed, ЧЕЛНЫ, [яндекс])[0] == g.GEO_EXACT
    assert g.verdict(dataclasses.replace(parsed, street="38 комплекс"), ЧЕЛНЫ, [яндекс])[0] == (
        g.GEO_EXACT
    )
    assert g.house_key("07") == g.house_key("7") == "7"
    assert g.house_key("0") == "0" and g.house_key("10") == "10"
    assert g.verdict(dataclasses.replace(parsed, house="07"), ЧЕЛНЫ, [яндекс])[0] == g.GEO_EXACT
    # Другой комплекс с тем же домом — не тот.
    assert g.verdict(parsed, ЧЕЛНЫ, [dataclasses.replace(яндекс, street="39-й комплекс")])[0] == (
        g.GEO_STREET_MISMATCH
    )


def test_ангарск_квартал_84_85_дом_4_выбирает_не_мусор() -> None:
    """DaData первым даёт «кв-л 1, д 84/85» — ключ дома его отсеивает."""
    hits = _dadata(
        {
            "suggestions": [
                {
                    "value": "Иркутская обл, г Ангарск, кв-л 1, д 84/85",
                    "data": {
                        "region_with_type": "Иркутская обл",
                        "city": "Ангарск",
                        "street_with_type": "кв-л 1",
                        "house": "84/85",
                        "geo_lat": "52.5",
                        "geo_lon": "103.8",
                        "qc_geo": "0",
                        "fias_level": "8",
                    },
                },
                {
                    "value": "Иркутская обл, г Ангарск, кв-л 84/85, д 4",
                    "data": {
                        "region_with_type": "Иркутская обл",
                        "city": "Ангарск",
                        "street_with_type": "кв-л 84/85",
                        "house": "4",
                        "geo_lat": "52.54",
                        "geo_lon": "103.9",
                        "qc_geo": "0",
                        "fias_level": "8",
                    },
                },
            ]
        }
    )
    for улица in ("квартал 84/85", "кв-л 84/85", "84/85 квартал"):
        статус, hit = g.verdict(g.Parsed(street=улица, house="4"), АНГАРСК, hits)
        assert статус == g.GEO_EXACT and hit is hits[1], улица
        assert hit.precise
    assert g.format_address(hits[1], g.Parsed(street="квартал 84/85", house="4")) == (
        "кв-л 84/85, 4, Ангарск"
    )
    # Дом «84/85» на квартале 1 — другой дом: «квартал 1, 84/85» так и остаётся.
    assert g.verdict(g.Parsed(street="квартал 84/85", house="19"), АНГАРСК, hits)[0] == (
        g.GEO_HOUSE_MISMATCH
    )


def test_ялга_пункт_без_типа_в_запросе_и_вердикте() -> None:
    """«Саранск, Ялга, ул Садовая 3»: без «Ялга» карта подтверждала
    Садовая, 3 в самом Саранске — не тот дом."""
    parsed = g.Parsed(street="ул Садовая", house="3", settlement="Ялга", locality="Саранск")
    query = g.build_query(parsed, САРАНСК)
    assert query is not None and query.settlement == "Ялга" and query.city == "Саранск"
    ялга = _dadata(
        {
            "suggestions": [
                {
                    "value": "Респ Мордовия, г Саранск, рп Ялга, ул Садовая, д 3",
                    "data": {
                        "region_with_type": "Респ Мордовия",
                        "city": "Саранск",
                        "settlement": "Ялга",
                        "settlement_with_type": "рп Ялга",
                        "street_with_type": "ул Садовая",
                        "house": "3",
                        "geo_lat": "54.15",
                        "geo_lon": "45.1",
                        "qc_geo": "0",
                        "fias_level": "8",
                    },
                },
                {
                    "value": "Респ Мордовия, г Саранск, ул Садовая, д 3",
                    "data": {
                        "region_with_type": "Респ Мордовия",
                        "city": "Саранск",
                        "street_with_type": "ул Садовая",
                        "house": "3",
                        "geo_lat": "54.19",
                        "geo_lon": "45.18",
                        "qc_geo": "0",
                        "fias_level": "8",
                    },
                },
            ]
        }
    )
    статус, hit = g.verdict(parsed, САРАНСК, ялга)
    assert статус == g.GEO_EXACT and hit is ялга[0]
    assert g.format_address(hit, parsed) == "ул Садовая, 3, Ялга, Саранск"
    # Пункт назван, а дом только в городе — отказ, не дом в Саранске.
    assert g.verdict(parsed, САРАНСК, [ялга[1]])[0] == g.GEO_SETTLEMENT_MISMATCH


# ── воркер: цепочка dadata → yandex за точкой, пункт клиента, комплекс Челнов ──


def ctx(db_sessionmaker: Any, redis: Any) -> dict:
    return {"db_session_factory": db_sessionmaker, "redis": redis, "job_try": 1}


async def _строка(seed: Any, db_sessionmaker: Any, слаг: str, found: address_parse.Found) -> Any:
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation_id)
        conv.item_city_slug = слаг
        card = await s.get(Client, seed.client_id)
        card.address = None
        # Реплика строки — её цитата, как на живом пути: автозапись с 19.09
        # перечитывает реплику нынешним разбором, и речь seed'а («Экран
        # разбит, почём?») в карточку не пошла бы.
        сообщение = (
            await s.execute(sa.select(Message).where(Message.id == seed.message_id))
        ).scalar_one()
        сообщение.body = found.raw
        записано = await clients_svc.record_address_candidate(
            s,
            client=card,
            conversation_id=conv.id,
            message_id=seed.message_id,
            message_at=datetime.now(UTC),
            found=found,
            now=datetime.now(UTC),
        )
        await s.commit()
        assert записано.candidate_id is not None
        return записано.candidate_id


async def _row(db_sessionmaker: Any, cid: Any) -> ClientAddressCandidate:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row is not None
        return row


def _разобрать(текст: str) -> address_parse.Found:
    found = address_parse.parse(текст)
    assert found is not None, текст
    return found


@pytest.fixture
def dadata_отвечает(monkeypatch: Any) -> dict[str, Any]:
    """DaData на шлюзе с ключом: `ответы` — список ответов по порядку
    походов (последний повторяется); походы и запросы считаются."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    состояние: dict[str, Any] = {"ответы": [[]], "запросы": []}

    async def search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        состояние["запросы"].append(query)
        ответы = состояние["ответы"]
        ответ = ответы.pop(0) if len(ответы) > 1 else ответы[0]
        if kw.get("seen") is not None:
            kw["seen"].extend(ответ)
        return list(ответ)

    async def без_точки(city: Any, region: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(worker.dadata, "search", search)
    monkeypatch.setattr(worker.dadata, "city_point", без_точки)
    return состояние


@pytest.fixture
def osm_пусто(monkeypatch: Any) -> list[g.Query]:
    вызовы: list[g.Query] = []

    async def search(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        вызовы.append(query)
        if wait is not None:
            await wait()
        return []

    monkeypatch.setattr(worker.nominatim, "search", search)
    return вызовы


@pytest.fixture
def яндекс_отвечает(monkeypatch: Any) -> dict[str, Any]:
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    состояние: dict[str, Any] = {"ответ": [], "запросы": []}

    async def search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        состояние["запросы"].append(query)
        return list(состояние["ответ"])

    monkeypatch.setattr(worker.yandex_geocoder, "search", search)
    return состояние


async def _режим(db_sessionmaker: Any, режим: str) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s,
            {
                app_settings.ADDRESS_GEO_PROVIDER: режим,
                app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT: 900,
                app_settings.ADDRESS_GEO_SUGGEST_ENABLED: False,
                app_settings.ADDRESS_GEO_AHUNTER_ENABLED: False,
                app_settings.ADDRESS_GEO_SPELLER_ENABLED: False,
            },
            user_id=None,
        )
        await s.commit()


async def test_точка_дома_dadata_уточняется_яндексом(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    яндекс_отвечает: dict,
) -> None:
    """«Янтарная 9к1»: DaData подтвердила дом, точка — центр микрорайона;
    Яндекс спрашивается по компонентам дома и даёт точку дома."""
    await _режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [[ЯНТАРНАЯ_DADATA]]
    яндекс_отвечает["ответ"] = [ЯНТАРНАЯ_ЯНДЕКС]
    cid = await _строка(seed_conversation, db_sessionmaker, "sertolovo", _разобрать("Янтарная 9к1"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_EXACT
    assert row.geo_provider == "dadata+yandex"
    assert (row.geo_lat, row.geo_lon) == (ЯНТАРНАЯ_ЯНДЕКС.lat, ЯНТАРНАЯ_ЯНДЕКС.lon)
    assert row.geo_formatted == "ул Янтарная, 9 к 1, Зеленая Роща, Сертолово"
    # Запрос к Яндексу — из компонентов найденного дома, не из слов клиента.
    assert яндекс_отвечает["запросы"] == [
        g.Query(
            region="Ленинградская область",
            city="Сертолово",
            settlement="Зеленая Роща",
            street="ул Янтарная",
            house="9 к 1",
        )
    ]
    assert await worker.yandex_calls_today(redis) == 1
    assert osm_пусто == []  # DaData подтвердила — OSM не спрашивался
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_яндекс_дал_другой_дом_точка_остаётся_приблизительной(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    яндекс_отвечает: dict,
) -> None:
    await _режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [[ЯНТАРНАЯ_DADATA]]
    яндекс_отвечает["ответ"] = [dataclasses.replace(ЯНТАРНАЯ_ЯНДЕКС, house="7")]
    cid = await _строка(seed_conversation, db_sessionmaker, "sertolovo", _разобрать("Янтарная 9к1"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata~approx" and g.point_is_approx(row.geo_provider)
    assert (row.geo_lat, row.geo_lon) == (ЯНТАРНАЯ_DADATA.lat, ЯНТАРНАЯ_DADATA.lon)
    assert row.geo_status == g.GEO_EXACT  # вердикт не меняется, меняется пометка


async def test_без_яндекса_и_за_потолком_точка_приблизительная_без_похода(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    яндекс_отвечает: dict,
) -> None:
    await _режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [[ЯНТАРНАЯ_DADATA]]
    яндекс_отвечает["ответ"] = [ЯНТАРНАЯ_ЯНДЕКС]
    await redis.set(worker.yandex_calls_key(), 900)  # потолок выбран
    cid = await _строка(seed_conversation, db_sessionmaker, "sertolovo", _разобрать("Янтарная 9к1"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata~approx" and яндекс_отвечает["запросы"] == []
    # Точная точка DaData (qc_geo 0) Яндекса не просит и пометки не носит.
    await redis.delete(worker.yandex_calls_key())
    dadata_отвечает["ответы"] = [[dataclasses.replace(ЯНТАРНАЯ_DADATA, precise=True)]]
    cid2 = await _строка(
        seed_conversation, db_sessionmaker, "sertolovo", _разобрать("ул Янтарная 9 к 1")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid2) == g.GEO_EXACT
    row2 = await _row(db_sessionmaker, cid2)
    assert row2.geo_provider == "dadata" and яндекс_отвечает["запросы"] == []


async def test_днт_клиента_в_области_это_адрес_а_не_вариант(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
) -> None:
    """«ДНТ Ангара тур ул. Луговая 3» при объявлении в Улан-Удэ: в городе дома
    нет, по области DaData находит его в Кедровке — в названном клиентом ДНТ.
    Раньше: «есть в области — проверьте вариант»; теперь exact и автозапись."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[], АНГАРА_ТУР]  # по городу пусто, по области — дом
    cid = await _строка(
        seed_conversation, db_sessionmaker, "ulan-ude", _разобрать("ДНТ Ангара тур ул. Луговая 3.")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_EXACT and not row.geo_variants
    assert row.geo_formatted == "ул Луговая, 3, ДНТ Ангара-Тур, Кедровка"
    assert row.geo_provider == "dadata~approx"  # Яндекса нет — точка массива, помечена
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    # Массив с типом ушёл и в запрос к карте.
    assert dadata_отвечает["запросы"][0].settlement == "ДНТ Ангара Тур"


async def test_город_и_деревня_клиента_дом_в_них_exact_а_не_другой_город(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
) -> None:
    """«Люберцы Д. Горелово Снт Заречное Д. 36» при объявлении в Томилине."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [_горелово("Заречное (деревня Горелово)")]
    cid = await _строка(
        seed_conversation,
        db_sessionmaker,
        "tomilino",
        _разобрать("Люберцы Д. Горелово Снт Заречное Д. 36"),
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_formatted == "тер. СНТ Заречное, 36, деревня Горелово, Люберцы"
    assert not row.geo_variants
    # Дом искали в городе клиента, не объявления.
    assert dadata_отвечает["запросы"][0].city == "Люберцы"


async def test_участок_снт_в_воркере_exact_вместо_нашла_улицу_но_не_дом(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [СОЛНЕЧНЫЙ]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "ulan-ude", _разобрать("Снт Солнечный 47")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_formatted == "тер. СНТ Солнечный, 47, Солнечный"
    assert row.geo_provider == "dadata~approx"
    assert dadata_отвечает["запросы"][0].settlement == "СНТ Солнечный"


async def test_место_с_домом_идёт_путём_дома(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
) -> None:
    """Будущее представление участка P: kind=place с домом — карта сверяет дом,
    а не ищет точку места."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [СОЛНЕЧНЫЙ]
    found = address_parse.Found(
        street="",
        house="47",
        raw="Снт Солнечный 47",
        start=0,
        end=16,
        level="A",
        settlement="Солнечный",
        settlement_type="СНТ",
        kind=address_parse.KIND_PLACE,
    )
    cid = await _строка(seed_conversation, db_sessionmaker, "ulan-ude", found)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_formatted == "тер. СНТ Солнечный, 47, Солнечный"
    assert dadata_отвечает["запросы"][0] == g.Query(
        region="Республика Бурятия",
        city="Улан-Удэ",
        settlement="СНТ Солнечный",
        street="",
        house="47",
    )


async def test_челны_цепочка_доходит_до_яндекса(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    яндекс_отвечает: dict,
) -> None:
    """«38-й комплекс, 7»: DaData не знает, OSM пусто — Яндекс подтверждает."""
    await _режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [[]]
    яндекс_отвечает["ответ"] = [
        g.GeoHit(
            street="38-й комплекс",
            house="7",
            settlement=None,
            city="Набережные Челны",
            region="Республика Татарстан",
            lat=55.7,
            lon=52.4,
            house_level=True,
        )
    ]
    found = address_parse.Found(
        street="38-й комплекс", house="7", raw="38/07", start=0, end=5, level="A"
    )
    cid = await _строка(seed_conversation, db_sessionmaker, "naberezhnye_chelny", found)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (
        row.geo_provider == "yandex" and row.geo_formatted == "38-й комплекс, 7, Набережные Челны"
    )
    assert osm_пусто and яндекс_отвечает["запросы"][0].free_text == (
        "Республика Татарстан, Набережные Челны, 38-й комплекс 7"
    )


async def test_автозапись_исправляет_себя_по_той_же_реплике(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any
) -> None:
    """Ангарск (владелец 18.09): старый разбор реплики «85-й квартал, 17 /
    улица Гагрина, 12» дал «ул Гагарина, 17» — карта подтвердила дом в
    Байкальске, автозапись положила его в карточку. Новый разбор той же
    реплики дал «кв-л 85, 17», тоже подтверждён — карточка обязана перейти
    на него, а старую строку автоматика отказывает сама (никто из людей её
    не подтверждал)."""

    async def exact(cid: Any, formatted: str, точка: tuple[float, float]) -> None:
        async with db_sessionmaker() as s:
            row = await s.get(ClientAddressCandidate, cid)
            row.geo_status = g.GEO_EXACT
            row.geo_formatted = formatted
            row.geo_provider = "dadata"
            row.geo_lat, row.geo_lon = точка
            await s.commit()

    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()
    старая = await _строка(
        seed_conversation,
        db_sessionmaker,
        "angarsk",
        address_parse.Found(
            street="улица Гагрина",
            house="17",
            raw="85-й квартал, 17 / улица Гагрина, 12",
            start=0,
            end=36,
            level="A",
        ),
    )
    await exact(старая, "ул Гагарина, 17, Байкальск, Ангарск", (52.5222, 103.9401))
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        assert card.address_candidate_id == старая and "Гагарина" in card.address
    # Новый разбор ТОЙ ЖЕ реплики (message_id и цитата те же).
    новая = await _строка(
        seed_conversation,
        db_sessionmaker,
        "angarsk",
        address_parse.Found(
            street="85-й квартал",
            house="17",
            raw="85-й квартал, 17 / улица Гагрина, 12",
            start=0,
            end=36,
            level="A",
        ),
    )
    async with db_sessionmaker() as s:  # _строка обнуляет адрес ради фикстуры — вернём старый
        card = await s.get(Client, seed_conversation.client_id)
        card.address = "ул Гагарина, 17, Байкальск, Ангарск"
        card.address_candidate_id = старая
        await s.commit()
    # Другая точка: школа на Гагарина и дом в 85-м квартале — разные места.
    await exact(новая, "кв-л 85, 17, Ангарск", (52.5368, 103.9000))
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        assert card.address_candidate_id == новая and card.address == "кв-л 85, 17, Ангарск"
        assert card.address_set_by_id is None
        прежняя = await s.get(ClientAddressCandidate, старая)
        assert прежняя.status == clients_svc.CANDIDATE_REJECTED and прежняя.resolved_by_id is None
        assert (await s.get(ClientAddressCandidate, новая)).status == clients_svc.CANDIDATE_ACCEPTED
    # Второй прогон — ничего не меняет: два подтверждённых дома из одной реплики уже разведены.
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "skip"
    )


# ── ревью 18.09: pending не уступает, точка пункта не улика, пригород, город клиента ──


async def _exact(
    db_sessionmaker: Any,
    cid: Any,
    formatted: str,
    точка: tuple[float, float],
    *,
    provider: str = "dadata",
) -> None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status = g.GEO_EXACT
        row.geo_formatted = formatted
        row.geo_provider = provider
        row.geo_lat, row.geo_lon = точка
        await s.commit()


async def _автозапись_включена(db_sessionmaker: Any) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_DETECT_AUTOFILL: True}, user_id=None)
        await s.commit()


def _found(street: str, house: str, raw: str | None = None, **kw: Any) -> address_parse.Found:
    return address_parse.Found(
        street=street, house=house, raw=raw or f"{street} {house}", start=0, end=0, level="A", **kw
    )


async def test_источник_со_сброшенным_вердиктом_не_уступает_чужой_строке(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any
) -> None:
    """Источник карточки (авто) получил пункт из соседней реплики — вердикт
    сброшен, строка ждёт карту. В этом окне другая строка из ДРУГОЙ реплики
    стала exact: автозапись не заменяет источник и не отказывает его — два
    дома из разных реплик разводит оператор, а «pending» — не отказ карты."""
    await _автозапись_включена(db_sessionmaker)
    источник = await _строка(
        seed_conversation, db_sessionmaker, "saransk", _found("ул Ленина", "5")
    )
    await _exact(db_sessionmaker, источник, "ул Ленина, 5, Саранск", (54.18, 45.18))
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        conv = await s.get(Conversation, seed_conversation.conversation_id)
        другая = ClientAddressCandidate(
            client_id=seed_conversation.client_id,
            conversation_id=conv.id,
            message_id=None,
            message_at=datetime.now(UTC),
            value="ул Мира, 7",
            street="ул Мира",
            house="7",
            raw="ул Мира 7",
            level="A",
            source="inbound",
            status=clients_svc.CANDIDATE_PENDING,
            kind=address_parse.KIND_HOUSE,
            detected_at=datetime.now(UTC),
            geo_status=g.GEO_EXACT,
            geo_formatted="ул Мира, 7, Саранск",
            geo_provider="dadata",
            geo_lat=54.19,
            geo_lon=45.20,
        )
        s.add(другая)
        clients_svc.сбросить_вердикт(
            await s.get(ClientAddressCandidate, источник), reason="settlement_named"
        )
        await s.commit()
        другая_id = другая.id
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "skip"
    )
    async with db_sessionmaker() as s:
        card = await s.get(Client, seed_conversation.client_id)
        assert card.address_candidate_id == источник and card.address == "ул Ленина, 5, Саранск"
        assert (await s.get(ClientAddressCandidate, источник)).status == (
            clients_svc.CANDIDATE_ACCEPTED
        )
        assert (await s.get(ClientAddressCandidate, другая_id)).status == (
            clients_svc.CANDIDATE_PENDING
        )


def _строка_с_точкой(
    street: str, house: str, formatted: str, точка: tuple[float, float], provider: str
) -> ClientAddressCandidate:
    return ClientAddressCandidate(
        kind=address_parse.KIND_HOUSE,
        street=street,
        house=house,
        value=f"{street}, {house}",
        level="A",
        geo_status=g.GEO_EXACT,
        geo_formatted=formatted,
        geo_provider=provider,
        geo_lat=точка[0],
        geo_lon=точка[1],
        detected_at=datetime(2026, 9, 18, tzinfo=UTC),
        status=clients_svc.CANDIDATE_PENDING,
    )


def test_приблизительная_точка_не_улика_одного_дома() -> None:
    """DaData при `qc_geo` 2/3 ставит одну точку на всю деревню или массив:
    «ул Ленина 5» и «ул Пушкина 7» одной деревни стояли в одной точке и
    считались одним домом. Точка не дома — не улика одного дома: остаются
    строка карты и её ключ."""
    точка = (53.29, 50.41)
    a = _строка_с_точкой("ул Ленина", "5", "ул Ленина, 5, Смышляевка", точка, "dadata~approx")
    b = _строка_с_точкой("ул Пушкина", "7", "ул Пушкина, 7, Смышляевка", точка, "dadata~approx")
    assert not clients_svc.одно_место(a, b) and not clients_svc.одно_место(b, a)
    # Та же точка у одной из двух — тоже не улика.
    b.geo_provider = "dadata"
    assert not clients_svc.одно_место(a, b)
    # Точные точки в одном месте — один дом (две карты об одном доме).
    a.geo_provider = "dadata+yandex"
    assert clients_svc.одно_место(a, b)
    # Одна строка карты — один дом и с приблизительной точкой.
    a.geo_provider = b.geo_provider = "dadata~approx"
    b.geo_formatted = a.geo_formatted
    assert clients_svc.одно_место(a, b)
    # Память об отказе по месту: приблизительной точки у неё нет.
    assert worker._место(a) is None
    a.geo_provider = "dadata"
    assert worker._место(a) == (53.29, 50.41)


async def test_два_дома_деревни_в_одной_точке_автозапись_пропускает(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any
) -> None:
    """Две exact-строки из разных реплик с одной приблизительной точкой и
    разными домами — два адреса, а не «одно место»: автозапись пропускает,
    решает оператор. А отказ по одному дому деревни не запирает другой."""
    await _автозапись_включена(db_sessionmaker)
    точка = (53.29, 50.41)
    a = await _строка(seed_conversation, db_sessionmaker, "samara", _found("ул Ленина", "5"))
    await _exact(db_sessionmaker, a, "ул Ленина, 5, Смышляевка", точка, provider="dadata~approx")
    b = await _строка(seed_conversation, db_sessionmaker, "samara", _found("ул Пушкина", "7"))
    await _exact(db_sessionmaker, b, "ул Пушкина, 7, Смышляевка", точка, provider="dadata~approx")
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "skip"
    )
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, a)
        row.status = clients_svc.CANDIDATE_REJECTED
        row.resolved_at = datetime.now(UTC)
        await s.commit()
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    async with db_sessionmaker() as s:
        assert (await s.get(Client, seed_conversation.client_id)).address_candidate_id == b


САМАРА = (53.1959, 50.1002)
СМЫШЛЯЕВКА = дом(
    settlement="Смышляевка",
    city=None,
    region="Самарская область",
    lat=53.2569,
    lon=50.3563,
)
КИНЕЛЬ = дом(settlement=None, city="Кинель", region="Самарская область", lat=53.2213, lon=50.6342)
ДАЛЬНИЙ = дом(settlement="Дальнее", city=None, region="Самарская область", lat=53.1959, lon=51.0)


@pytest.fixture
def точка_города(monkeypatch: Any) -> dict[str, Any]:
    состояние: dict[str, Any] = {"точка": None}

    async def city_point(city: Any, region: Any, **kw: Any) -> Any:
        return состояние["точка"]

    monkeypatch.setattr(worker.dadata, "city_point", city_point)
    return состояние


async def test_единственный_дом_в_40_км_от_города_это_адрес(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """Владелец 18.09: у половины «в другом месте» единственный дом стоит в
    2–19 км от города объявления (Реутов → Москва, Самара → Смышляевка) —
    пригород. Единственный дом области в 40 км — подтверждён: exact с
    пунктом дома, автозапись."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[], [СМЫШЛЯЕВКА]]  # по городу пусто, около и по области — один дом
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_formatted) == (g.GEO_EXACT, "ул Ленина, 5, Смышляевка")
    assert row.geo_provider == "dadata" and not row.geo_variants
    assert (row.geo_lat, row.geo_lon) == (СМЫШЛЯЕВКА.lat, СМЫШЛЯЕВКА.lon)
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


@pytest.mark.parametrize(
    ("ответы_области", "точка", "ожидание"),
    [
        ([ДАЛЬНИЙ], САМАРА, 1),  # 60 км — не пригород
        ([СМЫШЛЯЕВКА, КИНЕЛЬ], САМАРА, 2),  # два дома в радиусе — выбирает оператор
        ([СМЫШЛЯЕВКА], None, 1),  # точки города нет — правило не применяется
    ],
)
async def test_пригород_не_применяется(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    ответы_области: list[g.GeoHit],
    точка: tuple[float, float] | None,
    ожидание: int,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = точка
    dadata_отвечает["ответы"] = [[], ответы_области]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_ELSEWHERE and len(row.geo_variants or []) == ожидание


async def test_пригород_не_применяется_когда_клиент_назвал_другой_пункт(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """«д. Курумоч ул Ленина 5»: клиент назвал деревню, а единственный дом
    области — в Смышляевке: не тот дом, пусть и в 18 км."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[], [СМЫШЛЯЕВКА]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "samara", _разобрать("д. Курумоч ул Ленина 5")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_ELSEWHERE and len(row.geo_variants or []) == 1


def test_пригород_чистая_функция() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    САМАРА_ГОРОД = City("Самара", "Самарская область", "Europe/Samara")
    assert g.suburb_hit(parsed, САМАРА_ГОРОД, [СМЫШЛЯЕВКА], САМАРА) is СМЫШЛЯЕВКА
    assert g.suburb_hit(parsed, САМАРА_ГОРОД, [ДАЛЬНИЙ], САМАРА) is None
    assert g.suburb_hit(parsed, САМАРА_ГОРОД, [СМЫШЛЯЕВКА, КИНЕЛЬ], САМАРА) is None
    assert g.suburb_hit(parsed, САМАРА_ГОРОД, [СМЫШЛЯЕВКА], None) is None
    # Не тот номер дома, не та улица, не та область — не кандидат.
    for чужой in (
        dataclasses.replace(СМЫШЛЯЕВКА, house="7"),
        dataclasses.replace(СМЫШЛЯЕВКА, street="ул Мира"),
        dataclasses.replace(СМЫШЛЯЕВКА, region="Оренбургская область"),
        dataclasses.replace(СМЫШЛЯЕВКА, house_level=False),
    ):
        assert g.suburb_hit(parsed, САМАРА_ГОРОД, [чужой], САМАРА) is None
    # Клиент назвал город или пункт — дом обязан быть в нём.
    assert (
        g.suburb_hit(
            dataclasses.replace(parsed, locality="Кинель"), САМАРА_ГОРОД, [СМЫШЛЯЕВКА], САМАРА
        )
        is None
    )
    assert (
        g.suburb_hit(
            dataclasses.replace(parsed, locality="Смышляевка"), САМАРА_ГОРОД, [СМЫШЛЯЕВКА], САМАРА
        )
        is СМЫШЛЯЕВКА
    )
    assert (
        g.suburb_hit(
            dataclasses.replace(parsed, settlement="Курумоч", settlement_type="деревня"),
            САМАРА_ГОРОД,
            [СМЫШЛЯЕВКА],
            САМАРА,
        )
        is None
    )
    assert round(g.distance_km(САМАРА, (СМЫШЛЯЕВКА.lat, СМЫШЛЯЕВКА.lon))) == 18


ВОЛГОДОНСК = (47.5136, 42.1512)
ЦИМЛЯНСК_ДОМ = дом(
    settlement=None, city="Цимлянск", region="Ростовская область", lat=47.6479, lon=42.0936
)
ВОЛГОДОНСК_ДОМ = дом(
    settlement=None, city="Волгодонск", region="Ростовская область", lat=47.52, lon=42.16
)


async def test_город_клиента_вне_справочника_дом_ищется_в_нём(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
) -> None:
    """Замер 18.09: «г Цимлянск, ул Ленина 5» при объявлении в Волгодонске —
    новый разбор берёт город клиента, а Цимлянска в справочнике нет: дом
    ищется в нём (DaData с city=Цимлянск), и это exact, а не «клиент назвал
    другой город» без вариантов и без поиска."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[ЦИМЛЯНСК_ДОМ]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "volgodonsk", _разобрать("г Цимлянск, ул Ленина 5")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_formatted == "ул Ленина, 5, Цимлянск"
    assert dadata_отвечает["запросы"][0].city == "Цимлянск"
    assert dadata_отвечает["запросы"][0].region == "Ростовская область"


async def test_дома_в_городе_клиента_нет_варианты_и_поиск_по_области(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """В Цимлянске такого дома нет, в Волгодонске есть: «клиент назвал другой
    город» — с вариантом, а не пустой тупик; пригородное правило на чужой
    город не распространяется (клиент назвал свой)."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ВОЛГОДОНСК
    dadata_отвечает["ответы"] = [[], [ВОЛГОДОНСК_ДОМ]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "volgodonsk", _разобрать("г Цимлянск, ул Ленина 5")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_OTHER_CITY
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_OTHER_CITY
    assert [v["formatted"] for v in row.geo_variants or []] == ["ул Ленина, 5, Волгодонск"]
    assert g.GEO_OTHER_CITY in worker._ИЩЕМ_ПО_ОБЛАСТИ
    assert g.GEO_OTHER_CITY in worker._ПЕРЕЧИТАТЬ_МОДЕЛЬЮ


async def test_другой_город_от_карты_послабее_перепроверяется_областью(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    monkeypatch: Any,
) -> None:
    """DaData по городу клиента пусто, OSM отдал дом-тёзку в Волгодонске
    (`other_city_in_text`) — раньше это был тупик без поиска по области;
    теперь область спрашивается, и дом в Цимлянске находится."""
    await _режим(db_sessionmaker, "nominatim")

    async def osm(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        if wait is not None:
            await wait()
        return [ВОЛГОДОНСК_ДОМ]

    monkeypatch.setattr(worker.nominatim, "search", osm)
    dadata_отвечает["ответы"] = [[], [ЦИМЛЯНСК_ДОМ]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "volgodonsk", _разобрать("г Цимлянск, ул Ленина 5")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_formatted == "ул Ленина, 5, Цимлянск" and row.geo_provider == "dadata"
