"""app/bots/ai.py — контракт AI-подсистемы (02 §3).

Главный инвариант всего файла: **ни один тест не ходит в сеть**. Клиент
подменяется фейком (или respx на адрес шлюза `http://gw.test`, docs/46), а
режим `AI_FAKE=1` проверяется отдельно — там сетевого клиента не должно быть
даже создано.

Второй инвариант: `None` — штатный ответ, а не исключение. Таймаут, 429,
отсутствие ключа, отказ модели — движок обязан получить одинаковый `None` и
уйти в `handoff(ai_unavailable)` (02 §3.3).
"""

import asyncio
import json
from typing import Any

import httpx
import pytest
import respx

from app.bots import ai as ai_mod
from app.core.config import settings
from app.integrations import gateway

GW_TOOL = "http://gw.test/llm/anthropic/tool"

# --- инфраструктура ----------------------------------------------------------


class Block:
    """Кадр ответа Claude: tool_use или что-то ещё."""

    def __init__(self, type_: str, name: str | None = None, data: Any = None) -> None:
        self.type = type_
        self.name = name
        self.input = data


class Response:
    def __init__(self, *blocks: Block) -> None:
        self.content = list(blocks)


class FakeMessages:
    """`client.messages` из SDK: пишет полученные kwargs, отдаёт заготовку."""

    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, BaseException):
            raise self.result
        if callable(self.result):
            return await self.result(**kwargs)
        return self.result


class FakeClient:
    def __init__(self, result: Any) -> None:
        self.messages = FakeMessages(result)


@pytest.fixture(autouse=True)
def clean_ai(monkeypatch, _шлюз_в_тестах):
    """Каждый тест начинается с закрытого предохранителя и без клиента; ключ
    Anthropic «есть на шлюзе» (снимок `/status`, docs/46)."""
    monkeypatch.setattr(settings, "ai_fake", False, raising=False)
    monkeypatch.setitem(gateway.known_keys, "anthropic", True)
    ai_mod.reset_client()
    ai_mod.reset_breaker()
    yield
    ai_mod.reset_client()
    ai_mod.reset_breaker()


@pytest.fixture
def wire(monkeypatch):
    """Подменить клиента заготовкой; возвращает сам фейк для ассертов."""

    def _wire(result: Any) -> FakeClient:
        client = FakeClient(result)
        monkeypatch.setattr(ai_mod, "get_client", lambda: client)
        return client

    return _wire


def answer_block(**over: Any) -> Response:
    data = {"reply": "Ориентировочно от 1500 ₽.", "confidence": 0.82, "needs_operator": False}
    data.update(over)
    return Response(Block("tool_use", "submit_answer", data))


class Bot:
    def __init__(self, knowledge_base: str | None = "Замена экрана iPhone 13 — от 5900 ₽.") -> None:
        self.id = "bot-1"
        self.knowledge_base = knowledge_base


DIALOG = [
    {"role": "user", "content": "Здравствуйте, разбит экран iPhone 13"},
    {"role": "assistant", "content": "Здравствуйте! Уточню у мастера."},
]


# --- AI_FAKE: ни одного сетевого вызова --------------------------------------


async def test_fake_mode_never_builds_a_client(monkeypatch):
    """AI_FAKE=1 — контракт тот же, сети нет. Так работает CI (07 §1.1)."""
    monkeypatch.setattr(settings, "ai_fake", True, raising=False)
    monkeypatch.setitem(gateway.known_keys, "anthropic", False)

    def boom() -> Any:  # pragma: no cover — вызов = провал теста
        raise AssertionError("AI_FAKE не имеет права трогать сеть")

    monkeypatch.setattr(ai_mod, "get_client", boom)

    answer = await ai_mod.ai_answer(Bot(), DIALOG, "Ремонт iPhone 13")
    assert answer is not None
    assert set(answer) == {"reply", "confidence", "needs_operator"}
    assert answer["needs_operator"] is False

    classification = await ai_mod.classify_message(["Здравствуйте"])
    assert classification == {
        "sentiment": "neutral",
        "wants_human": False,
        "reason": "AI_FAKE: классификация по ключевым словам",
    }
    assert (await ai_mod.extract_entities(["разбит экран"]))["problem"] == "разбит экран"


async def test_fake_mode_is_deterministic_on_negative_and_human_requests(monkeypatch):
    monkeypatch.setattr(settings, "ai_fake", True, raising=False)

    angry = await ai_mod.classify_message(["Ужасный сервис, верните деньги"])
    assert angry == {
        "sentiment": "negative",
        "wants_human": False,
        "reason": "AI_FAKE: классификация по ключевым словам",
    }
    assert (await ai_mod.classify_message(["позовите оператора"]))["wants_human"] is True

    handoff = await ai_mod.ai_answer(
        Bot(), [{"role": "user", "content": "позовите оператора"}], None
    )
    assert handoff is not None
    assert handoff["needs_operator"] is True
    assert handoff["confidence"] == 0.2


