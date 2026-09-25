"""Яндекс Геокодер (HTTP API 1.x) — карта по ключу. Перенос
`app/integrations/yandex_geocoder.py` LeadChat (11.09): HTTP и разбор.

Ключ выдаёт кабинет разработчика Яндекса, и условия тарифа принимает владелец:
бесплатный тариф Геокодера для закрытых коммерческих систем не предназначен, а
хранить ответы он запрещает — обе оговорки решает не код, а договор.

Разбор ответа написан по документации формата
(`GeoObjectCollection.featureMember[].GeoObject`,
`metaDataProperty.GeocoderMetaData.{precision, Address.Components[]}`,
`Point.pos = "lon lat"`) и проверен на записанном образце.

Ключ в запросе (`apikey`) — секрет: в журнал не попадает, в ошибках URL не
печатается.
"""

from __future__ import annotations

from typing import Any

import httpx

from leadchat_gateway import http
from leadchat_gateway.config import settings
from leadchat_gateway.errors import ProviderError
from leadchat_gateway.types import GeoHit

BASE_URL = "https://geocode-maps.yandex.ru/1.x/"
RESULTS = 5
TIMEOUT = httpx.Timeout(5.0, connect=3.0)


async def search(free_text: str, *, city: str | None) -> list[GeoHit]:
    """Один GET к Геокодеру по свободной строке; `city` нужен разбору —
    выбрать среди населённых пунктов ответа город объявления."""
    ключ = settings.yandex_geocoder_key.strip()
    if not ключ:
        raise ProviderError("no_key", None, "ключ Яндекс Геокодера не задан")
    params = {
        "apikey": ключ,
        "format": "json",
        "lang": "ru_RU",
        "results": str(RESULTS),
        "geocode": free_text,
    }
    try:
        async with http.client(TIMEOUT) as client:
            resp = await client.get(BASE_URL, params=params)
    except httpx.HTTPError as exc:
        raise http.network_error(exc) from exc
    http.check_status(resp)
    return parse_response(http.parse_json(resp), city=city)


def parse_response(данные: Any, *, city: str | None) -> list[GeoHit]:
    """Ответ Яндекса → общий вид. Чистая функция — проверяется на образце."""
    try:
        члены = данные["response"]["GeoObjectCollection"]["featureMember"]
    except (KeyError, TypeError) as exc:
        raise ProviderError("bad_response", None, "нет GeoObjectCollection") from exc
    out: list[GeoHit] = []
    for член in члены if isinstance(члены, list) else []:
        hit = _hit(член.get("GeoObject") or {}, city=city)
        if hit is not None:
            out.append(hit)
    return out


def _hit(obj: dict[str, Any], *, city: str | None) -> GeoHit | None:
    мета = ((obj.get("metaDataProperty") or {}).get("GeocoderMetaData")) or {}
    компоненты = ((мета.get("Address") or {}).get("Components")) or []
    pos = ((obj.get("Point") or {}).get("pos")) or ""
    try:
        lon_s, lat_s = pos.split()
        lat, lon = float(lat_s), float(lon_s)
    except ValueError:
        return None
    по_виду: dict[str, list[str]] = {}
    for к in компоненты:
        if isinstance(к, dict) and к.get("kind") and к.get("name"):
            по_виду.setdefault(str(к["kind"]), []).append(str(к["name"]))
    населённые = по_виду.get("locality", [])
    # Город — тот из населённых пунктов, что совпал с городом объявления;
    # иначе первый. Остальные пункты и районы — «посёлок» внутри него.
    город = next(
        (
            n
            for n in населённые
            if city and n.lower().replace("ё", "е") == city.lower().replace("ё", "е")
        ),
        населённые[0] if населённые else None,
    )
    # Второй населённый пункт — отдельное место; район — только район.
    прочие_пункты = [n for n in населённые if n != город]
    районы = по_виду.get("district", [])
    прочие = прочие_пункты + районы
    вид = "place" if прочие_пункты else "district"

    def первый(вид: str) -> str | None:
        return по_виду[вид][0] if по_виду.get(вид) else None

    return GeoHit(
        street=первый("street"),
        house=первый("house"),
        settlement=прочие[0] if прочие else None,
        city=город,
        region=по_виду["province"][-1] if по_виду.get("province") else None,
        lat=lat,
        lon=lon,
        house_level=мета.get("precision") == "exact",
        settlement_kind=вид,
    )
