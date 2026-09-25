"""N24 (19.09): «улица не та» (`street_mismatch`) — не тупик цепочки.

Дом N на другой улице у первой карты значит лишь «на улице клиента дома N у
ЭТОЙ карты нет» — та же улика, что `house_missing`: дальше идут вторая карта,
уступка улице DaData, область, точка массива, правила и строка улицы. Только
уровни A/B (`STREET_EVIDENCE_LEVELS`), и ни один сторож вердикта не
ослабляется: каждый следующий ответ судит тот же `verdict`.

Именованный сторож `city_disputes_street` («город объявления сам спорит об
улице»): дом с номером клиента, в его городе, на улице той же основы и чужого
типа («ул Ленина 5» ↔ «пр-кт Ленина, д 5») — спор о типе внутри города, и
решать его областью нельзя: одноимённая улица нужного типа в пригороде —
подмена. Для такой строки за карты города не выходим, а пригород и
«единственный в радиусе» решением не считаем (A8).

Стенды — на записанных ответах карт; адреса вымышленные.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest
import structlog

from app.services import app_settings
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit import test_autobind_policy_1809 as политика
from tests.unit import test_geo_1809 as стенд
from tests.unit.test_autobind_policy_1809 import (
    ОРСК,
    САМАРА_ГОРОД,
    СОСЕД_4,
    УЛИЦА_МОЛОДЁЖНАЯ,
    _evidence,
    _массив,
)
from tests.unit.test_geo_1809 import (
    ДАЛЬНИЙ,
    САМАРА,
    СМЫШЛЯЕВКА,
    _row,
    _разобрать,
    _режим,
    _строка,
    ctx,
    дом,
)

#: Карты подменены записанными ответами — фикстуры стендов 18.09 под своими
#: именами (присваивание, не импорт: pytest собирает их по имени модуля).
dadata_отвечает = стенд.dadata_отвечает
osm_пусто = стенд.osm_пусто
яндекс_отвечает = стенд.яндекс_отвечает
точка_города = стенд.точка_города
osm_отвечает = политика.osm_отвечает

pytestmark = pytest.mark.anyio

A = "ул Молодёжная 2"
C = "Молодёжная 2"
#: Другая основа — спора нет (улица клиента карте неизвестна, дом подобран по номеру).
ЧУЖАЯ_2 = дом(street="ул Строителей", house="2")
#: Та же основа, чужой тип — спор города об улице.
СПОРНАЯ_2 = дом(street="пр-кт Молодёжный", house="2")
ТОЧНЫЙ_2 = дом(street="ул Молодёжная", house="2", precise=True)
ГОРОД_ПРКТ = дом(
    street="пр-кт Ленина",
    house="5",
    city="Самара",
    region="Самарская область",
    lat=53.20,
    lon=50.10,
)
МИРА_САМАРА = дом(street="ул Мира", house="5", city="Самара", region="Самарская область")
К = дом(street="Комсомольская улица", house="5")
К_ДРУГОЙ_ГОРОД = dataclasses.replace(К, city="Новотроицк")
К_САМАРА = dataclasses.replace(К, city="Самара", region="Самарская область")

ЛЕНИНА_5 = g.Parsed(street="ул Ленина", house="5")


def _уровень(text: str) -> None:
    """Разбор стенда обязан давать те уровни, на которых стоят стенды ниже."""
    assert _разобрать(text).level == ("C" if text == C else "A"), text


# ── чистый слой: сторожа N24 ───────────────────────────────────────────────────


def test_street_mismatch_continues_только_A_B() -> None:
    assert g.street_mismatch_continues(g.GEO_STREET_MISMATCH, "A")
    assert g.street_mismatch_continues(g.GEO_STREET_MISMATCH, "B")
    assert not g.street_mismatch_continues(g.GEO_STREET_MISMATCH, "C")
    assert not g.street_mismatch_continues(g.GEO_STREET_MISMATCH, None)
    assert not g.street_mismatch_continues(g.GEO_HOUSE_MISMATCH, "A")
    assert not g.street_mismatch_continues(g.GEO_NOT_FOUND, "A")


def test_street_mismatch_continues_beyond_city() -> None:
    assert g.street_mismatch_continues_beyond_city(g.GEO_STREET_MISMATCH, "A", city_disputes=False)
    assert not g.street_mismatch_continues_beyond_city(
        g.GEO_STREET_MISMATCH, "A", city_disputes=True
    )
    assert not g.street_mismatch_continues_beyond_city(
        g.GEO_STREET_MISMATCH, "C", city_disputes=False
    )
    assert not g.street_mismatch_continues_beyond_city(
        g.GEO_HOUSE_MISMATCH, "A", city_disputes=False
    )


@pytest.mark.parametrize(
    ("parsed", "city", "city_hits", "ожидание"),
    [
        # Дом клиента в его городе на улице той же основы чужого типа — спор.
        (ЛЕНИНА_5, САМАРА_ГОРОД, [([ГОРОД_ПРКТ], "dadata")], True),
        # Любая карта города: DaData пусто, OSM отдал спорный дом.
        (ЛЕНИНА_5, САМАРА_ГОРОД, [([], "dadata"), ([ГОРОД_ПРКТ], "nominatim")], True),
        # Другая основа — карта улицы клиента не знает, спора нет.
        (
            ЛЕНИНА_5,
            САМАРА_ГОРОД,
            [([dataclasses.replace(ГОРОД_ПРКТ, street="ул Мира")], "dadata")],
            False,
        ),
        # Другой номер — `house_mismatch`, не спор.
        (ЛЕНИНА_5, САМАРА_ГОРОД, [([dataclasses.replace(ГОРОД_ПРКТ, house="7")], "dadata")], False),
        # Другой город — `city_mismatch`, не спор.
        (
            ЛЕНИНА_5,
            САМАРА_ГОРОД,
            [([dataclasses.replace(ГОРОД_ПРКТ, city="Тольятти")], "dadata")],
            False,
        ),
        # Нашлась улица нужного типа с домом — вердикт и не был бы «улица не та».
        (
            ЛЕНИНА_5,
            САМАРА_ГОРОД,
            [([ГОРОД_ПРКТ, dataclasses.replace(ГОРОД_ПРКТ, street="ул Ленина")], "dadata")],
            False,
        ),
        # Клиент типа не назвал — вердикт `exact`, спорить не о чем.
        (g.Parsed(street="Ленина", house="5"), САМАРА_ГОРОД, [([ГОРОД_ПРКТ], "dadata")], False),
        # Клиент назвал другой город словами — `other_city`, сторож наследует место.
        (
            g.Parsed(street="ул Ленина", house="5", locality="Тольятти"),
            САМАРА_ГОРОД,
            [([ГОРОД_ПРКТ], "dadata")],
            False,
        ),
        # Симметрия по основе: «Комсомольский проспект» при «Комсомольская улица».
        (
            g.Parsed(street="Комсомольский проспект", house="5"),
            САМАРА_ГОРОД,
            [([К_САМАРА], "dadata")],
            True,
        ),
        # Два типа у клиента («ул. Старое шоссе») — спор, как сегодня; чинит N6.
        (
            g.Parsed(street="ул. Старое шоссе", house="2"),
            САМАРА_ГОРОД,
            [([dataclasses.replace(К_САМАРА, street="Старое шоссе", house="2")], "dadata")],
            True,
        ),
        # Города нет / ответов нет — спора нет.
        (ЛЕНИНА_5, None, [([ГОРОД_ПРКТ], "dadata")], False),
        (ЛЕНИНА_5, САМАРА_ГОРОД, [], False),
    ],
)
def test_city_disputes_street(
    parsed: g.Parsed, city: Any, city_hits: list[tuple[list[g.GeoHit], str]], ожидание: bool
) -> None:
    assert g.city_disputes_street(parsed, city, city_hits) is ожидание


# ── чистый слой: правила `auto_decide` после «улица не та» ────────────────────


def test_street_point_после_street_mismatch() -> None:
    """Улица DaData без дома + дом того же номера на чужой улице у OSM: точка
    улицы, как при `house_missing`. Диверсии: уровень C, `house_mismatch`,
    спор города об улице."""
    parsed = g.Parsed(street="ул Молодёжная", house="2")
    e = _evidence(
        status=g.GEO_STREET_MISMATCH,
        parsed=parsed,
        city_hits=[([УЛИЦА_МОЛОДЁЖНАЯ], "dadata"), ([ЧУЖАЯ_2], "nominatim")],
    )
    решение = g.auto_decide(e)
    assert решение is not None and (решение.rule, решение.provider, решение.approx) == (
        g.RULE_STREET_POINT,
        "dadata",
        False,
    )
    assert (решение.hit.house, решение.hit.house_level, решение.hit.precise) == ("2", False, False)
    assert (
        g.auto_decide(dataclasses.replace(e, parsed=dataclasses.replace(parsed, level="C"))) is None
    )
    assert g.auto_decide(dataclasses.replace(e, status=g.GEO_HOUSE_MISMATCH)) is None
    спор = dataclasses.replace(
        e, city_hits=[([УЛИЦА_МОЛОДЁЖНАЯ], "dadata"), ([СПОРНАЯ_2], "nominatim")]
    )
    assert g.auto_decide(спор) is None
    # (C1) Те же улики спора при другом статусе (какая из слабых карт что
    # отдала — улицу без дома или спорный дом) — тоже None: сторож один на
    # все отказы, а не на ветку `street_mismatch`.
    for status in (g.GEO_HOUSE_MISSING, g.GEO_NOT_FOUND):
        assert g.auto_decide(dataclasses.replace(спор, status=status)) is None, status


def test_комсомольский_проспект_не_комсомольская_улица_на_всей_цепочке() -> None:
    """Инвариант §2.1: чужой тип улицы не проходит ни одним звеном цепочки —
    ни спором города (правил нет), ни областью, ни строкой, ни пригородом."""
    parsed = g.Parsed(street="Комсомольский проспект", house="5")
    # (a) вердикт первой карты.
    assert g.verdict(parsed, ОРСК, [К])[0] == g.GEO_STREET_MISMATCH
    # (b) спор города: правил нет, хотя область дала бы «единственный дом».
    assert g.city_disputes_street(parsed, ОРСК, [([К], "dadata")]) is True
    assert (
        g.auto_decide(
            _evidence(
                status=g.GEO_STREET_MISMATCH,
                parsed=parsed,
                city_hits=[([К], "dadata"), ([К], "nominatim")],
                region_hits=[К, К_ДРУГОЙ_ГОРОД],
                region_provider="dadata",
                city_point=(51.2, 58.5),
            )
        )
        is None
    )
    # (c) без спора (в городе другая основа), чужой тип в области — тем же сторожем.
    assert (
        g.auto_decide(
            _evidence(
                status=g.GEO_STREET_MISMATCH,
                parsed=parsed,
                city_hits=[([дом(street="ул Мира", house="5")], "dadata")],
                region_hits=[К_ДРУГОЙ_ГОРОД],
                region_provider="dadata",
                city_point=(51.2, 58.5),
            )
        )
        is None
    )
    assert g.known_street(parsed, ОРСК, [К, К_ДРУГОЙ_ГОРОД]) is None
    assert g.variants(parsed, ОРСК, [К_ДРУГОЙ_ГОРОД]) == []
    assert g.suburb_hit(parsed, ОРСК, [К_ДРУГОЙ_ГОРОД], (51.2, 58.5)) is None
    # (d) положительный близнец: без типа у клиента улица сходится.
    assert g.verdict(g.Parsed(street="Комсомольская", house="5"), ОРСК, [К])[0] == g.GEO_EXACT


def test_area_point_после_street_mismatch() -> None:
    """«СНТ Солнечный 59»: в посёлке-тёзке Солнечный нашёлся дом 59 на другой
    улице (`street_mismatch`), а массив карта знает один — точка массива."""
    parsed = g.Parsed(
        street="СНТ Солнечный", house="59", settlement="Солнечный", settlement_type="СНТ"
    )
    чужой_59 = дом(street="ул Садовая", house="59", settlement="Солнечный")
    assert g.verdict(parsed, ОРСК, [чужой_59])[0] == g.GEO_STREET_MISMATCH
    место = g.Place(
        settlement=None, settlement_type=None, area="СНТ Солнечный", district=None, street=""
    )
    e = _evidence(
        status=g.GEO_STREET_MISMATCH,
        parsed=parsed,
        city_hits=[([чужой_59], "dadata")],
        place=место,
        place_hits=[_массив("СНТ Солнечный")],
    )
    решение = g.auto_decide(e)
    assert решение is not None and (решение.rule, решение.hit.area, решение.hit.house) == (
        g.RULE_AREA_POINT,
        "СНТ Солнечный",
        "59",
    )
    assert (
        g.auto_decide(dataclasses.replace(e, parsed=dataclasses.replace(parsed, level="C"))) is None
    )


def test_fraction_head_после_street_mismatch() -> None:
    """«13/38»: дробь нашлась на чужой улице, а дом «13» на улице клиента уже в
    ответах по городу — правило дроби решает без сети, как при `house_mismatch`."""
    parsed = g.Parsed(street="ул Ленина", house="13/38")
    чужая_дробь = дом(street="ул Мира", house="13/38")
    дом_13 = дом(house="13")
    assert g.verdict(parsed, ОРСК, [чужая_дробь, дом_13])[0] == g.GEO_STREET_MISMATCH
    e = _evidence(
        status=g.GEO_STREET_MISMATCH,
        parsed=parsed,
        hits=[чужая_дробь, дом_13],
        city_hits=[([чужая_дробь, дом_13], "dadata")],
    )
    решение = g.auto_decide(e)
    assert решение is not None and (
        решение.rule,
        решение.hit.house,
        решение.office,
        решение.provider,
    ) == (g.RULE_FRACTION_HEAD, "13/38", "38", "dadata")
    assert (
        g.auto_decide(dataclasses.replace(e, parsed=dataclasses.replace(parsed, level="C"))) is None
    )
    # Спор города: дробь на «пр-кт Ленина» — правил нет.
    спорная_дробь = дом(street="пр-кт Ленина", house="13/38")
    assert (
        g.auto_decide(
            dataclasses.replace(
                e, hits=[спорная_дробь, дом_13], city_hits=[([спорная_дробь, дом_13], "dadata")]
            )
        )
        is None
    )


def test_only_in_radius_молчит_при_споре_города() -> None:
    """(A8) `elsewhere` с одним домом в радиусе из двух — при споре города об
    улице не решение: вариант оператору. Близнец без спора решает."""
    e = _evidence(
        status=g.GEO_ELSEWHERE,
        parsed=ЛЕНИНА_5,
        city=САМАРА_ГОРОД,
        city_hits=[([ГОРОД_ПРКТ], "dadata")],
        region_hits=[СМЫШЛЯЕВКА, ДАЛЬНИЙ],
        region_provider="dadata",
        city_point=САМАРА,
    )
    assert g.auto_decide(e) is None
    решение = g.auto_decide(dataclasses.replace(e, city_hits=[([], "dadata")]))
    assert решение is not None and решение.rule == g.RULE_ONLY_IN_RADIUS
    assert решение.hit is СМЫШЛЯЕВКА


#: Спорный дом к «ул Ленина 5»: номер клиента, его город, та же основа, чужой тип.
СПОРНЫЙ_5 = дом(street="пр-кт Ленина", house="5")
#: Догадка карты уровня улицы: «ул Ленина» без дома.
УЛИЦА_ЛЕНИНА = дом(house=None, house_level=False, precise=False)
СНТ_СОЛНЕЧНЫЙ_59 = g.Parsed(
    street="СНТ Солнечный", house="59", settlement="Солнечный", settlement_type="СНТ"
)
МЕСТО_СНТ_СОЛНЕЧНЫЙ = g.Place(
    settlement=None, settlement_type=None, area="СНТ Солнечный", district=None, street=""
)


@pytest.mark.parametrize(
    ("status", "правило", "спорный", "улики"),
    [
        # W3 наоборот: спорный дом отдала DaData, улицу без дома — OSM. Статус
        # — по карте вердикта (`house_missing`), улики спора те же, что при
        # `street_mismatch`.
        (
            g.GEO_HOUSE_MISSING,
            g.RULE_STREET_POINT,
            СПОРНЫЙ_5,
            {"city_hits": [([УЛИЦА_ЛЕНИНА], "nominatim")]},
        ),
        (
            g.GEO_NOT_FOUND,
            g.RULE_STREET_POINT,
            СПОРНЫЙ_5,
            {"city_hits": [([УЛИЦА_ЛЕНИНА], "nominatim")]},
        ),
        (
            g.GEO_STREET_MISMATCH,
            g.RULE_STREET_POINT,
            СПОРНЫЙ_5,
            {"city_hits": [([УЛИЦА_ЛЕНИНА], "nominatim")]},
        ),
        # Улица — из ответов области, спорный дом — по городу.
        (
            g.GEO_NOT_FOUND,
            g.RULE_STREET_POINT,
            СПОРНЫЙ_5,
            {
                "city_hits": [([], "nominatim")],
                "region_hits": [УЛИЦА_ЛЕНИНА],
                "region_provider": "yandex",
            },
        ),
        # Дробь головой: «пр-кт Ленина, 13/38» у DaData, дом «13» у OSM — два
        # прочтения («ул Ленина 13, кв 38» против «пр-кт Ленина 13/38»).
        (
            g.GEO_HOUSE_MISMATCH,
            g.RULE_FRACTION_HEAD,
            дом(street="пр-кт Ленина", house="13/38"),
            {
                "parsed": g.Parsed(street="ул Ленина", house="13/38"),
                "city_hits": [([дом(house="13")], "nominatim")],
            },
        ),
        # Семья дома: «пр-кт Ленина, 29» у DaData, «ул Ленина, 29А» у OSM.
        (
            g.GEO_HOUSE_MISMATCH,
            g.RULE_HOUSE_FAMILY,
            дом(street="пр-кт Ленина", house="29"),
            {
                "parsed": g.Parsed(street="ул Ленина", house="29"),
                "city_hits": [([дом(house="29А")], "nominatim")],
            },
        ),
        # Точка массива: «п. Солнечный, ул Солнечная, 59» у DaData при
        # «СНТ Солнечный 59» — та же основа, чужой тип.
        (
            g.GEO_NOT_FOUND,
            g.RULE_AREA_POINT,
            дом(street="ул Солнечная", house="59", settlement="Солнечный"),
            {
                "parsed": СНТ_СОЛНЕЧНЫЙ_59,
                "city_hits": [([], "nominatim")],
                "place": МЕСТО_СНТ_СОЛНЕЧНЫЙ,
                "place_hits": [_массив("СНТ Солнечный")],
            },
        ),
    ],
)
def test_спор_города_один_сторож_на_все_отказы_и_правила(
    status: str, правило: str, спорный: g.GeoHit, улики: dict[str, Any]
) -> None:
    """(C1) Улики спора города не зависят от того, какая карта отдала спорный
    дом, а статус зависит: при споре решения нет ни для какого отказа и ни для
    какого правила — точка улицы, точка массива, дробь и семья дома выбирали
    бы одно из двух прочтений за оператора. Близнец без спора — тот же дом на
    улице другой основы («ул Мира») — правило решает."""
    e = _evidence(status=status, **улики)
    e = dataclasses.replace(e, city_hits=[([спорный], "dadata"), *e.city_hits])
    assert g.city_disputes_street(e.parsed, e.city, e.city_hits) is True
    assert g.auto_decide(e) is None
    близнец = dataclasses.replace(
        e,
        city_hits=[([dataclasses.replace(спорный, street="ул Мира")], "dadata"), *e.city_hits[1:]],
    )
    assert g.city_disputes_street(близнец.parsed, близнец.city, близнец.city_hits) is False
    решение = g.auto_decide(близнец)
    assert решение is not None and решение.rule == правило, (status, правило)


# ── воркер: вторая карта по городу ─────────────────────────────────────────────


async def test_вторая_карта_после_street_mismatch(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    яндекс_отвечает: dict,
) -> None:
    """OSM отдал дом 2 на другой улице — Яндекс спрашивается по тому же городу
    и подтверждает дом на улице клиента: exact, автозапись. Уровень C — нет."""
    _уровень(A)
    await _режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [[]]
    osm_отвечает["ответ"] = [ЧУЖАЯ_2]
    яндекс_отвечает["ответ"] = [ТОЧНЫЙ_2]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(A))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_EXACT,
        "yandex",
        "ул Молодёжная, 2, Орск",
    )
    assert [q.city for q in яндекс_отвечает["запросы"]] == ["Орск"]
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        == g.GRADE_EXACT
    )
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_вторая_карта_после_street_mismatch_уровню_c_не_идёт(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    яндекс_отвечает: dict,
) -> None:
    _уровень(C)
    await _режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [[]]
    osm_отвечает["ответ"] = [ЧУЖАЯ_2]
    яндекс_отвечает["ответ"] = [ТОЧНЫЙ_2]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(C))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_STREET_MISMATCH
    row = await _row(db_sessionmaker, cid)
    assert яндекс_отвечает["запросы"] == []
    assert (row.geo_status, row.geo_formatted, row.geo_lat) == (g.GEO_STREET_MISMATCH, None, None)
    assert len(dadata_отвечает["запросы"]) == 1


@pytest.mark.parametrize("ответ_яндекса", [[ТОЧНЫЙ_2], [СПОРНАЯ_2]])
async def test_спор_города_вторая_карта_идёт(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    яндекс_отвечает: dict,
    ответ_яндекса: list[g.GeoHit],
) -> None:
    """Спор города об улице (OSM: «пр-кт Молодёжный, 2») вторую карту по городу
    не останавливает: Яндекс спор может только разрешить («ул Молодёжная, 2» →
    exact). Отдал тот же «пр-кт» — спор остаётся, за карты города не идём."""
    await _режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [[]]
    osm_отвечает["ответ"] = [СПОРНАЯ_2]
    яндекс_отвечает["ответ"] = ответ_яндекса
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(A))
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    row = await _row(db_sessionmaker, cid)
    assert len(яндекс_отвечает["запросы"]) == 1
    события = {л["event"] for л in логи}
    if ответ_яндекса == [ТОЧНЫЙ_2]:
        assert итог == g.GEO_EXACT
        assert (row.geo_provider, row.geo_formatted) == ("yandex", "ул Молодёжная, 2, Орск")
        assert "geocode.city_disputes_street" not in события
    else:
        assert итог == g.GEO_STREET_MISMATCH
        assert (row.geo_formatted, row.geo_lat, row.geo_variants or []) == (None, None, [])
        assert len(dadata_отвечает["запросы"]) == 1, "области нет"
        assert "geocode.city_disputes_street" in события


# ── воркер: область, уступка, текст ────────────────────────────────────────────


async def test_область_после_street_mismatch_пригород(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    точка_города: dict,
) -> None:
    """Обе карты города дали дом 5 на «ул Мира» (другая основа — спора нет):
    область спрашивается, единственный дом в 18 км — пригород, exact.
    Положительный близнец `test_спор_города_об_улице_область_не_решает`."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[МИРА_САМАРА], [СМЫШЛЯЕВКА]]
    osm_отвечает["ответ"] = [МИРА_САМАРА]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_EXACT,
        "dadata",
        "ул Ленина, 5, Смышляевка",
    )
    assert (row.geo_lat, row.geo_lon) == (СМЫШЛЯЕВКА.lat, СМЫШЛЯЕВКА.lon)
    assert "geocode.suburb_accepted" in {л["event"] for л in логи}
    assert len(dadata_отвечает["запросы"]) >= 2, "город + область"


