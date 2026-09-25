"""Крым: фильтр DaData по КЛАДР вместо ISO и региональный догон (пакет 2, 19.09).

Что было: шлюз ограничивал поиск «по области» для Крыма и Севастополя кодами
`RU-CR`/`RU-SEV`. У DaData таких кодов нет (её справочник живёт по
международному ISO: `UA-43`/`UA-40`), а фильтр по несуществующему коду отдаёт
пустой `suggestions` молча — все места Крыма с типом пункта («город
Бахчисарай», «пгт Мирный») и дома, искавшиеся по области, уходили в
`not_found`. Стенд 19.09 (четыре живых запроса «город Бахчисарай»): `RU-CR` →
0, `kladr_id 91` → 3, `UA-43` → 3, без фильтра → 3.

Что стережётся здесь (со стороны LeadChat; сам фильтр — в `gateway/tests`):
- договор между слоями: всякий крымский слаг справочника объявлений ведёт в
  фильтр КЛАДР 91/92 и ни в какой ISO (память ssylka-na-storozha-kotorogo-net);
- вердикт LeadChat к ответу DaData по Крыму готов: дом Ялты — `exact`, места
  — `exact` с точкой пункта/города, сторож пункта не ослаб;
- воркер зовёт место с типом через фильтр РЕГИОНА (`city=None`), а не города —
  именно этот путь и был мёртв;
- `address-recheck --regions` отбирает строки через `conversation_city` (одна
  функция на всех: колонка, без неё — ссылка), печатает счётчики, не падает на
  несуществующем регионе и потолке среза, без опции ведёт себя как раньше.

Сеть здесь не ходит. Образцы ответов DaData: живьём 19.09 сняты
`region_iso_code`, `region_kladr_id` и `value` первой подсказки каждого ответа;
`value` второй подсказки «Бахчисарая» у пробы был «г Симферополь, ул
Бахчисарайская» (без региона) — здесь он с регионом для читаемости, разбор поле
`value` не читает. Раскладка остальных полей `data` — по образцам 12–18.09
(лимит живых запросов ушёл на проверку гипотезы). ПД нет: объекты справочника;
«Войкова 37» в отчётах стенда не встречается.
"""

from __future__ import annotations

from typing import Any

import pytest
from leadchat_gateway.providers import dadata as gw_dadata

from app import cli
from app.integrations import gateway
from app.integrations.avito.listing_url import CITIES, City, region_by_prefix
from app.models import ClientAddressCandidate
from app.services import address_parse
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit import test_geo_1809 as стенд
from tests.unit.test_autobind_ops_1809 import _диалог
from tests.unit.test_geo_1809 import _row, _режим, _строка, ctx

#: Карты подменены — фикстуры стенда 18.09 под своими именами (присваивание, не
#: импорт: pytest собирает их по имени модуля).
osm_пусто = стенд.osm_пусто

pytestmark = pytest.mark.anyio

ЯЛТА = City("Ялта", "Республика Крым", "Europe/Simferopol")
СИМФЕРОПОЛЬ = City("Симферополь", "Республика Крым", "Europe/Simferopol")
ЕВПАТОРИЯ = City("Евпатория", "Республика Крым", "Europe/Simferopol")


def _данные(**kw: Any) -> dict[str, Any]:
    """Поле `data` подсказки DaData по Крыму: регион один на всех."""
    d: dict[str, Any] = {
        "region_iso_code": "UA-43",
        "region_kladr_id": "9100000000000",
        "region_with_type": "Респ Крым",
        "area_with_type": None,
        "city": None,
        "city_with_type": None,
        "settlement": None,
        "settlement_with_type": None,
        "street_with_type": None,
        "house": None,
        "geo_lat": None,
        "geo_lon": None,
        "qc_geo": None,
        "fias_level": None,
    }
    d.update(kw)
    return d


#: «город Бахчисарай» с фильтром `[{"kladr_id":"91"},{"kladr_id":"92"}]`: город
#: (уровень 4), улица-тёзка в Симферополе (7), район (3 — не место).
ОТВЕТ_БАХЧИСАРАЙ = {
    "suggestions": [
        {
            "value": "респ Крым, г Бахчисарай",
            "data": _данные(
                area_with_type="Бахчисарайский р-н",
                city="Бахчисарай",
                city_with_type="г Бахчисарай",
                geo_lat="44.751407",
                geo_lon="33.875445",
                qc_geo="4",
                fias_level="4",
            ),
        },
        {
            "value": "респ Крым, г Симферополь, ул Бахчисарайская",
            "data": _данные(
                city="Симферополь",
                city_with_type="г Симферополь",
                street_with_type="ул Бахчисарайская",
                geo_lat="44.9727",
                geo_lon="34.1096",
                qc_geo="2",
                fias_level="7",
            ),
        },
        {
            "value": "респ Крым, Бахчисарайский р-н",
            "data": _данные(
                area_with_type="Бахчисарайский р-н",
                geo_lat="44.75",
                geo_lon="33.87",
                qc_geo="4",
                fias_level="3",
            ),
        },
    ]
}

