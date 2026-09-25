"""Монитор внешних сервисов (16.09): состояние встроенных считается из
снимка ключей шлюза, настроек и счётчиков; свои записи заводятся, правятся,
удаляются; проверка доступности идёт через шлюз Амстердама (сторож адреса —
там же, gateway/tests/test_gw_check.py), а строка самого шлюза — первой и
напрямую в `/health`; итог помнится сутки."""

from __future__ import annotations

import json

import httpx
import pytest
import respx
import sqlalchemy as sa

from app.core.config import settings
from app.integrations import gateway
from app.models import AuditLog
from app.services import api_monitor, app_settings
from app.workers import geocode as worker

pytestmark = pytest.mark.anyio

GW = "http://gw.test"


def hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _по_ключу(items: list[dict], key: str) -> dict:
    return next(r for r in items if r["key"] == key)


async def test_встроенные_состояния(db, redis, monkeypatch) -> None:  # noqa: ANN001
    # Ключи — по снимку `/status` шлюза: у OpenRouter и Геосаджеста есть,
    # у DaData и Groq нет (в снимке отсутствуют).
    monkeypatch.setattr(gateway, "known_keys", {"openrouter": True, "yandex_suggest": True})
    await app_settings.set_many(
        db,
        {
            app_settings.ADDRESS_GEO_SPELLER_ENABLED: False,
            app_settings.ADDRESS_GEO_SPELLER_DAILY_LIMIT: 100,
            app_settings.ADDRESS_LLM_DAILY_LIMIT: 3,
        },
        user_id=None,
    )
    await db.commit()
    await redis.set(worker.ahunter_calls_key(), 7)
    await redis.set("geo:ahunter:down", "1")
    await redis.set("geo:exhausted:yandex_suggest", 1)
    await redis.set("geo:blocked:nominatim", worker.BLOCKED_THRESHOLD)
    from app.workers.address_llm import llm_calls_key

    await redis.set(llm_calls_key(), 3)
    items = await api_monitor.overview(db, redis)
    # Шлюз — первой строкой: адрес и токен заданы, снимок есть — «подключён».
    шлюз = items[0]
    assert (шлюз["key"], шлюз["state"], шлюз["key_present"], шлюз["env_var"]) == (
        "gateway",
        api_monitor.STATE_OK,
        True,
        "GATEWAY_TOKEN",
    )
    assert шлюз["url"] == f"{GW}/health"
    assert _по_ключу(items, "dadata")["state"] == api_monitor.STATE_NO_KEY
    # Ключ ищут у шлюза, а не в .env LeadChat (проверка 24.09).
    assert (
        _по_ключу(items, "dadata")["state_note"] == "у шлюза внешних API нет ключа DADATA_API_KEY"
    )
    assert _по_ключу(items, "speller")["state"] == api_monitor.STATE_OFF
    ah = _по_ключу(items, "ahunter")
    assert (ah["state"], ah["used_today"], ah["key_present"]) == (api_monitor.STATE_DOWN, 7, None)
    orr = _по_ключу(items, "openrouter")
    assert (orr["state"], orr["used_today"], orr["daily_limit"]) == (api_monitor.STATE_LIMIT, 3, 3)
    assert "полуночи" in orr["state_note"]
    # Groq без ключа — «нет ключа», расход у него не показывается.
    assert _по_ключу(items, "groq")["state"] == api_monitor.STATE_NO_KEY
    assert _по_ключу(items, "groq")["used_today"] is None
    # Встроенные проверяются шлюзом по имени провайдера; экрану — адрес
    # пробы (кнопке «Проверить» нужен непустой адрес).
    assert orr["url"] == f"{GW}/check/openrouter"
    assert await api_monitor.probe_spec(db, redis, "groq") == api_monitor.Probe(provider="groq")
    assert await api_monitor.probe_spec(db, redis, "dadata") == api_monitor.Probe(provider="dadata")
    assert await api_monitor.probe_spec(db, redis, "gateway") == api_monitor.Probe(
        url=f"{GW}/health"
    )
    # Закрыт на сутки — потолок; три блокировки — «не отвечает».
    assert _по_ключу(items, "yandex_suggest")["state"] == api_monitor.STATE_LIMIT
    assert _по_ключу(items, "nominatim")["state"] == api_monitor.STATE_DOWN
    # Тарифная заметка видна в любом состоянии; ни токена, ни ключей в ответе.
    assert "1 000" in _по_ключу(items, "yandex_suggest")["notes"]
    assert "test-token" not in json.dumps(items, ensure_ascii=False)
    assert all(r["kind"] == "builtin" and not r["editable"] for r in items)
    assert [r["key"] for r in items[:2]] == ["gateway", "dadata"]