async def test_область_после_street_mismatch_чужая_улица_области_не_проходит(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    точка_города: dict,
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[МИРА_САМАРА], [dataclasses.replace(СМЫШЛЯЕВКА, street="ул Мира")]]
    osm_отвечает["ответ"] = [МИРА_САМАРА]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_STREET_MISMATCH
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_formatted, row.geo_variants or []) == (
        g.GEO_STREET_MISMATCH,
        None,
        [],
    )
    assert len(dadata_отвечает["запросы"]) >= 2


async def test_уступка_улице_dadata_после_street_mismatch(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
) -> None:
    """DaData знает улицу без дома (`house_missing`), OSM отдал дом 2 на другой
    улице: место OSM занимает улица справочника (как при `not_found`), дальше
    точка улицы `~approx`."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[УЛИЦА_МОЛОДЁЖНАЯ]]
    osm_отвечает["ответ"] = [ЧУЖАЯ_2]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(A))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_EXACT,
        "dadata~approx",
        "ул Молодёжная, 2, Орск",
    )
    assert (row.geo_lat, row.geo_lon) == (УЛИЦА_МОЛОДЁЖНАЯ.lat, УЛИЦА_МОЛОДЁЖНАЯ.lon)
    assert [(л["rule"], л["was"]) for л in логи if л["event"] == "geocode.auto_decided"] == [
        (g.RULE_STREET_POINT, g.GEO_HOUSE_MISSING)
    ]
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


@pytest.mark.parametrize(
    ("текст", "ответ_osm", "выключить", "formatted", "grade"),
    [
        # Невод C — как до N24: отказ без строки и без точки.
        (C, [ЧУЖАЯ_2], False, None, None),
        # Выключатель выключен — уступки, правил и строки нет; область идёт.
        (A, [ЧУЖАЯ_2], True, None, None),
        # Спор города («пр-кт Молодёжный, 2»): уступки и точки нет; строка
        # улицы текстом по `по_городу` — как сегодня.
        (A, [СПОРНАЯ_2], False, "ул Молодёжная, 2, Орск", g.GRADE_TEXT),
    ],
)
async def test_уступка_улице_dadata_не_идёт_без_сторожа(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    текст: str,
    ответ_osm: list[g.GeoHit],
    выключить: bool,
    formatted: str | None,
    grade: str | None,
) -> None:
    _уровень(текст)
    await _режим(db_sessionmaker, "nominatim")
    if выключить:
        async with db_sessionmaker() as s:
            await app_settings.set_many(
                s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None
            )
            await s.commit()
    dadata_отвечает["ответы"] = [[УЛИЦА_МОЛОДЁЖНАЯ]]
    osm_отвечает["ответ"] = ответ_osm
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(текст))
    with structlog.testing.capture_logs() as логи:
        assert (
            await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
            == g.GEO_STREET_MISMATCH
        )
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_provider, row.geo_formatted, row.geo_lat) == (
        g.GEO_STREET_MISMATCH,
        "nominatim",
        formatted,
        None,
    )
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        == grade
    )
    assert "geocode.auto_decided" not in {л["event"] for л in логи}
    if ответ_osm == [СПОРНАЯ_2]:
        assert "geocode.city_disputes_street" in {л["event"] for л in логи}
        assert len(dadata_отвечает["запросы"]) == 1, "за карты города не ходим"


@pytest.mark.parametrize("ответ_dadata", [[СПОРНАЯ_2], [ЧУЖАЯ_2]])
async def test_спор_города_карты_наоборот_точки_улицы_нет(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    ответ_dadata: list[g.GeoHit],
) -> None:
    """(C1) W3 наоборот: спорный дом «пр-кт Молодёжный, 2» отдала DaData, улицу
    без дома — OSM (`house_missing`). Улики спора те же, что в
    `test_уступка_улице_dadata_не_идёт_без_сторожа`, и итог тот же: точки
    улицы нет, строка — текстом по `по_городу`; область идёт как сегодня.
    Близнец: DaData отдала дом другой основы («ул Строителей») — точка улицы
    из ответа OSM, `~approx`."""
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [ответ_dadata]
    osm_отвечает["ответ"] = [УЛИЦА_МОЛОДЁЖНАЯ]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(A))
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    row = await _row(db_sessionmaker, cid)
    события = {л["event"] for л in логи}
    решения = [(л["rule"], л["was"]) for л in логи if л["event"] == "geocode.auto_decided"]
    assert len(dadata_отвечает["запросы"]) >= 2, "город + область"
    if ответ_dadata == [СПОРНАЯ_2]:
        assert итог == g.GEO_HOUSE_MISSING
        assert (row.geo_status, row.geo_provider, row.geo_formatted, row.geo_lat) == (
            g.GEO_HOUSE_MISSING,
            "nominatim",
            "ул Молодёжная, 2, Орск",
            None,
        )
        assert (
            g.card_grade(
                row.kind,
                row.geo_status,
                row.geo_provider,
                row.geo_lat,
                row.geo_lon,
                row.geo_formatted,
            )
            == g.GRADE_TEXT
        )
        assert решения == [] and "geocode.auto_decided" not in события
        assert [л["status"] for л in логи if л["event"] == "geocode.city_disputes_street"] == [
            g.GEO_HOUSE_MISSING
        ]
    else:
        assert итог == g.GEO_EXACT
        assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
            g.GEO_EXACT,
            "nominatim~approx",
            "ул Молодёжная, 2, Орск",
        )
        assert (row.geo_lat, row.geo_lon) == (УЛИЦА_МОЛОДЁЖНАЯ.lat, УЛИЦА_МОЛОДЁЖНАЯ.lon)
        assert решения == [(g.RULE_STREET_POINT, g.GEO_HOUSE_MISSING)]
        assert "geocode.city_disputes_street" not in события


@pytest.mark.parametrize("текст", [A, C])
async def test_текст_после_street_mismatch_с_уликой_области(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    текст: str,
) -> None:
    """По городу обе карты знают только дом 2 на «ул Строителей»; по области
    DaData отдаёт дом 4 на улице клиента — улика улицы, строка без точки.
    Уровень C: области нет, строки нет."""
    _уровень(текст)
    await _режим(db_sessionmaker, "nominatim")
    dadata_отвечает["ответы"] = [[ЧУЖАЯ_2], [СОСЕД_4]]
    osm_отвечает["ответ"] = [ЧУЖАЯ_2]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(текст))
    with structlog.testing.capture_logs() as логи:
        assert (
            await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
            == g.GEO_STREET_MISMATCH
        )
    row = await _row(db_sessionmaker, cid)
    тексты = [л["status"] for л in логи if л["event"] == "geocode.street_text"]
    if текст == A:
        assert (row.geo_formatted, row.geo_lat, row.geo_lon) == (
            "ул Молодёжная, 2, Орск",
            None,
            None,
        )
        assert len(dadata_отвечает["запросы"]) == 2 and тексты == [g.GEO_STREET_MISMATCH]
        assert (
            g.card_grade(
                row.kind,
                row.geo_status,
                row.geo_provider,
                row.geo_lat,
                row.geo_lon,
                row.geo_formatted,
            )
            == g.GRADE_TEXT
        )
    else:
        assert (row.geo_formatted, len(dadata_отвечает["запросы"]), тексты) == (None, 1, [])


async def test_область_идёт_а_тип_улицы_держит(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    точка_города: dict,
) -> None:
    """Инвариант §2.1 в воркере, «гард по слову» наоборот: попытка по области
    была (без спора — в городе другая основа), а «пр-кт Ленина, 5» в пригороде
    тип-сторож не пропустил: ни точки, ни варианта, ни строки."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [
        [МИРА_САМАРА],
        [МИРА_САМАРА, dataclasses.replace(СМЫШЛЯЕВКА, street="пр-кт Ленина")],
    ]
    osm_отвечает["ответ"] = [МИРА_САМАРА]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_STREET_MISMATCH
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_formatted, row.geo_lat, row.geo_variants or []) == (
        g.GEO_STREET_MISMATCH,
        None,
        None,
        [],
    )
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        is None
    )
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    assert len(dadata_отвечает["запросы"]) >= 2, "город + область"


