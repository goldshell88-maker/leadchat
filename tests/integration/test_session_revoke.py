"""INT: принудительный обрыв сессии на настоящих Postgres + Redis (07 §1.2).

Здесь заперт блокер приёмки Б4 целиком, а не по частям. В акте записано, что
отключённый сотрудник **продолжал получать ленту переписки 21 секунду
наблюдения и продолжал бы дальше**: строка в БД менялась, а сокет никто не
закрывал — потребитель сигнала (`app/ws/hub.py`, `control:revoked`) был
написан, отправителя не было.

Юнит-тесты (`tests/unit/test_users_admin.py`) проверяют контракт ручек на
fakeredis: событие опубликовано, ключи проставлены. Но именно того, что
провалилось на приёмке, они увидеть не могут — там нет ни живого сокета, ни
настоящего Pub/Sub, ни хаба. Поэтому здесь:

* сокет поднимается через **настоящий ASGI-цикл** приложения (тикет, accept,
  кадры, код закрытия) — без сети, но по протоколу и через тот же код ручки;
* хаб слушает **настоящий Redis Pub/Sub**, а не подменённый вызов;
* отдельным тестом проверено то, ради чего сигнал вообще идёт через шину:
  сокет, открытый **в другом процессе** uvicorn (WEB_CONCURRENCY=2, 08 §5.1),
  закрывается так же — публикация единственный способ до него дотянуться.

Проверка «лента переписки прекратилась» сделана буквально: до отключения в
шину уходит `message:new` и сотрудник его получает, после отключения — такой
же кадр в сокет не приходит.
"""

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api import deps
from app.core.security import create_access_token, hash_password, issue_refresh_token
from app.db import session as db_mod
from app.main import create_app
from app.models import AuditLog, User
from app.services.sessions import revoked_key
from app.ws import hub as hub_mod
from app.ws.events import EVENTS_CHANNEL, publish_event
from tests.integration.conftest import requires_docker

pytestmark = requires_docker

PASSWORD = "integration-pass-123"
WS_PATH = "/api/v1/ws"

WS_CLOSE_LOGOUT = 4403  # уйти на /login (01 §11.7)
WS_CLOSE_RELOGIN = 4401  # взять новый тикет и переподключиться

# Кадры, которые хаб рассылает сам по себе (01 §11.3) и которые к предмету
# этого файла отношения не имеют.
NOISE_TYPES = {"presence:online"}


# ------------------------------------------------------------------ fixtures


@pytest.fixture
async def pg_sessionmaker(pg_async_url: str) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # NullPool: у pytest-asyncio на каждый тест свой event loop — соединения
    # asyncpg из пула нельзя переиспользовать между циклами.
    engine = create_async_engine(pg_async_url, poolclass=NullPool)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture(autouse=True)
async def _clean(pg_sessionmaker, redis, monkeypatch) -> None:
    async with pg_sessionmaker() as s:
        await s.execute(text("TRUNCATE users, audit_log CASCADE"))
        await s.commit()
    await redis.flushdb()
    # 30-секундный grace presence'а (01 §11.3) пережил бы сам тест фоновой
    # задачей: она нужна проду, а здесь только мешает закрытию цикла.
    monkeypatch.setattr("app.core.config.settings.ws_presence_offline_grace_seconds", 0)


@pytest.fixture
async def app(pg_sessionmaker, redis, pg_async_url: str) -> AsyncIterator[Any]:
    """Приложение с настоящим хабом на настоящем Pub/Sub.

    Lifespan руками, а не через httpx: сокет ходит в БД мимо зависимости
    ``get_db`` (`app/api/routes/ws.py` — сессия на всё время жизни сокета
    держала бы соединение в `idle in transaction`), поэтому глобальный engine
    процесса обязан смотреть в контейнерную базу, а не в DSN из настроек.
    """
    await db_mod.dispose_engine()
    db_mod.init_engine(pg_async_url, component="test")

    application = create_app()

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with pg_sessionmaker() as session:
            yield session

    application.dependency_overrides[deps.get_db] = override_get_db
    application.dependency_overrides[deps.get_redis] = lambda: redis

    hub_mod.init_hub(redis)
    await _wait_for_subscribers(redis, 1)
    yield application
    await hub_mod.shutdown_hub()
    await db_mod.dispose_engine()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://testserver") as c:
        yield c


