"""Passwords, JWT and refresh/invite tokens (DESIGN §9, 01 §1.2, §2).

- Passwords: argon2id (argon2-cffi defaults).
- Access token: JWT HS256, TTL 15 min, payload ``{sub, role, iat, exp, jti}``.
  The role in the token is only a UI cache — permission checks always read
  the DB row (01 §1.2).
- Refresh token: opaque token in the httpOnly cookie ``lc_refresh``; state
  lives in Redis with rotation and a denylist. Reuse of a rotated token
  revokes the whole chain of the user's refresh tokens (01 §2.2).
- Invite token: one-time, Redis ``invite:{token} → user_id``, TTL 72 h,
  consumed with GETDEL (01 §2.4).
"""

import json
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import cast

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from redis.asyncio import Redis

from app.core.config import settings
from app.core.errors import ApiError

REFRESH_COOKIE_NAME = "lc_refresh"
REFRESH_COOKIE_PATH = "/api/v1/auth"
INVITE_TTL_SECONDS = 72 * 3600

_hasher = PasswordHasher()  # argon2id by default

# Used to equalize timing when the email does not exist.
_DUMMY_HASH = _hasher.hash(secrets.token_hex(8))


# --- Passwords -------------------------------------------------------------


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def dummy_verify(password: str) -> None:
    """Burn the same CPU as a real verify — anti user-enumeration timing."""
    verify_password(_DUMMY_HASH, password)


def make_unreachable_hash() -> str:
    """Placeholder for users.password_hash until the invite is accepted (01 §3.2)."""
    return "!invite-pending:" + secrets.token_hex(16)


def is_password_set(password_hash: str) -> bool:
    return password_hash.startswith("$argon2")


# --- Access JWT ------------------------------------------------------------


def create_access_token(*, user_id: str, role: str) -> str:
    moment = time.time()
    now = int(moment)
    payload = {
        "sub": user_id,
        "role": role,
        "iat": now,
        # Отзыв сессий сверяется с моментом выдачи до миллисекунды: токен,
        # выписанный в ту же секунду сразу после отзыва (смена своего пароля и
        # тут же обновление), по одному `iat` неотличим от выписанного до.
        "iat_ms": int(moment * 1000),
        "exp": now + settings.jwt_access_ttl_seconds,
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_access_token(token: str) -> dict:
    """Decode and validate an access JWT; any failure → 401 unauthorized."""
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.InvalidTokenError as exc:
        raise ApiError("unauthorized", status=401) from exc


# --- Refresh tokens (opaque, state in Redis) -------------------------------


class RefreshTokenInvalid(Exception):
    """Refresh token is unknown, expired, revoked or reused."""


def _refresh_ttl() -> int:
    return settings.refresh_ttl_days * 86400


def _key(token: str) -> str:
    return f"refresh:{token}"


def _rotated_key(token: str) -> str:
    return f"refresh_rotated:{token}"


def _user_set_key(user_id: str) -> str:
    return f"user_refresh:{user_id}"


async def issue_refresh_token(redis: Redis, user_id: str, remember: bool) -> str:
    token = secrets.token_urlsafe(48)
    ttl = _refresh_ttl()
    await redis.set(_key(token), json.dumps({"user_id": user_id, "remember": remember}), ex=ttl)
    await redis.sadd(_user_set_key(user_id), token)
    await redis.expire(_user_set_key(user_id), ttl)
    return token


async def revoke_all_user_refresh(redis: Redis, user_id: str) -> None:
    """Revoke the whole refresh chain of a user (reuse detected / deactivation)."""
    # decode_responses=True everywhere — redis-py just types this loosely
    tokens = cast(set[str], await redis.smembers(_user_set_key(user_id)))
    for token in tokens:
        await redis.delete(_key(token))
    await redis.delete(_user_set_key(user_id))


async def rotate_refresh_token(redis: Redis, token: str) -> tuple[str, bool]:
    """Consume a refresh token; returns (user_id, remember).

    The caller issues a fresh token afterwards. Reuse of an already rotated
    token revokes every refresh token of that user and raises.
    """
    raw = await redis.getdel(_key(token))
    if raw is None:
        # Not active. Was it rotated before? Then the cookie was stolen/reused.
        user_id = cast(str | None, await redis.get(_rotated_key(token)))
        if user_id is not None:
            await revoke_all_user_refresh(redis, user_id)
        raise RefreshTokenInvalid
    data = json.loads(raw)
    user_id = data["user_id"]
    await redis.set(_rotated_key(token), user_id, ex=_refresh_ttl())
    await redis.srem(_user_set_key(user_id), token)
    return user_id, bool(data.get("remember", False))


async def revoke_refresh_token(redis: Redis, token: str) -> None:
    """Logout: put the token in the denylist without touching the rest of the chain."""
    raw = await redis.getdel(_key(token))
    if raw is None:
        return
    user_id = json.loads(raw)["user_id"]
    await redis.set(_rotated_key(token), user_id, ex=_refresh_ttl())
    await redis.srem(_user_set_key(user_id), token)


# --- Invite tokens ---------------------------------------------------------


async def issue_invite(redis: Redis, user_id: str) -> tuple[str, datetime]:
    """One-time invite token; reissue kills the previous one (01 §3.3)."""
    old = cast(str | None, await redis.getdel(f"invite_user:{user_id}"))
    if old is not None:
        await redis.delete(f"invite:{old}")
    token = "inv_" + secrets.token_urlsafe(32)
    await redis.set(f"invite:{token}", user_id, ex=INVITE_TTL_SECONDS)
    await redis.set(f"invite_user:{user_id}", token, ex=INVITE_TTL_SECONDS)
    expires_at = datetime.now(UTC) + timedelta(seconds=INVITE_TTL_SECONDS)
    return token, expires_at


async def peek_invite(redis: Redis, token: str) -> str | None:
    return cast(str | None, await redis.get(f"invite:{token}"))


async def consume_invite(redis: Redis, token: str) -> str | None:
    user_id = cast(str | None, await redis.getdel(f"invite:{token}"))
    if user_id is not None:
        await redis.delete(f"invite_user:{user_id}")
    return user_id


def invite_url(token: str) -> str:
    """Ссылка, которую человек откроет в браузере.

    ⚠ БЕРЁТСЯ `frontend_base`, А НЕ `domain`. Стояло f"https://{settings.domain}/…" —
    то есть боевой домен ВСЕГДА, даже когда команду запускают на стенде. 14 августа
    владелец получил на локальной машине ссылку на chat.partner-lead-centre.ru с
    токеном из локальной базы: открыть её нельзя нигде — на бою такого токена нет, а
    стенд по этому адресу не отвечает.

    Настоящая цена не в неудобстве. Со staging-машины так выписывается приглашение,
    которое уходит сотруднику письмом и ведёт на БОЕВОЙ адрес; человек жмёт, получает
    «ссылка недействительна» и идёт разбираться не туда.

    `frontend_base` заведён ровно для этого — «origin of the SPA for user-facing
    redirects (dev :5173, prod domain)», — и до сих пор не имел ни одного потребителя.
    На стенде достаточно задать FRONTEND_BASE_URL=http://localhost:5173.
    """
    return f"{settings.frontend_base}/invite/{token}"
