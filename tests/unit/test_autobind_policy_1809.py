"""Автопривязка адреса без человека — политика «решить сама» (участок A, 18.09).

Решение владельца: адрес привязывается к карточке сам, оператор ничего не
подтверждает. Здесь — чистый слой (`card_grade`, `auto_decide` и правила,
`known_street`, страна, дробь, семья дома, массив, место у Яндекса) на
записанных ответах карт и воркер целиком: два обязательных случая владельца
(«ул. Серебрянская д8» в Иванове и «ЖК Дубровино» при объявлении в Москве),
поиск по стране, дробь головой, точка массива, точка улицы, выбор из
нескольких, строка улицы без точки. На каждое правило — диверсия: без
признака решения нет, выключатель выключен — вердикт как до 18.09.

Сеть не ходит: карты подменены записанными ответами (фикстуры
`tests/unit/test_geo_1809.py`). Адреса — из скринов владельца и вымышленные.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
import structlog

from app.integrations import gateway
from app.integrations.avito.listing_url import City
from app.services import address_parse, app_settings
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit import test_geo_1809 as стенд
from tests.unit.test_geo_1809 import (
    КИНЕЛЬ,
    САМАРА,
    СМЫШЛЯЕВКА,
    _row,
    _разобрать,
    _режим,
    _строка,
    ctx,
    дом,
)

#: Карты подменены записанными ответами — фикстуры стенда 18.09 под своими
#: именами (присваивание, не импорт: pytest собирает их по имени модуля).
dadata_отвечает = стенд.dadata_отвечает
osm_пусто = стенд.osm_пусто
яндекс_отвечает = стенд.яндекс_отвечает
точка_города = стенд.точка_города

pytestmark = pytest.mark.anyio

ОРСК = City("Орск", "Оренбургская область", "Asia/Yekaterinburg")
ПСКОВ = City("Псков", "Псковская область", "Europe/Moscow")
САМАРА_ГОРОД = City("Самара", "Самарская область", "Europe/Samara")
ВОЛГОДОНСК = City("Волгодонск", "Ростовская область", "Europe/Moscow")
МОСКВА = City("Москва", "Москва", "Europe/Moscow")

СЫЗРАНЬ = дом(settlement=None, city="Сызрань", region="Самарская область", lat=53.15, lon=48.47)
ДАЛЬНИЙ = дом(settlement="Дальнее", city=None, region="Самарская область", lat=53.1959, lon=51.0)


def _evidence(**kw: Any) -> g.Evidence:
    база: dict[str, Any] = {
        "status": g.GEO_AMBIGUOUS,
        "parsed": g.Parsed(street="ул Ленина", house="5"),
        "city": ОРСК,
        "hits": [],
        "city_hits": [],
        "region_hits": [],
        "variants": [],
        "city_point": None,
        "hints": [],
    }
    база.update(kw)
    return g.Evidence(**база)


# ── степень: один судья ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("kind", "status", "provider", "lat", "formatted", "ожидание"),
    [
        ("house", "exact", "dadata", 51.2, "ул Ленина, 5, Орск", g.GRADE_EXACT),
        ("house", "exact", "dadata+yandex", 51.2, "ул Ленина, 5, Орск", g.GRADE_EXACT),
        ("house", "exact", "dadata~approx", 51.2, "ул Ленина, 5, Орск", g.GRADE_APPROX),
        # Хвост читается подстрокой: за ним может стоять ещё пометка.
        ("house", "exact", "dadata~approx~picked", 51.2, "ул Ленина, 5", g.GRADE_APPROX),
        # Место — всегда центр пункта или массива, хвоста у него нет.
        ("place", "exact", "dadata", 51.2, "СНТ Урожай", g.GRADE_APPROX),
        ("place", "exact", "yandex", 51.2, "ЖК Дубровино, село Дубровино", g.GRADE_APPROX),
        # Инвариант нарушен: exact без точки — строке верить нельзя.
        ("house", "exact", "dadata", None, "ул Ленина, 5, Орск", None),
        # Отказ по содержанию со строкой улицы — текст; без строки — ничего.
        ("house", "house_mismatch", "dadata", None, "ул Ленина, 5, Орск", g.GRADE_TEXT),
        ("house", "house_missing", "dadata", None, "ул Ленина, 5, Орск", g.GRADE_TEXT),
        ("house", "street_mismatch", "dadata", None, None, None),
        ("house", "house_mismatch", "dadata", None, "", None),
        # Выбор из нескольких и «ещё проверяем» — не степень.
        ("house", "ambiguous", "dadata", None, "ул Ленина, 5", None),
        ("house", "elsewhere", "dadata", None, "ул Ленина, 5", None),
        ("house", "pending", "nominatim", None, None, None),
        ("house", "no_city", "none", None, None, None),
        ("house", None, None, None, None, None),
    ],
)
def test_card_grade_единственный_судья(
    kind: str,
    status: str | None,
    provider: str | None,
    lat: float | None,
    formatted: str | None,
    ожидание: str | None,
) -> None:
    lon = 58.5 if lat is not None else None
    assert g.card_grade(kind, status, provider, lat, lon, formatted) == ожидание
    # Степень точки для экрана — тот же судья: текст и «не годна» — точки нет.
    точка = g.point_precision(kind, status, provider, lat, lon)
    assert точка == (ожидание if ожидание in (g.GRADE_EXACT, g.GRADE_APPROX) else g.PRECISION_NONE)


def test_хвост_approx_читается_подстрокой_и_не_дублируется() -> None:
    assert g.point_is_approx("dadata~approx")
    assert g.point_is_approx("dadata~approx~picked")
    assert not g.point_is_approx("dadata+yandex") and not g.point_is_approx(None)
    assert g.mark_approx("dadata") == "dadata~approx"
    assert g.mark_approx("dadata~approx") == "dadata~approx"
    assert g.mark_approx("dadata~approx~picked") == "dadata~approx~picked"


def test_отказы_по_содержанию_один_набор_у_судьи_и_воркера() -> None:
    assert worker._БЕЗ_DADATA_НЕ_ПРИГОВОР is g.REFUSAL_STATUSES
    assert g.GEO_AMBIGUOUS not in g.REFUSAL_STATUSES and g.GEO_NO_CITY not in g.REFUSAL_STATUSES
    # Строка улицы под пустотой карты не пишется никогда (контракт, п. 2).
    assert g.GEO_NOT_FOUND in g.REFUSAL_STATUSES and g.GEO_NOT_FOUND not in worker._ТЕКСТ_ПОСЛЕ
    assert worker._ТЕКСТ_ПОСЛЕ < g.REFUSAL_STATUSES
    assert g.GRADE_RANK[g.GRADE_EXACT] < g.GRADE_RANK[g.GRADE_APPROX] < g.GRADE_RANK[g.GRADE_TEXT]


def test_ask_reason_единственная_причина_город() -> None:
    assert (
        g.ask_reason(geo_status="no_city", city_known=False, locality=None, variants=[]) == "city"
    )
    # Слаг есть, но не в справочнике — город известен человеку: не вопрос.
    assert g.ask_reason(geo_status="no_city", city_known=True, locality=None, variants=[]) is None
    # Клиент город назвал, по стране пусто — сказанное не переспрашиваем.
    assert (
        g.ask_reason(geo_status="no_city", city_known=False, locality="Цимлянск", variants=[])
        is None
    )
    # Назвал, а тёзок несколько — «уточните город и область».
    assert (
        g.ask_reason(
            geo_status="no_city", city_known=False, locality="Поляны", variants=[{"formatted": "x"}]
        )
        == "city"
    )
    assert (
        g.ask_reason(geo_status="not_found", city_known=False, locality=None, variants=[]) is None
    )
    assert g.ask_reason(geo_status="exact", city_known=False, locality=None, variants=[]) is None


# ── чистые помощники: семья дома, дробь ───────────────────────────────────────


def test_семья_дома_и_голова_дроби() -> None:
    assert g.house_family_match("29", "29А") and g.house_family_match("29а", "29")
    assert g.house_family_match("12", "12 к 2")
    assert not g.house_family_match("29А", "29Б"), "две буквы — два дома"
    assert not g.house_family_match("29", "30") and not g.house_family_match("29", "29")
    assert not g.house_family_match("", "29") and not g.house_family_match("А", "29")

    assert g.fraction_head(g.Parsed(street="ул Ленина", house="13/38")) == ("13", "38")
    assert g.fraction_head(g.Parsed(street="ул Ленина", house="1/2/3")) == ("1", "2/3")
    for кривой in ("13/", "/38", "13", ""):
        assert g.fraction_head(g.Parsed(street="ул Ленина", house=кривой)) is None, кривой


def test_вердикт_с_семьёй_при_точном_ключе_тот_же() -> None:
    parsed = g.Parsed(street="ул Ленина", house="29")
    ответы = [дом(house="29"), дом(house="29А", lat=51.21)]
    assert g.verdict(parsed, ОРСК, ответы, family=True) == g.verdict(parsed, ОРСК, ответы)
    # Точного ключа нет: без семьи — не тот дом, с семьёй — «29А».
    assert g.verdict(parsed, ОРСК, [дом(house="29А")])[0] == g.GEO_HOUSE_MISMATCH
    статус, hit = g.verdict(parsed, ОРСК, [дом(house="29А")], family=True)
    assert статус == g.GEO_EXACT and hit is not None and hit.house == "29А"


# ── STREET_KNOWN ───────────────────────────────────────────────────────────────


def _луговая(city: str, house: str | None, *, house_level: bool, lat: float) -> g.GeoHit:
    return g.GeoHit(
        street="ул Луговая",
        house=house,
        settlement=None,
        city=city,
        region="Псковская обл",
        lat=lat,
        lon=28.3,
        house_level=house_level,
    )


def test_known_street_улица_в_пункте_клиента_и_дом_соседа_в_городе() -> None:
    сорокино = g.Parsed(
        street="улица луговая", house="5", settlement="Сорокино", settlement_type="деревня"
    )
    улица = _луговая("Сорокино", None, house_level=False, lat=57.9)
    дом_в_пскове = _луговая("Псков", "5", house_level=True, lat=57.78)
    assert g.known_street(сорокино, ПСКОВ, [дом_в_пскове, улица]) is улица
    # Пункт клиента в ответе не назван — улица в Пскове ничего не доказывает.
    assert g.known_street(сорокино, ПСКОВ, [дом_в_пскове]) is None

    молодёжная = g.Parsed(street="ул Молодёжная", house="2")
    сосед = дом(street="ул Молодёжная", house="4")
    assert g.known_street(молодёжная, ОРСК, [сосед]) is сосед
    # Город объявления не совпал; улица шире слов клиента; другой регион.
    assert g.known_street(молодёжная, ОРСК, [dataclasses.replace(сосед, city="Гай")]) is None
    assert (
        g.known_street(молодёжная, ОРСК, [dataclasses.replace(сосед, street="ул Строителей")])
        is None
    )
    assert (
        g.known_street(молодёжная, ОРСК, [dataclasses.replace(сосед, region="Челябинская область")])
        is None
    )
    # Слаг-регион: город не сверяется.
    assert (
        g.known_street(молодёжная, City("Оренбург и область", ОРСК.region, ОРСК.tz), [сосед])
        is сосед
    )
    # Клиент назвал город — он обязан быть в ответе.
    assert g.known_street(dataclasses.replace(молодёжная, locality="Гай"), ОРСК, [сосед]) is None
    assert g.known_street(dataclasses.replace(молодёжная, locality="Орск"), ОРСК, [сосед]) is сосед


def test_known_street_не_адрес_не_проходит() -> None:
    """Замер 18.09: «инвалид, 2», «ГГц, 6» — уровни A/B без улицы у карты."""
    город = g.GeoHit(
        street=None,
        house=None,
        settlement=None,
        city="Орск",
        region="Оренбургская область",
        lat=51.2,
        lon=58.5,
        house_level=False,
    )
    assert g.known_street(g.Parsed(street="инвалид", house="2"), ОРСК, [город]) is None
    assert (
        g.known_street(g.Parsed(street="ГГц", house="6"), ОРСК, [дом(street="ул Ленина")]) is None
    )
    assert g.known_street(g.Parsed(street="", house="6"), ОРСК, [дом()]) is None
    assert g.known_street(g.Parsed(street="ул Ленина", house="6"), None, [дом()]) is None


def _молодёжная_самара(settlement: str | None, *, lat: float, house: str | None = None) -> g.GeoHit:
    return g.GeoHit(
        street="ул Молодёжная",
        house=house,
        settlement=settlement,
        city="Самара",
        region="Самарская обл",
        lat=lat,
        lon=50.1 + (lat - 53.19),
        house_level=house is not None,
        precise=house is not None,
    )


#: Две догадки DaData уровня 7 на «Молодёжная 2» в Самаре: улица в посёлке
#: Красная Глинка (25 км от центра) и улица в самом городе (ревью 19.09).
КРАСНАЯ_ГЛИНКА = _молодёжная_самара("Красная Глинка", lat=53.38)
САМАРА_БЕЗ_ПУНКТА = _молодёжная_самара(None, lat=53.19)
ЗУБЧАНИНОВКА = _молодёжная_самара("Зубчаниновка", lat=53.26)


def test_known_street_одноимённые_улицы_в_пунктах_города_решаются_как_дома() -> None:
    """Одноимённые улицы в разных пунктах одного города — та же неоднозначность,
    что у домов (`verdict` → `ambiguous`): не первая по порядку карты, а
    единственная в самом городе, когда клиент пункта не называл; два посёлка
    — None. Порядок списка результата не меняет."""
    parsed = g.Parsed(street="Молодёжная", house="2")
    # Посёлок + сам город, пункта у клиента нет → сам город (как `_best_of` на домах).
    assert g.known_street(parsed, САМАРА_ГОРОД, [КРАСНАЯ_ГЛИНКА, САМАРА_БЕЗ_ПУНКТА]) is (
        САМАРА_БЕЗ_ПУНКТА
    )
    assert g.known_street(parsed, САМАРА_ГОРОД, [САМАРА_БЕЗ_ПУНКТА, КРАСНАЯ_ГЛИНКА]) is (
        САМАРА_БЕЗ_ПУНКТА
    )
    # Тот же ответ у домов на тех же улицах: вердикт — выбор, `_best_of` — сам город.
    дома = [
        dataclasses.replace(КРАСНАЯ_ГЛИНКА, house="2", house_level=True, precise=True),
        dataclasses.replace(САМАРА_БЕЗ_ПУНКТА, house="2", house_level=True, precise=True),
    ]
    assert g.verdict(parsed, САМАРА_ГОРОД, дома) == (g.GEO_AMBIGUOUS, None)
    решение = g.auto_decide(_evidence(city=САМАРА_ГОРОД, parsed=parsed, hits=дома))
    assert решение is not None and решение.hit is дома[1]
    # Два посёлка — ни один не в самом городе → None, в любом порядке.
    assert g.known_street(parsed, САМАРА_ГОРОД, [КРАСНАЯ_ГЛИНКА, ЗУБЧАНИНОВКА]) is None
    assert g.known_street(parsed, САМАРА_ГОРОД, [ЗУБЧАНИНОВКА, КРАСНАЯ_ГЛИНКА]) is None
    # Клиент назвал пункт — улица в нём одна, посёлок-сосед не мешает.
    в_глинке = dataclasses.replace(parsed, settlement="Красная Глинка", settlement_type="п")
    assert g.known_street(в_глинке, САМАРА_ГОРОД, [ЗУБЧАНИНОВКА, КРАСНАЯ_ГЛИНКА]) is (
        КРАСНАЯ_ГЛИНКА
    )
    # Слаг-регион: «сам город» не определить → None.
    регион = City("Самара и область", САМАРА_ГОРОД.region, САМАРА_ГОРОД.tz)
    assert g.known_street(parsed, регион, [КРАСНАЯ_ГЛИНКА, САМАРА_БЕЗ_ПУНКТА]) is None


def test_known_street_одна_улица_под_разными_именами_карт_не_делится() -> None:
    """Диверсии на регресс: одна и та же улица, записанная картами по-разному,
    остаётся одной группой — иначе сторож глушил бы честные улицы."""
    parsed = g.Parsed(street="ул Молодёжная", house="2")
    # DaData: улица с микрорайоном и без — один объект в 300 м.
    с_мкр = _молодёжная_самара("Металлург", lat=53.193)
    assert g.known_street(parsed, САМАРА_ГОРОД, [с_мкр, САМАРА_БЕЗ_ПУНКТА]) is с_мкр
    assert g.known_street(parsed, САМАРА_ГОРОД, [САМАРА_БЕЗ_ПУНКТА, с_мкр]) is САМАРА_БЕЗ_ПУНКТА
    # Тот же микрорайон, но за 25 км — уже другая улица (порог `_ОДНА_УЛИЦА_KM`).
    assert (
        g.known_street(parsed, САМАРА_ГОРОД, [dataclasses.replace(с_мкр, lat=53.38)]) is not None
    ), "одна группа с пунктом — улица (пункта клиент не называл, но она одна)"
    assert (
        g.known_street(
            parsed, САМАРА_ГОРОД, [dataclasses.replace(с_мкр, lat=53.38), САМАРА_БЕЗ_ПУНКТА]
        )
        is САМАРА_БЕЗ_ПУНКТА
    )
    # DaData без пункта + OSM с районом города (`settlement_kind="district"`).
    район_osm = dataclasses.replace(
        САМАРА_БЕЗ_ПУНКТА, settlement="Кировский район", settlement_kind="district", lat=53.24
    )
    assert g.known_street(parsed, САМАРА_ГОРОД, [САМАРА_БЕЗ_ПУНКТА, район_osm]) is (
        САМАРА_БЕЗ_ПУНКТА
    )
    assert g.known_street(parsed, САМАРА_ГОРОД, [район_osm]) is район_osm
    # «ул Молодёжная» DaData + «Молодёжная улица» Яндекса — слова типа не имя.
    яндекс = dataclasses.replace(САМАРА_БЕЗ_ПУНКТА, street="Молодёжная улица", lat=53.2)
    assert g.known_street(parsed, САМАРА_ГОРОД, [САМАРА_БЕЗ_ПУНКТА, яндекс]) is САМАРА_БЕЗ_ПУНКТА
    # Дом соседа и улица без дома — одна улица; первый ответ — представитель.
    сосед = _молодёжная_самара(None, lat=53.191, house="4")
    assert g.known_street(parsed, САМАРА_ГОРОД, [сосед, САМАРА_БЕЗ_ПУНКТА]) is сосед


# ── страна ─────────────────────────────────────────────────────────────────────

КЕДРОВСКАЯ_РЯЗАНЬ = дом(
    street="ул Кедровская", house="4", city="Рязань", region="Рязанская обл", lat=54.63, lon=39.69
)
КОЛХОЗНАЯ_ПОЛЯНЫ_РЯЗАНЬ = дом(
    street="ул Колхозная",
    house="4",
    settlement="Поляны",
    city=None,
    region="Рязанская обл",
    lat=54.7,
    lon=39.6,
)
КОЛХОЗНАЯ_ПОЛЯНЫ_КИРОВ = дом(
    street="ул Колхозная",
    house="4",
    settlement="Поляны",
    city=None,
    region="Кировская обл",
    lat=58.6,
    lon=49.6,
)


def test_страна_запрос_без_области_и_вердикт_только_по_единственному_точному_дому() -> None:
    parsed = g.Parsed(street="ул Кедровская", house="4")
    assert g.build_country_query(parsed) == g.Query(
        region=None, city=None, settlement=None, street="ул Кедровская", house="4"
    )
    # Город назвал клиент — в `locations`; пункт — с типом массива, как у `build_query`.
    assert (
        g.build_country_query(dataclasses.replace(parsed, locality="Цимлянск")).city == "Цимлянск"
    )
    assert (
        g.build_country_query(
            g.Parsed(street="", house="47", settlement="Солнечный", settlement_type="СНТ")
        )
        is None
    ), "без улицы и массива голый номер по стране — шум"
    снт = g.Parsed(street="ул Садовая", house="47", settlement="Солнечный", settlement_type="СНТ")
    assert g.build_country_query(снт).settlement == "СНТ Солнечный"
    assert g.build_country_query(g.Parsed(street="ул Кедровская", house="")) is None

    assert g.country_verdict(parsed, [КЕДРОВСКАЯ_РЯЗАНЬ]) == (g.GEO_EXACT, КЕДРОВСКАЯ_РЯЗАНЬ)
    # Точка улицы (`precise=False`) по стране единственной не считается.
    assert g.country_verdict(parsed, [dataclasses.replace(КЕДРОВСКАЯ_РЯЗАНЬ, precise=False)]) == (
        g.GEO_NOT_FOUND,
        None,
    )
    assert g.country_verdict(parsed, [])[0] == g.GEO_NOT_FOUND
    # Клиент назвал город — дом обязан быть в нём.
    assert (
        g.country_verdict(dataclasses.replace(parsed, locality="Рязань"), [КЕДРОВСКАЯ_РЯЗАНЬ])[0]
        == g.GEO_EXACT
    )
    assert (
        g.country_verdict(dataclasses.replace(parsed, locality="Касимов"), [КЕДРОВСКАЯ_РЯЗАНЬ])[0]
        == g.GEO_NOT_FOUND
    )
    # Две области — выбор, и в строке варианта область: иначе они неразличимы.
    поляны = g.Parsed(street="ул Колхозная", house="4", settlement="Поляны", settlement_type="с")
    ответы = [КОЛХОЗНАЯ_ПОЛЯНЫ_РЯЗАНЬ, КОЛХОЗНАЯ_ПОЛЯНЫ_КИРОВ]
    assert g.country_verdict(поляны, ответы) == (g.GEO_AMBIGUOUS, None)
    варианты = g.country_variants(поляны, ответы)
    assert [v["formatted"] for v in варианты] == [
        "ул Колхозная, 4, с Поляны, Рязанская обл",
        "ул Колхозная, 4, с Поляны, Кировская обл",
    ]
    assert [v["region"] for v in варианты] == ["Рязанская обл", "Кировская обл"]


def _центральная_15(
    settlement: str | None, city: str | None, region: str, *, lat: float, lon: float, precise: bool
) -> g.GeoHit:
    return дом(
        street="ул Центральная",
        house="15",
        settlement=settlement,
        city=city,
        region=region,
        lat=lat,
        lon=lon,
        precise=precise,
    )


#: «ул Центральная, 15» по стране (ревью 19.09): две деревни-тёзки с точкой
#: пункта (qc_geo 3, у ФИАС координат домов в деревнях нет) и точный дом в
#: городе — три разных адреса в трёх местах.
ИВАНОВКА_ОРЕНБУРГ = _центральная_15(
    "Ивановка", None, "Оренбургская обл", lat=52.0, lon=55.0, precise=False
)
ИВАНОВКА_САМАРА = _центральная_15(
    "Ивановка", None, "Самарская обл", lat=53.0, lon=50.0, precise=False
)
БУЗУЛУК = _центральная_15(None, "Бузулук", "Оренбургская обл", lat=52.78, lon=52.26, precise=True)


def test_страна_неточные_тёзки_считаются_соперниками_а_точность_решает_только_exact() -> None:
    """Единственность по стране считается по ВСЕМ домам-тёзкам, а не только по
    точным: иначе один точный дом среди деревень с точкой пункта — ложный
    `exact`, и карточка получала бы Бузулук без человека."""
    parsed = g.Parsed(street="ул Центральная", house="15")
    ответы = [ИВАНОВКА_ОРЕНБУРГ, ИВАНОВКА_САМАРА, БУЗУЛУК]
    assert g.country_verdict(parsed, ответы) == (g.GEO_AMBIGUOUS, None)
    варианты = g.country_variants(parsed, ответы)
    # Все три, с областью; точный — первым (представитель группы — точный).
    assert [v["formatted"] for v in варианты] == [
        "ул Центральная, 15, Бузулук, Оренбургская обл",
        "ул Центральная, 15, Ивановка, Оренбургская обл",
        "ул Центральная, 15, Ивановка, Самарская обл",
    ]
    # Один точный без тёзок — по-прежнему `exact`; одна неточная группа — `not_found`.
    assert g.country_verdict(parsed, [БУЗУЛУК]) == (g.GEO_EXACT, БУЗУЛУК)
    assert g.country_verdict(parsed, [ИВАНОВКА_ОРЕНБУРГ]) == (g.GEO_NOT_FOUND, None)
    assert g.country_verdict(parsed, [ИВАНОВКА_ОРЕНБУРГ, ИВАНОВКА_САМАРА]) == (
        g.GEO_AMBIGUOUS,
        None,
    )
    # Диверсия против порядка представителя: неточный дубль того же дома
    # (точка пункта) и точный в одном пункте, неточный первым — группа одна,
    # представитель точный → `exact` с точным домом.
    точная = dataclasses.replace(ИВАНОВКА_ОРЕНБУРГ, lat=52.004, lon=55.006, precise=True)
    assert g.country_verdict(parsed, [ИВАНОВКА_ОРЕНБУРГ, точная]) == (g.GEO_EXACT, точная)
    assert g.country_verdict(parsed, [точная, ИВАНОВКА_ОРЕНБУРГ]) == (g.GEO_EXACT, точная)


def test_страна_две_ивановки_в_одной_области_разные_адреса() -> None:
    """Ключ адреса района не знает: «д. Ивановка, ул Центральная, 15» в двух
    районах Оренбургской области — один ключ, точки за 100 км. Дальше
    `_ОДНО_МЕСТО_KM` — разные адреса (выбор оператора), ближе — один дом."""
    parsed = g.Parsed(street="ул Центральная", house="15")
    первая = dataclasses.replace(ИВАНОВКА_ОРЕНБУРГ, precise=True)
    вторая = dataclasses.replace(первая, lat=51.5, lon=56.5)
    assert g.country_verdict(parsed, [первая, вторая]) == (g.GEO_AMBIGUOUS, None)
    # Строки вариантов одинаковы — район в `GeoHit` со шлюза не приезжает
    # (известный предел): оператора различают точки, а не текст.
    assert [v["formatted"] for v in g.country_variants(parsed, [первая, вторая])] == [
        "ул Центральная, 15, Ивановка, Оренбургская обл",
        "ул Центральная, 15, Ивановка, Оренбургская обл",
    ]
    assert [(v["lat"], v["lon"]) for v in g.country_variants(parsed, [первая, вторая])] == [
        (52.0, 55.0),
        (51.5, 56.5),
    ]
    # Тот же дом двумя строками ФИАС в 500 м — один адрес.
    рядом = dataclasses.replace(первая, lat=52.0045)
    assert g.country_verdict(parsed, [первая, рядом]) == (g.GEO_EXACT, первая)
    # Микрорайон в одном ответе назван, в другом нет, точки в 50 м — один дом.
    с_мкр = dataclasses.replace(БУЗУЛУК, settlement="Северный", lat=52.7803)
    assert g.country_verdict(parsed, [БУЗУЛУК, с_мкр]) == (g.GEO_EXACT, БУЗУЛУК)


# ── место у второй карты ───────────────────────────────────────────────────────

ДУБРОВИНО_ЯНДЕКС = g.GeoHit(
    street=None,
    house=None,
    settlement="ЖК Дубровино",
    city="село Дубровино",
    region="Москва",
    lat=55.506317,
    lon=37.50375,
    house_level=False,
    settlement_kind="district",
)
#: Хвост ответа Яндекса на «Москва, ЖК Дубровино» — мусор вплоть до другой страны.
МУСОР_ЯНДЕКСА = [
    g.GeoHit(
        street=None,
        house=None,
        settlement="Дубровино",
        city="Подольск",
        region="Тверская область",
        lat=56.8,
        lon=35.9,
        house_level=False,
    ),
    g.GeoHit(
        street=None,
        house=None,
        settlement=None,
        city="Ташкент",
        region="Ташкентская область",
        lat=41.3,
        lon=69.2,
        house_level=False,
    ),
]


def _место(текст: str, *, locality: str | None = None) -> g.Place:
    found = address_parse.parse_place(текст)
    assert found is not None, текст
    return g.Place(
        settlement=found.settlement,
        settlement_type=found.settlement_type,
        area=found.area,
        district=None,
        level=found.level,
        street="",
        locality=locality,
    )


def test_место_у_яндекса_сторожа_региона_и_слов_места() -> None:
    место = _место("ЖК Дубровино")
    assert место.area == "ЖК Дубровино" and место.settlement is None
    hit = g.yandex_place_hit(место, МОСКВА, [ДУБРОВИНО_ЯНДЕКС, *МУСОР_ЯНДЕКСА])
    assert hit is not None and (hit.lat, hit.lon) == (55.506317, 37.50375)
    assert hit.kind == "area" and hit.area == "ЖК Дубровино" and hit.settlement is None
    assert g.format_place(hit, место) == "ЖК Дубровино, село Дубровино"
    # Степень места по контракту — приблизительно, хвоста провайдера не нужно.
    assert (
        g.card_grade("place", "exact", "yandex", hit.lat, hit.lon, "ЖК Дубровино") == g.GRADE_APPROX
    )
    # Диверсии: только мусор → None; регион не тот; слова места не те; город клиента не назван.
    assert g.yandex_place_hit(место, МОСКВА, МУСОР_ЯНДЕКСА) is None
    assert (
        g.yandex_place_hit(
            место, City("Тверь", "Тверская область", "Europe/Moscow"), [ДУБРОВИНО_ЯНДЕКС]
        )
        is None
    )
    assert g.yandex_place_hit(_место("ЖК Дубки"), МОСКВА, [ДУБРОВИНО_ЯНДЕКС]) is None
    assert (
        g.yandex_place_hit(_место("ЖК Дубровино", locality="Подольск"), МОСКВА, [ДУБРОВИНО_ЯНДЕКС])
        is None
    )
    assert g.yandex_place_hit(место, None, [ДУБРОВИНО_ЯНДЕКС]) is None


def test_место_у_яндекса_названный_клиентом_город_объявления_не_гасит_спутник() -> None:
    """«Москва, ЖК Дубровино» (ревью 19.09): у пункта-спутника слова «Москва»
    в settlement/city ответа нет (`city='село Дубровино'`), и сторож слов
    города глушил случай владельца. Город объявления или регион ответа,
    названный клиентом, проходит; чужой город («Подольск») — нет."""
    с_москвой = _место("ЖК Дубровино", locality="Москва")
    assert с_москвой.locality == "Москва"
    hit = g.yandex_place_hit(с_москвой, МОСКВА, [*МУСОР_ЯНДЕКСА, ДУБРОВИНО_ЯНДЕКС])
    assert hit is not None and (hit.lat, hit.lon) == (55.506317, 37.50375)
    # Объявление в Подольске (область), клиент назвал Москву — регион ответа.
    подольск = City("Подольск", "Московская область", "Europe/Moscow")
    hit = g.yandex_place_hit(с_москвой, подольск, [ДУБРОВИНО_ЯНДЕКС])
    assert hit is not None and hit.area == "ЖК Дубровино"
    # Объявление в Подольске, клиент назвал Подольск — город объявления, он у
    # места не сверяется (спутник в черте города не лежит): проходит, как и
    # без слов о городе. Чужой город («Химки») — ни словами, ни регионом → None.
    assert (
        g.yandex_place_hit(
            _место("ЖК Дубровино", locality="Подольск"), подольск, [ДУБРОВИНО_ЯНДЕКС]
        )
        is not None
    )
    assert (
        g.yandex_place_hit(_место("ЖК Дубровино", locality="Химки"), подольск, [ДУБРОВИНО_ЯНДЕКС])
        is None
    )
    # Город клиента, совпадающий с областью лишь основой («Самара» против
    # «Самарская область»), регионом не считается; но город объявления,
    # названный клиентом, равносилен неназванному — спутник в черте города
    # не лежит. Чужой город («Сызрань») — по-прежнему None.
    самара = City("Самара", "Самарская область", "Europe/Samara")
    южный = dataclasses.replace(
        ДУБРОВИНО_ЯНДЕКС, settlement="ЖК Южный город", city="Лопатино", region="Самарская область"
    )
    assert (
        g.yandex_place_hit(_место("ЖК Южный город", locality="Лопатино"), самара, [южный])
        is not None
    )
    assert (
        g.yandex_place_hit(_место("ЖК Южный город", locality="Самара"), самара, [южный]) is not None
    )
    assert g.yandex_place_hit(_место("ЖК Южный город", locality="Сызрань"), самара, [южный]) is None
    assert g.region_matches("Самара", "Самарская область") is False


# ── auto_decide: (1) один в радиусе ────────────────────────────────────────────


def test_only_in_radius_один_из_двух_в_40_км() -> None:
    e = _evidence(
        status=g.GEO_ELSEWHERE,
        city=САМАРА_ГОРОД,
        region_hits=[СМЫШЛЯЕВКА, СЫЗРАНЬ],
        variants=[{"formatted": "x"}, {"formatted": "y"}],
        city_point=САМАРА,
    )
    решение = g.auto_decide(e)
    assert решение is not None and решение.rule == g.RULE_ONLY_IN_RADIUS
    assert решение.hit is СМЫШЛЯЕВКА and решение.approx
    # Диверсии: два в радиусе — монета; точки города нет; оба дальше 40 км;
    # клиент назвал другой пункт; чужой город под расстояние не попадает.
    assert g.auto_decide(dataclasses.replace(e, region_hits=[СМЫШЛЯЕВКА, КИНЕЛЬ])) is None
    assert g.auto_decide(dataclasses.replace(e, city_point=None)) is None
    assert g.auto_decide(dataclasses.replace(e, region_hits=[ДАЛЬНИЙ, СЫЗРАНЬ])) is None
    assert (
        g.auto_decide(
            dataclasses.replace(
                e,
                parsed=g.Parsed(
                    street="ул Ленина", house="5", settlement="Курумоч", settlement_type="д"
                ),
            )
        )
        is None
    )
    assert (
        g.auto_decide(
            dataclasses.replace(
                e,
                status=g.GEO_OTHER_CITY,
                parsed=g.Parsed(street="ул Ленина", house="5", locality="Сызрань"),
            )
        )
        is None
    )
    # Одиночку решает пригород воркера, не правило.
    assert g.auto_decide(dataclasses.replace(e, region_hits=[СМЫШЛЯЕВКА])) is None


# ── auto_decide: (2) неоднозначность ───────────────────────────────────────────


def test_fullest_street_красная_керчь_против_красной() -> None:
    parsed = g.Parsed(street="Красная Керчь", house="5")
    красная = дом(street="Красная улица", lat=51.20)
    керчь = дом(street="улица Красная Керчь", lat=51.25)
    assert g.verdict(parsed, ОРСК, [красная, керчь])[0] == g.GEO_AMBIGUOUS
    решение = g.auto_decide(_evidence(parsed=parsed, hits=[красная, керчь]))
    assert решение is not None and (решение.rule, решение.hit) == (g.RULE_FULLEST_STREET, керчь)
    assert решение.approx
    # Одно слово у клиента: «Победы» и «30-летия Победы» им не различить.
    победы = g.Parsed(street="Победы", house="56")
    ответы = [
        дом(street="улица Победы", house="56", lat=51.20),
        дом(street="улица 30-летия Победы", house="56", lat=51.25),
    ]
    assert g.verdict(победы, ОРСК, ответы)[0] == g.GEO_AMBIGUOUS
    assert g.auto_decide(_evidence(parsed=победы, hits=ответы)) is None
    # Две полные — решения нет.
    assert (
        g.auto_decide(_evidence(parsed=parsed, hits=[керчь, dataclasses.replace(керчь, lat=51.3)]))
        is None
    )


def test_one_spot_дубли_гар_в_150_метрах_только_точные() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    a = дом(settlement="Заречный", lat=51.2000)
    b = дом(settlement="Крыловка", lat=51.2012)  # ≈130 м
    assert len(g.distinct_addresses([a, b])) == 2
    решение = g.auto_decide(_evidence(parsed=parsed, hits=[a, b]))
    assert решение is not None and решение.rule == g.RULE_ONE_SPOT and решение.approx
    # Диверсии: точка улицы сближает разные дома — не считается; 700 м — не пятно.
    assert (
        g.auto_decide(_evidence(parsed=parsed, hits=[a, dataclasses.replace(b, precise=False)]))
        is None
    )
    assert (
        g.auto_decide(_evidence(parsed=parsed, hits=[a, dataclasses.replace(b, lat=51.2063)]))
        is None
    )


def test_best_of_подсказка_из_соседней_реплики_и_дом_в_самом_городе() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    в_городе = дом(settlement=None)
    заречный = дом(settlement="Заречный", lat=51.3)
    крыловка = дом(settlement="Крыловка", lat=51.4)
    # Подсказка называет ровно один пункт — он и записывается в разбор.
    решение = g.auto_decide(_evidence(parsed=parsed, hits=[заречный, крыловка], hints=["Заречный"]))
    assert решение is not None and решение.rule == g.RULE_BEST_OF and решение.hit is заречный
    assert решение.parsed.settlement == "Заречный" and решение.approx
    # Подсказки нет, пункт не назван — единственный дом в самом городе.
    решение = g.auto_decide(_evidence(parsed=parsed, hits=[в_городе, заречный]))
    assert решение is not None and решение.rule == g.RULE_BEST_OF and решение.hit is в_городе
    # Диверсии: подсказка под оба; два дома в городе; ни признака — точность и близость не решают.
    assert (
        g.auto_decide(
            _evidence(parsed=parsed, hits=[заречный, крыловка], hints=["Заречный", "Крыловка"])
        )
        is None
    )
    assert (
        g.auto_decide(
            _evidence(parsed=parsed, hits=[в_городе, дом(street="пер Ленина", lat=51.25)])
        )
        is None
    )
    assert (
        g.auto_decide(_evidence(parsed=parsed, hits=[заречный, крыловка], city_point=(51.3, 58.5)))
        is None
    )
    # Клиент пункт назвал (карта его не нашла, вердикт мягкий) — дом в городе не годится.
    assert (
        g.auto_decide(
            _evidence(
                parsed=dataclasses.replace(parsed, settlement="Кошелев", settlement_type="мкр"),
                hits=[в_городе, заречный],
            )
        )
        is None
    )


def test_in_named_city_решают_правила_неоднозначности_без_расстояний() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5", locality="Цимлянск")
    центр = дом(city="Цимлянск", region="Ростовская область", lat=47.65, lon=42.1)
    яр = дом(
        city="Цимлянск", settlement="Красный Яр", region="Ростовская область", lat=47.7, lon=42.2
    )
    e = _evidence(
        status=g.GEO_OTHER_CITY,
        parsed=parsed,
        city=ВОЛГОДОНСК,
        region_hits=[центр, яр],
        variants=[{"formatted": "a"}, {"formatted": "b"}],
        hints=["Красный Яр"],
    )
    решение = g.auto_decide(e)
    assert решение is not None and решение.rule == g.RULE_BEST_OF and решение.hit is яр
    # Без подсказки — дом «в самом городе» чужого города не решает: город объявления другой.
    assert g.auto_decide(dataclasses.replace(e, hints=[])) is None
    # Пункт с типом не найден, дом в городе один — сторож пункта отсеивает.
    assert (
        g.auto_decide(
            dataclasses.replace(
                e,
                parsed=dataclasses.replace(
                    parsed, settlement="Приморский", settlement_type="посёлок"
                ),
                region_hits=[центр],
                hints=[],
            )
        )
        is None
    )


# ── auto_decide: (3) дробь, (8) семья, (3) улица, (7) массив ───────────────────

ДОМ_13 = дом(house="13")


def test_fraction_head_из_ответов_по_городу_без_сети() -> None:
    parsed = g.Parsed(street="ул Ленина", house="13/38")
    assert g.verdict(parsed, ОРСК, [ДОМ_13])[0] == g.GEO_HOUSE_MISMATCH
    решение = g.auto_decide(
        _evidence(
            status=g.GEO_HOUSE_MISMATCH,
            parsed=parsed,
            hits=[ДОМ_13],
            city_hits=[([ДОМ_13], "dadata")],
        )
    )
    assert решение is not None and решение.rule == g.RULE_FRACTION_HEAD
    assert (решение.hit.house, решение.office, решение.provider, решение.approx) == (
        "13/38",
        "38",
        "dadata",
        True,
    )
    # Из сетевого ответа воркера, когда по городу дома «13» не было.
    решение = g.auto_decide(
        _evidence(
            status=g.GEO_NOT_FOUND,
            parsed=parsed,
            city_hits=[([], "dadata"), ([], "nominatim")],
            fraction_hits=([ДОМ_13], "dadata"),
        )
    )
    assert (
        решение is not None
        and решение.rule == g.RULE_FRACTION_HEAD
        and решение.provider == "dadata"
    )
    # Хвост меньше 10 — не квартира; «1/2/3» не падает; дома «N» нет — None.
    решение = g.auto_decide(
        _evidence(
            status=g.GEO_HOUSE_MISMATCH,
            parsed=dataclasses.replace(parsed, house="185/4"),
            city_hits=[([дом(house="185")], "dadata")],
        )
    )
    assert решение is not None and решение.office is None and решение.hit.house == "185/4"
    решение = g.auto_decide(
        _evidence(
            status=g.GEO_HOUSE_MISMATCH,
            parsed=dataclasses.replace(parsed, house="1/2/3"),
            city_hits=[([дом(house="1")], "dadata")],
        )
    )
    assert решение is not None and решение.office is None
    assert (
        g.auto_decide(
            _evidence(
                status=g.GEO_NOT_FOUND,
                parsed=parsed,
                city_hits=[([], "dadata")],
                fraction_hits=([], "dadata"),
            )
        )
        is None
    )


def test_house_family_29_против_29а_с_пометкой() -> None:
    parsed = g.Parsed(street="ул Ленина", house="29")
    решение = g.auto_decide(
        _evidence(
            status=g.GEO_HOUSE_MISMATCH,
            parsed=parsed,
            hits=[],
            city_hits=[([], "nominatim"), ([дом(house="29А")], "dadata")],
        )
    )
    assert решение is not None and решение.rule == g.RULE_HOUSE_FAMILY
    assert (решение.hit.house, решение.provider, решение.approx) == ("29А", "dadata", True)
    # Семья из двух («29А» и «29Б») — выбор, не решение; не тот статус — молчит.
    семья = [дом(house="29А"), дом(house="29Б", lat=51.25)]
    assert (
        g.auto_decide(
            _evidence(status=g.GEO_HOUSE_MISMATCH, parsed=parsed, city_hits=[(семья, "dadata")])
        )
        is None
    )
    assert (
        g.auto_decide(
            _evidence(
                status=g.GEO_HOUSE_MISSING,
                parsed=parsed,
                city_hits=[([дом(house="29А")], "dadata")],
            )
        )
        is None
    )


def test_street_point_из_улицы_dadata_при_пустом_osm() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    улица = дом(house=None, house_level=False, precise=False)
    e = _evidence(
        status=g.GEO_NOT_FOUND, parsed=parsed, city_hits=[([улица], "dadata"), ([], "nominatim")]
    )
    решение = g.auto_decide(e)
    assert (
        решение is not None and решение.rule == g.RULE_STREET_POINT and решение.provider == "dadata"
    )
    assert (решение.hit.house, решение.hit.house_level, решение.hit.precise, решение.approx) == (
        "5",
        False,
        False,
        False,
    )
    # Улица из ответов области — с именем карты области.
    решение = g.auto_decide(
        dataclasses.replace(
            e, city_hits=[([], "nominatim")], region_hits=[улица], region_provider="yandex"
        )
    )
    assert решение is not None and решение.provider == "yandex"
    # Диверсии: дом соседа — не точка улицы (только строка без точки); не тот статус.
    assert g.auto_decide(dataclasses.replace(e, city_hits=[([дом(house="7")], "dadata")])) is None
    assert g.auto_decide(dataclasses.replace(e, status=g.GEO_HOUSE_MISMATCH)) is None
    # «Улица не та» — с N24 (19.09) та же улика, но только уровням A/B и без
    # спора города об улице («пр-кт Ленина, 5» в городе при «ул Ленина 5»).
    assert (
        g.auto_decide(
            dataclasses.replace(
                e, status=g.GEO_STREET_MISMATCH, parsed=dataclasses.replace(parsed, level="C")
            )
        )
        is None
    )
    assert (
        g.auto_decide(
            dataclasses.replace(
                e,
                status=g.GEO_STREET_MISMATCH,
                city_hits=[
                    ([улица], "dadata"),
                    ([дом(street="пр-кт Ленина", house="5")], "nominatim"),
                ],
            )
        )
        is None
    )


def test_точка_улицы_и_массива_только_уровням_текста() -> None:
    """Один сторож уровня на текст и точку (ревью 19.09): улика «карта знает
    улицу, дома нет» неводу C не годится ни строкой, ни точкой — иначе
    «балконе, 4» ложилось бы в карточку точкой «ул Балконная». Дом уровня C
    в карточку — только `exact` вердикта (карта подтвердила сам дом)."""
    assert g.STREET_EVIDENCE_LEVELS == frozenset({address_parse.LEVEL_A, address_parse.LEVEL_B})
    улица = дом(street="ул Балконная", house=None, house_level=False, precise=False)
    for уровень, ожидание in (("A", g.RULE_STREET_POINT), ("B", g.RULE_STREET_POINT), ("C", None)):
        e = _evidence(
            status=g.GEO_HOUSE_MISSING,
            parsed=g.Parsed(street="балконе", house="4", level=уровень),
            city_hits=[([улица], "dadata")],
        )
        решение = g.auto_decide(e)
        assert (решение.rule if решение else None) == ожидание, уровень
    # Точка массива — тот же сторож.
    снт = g.Parsed(
        street="СНТ Солнечный", house="47", settlement="Солнечный", settlement_type="СНТ"
    )
    место = g.Place(settlement=None, settlement_type=None, area="СНТ Солнечный", district=None)
    for уровень, ожидание in (("A", g.RULE_AREA_POINT), ("C", None)):
        e = _evidence(
            status=g.GEO_NOT_FOUND,
            parsed=dataclasses.replace(снт, level=уровень),
            place=место,
            place_hits=[_массив("СНТ Солнечный")],
        )
        решение = g.auto_decide(e)
        assert (решение.rule if решение else None) == ожидание, уровень
    # Уровень C не мешает правилам про настоящие дома карты: семья дома.
    e = _evidence(
        status=g.GEO_HOUSE_MISMATCH,
        parsed=g.Parsed(street="Ленина", house="29", level="C"),
        city_hits=[([дом(house="29А")], "dadata")],
    )
    решение = g.auto_decide(e)
    assert решение is not None and решение.rule == g.RULE_HOUSE_FAMILY


def test_точка_улицы_судит_улицы_всех_карт_разом() -> None:
    """Две догадки DaData в разных посёлках города — неоднозначность; слабая
    карта с единственной догадкой в одном из них её не обходит: улицы всех
    карт судятся одним `known_street`. Имя карты — у выбранного ответа."""
    parsed = g.Parsed(street="Молодёжная", house="2", level="B")
    e = _evidence(
        status=g.GEO_HOUSE_MISSING,
        parsed=parsed,
        city=САМАРА_ГОРОД,
        city_hits=[([КРАСНАЯ_ГЛИНКА, ЗУБЧАНИНОВКА], "dadata"), ([], "nominatim")],
    )
    assert g.auto_decide(e) is None
    osm_глинка = dataclasses.replace(КРАСНАЯ_ГЛИНКА, lat=53.381, settlement_kind="place")
    assert (
        g.auto_decide(
            dataclasses.replace(e, city_hits=[*e.city_hits[:1], ([osm_глинка], "nominatim")])
        )
        is None
    )
    assert (
        g.auto_decide(dataclasses.replace(e, region_hits=[osm_глинка], region_provider="yandex"))
        is None
    )
    # Посёлок + сам город → сам город, и карта — та, что дала выбранный ответ.
    решение = g.auto_decide(
        dataclasses.replace(
            e, city_hits=[([КРАСНАЯ_ГЛИНКА], "dadata"), ([САМАРА_БЕЗ_ПУНКТА], "nominatim")]
        )
    )
    assert решение is not None and (решение.rule, решение.provider) == (
        g.RULE_STREET_POINT,
        "nominatim",
    )
    assert (решение.hit.settlement, решение.hit.house, решение.hit.lat) == (None, "2", 53.19)


def _массив(name: str, *, kind: str = "area", settlement: str | None = None) -> g.PlaceHit:
    return g.PlaceHit(
        name=name,
        kind=kind,
        settlement=settlement,
        area=name if kind == "area" else None,
        city=None,
        district="Оренбургский р-н",
        region="Оренбургская область",
        lat=51.6,
        lon=55.2,
    )


def test_area_point_участок_массива_даёт_точку_массива() -> None:
    parsed = g.Parsed(street="СНТ Урожай", house="273", settlement="Урожай", settlement_type="СНТ")
    место = g.Place(
        settlement=None, settlement_type=None, area="СНТ Урожай", district=None, street=""
    )
    e = _evidence(
        status=g.GEO_HOUSE_MISSING, parsed=parsed, place=место, place_hits=[_массив("СНТ Урожай")]
    )
    решение = g.auto_decide(e)
    assert (
        решение is not None and решение.rule == g.RULE_AREA_POINT and решение.provider == "dadata"
    )
    assert (решение.hit.house, решение.hit.area, решение.hit.house_level, решение.hit.precise) == (
        "273",
        "СНТ Урожай",
        False,
        False,
    )
    assert g.format_address(решение.hit, parsed) == "СНТ Урожай, 273"
    # Посёлок-тёзка рядом с массивом — точка массива; только посёлок — не массив, None.
    солнечный = g.Place(
        settlement=None, settlement_type=None, area="СНТ Солнечный", district=None, street=""
    )
    p = dataclasses.replace(parsed, street="СНТ Солнечный", house="47", settlement="Солнечный")
    оба = [
        _массив("Солнечный", kind="settlement", settlement="Солнечный"),
        _массив("СНТ Солнечный"),
    ]
    решение = g.auto_decide(dataclasses.replace(e, parsed=p, place=солнечный, place_hits=оба))
    assert решение is not None and решение.hit.area == "СНТ Солнечный"
    assert (
        g.auto_decide(dataclasses.replace(e, parsed=p, place=солнечный, place_hits=оба[:1])) is None
    )
    # Два массива-тёзки — выбор; не спрашивали — None.
    assert (
        g.auto_decide(
            dataclasses.replace(
                e,
                place_hits=[
                    _массив("СНТ Урожай"),
                    dataclasses.replace(_массив("СНТ Урожай"), lat=52.0, district="Гайский р-н"),
                ],
            )
        )
        is None
    )
    assert g.auto_decide(dataclasses.replace(e, place_hits=None)) is None


def test_auto_decide_молчит_на_exact_и_без_города() -> None:
    assert g.auto_decide(_evidence(status=g.GEO_EXACT, hit=дом())) is None
    решение = g.auto_decide(
        _evidence(status=g.GEO_EXACT, hit=КЕДРОВСКАЯ_РЯЗАНЬ, city=None, country=True)
    )
    assert решение is not None and (решение.rule, решение.approx, решение.provider) == (
        g.RULE_COUNTRY,
        True,
        "dadata",
    )
    assert g.auto_decide(_evidence(status=g.GEO_NO_CITY, city=None)) is None
    # «Улица не та» без улик и уровня C — молчит (с N24 уровни A/B с уликой
    # улицы решаются как `house_missing`; см. test_paket2_street_mismatch_1909).
    assert g.auto_decide(_evidence(status=g.GEO_STREET_MISMATCH)) is None
    assert (
        g.auto_decide(
            _evidence(
                status=g.GEO_STREET_MISMATCH,
                parsed=g.Parsed(street="ул Ленина", house="5", level="C"),
                city_hits=[([дом(house=None, house_level=False, precise=False)], "dadata")],
            )
        )
        is None
    )


# ── воркер: два случая владельца ───────────────────────────────────────────────

СЕРЕБРЯНСКАЯ_DADATA = [
    дом(
        street="Серебрянская улица",
        house=h,
        city="Иваново",
        region="Ивановская обл",
        lat=57.0124 + i / 1000,
        lon=40.942,
    )
    for i, h in enumerate(("6", "10"))
]
СЕРЕБРЯНСКАЯ_ЯНДЕКС = g.GeoHit(
    street="Серебрянская улица",
    house="8",
    settlement=None,
    city="Иваново",
    region="Ивановская область",
    lat=57.012416,
    lon=40.941792,
    house_level=True,
)


@pytest.fixture
def osm_отвечает(monkeypatch: Any) -> dict[str, Any]:
    состояние: dict[str, Any] = {"ответ": [], "запросы": []}

    async def search(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        состояние["запросы"].append(query)
        if wait is not None:
            await wait()
        return list(состояние["ответ"])

    monkeypatch.setattr(worker.nominatim, "search", search)
    return состояние


async def test_иваново_дом_есть_у_яндекса_после_house_mismatch(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    яндекс_отвечает: dict,
) -> None:
    """Владелец 18.09: «ул. Серебрянская д8» — DaData и OSM знают улицу с другими
    домами, Яндекс отдаёт дом 8 точкой. До 18.09 `house_mismatch` был тупиком
    цепочки: вторая карта спрашивалась только после not_found/house_missing."""
    assert g.GEO_HOUSE_MISMATCH in worker._ВТОРАЯ_КАРТА_ПОСЛЕ
    assert g.GEO_HOUSE_MISMATCH in worker._ИЩЕМ_ПО_ОБЛАСТИ
    await _режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [СЕРЕБРЯНСКАЯ_DADATA]
    osm_отвечает["ответ"] = [
        dataclasses.replace(СЕРЕБРЯНСКАЯ_DADATA[1], region="Ивановская область")
    ]
    яндекс_отвечает["ответ"] = [СЕРЕБРЯНСКАЯ_ЯНДЕКС]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "ivanovo", _разобрать("ул. Серебрянская д8")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider) == (g.GEO_EXACT, "yandex")
    assert (row.geo_lat, row.geo_lon) == (57.012416, 40.941792)
    assert row.geo_formatted == "Серебрянская улица, 8, Иваново" and not row.geo_variants
    # Яндекс спрошен второй картой в городе (не по области), один раз.
    assert [q.city for q in яндекс_отвечает["запросы"]] == ["Иваново"]
    assert (
        g.card_grade(
            "house", row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        == g.GRADE_EXACT
    )


async def test_дубровино_место_после_отказа_dadata_у_яндекса(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    monkeypatch: Any,
    osm_пусто: Any,
    яндекс_отвечает: dict,
) -> None:
    """Владелец 18.09: «ЖК Дубровино» при объявлении в Москве — DaData не знает,
    Яндекс на «Москва, ЖК Дубровино» отдаёт место первым, дальше мусор."""
    await _режим(db_sessionmaker, "nominatim")
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def search_place(place: g.Place, *, region: str | None, **kw: Any) -> list[g.PlaceHit]:
        await kw["on_request"]()
        return []

    monkeypatch.setattr(worker.dadata, "search_place", search_place)
    яндекс_отвечает["ответ"] = [ДУБРОВИНО_ЯНДЕКС, *МУСОР_ЯНДЕКСА]
    found = address_parse.parse_place("ЖК Дубровино")
    assert found is not None
    cid = await _строка(seed_conversation, db_sessionmaker, "moskva", found)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.kind, row.geo_status, row.geo_provider) == ("place", g.GEO_EXACT, "yandex")
    assert row.geo_formatted == "ЖК Дубровино, село Дубровино"
    assert (row.geo_lat, row.geo_lon) == (55.506317, 37.50375)
    assert [q.free_text for q in яндекс_отвечает["запросы"]] == ["Москва, ЖК Дубровино"]
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        == g.GRADE_APPROX
    )
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_дубровино_мусор_яндекса_не_проходит_сторожей(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    monkeypatch: Any,
    osm_пусто: Any,
    яндекс_отвечает: dict,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def search_place(place: g.Place, *, region: str | None, **kw: Any) -> list[g.PlaceHit]:
        await kw["on_request"]()
        return []

    monkeypatch.setattr(worker.dadata, "search_place", search_place)
    яндекс_отвечает["ответ"] = list(МУСОР_ЯНДЕКСА)
    found = address_parse.parse_place("ЖК Дубровино")
    assert found is not None
    cid = await _строка(seed_conversation, db_sessionmaker, "moskva", found)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_NOT_FOUND,
        "dadata",
        None,
    )
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


# ── воркер: дробь ──────────────────────────────────────────────────────────────


async def test_дробь_головой_спрашивается_домом_n_и_хвост_становится_квартирой(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[], [ДОМ_13]]  # «13/38» и «13 к 38» пусто; дом «13» есть
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 13/38"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider) == (g.GEO_EXACT, "dadata~approx")
    assert row.geo_formatted == "ул Ленина, 13/38, Орск" and row.office == "38"
    assert (row.geo_lat, row.geo_lon) == (ДОМ_13.lat, ДОМ_13.lon) and not row.geo_variants
    assert dadata_отвечает["запросы"][1] == g.Query(
        region="Оренбургская область", city="Орск", settlement=None, street="ул Ленина", house="13"
    )
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_дробь_без_сети_когда_dadata_сразу_отдала_дом_n(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[ДОМ_13]]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 13/38"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_provider, row.office, row.geo_formatted) == (
        "dadata~approx",
        "38",
        "ул Ленина, 13/38, Орск",
    )
    assert all(q.house == "13/38" for q in dadata_отвечает["запросы"]), (
        "дом «13» уже в ответах — сеть не нужна"
    )


async def test_дробь_под_выключателем_остаётся_отказом(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None)
        await s.commit()
    dadata_отвечает["ответы"] = [[ДОМ_13]]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 13/38"))
    # Как до 18.09: дом «13» DaData не решает, а пустота OSM остаётся вердиктом
    # (уступка содержательному отказу DaData — тоже под выключателем).
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NOT_FOUND
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_provider, row.office, row.geo_formatted) == ("nominatim", None, None)
    assert dadata_отвечает["запросы"][0].house == "13/38" and len(dadata_отвечает["запросы"]) == 2
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


# ── воркер: страна ─────────────────────────────────────────────────────────────


async def test_страна_без_города_единственный_точный_дом_записывается_приблизительно(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[КЕДРОВСКАЯ_РЯЗАНЬ]]
    cid = await _строка(seed_conversation, db_sessionmaker, None, _разобрать("ул Кедровская 4"))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_EXACT,
        "dadata~approx",
        "ул Кедровская, 4, Рязань",
    )
    assert (row.geo_lat, row.geo_lon) == (КЕДРОВСКАЯ_РЯЗАНЬ.lat, КЕДРОВСКАЯ_РЯЗАНЬ.lon)
    assert dadata_отвечает["запросы"] == [
        g.Query(region=None, city=None, settlement=None, street="ул Кедровская", house="4")
    ]
    assert [л["rule"] for л in логи if л["event"] == "geocode.auto_decided"] == [g.RULE_COUNTRY]
    assert not [л for л in логи if л["event"] == "geocode.ask_client"]
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_страна_два_города_тёзки_варианты_с_областью_и_вопрос_клиенту(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[КОЛХОЗНАЯ_ПОЛЯНЫ_РЯЗАНЬ, КОЛХОЗНАЯ_ПОЛЯНЫ_КИРОВ]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, None, _разобрать("с. Поляны ул Колхозная 4")
    )
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_NO_CITY and row.geo_formatted is None
    assert [v["formatted"] for v in row.geo_variants] == [
        "ул Колхозная, 4, село Поляны, Рязанская обл",
        "ул Колхозная, 4, село Поляны, Кировская обл",
    ]
    # С пунктом ищем только с пунктом: «улица дом» по всей стране — шум.
    assert (
        dadata_отвечает["запросы"][0].settlement == "Поляны"
        and dadata_отвечает["запросы"][0].region is None
    )
    assert [л["reason"] for л in логи if л["event"] == "geocode.ask_client"] == [g.ASK_CITY]
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_страна_с_городом_клиента_вне_справочника_и_без_ответа_не_переспрашивает(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, None, _разобрать("г Цимлянск, ул Ленина 5")
    )
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY
    assert (
        dadata_отвечает["запросы"][0].city == "Цимлянск"
        and dadata_отвечает["запросы"][0].region is None
    )
    assert not [л for л in логи if л["event"] == "geocode.ask_client"], (
        "сказанное не переспрашиваем"
    )
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_NO_CITY and row.geo_attempts == 1


async def test_страна_за_потолком_dadata_попытка_не_тратится(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def потолок(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        raise g.GeocodeError("dadata", "limit")

    monkeypatch.setattr(worker.dadata, "search", потолок)
    cid = await _строка(seed_conversation, db_sessionmaker, None, _разобрать("ул Кедровская 4"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_attempts, row.geo_variants or None) == (g.GEO_NO_CITY, 0, None)


async def test_страна_слаг_вне_справочника_ищется_но_вопроса_нет(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[]]
    cid = await _строка(seed_conversation, db_sessionmaker, "kotlas", _разобрать("ул Кедровская 4"))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY
    assert len(dadata_отвечает["запросы"]) == 1, (
        "город в ссылке виден человеку, карте — нет: страну ищем"
    )
    assert not [л for л in логи if л["event"] == "geocode.ask_client"]


async def test_страна_под_выключателем_не_ищется(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None)
        await s.commit()
    dadata_отвечает["ответы"] = [[КЕДРОВСКАЯ_РЯЗАНЬ]]
    cid = await _строка(seed_conversation, db_sessionmaker, None, _разобрать("ул Кедровская 4"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_NO_CITY
    assert dadata_отвечает["запросы"] == []


# ── воркер: массив, точка улицы, выбор из нескольких ───────────────────────────


async def test_участок_массива_точка_массива_когда_дома_у_фиас_нет(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    monkeypatch: Any,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    места: list[tuple[g.Place, dict[str, Any]]] = []

    async def search_place(place: g.Place, *, region: str | None, **kw: Any) -> list[g.PlaceHit]:
        await kw["on_request"]()
        места.append((place, {"region": region, "city": kw.get("city")}))
        return [_массив("СНТ Урожай")]

    monkeypatch.setattr(worker.dadata, "search_place", search_place)
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("СНТ Урожай 273"))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_EXACT,
        "dadata~approx",
        "СНТ Урожай, 273",
    )
    assert (row.geo_lat, row.geo_lon) == (51.6, 55.2)
    # Массив спрашивается массивом без пункта, в районах области, не в черте города.
    [(place, аргументы)] = места
    assert (place.area, place.settlement, place.query_text) == ("СНТ Урожай", None, "СНТ Урожай")
    assert аргументы == {"region": "Оренбургская область", "city": None}
    assert [л["rule"] for л in логи if л["event"] == "geocode.auto_decided"] == [g.RULE_AREA_POINT]
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def _псков(seed_conversation: Any, db_sessionmaker: Any, monkeypatch: Any) -> Any:
    """Стенд Пскова (владелец 16.09): улица в деревне есть, дома 5 нет; дом 5 —
    в самом Пскове. До 18.09 — вариант улицы для оператора."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    cid = await _строка(
        seed_conversation,
        db_sessionmaker,
        "pskov",
        _разобрать("Деревня Сорокино улица Луговая дом 5"),
    )

    async def dadata_search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        await kw["on_request"]()
        seen = kw.get("seen")
        if query.settlement:
            улица = _луговая("Сорокино", "5", house_level=False, lat=57.9)
            if seen is not None:
                seen.append(улица)
            if kw.get("without_settlement_too", True) is False:
                return [улица]
        дома = [_луговая("Псков", "5", house_level=True, lat=57.78)]
        if seen is not None:
            seen.extend(дома)
        return дома

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    return cid


