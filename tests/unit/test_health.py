"""/api/health (01 §1.1, 05 §7.2): ok / graceful degradation, never a 500."""

from app.api import deps

HEALTH = "/api/health"


async def test_health_ok(client):
    r = await client.get(HEALTH)
    assert r.status_code == 200
    assert r.json() == {
        "status": "ok",
        "db": True,
        "redis": True,
        "version": "test-version",  # env APP_VERSION from tests/conftest.py
    }


async def test_health_no_auth_required(client):
    r = await client.get(HEALTH)  # no Authorization header at all
    assert r.status_code == 200


class _BrokenSession:
    async def execute(self, *args, **kwargs):
        raise RuntimeError("db is down")


class _BrokenRedis:
    async def ping(self):
        raise ConnectionError("redis is down")


async def test_health_degrades_when_db_down(app, client):
    async def broken_db():
        yield _BrokenSession()

    app.dependency_overrides[deps.get_db] = broken_db
    r = await client.get(HEALTH)
    assert r.status_code == 200  # degradation, not 500
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] is False
    assert body["redis"] is True


async def test_health_degrades_when_redis_down(app, client):
    app.dependency_overrides[deps.get_redis] = lambda: _BrokenRedis()
    r = await client.get(HEALTH)
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "degraded"
    assert body["db"] is True
    assert body["redis"] is False
