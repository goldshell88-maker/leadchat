"""INT: migrations from scratch + the auth flow against a real Postgres
(testcontainers). Verifies what SQLite cannot: the 0001 migration DDL
(citext, partitioned messages, GIN indexes) and citext-driven
case-insensitive login."""

from collections.abc import AsyncIterator

import fakeredis.aioredis
import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import deps
from app.core.security import hash_password
from app.main import create_app
from app.models import AuditLog, User
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

PASSWORD = "integration-pass-123"


@pytest.fixture
async def pg_sessionmaker(
    pg_async_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # NullPool: pytest-asyncio uses a fresh event loop per test — no
    # cross-loop reuse of pooled asyncpg connections.
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def client(
    pg_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with pg_sessionmaker() as session:
            yield session

    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[deps.get_redis] = lambda: redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c
    await redis.aclose()


async def test_migrations_created_full_schema(pg_sessionmaker):
    async with pg_sessionmaker() as session:
        tables = set(
            (
                await session.execute(
                    text("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
                )
            ).scalars()
        )
        assert {
            "users",
            "avito_accounts",
            "clients",
            "conversations",
            "messages",
            "bots",
            "templates",
            "audit_log",
            "alembic_version",
        } <= tables

        # messages is partitioned (DESIGN §4.4)
        partitioned = (
            await session.execute(
                text("SELECT relkind::text FROM pg_class WHERE relname = 'messages'")
            )
        ).scalar_one()
        assert partitioned == "p"

        # GIN indexes + partial unique idempotency index exist
        indexes = set(
            (
                await session.execute(
                    text("SELECT indexname FROM pg_indexes WHERE schemaname = 'public'")
                )
            ).scalars()
        )
        assert "ix_conversations_tags" in indexes
        assert "ix_messages_search" in indexes
        assert "uq_messages_conversation_external_created" in indexes


async def test_login_flow_against_real_pg(pg_sessionmaker, client):
    async with pg_sessionmaker() as session:
        session.add(
            User(
                email="Admin@Leadchat.Test",  # mixed case on purpose: citext
                password_hash=hash_password(PASSWORD),
                full_name="Интеграционный Админ",
                role="admin",
            )
        )
        await session.commit()

    r = await client.post(
        "/api/v1/auth/login",
        json={"email": "admin@leadchat.test", "password": PASSWORD, "remember": True},
    )
    assert r.status_code == 200, r.text
    access = r.json()["access_token"]
    assert r.json()["user"]["role"] == "admin"

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert me.status_code == 200
    assert "users:manage" in me.json()["permissions"]

    refresh = await client.post("/api/v1/auth/refresh")
    assert refresh.status_code == 200

    async with pg_sessionmaker() as session:
        actions = (await session.execute(select(AuditLog.action))).scalars().all()
    assert "auth.login" in actions
