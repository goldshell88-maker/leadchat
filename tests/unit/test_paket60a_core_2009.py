"""Пакет 6.0а, ядро (20.09): след решения, политика правил, предложения.

Программа «автоматически и точно» §0.3/§1.1 (I-1, I-2, I-5-контракт, I-9-ядро):

* `trace jsonb` + `region text` у строки: след разбора пишет
  `record_address_candidate`, след суда — воркер (`geocode.verdict_trace`),
  сброс уводит прежний суд в `trace.prev` (`сброс_вердикта`), удержание
  прежнего вердикта возвращает его след;
* политика правил — ВХОД `auto_decide(e, policy)`: `off` — правило само
  молчит и `or`-цепочка идёт дальше (не пост-фильтр); `shadow` — двойной суд,
  разница в `Shadow`; `suggest` — хвост `~suggest`, `card_grade` → None,
  автозаписи нет, принятие снимает хвост; `approx` — хвост всегда;
* реестры `RULE_DEFAULT_POLICY`/`RULE_LABEL` (у каждого `RULE_*` подпись и
  политика) и `address_parse.PARSE_RULES`; настройки `address_geo.rule_policy`
  и `address_parse.rules` с 400 на неизвестное имя;
* `geo_view` отдаёт `rule`, `rule_label`, `suggest`; `_то_же_место` для пары
  «не обе exact» различает названные пункты.

ДИВЕРСИИ (каждая обязана краснеть): убрать правило из `RULE_LABEL` или
`RULE_DEFAULT_POLICY` — `test_у_каждого_правила_подпись_и_политика`; снять
проверку имени в `parse_rule_policy` — `test_parse_rule_policy_валидация` и
`test_настройка_rule_policy_неизвестное_имя_400`; вернуть политику
пост-фильтром после `or`-цепочки (`_под_политикой(auto_decide(e), …)`) —
`test_off_не_пост_фильтр_цепочка_идёт_дальше` (street_point вместо None);
сравнивать тень с боевым только по строке карты — `test_shadow_не_меняет_вердикт_и_даёт_тень`
(дом-голова и точка улицы печатаются одинаково); убрать `point_is_suggest` из
`card_grade` — `test_suggest_card_grade_none_и_хвост`; снять `strip_suggest` в
`point_precision` — тот же тест (точка предложения стала бы «none»); не
накладывать ворота `in_named_city` поверх правила — `test_ворота_in_named_city`;
не уводить суд в `prev` при сбросе — `test_сброс_вердикта_кладёт_суд_в_prev`;
писать `trace` у `pending` в `_пометить` — `test_воркер_не_трогает_след_у_отложенной`;
убрать `_другой_пункт` из `_то_же_место` — `test_то_же_место_различает_названные_пункты`;
ставить автозапись при `~suggest` в воркере — `test_воркер_suggest_пишет_хвост_без_автозаписи`
(ключ `addr-fill` появится); чистить варианты у предложения —
`test_воркер_пригород_под_политикой` (вариант пропадёт); не снимать хвост при
принятии — `test_принятие_снимает_хвост_suggest`; не возвращать след при
удержании прежнего вердикта (`verdict_trace` всегда) —
`test_воркер_удержал_прежний_вердикт_и_вернул_его_след`; вписать новое
`RULE_*` в реестр с умолчанием `exact` — `test_у_каждого_правила_подпись_и_политика`
(новое правило рождается в тени, §0.3); снять ворота правила (10) с ветки
дроби (вернуть в `_settlement_in_street` голый `_дробь_решена`) —
`test_ворота_settlement_in_street_поверх_дроби` (`off` перестаёт молчать на
«Кола победы 4/37»); убрать `point_is_suggest` из ворот `refresh_auto_address`
— `test_пересуд_источника_под_suggest_не_трогает_карточку` (в поле лягут слова
клиента с `verdict_refused`); читать `address_geo.rule_policy` в цикле по
городам — `test_политика_читается_один_раз_на_задачу` (шпион насчитает больше
одного). Все диверсии прогнаны 20.09 скриптом «правка → тест → откат по
хешу»: каждая красная.

Адреса — стенды 18.09 (Орск, Псков/Сорокино, Самара/Смышляевка); ПД нет.
"""

from __future__ import annotations

import dataclasses
import inspect
import re
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
import structlog

from app.integrations import gateway
from app.integrations.avito.listing_url import City
from app.models import AppSetting, AuditLog, Client, ClientAddressCandidate
from app.models.client import CANDIDATE_PENDING
from app.services import address_parse, app_settings
from app.services import clients as clients_svc
from app.services import geocode as g
from app.workers import geocode as worker
from tests.unit import test_autobind_policy_1809 as п
from tests.unit import test_geo_1809 as стенд
from tests.unit import test_paket4_kola_1909 as кола
from tests.unit import test_paket4_neighbour_1909 as сосед
from tests.unit import test_paket5_2009 as п5
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

# Фикстуры соседних стендов — присваиванием, не импортом имени (ruff F811).
dadata_отвечает = стенд.dadata_отвечает
osm_пусто = стенд.osm_пусто
точка_города = стенд.точка_города
dadata_пусто = п5.dadata_пусто
яндекс = п5.яндекс

pytestmark = pytest.mark.anyio

ОРСК = City("Орск", "Оренбургская область", "Asia/Yekaterinburg")
ВОЛГОДОНСК = City("Волгодонск", "Ростовская область", "Europe/Moscow")


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _evidence(**kw: Any) -> g.Evidence:
    return п._evidence(**kw)


def _цепочка() -> g.Evidence:
    """Улики, где срабатывают ДВА правила подряд: «13/38» не найден, дом «13»
    есть (`fraction_head`), улица без дома есть (`street_point`)."""
    return _evidence(
        status=g.GEO_HOUSE_MISSING,
        parsed=g.Parsed(street="ул Ленина", house="13/38"),
        city_hits=[
            ([дом(house="13"), дом(house=None, house_level=False, precise=False)], "dadata")
        ],
    )


# ── реестры ───────────────────────────────────────────────────────────────────