async def test_точка_улицы_в_деревне_уточняется_яндексом(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    monkeypatch: Any,
    osm_пусто: Any,
    яндекс_отвечает: dict,
) -> None:
    """Точка улицы (`street_point`, `precise=False`) ведёт воркер к Яндексу за
    точкой дома по компонентам улицы деревни; тот же дом с точной точкой —
    его координаты и `+yandex`. Яндекс по области дома не знает (иначе дом
    нашёлся бы там раньше — другой, тоже законный путь)."""
    await _режим(db_sessionmaker, "nominatim")
    cid = await _псков(seed_conversation, db_sessionmaker, monkeypatch)
    дом_5 = dataclasses.replace(
        _луговая("Сорокино", "5", house_level=True, lat=57.91), region="Псковская область"
    )
    запросы: list[g.Query] = []

    async def яндекс(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        await kw["on_request"]()
        запросы.append(query)
        # Дом — только по компонентам найденной улицы (город = деревня).
        return [дом_5] if query.city == "Сорокино" else []

    monkeypatch.setattr(worker.yandex_geocoder, "search", яндекс)
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider) == (g.GEO_EXACT, "dadata+yandex")
    assert row.geo_formatted == "ул Луговая, 5, Сорокино" and row.geo_lat == 57.91
    assert not row.geo_variants
    assert [л["rule"] for л in логи if л["event"] == "geocode.auto_decided"] == [
        g.RULE_STREET_POINT
    ]
    # Точку дома спрашивали по компонентам улицы деревни, не по дому в Пскове.
    assert запросы[-1].city == "Сорокино" and запросы[-1].house == "5"
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_точка_улицы_без_яндекса_приблизительная(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    cid = await _псков(seed_conversation, db_sessionmaker, monkeypatch)
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_lat) == (g.GEO_EXACT, "dadata~approx", 57.9)
    assert row.geo_formatted == "ул Луговая, 5, Сорокино"
    assert [л["rule"] for л in логи if л["event"] == "geocode.auto_decided"] == [
        g.RULE_STREET_POINT
    ]
    assert (
        g.card_grade(
            "house", row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        == g.GRADE_APPROX
    )


async def test_один_в_радиусе_из_двух_решается_сам(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[], [СМЫШЛЯЕВКА, СЫЗРАНЬ]]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_EXACT,
        "dadata~approx",
        "ул Ленина, 5, Смышляевка",
    )
    assert (row.geo_lat, row.geo_lon) == (СМЫШЛЯЕВКА.lat, СМЫШЛЯЕВКА.lon) and not row.geo_variants
    [решение] = [л for л in логи if л["event"] == "geocode.auto_decided"]
    assert решение["rule"] == g.RULE_ONLY_IN_RADIUS and решение["was"] == g.GEO_ELSEWHERE
    assert sorted(решение["km"]) == [18, 109]
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_один_в_радиусе_под_выключателем_остаётся_выбором_оператора(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None)
        await s.commit()
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[], [СМЫШЛЯЕВКА, СЫЗРАНЬ]]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, len(row.geo_variants)) == (
        g.GEO_ELSEWHERE,
        "dadata",
        2,
    )
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_пункт_не_назван_дом_в_самом_городе_решается_сам(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    """Прежний стенд ревью 12.09 (`ambiguous` с двумя вариантами) под
    автопривязкой: пункт не назван, ровно один дом стоит в самом городе —
    правило `best_of`; второго запроса по области по-прежнему нет."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[дом(), дом(settlement="Заречный", lat=51.3)]]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 5"))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_provider, row.geo_formatted, row.geo_variants or None) == (
        "dadata~approx",
        "ул Ленина, 5, Орск",
        None,
    )
    assert [q.city for q in dadata_отвечает["запросы"]] == ["Орск"]
    assert [л["rule"] for л in логи if л["event"] == "geocode.auto_decided"] == [g.RULE_BEST_OF]


async def test_семья_дома_в_воркере_29_становится_29а_а_семья_из_двух_строкой_улицы(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    """«ул Ленина 29», у DaData только «29А» — правило `house_family`: дом с
    пометкой. Семья из двух («29А» и «29Б») — выбор, не решение: строка
    опускается до строки улицы без точки (дома-соседи улицу доказывают)."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[дом(house="29А")]]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 29"))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_provider, row.geo_formatted) == ("dadata~approx", "ул Ленина, 29А, Орск")
    assert [л["rule"] for л in логи if л["event"] == "geocode.auto_decided"] == [
        g.RULE_HOUSE_FAMILY
    ]

    # Другая улица: та же строка в диалоге вернула бы уже решённую.
    семья = [дом(street="ул Мира", house="29А"), дом(street="ул Мира", house="29Б", lat=51.25)]
    dadata_отвечает["ответы"] = [семья]
    cid2 = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Мира 29, кв 3"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid2) == g.GEO_HOUSE_MISMATCH
    row2 = await _row(db_sessionmaker, cid2)
    assert (row2.geo_status, row2.geo_provider, row2.geo_lat) == (
        g.GEO_HOUSE_MISMATCH,
        "dadata",
        None,
    )
    assert row2.geo_formatted == "ул Мира, 29, Орск" and not row2.geo_variants
    assert (
        g.card_grade(row2.kind, row2.geo_status, row2.geo_provider, None, None, row2.geo_formatted)
        == g.GRADE_TEXT
    )


