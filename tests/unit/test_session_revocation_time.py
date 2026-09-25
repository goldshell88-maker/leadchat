"""Отзыв сессий сверяется с моментом выдачи токена до миллисекунды.

Смена своего пароля и тут же обновление токена укладываются в одну секунду:
по одному `iat` новый токен неотличим от выписанного до отзыва, и вкладка, где
пароль сменили, получала отказ.
"""

import time

import pytest

from app.core.security import create_access_token, decode_access_token
from app.services.sessions import access_revoked_at, revoked_key, выписан_после

pytestmark = pytest.mark.anyio


def test_a_token_issued_a_moment_after_the_revocation_passes() -> None:
    отозвано = int(time.time() * 1000) - 5
    payload = decode_access_token(create_access_token(user_id="u", role="manager"))

    assert выписан_после(payload, отозвано)


def test_a_token_issued_before_the_revocation_is_refused() -> None:
    payload = decode_access_token(create_access_token(user_id="u", role="manager"))

    assert not выписан_после(payload, payload["iat_ms"])


def test_an_old_token_in_the_same_second_is_refused_as_doubtful() -> None:
    """У токена прежнего формата одно `iat`: в ту же секунду — неизвестно, до или после."""
    сейчас = int(time.time())

    assert not выписан_после({"iat": сейчас}, сейчас * 1000 + 400)
    assert выписан_после({"iat": сейчас + 1}, сейчас * 1000 + 400)


async def test_an_old_mark_in_seconds_is_read_as_the_end_of_that_second(redis) -> None:
    await redis.set(revoked_key("u"), "1700000000", ex=900)

    assert await access_revoked_at(redis, "u") == 1_700_000_000_999