async def test_fake_mode_is_available_without_a_key(monkeypatch):
    monkeypatch.setitem(gateway.known_keys, "anthropic", False)
    monkeypatch.setattr(settings, "ai_fake", False, raising=False)
    assert ai_mod.is_available() is False
    monkeypatch.setattr(settings, "ai_fake", True, raising=False)
    assert ai_mod.is_available() is True
    # Шлюз не настроен — модели нет, кроме как в AI_FAKE.
    monkeypatch.setitem(gateway.known_keys, "anthropic", True)
    monkeypatch.setattr(settings, "gateway_url", "", raising=False)
    assert ai_mod.is_available() is True
    monkeypatch.setattr(settings, "ai_fake", False, raising=False)
    assert ai_mod.is_available() is False


# --- клиент: адаптер шлюза (docs/46) ------------------------------------------


async def test_client_is_none_without_a_key(monkeypatch):
    """Нет ключа на шлюзе -> звать некого; для движка это «модель недоступна»."""
    monkeypatch.setitem(gateway.known_keys, "anthropic", False)
    assert ai_mod.get_client() is None
    assert await ai_mod.ai_answer(Bot(), DIALOG, None) is None
    assert await ai_mod.classify_message(["текст"]) is None
    assert await ai_mod.extract_entities(["текст"]) is None
    # Шлюз не настроен — тоже некого.
    monkeypatch.setitem(gateway.known_keys, "anthropic", True)
    monkeypatch.setattr(settings, "gateway_token", "", raising=False)
    assert ai_mod.get_client() is None


@respx.mock
async def test_client_calls_the_gateway_tool_endpoint():
    """Ключ и SDK Anthropic живут на шлюзе; отсюда уходит один POST с
    инструментом, а ответ шлюза становится блоком `tool_use` для `_call_tool`."""
    ответ = {"reply": "Ориентировочно от 1500 ₽.", "confidence": 0.7, "needs_operator": False}
    маршрут = respx.post(GW_TOOL).mock(
        return_value=httpx.Response(200, json={"ok": True, "input": ответ})
    )
    client = ai_mod.get_client()
    assert client is not None
    answer = await ai_mod.ai_answer(Bot("Диагностика бесплатная"), DIALOG, "Ремонт MacBook")
    assert answer == {
        "reply": "Ориентировочно от 1500 ₽.",
        "confidence": 0.7,
        "needs_operator": False,
    }
    запрос = маршрут.calls[0].request
    assert запрос.headers["Authorization"] == "Bearer test-token"
    тело = json.loads(запрос.content)
    assert set(тело) == {"model", "system", "messages", "tool", "max_tokens", "timeout_sec"}
    assert тело["model"] == settings.ai_model_answer
    assert тело["max_tokens"] == settings.ai_max_tokens_answer
    assert тело["tool"]["name"] == "submit_answer" and тело["tool"]["strict"] is True
    assert тело["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Диагностика бесплатная" in тело["system"][1]["text"]
    assert "Ремонт MacBook" in тело["messages"][0]["content"]
    # Бюджет шлюза меньше нашего таймаута похода, тот — меньше стены `wait_for`:
    # ответ шлюза успевает доехать раньше, чем мы бросим ждать.
    assert тело["timeout_sec"] < ai_mod.CLIENT_TIMEOUT_SECONDS < ai_mod.AI_TIMEOUT_SECONDS
    # Второй вызов переиспользует адаптер, а не создаёт новый.
    assert ai_mod.get_client() is client


def test_client_is_rebuilt_when_settings_change(monkeypatch):
    first = ai_mod.get_client()
    assert first is not None
    monkeypatch.setattr(settings, "gateway_url", "http://other.test", raising=False)
    assert ai_mod.get_client() is not first


@respx.mock
async def test_gateway_refusal_is_the_same_as_unavailable():
    """Шлюз лёг, не принял токен, провайдер отказал или ключа на шлюзе нет —
    для движка это одинаковый `None`, и предохранитель считает отказ."""
    respx.post(GW_TOOL).mock(side_effect=httpx.ConnectError("boom"))
    assert await ai_mod.classify_message(["текст"]) is None
    respx.post(GW_TOOL).mock(return_value=httpx.Response(401, json={"detail": "x"}))
    assert await ai_mod.classify_message(["текст"]) is None
    respx.post(GW_TOOL).mock(
        return_value=httpx.Response(
            200, json={"ok": False, "kind": "network", "status": 529, "detail": "overloaded"}
        )
    )
    assert await ai_mod.classify_message(["текст"]) is None
    assert ai_mod._fail_count == 3
    # Ключа на шлюзе нет: снимок помечается, дальше модель не зовётся вовсе.
    маршрут = respx.post(GW_TOOL).mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "no_key", "status": None})
    )
    assert ai_mod.is_available() is True
    assert await ai_mod.classify_message(["текст"]) is None
    assert ai_mod.is_available() is False and ai_mod.get_client() is None
    походов = маршрут.call_count
    assert await ai_mod.extract_entities(["текст"]) is None
    assert маршрут.call_count == походов


