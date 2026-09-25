"""Доставка исходящих на настоящих Postgres + Redis (07 §1.2, 08 §3).

Полный тракт: ``POST /conversations/{id}/messages`` -> pending в
партиционированной таблице -> ARQ-задача ``deliver_message`` -> Авито
(мок вместо fake-avito) -> delivered + WS-событие ``message:status``.

Отдельно проверены сценарии отказа: 5 попыток с backoff и переход в
``failed``; 429 с Retry-After; аккаунт в needs_reauth; ``/retry``,
возвращающий сообщение в очередь; нарезка текста длиннее 1000 символов.
"""

import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from arq import Retry
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import deps
from app.api.routes import messages as messages_routes
from app.core.security import create_access_token, hash_password
from app.integrations.avito.errors import (
    AvitoApiError,
    AvitoAuthError,
    AvitoUnavailable,
    RateLimited,
)
from app.main import create_app
from app.models import AvitoAccount, Client, Conversation, Message, User
from app.services import crypto
from app.services.avito_text import AVITO_TEXT_LIMIT
from app.workers import deliver as deliver_mod
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

NOW = datetime.now(UTC).replace(microsecond=0)


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def pg_sessionmaker(pg_async_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean(pg_sessionmaker, redis):
    async with pg_sessionmaker() as s:
        await s.execute(
            text(
                "TRUNCATE webhook_raw_log, messages, audit_log, conversations, "
                "clients, avito_accounts, users CASCADE"
            )
        )
        await s.commit()
    await redis.flushdb()


@pytest.fixture
async def seed(pg_sessionmaker):
    """Аккаунт с настоящими зашифрованными токенами + клиент + диалог + менеджер."""
    from types import SimpleNamespace

    async with pg_sessionmaker() as s:
        account = AvitoAccount(
            title="LP-Доставка",
            avito_user_id=111222333,
            access_token_enc=crypto.encrypt_token("access-token"),
            refresh_token_enc=crypto.encrypt_token("refresh-token"),
            token_expires_at=NOW + timedelta(days=1),
            status="active",
            webhook_secret="whsec-delivery",
        )
        manager = User(
            email="manager@delivery.test",
            password_hash=hash_password("correct horse battery staple"),
            full_name="Анна Смирнова",
            role="manager",
            is_active=True,
        )
        admin = User(
            email="admin@delivery.test",
            password_hash=hash_password("correct horse battery staple"),
            full_name="Админ",
            role="admin",
            is_active=True,
        )
        client_row = Client(channel="avito", external_id="923456789", name="Иван Петров")
        s.add_all([account, manager, admin, client_row])
        await s.flush()
        conv = Conversation(
            channel="avito",
            external_chat_id=f"fa-chat-{uuid.uuid4().hex[:12]}",
            account_id=account.id,
            client_id=client_row.id,
            status="new",
            unread_count=1,
            last_message_at=NOW,
        )
        s.add(conv)
        await s.commit()
        return SimpleNamespace(
            account=account,
            manager=manager,
            admin=admin,
            conversation=conv,
            token=create_access_token(user_id=str(manager.id), role="manager"),
            admin_token=create_access_token(user_id=str(admin.id), role="admin"),
        )


@pytest.fixture
async def api(pg_sessionmaker, redis) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    # app/main.py — чужая зона: роутер отправки монтируем в тесте
    app.include_router(messages_routes.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with pg_sessionmaker() as session:
            yield session

    app.dependency_overrides[deps.get_db] = override_get_db
    app.dependency_overrides[deps.get_redis] = lambda: redis
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest.fixture
def ctx(pg_sessionmaker, redis) -> dict:
    return {"db_session_factory": pg_sessionmaker, "redis": redis}


# ------------------------------------------------------------------- helpers


class FakeAvito:
    """Мок ``POST /messenger/v1/accounts/{uid}/chats/{id}/messages``."""

    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.sent: list[str] = []
        self.tokens: list[str] = []

    # Инстанс кладётся в класс вместо метода: дескриптор не срабатывает,
    # поэтому self адаптера сюда не приходит — сигнатура без него.
    async def __call__(self, token, user_id, chat_id, text_):  # noqa: ANN001
        self.tokens.append(token)
        if self.error is not None:
            raise self.error
        self.sent.append(text_)
        return f"fa-msg-{len(self.sent)}"


def install(monkeypatch, fake: FakeAvito) -> FakeAvito:
    monkeypatch.setattr(deliver_mod._OutboundClient, "send_message", fake)
    return fake


async def send(api, seed, text_: str = "Замена экрана — от 8 900 ₽", token: str | None = None):
    return await api.post(
        f"/api/v1/conversations/{seed.conversation.id}/messages",
        json={"text": text_, "client_message_id": str(uuid.uuid4())},
        headers={"Authorization": f"Bearer {token or seed.token}"},
    )


async def get_message(pg_sessionmaker, message_id) -> Message:
    async with pg_sessionmaker() as s:
        return (await s.execute(select(Message).where(Message.id == message_id))).scalar_one()


def defer_ms(retry: Retry) -> int:
    """Задержка из arq.Retry — в миллисекундах."""
    assert retry.defer_score is not None
    return retry.defer_score


async def collect_events(pubsub, *, at_most: int, timeout: float = 2.0) -> list[dict]:
    events: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(events) < at_most:
        msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.1)
        if msg and msg["type"] == "message":
            events.append(json.loads(msg["data"]))
    return events


# --------------------------------------------------------------------- tests


async def test_full_delivery_path(api, ctx, seed, redis, pg_sessionmaker, monkeypatch):
    """POST -> pending -> воркер -> delivered + message:status (08 §3)."""
    fake = install(monkeypatch, FakeAvito())

    r = await send(api, seed)
    assert r.status_code == 201, r.text
    message_id = uuid.UUID(r.json()["id"])
    assert r.json()["delivery_status"] == "pending"

    # джоба реально поставлена в очередь ARQ этим же Redis'ом
    assert await redis.zcard("arq:queue") == 1

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

    row = await get_message(pg_sessionmaker, message_id)
    assert row.delivery_status == "delivered"
    assert row.external_message_id == "fa-msg-1"
    assert fake.sent == ["Замена экрана — от 8 900 ₽"]
    assert fake.tokens == ["access-token"]  # токен расшифрован из БД

    (event,) = await collect_events(pubsub, at_most=1)
    assert event["type"] == "message:status"
    assert event["data"]["message_id"] == str(message_id)
    assert event["data"]["delivery_status"] == "delivered"
    assert "error" not in event["data"]
    await pubsub.aclose()


async def test_delivery_is_idempotent_to_double_job(api, ctx, seed, pg_sessionmaker, monkeypatch):
    """Повторная постановка джобы не даёт второй отправки (08 §3/§8.4)."""
    fake = install(monkeypatch, FakeAvito())
    message_id = uuid.UUID((await send(api, seed)).json()["id"])

    await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)
    await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

    assert len(fake.sent) == 1
    assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "delivered"


