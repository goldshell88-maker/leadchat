"""fake-avito — standalone in-memory мок Avito Messenger API + OAuth.

Scope: docs/07-TESTING-SECURITY.md §2 (полная спецификация) + нужды спринта 2.
State живёт в памяти одного процесса, сбрасывается POST /_control/reset.
Никакой логики «if test» в боевом коде — бэкенду только подменяют
AVITO_API_BASE / AVITO_AUTH_URL на этот сервис.

КАНОН ФОРМАТОВ (docs/01-API-SPEC.md §10: «сырой JSON Мессенджера Авито v3»).
AvitoAdapter пишется под форматы этого файла, поэтому фиксируем их здесь:

Вебхук (POST на URL, зарегистрированный через /messenger/v3/webhook):

    {
      "id": "<uuid доставки>",
      "version": "v3.0.0",
      "timestamp": 1754382000,
      "payload": {
        "type": "message",
        "value": {
          "id": "fa-msg-…",              // external_message_id
          "chat_id": "fa-chat-…",        // external_chat_id
          "user_id": 111222333,          // владелец аккаунта (= avito_user_id)
          "author_id": 923456789,        // автор; == user_id -> эхо, воркер отбрасывает
          "created": 1754382000,         // unix-секунды
          "type": "text",
          "chat_type": "u2i",            // u2i: чат по объявлению; u2u: без него
          "content": {"text": "…"},
          "published_at": "2026-08-05T07:00:00Z",
          "item_id": 1234567890,         // только у чатов с объявлением
          "item": {"id": …, "title": …,  // расширение мока: реальный Авито шлёт
                   "price_string": …,    // только item_id — адаптер обязан уметь
                   "url": …}             // жить без ключа "item"
        }
      }
    }

Чат (GET /messenger/v2/accounts/{uid}/chats -> {"chats": [...], "meta": {...}}):

    {
      "id": "fa-chat-…", "created": …, "updated": …, "chat_type": "u2i",
      "has_unread": true,                // расширение мока для reconciliation/backfill
      "context": {"type": "item", "value": {"id", "title", "price_string", "url"}} | null,
      "last_message": {<сообщение>} | null,
      "users": [{"id": <uid аккаунта>, "name": …}, {"id": <клиент>, "name": …}]
    }

Сообщение истории (GET …/chats/{chat_id}/messages/ -> {"messages": [...], "meta": ...},
отсортировано по created по убыванию — как у реального Авито):

    {
      "id": "fa-msg-…", "chat_id": "fa-chat-…", "author_id": …, "created": …,
      "type": "text", "content": {"text": "…"},
      "direction": "in" | "out"          // относительно аккаунта: out == наш исходящий
    }
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
import zlib
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import AliasChoices, BaseModel, Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s fake-avito %(message)s")
log = logging.getLogger("fake_avito")

# httpx на уровне INFO печатает строку `HTTP Request: POST <полный url> ...`,
# а в url вебхука лежит `?secret=<секрет аккаунта>` — это прямая утечка секрета
# в docker-логи (07 §4.6 S1). Свои вызовы мы логируем сами, уже замаскированными.
logging.getLogger("httpx").setLevel(logging.WARNING)


def _mask_url(url: str) -> str:
    """Обрезает query-string: там ходит webhook-секрет (07 §4.6 S1)."""
    head, sep, _ = url.partition("?")
    return f"{head}?<query-скрыт>" if sep else head


app = FastAPI(title="fake-avito", docs_url="/docs")

# --- Константы -------------------------------------------------------------

DEFAULT_ACCOUNT_UID = 111222333
DEFAULT_ACCOUNT_NAME = "Fake Avito Account"
DEFAULT_ACCESS_TTL_SECONDS = 86400          # прод-подобный TTL; тесты ставят 60 (/_control/config)
WEBHOOK_TIMEOUT_SECONDS = 3.0               # контракт задачи: доставка вебхука с таймаутом 3 с
SLOW_MODE_DELAY_SECONDS = 20.0              # 07 §2.3: больше клиентского таймаута 15 с

# Куда 302-ить со страницы /oauth, если redirect_uri не передан в query.
# 01 §4.3: callback — публичный endpoint БЭКЕНДА (браузер админа приходит с Авито).
DEFAULT_REDIRECT_URI = os.environ.get(
    "AVITO_REDIRECT_URI", "http://localhost:8000/api/v1/avito/callback"
)

# --- State -----------------------------------------------------------------


def _initial_state() -> dict[str, Any]:
    return {
        "accounts": {},        # uid -> {"user_id", "name", "email"}
        "codes": {},           # code -> {"account_user_id", "used"}
        "access_tokens": {},   # token -> {"account_user_id", "expires_at"}
        "refresh_tokens": {},  # token -> {"account_user_id", "used"}
        "webhooks": {},        # uid -> url подписки
        "chats": {},           # uid -> {chat_id: chat}
        "outbox": [],          # всё, что бэкенд «отправил в Авито» (07 §2.2)
        # image_id -> {"account_user_id", "size", "sha256", "uploaded"}.
        # ⚠ ЗАЛИВКА ЗАПОМИНАЕТСЯ ОТДЕЛЬНО ОТ ОТПРАВКИ, И ЭТО НЕ ПЕДАНТИЗМ.
        # У Авито отправка фото — ДВА запроса подряд (uploadImages, затем
        # messages/image) без ключа идемпотентности: обрыв между ними при
        # повторе доставки заливает фото второй раз, и клиент видит дубль.
        # Проверить защиту от этого можно единственным способом — посчитав
        # заливки отдельно от отправок, поэтому здесь свой словарь.
        "images": {},
        "mode": {"mode": "ok", "scope": "all"},
        "drop_webhooks": 0,    # следующие N вебхуков не доставлять (07 §2.2)
        "access_ttl_seconds": DEFAULT_ACCESS_TTL_SECONDS,
    }


STATE: dict[str, Any] = _initial_state()


def _ensure_account(uid: int, name: str | None = None) -> dict[str, Any]:
    account = STATE["accounts"].get(uid)
    if account is None:
        account = {
            "user_id": uid,
            "name": name or f"Avito user {uid}",
            "email": f"user{uid}@avito.local",
        }
        STATE["accounts"][uid] = account
    STATE["chats"].setdefault(uid, {})
    return account


_ensure_account(DEFAULT_ACCOUNT_UID, DEFAULT_ACCOUNT_NAME)

# --- Вспомогательные -------------------------------------------------------


@app.middleware("http")
async def log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Каждый принятый запрос — в лог: первый источник истины при флаки-тестах (07 §2.3)."""
    response = await call_next(request)
    log.info("%s %s -> %s", request.method, request.url.path, response.status_code)
    return response


