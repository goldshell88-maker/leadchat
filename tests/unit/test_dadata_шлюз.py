"""Обёртка DaData поверх шлюза (docs/46): что спрашивать решает LeadChat, как —
шлюз. Здесь проверяется граница: цикл текстов = столько же походов и счётов,
кэш гасит повтор без похода и без счёта, `seen` копит все раунды, `near`
едет в теле, отказы шлюза становятся `GeocodeError` с прежними видами, а ни
ключ, ни адрес не попадают в текст ошибки и в лог."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest
import respx
import structlog

from app.integrations import dadata, gateway
from app.services import geo_cache
from app.services import geocode as g

pytestmark = pytest.mark.anyio

GW = "http://gw.test"

ДОМ: dict[str, Any] = {
    "street": "ул Ленина",
    "house": "12",
    "settlement": None,
    "city": "Обнинск",
    "region": "Калужская обл",
    "lat": 55.1,
    "lon": 36.6,
    "house_level": True,
    "interpolated": False,
    "settlement_kind": "place",
}
УЛИЦА = {**ДОМ, "house": None, "house_level": False}


def _ответ(*hits: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "hits": list(hits)})


@pytest.fixture(autouse=True)
def _ключ_на_шлюзе(monkeypatch) -> None:  # noqa: ANN001
    """Снимок `/status` в тестах пуст (conftest); здесь у DaData ключ есть."""
    monkeypatch.setattr(gateway, "known_keys", {"dadata": True})


def test_включена_по_снимку_шлюза(monkeypatch) -> None:  # noqa: ANN001
    assert dadata.enabled()
    monkeypatch.setattr(gateway, "known_keys", {"dadata": False})
    assert not dadata.enabled()
    monkeypatch.setattr(gateway, "known_keys", {"dadata": True})
    monkeypatch.setattr(gateway, "enabled", lambda: False)
    assert not dadata.enabled()


@respx.mock
async def test_два_текста_два_похода_и_повтор_из_кэша(redis) -> None:  # noqa: ANN001
    geo_cache.bind(redis)
    маршрут = respx.post(f"{GW}/geo/dadata").mock(side_effect=[_ответ(), _ответ(ДОМ)])
    q = g.Query(
        region="Калужская область",
        city=None,
        settlement="Обнинск",
        street="улица Ленина",
        house="12",
    )
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    hits = await dadata.search(q, on_request=посчитать)
    assert [h.house for h in hits] == ["12"] and isinstance(hits[0], g.GeoHit)
    assert маршрут.call_count == 2 and счёт == 2, "тариф считает запросы, а не вызовы search"
    первый, второй = (json.loads(c.request.content) for c in маршрут.calls)
    assert первый == {
        "text": "Обнинск улица Ленина 12",
        "region": "Калужская область",
        "city": None,
        "near": None,
    }
    assert второй["text"] == "улица Ленина 12"
    assert маршрут.calls[0].request.headers["Authorization"] == "Bearer test-token"
    # Те же тексты второй раз — оба из кэша: ни похода, ни счёта.
    assert await dadata.search(q, on_request=посчитать) == hits
    assert маршрут.call_count == 2 and счёт == 2
    # Другой дом — другой ключ кэша.
    маршрут.side_effect = [_ответ(), _ответ()]
    await dadata.search(dataclasses.replace(q, house="14"), on_request=посчитать)
    assert маршрут.call_count == 4 and счёт == 4


@respx.mock
async def test_только_с_пунктом_один_поход_и_seen_копит_все_раунды() -> None:
    маршрут = respx.post(f"{GW}/geo/dadata").mock(side_effect=[_ответ(УЛИЦА), _ответ(ДОМ)])
    q = g.Query(
        region="Калужская область",
        city=None,
        settlement="Обнинск",
        street="улица Ленина",
        house="12",
    )
    все: list[g.GeoHit] = []
    hits = await dadata.search(q, seen=все)
    assert [h.house for h in hits] == ["12"] and [h.house for h in все] == [None, "12"]
    assert маршрут.call_count == 2
    маршрут.side_effect = [_ответ()]
    assert await dadata.search(q, without_settlement_too=False) == []
    assert маршрут.call_count == 3


@respx.mock
async def test_near_едет_в_теле_и_дробь_спрашивается_дважды() -> None:
    маршрут = respx.post(f"{GW}/geo/dadata").mock(return_value=_ответ())
    q = g.Query(
        region="Вологодская область", city=None, settlement=None, street="Рябиновая", house="8"
    )
    await dadata.search(q, near=(59.12, 37.9))
    assert json.loads(маршрут.calls[0].request.content)["near"] == [59.12, 37.9]
    q = g.Query(region=None, city="Орск", settlement=None, street="Литейная", house="14/2")
    await dadata.search(q)
    тексты = [json.loads(c.request.content)["text"] for c in маршрут.calls[1:]]
    assert тексты == ["Литейная 14/2", "Литейная 14 к 2"]


@respx.mock
async def test_точка_города_и_место(redis) -> None:  # noqa: ANN001
    geo_cache.bind(redis)
    город = respx.post(f"{GW}/geo/dadata/city").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "point": [59.12, 37.9]}),
            httpx.Response(200, json={"ok": True, "point": None}),
        ]
    )
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    assert await dadata.city_point("Череповец", "Вологодская область", on_request=посчитать) == (
        59.12,
        37.9,
    )
    assert json.loads(город.calls[0].request.content) == {
        "city": "Череповец",
        "region": "Вологодская область",
    }
    # «Города нет» тоже запоминается: второй раз — без похода.
    assert await dadata.city_point("Нигдеевск", None, on_request=посчитать) is None
    assert await dadata.city_point("Нигдеевск", None, on_request=посчитать) is None
    assert await dadata.city_point("Череповец", "Вологодская область") == (59.12, 37.9)
    assert город.call_count == 2 and счёт == 2

    место = respx.post(f"{GW}/geo/dadata/place").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "hits": [
                    {
                        "name": "зона Южный",
                        "kind": "area",
                        "settlement": "Большая Дубрава",
                        "area": "зона Южный",
                        "city": None,
                        "district": "Гатчинский р-н",
                        "region": "Ленинградская обл",
                        "lat": 59.651234,
                        "lon": 30.104567,
                    }
                ],
            },
        )
    )
    hits = await dadata.search_place(
        g.Place("Большая Дубрава", "деревня", "массив Южный", "Гатчинский р-н"),
        region="Ленинградская область",
        on_request=посчитать,
    )
    assert isinstance(hits[0], g.PlaceHit) and hits[0].kind == "area"
    assert json.loads(место.calls[0].request.content) == {
        "query_text": "Гатчинский р-н деревня Большая Дубрава массив Южный",
        "region": "Ленинградская область",
        "city": None,
    }
    assert (
        await dadata.search_place(
            g.Place("Большая Дубрава", "деревня", "массив Южный", "Гатчинский р-н"),
            region="Ленинградская область",
            on_request=посчитать,
        )
        == hits
    )
    assert место.call_count == 1 and счёт == 3


@respx.mock
async def test_виды_отказов_шлюза_и_провайдера() -> None:
    q = g.Query(region=None, city="Орск", settlement=None, street="x", house="1")
    маршрут = respx.post(f"{GW}/geo/dadata")
    for kind_шлюза, kind_карты in (
        ("blocked", "blocked"),
        ("network", "network"),
        ("bad_response", "bad_response"),
        ("auth", "network"),  # 401 самого шлюза (токен) — отказ шлюза, не бан карты
    ):
        маршрут.mock(
            return_value=httpx.Response(
                200, json={"ok": False, "kind": kind_шлюза, "status": 418, "detail": "x"}
            )
        )
        with pytest.raises(g.GeocodeError) as exc:
            await dadata.search(q)
        assert (exc.value.provider, exc.value.kind, exc.value.status) == ("dadata", kind_карты, 418)
    # Отказ САМОГО шлюза (обрыв, 5xx, не-JSON) — «сеть»: как «карта не отвечает».
    маршрут.mock(side_effect=httpx.ConnectTimeout("boom"))
    with pytest.raises(g.GeocodeError) as exc:
        await dadata.search(q)
    assert exc.value.kind == "network"
    respx.post(f"{GW}/geo/dadata/city").mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(g.GeocodeError) as exc:
        await dadata.city_point("Орск", None)
    assert (exc.value.kind, exc.value.status) == ("network", 502)
    маршрут.mock(return_value=httpx.Response(200, json={"ok": True, "hits": "?"}))
    with pytest.raises(g.GeocodeError) as exc:
        await dadata.search(q)
    assert exc.value.kind == "bad_response"


@respx.mock
async def test_без_ключа_на_шлюзе_blocked_и_снимок_помечен(redis) -> None:  # noqa: ANN001
    geo_cache.bind(redis)
    маршрут = respx.post(f"{GW}/geo/dadata").mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "no_key", "status": None})
    )
    q = g.Query(region=None, city="Орск", settlement=None, street="x", house="1")
    assert dadata.enabled()
    with pytest.raises(g.GeocodeError) as exc:
        await dadata.search(q)
    assert (exc.value.kind, exc.value.status) == ("blocked", None)
    assert gateway.known_keys == {"dadata": False} and not dadata.enabled()
    # Снимок сказал «ключа нет» — больше не ходим, отказ тот же.
    with pytest.raises(g.GeocodeError) as exc:
        await dadata.search(q)
    assert exc.value.kind == "blocked" and маршрут.call_count == 1
    # Отказ не кэшируется.
    assert [k async for k in redis.scan_iter("geo:cache:dadata:*")] == []


@respx.mock
async def test_ни_ключа_ни_адреса_в_ошибке_и_логах() -> None:
    respx.post(f"{GW}/geo/dadata").mock(side_effect=httpx.ConnectTimeout("boom"))
    q = g.Query(region=None, city="Орск", settlement=None, street="ул Ленина", house="5")
    with structlog.testing.capture_logs() as логи:
        with pytest.raises(g.GeocodeError) as exc:
            await dadata.search(q)
    журнал = json.dumps(логи, ensure_ascii=False)
    assert логи and "gateway.unreachable" in журнал
    for слово in ("Ленина", "Орск", "gw.test", "test-token"):
        assert слово not in str(exc.value) and слово not in журнал


async def test_клиент_из_теста_идёт_в_шлюз() -> None:
    def обработчик(request: httpx.Request) -> httpx.Response:
        assert request.url == f"{GW}/geo/dadata"
        assert request.headers["Authorization"] == "Bearer test-token"
        return _ответ(ДОМ)

    async with httpx.AsyncClient(transport=httpx.MockTransport(обработчик)) as client:
        hits = await dadata.search(
            g.Query(region=None, city="Обнинск", settlement=None, street="Ленина", house="12"),
            client=client,
        )
    assert hits == [g.GeoHit(**ДОМ)]