async def test_five_attempts_then_failed(api, ctx, seed, redis, pg_sessionmaker, monkeypatch):
    """5xx: попытки 1–4 просят Retry, пятая переводит в failed (01 §6.2)."""
    install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 500", status=500)))
    message_id = uuid.UUID((await send(api, seed)).json()["id"])

    delays = []
    for attempt in range(1, deliver_mod.MAX_TRIES):
        with pytest.raises(Retry) as exc:
            await deliver_mod.deliver_message({**ctx, "job_try": attempt}, message_id)
        delays.append(defer_ms(exc.value))  # arq хранит задержку в миллисекундах
        assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "pending"

    # экспоненциальный рост 1, 2, 4, 8 c (+ джиттер ±20%)
    assert all(delays[i] < delays[i + 1] for i in range(len(delays) - 1)), delays
    assert 800 <= delays[0] <= 1200 and 6400 <= delays[-1] <= 9600

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)

    row = await get_message(pg_sessionmaker, message_id)
    assert row.delivery_status == "failed"
    (event,) = await collect_events(pubsub, at_most=1)
    assert event["data"]["delivery_status"] == "failed"
    assert event["data"]["error_code"] == "delivery_exhausted"
    assert event["data"]["error"]
    await pubsub.aclose()


async def test_chat_gone_fails_at_once_with_the_reason(
    api, ctx, seed, redis, pg_sessionmaker, monkeypatch
):
    """404 на отправку — сразу «не доставлено» с причиной, без пяти попыток (проверка 24.09).

    Чат удалён или клиент заблокировал: повтор не изменит ответа Авито. Было —
    пять попыток впустую и текст «не доставлено после 5 попыток» без причины.
    """
    fake = install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 404", status=404)))
    message_id = uuid.UUID((await send(api, seed)).json()["id"])

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)  # без Retry

    assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "failed"
    assert len(fake.tokens) == 1, "попытка должна быть одна"
    (event,) = await collect_events(pubsub, at_most=1)
    assert event["data"]["error"] == deliver_mod.CHAT_GONE_ERROR
    assert event["data"]["error_code"] == "delivery_failed"
    await pubsub.aclose()


async def test_server_error_is_still_retried(api, ctx, seed, pg_sessionmaker, monkeypatch):
    """5xx — временное: первая попытка просит повтор, как и раньше."""
    install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 503", status=503)))
    message_id = uuid.UUID((await send(api, seed)).json()["id"])

    with pytest.raises(Retry):
        await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)
    assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "pending"