# ── воркер: строка улицы без точки ─────────────────────────────────────────────

СОСЕД_4 = дом(street="ул Молодёжная", house="4")
#: Догадка DaData уровня 7: улица есть, дома нет, точка улицы (qc_geo 2).
УЛИЦА_МОЛОДЁЖНАЯ = дом(street="ул Молодёжная", house=None, house_level=False, precise=False)


async def test_строка_улицы_без_точки_когда_карта_знает_улицу_соседом(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    """«ул Молодёжная 2» уровня A: у DaData на улице дом 4, второго нет, OSM
    пуст. Пустота OSM уступает содержательному отказу DaData; строка улицы с
    номером клиента ложится в `geo_formatted` без координат — степень `text`."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[СОСЕД_4]]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Молодёжная 2"))
    with structlog.testing.capture_logs() as логи:
        assert (
            await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_HOUSE_MISMATCH
        )
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider) == (g.GEO_HOUSE_MISMATCH, "dadata")
    assert row.geo_formatted == "ул Молодёжная, 2, Орск" and (row.geo_lat, row.geo_lon) == (
        None,
        None,
    )
    assert not row.geo_variants
    assert [л["status"] for л in логи if л["event"] == "geocode.street_text"] == [
        g.GEO_HOUSE_MISMATCH
    ]
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        == g.GRADE_TEXT
    )
    assert (
        g.point_precision(row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon)
        == g.PRECISION_NONE
    )
    # Сборщик текста карточки верит строке карты, а не статусу.
    assert (
        g.address_text(
            geo_formatted=row.geo_formatted,
            geo_status=row.geo_status,
            value=row.value,
            parts={"office": "7"},
        )
        == "ул Молодёжная, 2, Орск, кв 7"
    )


@pytest.mark.parametrize(
    ("текст", "ответ_dadata", "ответ_osm", "выключить", "ожидание"),
    [
        # Улица в ответах не сошлась — строки нет (STREET_KNOWN): отказ по
        # дому с чужой улицей и отказ по улице с тем же номером.
        (
            "ул Молодёжная 2",
            [дом(street="ул Строителей", house="4")],
            [dataclasses.replace(СОСЕД_4, street="ул Строителей")],
            False,
            g.GEO_HOUSE_MISMATCH,
        ),
        # «Улица не та» уровня A: с N24 цепочка идёт по области и отказывает
        # тем же сторожем — улицы клиента ни у кого нет, строки нет.
        (
            "ул Молодёжная 2",
            [дом(street="ул Строителей", house="2")],
            [дом(street="ул Строителей", house="2")],
            False,
            g.GEO_STREET_MISMATCH,
        ),
        # Невод уровня C текстом — никогда.
        ("Молодёжная 2", [СОСЕД_4], [], False, g.GEO_HOUSE_MISMATCH),
        # …и точкой улицы — тоже никогда (ревью 19.09): уличный хит без дома
        # у DaData для C остаётся `house_missing` без строки и без точки.
        ("Молодёжная 2", [УЛИЦА_МОЛОДЁЖНАЯ], [], False, g.GEO_HOUSE_MISSING),
        # Выключатель выключен — как до 18.09 (и пустота OSM остаётся пустотой).
        ("ул Молодёжная 2", [СОСЕД_4], [], True, g.GEO_NOT_FOUND),
    ],
)
async def test_строка_улицы_не_пишется_без_сторожа(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    текст: str,
    ответ_dadata: list[g.GeoHit],
    ответ_osm: list[g.GeoHit],
    выключить: bool,
    ожидание: str,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    if выключить:
        async with db_sessionmaker() as s:
            await app_settings.set_many(
                s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None
            )
            await s.commit()
    dadata_отвечает["ответы"] = [ответ_dadata]
    osm_отвечает["ответ"] = ответ_osm
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(текст))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == ожидание
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_formatted, row.geo_lat) == (ожидание, None, None)
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        is None
    )


@pytest.mark.parametrize(
    ("текст", "ожидание", "provider", "lat", "formatted", "автозапись"),
    [
        # Уровень A: та же улика — точка улицы, `~approx`, автозапись ставится.
        ("ул Липовая 6", g.GEO_EXACT, "dadata~approx", 51.2, "ул Липовая, 6, Орск", True),
        # Уровень C: отказ без строки, без точки, без автозаписи (до 18.09 так и было).
        ("Липовая 6", g.GEO_HOUSE_MISSING, "dadata", None, None, False),
    ],
)
async def test_точка_улицы_уровню_c_не_пишется(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    текст: str,
    ожидание: str,
    provider: str,
    lat: float | None,
    formatted: str | None,
    автозапись: bool,
) -> None:
    """Ревью 19.09: строка уровня C с уличным хитом DaData без дома уходила в
    карточку приблизительной точкой улицы — по улике, по которой тексту C
    отказано. Один сторож уровня на обе степени."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [
        [дом(street="ул Липовая", house=None, house_level=False, precise=False)]
    ]
    found = _разобрать(текст)
    assert found.level == (address_parse.LEVEL_C if текст[0].isupper() else address_parse.LEVEL_A)
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", found)
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == ожидание
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_lat, row.geo_formatted) == (
        ожидание,
        provider,
        lat,
        formatted,
    )
    assert [л["rule"] for л in логи if л["event"] == "geocode.auto_decided"] == (
        [g.RULE_STREET_POINT] if автозапись else []
    )
    степень = g.card_grade(
        row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
    )
    assert степень == (g.GRADE_APPROX if автозапись else None)
    assert (
        bool(await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}"))
        is автозапись
    )