#: Правила 18–20.09 поимённо — те, что на 20.09 уже писали в карточку и потому
#: получили умолчание `exact` («как есть в коде»). Набор закрыт: правило
#: пакета 7 и позже сюда не вписывается, а рождается в тени (§0.3, К-3).
_ПРАВИЛА_2009 = frozenset(
    {
        g.RULE_ONLY_IN_RADIUS,
        g.RULE_FULLEST_STREET,
        g.RULE_ONE_SPOT,
        g.RULE_BEST_OF,
        g.RULE_STREET_POINT,
        g.RULE_FRACTION_HEAD,
        g.RULE_HOUSE_FAMILY,
        g.RULE_AREA_POINT,
        g.RULE_COUNTRY,
        g.RULE_NEIGHBOUR_SETTLEMENT,
        g.RULE_SETTLEMENT_IN_STREET,
        g.RULE_SUBURB,
        g.RULE_IN_NAMED_CITY,
    }
)


def test_у_каждого_правила_подпись_и_политика() -> None:
    """Правило рождается с именем, подписью и степенью в одном месте (I-9):
    каждый `RULE_*` модуля — в обоих реестрах, и в реестрах нет чужих имён.

    Умолчание степени — по возрасту правила: 13 правил 18–20.09 поимённо
    `exact` (они уже писали в карточку), любое правило вне набора — рождение
    в `off`/`shadow`/`suggest`, не в `approx`/`exact` (§0.3: первый замер идёт
    по тени в бою). Диверсия: вписать новое `RULE_*` в реестр с `exact` —
    красный; переставить старое в `shadow` — тоже красный."""
    правила = {v for k, v in vars(g).items() if k.startswith("RULE_") and isinstance(v, str)}
    assert правила, "константы RULE_* не найдены"
    assert set(g.RULE_LABEL) == правила
    assert set(g.RULE_DEFAULT_POLICY) == правила
    assert all(label.strip() for label in g.RULE_LABEL.values())
    assert set(g.RULE_DEFAULT_POLICY.values()) <= set(g.POLICIES)
    assert _ПРАВИЛА_2009 <= правила
    # Правила 18–20.09 — «как есть»: ровно они и только они стартуют в exact.
    assert {r: g.RULE_DEFAULT_POLICY[r] for r in _ПРАВИЛА_2009} == dict.fromkeys(
        _ПРАВИЛА_2009, g.POLICY_EXACT
    )
    новые = {r: g.RULE_DEFAULT_POLICY[r] for r in правила - _ПРАВИЛА_2009}
    assert all(p not in (g.POLICY_APPROX, g.POLICY_EXACT) for p in новые.values()), новые
    assert g.rule_label(g.RULE_STREET_POINT) == "точка улицы, дом не найден"
    assert g.rule_label(None) is None and g.rule_label("нет такого") is None


def test_parse_rule_policy_валидация() -> None:
    assert g.parse_rule_policy("") == {} and g.parse_rule_policy(None) == {}
    assert g.parse_rule_policy(" street_point = Suggest ; suburb=shadow\nbest_of=off,") == {
        "street_point": "suggest",
        "suburb": "shadow",
        "best_of": "off",
    }
    with pytest.raises(ValueError, match="неизвестное правило"):
        g.parse_rule_policy("street_poin=off")
    with pytest.raises(ValueError, match="неизвестная политика"):
        g.parse_rule_policy("street_point=maybe")
    with pytest.raises(ValueError, match="правило=политика"):
        g.parse_rule_policy("street_point")
    # Настройка перекрывает умолчание; неизвестное реестру имя — тень (К-3).
    assert g.rule_policy(None, g.RULE_STREET_POINT) == g.POLICY_EXACT
    assert g.rule_policy({"street_point": "off"}, g.RULE_STREET_POINT) == g.POLICY_OFF
    assert g.rule_policy({}, "new_rule_from_paket_7") == g.POLICY_SHADOW
    # Правило разбора: пустая строка — одни умолчания реестра (с 7a реестр
    # не пуст: стоп-классы и эхо, все `off`); чужое имя по-прежнему неизвестно.
    assert address_parse.rules_from_setting("") == dict(address_parse.PARSE_RULES)
    assert all(v == address_parse.PARSE_OFF for v in address_parse.PARSE_RULES.values())
    with pytest.raises(ValueError, match="неизвестное правило разбора"):
        address_parse.rules_from_setting("nope_rule_2109=on")


