"""Клиент шлюза внешних API на Амстердаме (docs/46-ШЛЮЗ-API.md).

Единственное место в LeadChat, откуда уходят походы к DaData, OSM, Яндексу,
Ahunter, Спеллеру, читателям адреса и Anthropic. Кода самих провайдеров здесь
нет — он в `gateway/leadchat_gateway/` и живёт на Амстердаме вместе с ключами.
Обёртки в `app/integrations/*.py` сохраняют прежние имена функций и зовут
`call()`; шлюз лёг — все помощники молчат (решение владельца 16.09).

ТРИ ПРАВИЛА.

1. Шлюз отвечает всегда JSON: HTTP 200 + `{"ok": true, …}` или `{"ok": false,
   "kind", "status", "detail"}` — отказ ПРОВАЙДЕРА. Всё остальное (обрыв,
   5xx, 401, не-JSON) — отказ ШЛЮЗА, и обёртки читают его как «network»:
   те же кулдауны, что при «карта не отвечает».
2. `kind: "no_key"` — ключа у шлюза нет. Снимок `known_keys` помечает
   провайдера, и `enabled()` провайдера становится False до следующего
   `refresh_status()`. Пока снимка нет вовсе — считаем, что ключи есть:
   первый же поход всё расскажет.
3. В лог — только путь, тип ошибки и статус. Тела запросов (там адрес
   клиента) и токен не пишутся никогда.
"""

from __future__ import annotations

import math
import time
from typing import Any

import httpx
import structlog

from app.core.config import settings

log = structlog.get_logger("app.integrations.gateway")

#: Таймаут по умолчанию для похода в шлюз; обёртки задают свой (таймаут
#: провайдера + запас на дорогу по мосту).
DEFAULT_TIMEOUT_SEC = 8.0
STATUS_TTL_SEC = 60.0

#: Снимок `/status`: провайдер → есть ли ключ (None = ключ не нужен).
#: `None` целиком — снимка ещё не было. Тесты подменяют напрямую.
known_keys: dict[str, bool | None] | None = None
_status_at: float = -math.inf
#: Провайдеры, ответившие `no_key` до или мимо снимка. Отдельно от снимка:
#: завести из одного ответа «снимок с одним провайдером» значило бы выключить
#: всех остальных. Чистится удачным `refresh_status()`.
_без_ключа: set[str] = set()
#: Почему последний `refresh_status()` не удался (`kind`), None — удался.
#: Монитор показывает это в строке шлюза: снимок мог остаться от живого
#: шлюза, а сам он уже лежит.
last_status_error: str | None = None


class GatewayError(Exception):
    """Шлюз или провайдер за ним не ответили по-человечески.

    `kind` — как у `GeocodeError`: `network` (обрыв, таймаут, 5xx шлюза или
    провайдера), `blocked`, `bad_response`, `no_key`, `exhausted`, `auth`
    (401: провайдер отверг ключ или шлюз — токен). `status` — код провайдера,
    если он был. Текст без URL и тела.
    """

    def __init__(
        self,
        kind: str,
        status: int | None = None,
        detail: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.kind = kind
        self.status = status
        self.detail = detail
        #: Остальные поля отказа провайдера (например, `attempts` у читателей).
        self.extra: dict[str, Any] = extra or {}
        super().__init__(f"gateway: {kind}" + (f" ({status})" if status else ""))


#: Отказ шлюза → `kind` для `GeocodeError`/`OpenRouterError`. Отказ провайдера
#: (`network`, `blocked`, `bad_response`, `exhausted`) — как есть; `no_key` —
#: «закрыто», как раньше при пустом ключе в env; `auth` — это 401 САМОГО
#: шлюза (токен), то есть отказ шлюза, а не провайдера: «сеть», те же
#: кулдауны, что при «шлюз не отвечает», и никакого бана карты на сутки.
_ВИД_ОТКАЗА = {
    "network": "network",
    "blocked": "blocked",
    "bad_response": "bad_response",
    "exhausted": "exhausted",
    "no_key": "blocked",
    "auth": "network",
}


def kind_for(exc: GatewayError) -> str:
    """`kind` ошибки LeadChat по отказу шлюза (см. `_ВИД_ОТКАЗА`)."""
    return _ВИД_ОТКАЗА.get(exc.kind, "network")


def enabled() -> bool:
    """Шлюз настроен: есть адрес и токен. Без них помощники выключены целиком."""
    return bool(settings.gateway_url.strip() and settings.gateway_token.strip())


def key_present(provider: str) -> bool:
    """Есть ли у шлюза ключ провайдера по последнему снимку `/status`.

    Снимка нет вовсе — True (первый поход покажет). Снимок есть, а
    провайдера в нём нет — False: `/status` перечисляет всех, и пустой
    снимок значит «ни одного ключа» (так его и задаёт conftest тестов).
    Провайдер, которому ключ не нужен (`None` в снимке), — True.
    """
    if provider in _без_ключа:
        return False
    if known_keys is None:
        return True
    if provider not in known_keys:
        return False
    значение = known_keys[provider]
    return True if значение is None else bool(значение)


def _base() -> str:
    return settings.gateway_url.strip().rstrip("/")


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.gateway_token.strip()}"}