@pytest.fixture
async def people(pg_sessionmaker) -> SimpleNamespace:
    """Администратор (он же актор) и сотрудник, над которым ставится опыт."""
    async with pg_sessionmaker() as s:
        admin = User(
            email="admin@leadchat.test",
            password_hash=hash_password(PASSWORD),
            full_name="Ирина Админова",
            role="admin",
        )
        employee = User(
            email="manager@leadchat.test",
            password_hash=hash_password(PASSWORD),
            full_name="Пётр Ковалёв",
            role="manager",
        )
        s.add_all([admin, employee])
        await s.commit()
        await s.refresh(admin)
        await s.refresh(employee)
    return SimpleNamespace(
        admin=admin,
        employee=employee,
        admin_auth=bearer(admin),
        employee_auth=bearer(employee),
    )


def bearer(user: User) -> dict[str, str]:
    """Заголовок с действующим access-токеном (роль — снимок на момент выпуска)."""
    return {"Authorization": f"Bearer {create_access_token(user_id=str(user.id), role=user.role)}"}


# ------------------------------------------------------------------ helpers


async def _wait_for_subscribers(redis: Redis, expected: int, timeout: float = 5.0) -> None:
    """Дождаться подписки хаба на канал `events`.

    Publish в канал без подписчиков не ошибка, а тишина: без этого ожидания
    тест ловил бы гонку «отключили раньше, чем хаб успел подписаться» и
    краснел бы через раз не по делу.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        counts = await redis.pubsub_numsub(EVENTS_CHANNEL)
        if counts and int(counts[0][1]) >= expected:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"хаб не подписался на {EVENTS_CHANNEL} за {timeout} с")


class WsProbe:
    """Клиент WebSocket поверх ASGI-интерфейса приложения.

    Настоящего сокета в TCP нет, но есть всё, ради чего тест написан: разбор
    тикета, `accept`, кадры и — главное — **код закрытия**, который хаб шлёт
    сокету. httpx для этого не годится (ASGITransport умеет только HTTP), а
    поднимать uvicorn ради одного соединения — менять скорость на ничто.
    """

    def __init__(self, app: Any, ticket: str) -> None:
        self._app = app
        self._ticket = ticket
        self._to_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._from_app: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None

    @property
    def _scope(self) -> dict[str, Any]:
        return {
            "type": "websocket",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "scheme": "ws",
            "server": ("testserver", 80),
            "client": ("testclient", 50000),
            "root_path": "",
            "path": WS_PATH,
            "raw_path": WS_PATH.encode(),
            "query_string": f"ticket={self._ticket}".encode(),
            "headers": [(b"host", b"testserver")],
            "subprotocols": [],
            "state": {},
        }

    async def __aenter__(self) -> "WsProbe":
        self._task = asyncio.create_task(
            self._app(self._scope, self._to_app.get, self._from_app.put)
        )
        await self._to_app.put({"type": "websocket.connect"})
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self._to_app.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(self._task, timeout=5)

    async def next_message(self, timeout: float = 5.0) -> dict[str, Any]:
        return await asyncio.wait_for(self._from_app.get(), timeout=max(0.05, timeout))

    async def accepted(self) -> None:
        msg = await self.next_message()
        assert msg["type"] == "websocket.accept", msg

    async def expect_frame(self, type_: str, timeout: float = 5.0) -> dict[str, Any]:
        """Дождаться прикладного кадра (01 §11.2) нужного типа.

        Служебный шум пропускаем: на подключение хаб честно рассылает
        `presence:online`, и ждать «первый кадр» вместо «нужный кадр» значило
        бы писать тест, который краснеет от собственной корректной работы.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            msg = await self.next_message(deadline - loop.time())
            assert msg["type"] == "websocket.send", msg
            frame = json.loads(msg["text"])
            if frame["type"] == type_:
                return frame
            assert frame["type"] in NOISE_TYPES, f"неожиданный кадр: {frame}"

    async def closed_with(self, timeout: float = 5.0) -> int:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            msg = await self.next_message(deadline - loop.time())
            if msg["type"] == "websocket.send":
                frame = json.loads(msg["text"])
                assert frame["type"] in NOISE_TYPES, f"кадр после отключения: {frame}"
                continue
            assert msg["type"] == "websocket.close", msg
            return int(msg.get("code", 1000))

    async def gets_no_feed(self, seconds: float = 1.0) -> None:
        """Убедиться, что лента переписки в этот сокет больше не идёт."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        while True:
            try:
                msg = await self.next_message(deadline - loop.time())
            except TimeoutError:
                return
            assert msg["type"] == "websocket.send", msg
            frame = json.loads(msg["text"])
            assert frame["type"] in NOISE_TYPES, f"отключённый всё ещё читает ленту: {frame}"


class DeadDropSocket:
    """Сокет «в соседнем процессе»: хаб умеет только send_text и close."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.close_code: int | None = None

    async def send_text(self, data: str) -> None:
        self.frames.append(json.loads(data))

    async def close(self, code: int = 1000) -> None:
        self.close_code = code


