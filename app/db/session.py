"""Async engine + session factory (08 §1.2).

Infrastructure is created once per process (app lifespan / CLI entry) and
lives as module attributes. One session per request via the ``get_db``
dependency in api/deps.py; transactions are explicit — no auto-commit
middleware.

Долг спринта 4 («idle in transaction» на проде). Соединение попадает в это
состояние, когда сессия открыла транзакцию (SQLAlchemy делает это неявно на
ПЕРВОМ же запросе — например, при чтении пользователя в auth-зависимости) и
дальше долго ничего не делает: пока сессия жива, соединение занято и в пул
не возвращается. Лечим в три эшелона:

1. ``expire_on_commit=False`` — после commit'а объекты не «протухают», и
   сериализация ответа не открывает новую транзакцию ради refresh'а.
2. :func:`db_session` / :func:`session_scope` — в ``finally`` всегда rollback
   незакрытой транзакции и close: соединение уходит в пул чистым, даже если
   обработчик упал между запросом и commit'ом. Где нужна транзакция — она
   открывается явно (``async with session.begin()``, см. :func:`transaction`).
3. ``idle_in_transaction_session_timeout`` на стороне PostgreSQL — последний
   рубеж: висящую транзакцию гасит сервер, пул не выедается насухо (05 §4).

Плюс гигиена пула: ``pool_pre_ping`` (мёртвое соединение переоткрывается, а не
роняет запрос), ``pool_recycle`` (потолок возраста соединения; от firewall'а
страхует пинг, а не срок — см. ``db_pool_recycle_seconds`` в config.py) и
ограниченный ``pool_timeout`` вместо бесконечного ожидания.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import settings

log = structlog.get_logger("app.db")

engine: AsyncEngine | None = None
session_factory: async_sessionmaker[AsyncSession] | None = None


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def _pool_kwargs(url: str) -> dict[str, Any]:
    """Аргументы QueuePool — только для настоящей БД.

    SQLite (юнит-тесты) обслуживается Static/NullPool: ``pool_size`` и соседи
    для них недопустимы и падают TypeError'ом ещё на create_async_engine.
    """
    if _is_sqlite(url):
        return {}
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout_seconds,
        # Два часа (06.09): холодное соединение платит 40–62 мс планирования
        # на первом же партиционированном запросе. Долгий срок держится на
        # `pool_pre_ping` ниже — без него он был бы опасен.
        "pool_recycle": settings.db_pool_recycle_seconds,
        "pool_pre_ping": True,
    }


def _connect_args(url: str, application_name: str, component: str = "unknown") -> dict[str, Any]:
    """asyncpg server_settings: видимость в pg_stat_activity + таймауты."""
    if not url.startswith("postgresql"):
        return {}
    server_settings: dict[str, str] = {"application_name": application_name}
    if settings.db_idle_in_transaction_timeout_ms > 0:
        server_settings["idle_in_transaction_session_timeout"] = str(
            settings.db_idle_in_transaction_timeout_ms
        )
    потолок = settings.db_statement_timeout_ms
    if component == "api" and settings.db_api_statement_timeout_ms > 0:
        # ⚠ ПОТОЛОК ЗАПРОСА — ТОЛЬКО У API, И ЭТО РАЗНИЦА ПО СУЩЕСТВУ.
        #
        # Общий потолок выключен намеренно: миграции и обратное заполнение
        # бывают долгими по делу. Но у веб-запроса долгого дела нет — за ним
        # сидит человек, и после нескольких секунд он всё равно решит, что
        # приложение зависло.
        #
        # ⚠ ЗАЧЕМ ОН НУЖЕН БЫЛ ВЧЕРА. Пул к базе — десять соединений на рабочий
        # процесс. Один запрос, идущий полторы минуты, занимает одно из десяти
        # и не отдаёт; при двух-трёх таких весь API встаёт целиком, и ждут все,
        # включая тех, кто просто открывает диалог. Замер боя 02.09 показал
        # ровно эту картину: поиск по телефону 27-93 с — и p95 у соседних, самих
        # по себе быстрых ручек: 17,7 с у карточки диалога и у ленты сообщений.
        #
        # Потолок не ускоряет ни одного запроса. Он делает другое: превращает
        # «встало у всех» в «не получилось у одного». Это и есть надёжность.
        потолок = settings.db_api_statement_timeout_ms
    if потолок > 0:
        server_settings["statement_timeout"] = str(потолок)
    return {"server_settings": server_settings}


def init_engine(url: str | None = None, *, component: str = "unknown") -> AsyncEngine:
    """Создать engine процесса (идемпотентно).

    ``component`` попадает в ``application_name`` — в ``pg_stat_activity`` сразу
    видно, чьи соединения висят (05 §8 runbook «кто держит пул»).

    ⚠ УМОЛЧАНИЕ — «unknown», А НЕ «api», И ЭТО ВЫБОР В ПОЛЬЗУ БЕЗОПАСНОГО
    ПРОМАХА. Раньше умолчанием было «api», и забытый аргумент выдавал процессу
    ПОТОЛОК ЗАПРОСА В 15 СЕКУНД, задуманный только для веб-процесса. Именно это
    и случилось: воркер поднимал движок без аргумента и в бою ходил с чужим
    потолком под чужим именем (разбор 03.09; в pg_stat_activity сорок строк
    `leadchat-api` и ни одной воркерской).

    Опаснее всего это было у планировщика: `refresh_stats_mv` зовёт движок без
    аргумента, а сам REFRESH идёт 11-13 секунд — впритык к пятнадцати. Спасала
    только идемпотентность (движок к тому времени уже поднят из `main`), то
    есть случайность порядка вызовов.

    Теперь забытый аргумент не режет ничего и хорошо виден: `leadchat-unknown`
    в `pg_stat_activity` — это само по себе находка.
    """
    global engine, session_factory
    if engine is None:
        dsn = url or settings.database_url
        engine = create_async_engine(
            dsn,
            **_pool_kwargs(dsn),
            connect_args=_connect_args(dsn, f"leadchat-{component}", component),
        )
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
    return engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if session_factory is None:
        init_engine()
    assert session_factory is not None
    return session_factory


async def _release(session: AsyncSession) -> None:
    """Вернуть соединение в пул без открытой транзакции — единственная точка.

    ``close()`` и сам откатывает незавершённую транзакцию, но делает это молча:
    явный rollback + лог показывает в проде, какие ручки забывают commit и
    держат `idle in transaction`.
    """
    try:
        if session.in_transaction():
            log.debug("db.session_rollback_on_release")
            await session.rollback()
    except Exception as exc:  # соединение уже мертво — не мешаем закрытию
        log.warning("db.session_rollback_failed", error=type(exc).__name__)
    finally:
        await session.close()


async def db_session() -> AsyncIterator[AsyncSession]:
    """Тело FastAPI-зависимости ``get_db``: сессия на запрос.

    Транзакцию открывает вызывающий код (08 §8.1); мы гарантируем только одно —
    после ответа соединение вернулось в пул и транзакции на нём нет.
    """
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        await _release(session)


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Сессия для CLI/воркеров/планировщика с тем же контрактом освобождения."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
    finally:
        await _release(session)


@asynccontextmanager
async def transaction() -> AsyncIterator[AsyncSession]:
    """Сессия + ЯВНАЯ транзакция: commit на выходе, rollback на исключении."""
    async with session_scope() as session:
        async with session.begin():
            yield session


def pool_status() -> dict[str, int]:
    """Срез пула для /api/health/deep (05 §7.2) — только числа, без DSN.

    Разные пулы (QueuePool в проде, StaticPool в тестах) отвечают на разный
    набор методов — берём то, что есть.
    """
    if engine is None:
        return {}
    pool = engine.pool
    out: dict[str, int] = {}
    for name in ("size", "checkedin", "checkedout", "overflow"):
        probe = getattr(pool, name, None)
        if probe is None:
            continue
        try:
            out[name] = int(probe())
        except Exception:  # pragma: no cover — пул без этого счётчика
            continue
    return out


async def dispose_engine() -> None:
    global engine, session_factory
    if engine is not None:
        await engine.dispose()
        engine = None
        session_factory = None
