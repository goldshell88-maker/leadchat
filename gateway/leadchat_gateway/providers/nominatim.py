"""Nominatim (OpenStreetMap) — карта без ключа. Перенос `app/integrations/nominatim.py`
LeadChat (11.09): HTTP, честный User-Agent, разбор ответа и темп.

Политика использования OSM допускает наш объём при трёх условиях: не чаще одного
запроса в секунду, честный `User-Agent` с контактом, ответы не переспрашиваются.
Первое здесь держит сам шлюз (`_держать_темп`): LeadChat тоже ждёт своего замка
перед каждым походом, но шлюз — единственная дверь к OSM, и темп обязан жить у
двери. Третье — кэш LeadChat (`geo_cache`), сюда не переезжает.

⚠ ПРОВЕРЕНО ЖИВЬЁМ 11.09 НА ОБРАЗЦЕ ВЛАДЕЛЬЦА. Свободный текст «Орск, п
заречный ул звенигородская д 1» → 0 результатов; структурный запрос
`street=1 улица Звенигородская&city=Орск` → здание. Поэтому два режима:
`structured` (компоненты) и `free` (свободная строка — запасной путь), а какой
и в каком порядке спрашивать — решает LeadChat.

⚠ ОШИБКИ БЕЗ URL И ТЕЛА. В строке запроса — адрес клиента; `str(exc)` от httpx
несёт URL целиком. Наружу уходит только `ProviderError(kind, status)`.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from leadchat_gateway import http
from leadchat_gateway.config import settings
from leadchat_gateway.errors import ProviderError
from leadchat_gateway.types import GeoHit

BASE_URL = "https://nominatim.openstreetmap.org/search"
#: Пять ответов, а не один: единственность дома — сторож вердикта LeadChat, и по
#: одному ответу его не проверить.
LIMIT = 5
TIMEOUT = httpx.Timeout(5.0, connect=3.0)
#: Темп OSM: не чаще одного запроса в секунду. Тесты подменяют.
MIN_INTERVAL_SEC = 1.0

#: Где у OSM лежит населённый пункт внутри города/округа — по убыванию
#: точности. `village`/`hamlet`/`town` — отдельный пункт; `suburb`,
#: `neighbourhood`, `quarter` — район внутри города (и посёлок Заречный, и
#: городской район Форштадт: различить их OSM не помогает, поэтому район идёт
#: в строку адреса только когда его назвал клиент — `GeoHit.settlement_kind`).
_КЛЮЧИ_ПУНКТА = (
    ("village", "place"),
    ("hamlet", "place"),
    ("suburb", "district"),
    ("neighbourhood", "district"),
    ("quarter", "district"),
    ("town", "place"),
)
_КЛЮЧИ_ГОРОДА = ("city", "town", "municipality")

#: Момент, раньше которого следующий запрос к OSM не уйдёт (monotonic).
_не_раньше = 0.0


def user_agent() -> str:
    """Политика OSM требует назвать приложение и контакт."""
    контакт = settings.nominatim_contact.strip()
    return f"LeadChat/1.0 ({контакт})" if контакт else "LeadChat/1.0"


def params(
    *,
    mode: str,
    street: str = "",
    house: str = "",
    city: str | None = None,
    region: str | None = None,
    free_text: str = "",
) -> dict[str, str]:
    """Параметры одного запроса: структурный из компонентов или свободный текст."""
    общие = {
        "format": "jsonv2",
        "addressdetails": "1",
        "accept-language": "ru",
        "limit": str(LIMIT),
        "countrycodes": "ru",
    }
    if mode == "free":
        return {**общие, "q": free_text}
    return {
        **общие,
        "street": f"{house} {street}",
        **({"city": city} if city else {}),
        **({"state": region} if region else {}),
    }


async def _держать_темп() -> None:
    """Занять следующий слот темпа и дождаться его.

    Чтение и запись `_не_раньше` идут без `await` между ними, поэтому два
    одновременных запроса получают соседние слоты, а не один на двоих; замок
    не нужен, и никто не спит с замком в руках.
    """
    global _не_раньше
    сейчас = time.monotonic()
    слот = max(сейчас, _не_раньше)
    _не_раньше = слот + MIN_INTERVAL_SEC
    if слот > сейчас:
        await asyncio.sleep(слот - сейчас)


async def search(параметры: dict[str, str]) -> list[GeoHit]:
    """Один GET к OSM с уже собранными параметрами (см. `params`)."""
    await _держать_темп()
    try:
        async with http.client(TIMEOUT, headers={"User-Agent": user_agent()}) as client:
            resp = await client.get(BASE_URL, params=параметры)
    except httpx.HTTPError as exc:
        raise http.network_error(exc) from exc
    # 403 и HTML вместо JSON — так Nominatim отвечает заблокированным клиентам;
    # 429 и 5xx — временное, повтор с откладыванием (ревью 11.09).
    http.check_status(resp, html_is_blocked=True)
    данные = http.parse_json(resp, html_is_blocked=True)
    if not isinstance(данные, list):
        raise ProviderError("bad_response", resp.status_code, "ответ не список")
    return [hit for hit in (_hit(r) for r in данные if isinstance(r, dict)) if hit is not None]


def _hit(r: dict[str, Any]) -> GeoHit | None:
    адрес = r.get("address") or {}
    if not isinstance(адрес, dict):
        return None
    try:
        lat, lon = float(r["lat"]), float(r["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    дом = адрес.get("house_number")
    ранг = int(r.get("place_rank") or 0)
    # Дом — это ранг 30 с номером. Интерполированный адрес OSM рисует линией
    # (`way` категории `place` типа `house`): это догадка о положении, а не дом.
    интерполяция = (
        r.get("osm_type") == "way" and r.get("category") == "place" and r.get("type") == "house"
    )
    пункт, вид = next(((адрес[k], v) for k, v in _КЛЮЧИ_ПУНКТА if адрес.get(k)), (None, "place"))
    return GeoHit(
        street=адрес.get("road") or адрес.get("pedestrian") or адрес.get("footway"),
        house=str(дом) if дом else None,
        settlement=пункт,
        city=next((адрес[k] for k in _КЛЮЧИ_ГОРОДА if адрес.get(k)), None),
        region=адрес.get("state") or адрес.get("region"),
        lat=lat,
        lon=lon,
        house_level=bool(дом) and ранг >= 30,
        interpolated=интерполяция,
        settlement_kind=вид,
    )