@pytest.mark.parametrize("текст", [A, C])
async def test_dadata_за_потолком_посреди_области_после_street_mismatch(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    текст: str,
) -> None:
    """Потолок DaData выбран между запросом по городу и областью: без области
    вердикт A/B не окончателен — строка откладывается, попытка не тратится.
    Уровень C области не ждёт — приговор карт как до N24."""
    _уровень(текст)
    await _режим(db_sessionmaker, "nominatim")
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_DADATA_DAILY_LIMIT: 1}, user_id=None
        )
        await s.commit()
    dadata_отвечает["ответы"] = [[ЧУЖАЯ_2]]
    osm_отвечает["ответ"] = [ЧУЖАЯ_2]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(текст))
    итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    row = await _row(db_sessionmaker, cid)
    assert len(dadata_отвечает["запросы"]) == 1
    if текст == A:
        assert итог == "deferred"
        assert (row.geo_status, row.geo_provider, row.geo_attempts) == (
            g.GEO_PENDING,
            "nominatim",
            0,
        )
    else:
        assert итог == g.GEO_STREET_MISMATCH
        assert (row.geo_status, row.geo_attempts) == (g.GEO_STREET_MISMATCH, 1)


# ── воркер: ответ Яндекса — улика (A5) ─────────────────────────────────────────