async def test_строка_шлюза(db, redis, monkeypatch) -> None:  # noqa: ANN001
    async def шлюз() -> dict:
        return (await api_monitor.overview(db, redis))[0]

    # Снимка ключей нет — шлюз не отвечал: строка «не отвечает», остальные
    # ключи считаются заданными (нет ключа ≠ нет связи).
    monkeypatch.setattr(gateway, "known_keys", None)
    items = await api_monitor.overview(db, redis)
    assert items[0]["state"] == api_monitor.STATE_DOWN
    assert "снимок ключей не получен" in items[0]["state_note"]
    assert _по_ключу(items, "dadata")["key_present"] is True
    assert _по_ключу(items, "dadata")["state"] == api_monitor.STATE_OK
    # Нет токена — «нет ключа»; нет адреса — «выключен».
    monkeypatch.setattr(gateway, "known_keys", {})
    monkeypatch.setattr(settings, "gateway_token", "")
    строка = await шлюз()
    assert (строка["state"], строка["key_present"]) == (api_monitor.STATE_NO_KEY, False)
    assert "GATEWAY_TOKEN" in строка["state_note"]
    monkeypatch.setattr(settings, "gateway_url", "")
    строка = await шлюз()
    assert (строка["state"], строка["url"]) == (api_monitor.STATE_OFF, None)
    assert "GATEWAY_URL" in строка["state_note"]
    # Последняя проверка не прошла — «не отвечает» с её словами.
    monkeypatch.setattr(settings, "gateway_url", GW)
    monkeypatch.setattr(settings, "gateway_token", "test-token")
    await redis.set(
        "api:check:gateway",
        json.dumps(
            {"ok": False, "status": 502, "ms": 3, "error": "нет связи со шлюзом: ответ 502"}
        ),
    )
    строка = await шлюз()
    assert строка["state"] == api_monitor.STATE_DOWN and "502" in строка["state_note"]


async def test_причины_выключенности(db, redis) -> None:  # noqa: ANN001
    await app_settings.set_many(db, {app_settings.ADDRESS_GEO_ENABLED: False}, user_id=None)
    await db.commit()
    items = await api_monitor.overview(db, redis)
    assert _по_ключу(items, "dadata")["state_note"] == "проверка по карте выключена"
    await app_settings.set_many(db, {app_settings.ADDRESS_GEO_ENABLED: True}, user_id=None)
    await db.commit()
    items = await api_monitor.overview(db, redis)
    assert "OpenStreetMap" in _по_ключу(items, "yandex_geocoder")["state_note"]


