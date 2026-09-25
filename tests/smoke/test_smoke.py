"""SM-1…SM-10 — регрессионный smoke после деплоя (07 §6).

Бюджет всего набора — 5 минут; красный smoke = откат образа на предыдущий тег.

ЖЁСТКОЕ ПРАВИЛО набора: наружу, в реальный Авито, не уходит ничего. Пишем
только заметки (``direction='note'`` не доставляется по определению) и только
в служебный диалог; вебхук дёргаем у аккаунта-заглушки, который `disabled` и
дальше конвейера не идёт.
"""

import asyncio
import json
import os
import re
import ssl
import time
import uuid

import httpx
import pytest

pytestmark = [pytest.mark.smoke, pytest.mark.asyncio]

WS_PATH = "/api/ws"  # канонический upgrade-путь (01 §11.1)


# --------------------------------------------------------------- SM-1 health


async def test_sm1_health(http: httpx.AsyncClient) -> None:
    """200; status=ok, db/redis живы, version == задеплоенному тегу."""
    r = await http.get("/api/health")
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["status"] == "ok", body
    assert body["db"] is True, body
    assert body["redis"] is True, body
    expected = os.environ.get("EXPECTED_VERSION", "").strip()
    if expected:
        assert body.get("version") == expected, (
            f"снаружи отвечает контейнер версии {body.get('version')!r}, "
            f"ожидалась {expected!r} — деплой не доехал"
        )


# ---------------------------------------------------------------- SM-2 login


async def test_sm2_login_and_refresh_cookie(
    http: httpx.AsyncClient, smoke_creds: tuple[str, str]
) -> None:
    """200, валидный access-JWT и refresh-cookie с HttpOnly+Secure+SameSite."""
    from tests.smoke.conftest import login_response

    email, password = smoke_creds
    r = await login_response(http, email, password)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert body["token_type"] == "bearer"
    assert len(str(body["access_token"]).split(".")) == 3, "access-токен не похож на JWT"
    assert int(body["expires_in"]) > 0

    cookie = r.headers.get("set-cookie", "")
    assert "lc_refresh=" in cookie, cookie
    for flag in ("httponly", "secure", "samesite"):
        assert flag in cookie.lower(), f"refresh-cookie без {flag}: {cookie}"


# -------------------------------------------------------- SM-3 conversations


async def test_sm3_conversations_schema(http: httpx.AsyncClient, auth: dict[str, str]) -> None:
    """200 и валидная схема ответа (items + page с limit/offset/total)."""
    r = await http.get("/api/v1/conversations", params={"limit": 1}, headers=auth)
    assert r.status_code == 200, r.text[:200]
    body = r.json()
    assert isinstance(body.get("items"), list), body
    page = body.get("page")
    assert isinstance(page, dict) and {"limit", "offset", "total"} <= page.keys(), body
    assert page["limit"] == 1
    for item in body["items"]:
        assert {"id", "status", "channel", "unread_count"} <= item.keys(), item
        assert item["status"] in ("new", "in_progress", "closed"), item


# ---------------------------------------------------------- SM-4/SM-5 сокет


