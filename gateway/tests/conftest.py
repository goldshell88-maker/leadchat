"""Тесты шлюза гоняются из корня LeadChat: `uv run pytest gateway/tests -q`
(корневой pyproject добавляет `gateway` в pythonpath). Сеть — только respx
на адреса провайдеров; настоящих походов нет."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from leadchat_gateway.config import settings

TOKEN = "test-token"


@pytest.fixture(autouse=True)
def _чистое_окружение(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ключи из окружения разработчика не должны просачиваться в тесты."""
    for поле in type(settings).model_fields:
        if поле.endswith(("_key", "_token", "_contact", "_models")):
            monkeypatch.setattr(settings, поле, "")
    monkeypatch.setattr(settings, "gateway_token", TOKEN)


@pytest.fixture
async def gw() -> AsyncIterator[httpx.AsyncClient]:
    """Клиент к приложению шлюза с верным токеном."""
    from leadchat_gateway.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gw",
        headers={"Authorization": f"Bearer {TOKEN}"},
    ) as client:
        yield client


@pytest.fixture
async def gw_anon() -> AsyncIterator[httpx.AsyncClient]:
    """Клиент без токена."""
    from leadchat_gateway.main import app

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gw"
    ) as client:
        yield client
