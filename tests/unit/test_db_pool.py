"""Пул соединений (08 §1.2) — регрессия на «idle in transaction» с прода.

Инвариант, который здесь закреплён: после запроса соединение вернулось в пул,
и открытой транзакции на нём нет. Всё остальное (pre_ping, recycle, серверный
idle_in_transaction_session_timeout) — конфигурация, проверяем, что она
доезжает до engine, а не остаётся комментарием в документации.
"""

from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.config import settings
from app.db import session as db_mod
from app.models import Base

SQLITE_URL = "sqlite+aiosqlite://"


@pytest.fixture
async def process_engine() -> AsyncIterator[None]:
    """Глобальный engine процесса на SQLite — как его создаёт lifespan."""
    await db_mod.dispose_engine()
    engine = db_mod.init_engine(SQLITE_URL, component="test")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    await db_mod.dispose_engine()


async def assert_pool_clean() -> None:
    """Соединение в пуле и на нём НЕТ открытой транзакции.

    На SQLite (StaticPool) соединение одно на весь engine — именно поэтому
    проверка честная: незакрытая транзакция предыдущей сессии была бы видна
    прямо здесь. В проде тот же инвариант держит ``_release`` + серверный
    ``idle_in_transaction_session_timeout``.
    """
    engine = db_mod.init_engine(SQLITE_URL)
    async with engine.connect() as conn:
        raw = await conn.get_raw_connection()
        assert raw.driver_connection.in_transaction is False
    assert db_mod.pool_status().get("checkedout", 0) == 0


# ------------------------------------------------------------- конфигурация


def test_session_factory_does_not_expire_on_commit(process_engine):
    """expire_on_commit=False: после commit'а сериализация ответа не лезет в БД
    за refresh'ем — а значит, не открывает новую транзакцию (08 §8.1)."""
    factory = db_mod.get_session_factory()
    assert factory.kw["expire_on_commit"] is False


def test_pool_kwargs_are_applied_for_postgres_only():
    pg = db_mod._pool_kwargs("postgresql+asyncpg://u:p@h/db")
    assert pg["pool_size"] == settings.db_pool_size
    assert pg["max_overflow"] == settings.db_max_overflow
    assert pg["pool_timeout"] == settings.db_pool_timeout_seconds
    assert pg["pool_recycle"] == settings.db_pool_recycle_seconds
    assert pg["pool_pre_ping"] is True
    # SQLite обслуживается Static/NullPool: аргументы QueuePool ему нельзя
    assert db_mod._pool_kwargs(SQLITE_URL) == {}


def test_connect_args_guard_idle_transactions():
    # ⚠ ПРОЦЕСС НАЗЫВАЕТСЯ ЯВНО, И ЭТО ПРАВКА 03.09.
    #
    # Здесь стоял вызов без третьего аргумента, и проверка держалась на том,
    # что умолчанием было «api». Ровно это умолчание и оказалось дефектом: в
    # бою воркер поднимал движок без аргумента и получал чужой потолок в 15
    # секунд под чужим именем `leadchat-api`. Умолчание сменено на «unknown»,
    # то есть забытый аргумент теперь ничего не режет и хорошо виден.
    # Проверка самого умолчания — в `test_engine_component_0309.py`.
    args = db_mod._connect_args("postgresql+asyncpg://u:p@h/db", "leadchat-api", "api")
    server = args["server_settings"]
    assert server["application_name"] == "leadchat-api"  # видно в pg_stat_activity
    assert server["idle_in_transaction_session_timeout"] == str(
        settings.db_idle_in_transaction_timeout_ms
    )
    # ⚠ У ВЕБ-ПРОЦЕССА ПОТОЛОК ЕСТЬ, И ЭТО ПРАВКА 02.09, А НЕ ОСЛАБЛЕНИЕ.
    # Прежде здесь стояло «statement_timeout не задан»: общий потолок выключен,
    # потому что миграции и обратное заполнение бывают долгими по делу. Довод
    # верен и сохранён — но только для фоновых процессов, что и проверяется
    # строкой ниже. У веб-запроса долгого дела нет: за ним сидит человек, а один
    # застрявший запрос занимает соединение из пула в десять и морит голодом
    # всех остальных. Замер боя 02.09: поиск по телефону 27-93 с, пул исчерпан
    # (166 ошибок QueuePool), приём сообщений от Авито потерял 105 вебхуков из
    # 566. Разбор — в `tests/unit/test_api_statement_timeout_0209.py`.
    assert server["statement_timeout"] == str(settings.db_api_statement_timeout_ms)
    фоновый = db_mod._connect_args("postgresql+asyncpg://u:p@h/db", "leadchat-worker", "worker")
    assert "statement_timeout" not in фоновый["server_settings"]
    assert db_mod._connect_args(SQLITE_URL, "leadchat-api", "api") == {}