@contextlib.asynccontextmanager
async def open_socket(
    app: Any, client: httpx.AsyncClient, auth: dict[str, str]
) -> AsyncIterator[WsProbe]:
    """Тикет (01 §11.1) → подключение → принятый сокет."""
    ticket = (await client.post("/api/v1/ws/ticket", headers=auth)).json()["ticket"]
    async with WsProbe(app, ticket) as probe:
        await probe.accepted()
        yield probe


async def feed_message(redis: Redis, text_: str = "Здравствуйте, ещё актуально?") -> None:
    """Кадр ленты переписки — ровно то, что отключённый продолжал читать."""
    await publish_event(
        redis,
        "message:new",
        {
            "conversation_id": str(uuid.uuid4()),
            "message": {"id": str(uuid.uuid4()), "direction": "in", "body": text_},
        },
    )


async def audit_actions(pg_sessionmaker, action: str) -> list[AuditLog]:
    async with pg_sessionmaker() as s:
        rows = await s.execute(select(AuditLog).where(AuditLog.action == action))
        return list(rows.scalars())


# ------------------------------------------------------------------ тесты


async def test_deactivation_kills_the_live_socket_and_rest_at_once(
    app, client, redis, people, pg_sessionmaker
):
    """Тот самый провал приёмки, воспроизведённый и закрытый.

    Сотрудник с живым токеном и открытым сокетом → администратор нажимает
    «Отключить» → лента переписки прекращается, сокет закрыт кодом 4403, REST
    отвечает отказом на том же самом токене.
    """
    refresh = await issue_refresh_token(redis, str(people.employee.id), remember=False)
    async with open_socket(app, client, people.employee_auth) as probe:
        # 1. до отключения человек в системе: REST отвечает, лента идёт
        alive = await client.get("/api/v1/conversations", headers=people.employee_auth)
        assert alive.status_code == 200, alive.text
        await feed_message(redis)
        await probe.expect_frame("message:new")

        # 2. администратор отключает сотрудника
        off = await client.post(
            f"/api/v1/users/{people.employee.id}/deactivate", headers=people.admin_auth
        )
        assert off.status_code == 200, off.text
        assert off.json()["user"]["is_active"] is False

        # 3. сокет закрыт хабом — не «через 21 секунду», а по событию из шины
        assert await probe.closed_with() == WS_CLOSE_LOGOUT

        # 4. лента переписки в это окно больше не приходит
        await feed_message(redis, "А это уже не для ваших глаз")
        await probe.gets_no_feed()

        # 5. REST на том же токене — отказ; refresh-цепочка мертва.
        #
        # Код именно 401, и это не придирка к цифре: проверка денилиста стоит в
        # `get_current_user` ДО загрузки пользователя из базы. Не будь пометки
        # `revoked_users:{id}`, запрос дошёл бы до строки БД и отказ пришёл бы
        # с 403 (`is_active=False`) — то есть «in (401, 403)» зеленело бы и с
        # неработающим денилистом, а вместе с ним и весь этот файл.
        for path in ("/api/v1/auth/me", "/api/v1/conversations"):
            denied = await client.get(path, headers=people.employee_auth)
            assert denied.status_code == 401, f"{path}: {denied.status_code} {denied.text}"
        assert await redis.get(f"refresh:{refresh}") is None
        assert await redis.exists(revoked_key(people.employee.id)) == 1

        rows = await audit_actions(pg_sessionmaker, "user.deactivated")
        assert [r.entity_id for r in rows] == [str(people.employee.id)]


async def test_the_denylist_alone_stops_a_live_token(app, client, redis, people, pg_sessionmaker):
    """Дверь закрывает именно денилист, а не флаг `is_active`.

    В основном сценарии отключённому сотруднику отказали бы и без денилиста —
    его срезал бы `is_active`, и тест зеленел бы, доказывая не то. Поэтому
    здесь взято действие, которое рвёт сессии, НЕ трогая активность: сброс
    пароля (01 §3.3). Сотрудник остаётся активным, а выданный ему раньше
    access-токен обязан перестать приниматься немедленно — и открытый сокет
    закрыться, потому что учётной записи, у которой больше нет пароля, нечего
    держать открытой сессию.
    """
    async with open_socket(app, client, people.employee_auth) as probe:
        alive = await client.get("/api/v1/auth/me", headers=people.employee_auth)
        assert alive.status_code == 200, alive.text

        reset = await client.post(
            f"/api/v1/users/{people.employee.id}/reset-password", headers=people.admin_auth
        )
        assert reset.status_code == 200, reset.text

        assert await probe.closed_with() == WS_CLOSE_LOGOUT
        denied = await client.get("/api/v1/auth/me", headers=people.employee_auth)
        assert denied.status_code == 401, denied.text

    async with pg_sessionmaker() as s:
        row = await s.get(User, people.employee.id)
        assert row is not None and row.is_active is True  # это не отключение