# --- ai_answer ---------------------------------------------------------------


async def test_ai_answer_parses_the_tool_call(wire):
    client = wire(answer_block())
    answer = await ai_mod.ai_answer(Bot(), DIALOG, "Ремонт iPhone 13")

    assert answer == {
        "reply": "Ориентировочно от 1500 ₽.",
        "confidence": 0.82,
        "needs_operator": False,
    }
    call = client.messages.calls[0]
    assert call["model"] == settings.ai_model_answer
    # Структура гарантируется инструментом, а не парсингом текста (02 §3.2).
    assert call["tool_choice"] == {
        "type": "tool",
        "name": "submit_answer",
        "disable_parallel_tool_use": True,
    }
    assert call["tools"][0]["strict"] is True
    assert call["thinking"] == {"type": "disabled"}


async def test_ai_answer_sends_the_knowledge_base_and_item_context(wire):
    client = wire(answer_block())
    await ai_mod.ai_answer(Bot("Диагностика бесплатная"), DIALOG, "Ремонт MacBook")
    call = client.messages.calls[0]

    # Каркас кэшируется, база знаний — вторым блоком (02 §3.1).
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Диагностика бесплатная" in call["system"][1]["text"]
    assert "Ремонт MacBook" in call["messages"][0]["content"]
    assert [m["content"] for m in call["messages"][1:]] == [m["content"] for m in DIALOG]


async def test_ai_answer_skips_a_blank_item_title(wire):
    """Объявление из одних пробелов — это «объявления нет», а не «по «   »»."""
    client = wire(answer_block())
    await ai_mod.ai_answer(Bot(), DIALOG, "   ")
    messages = client.messages.calls[0]["messages"]

    assert not any("объявлению" in str(m["content"]) for m in messages)
    assert [m["content"] for m in messages] == [m["content"] for m in DIALOG]


async def test_ai_answer_marks_an_empty_knowledge_base(wire):
    client = wire(answer_block())
    await ai_mod.ai_answer(Bot(knowledge_base="   "), DIALOG, None)
    assert ai_mod.EMPTY_KB in client.messages.calls[0]["system"][1]["text"]


async def test_ai_answer_clamps_confidence_and_cuts_the_reply(wire):
    wire(answer_block(confidence=7.5, reply="я" * 5000))
    answer = await ai_mod.ai_answer(Bot(), DIALOG, None)
    assert answer is not None
    assert answer["confidence"] == 1.0
    assert len(answer["reply"]) == ai_mod.MAX_REPLY_CHARS

    ai_mod.reset_breaker()
    wire(answer_block(confidence="не число"))
    fallback = await ai_mod.ai_answer(Bot(), DIALOG, None)
    assert fallback is not None
    assert fallback["confidence"] == 0.0


async def test_ai_answer_without_dialog_does_not_call_the_model(wire):
    client = wire(answer_block())
    assert await ai_mod.ai_answer(Bot(), [], None) is None
    assert await ai_mod.ai_answer(Bot(), [{"role": "user", "content": "   "}], None) is None
    assert client.messages.calls == []


# --- недоступность: всё это один и тот же `None` ------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        TimeoutError("timeout"),
        RuntimeError("429 rate limit"),
        ConnectionError("proxy is down"),
    ],
    ids=["timeout", "rate_limit", "proxy_down"],
)
async def test_any_failure_becomes_none(wire, failure):
    """02 §3.3: движок обязан увидеть одинаковый None и сделать handoff."""
    wire(failure)
    assert await ai_mod.ai_answer(Bot(), DIALOG, None) is None
    ai_mod.reset_breaker()
    assert await ai_mod.classify_message(["текст"]) is None


async def test_wall_clock_budget_is_enforced(wire, monkeypatch):
    """Медленная модель не держит тик дольше AI_TIMEOUT_SECONDS."""
    monkeypatch.setattr(ai_mod, "AI_TIMEOUT_SECONDS", 0.05)

    async def slow(**_: Any) -> Any:  # pragma: no cover — прерывается таймаутом
        await asyncio.sleep(5)
        return answer_block()

    wire(slow)
    assert await ai_mod.ai_answer(Bot(), DIALOG, None) is None


