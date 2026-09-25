"""Читатели адреса (OpenRouter → Groq → Mistral) и Anthropic для ботов —
перенос из tests/unit/test_address_llm_1309.py и test_ahunter_speller_1609.py
LeadChat (16.09). Сеть — respx на адреса точек; SDK Anthropic — фейк.

Что стережёт файл: порядок точек и моделей, разные 429 (аккаунт — стоп точки,
провайдер модели — следующая модель), счёт `attempts` и при отказе тоже, общий
дедлайн, отсутствие ключа как `no_key`, ключи ни в ответах, ни в логах.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
import respx
import structlog

from leadchat_gateway.config import settings
from leadchat_gateway.providers import anthropic as anthropic_provider
from leadchat_gateway.providers import llm

pytestmark = pytest.mark.anyio

OR = "https://or.test/api/v1/chat/completions"
GROQ = "https://groq.test/openai/v1/chat/completions"
MISTRAL = "https://mistral.test/v1/chat/completions"


def _ответ_модели(**поля: Any) -> httpx.Response:
    content = json.dumps({"street": "ул Ленина", "house": "5", "confidence": "high", **поля})
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


@pytest.fixture
def openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", "k")
    monkeypatch.setattr(settings, "openrouter_base_url", "https://or.test/api/v1")
    monkeypatch.setattr(settings, "openrouter_address_models", "a/one:free,b/two:free")


# --- parse_content и endpoints -------------------------------------------------


def test_ответ_модели_разбирается_из_ограды_и_болтовни() -> None:
    assert llm.parse_content('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_content('Вот ответ: {"a": 1} — готово') == {"a": 1}
    with pytest.raises(ValueError):
        llm.parse_content("никакого json")
    with pytest.raises(ValueError):
        llm.parse_content("[1, 2]")


def test_точки_только_с_ключом_и_умолчания(monkeypatch: pytest.MonkeyPatch) -> None:
    assert llm.endpoints() == []
    monkeypatch.setattr(settings, "mistral_api_key", "k-m")
    monkeypatch.setattr(settings, "mistral_base_url", "")  # пусто в .env — умолчание
    (точка,) = llm.endpoints()
    assert точка.name == "mistral"
    assert точка.base_url == "https://api.mistral.ai/v1"
    assert точка.models == llm.DEFAULT_MISTRAL_MODELS
    monkeypatch.setattr(settings, "groq_api_key", "k-g")
    monkeypatch.setattr(settings, "groq_address_models", " x/one , y/two ")
    assert [т.name for т in llm.endpoints()] == ["groq", "mistral"]
    assert llm.endpoints()[0].models == ("x/one", "y/two")


# --- /llm/chat -----------------------------------------------------------------


@respx.mock
async def test_модели_по_очереди_и_attempts(gw: httpx.AsyncClient, openrouter: None) -> None:
    запросы: list[str] = []

    def обработчик(request: httpx.Request) -> httpx.Response:
        тело = json.loads(request.content)
        запросы.append(тело["model"])
        assert request.headers["Authorization"] == "Bearer k"
        assert тело["messages"] == [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
        ]
        assert тело["response_format"] == {"type": "json_object"}
        if тело["model"] == "a/one:free":
            return httpx.Response(503)
        return _ответ_модели()

    respx.post(OR).mock(side_effect=обработчик)
    r = await gw.post("/llm/chat", json={"system": "s", "user": "u"})
    assert r.status_code == 200
    данные = r.json()
    assert данные["ok"] is True
    assert данные["content"]["street"] == "ул Ленина"
    assert данные["model"] == "b/two:free"
    assert данные["attempts"] == 2
    assert запросы == ["a/one:free", "b/two:free"]
    assert "Bearer k" not in r.text and '"k"' not in r.text


@respx.mock
async def test_все_лежат_это_exhausted_с_attempts(gw: httpx.AsyncClient, openrouter: None) -> None:
    respx.post(OR).mock(return_value=httpx.Response(503))
    r = await gw.post("/llm/chat", json={"system": "s", "user": "u"})
    assert r.status_code == 200
    assert r.json() == {
        "ok": False,
        "kind": "exhausted",
        "status": 503,
        "detail": "все модели точки отказали",
        "attempts": 2,
    }


@respx.mock
async def test_429_аккаунта_стоп_один_запрос(gw: httpx.AsyncClient, openrouter: None) -> None:
    маршрут = respx.post(OR).mock(
        return_value=httpx.Response(
            429, json={"error": {"message": "Rate limit exceeded: free-models-per-day"}}
        )
    )
    r = await gw.post("/llm/chat", json={"system": "s", "user": "u"})
    данные = r.json()
    assert (данные["ok"], данные["kind"], данные["status"]) == (False, "exhausted", 429)
    assert данные["attempts"] == 1 and маршрут.call_count == 1


@respx.mock
async def test_429_провайдера_и_пустой_объект_это_следующая_модель(
    gw: httpx.AsyncClient, openrouter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openrouter_address_models", "a/one:free,b/two:free,c/three:free")
    запросы: list[str] = []

    def провайдер(request: httpx.Request) -> httpx.Response:
        модель = json.loads(request.content)["model"]
        запросы.append(модель)
        if модель == "a/one:free":
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": "Provider returned error",
                        "code": 429,
                        "metadata": {"raw": "a/one:free is temporarily rate-limited upstream"},
                    }
                },
            )
        return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})

    respx.post(OR).mock(side_effect=провайдер)
    r = await gw.post("/llm/chat", json={"system": "s", "user": "u"})
    данные = r.json()
    assert данные["ok"] is False and данные["kind"] == "exhausted"
    assert запросы == ["a/one:free", "b/two:free", "c/three:free"]
    assert данные["attempts"] == 3


async def test_без_ключей_это_no_key(gw: httpx.AsyncClient) -> None:
    r = await gw.post("/llm/chat", json={"system": "s", "user": "u"})
    assert r.status_code == 200
    данные = r.json()
    assert (данные["ok"], данные["kind"], данные["attempts"]) == (False, "no_key", 0)


@respx.mock
async def test_читатели_по_очереди_openrouter_groq_mistral(
    gw: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "openrouter_api_key", "k-or")
    monkeypatch.setattr(settings, "openrouter_base_url", "https://or.test/api/v1")
    monkeypatch.setattr(settings, "openrouter_address_models", "a/one:free")
    monkeypatch.setattr(settings, "groq_api_key", "k-groq")
    monkeypatch.setattr(settings, "groq_base_url", "https://groq.test/openai/v1")
    monkeypatch.setattr(settings, "groq_address_models", "")
    monkeypatch.setattr(settings, "mistral_api_key", "k-mistral")
    monkeypatch.setattr(settings, "mistral_base_url", "https://mistral.test/v1")
    monkeypatch.setattr(settings, "mistral_address_models", "mistral-small-latest")
    assert [т.name for т in llm.endpoints()] == ["openrouter", "groq", "mistral"]
    запросы: list[tuple[str, str, str]] = []

    def обработчик(request: httpx.Request) -> httpx.Response:
        модель = json.loads(request.content)["model"]
        запросы.append((request.url.host, модель, request.headers["Authorization"]))
        if request.url.host == "or.test":
            # Суточный потолок аккаунта OpenRouter — к следующему читателю.
            return httpx.Response(429, json={"error": {"message": "free-models-per-day"}})
        if request.url.host == "groq.test":
            return httpx.Response(403)  # закрыт — не приговор Mistral
        return _ответ_модели()

    respx.post(OR).mock(side_effect=обработчик)
    respx.post(GROQ).mock(side_effect=обработчик)
    respx.post(MISTRAL).mock(side_effect=обработчик)
    with structlog.testing.capture_logs() as логи:
        r = await gw.post("/llm/chat", json={"system": "s", "user": "u"})
    данные = r.json()
    assert данные["ok"] is True
    assert данные["content"]["street"] == "ул Ленина"
    assert данные["model"] == "mistral:mistral-small-latest"
    assert данные["attempts"] == 3
    assert запросы == [
        ("or.test", "a/one:free", "Bearer k-or"),
        ("groq.test", "openai/gpt-oss-120b", "Bearer k-groq"),
        ("mistral.test", "mistral-small-latest", "Bearer k-mistral"),
    ]
    # Отказы точек в логе — с kind, без ключей и без промпта.
    отказы = [(л["endpoint"], л["kind"]) for л in логи if л["event"] == "llm.endpoint_failed"]
    assert отказы == [("openrouter", "exhausted"), ("groq", "blocked")]
    текст_логов = json.dumps(логи, ensure_ascii=False)
    assert "k-or" not in текст_логов and "k-groq" not in текст_логов

    # Только Mistral с ключом — OpenRouter не спрашивается, имя точки в ответе.
    monkeypatch.setattr(settings, "openrouter_api_key", "")
    monkeypatch.setattr(settings, "groq_api_key", "")
    запросы.clear()
    r = await gw.post("/llm/chat", json={"system": "s", "user": "u"})
    assert r.json()["model"] == "mistral:mistral-small-latest" and len(запросы) == 1

    # Все точки отвергли ключ — kind последней: blocked со статусом.
    monkeypatch.setattr(settings, "groq_api_key", "k-groq")
    respx.post(GROQ).mock(return_value=httpx.Response(401))
    respx.post(MISTRAL).mock(return_value=httpx.Response(403))
    r = await gw.post("/llm/chat", json={"system": "s", "user": "u"})
    assert (r.json()["kind"], r.json()["status"], r.json()["attempts"]) == ("blocked", 403, 2)


@respx.mock
async def test_обрыв_сети_это_следующая_модель_и_network(
    gw: httpx.AsyncClient, openrouter: None
) -> None:
    respx.post(OR).mock(side_effect=httpx.ConnectTimeout("boom"))
    with structlog.testing.capture_logs() as логи:
        r = await gw.post("/llm/chat", json={"system": "s", "user": "u"})
    данные = r.json()
    assert (данные["ok"], данные["kind"], данные["attempts"]) == (False, "exhausted", 2)
    сетевые = [л for л in логи if л["event"] == "llm.network"]
    assert [л["error"] for л in сетевые] == ["ConnectTimeout", "ConnectTimeout"]
    assert "or.test" not in json.dumps(логи)


@respx.mock
async def test_дедлайн_не_даёт_начать_следующий_запрос(
    gw: httpx.AsyncClient, openrouter: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Часы:
        """Каждый взгляд на часы — плюс секунда: дедлайн наступает без сна."""

        t = 0.0

        def monotonic(self) -> float:
            self.t += 1.0
            return self.t

    monkeypatch.setattr(llm, "time", Часы())
    monkeypatch.setattr(llm, "_ЗАПАС_СЕК", 0.0)
    monkeypatch.setattr(settings, "groq_api_key", "k-groq")
    monkeypatch.setattr(settings, "groq_base_url", "https://groq.test/openai/v1")
    маршрут = respx.post(OR).mock(return_value=httpx.Response(503))
    groq = respx.post(GROQ).mock(return_value=_ответ_модели())
    with structlog.testing.capture_logs() as логи:
        r = await gw.post("/llm/chat", json={"system": "s", "user": "u", "deadline_sec": 2.5})
    данные = r.json()
    # Первая модель OpenRouter спрошена, вторая — уже за дедлайном; до Groq,
    # который ответил бы, очередь не дошла.
    assert (данные["ok"], данные["kind"], данные["attempts"]) == (False, "exhausted", 1)
    assert маршрут.call_count == 1 and groq.call_count == 0
    assert [л["endpoint"] for л in логи if л["event"] == "llm.deadline"] == ["groq"]
    # Дедлайн меньше запаса на запрос — ни одного похода.
    monkeypatch.setattr(llm, "_ЗАПАС_СЕК", 75.0)
    r = await gw.post("/llm/chat", json={"system": "s", "user": "u", "deadline_sec": 1})
    assert r.json()["attempts"] == 0 and r.json()["kind"] == "exhausted"
    assert маршрут.call_count == 1


async def test_кривой_запрос_это_422(gw: httpx.AsyncClient) -> None:
    assert (await gw.post("/llm/chat", json={"system": "s"})).status_code == 422
    assert (
        await gw.post("/llm/chat", json={"system": "s", "user": "u", "deadline_sec": 0})
    ).status_code == 422


# --- /llm/anthropic/tool -------------------------------------------------------


class Block:
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
    def __init__(self, result: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.messages = FakeMessages(result)


TOOL = {
    "name": "submit_answer",
    "description": "ответ",
    "input_schema": {"type": "object", "properties": {}},
    "strict": True,
}


@pytest.fixture
def sdk(monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Подменить сборку SDK-клиента фейком; возвращает фабрику с результатом."""
    anthropic_provider.reset_client()
    собранные: list[FakeClient] = []

    def _wire(result: Any) -> list[FakeClient]:
        def build(api_key: str, base_url: str, timeout_sec: float) -> FakeClient:
            client = FakeClient(result, api_key=api_key, base_url=base_url, timeout=timeout_sec)
            собранные.append(client)
            return client

        monkeypatch.setattr(anthropic_provider, "_build_client", build)
        return собранные

    yield _wire
    anthropic_provider.reset_client()


