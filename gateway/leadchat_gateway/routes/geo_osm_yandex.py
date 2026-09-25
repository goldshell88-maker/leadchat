"""POST /geo/nominatim, /geo/yandex, /geo/yandex-suggest — один вызов = один
запрос к провайдеру. Что и в каком порядке спрашивать (три попытки OSM, кэш,
счётчики) решает LeadChat; здесь — только как спросить и как прочитать ответ.

В лог — провайдер, `kind` и статус. Тело запроса (адрес клиента) — никогда.
"""

from __future__ import annotations

from typing import Any, Literal

import structlog
from fastapi import APIRouter
from pydantic import BaseModel

from leadchat_gateway.errors import ProviderError, from_error, ok
from leadchat_gateway.providers import nominatim, yandex_geocoder, yandex_suggest

router = APIRouter()
log = structlog.get_logger("leadchat_gateway.geo")


class NominatimIn(BaseModel):
    mode: Literal["structured", "free"] = "structured"
    street: str = ""
    house: str = ""
    city: str | None = None
    region: str | None = None
    free_text: str = ""


class YandexIn(BaseModel):
    free_text: str = ""
    city: str | None = None


class YandexSuggestIn(BaseModel):
    free_text: str = ""


def _отказ(provider: str, exc: ProviderError) -> dict[str, Any]:
    log.info("geo.provider_failed", provider=provider, kind=exc.kind, status=exc.status)
    return from_error(exc)


@router.post("/geo/nominatim")
async def geo_nominatim(body: NominatimIn) -> dict[str, Any]:
    try:
        hits = await nominatim.search(nominatim.params(**body.model_dump()))
    except ProviderError as exc:
        return _отказ("nominatim", exc)
    return ok(hits=[h.model_dump() for h in hits])


@router.post("/geo/yandex")
async def geo_yandex(body: YandexIn) -> dict[str, Any]:
    try:
        hits = await yandex_geocoder.search(body.free_text, city=body.city)
    except ProviderError as exc:
        return _отказ("yandex_geocoder", exc)
    return ok(hits=[h.model_dump() for h in hits])


@router.post("/geo/yandex-suggest")
async def geo_yandex_suggest(body: YandexSuggestIn) -> dict[str, Any]:
    try:
        hits = await yandex_suggest.suggest(body.free_text)
    except ProviderError as exc:
        return _отказ("yandex_suggest", exc)
    return ok(hits=[h.model_dump() for h in hits])
