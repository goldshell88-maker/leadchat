"""Авто-«отошёл» простаивающей вкладки не перебивает работающее устройство.

Владелец 19.09: работал с телефона, компьютер каждые две минуты слал «отошёл»
(авто-«отошёл» по тишине), сторож «недоступен 15 минут» отбирал диалоги у
человека в сети. Память об активности у вкладки — своя на браузер, у сервера —
на человека: «на месте» с любого устройства помнится 30 минут, и авто-«отошёл»
(флаг `auto`) за это время не принимается. Ручной «Отошёл» — как прежде.
"""

from __future__ import annotations

import pytest

from app.ws import presence

pytestmark = pytest.mark.anyio

PATH = "/api/v1/presence"


def _auth(tokens: dict[str, str], role: str = "admin") -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[role]}"}


async def _status(client, tokens) -> str:  # noqa: ANN001
    r = await client.get(PATH, headers=_auth(tokens))
    assert r.status_code == 200, r.text
    return r.json()["status"]


async def test_авто_отошёл_не_перебивает_недавнее_на_месте(client, tokens):  # noqa: ANN001
    # Телефон: «на месте».
    r = await client.put(PATH, headers=_auth(tokens), json={"status": "online"})
    assert r.status_code == 200 and r.json()["status"] == "online"
    # Компьютер молчал 30 минут и шлёт авто-«отошёл» — сервер отвечает «на месте»
    # и статус не меняет.
    r = await client.put(PATH, headers=_auth(tokens), json={"status": "away", "auto": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "online"
    assert await _status(client, tokens) == "online"


async def test_ручной_отошёл_принимается_всегда(client, tokens):  # noqa: ANN001
    await client.put(PATH, headers=_auth(tokens), json={"status": "online"})
    r = await client.put(PATH, headers=_auth(tokens), json={"status": "away"})
    assert r.status_code == 200 and r.json()["status"] == "away"
    assert await _status(client, tokens) == "away"


async def test_авто_отошёл_принимается_когда_активности_давно_не_было(
    client, tokens, redis, monkeypatch
):  # noqa: ANN001
    await client.put(PATH, headers=_auth(tokens), json={"status": "online"})
    # Активность «состарилась» — окно прошло.
    monkeypatch.setattr(presence, "ACTIVE_WINDOW_SECONDS", 0)
    r = await client.put(PATH, headers=_auth(tokens), json={"status": "away", "auto": True})
    assert r.status_code == 200 and r.json()["status"] == "away"
    assert await _status(client, tokens) == "away"


async def test_диверсия_без_флага_auto_поведение_прежнее(client, tokens):  # noqa: ANN001
    """Старый фронт без `auto` — как до 19.09: любой «отошёл» принимается."""
    await client.put(PATH, headers=_auth(tokens), json={"status": "online"})
    r = await client.put(PATH, headers=_auth(tokens), json={"status": "away", "auto": False})
    assert r.json()["status"] == "away"