# ── политика — вход auto_decide ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("правило", "улики"),
    [
        (
            g.RULE_STREET_POINT,
            lambda: _evidence(
                status=g.GEO_NOT_FOUND,
                parsed=g.Parsed(street="ул Ленина", house="5"),
                city_hits=[([дом(house=None, house_level=False, precise=False)], "dadata")],
            ),
        ),
        (
            g.RULE_FRACTION_HEAD,
            lambda: _evidence(
                status=g.GEO_HOUSE_MISMATCH,
                parsed=g.Parsed(street="ул Ленина", house="13/38"),
                city_hits=[([дом(house="13")], "dadata")],
            ),
        ),
        (
            g.RULE_HOUSE_FAMILY,
            lambda: _evidence(
                status=g.GEO_HOUSE_MISMATCH,
                parsed=g.Parsed(street="ул Ленина", house="29"),
                city_hits=[([дом(house="29А")], "dadata")],
            ),
        ),
        (g.RULE_COUNTRY, lambda: _evidence(status=g.GEO_EXACT, city=None, hit=дом(), country=True)),
        (
            g.RULE_ONE_SPOT,
            lambda: _evidence(
                status=g.GEO_AMBIGUOUS,
                hits=[
                    дом(settlement="Заречный", lat=51.2000),
                    дом(settlement="Крыловка", lat=51.2012),
                ],
            ),
        ),
        (
            g.RULE_BEST_OF,
            lambda: _evidence(
                status=g.GEO_AMBIGUOUS,
                hits=[дом(settlement="Заречный", lat=51.3), дом(settlement="Крыловка", lat=51.4)],
                hints=["Заречный"],
            ),
        ),
        (
            g.RULE_FULLEST_STREET,
            lambda: _evidence(
                parsed=g.Parsed(street="Красная Керчь", house="5"),
                hits=[
                    дом(street="Красная улица", lat=51.20),
                    дом(street="улица Красная Керчь", lat=51.25),
                ],
            ),
        ),
        (
            g.RULE_ONLY_IN_RADIUS,
            lambda: _evidence(
                status=g.GEO_ELSEWHERE,
                city=п.САМАРА_ГОРОД,
                region_hits=[СМЫШЛЯЕВКА, п.СЫЗРАНЬ],
                variants=[{"formatted": "x"}, {"formatted": "y"}],
                city_point=САМАРА,
            ),
        ),
        # Стенд соседа за забором (Кленово/Липовка, 19.09) — как есть.
        (g.RULE_NEIGHBOUR_SETTLEMENT, сосед._улики),
        (
            g.RULE_AREA_POINT,
            lambda: _evidence(
                status=g.GEO_HOUSE_MISSING,
                parsed=g.Parsed(
                    street="СНТ Урожай", house="273", settlement="Урожай", settlement_type="СНТ"
                ),
                place=g.Place(
                    settlement=None,
                    settlement_type=None,
                    area="СНТ Урожай",
                    district=None,
                    street="",
                ),
                place_hits=[п._массив("СНТ Урожай")],
            ),
        ),
        # Прочтение (10), ветка (i): дом «4» целиком в Коле — решает своим
        # именем. Ветка (ii) (дробь) — отдельный тест: там `rule=fraction_head`.
        (
            g.RULE_SETTLEMENT_IN_STREET,
            lambda: кола._улики(parsed=g.Parsed(street="Кола победы", house="4", level="B")),
        ),
    ],
)
def test_off_равно_как_без_правила_а_политика_меняет_степень(правило: str, улики: Any) -> None:
    """На каждом правиле: без политики — решает; `off` — не решает оно
    (None или следующее в цепочке); `suggest`/`approx` — то же решение с
    политикой; `shadow` в боевом суде = `off`."""
    e = улики()
    решение = g.auto_decide(e)
    assert решение is not None and решение.rule == правило, правило
    assert решение.policy == g.POLICY_EXACT
    без = g.auto_decide(e, {правило: g.POLICY_OFF})
    assert без is None or без.rule != правило
    assert g.auto_decide(e, {правило: g.POLICY_SHADOW}) == без
    предложение = g.auto_decide(e, {правило: g.POLICY_SUGGEST})
    assert predloz(предложение) == (правило, g.POLICY_SUGGEST, решение.approx)
    приблизительно = g.auto_decide(e, {правило: g.POLICY_APPROX})
    assert predloz(приблизительно) == (правило, g.POLICY_APPROX, True)
    # Явный `exact` = как без политики.
    assert g.auto_decide(e, {правило: g.POLICY_EXACT}) == решение


def predloz(d: g.Decision | None) -> tuple[str, str, bool] | None:
    return (d.rule, d.policy, d.approx) if d is not None else None


def test_off_не_пост_фильтр_цепочка_идёт_дальше() -> None:
    """К-4: политика — вход правила. Дробь выключена → решает следующее
    правило цепочки (точка улицы), а не «ничего»."""
    e = _цепочка()
    assert g.auto_decide(e).rule == g.RULE_FRACTION_HEAD  # type: ignore[union-attr]
    дальше = g.auto_decide(e, {g.RULE_FRACTION_HEAD: g.POLICY_OFF})
    assert дальше is not None and дальше.rule == g.RULE_STREET_POINT
    # Оба выключены — только тогда None.
    assert (
        g.auto_decide(e, {g.RULE_FRACTION_HEAD: g.POLICY_OFF, g.RULE_STREET_POINT: g.POLICY_OFF})
        is None
    )


def test_shadow_не_меняет_вердикт_и_даёт_тень() -> None:
    e = _цепочка()
    политика = {g.RULE_FRACTION_HEAD: g.POLICY_SHADOW}
    боевое = g.auto_decide(e, политика)
    assert боевое is not None and боевое.rule == g.RULE_STREET_POINT
    тень = g.shadow_decide(e, политика)
    assert тень is not None
    assert (тень.rule, тень.status, тень.instead) == (
        g.RULE_FRACTION_HEAD,
        g.GEO_EXACT,
        "street_point",
    )
    assert тень.key == "ул Ленина, 13/38, Орск"
    assert тень.as_trace() == {
        "rule": "fraction_head",
        "status": "exact",
        "key": "ул Ленина, 13/38, Орск",
        "km": None,
        "instead": "street_point",
    }
    # Тень без правил в тени — пусто; тень при молчащем боевом — `instead=None`.
    assert g.shadow_decide(e, {}) is None
    одна = g.shadow_decide(
        e, {g.RULE_FRACTION_HEAD: g.POLICY_SHADOW, g.RULE_STREET_POINT: g.POLICY_OFF}
    )
    assert одна is not None and одна.instead is None
    # `Shadow` без ПД: только имя, статус, ключ дома, км, имя боевого.
    assert set(тень.as_trace()) == {"rule", "status", "key", "km", "instead"}


def test_ворота_in_named_city() -> None:
    """Ворота ветки поверх правила — строже из двух."""
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
    assert g.auto_decide(e).rule == g.RULE_BEST_OF  # type: ignore[union-attr]
    assert g.auto_decide(e, {g.RULE_IN_NAMED_CITY: g.POLICY_OFF}) is None
    assert g.auto_decide(e, {g.RULE_IN_NAMED_CITY: g.POLICY_SHADOW}) is None
    тень = g.shadow_decide(e, {g.RULE_IN_NAMED_CITY: g.POLICY_SHADOW})
    assert тень is not None and тень.rule == g.RULE_BEST_OF
    предложение = g.auto_decide(e, {g.RULE_IN_NAMED_CITY: g.POLICY_SUGGEST})
    assert predloz(предложение) == (g.RULE_BEST_OF, g.POLICY_SUGGEST, True)
    # Правило строже ворот — остаётся правило.
    строже = g.auto_decide(
        e, {g.RULE_IN_NAMED_CITY: g.POLICY_EXACT, g.RULE_BEST_OF: g.POLICY_SUGGEST}
    )
    assert predloz(строже) == (g.RULE_BEST_OF, g.POLICY_SUGGEST, True)


