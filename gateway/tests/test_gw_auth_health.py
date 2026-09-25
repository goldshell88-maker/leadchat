"""Каркас шлюза: /health открыт, остальное — по токену, пустой токен = 503."""

from __future__ import annotations

import httpx
import pytest

from leadchat_gateway.config import settings

pytestmark = pytest.mark.anyio


async def test_health_без_токена(gw_anon: httpx.AsyncClient) -> None:
    r = await gw_anon.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True and r.json()["version"]


async def test_status_только_по_токену(gw: httpx.AsyncClient, gw_anon: httpx.AsyncClient) -> None:
    assert (await gw_anon.get("/status")).status_code == 401
    assert (
        await gw_anon.get("/status", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401
    assert (await gw_anon.get("/status", headers={"Authorization": "Basic x"})).status_code == 401
    r = await gw.get("/status")
    assert r.status_code == 200
    данные = r.json()
    assert данные["ok"] is True
    провайдеры = данные["providers"]
    assert set(провайдеры) == {
        "dadata",
        "nominatim",
        "yandex_geocoder",
        "yandex_suggest",
        "ahunter",
        "speller",
        "openrouter",
        "groq",
        "mistral",
        "anthropic",
    }
    # Ключей в тестах нет; у тех, кому ключ не нужен, — None.
    assert провайдеры["dadata"]["key_present"] is False
    assert провайдеры["nominatim"]["key_present"] is None
    assert провайдеры["ahunter"]["key_present"] is None
    # Значения ключей наружу не уходят — только факт наличия и адрес.
    assert set(провайдеры["dadata"]) == {"key_present", "base_url"}
    assert провайдеры["openrouter"]["base_url"] == "https://openrouter.ai/api/v1"


async def test_ключ_виден_в_status_как_факт(gw: httpx.AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "dadata_api_key", "СЕКРЕТ-1")
    monkeypatch.setattr(settings, "groq_api_key", "СЕКРЕТ-2")
    r = await gw.get("/status")
    assert r.json()["providers"]["dadata"]["key_present"] is True
    assert r.json()["providers"]["groq"]["key_present"] is True
    assert "СЕКРЕТ" not in r.text


async def test_пустой_токен_шлюза_это_503(gw_anon: httpx.AsyncClient, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(settings, "gateway_token", "")
    r = await gw_anon.get("/status", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 503
    assert (await gw_anon.get("/health")).status_code == 200


def test_версия_без_файла_это_dev(monkeypatch, tmp_path) -> None:  # noqa: ANN001
    import leadchat_gateway as pkg

    monkeypatch.setattr(pkg, "_VERSION_FILE", tmp_path / "нет")
    assert pkg.version() == "dev"
    (tmp_path / "VERSION").write_text("abc1234\n", encoding="utf-8")
    monkeypatch.setattr(pkg, "_VERSION_FILE", tmp_path / "VERSION")
    assert pkg.version() == "abc1234"
