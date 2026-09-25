"""Anthropic для ботов LeadChat: один вызов модели с принудительным tool-use
(переезд `_call_tool` из `app/bots/ai.py`, 16.09).

Промпты, схемы инструментов, предохранитель и `AI_FAKE` остались в LeadChat;
здесь — только ключ, SDK и чтение блока `tool_use`. Ответ — `input` этого
блока; всё остальное (нет блока, отказ модели, сеть) — `ProviderError`, и
LeadChat читает его как «модель недоступна» → handoff, как и раньше.

⚠ Класть ли ключ Anthropic на Амстердам — решение владельца (docs/46 §6): с
петербургского прода `api.anthropic.com` отвечает 403, а обходить региональные
ограничения провайдера через промежуточные узлы нельзя. Код этого не решает:
без `ANTHROPIC_API_KEY` ручка отвечает `no_key`.

SDK импортируется лениво: шлюз без пакета `anthropic` обязан подниматься и
обслуживать карты — тогда ручка отвечает `network` с именем причины.
"""

from __future__ import annotations

import asyncio
from typing import Any

import structlog

from leadchat_gateway.config import settings
from leadchat_gateway.errors import ProviderError

log = structlog.get_logger("leadchat_gateway.providers.anthropic")

#: Клиент SDK живёт долго (пул соединений); пересобирается при смене ключа,
#: адреса или бюджета — в бою это происходит только при перезапуске.
_client: Any = None
_client_key: tuple[str, str, float] | None = None


def _build_client(api_key: str, base_url: str, timeout_sec: float) -> Any:
    from anthropic import AsyncAnthropic

    # Авторетраи SDK запрещены: они умножают время внутри бюджета вызова, а
    # LeadChat и так уходит в handoff по первой же неудаче (02 §3.3).
    # Таймаут SDK чуть меньше стены `wait_for`, чтобы сокет закрылся первым.
    return AsyncAnthropic(
        api_key=api_key,
        base_url=base_url,
        timeout=max(1.0, timeout_sec - 1.0),
        max_retries=0,
    )


def _база() -> str:
    # Пустая строка `ANTHROPIC_BASE_URL=` в .env — умолчание, а не «без адреса».
    return (settings.anthropic_base_url or "").strip() or str(
        type(settings).model_fields["anthropic_base_url"].default
    )


def _get_client(api_key: str, timeout_sec: float) -> Any:
    global _client, _client_key
    ключ = (api_key, _база(), timeout_sec)
    if _client is not None and _client_key == ключ:
        return _client
    _client = _build_client(api_key, _база(), timeout_sec)
    _client_key = ключ
    return _client


def reset_client() -> None:
    """Забыть клиент (тесты)."""
    global _client, _client_key
    _client, _client_key = None, None


async def call_tool(
    *,
    model: str,
    system: Any,
    messages: list[dict[str, Any]],
    tool: dict[str, Any],
    max_tokens: int,
    timeout_sec: float,
) -> dict[str, Any]:
    """`input` блока `tool_use` с именем `tool["name"]`; иначе `ProviderError`."""
    api_key = settings.anthropic_api_key.strip()
    if not api_key:
        raise ProviderError("no_key", None, "ANTHROPIC_API_KEY не задан на шлюзе")
    try:
        import anthropic
    except ModuleNotFoundError as exc:
        log.warning("anthropic.sdk_missing")
        raise ProviderError("network", None, "SDK anthropic не установлен") from exc
    client = _get_client(api_key, timeout_sec)
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=[tool],
                # Рассуждения выключены осознанно: бюджет вызова — секунды, а
                # ответ и так жёстко структурирован схемой инструмента.
                thinking={"type": "disabled"},
                tool_choice={
                    "type": "tool",
                    "name": tool["name"],
                    "disable_parallel_tool_use": True,
                },
            ),
            timeout=timeout_sec,
        )
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
        log.warning("anthropic.blocked", model=model, status=exc.status_code)
        raise ProviderError("blocked", exc.status_code, type(exc).__name__) from exc
    except Exception as exc:  # noqa: BLE001 — любая ошибка = недоступность (02 §3.3)
        status = getattr(exc, "status_code", None)
        log.warning("anthropic.call_failed", model=model, error=type(exc).__name__, status=status)
        raise ProviderError(
            "network", status if isinstance(status, int) else None, type(exc).__name__
        ) from exc

    block = next(
        (
            b
            for b in getattr(response, "content", None) or []
            if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == tool["name"]
        ),
        None,
    )
    if block is None:
        # Отказ модели, refusal, пустой ответ: не ошибка сети, но результата
        # нет — для LeadChat это та же недоступность.
        log.warning("anthropic.no_tool_use", model=model, tool=tool["name"])
        raise ProviderError("bad_response", None, "нет блока tool_use")
    data = getattr(block, "input", None)
    if not isinstance(data, dict):
        raise ProviderError("bad_response", None, "input блока не объект")
    return data