def _now() -> int:
    return int(time.time())


def _client_id(name: str) -> int:
    """Детерминированный id клиента по имени — тестам легко ассертить."""
    return 900_000_000 + zlib.crc32(name.encode("utf-8")) % 90_000_000


def _account_from_bearer(request: Request) -> dict[str, Any]:
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    record = STATE["access_tokens"].get(auth.removeprefix("Bearer ").strip())
    if record is None or record["expires_at"] <= time.time():
        raise HTTPException(status_code=401, detail="invalid or expired token")
    return STATE["accounts"][record["account_user_id"]]


def _require_uid_match(account: dict[str, Any], uid: int) -> None:
    if account["user_id"] != uid:
        raise HTTPException(status_code=403, detail="token does not belong to this account")


def _uid_for_client_id(client_id: str) -> int:
    """Устойчивый номер аккаунта по client_id — тот же вход даёт тот же выход.

    Девять знаков, как у настоящих идентификаторов Авито; ноль исключён, чтобы
    не столкнуться с «пусто». Случайность здесь была бы вредна: переподключение
    теми же ключами обязано попасть в тот же аккаунт.
    """
    digest = hashlib.sha256(client_id.encode()).digest()
    return 100_000_000 + int.from_bytes(digest[:6], "big") % 900_000_000


def _issue_pair(uid: int) -> dict[str, Any]:
    """Новая пара токенов. Refresh одноразовый — как у настоящего Авито (07 §2.1)."""
    ttl = STATE["access_ttl_seconds"]
    access = f"fa-access-{uuid.uuid4().hex}"
    refresh = f"fa-refresh-{uuid.uuid4().hex}"
    STATE["access_tokens"][access] = {"account_user_id": uid, "expires_at": time.time() + ttl}
    STATE["refresh_tokens"][refresh] = {"account_user_id": uid, "used": False}
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "Bearer",
        "expires_in": ttl,
    }