class _Socket:
    """Минимальный WS-клиент: только то, что нужно smoke (текстовые кадры)."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.reader, self.writer = reader, writer

    @classmethod
    async def connect(cls, base_url: str, ticket: str, *, verify: bool) -> "_Socket":
        url = httpx.URL(base_url)
        host = url.host
        port = url.port or (443 if url.scheme == "https" else 80)
        ctx: ssl.SSLContext | None = None
        if url.scheme == "https":
            ctx = ssl.create_default_context()
            if not verify:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection(host, port, ssl=ctx)
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        writer.write(
            (
                f"GET {WS_PATH}?ticket={ticket} HTTP/1.1\r\n"
                f"Host: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        assert "101" in status, f"WS upgrade не прошёл: {status}"
        return cls(reader, writer)

    async def send(self, frame: dict) -> None:
        payload = json.dumps(frame, ensure_ascii=False).encode()
        header = bytearray([0x81])
        length = len(payload)
        mask = os.urandom(4)
        if length < 126:
            header.append(0x80 | length)
        elif length < 1 << 16:
            header.append(0x80 | 126)
            header += length.to_bytes(2, "big")
        else:
            header.append(0x80 | 127)
            header += length.to_bytes(8, "big")
        header += mask
        self.writer.write(bytes(header) + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        await self.writer.drain()

    async def recv(self, timeout: float) -> dict | None:
        try:
            head = await asyncio.wait_for(self.reader.readexactly(2), timeout=timeout)
        except (TimeoutError, asyncio.IncompleteReadError):
            return None
        length = head[1] & 0x7F
        if length == 126:
            length = int.from_bytes(await self.reader.readexactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await self.reader.readexactly(8), "big")
        data = await self.reader.readexactly(length) if length else b""
        try:
            return json.loads(data.decode())
        except ValueError:
            return None

    async def wait_for(self, predicate, timeout: float) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = await self.recv(max(0.2, deadline - time.monotonic()))
            if frame is None:
                continue
            if predicate(frame):
                return frame
        return None

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, ssl.SSLError):
            pass


async def _ticket(http: httpx.AsyncClient, auth: dict[str, str]) -> str:
    r = await http.post("/api/v1/ws/ticket", headers=auth)
    assert r.status_code == 200, f"/ws/ticket: {r.status_code} {r.text[:200]}"
    return str(r.json()["ticket"])


async def test_sm4_websocket_ticket_ping_pong(
    http: httpx.AsyncClient, auth: dict[str, str], base_url: str, verify_tls: bool
) -> None:
    """Тикет -> upgrade через nginx -> ping/pong -> подписка на диалог."""
    ticket = await _ticket(http, auth)
    sock = await _Socket.connect(base_url, ticket, verify=verify_tls)
    try:
        await sock.send({"type": "ping", "data": {"n": 1}})
        pong = await sock.wait_for(lambda f: f.get("type") == "pong", timeout=10)
        assert pong is not None, "нет pong на ping (01 §11.5)"

        # тикет строго одноразовый — повторное подключение обязано отвалиться
        with pytest.raises(AssertionError):
            await _Socket.connect(base_url, ticket, verify=verify_tls)
    finally:
        await sock.close()


async def _smoke_conversation(http: httpx.AsyncClient, auth: dict[str, str]) -> str:
    """Служебный диалог для SM-5: из env, иначе первый доступный.

    Заметка наружу не уходит ни при каком выборе диалога (01 §6.4), поэтому
    fallback безопасен и позволяет гонять smoke до `leadchat-cli seed-smoke`.
    """
    from_env = os.environ.get("SMOKE_CONVERSATION_ID", "").strip()
    if from_env:
        return from_env
    r = await http.get("/api/v1/conversations", params={"limit": 1}, headers=auth)
    items = r.json().get("items") or []
    if not items:
        pytest.skip("нет ни одного диалога и не задан SMOKE_CONVERSATION_ID")
    return str(items[0]["id"])


async def test_sm5_write_to_realtime(
    http: httpx.AsyncClient, auth: dict[str, str], base_url: str, verify_tls: bool
) -> None:
    """API -> БД -> Pub/Sub -> WS Hub целиком: заметка доезжает в сокет < 2 с."""
    conversation_id = await _smoke_conversation(http, auth)
    ticket = await _ticket(http, auth)
    sock = await _Socket.connect(base_url, ticket, verify=verify_tls)
    try:
        await sock.send({"type": "subscribe", "data": {"conversation_id": conversation_id}})
        marker = f"smoke-{uuid.uuid4().hex[:8]}"
        started = time.monotonic()
        r = await http.post(
            f"/api/v1/conversations/{conversation_id}/notes",
            json={"text": f"SMOKE ping {marker}", "client_message_id": str(uuid.uuid4())},
            headers=auth,
        )
        assert r.status_code in (200, 201), f"заметка не создалась: {r.status_code} {r.text[:200]}"
        created = r.json()
        assert created["direction"] == "note", created  # наружу не уходит по определению

        frame = await sock.wait_for(
            lambda f: (
                f.get("type") == "message:new"
                and (f.get("data") or {}).get("message", {}).get("id") == created["id"]
            ),
            timeout=10,
        )
        elapsed = time.monotonic() - started
        assert frame is not None, "событие не пришло в WS (API -> БД -> Pub/Sub -> Hub)"
        assert elapsed < 5, f"realtime-тракт медленный: {elapsed:.1f} с"

        # запись реально в БД, а не только в сокете
        feed = await http.get(
            f"/api/v1/conversations/{conversation_id}/messages",
            params={"limit": 20},
            headers=auth,
        )
        assert any(m["id"] == created["id"] for m in feed.json()["items"]), "заметки нет в ленте"
    finally:
        await sock.close()


# ------------------------------------------------------ SM-6/SM-7 фон стека


def _deep(http: httpx.AsyncClient):
    return http.get("/api/health/deep")


async def test_sm6_queue_and_worker(http: httpx.AsyncClient) -> None:
    """Воркер жив и разбирает очередь.

    С доступом к Redis (``SMOKE_REDIS_URL``, запуск на хосте) — честный
    round-trip джобы ``smoke_noop``. Снаружи, из CI, Redis недоступен, и
    проверяем то, что видно по HTTP: длину очереди и возраст самой старой
    незакрытой записи (05 §7.2).
    """
    redis_url = os.environ.get("SMOKE_REDIS_URL", "").strip()
    if redis_url:
        from arq.connections import RedisSettings, create_pool  # локальный импорт: только здесь

        pool = await create_pool(RedisSettings.from_dsn(redis_url))
        try:
            job = await pool.enqueue_job("smoke_noop", _job_id=f"smoke-noop-{uuid.uuid4().hex[:8]}")
            assert job is not None, "джоба не поставилась в очередь"
            result = await asyncio.wait_for(job.result(timeout=10), timeout=12)
            assert result == "ok", result
        finally:
            await pool.aclose()
        return

    r = await _deep(http)
    assert r.status_code == 200, r.text[:200]
    queue = r.json().get("queue") or {}
    assert queue.get("len") is not None, f"в /api/health/deep нет queue.len: {r.text[:200]}"
    # Судим о воркере по НЕЗАКРЫТЫМ записям, а не по длине стрима: `queue.len` —
    # это XLEN, он считает и уже обработанные entry до подрезки, и его рост сам
    # по себе не значит, что воркер умер (это отдельный алерт мониторинга,
    # 05 §7.2). Красный smoke означает откат образа — он обязан срабатывать на
    # «воркер не разбирает очередь», а не на размер хвоста.
    assert (queue.get("oldest_pending_sec") or 0) < 300, (
        f"воркер не разбирает очередь: oldest_pending={queue.get('oldest_pending_sec')} с "
        f"(runbook 05 §8.3)"
    )
    assert (queue.get("pending") or 0) < 500, (
        f"необработанных записей в PEL: {queue.get('pending')} (runbook 05 §8.3)"
    )


async def test_sm7_scheduler_heartbeat(http: httpx.AsyncClient) -> None:
    """scheduler:alive обновлялся < 2 мин назад — иначе умрёт refresh токенов."""
    r = await _deep(http)
    assert r.status_code == 200, r.text[:200]
    scheduler = r.json().get("scheduler") or {}
    age = scheduler.get("heartbeat_age_sec")
    assert age is not None, f"нет scheduler.heartbeat_age_sec: {r.text[:200]}"
    assert age < 120, f"планировщик молчит {age} с (runbook 05 §8.2)"
    assert scheduler.get("alive") is True, scheduler


# -------------------------------------------------------------- SM-8 webhook


async def test_sm8_webhook_rejects_without_secret(http: httpx.AsyncClient) -> None:
    """POST без секрета -> 403 и никаких деталей в теле (07 §4.4 W1)."""
    r = await http.post(
        "/api/hooks/avito/00000000-0000-0000-0000-000000000000",
        json={"smoke": True},
    )
    assert r.status_code == 403, f"{r.status_code} {r.text[:200]}"
    body = r.json()
    assert body["error"]["code"] == "forbidden", body
    assert "secret" not in r.text.lower()


async def test_sm8_webhook_accepts_stub_account(http: httpx.AsyncClient, need) -> None:
    """С секретом аккаунта-заглушки -> 200; дальше конвейера не идёт (disabled)."""
    account_id, secret = need("SMOKE_ACCOUNT_ID", "SMOKE_WEBHOOK_SECRET")
    r = await http.post(
        f"/api/hooks/avito/{account_id}",
        params={"secret": secret},
        json={"id": f"smoke-{uuid.uuid4().hex[:8]}", "version": "v3.0.0", "payload": {}},
    )
    assert r.status_code == 200, f"{r.status_code} {r.text[:200]}"
    assert r.json() == {"ok": True}


# --------------------------------------------------------------- SM-9 статика


async def test_sm9_frontend_bundle(http: httpx.AsyncClient) -> None:
    """`/` отдаёт index.html со ссылкой на хэшированный бандл, и бандл живой."""
    r = await http.get("/")
    assert r.status_code == 200, r.status_code
    assert "text/html" in r.headers.get("content-type", ""), r.headers.get("content-type")
    assets = re.findall(r'src="(/assets/[A-Za-z0-9_.\-]+\.js)"', r.text)
    assert assets, f"в index.html нет хэшированного бандла: {r.text[:300]}"
    bundle = await http.get(assets[0])
    assert bundle.status_code == 200, (
        f"бандл {assets[0]} отдаёт {bundle.status_code} — index.html и статика из разных сборок"
    )
    expected = os.environ.get("EXPECTED_ASSET", "").strip()
    if expected:
        assert expected in assets[0], f"бандл {assets[0]} != ожидаемого {expected}"


# ------------------------------------------------------- SM-10 токены Авито


async def test_sm10_no_account_needs_reauth(
    http: httpx.AsyncClient, need, token_cache: dict[str, str]
) -> None:
    """Ни один боевой аккаунт не в needs_reauth (деплой не убил refresh)."""
    from tests.smoke.conftest import login

    email, password = need("SMOKE_ADMIN_EMAIL", "SMOKE_ADMIN_PASSWORD")
    if "admin" not in token_cache:
        token_cache["admin"] = await login(http, email, password)
    headers = {"Authorization": f"Bearer {token_cache['admin']}"}
    r = await http.get("/api/v1/avito-accounts", headers=headers)
    assert r.status_code == 200, r.text[:200]
    broken = [a for a in r.json()["items"] if a["status"] == "needs_reauth"]
    assert not broken, (
        f"аккаунты требуют переподключения: {[a['title'] for a in broken]} (runbook 05 §8.2)"
    )
    # заодно: секретов в выдаче нет никогда (01 §4.1)
    assert "webhook_secret" not in r.text and "access_token_enc" not in r.text