def test_ворота_settlement_in_street_поверх_дроби() -> None:
    """Ревью 20.09 (#1): строка владельца «Кола победы 4/37» решается веткой
    (ii) правила (10) — именем дроби (`rule=fraction_head`, docs/47), но
    политика ворот `settlement_in_street` обязана лечь поверх: `off`/`shadow`
    гасят прочтение и в бою (иначе откат правила (10) не действовал бы ровно
    на том классе, ради которого оно заведено), тень несёт имя дроби,
    `suggest`/`approx` ложатся на решение, и из двух политик действует более
    строгая. None при `off` — окончательный (Б-2): в дробь по сырому разбору
    строка не падает."""
    e = кола._улики()
    ворота, дробь = g.RULE_SETTLEMENT_IN_STREET, g.RULE_FRACTION_HEAD
    решение = g.auto_decide(e)
    assert решение is not None and (решение.rule, решение.policy) == (дробь, g.POLICY_EXACT)
    assert (решение.parsed.settlement, решение.hit.city) == ("Кола", "Кола")
    assert g.auto_decide(e, {ворота: g.POLICY_OFF}) is None
    assert g.auto_decide(e, {ворота: g.POLICY_SHADOW}) is None
    тень = g.shadow_decide(e, {ворота: g.POLICY_SHADOW})
    assert тень is not None and тень.as_trace() == {
        "rule": "fraction_head",
        "status": "exact",
        "key": "ул Победы, 4/37, Кола",
        "km": 9.0,
        "instead": None,
    }
    assert predloz(g.auto_decide(e, {ворота: g.POLICY_SUGGEST})) == (дробь, g.POLICY_SUGGEST, True)
    assert predloz(g.auto_decide(e, {ворота: g.POLICY_APPROX})) == (дробь, g.POLICY_APPROX, True)
    # Строже из двух: дробь предложением при воротах `approx` — предложение;
    # ворота предложением при живой дроби — тоже; дробь выключена — молчит.
    assert predloz(g.auto_decide(e, {дробь: g.POLICY_SUGGEST, ворота: g.POLICY_APPROX})) == (
        дробь,
        g.POLICY_SUGGEST,
        True,
    )
    assert predloz(g.auto_decide(e, {дробь: g.POLICY_EXACT, ворота: g.POLICY_SUGGEST})) == (
        дробь,
        g.POLICY_SUGGEST,
        True,
    )
    assert g.auto_decide(e, {дробь: g.POLICY_OFF}) is None
    # Явный `exact` у ворот = как без политики.
    assert g.auto_decide(e, {ворота: g.POLICY_EXACT}) == решение


def test_policy_decision_для_пригорода() -> None:
    parsed = g.Parsed(street="ул Ленина", house="5")
    assert g.policy_decision({}, g.RULE_SUBURB, СМЫШЛЯЕВКА, False, parsed, km=17.0) is not None
    assert g.policy_decision({"suburb": "off"}, g.RULE_SUBURB, СМЫШЛЯЕВКА, False, parsed) is None
    assert g.policy_decision({"suburb": "shadow"}, g.RULE_SUBURB, СМЫШЛЯЕВКА, False, parsed) is None
    тень = g.policy_decision(
        {"suburb": "shadow"}, g.RULE_SUBURB, СМЫШЛЯЕВКА, False, parsed, shadow_as=g.POLICY_EXACT
    )
    assert тень is not None and тень.rule == g.RULE_SUBURB
    предложение = g.policy_decision({"suburb": "suggest"}, g.RULE_SUBURB, СМЫШЛЯЕВКА, False, parsed)
    assert predloz(предложение) == (g.RULE_SUBURB, g.POLICY_SUGGEST, False)


# ── suggest: судья и хвост ────────────────────────────────────────────────────


def test_suggest_card_grade_none_и_хвост() -> None:
    assert g.mark_suggest("dadata") == "dadata~suggest"
    assert g.mark_suggest("dadata~approx~suggest") == "dadata~approx~suggest"
    assert g.point_is_suggest("dadata~approx~suggest") and not g.point_is_suggest("dadata~approx")
    assert g.strip_suggest("dadata~approx~suggest") == "dadata~approx"
    assert g.strip_suggest("dadata~suggest") == "dadata" and g.strip_suggest(None) is None
    # Одна строка в единственном судье: предложение в карточку не идёт ни
    # точкой, ни текстом; после снятия хвоста — степень, записанная правилом.
    assert g.card_grade("house", "exact", "dadata~suggest", 51.2, 58.5, "ул Ленина, 5") is None
    assert g.card_grade("house", "exact", "dadata~approx~suggest", 51.2, 58.5, "x") is None
    assert g.card_grade("house", "exact", "dadata", 51.2, 58.5, "x") == g.GRADE_EXACT
    assert g.card_grade("house", "exact", "dadata~approx", 51.2, 58.5, "x") == g.GRADE_APPROX
    # Точка предложения на экране — настоящая.
    assert g.point_precision("house", "exact", "dadata~suggest", 51.2, 58.5) == g.PRECISION_EXACT
    assert (
        g.point_precision("house", "exact", "dadata~approx~suggest", 51.2, 58.5)
        == g.PRECISION_APPROX
    )
    # Ранг предложения — по уликам оператора (варианты), не по степени.
    assert (
        g.verdict_rank("house", "exact", "dadata~suggest", 51.2, 58.5, "x", [{"a": 1}])
        == g.RANK_VARIANTS
    )
    assert g.verdict_rank("house", "exact", "dadata~suggest", 51.2, 58.5, "x", None) == g.RANK_NONE


# ── след: сборка, сброс, удержание ────────────────────────────────────────────