async def _apply_mode(scope: str) -> None:
    """Режим ошибок из /_control/mode (07 §2.3). scope: "token" | "messages"."""
    mode = STATE["mode"]
    if mode["mode"] == "ok" or mode["scope"] not in ("all", scope):
        return
    if mode["mode"] == "slow":
        await asyncio.sleep(SLOW_MODE_DELAY_SECONDS)
        return
    if mode["mode"] == "429":
        raise HTTPException(
            status_code=429, detail="simulated 429 (control mode)", headers={"Retry-After": "5"}
        )
    raise HTTPException(
        status_code=int(mode["mode"]), detail=f"simulated {mode['mode']} (control mode)"
    )


async def _form_or_json(request: Request) -> dict[str, Any]:
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        body = await request.json()
        return dict(body) if isinstance(body, dict) else {}
    form = await request.form()
    return {k: v for k, v in form.items()}


# --- Домен: чаты и сообщения ----------------------------------------------


def _new_chat(
    uid: int,
    client_id: int,
    client_name: str,
    item: dict[str, Any] | None = None,
    created: int | None = None,
) -> dict[str, Any]:
    ts = created if created is not None else _now()
    chat = {
        "id": f"fa-chat-{uuid.uuid4().hex[:16]}",
        "created": ts,
        "updated": ts,
        "chat_type": "u2i" if item else "u2u",
        "has_unread": False,
        "item": item,
        "client": {"id": client_id, "name": client_name},
        "messages": [],
    }
    STATE["chats"][uid][chat["id"]] = chat
    return chat


def _append_message(
    chat: dict[str, Any], author_id: int, text: str, created: int | None = None
) -> dict[str, Any]:
    msg = {
        "id": f"fa-msg-{uuid.uuid4().hex[:16]}",
        "chat_id": chat["id"],
        "author_id": author_id,
        "created": created if created is not None else _now(),
        "type": "text",
        "content": {"text": text},
    }
    chat["messages"].append(msg)
    chat["updated"] = max(chat["updated"], msg["created"])
    return msg


def _append_image_message(
    chat: dict[str, Any], author_id: int, image_id: str
) -> dict[str, Any]:
    """Сообщение-картинка. Форма `content.image.sizes` — как отдаёт Авито.

    Отдельная функция, а не флаг у `_append_message`: у картинки нет текста
    вовсе, и попытка уложить оба вида в одну подпись кончилась бы полем
    `text: ""` в чате — тем самым пустым телом, из-за которого 11 августа
    пропадал вид сообщения в списке диалогов.
    """
    msg = {
        "id": f"fa-msg-{uuid.uuid4().hex[:16]}",
        "chat_id": chat["id"],
        "author_id": author_id,
        "created": _now(),
        "type": "image",
        "content": {
            "image": {
                "sizes": {
                    "140x105": f"https://fake.avito/img/{image_id}/140x105.jpg",
                    "1280x960": f"https://fake.avito/img/{image_id}/1280x960.jpg",
                }
            }
        },
    }
    chat["messages"].append(msg)
    chat["updated"] = max(chat["updated"], msg["created"])
    return msg


def _message_out(msg: dict[str, Any], account_uid: int) -> dict[str, Any]:
    return {**msg, "direction": "out" if msg["author_id"] == account_uid else "in"}


def _chat_out(chat: dict[str, Any], account: dict[str, Any]) -> dict[str, Any]:
    last = chat["messages"][-1] if chat["messages"] else None
    context = {"type": "item", "value": dict(chat["item"])} if chat["item"] else None
    return {
        "id": chat["id"],
        "created": chat["created"],
        "updated": chat["updated"],
        "chat_type": chat["chat_type"],
        "has_unread": chat["has_unread"],
        "context": context,
        "last_message": _message_out(last, account["user_id"]) if last else None,
        "users": [
            {"id": account["user_id"], "name": account["name"]},
            {"id": chat["client"]["id"], "name": chat["client"]["name"]},
        ],
    }


def _get_chat_or_404(uid: int, chat_id: str) -> dict[str, Any]:
    chat = STATE["chats"].get(uid, {}).get(chat_id)
    if chat is None:
        raise HTTPException(status_code=404, detail=f"chat {chat_id} not found")
    return chat


# --- Вебхуки: payload и доставка ------------------------------------------


