"""Яндекс Геосаджест — подсказка адреса, терпимая к опечаткам и сокращениям.
Перенос `app/integrations/yandex_suggest.py` LeadChat (11.09): HTTP и разбор.

Саджест — это подсказки при наборе: по обрывку с ошибкой он предлагает
настоящую улицу. LeadChat спрашивает его ТОЛЬКО когда карты дом не нашли, берёт
исправленную улицу и дом — и снова идёт к карте за координатами. Саджест сам
ничего не подтверждает: у него нет координат, это текст.

Разбор — по документации `suggest-maps.yandex.ru/v1/suggest` с
`print_address=1`: `results[].address.component[] = {name, kind: [KIND]}`,
виды COUNTRY / REGION / PROVINCE / LOCALITY / DISTRICT / STREET / HOUSE.
"""

from __future__ import annotations

from typing import Any

import httpx

from leadchat_gateway import http
from leadchat_gateway.config import settings
from leadchat_gateway.errors import ProviderError
from leadchat_gateway.types import SuggestedYandex

BASE_URL = "https://suggest-maps.yandex.ru/v1/suggest"
RESULTS = 3
TIMEOUT = httpx.Timeout(5.0, connect=3.0)


async def suggest(free_text: str) -> list[SuggestedYandex]:
    """Один GET к Саджесту по свободной строке."""
    ключ = settings.yandex_suggest_key.strip()
    if not ключ:
        raise ProviderError("no_key", None, "ключ Яндекс Геосаджеста не задан")
    params = {
        "apikey": ключ,
        "text": free_text,
        "results": str(RESULTS),
        "print_address": "1",
        "types": "house",
        "lang": "ru",
    }
    try:
        async with http.client(TIMEOUT) as client:
            resp = await client.get(BASE_URL, params=params)
    except httpx.HTTPError as exc:
        raise http.network_error(exc) from exc
    http.check_status(resp)
    return parse_response(http.parse_json(resp))


def parse_response(данные: Any) -> list[SuggestedYandex]:
    """Ответ Саджеста → компоненты. Чистая функция — проверяется на образце."""
    if not isinstance(данные, dict):
        raise ProviderError("bad_response", None, "ответ не объект")
    # ⚠ «НИЧЕГО НЕ НАШЛИ» — ЭТО `{}` БЕЗ КЛЮЧА `results`, А НЕ ПУСТОЙ СПИСОК
    # (бой 12.09: 95 «bad_response» за два часа — все на пустой ответ).
    результаты = данные.get("results", [])
    if not isinstance(результаты, list):
        raise ProviderError("bad_response", None, "results не список")
    out: list[SuggestedYandex] = []
    for r in результаты:
        if not isinstance(r, dict):
            continue
        адрес = r.get("address") or {}
        по_виду: dict[str, list[str]] = {}
        for c in адрес.get("component") or []:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            for kind in c.get("kind") or []:
                по_виду.setdefault(str(kind).upper(), []).append(str(c["name"]))

        улица, дом = _первый(по_виду, "STREET"), _первый(по_виду, "HOUSE")
        if not улица or not дом:
            continue  # подсказка без дома бесполезна: искали дом
        населённые = по_виду.get("LOCALITY") or []
        районы = по_виду.get("DISTRICT") or населённые[1:]
        регионы = по_виду.get("REGION") or по_виду.get("PROVINCE") or []
        out.append(
            SuggestedYandex(
                street=улица,
                house=дом,
                city=населённые[0] if населённые else None,
                settlement=районы[0] if районы else None,
                region=регионы[-1] if регионы else None,
                formatted=адрес.get("formatted_address"),
            )
        )
    return out


def _первый(по_виду: dict[str, list[str]], вид: str) -> str | None:
    return по_виду[вид][0] if по_виду.get(вид) else None