def test_verdict_trace_ключи_prev_и_пустые_не_пишутся() -> None:
    база = {
        "parse_form": "street_house",
        "level_reason": "typed",
        "rule": "street_point",
        "policy": "exact",
        "km": 3.0,
        "query_form": "house",
    }
    след = g.verdict_trace(
        база,
        rule=g.RULE_FRACTION_HEAD,
        policy=g.POLICY_SUGGEST,
        km=1.2345,
        hit=СМЫШЛЯЕВКА,
        query_form=g.QUERY_FORM_HOUSE,
        settlement_read="Смышляевка",
        shadow=None,
    )
    assert след == {
        "parse_form": "street_house",
        "level_reason": "typed",
        "rule": "fraction_head",
        "policy": "suggest",
        "km": 1.23,
        "region": "Самарская область",
        "hit_city": "Смышляевка",
        "query_form": "house",
        "settlement_read": "Смышляевка",
        "suggest": True,
        "prev": {"rule": "street_point", "policy": "exact", "km": 3.0, "query_form": "house"},
    }
    # Без правила: только карта; `policy`/`suggest` не пишутся; prev остаётся один уровень.
    второй = g.verdict_trace(след, rule=None, policy=None, km=None, hit=None, query_form="house")
    assert второй["prev"]["rule"] == "fraction_head" and "prev" not in второй["prev"]
    assert set(второй) == {"parse_form", "level_reason", "query_form", "prev"}
    # Сброс: суд — в prev, разбор остаётся; удержание — суд возвращается.
    после_сброса = g.trace_after_reset(след)
    assert после_сброса is not None and "rule" not in после_сброса
    assert (
        после_сброса["prev"]["rule"] == "fraction_head"
        and после_сброса["parse_form"] == "street_house"
    )
    assert g.trace_after_reset(None) is None and g.trace_after_reset({}) is None
    восстановлен = g.trace_restored(после_сброса)
    assert восстановлен is not None and восстановлен["rule"] == "fraction_head"
    assert "prev" not in восстановлен
    # Суд не уходил — след как есть.
    assert g.trace_restored(след) == след
    assert set(g.TRACE_VERDICT_KEYS) >= {"rule", "policy", "km", "region", "hit_city", "shadow"}