def _webhook_payload(account_uid: int, chat: dict[str, Any], msg: dict[str, Any]) -> dict[str, Any]:
    """Конверт Авито Messenger v3 — канон для AvitoAdapter.parse_webhook (01 §10)."""
    value: dict[str, Any] = {
        "id": msg["id"],
        "chat_id": chat["id"],
        "user_id": account_uid,
        "author_id": msg["author_id"],
        "created": msg["created"],
        "type": "text",
        "chat_type": chat["chat_type"],
        "content": {"text": msg["content"]["text"]},
        "published_at": datetime.fromtimestamp(msg["created"], tz=UTC).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    if chat["item"]:
        value["item_id"] = chat["item"]["id"]
        value["item"] = dict(chat["item"])  # расширение мока; см. докстринг модуля
    return {
        "id": str(uuid.uuid4()),
        "version": "v3.0.0",
        "timestamp": msg["created"],
        "payload": {"type": "message", "value": value},
    }


async def _deliver_webhook(
    account_uid: int, chat: dict[str, Any], msg: dict[str, Any]
) -> dict[str, Any]:
    """POST вебхука на зарегистрированный URL. Результат — в ответ /_control/incoming."""
    url = STATE["webhooks"].get(account_uid)
    if not url:
        return {"url": None, "delivered": False, "reason": "not_registered"}
    if STATE["drop_webhooks"] > 0:
        STATE["drop_webhooks"] -= 1
        log.info("webhook dropped (drop_webhooks): chat=%s msg=%s", chat["id"], msg["id"])
        return {"url": url, "delivered": False, "reason": "dropped"}
    payload = _webhook_payload(account_uid, chat, msg)
    try:
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_SECONDS) as http:
            resp = await http.post(url, json=payload)
    except httpx.HTTPError as exc:
        log.warning("webhook delivery failed: %s -> %s", _mask_url(url), exc)
        return {"url": url, "delivered": False, "reason": "error", "error": str(exc)}
    result: dict[str, Any] = {
        "url": url,
        "delivered": 200 <= resp.status_code < 300,
        "status_code": resp.status_code,
    }
    if not result["delivered"]:
        result["reason"] = "http_error"
    return result


# --- Health ----------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# --- OAuth -----------------------------------------------------------------


@app.get("/oauth")
async def oauth_page(
    state: str = "",
    redirect_uri: str | None = None,
    account_user_id: int | None = None,
    response_type: str = "code",  # noqa: ARG001 — принимаем, как настоящий Авито
    client_id: str = "",  # noqa: ARG001
    scope: str = "",  # noqa: ARG001
) -> HTMLResponse:
    """Страница-заглушка согласия: мгновенный 302 на redirect_uri с code+state.

    redirect_uri в query — приоритет; иначе env AVITO_REDIRECT_URI (реальный Авито
    берёт его из настроек приложения в кабинете, поэтому в build_authorize_url
    DESIGN §8.1 его нет). account_user_id позволяет тестам «подключать» разные
    аккаунты; по умолчанию — дефолтный.
    """
    uid = account_user_id if account_user_id is not None else DEFAULT_ACCOUNT_UID
    _ensure_account(uid)
    code = f"fa-code-{uuid.uuid4().hex}"
    STATE["codes"][code] = {"account_user_id": uid, "used": False}
    target = redirect_uri or DEFAULT_REDIRECT_URI
    location = f"{target}{'&' if '?' in target else '?'}{urlencode({'code': code, 'state': state})}"
    log.info("oauth: issued code for uid=%s -> %s", uid, target)
    html = (
        "<!doctype html><html lang='ru'><meta charset='utf-8'>"
        "<title>fake-avito OAuth</title>"
        "<body style='font-family:sans-serif'>"
        "<p>fake-avito: согласие выдано, возвращаемся в LeadChat…</p>"
        f"<p><a href='{location}'>Продолжить вручную</a></p></body></html>"
    )
    return HTMLResponse(html, status_code=302, headers={"Location": location})


