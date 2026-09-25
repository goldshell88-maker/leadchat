"""AI-подсистема бота: `ai_answer`, `classify_message`, `extract_entities` (02 §3).

Модуль — единственное место во всём бэкенде, которое зовёт Claude, и
вызывается он ТОЛЬКО из воркера (`app/bots/runtime.py` → `ScenarioEngine`).
Из HTTP-запроса пользователя — никогда: бюджет одного вызова (10 секунд)
несовместим с синхронным ответом API. Сам поход к Anthropic с 16.09 живёт на
шлюзе Амстердама (`POST /llm/anthropic/tool`, docs/46): ключ и SDK — там,
здесь — промпты, схемы инструментов, предохранитель и адаптер `GatewayAnthropic`
с интерфейсом SDK, чтобы `_call_tool` не заметил переезда.

Три правила, ради которых модуль выглядит именно так:

1. **`None` — штатный ответ.** Таймаут, 429, 5xx, отказ модели, отсутствие
   ключа на шлюзе, недоступность самого шлюза — всё это одно и то же: «модель
   недоступна». Движок делает `handoff(ai_unavailable)` и НИКОГДА не ретраит
   (02 §3.3, решение владельца №4: недоступность AI не блокирует доставку).
2. **Структура ответа гарантируется tool-use со `strict`**, а не парсингом
   свободного текста: распарсенный `input` обязан соответствовать схеме.
3. **Класть ли ключ Anthropic на шлюз — решение владельца** (docs/46 §6). С
   петербургского сервера API Anthropic отдаёт 403; ВНИМАНИЕ: обходить
   региональные ограничения провайдера через промежуточные узлы нельзя — при
   недоступности AI движок штатно делает handoff.

`AI_FAKE=1` — детерминированные ответы БЕЗ единого сетевого вызова: CI,
локальная отладка и демо без ключа. Контракт тот же самый.

ПРИВАТНОСТЬ: тексты приходят сюда уже с замаскированными телефонами
(`app.bots.steps.mask_phones`), поэтому поле `phone` из EXTRACT_TOOL движок
осознанно игнорирует — источник истины по телефону только локальная регулярка.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any, TypedDict

import structlog

from app.core.config import settings
from app.integrations import gateway

log = structlog.get_logger("app.bots.ai")

# Бюджет стены на один вызов (05 §4: AI_TIMEOUT_SECONDS). Таймаут похода в
# шлюз чуть меньше, чтобы сокет закрылся до нашего `wait_for`; бюджет самого
# шлюза на SDK — ещё меньше, чтобы его ответ успел доехать раньше, чем мы
# бросим ждать (иначе шлюз доработает впустую).
AI_TIMEOUT_SECONDS = float(settings.ai_timeout_seconds)
CLIENT_TIMEOUT_SECONDS = max(1.0, AI_TIMEOUT_SECONDS - 1.0)
GATEWAY_BUDGET_SECONDS = max(1.0, CLIENT_TIMEOUT_SECONDS - 2.0)

MAX_REPLY_CHARS = 1000  # верхний предел ответа модели; шаг режет ещё раз
MAX_EXTRACT_CHARS = 4000  # 02 §3.5: конкатенация входящих клиента
CLASSIFY_CONTEXT = 3  # последние сообщения клиента (02 §3.4)

# --- предохранитель (02 §3.3) ------------------------------------------------
# Счётчик отказов на ПРОЦЕСС, а не в Redis: воркеров единицы, и каждому
# достаточно защитить себя — при инциденте у провайдера мы перестаём расстрелив
# ать rate limit и мгновенно уходим в handoff. Первый успех сбрасывает счётчик.
BREAKER_THRESHOLD = 10
BREAKER_WINDOW_SECONDS = 300.0

_fail_count = 0
_fail_window_started = 0.0


def _breaker_open(now: float | None = None) -> bool:
    """True — вызовы временно запрещены (10 отказов за 5 минут)."""
    moment = now if now is not None else time.monotonic()
    if moment - _fail_window_started > BREAKER_WINDOW_SECONDS:
        return False
    return _fail_count >= BREAKER_THRESHOLD


def _note_failure(now: float | None = None) -> None:
    global _fail_count, _fail_window_started
    moment = now if now is not None else time.monotonic()
    if moment - _fail_window_started > BREAKER_WINDOW_SECONDS:
        _fail_window_started, _fail_count = moment, 0
    _fail_count += 1


def _note_success() -> None:
    global _fail_count, _fail_window_started
    _fail_count, _fail_window_started = 0, 0.0


def reset_breaker() -> None:
    """Сброс предохранителя (тесты и ручное вмешательство админа)."""
    _note_success()


# --- промпты (02 §3.1, §3.4, §3.5) -------------------------------------------

AI_ANSWER_SYSTEM_STATIC = """\
Ты — ассистент сервисного центра Lead Partner (ремонт техники: смартфоны, планшеты,
ноутбуки, бытовая техника). Ты отвечаешь клиентам в чате Авито от имени сервиса,
пока мастер недоступен. Твоя задача — дать первичную консультацию по типовым
вопросам (ориентировочные цены, сроки, порядок работы) и собрать контекст для мастера.