async def test_response_without_tool_use_is_unavailable(wire):
    """Отказ модели/пустой ответ — тоже недоступность, а не пустой reply."""
    wire(Response(Block("text")))
    assert await ai_mod.ai_answer(Bot(), DIALOG, None) is None
    ai_mod.reset_breaker()
    wire(Response(Block("tool_use", "submit_answer", "не словарь")))
    assert await ai_mod.ai_answer(Bot(), DIALOG, None) is None


# --- предохранитель (02 §3.3) ------------------------------------------------


async def test_breaker_stops_calling_after_repeated_failures(wire):
    client = wire(RuntimeError("529 overloaded"))
    for _ in range(ai_mod.BREAKER_THRESHOLD):
        assert await ai_mod.classify_message(["текст"]) is None
    calls_before = len(client.messages.calls)

    assert await ai_mod.classify_message(["текст"]) is None
    # Провайдер лежит — API больше не расстреливаем, сразу отдаём None.
    assert len(client.messages.calls) == calls_before


async def test_breaker_is_reset_by_a_success(wire):
    wire(RuntimeError("529"))
    for _ in range(ai_mod.BREAKER_THRESHOLD - 1):
        await ai_mod.classify_message(["текст"])

    client = wire(Response(Block("tool_use", "classify", {"sentiment": "neutral"})))
    assert await ai_mod.classify_message(["текст"]) == {"sentiment": "neutral"}
    # После успеха окно чистое: следующий вызов снова доходит до API.
    assert await ai_mod.classify_message(["ещё"]) is not None
    assert len(client.messages.calls) == 2


def test_breaker_window_expires():
    """Окно 5 минут: старые отказы не держат предохранитель вечно."""
    ai_mod.reset_breaker()
    for _ in range(ai_mod.BREAKER_THRESHOLD):
        ai_mod._note_failure(now=1000.0)
    assert ai_mod._breaker_open(now=1000.0) is True
    assert ai_mod._breaker_open(now=1000.0 + ai_mod.BREAKER_WINDOW_SECONDS + 1) is False


# --- classify_message / extract_entities -------------------------------------


async def test_classify_sends_only_the_last_messages(wire):
    client = wire(Response(Block("tool_use", "classify", {"sentiment": "negative"})))
    texts = [f"сообщение {i}" for i in range(10)]
    assert await ai_mod.classify_message(texts) == {"sentiment": "negative"}

    call = client.messages.calls[0]
    assert call["model"] == settings.ai_model_classify
    sent = call["messages"][0]["content"]
    assert "сообщение 9" in sent and "сообщение 5" not in sent


async def test_classify_ignores_empty_input(wire):
    client = wire(Response(Block("tool_use", "classify", {})))
    assert await ai_mod.classify_message([]) is None
    assert await ai_mod.classify_message(["   ", None]) is None  # type: ignore[list-item]
    assert client.messages.calls == []


async def test_extract_returns_entities_and_truncates_input(wire):
    client = wire(
        Response(
            Block(
                "tool_use",
                "extract",
                {
                    "phone": "+79990000000",
                    "device_brand": "Apple",
                    "device_model": "iPhone 13",
                    "problem": "разбит экран",
                },
            )
        )
    )
    result = await ai_mod.extract_entities(["а" * 9000, "разбит экран"])
    assert result is not None
    assert result["device_model"] == "iPhone 13"
    assert len(client.messages.calls[0]["messages"][0]["content"]) == ai_mod.MAX_EXTRACT_CHARS


async def test_extract_ignores_empty_input(wire):
    client = wire(Response(Block("tool_use", "extract", {})))
    assert await ai_mod.extract_entities([]) is None
    assert client.messages.calls == []


# --- контракт, на который завязан движок -------------------------------------


def test_the_suite_runs_in_fake_mode_by_default(monkeypatch):
    """07 §1.1: прогон тестов не имеет права пойти в сеть.

    `tests/conftest.py` выставляет AI_FAKE=1 до импорта `app.*`; тесты этого
    файла снимают флаг точечно и подменяют клиента фейком.
    """
    monkeypatch.undo()  # снять точечные подмены фикстуры clean_ai
    from app.core.config import Settings

    assert Settings().ai_fake is True  # type: ignore[call-arg]


def test_module_satisfies_the_engine_protocol():
    """`ScenarioEngine` импортирует модуль и зовёт эти три функции по имени."""
    from app.bots.engine import AIBackend

    assert isinstance(ai_mod, AIBackend)
    assert callable(ai_mod.extract_entities)
    assert callable(ai_mod.is_available)