async def test_a_new_password_returns_the_employee_to_work_at_once(
    app, client, redis, people, pg_sessionmaker
):
    """Обратная сторона денилиста: он не должен запирать своего же хозяина.

    Пометка `revoked_users:{id}` живёт 15 минут и не различает старый токен и
    выданный секунду назад. Сотрудник, поставивший новый пароль по ссылке
    сброса, получает свежий токен — и обязан работать сразу, а не «через
    четверть часа». Снимает пометку `invite_accept` (см. `clear_revocation`).
    """
    reset = await client.post(
        f"/api/v1/users/{people.employee.id}/reset-password", headers=people.admin_auth
    )
    assert reset.status_code == 200, reset.text
    token = reset.json()["invite_url"].rsplit("/", 1)[-1]

    accepted = await client.post(
        "/api/v1/auth/invite/accept", json={"token": token, "password": "новый пароль 123"}
    )
    assert accepted.status_code == 200, accepted.text
    fresh = {"Authorization": f"Bearer {accepted.json()['access_token']}"}

    assert await redis.exists(revoked_key(people.employee.id)) == 0
    for path in ("/api/v1/auth/me", "/api/v1/conversations"):
        back = await client.get(path, headers=fresh)
        assert back.status_code == 200, f"{path}: {back.status_code} {back.text}"


async def test_revoked_socket_dies_in_another_uvicorn_process(
    app, client, redis, redis_url, people
):
    """Сигнал идёт через Pub/Sub именно ради этого (08 §5.1).

    При WEB_CONCURRENCY=2 сокет сотрудника живёт в одном процессе, а ручку
    «Отключить» обслуживает другой: прямой вызов локального хаба закрыл бы
    ровно ноль сокетов, и приёмка провалилась бы снова — только через раз.
    """
    other_redis = Redis.from_url(redis_url, decode_responses=True)
    other_hub = hub_mod.Hub(other_redis)
    listener = asyncio.create_task(other_hub.run())
    try:
        await _wait_for_subscribers(redis, 2)  # хаб приложения + «соседний процесс»
        socket = DeadDropSocket()
        other_hub.attach(socket, people.employee)

        await client.post(
            f"/api/v1/users/{people.employee.id}/deactivate", headers=people.admin_auth
        )

        deadline = asyncio.get_running_loop().time() + 5
        while socket.close_code is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.05)
        assert socket.close_code == WS_CLOSE_LOGOUT
        assert other_hub.sessions == {}  # хаб отцепил сессию, а не только закрыл сокет
    finally:
        listener.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await listener
        await other_redis.aclose()


async def test_role_change_asks_for_reconnect_without_locking_out(app, client, redis, people):
    """Понижение — не бан (01 §3.4): сокет закрывается 4401, доступ остаётся.

    4401 — «возьми новый тикет»: сессия хаба несёт снимок роли, сделанный на
    connect'е (08 §5.3), и после понижения показывала бы старые права. А вот
    REST продолжает работать сразу, уже с новой ролью: право читается из
    строки БД на каждом запросе, поэтому старый токен со стухшим `role`
    в теле не даёт ничего лишнего.
    """
    async with open_socket(app, client, people.employee_auth) as probe:
        patched = await client.patch(
            f"/api/v1/users/{people.employee.id}",
            headers=people.admin_auth,
            json={"role": "observer"},
        )
        assert patched.status_code == 200, patched.text
        assert await probe.closed_with() == WS_CLOSE_RELOGIN

        me = await client.get("/api/v1/auth/me", headers=people.employee_auth)
        assert me.status_code == 200, me.text
        assert me.json()["role"] == "observer"  # роль из БД, а не из тела токена
        assert "messages:send" not in me.json()["permissions"]
        assert await redis.exists(revoked_key(people.employee.id)) == 0