ПРАВИЛА (нарушать их нельзя ни при каких условиях):
1. Отвечай ТОЛЬКО на основе базы знаний, приведённой ниже. Если ответа в базе нет —
   не отвечай по существу: верни needs_operator=true и низкую confidence.
2. НИКОГДА не обещай точную стоимость или срок ремонта. Любая цена — «ориентировочно,
   от N ₽, точная стоимость после бесплатной диагностики».
3. НИКОГДА не выдумывай услуги, акции, скидки, адреса или гарантийные условия,
   которых нет в базе знаний.
4. Не проси предоплату, не давай реквизитов, не отправляй ссылок.
5. Не выдавай себя за живого мастера, но и не подчёркивай, что ты бот, если не спросили.
   Если спросили прямо — честно скажи, что ты автоответчик сервиса, и предложи позвать мастера.
6. Пиши по-русски, дружелюбно и коротко: 1–3 предложения, без списков и заголовков,
   без эмодзи-спама (максимум один эмодзи). Обращайся на «вы».
7. Если клиент раздражён, ругается, говорит о жалобе/возврате/споре — не спорь,
   верни needs_operator=true.
8. Если вопрос не про ремонт техники (спам, реклама, другая тема) — needs_operator=true,
   confidence не выше 0.2.
9. Сомневаешься — needs_operator=true. Передать мастеру — всегда лучше, чем ошибиться.

Ответ верни ТОЛЬКО вызовом инструмента submit_answer. Поля:
- reply: текст ответа клиенту (даже при needs_operator=true — вежливая фраза-мост,
  например «Передаю ваш вопрос мастеру, он ответит в ближайшее время»);
- confidence: число 0..1 — насколько ответ покрыт базой знаний
  (1.0 — прямой ответ из базы; 0.5 — частично; ниже 0.4 — базы не хватает);