class TestAttachmentsNeverVanishQuietly:
    """Файл, который Авито от нас не берёт, уйти не может — и притворяться нельзя.

    ⚠ ПРАВИЛО ПОМЕНЯЛОСЬ 07.09, И ЭТИ ПРОВЕРКИ ПОМЕНЯЛИСЬ ВМЕСТЕ С НИМ.
    Раньше отказом кончалось ЛЮБОЕ вложение, и здесь стояло `kind: "image"`.
    Теперь картинка уходит клиенту (`uploadImages` + `messages/image`,
    docs/26-AVITO-API-CATALOG.md стр. 49 и 63), а вот произвольный файл не
    уйдёт никогда — отдельного метода в каталоге Авито нет. Поэтому предмет
    проверки здесь — pdf; путь картинки целиком закрыт
    tests/unit/test_deliver_images.py.

    Смысл остался прежним: сообщение «текст + файл» уходило как ОДИН ТЕКСТ.
    Файл исчезал с записью в лог, а оператор видел галочку «доставлено».
    Приложил смету с подписью «вот смета» — клиент прочитал подпись без
    сметы. Половина сообщения — не доставка.
    """

    @staticmethod
    async def _attach(pg_sessionmaker, message_id, *, drop_text: bool = False):
        """Вешаем вложение прямо в строку.

        Через ручку не выйдет: она требует `media_id`, живущий в Redis после
        настоящей загрузки файла. Проверяем ветку ВОРКЕРА, а загрузка к ней
        отношения не имеет — незачем тащить в тест ещё и её.
        """
        async with pg_sessionmaker() as s:
            row = (await s.execute(select(Message).where(Message.id == message_id))).scalar_one()
            row.attachments = [
                {
                    "media_id": "m-1",
                    "kind": "file",
                    "url": "/x",
                    "name": "смета.pdf",
                    "size": 8,
                }
            ]
            if drop_text:
                row.body = ""
            await s.commit()

    async def test_text_with_a_file_is_not_delivered_as_text_alone(
        self, api, ctx, seed, redis, pg_sessionmaker, monkeypatch
    ):
        fake = install(monkeypatch, FakeAvito())
        message_id = uuid.UUID((await send(api, seed, "вот смета")).json()["id"])
        await self._attach(pg_sessionmaker, message_id)

        pubsub = redis.pubsub()
        await pubsub.subscribe("events")
        await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

        # Главное: в Авито не ушло НИЧЕГО. Раньше здесь был голый текст.
        assert fake.sent == []

        row = await get_message(pg_sessionmaker, message_id)
        assert row.delivery_status == "failed"

        (event,) = await collect_events(pubsub, at_most=1)
        assert event["data"]["error_code"] == "attachments_unsupported"
        # Текст отказа обязан называть ограничение: прежнее «пока недоступна»
        # обещало, что скоро включат, и оператор прикладывал pdf ещё раз.
        assert "изображения" in event["data"]["error"]
        await pubsub.aclose()

    async def test_a_file_without_text_still_fails(
        self, api, ctx, seed, pg_sessionmaker, monkeypatch
    ):
        """Прежнее поведение сохранено — оно и было правильным."""
        fake = install(monkeypatch, FakeAvito())
        message_id = uuid.UUID((await send(api, seed, "будет смета")).json()["id"])
        await self._attach(pg_sessionmaker, message_id, drop_text=True)

        await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

        assert fake.sent == []
        assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "failed"

    async def test_plain_text_is_untouched(self, api, ctx, seed, pg_sessionmaker, monkeypatch):
        """Сторож против перегиба: без вложений доставка прежняя."""
        fake = install(monkeypatch, FakeAvito())
        message_id = uuid.UUID((await send(api, seed, "цена 8900")).json()["id"])

        await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

        assert fake.sent == ["цена 8900"]
        assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "delivered"


async def test_retry_returns_failed_message_to_pending_and_delivers(
    api, ctx, seed, pg_sessionmaker, monkeypatch
):
    """01 §6.3: /retry -> pending -> новая джоба -> delivered."""
    install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 500", status=500)))
    message_id = uuid.UUID((await send(api, seed)).json()["id"])
    await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)
    assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "failed"

    r = await api.post(
        f"/api/v1/messages/{message_id}/retry",
        headers={"Authorization": f"Bearer {seed.token}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["delivery_status"] == "pending"
    assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "pending"

    fake = install(monkeypatch, FakeAvito())  # Авито «починился»
    await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)
    row = await get_message(pg_sessionmaker, message_id)
    assert row.delivery_status == "delivered"
    assert row.external_message_id == "fa-msg-1"
    assert len(fake.sent) == 1


async def test_rate_limited_respects_retry_after(api, ctx, seed, pg_sessionmaker, monkeypatch):
    """429: ждём не меньше Retry-After, сообщение остаётся pending (07 §2.3)."""
    install(monkeypatch, FakeAvito(error=RateLimited(retry_after=42)))
    message_id = uuid.UUID((await send(api, seed)).json()["id"])

    with pytest.raises(Retry) as exc:
        await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)
    assert defer_ms(exc.value) >= 42_000  # мс
    assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "pending"


async def test_rate_limited_gives_up_on_last_try(api, ctx, seed, pg_sessionmaker, monkeypatch):
    """У лимита тоже есть дно: на последней попытке сообщение помечается неотправленным.

    НАЙДЕНО ОБХОДОМ КОДА 12 августа. Ветка `RateLimited` повторяла безусловно,
    а соседняя (5xx) считала попытки и на исходе бюджета звала `_fail`. ARQ при
    `job_try > max_tries` обрывает задачу, НЕ ВЫЗЫВАЯ тело функции, — значит на
    затяжном 429 `_fail` не звался никогда. Сообщение оставалось `pending`
    навсегда: оператор видел его отправленным, клиент не получал ничего,
    уведомления не было, а отметка «клиент ждёт» к этому времени уже снята.

    Соседний тест выше проверяет ПЕРВУЮ попытку (ждём Retry-After и остаёмся
    pending) — и он был единственным на 429, поэтому дыру и не поймали.
    """
    install(monkeypatch, FakeAvito(error=RateLimited(retry_after=42)))
    message_id = uuid.UUID((await send(api, seed)).json()["id"])

    # Последняя попытка бюджета: повторять больше нечем.
    await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)

    row = await get_message(pg_sessionmaker, message_id)
    assert row.delivery_status == "failed", (
        "сообщение осталось pending — оператор считает его отправленным"
    )
    # Текст отказа модель не хранит — он уходит в WS-кадр и в журнал; здесь
    # проверяем сам факт, что сообщение перестало числиться отправляемым.


async def test_needs_reauth_fails_immediately_and_notifies_admins(
    api, ctx, seed, redis, pg_sessionmaker, monkeypatch
):
    """Аккаунт не active — доставлять некому: сразу failed + событие админам."""
    fake = install(monkeypatch, FakeAvito())
    message_id = uuid.UUID((await send(api, seed)).json()["id"])

    async with pg_sessionmaker() as s:
        account = await s.get(AvitoAccount, seed.account.id)
        account.status = "needs_reauth"
        await s.commit()

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

    row = await get_message(pg_sessionmaker, message_id)
    assert row.delivery_status == "failed"
    assert fake.sent == []  # в Авито даже не ходили

    events = await collect_events(pubsub, at_most=2)
    types = {e["type"] for e in events}
    assert "message:status" in types and "account:needs_reauth" in types
    status = next(e for e in events if e["type"] == "message:status")
    assert status["data"]["error_code"] == "account_needs_reauth"
    await pubsub.aclose()