async def test_сброс_вердикта_кладёт_суд_в_prev(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 5"))
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.trace = {"parse_form": "street_house", "rule": "street_point", "policy": "exact"}
        row.geo_status = g.GEO_EXACT
        await s.commit()
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        clients_svc.сбросить_вердикт(row, reason="test")
        await s.commit()
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING
    assert row.trace == {
        "parse_form": "street_house",
        "prev": {"rule": "street_point", "policy": "exact"},
    }
    # Строитель набора: без следа ключа нет (bulk без строки след не трогает),
    # со следом — есть; `None` стирает.
    assert "trace" not in clients_svc.сброс_вердикта(с_попытками=True, улики=None)
    assert clients_svc.сброс_вердикта(с_попытками=True, улики=None, след={"a": 1})["trace"] == {
        "a": 1
    }
    assert clients_svc.сброс_вердикта(с_попытками=True, улики=None, след=None)["trace"] is None


async def test_record_address_candidate_пишет_след_разбора_и_регион(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    found = dataclasses.replace(
        _разобрать("ул Ленина 5"),
        region="Оренбургская область",
        parse_form="street_house",
        level_reason="street_type",
        locality_form=None,
    )
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", found)
    row = await _row(db_sessionmaker, cid)
    assert row.region == "Оренбургская область"
    assert row.trace == {"parse_form": "street_house", "level_reason": "street_type"}
    # Без следа — SQL NULL, не пустой объект (урок 0083).
    без = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Пушкина 7"))
    assert (await _row(db_sessionmaker, без)).trace is None
    assert clients_svc.след_разбора(_разобрать("ул Пушкина 7")) is None


async def test_geo_view_отдаёт_правило_подпись_и_предложение(
    seed_conversation: Any, db_sessionmaker: Any
) -> None:
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 5"))
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status, row.geo_provider = g.GEO_EXACT, "dadata~approx~suggest"
        row.geo_lat, row.geo_lon, row.geo_formatted = 51.2, 58.5, "ул Ленина, 5, Орск"
        row.trace = {"rule": g.RULE_STREET_POINT, "policy": "suggest", "suggest": True}
        await s.commit()
    row = await _row(db_sessionmaker, cid)
    вид = clients_svc.geo_view(row)
    assert вид is not None
    assert (вид["rule"], вид["rule_label"], вид["suggest"]) == (
        "street_point",
        "точка улицы, дом не найден",
        True,
    )
    assert вид["precision"] == g.PRECISION_APPROX and clients_svc.candidate_grade(row) is None
    # Источник карточки читает тот же вид.
    assert clients_svc._geo_на_экран(row, True) == вид
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.trace, row.geo_provider = None, "dadata"
        await s.commit()
    вид = clients_svc.geo_view(await _row(db_sessionmaker, cid))
    assert вид is not None and (вид["rule"], вид["rule_label"], вид["suggest"]) == (
        None,
        None,
        False,
    )


async def test_принятие_снимает_хвост_suggest(
    seed_conversation: Any, db_sessionmaker: Any, make_user: Any
) -> None:
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 5"))
    actor = await make_user("p60a-admin@test.local", role="admin")
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.geo_status, row.geo_provider = g.GEO_EXACT, "dadata~approx~suggest"
        row.geo_lat, row.geo_lon, row.geo_formatted = 51.2, 58.5, "ул Ленина, 5, Орск"
        row.trace = {"rule": g.RULE_STREET_POINT, "suggest": True}
        await s.commit()
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        card = await s.get(Client, row.client_id)
        await clients_svc.resolve_address_candidate(
            s, candidate=row, client=card, decision="replace", actor=actor
        )
        await s.commit()
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata~approx" and row.trace["suggest"] is True
    assert clients_svc.candidate_grade(row) == g.GRADE_APPROX
    # Отказ хвост не трогает: строка `rejected` остаётся предложением в следе.
    другой = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Пушкина 7"))
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, другой)
        row.geo_status, row.geo_provider = g.GEO_EXACT, "dadata~suggest"
        card = await s.get(Client, row.client_id)
        await clients_svc.resolve_address_candidate(
            s, candidate=row, client=card, decision="reject", actor=actor
        )
        await s.commit()
    assert (await _row(db_sessionmaker, другой)).geo_provider == "dadata~suggest"


def _строка_места(**kw: Any) -> ClientAddressCandidate:
    база: dict[str, Any] = {
        "id": uuid.uuid4(),
        "client_id": uuid.uuid4(),
        "conversation_id": uuid.uuid4(),
        "value": "Лесная, 15",
        "street": "Лесная",
        "house": "15",
        "raw": "Лесная 15",
        "level": "A",
        "status": CANDIDATE_PENDING,
        "detected_at": datetime.now(UTC),
        "kind": address_parse.KIND_HOUSE,
        "geo_status": g.GEO_HOUSE_MISSING,
    }
    база.update(kw)
    return ClientAddressCandidate(**база)


def test_то_же_место_различает_названные_пункты() -> None:
    """I-1: отказ человеком от «Лесная 15» (Ижевск) не запирает «Лесная
    15, Воткинск» — названные пункты разные. Названный у одной — не спор."""
    ижевск = _строка_места(locality="Ижевск")
    воткинск = _строка_места(locality="Воткинск")
    без_пункта = _строка_места()
    assert not clients_svc._то_же_место(ижевск, воткинск)
    assert clients_svc._то_же_место(ижевск, без_пункта)
    assert clients_svc._то_же_место(ижевск, _строка_места(locality="ижевск"))
    # Пункт с типом — тот же сторож.
    assert not clients_svc._то_же_место(
        _строка_места(settlement="Ударник"), _строка_места(settlement="Заря")
    )
    # Обе подтверждены картой — судит карта, как прежде.
    a = _строка_места(locality="Ижевск", geo_status=g.GEO_EXACT, geo_formatted="ул Лесная, 15")
    b = _строка_места(locality="Воткинск", geo_status=g.GEO_EXACT, geo_formatted="ул Лесная, 15")
    assert clients_svc._то_же_место(a, b)


# ── настройки: 400 на неизвестное имя, экран ──────────────────────────────────


async def test_настройка_rule_policy_неизвестное_имя_400(client: Any, tokens: Any) -> None:
    url = "/api/v1/settings/address-detect"
    плохо = await client.patch(
        url, headers=hdr(tokens["admin"]), json={"rule_policy": "street_poin=off"}
    )
    assert плохо.status_code == 400, плохо.text
    assert "неизвестное правило" in плохо.json()["error"]["message"]
    assert плохо.json()["error"]["details"]["fields"][0]["field"] == "address_geo.rule_policy"
    плохо = await client.patch(
        url, headers=hdr(tokens["admin"]), json={"rule_policy": "suburb=later"}
    )
    assert плохо.status_code == 400, плохо.text
    плохо = await client.patch(url, headers=hdr(tokens["admin"]), json={"parse_rules": "STOP_X=on"})
    assert плохо.status_code == 400, плохо.text
    # Годное — записано и видно вместе с действующей лестницей.
    ок = await client.patch(
        url,
        headers=hdr(tokens["admin"]),
        json={"rule_policy": "street_point=suggest, suburb=shadow"},
    )
    assert ок.status_code == 200, ок.text
    тело = ок.json()
    assert (
        тело["rule_policy"] == "street_point=suggest, suburb=shadow" and тело["parse_rules"] == ""
    )
    действующая = {r["rule"]: r["policy"] for r in тело["rule_policy_effective"]}
    assert действующая["street_point"] == "suggest" and действующая["suburb"] == "shadow"
    assert действующая["best_of"] == "exact"
    assert {r["rule"]: r["label"] for r in тело["rule_policy_effective"]} == g.RULE_LABEL
    # Пустая строка — снять перекрытия.
    ок = await client.patch(url, headers=hdr(tokens["admin"]), json={"rule_policy": ""})
    assert ок.status_code == 200 and ок.json()["rule_policy"] == ""


async def test_мусор_в_таблице_читается_как_есть_а_бой_разбирает_нестрого(
    db_sessionmaker: Any,
) -> None:
    """Имя, снятое из реестра после записи (ревью 20.09, #5/#14): чтение
    отдаёт строку как есть (содержимое на чтении не проверяется — иначе одно
    имя обнуляло бы ВСЕ перекрытия к `exact`), бой разбирает нестрого —
    плохая пара пропускается, остальные действуют; запись строгая по-прежнему."""
    async with db_sessionmaker() as s:
        s.add(
            AppSetting(
                key=app_settings.ADDRESS_GEO_RULE_POLICY, value="street_poin=off, suburb=shadow"
            )
        )
        await s.commit()
    async with db_sessionmaker() as s:
        сырое = await app_settings.get(s, app_settings.ADDRESS_GEO_RULE_POLICY)
        assert сырое == "street_poin=off, suburb=shadow"
        assert g.parse_rule_policy(сырое, strict=False) == {"suburb": "shadow"}
        with pytest.raises(ValueError, match="неизвестное правило"):
            g.parse_rule_policy(сырое)
        with pytest.raises(Exception, match="неизвестное правило"):
            await app_settings.set_many(
                s, {app_settings.ADDRESS_GEO_RULE_POLICY: "street_poin=off"}, user_id=None
            )


# ── воркер: след, политика, предложение ───────────────────────────────────────


async def _политика(db_sessionmaker: Any, текст: str) -> None:
    async with db_sessionmaker() as s:
        await app_settings.set_many(s, {app_settings.ADDRESS_GEO_RULE_POLICY: текст}, user_id=None)
        await s.commit()


async def test_воркер_пишет_след_суда(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, osm_пусто: Any
) -> None:
    """Стенд Пскова: точка улицы без Яндекса — `~approx`; след: правило,
    политика, форма запроса, регион и пункт ответа карты; след разбора
    остаётся."""
    await _режим(db_sessionmaker, "nominatim")
    cid = await п._псков(seed_conversation, db_sessionmaker, monkeypatch)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.trace = {"parse_form": "settlement_street_house"}
        await s.commit()
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata~approx"
    assert row.trace == {
        "parse_form": "settlement_street_house",
        "rule": "street_point",
        "policy": "exact",
        "region": "Псковская обл",
        "hit_city": "Сорокино",
        "query_form": "house",
    }
    assert await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_воркер_off_и_shadow_как_без_правила(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, osm_пусто: Any
) -> None:
    """`street_point=shadow`: вердикт как без правила (строка улицы без точки,
    `house_missing`), тень — в следе и журнале без ПД; `off` — то же без тени."""
    await _режим(db_sessionmaker, "nominatim")
    await _политика(db_sessionmaker, "street_point=shadow")
    cid = await п._псков(seed_conversation, db_sessionmaker, monkeypatch)
    with structlog.testing.capture_logs() as логи:
        итог = await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid)
    row = await _row(db_sessionmaker, cid)
    assert итог == row.geo_status == g.GEO_HOUSE_MISSING
    assert row.geo_lat is None and row.geo_formatted == "ул Луговая, 5, Сорокино"
    assert "rule" not in row.trace
    assert row.trace["shadow"] == {
        "rule": "street_point",
        "status": "exact",
        "key": "ул Луговая, 5, Сорокино",
        "km": None,
        "instead": None,
    }
    тени = [л for л in логи if л["event"] == "geocode.rule_shadow"]
    assert [т["rule"] for т in тени] == ["street_point"] and "key" not in тени[0]
    # off — без тени: та же строка после сброса (прежний суд ушёл в `prev`).
    await _политика(db_sessionmaker, "street_point=off")
    async with db_sessionmaker() as s:
        clients_svc.сбросить_вердикт(await s.get(ClientAddressCandidate, cid), reason="test")
        await s.commit()
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_HOUSE_MISSING
    row = await _row(db_sessionmaker, cid)
    assert "rule" not in row.trace and "shadow" not in row.trace
    assert (
        row.trace["query_form"] == "house" and row.trace["prev"]["shadow"]["rule"] == "street_point"
    )


