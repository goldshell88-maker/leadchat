"""Ahunter («Охотник за адресами») — второй справочник ГАР, без ключа и потолка.

ЗАЧЕМ (исследование 16.09). DaData — 10 000 запросов в сутки на все методы,
и к вечеру догона её нет; Яндекс Саджест — 1 000 в сутки и чужая лицензия.
Ahunter отдаёт подсказки по тому же ГАР одной строкой, без регистрации и
без суточного потолка, знает кварталы Ангарска («кв-л 57, дом 8»), корпуса
Зеленограда («г Зеленоград, корп 1218»), СНТ и территории. Координат в
анонимном ответе нет — как и у Саджеста, это ТЕКСТ: исправленная улица,
дом и пункт, с которыми мы снова идём к карте за координатами и вердиктом.
Сам Ahunter ничего не подтверждает.

С 16.09 поход к Ahunter и разбор его строки живут на шлюзе (docs/46,
`gateway/leadchat_gateway/providers/ahunter.py`); здесь — обёртка с прежним
именем: собрать строку запроса, кэш, счётчик, маппинг отказов.

⚠ СЕРВИС ЖИВЁТ НА ОДНОМ СЕРВЕРЕ И БЕЗ SLA: любой отказ (сеть, 403, не JSON)
кладёт его на десять минут — `_ahunter_прилёг` в воркере, чтобы хвост починки
не стучал в лежащий сервер строкой за строкой. Клиентских данных наружу не
уходит: только собранная адресная строка — город, пункт, улица и дом из
разбора (`query_text`), без имён и телефонов.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.integrations import gateway
from app.services import geo_cache
from app.services.geocode import GeocodeError, Query

PROVIDER = "ahunter"
#: Таймаут провайдера на шлюзе 4 с + запас на дорогу по мосту.
TIMEOUT_SEC = 6.0


@dataclass(frozen=True, slots=True)
class Suggested:
    """Подсказка ГАР — компоненты словами справочника, без координат."""

    street: str
    house: str
    city: str | None
    settlement: str | None
    district: str | None
    region: str | None
    formatted: str


_ПОЛЯ = tuple(f.name for f in dataclasses.fields(Suggested))


def query_text(query: Query) -> str:
    """Строка для Ahunter: город, пункт, улица, дом — без региона (он у
    справочника свой, а слово «область» в запросе только мешает)."""
    куски = [query.city, query.settlement, f"{query.street_for_map} {query.house}"]
    return " ".join(к.strip() for к in куски if к and к.strip())


def _собрать(hits: Any) -> list[Suggested]:
    """`hits` шлюза (или кэша) → подсказки. Лишние поля шлюза не мешают,
    недостающие — форма не та."""
    if not isinstance(hits, list):
        raise GeocodeError(PROVIDER, "bad_response")
    try:
        return [Suggested(**{п: h[п] for п in _ПОЛЯ}) for h in hits]
    except (KeyError, TypeError) as exc:
        raise GeocodeError(PROVIDER, "bad_response") from exc


async def suggest(
    query: Query,
    *,
    client: httpx.AsyncClient | None = None,
    on_request: Callable[[], Awaitable[None]] | None = None,
) -> list[Suggested]:
    """`on_request` — счётчик походов (для экрана настроек), не потолок;
    зовётся только перед настоящим походом в шлюз, не при попадании в кэш."""
    тело = {"text": query_text(query)}
    для_кэша = {"v": 2, **тело}
    из_кэша = await geo_cache.get(PROVIDER, для_кэша)
    if из_кэша is not None:
        return _собрать(из_кэша)
    if on_request is not None:
        await on_request()
    try:
        данные = await gateway.call(
            "/geo/ahunter", тело, timeout=TIMEOUT_SEC, client=client, provider=PROVIDER
        )
    except gateway.GatewayError as exc:
        raise GeocodeError(PROVIDER, gateway.kind_for(exc), exc.status) from exc
    hits = данные.get("hits")
    ответ = _собрать(hits)
    await geo_cache.put(PROVIDER, для_кэша, hits, empty=not ответ)
    return ответ
