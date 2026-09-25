"""OSM (Nominatim), Яндекс Геокодер и Геосаджест за шлюзом: что уходит
провайдеру, что возвращается LeadChat, что попадает в журнал.

Без сети — все ответы записаны (respx на адреса провайдеров; в шлюз — через
ASGI). Живой ответ Nominatim на образец владельца снят с прода 11.09; адрес,
идентификаторы OSM и координаты в нём заменены синтетикой той же формы.
Перенесено из `tests/unit/test_geocode_provider.py` LeadChat вместе с кодом
провайдеров (16.09).

⚠ ГЛАВНОЕ, ЧТО СТЕРЕЖЁТСЯ, — ЧТО НАРУЖУ НЕ УХОДИТ ЛИШНЕГО, А В ОТВЕТ И ЖУРНАЛ
НЕ ПОПАДАЮТ АДРЕС И КЛЮЧ. В строке запроса — улица, дом, пункт, город; ключ
Яндекса едет параметром. Отказ провайдера превращается в `{"ok": false, kind}`
без URL: `str(exc)` от httpx несёт URL целиком, а в нём адрес клиента.
"""

from __future__ import annotations

import time
from typing import Any

import httpx
import pytest
import respx
import structlog

from leadchat_gateway.config import settings
from leadchat_gateway.errors import ProviderError
from leadchat_gateway.providers import nominatim, yandex_geocoder, yandex_suggest

pytestmark = pytest.mark.anyio

#: То, что LeadChat собирает из `Query` для структурного запроса и свободного
#: текста (`street` уже развёрнут словарём типов улиц LeadChat).
СТРУКТУРНЫЙ: dict[str, Any] = {
    "mode": "structured",
    "street": "улица звенигородская",
    "house": "1",
    "city": "Орск",
    "region": "Оренбургская область",
    "free_text": "Оренбургская область, Орск, Заречный, улица звенигородская 1",
}
СВОБОДНЫЙ = {**СТРУКТУРНЫЙ, "mode": "free"}

#: Живой ответ Nominatim, снятый с прода 11.09 (jsonv2, addressdetails=1);
#: адрес, id и координаты заменены синтетикой.
ОТВЕТ_ЗАРЕЧНЫЙ = [
    {
        "place_id": 200000001,
        "osm_type": "way",
        "osm_id": 120000001,
        "lat": "51.2101234",
        "lon": "58.5012345",
        "category": "building",
        "type": "apartments",
        "place_rank": 30,
        "addresstype": "building",
        "display_name": "1, Звенигородская улица, Заречный, Советский район, Орск, "
        "городской округ Орск, Оренбургская область, Приволжский федеральный округ, 462400, Россия",
        "address": {
            "house_number": "1",
            "road": "Звенигородская улица",
            "suburb": "Заречный",
            "city_district": "Советский район",
            "city": "Орск",
            "county": "городской округ Орск",
            "state": "Оренбургская область",
            "postcode": "462400",
            "country": "Россия",
            "country_code": "ru",
        },
    }
]

ДОМ = {
    "street": "Звенигородская улица",
    "house": "1",
    "settlement": "Заречный",
    "city": "Орск",
    "region": "Оренбургская область",
    "lat": 51.2101234,
    "lon": 58.5012345,
    "house_level": True,
    "interpolated": False,
    "settlement_kind": "district",
    "precise": True,
    "area": None,
}