@app.post("/token")
async def token(request: Request) -> dict[str, Any]:
    """OAuth-токены: authorization_code / refresh_token (одноразовый!) / client_credentials."""
    await _apply_mode("token")
    payload = await _form_or_json(request)
    grant_type = payload.get("grant_type")

    if grant_type == "authorization_code":
        record = STATE["codes"].get(str(payload.get("code") or ""))
        if record is None or record["used"]:
            raise HTTPException(status_code=400, detail="invalid or already used code")
        record["used"] = True
        return _issue_pair(record["account_user_id"])

    if grant_type == "refresh_token":
        record = STATE["refresh_tokens"].get(str(payload.get("refresh_token") or ""))
        if record is None or record["used"]:
            # сгоревший refresh -> 400: ловим гонки double-refresh (07 §2.1)
            raise HTTPException(status_code=400, detail="invalid or already used refresh_token")
        record["used"] = True
        return _issue_pair(record["account_user_id"])

    if grant_type == "client_credentials":
        # КАЖДАЯ ПАРА КЛЮЧЕЙ — СВОЙ АККАУНТ, как у настоящего Авито.
        #
        # Раньше здесь стоял один и тот же DEFAULT_ACCOUNT_UID на любые ключи.
        # Из-за этого второй канал подключить было нельзя вовсе: система
        # получала тот же avito_user_id и обновляла ту же строку — заказчик
        # так и сказал, «я могу подключить только 1 аккаунт». Поломка была
        # только в имитаторе, но выглядела как поломка системы, а проверить
        # раздачу диалогов по девяти каналам до подключения боевых стало
        # невозможно.
        #
        # Идентификатор выводится из client_id устойчиво: одни и те же ключи
        # всегда дают один и тот же аккаунт (переподключение не плодит
        # дубликатов), разные — разные.
        client_id = str(payload.get("client_id") or "")
        uid = _uid_for_client_id(client_id) if client_id else DEFAULT_ACCOUNT_UID
        _ensure_account(uid, name=f"Тестовый аккаунт {uid % 1000:03d}")
        # refresh для этого grant'а настоящий Авито не выдаёт — и мы не выдаём
        pair = _issue_pair(uid)
        del STATE["refresh_tokens"][pair.pop("refresh_token")]
        return pair

    raise HTTPException(status_code=400, detail=f"unsupported grant_type: {grant_type!r}")


# --- Core API --------------------------------------------------------------


@app.get("/core/v1/accounts/self")
async def accounts_self(request: Request) -> dict[str, Any]:
    account = _account_from_bearer(request)
    return {"id": account["user_id"], "name": account["name"], "email": account["email"]}


# --- Messenger API ---------------------------------------------------------


@app.post("/messenger/v3/webhook")
async def register_webhook(request: Request) -> dict[str, bool]:
    """Запоминает URL подписки per-аккаунт (по Bearer-токену); кривой URL -> 400."""
    account = _account_from_bearer(request)
    payload = await _form_or_json(request)
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="url must be an http(s) URL")
    STATE["webhooks"][account["user_id"]] = url
    log.info("webhook registered: uid=%s url=%s", account["user_id"], _mask_url(url))
    return {"ok": True}


@app.post("/messenger/v1/webhook/unsubscribe")
async def unsubscribe_webhook(request: Request) -> dict[str, bool]:
    """Снятие подписки (01 §4.5 disable). Идемпотентно."""
    account = _account_from_bearer(request)
    STATE["webhooks"].pop(account["user_id"], None)
    return {"ok": True}


# ОБА ГЛАГОЛА — и это не лень, а признание незнания (#39).
#
# В нашем каталоге метод записан дважды и по-разному: в разделе «чего мы не
# используем» как GET, в таблице из спецификации Авито — как POST. Имитатор
# отвечал только на GET, то есть подтверждал нашу же догадку. Именно так этот
# проект уже дважды попал на боевом Авито: путь v2 вместо v3 и 403 вместо 401.
#
# Пока не проверено на живом аккаунте, имитатор отвечает на оба — и не
# притворяется, будто знает, какой из них настоящий.
@app.post("/messenger/v1/subscriptions")
@app.get("/messenger/v1/subscriptions")
async def subscriptions(request: Request) -> dict[str, Any]:
    account = _account_from_bearer(request)
    url = STATE["webhooks"].get(account["user_id"])
    items = [{"url": url, "version": "3.0.0"}] if url else []
    return {"subscriptions": items}


@app.get("/messenger/v2/accounts/{uid}/chats")
async def list_chats(
    uid: int,
    request: Request,
    offset: int = 0,
    limit: int = 100,
    unread_only: bool = False,
) -> dict[str, Any]:
    """Список чатов: пагинация offset/limit, фильтр unread_only (DESIGN §1.3)."""
    await _apply_mode("messages")
    account = _account_from_bearer(request)
    _require_uid_match(account, uid)
    chats = list(STATE["chats"].get(uid, {}).values())
    if unread_only:
        chats = [c for c in chats if c["has_unread"]]
    chats.sort(key=lambda c: c["updated"], reverse=True)
    offset, limit = max(offset, 0), max(1, min(limit, 100))
    page = chats[offset : offset + limit]
    return {
        "chats": [_chat_out(c, account) for c in page],
        "meta": {"total": len(chats), "offset": offset, "limit": limit},
    }