#: «пгт Мирный»: посёлок в округе Евпатории (уровень 6) и улица в нём (7).
ОТВЕТ_МИРНЫЙ = {
    "suggestions": [
        {
            "value": "респ Крым, г Евпатория, пгт Мирный",
            "data": _данные(
                city="Евпатория",
                city_with_type="г Евпатория",
                settlement="Мирный",
                settlement_with_type="пгт Мирный",
                geo_lat="45.26",
                geo_lon="33.03",
                qc_geo="4",
                fias_level="6",
            ),
        },
        {
            "value": "респ Крым, г Евпатория, пгт Мирный, ул Мирная",
            "data": _данные(
                city="Евпатория",
                city_with_type="г Евпатория",
                settlement="Мирный",
                settlement_with_type="пгт Мирный",
                street_with_type="ул Мирная",
                geo_lat="45.261",
                geo_lon="33.031",
                qc_geo="2",
                fias_level="7",
            ),
        },
    ]
}


def _дом_войкова(город: str, дом: str, уровень: str, lat: str, lon: str) -> dict[str, Any]:
    return {
        "value": f"респ Крым, г {город}, ул Войкова, д {дом}",
        "data": _данные(
            city=город,
            city_with_type=f"г {город}",
            street_with_type="ул Войкова",
            house=дом,
            geo_lat=lat,
            geo_lon=lon,
            qc_geo="0",
            fias_level=уровень,
        ),
    }


#: «ул Войкова 37» по области: дом Ялты и квартира в нём, тёзка в Керчи,
#: соседний номер.
ОТВЕТ_ВОЙКОВА = {
    "suggestions": [
        _дом_войкова("Ялта", "37", "8", "44.495", "34.166"),
        _дом_войкова("Ялта", "37", "9", "44.495", "34.166"),
        _дом_войкова("Керчь", "37", "8", "45.35", "36.47"),
        _дом_войкова("Ялта", "3", "8", "44.494", "34.165"),
    ]
}


def _места(ответ: dict[str, Any]) -> list[g.PlaceHit]:
    """Разбор живёт в шлюзе; вердикту нужны dataclass-ы LeadChat."""
    return [g.PlaceHit(**h.model_dump()) for h in gw_dadata.parse_places(ответ)]


def _дома(ответ: dict[str, Any]) -> list[g.GeoHit]:
    return [g.GeoHit(**h.model_dump()) for h in gw_dadata.parse_response(ответ)]


# ── договор между слоями ──────────────────────────────────────────────────────


def test_слаги_крыма_ведут_в_фильтр_кладр() -> None:
    """Ломается, если регион переименуют в справочнике слагов или в словаре
    шлюза — с любой стороны: строки регионов — ключи словаря шлюза. Шесть
    известных городов обязаны быть, но новый крымский город в `CITIES`
    (правило сопровождения `listing_url`) договор не ломает — цикл ниже идёт
    по всем крымским (ревью 19.09)."""
    крымские = [c for c in CITIES.values() if c.tz == "Europe/Simferopol"]
    assert {c.name for c in крымские} >= {
        "Симферополь",
        "Севастополь",
        "Керчь",
        "Ялта",
        "Евпатория",
        "Феодосия",
    }
    регионы = {c.region for c in крымские} | {region_by_prefix("respublika_krym")}
    assert регионы == {"Республика Крым", "Севастополь"}
    for регион in регионы:
        фильтр = gw_dadata.locations_for(region=регион, city=None)
        assert {"kladr_id": "91"} in фильтр and {"kladr_id": "92"} in фильтр, регион
        assert not any("region_iso_code" in ф for ф in фильтр), регион
    # Контрпример: «Крым» внутри слова — не Крым.
    крымск = CITIES["krymsk"]
    assert крымск.region == "Краснодарский край"
    assert gw_dadata.locations_for(region=крымск.region, city=None) == [{"region": "Краснодарский"}]


# ── вердикт LeadChat к ответу DaData по Крыму ─────────────────────────────────