@pytest.fixture(autouse=True)
def _без_темпа(monkeypatch: pytest.MonkeyPatch) -> None:
    """Темп OSM (секунда между запросами) в тестах выключен — кроме теста,
    который проверяет сам темп и ставит интервал себе."""
    monkeypatch.setattr(nominatim, "MIN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(nominatim, "_не_раньше", 0.0)


def _params(маршрут: respx.Route, n: int = 0) -> dict[str, str]:
    return dict(httpx.URL(str(маршрут.calls[n].request.url)).params)


# --- Nominatim ---------------------------------------------------------------


@respx.mock
async def test_структурный_запрос_из_компонентов_и_честный_user_agent(
    gw: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "nominatim_contact", "admin@example.org")
    маршрут = respx.get(nominatim.BASE_URL).mock(
        return_value=httpx.Response(200, json=ОТВЕТ_ЗАРЕЧНЫЙ)
    )
    r = await gw.post("/geo/nominatim", json=СТРУКТУРНЫЙ)

    assert маршрут.call_count == 1
    params = _params(маршрут)
    assert params["street"] == "1 улица звенигородская"
    assert params["city"] == "Орск"
    assert params["state"] == "Оренбургская область"
    assert params["limit"] == "5"
    assert params["countrycodes"] == "ru"
    assert "q" not in params
    assert маршрут.calls[0].request.headers["User-Agent"] == "LeadChat/1.0 (admin@example.org)"

    assert r.status_code == 200
    assert r.json() == {"ok": True, "hits": [ДОМ]}


@respx.mock
async def test_свободный_текст_это_q_без_компонентов(gw: httpx.AsyncClient) -> None:
    маршрут = respx.get(nominatim.BASE_URL).mock(return_value=httpx.Response(200, json=[]))
    r = await gw.post("/geo/nominatim", json=СВОБОДНЫЙ)
    assert r.json() == {"ok": True, "hits": []}
    params = _params(маршрут)
    assert params["q"] == СВОБОДНЫЙ["free_text"]
    assert "street" not in params and "city" not in params and "state" not in params


@respx.mock
async def test_без_города_и_области_их_нет_в_параметрах(gw: httpx.AsyncClient) -> None:
    маршрут = respx.get(nominatim.BASE_URL).mock(return_value=httpx.Response(200, json=[]))
    await gw.post("/geo/nominatim", json={**СТРУКТУРНЫЙ, "city": None, "region": None})
    params = _params(маршрут)
    assert params["street"] == "1 улица звенигородская"
    assert "city" not in params and "state" not in params


def test_user_agent_без_контакта() -> None:
    assert nominatim.user_agent() == "LeadChat/1.0"


@respx.mock
async def test_интерполированный_адрес_помечается_догадкой(gw: httpx.AsyncClient) -> None:
    ответ = [dict(ОТВЕТ_ЗАРЕЧНЫЙ[0], osm_type="way", category="place", type="house")]
    respx.get(nominatim.BASE_URL).mock(return_value=httpx.Response(200, json=ответ))
    r = await gw.post("/geo/nominatim", json=СТРУКТУРНЫЙ)
    (hit,) = r.json()["hits"]
    assert hit["interpolated"] is True


@respx.mock
async def test_запрет_это_blocked(gw: httpx.AsyncClient) -> None:
    respx.get(nominatim.BASE_URL).mock(
        return_value=httpx.Response(403, text="<html>Blocked</html>")
    )
    r = await gw.post("/geo/nominatim", json=СТРУКТУРНЫЙ)
    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert (r.json()["kind"], r.json()["status"]) == ("blocked", 403)


@pytest.mark.parametrize("код", [429, 503])
@respx.mock
async def test_помедленнее_и_обслуживание_это_повтор_а_не_бан(
    gw: httpx.AsyncClient, код: int
) -> None:
    """503 на десять минут не должен замораживать строки навсегда (ревью 11.09)."""
    respx.get(nominatim.BASE_URL).mock(return_value=httpx.Response(код, text="busy"))
    r = await gw.post("/geo/nominatim", json=СТРУКТУРНЫЙ)
    assert (r.json()["kind"], r.json()["status"]) == ("network", код)


@respx.mock
async def test_html_вместо_json_это_blocked(gw: httpx.AsyncClient) -> None:
    respx.get(nominatim.BASE_URL).mock(
        return_value=httpx.Response(200, text="<html>rate limited</html>")
    )
    r = await gw.post("/geo/nominatim", json=СТРУКТУРНЫЙ)
    assert r.json()["kind"] == "blocked"


@respx.mock
async def test_не_список_это_bad_response(gw: httpx.AsyncClient) -> None:
    respx.get(nominatim.BASE_URL).mock(return_value=httpx.Response(200, json={"error": "x"}))
    r = await gw.post("/geo/nominatim", json=СТРУКТУРНЫЙ)
    assert r.json()["kind"] == "bad_response"


@respx.mock
async def test_обрыв_сети_это_network_и_без_адреса_в_ответе_и_журнале(
    gw: httpx.AsyncClient,
) -> None:
    respx.get(nominatim.BASE_URL).mock(side_effect=httpx.ConnectTimeout("timed out"))
    with structlog.testing.capture_logs() as logs:
        r = await gw.post("/geo/nominatim", json=СТРУКТУРНЫЙ)
    assert r.json()["kind"] == "network"
    assert r.json()["status"] is None
    # Ни в ответе, ни в журнале — ни улицы, ни адреса провайдера.
    assert "звенигородск" not in r.text.lower()
    assert "nominatim.openstreetmap.org" not in r.text
    журнал = str(logs).lower()
    assert "звенигородск" not in журнал and "openstreetmap" not in журнал
    assert any(з.get("event") == "geo.provider_failed" for з in logs)


@respx.mock
async def test_темп_osm_второй_запрос_не_раньше_интервала(
    gw: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Политика OSM — не чаще запроса в секунду; темп держит шлюз у самой двери."""
    monkeypatch.setattr(nominatim, "MIN_INTERVAL_SEC", 0.2)
    моменты: list[float] = []

    def ответ(request: httpx.Request) -> httpx.Response:
        моменты.append(time.monotonic())
        return httpx.Response(200, json=[])

    respx.get(nominatim.BASE_URL).mock(side_effect=ответ)
    начало = time.monotonic()
    await gw.post("/geo/nominatim", json=СТРУКТУРНЫЙ)
    await gw.post("/geo/nominatim", json=СВОБОДНЫЙ)
    assert len(моменты) == 2
    assert моменты[1] - моменты[0] >= 0.15
    assert time.monotonic() - начало >= 0.19


# --- Яндекс Геокодер ---------------------------------------------------------

ОБРАЗЕЦ_ГЕОКОДЕРА = {
    "response": {
        "GeoObjectCollection": {
            "featureMember": [
                {
                    "GeoObject": {
                        "metaDataProperty": {
                            "GeocoderMetaData": {
                                "precision": "exact",
                                "kind": "house",
                                "Address": {
                                    "Components": [
                                        {"kind": "country", "name": "Россия"},
                                        {
                                            "kind": "province",
                                            "name": "Приволжский федеральный округ",
                                        },
                                        {"kind": "province", "name": "Оренбургская область"},
                                        {"kind": "locality", "name": "Орск"},
                                        {"kind": "district", "name": "посёлок Заречный"},
                                        {"kind": "street", "name": "Звенигородская улица"},
                                        {"kind": "house", "name": "1"},
                                    ]
                                },
                            }
                        },
                        "Point": {"pos": "58.501234 51.210123"},
                    }
                }
            ]
        }
    }
}


def test_яндекс_разбирается_по_записанному_образцу() -> None:
    """Формат по документации 1.x; живьём не проверен — ключа нет (11.09)."""
    (hit,) = yandex_geocoder.parse_response(ОБРАЗЕЦ_ГЕОКОДЕРА, city="Орск")
    assert hit.street == "Звенигородская улица"
    assert hit.house == "1"
    assert hit.settlement == "посёлок Заречный"
    assert hit.settlement_kind == "district"
    assert hit.city == "Орск"
    assert hit.region == "Оренбургская область"
    assert (hit.lat, hit.lon) == (51.210123, 58.501234)
    assert hit.house_level is True


def test_яндекс_не_та_форма_это_bad_response() -> None:
    with pytest.raises(ProviderError) as exc:
        yandex_geocoder.parse_response({"response": {}}, city=None)
    assert exc.value.kind == "bad_response"
    assert (
        yandex_geocoder.parse_response(
            {"response": {"GeoObjectCollection": {"featureMember": []}}}, city=None
        )
        == []
    )


@respx.mock
async def test_яндекс_ключ_едет_параметром_и_не_попадает_в_ответ(
    gw: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "yandex_geocoder_key", "SECRET-KEY-123")
    маршрут = respx.get(yandex_geocoder.BASE_URL).mock(
        return_value=httpx.Response(403, json={"statusCode": 403})
    )
    with structlog.testing.capture_logs() as logs:
        r = await gw.post("/geo/yandex", json={"free_text": СТРУКТУРНЫЙ["free_text"]})
    assert (r.json()["kind"], r.json()["status"]) == ("blocked", 403)
    assert "SECRET-KEY-123" not in r.text
    assert "SECRET-KEY-123" not in str(logs)
    params = _params(маршрут)
    assert params["apikey"] == "SECRET-KEY-123"
    assert params["geocode"] == СТРУКТУРНЫЙ["free_text"]
    assert params["results"] == "5" and params["format"] == "json"


@respx.mock
async def test_яндекс_удача_и_город_объявления_в_разборе(
    gw: httpx.AsyncClient, monkeypatch
) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "yandex_geocoder_key", "k")
    respx.get(yandex_geocoder.BASE_URL).mock(
        return_value=httpx.Response(200, json=ОБРАЗЕЦ_ГЕОКОДЕРА)
    )
    r = await gw.post("/geo/yandex", json={"free_text": "x", "city": "Орск"})
    (hit,) = r.json()["hits"]
    assert r.json()["ok"] is True
    assert (hit["city"], hit["settlement"], hit["house_level"]) == (
        "Орск",
        "посёлок Заречный",
        True,
    )


@respx.mock
async def test_яндекс_без_ключа_это_no_key_и_без_похода(gw: httpx.AsyncClient) -> None:
    маршрут = respx.get(yandex_geocoder.BASE_URL).mock(return_value=httpx.Response(200, json={}))
    r = await gw.post("/geo/yandex", json={"free_text": "x"})
    assert r.json()["ok"] is False and r.json()["kind"] == "no_key"
    assert маршрут.call_count == 0


# --- Яндекс Геосаджест -------------------------------------------------------

ОБРАЗЕЦ_САДЖЕСТА = {
    "results": [
        {
            "title": {"text": "Звенигородская улица, 1"},
            "subtitle": {"text": "Орск, Оренбургская область, Россия"},
            "tags": ["house"],
            "address": {
                "formatted_address": "Россия, Оренбургская область, Орск, посёлок Заречный, "
                "Звенигородская улица, 1",
                "component": [
                    {"name": "Россия", "kind": ["COUNTRY"]},
                    {"name": "Оренбургская область", "kind": ["REGION"]},
                    {"name": "Орск", "kind": ["LOCALITY"]},
                    {"name": "посёлок Заречный", "kind": ["DISTRICT"]},
                    {"name": "Звенигородская улица", "kind": ["STREET"]},
                    {"name": "1", "kind": ["HOUSE"]},
                ],
            },
        },
        # Подсказка без дома — не годится: искали дом.
        {
            "title": {"text": "Звенигородская улица"},
            "address": {"component": [{"name": "Звенигородская улица", "kind": ["STREET"]}]},
        },
    ]
}


def test_саджест_разбирается_по_документированному_образцу() -> None:
    """Формат `print_address=1`; живьём не проверен — ключ ещё не активен (11.09)."""
    (п,) = yandex_suggest.parse_response(ОБРАЗЕЦ_САДЖЕСТА)
    assert (п.street, п.house, п.city, п.settlement, п.region) == (
        "Звенигородская улица",
        "1",
        "Орск",
        "посёлок Заречный",
        "Оренбургская область",
    )
    assert п.formatted and п.formatted.startswith("Россия, Оренбургская область")


def test_саджест_ничего_не_нашёл_это_пустой_ответ_а_не_сбой() -> None:
    """Живой ответ без совпадений — `{}` без `results` (бой 12.09: 95 «сбоев» за два часа)."""
    assert yandex_suggest.parse_response({}) == []
    assert yandex_suggest.parse_response({"results": []}) == []
    with pytest.raises(ProviderError) as exc:
        yandex_suggest.parse_response([])
    assert exc.value.kind == "bad_response"


@respx.mock
async def test_саджест_шлёт_компоненты_и_не_роняет_ключ_в_ответ(
    gw: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "yandex_suggest_key", "SUGGEST-KEY-1")
    маршрут = respx.get(yandex_suggest.BASE_URL).mock(return_value=httpx.Response(403))
    r = await gw.post("/geo/yandex-suggest", json={"free_text": СТРУКТУРНЫЙ["free_text"]})
    assert r.json()["kind"] == "blocked"
    assert "SUGGEST-KEY-1" not in r.text
    params = _params(маршрут)
    assert params["apikey"] == "SUGGEST-KEY-1"
    assert params["text"] == СТРУКТУРНЫЙ["free_text"]
    assert params["types"] == "house" and params["print_address"] == "1"


@respx.mock
async def test_саджест_удача_и_пустой_ответ(gw: httpx.AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "yandex_suggest_key", "k")
    respx.get(yandex_suggest.BASE_URL).mock(
        side_effect=[
            httpx.Response(200, json=ОБРАЗЕЦ_САДЖЕСТА),
            httpx.Response(200, json={}),
        ]
    )
    r = await gw.post("/geo/yandex-suggest", json={"free_text": "x"})
    (п,) = r.json()["hits"]
    assert set(п) == {"street", "house", "city", "settlement", "region", "formatted"}
    assert (п["street"], п["house"]) == ("Звенигородская улица", "1")
    r = await gw.post("/geo/yandex-suggest", json={"free_text": "x"})
    assert r.json() == {"ok": True, "hits": []}


@respx.mock
async def test_саджест_без_ключа_это_no_key(gw: httpx.AsyncClient) -> None:
    r = await gw.post("/geo/yandex-suggest", json={"free_text": "x"})
    assert r.json()["kind"] == "no_key"
