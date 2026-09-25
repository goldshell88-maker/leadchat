"""LeadChat клиентам сам не пишет: вопрос об адресе не включается.

Решение владельца 20.09, повторено 24.09: «сам LeadChat не должен ничего
спрашивать». Тумблер снят с экрана; здесь стережётся, что и запросом его не
включить, а выключенное состояние записывается как раньше.
"""

from __future__ import annotations

import pytest

from app.services import app_settings

pytestmark = pytest.mark.anyio


def _hdr(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_the_ask_cannot_be_switched_on(client, tokens, db) -> None:  # noqa: ANN001
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"ask_enabled": True},
        headers=_hdr(tokens["admin"]),
    )

    assert res.status_code == 400, res.text
    assert res.json()["error"]["details"]["fields"][0]["field"] == "ask_enabled"
    assert await app_settings.get(db, app_settings.ADDRESS_ASK_ENABLED) is False


async def test_switching_it_off_still_saves(client, tokens, db) -> None:  # noqa: ANN001
    res = await client.patch(
        "/api/v1/settings/address-detect",
        json={"ask_enabled": False},
        headers=_hdr(tokens["admin"]),
    )

    assert res.status_code == 200, res.text
    assert res.json()["ask_enabled"] is False