- needs_operator: true, если нужен живой сотрудник (нет ответа в базе, негатив,
  нетиповой случай, просьба позвать человека)."""

AI_ANSWER_KB_TEMPLATE = (
    "=== БАЗА ЗНАНИЙ LEAD PARTNER ===\n{knowledge_base}\n=== КОНЕЦ БАЗЫ ЗНАНИЙ ==="
)
EMPTY_KB = "(база знаний пуста)"

CLASSIFY_SYSTEM = (
    "Ты — классификатор сообщений клиентов сервиса ремонта техники Lead Partner.\n"
    "Тебе дают последние сообщения клиента из чата Авито. Определи:\n"
    "1) sentiment: negative — если клиент зол, ругается, жалуется на сервис, грозит "
    "отзывом, спором на Авито, возвратом денег, юристом; neutral — обычный вопрос; "
    "positive — благодарность, согласие.\n"
    "2) wants_human: просит ли клиент живого человека (примеры: «позовите оператора», "
    "«есть тут кто живой?», «хватит мне писать ботом», «дайте мастера», "
    "«соедините с менеджером»).\n"
    "Иронию и вежливое недовольство («ну отлично, конечно…») тоже считай негативом.\n"
    "Ответь только вызовом инструмента classify."
)

EXTRACT_SYSTEM = (
    "Извлеки данные из сообщений клиента сервиса ремонта техники. "
    "Не выдумывай: если чего-то нет в тексте — верни null. "
    "Модель техники нормализуй (айфон 13 про -> iPhone 13 Pro). "
    "Ответь только вызовом инструмента extract."
)

# --- инструменты (strict: распарсенный input обязан соответствовать схеме) ----

SUBMIT_ANSWER_TOOL: dict[str, Any] = {
    "name": "submit_answer",
    "description": "Вернуть структурированный ответ для клиента сервиса Lead Partner.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "reply": {
                "type": "string",
                "description": "Ответ клиенту, 1-3 предложения, по-русски",
            },
            "confidence": {
                "type": "number",
                "description": "0..1, покрытие ответа базой знаний",
            },
            "needs_operator": {
                "type": "boolean",
                "description": "true, если нужен живой сотрудник",
            },
        },
        "required": ["reply", "confidence", "needs_operator"],
        "additionalProperties": False,
    },
}

CLASSIFY_TOOL: dict[str, Any] = {
    "name": "classify",
    "description": "Классифицировать сообщение клиента сервиса ремонта техники.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "sentiment": {
                "type": "string",
                "enum": ["positive", "neutral", "negative"],
                "description": ("negative = злость, жалоба, угроза отзывом/спором, мат, обвинения"),
            },
            "wants_human": {
                "type": "boolean",
                "description": (
                    "true, если клиент просит живого человека/мастера/менеджера "
                    "(в т.ч. перефразированно)"
                ),
            },
            "reason": {
                "type": "string",
                "description": "краткое объяснение по-русски, до 15 слов",
            },
        },
        "required": ["sentiment", "wants_human", "reason"],
        "additionalProperties": False,
    },
}

EXTRACT_TOOL: dict[str, Any] = {
    "name": "extract",
    "description": "Извлечь структурированные данные из переписки с клиентом сервиса ремонта.",
    "strict": True,
    "input_schema": {
        "type": "object",
        "properties": {
            "phone": {
                "type": ["string", "null"],
                "description": "телефон в формате +7XXXXXXXXXX или null",
            },
            "device_brand": {
                "type": ["string", "null"],
                "description": "производитель: Apple, Samsung, HP, LG, Bosch... или null",
            },
            "device_model": {
                "type": ["string", "null"],
                "description": "модель как можно точнее: iPhone 13, MacBook Air M1... или null",
            },
            "problem": {
                "type": ["string", "null"],
                "description": "суть неисправности одним предложением по-русски или null",
            },
        },
        "required": ["phone", "device_brand", "device_model", "problem"],
        "additionalProperties": False,
    },
}


class AIAnswer(TypedDict):
    reply: str
    confidence: float
    needs_operator: bool


# --- клиент ------------------------------------------------------------------

_client: GatewayAnthropic | None = None
_client_key: tuple[str, str] | None = None


class _Messages:
    """`client.messages` — форма SDK: `await client.messages.create(**kwargs)`."""

    def __init__(self, owner: GatewayAnthropic) -> None:
        self._owner = owner

    async def create(self, **kwargs: Any) -> Any:
        return await self._owner.create(**kwargs)


class GatewayAnthropic:
    """Адаптер под интерфейс `AsyncAnthropic`: один вызов = один поход в
    `POST /llm/anthropic/tool` шлюза, ответ — объект с `.content` из блока
    `tool_use`. Ошибки шлюза (`GatewayError`) выпускает наружу: их ловит
    `_call_tool` и превращает в `None`, как любой отказ SDK."""

    def __init__(self) -> None:
        self.messages = _Messages(self)

    async def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: Any,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        **_: Any,
    ) -> Any:
        # `tool_choice`/`thinking` шлюз собирает сам из инструмента: ровно так
        # же, как раньше собирал `_call_tool`.
        (tool,) = tools
        данные = await gateway.call(
            "/llm/anthropic/tool",
            {
                "model": model,
                "system": system,
                "messages": messages,
                "tool": tool,
                "max_tokens": max_tokens,
                "timeout_sec": GATEWAY_BUDGET_SECONDS,
            },
            timeout=CLIENT_TIMEOUT_SECONDS,
            provider="anthropic",
        )
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=tool["name"], input=данные.get("input"))]
        )


def is_available() -> bool:
    """Есть ли смысл звать модель вообще (ключ на шлюзе либо AI_FAKE).

    Читает песочница: `ai_mode="real"` без ключа обязан честно отдать
    `503 upstream_unavailable` (01 §8.6), а не молча ронять каждый шаг в
    handoff. «Ключ на шлюзе» — по снимку `/status`; пока снимка нет, верим,
    что ключ есть: первый же вызов всё расскажет.
    """
    return bool(settings.ai_fake or (gateway.enabled() and gateway.key_present("anthropic")))


def get_client() -> Any | None:
    """Ленивый адаптер шлюза. `None` — звать некого (шлюз не настроен или на
    нём нет ключа Anthropic).

    Пересобирается при смене адреса/токена шлюза на лету; сам он без
    состояния, кэш — чтобы `get_client() is get_client()` держалось, как у
    клиента SDK.
    """
    global _client, _client_key
    if not (gateway.enabled() and gateway.key_present("anthropic")):
        return None
    key = (settings.gateway_url, settings.gateway_token)
    if _client is not None and _client_key == key:
        return _client
    _client = GatewayAnthropic()
    _client_key = key
    return _client


def reset_client() -> None:
    """Забыть клиент (тесты и смена настроек на лету)."""
    global _client, _client_key
    _client, _client_key = None, None


async def _call_tool(
    *,
    model: str,
    system: Any,
    messages: list[dict[str, Any]],
    tool: dict[str, Any],
    max_tokens: int,
) -> dict[str, Any] | None:
    """Один вызов модели с принудительным tool-use. `None` = недоступна.

    Исключений наружу не выпускает: для движка «модель недоступна» и «модель
    ошиблась» — одна и та же штатная ветка.
    """
    if _breaker_open():
        log.warning("bot.ai_breaker_open", model=model)
        return None
    client = get_client()
    if client is None:
        return None
    try:
        response = await asyncio.wait_for(
            client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
                tools=[tool],
                # Рассуждения выключены осознанно: бюджет вызова 10 секунд, а
                # ответ и так жёстко структурирован схемой инструмента.
                thinking={"type": "disabled"},
                tool_choice={
                    "type": "tool",
                    "name": tool["name"],
                    "disable_parallel_tool_use": True,
                },
            ),
            timeout=AI_TIMEOUT_SECONDS,
        )
    except Exception as exc:  # noqa: BLE001 — любая ошибка = недоступность (02 §3.3)
        _note_failure()
        log.warning("bot.ai_call_failed", model=model, tool=tool["name"], error=type(exc).__name__)
        return None

    block = next(
        (
            b
            for b in getattr(response, "content", None) or []
            if getattr(b, "type", None) == "tool_use" and getattr(b, "name", None) == tool["name"]
        ),
        None,
    )
    if block is None:
        # Аномалия: отказ модели, refusal, пустой ответ. Не ошибка сети, но
        # результата нет — считаем недоступностью и уходим в handoff.
        _note_failure()
        log.warning("bot.ai_no_tool_use", model=model, tool=tool["name"])
        return None
    _note_success()
    data = getattr(block, "input", None)
    return data if isinstance(data, dict) else None


# --- fake-режим (AI_FAKE=1): ни одного сетевого вызова -----------------------

FAKE_REPLY = "Здравствуйте! Ориентировочно ремонт от 1500 ₽, точная цена после диагностики."
FAKE_CONFIDENCE = 0.9
FAKE_NEGATIVE_MARKERS = ("ужасн", "отвратительн", "кошмар", "хамств", "жалоб", "верните деньги")
FAKE_HUMAN_MARKERS = ("оператор", "менеджер", "живой человек", "позовите человека", "мастера")


def _fake_answer(dialog: list[dict[str, Any]]) -> AIAnswer:
    last = ""
    for item in reversed(dialog):
        if isinstance(item, dict) and item.get("role") == "user":
            last = str(item.get("content") or "")
            break
    lowered = last.lower()
    needs_operator = any(marker in lowered for marker in FAKE_NEGATIVE_MARKERS + FAKE_HUMAN_MARKERS)
    return AIAnswer(
        reply="Передаю ваш вопрос мастеру, он ответит в ближайшее время."
        if needs_operator
        else FAKE_REPLY,
        confidence=0.2 if needs_operator else FAKE_CONFIDENCE,
        needs_operator=needs_operator,
    )


def _fake_classification(texts: list[str]) -> dict[str, Any]:
    joined = " ".join(texts).lower()
    negative = any(marker in joined for marker in FAKE_NEGATIVE_MARKERS)
    return {
        "sentiment": "negative" if negative else "neutral",
        "wants_human": any(marker in joined for marker in FAKE_HUMAN_MARKERS),
        "reason": "AI_FAKE: классификация по ключевым словам",
    }


# --- публичный контракт ------------------------------------------------------


async def ai_answer(
    bot: Any,
    dialog: list[dict[str, Any]],
    item_title: str | None,
    *,
    client_name: str = "",
    city: str = "",
    # канальные данные нужны лид-боту, Claude их не использует
    channel: dict[str, str] | None = None,
    conv_key: str = "",
) -> AIAnswer | None:
    """Шаг `ai_answer` (02 §3.2). `None` — вызывающий обязан сделать handoff."""
    if settings.ai_fake:
        return _fake_answer(dialog)
    if not dialog:
        return None
    knowledge_base = (getattr(bot, "knowledge_base", None) or "").strip() or EMPTY_KB
    system = [
        # Каркас стабилен -> prompt-кэш; база знаний вторым блоком (02 §3.1).
        {
            "type": "text",
            "text": AI_ANSWER_SYSTEM_STATIC,
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": AI_ANSWER_KB_TEMPLATE.format(knowledge_base=knowledge_base),
        },
    ]
    messages: list[dict[str, Any]] = []
    # `strip()`, а не просто «непустая строка»: у объявления, приехавшего из
    # Авито с одними пробелами в названии, модель получала бы «по объявлению
    # «   »» — строку, которая честно говорит только о нашей неаккуратности.
    title = (item_title or "").strip()
    _ctx = []
    if (client_name or "").strip():
        _ctx.append(f"клиент {client_name.strip()}")
    if title:
        _ctx.append(f"пишет по объявлению «{title}»")
    if (city or "").strip():
        _ctx.append(f"город {city.strip()}")
    if _ctx:
        messages.append({"role": "user", "content": "[Контекст: " + ", ".join(_ctx) + "]"})
    messages += [
        {"role": str(m.get("role") or "user"), "content": str(m.get("content") or "")}
        for m in dialog
        if isinstance(m, dict) and str(m.get("content") or "").strip()
    ]
    if not messages:
        return None

    data = await _call_tool(
        model=settings.ai_model_answer,
        system=system,
        messages=messages,
        tool=SUBMIT_ANSWER_TOOL,
        max_tokens=settings.ai_max_tokens_answer,
    )
    if data is None:
        return None
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return AIAnswer(
        reply=str(data.get("reply") or "")[:MAX_REPLY_CHARS],
        confidence=max(0.0, min(1.0, confidence)),
        needs_operator=bool(data.get("needs_operator")),
    )


async def classify_message(texts: list[str]) -> dict[str, Any] | None:
    """Классификатор негатива и просьбы позвать человека (02 §3.4), best effort."""
    cleaned = [t.strip() for t in texts if isinstance(t, str) and t.strip()]
    if not cleaned:
        return None
    if settings.ai_fake:
        return _fake_classification(cleaned)
    return await _call_tool(
        model=settings.ai_model_classify,
        system=CLASSIFY_SYSTEM,
        messages=[{"role": "user", "content": "\n---\n".join(cleaned[-CLASSIFY_CONTEXT:])}],
        tool=CLASSIFY_TOOL,
        max_tokens=settings.ai_max_tokens_classify,
    )


async def extract_entities(texts: list[str]) -> dict[str, Any] | None:
    """Эшелон 2 извлечения перед handoff (02 §3.5), best effort.

    `phone` возвращается контрактом инструмента, но движок его игнорирует:
    тексты приходят сюда уже маскированными.
    """
    cleaned = [t.strip() for t in texts if isinstance(t, str) and t.strip()]
    if not cleaned:
        return None
    if settings.ai_fake:
        return {"phone": None, "device_brand": None, "device_model": None, "problem": cleaned[-1]}
    return await _call_tool(
        model=settings.ai_model_classify,
        system=EXTRACT_SYSTEM,
        messages=[{"role": "user", "content": "\n".join(cleaned)[:MAX_EXTRACT_CHARS]}],
        tool=EXTRACT_TOOL,
        max_tokens=settings.ai_max_tokens_classify,
    )