async def test_воркер_suggest_пишет_хвост_без_автозаписи(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, osm_пусто: Any
) -> None:
    await _режим(db_sessionmaker, "nominatim")
    await _политика(db_sessionmaker, "street_point=suggest")
    cid = await п._псков(seed_conversation, db_sessionmaker, monkeypatch)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata~approx~suggest" and row.geo_lat == 57.9
    assert row.geo_formatted == "ул Луговая, 5, Сорокино"
    assert clients_svc.candidate_grade(row) is None
    assert row.trace["rule"] == "street_point" and row.trace["policy"] == "suggest"
    assert row.trace["suggest"] is True
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")
    вид = clients_svc.geo_view(row)
    assert вид is not None and вид["suggest"] and вид["precision"] == g.PRECISION_APPROX


async def test_воркер_пригород_под_политикой(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """Стенд Самары: `suburb=suggest` — exact с хвостом, вариант остаётся
    оператору, автозаписи нет; `suburb=shadow` — `elsewhere` с вариантом и
    тенью; `suburb=approx` — `~approx` всегда."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[], [СМЫШЛЯЕВКА]]
    await _политика(db_sessionmaker, "suburb=suggest")
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata~suggest" and row.geo_formatted == "ул Ленина, 5, Смышляевка"
    # Вариант «в другом месте» остаётся оператору рядом с предложением.
    assert len(row.geo_variants or []) == 1
    assert row.trace["rule"] == "suburb" and row.trace["policy"] == "suggest"
    км = round(g.distance_km(САМАРА, (СМЫШЛЯЕВКА.lat, СМЫШЛЯЕВКА.lon)), 2)
    assert row.trace["km"] == км
    assert row.trace["region"] == "Самарская область" and row.trace["hit_city"] == "Смышляевка"
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")

    await _политика(db_sessionmaker, "suburb=shadow")
    # Другая улица (тот же номер): «один адрес — одна строка» иначе дописал бы
    # ту же строку, а дом области обязан совпасть номером.
    dadata_отвечает["ответы"] = [[], [dataclasses.replace(СМЫШЛЯЕВКА, street="ул Пушкина")]]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Пушкина 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_ELSEWHERE
    row = await _row(db_sessionmaker, cid)
    assert len(row.geo_variants or []) == 1 and "rule" not in row.trace
    assert row.trace["shadow"]["rule"] == "suburb" and row.trace["shadow"]["instead"] is None
    assert row.trace["shadow"]["km"] == км

    await _политика(db_sessionmaker, "suburb=approx")
    dadata_отвечает["ответы"] = [[], [dataclasses.replace(СМЫШЛЯЕВКА, street="ул Мира")]]
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Мира 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata~approx" and row.trace["policy"] == "approx"
    assert clients_svc.candidate_grade(row) == g.GRADE_APPROX


async def test_воркер_suggest_правила_оставляет_варианты(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    dadata_отвечает: dict,
    osm_пусто: Any,
    точка_города: dict,
) -> None:
    """`only_in_radius=suggest`: два дома области, один в 40 км — решение
    записано предложением, а оба варианта остались оператору."""
    await _режим(db_sessionmaker, "nominatim")
    точка_города["точка"] = САМАРА
    dadata_отвечает["ответы"] = [[], [СМЫШЛЯЕВКА, ДАЛЬНИЙ]]
    await _политика(db_sessionmaker, "only_in_radius=suggest")
    cid = await _строка(seed_conversation, db_sessionmaker, "samara", _разобрать("ул Ленина 5"))
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata~approx~suggest"
    assert row.geo_formatted == "ул Ленина, 5, Смышляевка"
    assert len(row.geo_variants or []) == 2
    assert row.trace["rule"] == "only_in_radius" and row.trace["suggest"] is True
    assert clients_svc.candidate_grade(row) is None
    assert not await redis.exists(f"arq:job:addr-fill:{seed_conversation.conversation_id}")


async def test_воркер_не_трогает_след_у_отложенной(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, osm_пусто: Any
) -> None:
    """Отказ без DaData отложен (`pending`): суда не было — след разбора
    остаётся как есть, ключей суда нет (то же условие, что у `geo_prev`)."""
    from tests.unit import test_geo_bez_api_1309 as без_api

    await _режим(db_sessionmaker, "nominatim")
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    await без_api._потолок_dadata_выбран(redis)
    cid = await _строка(seed_conversation, db_sessionmaker, "orsk", _разобрать("ул Ленина 5"))
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.trace = {"parse_form": "street_house"}
        await s.commit()
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == "deferred"
    row = await _row(db_sessionmaker, cid)
    assert row.geo_status == g.GEO_PENDING and row.trace == {"parse_form": "street_house"}


async def test_воркер_удержал_прежний_вердикт_и_вернул_его_след(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    яндекс: Any,
) -> None:
    """Стенд пакета 5: улики удержаны (`rejudge_kept`) — след того суда,
    ушедший в `prev` при сбросе, возвращается наверх; нового `rule` нет."""
    await п5._режим(db_sessionmaker)
    cid = await п5._строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    await п5._со_снимком(db_sessionmaker, cid)
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        row.trace = {
            "parse_form": "street_house",
            "prev": {"rule": "street_point", "query_form": "house"},
        }
        await s.commit()
    await redis.set(worker.yandex_calls_key(), 900)
    итог = await worker.geocode_candidate(
        ctx(db_sessionmaker, redis), cid, origin=worker.ORIGIN_REPAIR
    )
    row = await _row(db_sessionmaker, cid)
    assert итог == row.geo_status == g.GEO_HOUSE_MISSING and row.geo_prev["reason"] == "kept"
    assert row.trace == {
        "parse_form": "street_house",
        "rule": "street_point",
        "query_form": "house",
    }


async def _карточка(db_sessionmaker: Any, client_id: Any) -> Client:
    async with db_sessionmaker() as s:
        card = await s.get(Client, client_id)
        assert card is not None
        return card


async def _правки_адреса(db_sessionmaker: Any) -> list[str]:
    """Причины записей `client.address_edited` по порядку — след пересборки."""
    async with db_sessionmaker() as s:
        return list(
            (
                await s.execute(
                    sa.select(AuditLog.details["reason"].as_string())
                    .where(AuditLog.action == "client.address_edited")
                    .order_by(AuditLog.created_at)
                )
            )
            .scalars()
            .all()
        )


async def test_пересуд_источника_под_suggest_не_трогает_карточку(
    seed_conversation: Any, db_sessionmaker: Any, redis: Any, monkeypatch: Any, osm_пусто: Any
) -> None:
    """Ревью 20.09 (#2): строка-источник автоадреса пересужена под политикой
    `suggest` (правило понижено, вердикт сброшен) — воркер выносит ТО ЖЕ
    решение с хвостом `~suggest`, `candidate_grade` → None, и без ворот
    `refresh_auto_address` переписал бы карточку словами клиента с причиной
    `verdict_refused`, хотя карта не отказывала. Предложение — состояние «не
    трогать»: текст, источник и пустой журнал правок остаются как были."""
    await _режим(db_sessionmaker, "nominatim")
    cid = await п._псков(seed_conversation, db_sessionmaker, monkeypatch)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert (await _row(db_sessionmaker, cid)).geo_provider == "dadata~approx"
    assert (
        await worker.autofill_address(
            ctx(db_sessionmaker, redis), seed_conversation.conversation_id
        )
        == "filled"
    )
    card = await _карточка(db_sessionmaker, seed_conversation.client_id)
    записано = card.address
    assert записано == "ул Луговая, 5, Сорокино"
    assert card.address_candidate_id == cid and card.address_set_at is None
    assert await _правки_адреса(db_sessionmaker) == []
    # Правило понижено до предложения, строка вернулась в очередь карты.
    await _политика(db_sessionmaker, "street_point=suggest")
    async with db_sessionmaker() as s:
        clients_svc.сбросить_вердикт(
            await s.get(ClientAddressCandidate, cid), reason="street_type_named"
        )
        await s.commit()
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    row = await _row(db_sessionmaker, cid)
    assert row.geo_provider == "dadata~approx~suggest" and row.trace["suggest"] is True
    assert clients_svc.candidate_grade(row) is None
    card = await _карточка(db_sessionmaker, seed_conversation.client_id)
    assert card.address == записано
    assert card.address_candidate_id == cid and card.address_set_at is None
    assert await _правки_адреса(db_sessionmaker) == []
    # Прямой вызов на той же строке — те же ворота, без записи и журнала.
    async with db_sessionmaker() as s:
        row = await s.get(ClientAddressCandidate, cid)
        card = await s.get(Client, seed_conversation.client_id)
        assert await clients_svc.refresh_auto_address(s, card, row) is False
        assert card.address == записано
    assert await _правки_адреса(db_sessionmaker) == []


async def test_политика_читается_один_раз_на_задачу(
    seed_conversation: Any,
    db_sessionmaker: Any,
    redis: Any,
    monkeypatch: Any,
    osm_пусто: Any,
    dadata_пусто: Any,
    яндекс: Any,
) -> None:
    """Читатель настройки — один вызов `get(ADDRESS_GEO_RULE_POLICY)` на
    задачу, в фазе 1 до ветвления; правила получают словарь, а не ходят в базу
    сами. Считается ПОВЕДЕНИЕ (ревью 20.09, #3/#19): шпион на
    `app_settings.get` — воркер зовёт её через атрибут модуля — считает
    только чтения этого ключа за прогон стенда пакета 5, прошедшего фазу 1
    до суда (DaData и OSM пусто, Яндекс отдаёт дом → `exact`). Чтение в
    цикле по картам города дало бы больше одного при одном упоминании в
    тексте; вынос в помощник — по-прежнему одно."""
    await п5._режим(db_sessionmaker)
    cid = await п5._строка(seed_conversation, db_sessionmaker, "ул Звенигородская 1")
    исходный = app_settings.get
    чтения: list[str] = []

    async def шпион(db: Any, key: str, *a: Any, **kw: Any) -> Any:
        if key == app_settings.ADDRESS_GEO_RULE_POLICY:
            чтения.append(key)
        return await исходный(db, key, *a, **kw)

    monkeypatch.setattr(app_settings, "get", шпион)
    assert await worker.geocode_candidate(ctx(db_sessionmaker, redis), cid) == g.GEO_EXACT
    assert len(чтения) == 1
    # Граница слоёв: услуга суда (`geocode`) настройку не знает — политику ей
    # приносят словарём.
    assert not re.search(r"ADDRESS_GEO_RULE_POLICY", inspect.getsource(g))