async def test_строка_улицы_не_пишется_при_двух_одноимённых_улицах_в_городе(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, dadata_отвечает: dict, osm_пусто: Any
) -> None:
    """Самара, «ул Молодёжная 2»: у DaData соседи «6, Красная Глинка» и «4,
    Самара» — две улицы в 25 км. Строка улицы без точки первой из них в
    карточку не идёт: `known_street` видит неоднозначность (ревью 19.09).
    Сосед только в самом городе — строка есть."""
    await _режим(db_sessionmaker, "nominatim")
    глинка_6 = _молодёжная_самара("Красная Глинка", lat=53.38, house="6")
    самара_4 = _молодёжная_самара(None, lat=53.19, house="4")
    dadata_отвечает["ответы"] = [[глинка_6, самара_4]]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Молодёжная 2"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_HOUSE_MISMATCH
    row = await _row(db_sessionmaker, cid)
    # Сам город против посёлка — сам город (как `_best_of` у домов): строка есть.
    assert (row.geo_status, row.geo_formatted, row.geo_lat) == (
        g.GEO_HOUSE_MISMATCH,
        "ул Молодёжная, 2, Самара",
        None,
    )
    # Два посёлка — ни строки, ни точки (другой номер: та же строка в диалоге
    # вернула бы уже решённую).
    dadata_отвечает["ответы"] = [
        [глинка_6, _молодёжная_самара("Зубчаниновка", lat=53.26, house="4")]
    ]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Молодёжная 3"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_HOUSE_MISMATCH
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_formatted, row.geo_lat) == (g.GEO_HOUSE_MISMATCH, None, None)
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        is None
    )


async def test_без_dadata_на_сутки_ни_решений_ни_строки_улицы(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, osm_отвечает: dict
) -> None:
    """DaData настроена, но за потолком: `ambiguous` от OSM — не в отказах по
    содержанию и не откладывается, но и решением автоматики не становится."""
    await _режим(db_sessionmaker, "nominatim")
    monkeypatch.setitem(gateway.known_keys, "dadata", True)

    async def потолок(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        raise g.GeocodeError("dadata", "limit")

    monkeypatch.setattr(worker.dadata, "search", потолок)
    osm_отвечает["ответ"] = [дом(), дом(settlement="Заречный", lat=51.3)]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_AMBIGUOUS
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, len(row.geo_variants), row.geo_formatted) == (
        g.GEO_AMBIGUOUS,
        "nominatim",
        2,
        None,
    )
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