def _тело(**over: Any) -> dict[str, Any]:
    тело = {
        "model": "claude-haiku-4-5",
        "system": [{"type": "text", "text": "ты бот", "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": "разбит экран"}],
        "tool": TOOL,
        "max_tokens": 256,
        "timeout_sec": 12.0,
    }
    тело.update(over)
    return тело


async def test_блок_tool_use_становится_input(
    gw: httpx.AsyncClient, sdk, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-СЕКРЕТ")
    monkeypatch.setattr(settings, "anthropic_base_url", "https://api.anthropic.test")
    клиенты = sdk(Response(Block("text"), Block("tool_use", "submit_answer", {"reply": "ок"})))
    with structlog.testing.capture_logs() as логи:
        r = await gw.post("/llm/anthropic/tool", json=_тело())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "input": {"reply": "ок"}}
    (клиент,) = клиенты
    # Клиент SDK: ключ, адрес, таймаут чуть меньше стены, без авторетраев.
    assert клиент.kwargs == {
        "api_key": "sk-СЕКРЕТ",
        "base_url": "https://api.anthropic.test",
        "timeout": 12.0,
    }
    (вызов,) = клиент.messages.calls
    assert вызов["model"] == "claude-haiku-4-5"
    assert вызов["max_tokens"] == 256
    assert вызов["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert вызов["messages"] == [{"role": "user", "content": "разбит экран"}]
    assert вызов["tools"] == [TOOL]
    assert вызов["thinking"] == {"type": "disabled"}
    assert вызов["tool_choice"] == {
        "type": "tool",
        "name": "submit_answer",
        "disable_parallel_tool_use": True,
    }
    assert "СЕКРЕТ" not in r.text and "СЕКРЕТ" not in json.dumps(логи, ensure_ascii=False)
    # Второй вызов — тот же клиент, а не новый пул соединений.
    await gw.post("/llm/anthropic/tool", json=_тело())
    assert len(клиенты) == 1 and len(клиент.messages.calls) == 2


def test_таймаут_sdk_меньше_стены(monkeypatch: pytest.MonkeyPatch) -> None:
    собранные: dict[str, Any] = {}

    class Recorder:
        def __init__(self, **kwargs: Any) -> None:
            собранные.update(kwargs)

    monkeypatch.setitem(
        __import__("sys").modules, "anthropic", type("M", (), {"AsyncAnthropic": Recorder})
    )
    anthropic_provider._build_client("sk", "https://a.test", 15.0)
    assert собранные == {
        "api_key": "sk",
        "base_url": "https://a.test",
        "timeout": 14.0,
        "max_retries": 0,
    }


async def test_без_ключа_это_no_key_и_sdk_не_трогается(gw: httpx.AsyncClient, sdk) -> None:  # noqa: ANN001
    клиенты = sdk(Response())
    r = await gw.post("/llm/anthropic/tool", json=_тело())
    assert r.json()["ok"] is False and r.json()["kind"] == "no_key"
    assert клиенты == []


async def test_отказ_модели_и_сети(
    gw: httpx.AsyncClient, sdk, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    import anthropic

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")
    # Нет блока tool_use — bad_response.
    sdk(Response(Block("text")))
    r = await gw.post("/llm/anthropic/tool", json=_тело())
    assert (r.json()["kind"], r.json()["status"]) == ("bad_response", None)
    # Чужое имя инструмента — тоже.
    anthropic_provider.reset_client()
    sdk(Response(Block("tool_use", "other", {"x": 1})))
    assert (await gw.post("/llm/anthropic/tool", json=_тело())).json()["kind"] == "bad_response"
    # Ключ отвергнут — blocked со статусом SDK.
    anthropic_provider.reset_client()
    запрос = httpx.Request("POST", "https://api.anthropic.test/v1/messages")
    sdk(
        anthropic.AuthenticationError(
            "invalid x-api-key", response=httpx.Response(401, request=запрос), body=None
        )
    )
    r = await gw.post("/llm/anthropic/tool", json=_тело())
    assert (r.json()["kind"], r.json()["status"], r.json()["detail"]) == (
        "blocked",
        401,
        "AuthenticationError",
    )
    # Любая другая ошибка SDK — network с именем класса и статусом, если был.
    anthropic_provider.reset_client()
    sdk(
        anthropic.RateLimitError(
            "slow down", response=httpx.Response(429, request=запрос), body=None
        )
    )
    r = await gw.post("/llm/anthropic/tool", json=_тело())
    assert (r.json()["kind"], r.json()["status"], r.json()["detail"]) == (
        "network",
        429,
        "RateLimitError",
    )
    anthropic_provider.reset_client()
    sdk(ConnectionError("прокси недоступен"))
    r = await gw.post("/llm/anthropic/tool", json=_тело())
    assert (r.json()["kind"], r.json()["status"], r.json()["detail"]) == (
        "network",
        None,
        "ConnectionError",
    )
    assert "прокси" not in r.text


async def test_стена_времени_держится(
    gw: httpx.AsyncClient, sdk, monkeypatch: pytest.MonkeyPatch
) -> None:  # noqa: ANN001
    import asyncio

    monkeypatch.setattr(settings, "anthropic_api_key", "sk-test")

    async def вечность(**kwargs: Any) -> Any:
        await asyncio.sleep(5)

    sdk(вечность)
    r = await gw.post("/llm/anthropic/tool", json=_тело(timeout_sec=0.05))
    assert (r.json()["kind"], r.json()["detail"]) == ("network", "TimeoutError")