async def test_auth_error_triggers_single_token_refresh(
    api, ctx, seed, pg_sessionmaker, monkeypatch
):
    """401 -> ровно один авто-рефреш; если он не помог — failed (DESIGN §8.2)."""
    fake = install(monkeypatch, FakeAvito(error=AvitoAuthError()))
    refreshes = []

    async def fake_refresh(account, db, redis_):  # noqa: ANN001
        refreshes.append(account.id)
        account.status = "needs_reauth"
        await db.commit()
        return False

    monkeypatch.setattr("app.services.avito_accounts.refresh_tokens", fake_refresh)
    message_id = uuid.UUID((await send(api, seed)).json()["id"])
    await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

    assert refreshes == [seed.account.id]  # ровно один, не цикл
    assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "failed"
    assert len(fake.tokens) == 1


class TestRefreshFailureIsNotAlwaysRevokedAccess:
    """«Токен не обновился» и «доступ отозвали» — разные беды и разная цена.

    НАЙДЕНО ОБХОДОМ КОДА 12 августа. `refresh_tokens` возвращает False и на
    ОБЫЧНОЙ недоступности Авито — там это записано прямым текстом
    («ВРЕМЕННАЯ БЕДА — СТАТУС НЕ ТРОГАЕМ»). А воркер читал любой False как
    отзыв доступа: админам уходила критичная тревога «канал требует
    переподключения», сообщение хоронилось насовсем БЕЗ ЕДИНОГО ПОВТОРА.

    Цена: ночная тревога на исправном канале — человек идёт переподключать
    то, с чем всё в порядке, — и потерянный ответ клиенту из-за минутного
    сбоя Авито. Отличаем по единственному честному признаку: настоящий отзыв
    сервис уже записал в статус аккаунта.
    """

    @staticmethod
    def _refresh_fails_quietly(monkeypatch) -> None:
        """Авито не ответил на обмен токена: False, статус НЕ тронут."""

        async def fake_refresh(account, db, redis_):  # noqa: ANN001
            return False

        monkeypatch.setattr("app.services.avito_accounts.refresh_tokens", fake_refresh)

    async def test_temporary_failure_retries_and_keeps_quiet(
        self, api, ctx, seed, redis, pg_sessionmaker, monkeypatch
    ):
        install(monkeypatch, FakeAvito(error=AvitoAuthError()))
        self._refresh_fails_quietly(monkeypatch)
        message_id = uuid.UUID((await send(api, seed)).json()["id"])

        pubsub = redis.pubsub()
        await pubsub.subscribe("events")
        with pytest.raises(Retry):  # раньше здесь молча наступал failed
            await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

        row = await get_message(pg_sessionmaker, message_id)
        assert row.delivery_status == "pending", "доставку похоронили на временном сбое"

        events = await collect_events(pubsub, at_most=1, timeout=0.5)
        assert events == [], "админам ушла тревога о переподключении исправного канала"
        await pubsub.aclose()

        # и сам аккаунт остался рабочим — его никто не выключал
        async with pg_sessionmaker() as s:
            assert (await s.get(AvitoAccount, seed.account.id)).status == "active"

    async def test_temporary_failure_ends_as_exhausted_not_as_reauth(
        self, api, ctx, seed, redis, pg_sessionmaker, monkeypatch
    ):
        """Бюджет повторов кончился — отказ честный, но причина не подменена."""
        install(monkeypatch, FakeAvito(error=AvitoAuthError()))
        self._refresh_fails_quietly(monkeypatch)
        message_id = uuid.UUID((await send(api, seed)).json()["id"])

        pubsub = redis.pubsub()
        await pubsub.subscribe("events")
        await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)

        assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "failed"
        events = await collect_events(pubsub, at_most=2, timeout=1.0)
        assert "account:needs_reauth" not in {e["type"] for e in events}
        status = next(e for e in events if e["type"] == "message:status")
        assert status["data"]["error_code"] == "delivery_exhausted"
        await pubsub.aclose()

    async def test_real_revocation_still_fails_at_once(
        self, api, ctx, seed, redis, pg_sessionmaker, monkeypatch
    ):
        """Сторож против перегиба: настоящий отзыв по-прежнему хоронит сразу."""
        install(monkeypatch, FakeAvito(error=AvitoAuthError()))

        async def fake_refresh(account, db, redis_):  # noqa: ANN001
            account.status = "needs_reauth"
            await db.commit()
            return False

        monkeypatch.setattr("app.services.avito_accounts.refresh_tokens", fake_refresh)
        message_id = uuid.UUID((await send(api, seed)).json()["id"])

        pubsub = redis.pubsub()
        await pubsub.subscribe("events")
        await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

        assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "failed"
        events = await collect_events(pubsub, at_most=2, timeout=1.0)
        assert "account:needs_reauth" in {e["type"] for e in events}
        await pubsub.aclose()


