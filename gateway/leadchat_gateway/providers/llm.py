"""Читатели адреса: OpenRouter → Groq → Mistral (переезд из
`app/integrations/openrouter.py` LeadChat, 16.09).

ЗАЧЕМ. Правила разбора адреса ловят «улицу + дом», а речь вокруг («можно
будет часов 11 улица Зеленогорская 17/3кв 4») читает модель и ПРЕДЛАГАЕТ
разбор; решает по-прежнему сторож цитаты и карта на стороне LeadChat.

⚠ ТОЛЬКО БЕСПЛАТНЫЕ МОДЕЛИ — решение владельца 11.09: «не хочу, чтобы поле
тратило деньги через API Anthropic, только бесплатные API OpenRouter». Список —
в `OPENROUTER_ADDRESS_MODELS`, по умолчанию три с поддержкой JSON-ответа;
модели пробуются по очереди: 5xx/сеть/кривой JSON — следующая, 429 аккаунта —
следующая ТОЧКА (лимит на аккаунт, соседняя модель ответит тем же).
Бесплатный тариф OpenRouter: 20 запросов в минуту и 50 в сутки (1000 — при
купленных кредитах от $10); потолок на сутки считает LeadChat.

Читателей три (`endpoints`): OpenRouter, затем Groq (`GROQ_API_KEY`, 1 000
запросов в сутки на модель) и Mistral (`MISTRAL_API_KEY`, Free — $10 кредитов
в месяц). Все — один и тот же OpenAI-совместимый `chat/completions`. Шлюз
ходит к ним напрямую из Амстердама — мост в Caddy больше не нужен.

Что уходит наружу: текст входящих реплик клиента за сутки (телефоны погашены
на стороне LeadChat) и город объявления. Провайдеры бесплатных моделей хранят
промпты и могут учиться на них — принято владельцем ради самой помощи.

Один вызов `chat()` — вся цепочка: перебор точек и моделей под общим дедлайном;
сколько запросов ушло — `attempts` (LeadChat считает по ним суточный потолок).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx
import structlog

from leadchat_gateway import http
from leadchat_gateway.config import settings
from leadchat_gateway.errors import ProviderError

log = structlog.get_logger("leadchat_gateway.providers.llm")

#: Порядок — по стенду 14.09 с прода (36 размеченных реплик боя, маски вместо
#: номеров): Nex-pro 31 верных, 1 ложный, ~28 с; Cohere north-mini 27/2, 17 с;
#: Dots 26/1, 6 с; Nex-mini 23/2, 6 с; Nemotron-super и -lightning ломают JSON
#: (21 ошибка из 36); Gemma-4 упирается в лимит провайдера; Inkling 403,
#: Ling 400. Читатель работает в фоне — точность важнее секунд, поэтому
#: первой медленная Nex-pro, за ней быстрые запасные.
DEFAULT_MODELS = (
    "nex-agi/nex-n2.5-pro:free",
    "dots-studio/dots-3-note-preview:free",
    "cohere/north-mini-code:free",
)
#: Запасные читатели: модели бесплатных тарифов — Groq gpt-oss-120b
#: (1 000/сутки), Mistral Small (в $10 месячных кредитов).
DEFAULT_GROQ_MODELS = ("openai/gpt-oss-120b", "qwen/qwen3.8-27b")
DEFAULT_MISTRAL_MODELS = ("mistral-small-latest", "ministral-8b-latest")
#: Nex-pro отвечает в среднем 28 с — хвосту нужен запас, 40 с резало его.
TIMEOUT = httpx.Timeout(75.0, connect=5.0)
#: Рассуждающие модели (Nemotron) тратят сотни токенов на рассуждение до
#: ответа; при 400 ответ обрезался до «{}» (проба с прода 13.09).
MAX_TOKENS = 1500
#: Запрос не начинается, если до дедлайна меньше его таймаута: задача ARQ
#: в LeadChat живёт 300 с, а три точки по две-три модели по 75 с дали бы
#: до 525 с — задачу убили бы посреди ответа (ревью 16.09).
_ЗАПАС_СЕК = 75.0

_ОГРАДА_JSON = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


@dataclass(frozen=True, slots=True)
class Endpoint:
    """Куда идти за ответом: имя, адрес, ключ, модели по очереди."""

    name: str
    base_url: str
    api_key: str
    models: tuple[str, ...]


class ChatError(ProviderError):
    """Все точки отказали. `attempts` — сколько запросов ушло: LeadChat считает
    по ним суточный потолок и при отказе тоже."""

    def __init__(
        self, kind: str, status: int | None = None, detail: str = "", *, attempts: int
    ) -> None:
        super().__init__(kind, status, detail)
        self.attempts = attempts


def _настройка(имя: str) -> str:
    return (getattr(settings, имя, "") or "").strip()


def _список(имя: str, умолчание: tuple[str, ...]) -> tuple[str, ...]:
    свои = tuple(m.strip() for m in _настройка(имя).split(",") if m.strip())
    return свои or умолчание


def endpoints() -> list[Endpoint]:
    """Читатели с ключом, по порядку: OpenRouter → Groq → Mistral."""
    out: list[Endpoint] = []
    for имя, умолчание_моделей in (
        ("openrouter", DEFAULT_MODELS),
        ("groq", DEFAULT_GROQ_MODELS),
        ("mistral", DEFAULT_MISTRAL_MODELS),
    ):
        ключ = _настройка(f"{имя}_api_key")
        if not ключ:
            continue
        # Пустой адрес в .env — не «без адреса», а умолчание провайдера.
        адрес = _настройка(f"{имя}_base_url") or str(
            type(settings).model_fields[f"{имя}_base_url"].default
        )
        out.append(
            Endpoint(
                name=имя,
                base_url=адрес.rstrip("/"),
                api_key=ключ,
                models=_список(f"{имя}_address_models", умолчание_моделей),
            )
        )
    return out


def parse_content(content: str) -> dict[str, Any]:
    """Текст ответа модели → JSON-объект; ограда ```json``` снимается."""
    текст = content.strip()
    m = _ОГРАДА_JSON.match(текст)
    if m:
        текст = m.group(1)
    if not текст.startswith("{"):
        # Модель поболтала вокруг — берём первый объект.
        начало, конец = текст.find("{"), текст.rfind("}")
        if начало < 0 or конец <= начало:
            raise ValueError("no json object")
        текст = текст[начало : конец + 1]
    данные = json.loads(текст)
    if not isinstance(данные, dict):
        raise ValueError("json is not an object")
    return данные


@dataclass
class _Ход:
    """Общий счёт одного вызова `chat`: когда начали и сколько запросов ушло."""

    начало: float
    attempts: int = 0

    def просрочен(self, deadline_sec: float) -> bool:
        return time.monotonic() - self.начало > deadline_sec - _ЗАПАС_СЕК


async def chat(system: str, user: str, *, deadline_sec: float) -> tuple[dict[str, Any], str, int]:
    """Ответ модели как JSON-объект, имя ответившей модели («groq:…» у запасных)
    и число запросов.

    Читатели — по очереди (`endpoints`): OpenRouter выбрал суточный потолок или
    лёг — отвечает Groq, потом Mistral; 401/403 у одного — не приговор
    остальным. Следующая точка не зовётся, когда времени на неё уже нет.
    Полный провал — `ChatError` с `kind` последней ошибки и `attempts`.
    """
    точки = endpoints()
    if not точки:
        raise ChatError("no_key", None, "ни у одного читателя нет ключа", attempts=0)
    ход = _Ход(начало=time.monotonic())
    последняя: ProviderError | None = None
    async with http.client(TIMEOUT) as client:
        for точка in точки:
            if ход.просрочен(deadline_sec):
                log.warning("llm.deadline", endpoint=точка.name, attempts=ход.attempts)
                break
            try:
                данные, модель = await _спросить(
                    точка, system, user, client=client, ход=ход, deadline_sec=deadline_sec
                )
            except ProviderError as exc:
                log.warning(
                    "llm.endpoint_failed", endpoint=точка.name, kind=exc.kind, status=exc.status
                )
                последняя = exc
                continue
            return данные, модель, ход.attempts
    if последняя is None:
        raise ChatError(
            "exhausted", None, "дедлайн вышел до первого запроса", attempts=ход.attempts
        )
    raise ChatError(последняя.kind, последняя.status, последняя.detail, attempts=ход.attempts)


async def _спросить(
    точка: Endpoint,
    system: str,
    user: str,
    *,
    client: httpx.AsyncClient,
    ход: _Ход,
    deadline_sec: float,
) -> tuple[dict[str, Any], str]:
    headers = {
        "Authorization": f"Bearer {точка.api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Title": "LeadChat address reader",
    }
    последняя: ProviderError | None = None
    for модель in точка.models:
        if ход.просрочен(deadline_sec):
            break
        body = {
            "model": модель,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": MAX_TOKENS,
            "response_format": {"type": "json_object"},
        }
        ход.attempts += 1
        try:
            resp = await client.post(
                f"{точка.base_url}/chat/completions", json=body, headers=headers
            )
        except httpx.HTTPError as exc:
            log.warning("llm.network", model=модель, error=type(exc).__name__)
            последняя = http.network_error(exc)
            continue
        if resp.status_code in (401, 403):
            raise ProviderError("blocked", resp.status_code, "ключ читателя отвергнут")
        if resp.status_code == 429:
            # Два разных 429: у ПРОВАЙДЕРА модели («temporarily rate-limited
            # upstream», проба с прода 13.09 — Gemma) — следующая модель
            # ответит; у АККАУНТА OpenRouter (20 в минуту, 50 в сутки без
            # кредитов) — следующая ответит тем же, а потолок потратится втрое.
            # У Groq и Mistral 429 — всегда аккаунт: следующая точка.
            if точка.name == "openrouter" and _лимит_провайдера(resp):
                log.warning("llm.provider_limited", model=модель)
                последняя = ProviderError("network", 429, "лимит провайдера модели")
                continue
            log.warning("llm.rate_limited", model=модель)
            raise ProviderError("exhausted", 429, "лимит аккаунта читателя")
        if resp.status_code != 200:
            # 400 — модель не умеет JSON-режим, 5xx — провайдер лёг: лечится
            # следующей моделью.
            log.warning("llm.status", model=модель, status=resp.status_code)
            последняя = ProviderError("network", resp.status_code, "неожиданный статус")
            continue
        try:
            content = resp.json()["choices"][0]["message"]["content"]
            данные = parse_content(str(content))
            if not данные:
                # «{}» — ответ обрезан или модель промолчала: следующая.
                raise ValueError("empty object")
            return данные, (модель if точка.name == "openrouter" else f"{точка.name}:{модель}")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            log.warning("llm.bad_response", model=модель, error=type(exc).__name__)
            последняя = ProviderError("bad_response", resp.status_code, type(exc).__name__)
            continue
    raise ProviderError(
        "exhausted",
        последняя.status if последняя else None,
        "все модели точки отказали" if последняя else "дедлайн вышел",
    )


def _лимит_провайдера(resp: httpx.Response) -> bool:
    """429 от провайдера модели, а не от аккаунта OpenRouter."""
    try:
        ошибка = resp.json().get("error") or {}
    except ValueError:
        return False
    текст = " ".join(str(v) for v in (ошибка.get("message"), ошибка.get("metadata"))).lower()
    return "upstream" in текст or "provider" in текст
