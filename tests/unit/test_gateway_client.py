"""Клиент шлюза внешних API (docs/46): один поход, единая форма ответа,
снимок ключей, отказ шлюза ≠ отказ провайдера, ничего лишнего в логах."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import structlog

from app.core.config import settings
from app.integrations import gateway

pytestmark = pytest.mark.anyio

GW = "http://gw.test"


def test_включён_только_с_адресом_и_токеном(monkeypatch) -> None:  # noqa: ANN001
    assert gateway.enabled()
    monkeypatch.setattr(settings, "gateway_token", "")
    assert not gateway.enabled()
    monkeypatch.setattr(settings, "gateway_token", "t")
    monkeypatch.setattr(settings, "gateway_url", "  ")
    assert not gateway.enabled()


def test_ключ_считается_по_снимку(monkeypatch) -> None:  # noqa: ANN001
    # Снимка нет — верим, что ключ есть; первый поход покажет.
    monkeypatch.setattr(gateway, "known_keys", None)
    assert gateway.key_present("dadata")
    monkeypatch.setattr(gateway, "known_keys", {"dadata": False, "nominatim": None})
    assert not gateway.key_present("dadata")
    assert gateway.key_present("nominatim")  # ключ не нужен
    # Снимок есть, провайдера в нём нет — ключа нет: `/status` перечисляет всех.
    assert not gateway.key_present("speller")
    monkeypatch.setattr(gateway, "known_keys", {})
    assert not gateway.key_present("dadata")


@respx.mock
async def test_поход_удача_и_отказ_провайдера() -> None:
    gateway.known_keys = {"yandex_geocoder": True}  # фикстура вернёт пустой снимок
    маршрут = respx.post(f"{GW}/geo/yandex").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "hits": [{"lat": 1.0}]}),
            httpx.Response(
                200, json={"ok": False, "kind": "blocked", "status": 403, "detail": "доступ закрыт"}
            ),
        ]
    )
    данные = await gateway.call("/geo/yandex", {"free_text": "x"}, provider="yandex_geocoder")
    assert данные["hits"] == [{"lat": 1.0}]
    запрос = маршрут.calls[0].request
    assert запрос.headers["Authorization"] == "Bearer test-token"
    assert json.loads(запрос.content) == {"free_text": "x"}
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.call("/geo/yandex", {"free_text": "x"}, provider="yandex_geocoder")
    assert (exc.value.kind, exc.value.status) == ("blocked", 403)
    assert exc.value.extra == {}
    # Лишние поля отказа (число попыток у читателей) едут в `extra`.
    respx.post(f"{GW}/llm/chat").mock(
        return_value=httpx.Response(
            200, json={"ok": False, "kind": "exhausted", "status": 429, "attempts": 3}
        )
    )
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.call("/llm/chat", {"system": "s", "user": "u"})
    assert (exc.value.kind, exc.value.extra) == ("exhausted", {"attempts": 3})
    assert gateway.kind_for(exc.value) == "exhausted"
    assert gateway.kind_for(gateway.GatewayError("auth", 401)) == "network"
    assert gateway.kind_for(gateway.GatewayError("no_key")) == "blocked"
    # Отказ провайдера не портит снимок ключей.
    assert gateway.key_present("yandex_geocoder")


@respx.mock
async def test_без_ключа_на_шлюзе_снимок_помечается() -> None:
    respx.post(f"{GW}/geo/dadata").mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "no_key", "status": None})
    )
    gateway.known_keys = {"dadata": True}
    assert gateway.key_present("dadata")
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.call("/geo/dadata", {"text": "x"}, provider="dadata")
    assert exc.value.kind == "no_key"
    assert not gateway.key_present("dadata")


@respx.mock
async def test_отказ_самого_шлюза_это_network_и_без_адреса_в_логе() -> None:
    gateway.known_keys = {"dadata": True}
    respx.post(f"{GW}/geo/dadata").mock(side_effect=httpx.ConnectTimeout("boom"))
    with structlog.testing.capture_logs() as логи:
        with pytest.raises(gateway.GatewayError) as exc:
            await gateway.call("/geo/dadata", {"text": "Ленина 5"}, provider="dadata")
    assert exc.value.kind == "network"
    события = [л for л in логи if л["event"] == "gateway.unreachable"]
    assert события and события[0]["error"] == "ConnectTimeout"
    assert "Ленина" not in json.dumps(логи, ensure_ascii=False)
    assert "test-token" not in json.dumps(логи, ensure_ascii=False)
    # 5xx шлюза — тоже network, 401 — auth, не-JSON — bad_response.
    respx.post(f"{GW}/geo/dadata").mock(return_value=httpx.Response(502, text="bad gateway"))
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.call("/geo/dadata", {"text": "x"})
    assert (exc.value.kind, exc.value.status) == ("network", 502)
    respx.post(f"{GW}/geo/dadata").mock(return_value=httpx.Response(401, json={"detail": "x"}))
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.call("/geo/dadata", {"text": "x"})
    assert exc.value.kind == "auth"
    respx.post(f"{GW}/geo/dadata").mock(return_value=httpx.Response(200, text="<html>"))
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.call("/geo/dadata", {"text": "x"})
    assert exc.value.kind == "bad_response"
    # Снимок ключей от отказов шлюза не меняется.
    assert gateway.key_present("dadata")


async def test_без_настроек_поход_невозможен(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "gateway_url", "")
    with pytest.raises(gateway.GatewayError) as exc:
        await gateway.call("/geo/dadata", {"text": "x"})
    assert exc.value.kind == "network"


@respx.mock
async def test_снимок_status_обновляется_по_ttl(monkeypatch) -> None:  # noqa: ANN001
    маршрут = respx.get(f"{GW}/status").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "providers": {
                    "dadata": {"key_present": True, "base_url": "u"},
                    "nominatim": {"key_present": None, "base_url": "u"},
                    "groq": {"key_present": False, "base_url": "u"},
                },
            },
        )
    )
    monkeypatch.setattr(gateway, "known_keys", None)
    monkeypatch.setattr(gateway, "_status_at", float("-inf"))
    await gateway.refresh_status()
    assert gateway.known_keys == {"dadata": True, "nominatim": None, "groq": False}
    assert gateway.key_present("dadata") and not gateway.key_present("groq")
    # Свежий снимок не перечитывается; force — перечитывается.
    await gateway.refresh_status()
    assert маршрут.call_count == 1
    await gateway.refresh_status(force=True)
    assert маршрут.call_count == 2
    # Обрыв связи снимок не трогает, но помнится до конца TTL: следующий
    # вызов без force не ходит в шлюз снова.
    сломан = respx.get(f"{GW}/status").mock(side_effect=httpx.ConnectError("x"))
    await gateway.refresh_status(force=True)
    assert gateway.known_keys == {"dadata": True, "nominatim": None, "groq": False}
    await gateway.refresh_status()
    assert сломан.call_count == 3  # тот же маршрут: два удачных + один обрыв, без повтора


async def test_передача_клиента_из_теста() -> None:
    def обработчик(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"ok": True, "fixes": {}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(обработчик)) as client:
        данные = await gateway.call("/spell", {"text": "x", "words": []}, client=client)
    assert данные == {"ok": True, "fixes": {}}


@respx.mock
async def test_no_key_без_снимка_не_выключает_остальных(monkeypatch) -> None:  # noqa: ANN001
    """Ревью 18.09: один ответ `no_key` до первого снимка заводил снимок с
    одним провайдером — и все остальные считались «без ключа»."""
    monkeypatch.setattr(gateway, "known_keys", None)
    respx.post(f"{GW}/geo/dadata").mock(
        return_value=httpx.Response(200, json={"ok": False, "kind": "no_key"})
    )
    with pytest.raises(gateway.GatewayError):
        await gateway.call("/geo/dadata", {"text": "x"}, provider="dadata")
    assert not gateway.key_present("dadata")
    assert gateway.key_present("yandex_geocoder") and gateway.key_present("openrouter")
    assert gateway.known_keys is None
    # Память «ключа нет» живёт до удачного снимка; неудачный её не трогает.
    respx.get(f"{GW}/status").mock(side_effect=httpx.ConnectError("x"))
    await gateway.refresh_status(force=True)
    assert not gateway.key_present("dadata") and gateway.last_status_error == "network"
    respx.get(f"{GW}/status").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "providers": {"dadata": {"key_present": True, "base_url": "u"}}}
        )
    )
    await gateway.refresh_status(force=True)
    assert gateway.key_present("dadata") and gateway.last_status_error is None
