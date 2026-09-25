"""Проверка доступности на шлюзе (перенос из tests/unit/test_api_monitor_1609.py
LeadChat, 16.09): сторож не пускает внутрь, итог читается словами, по
редиректам не идём, встроенные проверяются своим адресом и ключом, а ключ
и адрес не попадают ни в ответ, ни в лог."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

import httpx
import pytest
import respx
import structlog

from leadchat_gateway import check
from leadchat_gateway.config import settings

pytestmark = pytest.mark.anyio


def _ответ(request: httpx.Request) -> httpx.Response:
    """Выдуманные внешние хосты — как в исходном тесте монитора. Проба идёт
    на IP, имя — в Host: различаем по нему."""
    host = request.headers.get("Host", request.url.host).split(":")[0]
    if host == "ok.test":
        return httpx.Response(405)  # GET без тела — но сервер жив
    if host == "auth.test":
        return httpx.Response(403)
    if host == "redirect.test":
        return httpx.Response(302, headers={"Location": "http://127.0.0.1:6379/"})
    if host == "127.0.0.1":
        raise AssertionError("внутренний адрес достигнут")
    return httpx.Response(503)


async def test_имя_разрешается_один_раз_и_поход_идёт_на_проверенный_ip(
    gw: httpx.AsyncClient, monkeypatch
) -> None:  # noqa: ANN001
    """DNS-rebinding (ревью 18.09): имя с TTL 0 отвечает сторожу публичным
    адресом, а походу — внутренним. Поэтому поход идёт на IP из сторожа, а
    имя едет только в Host; второго разрешения нет."""
    ответы = iter(["93.184.216.9", "127.0.0.1"])

    def getaddrinfo(host, port, *a, **kw):  # noqa: ANN001, ANN202
        return [(2, 1, 6, "", (next(ответы), port))]

    monkeypatch.setattr(check.socket, "getaddrinfo", getaddrinfo)
    with respx.mock(assert_all_called=False) as сеть:
        маршрут = сеть.route(host="93.184.216.9").mock(return_value=httpx.Response(204))
        сеть.route(host="127.0.0.1").mock(side_effect=AssertionError("внутренний адрес достигнут"))
        r = await gw.post("/check/url", json={"url": "http://rebind.test:8080/secret"})
    итог = r.json()["result"]
    assert итог["ok"] and итог["status"] == 204
    запрос = маршрут.calls.last.request
    assert запрос.url.host == "93.184.216.9" and запрос.url.port == 8080
    assert запрос.headers["Host"] == "rebind.test:8080"


async def test_имя_с_внутренним_адресом_среди_ответов_не_пускается(
    gw: httpx.AsyncClient, monkeypatch
) -> None:  # noqa: ANN001
    def getaddrinfo(host, port, *a, **kw):  # noqa: ANN001, ANN202
        return [(2, 1, 6, "", ("93.184.216.9", port)), (2, 1, 6, "", ("10.10.0.1", port))]

    monkeypatch.setattr(check.socket, "getaddrinfo", getaddrinfo)
    with respx.mock(assert_all_called=False):
        r = await gw.post("/check/url", json={"url": "https://two-faced.test/"})
    assert r.json()["result"]["error"] == "внутренний адрес"


async def test_долгое_разрешение_имени_не_держит_пробу(gw: httpx.AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(check, "DNS_TIMEOUT_SEC", 0.05)

    def медленно(url):  # noqa: ANN001, ANN202
        import time

        time.sleep(0.3)
        return None, "93.184.216.9"

    monkeypatch.setattr(check, "_чужой_адрес", медленно)
    r = await gw.post("/check/url", json={"url": "http://slow.test/"})
    assert r.json()["result"]["error"] == "имя не разрешилось вовремя"


async def test_сторож_не_пускает_внутрь(gw: httpx.AsyncClient) -> None:
    # Ни одного похода: все адреса отбиваются до сети (respx без маршрутов
    # уронил бы любой настоящий запрос).
    with respx.mock(assert_all_called=False):
        for url in (
            "http://localhost:6379/",
            "http://127.0.0.1/",
            "ftp://ok.test/",
            "http://10.0.0.5/",
            "http://10.10.0.5/",  # WireGuard: мы сами и прод LeadChat
            "http://ok.test:99999/",
            "http://[::1/x",
            "http://foo.internal/",
            "http://svc.localhost/",
        ):
            r = await gw.post("/check/url", json={"url": url})
            assert r.status_code == 200, url
            итог = r.json()
            assert итог["ok"] is True, url
            assert not итог["result"]["ok"] and итог["result"]["error"], url
    пусто = await gw.post("/check/url", json={"url": ""})
    assert пусто.json()["result"]["error"] == "адрес не задан"
    assert (await gw.post("/check/url", json={})).json()["result"]["error"] == "адрес не задан"
    assert (await gw.post("/check/url", json={"url": 5})).status_code == 422
    # WireGuard закрыт и буквально, поверх is_global.
    assert check._чужой_адрес("http://10.10.0.2:8792/health") == ("внутренний адрес", None)
    assert check._чужой_адрес("http://10.10.0.1/") == ("внутренний адрес", None)


#: Выдуманные хосты формы → «проверенные» IP, на которые идёт проба.
IP = {
    "ok.test": "93.184.216.1",
    "down.test": "93.184.216.2",
    "auth.test": "93.184.216.3",
    "redirect.test": "93.184.216.4",
    "bridge.test": "93.184.216.5",
    "dead.test": "93.184.216.6",
}


async def test_проба_адреса_читается_словами(gw: httpx.AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    # Наружу — через подменённого сторожа: выдуманные хосты не разрешаются,
    # сторож отдаёт IP, и проба идёт на него с именем в Host.
    monkeypatch.setattr(check, "_чужой_адрес", lambda url: (None, IP[urlsplit(url).hostname or ""]))
    with respx.mock(assert_all_called=False) as сеть:
        # Маршруты сверяются по порядку: частные — раньше общего.
        редирект = сеть.route(host=IP["redirect.test"]).mock(side_effect=_ответ)
        сеть.route(host=IP["bridge.test"]).mock(return_value=httpx.Response(404, text="not found"))
        сеть.route(host=IP["dead.test"]).mock(side_effect=httpx.ConnectTimeout("boom"))
        сеть.route().mock(side_effect=_ответ)

        async def проба(url: str) -> dict:
            r = await gw.post("/check/url", json={"url": url})
            assert r.status_code == 200
            return r.json()["result"]

        жив = await проба("https://ok.test/v1")
        assert жив["ok"] and жив["status"] == 405 and жив["ms"] is not None
        assert жив["error"] is None
        плохо = await проба("https://down.test/")
        assert not плохо["ok"] and плохо["status"] == 503 and "беде" in плохо["error"]
        закрыт = await проба("https://auth.test/")
        assert not закрыт["ok"] and "403" in закрыт["error"]
        # Редирект внутрь — не следуем: один запрос, 302 = жив.
        было = len(сеть.calls)
        ред = await проба("https://redirect.test/")
        assert ред["ok"] and ред["status"] == 302
        assert редирект.call_count == 1 and len(сеть.calls) == было + 1
        # GET 404 своей записи — по-прежнему жив (только POST-проба читателя
        # читает 404 как «путь не найден»).
        свой = await проба("https://bridge.test/ping")
        assert свой["ok"] and свой["status"] == 404
        # Обрыв — словами, без имени класса.
        обрыв = await проба("https://dead.test/")
        assert not обрыв["ok"] and обрыв["status"] is None
        assert обрыв["error"] == "нет ответа на соединение (таймаут)"
        # User-Agent монитора на месте.
        assert сеть.calls.last.request.headers["User-Agent"] == check.USER_AGENT


async def test_читатели_через_post_с_пустым_телом(gw: httpx.AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "openrouter_api_key", "k-open")
    monkeypatch.setattr(settings, "mistral_api_key", "k-mistral")

    def читатель(request: httpx.Request) -> httpx.Response:
        # Пустое тело доходит до провайдера, и тот отбивает 400 — ключ уже
        # принят; без ключа — 401.
        assert request.method == "POST" and request.content == b"{}"
        return httpx.Response(401 if "Authorization" not in request.headers else 400)

    with respx.mock(assert_all_called=False) as сеть:
        маршрут = сеть.post("https://openrouter.ai/api/v1/chat/completions").mock(
            side_effect=читатель
        )
        сеть.post("https://api.groq.com/openai/v1/chat/completions").mock(side_effect=читатель)
        сеть.post("https://api.mistral.ai/v1/chat/completions").mock(
            return_value=httpx.Response(404, text="not found")
        )
        with structlog.testing.capture_logs() as логи:
            жив = (await gw.post("/check/openrouter")).json()
            # Ключа нет: 401 — не «отвергнут», а «не задан».
            нет = (await gw.post("/check/groq")).json()
            # 404 на POST по известному пути — не «жив»: работа на таком падает.
            мимо = (await gw.post("/check/mistral")).json()
    assert жив["ok"] is True and жив["result"]["ok"] and жив["result"]["status"] == 400
    assert маршрут.calls.last.request.headers["Authorization"] == "Bearer k-open"
    assert not нет["result"]["ok"] and нет["result"]["error"] == "401: ключ на сервере не задан"
    assert not мимо["result"]["ok"] and мимо["result"]["status"] == 404
    assert "/api/v1" in мимо["result"]["error"]
    # Ключ и адрес — ни в ответе, ни в логе.
    текст = json.dumps([жив, нет, мимо], ensure_ascii=False) + json.dumps(логи, ensure_ascii=False)
    assert "k-open" not in текст and "k-mistral" not in текст
    assert "openrouter.ai" not in текст and "chat/completions" not in текст
    события = [л for л in логи if л["event"] == "check.provider"]
    assert [(л["provider"], л["ok"], л["status"]) for л in события] == [
        ("openrouter", True, 400),
        ("groq", False, 401),
        ("mistral", False, 404),
    ]


async def test_справочники_своим_адресом_и_ключом(gw: httpx.AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "dadata_api_key", "k-dadata")
    monkeypatch.setattr(settings, "yandex_geocoder_key", "k-yandex")
    monkeypatch.setattr(settings, "nominatim_contact", "admin@example.org")
    with respx.mock(assert_all_called=False) as сеть:
        dadata = сеть.get(
            "https://suggestions.dadata.ru/suggestions/api/4_1/rs/suggest/address"
        ).mock(return_value=httpx.Response(200, json={"suggestions": []}))
        яндекс = сеть.get("https://geocode-maps.yandex.ru/1.x/").mock(
            return_value=httpx.Response(200, json={})
        )
        саджест = сеть.get("https://suggest-maps.yandex.ru/v1/suggest").mock(
            return_value=httpx.Response(403)
        )
        osm = сеть.get("https://nominatim.openstreetmap.org/status").mock(
            return_value=httpx.Response(200, text="OK")
        )
        сеть.get("https://ahunter.ru/site/suggest/address").mock(
            return_value=httpx.Response(200, json={"suggestions": []})
        )
        сеть.get("https://speller.yandex.net/services/spellservice.json/checkText").mock(
            return_value=httpx.Response(200, json=[])
        )
        антропик = сеть.get("https://api.anthropic.com/v1/models").mock(
            return_value=httpx.Response(403)
        )
        итоги = {
            имя: (await gw.post(f"/check/{имя}")).json()["result"]
            for имя in (
                "dadata",
                "yandex_geocoder",
                "yandex_suggest",
                "nominatim",
                "ahunter",
                "speller",
                "anthropic",
            )
        }
    assert dadata.calls.last.request.headers["Authorization"] == "Token k-dadata"
    assert итоги["dadata"]["ok"] and итоги["dadata"]["status"] == 200
    assert яндекс.calls.last.request.url.params["apikey"] == "k-yandex"
    assert итоги["yandex_geocoder"]["ok"]
    # Ключа Геосаджеста нет: без apikey в запросе, 403 читается как «не задан».
    assert "apikey" not in саджест.calls.last.request.url.params
    assert итоги["yandex_suggest"]["error"] == "403: ключ на сервере не задан"
    assert osm.calls.last.request.headers["User-Agent"] == "LeadChat/1.0 (admin@example.org)"
    assert итоги["nominatim"]["ok"] and итоги["ahunter"]["ok"] and итоги["speller"]["ok"]
    assert "x-api-key" not in антропик.calls.last.request.headers
    assert итоги["anthropic"]["error"] == "403: ключ на сервере не задан"
    assert "k-dadata" not in json.dumps(итоги) and "k-yandex" not in json.dumps(итоги)


async def test_неизвестный_провайдер_и_токен(
    gw: httpx.AsyncClient, gw_anon: httpx.AsyncClient
) -> None:
    assert (await gw.post("/check/2gis")).status_code == 404
    assert (await gw_anon.post("/check/dadata")).status_code == 401
    assert (await gw_anon.post("/check/url", json={"url": "https://ok.test/"})).status_code == 401