def test_connect_args_can_be_disabled(monkeypatch):
    monkeypatch.setattr(settings, "db_idle_in_transaction_timeout_ms", 0)
    monkeypatch.setattr(settings, "db_statement_timeout_ms", 15_000)
    server = db_mod._connect_args("postgresql+asyncpg://u:p@h/db", "x")["server_settings"]
    assert "idle_in_transaction_session_timeout" not in server
    assert server["statement_timeout"] == "15000"


# ------------------------------------------------------------- освобождение


async def test_dependency_releases_connection_without_transaction(process_engine):
    """Тело зависимости get_db: транзакция открыта на запросе — закрыта после."""
    gen = db_mod.db_session()
    session = await anext(gen)
    await session.execute(text("SELECT 1"))
    assert session.in_transaction() is True  # SQLAlchemy начал транзакцию неявно

    await gen.aclose()  # FastAPI закрывает генератор после ответа
    assert session.in_transaction() is False
    await assert_pool_clean()


async def test_dependency_releases_connection_after_exception(process_engine):
    """Обработчик упал между запросом и commit'ом — соединение всё равно наше."""
    gen = db_mod.db_session()
    session = await anext(gen)
    await session.execute(text("SELECT 1"))

    with pytest.raises(RuntimeError):
        await gen.athrow(RuntimeError("boom"))
    assert session.in_transaction() is False
    await assert_pool_clean()


async def test_session_scope_releases_connection(process_engine):
    async with db_mod.session_scope() as session:
        await session.execute(text("SELECT 1"))
        assert session.in_transaction() is True
    assert session.in_transaction() is False
    await assert_pool_clean()


async def test_transaction_helper_commits_and_releases(process_engine):
    """Явная транзакция (08 §8.1): commit на выходе, соединение свободно."""
    async with db_mod.transaction() as session:
        assert session.in_transaction() is True
        await session.execute(text("SELECT 1"))
    assert session.in_transaction() is False
    await assert_pool_clean()


async def test_transaction_helper_rolls_back_on_error(process_engine):
    with pytest.raises(RuntimeError):
        async with db_mod.transaction() as session:
            await session.execute(text("SELECT 1"))
            raise RuntimeError("boom")
    assert session.in_transaction() is False
    await assert_pool_clean()


# ------------------------------------------------------- сквозной HTTP-путь


async def test_http_request_returns_connection_to_pool(process_engine):
    """Настоящий запрос через настоящую зависимость: после ответа пул пуст.

    Именно этот сценарий на проде оставлял соединения в 'idle in transaction':
    auth-зависимость читала пользователя (открывалась транзакция), ручка ничего
    не коммитила, и сессия жила дольше запроса.
    """
    seen: dict[str, AsyncSession] = {}
    app = FastAPI()

    @app.get("/probe")
    async def probe(db: AsyncSession = Depends(deps.get_db)) -> dict:
        await db.execute(text("SELECT 1"))  # неявная транзакция, как в auth-зависимости
        seen["session"] = db
        assert db.in_transaction() is True
        return {"ok": True}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        assert (await client.get("/probe")).status_code == 200
        assert (await client.get("/probe")).status_code == 200

    assert seen["session"].in_transaction() is False
    await assert_pool_clean()


# ------------------------------------------------------------- WebSocket


