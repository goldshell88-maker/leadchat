"""Ahunter и Спеллер на шлюзе (16.09; переезд из tests/unit/test_ahunter_speller_1609.py).

1. Ahunter: строка ГАР разбирается на компоненты; «д Ольхово» — деревня,
   не дом; без дома подсказка не берётся; формы дома («5 А», «к.2», «лит АБ»).
2. Спеллер: только однозначные замены не дальше двух правок.
3. Ручки: `POST /geo/ahunter` шлёт параметры через «;» с квотированием и
   своими заголовками, отказы провайдера — `{"ok": false, kind}`; `POST /spell`
   шлёт text/lang/format и отдаёт замены.
"""

from __future__ import annotations

from urllib.parse import quote

import httpx
import pytest
import respx

from leadchat_gateway.errors import ProviderError
from leadchat_gateway.providers import ahunter, speller

pytestmark = pytest.mark.anyio

# --- 1. Ahunter: разбор -----------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "street", "house", "city", "settlement"),
    [
        ("обл Иркутская, г Ангарск, кв-л 57, дом 8", "кв-л 57", "8", "Ангарск", None),
        ("обл Иркутская, г Ангарск, мкр 12, стр 4", "мкр 12", "стр 4", "Ангарск", None),
        # Второй «г» — город объявления, первый — город-регион.
        ("г Москва, г Зеленоград, корп 1218", "", "корп 1218", "Зеленоград", None),
        (
            "обл Псковская, р-н Псковский, д Ольхово, ул Кленовая, дом 6",
            "ул Кленовая",
            "6",
            None,
            "Ольхово",
        ),
        (
            "г Санкт-Петербург, ул Ленина, дом 5, корп 2, лит А",
            "ул Ленина",
            "5 корп 2 лит А",
            "Санкт-Петербург",
            None,
        ),
        (
            "обл Московская, г Красногорск, рп Нахабино, ул Школьная, дом 7",
            "ул Школьная",
            "7",
            "Красногорск",
            "Нахабино",
        ),
    ],
)
def test_ahunter_разбор_строки_гар(
    value: str, street: str, house: str, city: str | None, settlement: str | None
) -> None:
    п = ahunter.parse_value(value)
    assert п is not None
    assert (п.street, п.house, п.city, п.settlement) == (street, house, city, settlement)


def test_ahunter_без_дома_не_подсказка() -> None:
    assert ahunter.parse_value("обл Ленинградская, р-н Всеволожский, тер. СНТ Маяк") is None
    assert ahunter.parse_response({"query": "x", "suggestions": []}) == []
    assert ahunter.parse_response({"query": "x"}) == []
    with pytest.raises(ProviderError) as exc:
        ahunter.parse_response([])
    assert exc.value.kind == "bad_response"
    with pytest.raises(ProviderError):
        ahunter.parse_response({"suggestions": "нет"})


@pytest.mark.parametrize(
    ("value", "house"),
    [
        ("г Орск, ул Ленина, дом 5 А", "5 А"),
        ("г Орск, ул Ленина, дом 5, к.2", "5 к 2"),
        ("г Орск, ул Ленина, дом 5, лит АБ", "5 лит АБ"),
        ("обл Иркутская, г Ангарск, кв-л 57, дом 8, двлд 2", "8 двлд 2"),
    ],
)
def test_ahunter_формы_дома(value: str, house: str) -> None:
    п = ahunter.parse_value(value)
    assert п is not None and п.house == house


def test_ahunter_семь_полей_в_ответе_ручки() -> None:
    """Обёртка LeadChat собирает `Suggested(**h)` по именам — набор полей закреплён."""
    п = ahunter.parse_value("обл Иркутская, р-н Ангарский, г Ангарск, кв-л 57, дом 8")
    assert п is not None
    assert п.model_dump() == {
        "street": "кв-л 57",
        "house": "8",
        "city": "Ангарск",
        "settlement": None,
        "district": "р-н Ангарский",
        "region": "обл Иркутская",
        "formatted": "обл Иркутская, р-н Ангарский, г Ангарск, кв-л 57, дом 8",
    }


