"""«Взяли тысячу» — состояние канала, а не предупреждение (замер боя 05.09).

ЧТО БЫЛО. `avito.chats_offset_limit` писался уровнем warning. У 17 каналов
непрочитанных больше тысячи ПОСТОЯННО, и строка повторялась каждый прогон
сверки, раз в пять минут, с одними и теми же полями taken=1000 limit=1000:
около 4 900 одинаковых предупреждений в сутки. Настоящие (voice.failed —
3 % потока) в них тонули.

ПОЧЕМУ INFO, А НЕ «WARNING РАЗ В ЧАС». Раз в час — это 17 × 24 = 408
одинаковых строк в сутки, всё равно втрое больше настоящих; плюс ключ в Redis
и лишний заход ради уровня одной строки журнала. Действия у человека по ней
нет: потолок наш и намеренный. Факт не пропадает — те же поля и тот же event,
а INFO в бою включён.

⚠ ДИВЕРСИЯ: вернуть `log.warning` в `_LiveAdapter.fetch_chats`
(workers/reconciliation.py) или в `AvitoAdapter.fetch_chats` (adapter.py) —
краснеет соответствующий вариант. Сторож не пустой: он же проверяет, что
потолок ДОСТИГНУТ (отдано ровно 1000 чатов и строка есть), иначе зеленел бы и
без строки вовсе.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog

from app.integrations.avito.adapter import CHATS_MAX_OFFSET, AvitoAdapter
from app.models import AvitoAccount
from app.workers import reconciliation as rec

ACCOUNT_UID = 772600
CLIENT_UID = 999601
EVENT = "avito.chats_offset_limit"


def _account() -> AvitoAccount:
    return AvitoAccount(
        id=uuid.uuid4(),
        title="LP-Потолок",
        avito_user_id=ACCOUNT_UID,
        access_token_enc=b"enc",
        refresh_token_enc=b"enc",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        status="active",
        webhook_secret="whsec",
    )


def _raw_chat(i: int) -> dict[str, Any]:
    return {
        "id": f"u2i-{i}",
        "users": [{"id": ACCOUNT_UID, "name": "Мы"}, {"id": CLIENT_UID, "name": "Клиент"}],
    }


@pytest.fixture(params=["сверка", "адаптер"])
def adapter(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Оба обхода списка чатов — сверка и штатный адаптер — над фейковым
    транспортом, в котором чатов больше потолка на страницу.

    Подменяется метод клиента, а не `_call`: маршрут вызова остаётся боевым.
    """
    obj: Any = rec._LiveAdapter() if request.param == "сверка" else AvitoAdapter()
    chats = [_raw_chat(i) for i in range(CHATS_MAX_OFFSET + 100)]

    async def _get_chats(
        _token: str, _uid: int, *, offset: int = 0, limit: int = 100, **_kw: Any
    ) -> list[dict[str, Any]]:
        return chats[offset : offset + limit]

    monkeypatch.setattr(obj._client, "get_chats", _get_chats)
    monkeypatch.setattr("app.services.crypto.decrypt_token", lambda _b: "token")
    return obj


async def test_hitting_the_ceiling_is_info_not_warning(adapter: Any) -> None:
    with structlog.testing.capture_logs() as logs:
        chats = [chat async for chat in adapter.fetch_chats(_account())]

    hits = [entry for entry in logs if entry["event"] == EVENT]
    assert len(chats) == CHATS_MAX_OFFSET, "потолок не достигнут — сторож проверял бы пустоту"
    assert len(hits) == 1, "строка о потолке обязана быть: факт из журнала не пропадает"
    assert hits[0]["log_level"] == "info"
    assert hits[0]["taken"] == CHATS_MAX_OFFSET