async def refresh_status(*, client: httpx.AsyncClient | None = None, force: bool = False) -> None:
    """Обновить снимок `/status`, если он старше `STATUS_TTL_SEC`.

    Ошибки не поднимаются: снимок просто остаётся прежним — решать «есть
    ли ключ» по обрыву связи нельзя, это два разных состояния.
    """
    global known_keys, _status_at, last_status_error
    if not enabled():
        return
    if not force and time.monotonic() - _status_at < STATUS_TTL_SEC:
        return
    # Неудачу тоже помним до конца TTL: иначе при лежащем шлюзе каждый
    # обзор монитора и каждая задача воркера ждали бы connect-таймаут заново.
    _status_at = time.monotonic()
    try:
        данные = await call(
            "/status", None, method="GET", timeout=DEFAULT_TIMEOUT_SEC, client=client
        )
    except GatewayError as exc:
        log.warning("gateway.status_failed", kind=exc.kind, status=exc.status)
        last_status_error = exc.kind
        return
    провайдеры = данные.get("providers")
    if not isinstance(провайдеры, dict):
        log.warning("gateway.status_bad_shape")
        last_status_error = "bad_response"
        return
    снимок: dict[str, bool | None] = {}
    for имя, строка in провайдеры.items():
        if isinstance(строка, dict):
            ключ = строка.get("key_present")
            снимок[str(имя)] = None if ключ is None else bool(ключ)
    known_keys = снимок
    _без_ключа.clear()
    last_status_error = None


def mark_no_key(provider: str | None) -> None:
    """Запомнить, что у шлюза нет ключа провайдера, — до следующего снимка."""
    if not provider:
        return
    _без_ключа.add(provider)
    if known_keys is not None and provider in known_keys:
        known_keys[provider] = False


async def call(
    path: str,
    payload: dict[str, Any] | None,
    *,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    client: httpx.AsyncClient | None = None,
    method: str = "POST",
    provider: str | None = None,
) -> dict[str, Any]:
    """Один поход в шлюз. Возвращает тело `{"ok": true, …}` целиком.

    `provider` — чьё имя пометить в снимке при `no_key`. `client` — для
    тестов (транспорт к шлюзу); в бою свой клиент на каждый поход, как у
    клиента лид-бота.
    """
    if not enabled():
        raise GatewayError("network", None, "шлюз не настроен")
    url = _base() + path
    try:
        if client is None:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(timeout, connect=min(3.0, timeout)),
                headers=_headers(),
                follow_redirects=False,
            ) as own:
                resp = await own.request(method, url, json=payload)
        else:
            resp = await client.request(method, url, json=payload, headers=_headers())
    except httpx.HTTPError as exc:
        # Тип исключения — да, текст — нет: в тексте httpx есть URL.
        log.warning("gateway.unreachable", path=path, error=type(exc).__name__)
        raise GatewayError("network", None, type(exc).__name__) from exc
    if resp.status_code == 401:
        log.warning("gateway.rejected", path=path)
        raise GatewayError("auth", 401, "шлюз не принял токен")
    if resp.status_code == 503:
        log.warning("gateway.not_configured", path=path)
        raise GatewayError("network", 503, "шлюз не настроен (нет токена)")
    if resp.status_code != 200:
        log.warning("gateway.bad_status", path=path, status=resp.status_code)
        raise GatewayError("network", resp.status_code, "шлюз ответил не 200")
    try:
        данные = resp.json()
    except ValueError as exc:
        log.warning("gateway.bad_json", path=path)
        raise GatewayError("bad_response", 200, "ответ шлюза не JSON") from exc
    if not isinstance(данные, dict) or "ok" not in данные:
        log.warning("gateway.bad_shape", path=path)
        raise GatewayError("bad_response", 200, "ответ шлюза не той формы")
    if данные["ok"] is True:
        return данные
    kind = str(данные.get("kind") or "bad_response")
    status = данные.get("status")
    if kind == "no_key":
        mark_no_key(provider)
    log.info(
        "gateway.provider_failed",
        path=path,
        provider=provider,
        kind=kind,
        status=status,
    )
    лишнее = {к: з for к, з in данные.items() if к not in ("ok", "kind", "status", "detail")}
    raise GatewayError(
        kind,
        status if isinstance(status, int) else None,
        str(данные.get("detail") or ""),
        extra=лишнее,
    )