def test_ялта_войкова_37_по_области_exact() -> None:
    found = address_parse.parse("ул. Войкова 37")
    assert found is not None
    parsed = g.Parsed(
        street=found.street,
        house=found.house,
        settlement=found.settlement,
        settlement_type=found.settlement_type,
        locality=found.locality,
        level=found.level,
    )
    hits = _дома(ОТВЕТ_ВОЙКОВА)
    assert [(h.city, h.house, h.house_level, h.precise) for h in hits] == [
        ("Ялта", "37", True, True),
        ("Ялта", "37", True, True),
        ("Керчь", "37", True, True),
        ("Ялта", "3", True, True),
    ]
    статус, дом = g.verdict(parsed, ЯЛТА, hits)
    assert статус == g.GEO_EXACT and дом is not None
    assert (дом.city, дом.house, дом.region) == ("Ялта", "37", "Респ Крым")
    assert g.format_address(дом, parsed) == "ул Войкова, 37, Ялта"
    assert g.card_grade("house", g.GEO_EXACT, "dadata", дом.lat, дом.lon, None) == g.GRADE_EXACT
    # Севастопольское объявление тот же ответ по региону не отвергнет.
    assert g.region_matches("Севастополь", hits[0].region)
    assert g.region_matches("Республика Крым", hits[0].region)


def test_бахчисарай_и_мирный_места_крыма() -> None:
    места = _места(ОТВЕТ_БАХЧИСАРАЙ)
    # Район (уровень 3) местом не стал.
    assert [(h.kind, h.city) for h in места] == [("city", "Бахчисарай"), ("street", "Симферополь")]
    бахчисарай = g.Place(settlement="Бахчисарай", settlement_type="город", area=None, district=None)
    статус, hit = g.place_verdict(бахчисарай, СИМФЕРОПОЛЬ, места)
    assert статус == g.GEO_EXACT and hit is not None and hit.kind == "city"
    assert (hit.lat, hit.lon) == (44.751407, 33.875445)
    assert "Бахчисарай" in g.format_place(hit, бахчисарай)

    мирный = g.Place(settlement="Мирный", settlement_type="пгт", area=None, district=None)
    статус, hit = g.place_verdict(мирный, ЕВПАТОРИЯ, _места(ОТВЕТ_МИРНЫЙ))
    assert статус == g.GEO_EXACT and hit is not None
    assert (hit.kind, hit.settlement, hit.city) == ("settlement", "Мирный", "Евпатория")
    assert g.format_place(hit, мирный) == "пгт Мирный, Евпатория"

    # Контрпример: фильтр региона не ослабил сторож пункта — чужое село против
    # ответа про Бахчисарай остаётся «пункт не совпал», а не exact.
    дубровское = g.Place(settlement="Дубровское", settlement_type="село", area=None, district=None)
    assert g.place_verdict(дубровское, СИМФЕРОПОЛЬ, места) == (g.GEO_SETTLEMENT_MISMATCH, None)


# ── воркер: место с типом идёт через фильтр РЕГИОНА ───────────────────────────


async def test_воркер_место_крыма_через_подмену_шлюза(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    monkeypatch: Any,
    osm_пусто: Any,
) -> None:
    """У пункта с типом при объявлении в Симферополе воркер зовёт
    `search_place(region="Республика Крым", city=None)` — тот самый путь, где
    фильтр `RU-CR` отдавал пустоту. С ответом DaData строка становится `exact`,
    карта — `dadata`, степень карточки у места — `approx`."""
    await _режим(db_sessionmaker, "nominatim")
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    вызовы: list[tuple[str | None, str | None]] = []

    async def search_place(
        place: g.Place, *, region: str | None, city: str | None = None, **kw: Any
    ) -> list[g.PlaceHit]:
        await kw["on_request"]()
        вызовы.append((region, city))
        assert place.query_text == "город Бахчисарай"
        return _места(ОТВЕТ_БАХЧИСАРАЙ)

    monkeypatch.setattr(worker.dadata, "search_place", search_place)
    found = address_parse.parse_place("город Бахчисарай")
    assert found is not None and found.kind == address_parse.KIND_PLACE
    cid = await _строка(seed_conversation, db_sessionmaker, "simferopol", found)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert вызовы == [("Республика Крым", None)]
    row = await _row(db_sessionmaker, cid)
    assert (row.kind, row.geo_status, row.geo_provider) == ("place", g.GEO_EXACT, "dadata")
    assert (row.geo_lat, row.geo_lon) == (44.751407, 33.875445)
    assert row.geo_formatted and "Бахчисарай" in row.geo_formatted
    assert (
        g.card_grade(
            row.kind, row.geo_status, row.geo_provider, row.geo_lat, row.geo_lon, row.geo_formatted
        )
        == g.GRADE_APPROX
    )