# --- 2. Спеллер: выбор варианта --------------------------------------------------


def test_speller_выбор_варианта() -> None:
    ответ = [{"code": 1, "word": "Ленена", "s": ["Ленина"], "pos": 0, "len": 6}]
    assert speller.pick(ответ, ["Ленена"]) == {"Ленена": "Ленина"}
    # Два варианта, далёкая замена, то же слово — не замена.
    assert (
        speller.pick([{"code": 1, "word": "Салавье", "s": ["Соловье", "Славье"]}], ["Салавье"])
        == {}
    )
    assert speller.pick([{"code": 1, "word": "Ленена", "s": ["Ленинградская"]}], ["Ленена"]) == {}
    assert speller.pick([{"code": 1, "word": "Мира", "s": ["Мира"]}], ["Мира"]) == {}
    # Слово, которого LeadChat не спрашивал, — не наше.
    assert speller.pick([{"code": 1, "word": "Ленена", "s": ["Ленина"]}], ["Мира"]) == {}
    with pytest.raises(ProviderError) as exc:
        speller.pick({"bad": 1}, ["x"])
    assert exc.value.kind == "bad_response"


def test_speller_граница_двух_правок_и_живая_форма_ответа() -> None:
    # Живой Спеллер отдаёт несколько вариантов по убыванию уверенности.
    ответ = [{"code": 1, "word": "Звенигародская", "s": ["Звенигородская", "Звенигродская"]}]
    assert speller.pick(ответ, ["Звенигародская"]) == {"Звенигародская": "Звенигородская"}
    # Три правки — не берём; второй вариант ближе первого — не берём.
    assert speller.pick([{"code": 1, "word": "Ленена", "s": ["Ленинская"]}], ["Ленена"]) == {}
    assert (
        speller.pick([{"code": 1, "word": "Ленена", "s": ["Ленинка", "Ленина"]}], ["Ленена"]) == {}
    )
    # Ровно две правки — берём (пример из docstring), три — уже другой топоним.
    assert speller.pick([{"code": 1, "word": "Салавье", "s": ["Соловье"]}], ["Салавье"]) == {
        "Салавье": "Соловье"
    }
    assert speller.pick([{"code": 1, "word": "Ленена", "s": ["Ленинск"]}], ["Ленена"]) == {}
    # Перестановка соседних букв — одна правка (Дамерау), не две.
    assert speller._расстояние("амнудсена", "амундсена") == 1
    assert speller._расстояние("ленена", "ленина") == 1


# --- 3. ручки ----------------------------------------------------------------------

ТЕКСТ = "Ангарск 57 квартал 8"
ОТВЕТ_AHUNTER = {
    "query": ТЕКСТ,
    "suggestions": [
        {"value": "обл Иркутская, г Ангарск, кв-л 57, дом 8", "machine": "x", "zip": "665830"},
        {"value": "обл Иркутская, г Ангарск, кв-л 57"},  # без дома — не подсказка
    ],
}


@respx.mock
async def test_ahunter_ручка_url_через_точку_с_запятой_и_заголовки(gw: httpx.AsyncClient) -> None:
    маршрут = respx.get(url__startswith=ahunter.BASE_URL).mock(
        return_value=httpx.Response(200, json=ОТВЕТ_AHUNTER)
    )
    r = await gw.post("/geo/ahunter", json={"text": ТЕКСТ})
    assert r.status_code == 200
    данные = r.json()
    assert данные["ok"] is True
    assert данные["hits"] == [
        {
            "street": "кв-л 57",
            "house": "8",
            "city": "Ангарск",
            "settlement": None,
            "district": None,
            "region": "обл Иркутская",
            "formatted": "обл Иркутская, г Ангарск, кв-л 57, дом 8",
        }
    ]
    запрос = маршрут.calls.last.request
    # Параметры через «;», текст квотирован целиком (пробел — %20, не «+»).
    assert запрос.url.query.decode() == f"output=json;count=5;query={quote(ТЕКСТ, safe='')}"
    assert запрос.headers["User-Agent"] == ahunter.USER_AGENT
    assert запрос.headers["Accept"] == "application/json"