async def test_ws_endpoint_does_not_hold_a_pooled_connection(process_engine, monkeypatch):
    """Сокет живёт часами — слот пула он держать не должен.

    Это и был настоящий источник 'idle in transaction' на проде: зависимость
    ``get_db`` в сигнатуре ``ws_endpoint`` закрывалась только после выхода из
    обработчика, а ``db.get(User, ...)`` неявно открывал транзакцию — каждая
    открытая вкладка оператора выедала одно соединение пула насмерть.
    """
    import inspect

    from app.api.routes import ws as ws_routes

    params = inspect.signature(ws_routes.ws_endpoint).parameters
    assert "db" not in params, "сессия БД в сигнатуре WS-обработчика — это утечка пула"

    # и сквозная проверка: после lookup'а пользователя пул чист
    from app.core.security import hash_password
    from app.models import User

    async with db_mod.session_scope() as session:
        user = User(
            email="ws-pool@leadchat.test",
            password_hash=hash_password("correct horse battery staple"),
            full_name="WS Pool",
            role="manager",
            is_active=True,
        )
        session.add(user)
        await session.commit()
        user_id = user.id

    class _Socket:
        def __init__(self) -> None:
            self.accepted = False
            self.closed: int | None = None
            # У настоящего WebSocket заголовки есть ВСЕГДА; пустые здесь значат
            # «не браузер», и проверка Origin (01.09) такое соединение пропускает.
            self.headers: dict[str, str] = {}

        async def accept(self) -> None:
            self.accepted = True
            # ровно та точка, где раньше уже висела открытая транзакция
            await assert_pool_clean()

        async def close(self, code: int = 1000) -> None:
            self.closed = code

        async def receive_text(self) -> str:
            raise AssertionError("тест не доходит до цикла кадров")

    class _Redis:
        async def getdel(self, key: str) -> str:
            return str(user_id)

        async def get(self, key: str) -> None:
            return None  # сессия не отзывалась — отметки отзыва нет (01 §1.2)

    class _StopHere(Exception):
        """Обрываем обработчик сразу после accept() — цикл кадров не наш предмет."""

    def _no_hub():
        raise _StopHere

    socket = _Socket()
    monkeypatch.setattr(ws_routes, "get_hub", _no_hub)
    with pytest.raises(_StopHere):
        await ws_routes.ws_endpoint(socket, ticket="wst_x", redis=_Redis())  # type: ignore[arg-type]
    assert socket.accepted is True  # accept() дошёл, и пул на нём был чист
    await assert_pool_clean()


async def test_ws_endpoint_rejects_inactive_user_without_leaking(process_engine):
    """Ветка 4403 тоже обязана вернуть соединение в пул."""
    from app.api.routes import ws as ws_routes
    from app.core.security import hash_password
    from app.models import User

    async with db_mod.session_scope() as session:
        user = User(
            email="ws-inactive@leadchat.test",
            password_hash=hash_password("correct horse battery staple"),
            full_name="WS Inactive",
            role="manager",
            is_active=False,
        )
        session.add(user)
        await session.commit()
        user_id = user.id

    class _Socket:
        def __init__(self) -> None:
            self.closed: int | None = None
            # У настоящего WebSocket заголовки есть ВСЕГДА; пустые здесь значат
            # «не браузер», и проверка Origin (01.09) такое соединение пропускает.
            self.headers: dict[str, str] = {}

        async def close(self, code: int = 1000) -> None:
            self.closed = code

    class _Redis:
        async def getdel(self, key: str) -> str:
            return str(user_id)

        async def get(self, key: str) -> None:
            raise AssertionError("до отметки отзыва не доходим: отключённого срезает is_active")

    socket = _Socket()
    await ws_routes.ws_endpoint(socket, ticket="wst_x", redis=_Redis())  # type: ignore[arg-type]
    assert socket.closed == 4403  # чтение is_active у detached-объекта не упало
    await assert_pool_clean()


def test_no_module_bypasses_the_single_release_path():
    """Страж: `factory()` в обход db_session/session_scope = второй путь
    освобождения соединения, а значит — шанс разъехаться на следующей правке.

    Легальные владельцы фабрики: сам модуль сессий и точка сборки ctx воркера
    (ARQ отдаёт фабрику задачам, каждая открывает свой скоуп сама).
    """
    import pathlib

    root = pathlib.Path(db_mod.__file__).resolve().parents[2]
    allowed = {"app/db/session.py", "app/workers/main.py"}
    offenders = []
    for path in sorted((root / "app").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in allowed:
            continue
        if "get_session_factory()()" in path.read_text(encoding="utf-8"):
            offenders.append(rel)
    assert offenders == [], f"сессия создаётся в обход session_scope: {offenders}"