# V3 — как в каталоге API Авито. Имитатор обязан повторять НАСТОЯЩИЕ пути,
# иначе он подтверждает наши догадки вместо того, чтобы их проверять.
@app.get("/messenger/v3/accounts/{uid}/chats/{chat_id}/messages/")
async def chat_messages(
    uid: int,
    chat_id: str,
    request: Request,
    offset: int = 0,
    limit: int = 100,
) -> dict[str, Any]:
    """История чата, новые сверху (как у реального Авито). Чтение не сбрасывает has_unread."""
    await _apply_mode("messages")
    account = _account_from_bearer(request)
    _require_uid_match(account, uid)
    chat = _get_chat_or_404(uid, chat_id)
    messages = sorted(chat["messages"], key=lambda m: (m["created"], m["id"]), reverse=True)
    offset, limit = max(offset, 0), max(1, min(limit, 100))
    page = messages[offset : offset + limit]
    return {
        "messages": [_message_out(m, uid) for m in page],
        "meta": {"total": len(messages), "offset": offset, "limit": limit},
    }


@app.post("/messenger/v1/accounts/{uid}/chats/{chat_id}/messages")
async def send_message(uid: int, chat_id: str, request: Request) -> dict[str, str]:
    """Приём исходящего от бэкенда (понадобится в спринте 3): кладёт в чат и outbox.

    Тело — как в DESIGN §8.2: {"message": {"text": "..."}, "type": "text"}.
    Ответ оператора «прочитывает» чат: has_unread сбрасывается.
    """
    await _apply_mode("messages")
    account = _account_from_bearer(request)
    _require_uid_match(account, uid)
    chat = _get_chat_or_404(uid, chat_id)
    body = await _form_or_json(request)
    message = body.get("message")
    text = str(message.get("text", "")).strip() if isinstance(message, dict) else ""
    if not text:
        raise HTTPException(status_code=400, detail="message.text is required")
    msg = _append_message(chat, uid, text)
    chat["has_unread"] = False
    STATE["outbox"].append(
        {
            "account_user_id": uid,
            "chat_id": chat["id"],
            "message_id": msg["id"],
            "text": text,
            "created": msg["created"],
        }
    )
    return {"id": msg["id"]}


@app.post("/messenger/v1/accounts/{uid}/uploadImages")
async def upload_images(uid: int, request: Request) -> dict[str, Any]:
    """Заливка картинки. Ответ Авито — объект, КЛЮЧ которого и есть image_id.

    Форма ответа `{"<image_id>": {"<размер>": "<url>", ...}}` повторяет боевую
    (docs/26-AVITO-API-CATALOG.md): идентификатор лежит В КЛЮЧЕ, а не в поле.
    Имитатор, отвечающий привычным `{"id": ...}`, скрыл бы ровно ту ошибку
    разбора, которая на бою стоит недоставленного фото.
    """
    await _apply_mode("messages")
    account = _account_from_bearer(request)
    _require_uid_match(account, uid)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="uploadfile is required")
    image_id = f"fa-img-{uuid.uuid4().hex[:16]}"
    STATE["images"][image_id] = {
        "account_user_id": uid,
        "size": len(body),
        # Отпечаток тела — по нему тест видит, что повторная доставка залила
        # ТО ЖЕ САМОЕ фото второй раз, а не другое.
        "sha256": hashlib.sha256(body).hexdigest(),
        "uploaded": _now(),
    }
    return {image_id: {"140x105": f"https://fake.avito/img/{image_id}/140x105.jpg"}}


