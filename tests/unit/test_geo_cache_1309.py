"""Кэш ответов карт (владелец 13.09: «тратить меньше ресурса API»).

Один и тот же запрос — один поход: второй раз ответ берётся из Redis, счётчик
суточного потолка не растёт. Отказ карты не кэшируется. Без привязки к Redis
кэш молчит. С 16.09 «поход» — это поход в шлюз Амстердама (docs/46): транспорт
в тестах отвечает как шлюз, а не как провайдер.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations import dadata, gateway, yandex_geocoder
from app.services import geo_cache, geocode

pytestmark = pytest.mark.anyio

ЗАПРОС = geocode.Query(
    region="Оренбургская область", city="Орск", settlement=None, street="ул Мира", house="7"
)
ДОМ_7 = {
    "street": "ул Мира",
    "house": "7",
    "settlement": None,
    "city": "Орск",
    "region": "Оренбургская обл",
    "lat": 51.2,
    "lon": 58.5,
    "house_level": True,
}


async def test_dadata_второй_раз_из_кэша_и_без_счётчика(redis, monkeypatch) -> None:
    monkeypatch.setitem(gateway.known_keys, "dadata", True)
    geo_cache.bind(redis)
    походов = 0
    счёт = 0

    def обработчик(request: httpx.Request) -> httpx.Response:
        nonlocal походов
        походов += 1
        assert request.url.path == "/geo/dadata"
        return httpx.Response(200, json={"ok": True, "hits": [ДОМ_7]})

    async def посчитать() -> None:
        nonlocal счёт
        счёт += 1

    async with httpx.AsyncClient(transport=httpx.MockTransport(обработчик)) as client:
        первый = await dadata.search(ЗАПРОС, client=client, on_request=посчитать)
        второй = await dadata.search(ЗАПРОС, client=client, on_request=посчитать)
    assert первый == второй and первый[0].house == "7"
    assert походов == 1 and счёт == 1
    # Другой запрос — другой ключ.
    async with httpx.AsyncClient(transport=httpx.MockTransport(обработчик)) as client:
        await dadata.search(
            geocode.Query(
                region="Оренбургская область",
                city="Орск",
                settlement=None,
                street="ул Мира",
                house="9",
            ),
            client=client,
            on_request=посчитать,
        )
    assert походов == 2 and счёт == 2


async def test_отказ_карты_не_кэшируется_а_без_redis_кэш_молчит(redis, monkeypatch) -> None:
    monkeypatch.setitem(gateway.known_keys, "yandex_geocoder", True)
    geo_cache.bind(redis)
    ответы = [
        httpx.Response(200, json={"ok": False, "kind": "network", "status": 503}),
        httpx.Response(200, json={"ok": True, "hits": []}),
    ]
    походов = 0

    def обработчик(request: httpx.Request) -> httpx.Response:
        nonlocal походов
        походов += 1
        # Ключ Яндекса живёт на шлюзе: в LeadChat его нет и в запросе его нет.
        assert "apikey" not in request.url.params and b"apikey" not in request.content
        assert request.headers["Authorization"] == "Bearer test-token"
        return ответы[походов - 1]

    async with httpx.AsyncClient(transport=httpx.MockTransport(обработчик)) as client:
        with pytest.raises(geocode.GeocodeError) as exc:
            await yandex_geocoder.search(ЗАПРОС, client=client)
        assert exc.value.kind == "network"
        await yandex_geocoder.search(ЗАПРОС, client=client)
        await yandex_geocoder.search(ЗАПРОС, client=client)
    assert походов == 2  # отказ не запомнен, удача — запомнена
    ключи = [k async for k in redis.scan_iter("geo:cache:yandex:*")]
    assert len(ключи) == 1
    geo_cache.bind(None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(обработчик)) as client:
        ответы.append(httpx.Response(200, json={"ok": True, "hits": []}))
        await yandex_geocoder.search(ЗАПРОС, client=client)
    assert походов == 3
