"""Проверка доступности: встроенные провайдеры и произвольные адреса.

Переехала из монитора LeadChat (`app/services/api_monitor.py`, 16.09): ключи
и сеть теперь здесь, поэтому и проба идёт отсюда — тем же адресом и ключом,
которыми ходит работа. LeadChat лишь просит `POST /check/{provider}` и
показывает итог; сам он наружу больше не ходит.

Итог пробы — `{ok, status, ms, error}`, слова — для администратора, не для
разработчика. Жив — ответ 2xx/3xx или 405 (метод не тот, но сервер на месте)
и любой прочий 4xx (пустой запрос не по вкусу, но сервер отвечает); 401 —
«ключ отвергнут», 403 — «доступ закрыт»; когда ключа на шлюзе нет вовсе,
401/403 — «ключ не задан»: серверу нечего отвергать. По переадресациям не
идём: Location мог бы увести внутрь.

`/check/url` — адрес из формы владельца LeadChat: сторож `_чужой_адрес` не
пускает на localhost, во внутренние сети и, отдельно, в `10.10.0.0/24` —
WireGuard, где живём мы сами и прод. Адреса провайдеров — из настроек, их
задал инженер; сторож их не касается.

Ни URL, ни ключ, ни тело ответа в лог не пишутся — только имя провайдера,
`ok`, статус и время.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx
import structlog

from leadchat_gateway.config import settings
from leadchat_gateway.routes import status as status_route

log = structlog.get_logger("leadchat_gateway.check")

PROBE_TIMEOUT = httpx.Timeout(6.0, connect=3.0)
USER_AGENT = "LeadChat/1.0 (api-monitor)"

#: Подсеть WireGuard между продом LeadChat и Амстердамом. `ip.is_global` и так
#: её не пропустит (10/8 — частная), но явный запрет переживёт любое изменение
#: семантики `is_global` в stdlib: сюда стучаться из монитора нельзя никогда.
WIREGUARD_NET = ipaddress.ip_network("10.10.0.0/24")

#: Имена исключений httpx — словами: их читает администратор, не разработчик.
_ОШИБКИ = {
    "ConnectTimeout": "нет ответа на соединение (таймаут)",
    "ReadTimeout": "сервер не ответил вовремя",
    "ConnectError": "не удалось соединиться",
    "RemoteProtocolError": "сервер оборвал ответ",
    "TooManyRedirects": "слишком много переадресаций",
    "InvalidURL": "адрес не разбирается",
    "UnsupportedProtocol": "протокол не поддерживается",
}


@dataclass(frozen=True, slots=True)
class Spec:
    """Как проверять провайдера: адрес, заголовки с ключом, метод, тело.

    `method`/`json` — читатели адреса проверяются POST'ом с пустым телом в
    `chat/completions`: пустое тело сервер отбивает 400/422 до тарификации,
    а ключ при этом уже проверен. `key_missing` — ключа в окружении шлюза
    нет: 401/403 тогда не «ключ отвергнут», а «ключа нет».
    """

    url: str
    headers: dict[str, str] = field(default_factory=dict)
    method: str = "GET"
    json: dict[str, Any] | None = None
    key_missing: bool = False


def _ключ(поле: str) -> str:
    return (getattr(settings, поле, "") or "").strip()


def _user_agent_osm() -> str:
    # Политика OSM просит контакт в User-Agent — тот же, что у рабочих запросов.
    контакт = settings.nominatim_contact.strip()
    return f"LeadChat/1.0 ({контакт})" if контакт else "LeadChat/1.0"


def spec_for(provider: str) -> Spec | None:
    """Проба встроенного провайдера тем же адресом и ключом, что и работа;
    неизвестное имя — None."""
    if provider == "dadata":
        ключ = _ключ("dadata_api_key")
        return Spec(
            url=status_route.base_url("dadata"),
            headers={"Authorization": f"Token {ключ}"} if ключ else {},
            key_missing=not ключ,
        )
    if provider == "nominatim":
        return Spec(
            url="https://nominatim.openstreetmap.org/status",
            headers={"User-Agent": _user_agent_osm()},
        )
    if provider == "yandex_geocoder":
        ключ = _ключ("yandex_geocoder_key")
        база = status_route.base_url("yandex_geocoder")
        return Spec(
            url=f"{база}?apikey={ключ}&format=json&geocode=Москва"
            if ключ
            else f"{база}?format=json&geocode=Москва",
            key_missing=not ключ,
        )
    if provider == "yandex_suggest":
        ключ = _ключ("yandex_suggest_key")
        база = status_route.base_url("yandex_suggest")
        return Spec(
            url=f"{база}?apikey={ключ}&text=Москва" if ключ else f"{база}?text=Москва",
            key_missing=not ключ,
        )
    if provider == "ahunter":
        return Spec(url=f"{status_route.base_url('ahunter')}?output=json;query=Москва")
    if provider == "speller":
        return Spec(url=f"{status_route.base_url('speller')}?text=тест&lang=ru")
    if provider in ("openrouter", "groq", "mistral"):
        ключ = _ключ(f"{provider}_api_key")
        return Spec(
            url=f"{status_route.base_url(provider)}/chat/completions",
            headers={"Authorization": f"Bearer {ключ}"} if ключ else {},
            method="POST",
            json={},
            key_missing=not ключ,
        )
    if provider == "anthropic":
        ключ = _ключ("anthropic_api_key")
        return Spec(
            url=f"{status_route.base_url('anthropic')}/v1/models",
            headers={"x-api-key": ключ, "anthropic-version": "2023-06-01"} if ключ else {},
            key_missing=not ключ,
        )
    return None


async def probe_provider(provider: str) -> dict[str, Any] | None:
    """Итог пробы встроенного провайдера; неизвестное имя — None."""
    spec = spec_for(provider)
    if spec is None:
        return None
    итог = await probe(
        spec.url,
        headers=spec.headers,
        method=spec.method,
        json=spec.json,
        key_missing=spec.key_missing,
    )
    log.info(
        "check.provider", provider=provider, ok=итог["ok"], status=итог["status"], ms=итог["ms"]
    )
    return итог


#: Сколько ждём разрешения имени из формы: системный резолвер может думать
#: десятки секунд, а LeadChat ждёт ответ шлюза около восьми.
DNS_TIMEOUT_SEC = 4.0


async def probe_url(url: str) -> dict[str, Any]:
    """Итог пробы адреса из формы владельца — через сторожа `_чужой_адрес`.

    Идём на тот IP, который проверил сторож, а не на имя: иначе имя с TTL 0
    могло бы ответить сторожу публичным адресом, а походу — 10.10.0.1
    (DNS-rebinding, ревью 18.09). Имя остаётся в `Host` и SNI.
    """
    url = (url or "").strip()
    if not url:
        return {"ok": False, "status": None, "ms": None, "error": "адрес не задан"}
    try:
        # Разрешение имени — блокирующий вызов, в поток и с потолком.
        почему, ip = await asyncio.wait_for(
            asyncio.to_thread(_чужой_адрес, url), timeout=DNS_TIMEOUT_SEC
        )
    except TimeoutError:
        return {"ok": False, "status": None, "ms": None, "error": "имя не разрешилось вовремя"}
    if почему is not None or ip is None:
        return {"ok": False, "status": None, "ms": None, "error": почему or "внутренний адрес"}
    части = urlsplit(url)
    хост = части.hostname or ""
    порт = f":{части.port}" if части.port else ""
    адрес_ip = f"[{ip}]" if ":" in ip else ip
    приколотый = части._replace(netloc=f"{адрес_ip}{порт}").geturl()
    итог = await probe(
        приколотый,
        headers={"Host": части.netloc},
        sni_hostname=хост if части.scheme == "https" else None,
    )
    log.info("check.url", ok=итог["ok"], status=итог["status"], ms=итог["ms"])
    return итог


async def probe(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    json: dict[str, Any] | None = None,
    key_missing: bool = False,
    sni_hostname: str | None = None,
) -> dict[str, Any]:
    """Сходить по адресу один раз и рассказать словами, что вышло.

    Тело ответа не читается: пробе нужен только статус, а чужой адрес мог бы
    отдать гигабайт. `sni_hostname` — имя для TLS, когда в URL стоит IP.
    """
    начало = time.monotonic()
    extensions = {"sni_hostname": sni_hostname} if sni_hostname else None
    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT,
            headers={"User-Agent": USER_AGENT, **(headers or {})},
            follow_redirects=False,
        ) as own:
            запрос = own.build_request(method, url, json=json, extensions=extensions)
            resp = await own.send(запрос, stream=True)
            await resp.aclose()
    except (httpx.HTTPError, httpx.InvalidURL) as exc:
        мс = round((time.monotonic() - начало) * 1000)
        имя = type(exc).__name__
        return {"ok": False, "status": None, "ms": мс, "error": _ОШИБКИ.get(имя, имя)}
    итог = _итог(resp.status_code, round((time.monotonic() - начало) * 1000))
    if key_missing and resp.status_code in (401, 403):
        итог["error"] = f"{resp.status_code}: ключ на сервере не задан"
    elif method == "POST" and resp.status_code == 404:
        # Путь пробы известен — тот, которым ходит работа; живой читатель на
        # него 404 не отвечает никогда (400/401/422). Значит, адрес без
        # /api/v1 или отвечает что-то мимо пути — работа на таком падает, и
        # «жив» здесь было бы ложью.
        итог["ok"] = False
        итог["error"] = "404: путь не найден — адрес без /api/v1 или не тот сервер"
    return итог


def _итог(status: int, мс: int) -> dict[str, Any]:
    if status < 400 or status == 405:
        return {"ok": True, "status": status, "ms": мс, "error": None}
    if status == 401:
        ошибка = "401: ключ отвергнут"
    elif status == 403:
        ошибка = "403: доступ закрыт (ключ, тариф или страна)"
    elif status == 429:
        ошибка = "429: лимит запросов выбран"
    elif status >= 500:
        ошибка = f"{status}: сервер в беде"
    else:
        # Сервер жив, просто пустой запрос ему не по вкусу (400/404/422).
        return {"ok": True, "status": status, "ms": мс, "error": None}
    return {"ok": False, "status": status, "ms": мс, "error": ошибка}


def _чужой_адрес(url: str) -> tuple[str | None, str | None]:
    """(почему нельзя, проверенный IP). Нельзя — не http(s), не разбирается
    или ведёт на свой/внутренний хост.

    Проба идёт с Амстердама, где рядом лид-бот и WireGuard до прода — адрес
    из формы не должен вести ни в них, ни в localhost. Проверяются и
    буквальный IP, и всё, во что разрешается имя; поход потом идёт на
    возвращённый IP, а не на имя.
    """
    try:
        части = urlsplit(url)
        порт = части.port  # ValueError: порт вне 0–65535, «[::1» без скобки
        host = части.hostname
    except (ValueError, UnicodeError):
        return "адрес не разбирается: проверьте порт и скобки", None
    if части.scheme not in ("http", "https") or not host:
        return "адрес должен начинаться с http:// или https://", None
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        return "внутренний адрес", None
    try:
        адреса = [
            info[4][0] for info in socket.getaddrinfo(host, порт or 443, proto=socket.IPPROTO_TCP)
        ]
    except (socket.gaierror, UnicodeError):
        return "имя не разрешается", None
    первый: str | None = None
    for a in адреса:
        try:
            ip = ipaddress.ip_address(str(a))
        except ValueError:
            continue
        if ip in WIREGUARD_NET or not ip.is_global:
            return "внутренний адрес", None
        первый = первый or str(ip)
    if первый is None:
        return "имя не разрешается", None
    return None, первый
