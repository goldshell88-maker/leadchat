"""Сокет после отзыва сессий: пускает перезашедшего, не пускает старый билет.

После «Задать пароль» сотрудник входил новым паролем, REST работал, а сокет
15 минут отвергался: проверка здесь была «пометка есть», а REST с 27.08
сверяет время. Коллеги видели его «не в сети», передать ему диалог было нельзя.
"""

import time

import pytest

from app.api.routes import ws as ws_routes
from app.services.sessions import revoked_key

pytestmark = pytest.mark.anyio


class _Accepted(Exception):
    pass


class _FakeSocket:
    headers: dict[str, str] = {}

    def __init__(self) -> None:
        self.closed: int | None = None

    async def close(self, code: int) -> None:
        self.closed = code

    async def accept(self) -> None:
        raise _Accepted


async def _connect(redis, monkeypatch, db_sessionmaker, ticket_value: str) -> _FakeSocket:
    monkeypatch.setattr(ws_routes.db_mod, "session_scope", db_sessionmaker)
    await redis.set("ws_ticket:wst_test", ticket_value, ex=60)
    sock = _FakeSocket()
    try:
        await ws_routes.ws_endpoint(sock, ticket="wst_test", redis=redis)  # type: ignore[arg-type]
    except _Accepted:
        sock.closed = 0
    return sock


async def test_a_ticket_issued_after_the_revocation_connects(
    redis, monkeypatch, db_sessionmaker, users_by_role
):
    user = users_by_role["manager"]
    сейчас_мс = int(time.time() * 1000)
    await redis.set(revoked_key(user.id), str(сейчас_мс - 300), ex=900)

    sock = await _connect(redis, monkeypatch, db_sessionmaker, f"{user.id}|{сейчас_мс}")

    assert sock.closed == 0, "перезашедший новым паролем обязан получить живую связь"


async def test_a_ticket_issued_before_the_revocation_is_refused(
    redis, monkeypatch, db_sessionmaker, users_by_role
):
    user = users_by_role["manager"]
    сейчас_мс = int(time.time() * 1000)
    await redis.set(revoked_key(user.id), str(сейчас_мс), ex=900)

    sock = await _connect(redis, monkeypatch, db_sessionmaker, f"{user.id}|{сейчас_мс - 300}")

    assert sock.closed == 4403


async def test_a_ticket_without_a_time_is_refused_during_a_revocation(
    redis, monkeypatch, db_sessionmaker, users_by_role
):
    user = users_by_role["manager"]
    await redis.set(revoked_key(user.id), str(int(time.time())), ex=900)

    sock = await _connect(redis, monkeypatch, db_sessionmaker, str(user.id))

    assert sock.closed == 4403