async def _потолок_яндекса(db_sessionmaker: Any, потолок: int) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(
            s, {app_settings.ADDRESS_GEO_YANDEX_DAILY_LIMIT: потолок}, user_id=None
        )
        await s.commit()


async def test_ответ_яндекса_остаётся_уликой_улицы(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    яндекс_отвечает: dict,
) -> None:
    """Яндекс второй картой дом не подтвердил, но улицу клиента знает
    (`house_missing`): его ответ не заменяет вердикт OSM, однако остаётся в
    ответах по городу — точка улицы берётся из него. Потолок Яндекса — один
    запрос: область и точку он не спрашивает, улика только из второй карты."""
    await _режим(db_sessionmaker, "osm_then_yandex")
    await _потолок_яндекса(db_sessionmaker, 1)
    dadata_отвечает["ответы"] = [[]]
    osm_отвечает["ответ"] = [ЧУЖАЯ_2]
    яндекс_отвечает["ответ"] = [УЛИЦА_МОЛОДЁЖНАЯ]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(A))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert len(яндекс_отвечает["запросы"]) == 1
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_EXACT,
        "yandex~approx",
        "ул Молодёжная, 2, Орск",
    )
    assert [
        (л["rule"], л["provider"], л["was"]) for л in логи if л["event"] == "geocode.auto_decided"
    ] == [(g.RULE_STREET_POINT, "yandex", g.GEO_STREET_MISMATCH)]


