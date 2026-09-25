"""POST /geo/dadata, /geo/dadata/place, /geo/dadata/city — DaData «Подсказки».

Каждая ручка — ровно один запрос к DaData (docs/46 §2.1): циклы по текстам,
кэш и суточный счётчик — у обёртки LeadChat. Отказ провайдера — HTTP 200 и
`{"ok": false, "kind", "status", "detail"}`; в лог — только провайдер,
операция, `kind` и статус: в теле запроса адрес клиента.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter
from pydantic import BaseModel

from leadchat_gateway.errors import ProviderError, from_error, ok
from leadchat_gateway.providers import dadata

log = structlog.get_logger("leadchat_gateway.routes.geo_dadata")

router = APIRouter()


class AskIn(BaseModel):
    """Одна строка запроса, уже собранная LeadChat («Заречный улица
    Звенигородская 1»), и чем ограничить поиск: город, иначе область, а при
    `near` — круг вокруг точки."""

    text: str
    region: str | None = None
    city: str | None = None
    near: tuple[float, float] | None = None


class PlaceIn(BaseModel):
    query_text: str
    region: str | None = None
    city: str | None = None


class CityIn(BaseModel):
    city: str
    region: str | None = None


def _отказ(op: str, exc: ProviderError) -> dict[str, Any]:
    log.warning("dadata.failed", provider=dadata.PROVIDER, op=op, kind=exc.kind, status=exc.status)
    return from_error(exc)


@router.post("/geo/dadata")
async def geo_dadata(body: AskIn) -> dict[str, Any]:
    try:
        hits = await dadata.ask(body.text, region=body.region, city=body.city, near=body.near)
    except ProviderError as exc:
        return _отказ("ask", exc)
    return ok(hits=[h.model_dump() for h in hits])


@router.post("/geo/dadata/place")
async def geo_dadata_place(body: PlaceIn) -> dict[str, Any]:
    try:
        hits = await dadata.search_place(body.query_text, region=body.region, city=body.city)
    except ProviderError as exc:
        return _отказ("place", exc)
    return ok(hits=[h.model_dump() for h in hits])


@router.post("/geo/dadata/city")
async def geo_dadata_city(body: CityIn) -> dict[str, Any]:
    try:
        point = await dadata.city_point(body.city, body.region)
    except ProviderError as exc:
        return _отказ("city", exc)
    return ok(point=list(point) if point is not None else None)