# ── address-recheck --regions ─────────────────────────────────────────────────


async def test_address_recheck_regions_отбирает_по_conversation_city(
    db_sessionmaker: Any, make_avito_account: Any, capsys: Any
) -> None:
    """Семь строк `not_found` в `pending`; регион — через `conversation_city`:
    колонка, без неё — ссылка. Слаг вне справочника и строка без слага —
    считаются и печатаются, а не выпадают молча. «Крым» внутри слова — не Крым."""
    account = await make_avito_account()
    строки = [
        ("yalta", None),
        ("habarovsk", None),
        ("kerch-2", None),
        (None, "https://www.avito.ru/yalta/predlozheniya_uslug/remont_1234567890"),
        (None, None),
        ("krymsk", None),
        ("sevastopol_gagarinskiy", None),
    ]
    ids: dict[str, Any] = {}
    async with db_sessionmaker() as s:
        for слаг, ссылка in строки:
            _, conv, row = await _диалог(
                s,
                account.id,
                текст="город Бахчисарай",
                строка={
                    "status": "pending",
                    "kind": "place",
                    "geo_status": "not_found",
                    "geo_lat": None,
                    "geo_lon": None,
                    "geo_formatted": None,
                },
            )
            conv.item_city_slug = слаг
            conv.item_url = ссылка
            assert row is not None
            ids[слаг or ссылка or "пусто"] = row.id
        await s.commit()

    async def статусы() -> dict[str, str]:
        async with db_sessionmaker() as s:
            out = {}
            for ключ, rid in ids.items():
                row = await s.get(ClientAddressCandidate, rid)
                assert row is not None
                out[ключ] = row.geo_status
            return out

    все_не_найдены = dict.fromkeys(ids, "not_found")

    # Несуществующий регион — 0 строк и печать, не ошибка.
    async with db_sessionmaker() as s:
        await cli.run_address_recheck(
            s, statuses="not_found", regions="Нет такого региона", dry_run=False
        )
    assert "0 строк из 7" in capsys.readouterr().out
    assert await статусы() == все_не_найдены

    # Срез шире потолка — печать и выход, ничего не тронуто.
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(cli, "_ПОТОЛОК_РЕГИОНАЛЬНОГО_СРЕЗА", 1)
        async with db_sessionmaker() as s:
            await cli.run_address_recheck(
                s, statuses="not_found", regions="Республика Крым", dry_run=False
            )
    assert "срез шире потолка 1" in capsys.readouterr().out
    assert await статусы() == все_не_найдены

    # Сухой прогон: счётчики, ничего не записано.
    async with db_sessionmaker() as s:
        await cli.run_address_recheck(
            s, statuses="not_found", regions=" Республика Крым ", dry_run=True
        )
    вывод = capsys.readouterr().out
    assert "регионы ['Республика Крым']: 2 строк из 7" in вывод
    assert "слаг вне справочника: 1" in вывод and "без слага: 1" in вывод
    assert "регионов ['Республика Крым']: 2; сухой прогон: True" in вывод
    assert await статусы() == все_не_найдены

    # Боевой: Крым и Севастополь — колонка, ссылка и составной слаг Севастополя.
    async with db_sessionmaker() as s:
        await cli.run_address_recheck(
            s, statuses="not_found", regions="Республика Крым,Севастополь", dry_run=False
        )
    вывод = capsys.readouterr().out
    assert "3 строк из 7" in вывод and "возвращено в очередь: 3" in вывод
    assert await статусы() == {
        "yalta": "pending",
        "habarovsk": "not_found",
        "kerch-2": "not_found",
        "https://www.avito.ru/yalta/predlozheniya_uslug/remont_1234567890": "pending",
        "пусто": "not_found",
        "krymsk": "not_found",
        "sevastopol_gagarinskiy": "pending",
    }
    async with db_sessionmaker() as s:
        сброшенная = await s.get(ClientAddressCandidate, ids["yalta"])
        assert сброшенная is not None
        assert (сброшенная.geo_attempts, сброшенная.geo_checked_at) == (0, None)

    # Без опции — поведение прежнее: все оставшиеся в очередь.
    async with db_sessionmaker() as s:
        await cli.run_address_recheck(s, statuses="not_found", dry_run=False)
    вывод = capsys.readouterr().out
    assert "регионы" not in вывод and "возвращено в очередь: 4" in вывод
    assert set((await статусы()).values()) == {"pending"}
