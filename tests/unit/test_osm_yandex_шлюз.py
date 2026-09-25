"""Обёртки OSM, Яндекс Геокодера и Геосаджеста поверх шлюза (docs/46, 16.09).

Что осталось в LeadChat и здесь стережётся: три попытки OSM и `wait` перед
каждым походом, кэш `geo_cache` (попадание — ни похода, ни счёта), `on_request`
только перед настоящим походом, `enabled()` по снимку ключей шлюза, маппинг
отказов шлюза в `GeocodeError` без адреса в тексте и журнале. Как спросить
провайдера — проверяется в `gateway/tests/test_gw_osm_yandex.py`.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
import structlog

from app.core.config import settings
from app.integrations import gateway, nominatim, yandex_geocoder, yandex_suggest
from app.services import geo_cache
from app.services import geocode as g

pytestmark = pytest.mark.anyio

GW = "http://gw.test"

ЗАПРОС = g.Query(
    region="Оренбургская область",
    city="Орск",
    settlement="Заречный",
    street="ул звенигородская",
    house="1",
)

#: Ответ шлюза — поля один в один с `GeoHit`.
ДОМ: dict[str, Any] = {
    "street": "Звенигородская улица",
    "house": "1",
    "settlement": "Заречный",
    "city": "Орск",
    "region": "Оренбургская область",
    "lat": 51.2101234,
    "lon": 58.5012345,
    "house_level": True,
    "interpolated": False,
    "settlement_kind": "district",
}


def _тело(маршрут: respx.Route, n: int) -> dict:
    return json.loads(маршрут.calls[n].request.content)


# --- OSM ---------------------------------------------------------------------


@respx.mock
async def test_osm_три_попытки_это_три_похода_и_три_ожидания() -> None:
    маршрут = respx.post(f"{GW}/geo/nominatim").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "hits": []}),
            httpx.Response(200, json={"ok": True, "hits": []}),
            httpx.Response(200, json={"ok": True, "hits": [ДОМ]}),
        ]
    )
    ожиданий = 0

    async def темп() -> None:
        nonlocal ожиданий
        ожиданий += 1

    hits = await nominatim.search(ЗАПРОС, wait=темп)
    assert hits == [g.GeoHit(**ДОМ)]
    assert маршрут.call_count == 3
    # Замок темпа — перед КАЖДЫМ походом, а не один раз на поиск (ревью 11.09).
    assert ожиданий == 3
    # Что спросить — решает обёртка: компоненты, не текст сообщения.
    assert _тело(маршрут, 0) == {
        "mode": "structured",
        "street": "улица звенигородская",
        "house": "1",
        "city": "Орск",
        "region": "Оренбургская область",
        "free_text": "Оренбургская область, Орск, Заречный, улица звенигородская 1",
    }
    assert _тело(маршрут, 1)["city"] == "Заречный"
    assert _тело(маршрут, 2)["mode"] == "free"
    assert маршрут.calls[0].request.headers["Authorization"] == "Bearer test-token"


@respx.mock
async def test_osm_без_посёлка_две_попытки() -> None:
    маршрут = respx.post(f"{GW}/geo/nominatim").mock(
        return_value=httpx.Response(200, json={"ok": True, "hits": []})
    )
    import dataclasses

    assert await nominatim.search(dataclasses.replace(ЗАПРОС, settlement=None)) == []
    assert маршрут.call_count == 2
    assert [_тело(маршрут, i)["mode"] for i in range(2)] == ["structured", "free"]


@respx.mock
async def test_osm_попадание_в_кэш_ни_похода_ни_ожидания(redis) -> None:  # noqa: ANN001
    geo_cache.bind(redis)
    маршрут = respx.post(f"{GW}/geo/nominatim").mock(
        return_value=httpx.Response(200, json={"ok": True, "hits": [ДОМ]})
    )
    ожиданий = 0

    async def темп() -> None:
        nonlocal ожиданий
        ожиданий += 1

    первый = await nominatim.search(ЗАПРОС, wait=темп)
    второй = await nominatim.search(ЗАПРОС, wait=темп)
    assert первый == второй == [g.GeoHit(**ДОМ)]
    assert маршрут.call_count == 1
    assert ожиданий == 1
    assert len([k async for k in redis.scan_iter("geo:cache:nominatim:*")]) == 1


@respx.mock
async def test_osm_отказ_провайдера_и_отказ_шлюза() -> None:
    маршрут = respx.post(f"{GW}/geo/nominatim")
    for ответ, ожидаем in [
        (
            {"ok": False, "kind": "blocked", "status": 403, "detail": "доступ закрыт"},
            ("blocked", 403),
        ),
        ({"ok": False, "kind": "network", "status": 503, "detail": ""}, ("network", 503)),
        ({"ok": False, "kind": "bad_response", "status": 200, "detail": ""}, ("bad_response", 200)),
    ]:
        маршрут.mock(return_value=httpx.Response(200, json=ответ))
        with pytest.raises(g.GeocodeError) as exc:
            await nominatim.search(ЗАПРОС)
        assert (exc.value.provider, exc.value.kind, exc.value.status) == ("nominatim", *ожидаем)
    # Шлюз не принял токен — это не бан карты: повтор, а не сутки простоя.
    маршрут.mock(return_value=httpx.Response(401, json={"detail": "x"}))
    with pytest.raises(g.GeocodeError) as exc:
        await nominatim.search(ЗАПРОС)
    assert exc.value.kind == "network"
    # Ответ шлюза не той формы.
    маршрут.mock(return_value=httpx.Response(200, json={"ok": True, "hits": [{"lat": 1}]}))
    with pytest.raises(g.GeocodeError) as exc:
        await nominatim.search(ЗАПРОС)
    assert exc.value.kind == "bad_response"


@respx.mock
async def test_osm_обрыв_шлюза_это_network_и_без_адреса_в_тексте_и_журнале() -> None:
    respx.post(f"{GW}/geo/nominatim").mock(side_effect=httpx.ConnectTimeout("timed out"))
    with structlog.testing.capture_logs() as logs, pytest.raises(g.GeocodeError) as exc:
        await nominatim.search(ЗАПРОС)
    assert exc.value.kind == "network"
    assert "звенигородск" not in str(exc.value).lower()
    assert "gw.test" not in str(exc.value)
    assert "звенигородск" not in json.dumps(logs, ensure_ascii=False).lower()
    assert any(з.get("event") == "gateway.unreachable" for з in logs)


async def test_osm_клиент_из_теста_идёт_в_шлюз() -> None:
    видел: list[httpx.Request] = []

    def обработчик(request: httpx.Request) -> httpx.Response:
        видел.append(request)
        return httpx.Response(200, json={"ok": True, "hits": [ДОМ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(обработчик)) as client:
        hits = await nominatim.search(ЗАПРОС, client=client)
    assert hits == [g.GeoHit(**ДОМ)]
    assert str(видел[0].url) == f"{GW}/geo/nominatim"
    assert видел[0].headers["Authorization"] == "Bearer test-token"


# --- Яндекс Геокодер ---------------------------------------------------------


@respx.mock
async def test_яндекс_on_request_только_при_походе_и_кэш(redis) -> None:  # noqa: ANN001
    geo_cache.bind(redis)
    маршрут = respx.post(f"{GW}/geo/yandex").mock(
        return_value=httpx.Response(200, json={"ok": True, "hits": [ДОМ]})
    )
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    первый = await yandex_geocoder.search(ЗАПРОС, on_request=посчитать)
    второй = await yandex_geocoder.search(ЗАПРОС, on_request=посчитать)
    assert первый == второй == [g.GeoHit(**ДОМ)]
    assert маршрут.call_count == 1 and счёт == 1
    assert _тело(маршрут, 0) == {"free_text": ЗАПРОС.free_text, "city": "Орск"}
    assert len([k async for k in redis.scan_iter("geo:cache:yandex:*")]) == 1


@respx.mock
async def test_яндекс_отказ_не_кэшируется_и_счёт_идёт(redis) -> None:  # noqa: ANN001
    geo_cache.bind(redis)
    respx.post(f"{GW}/geo/yandex").mock(
        side_effect=[
            httpx.Response(200, json={"ok": False, "kind": "network", "status": 503}),
            httpx.Response(200, json={"ok": True, "hits": []}),
        ]
    )
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    with pytest.raises(g.GeocodeError) as exc:
        await yandex_geocoder.search(ЗАПРОС, on_request=посчитать)
    assert (exc.value.provider, exc.value.kind, exc.value.status) == ("yandex", "network", 503)
    assert await yandex_geocoder.search(ЗАПРОС, on_request=посчитать) == []
    assert счёт == 2
    assert len([k async for k in redis.scan_iter("geo:cache:yandex:*")]) == 1


@respx.mock
async def test_яндекс_без_ключа_на_шлюзе_это_blocked_и_снимок_помечен(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    monkeypatch.setitem(gateway.known_keys, "yandex_suggest", True)
    respx.post(f"{GW}/geo/yandex").mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "no_key", "status": None})
    )
    assert yandex_geocoder.enabled()
    with pytest.raises(g.GeocodeError) as exc:
        await yandex_geocoder.search(ЗАПРОС)
    # Как раньше при пустом ключе: `blocked` без статуса.
    assert (exc.value.kind, exc.value.status) == ("blocked", None)
    assert gateway.known_keys is not None and gateway.known_keys["yandex_geocoder"] is False
    assert not yandex_geocoder.enabled()
    assert yandex_suggest.enabled()  # снимок помечает только виновника


def test_enabled_по_снимку_и_настройкам_шлюза(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    monkeypatch.setitem(gateway.known_keys, "yandex_suggest", True)
    assert yandex_geocoder.enabled() and yandex_suggest.enabled()
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", False)
    assert not yandex_geocoder.enabled() and yandex_suggest.enabled()
    monkeypatch.setitem(gateway.known_keys, "yandex_suggest", False)
    assert not yandex_suggest.enabled()
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    assert yandex_geocoder.enabled()
    # Снимка ещё не было — верим, что ключ есть: первый поход покажет.
    monkeypatch.setattr(gateway, "known_keys", None)
    assert yandex_geocoder.enabled() and yandex_suggest.enabled()
    # Шлюз не настроен — выключено всё, снимок ни при чём.
    monkeypatch.setattr(settings, "gateway_token", "")
    assert not yandex_geocoder.enabled() and not yandex_suggest.enabled()


# --- Яндекс Геосаджест -------------------------------------------------------


@respx.mock
async def test_саджест_payload_и_Suggested() -> None:
    подсказка: dict[str, Any] = {
        "street": "Звенигородская улица",
        "house": "1",
        "city": "Орск",
        "settlement": "посёлок Заречный",
        "region": "Оренбургская область",
        "formatted": None,
    }
    маршрут = respx.post(f"{GW}/geo/yandex-suggest").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "hits": [подсказка]}),
            httpx.Response(200, json={"ok": True, "hits": []}),
        ]
    )
    счёт = 0

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    assert await yandex_suggest.suggest(ЗАПРОС, on_request=посчитать) == [
        yandex_suggest.Suggested(**подсказка)
    ]
    assert _тело(маршрут, 0) == {"free_text": ЗАПРОС.free_text}
    # Пустой ответ — пусто, не сбой (бой 12.09).
    assert await yandex_suggest.suggest(ЗАПРОС, on_request=посчитать) == []
    assert счёт == 2


@respx.mock
async def test_саджест_без_ключа_и_бан(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    monkeypatch.setitem(gateway.known_keys, "yandex_suggest", True)
    маршрут = respx.post(f"{GW}/geo/yandex-suggest").mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "no_key", "status": None})
    )
    with pytest.raises(g.GeocodeError) as exc:
        await yandex_suggest.suggest(ЗАПРОС)
    assert (exc.value.provider, exc.value.kind) == ("yandex_suggest", "blocked")
    assert not yandex_suggest.enabled() and yandex_geocoder.enabled()
    маршрут.mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "blocked", "status": 403})
    )
    with pytest.raises(g.GeocodeError) as exc:
        await yandex_suggest.suggest(ЗАПРОС)
    assert (exc.value.kind, exc.value.status) == ("blocked", 403)