async def test_спор_города_считается_после_дописки_яндекса(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    яндекс_отвечает: dict,
) -> None:
    """OSM: дом 2 на «ул Строителей» (спора нет), Яндекс: «пр-кт Молодёжный, 2»
    — спор города виден только с ответом Яндекса в `по_городу`: области нет."""
    await _режим(db_sessionmaker, "osm_then_yandex")
    dadata_отвечает["ответы"] = [[]]
    osm_отвечает["ответ"] = [ЧУЖАЯ_2]
    яндекс_отвечает["ответ"] = [СПОРНАЯ_2]
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать(A))
    with structlog.testing.capture_logs() as логи:
        assert (
            await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
            == g.GEO_STREET_MISMATCH
        )
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_formatted) == (g.GEO_STREET_MISMATCH, None)
    assert len(яндекс_отвечает["запросы"]) == 1 and len(dadata_отвечает["запросы"]) == 1
    assert [л["status"] for л in логи if л["event"] == "geocode.city_disputes_street"] == [
        g.GEO_STREET_MISMATCH
    ]


# ── воркер: точка массива ──────────────────────────────────────────────────────


@pytest.mark.parametrize("выключить", [False, True])
async def test_точка_массива_после_street_mismatch(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    monkeypatch: Any,
    выключить: bool,
) -> None:
    """«СНТ Солнечный 59»: в посёлке-тёзке дом 59 на «ул Садовая»
    (`street_mismatch`) — массив спрашивается, точка массива `~approx`.
    Под выключателем массив не спрашивается."""
    await _режим(db_sessionmaker, "nominatim")
    if выключить:
        async with db_sessionmaker() as s:
            await app_settings.set_many(
                s, {app_settings.ADDRESS_GEO_AUTO_DECIDE: False}, user_id=None
            )
            await s.commit()
    места: list[g.Place] = []

    async def search_place(place: g.Place, *, region: str | None, **kw: Any) -> list[g.PlaceHit]:
        await kw["on_request"]()
        места.append(place)
        return [_массив("СНТ Солнечный")]

    monkeypatch.setattr(worker.dadata, "search_place", search_place)
    чужой_59 = дом(street="ул Садовая", house="59", settlement="Солнечный")
    dadata_отвечает["ответы"] = [[чужой_59]]
    osm_отвечает["ответ"] = [чужой_59]
    found = _разобрать("СНТ Солнечный 59")
    assert (found.level, found.settlement_type) == ("A", "СНТ")
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", found)
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    row = await _row(db_sessionmaker, cid)
    if выключить:
        assert итог == g.GEO_STREET_MISMATCH and места == []
        assert (row.geo_formatted, row.geo_lat) == (None, None)
        return
    assert итог == g.GEO_EXACT
    assert (row.geo_status, row.geo_provider, row.geo_formatted) == (
        g.GEO_EXACT,
        "dadata~approx",
        "СНТ Солнечный, 59",
    )
    assert (row.geo_lat, row.geo_lon) == (51.6, 55.2)
    assert [p.area for p in места] == ["СНТ Солнечный"]
    assert [(л["rule"], л["was"]) for л in логи if л["event"] == "geocode.auto_decided"] == [
        (g.RULE_AREA_POINT, g.GEO_STREET_MISMATCH)
    ]


