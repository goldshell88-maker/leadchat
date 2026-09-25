"""Регрессионный smoke после деплоя (07 §6).

Набор ходит по СЕТИ в уже задеплоенный стенд и ничего не импортирует из
``app``: он проверяет то, что реально отвечает снаружи, а не то, что собралось
бы локально. Поэтому здесь свои фикстуры и свой конфиг из переменных
окружения — без ``SMOKE_BASE_URL`` весь набор пропускается, чтобы обычный
``pytest tests/unit`` в CI не начал стучаться в прод.

Переменные (все, кроме BASE_URL, необязательные — соответствующая проверка
деградирует до skip, а с ``--strict-smoke`` до провала):

  SMOKE_BASE_URL           https://chat.example.ru
  SMOKE_EMAIL/PASSWORD     учётка smoke-пользователя (07 §6, seed-smoke)
  SMOKE_ADMIN_EMAIL/…      учётка с `accounts:read` для SM-10 (роль head, seed-smoke)
  EXPECTED_VERSION         тег, который обязан отдавать /api/health (SM-1)
  SMOKE_CONVERSATION_ID    служебный диалог SMOKE-CONV для SM-5
  SMOKE_ACCOUNT_ID +
  SMOKE_WEBHOOK_SECRET     аккаунт-заглушка для SM-8 (положительная половина)
  SMOKE_REDIS_URL          прямой доступ к Redis для SM-6/SM-7 (запуск на хосте)
"""

import asyncio
import os
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


BASE_URL = (_env("SMOKE_BASE_URL") or "").rstrip("/")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--strict-smoke",
        action="store_true",
        default=False,
        help="SKIP из-за незаданных переменных считать провалом (гейт релиза, 07 §7)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if BASE_URL:
        return
    skip = pytest.mark.skip(reason="SMOKE_BASE_URL не задан — smoke не запускается")
    for item in items:
        if "smoke" in item.keywords:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def need(request: pytest.FixtureRequest):
    """`need("SMOKE_EMAIL")` — значение или skip/fail по --strict-smoke."""
    strict = request.config.getoption("--strict-smoke")

    def _need(*names: str) -> tuple[str, ...]:
        missing = [n for n in names if not _env(n)]
        if missing:
            message = f"не заданы {', '.join(missing)}"
            if strict:
                pytest.fail(f"--strict-smoke: {message}")
            pytest.skip(message)
        return tuple(_env(n) or "" for n in names)

    return _need


@pytest.fixture(scope="session")
def verify_tls() -> bool:
    # sslip.io-стенд ходит по самоподписанному сертификату; в CI против боевого
    # домена проверка включена (SMOKE_INSECURE не выставляется).
    return not bool(_env("SMOKE_INSECURE"))


@pytest.fixture
async def http(base_url: str, verify_tls: bool) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=base_url, verify=verify_tls, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="session")
def smoke_creds(need) -> tuple[str, str]:
    return need("SMOKE_EMAIL", "SMOKE_PASSWORD")


@pytest.fixture(scope="session")
def token_cache() -> dict[str, str]:
    """Логинимся ОДИН раз на весь прогон.

    `/api/v1/auth/login` под rate-limit'ом nginx (10 r/min на IP, 05 §3.1):
    логин в каждом тесте упирался бы в 429 и красил бы smoke без единой
    настоящей поломки. Access-токен живёт 15 минут — набору хватает с запасом.
    """
    return {}


LOGIN_RETRY_DELAYS = (7, 20, 40)  # суммарно < 70 с, укладываемся в бюджет 07 §6


async def login(http: httpx.AsyncClient, email: str, password: str) -> str:
    """Логин с коротким отступом на 429.

    Лимитер `/auth/login` — 10 r/min на IP (05 §3.1) и общий для всего, что
    стучится с этого адреса: параллельный прогон, чей-то ретрай или подряд
    идущий деплой могут выесть бюджет. 429 от лимитера — не поломка деплоя, а
    smoke обязан краснеть только на настоящих поломках, поэтому пробуем
    несколько раз и лишь потом сдаёмся.
    """
    response = await login_response(http, email, password)
    assert response.status_code == 200, (
        f"логин {email}: {response.status_code} {response.text[:200]}"
    )
    return str(response.json()["access_token"])


async def login_response(http: httpx.AsyncClient, email: str, password: str) -> httpx.Response:
    """Тот же логин, но отдаёт ответ целиком — SM-2 смотрит заголовки."""
    last: httpx.Response | None = None
    for delay in (0, *LOGIN_RETRY_DELAYS):
        if delay:
            await asyncio.sleep(delay)
        last = await http.post("/api/v1/auth/login", json={"email": email, "password": password})
        if last.status_code != 429:
            break
    assert last is not None
    return last


@pytest.fixture
async def token(
    http: httpx.AsyncClient, smoke_creds: tuple[str, str], token_cache: dict[str, str]
) -> str:
    email, password = smoke_creds
    if "smoke" not in token_cache:
        token_cache["smoke"] = await login(http, email, password)
    return token_cache["smoke"]


@pytest.fixture
def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="session")
def env() -> Iterator[dict[str, str | None]]:
    yield {name: _env(name) for name in os.environ}