async def test_long_text_is_split_into_avito_sized_parts(
    api, ctx, seed, pg_sessionmaker, monkeypatch
):
    """01 §6.2: воркер сам режет длинный текст по лимиту Авито (~1000)."""
    fake = install(monkeypatch, FakeAvito())
    long_text = "\n".join(f"Строка прайса номер {i:03d} — цена 1500 рублей." for i in range(60))
    assert len(long_text) > AVITO_TEXT_LIMIT

    message_id = uuid.UUID((await send(api, seed, long_text)).json()["id"])
    await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

    assert len(fake.sent) > 1
    assert all(len(p) <= AVITO_TEXT_LIMIT for p in fake.sent)
    assert "".join(fake.sent).replace("\n", "") == long_text.replace("\n", "")
    row = await get_message(pg_sessionmaker, message_id)
    assert row.delivery_status == "delivered"
    assert row.body == long_text  # в БД лежит целый текст, режется только отправка


class TestSplitMessageResumesInsteadOfRepeating:
    """Разрезанный ответ продолжается с места обрыва, а не с начала.

    НАЙДЕНО ОБХОДОМ КОДА 12 августа. Прайс на 2500 знаков уходит тремя
    сообщениями. Авито ответил 500 на третьем — повтор джобы начинал с нуля,
    и клиент получал первые два куска ВТОРОЙ раз. При пяти попытках это до
    девяти лишних сообщений: стена дублей, в которой не найти цену, оператор
    со стороны выглядит сломанным ботом, а Авито за такой поток ещё и режет
    лимит. Номер отправленной части — единственное, чего не знают ни база,
    ни ARQ, поэтому он живёт в Redis по `message_id`.
    """

    LONG_TEXT = "\n".join(f"Строка прайса номер {i:03d} — цена 1500 рублей." for i in range(60))

    class BreaksAfter:
        """Отдаёт первые `ok` частей, на следующей роняет 500."""

        def __init__(self, ok: int) -> None:
            self.ok = ok
            self.sent: list[str] = []

        async def __call__(self, token, user_id, chat_id, text_):  # noqa: ANN001
            if len(self.sent) >= self.ok:
                raise AvitoApiError("Авито: 500", status=500)
            self.sent.append(text_)
            return f"fa-msg-{len(self.sent)}"

    async def test_retry_sends_only_the_tail(self, api, ctx, seed, pg_sessionmaker, monkeypatch):
        parts = deliver_mod.split_text(self.LONG_TEXT)
        assert len(parts) >= 3, "текст обязан резаться минимум на три части"

        message_id = uuid.UUID((await send(api, seed, self.LONG_TEXT)).json()["id"])

        broken = self.BreaksAfter(ok=2)
        monkeypatch.setattr(deliver_mod._OutboundClient, "send_message", broken)
        with pytest.raises(Retry):
            await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)
        assert broken.sent == parts[:2]  # два куска клиент уже прочитал

        healed = install(monkeypatch, FakeAvito())  # Авито починился
        await deliver_mod.deliver_message({**ctx, "job_try": 2}, message_id)

        assert healed.sent == parts[2:], "клиент получил уже прочитанные куски повторно"
        row = await get_message(pg_sessionmaker, message_id)
        assert row.delivery_status == "delivered"
        assert row.external_message_id is not None  # id последней части не потерян
        # и весь текст у клиента ровно один раз, без склейки и без пропусков
        assert broken.sent + healed.sent == parts

    async def test_operator_retry_does_not_repeat_the_head(
        self, api, ctx, seed, pg_sessionmaker, monkeypatch
    ):
        """Кнопка «Повторить» после провала — тот же случай: голову не шлём."""
        parts = deliver_mod.split_text(self.LONG_TEXT)
        message_id = uuid.UUID((await send(api, seed, self.LONG_TEXT)).json()["id"])

        broken = self.BreaksAfter(ok=1)
        monkeypatch.setattr(deliver_mod._OutboundClient, "send_message", broken)
        await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)
        assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "failed"

        r = await api.post(
            f"/api/v1/messages/{message_id}/retry",
            headers={"Authorization": f"Bearer {seed.token}"},
        )
        assert r.status_code == 200, r.text

        healed = install(monkeypatch, FakeAvito())
        await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)

        assert healed.sent == parts[1:], "повтор начал длинный ответ заново"
        assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "delivered"


async def test_send_writes_audit_and_assigns_on_postgres(api, seed, pg_sessionmaker, monkeypatch):
    """Автоназначение и audit по 06 §0.3 — на настоящей схеме с партициями."""
    install(monkeypatch, FakeAvito())
    assert (await send(api, seed)).status_code == 201

    async with pg_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation.id)
        assert conv.status == "in_progress"
        assert conv.assignee_id == seed.manager.id
        actions = [
            row[0]
            for row in (
                await s.execute(text("SELECT action FROM audit_log ORDER BY created_at"))
            ).all()
        ]
    assert "conversation.assigned" in actions
    assert "conversation.status_changed" in actions


async def test_message_lands_in_monthly_partition(api, seed, pg_sessionmaker, monkeypatch):
    """08 §6.3: исходящее пишется в партицию текущего месяца, не в родителя."""
    install(monkeypatch, FakeAvito())
    message_id = uuid.UUID((await send(api, seed)).json()["id"])

    async with pg_sessionmaker() as s:
        table = (
            await s.execute(
                text("SELECT tableoid::regclass::text FROM messages WHERE id = :id"),
                {"id": message_id},
            )
        ).scalar_one()
    assert table.startswith("messages_y"), table


# ------------------------------------------- обогащение клиента (хвост «а»)


