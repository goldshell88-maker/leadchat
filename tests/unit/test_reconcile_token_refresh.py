"""Сверка не должна умирать от истёкшего токена.

НАЙДЕНО В ЛОГАХ БОЕВОЙ СИСТЕМЫ 12 августа:

    AvitoAuthError: Авито: токен не принят (403)
    app/workers/reconciliation.py:137 -> fetch_chats -> get_chats

Сверка — это страховка от НЕДОШЕДШИХ вебхуков. Она ходила в Авито напрямую,
с токеном, расшифрованным один раз на весь прогон, минуя обёртку `_call`, где
живут бюджет ограничителя, сон на 429 и ровно один авто-рефреш на 401/403.
Поэтому истёкший токен ронял прогон целиком: сверка переставала работать ровно
тогда, когда её единственная задача — догнать потерянное.

У Авито истёкший access-токен даёт 403, а не 401 — это разобрано и проверено в
`app/integrations/avito/errors.py`. То есть ветка авто-рефреша здесь не
теоретическая: это ровно пойманный случай.

Проверка ломанием: верните в `fetch_chats` прямой вызов `self._client.get_chats`
— падает `test_expired_token_is_refreshed_not_fatal`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.avito.errors import AvitoAuthError
from app.models import AvitoAccount
from app.workers import reconciliation as rec


def _account() -> AvitoAccount:
    return AvitoAccount(
        id=uuid.uuid4(),
        title="LP-Сверка",
        avito_user_id=771100,
        access_token_enc=b"enc",
        refresh_token_enc=b"enc",
        token_expires_at=datetime.now(UTC) + timedelta(hours=1),
        status="active",
        webhook_secret="whsec",
    )


async def test_expired_token_is_refreshed_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """Первый заход — 403, после обновления токена второй проходит."""
    account = _account()
    adapter = rec._LiveAdapter()

    calls: list[str] = []

    async def _get_chats(_token: str, _uid: int, **_kw: Any) -> list[dict[str, Any]]:
        calls.append("get_chats")
        if len(calls) == 1:
            raise AvitoAuthError(status=403)
        return []

    refreshed: list[str] = []

    async def _refresh(_account: AvitoAccount) -> None:
        refreshed.append("refreshed")

    monkeypatch.setattr(adapter._client, "get_chats", _get_chats)
    monkeypatch.setattr(adapter._adapter, "_refresh_detached", _refresh)
    # Расшифровка токена нас здесь не интересует — важен маршрут вызова.
    monkeypatch.setattr("app.services.crypto.decrypt_token", lambda _b: "token")

    chats = [c async for c in adapter.fetch_chats(account)]

    assert chats == []
    assert calls == ["get_chats", "get_chats"], "после 403 обязан быть повтор"
    assert refreshed == ["refreshed"], "обновление токена не сработало"


async def test_second_rejection_is_not_retried_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """Обновились и снова 403 — сдаёмся, а не крутим бесконечный цикл.

    Бесконечный повтор был бы хуже падения: он молча жёг бы лимит запросов к
    чужому API и не оставлял следа в журнале.
    """
    account = _account()
    adapter = rec._LiveAdapter()
    calls: list[str] = []

    async def _always_403(_token: str, _uid: int, **_kw: Any) -> list[dict[str, Any]]:
        calls.append("x")
        raise AvitoAuthError(status=403)

    async def _refresh(_account: AvitoAccount) -> None:
        return None

    monkeypatch.setattr(adapter._client, "get_chats", _always_403)
    monkeypatch.setattr(adapter._adapter, "_refresh_detached", _refresh)
    monkeypatch.setattr("app.services.crypto.decrypt_token", lambda _b: "token")

    with pytest.raises(AvitoAuthError):
        [c async for c in adapter.fetch_chats(account)]
    assert len(calls) == 2, "ровно одна попытка обновления, не больше"


async def test_history_goes_through_the_same_door(monkeypatch: pytest.MonkeyPatch) -> None:
    """Загрузка истории — тот же маршрут: она ходит в тот же чужой API."""
    account = _account()
    adapter = rec._LiveAdapter()
    calls: list[str] = []

    async def _messages(_token: str, _uid: int, _chat: str, **_kw: Any) -> list[dict[str, Any]]:
        calls.append("messages")
        if len(calls) == 1:
            raise AvitoAuthError(status=403)
        return []

    async def _refresh(_account: AvitoAccount) -> None:
        return None

    monkeypatch.setattr(adapter._client, "get_chat_messages", _messages)
    monkeypatch.setattr(adapter._adapter, "_refresh_detached", _refresh)
    monkeypatch.setattr("app.services.crypto.decrypt_token", lambda _b: "token")

    chat = type("Chat", (), {"external_chat_id": "u2i-1"})()
    events = [e async for e in adapter.fetch_history(account, chat)]

    assert events == []
    assert len(calls) == 2


def test_adapter_gets_the_session_and_redis_it_needs() -> None:
    """Без сессии и Redis авто-рефреш объявлен, но не работает.

    `_refresh_detached` при пустых зависимостях честно бросает AvitoAuthError —
    то есть повтор был бы, а обновления не было бы. Ровно такую «объявленную,
    но не работающую» механику проект уже ловил на уведомлении о мёртвом канале.
    """
    factory, redis = object(), object()
    adapter = rec.get_adapter({"db_session_factory": factory, "redis": redis})
    assert adapter._adapter._db_factory is factory
    assert adapter._adapter._redis is redis
