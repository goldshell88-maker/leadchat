"""POST /check/{provider} и POST /check/url — проверка доступности для
монитора LeadChat (`/settings/apis`).

Итог пробы — всегда `{"ok": true, "result": {ok, status, ms, error}}`: провайдер
не ответил или сторож не пустил — это содержимое `result`, а не отказ шлюза.
Так LeadChat отличает «шлюз жив, провайдер лежит» от «шлюза нет». Неизвестное
имя провайдера — HTTP 404: такого имени у монитора нет по построению.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from leadchat_gateway import check
from leadchat_gateway.errors import ok

router = APIRouter()


class UrlBody(BaseModel):
    url: str = ""


# `/check/url` объявлен раньше `/check/{provider}`: маршруты сверяются по
# порядку, и «url» иначе ушёл бы в имя провайдера.
@router.post("/check/url")
async def check_url(body: UrlBody) -> dict[str, Any]:
    return ok(result=await check.probe_url(body.url))


@router.post("/check/{provider}")
async def check_provider(provider: str) -> dict[str, Any]:
    итог = await check.probe_provider(provider)
    if итог is None:
        raise HTTPException(status_code=404, detail="неизвестный провайдер")
    return ok(result=итог)