# ── воркер: спор города об улице ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("текст", "дом_города", "пригород"),
    [
        ("ул Ленина 5", ГОРОД_ПРКТ, СМЫШЛЯЕВКА),
        (
            "Комсомольский проспект 5",
            К_САМАРА,
            dataclasses.replace(СМЫШЛЯЕВКА, street="Комсомольский проспект"),
        ),
    ],
)
async def test_спор_города_об_улице_область_не_решает(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_отвечает: dict,
    точка_города: dict,
    текст: str,
    дом_города: g.GeoHit,
    пригород: g.GeoHit,
) -> None:
    """Обе карты города нашли дом клиента на улице той же основы чужого типа —
    спор о типе внутри города: область не спрашивается (одноимённая улица
    нужного типа в пригороде была бы подменой), строка остаётся `street_mismatch`
    без точки, вариантов и строки. Положительный близнец —
    `test_область_после_street_mismatch_пригород`."""
    found = _разобрать(текст)
    assert (found.street, found.level) == (текст.rsplit(" ", 1)[0], "A")
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[дом_города], [дом_города, пригород]]
    osm_отвечает["ответ"] = [дом_города]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", found)
    with structlog.testing.capture_logs() as логи:
        assert (
            await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
            == g.GEO_STREET_MISMATCH
        )
    row = await _row(db_sessionmaker, cid)
    assert (row.geo_status, row.geo_formatted, row.geo_lat, row.geo_variants or []) == (
        g.GEO_STREET_MISMATCH,
        None,
        None,
        [],
    )
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        is None
    )
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    assert len(dadata_отвечает["запросы"]) == 1, "область не спрашивали"
    события = {л["event"] for л in логи}
    assert "geocode.city_disputes_street" in события
    assert "geocode.suburb_accepted" not in события


async def test_мешаный_путь_спор_города_пригород_не_принимается(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """(A8) Дыра мешаного пути: DaData по городу отдала «пр-кт Ленина, 5», OSM
    пуст → `not_found` → область как сегодня → единственный «ул Ленина, 5» в
    Смышляевке. До правки — пригород и exact; теперь — вариант оператору.
    Близнец без спора (город пуст) — `test_единственный_дом_в_40_км_от_города_это_адрес`."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[ГОРОД_ПРКТ], [ГОРОД_ПРКТ, СМЫШЛЯЕВКА]]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    with structlog.testing.capture_logs() as логи:
        assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_ELSEWHERE and len(row.geo_variants or []) == 1
    assert row.geo_variants[0]["formatted"] == "ул Ленина, 5, Смышляевка"
    assert (row.geo_formatted, row.geo_lat, row.geo_lon) == (None, None, None)
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    assert [л["status"] for л in логи if л["event"] == "geocode.city_disputes_street"] == [
        g.GEO_NOT_FOUND
    ]
    assert "geocode.suburb_accepted" not in {л["event"] for л in логи}