async def test_свои_записи_через_api(client, tokens, db_sessionmaker) -> None:  # noqa: ANN001
    res = await client.post(
        "/api/v1/settings/apis",
        json={
            "name": "2ГИС Places (демо)",
            "purpose": "Организации по названию",
            "url": "https://catalog.api.2gis.com/3.0/items",
            "daily_limit": 1000,
            "notes": "демо-ключ на 1 000 запросов навсегда",
        },
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 201, res.text
    key = res.json()["key"]
    assert key == "2gis-places-demo"
    row = _по_ключу(res.json()["items"], key)
    assert (row["kind"], row["editable"], row["state"], row["daily_limit"]) == (
        "custom",
        True,
        api_monitor.STATE_UNKNOWN,
        1000,
    )
    # Второй с тем же именем — другой ключ; имя встроенного — тоже не занимает его ключ.
    again = await client.post(
        "/api/v1/settings/apis", json={"name": "2ГИС Places (демо)"}, headers=hdr(tokens["admin"])
    )
    assert again.json()["key"] == "2gis-places-demo-2"
    как_встроенный = await client.post(
        "/api/v1/settings/apis", json={"name": "DaData"}, headers=hdr(tokens["admin"])
    )
    assert как_встроенный.json()["key"] == "dadata-2"

    res = await client.patch(
        f"/api/v1/settings/apis/{key}",
        json={"daily_limit": 500, "enabled": False, "notes": "кончился"},
        headers=hdr(tokens["admin"]),
    )
    assert res.status_code == 200, res.text
    row = _по_ключу(res.json()["items"], key)
    assert (row["daily_limit"], row["enabled"], row["state"], row["notes"]) == (
        500,
        False,
        api_monitor.STATE_OFF,
        "кончился",
    )
    res = await client.patch(
        f"/api/v1/settings/apis/{key}",
        json={"unlimited_daily": True, "url": ""},
        headers=hdr(tokens["admin"]),
    )
    row = _по_ключу(res.json()["items"], key)
    assert row["daily_limit"] is None and row["url"] is None

    res = await client.delete(f"/api/v1/settings/apis/{key}", headers=hdr(tokens["admin"]))
    assert res.status_code == 200 and not any(r["key"] == key for r in res.json()["items"])
    assert (
        await client.delete(f"/api/v1/settings/apis/{key}", headers=hdr(tokens["admin"]))
    ).status_code == 404
    # Встроенную запись править нельзя.
    assert (
        await client.patch(
            "/api/v1/settings/apis/dadata", json={"notes": "x"}, headers=hdr(tokens["admin"])
        )
    ).status_code == 404
    async with db_sessionmaker() as s:
        действия = (
            (await s.execute(sa.select(AuditLog.action).where(AuditLog.entity == "api_registry")))
            .scalars()
            .all()
        )
    assert sorted(действия) == sorted(
        [
            "settings.api_added",
            "settings.api_added",
            "settings.api_added",
            "settings.api_changed",
            "settings.api_changed",
            "settings.api_removed",
        ]
    )


async def test_право_только_у_администратора(client, tokens) -> None:  # noqa: ANN001
    h = hdr(tokens["manager"])
    assert (await client.get("/api/v1/settings/apis", headers=h)).status_code == 403
    assert (
        await client.post("/api/v1/settings/apis", json={"name": "x"}, headers=h)
    ).status_code == 403
    assert (await client.patch("/api/v1/settings/apis/x", json={}, headers=h)).status_code == 403
    assert (await client.delete("/api/v1/settings/apis/x", headers=h)).status_code == 403
    assert (await client.post("/api/v1/settings/apis/x/check", headers=h)).status_code == 403


@respx.mock
async def test_проверка_через_шлюз(redis) -> None:  # noqa: ANN001
    # Встроенный: шлюз проверяет сам, ответ — его итог как есть.
    dadata = respx.post(f"{GW}/check/dadata").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"ok": True, "status": 200, "ms": 41, "error": None}}
        )
    )
    итог = await api_monitor.probe(redis, "dadata", api_monitor.Probe(provider="dadata"))
    assert (итог["ok"], итог["status"], итог["ms"], итог["error"]) == (True, 200, 41, None)
    assert dadata.calls.last.request.headers["Authorization"] == "Bearer test-token"
    assert dadata.calls.last.request.content == b""
    # Провайдер лежит — это итог шлюза, а не «нет связи».
    respx.post(f"{GW}/check/groq").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "ok": False,
                    "status": 401,
                    "ms": 9,
                    "error": "401: ключ на сервере не задан",
                },
            },
        )
    )
    нет = await api_monitor.probe(redis, "groq", api_monitor.Probe(provider="groq"))
    assert not нет["ok"] and нет["error"] == "401: ключ на сервере не задан"
    # Своя запись — `/check/url` с адресом в теле; сторож — на шлюзе.
    свой = respx.post(f"{GW}/check/url").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"ok": False, "status": None, "ms": None, "error": "внутренний адрес"},
            },
        )
    )
    итог = await api_monitor.probe(redis, "svc", api_monitor.Probe(url="http://10.0.0.5/"))
    assert not итог["ok"] and итог["error"] == "внутренний адрес"
    assert json.loads(свой.calls.last.request.content) == {"url": "http://10.0.0.5/"}
    # Пустой адрес — без похода.
    assert (await api_monitor.probe(redis, "x", None))["error"] == "адрес не задан"
    assert (await api_monitor.probe(redis, "x", api_monitor.Probe(url="")))["error"] == (
        "адрес не задан"
    )
    # Шлюз не отвечает / отвечает не тем — «нет связи со шлюзом».
    respx.post(f"{GW}/check/dadata").mock(side_effect=httpx.ConnectTimeout("boom"))
    обрыв = await api_monitor.probe(redis, "dadata", api_monitor.Probe(provider="dadata"))
    assert not обрыв["ok"] and обрыв["status"] is None
    assert обрыв["error"] == "нет связи со шлюзом: network (ConnectTimeout)"
    respx.post(f"{GW}/check/dadata").mock(return_value=httpx.Response(404, json={"detail": "x"}))
    мимо = await api_monitor.probe(redis, "dadata", api_monitor.Probe(provider="dadata"))
    assert мимо["error"] == "нет связи со шлюзом: network (404)"
    respx.post(f"{GW}/check/dadata").mock(return_value=httpx.Response(200, json={"ok": True}))
    форма = await api_monitor.probe(redis, "dadata", api_monitor.Probe(provider="dadata"))
    assert not форма["ok"] and форма["error"] == "ответ шлюза не той формы"
    # Итог запомнен.
    запомнено = await api_monitor._последняя_проверка(redis, "svc")
    assert запомнено is not None and запомнено["error"] == "внутренний адрес"
    последний = await api_monitor._последняя_проверка(redis, "dadata")
    assert последний is not None and последний["at"]


