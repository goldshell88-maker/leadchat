"""Модели: читатели адреса (`POST /llm/chat`) и Anthropic для ботов
(`POST /llm/anthropic/tool`).

Обе ручки отвечают HTTP 200 всегда: отказ провайдера — `{"ok": false, "kind",
"status", "detail"}` (`errors.py`), у `/llm/chat` ещё `attempts` — и при
отказе тоже: LeadChat считает по ним суточный потолок читателей.
"""

from __future__ import annotations

from typing import Any

import structlog
from fastapi import APIRouter
from pydantic import BaseModel, Field

from leadchat_gateway.errors import ProviderError, from_error, ok
from leadchat_gateway.providers import anthropic as anthropic_provider
from leadchat_gateway.providers import llm

log = structlog.get_logger("leadchat_gateway.routes.llm")

router = APIRouter()


class ChatIn(BaseModel):
    system: str
    user: str
    #: Потолок на всю цепочку читателей; задача LeadChat живёт 300 с.
    deadline_sec: float = Field(default=200.0, gt=0)


class ToolIn(BaseModel):
    model: str
    #: Строка или список блоков с `cache_control` — как LeadChat собирает.
    system: Any
    messages: list[dict[str, Any]]
    tool: dict[str, Any]
    max_tokens: int = Field(gt=0)
    timeout_sec: float = Field(default=15.0, gt=0)


@router.post("/llm/chat")
async def llm_chat(body: ChatIn) -> dict[str, Any]:
    try:
        content, model, attempts = await llm.chat(
            body.system, body.user, deadline_sec=body.deadline_sec
        )
    except llm.ChatError as exc:
        log.info("llm.chat_failed", kind=exc.kind, status=exc.status, attempts=exc.attempts)
        return from_error(exc, attempts=exc.attempts)
    log.info("llm.chat_ok", model=model, attempts=attempts)
    return ok(content=content, model=model, attempts=attempts)


@router.post("/llm/anthropic/tool")
async def anthropic_tool(body: ToolIn) -> dict[str, Any]:
    try:
        data = await anthropic_provider.call_tool(
            model=body.model,
            system=body.system,
            messages=body.messages,
            tool=body.tool,
            max_tokens=body.max_tokens,
            timeout_sec=body.timeout_sec,
        )
    except ProviderError as exc:
        return from_error(exc)
    return ok(input=data)
