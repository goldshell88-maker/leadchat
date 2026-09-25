"""AES-256-GCM для токенов Авито (DESIGN §1.5): roundtrip, tamper, ключи."""

import base64

import pytest

from app.services import crypto
from app.services.crypto import (
    KEY_SIZE,
    NONCE_SIZE,
    CryptoKeyError,
    DecryptError,
    decrypt,
    decrypt_token,
    encrypt,
    encrypt_token,
    load_key,
)


class TestRoundtrip:
    def test_bytes_roundtrip(self) -> None:
        plaintext = b"\x00\x01binary token payload\xff"
        assert decrypt(encrypt(plaintext)) == plaintext

    def test_token_str_roundtrip_unicode(self) -> None:
        token = "access-токен-с-юникодом-❤"
        assert decrypt_token(encrypt_token(token)) == token

    def test_empty_plaintext_roundtrip(self) -> None:
        assert decrypt(encrypt(b"")) == b""

    def test_nonce_is_per_message(self) -> None:
        """Одинаковый plaintext -> разные блобы (случайный nonce на сообщение)."""
        plaintext = b"same token"
        blobs = {encrypt(plaintext) for _ in range(10)}
        assert len(blobs) == 10
        for blob in blobs:
            assert decrypt(blob) == plaintext

    def test_blob_layout_nonce_prefix(self) -> None:
        blob = encrypt(b"x")
        # nonce (12) + ciphertext (1) + tag (16)
        assert len(blob) == NONCE_SIZE + 1 + 16


class TestTamper:
    @pytest.mark.parametrize("position", [0, NONCE_SIZE, -1], ids=["nonce", "ciphertext", "tag"])
    def test_bitflip_raises(self, position: int) -> None:
        blob = bytearray(encrypt(b"secret token"))
        blob[position] ^= 0x01
        with pytest.raises(DecryptError):
            decrypt(bytes(blob))

    def test_truncated_blob_raises(self) -> None:
        blob = encrypt(b"secret token")
        with pytest.raises(DecryptError):
            decrypt(blob[: NONCE_SIZE + 8])

    def test_garbage_raises(self) -> None:
        with pytest.raises(DecryptError):
            decrypt(b"\x00" * 64)

    def test_other_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        blob = encrypt_token("secret")
        other = base64.b64encode(b"\x02" * KEY_SIZE).decode()
        monkeypatch.setattr(crypto.settings, "token_enc_key", other)
        with pytest.raises(DecryptError):
            decrypt_token(blob)


class TestLoadKey:
    def test_base64_key(self) -> None:
        raw = base64.b64encode(b"\x07" * KEY_SIZE).decode()  # как openssl rand -base64 32
        assert load_key(raw) == b"\x07" * KEY_SIZE

    def test_hex_key(self) -> None:
        assert load_key("ab" * KEY_SIZE) == bytes.fromhex("ab" * KEY_SIZE)

    def test_surrounding_whitespace_tolerated(self) -> None:
        raw = base64.b64encode(b"\x07" * KEY_SIZE).decode()
        assert load_key(f"  {raw}\n") == b"\x07" * KEY_SIZE

    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "not-a-key!!!",
            base64.b64encode(b"\x01" * 16).decode(),  # валидный base64, но 16 байт
            "ab" * 16,  # валидный hex, но 16 байт
        ],
        ids=["empty", "garbage", "short-base64", "short-hex"],
    )
    def test_bad_key_raises(self, raw: str) -> None:
        with pytest.raises(CryptoKeyError):
            load_key(raw)