def install_chats(monkeypatch, chats: list[dict]) -> list[str]:
    """Мок обоих путей ``client_enrich``: точечного GET чата и списка чатов.

    Никаких настоящих HTTP-запросов из тестов — оба метода подменены.
    """
    calls: list[str] = []

    async def fake_request(self, method, path, **kw):  # noqa: ANN001
        calls.append(path)
        return httpx.Response(404, json={"error": "not implemented in fake-avito"})

    async def fake_get_chats(self, token, user_id, *, offset=0, limit=100, unread_only=False):  # noqa: ANN001
        calls.append("chats")
        return chats if offset == 0 else []

    monkeypatch.setattr("app.integrations.avito.client.AvitoClient._request", fake_request)
    monkeypatch.setattr("app.integrations.avito.client.AvitoClient.get_chats", fake_get_chats)
    return calls


async def test_enrich_client_fills_name_and_publishes_update(
    ctx, seed, redis, pg_sessionmaker, monkeypatch
):
    """Хвост «а»: вебхук v3 имени не несёт — тянем его лениво из API Авито."""
    from app.services.client_enrich import enrich_client

    async with pg_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation.id)
        client_row = await s.get(Client, conv.client_id)
        client_row.name = None  # так выглядит клиент, созданный из вебхука
        await s.commit()

    install_chats(
        monkeypatch,
        [
            {
                "id": seed.conversation.external_chat_id,
                "users": [
                    {"id": seed.account.avito_user_id, "name": "LP-Доставка"},
                    {"id": 923456789, "name": "Иван Петров"},
                ],
            }
        ],
    )

    pubsub = redis.pubsub()
    await pubsub.subscribe("events")
    await enrich_client(ctx, seed.conversation.id)

    async with pg_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation.id)
        assert (await s.get(Client, conv.client_id)).name == "Иван Петров"

    (event,) = await collect_events(pubsub, at_most=1)
    assert event["type"] == "conversation:updated"
    assert event["data"]["patch"]["client"]["name"] == "Иван Петров"
    await pubsub.aclose()


async def test_enrich_client_is_noop_when_nothing_is_missing(
    ctx, seed, pg_sessionmaker, monkeypatch
):
    """Идемпотентность: узнавать нечего — в Авито не ходим.

    С 11 августа у задачи ДВА повода (docs/33 §14а): имя клиента и объявление.
    С 15 августа — ТРЕТИЙ: фото клиента (просьба владельца со снимками Jivo).
    С 2 сентября — ЧЕТВЁРТЫЙ: ссылка на профиль клиента (просьба владельца).
    Раньше «имя известно» означало «делать нечего», и диалог с пустым
    объявлением оставался с прочерком навсегда. Поэтому холостым прогон
    считается, только когда известно ВСЁ ЧЕТВЕРО.

    ⚠ У ЧЕТВЁРТОГО ПОВОДА ГАСИТ ЕГО НЕ ЗНАЧЕНИЕ, А ОТМЕТКА О ВОПРОСЕ.
    Ссылки у клиента может не быть вовсе — Авито не обязан её давать. Считай мы
    поводом пустоту самой ссылки, этот клиент гонял бы нас в чужой API на
    каждое своё сообщение вечно. Поэтому «спрашивали» пишется отдельной
    отметкой, и здесь ставится именно она.
    """
    from app.services.client_enrich import enrich_client

    async with pg_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation.id)
        conv.item_title = "Ремонт холодильников"  # объявление тоже известно
        client_row = await s.get(Client, conv.client_id)
        client_row.avatar_url = "https://static.avito.ru/i/x.png"  # и фото известно
        client_row.profile_checked_at = datetime(2026, 9, 2, tzinfo=UTC)  # и про профиль спросили
        await s.commit()

    calls = install_chats(monkeypatch, [])
    await enrich_client(ctx, seed.conversation.id)  # имя «Иван Петров» уже в seed

    assert calls == []
    async with pg_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation.id)
        assert (await s.get(Client, conv.client_id)).name == "Иван Петров"


async def test_enrich_client_goes_for_the_item_even_when_name_is_known(
    ctx, seed, pg_sessionmaker, monkeypatch
):
    """Имя известно, объявления нет — за карточкой чата всё равно идём.

    Это ровно тот случай, который давал пустую колонку «Объявление» на боевых
    аккаунтах: клиент уже знаком (второе обращение), поэтому прежний повод
    «новый клиент без имени» не срабатывал, а объявления в вебхуке нет никогда.
    """
    from app.services.client_enrich import enrich_client

    async with pg_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation.id)
        assert conv.item_title is None
        chat_id = conv.external_chat_id

    calls = install_chats(
        monkeypatch,
        [
            {
                "id": chat_id,
                "users": [{"id": seed.account.avito_user_id, "name": "Мы"}],
                "context": {
                    "type": "item",
                    "value": {"title": "Ремонт варочных панелей", "url": "https://avito.ru/i/5"},
                },
            }
        ],
    )
    await enrich_client(ctx, seed.conversation.id)

    assert calls, "за карточкой чата обязаны сходить"
    async with pg_sessionmaker() as s:
        conv = await s.get(Conversation, seed.conversation.id)
        assert conv.item_title == "Ремонт варочных панелей"


# ===========================================================================
#  #26 — неотправленный ответ виден в списке слева, а не только в ленте
# ===========================================================================


