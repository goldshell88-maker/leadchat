"""AES-256-GCM для токенов Авито (DESIGN §1.5).

Ключ — ``settings.token_enc_key``: base64 (``openssl rand -base64 32``,
как в .env.example) или hex (64 символа); после декодирования — ровно
32 байта. Nonce — 96 бит, случайный на каждое сообщение, хранится
префиксом шифртекста: ``blob = nonce (12 байт) || ciphertext+tag``.
Режим аутентифицированный: подмена любого байта -> ``DecryptError``.

Токены в логи не попадают никогда (DESIGN §1.5) — модуль не логирует
ни plaintext, ни ключ.
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings

NONCE_SIZE = 12  # 96 бит — рекомендация NIST SP 800-38D для GCM
KEY_SIZE = 32  # AES-256


class CryptoKeyError(ValueError):
    """TOKEN_ENC_KEY не удалось разобрать: не base64/hex или не 32 байта."""


class DecryptError(ValueError):
    """Шифртекст повреждён/подделан или зашифрован другим ключом."""


def load_key(raw: str) -> bytes:
    """Декодирует ключ из base64 или hex; строго 32 байта на выходе."""
    candidates: list[bytes] = []
    raw = raw.strip()
    try:
        candidates.append(base64.b64decode(raw, validate=True))
    except (binascii.Error, ValueError):
        pass
    try:
        candidates.append(bytes.fromhex(raw))
    except ValueError:
        pass
    for candidate in candidates:
        if len(candidate) == KEY_SIZE:
            return candidate
    raise CryptoKeyError(
        "TOKEN_ENC_KEY должен быть 32 байта в base64 (openssl rand -base64 32) или hex"
    )


# Кэш AESGCM на значение ключа: пересоздание на каждый вызов дорого,
# а кэш по строке ключа корректно переживает смену settings в тестах.
_cache: tuple[str, AESGCM] | None = None


def _aesgcm() -> AESGCM:
    global _cache
    raw = settings.token_enc_key
    if _cache is None or _cache[0] != raw:
        _cache = (raw, AESGCM(load_key(raw)))
    return _cache[1]


def encrypt(plaintext: bytes) -> bytes:
    """bytes -> nonce || ciphertext+tag."""
    nonce = os.urandom(NONCE_SIZE)
    return nonce + _aesgcm().encrypt(nonce, plaintext, None)


def decrypt(blob: bytes) -> bytes:
    """nonce || ciphertext+tag -> bytes; повреждение -> DecryptError."""
    if len(blob) < NONCE_SIZE + 16:  # nonce + GCM-tag — минимум валидного блоба
        raise DecryptError("шифртекст короче минимальной длины nonce+tag")
    try:
        return _aesgcm().decrypt(blob[:NONCE_SIZE], blob[NONCE_SIZE:], None)
    except InvalidTag as exc:
        raise DecryptError("шифртекст повреждён или ключ не совпадает") from exc


def encrypt_token(token: str) -> bytes:
    """Удобство для токенов Авито: str -> шифроблоб."""
    return encrypt(token.encode("utf-8"))


def decrypt_token(blob: bytes) -> str:
    """Шифроблоб -> str."""
    return decrypt(blob).decode("utf-8")
