"""Unit test base (07 §1.1): no external services — SQLite (aiosqlite,
in-memory) instead of Postgres, fakeredis instead of Redis, httpx ASGI
transport instead of a socket."""

from collections.abc import AsyncIterator, Awaitable, Callable

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from app.api import deps
from app.core.security import create_access_token, hash_password, make_unreachable_hash
from app.main import create_app
from app.models import Base, User

DEFAULT_PASSWORD = "correct horse battery staple"


@pytest.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
def db_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture
async def db(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with db_sessionmaker() as session:
        yield session


@pytest.fixture
async def redis() -> AsyncIterator[fakeredis.aioredis.FakeRedis]:
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def app(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    redis: fakeredis.aioredis.FakeRedis,
) -> FastAPI:
    application = create_app()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with db_sessionmaker() as session:
            yield session

    application.dependency_overrides[deps.get_db] = override_get_db
    application.dependency_overrides[deps.get_redis] = lambda: redis
    return application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    # https base_url so the cookie jar accepts/replays the Secure refresh cookie
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def make_user(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[User]]:
    async def _make(
        email: str,
        *,
        role: str = "manager",
        password: str = DEFAULT_PASSWORD,
        is_active: bool = True,
        invited: bool = False,
        full_name: str = "Test User",
        is_service: bool = False,
        handles_conversations: bool = True,
        department: str | None = None,
    ) -> User:
        async with db_sessionmaker() as session:
            user = User(
                email=email,
                password_hash=make_unreachable_hash() if invited else hash_password(password),
                full_name=full_name,
                role=role,
                is_active=is_active,
                is_service=is_service,
                handles_conversations=handles_conversations,
                department=department,
            )
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user

    return _make


@pytest.fixture
async def users_by_role(make_user: Callable[..., Awaitable[User]]) -> dict[str, User]:
    return {
        role: await make_user(f"{role}@leadchat.test", role=role)
        for role in ("admin", "head", "manager", "observer")
    }


@pytest.fixture
def в_сети(redis):  # noqa: ANN001, ANN201 — фикстура redis типизирована у себя
    """Отметить сотрудника подключённым.

    ⚠ ЗАЧЕМ ОТДЕЛЬНЫЙ ШАГ В ТЕСТАХ ПЕРЕДАЧИ (решение владельца 28.08):
    «сделай так, чтобы не было возможности передать диалог человеку в офлайн».

    Передача — предложение: диалог не появляется ни в очереди, ни в «Моих»
    получателя, пока тот её не примет. Отданный ушедшему домой, он не виден
    НИКОМУ и ждёт молча, а отдающий уверен, что дело сделано. Поэтому сервер
    отказывает, и каждый тест передачи обязан сказать вслух, что получатель на
    месте, — иначе он проверяет несуществующий случай.

    Ключ тот же, что пишет WS-хаб (`ws/presence.py`), и тот же, что читает
    список сотрудников в окне передачи: окно и запрет говорят об одном и том же.
    """

    async def _mark(user, status: str = "online") -> None:  # noqa: ANN001
        await redis.set(f"presence:{getattr(user, 'id', user)}", status)

    return _mark


@pytest.fixture
def tokens(users_by_role: dict[str, User]) -> dict[str, str]:
    return {
        role: create_access_token(user_id=str(user.id), role=user.role)
        for role, user in users_by_role.items()
    }


# --- sprint 2: inbound / conversations seeds ---------------------------------

import uuid as _uuid  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.models import AvitoAccount, Client, Conversation, Message  # noqa: E402


@pytest.fixture
def make_avito_account(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> Callable[..., Awaitable[AvitoAccount]]:
    async def _make(
        avito_user_id: int = 111222333,
        *,
        status: str = "active",
        webhook_secret: str = "whsec-test",
        title: str = "LP-Test",
    ) -> AvitoAccount:
        async with db_sessionmaker() as session:
            account = AvitoAccount(
                title=title,
                avito_user_id=avito_user_id,
                access_token_enc=b"enc-access",
                refresh_token_enc=b"enc-refresh",
                token_expires_at=datetime.now(UTC) + timedelta(days=1),
                status=status,
                webhook_secret=webhook_secret,
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return account

    return _make


@pytest.fixture
async def seed_conversation(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    make_avito_account: Callable[..., Awaitable[AvitoAccount]],
) -> SimpleNamespace:
    """Account + client + one conversation with one inbound message —
    базовый seed для RBAC-матрицы и API-тестов спринта 2."""
    account = await make_avito_account()
    now = datetime.now(UTC)
    async with db_sessionmaker() as session:
        client_row = Client(channel="avito", external_id="999001", name="Иван Петров")
        session.add(client_row)
        await session.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"chat-{_uuid.uuid4().hex[:8]}",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            unread_count=1,
            last_message_at=now,
        )
        session.add(conv)
        await session.flush()
        msg = Message(
            conversation_id=conv.id,
            external_message_id="am-seed-1",
            direction="in",
            sender_type="client",
            body="Здравствуйте! Экран разбит, почём?",
            attachments=[],
            delivery_status="delivered",
            created_at=now,
        )
        session.add(msg)
        await session.commit()
        return SimpleNamespace(
            account=account,
            client_id=client_row.id,
            conversation_id=conv.id,
            message_id=msg.id,
        )


async def drain_events(pubsub, *, max_misses: int = 2) -> list[dict]:
    """Собрать все события из Pub/Sub-подписки (fakeredis может отдать None
    на первом поллинге после subscribe — терпим пару промахов подряд)."""
    import json as _json

    events: list[dict] = []
    misses = 0
    while misses < max_misses:
        raw = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if raw is None:
            misses += 1
            continue
        misses = 0
        if raw["type"] == "message":
            events.append(_json.loads(raw["data"]))
    return events


@pytest.fixture(autouse=True)
def _без_кэша_карт():
    """Кэш ответов карт (13.09) привязывается воркером к Redis задачи; между
    тестами привязку снимаем, иначе ответ одного теста доехал бы в другой."""
    from app.services import geo_cache

    geo_cache.bind(None)
    yield
    geo_cache.bind(None)


@pytest.fixture(autouse=True)
def _без_сети_подсказчиков(monkeypatch):
    """Ahunter и Спеллер (16.09) работают без ключа и включены по умолчанию —
    без заглушки каждый тест воркера с отказом карты шёл бы в живые ahunter.ru
    и speller.yandex.net. Тест, которому нужен их ответ, подменяет сам."""
    from app.workers import geocode as worker

    async def нет_подсказок(query, **kw):  # noqa: ANN001
        return []

    async def нет_правки(street, **kw):  # noqa: ANN001
        return None

    monkeypatch.setattr(worker.ahunter, "suggest", нет_подсказок)
    monkeypatch.setattr(worker.speller, "fix_street", нет_правки)


@pytest.fixture(autouse=True)
def _шлюз_в_тестах(monkeypatch):
    """Шлюз внешних API (docs/46) в тестах «настроен» на выдуманный адрес и
    НЕ имеет ни одного ключа: снимок `/status` пуст и считается свежим, так что
    ни один тест не пойдёт ни в шлюз, ни в провайдера (ключи из окружения
    разработчика на цепочку не влияют — их у LeadChat больше нет). Тест,
    которому нужен провайдер, включает ключ сам:
    `monkeypatch.setitem(gateway.known_keys, "dadata", True)` — и подменяет
    функцию обёртки или ставит respx на http://gw.test."""
    from app.core.config import settings
    from app.integrations import gateway

    monkeypatch.setattr(settings, "gateway_url", "http://gw.test", raising=False)
    monkeypatch.setattr(settings, "gateway_token", "test-token", raising=False)
    monkeypatch.setattr(gateway, "known_keys", {})
    monkeypatch.setattr(gateway, "_без_ключа", set())
    monkeypatch.setattr(gateway, "last_status_error", None)
    monkeypatch.setattr(gateway, "_status_at", float("inf"))
