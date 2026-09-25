"""Подписи к «Отошёл» строятся по правилу освобождения для этого человека."""

import pytest

from app.core.security import create_access_token
from app.services import app_settings

pytestmark = pytest.mark.anyio


def _auth(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id=str(user.id), role=user.role)}"}


async def _configure(db, *, enabled: bool, exempt: str = "") -> None:
    await app_settings.set_many(
        db,
        {
            app_settings.RELEASE_UNAVAILABLE_ENABLED: enabled,
            app_settings.RELEASE_UNAVAILABLE_EXEMPT: exempt,
        },
        user_id=None,
    )
    await db.commit()


async def _rule(client, user) -> int | None:
    r = await client.get("/api/v1/presence/release", headers=_auth(user))
    assert r.status_code == 200, r.text
    return r.json()["release_after_minutes"]


async def test_released_after_fifteen_minutes_when_the_rule_is_on(client, users_by_role, db):
    await _configure(db, enabled=True)

    assert await _rule(client, users_by_role["manager"]) == 15


async def test_an_exempt_person_keeps_the_dialogs(client, users_by_role, db):
    manager = users_by_role["manager"]
    await _configure(db, enabled=True, exempt=str(manager.id))

    assert await _rule(client, manager) is None


async def test_nobody_is_released_when_the_rule_is_off(client, users_by_role, db):
    await _configure(db, enabled=False)

    assert await _rule(client, users_by_role["manager"]) is None
