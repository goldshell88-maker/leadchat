"""«Кола победы 4/37» — голова дроби для карт (А) и пункт из первого слова
улицы (Б, правило `settlement_in_street`), владелец 20.09.

Случай: объявление в Мурманске, реплика клиента целиком «Кола победы 4/37»
(уровень B). Разбор берёт «Кола победы» улицей (пункт без типа он видит только
перед типом улицы), DaData по городу молчит, OSM отдаёт улицу Победы в Коле без
дома, Яндекс (для реплики старше недели выключен) — дом «4» в Коле и в
Снежногорске; итог HEAD — `house_missing / nominatim`, карточка пуста. Кола —
город области в 9 км от Мурманска (трасса — docs/47 §4.9).

Два механизма, оба общие:
А — голова дроби «N» для «N/M» с хвостом-квартирой спрашивается у второй карты
    по городу (Яндекс, А1) и у DaData по области кругом 60 км (А2); судит то же
    правило `fraction_head` — ответами города, подсказками из соседних реплик
    (А-4) и радиусной веткой (один дом-голова в 40 км, город улицы не знает).
Б — первое слово улицы без типа читается пунктом ТОЛЬКО по уликам карты: место
    с тем же именем целиком в области (`search_place`), ни одна карта не знает
    улицы с этим словом, дом (или дом-голова дроби) на остатке улицы стоит в
    этом пункте не дальше 40 км. Установленное прочтение закрывает остальные
    правила и строку улицы без точки: они читали бы «Кола» лишним словом улицы.

Каждый сторож получает контрпример из возражений скептика 20.09: дом в «Новая
Кола» (Б-1), провал в дробь по сырому разбору (Б-2), дефис карты (Б-3), город
знает улицу (А-1), угловая нумерация (А-2), `house_mismatch` города (А-3),
запятая (А-4), дубль дома двумя картами (А-5), Апатиты из справочника (Б-6).

Сеть не ходит: карты подменены записанными ответами живого прогона 20.09
(docs/47 §4.9); координаты
городов настоящие (Мурманск 68.9585, 33.0827; Снежногорск 69.1937, 33.2364;
Североморск 69.0769, 33.4167), точка дома в Коле сдвинута, номер дома и
квартиры вымышленные.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from app.integrations import gateway
from app.integrations.avito.listing_url import City, city_by_name
from app.models import ClientAddressCandidate, Message
from app.services import address_parse, app_settings
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit import test_geo_1809 as стенд
from tests.unit.test_autobind_policy_1809 import ОРСК, _evidence
from tests.unit.test_geo_1809 import _row, _разобрать, _режим, _строка, ctx, дом
from tests.unit.test_paket4_neighbour_1909 import _уровень

точка_города = стенд.точка_города
яндекс_отвечает = стенд.яндекс_отвечает
dadata_отвечает = стенд.dadata_отвечает
osm_пусто = стенд.osm_пусто

pytestmark = pytest.mark.anyio

ОБЛАСТЬ = "Мурманская область"
МУРМАНСК = City("Мурманск", ОБЛАСТЬ, "Europe/Moscow")
ТОЧКА_МУРМАНСКА = (68.9585, 33.0827)

#: Ответы карт живого прогона 20.09 (docs/47 §4.9). DaData пишет регион кратко
#: («Мурманская обл»).
КОЛА_4_DADATA = g.GeoHit(
    street="ул Победы",
    house="4",
    settlement=None,
    city="Кола",
    region="Мурманская обл",
    lat=68.88285,
    lon=33.0027,
    house_level=True,
)
СНЕЖНОГОРСК_4 = dataclasses.replace(КОЛА_4_DADATA, city="Снежногорск", lat=69.1937, lon=33.2364)
#: Тот же дом у Яндекса — улица другими словами, точка в десятке метров.
КОЛА_4_ЯНДЕКС = dataclasses.replace(КОЛА_4_DADATA, street="улица Победы", lat=68.8829, lon=33.0028)
КОЛА_УЛИЦА_OSM = g.GeoHit(
    street="улица Победы",
    house=None,
    settlement="Кола",
    city="Кола",
    region=ОБЛАСТЬ,
    lat=68.88,
    lon=33.01,
    house_level=False,
)
МЕСТО_КОЛА = g.PlaceHit(
    name="г Кола",
    kind="city",
    settlement=None,
    area=None,
    city="Кола",
    district=None,
    region=ОБЛАСТЬ,
    lat=68.88,
    lon=33.02,
)
#: Контрпримеры: пункт-двойник по слову (183 км), Кола за 45 км, дом «4» и
#: улица в самом Мурманске, литеральный «4/37» в Североморске (18,7 км) и в
#: Кандалакше (200 км).
НОВАЯ_КОЛА_4 = dataclasses.replace(
    КОЛА_4_DADATA, settlement="Новая Кола", city="Новая Кола", lat=67.5, lon=31.0
)
#: Тот же двойник по слову в 7 км от Мурманска — расстояние его не отсекает,
#: отсекает только имя целиком.
НОВАЯ_КОЛА_4_РЯДОМ = dataclasses.replace(НОВАЯ_КОЛА_4, lat=68.90, lon=33.20)
КОЛА_4_ЗА_45_КМ = dataclasses.replace(КОЛА_4_DADATA, lat=68.56, lon=33.0)
МУРМАНСК_ПОБЕДЫ_4 = dataclasses.replace(КОЛА_4_DADATA, city="Мурманск", lat=68.96, lon=33.08)
МУРМАНСК_УЛ_ПОБЕДЫ = g.GeoHit(
    street="ул Победы",
    house=None,
    settlement=None,
    city="Мурманск",
    region="Мурманская обл",
    lat=68.96,
    lon=33.08,
    house_level=False,
    precise=False,
)
СЕВЕРОМОРСК_4_37 = dataclasses.replace(
    КОЛА_4_DADATA, house="4/37", city="Североморск", lat=69.0769, lon=33.4167
)
КАНДАЛАКША_4_37 = dataclasses.replace(
    КОЛА_4_DADATA, house="4/37", city="Кандалакша", lat=67.1517, lon=32.4125
)

КОЛА_ПОБЕДЫ = g.Parsed(street="Кола победы", house="4/37", level="B")
ПОБЕДЫ = g.Parsed(street="Победы", house="4/37", level="B")


def _улики(**kw: Any) -> g.Evidence:
    """Улики строки владельца после всей цепочки: DaData по городу пусто, OSM —
    улица в Коле без дома, голова по городу пуста, область (А2) — дом «4» в
    Коле и в Снежногорске, место «Кола» спрошено."""
    база: dict[str, Any] = {
        "status": g.GEO_HOUSE_MISSING,
        "parsed": КОЛА_ПОБЕДЫ,
        "city": МУРМАНСК,
        "hits": [КОЛА_УЛИЦА_OSM],
        "city_hits": [([], "dadata"), ([КОЛА_УЛИЦА_OSM], "nominatim")],
        "region_hits": [КОЛА_4_DADATA, СНЕЖНОГОРСК_4],
        "region_provider": "dadata",
        "fraction_hits": ([], "dadata"),
        "city_point": ТОЧКА_МУРМАНСКА,
        "street_place_hits": [МЕСТО_КОЛА],
    }
    база.update(kw)
    return _evidence(**база)


def _решение(**kw: Any) -> g.Decision | None:
    return g.auto_decide(_улики(**kw))


# ── чистые помощники ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("хвост", "квартира"),
    [("37", "37"), ("10", "10"), ("2", None), ("09", None), ("2/3", None), ("а", None)],
)
def test_flat_from_fraction(хвост: str, квартира: str | None) -> None:
    assert g.flat_from_fraction(хвост) == квартира


НОВОЧЕРКАССК = City("Новочеркасск", "Оренбургская область", "Europe/Moscow")


def test_corner_numbering_по_ответам_на_улице() -> None:
    """Другой дом с дробью на улице клиента — угловая нумерация; та же дробь
    или дробь на другой улице — нет."""
    атаманская = g.Parsed(street="Атаманская", house="18/64", level="B")
    сосед = дом(street="ул Атаманская", house="16/5", city="Новочеркасск")
    assert g.corner_numbering(атаманская, НОВОЧЕРКАССК, [сосед])
    assert not g.corner_numbering(
        атаманская, НОВОЧЕРКАССК, [dataclasses.replace(сосед, house="18/64")]
    )
    assert not g.corner_numbering(
        атаманская, НОВОЧЕРКАССК, [dataclasses.replace(сосед, street="ул Ленина")]
    )
    assert not g.corner_numbering(
        атаманская, НОВОЧЕРКАССК, [dataclasses.replace(сосед, house="16")]
    )
    assert not g.corner_numbering(атаманская, НОВОЧЕРКАССК, [])
    assert not g.corner_numbering(атаманская, None, [сосед])


def test_corner_numbering_только_в_месте_клиента() -> None:
    """C4: «Атаманская, 16/5» в ДРУГОМ городе области (Яндекс по городу и
    область отдают дома всего региона) о нумерации улицы клиента не говорит;
    в другой области — тем более. Клиент назвал пункт — считаются дома в нём,
    а не в самом городе; назвал город словом — в нём."""
    атаманская = g.Parsed(street="Атаманская", house="18/64", level="B")
    сосед = дом(street="ул Атаманская", house="16/5", city="Новочеркасск")
    в_чужом_городе = dataclasses.replace(сосед, city="Орск")
    assert not g.corner_numbering(атаманская, НОВОЧЕРКАССК, [в_чужом_городе])
    assert not g.corner_numbering(
        атаманская, НОВОЧЕРКАССК, [dataclasses.replace(сосед, region="Самарская область")]
    )
    # Дом в пункте города («мкр Донской» Новочеркасска) — тот же город.
    assert g.corner_numbering(
        атаманская, НОВОЧЕРКАССК, [dataclasses.replace(сосед, settlement="Донской")]
    )
    в_пункте = dataclasses.replace(атаманская, settlement="Кривянская")
    станица = dataclasses.replace(сосед, settlement="Кривянская", city=None)
    assert g.corner_numbering(в_пункте, НОВОЧЕРКАССК, [станица])
    assert not g.corner_numbering(в_пункте, НОВОЧЕРКАССК, [сосед])
    с_городом = dataclasses.replace(атаманская, locality="Орск")
    assert g.corner_numbering(с_городом, НОВОЧЕРКАССК, [в_чужом_городе])
    assert not g.corner_numbering(с_городом, НОВОЧЕРКАССК, [сосед])


@pytest.mark.parametrize(
    ("улица", "город", "ожидание"),
    [
        ("Кола победы", МУРМАНСК, ("Кола", "победы")),
        ("кола победы", МУРМАНСК, ("кола", "победы")),
        ("Усть-Кут победы", МУРМАНСК, ("Усть-Кут", "победы")),
        ("Красная площадь", МУРМАНСК, None),
        ("Мира", МУРМАНСК, None),
        ("40 лет Октября", МУРМАНСК, None),
        ("ул Победы", МУРМАНСК, None),
        ("Толстого Победы", МУРМАНСК, None),
        ("Мурманск победы", МУРМАНСК, None),
        ("Апатиты победы", МУРМАНСК, None),
        ("улице лесной", МУРМАНСК, None),
        ("Мой адрес", МУРМАНСК, None),
        ("Кола 6", МУРМАНСК, None),
        ("Ко победы", МУРМАНСК, None),
    ],
)
def test_street_head_as_settlement(
    улица: str, город: City, ожидание: tuple[str, str] | None
) -> None:
    assert g.street_head_as_settlement(g.Parsed(street=улица, house="4/37"), город) == ожидание


def test_street_head_as_settlement_молчит_при_названном_месте() -> None:
    """Пункт, город или массив назван клиентом — читать нечего."""
    assert g.street_head_as_settlement(КОЛА_ПОБЕДЫ, МУРМАНСК) == ("Кола", "победы")
    for поле in ("settlement", "locality", "area"):
        с_местом = dataclasses.replace(КОЛА_ПОБЕДЫ, **{поле: "Мурмаши"})
        assert g.street_head_as_settlement(с_местом, МУРМАНСК) is None, поле


def test_is_place_name_одно_определение() -> None:
    assert address_parse.is_place_name("Кола") and address_parse.is_place_name("Нижний Тагил")
    for речь in ("улице", "Приедете улицу", "Здравствуйте", "Мой адрес"):
        assert not address_parse.is_place_name(речь), речь
    # Подсказки карте живут тем же определением: «улице» подсказкой не идёт.
    assert address_parse.settlement_hints(["улице, Лесная 17"]) == []


def test_street_names_word() -> None:
    пресня = дом(street="улица Красная Пресня", house="5")
    assert g.street_names_word("Красная", [пресня])
    assert not g.street_names_word("Кола", [КОЛА_УЛИЦА_OSM, КОЛА_4_DADATA])
    assert not g.street_names_word("Кола", [дом(street="Кольский проспект")])
    # Дефис карты (Б-3): «Стара Загора» ↔ «ул Стара-Загора».
    assert g.street_names_word("Стара", [дом(street="ул Стара-Загора", city="Самара")])
    assert not g.street_names_word("Кола", [dataclasses.replace(КОЛА_4_DADATA, street=None)])
    assert not g.street_names_word("", [пресня])


def test_fraction_head_in_hits_и_без_дублей_пятна() -> None:
    assert g.fraction_head_in_hits(КОЛА_ПОБЕДЫ, МУРМАНСК, [КОЛА_4_DADATA])
    assert not g.fraction_head_in_hits(КОЛА_ПОБЕДЫ, МУРМАНСК, [КОЛА_УЛИЦА_OSM])
    assert not g.fraction_head_in_hits(
        g.Parsed(street="Победы", house="4"), МУРМАНСК, [КОЛА_4_DADATA]
    )
    assert not g.fraction_head_in_hits(КОЛА_ПОБЕДЫ, None, [КОЛА_4_DADATA])
    assert g._без_дублей_пятна([КОЛА_4_DADATA, КОЛА_4_ЯНДЕКС]) == [КОЛА_4_DADATA]
    assert g._без_дублей_пятна([КОЛА_4_DADATA, СНЕЖНОГОРСК_4]) == [КОЛА_4_DADATA, СНЕЖНОГОРСК_4]
    assert g._без_дублей_пятна([]) == []


# ── правило (10): прочтение первого слова улицы пунктом ────────────────────────


def test_прочтение_голова_дроби_в_пункте_решает_с_квартирой() -> None:
    """Строка владельца: прочтение «Кола» + «победы», дом-голова «4» в Коле по
    области → правило дроби с прочтением: номер клиента, кв 37, `~approx`,
    DaData, 9 км до Мурманска; Снежногорск отсечён именем пункта."""
    решение = _решение()
    assert решение is not None
    assert (решение.rule, решение.hit.house, решение.office, решение.approx, решение.provider) == (
        g.RULE_FRACTION_HEAD,
        "4/37",
        "37",
        True,
        "dadata",
    )
    assert (решение.hit.city, решение.hit.lat, решение.hit.lon) == ("Кола", 68.88285, 33.0027)
    assert (решение.parsed.settlement, решение.parsed.street, решение.parsed.house) == (
        "Кола",
        "победы",
        "4/37",
    )
    assert решение.km is not None and 8.9 < решение.km < 9.1
    assert g.format_address(решение.hit, решение.parsed) == "ул Победы, 4/37, Кола"


def test_прочтение_дом_целиком_в_пункте() -> None:
    решение = _решение(region_hits=[dataclasses.replace(КОЛА_4_DADATA, house="4/37")])
    assert решение is not None
    assert (решение.rule, решение.hit.house, решение.office, решение.approx, решение.provider) == (
        g.RULE_SETTLEMENT_IN_STREET,
        "4/37",
        None,
        True,
        "dadata",
    )
    assert решение.parsed.settlement == "Кола" and решение.km is not None and решение.km < 10
    # Дом целиком в пункте — и по ответам города (OSM отдал дом в Коле).
    решение = _решение(
        region_hits=[],
        city_hits=[
            ([], "dadata"),
            ([dataclasses.replace(КОЛА_УЛИЦА_OSM, house="4/37", house_level=True)], "nominatim"),
        ],
    )
    assert решение is not None and решение.rule == g.RULE_SETTLEMENT_IN_STREET
    assert решение.provider == "nominatim"


#: Второй пункт области с тем же именем целиком — посёлок Кола (`settlement`,
#: без города) в 13 км от Мурманска: с тем же домом на той же улице — соперник
#: города Кола, и различить их словом клиента нельзя.
ПОСЁЛОК_КОЛА_4 = dataclasses.replace(
    КОЛА_4_DADATA, settlement="Кола", city=None, lat=69.05, lon=33.30
)


def test_прочтение_соперники_по_объединённому_пулу_источников() -> None:
    """D1: два одноимённых пункта области с домом-головой «4» на Победы — город
    Кола от DaData по области и посёлок Кола от OSM по городу. Каждый источник
    порознь видит один дом и дал бы `exact`; вместе — двое, и правило молчит.
    То же для дома целиком (ветка (i)) и для обоих домов в одном источнике.
    Дубль одного дома у двух карт соперником не считается (`_без_дублей_пятна`)."""
    порознь = {
        "region_hits": [КОЛА_4_DADATA],
        "city_hits": [([], "dadata"), ([ПОСЁЛОК_КОЛА_4], "nominatim")],
    }
    assert _решение(**порознь) is None
    assert _решение(region_hits=[КОЛА_4_DADATA, ПОСЁЛОК_КОЛА_4]) is None
    целиком = {
        "region_hits": [dataclasses.replace(КОЛА_4_DADATA, house="4/37")],
        "city_hits": [
            ([], "dadata"),
            ([dataclasses.replace(ПОСЁЛОК_КОЛА_4, house="4/37")], "nominatim"),
        ],
    }
    assert _решение(**целиком) is None
    # Контроль: каждый из соперников в одиночку решает — и по области, и по городу.
    for источник in (порознь, целиком):
        только_область = _решение(**dict(источник, city_hits=[([], "dadata")]))
        assert только_область is not None and только_область.hit.city == "Кола"
        только_город = _решение(**dict(источник, region_hits=[]))
        assert только_город is not None and только_город.hit.settlement == "Кола"
        assert только_город.provider == "nominatim"
    # Тот же дом у DaData (область) и Яндекса (город) — один дом, решение есть.
    дубль = _решение(
        region_hits=[КОЛА_4_DADATA],
        city_hits=[([], "dadata"), ([КОЛА_4_ЯНДЕКС], "yandex")],
    )
    assert дубль is not None and дубль.rule == g.RULE_FRACTION_HEAD and дубль.office == "37"
    assert 12 < g.distance_km(ТОЧКА_МУРМАНСКА, (ПОСЁЛОК_КОЛА_4.lat, ПОСЁЛОК_КОЛА_4.lon)) < 14


def test_прочтение_без_дома_в_пункте_закрывает_дробь_по_сырому_разбору() -> None:
    """Б-2: место «Кола» есть, дома в Коле нет, а дом «4» на Победы — в самом
    Мурманске. Прочтение установлено → решения нет; без места та же строка
    решалась бы дробью в Мурманске с кв 37 — то самое прочтение «лишнее слово
    улицы», которое слово «Кола» опровергает."""
    город_знает = {
        "status": g.GEO_HOUSE_MISMATCH,
        "hits": [МУРМАНСК_ПОБЕДЫ_4],
        "city_hits": [([МУРМАНСК_ПОБЕДЫ_4], "dadata"), ([], "nominatim")],
        "region_hits": [],
        "fraction_hits": None,
    }
    assert _решение(**город_знает) is None
    assert g.street_head_reading(_улики(**город_знает)) == ("Кола", "победы")
    сегодня = _решение(**город_знает, street_place_hits=None)
    assert сегодня is not None and сегодня.rule == g.RULE_FRACTION_HEAD
    assert сегодня.hit.city == "Мурманск" and сегодня.office == "37"
    # Место спрошено, но пункта «Кола» в ответе нет — прочтения нет, путь прежний.
    без_пункта = _решение(**город_знает, street_place_hits=[])
    assert без_пункта is not None and без_пункта.rule == g.RULE_FRACTION_HEAD
    # `elsewhere`: литеральный «4/37» в Североморске (18,7 км) и в Кандалакше
    # (200 км) без прочтения решал бы `only_in_radius`; с прочтением — стоп.
    в_другом_месте = {
        "status": g.GEO_ELSEWHERE,
        "hits": [],
        "city_hits": [([], "dadata"), ([], "nominatim")],
        "region_hits": [СЕВЕРОМОРСК_4_37, КАНДАЛАКША_4_37],
        "fraction_hits": ([], "dadata"),
        "variants": g.variants(КОЛА_ПОБЕДЫ, МУРМАНСК, [СЕВЕРОМОРСК_4_37, КАНДАЛАКША_4_37]),
    }
    assert _решение(**в_другом_месте) is None
    радиус = _решение(**в_другом_месте, street_place_hits=None)
    assert радиус is not None and радиус.rule == g.RULE_ONLY_IN_RADIUS
    assert радиус.hit.city == "Североморск"
    # А дом «4/37» в самой Коле среди тех же вариантов — прочтение (i).
    с_колой = dict(
        в_другом_месте,
        region_hits=[СЕВЕРОМОРСК_4_37, dataclasses.replace(КОЛА_4_DADATA, house="4/37")],
    )
    решение = _решение(**с_колой)
    assert решение is not None and решение.rule == g.RULE_SETTLEMENT_IN_STREET
    assert решение.hit.city == "Кола"


@pytest.mark.parametrize(
    ("имя", "kw"),
    [
        ("места не спрашивали", {"street_place_hits": None}),
        (
            "место — улица",
            {"street_place_hits": [dataclasses.replace(МЕСТО_КОЛА, kind="street", city=None)]},
        ),
        (
            "место — массив",
            {
                "street_place_hits": [
                    dataclasses.replace(МЕСТО_КОЛА, kind="area", city=None, area="СНТ Кола")
                ]
            },
        ),
        (
            "место в другой области",
            {
                "street_place_hits": [
                    dataclasses.replace(МЕСТО_КОЛА, region="Ленинградская область")
                ]
            },
        ),
        (
            "имя не целиком (Колла)",
            {"street_place_hits": [dataclasses.replace(МЕСТО_КОЛА, city="Колла")]},
        ),
        (
            "имя не целиком (Новая Кола)",
            {"street_place_hits": [dataclasses.replace(МЕСТО_КОЛА, city="Новая Кола")]},
        ),
        ("дом в пункте-двойнике по слову (Б-1)", {"region_hits": [НОВАЯ_КОЛА_4]}),
        ("дом в пункте-двойнике по слову рядом (Б-1)", {"region_hits": [НОВАЯ_КОЛА_4_РЯДОМ]}),
        (
            "дом целиком в пункте-двойнике рядом (Б-1)",
            {"region_hits": [dataclasses.replace(НОВАЯ_КОЛА_4_РЯДОМ, house="4/37")]},
        ),
        ("дом в Коле за 45 км (Б-1)", {"region_hits": [КОЛА_4_ЗА_45_КМ]}),
        ("точки города нет", {"city_point": None}),
        ("уровень C", {"parsed": dataclasses.replace(КОЛА_ПОБЕДЫ, level="C")}),
        (
            "слово в улице карты",
            {"city_hits": [([дом(street="улица Кола", house="3", city="Мурманск")], "dadata")]},
        ),
        (
            "слово в улице карты с дефисом (Б-3)",
            {"city_hits": [([дом(street="ул Кола-Победы", house="3", city="Мурманск")], "dadata")]},
        ),
        (
            "пункт назван клиентом",
            {"parsed": dataclasses.replace(КОЛА_ПОБЕДЫ, settlement="Мурмаши")},
        ),
        ("тип улицы", {"parsed": dataclasses.replace(КОЛА_ПОБЕДЫ, street="Кола площадь")}),
        ("одно слово", {"parsed": ПОБЕДЫ}),
        (
            "остаток улицы не сошёлся в пункте",
            {"region_hits": [dataclasses.replace(КОЛА_4_DADATA, street="ул Мира")]},
        ),
        (
            "дом-голова в пункте есть, а сама дробь у карты — другая улица",
            {"region_hits": [dataclasses.replace(КОЛА_4_DADATA, street="ул Мира", house="4/37")]},
        ),
        ("итог — ответ на месте", {"status": g.GEO_SETTLEMENT_MISMATCH}),
    ],
)
def test_прочтение_сторожа(имя: str, kw: dict[str, Any]) -> None:
    """Любой сторож снят — молчание, а не «почти»: без места, место не пункт,
    другая область, имя не целиком, дом не в пункте или дальше пригорода, без
    точки города, невод C, слово в улице карты, названное место, тип улицы."""
    assert _решение(**kw) is None, имя


def test_прочтение_сторожа_не_ломают_строку_владельца() -> None:
    """Контроль параметров выше: та же база без подмены решает."""
    assert _решение() is not None
    # `street_mismatch` A/B без спора — тот же вход, что `house_missing` (N24).
    решение = _решение(status=g.GEO_STREET_MISMATCH)
    assert решение is not None and решение.rule == g.RULE_FRACTION_HEAD
    assert (
        _решение(status=g.GEO_STREET_MISMATCH, parsed=dataclasses.replace(КОЛА_ПОБЕДЫ, level="C"))
        is None
    )


def test_street_head_candidate_и_reading_на_уликах() -> None:
    """Гейт воркера и правило — одна функция: кандидат без места, прочтение —
    с местом; пул сторожа (в) — город + голова + область."""
    без_места = _улики(street_place_hits=None)
    assert g.street_head_candidate(без_места) == ("Кола", "победы")
    assert g.street_head_reading(без_места) is None
    assert g.street_head_reading(_улики()) == ("Кола", "победы")
    # Слово в ответе головы по городу тоже глушит кандидата (Б-4).
    с_улицей_в_голове = _улики(
        fraction_hits=([дом(street="ул Кола", house="4", city="Мурманск")], "dadata")
    )
    assert g.street_head_candidate(с_улицей_в_голове) is None
    assert g.street_head_candidate(_улики(city=None)) is None


# ── правило дроби: подсказки, угловая нумерация, радиусная ветка ────────────────


def test_fraction_head_с_подсказкой_из_соседней_реплики() -> None:
    """А-4: «Кола, победы 4/37» — улица из одного слова, «Кола» подсказкой.
    Головы «4» по области две (Кола и Снежногорск) — без подсказки монета,
    с подсказкой — Кола, и прочтение уезжает в `parsed`."""
    без = {"parsed": ПОБЕДЫ, "street_place_hits": None, "city_hits": [([], "dadata")], "hits": []}
    assert _решение(**без) is None
    решение = _решение(**без, hints=["Кола"])
    assert решение is not None and решение.rule == g.RULE_FRACTION_HEAD
    assert (решение.hit.city, решение.office, решение.parsed.settlement) == ("Кола", "37", "Кола")
    assert g.format_address(решение.hit, решение.parsed) == "ул Победы, 4/37, Кола"
    # Слова клиента как написаны — первыми: дом «4» в самом городе выигрывает у подсказки.
    в_городе = _решение(**dict(без, city_hits=[([МУРМАНСК_ПОБЕДЫ_4], "dadata")]), hints=["Кола"])
    assert в_городе is not None and в_городе.hit.city == "Мурманск"
    assert в_городе.parsed.settlement is None


def test_fraction_head_угловая_нумерация_без_квартиры() -> None:
    """А-2: на Атаманской у карты «16/5» — «18/64» не дом 18 с квартирой 64;
    дом-голова остаётся точкой (то же здание), `office` не пишется."""
    атаманская = g.Parsed(street="Атаманская", house="18/64", level="B")
    дом_18 = дом(street="ул Атаманская", house="18", city="Новочеркасск")
    сосед = dataclasses.replace(дом_18, house="16/5")
    база = {
        "parsed": атаманская,
        "city": НОВОЧЕРКАССК,
        "status": g.GEO_HOUSE_MISMATCH,
        "street_place_hits": None,
        "region_hits": [],
        "fraction_hits": None,
        "city_point": None,
    }
    с_соседом = _решение(**база, hits=[дом_18, сосед], city_hits=[([дом_18, сосед], "dadata")])
    assert с_соседом is not None and с_соседом.rule == g.RULE_FRACTION_HEAD
    assert (с_соседом.hit.house, с_соседом.office) == ("18/64", None)
    без_соседа = _решение(**база, hits=[дом_18], city_hits=[([дом_18], "dadata")])
    assert без_соседа is not None and без_соседа.office == "64"
    # C4: «16/5» на Атаманской в ДРУГОМ городе области — в ответах области и у
    # Яндекса по городу — квартиру не гасит: голова по городу решает с кв 64.
    чужой = dataclasses.replace(сосед, city="Орск", lat=51.4)
    в_чужом = _решение(
        **dict(база, region_hits=[чужой]),
        hits=[дом_18],
        city_hits=[([дом_18], "dadata"), ([чужой], "yandex")],
    )
    assert в_чужом is not None and (в_чужом.hit.city, в_чужом.office) == ("Новочеркасск", "64")


def test_fraction_head_область_радиусом() -> None:
    """А2 без слова клиента: единственный дом-голова области в 40 км от города
    → дробь с `~approx`, без квартиры, с `km`; двое в радиусе — None."""
    база = {"parsed": ПОБЕДЫ, "street_place_hits": None, "city_hits": [([], "dadata")], "hits": []}
    решение = _решение(**база, region_hits=[КОЛА_4_DADATA])
    assert решение is not None and решение.rule == g.RULE_FRACTION_HEAD
    assert (решение.hit.city, решение.hit.house, решение.office, решение.approx) == (
        "Кола",
        "4/37",
        None,
        True,
    )
    assert решение.provider == "dadata" and решение.km is not None and 8.9 < решение.km < 9.1
    assert решение.parsed is ПОБЕДЫ
    assert _решение(**база, region_hits=[КОЛА_4_DADATA, СНЕЖНОГОРСК_4]) is None
    assert _решение(**база, region_hits=[КОЛА_4_ЗА_45_КМ]) is None
    assert _решение(**база, region_hits=[КОЛА_4_DADATA], city_point=None) is None
    assert _решение(**dict(база, region_hits=[])) is None
    # Уровень C — геометрия не для невода (А-1).
    assert (
        _решение(
            **dict(база, parsed=dataclasses.replace(ПОБЕДЫ, level="C")), region_hits=[КОЛА_4_DADATA]
        )
        is None
    )


def test_fraction_head_радиус_молчит_когда_город_знает_улицу() -> None:
    """А-1: «Седова 3/14» в Сызрани — город знает улицу, дома 3 нет; голова
    одна в круге → не дом-голова в чужом пункте, а точка своей улицы."""
    база = {"parsed": ПОБЕДЫ, "street_place_hits": None, "hits": [МУРМАНСК_УЛ_ПОБЕДЫ]}
    решение = _решение(
        **база, city_hits=[([МУРМАНСК_УЛ_ПОБЕДЫ], "dadata")], region_hits=[КОЛА_4_DADATA]
    )
    assert решение is not None and решение.rule == g.RULE_STREET_POINT
    assert решение.hit.city == "Мурманск"
    # Улица города в ответах области — та же улика.
    решение = _решение(
        **база, city_hits=[([], "dadata")], region_hits=[КОЛА_4_DADATA, МУРМАНСК_УЛ_ПОБЕДЫ]
    )
    assert решение is not None and решение.rule == g.RULE_STREET_POINT


def test_fraction_head_радиус_склеивает_дубль_двух_карт() -> None:
    """А-5: тот же дом DaData и Яндекса в круге — решение, не монета; карта —
    по объекту (А-7)."""
    база = {"parsed": ПОБЕДЫ, "street_place_hits": None, "city_hits": [([], "dadata")], "hits": []}
    решение = _решение(**база, region_hits=[КОЛА_4_DADATA, КОЛА_4_ЯНДЕКС], region_provider="dadata")
    assert решение is not None and решение.rule == g.RULE_FRACTION_HEAD
    assert решение.hit.street == "ул Победы" and решение.provider == "dadata"
    assert not g._одно_пятно([КОЛА_4_DADATA, СНЕЖНОГОРСК_4])


def test_прежнее_правило_дроби_и_семьи_не_тронуто() -> None:
    """Контракт 18.09: голова по городу решает с квартирой; «185/4» без
    квартиры; дома нет — None (те же ожидания, что в `test_autobind_policy_1809`)."""
    parsed = g.Parsed(street="ул Ленина", house="13/38")
    дом_13 = дом(house="13")
    решение = g.auto_decide(
        _evidence(
            status=g.GEO_HOUSE_MISMATCH,
            parsed=parsed,
            hits=[дом_13],
            city_hits=[([дом_13], "dadata")],
        )
    )
    assert решение is not None and (решение.rule, решение.office) == (g.RULE_FRACTION_HEAD, "38")
    assert решение.provider == "dadata" and решение.km is None
    решение = g.auto_decide(
        _evidence(
            status=g.GEO_NOT_FOUND,
            parsed=parsed,
            city_hits=[([], "dadata")],
            fraction_hits=([дом_13], "dadata"),
        )
    )
    assert решение is not None and решение.provider == "dadata"
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


def test_версия_судьи_не_поднята_второй_раз() -> None:
    """А и Б идут тем же пакетом, что сосед за забором: версия уже поднята им
    (число закреплено одним assert в `test_paket4_neighbour_1909`)."""
    assert g.VERDICT_VERSION >= 2
    assert g.RULE_SETTLEMENT_IN_STREET == "settlement_in_street"
    assert g.STREET_HEAD_STATUSES == frozenset(
        {g.GEO_NOT_FOUND, g.GEO_HOUSE_MISSING, g.GEO_HOUSE_MISMATCH, g.GEO_ELSEWHERE}
    )
    assert МУРМАНСК == city_by_name("Мурманск") and city_by_name("Кола") is None
    assert city_by_name("Апатиты") is not None


# ── воркер: стенд на записанных ответах ────────────────────────────────────────


@pytest.fixture
def dadata_очередь(monkeypatch: Any) -> dict[str, Any]:
    """DaData на шлюзе с ключом: `ответы` — по порядку походов (последний
    повторяется); каждый поход записан вместе с `near` — по нему видно, что
    голова по области ушла кругом вокруг города."""
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    состояние: dict[str, Any] = {"ответы": [[]], "походы": []}

    async def search(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        состояние["походы"].append((query, kw.get("near")))
        ответы = состояние["ответы"]
        ответ = ответы.pop(0) if len(ответы) > 1 else ответы[0]
        if kw.get("seen") is not None:
            kw["seen"].extend(ответ)
        return list(ответ)

    monkeypatch.setattr(worker.dadata, "search", search)
    return состояние


@pytest.fixture
def osm_отвечает(monkeypatch: Any) -> dict[str, Any]:
    состояние: dict[str, Any] = {"ответ": [КОЛА_УЛИЦА_OSM], "запросы": []}

    async def search(query: g.Query, wait: Any = None, **kw: Any) -> list[g.GeoHit]:
        состояние["запросы"].append(query)
        if wait is not None:
            await wait()
        return list(состояние["ответ"])

    monkeypatch.setattr(worker.nominatim, "search", search)
    return состояние


@pytest.fixture
def место_отвечает(monkeypatch: Any) -> dict[str, Any]:
    состояние: dict[str, Any] = {"ответ": [МЕСТО_КОЛА], "походы": []}

    async def search_place(place: g.Place, *, region: str | None, **kw: Any) -> list[g.PlaceHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        состояние["походы"].append((place, region, kw.get("city")))
        return list(состояние["ответ"])

    monkeypatch.setattr(worker.dadata, "search_place", search_place)
    return состояние


async def _тело_реплики(db_sessionmaker: Any, seed: Any, текст: str) -> None:
    """Реплика клиента целиком — как в бою: стенд `_строка` кладёт в тело
    цитату разбора (`found.raw`), а имя перед запятой разбор в цитату не берёт,
    и подсказка «Кола» из неё пропала бы."""
    async with db_sessionmaker() as s:
        сообщение = (
            await s.execute(sa.select(Message).where(Message.id == seed.message_id))
        ).scalar_one()
        сообщение.body = текст
        await s.commit()


async def _состарить(db_sessionmaker: Any, cid: Any, дней: int = 10) -> None:
    """Реплика старше недели: Яндекс и Саджест воркер не спрашивает."""
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        assert row is not None
        row.message_at = datetime.now(UTC) - timedelta(days=дней)
        await s.commit()


async def _строка_колы(seed: Any, db_sessionmaker: Any, текст: str = "Кола победы 4/37") -> Any:
    found = _разобрать(текст)
    cid = await _строка(seed, db_sessionmaker, "murmansk", found)
    await _уровень(db_sessionmaker, cid, "B")
    return cid


def _походы(dadata_очередь: dict[str, Any]) -> list[tuple[str, str | None, bool]]:
    return [(q.house, q.city, near is not None) for q, near in dadata_очередь["походы"]]


async def test_воркер_кола_победы_4_37_exact_с_квартирой(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    яндекс_отвечает: dict,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    """Трасса строки a1b2c3d4 (реплика старше недели — Яндекс выключен): DaData
    по городу пусто на «4/37» и на голову «4», OSM — улица в Коле без дома,
    область пуста, А2 головой кругом → «г Кола, ул Победы, д 4», место «Кола»
    → правило дроби с прочтением: `exact`, `dadata~approx`, «ул Победы, 4/37,
    Кола», кв 37, точка дома, вариантов нет, версия 2; `street`/`settlement`
    строки не переписаны. Походы DaData — ровно пять, по порядку."""
    await _режим(db_sessionmaker, "osm_then_yandex")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    dadata_очередь["ответы"] = [[], [], [], [], [КОЛА_4_DADATA]]
    cid = await _строка_колы(seed_conversation, db_sessionmaker)
    await _состарить(db_sessionmaker, cid)
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted, row.office) == (
        g.GEO_EXACT,
        "dadata~approx",
        "ул Победы, 4/37, Кола",
        "37",
    )
    assert (row.geo_lat, row.geo_lon) == (КОЛА_4_DADATA.lat, КОЛА_4_DADATA.lon)
    assert not row.geo_variants and row.geo_verdict_version == g.VERDICT_VERSION
    assert (row.street, row.settlement) == ("Кола победы", None)
    [решение] = [л for л in логи if л["event"] == "geocode.auto_decided"]
    assert (решение["rule"], решение["was"], решение["approx"], решение["settlement_read"]) == (
        g.RULE_FRACTION_HEAD,
        g.GEO_HOUSE_MISSING,
        True,
        "Кола",
    )
    assert 8.9 < решение["rule_km"] < 9.1
    # Город «4/37», голова по городу «4», круг, без круга, голова по области кругом.
    assert _походы(dadata_очередь) == [
        ("4/37", "Мурманск", False),
        ("4", "Мурманск", False),
        ("4/37", None, True),
        ("4/37", None, False),
        ("4", None, True),
    ]
    запрос_а2, near = dadata_очередь["походы"][4]
    assert запрос_а2 == g.Query(
        region=ОБЛАСТЬ, city=None, settlement=None, street="Кола победы", house="4"
    )
    assert near == ТОЧКА_МУРМАНСКА
    assert яндекс_отвечает["запросы"] == []
    [(place, region, city)] = место_отвечает["походы"]
    assert (place.query_text, region, city) == ("Кола", ОБЛАСТЬ, None)
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_воркер_а2_не_ходит_когда_голова_уже_в_области(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    """DaData на «4/37» кругом сама отдала «д 4» соседом — голова уже в ответах
    области, поход головой не тратится; итог тот же."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    dadata_очередь["ответы"] = [[], [], [КОЛА_4_DADATA]]
    cid = await _строка_колы(seed_conversation, db_sessionmaker)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_formatted, row.office, row.geo_provider) == (
        "ул Победы, 4/37, Кола",
        "37",
        "dadata~approx",
    )
    assert _походы(dadata_очередь) == [
        ("4/37", "Мурманск", False),
        ("4", "Мурманск", False),
        ("4/37", None, True),
        ("4/37", None, False),
    ]
    assert len(место_отвечает["походы"]) == 1


