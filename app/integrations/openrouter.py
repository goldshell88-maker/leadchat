"""Читатели адреса — OpenRouter → Groq → Mistral — через шлюз на Амстердаме
(docs/46, 16.09). Обёртка: то же имя модуля и те же `chat_json`/`enabled`/
`OpenRouterError`, что до переезда.

ЗАЧЕМ. Правила разбора (`address_parse`) ловят адрес там, где есть голова
«улица + дом», и молчат на речи вокруг («можно будет часов 11 улица
Зеленогорская 17/3кв 4»). Владелец: «у нас есть бесплатные ИИ-модели, пусть они
помогают». Модель читает переписку клиента и ПРЕДЛАГАЕТ разбор; решает
по-прежнему сторож цитаты и карта (`address_llm`, `workers/address_llm`).

⚠ ТОЛЬКО БЕСПЛАТНЫЕ МОДЕЛИ — решение владельца 11.09. Ключи читателей, список
моделей, перебор точек и разбор ответа живут в шлюзе
(`gateway/leadchat_gateway/providers/llm.py`); здесь — один поход в
`POST /llm/chat` и счётчик запросов.

Что уходит наружу: текст входящих реплик клиента за сутки (телефоны
погашены) и город объявления — через Амстердам, как раньше через мост
OpenRouter в Caddy; оба сервера — владельца.

Потолок `ADDRESS_LLM_DAILY_LIMIT` считает запросы ко всем читателям вместе:
шлюз возвращает `attempts`, и `on_request` зовётся столько же раз.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import structlog

from app.integrations import gateway

log = structlog.get_logger()

PROVIDER = "openrouter"
#: Читатели в порядке очереди на шлюзе; `enabled()` — хоть у одного есть ключ.
READERS = ("openrouter", "groq", "mistral")
#: Потолок на ВСЮ цепочку читателей: задача ARQ живёт 300 с (`job_timeout`),
#: а три точки по две-три модели с таймаутом 75 с дали бы до 525 с — задачу
#: убили бы посреди ответа (ревью 16.09). Шлюз держит его сам, мы ждём чуть
#: дольше — на дорогу по мосту.
DEADLINE_SEC = 200.0
_ЗАПАС_МОСТА_СЕК = 10.0


class OpenRouterError(Exception):
    """Модели не ответили по-человечески. Без тела ответа — в нём переписка.

    `kind`: `blocked` — ключей нет (у шлюза) или 401/403 у всех читателей,
    повторять бесполезно; `exhausted` — все модели по очереди отказали
    (лимиты, сеть, не JSON); `network` — обрыв/таймаут у последней точки или
    недоступен сам шлюз; `bad_response` — ответ не той формы.
    """

    def __init__(self, kind: str, status: int | None = None) -> None:
        super().__init__(f"{PROVIDER}: {kind}" + (f" ({status})" if status else ""))
        self.kind = kind
        self.status = status


def enabled() -> bool:
    """Шлюз настроен и по его снимку хоть у одного читателя есть ключ."""
    return gateway.enabled() and any(gateway.key_present(имя) for имя in READERS)


async def chat_json(
    system: str,
    user: str,
    *,
    client: httpx.AsyncClient | None = None,
    on_request: Callable[[], Awaitable[None]] | None = None,
) -> tuple[dict[str, Any], str]:
    """Ответ модели как JSON-объект и имя ответившей модели («groq:…» у запасных).

    `on_request` — счётчик: один раз ДО похода в шлюз и ещё `attempts - 1`
    раз после ответа — по числу запросов, которые шлюз сделал к читателям
    (потолок считает запросы, а не ответы). При отказе число попыток
    приезжает в `GatewayError.extra["attempts"]` и считается так же.
    """
    if not enabled():
        raise OpenRouterError("blocked")
    if on_request is not None:
        await on_request()
    try:
        данные = await gateway.call(
            "/llm/chat",
            {"system": system, "user": user, "deadline_sec": DEADLINE_SEC},
            timeout=DEADLINE_SEC + _ЗАПАС_МОСТА_СЕК,
            client=client,
            provider=PROVIDER,
        )
    except gateway.GatewayError as exc:
        await _досчитать(on_request, exc.extra.get("attempts"))
        raise OpenRouterError(_kind(exc), exc.status) from exc
    content, модель = данные.get("content"), данные.get("model")
    if not isinstance(content, dict) or not isinstance(модель, str):
        log.warning("openrouter.bad_shape")
        raise OpenRouterError("bad_response")
    await _досчитать(on_request, данные.get("attempts"))
    return content, модель


async def _досчитать(on_request: Callable[[], Awaitable[None]] | None, attempts: Any) -> None:
    """Первый запрос уже посчитан до похода; дозвать счётчик на остальные."""
    if on_request is None or not isinstance(attempts, int):
        return
    for _ in range(max(0, attempts - 1)):
        await on_request()


def _kind(exc: gateway.GatewayError) -> str:
    if exc.kind == "no_key":
        # Ни у одного читателя нет ключа: снимок помнит про всех троих, чтобы
        # `enabled()` погас до следующего `refresh_status()`, а не только про
        # `openrouter`, которого пометил клиент шлюза.
        for имя in READERS:
            gateway.mark_no_key(имя)
        return "blocked"
    return gateway.kind_for(exc)
