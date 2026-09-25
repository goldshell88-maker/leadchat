"""«Обновить токен», «Включить» и «Переподключить» говорят правду о причине (проверка 24.09).

Обновление отдавало наружу один bool, и ручки гадали по статусу: у активного
канала на своих ключах недоступность Авито читалась «Токен уже обновляется —
подождите несколько секунд», у канала в needs_reauth — «Авито отозвал доступ»,
кнопка центра уведомлений советовала проверить, не удалено ли приложение.
Человек шёл чинить то, что не сломано, или бесконечно ждал «несколько секунд».

ДИВЕРСИИ: вернуть ветку «статус needs_reauth → отозвал» вместо исхода —
краснеет «needs_reauth и таймаут»; отдать REFRESH_BUSY на сбой ключей —
краснеет «503 Авито».
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import respx

from app.core.config import settings
from app.models import AvitoAccount
from app.services import avito_accounts, crypto

pytestmark = pytest.mark.anyio

TOKEN_URL = f"{settings.avito_api_base}/token"


async def _own_keys_account(db_sessionmaker: Any, *, status: str = "active") -> AvitoAccount:
    async with db_sessionmaker() as s:
        account = AvitoAccount(
            title="Ключи",
            avito_user_id=424242,
            access_token_enc=crypto.encrypt_token("old-access"),
            refresh_token_enc=b"",
            token_expires_at=datetime.now(UTC) + timedelta(hours=1),
            status=status,
            webhook_secret="whsec",
            client_id="cid",
            client_secret_enc=crypto.encrypt_token("csecret"),
        )
        s.add(account)
        await s.commit()
        await s.refresh(account)
        return account


async def _refresh(client: Any, tokens: Any, account: AvitoAccount) -> httpx.Response:
    return await client.post(
        f"/api/v1/avito-accounts/{account.id}/refresh-token",
        headers={"Authorization": f"Bearer {tokens['admin']}"},
    )


@respx.mock
async def test_keys_channel_and_avito_down_says_avito_did_not_answer(
    client: Any, tokens: Any, db_sessionmaker: Any
) -> None:
    account = await _own_keys_account(db_sessionmaker)
    respx.post(TOKEN_URL).mock(side_effect=httpx.ConnectError("boom"))

    r = await _refresh(client, tokens, account)

    assert r.status_code == 502, r.text
    assert r.json()["error"]["details"]["reason"] == "avito_unavailable"
    assert "Авито не ответил" in r.json()["error"]["message"]


@respx.mock
async def test_keys_channel_and_avito_503_is_not_a_parallel_refresh(
    client: Any, tokens: Any, db_sessionmaker: Any
) -> None:
    account = await _own_keys_account(db_sessionmaker)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))

    r = await _refresh(client, tokens, account)

    assert r.status_code == 502, r.text
    assert r.json()["error"]["details"]["reason"] == "avito_unavailable"


@respx.mock
async def test_keys_channel_in_needs_reauth_and_timeout_is_not_a_revocation(
    client: Any, tokens: Any, db_sessionmaker: Any
) -> None:
    account = await _own_keys_account(db_sessionmaker, status="needs_reauth")
    respx.post(TOKEN_URL).mock(side_effect=httpx.ReadTimeout("slow"))

    r = await _refresh(client, tokens, account)

    assert r.status_code == 502, r.text
    assert "отозвал" not in r.json()["error"]["message"]


@respx.mock
async def test_keys_really_rejected_is_named_so(
    client: Any, tokens: Any, db_sessionmaker: Any
) -> None:
    account = await _own_keys_account(db_sessionmaker)
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(401))

    r = await _refresh(client, tokens, account)

    assert r.status_code == 422, r.text
    assert r.json()["error"]["details"]["reason"] == "needs_reauth"
    assert "не принял ключи приложения" in r.json()["error"]["message"]


@respx.mock
async def test_the_notification_button_names_the_real_cause(
    db_sessionmaker: Any, redis: Any, make_user: Any
) -> None:
    account = await _own_keys_account(db_sessionmaker, status="needs_reauth")
    admin = await make_user("admin-reauth@leadchat.test", role="admin", full_name="Админ")
    respx.post(TOKEN_URL).mock(return_value=httpx.Response(503))

    async with db_sessionmaker() as s:
        result = await avito_accounts.reconnect_account_action(
            s, redis, entity_id=str(account.id), actor=admin
        )

    assert "Авито не ответил" in result["message"]
    assert "удалено" not in result["message"]