@respx.mock
async def test_ahunter_ручка_отказы_провайдера(gw: httpx.AsyncClient) -> None:
    маршрут = respx.get(url__startswith=ahunter.BASE_URL)
    маршрут.mock(return_value=httpx.Response(403, text="forbidden"))
    r = await gw.post("/geo/ahunter", json={"text": ТЕКСТ})
    assert r.status_code == 200
    assert (r.json()["ok"], r.json()["kind"], r.json()["status"]) == (False, "blocked", 403)
    маршрут.mock(return_value=httpx.Response(503))
    r = await gw.post("/geo/ahunter", json={"text": ТЕКСТ})
    assert (r.json()["kind"], r.json()["status"]) == ("network", 503)
    маршрут.mock(side_effect=httpx.ConnectTimeout("boom"))
    r = await gw.post("/geo/ahunter", json={"text": ТЕКСТ})
    assert r.json()["kind"] == "network" and r.json()["status"] is None
    маршрут.mock(return_value=httpx.Response(200, text="<html>нет</html>"))
    r = await gw.post("/geo/ahunter", json={"text": ТЕКСТ})
    assert r.json()["kind"] == "bad_response"
    # Текста запроса в ответе-отказе нет: он уходит в лог и на экран монитора.
    assert "Ангарск" not in r.text
    # `suggestions: null` — Ahunter ничего не нашёл, это пусто, а не сбой.
    маршрут.mock(return_value=httpx.Response(200, json={"query": ТЕКСТ, "suggestions": None}))
    r = await gw.post("/geo/ahunter", json={"text": ТЕКСТ})
    assert r.json() == {"ok": True, "hits": []}


@respx.mock
async def test_spell_ручка_параметры_и_выбор(gw: httpx.AsyncClient) -> None:
    маршрут = respx.get(speller.BASE_URL).mock(
        return_value=httpx.Response(
            200,
            json=[
                {"code": 1, "word": "Ленена", "s": ["Ленина", "Ленька"], "pos": 0, "len": 6},
                {"code": 1, "word": "Салавье", "s": ["Соловье", "Славье"], "pos": 7, "len": 7},
            ],
        )
    )
    r = await gw.post("/spell", json={"text": "Ленена Салавье", "words": ["Ленена", "Салавье"]})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "fixes": {"Ленена": "Ленина"}}
    params = маршрут.calls.last.request.url.params
    assert dict(params) == {"text": "Ленена Салавье", "lang": "ru", "format": "plain"}


@respx.mock
async def test_spell_ручка_отказы(gw: httpx.AsyncClient) -> None:
    маршрут = respx.get(speller.BASE_URL)
    маршрут.mock(return_value=httpx.Response(200, text="не json"))
    r = await gw.post("/spell", json={"text": "Ленена", "words": ["Ленена"]})
    assert r.status_code == 200
    assert (r.json()["ok"], r.json()["kind"]) == (False, "bad_response")
    маршрут.mock(return_value=httpx.Response(200, json={"не": "список"}))
    r = await gw.post("/spell", json={"text": "Ленена", "words": ["Ленена"]})
    assert r.json()["kind"] == "bad_response"
    маршрут.mock(return_value=httpx.Response(429))
    r = await gw.post("/spell", json={"text": "Ленена", "words": ["Ленена"]})
    assert (r.json()["kind"], r.json()["status"]) == ("network", 429)
    # Форма входа: без words — 422 самого шлюза, а не поход к провайдеру.
    r = await gw.post("/spell", json={"text": "Ленена"})
    assert r.status_code == 422
    assert маршрут.call_count == 3


async def test_текстовые_ручки_только_по_токену(gw_anon: httpx.AsyncClient) -> None:
    assert (await gw_anon.post("/geo/ahunter", json={"text": "x"})).status_code == 401
    assert (await gw_anon.post("/spell", json={"text": "x", "words": []})).status_code == 401