async def test_воркер_14_2_голова_по_области_не_спрашивается(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    """«Кола победы 14/2»: хвост — корпус, не квартира (`flat_from_fraction`),
    А2 не ходит; голова по городу — как сегодня; место спрошено, дома «14» в
    Коле нет → прочтение без дома: `house_missing`, карточка пуста."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    cid = await _строка_колы(seed_conversation, db_sessionmaker, "Кола победы 14/2")
    with structlog.testing.capture_logs() as логи:
        assert (
            await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_HOUSE_MISSING
        )
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_formatted, row.office, row.geo_lat) == (
        g.GEO_HOUSE_MISSING,
        None,
        None,
        None,
    )
    assert _походы(dadata_очередь) == [
        ("14/2", "Мурманск", False),
        ("14", "Мурманск", False),
        ("14/2", None, True),
        ("14/2", None, False),
    ]
    assert len(место_отвечает["походы"]) == 1
    assert all(л["event"] != "geocode.auto_decided" for л in логи)
    [нерешено] = [л for л in логи if л["event"] == "geocode.street_head_unresolved"]
    assert нерешено["settlement"] == "Кола"


@pytest.mark.parametrize("место", [[МЕСТО_КОЛА], []], ids=["место есть", "места нет"])
async def test_воркер_прочтение_без_дома_в_пункте_не_падает_в_дробь_и_текст(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_пусто: Any,
    точка_города: dict,
    место_отвечает: dict,
    место: list[g.PlaceHit],
) -> None:
    """Б-2 в воркере: DaData по городу знает «ул Победы» в Мурманске (без дома),
    головой «4» отдаёт дом 4 в Мурманске, область пуста. С местом «Кола» —
    прочтение установлено, дома в Коле нет: ни дроби в Мурманске, ни строки
    улицы «ул Победы, 4/37, Мурманск» текстом; строка остаётся `house_missing`.
    Без места (DaData пункта «Кола» не знает) — путь сегодняшний: дробь в
    Мурманске с кв 37. А2 не ходит: голова по городу дом дала."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    место_отвечает["ответ"] = место
    dadata_очередь["ответы"] = [[МУРМАНСК_УЛ_ПОБЕДЫ], [МУРМАНСК_ПОБЕДЫ_4], []]
    cid = await _строка_колы(seed_conversation, db_sessionmaker)
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    row = await _row(db_sessionmaker, cid)
    assert _походы(dadata_очередь) == [
        ("4/37", "Мурманск", False),
        ("4", "Мурманск", False),
        ("4/37", None, True),
        ("4/37", None, False),
    ]
    assert len(место_отвечает["походы"]) == 1
    if место:
        assert итог == g.GEO_HOUSE_MISSING
        assert (row.geo_status, row.geo_formatted, row.office, row.geo_lat) == (
            g.GEO_HOUSE_MISSING,
            None,
            None,
            None,
        )
        assert all(л["event"] not in ("geocode.auto_decided", "geocode.street_text") for л in логи)
        assert any(л["event"] == "geocode.street_head_unresolved" for л in логи)
    else:
        assert итог == g.GEO_EXACT
        assert (row.geo_formatted, row.office, row.geo_provider) == (
            "ул Победы, 4/37, Мурманск",
            "37",
            "dadata~approx",
        )
        [решение] = [л for л in логи if л["event"] == "geocode.auto_decided"]
        assert (решение["rule"], решение["settlement_read"]) == (g.RULE_FRACTION_HEAD, None)


async def test_воркер_кола_с_запятой_тот_же_итог(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    """А-4: «Кола, победы 4/37» — «Кола» подсказкой из имени перед запятой,
    улица из одного слова (Б молчит). А2 головой кругом отдаёт Колу и
    Снежногорск — без подсказки монета, с подсказкой Кола, кв 37."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    dadata_очередь["ответы"] = [[], [], [], [], [КОЛА_4_DADATA, СНЕЖНОГОРСК_4]]
    cid = await _строка_колы(seed_conversation, db_sessionmaker, "Кола, победы 4/37")
    await _тело_реплики(db_sessionmaker, seed_conversation, "Кола, победы 4/37")
    row = await _row(db_sessionmaker, cid)
    assert (row.street, row.settlement) == ("победы", None)
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_formatted, row.office, row.geo_provider) == (
        "ул Победы, 4/37, Кола",
        "37",
        "dadata~approx",
    )
    [решение] = [л for л in логи if л["event"] == "geocode.auto_decided"]
    assert (решение["rule"], решение["settlement_read"]) == (g.RULE_FRACTION_HEAD, "Кола")
    assert место_отвечает["походы"] == []
    # Подсказка ходила и своим текстом (HINT_QUERIES), голова по области — после неё.
    assert [q.house for q, _ in dadata_очередь["походы"]][-1] == "4"


async def test_воркер_живая_строка_а1_яндекс_головой(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_пусто: Any,
    точка_города: dict,
    monkeypatch: Any,
) -> None:
    """А1: свежая строка, «ул Ленина 13/38» в Орске — DaData и OSM дома «13»
    не знают, Яндекс головой отдаёт → `exact`, `yandex~approx`, кв 38. Яндекс
    спрашивается ровно дважды: по городу дробью и головой."""
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    await _режим(db_sessionmaker, "osm_then_yandex")
    дом_13 = дом(house="13")
    запросы: list[g.Query] = []

    async def yandex(query: g.Query, **kw: Any) -> list[g.GeoHit]:
        if kw.get("on_request"):
            await kw["on_request"]()
        запросы.append(query)
        return [дом_13] if query.house == "13" else []

    monkeypatch.setattr(worker.yandex_geocoder, "search", yandex)
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 13/38"))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_provider, row.office, row.geo_formatted) == (
        "yandex~approx",
        "38",
        "ул Ленина, 13/38, Орск",
    )
    # По городу дробью, потом головой; третий поход — область дробью, как и
    # прежде (итог по городу был `not_found`), головой область не спрашивается.
    assert [q.house for q in запросы][:2] == ["13/38", "13"]
    assert [q.house for q in запросы].count("13") == 1
    [решение] = [л for л in логи if л["event"] == "geocode.auto_decided"]
    assert (решение["rule"], решение["provider"]) == (g.RULE_FRACTION_HEAD, "yandex")
    # Область головой не ходила: вторая карта по городу дом дала.
    assert all(not (q.house == "13" and q.city is None) for q, _ in dadata_очередь["походы"])


async def test_воркер_а1_не_спрашивается_когда_dadata_дала_дом_или_спорит(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_пусто: Any,
    яндекс_отвечает: dict,
) -> None:
    """А-6: DaData головой отдала два дома «13» на двух Ленина (`ambiguous`) —
    Яндекс её не перебивает; и дала один — Яндекс не нужен."""
    await _режим(db_sessionmaker, "osm_then_yandex")
    дом_13 = дом(house="13")
    dadata_очередь["ответы"] = [
        [],
        [дом_13, dataclasses.replace(дом_13, settlement="Ора", lat=51.4)],
    ]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 13/38"))
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    assert итог != g.GEO_EXACT
    assert "13" not in [q.house for q in яндекс_отвечает["запросы"]]
    assert [q.house for q, _ in dadata_очередь["походы"]][:2] == ["13/38", "13"]


async def test_воркер_house_mismatch_города_а2_не_ходит(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """А-3: DaData по городу знает Ленина с домами «13А» и «14», дома «13» нет
    (`house_mismatch` головой) — улицу город знает, голову по области не
    спрашивают: походы DaData без запроса «13» кругом."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = (51.23, 58.47)
    дома = [дом(house="13А"), дом(house="14")]
    dadata_очередь["ответы"] = [дома, дома, [], []]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 13/38"))
    await _уровень(db_sessionmaker, cid, "B")
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    assert итог != g.GEO_EXACT
    assert all(not (q.house == "13" and q.city is None) for q, _ in dadata_очередь["походы"])


async def test_воркер_дробь_найдена_областью_а2_не_ходит(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """Область нашла «4/37» целиком в Североморске (18,7 км) — дробь выиграла:
    пригород решает, голову по области не спрашивают."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    dadata_очередь["ответы"] = [[], [], [СЕВЕРОМОРСК_4_37]]
    cid = await _строка_колы(seed_conversation, db_sessionmaker, "Победы 4/37")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_formatted == "ул Победы, 4/37, Североморск" and row.geo_provider == "dadata"
    assert all(not (q.house == "4" and q.city is None) for q, _ in dadata_очередь["походы"])


async def test_воркер_дробь_в_чужом_городе_не_гасит_голову_по_области(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    """C4: область на «4/37» отдала «ул Победы, 8/2» в Снежногорске — дробь на
    одноимённой улице ЧУЖОГО города. Прежний `corner_numbering` считал её
    угловой нумерацией улицы клиента и запирал А2; теперь дом-голова по
    области спрашивается, Кола решает с кв 37."""
    await _режим(db_sessionmaker, "osm_then_yandex")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    снежногорск_8_2 = dataclasses.replace(СНЕЖНОГОРСК_4, house="8/2")
    dadata_очередь["ответы"] = [[], [], [снежногорск_8_2], [], [КОЛА_4_DADATA]]
    cid = await _строка_колы(seed_conversation, db_sessionmaker)
    await _состарить(db_sessionmaker, cid)
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_formatted, row.office, row.geo_provider) == (
        "ул Победы, 4/37, Кола",
        "37",
        "dadata~approx",
    )
    assert _походы(dadata_очередь)[-1] == ("4", None, True)
    [решение] = [л for л in логи if л["event"] == "geocode.auto_decided"]
    assert (решение["rule"], решение["settlement_read"]) == (g.RULE_FRACTION_HEAD, "Кола")


@pytest.mark.parametrize("место", [[МЕСТО_КОЛА], []], ids=["место есть", "места нет"])
async def test_воркер_слово_пункта_раньше_пригорода(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    точка_города: dict,
    место_отвечает: dict,
    место: list[g.PlaceHit],
) -> None:
    """C1: «Кола победы 4/37», область нашла единственный «4/37» — в
    Североморске (18,7 км), голова по области не ходит (вариант есть). Пригород
    забирал бы его: `_улица_не_шире` прощает клиенту слово «Кола». Первое слово
    похоже на пункт — место спрашивается ДО пригорода; DaData знает «г Кола» →
    прочтение установлено, пригород не применяется, дома в Коле нет →
    `elsewhere` с вариантом оператору, карточка пуста, журнал
    `street_head_unresolved`. Пункта «Кола» у DaData нет — пригород как прежде:
    `exact` Североморска. Место спрошено один раз."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    место_отвечает["ответ"] = место
    dadata_очередь["ответы"] = [[], [], [СЕВЕРОМОРСК_4_37]]
    cid = await _строка_колы(seed_conversation, db_sessionmaker)
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    row = await _row(db_sessionmaker, cid)
    assert len(место_отвечает["походы"]) == 1
    assert all(not (q.house == "4" and q.city is None) for q, _ in dadata_очередь["походы"])
    if место:
        assert итог == g.GEO_ELSEWHERE
        assert (row.geo_status, row.geo_formatted, row.geo_lat, row.office) == (
            g.GEO_ELSEWHERE,
            None,
            None,
            None,
        )
        assert [v["formatted"] for v in row.geo_variants or []] == ["ул Победы, 4/37, Североморск"]
        assert all(
            л["event"] not in ("geocode.suburb_accepted", "geocode.auto_decided") for л in логи
        )
        [нерешено] = [л for л in логи if л["event"] == "geocode.street_head_unresolved"]
        assert нерешено["settlement"] == "Кола"
        assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    else:
        assert итог == g.GEO_EXACT
        assert (row.geo_formatted, row.geo_provider) == ("ул Победы, 4/37, Североморск", "dadata")
        assert any(л["event"] == "geocode.suburb_accepted" for л in логи)


async def test_воркер_место_не_спрашивается_тип_и_слово_в_улице(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    """(а) тип улицы — «Красная площадь 1»; (в) слово в улице карты — «Ленина
    Победы 5» при OSM «улица Ленина»: запрос места не уходит."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    osm_отвечает["ответ"] = [
        dataclasses.replace(КОЛА_УЛИЦА_OSM, street="улица Ленина", city="Мурманск", settlement=None)
    ]
    for текст in ("Красная площадь 1", "Ленина Победы 5"):
        cid = await _строка_колы(seed_conversation, db_sessionmaker, текст)
        with structlog.testing.capture_logs() as логи:
            await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
        assert место_отвечает["походы"] == [], текст
        assert all(л.get("rule") != g.RULE_SETTLEMENT_IN_STREET for л in логи), текст


async def test_воркер_стара_загора_дефис_карты_место_не_спрашивается(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_пусто: Any,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    """Б-3: «Стара Загора 214/35» (Самара) — DaData знает «ул Стара-Загора»
    без дома; слово клиента в улице карты через дефис → места нет."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = (53.1959, 50.1002)
    улица = g.GeoHit(
        street="ул Стара-Загора",
        house=None,
        settlement=None,
        city="Самара",
        region="Самарская обл",
        lat=53.23,
        lon=50.2,
        house_level=False,
        precise=False,
    )
    dadata_очередь["ответы"] = [[улица], []]
    found = _разобрать("Стара Загора 214/35")
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", found)
    await _уровень(db_sessionmaker, cid, "B")
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    assert итог != g.GEO_EXACT and место_отвечает["походы"] == []


async def test_воркер_апатиты_из_справочника_место_не_спрашивается(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_пусто: Any,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    """Б-6, граница: «Апатиты победы 4» при объявлении в Мурманске — Апатиты
    в справочнике Авито, это `locality`, не пункт; прочтение (10) молчит, запрос
    места не тратится (край Б0 — очередь механизмов)."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    cid = await _строка_колы(seed_conversation, db_sessionmaker, "Апатиты победы 4")
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    assert итог != g.GEO_EXACT and место_отвечает["походы"] == []


async def test_воркер_уровень_c_и_выключатель_место_не_спрашивается(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    точка_города: dict,
    место_отвечает: dict,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    dadata_очередь["ответы"] = [[], [], [], [], [КОЛА_4_DADATA]]
    cid = await _строка_колы(seed_conversation, db_sessionmaker)
    await _уровень(db_sessionmaker, cid, "C")
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_HOUSE_MISSING
    assert место_отвечает["походы"] == []
    # Невод C и голову по области не получает.
    assert all(not (q.house == "4" and q.city is None) for q, _ in dadata_очередь["походы"])
    # Выключатель автопривязки: ни головы за город, ни места.
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None)
        await s.commit()
    dadata_очередь["походы"].clear()
    dadata_очередь["ответы"] = [[], [], [], [], [КОЛА_4_DADATA]]
    cid2 = await _строка_колы(seed_conversation, db_sessionmaker)
    await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid2)
    assert место_отвечает["походы"] == []
    assert all(q.house == "4/37" for q, _ in dadata_очередь["походы"])


async def test_воркер_отказ_dadata_на_месте_откладывает_строку(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_очередь: dict,
    osm_отвечает: dict,
    точка_города: dict,
    monkeypatch: Any,
) -> None:
    """403 на запросе места — через шлюз он приходит `kind=blocked` (gateway
    `_ВИД_ОТКАЗА`): DaData закрыта до московской полуночи (`_учесть_отказ_dadata`
    → `geo:exhausted:dadata`), решений «сама» нет, строка помечена «без
    DaData», обход `dadata_back` вернётся. `limit` этот учёт не трогает — им
    отказ здесь и не кормится (ревью 20.09, C5)."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = ТОЧКА_МУРМАНСКА
    dadata_очередь["ответы"] = [[], [], [], [], [КОЛА_4_DADATA]]

    async def отказ(place: g.Place, *, region: str | None, **kw: Any) -> list[g.PlaceHit]:
        raise g.GeocodeError("dadata", "blocked", 403)

    monkeypatch.setattr(worker.dadata, "search_place", отказ)
    cid = await _строка_колы(seed_conversation, db_sessionmaker)
    assert not await worker._закрыт_на_сутки(redis, "dadata")
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    assert итог != g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status != g.GEO_EXACT and row.geo_without_dadata is True and row.geo_lat is None
    assert any(л["event"] == "geocode.street_place_failed" for л in логи)
    assert all(л["event"] != "geocode.auto_decided" for л in логи)
    assert await worker._закрыт_на_сутки(redis, "dadata")


def test_города_стенда_совпадают_со_справочником() -> None:
    assert МУРМАНСК == City("Мурманск", ОБЛАСТЬ, "Europe/Moscow") and ОРСК.name == "Орск"
    assert 8.9 < g.distance_km(ТОЧКА_МУРМАНСКА, (КОЛА_4_DADATA.lat, КОЛА_4_DADATA.lon)) < 9.1
    assert 26 < g.distance_km(ТОЧКА_МУРМАНСКА, (СНЕЖНОГОРСК_4.lat, СНЕЖНОГОРСК_4.lon)) < 28
    assert 18 < g.distance_km(ТОЧКА_МУРМАНСКА, (СЕВЕРОМОРСК_4_37.lat, СЕВЕРОМОРСК_4_37.lon)) < 20
    assert g.distance_km(ТОЧКА_МУРМАНСКА, (КОЛА_4_ЗА_45_КМ.lat, КОЛА_4_ЗА_45_КМ.lon)) > g.SUBURB_KM
    assert g.distance_km(ТОЧКА_МУРМАНСКА, (НОВАЯ_КОЛА_4.lat, НОВАЯ_КОЛА_4.lon)) > 150
    assert g.distance_km(ТОЧКА_МУРМАНСКА, (НОВАЯ_КОЛА_4_РЯДОМ.lat, НОВАЯ_КОЛА_4_РЯДОМ.lon)) < 10
