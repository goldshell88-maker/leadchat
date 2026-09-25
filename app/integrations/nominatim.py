"""Nominatim (OpenStreetMap) — карта без ключа; с 16.09 через шлюз на Амстердаме.

ПОЧЕМУ ОН, А НЕ ТОЛЬКО ЯНДЕКС. Владелец просит сверять адрес «по Яндекс-картам
и всем открытым источникам». Яндекс-геокодер требует ключ и коммерческий
тариф; Nominatim отвечает без ключа (проверено с прода 11.09: 200 за 0,4 с) и по
политике использования допускает такой объём при трёх условиях: не чаще одного
запроса в секунду (замок в задаче — `wait` — и темп в самом шлюзе), честный
`User-Agent` с контактом (его ставит шлюз), ответы не переспрашиваются
(строка-кандидат уникальна по адресу и хранит вердикт; плюс кэш `geo_cache`).

⚠ ПРОВЕРЕНО ЖИВЬЁМ 11.09 НА ОБРАЗЦЕ ВЛАДЕЛЬЦА. Свободный текст «Орск, п
заречный ул звенигородская д 1» → 0 результатов; структурный запрос
`street=1 улица Звенигородская&city=Орск` → здание. Отсюда правило: только
структурный запрос из компонентов, свободный текст — запасной путь. Какие
попытки и в каком порядке — решает эта обёртка; как спросить OSM (параметры,
заголовки, разбор ответа) — шлюз (`gateway/leadchat_gateway/providers/nominatim.py`).

⚠ ОШИБКИ БЕЗ URL И ТЕЛА. В запросе — адрес клиента. Наружу уходит только
`GeocodeError(provider, kind, status)`; отказ самого шлюза — `network`.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from app.integrations import gateway
from app.services import geo_cache
from app.services.geocode import GeocodeError, GeoHit, Query

PROVIDER = "nominatim"
#: Таймаут провайдера у шлюза — 5 с; запас на дорогу по мосту.
TIMEOUT_SEC = 8.0

#: Ожидание темпа перед КАЖДЫМ походом в шлюз (= каждым запросом к OSM):
#: политика OSM — не чаще одного в секунду, а поиск делает до трёх запросов
#: подряд (структурный, «посёлок вместо города», свободный текст). Замок,
#: взятый один раз на весь поиск, давал залп из трёх (находка ревью 11.09).
Wait = Callable[[], Awaitable[None]]


async def search(
    query: Query, *, client: httpx.AsyncClient | None = None, wait: Wait | None = None
) -> list[GeoHit]:
    """Дома по структурному запросу; пусто — попробовать свободный текст.

    `wait` — ожидание темпа, зовётся перед каждым походом (см. `Wait`).
    `client` — транспорт к ШЛЮЗУ (для тестов).
    """
    попытка = {
        "mode": "structured",
        "street": query.street_for_map,
        "house": query.house,
        "city": query.city,
        "region": query.region,
        "free_text": query.free_text,
    }
    строки = await _спросить(попытка, client=client, wait=wait)
    if not строки and query.settlement and query.city:
        # Посёлок вместо города: «Ленина 5» в Ударнике карта под городом
        # Орском может не знать, а под посёлком — знает.
        строки = await _спросить({**попытка, "city": query.settlement}, client=client, wait=wait)
    if not строки:
        строки = await _спросить({**попытка, "mode": "free"}, client=client, wait=wait)
    return _hits(строки)


async def _спросить(
    payload: dict[str, Any], *, client: httpx.AsyncClient | None, wait: Wait | None
) -> list[dict[str, Any]]:
    """Один запрос к OSM через шлюз: кэш → темп → поход → кэш."""
    ключ = {"v": 3, **payload}
    из_кэша = await geo_cache.get(PROVIDER, ключ)
    if isinstance(из_кэша, list):
        return из_кэша
    if wait is not None:
        await wait()
    try:
        данные = await gateway.call(
            "/geo/nominatim", payload, timeout=TIMEOUT_SEC, client=client, provider=PROVIDER
        )
    except gateway.GatewayError as exc:
        raise GeocodeError(PROVIDER, gateway.kind_for(exc), exc.status) from exc
    hits = данные.get("hits")
    if not isinstance(hits, list):
        raise GeocodeError(PROVIDER, "bad_response")
    await geo_cache.put(PROVIDER, ключ, hits, empty=not hits)
    return hits


def _hits(строки: list[dict[str, Any]]) -> list[GeoHit]:
    try:
        # Поля — по именам (docs/46 §2.2); лишние поля шлюза не мешают.
        имена = {f.name for f in dataclasses.fields(GeoHit)}
        return [GeoHit(**{k: v for k, v in h.items() if k in имена}) for h in строки]
    except TypeError as exc:
        # Поля ответа шлюза — один в один с GeoHit; иное — чужой или сломанный шлюз.
        raise GeocodeError(PROVIDER, "bad_response") from exc