class TestUndeliveredIsVisibleWhereDecisionsAreMade:
    """Отправка упала — узнать об этом можно было только в открытом диалоге.

    ЧТО БЫЛО. Красный крест рисовался в ленте, а строка в левом списке
    выглядела успешно отвеченной: «Вы: перезвоню в течение часа», ничего
    красного. Оператор уходил к следующему клиенту и не возвращался.

    И вторая поломка, дороже первой: отметка «клиент ждёт» гасится в момент
    нажатия «Отправить» — ДО попытки доставки, — и при провале не возвращалась.
    Значит сторож «клиент ждёт 15 минут» на такой диалог не срабатывал никогда
    — ровно там, где он нужнее всего.

    Тесты идут на настоящем PostgreSQL намеренно: оба признака считаются
    агрегатами по `messages`, а это партиционированная таблица.
    """

    @staticmethod
    async def _client_wrote(pg_sessionmaker, conv_id, when: datetime) -> None:
        """Входящее от клиента + отметка ожидания — как ставит приём вебхука."""
        async with pg_sessionmaker() as s:
            s.add(
                Message(
                    conversation_id=conv_id,
                    direction="in",
                    sender_type="client",
                    body="Здравствуйте, экран разбит",
                    attachments=[],
                    delivery_status="delivered",
                    created_at=when,
                )
            )
            conv = await s.get(Conversation, conv_id)
            conv.awaiting_since = when
            await s.commit()

    @staticmethod
    async def _conv(pg_sessionmaker, conv_id) -> Conversation:
        async with pg_sessionmaker() as s:
            return await s.get(Conversation, conv_id)

    async def test_failed_reply_marks_the_row_and_restores_the_wait(
        self, api, ctx, seed, pg_sessionmaker, monkeypatch
    ):
        """Главная проверка класса."""
        wrote_at = NOW - timedelta(minutes=20)
        await self._client_wrote(pg_sessionmaker, seed.conversation.id, wrote_at)
        install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 500", status=500)))

        message_id = uuid.UUID((await send(api, seed)).json()["id"])
        # Отправка погасила ожидание ещё до попытки доставки — так и было.
        assert (await self._conv(pg_sessionmaker, seed.conversation.id)).awaiting_since is None

        await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)

        conv = await self._conv(pg_sessionmaker, seed.conversation.id)
        assert conv.undelivered_at is not None, "строка в списке обязана покраснеть"
        # Ждёт клиент с того момента, как НАПИСАЛ, а не с момента провала:
        # иначе двадцать минут молчания просто исчезли бы из отсчёта.
        assert conv.awaiting_since == wrote_at

    async def test_the_row_turns_red_in_the_same_frame_as_the_bubble(
        self, api, ctx, seed, redis, pg_sessionmaker, monkeypatch
    ):
        """Патч строки едет тем же событием, что и судьба пузыря.

        Двумя кадрами было бы нельзя: между ними существует момент, когда
        сообщение уже провалено, а строка ещё зелёная, — и именно в этот момент
        человек решает, идти ли к следующему клиенту.
        """
        await self._client_wrote(pg_sessionmaker, seed.conversation.id, NOW - timedelta(minutes=5))
        install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 500", status=500)))
        message_id = uuid.UUID((await send(api, seed)).json()["id"])

        pubsub = redis.pubsub()
        await pubsub.subscribe("events")
        await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)
        events = await collect_events(pubsub, at_most=3)
        await pubsub.aclose()

        status = next(e for e in events if e["type"] == "message:status")
        assert status["data"]["delivery_status"] == "failed"
        assert status["data"]["conversation_patch"]["undelivered"] is True
        # ⚠ ИМЯ ПОЛЯ ПРОВЕРЯЕМ ТО, КОТОРОЕ ЧИТАЕТ ЭКРАН. Прежняя редакция этой
        # строки сверяла `awaiting_since` — серверное имя, которого во фронте
        # нет ни одного (шкалу считает `waiting_since`, shared/lib/waiting.ts).
        # Проверка зеленела, а восстановленное ожидание до строки списка не
        # доезжало: сообщение, не ушедшее клиенту, висело без тревоги.
        assert "awaiting_since" not in status["data"]["conversation_patch"]
        assert status["data"]["conversation_patch"]["waiting_since"]

    async def test_author_gets_a_notification(self, api, ctx, seed, pg_sessionmaker, monkeypatch):
        """Колокольчик — АВТОРУ. К этому моменту он уже в следующем диалоге.

        Красная строка помогает только тому, кто на список смотрит; уведомление
        — единственное, что найдёт человека на другом экране.
        """
        from app.models.notification import Notification

        install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 500", status=500)))
        message_id = uuid.UUID((await send(api, seed)).json()["id"])

        await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)

        async with pg_sessionmaker() as s:
            rows = (
                (
                    await s.execute(
                        select(Notification).where(Notification.kind == "message.undelivered")
                    )
                )
                .scalars()
                .all()
            )
        assert len(rows) == 1
        assert rows[0].recipient_id == seed.manager.id
        assert str(seed.conversation.id) == rows[0].entity_id

    async def test_retry_clears_the_mark_at_once(
        self, api, ctx, seed, pg_sessionmaker, monkeypatch
    ):
        """Нажали «Повторить» — метка гаснет, не дожидаясь исхода.

        Сообщение снова в пути, и висящая метка означала бы «сделай
        что-нибудь» там, где делать уже нечего. Если повтор тоже не пройдёт,
        метку вернёт та же ветка провала.
        """
        install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 500", status=500)))
        message_id = uuid.UUID((await send(api, seed)).json()["id"])
        await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)
        assert (await self._conv(pg_sessionmaker, seed.conversation.id)).undelivered_at is not None

        response = await api.post(
            f"/api/v1/messages/{message_id}/retry",
            headers={"Authorization": f"Bearer {seed.token}"},
        )

        assert response.status_code == 200
        assert (await self._conv(pg_sessionmaker, seed.conversation.id)).undelivered_at is None

    async def test_successful_delivery_leaves_no_mark(
        self, api, ctx, seed, pg_sessionmaker, monkeypatch
    ):
        """Обратная сторона: метка, которая стоит у всех, — не метка."""
        await self._client_wrote(pg_sessionmaker, seed.conversation.id, NOW - timedelta(minutes=3))
        install(monkeypatch, FakeAvito())
        message_id = uuid.UUID((await send(api, seed)).json()["id"])

        await deliver_mod.deliver_message(ctx, message_id)

        conv = await self._conv(pg_sessionmaker, seed.conversation.id)
        assert conv.undelivered_at is None
        assert conv.awaiting_since is None, "клиенту ответили — ждать ему нечего"

    async def test_two_failures_keep_the_earliest_moment(
        self, api, ctx, seed, pg_sessionmaker, monkeypatch
    ):
        """Второй провал не сдвигает отметку вперёд.

        «Висит с 14:32» — это про первый непрошедший ответ. Обновляй мы момент
        на каждом провале, диалог, где не уходит ничего, выглядел бы самым
        свежим — ровно наоборот.
        """
        install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 500", status=500)))
        first = uuid.UUID((await send(api, seed, "Первый ответ")).json()["id"])
        await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, first)
        earliest = (await self._conv(pg_sessionmaker, seed.conversation.id)).undelivered_at

        second = uuid.UUID((await send(api, seed, "Второй ответ")).json()["id"])
        await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, second)

        assert (await self._conv(pg_sessionmaker, seed.conversation.id)).undelivered_at == earliest

    async def test_the_row_carries_the_flag_to_the_browser(
        self, api, ctx, seed, pg_sessionmaker, monkeypatch
    ):
        """Признак обязан быть в сериализаторе строки, иначе всё выше бесполезно."""
        from app.services.conversations import conversation_out

        install(monkeypatch, FakeAvito(error=AvitoApiError("Авито: 500", status=500)))
        message_id = uuid.UUID((await send(api, seed)).json()["id"])
        await deliver_mod.deliver_message({**ctx, "job_try": deliver_mod.MAX_TRIES}, message_id)

        conv = await self._conv(pg_sessionmaker, seed.conversation.id)
        row = conversation_out(
            conv, account=seed.account, client=None, assignee=None, last_message=None
        )
        assert row["undelivered"] is True


