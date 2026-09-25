"""Сосед за забором — правило `neighbour_settlement` (владелец 20.09).

Случай: «деревня Кленово, малиновая 17» при объявлении в Одинцове. DaData
отдаёт единственный дом «Липовка, ул Малиновая, 17» (юридически — соседний
посёлок, на местности — за общим забором), а улицы Малиновой в самом Кленово
не знает. HEAD 32a9717 заканчивал такую строку «в другом месте» с одним
вариантом и пустой карточкой (трасса — docs/47 §4.8).

Правило: итог `elsewhere` ПОСЛЕ области (в её ответах — и дом-сосед, и эхо
улицы пункта, если она там есть), пункт назван и не массив, уровень A/B,
кандидат — дом, у которого сошлось всё, кроме пункта, и он РОВНО ОДИН в
`NEIGHBOUR_KM` от точки названного пункта с ТЕМ ЖЕ именем → `exact` с
`~approx`. Каждый сторож здесь получает контрпример из возражений скептика
20.09: соперник с точкой пункта, нечёткая тёзка пункта (Жуково/Жуковка), дом в
самом городе, дом в СНТ, чужой тип улицы, слаг «Москва и МО» без области.

Сеть не ходит: карты подменены записанными ответами (фикстуры стенда 18.09).
География — Орск, имена пунктов и улиц вымышленные, координаты синтетические
(0,009° широты ≈ 1,0 км).
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
import structlog

from app.integrations.avito.listing_url import City
from app.models import ClientAddressCandidate
from app.services import app_settings
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit import test_geo_1809 as стенд
from tests.unit.test_autobind_policy_1809 import ОРСК, _evidence
from tests.unit.test_geo_1809 import _row, _разобрать, _режим, _строка, ctx, дом

dadata_отвечает = стенд.dadata_отвечает
osm_пусто = стенд.osm_пусто
точка_города = стенд.точка_города

pytestmark = pytest.mark.anyio

ОБЛАСТЬ = "Оренбургская область"
ТОЧКА_ОРСКА = (51.23, 58.47)

#: Дом-сосед: улица и номер клиента, пункт — Липовка, точка самого дома.
ЛИПОВКА_17 = дом(street="ул Малиновая", house="17", settlement="Липовка", lat=51.2000, lon=58.5000)
#: Точка названной деревни — 1,0 км от дома (0,009° широты).
КЛЕНОВО = g.PlaceHit(
    name="д Кленово",
    kind="settlement",
    settlement="Кленово",
    area=None,
    city="Орск",
    district=None,
    region=ОБЛАСТЬ,
    lat=51.2090,
    lon=58.5000,
)
КЛЕНОВО_5_КМ = dataclasses.replace(КЛЕНОВО, lat=51.2450)
КЛЕНОВО_80_КМ = dataclasses.replace(КЛЕНОВО, lat=51.92)
#: Второй дом той же улицы и номера в другом пункте, 0,67 км от точки Кленово.
ЖУКОВКА_17 = dataclasses.replace(ЛИПОВКА_17, settlement="Жуковка", lat=51.2150)
#: Тот же соперник с точкой пункта (qc_geo 3 — обычное дело у деревень).
ЖУКОВКА_17_ТОЧКА_ПУНКТА = dataclasses.replace(ЖУКОВКА_17, precise=False)
#: Эхо DaData уровня 7: улица в Кленово есть, дома 17 на ней нет — форма, в
#: которой «улица без дома» приходит на самом деле (номер клиента,
#: `house_level=False`, точка улицы).
ЭХО_КЛЕНОВО = дом(
    street="ул Малиновая",
    house="17",
    settlement="Кленово",
    lat=51.2090,
    lon=58.5000,
    house_level=False,
    precise=False,
)
#: Дом «17» без улицы в Кленово: шлюз ставит улицей имя пункта.
КЛЕНОВО_17_БЕЗ_УЛИЦЫ = dataclasses.replace(ЭХО_КЛЕНОВО, street="д Кленово", house_level=True)
КЛЕНОВО_ЦЕНТРАЛЬНАЯ_17 = dataclasses.replace(ЭХО_КЛЕНОВО, street="ул Центральная", house_level=True)
#: Дом клиента в самом городе объявления (пункта нет).
ОРСК_МАЛИНОВАЯ_17 = dataclasses.replace(ЛИПОВКА_17, settlement=None)
#: Дом в СНТ внутри соседнего пункта (форма DaData — массив в `area`; форма
#: Яндекса — второй пункт в `settlement`, `area` он не пишет никогда) и
#: переулок-тёзка.
СНТ_ЛУЧ_17 = dataclasses.replace(ЛИПОВКА_17, area="СНТ Луч")
СНТ_ЛУЧ_17_ЯНДЕКС = dataclasses.replace(
    ЛИПОВКА_17, street="Малиновая улица", settlement="СНТ Луч", lat=51.20030
)
#: Тот же сосед за 1 км от тёзки Кленово, но в 76 км от Орска: тёзка стоит
#: рядом с домом, а не с клиентом.
ЛИПОВКА_17_ЗА_76_КМ = dataclasses.replace(ЛИПОВКА_17, lat=51.911)
ПЕР_МАЛИНОВЫЙ_17 = dataclasses.replace(ЛИПОВКА_17, street="пер Малиновый")

КЛЕНОВО_МАЛИНОВАЯ = g.Parsed(
    street="малиновая", house="17", settlement="Кленово", settlement_type="деревня", level="A"
)


def _вариант(h: g.GeoHit) -> dict[str, Any]:
    return g.variants(КЛЕНОВО_МАЛИНОВАЯ, ОРСК, [h])[0]


def _улики(**kw: Any) -> g.Evidence:
    """Улики случая владельца после области: DaData по городу и область
    отдали один и тот же дом-сосед, точка пункта спрошена."""
    база: dict[str, Any] = {
        "status": g.GEO_ELSEWHERE,
        "parsed": КЛЕНОВО_МАЛИНОВАЯ,
        "city": ОРСК,
        "city_hits": [([ЛИПОВКА_17], "dadata"), ([], "nominatim")],
        "region_hits": [ЛИПОВКА_17],
        "region_provider": "dadata",
        "variants": [_вариант(ЛИПОВКА_17)],
        "city_point": ТОЧКА_ОРСКА,
        "settlement_place_hits": [КЛЕНОВО],
    }
    база.update(kw)
    return _evidence(**база)


def _решение(**kw: Any) -> g.Decision | None:
    return g.auto_decide(_улики(**kw))


# ── чистый слой: правило ───────────────────────────────────────────────────────


def test_сосед_за_забором_решает_с_пометкой_и_расстоянием() -> None:
    решение = _решение()
    assert решение is not None
    assert (решение.rule, решение.hit, решение.approx, решение.provider) == (
        g.RULE_NEIGHBOUR_SETTLEMENT,
        ЛИПОВКА_17,
        True,
        "dadata",
    )
    assert решение.km is not None and 0.9 < решение.km < 1.1
    # Разбор не подменяется: слово клиента о пункте остаётся в строке.
    assert решение.parsed is КЛЕНОВО_МАЛИНОВАЯ
    # Строка карты — адрес соседа, как в картах.
    assert g.format_address(решение.hit, решение.parsed) == "ул Малиновая, 17, Липовка, Орск"


def test_вход_только_elsewhere_после_области() -> None:
    """Улика «улица в пункте есть» — эхо DaData, которое живёт только в
    ответах области (`seen`); без области сторож слеп, и правило молчит на
    любом другом итоге — даже с теми же домами в уликах."""
    assert _решение(region_hits=[]) is None
    assert _решение(status=g.GEO_SETTLEMENT_MISMATCH, region_hits=[]) is None
    assert _решение(status=g.GEO_SETTLEMENT_MISMATCH) is None
    for статус in (
        g.GEO_STREET_MISMATCH,
        g.GEO_HOUSE_MISSING,
        g.GEO_NOT_FOUND,
        g.GEO_AMBIGUOUS,
        g.GEO_OTHER_CITY,
        g.GEO_CITY_MISMATCH,
    ):
        assert _решение(status=статус) is None, статус


def test_соперник_с_точкой_пункта_считается_до_подсчёта() -> None:
    """Скептик 1.1: второй дом той же улицы и номера в круге — соперник, даже
    если его точка — центр пункта (qc_geo 3); отбросить его до подсчёта
    значило бы выдать единственный точный за единственный."""
    assert _решение(region_hits=[ЛИПОВКА_17, ЖУКОВКА_17_ТОЧКА_ПУНКТА]) is None
    assert _решение(region_hits=[ЛИПОВКА_17, ЖУКОВКА_17]) is None
    # Соперник за кругом (5 км) — не соперник, хоть и неточный.
    далёкий = dataclasses.replace(ЖУКОВКА_17_ТОЧКА_ПУНКТА, lat=51.2540)
    решение = _решение(region_hits=[ЛИПОВКА_17, далёкий])
    assert решение is not None and решение.hit is ЛИПОВКА_17
    # Единственный выживший без точки дома — молчание: мерить нечем.
    неточный = dataclasses.replace(ЛИПОВКА_17, precise=False)
    assert _решение(city_hits=[([неточный], "dadata")], region_hits=[неточный], variants=[]) is None


def test_тот_же_дом_у_двух_карт_не_соперники() -> None:
    """DaData «ул Малиновая» и Яндекс «Малиновая улица» — один дом в десятках
    метров (`ONE_SPOT_KM`), `distinct_addresses` их не склеивает; решает
    первый — DaData. Два разных дома в 500 м — соперники."""
    яндекс = dataclasses.replace(
        ЛИПОВКА_17, street="Малиновая улица", settlement="посёлок Липовка", lat=51.20030
    )
    решение = _решение(city_hits=[([ЛИПОВКА_17], "dadata"), ([яндекс], "yandex")])
    assert решение is not None and решение.hit is ЛИПОВКА_17 and решение.provider == "dadata"
    другой = dataclasses.replace(яндекс, lat=51.2045)
    assert _решение(city_hits=[([ЛИПОВКА_17], "dadata"), ([другой], "yandex")]) is None


@pytest.mark.parametrize(
    ("пункт_клиента", "имя_места", "ожидание"),
    [
        ("Кленово", "Кленово", True),
        ("кленово", "Кленово", True),
        ("Горки-2", "Горки 2", True),
        ("Жуково", "Жуковка", False),  # опечатка ±1 — невод, не тёзка
        ("Марфино", "Марьино", False),
        ("Кленово", "Кленовка", False),
        ("Горки", "Горки-2", False),  # обрубок — не тёзка
    ],
)
def test_имя_пункта_только_целиком(пункт_клиента: str, имя_места: str, ожидание: bool) -> None:
    """Скептик 1.2: место ВЫБИРАЕТСЯ по минимуму расстояния, и терпимость
    `_пункт_назван` к опечатке и обрубку здесь ловила бы Жуковку для клиента
    из Жукова в 40 км."""
    parsed = dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, settlement=пункт_клиента)
    место = dataclasses.replace(КЛЕНОВО, settlement=имя_места, name=f"д {имя_места}")
    решение = _решение(parsed=parsed, variants=[], settlement_place_hits=[место])
    assert (решение is not None) is ожидание
    # Диверсия смысла: нечёткое сравнение эти пары пропускает — потому оно
    # здесь и не используется.
    assert g._пункт_назван(g._норм(пункт_клиента), g._слова(имя_места)) is True


def test_улица_в_названном_пункте_есть_правило_молчит() -> None:
    """Сторож (в): названный пункт улицу клиента знает — эхом DaData (дома нет),
    домом на ней или переулком-тёзкой чужого типа — тогда спор о доме на своей
    улице, не о соседе (N24, путь `street_point`/`house_missing`). Дом «17» без
    улицы и дом 17 на ДРУГОЙ улице пункта — не улика (улица у них — не улица
    клиента)."""
    assert _решение(region_hits=[ЛИПОВКА_17, ЭХО_КЛЕНОВО]) is None
    дом_21 = dataclasses.replace(ЭХО_КЛЕНОВО, house="21", house_level=True)
    assert _решение(region_hits=[ЛИПОВКА_17, дом_21]) is None
    переулок = dataclasses.replace(ЭХО_КЛЕНОВО, street="пер Малиновый")
    assert _решение(region_hits=[ЛИПОВКА_17, переулок]) is None
    # Эхо может лежать и в ответах по городу — пул общий.
    assert _решение(city_hits=[([ЛИПОВКА_17, ЭХО_КЛЕНОВО], "dadata")]) is None
    for не_улика in (КЛЕНОВО_17_БЕЗ_УЛИЦЫ, КЛЕНОВО_ЦЕНТРАЛЬНАЯ_17):
        решение = _решение(region_hits=[ЛИПОВКА_17, не_улика])
        assert решение is not None and решение.hit is ЛИПОВКА_17, не_улика.street


def test_дом_в_городе_объявления_в_снт_и_чужого_типа_не_кандидат() -> None:
    """Скептик 1.3, 1.4, 1.6: клиент назвал деревню — он не в городе; улицы
    СНТ внутренние; «малиновая» без типа — не «пер Малиновый»."""
    for чужой in (ОРСК_МАЛИНОВАЯ_17, СНТ_ЛУЧ_17, ПЕР_МАЛИНОВЫЙ_17):
        assert (
            _решение(
                city_hits=[([чужой], "dadata")], region_hits=[чужой], variants=[_вариант(чужой)]
            )
            is None
        ), чужой
    # C3: тот же СНТ в форме Яндекса — массив в `settlement`, `area` пуст;
    # единственный кандидат пришёл Яндексом по городу.
    assert СНТ_ЛУЧ_17_ЯНДЕКС.area is None
    assert (
        _решение(
            city_hits=[([], "dadata"), ([СНТ_ЛУЧ_17_ЯНДЕКС], "yandex")],
            region_hits=[СНТ_ЛУЧ_17_ЯНДЕКС],
            variants=[_вариант(СНТ_ЛУЧ_17_ЯНДЕКС)],
        )
        is None
    )
    assert g.neighbour_candidates(КЛЕНОВО_МАЛИНОВАЯ, ОРСК, [СНТ_ЛУЧ_17_ЯНДЕКС]) == []
    assert g._дом_в_массиве_с_участками(СНТ_ЛУЧ_17_ЯНДЕКС) and g._дом_в_массиве_с_участками(
        СНТ_ЛУЧ_17
    )
    assert not g._дом_в_массиве_с_участками(ЛИПОВКА_17)
    # Клиент назвал тип сам — переулок годится (тип сверил вердикт).
    с_типом = dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, street="пер малиновый")
    решение = _решение(
        parsed=с_типом,
        city_hits=[([ПЕР_МАЛИНОВЫЙ_17], "dadata")],
        region_hits=[ПЕР_МАЛИНОВЫЙ_17],
        variants=[],
    )
    assert решение is not None and решение.hit is ПЕР_МАЛИНОВЫЙ_17
    # Массив мягкого типа у соседа — обычный дом.
    в_микрорайоне = dataclasses.replace(ЛИПОВКА_17, area="мкр Восточный")
    решение = _решение(city_hits=[([в_микрорайоне], "dadata")], region_hits=[в_микрорайоне])
    assert решение is not None and решение.hit is в_микрорайоне


def test_радиус_и_места_пункта() -> None:
    """5 км — молчание; тёзка в 80 км рядом с тёзкой в 1 км — решает ближняя
    (не `ambiguous` мест: несколько Кленово в области — норма); массив,
    улица и чужая область местом не считаются; ответа нет — молчание."""
    assert _решение(settlement_place_hits=[КЛЕНОВО_5_КМ]) is None
    решение = _решение(settlement_place_hits=[КЛЕНОВО_80_КМ, КЛЕНОВО])
    assert решение is not None and решение.km is not None and решение.km < 1.1
    assert _решение(settlement_place_hits=[КЛЕНОВО_80_КМ]) is None
    for не_место in (
        # Массив и улица с именем пункта — не пункт: имя у них лежит в `area`
        # и `name`, а места с таким `kind` правило не читает.
        dataclasses.replace(КЛЕНОВО, kind="area", settlement=None, area="Кленово"),
        dataclasses.replace(КЛЕНОВО, kind="street", settlement=None, name="Кленово"),
        dataclasses.replace(КЛЕНОВО, region="Самарская область"),
    ):
        assert _решение(settlement_place_hits=[не_место]) is None, не_место.kind
    assert _решение(settlement_place_hits=[]) is None
    assert _решение(settlement_place_hits=None) is None
    # Город-тёзка уровня 4 — место: дачные посёлки у ФИАС бывают городами.
    город = dataclasses.replace(КЛЕНОВО, kind="city", settlement=None, city="Кленово")
    assert _решение(settlement_place_hits=[город]) is not None


def test_якорь_города_объявления_у_соседа() -> None:
    """C2: сосед в 1 км от тёзки Кленово, но в 76 км от Орска — тёзка стоит
    рядом с ДОМОМ, а не с клиентом (`search_place` отдаёт до десяти тёзок
    области, Яндекс по городу — дома всего региона): без якоря дом решался
    бы. Дальше `SUBURB_KM` от точки города кандидат не считается; точки города
    нет — молчание; дальний дом и соперником не становится — ближний решает
    (тот же довод, что у `only_in_radius`)."""
    далёкий = ЛИПОВКА_17_ЗА_76_КМ
    assert (
        0.9
        < g.distance_km((КЛЕНОВО_80_КМ.lat, КЛЕНОВО_80_КМ.lon), (далёкий.lat, далёкий.lon))
        < 1.1
    )
    assert g.distance_km(ТОЧКА_ОРСКА, (далёкий.lat, далёкий.lon)) > g.SUBURB_KM
    assert (
        _решение(
            city_hits=[([далёкий], "dadata")],
            region_hits=[далёкий],
            variants=[_вариант(далёкий)],
            settlement_place_hits=[КЛЕНОВО_80_КМ],
        )
        is None
    )
    # Контроль: тот же дом с точкой города рядом — решает.
    рядом = _решение(
        city_hits=[([далёкий], "dadata")],
        region_hits=[далёкий],
        variants=[_вариант(далёкий)],
        settlement_place_hits=[КЛЕНОВО_80_КМ],
        city_point=(51.93, 58.47),
    )
    assert рядом is not None and рядом.hit is далёкий
    assert _решение(city_point=None) is None
    assert g.distance_km(ТОЧКА_ОРСКА, (ЛИПОВКА_17.lat, ЛИПОВКА_17.lon)) < g.SUBURB_KM
    решение = _решение(
        region_hits=[ЛИПОВКА_17, далёкий], settlement_place_hits=[КЛЕНОВО, КЛЕНОВО_80_КМ]
    )
    assert решение is not None and решение.hit is ЛИПОВКА_17


def test_сторожа_слов_клиента() -> None:
    """Уровень C, пункт-массив, пункт не назван, номер не дом, спор города —
    молчание; названный город обязан быть у кандидата."""
    assert _решение(parsed=dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, level="C")) is None
    assert _решение(parsed=dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, level="B")) is not None
    снт = dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, settlement="Луч", settlement_type="СНТ")
    assert _решение(parsed=снт, settlement_place_hits=[КЛЕНОВО]) is None
    assert g.settlement_place(снт) is None
    без_пункта = dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, settlement=None, settlement_type=None)
    assert _решение(parsed=без_пункта) is None
    assert _решение(parsed=dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, house="4G")) is None
    с_городом = dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, locality="Гай")
    assert _решение(parsed=с_городом) is None
    assert _решение(parsed=dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, locality="Орск")) is not None
    # Спор города об улице гасит все правила до диспетчера: клиент назвал тип
    # («ул малиновая»), а в его пункте по городу нашёлся «пр-кт Малиновый, 17».
    с_типом_улицы = dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, street="ул малиновая")
    спорный = dataclasses.replace(ЭХО_КЛЕНОВО, street="пр-кт Малиновый", house_level=True)
    assert g.city_disputes_street(с_типом_улицы, ОРСК, [([спорный], "dadata")])
    assert _решение(parsed=с_типом_улицы, city_hits=[([спорный], "dadata")]) is None
    assert _решение(parsed=с_типом_улицы) is not None


def test_кандидаты_и_место_чистые_функции() -> None:
    пул = [ЛИПОВКА_17, dataclasses.replace(ЛИПОВКА_17, lat=51.20005)]  # два строения
    assert g.neighbour_candidates(КЛЕНОВО_МАЛИНОВАЯ, ОРСК, пул) == [ЛИПОВКА_17]
    assert g.neighbour_candidates(КЛЕНОВО_МАЛИНОВАЯ, None, пул) == []
    без_улицы = dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, street="")
    assert g.neighbour_candidates(без_улицы, ОРСК, пул) == []
    assert g.neighbour_candidates(КЛЕНОВО_МАЛИНОВАЯ, ОРСК, пул + [ЭХО_КЛЕНОВО]) == []
    место = g.settlement_place(КЛЕНОВО_МАЛИНОВАЯ)
    assert место is not None and место.query_text == "деревня Кленово"
    assert (место.street, место.area, место.district) == ("", None, None)
    assert g.settlement_place(dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, settlement=None)) is None


def test_версия_судьи_поднята_и_имя_правила_в_журнале() -> None:
    """Правило меняет `auto_decide` — версия судьи растёт (контракт шапки
    `VERDICT_VERSION`), обход починки вернёт хвост сам. Единственный assert с
    числом в пакете 20.09: поднять при следующей смене судьи (тем же коммитом,
    что и константу); воркерные тесты сравнивают с `g.VERDICT_VERSION`.
    25.09: 3 — массив-улица карты называет пункт клиента, гаражный кооператив
    без слова клиента не его улица (`tests/unit/test_geo_2509.py`)."""
    assert g.VERDICT_VERSION == 3
    assert g.RULE_NEIGHBOUR_SETTLEMENT == "neighbour_settlement"
    assert g.NEIGHBOUR_KM < g.SUBURB_KM


def test_street_in_settlement_без_типа_и_прежние_вызовы() -> None:
    """Прежний вызов сверяет тип улицы, названный клиентом («ул малиновая» ≠
    «пер Малиновый»); сторож соседа зовёт без типа — переулок в пункте тоже
    улика «улица в пункте есть»."""
    с_ул = dataclasses.replace(КЛЕНОВО_МАЛИНОВАЯ, street="ул малиновая")
    переулок = dataclasses.replace(ЭХО_КЛЕНОВО, street="пер Малиновый")
    assert g.street_in_settlement(с_ул, ОРСК, [переулок]) is None
    assert g.street_in_settlement(с_ул, ОРСК, [переулок], с_типом=False) is переулок
    assert g.street_in_settlement(с_ул, ОРСК, [ЭХО_КЛЕНОВО]) is ЭХО_КЛЕНОВО


# ── воркер: стенд на записанных ответах ────────────────────────────────────────


@pytest.fixture
def точка_пункта(monkeypatch: Any) -> dict[str, Any]:
    """`dadata.search_place` подменён: отдаёт `ответ`, считает походы и зовёт
    `on_request` (запрос — из того же потолка DaData)."""
    состояние: dict[str, Any] = {"ответ": [КЛЕНОВО], "походы": []}

    async def search_place(place: g.Place, *, region: str | None, **kw: Any) -> list[g.PlaceHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        состояние["походы"].append((place, region, kw.get("city")))
        return list(состояние["ответ"])

    monkeypatch.setattr(worker.dadata, "search_place", search_place)
    return состояние


async def _уровень(db_sessionmaker: Any, cid: Any, уровень: str) -> None:
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row is not None
        row.level = уровень
        await s.commit()


async def test_воркер_кленово_exact_с_пометкой_вместо_варианта(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    точка_пункта: dict,
) -> None:
    """Живой путь HEAD доводит строку до `elsewhere` с вариантом-соседом; правило
    забирает его: `exact`, `dadata~approx`, строка карты соседа, точка дома,
    вариантов нет, версия судьи 2, автозапись поставлена. Точка пункта спрошена
    один раз — «деревня Кленово» в области объявления."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_ОРСКА
    dadata_отвечает["ответы"] = [[ЛИПОВКА_17]]
    found = _разобрать("деревня Кленово, малиновая 17")
    assert (found.settlement_type, found.level) == ("деревня", "A")
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", found)
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_EXACT,
        "dadata~approx",
        "ул Малиновая, 17, Липовка, Орск",
    )
    assert (row.geo_lat, row.geo_lon) == (ЛИПОВКА_17.lat, ЛИПОВКА_17.lon)
    assert not row.geo_variants and row.geo_verdict_version == g.VERDICT_VERSION
    # Слово клиента о пункте в строке не переписано.
    assert row.settlement == "Кленово"
    [решение] = [л for л in логи if л["event"] == "geocode.auto_decided"]
    assert (решение["rule"], решение["was"], решение["approx"]) == (
        g.RULE_NEIGHBOUR_SETTLEMENT,
        g.GEO_ELSEWHERE,
        True,
    )
    assert 0.9 < решение["rule_km"] < 1.1
    [(place, region, city)] = точка_пункта["походы"]
    assert (place.query_text, region, city) == ("деревня Кленово", ОБЛАСТЬ, None)
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_воркер_дальше_радиуса_как_сегодня(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    точка_пункта: dict,
) -> None:
    """Точка пункта в 5 км — правило молчит честно: `elsewhere`, один вариант
    соседа, карточка пуста; запрос места был (кандидат в уликах лежал)."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_ОРСКА
    точка_пункта["ответ"] = [КЛЕНОВО_5_КМ]
    dadata_отвечает["ответы"] = [[ЛИПОВКА_17]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "orsk", _разобрать("деревня Кленово, малиновая 17")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_ELSEWHERE and row.geo_formatted is None
    assert [v["formatted"] for v in row.geo_variants or []] == ["ул Малиновая, 17, Липовка, Орск"]
    assert row.geo_verdict_version == g.VERDICT_VERSION and len(точка_пункта["походы"]) == 1
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_воркер_два_дома_в_круге_варианты_оператору(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    точка_пункта: dict,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_ОРСКА
    dadata_отвечает["ответы"] = [[ЛИПОВКА_17, ЖУКОВКА_17_ТОЧКА_ПУНКТА]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "orsk", _разобрать("деревня Кленово, малиновая 17")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_ELSEWHERE and len(row.geo_variants or []) == 2
    assert len(точка_пункта["походы"]) == 1


async def test_воркер_слаг_москва_и_мо_без_области_молчит(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_пункта: dict,
) -> None:
    """Скептик 6.2: под слагом «Москва и МО» город в запросе пуст, область не
    ищется, `seen` с эхом улицы пункта не собирается — сторож (в) слеп, и
    правило туда не ходит: точка пункта не спрашивается, `exact` нет."""
    await _режим(db_sessionmaker, "nominatim")
    липовка_мо = dataclasses.replace(
        ЛИПОВКА_17, city="Одинцово", region="Московская обл", lat=55.734924, lon=37.271203
    )
    dadata_отвечает["ответы"] = [[липовка_мо]]
    cid = await _строка(
        seed_conversation,
        db_sessionmaker,
        "moskva_i_mo",
        _разобрать("деревня Кленово, малиновая 17"),
    )
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    assert итог != g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status != g.GEO_EXACT and row.geo_lat is None
    assert точка_пункта["походы"] == []
    assert all(л.get("rule") != g.RULE_NEIGHBOUR_SETTLEMENT for л in логи)
    # Область под слагом не спрашивалась: у всех запросов DaData город пуст, а
    # `near`/`seen` не передавались.
    assert all(q.city is None for q in dadata_отвечает["запросы"])


async def test_воркер_эхо_улицы_пункта_первым_текстом_держит_путь_пскова(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    monkeypatch: Any,
    osm_пусто: Any,
    точка_города: dict,
    точка_пункта: dict,
) -> None:
    """Эхо DaData приходит первым текстом («Кленово малиновая 17») и теряется в
    `hits` за домом второго текста; область складывает его в `seen` — и там
    сторож (в) его видит: улица в Кленово есть → путь владельца 16.09
    (`house_missing`, точка улицы), не сосед. Точка пункта не спрашивается."""
    monkeypatch.setitem(стенд.gateway.known_keys, "dadata", True)
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_ОРСКА
    запросы: list[g.Query] = []

    async def dadata_search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        await kw["on_request"]()
        запросы.append(query)
        seen = kw.get("seen")
        if query.settlement and seen is not None:
            seen.append(ЭХО_КЛЕНОВО)  # текст с пунктом: улица есть, дома нет
        дома = [ЛИПОВКА_17]  # текст без пункта: дом соседа
        if seen is not None:
            seen.extend(дома)
        return дома

    monkeypatch.setattr(worker.dadata, "search", dadata_search)
    cid = await _строка(
        seed_conversation, db_sessionmaker, "orsk", _разобрать("деревня Кленово, малиновая 17")
    )
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    row = await _row(db_sessionmaker, cid)
    assert точка_пункта["походы"] == []
    правила = [л["rule"] for л in логи if л["event"] == "geocode.auto_decided"]
    assert g.RULE_NEIGHBOUR_SETTLEMENT not in правила
    # Итог — путь Пскова: точка улицы в названном пункте, а не дом соседа.
    assert итог == g.GEO_EXACT and правила == [g.RULE_STREET_POINT]
    assert row.geo_formatted is not None and "Кленово" in row.geo_formatted
    assert "Липовка" not in (row.geo_formatted or "")


async def test_воркер_без_кандидата_запрос_места_не_тратится(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    точка_пункта: dict,
) -> None:
    """Итог тот же `elsewhere` с вариантом, но дом — в самом городе объявления:
    кандидата у правила нет, и точка пункта не спрашивается (иначе запрос
    уходил бы на каждую строку с пунктом впустую); итог — как до правила."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_ОРСКА
    dadata_отвечает["ответы"] = [[ОРСК_МАЛИНОВАЯ_17]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "orsk", _разобрать("деревня Кленово, малиновая 17")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert len(row.geo_variants or []) == 1 and точка_пункта["походы"] == []


async def test_воркер_уровень_c_запрос_места_не_тратится(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    точка_пункта: dict,
) -> None:
    """Невод C перебивать слово клиента не вправе: та же константа
    `STREET_EVIDENCE_LEVELS`, что у точки улицы; запрос не уходит."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_ОРСКА
    dadata_отвечает["ответы"] = [[ЛИПОВКА_17]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "orsk", _разобрать("деревня Кленово, малиновая 17")
    )
    await _уровень(db_sessionmaker, cid, "C")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert len(row.geo_variants or []) == 1 and точка_пункта["походы"] == []


async def test_воркер_выключатель_автопривязки_запрос_места_не_тратится(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    точка_пункта: dict,
) -> None:
    """Выключатель выключен — вердикт как до 18.09 (вариант оператору),
    запроса нет."""
    await _режим(db_sessionmaker, "nominatim")
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None)
        await s.commit()
    точка_города["точка"] = ТОЧКА_ОРСКА
    dadata_отвечает["ответы"] = [[ЛИПОВКА_17]]
    cid = await _строка(
        seed_conversation, db_sessionmaker, "orsk", _разобрать("деревня Кленово, малиновая 17")
    )
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert len(row.geo_variants or []) == 1 and точка_пункта["походы"] == []


async def test_воркер_отказ_dadata_на_месте_откладывает_строку(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
    monkeypatch: Any,
) -> None:
    """403 на запросе места — как у точки массива: через шлюз он приходит
    `kind=blocked`, DaData закрыта до московской полуночи (`_учесть_отказ_dadata`
    → `geo:exhausted:dadata`; ревью 20.09, C5), решений «сама» нет, строка
    остаётся вариантом оператору (`elsewhere` — не отказ по содержанию,
    откладывать нечего) с флагом «без DaData»: обход `dadata_back` пересмотрит
    её, когда DaData вернётся."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_ОРСКА
    dadata_отвечает["ответы"] = [[ЛИПОВКА_17]]

    async def отказ(place: g.Place, *, region: str | None, **kw: Any) -> list[g.PlaceHit]:
        raise g.GeocodeError("dadata", "blocked", 403)

    monkeypatch.setattr(worker.dadata, "search_place", отказ)
    cid = await _строка(
        seed_conversation, db_sessionmaker, "orsk", _разобрать("деревня Кленово, малиновая 17")
    )
    assert not await worker._закрыт_на_сутки(redis, "dadata")
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_without_dadata) == (g.GEO_ELSEWHERE, True)
    assert len(row.geo_variants or []) == 1 and row.geo_lat is None
    assert any(л["event"] == "geocode.settlement_place_failed" for л in логи)
    assert all(л["event"] != "geocode.auto_decided" for л in логи)
    assert await worker._закрыт_на_сутки(redis, "dadata")


def test_города_стенда_совпадают_со_справочником() -> None:
    """Стенд считает по слагу `orsk`: если справочник переедет, тесты выше
    должны упасть здесь, а не молча в вердикте."""
    assert ОРСК == City("Орск", ОБЛАСТЬ, "Asia/Yekaterinburg")