@app.post("/messenger/v1/accounts/{uid}/chats/{chat_id}/messages/image")
async def send_image(uid: int, chat_id: str, request: Request) -> dict[str, Any]:
    """Отправка залитой картинки в чат. Тело: {"image_id": "..."}."""
    await _apply_mode("messages")
    account = _account_from_bearer(request)
    _require_uid_match(account, uid)
    chat = _get_chat_or_404(uid, chat_id)
    body = await _form_or_json(request)
    image_id = str(body.get("image_id") or "").strip()
    if not image_id:
        raise HTTPException(status_code=400, detail="image_id is required")
    # Незалитый идентификатор Авито не примет, и имитатор не должен: иначе
    # ошибка «отправляем фото, которое не заливали» доехала бы до боя молча.
    if image_id not in STATE["images"]:
        raise HTTPException(status_code=400, detail="unknown image_id")
    msg = _append_image_message(chat, uid, image_id)
    chat["has_unread"] = False
    STATE["outbox"].append(
        {
            "account_user_id": uid,
            "chat_id": chat["id"],
            "message_id": msg["id"],
            "image_id": image_id,
            "created": msg["created"],
        }
    )
    return {**msg, "direction": "out"}


# --- Управляющая плоскость /_control/* (07 §2.2) --------------------------


class ItemIn(BaseModel):
    id: int
    title: str = "Объявление"
    price_string: str | None = None
    url: str | None = None


class IncomingIn(BaseModel):
    account_user_id: int = Field(
        DEFAULT_ACCOUNT_UID, validation_alias=AliasChoices("account_user_id", "account_id")
    )
    chat_id: str | None = None
    author: str = Field(
        "Клиент Авито", validation_alias=AliasChoices("author", "author_name")
    )
    author_id: int | None = None
    text: str
    item: ItemIn | None = None


@app.post("/_control/incoming")
async def control_incoming(body: IncomingIn) -> dict[str, Any]:
    """Создаёт входящее в state И САМ доставляет вебхук POST'ом (таймаут 3 с).

    chat_id не задан -> новый чат (с item, если передан). Результат доставки —
    в ответе: тесты и dev-сценарии ассертят его, не подглядывая в логи.
    """
    account = _ensure_account(body.account_user_id)
    uid = account["user_id"]
    if body.chat_id is not None:
        chat = _get_chat_or_404(uid, body.chat_id)
        author_id = body.author_id if body.author_id is not None else chat["client"]["id"]
    else:
        author_id = body.author_id if body.author_id is not None else _client_id(body.author)
        chat = _new_chat(
            uid, author_id, body.author, item=body.item.model_dump() if body.item else None
        )
    msg = _append_message(chat, author_id, body.text)
    chat["has_unread"] = True
    delivery = await _deliver_webhook(uid, chat, msg)
    log.info(
        "incoming: uid=%s chat=%s msg=%s delivered=%s",
        uid, chat["id"], msg["id"], delivery["delivered"],
    )
    return {"ok": True, "chat_id": chat["id"], "message_id": msg["id"], "webhook": delivery}


class SeedHistoryIn(BaseModel):
    account_user_id: int = Field(
        DEFAULT_ACCOUNT_UID, validation_alias=AliasChoices("account_user_id", "account_id")
    )
    chats: int = Field(5, ge=1, le=500)
    unread_chats: int = Field(0, ge=0, le=500)


_SEED_NAMES = ["Иван", "Мария", "Пётр", "Ольга", "Сергей", "Анна", "Дмитрий", "Елена"]
_SEED_ITEMS = [
    {"id": 3060161080, "title": "iPhone 14 Pro 256 ГБ", "price_string": "78 000 ₽",
     "url": "https://avito.ru/items/3060161080"},
    {"id": 2841077113, "title": "Диван угловой, велюр", "price_string": "24 500 ₽",
     "url": "https://avito.ru/items/2841077113"},
    {"id": 3311902447, "title": "Ремонт квартир под ключ", "price_string": None,
     "url": "https://avito.ru/items/3311902447"},
]