@respx.mock
async def test_проба_самого_шлюза(redis) -> None:  # noqa: ANN001
    spec = api_monitor.Probe(url=f"{GW}/health")
    health = respx.get(f"{GW}/health").mock(
        return_value=httpx.Response(200, json={"ok": True, "version": "abc1234"})
    )
    жив = await api_monitor.probe(redis, "gateway", spec)
    assert жив["ok"] and жив["status"] == 200 and жив["ms"] is not None
    # Без токена: /health открыт, а проба не должна нести его наружу зря.
    assert "Authorization" not in health.calls.last.request.headers
    respx.get(f"{GW}/health").mock(return_value=httpx.Response(200, json={"ok": False}))
    assert (await api_monitor.probe(redis, "gateway", spec))["error"] == (
        "нет связи со шлюзом: ответ 200"
    )
    respx.get(f"{GW}/health").mock(return_value=httpx.Response(502, text="bad gateway"))
    лёг = await api_monitor.probe(redis, "gateway", spec)
    assert not лёг["ok"] and лёг["status"] == 502 and "502" in лёг["error"]
    respx.get(f"{GW}/health").mock(side_effect=httpx.ConnectError("x"))
    обрыв = await api_monitor.probe(redis, "gateway", spec)
    assert not обрыв["ok"] and обрыв["error"].startswith("нет связи со шлюзом")
    запомнено = await api_monitor._последняя_проверка(redis, "gateway")
    assert запомнено is not None and запомнено["status"] is None


@respx.mock
async def test_состояние_своей_записи_после_проверки(client, tokens) -> None:  # noqa: ANN001
    res = await client.post(
        "/api/v1/settings/apis",
        json={"name": "Мой сервис", "url": "https://svc.test/ping"},
        headers=hdr(tokens["admin"]),
    )
    key = res.json()["key"]
    маршрут = respx.post(f"{GW}/check/url").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"ok": True, "status": 200, "ms": 12, "error": None}}
        )
    )
    res = await client.post(f"/api/v1/settings/apis/{key}/check", headers=hdr(tokens["admin"]))
    assert res.status_code == 200 and res.json()["checked"]["ok"]
    assert json.loads(маршрут.calls.last.request.content) == {"url": "https://svc.test/ping"}
    assert _по_ключу(res.json()["items"], key)["state"] == api_monitor.STATE_OK
    respx.post(f"{GW}/check/url").mock(
        return_value=httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"ok": False, "status": 500, "ms": 30, "error": "500: сервер в беде"},
            },
        )
    )
    res = await client.post(f"/api/v1/settings/apis/{key}/check", headers=hdr(tokens["admin"]))
    assert _по_ключу(res.json()["items"], key)["state"] == api_monitor.STATE_DOWN
    assert "беде" in _по_ключу(res.json()["items"], key)["state_note"]
    # Встроенный через ручку — тем же путём, по имени провайдера.
    respx.post(f"{GW}/check/speller").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": {"ok": True, "status": 200, "ms": 5, "error": None}}
        )
    )
    res = await client.post("/api/v1/settings/apis/speller/check", headers=hdr(tokens["admin"]))
    assert res.status_code == 200 and res.json()["checked"]["status"] == 200
    assert (
        await client.post("/api/v1/settings/apis/нет-такого/check", headers=hdr(tokens["admin"]))
    ).status_code == 404


def test_ключ_из_имени() -> None:
    assert api_monitor.make_key("2ГИС Places (демо)") == "2gis-places-demo"
    assert api_monitor.make_key("   ") == "api"
    assert len(api_monitor.make_key("а" * 100)) <= 40
