"""Яндекс Геокодер (HTTP API 1.x) — карта по ключу; с 16.09 через шлюз на Амстердаме.

Владелец просит сверять адрес именно по Яндекс-картам. Ключ выдаёт кабинет
разработчика Яндекса, и условия тарифа принимает владелец: бесплатный тариф
Геокодера для закрытых коммерческих систем не предназначен, а хранить ответы
он запрещает — обе оговорки решает не код, а договор. Пока ключа нет,
провайдер выключен настройкой `address_geo.provider = nominatim`.

Ключ живёт на шлюзе (`/opt/leadchat-gateway/.env`), здесь его нет: `enabled()`
читает снимок `/status` шлюза. HTTP и разбор ответа —
`gateway/leadchat_gateway/providers/yandex_geocoder.py`; здесь — кэш, счётчик
и маппинг отказов в `GeocodeError`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.integrations import gateway
from app.services import geo_cache
from app.services.geocode import GeocodeError, GeoHit, Query

PROVIDER = "yandex"
#: Имя провайдера у шлюза (`/status`, снимок `gateway.known_keys`).
GATEWAY_PROVIDER = "yandex_geocoder"
#: Таймаут провайдера у шлюза — 5 с; запас на дорогу по мосту.
TIMEOUT_SEC = 8.0


def enabled() -> bool:
    return gateway.enabled() and gateway.key_present(GATEWAY_PROVIDER)


async def search(
    query: Query,
    *,
    client: httpx.AsyncClient | None = None,
    on_request: Callable[[], Awaitable[None]] | None = None,
) -> list[GeoHit]:
    """`on_request` — счётчик суточного потолка: зовётся только при настоящем
    походе, ответ из кэша (`geo_cache`) бесплатен. `client` — транспорт к ШЛЮЗУ."""
    payload = {"free_text": query.free_text, "city": query.city}
    ключ = {"v": 3, **payload}
    из_кэша = await geo_cache.get(PROVIDER, ключ)
    if isinstance(из_кэша, list):
        return _hits(из_кэша)
    if on_request is not None:
        await on_request()
    try:
        данные = await gateway.call(
            "/geo/yandex", payload, timeout=TIMEOUT_SEC, client=client, provider=GATEWAY_PROVIDER
        )
    except gateway.GatewayError as exc:
        raise GeocodeError(PROVIDER, gateway.kind_for(exc), exc.status) from exc
    hits = данные.get("hits")
    if not isinstance(hits, list):
        raise GeocodeError(PROVIDER, "bad_response")
    ответ = _hits(hits)
    await geo_cache.put(PROVIDER, ключ, hits, empty=not ответ)
    return ответ


def _hits(строки: list[dict[str, Any]]) -> list[GeoHit]:
    try:
        # Поля — по именам (docs/46 §2.2); лишние поля шлюза не мешают.
        имена = {f.name for f in dataclasses.fields(GeoHit)}
        return [GeoHit(**{k: v for k, v in h.items() if k in имена}) for h in строки]
    except TypeError as exc:
        # Поля ответа шлюза — один в один с GeoHit; иное — чужой или сломанный шлюз.
        raise GeocodeError(PROVIDER, "bad_response") from exc
