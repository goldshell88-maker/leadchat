"""Общий HTTP-слой провайдеров: клиент и каскад статусов.

Каскад один на всех (скопирован из прежних `app/integrations/*.py` LeadChat):
`httpx.HTTPError` → network; 401/403 → blocked; 429 и 5xx → network (повтор
уместен); любой другой не-200 → bad_response; JSON не разбирается →
bad_response (у OSM — blocked: HTML вместо JSON значит бан). По редиректам не
идём никогда: Location мог бы увести внутрь.
"""

from __future__ import annotations

from typing import Any

import httpx

from leadchat_gateway.errors import ProviderError

USER_AGENT = "LeadChat-gateway/1.0"


def client(timeout: httpx.Timeout, headers: dict[str, str] | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=timeout,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
        follow_redirects=False,
    )


def check_status(resp: httpx.Response, *, html_is_blocked: bool = False) -> None:
    """Поднять `ProviderError` по статусу; 200 — тишина."""
    code = resp.status_code
    if code in (401, 403):
        raise ProviderError("blocked", code, "доступ закрыт")
    if code == 429 or code >= 500:
        raise ProviderError("network", code, "провайдер перегружен или в беде")
    if code != 200:
        raise ProviderError("bad_response", code, "неожиданный статус")
    if html_is_blocked:
        тип = resp.headers.get("content-type", "")
        if "json" not in тип and resp.text.lstrip().startswith("<"):
            raise ProviderError("blocked", code, "HTML вместо JSON")


def parse_json(resp: httpx.Response, *, html_is_blocked: bool = False) -> Any:
    try:
        return resp.json()
    except ValueError as exc:
        if html_is_blocked and resp.text.lstrip().startswith("<"):
            raise ProviderError("blocked", resp.status_code, "HTML вместо JSON") from exc
        raise ProviderError("bad_response", resp.status_code, "тело не JSON") from exc


def network_error(exc: httpx.HTTPError) -> ProviderError:
    # Тип исключения — да, текст — нет: в тексте httpx есть URL с адресом.
    return ProviderError("network", None, type(exc).__name__)