@app.post("/_control/seed_history")
async def control_seed_history(body: SeedHistoryIn) -> dict[str, Any]:
    """Насыпает историю для backfill-теста: N чатов в прошлом, M — с непрочитанными.

    Вебхуки НЕ доставляются — историю бэкенд забирает сам (GET chats + messages).
    В каждом чате есть и клиентские, и наши исходящие сообщения: backfill обязан
    сохранить исходящие как контекст (08 §4.2). Непрочитанные чаты — самые свежие,
    их последнее сообщение — от клиента.
    """
    account = _ensure_account(body.account_user_id)
    uid = account["user_id"]
    unread_count = min(body.unread_chats, body.chats)
    now = _now()
    chat_ids: list[str] = []
    unread_ids: list[str] = []

    for i in range(body.chats):
        name = _SEED_NAMES[i % len(_SEED_NAMES)]
        item = dict(_SEED_ITEMS[i % len(_SEED_ITEMS)]) if i % 2 == 0 else None
        base = now - (i + 1) * 86400  # i+1 дней назад; свежее — раньше в цикле
        chat = _new_chat(uid, _client_id(f"{name}-{i}"), name, item=item, created=base)
        client = chat["client"]["id"]
        _append_message(chat, client, f"Здравствуйте! Ещё актуально? ({i + 1})", base)
        _append_message(chat, uid, "Добрый день, да, актуально.", base + 600)
        _append_message(chat, client, "Отлично, а торг уместен?", base + 1200)
        if i < unread_count:
            chat["has_unread"] = True  # ждут ответа: backfill переоткроет в 'new'
            unread_ids.append(chat["id"])
        else:
            _append_message(chat, uid, "Небольшой — приезжайте смотреть.", base + 1800)
            chat["has_unread"] = False
        chat_ids.append(chat["id"])

    log.info("seed_history: uid=%s chats=%d unread=%d", uid, len(chat_ids), len(unread_ids))
    return {
        "ok": True,
        "account_user_id": uid,
        "chats_created": len(chat_ids),
        "chat_ids": chat_ids,
        "unread_chat_ids": unread_ids,
    }


class ModeIn(BaseModel):
    mode: Literal["ok", "401", "429", "500", "slow"]
    scope: Literal["messages", "token", "all"] = "all"


@app.post("/_control/mode")
async def control_mode(body: ModeIn) -> dict[str, Any]:
    """Режим ошибок (07 §2.3): ok | 401 | 429 | 500 | slow; scope — какие endpoints."""
    STATE["mode"] = {"mode": body.mode, "scope": body.scope}
    return {"ok": True, **STATE["mode"]}


class ConfigIn(BaseModel):
    access_ttl_seconds: int = Field(..., ge=1, le=7 * 86400)


@app.post("/_control/config")
async def control_config(body: ConfigIn) -> dict[str, Any]:
    """TTL access-токена для новых выдач (07 §2.1: в тестах 60 с, чтобы refresh исполнялся)."""
    STATE["access_ttl_seconds"] = body.access_ttl_seconds
    return {"ok": True, "access_ttl_seconds": body.access_ttl_seconds}


class DropWebhooksIn(BaseModel):
    count: int = Field(..., ge=0, le=10_000)


@app.post("/_control/drop_webhooks")
async def control_drop_webhooks(body: DropWebhooksIn) -> dict[str, Any]:
    """Следующие N вебхуков не доставлять — тест reconciliation (07 §2.2)."""
    STATE["drop_webhooks"] = body.count
    return {"ok": True, "drop_webhooks": body.count}


@app.get("/_control/outbox")
async def control_outbox() -> dict[str, Any]:
    """Всё, что бэкенд «отправил в Авито», — главный ассерт e2e (07 §2.2)."""
    return {"outbox": STATE["outbox"]}


@app.get("/_control/state")
async def control_state() -> dict[str, Any]:
    """Диагностика для dev: сводка state без тел сообщений."""
    return {
        "accounts": sorted(STATE["accounts"]),
        "webhooks": {str(uid): url for uid, url in STATE["webhooks"].items()},
        "chats": {str(uid): len(chats) for uid, chats in STATE["chats"].items()},
        "outbox_len": len(STATE["outbox"]),
        # Число заливок отдельно от числа отправок — по ним тест идемпотентности
        # и судит: одно фото, залитое дважды, это дубль у клиента.
        "images_uploaded": len(STATE["images"]),
        "mode": STATE["mode"],
        "drop_webhooks": STATE["drop_webhooks"],
        "access_ttl_seconds": STATE["access_ttl_seconds"],
    }


@app.post("/_control/reset")
async def control_reset() -> dict[str, bool]:
    """Сброс всего state между тестами (07 §2.2). Остаётся только дефолтный аккаунт."""
    STATE.clear()
    STATE.update(_initial_state())
    _ensure_account(DEFAULT_ACCOUNT_UID, DEFAULT_ACCOUNT_NAME)
    return {"ok": True}


# --- Ошибки ----------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    # плоский формат, близкий к Авито: {"error": {"code": ..., "message": ...}}
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.status_code, "message": exc.detail}},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": 422,
                "message": "validation error",
                "details": jsonable_encoder(exc.errors()),
            }
        },
    )
