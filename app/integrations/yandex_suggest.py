"""Яндекс Геосаджест — подсказка адреса, терпимая к опечаткам и сокращениям;
с 16.09 через шлюз на Амстердаме.

ЗАЧЕМ (просьба владельца 11.09: «люди пишут адреса сокращённо или с
ошибками»). Геокодер и OSM на «Звенигародская» или «Звенигор 1» отвечают «не
нашли». Саджест — это подсказки при наборе: он устроен так, чтобы по обрывку с
ошибкой предложить настоящую улицу. Мы спрашиваем его ТОЛЬКО когда карты дом
не нашли, берём исправленную улицу и дом — и снова идём к карте за координатами
и вердиктом. Саджест сам ничего не подтверждает: у него нет координат и нет
ответа «дом существует», это текст.

Бесплатный тариф — своя тысяча запросов в сутки (отдельно от Геокодера);
считается ключом `geo:yandex_suggest:calls:<день>`.

Ключ живёт на шлюзе; `enabled()` читает снимок `/status`. HTTP и разбор —
`gateway/leadchat_gateway/providers/yandex_suggest.py`; здесь — кэш, счётчик
и маппинг отказов в `GeocodeError`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.integrations import gateway
from app.services import geo_cache
from app.services.geocode import GeocodeError, Query

PROVIDER = "yandex_suggest"
#: Таймаут провайдера у шлюза — 5 с; запас на дорогу по мосту.
TIMEOUT_SEC = 8.0


@dataclass(frozen=True, slots=True)
class Suggested:
    """Исправленный адрес из подсказки — только компоненты, без координат."""

    street: str
    house: str
    city: str | None
    settlement: str | None
    region: str | None
    formatted: str | None


def enabled() -> bool:
    return gateway.enabled() and gateway.key_present(PROVIDER)


async def suggest(
    query: Query,
    *,
    client: httpx.AsyncClient | None = None,
    on_request: Callable[[], Awaitable[None]] | None = None,
) -> list[Suggested]:
    """`on_request` — счётчик суточного потолка: только при настоящем походе.
    `client` — транспорт к ШЛЮЗУ (для тестов)."""
    payload = {"free_text": query.free_text}
    ключ = {"v": 2, **payload}
    из_кэша = await geo_cache.get(PROVIDER, ключ)
    if isinstance(из_кэша, list):
        return _hits(из_кэша)
    if on_request is not None:
        await on_request()
    try:
        данные = await gateway.call(
            "/geo/yandex-suggest", payload, timeout=TIMEOUT_SEC, client=client, provider=PROVIDER
        )
    except gateway.GatewayError as exc:
        raise GeocodeError(PROVIDER, gateway.kind_for(exc), exc.status) from exc
    hits = данные.get("hits")
    if not isinstance(hits, list):
        raise GeocodeError(PROVIDER, "bad_response")
    ответ = _hits(hits)
    await geo_cache.put(PROVIDER, ключ, hits, empty=not ответ)
    return ответ


def _hits(строки: list[dict[str, Any]]) -> list[Suggested]:
    try:
        return [Suggested(**h) for h in строки]
    except TypeError as exc:
        # Поля ответа шлюза — один в один с Suggested; иное — чужой или сломанный шлюз.
        raise GeocodeError(PROVIDER, "bad_response") from exc
