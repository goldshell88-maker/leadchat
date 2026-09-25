"""DaData за шлюзом: заголовок с ключом, тело запроса, `locations`/`locations_geo`,
границы уровней у точки города, каскад статусов и чистый разбор ответа.

Перенесено из `tests/unit/test_dadata_1209.py` LeadChat (12.09) вместе с
кодом провайдера. Образец ответа — живой, снят 12.09 на дом с дробью по
Хабаровскому краю (поля обрезаны до используемых; улица, дом и координаты
заменены синтетикой той же формы).
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import structlog

from leadchat_gateway.config import settings
from leadchat_gateway.errors import ProviderError
from leadchat_gateway.providers import dadata
from leadchat_gateway.routes import geo_dadata

pytestmark = pytest.mark.anyio

КЛЮЧ = "DADATA-KEY-1"

ОТВЕТ = {
    "suggestions": [
        {
            "value": "Хабаровский край, г Комсомольск-на-Амуре, ул Ясная, д 23 к 4",
            "data": {
                "region_with_type": "Хабаровский край",
                "city": "Комсомольск-на-Амуре",
                "settlement": None,
                "settlement_with_type": None,
                "street_with_type": "ул Ясная",
                "house": "23",
                "block_type": "к",
                "block": "4",
                "geo_lat": "50.55012",
                "geo_lon": "136.99456",
                "qc_geo": "0",
                "fias_level": "8",
            },
        },
        {
            "value": "…, кв 1",
            "data": {
                "region_with_type": "Хабаровский край",
                "city": "Комсомольск-на-Амуре",
                "street_with_type": "ул Ясная",
                "house": "23",
                "block_type": "к",
                "block": "4",
                "geo_lat": "50.55012",
                "geo_lon": "136.99456",
                "qc_geo": "0",
                "fias_level": "9",
            },
        },
        # Микрорайон без улицы — пункт и есть «улица».
        {
            "value": "Иркутская обл, г Ангарск, мкр 9, д 14",
            "data": {
                "region_with_type": "Иркутская обл",
                "city": "Ангарск",
                "settlement": "9",
                "settlement_with_type": "мкр 9",
                "street_with_type": None,
                "house": "14",
                "geo_lat": "52.51234",
                "geo_lon": "103.871234",
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
            "value": "Вологодская обл, Череповецкий р-н, д Новое Заозерье, ул Рябиновая, д 8",
            "data": {
                "region_with_type": "Вологодская обл",
                "city": None,
                "settlement": "Новое Заозерье",
                "settlement_with_type": "д Новое Заозерье",
                "street_with_type": "ул Рябиновая",
                "house": "8",
                "geo_lat": "59.3",
                "geo_lon": "37.6",
                "qc_geo": "3",
                "fias_level": "8",
            },
        },
        # Дома в справочнике нет — не дом, какими бы ни были координаты.
        {
            "value": "д Новое Заозерье, ул Рябиновая, д 88",
            "data": {
                "region_with_type": "Вологодская обл",
                "settlement": "Новое Заозерье",
                "settlement_with_type": "д Новое Заозерье",
                "street_with_type": "ул Рябиновая",
                "house": "88",
                "geo_lat": "59.3",
                "geo_lon": "37.6",
                "qc_geo": "0",
                "fias_level": "-1",
            },
        },
        # Улица без дома — не дом.
        {
            "value": "ул Ясная",
            "data": {"street_with_type": "ул Ясная", "house": None, "fias_level": "7"},
        },
    ]
}

ОТВЕТ_МЕСТА = {
    "suggestions": [
        {
            "value": "Ленинградская обл, Гатчинский р-н, зона Южный",
            "data": {
                "fias_level": "65",
                "region_with_type": "Ленинградская обл",
                "area_with_type": "Гатчинский р-н",
                "city": None,
                "settlement": "Южный (деревня Большая Дубрава)",
                "settlement_with_type": "зона Южный (деревня Большая Дубрава)",
                "geo_lat": "59.651234",
                "geo_lon": "30.104567",
            },
        },
        {
            "value": "Ленинградская обл, Гатчинский р-н, д Большая Дубрава",
            "data": {
                "fias_level": "6",
                "region_with_type": "Ленинградская обл",
                "area_with_type": "Гатчинский р-н",
                "city": None,
                "settlement": "Большая Дубрава",
                "settlement_with_type": "д Большая Дубрава",
                "geo_lat": "59.65",
                "geo_lon": "30.10",
            },
        },
    ]
}


#: Крым, стенд 19.09. Фильтр `[{"kladr_id": "91"}, {"kladr_id": "92"}]`. Живьём
#: сняты `region_iso_code` (UA-43 — документ, почему `RU-CR` молчал),
#: `region_kladr_id` и `value` ПЕРВОЙ подсказки; `value` второй у пробы был
#: «г Симферополь, ул Бахчисарайская» (без региона) — здесь дописан регион для
#: читаемости, разбор (`parse_places`/`parse_response`) поле `value` не читает.
#: Раскладка остальных полей `data` — по образцам 12–18.09 (лимит живых запросов
#: ушёл на проверку гипотезы). ПД нет: объекты справочника.
ОТВЕТ_БАХЧИСАРАЙ = {
    "suggestions": [
        {
            "value": "респ Крым, г Бахчисарай",
            "data": {
                "region_iso_code": "UA-43",
                "region_kladr_id": "9100000000000",
                "region_with_type": "Респ Крым",
                "area_with_type": "Бахчисарайский р-н",
                "city": "Бахчисарай",
                "city_with_type": "г Бахчисарай",
                "settlement": None,
                "settlement_with_type": None,
                "street_with_type": None,
                "house": None,
                "geo_lat": "44.751407",
                "geo_lon": "33.875445",
                "qc_geo": "4",
                "fias_level": "4",
            },
        },
        # Второй живой ответ: улица-тёзка в Симферополе — не место «Бахчисарай».
        {
            "value": "респ Крым, г Симферополь, ул Бахчисарайская",
            "data": {
                "region_iso_code": "UA-43",
                "region_kladr_id": "9100000000000",
                "region_with_type": "Респ Крым",
                "area_with_type": None,
                "city": "Симферополь",
                "city_with_type": "г Симферополь",
                "settlement": None,
                "settlement_with_type": None,
                "street_with_type": "ул Бахчисарайская",
                "house": None,
                "geo_lat": "44.9727",
                "geo_lon": "34.1096",
                "qc_geo": "2",
                "fias_level": "7",
            },
        },
        # Район (уровень 3) — не место, разбор его пропускает.
        {
            "value": "респ Крым, Бахчисарайский р-н",
            "data": {
                "region_iso_code": "UA-43",
                "region_kladr_id": "9100000000000",
                "region_with_type": "Респ Крым",
                "area_with_type": "Бахчисарайский р-н",
                "city": None,
                "settlement": None,
                "street_with_type": None,
                "house": None,
                "geo_lat": "44.75",
                "geo_lon": "33.87",
                "qc_geo": "4",
                "fias_level": "3",
            },
        },
    ]
}

#: «ул Войкова 37» по Крыму (дом по области): Ялта — дом и квартира в нём,
#: тёзка в Керчи, соседний номер. «Войкова 37» в отчётах стенда не встречается
#: — это не адрес клиента из корпуса.
ОТВЕТ_ВОЙКОВА = {
    "suggestions": [
        {
            "value": "респ Крым, г Ялта, ул Войкова, д 37",
            "data": {
                "region_iso_code": "UA-43",
                "region_kladr_id": "9100000000000",
                "region_with_type": "Респ Крым",
                "city": "Ялта",
                "city_with_type": "г Ялта",
                "settlement": None,
                "settlement_with_type": None,
                "street_with_type": "ул Войкова",
                "house": "37",
                "geo_lat": "44.495",
                "geo_lon": "34.166",
                "qc_geo": "0",
                "fias_level": "8",
            },
        },
        {
            "value": "респ Крым, г Ялта, ул Войкова, д 37, кв 1",
            "data": {
                "region_iso_code": "UA-43",
                "region_kladr_id": "9100000000000",
                "region_with_type": "Респ Крым",
                "city": "Ялта",
                "city_with_type": "г Ялта",
                "street_with_type": "ул Войкова",
                "house": "37",
                "geo_lat": "44.495",
                "geo_lon": "34.166",
                "qc_geo": "0",
                "fias_level": "9",
            },
        },
        {
            "value": "респ Крым, г Керчь, ул Войкова, д 37",
            "data": {
                "region_iso_code": "UA-43",
                "region_kladr_id": "9100000000000",
                "region_with_type": "Респ Крым",
                "city": "Керчь",
                "city_with_type": "г Керчь",
                "street_with_type": "ул Войкова",
                "house": "37",
                "geo_lat": "45.35",
                "geo_lon": "36.47",
                "qc_geo": "0",
                "fias_level": "8",
            },
        },
        {
            "value": "респ Крым, г Ялта, ул Войкова, д 3",
            "data": {
                "region_iso_code": "UA-43",
                "region_kladr_id": "9100000000000",
                "region_with_type": "Респ Крым",
                "city": "Ялта",
                "city_with_type": "г Ялта",
                "street_with_type": "ул Войкова",
                "house": "3",
                "geo_lat": "44.494",
                "geo_lon": "34.165",
                "qc_geo": "0",
                "fias_level": "8",
            },
        },
    ]
}


@pytest.fixture
def с_ключом(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "dadata_api_key", КЛЮЧ)


# --- чистый разбор ------------------------------------------------------------


def test_разбор_ответа_дом_корпус_микрорайон() -> None:
    hits = dadata.parse_response(ОТВЕТ)
    assert [(h.street, h.house, h.city, h.house_level, h.interpolated) for h in hits] == [
        ("ул Ясная", "23 к 4", "Комсомольск-на-Амуре", True, False),
        ("ул Ясная", "23 к 4", "Комсомольск-на-Амуре", True, False),
        ("мкр 9", "14", "Ангарск", True, False),
        ("ул Ленина", "12 к 2", "Орск", True, False),
        ("ул Рябиновая", "8", "Новое Заозерье", True, False),
        ("ул Рябиновая", "88", "Новое Заозерье", False, False),
    ]
    # Пункт стал «улицей», но остался и пунктом (по нему LeadChat сверяет названное).
    assert hits[2].settlement == "9"
    # Имена полей — один в один с dataclass-ом LeadChat: обёртка соберёт GeoHit(**d).
    assert set(hits[0].model_dump()) == {
        "street",
        "house",
        "settlement",
        "city",
        "region",
        "lat",
        "lon",
        "house_level",
        "interpolated",
        "settlement_kind",
        "precise",
        "area",
    }
    # Точка дома есть только при `qc_geo` 0/1; дом деревни с точкой пункта —
    # дом (`house_level`), но точка приблизительная.
    assert [h.precise for h in hits] == [True, True, True, True, False, True]


#: Живьём 18.09 (поля обрезаны; улицы, дома, массивы клиентов и точка первой
#: подсказки заменены синтетикой той же формы): дом ФИАС с точкой пункта,
#: участок массива без строки ФИАС, структура внутри города с родителем в скобках.
ОТВЕТ_1809 = {
    "suggestions": [
        # «Лучистая 7к3», Сертолово: дом есть (fias 8), точка — центр мкр
        # Чёрная Речка (qc_geo 3), за полтора километра от дома.
        {
            "value": "Ленинградская обл, г Сертолово, мкр Черная Речка, ул Лучистая, д 7 к 3",
            "data": {
                "region_with_type": "Ленинградская обл",
                "city": "Сертолово",
                "settlement": "Черная Речка",
                "settlement_with_type": "мкр Черная Речка",
                "street_with_type": "ул Лучистая",
                "house": "7",
                "block_type": "к",
                "block": "3",
                "geo_lat": "60.185107",
                "geo_lon": "30.134979",
                "qc_geo": "3",
                "fias_level": "8",
            },
        },
        # «СНТ Светлый 23», Бурятия: участка у ФИАС нет (fias 65), точка массива.
        {
            "value": "Респ Бурятия, Заиграевский р-н, тер. СНТ Светлый, д 23",
            "data": {
                "region_with_type": "Респ Бурятия",
                "area_with_type": "Заиграевский р-н",
                "city": None,
                "settlement": "Светлый",
                "settlement_with_type": "тер. СНТ Светлый",
                "street_with_type": None,
                "house": "23",
                "geo_lat": "51.9",
                "geo_lon": "108.1",
                "qc_geo": "3",
                "fias_level": "65",
            },
        },
        # Улица без дома в ФИАС с точкой улицы — по-прежнему не дом.
        {
            "value": "Респ Бурятия, г Улан-Удэ, ул Прямая, д 999",
            "data": {
                "region_with_type": "Респ Бурятия",
                "city": "Улан-Удэ",
                "street_with_type": "ул Прямая",
                "house": "999",
                "geo_lat": "51.8",
                "geo_lon": "107.6",
                "qc_geo": "2",
                "fias_level": "7",
            },
        },
        # «д. Ивняково, СНТ Рассвет, 17»: город есть — родитель из скобок
        # остаётся пунктом, структура уходит в массив.
        {
            "value": "Московская обл, г Люберцы, деревня Ивняково, тер. СНТ Рассвет, д 17",
            "data": {
                "region_with_type": "Московская обл",
                "city": "Люберцы",
                "settlement": "СНТ Рассвет (деревня Ивняково)",
                "settlement_with_type": "тер. СНТ Рассвет (деревня Ивняково)",
                "street_with_type": None,
                "house": "17",
                "geo_lat": "55.6",
                "geo_lon": "37.9",
                "qc_geo": "3",
                "fias_level": "8",
            },
        },
        # Города нет — родитель по-прежнему становится городом (бой 13.09).
        {
            "value": "Респ Бурятия, село Гурульба, тер ДНТ Лесная-Поляна, ул Прямая, д 3",
            "data": {
                "region_with_type": "Респ Бурятия",
                "area_with_type": "Иволгинский р-н",
                "city": None,
                "settlement": "ДНТ Лесная-Поляна (село Гурульба)",
                "settlement_with_type": "тер ДНТ Лесная-Поляна (село Гурульба)",
                "street_with_type": "ул Прямая",
                "house": "3",
                "geo_lat": "51.85",
                "geo_lon": "107.4",
                "qc_geo": "3",
                "fias_level": "8",
            },
        },
    ]
}


def test_точность_точки_участок_массива_и_структура_в_скобках() -> None:
    hits = dadata.parse_response(ОТВЕТ_1809)
    assert [
        (h.street, h.house, h.settlement, h.city, h.area, h.house_level, h.precise) for h in hits
    ] == [
        ("ул Лучистая", "7 к 3", "Черная Речка", "Сертолово", "мкр Черная Речка", True, False),
        ("тер. СНТ Светлый", "23", None, "Светлый", "СНТ Светлый", True, False),
        ("ул Прямая", "999", None, "Улан-Удэ", None, False, False),
        ("тер. СНТ Рассвет", "17", "Ивняково", "Люберцы", "СНТ Рассвет", True, False),
        ("ул Прямая", "3", "ДНТ Лесная-Поляна", "Гурульба", "ДНТ Лесная-Поляна", True, False),
    ]


def test_разбор_мест() -> None:
    hits = dadata.parse_places(ОТВЕТ_МЕСТА)
    assert [(h.kind, h.name, h.settlement, h.district) for h in hits] == [
        ("area", "зона Южный", "Большая Дубрава", "Гатчинский р-н"),
        ("settlement", "д Большая Дубрава", "Большая Дубрава", "Гатчинский р-н"),
    ]


def test_ответ_не_той_формы_это_bad_response() -> None:
    for кривой in ("строка", ["список"], {"suggestions": "не список"}):
        with pytest.raises(ProviderError) as exc:
            dadata.parse_response(кривой)
        assert exc.value.kind == "bad_response"
        with pytest.raises(ProviderError) as exc:
            dadata.parse_places(кривой)
        assert exc.value.kind == "bad_response"


def test_ограничение_поиска_город_или_область() -> None:
    assert dadata.locations_for(region="Хабаровский край", city="Хабаровск") == [
        {"city": "Хабаровск"}
    ]
    assert dadata.locations_for(region="Хабаровский край", city=None) == [{"region": "Хабаровский"}]
    assert dadata.locations_for(region="Республика Татарстан", city=None) == [
        {"region": "Татарстан"}
    ]
    assert dadata.locations_for(region="Москва и область", city=None) == [
        {"region_iso_code": "RU-MOW"},
        {"region_iso_code": "RU-MOS"},
    ]
    assert dadata.locations_for(region="Москва", city=None) == [
        {"region_iso_code": "RU-MOW"},
        {"region_iso_code": "RU-MOS"},
    ]
    assert dadata.locations_for(region="ХМАО — Югра", city=None) == [{"region_iso_code": "RU-KHM"}]
    assert dadata.locations_for(region=None, city=None) == []


def test_крым_и_севастополь_по_кладр_а_не_по_iso() -> None:
    """ISO у DaData для Крыма — UA-43/UA-40 (справочник HFLabs), RU-CR/RU-SEV
    там нет, и фильтр по ним отдавал пустоту молча (стенд 19.09: `RU-CR` → 0,
    `kladr_id 91` → 3, первый — «респ Крым, г Бахчисарай»). КЛАДР 91/92 от
    политики справочника не зависит; пара — как у Москвы и области."""
    assert dadata.locations_for(region="Республика Крым", city=None) == [
        {"kladr_id": "91"},
        {"kladr_id": "92"},
    ]
    assert dadata.locations_for(region="Севастополь", city=None) == [
        {"kladr_id": "92"},
        {"kladr_id": "91"},
    ]
    # Город назван — фильтр по городу, как везде; регион не участвует.
    assert dadata.locations_for(region="Республика Крым", city="Ялта") == [{"city": "Ялта"}]
    # Сторож: кодов, которых нет у DaData, не отдаёт ни один регион.
    коды = {ф.get("region_iso_code") for фф in dadata._РЕГИОН_ФИЛЬТР.values() for ф in фф}
    assert not коды & {"RU-CR", "RU-SEV"}
    # Контрпример: «Крым» внутри слова — не Крым. Крымск (Краснодарский край)
    # и «ул. Крымская» в Подмосковье ищутся своими регионами.
    assert dadata.locations_for(region="Краснодарский край", city=None) == [
        {"region": "Краснодарский"}
    ]
    assert dadata.locations_for(region="Краснодарский край", city="Крымск") == [{"city": "Крымск"}]
    assert dadata.locations_for(region="Московская область", city=None) == [
        {"region_iso_code": "RU-MOS"},
        {"region_iso_code": "RU-MOW"},
    ]
    # Копия, а не общий объект: правка ответа не портит словарь.
    a = dadata.locations_for(region="Республика Крым", city=None)
    a[0]["kladr_id"] = "00"
    assert dadata.locations_for(region="Республика Крым", city=None)[0] == {"kladr_id": "91"}


# --- ручки --------------------------------------------------------------------


@respx.mock
async def test_ключ_в_заголовке_тело_и_ограничение_городом(
    gw: httpx.AsyncClient, с_ключом: None
) -> None:
    маршрут = respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(200, json=ОТВЕТ))
    r = await gw.post(
        "/geo/dadata",
        json={
            "text": "Заречный улица Звенигородская 1",
            "region": "Оренбургская область",
            "city": "Орск",
        },
    )
    assert r.status_code == 200
    данные = r.json()
    assert данные["ok"] is True and len(данные["hits"]) == 6
    assert данные["hits"][0]["street"] == "ул Ясная" and данные["hits"][0]["house"] == "23 к 4"
    # Одна ручка — один запрос к DaData: цикл текстов остался у LeadChat.
    assert маршрут.call_count == 1
    запрос = маршрут.calls[0].request
    assert запрос.headers["Authorization"] == f"Token {КЛЮЧ}"
    assert запрос.headers["Accept"] == "application/json"
    тело = json.loads(запрос.content)
    assert тело == {
        "query": "Заречный улица Звенигородская 1",
        "count": dadata.COUNT,
        "locations": [{"city": "Орск"}],
    }
    # Ключ наружу не уходит.
    assert КЛЮЧ not in r.text


@respx.mock
async def test_без_города_ограничение_областью_и_iso(gw: httpx.AsyncClient, с_ключом: None) -> None:
    маршрут = respx.post(dadata.BASE_URL).mock(
        return_value=httpx.Response(200, json={"suggestions": []})
    )
    r = await gw.post("/geo/dadata", json={"text": "x 1", "region": "Хабаровский край"})
    assert r.json() == {"ok": True, "hits": []}
    assert json.loads(маршрут.calls[0].request.content)["locations"] == [{"region": "Хабаровский"}]
    await gw.post("/geo/dadata", json={"text": "x 1", "region": "Москва"})
    assert json.loads(маршрут.calls[1].request.content)["locations"] == [
        {"region_iso_code": "RU-MOW"},
        {"region_iso_code": "RU-MOS"},
    ]
    # Ни города, ни области — без ограничения.
    await gw.post("/geo/dadata", json={"text": "x 1"})
    assert "locations" not in json.loads(маршрут.calls[2].request.content)


@respx.mock
async def test_точка_города_и_поиск_в_круге(gw: httpx.AsyncClient, с_ключом: None) -> None:
    маршрут = respx.post(dadata.BASE_URL).mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "suggestions": [
                        {
                            "value": "г Череповец",
                            "data": {"city": "Череповец", "geo_lat": "59.12", "geo_lon": "37.9"},
                        },
                    ]
                },
            ),
            httpx.Response(200, json={"suggestions": []}),
            httpx.Response(200, json={"suggestions": []}),
        ]
    )
    r = await gw.post(
        "/geo/dadata/city", json={"city": "Череповец", "region": "Вологодская область"}
    )
    assert r.json() == {"ok": True, "point": [59.12, 37.9]}
    тело = json.loads(маршрут.calls[0].request.content)
    assert тело["from_bound"] == {"value": "city"} and тело["to_bound"] == {"value": "settlement"}
    assert тело["locations"] == [{"region": "Вологодская"}] and тело["count"] == 3
    # Города среди подсказок нет — `point: null`, а не отказ.
    r = await gw.post("/geo/dadata/city", json={"city": "Нигдеевск", "region": None})
    assert r.json() == {"ok": True, "point": None}
    assert "locations" not in json.loads(маршрут.calls[1].request.content)
    # Круг вместо области: `locations_geo`, без `locations`.
    r = await gw.post(
        "/geo/dadata",
        json={"text": "улица Рябиновая 8", "region": "Вологодская область", "near": [59.12, 37.9]},
    )
    assert r.json() == {"ok": True, "hits": []}
    тело = json.loads(маршрут.calls[2].request.content)
    assert тело["locations_geo"] == [
        {"lat": 59.12, "lon": 37.9, "radius_meters": dadata.NEAR_RADIUS_M}
    ]
    assert "locations" not in тело


@respx.mock
async def test_место_по_области_и_по_городу(gw: httpx.AsyncClient, с_ключом: None) -> None:
    маршрут = respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(200, json=ОТВЕТ_МЕСТА))
    r = await gw.post(
        "/geo/dadata/place",
        json={
            "query_text": "Гатчинский р-н деревня Большая Дубрава массив Южный",
            "region": "Ленинградская область",
        },
    )
    данные = r.json()
    assert данные["ok"] is True
    assert [(h["kind"], h["name"]) for h in данные["hits"]] == [
        ("area", "зона Южный"),
        ("settlement", "д Большая Дубрава"),
    ]
    assert set(данные["hits"][0]) == {
        "name",
        "kind",
        "settlement",
        "area",
        "city",
        "district",
        "region",
        "lat",
        "lon",
    }
    тело = json.loads(маршрут.calls[0].request.content)
    assert тело["query"] == "Гатчинский р-н деревня Большая Дубрава массив Южный"
    assert тело["locations"] == [{"region_iso_code": "RU-LEN"}, {"region_iso_code": "RU-SPE"}]
    # Город назван — ищем только в нём.
    await gw.post(
        "/geo/dadata/place",
        json={"query_text": "Индустриальный", "region": "Хабаровский край", "city": "Хабаровск"},
    )
    assert json.loads(маршрут.calls[1].request.content)["locations"] == [{"city": "Хабаровск"}]


@respx.mock
async def test_место_крыма_по_области_уходит_с_кладр_и_разбирается(
    gw: httpx.AsyncClient, с_ключом: None
) -> None:
    маршрут = respx.post(dadata.BASE_URL).mock(
        return_value=httpx.Response(200, json=ОТВЕТ_БАХЧИСАРАЙ)
    )
    r = await gw.post(
        "/geo/dadata/place", json={"query_text": "город Бахчисарай", "region": "Республика Крым"}
    )
    тело = json.loads(маршрут.calls[0].request.content)
    assert тело["locations"] == [{"kladr_id": "91"}, {"kladr_id": "92"}]
    хиты = r.json()["hits"]
    assert хиты and (хиты[0]["kind"], хиты[0]["city"], хиты[0]["region"]) == (
        "city",
        "Бахчисарай",
        "Респ Крым",
    )
    # Дом по области (city=None) — тот же фильтр.
    respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(200, json=ОТВЕТ_ВОЙКОВА))
    r = await gw.post("/geo/dadata", json={"text": "ул Войкова 37", "region": "Республика Крым"})
    assert json.loads(маршрут.calls[1].request.content)["locations"] == [
        {"kladr_id": "91"},
        {"kladr_id": "92"},
    ]
    h = r.json()["hits"][0]
    assert (h["city"], h["house"], h["house_level"], h["region"]) == (
        "Ялта",
        "37",
        True,
        "Респ Крым",
    )
    # Севастополь — пара в обратном порядке; круг `near` фильтр региона не берёт.
    await gw.post("/geo/dadata", json={"text": "ул Войкова 37", "region": "Севастополь"})
    assert json.loads(маршрут.calls[2].request.content)["locations"] == [
        {"kladr_id": "92"},
        {"kladr_id": "91"},
    ]
    await gw.post(
        "/geo/dadata",
        json={"text": "ул Войкова 37", "region": "Республика Крым", "near": [44.5, 34.17]},
    )
    assert "locations" not in json.loads(маршрут.calls[3].request.content)


@respx.mock
async def test_запрет_и_сеть_различаются_и_ключ_не_течёт(
    gw: httpx.AsyncClient, с_ключом: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Логгер модуля запоминает цепочку обработчиков при первом использовании
    # (`cache_logger_on_first_use`), поэтому на время захвата — свежий.
    monkeypatch.setattr(geo_dadata, "log", structlog.get_logger("leadchat_gateway.test"))
    respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(403, text="forbidden"))
    with structlog.testing.capture_logs() as логи:
        r = await gw.post("/geo/dadata", json={"text": "улица Ленина 5", "city": "Орск"})
    assert r.status_code == 200, "отказ провайдера — не отказ шлюза"
    assert r.json() == {"ok": False, "kind": "blocked", "status": 403, "detail": "доступ закрыт"}
    события = [л for л in логи if л["event"] == "dadata.failed"]
    assert события and события[0]["kind"] == "blocked" and события[0]["status"] == 403
    журнал = json.dumps(логи, ensure_ascii=False)
    assert КЛЮЧ not in журнал and "Ленина" not in журнал and "dadata.ru" not in журнал
    assert КЛЮЧ not in r.text

    respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(503))
    r = await gw.post("/geo/dadata", json={"text": "x 1", "city": "Орск"})
    assert (r.json()["ok"], r.json()["kind"], r.json()["status"]) == (False, "network", 503)
    respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(429))
    r = await gw.post("/geo/dadata/city", json={"city": "Орск"})
    assert (r.json()["kind"], r.json()["status"]) == ("network", 429)
    respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(401))
    r = await gw.post("/geo/dadata/place", json={"query_text": "x"})
    assert (r.json()["kind"], r.json()["status"]) == ("blocked", 401)
    respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(302, headers={"location": "/"}))
    r = await gw.post("/geo/dadata", json={"text": "x 1"})
    assert (r.json()["kind"], r.json()["status"]) == ("bad_response", 302)


@respx.mock
async def test_обрыв_и_кривое_тело(gw: httpx.AsyncClient, с_ключом: None) -> None:
    respx.post(dadata.BASE_URL).mock(side_effect=httpx.ConnectTimeout("boom"))
    r = await gw.post("/geo/dadata", json={"text": "улица Ленина 5", "city": "Орск"})
    assert r.json()["ok"] is False and r.json()["kind"] == "network"
    assert r.json()["status"] is None and r.json()["detail"] == "ConnectTimeout"
    assert "Ленина" not in r.text and "dadata.ru" not in r.text
    respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(200, text="<html>"))
    r = await gw.post("/geo/dadata", json={"text": "x 1"})
    assert (r.json()["kind"], r.json()["status"]) == ("bad_response", 200)
    respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(200, json={"suggestions": "?"}))
    r = await gw.post("/geo/dadata", json={"text": "x 1"})
    assert r.json()["kind"] == "bad_response"


@respx.mock
async def test_без_ключа_на_шлюзе_no_key_и_без_похода(gw: httpx.AsyncClient) -> None:
    маршрут = respx.post(dadata.BASE_URL).mock(return_value=httpx.Response(200, json=ОТВЕТ))
    for путь, тело in (
        ("/geo/dadata", {"text": "x 1"}),
        ("/geo/dadata/city", {"city": "Орск"}),
        ("/geo/dadata/place", {"query_text": "x"}),
    ):
        r = await gw.post(путь, json=тело)
        assert r.status_code == 200 and r.json()["ok"] is False
        assert r.json()["kind"] == "no_key" and r.json()["status"] is None
    assert маршрут.call_count == 0


async def test_кривой_вход_это_422_а_не_поход(gw: httpx.AsyncClient, с_ключом: None) -> None:
    assert (await gw.post("/geo/dadata", json={"region": "x"})).status_code == 422
    assert (await gw.post("/geo/dadata", json={"text": "x", "near": [1]})).status_code == 422
    assert (await gw.post("/geo/dadata/city", json={"region": "x"})).status_code == 422
    assert (await gw.post("/geo/dadata/place", json={})).status_code == 422
