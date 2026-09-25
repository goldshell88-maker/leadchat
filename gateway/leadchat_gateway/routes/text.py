"""Текстовые подсказчики без ключа: Ahunter (строка ГАР) и Яндекс Спеллер.

`POST /geo/ahunter` {text} → {hits: [SuggestedAhunter]} — один GET к Ahunter.
`POST /spell` {text, words} → {fixes: {слово: замена}} — один GET к Спеллеру,
выбор варианта здесь. Отказ провайдера — HTTP 200 и `{"ok": false, kind…}`
(`errors.from_error`); в лог — провайдер, kind и статус, без текста запроса.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter
from pydantic import BaseModel

from leadchat_gateway.errors import ProviderError, from_error, ok
from leadchat_gateway.providers import ahunter, speller

log = structlog.get_logger("leadchat_gateway.text")

router = APIRouter()


class AhunterIn(BaseModel):
    text: str


class SpellIn(BaseModel):
    text: str
    words: list[str]


def _отказ(provider: str, exc: ProviderError) -> dict[str, Any]:
    log.warning("provider.failed", provider=provider, kind=exc.kind, status=exc.status)
    return from_error(exc)


@router.post("/geo/ahunter")
async def geo_ahunter(body: AhunterIn) -> dict[str, Any]:
    try:
        hits = await ahunter.suggest(body.text)
    except ProviderError as exc:
        return _отказ(ahunter.PROVIDER, exc)
    return ok(hits=[h.model_dump() for h in hits])


@router.post("/spell")
async def spell(body: SpellIn) -> dict[str, Any]:
    try:
        fixes = await speller.check(body.text, body.words)
    except ProviderError as exc:
        return _отказ(speller.PROVIDER, exc)
    return ok(fixes=fixes)