async def test_timeout_after_send_does_not_duplicate_the_part(
    api, ctx, seed, redis, pg_sessionmaker, monkeypatch
):
    """Таймаут ПОСЛЕ отправки не даёт клиенту второе такое же сообщение.

    БОЕВОЙ СЛУЧАЙ (аудит 19.08, находка L-007). У отправки в Авито нет ключа
    идемпотентности. Запрос ушёл, Авито его приняло, а ответа мы не дождались —
    повтор отправлял ту же часть вторым разом, и клиент видел одно и то же
    сообщение дважды. Прогресс частей от этого не спасал: он растёт только
    после УСПЕШНОГО ответа.

    Теперь после таймаута следующая попытка сперва спрашивает сам чат: есть ли
    там уже наш текст. Есть — часть считается доставленной и не повторяется.
    """
    отправлено: list[str] = []

    async def первый_раз_таймаут(self, token, user_id, chat_id, text_):  # noqa: ANN001
        отправлено.append(text_)
        raise AvitoUnavailable(after_send=True)

    async def хвост_чата(self, token, user_id, chat_id, limit=5):  # noqa: ANN001
        return list(отправлено)  # Авито приняло: наш текст уже в чате

    monkeypatch.setattr(deliver_mod._OutboundClient, "send_message", первый_раз_таймаут)
    monkeypatch.setattr(deliver_mod._OutboundClient, "recent_own_texts", хвост_чата)

    message_id = uuid.UUID((await send(api, seed)).json()["id"])

    with pytest.raises(Retry):  # первая попытка: ушло, ответа нет
        await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)
    assert len(отправлено) == 1

    # вторая попытка обязана СВЕРИТЬСЯ и не отправлять заново
    await deliver_mod.deliver_message({**ctx, "job_try": 2}, message_id)
    assert len(отправлено) == 1, "часть ушла клиенту дважды — дубль в переписке"
    assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "delivered"


async def test_connect_failure_still_retries_the_part(
    api, ctx, seed, redis, pg_sessionmaker, monkeypatch
):
    """Не дозвонились — часть НЕ ушла, и повторить её обязаны.

    Обратная сторона той же правки: потерять ответ клиенту хуже, чем повторить.
    Отказ соединения означает «не ушло вовсе», сверяться не с чем, и вторая
    попытка отправляет ровно так же, как раньше.
    """
    попытки: list[str] = []
    сверялись: list[int] = []

    async def сначала_отказ(self, token, user_id, chat_id, text_):  # noqa: ANN001
        попытки.append(text_)
        if len(попытки) == 1:
            raise AvitoUnavailable()  # after_send=False: до Авито не дошло
        return "fa-msg-1"

    async def хвост_чата(self, token, user_id, chat_id, limit=5):  # noqa: ANN001
        сверялись.append(1)
        return []

    monkeypatch.setattr(deliver_mod._OutboundClient, "send_message", сначала_отказ)
    monkeypatch.setattr(deliver_mod._OutboundClient, "recent_own_texts", хвост_чата)

    message_id = uuid.UUID((await send(api, seed)).json()["id"])
    with pytest.raises(Retry):
        await deliver_mod.deliver_message({**ctx, "job_try": 1}, message_id)
    await deliver_mod.deliver_message({**ctx, "job_try": 2}, message_id)

    assert len(попытки) == 2, "часть обязана уйти со второй попытки"
    assert сверялись == [], "сверяться не с чем: запрос до Авито не дошёл"
    assert (await get_message(pg_sessionmaker, message_id)).delivery_status == "delivered"