async def test_deactivated_employee_cannot_open_a_new_socket(app, client, redis, people):
    """Тикет, выписанный до отключения, не должен пускать обратно.

    Иначе обрыв сессии превращался бы в «закрыли окно — открой заново»:
    тикет живёт 60 секунд, этого хватает, чтобы переподключиться.
    """
    ticket = (await client.post("/api/v1/ws/ticket", headers=people.employee_auth)).json()["ticket"]
    await client.post(f"/api/v1/users/{people.employee.id}/deactivate", headers=people.admin_auth)

    probe = WsProbe(app, ticket)
    async with probe:
        assert await probe.closed_with() == WS_CLOSE_LOGOUT  # 4403 ещё до accept (01 §11.1)


async def test_a_ticket_taken_before_a_password_reset_cannot_reopen_the_feed(
    app, client, redis, people
):
    """Та же дверь, но там, где `is_active` не помогает (01 §3.3, §11.1).

    Отключение закрывает вход в сокет флагом `is_active=False`, и предыдущий
    тест это проверяет. Сброс пароля сотрудника НЕ трогает: учётная запись
    остаётся активной. Значит, единственное, что отличает её от здоровой, —
    пометка `revoked_users:{id}`, и проверять её обязан сам `ws_endpoint`.

    Без этой проверки отключение сессии обходится за один шаг: тикет живёт
    60 секунд и проверяется только по Redis, поэтому окно, у которого только
    что отобрали пароль, берёт заранее выписанный тикет, открывает **новый**
    сокет и продолжает читать ленту переписки. Это ровно тот провал приёмки
    («сотрудника отключили, лента шла дальше»), только через вторую дверь:
    старые сокеты закрыты, а новый принят.
    """
    ticket = (await client.post("/api/v1/ws/ticket", headers=people.employee_auth)).json()["ticket"]
    reset = await client.post(
        f"/api/v1/users/{people.employee.id}/reset-password", headers=people.admin_auth
    )
    assert reset.status_code == 200, reset.text

    probe = WsProbe(app, ticket)
    async with probe:
        assert await probe.closed_with() == WS_CLOSE_LOGOUT  # 4403 до accept, как при отключении


async def test_full_cycle_invite_work_deactivate_denied(
    app, client, redis, pg_sessionmaker, people
):
    """Полный цикл на настоящей базе: приглашение → пароль → работа → отказ."""
    invited = await client.post(
        "/api/v1/users",
        headers=people.admin_auth,
        json={"email": "Newbie@Leadchat.Test", "full_name": "Новичок", "role": "manager"},
    )
    assert invited.status_code == 201, invited.text
    user_id = invited.json()["user"]["id"]
    token = invited.json()["invite_url"].rsplit("/", 1)[-1]

    accepted = await client.post(
        "/api/v1/auth/invite/accept", json={"token": token, "password": "пароль подлиннее"}
    )
    assert accepted.status_code == 200, accepted.text
    employee_auth = {"Authorization": f"Bearer {accepted.json()['access_token']}"}

    async with open_socket(app, client, employee_auth) as probe:
        working = await client.get("/api/v1/conversations", headers=employee_auth)
        assert working.status_code == 200, working.text

        off = await client.post(f"/api/v1/users/{user_id}/deactivate", headers=people.admin_auth)
        assert off.status_code == 200, off.text
        assert await probe.closed_with() == WS_CLOSE_LOGOUT

    # Именно 401, а не «401 или 403»: отказ обязан прийти от денилиста
    # `revoked_users:{id}` в `get_current_user`, ДО загрузки строки из БД.
    # Мягкое «in (401, 403)» зеленело бы и с полностью выключённым денилистом —
    # отключённого срезал бы `is_active=False` с кодом 403, а живой access-токен
    # оставался бы годным 15 минут. Проверено мутацией: с удалённой проверкой в
    # `app/api/deps.py` этот тест при мягком сравнении не краснел.
    denied = await client.get("/api/v1/conversations", headers=employee_auth)
    assert denied.status_code == 401, f"{denied.status_code} {denied.text}"
    # citext (миграция 0001): адрес нормализован, повторное приглашение — 409
    again = await client.post(
        "/api/v1/users",
        headers=people.admin_auth,
        json={"email": "newbie@leadchat.test", "full_name": "Двойник", "role": "manager"},
    )
    assert again.status_code == 409, again.text

    async with pg_sessionmaker() as s:
        row = await s.get(User, uuid.UUID(user_id))
        assert row is not None and row.is_active is False
    assert {r.action for r in await audit_actions(pg_sessionmaker, "user.invited")} == {
        "user.invited"
    }
