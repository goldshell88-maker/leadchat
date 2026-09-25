"""Integration base (07 §1.2): a real Postgres via testcontainers, real
``alembic upgrade head`` from scratch.

БЕЗ DOCKER НАБОР ПРОПУСКАЕТСЯ — НО НЕ НА СБОРКЕ. У разработчика Docker может
быть не запущен, и останавливать его работу из-за этого незачем. На сборке
всё наоборот: pytest на полностью пропущенном наборе возвращает 0, и CI
зеленеет, не выполнив НИ ОДНОЙ проверки — ни гонок в очереди, ни доставки, ни
партиций, ни отзыва токена. Тишина выглядит ровно как успех, и именно так
пропускают поломку.

Поэтому пропуск разрешён только вне CI. Переменную ``CI`` выставляют все
известные сборочные системы (GitHub Actions — всегда).
"""

import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False)
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


DOCKER_AVAILABLE = docker_available()

# Ровно то же значение, что читает pytest-`skipif` ниже, — чтобы «пропустить»
# и «упасть» не могли разойтись в трактовке.
IN_CI = os.environ.get("CI", "").lower() in {"1", "true", "yes"}

if IN_CI and not DOCKER_AVAILABLE:
    raise RuntimeError(
        "Интеграционные тесты требуют Docker, а он недоступен на сборке. "
        "Пропустить их здесь нельзя: зелёный прогон без единой выполненной "
        "проверки неотличим от настоящего."
    )

requires_docker = pytest.mark.skipif(
    not DOCKER_AVAILABLE, reason="Docker недоступен — integration-тесты пропущены"
)


def to_async_url(url: str) -> str:
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


def run_migrations(async_url: str) -> None:
    """`alembic upgrade head` in-process against the container DB."""
    from alembic import command
    from alembic.config import Config

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "app" / "db" / "migrations"))
    cfg.set_main_option("sqlalchemy.url", async_url)

    last_error: Exception | None = None
    for _ in range(5):  # the container may need a moment after the log-based wait
        try:
            command.upgrade(cfg, "head")
            return
        except Exception as exc:  # noqa: BLE001 — driver-specific connect errors
            last_error = exc
            time.sleep(2)
    raise AssertionError(f"alembic upgrade head не прошёл: {last_error}")


@pytest.fixture(scope="session")
def pg_async_url() -> Iterator[str]:
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker недоступен")
    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        async_url = to_async_url(pg.get_connection_url())
        run_migrations(async_url)
        yield async_url


@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    """Настоящий Redis (07 §1.2): Streams + consumer groups + Pub/Sub."""
    if not DOCKER_AVAILABLE:
        pytest.skip("Docker недоступен")
    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as rc:
        host = rc.get_container_host_ip()
        port = rc.get_exposed_port(6379)
        yield f"redis://{host}:{port}/0"
