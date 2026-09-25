"""Временная недоступность Авито не должна выключать канал.

ЧТО ЗДЕСЬ ОХРАНЯЕТСЯ. `client_id` и `client_secret` Авито выдаёт один раз и не
меняет (подтверждено владельцем 11 августа). Значит «ключи отозвали» — не
сценарий: отозвать их можно, только удалив или отключив приложение в кабинете.
До 11 августа обновление токена ловило `except (AvitoApiError, OSError)` целиком
и на ЛЮБУЮ ошибку переводило канал в `needs_reauth`. В эту ветку попадала почти
всегда обычная недоступность Авито — и канал выключался насовсем:

* обновление токенов перебирало только активные;
* оба соседних сторожа — тоже только активные;
* кнопка «Обновить токен» для такого канала отказывала.

Снаружи это выглядело так: обращения от клиентов приходят (вебхукам наш токен не
нужен), а любой ответ падает. Узнавал об этом только тот, кто пробовал ответить.

Проверка ломанием: замените `_keys_rejected(exc)` на `True` — падают первые три
теста; верните выборку планировщика к `status == "active"` — падает
``test_scheduler_retries_stuck_keys_account``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.integrations.avito.errors import AvitoApiError, AvitoAuthError, AvitoUnavailable
from app.models import AvitoAccount
from app.services import avito_accounts as svc


class _FakeRedis:
    """Ровно то, что нужно refresh_tokens: лок и публикация события."""

    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def set(self, *_a: Any, **_kw: Any) -> bool:
        return True

    async def delete(self, *_a: Any) -> int:
        return 1


@pytest.fixture
def keys_account_factory(db_sessionmaker: Any):
    """Канал, подключённый СВОИМИ КЛЮЧАМИ, — как оба боевых аккаунта."""

    async def _make(status: str = "active", expired: bool = False) -> AvitoAccount:
        from app.services import crypto

        async with db_sessionmaker() as session:
            account = AvitoAccount(
                title="LP-Ключи",
                avito_user_id=100000001,
                access_token_enc=crypto.encrypt_token("access"),
                refresh_token_enc=crypto.encrypt_token("refresh"),
                token_expires_at=(
                    datetime.now(UTC) - timedelta(hours=3)
                    if expired
                    else datetime.now(UTC) + timedelta(hours=20)
                ),
                status=status,
                webhook_secret="whsec-test",
                client_id="cid-permanent",
                client_secret_enc=crypto.encrypt_token("csecret-permanent"),
            )
            session.add(account)
            await session.commit()
            await session.refresh(account)
            return account

    return _make


def _client_raising(exc: BaseException) -> Any:
    class _Client:
        async def client_credentials_token(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            raise exc

    return _Client()


async def _run_refresh(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: Any,
    account: AvitoAccount,
    exc: BaseException,
) -> tuple[bool, str]:
    """Один прогон обновления токена с заданной ошибкой Авито."""
    monkeypatch.setattr(
        svc.AvitoClient, "fresh", staticmethod(lambda _db: _ready(_client_raising(exc)))
    )
    announced: list[str] = []

    async def _fake_announce(_db: Any, _redis: Any, acc: AvitoAccount, *, reason: str) -> None:
        announced.append(reason)

    monkeypatch.setattr(svc, "_announce_needs_reauth", _fake_announce)
    async with db_sessionmaker() as session:
        fresh = await session.get(AvitoAccount, account.id)
        assert fresh is not None
        ok = await svc.refresh_tokens(fresh, session, _FakeRedis())
        await session.refresh(fresh)
        return ok, fresh.status


async def _ready(value: Any) -> Any:
    return value


# --- временные беды: статус не трогаем ---------------------------------------


@pytest.mark.parametrize(
    ("exc", "why"),
    [
        (AvitoUnavailable(), "сеть, DNS, таймаут — до Авито не достучались"),
        (AvitoApiError("Авито прилёг", status=503), "5xx — беда на стороне Авито"),
        (AvitoApiError("много запросов", status=429), "лимит запросов, канал ни при чём"),
        (OSError("connection reset"), "сетевая ошибка уровня ОС"),
    ],
)
async def test_transient_failure_keeps_channel_alive(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: Any,
    keys_account_factory: Any,
    exc: BaseException,
    why: str,
) -> None:
    account = await keys_account_factory()
    ok, status = await _run_refresh(monkeypatch, db_sessionmaker, account, exc)
    assert ok is False, "токен не обновился — это правда"
    assert status == "active", f"канал выключен из-за временной беды ({why})"


# --- настоящий отказ: приложение удалили или отключили в кабинете -------------


@pytest.mark.parametrize(
    "exc",
    [
        AvitoAuthError(status=401),
        AvitoAuthError(status=403),
        AvitoApiError("invalid_client", status=400),
    ],
)
async def test_rejected_keys_disable_channel(
    monkeypatch: pytest.MonkeyPatch,
    db_sessionmaker: Any,
    keys_account_factory: Any,
    exc: BaseException,
) -> None:
    account = await keys_account_factory()
    ok, status = await _run_refresh(monkeypatch, db_sessionmaker, account, exc)
    assert ok is False
    assert status == "needs_reauth", "явный отказ Авито обязан выключить канал"


# --- канал не остаётся в ловушке ---------------------------------------------


async def test_scheduler_retries_stuck_keys_account(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, keys_account_factory: Any
) -> None:
    """Канал на ключах, застрявший в needs_reauth, планировщик берёт снова.

    Это и есть выход из ловушки: до правки обновлялись только активные, и канал
    оставался немым навсегда.
    """
    account = await keys_account_factory(status="needs_reauth", expired=True)

    class _Client:
        async def client_credentials_token(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            return {"access_token": "fresh-access", "expires_in": 86400}

    monkeypatch.setattr(svc.AvitoClient, "fresh", staticmethod(lambda _db: _ready(_Client())))
    async with db_sessionmaker() as session:
        refreshed = await svc.refresh_due_accounts(session, _FakeRedis())
        assert refreshed == 1, "застрявший канал на ключах не попал в обход планировщика"
        fresh = await session.get(AvitoAccount, account.id)
        assert fresh is not None
        await session.refresh(fresh)
        assert fresh.status == "active", "успешный запрос токена обязан вернуть канал в строй"


async def test_consent_account_stays_out_of_retry(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, make_avito_account: Any
) -> None:
    """Канал через согласие в needs_reauth планировщик не трогает.

    Там refresh-токен одноразовый: отозванный не оживёт, и лишний поход в Авито
    ничего не чинит — нужен человек с доступом к аккаунту.
    """
    await make_avito_account(status="needs_reauth")

    class _Client:
        async def client_credentials_token(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            raise AssertionError("канал через согласие не должен обновляться ключами")

        async def refresh_token(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            raise AssertionError("канал в needs_reauth не должен обновляться вовсе")

    monkeypatch.setattr(svc.AvitoClient, "fresh", staticmethod(lambda _db: _ready(_Client())))
    async with db_sessionmaker() as session:
        assert await svc.refresh_due_accounts(session, _FakeRedis()) == 0


async def test_service_stub_never_goes_to_avito_for_a_token(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any
) -> None:
    """Служебная заглушка в обход обновления токенов не попадает (аудит 19.08, L-003).

    ПОЧЕМУ ЭТО ВООБЩЕ МОЖЕТ СЛУЧИТЬСЯ. Заглушку заводит `seed-smoke` со статусом
    `disabled`, но 11 августа её включили кнопкой на боевой системе — и она стала
    для выборки обычным активным каналом. Токена Авито у неё нет вовсе (в поле
    строка-заглушка), поэтому каждый заход в обновление — это поход в Авито за
    несуществующим ключом: лишний расход лимита и ошибка в журнале, которую
    однажды прочтут как настоящую беду.

    Все сторожа отсекают её признаком `is_service`; обновление токенов было
    единственным боевым обходом, который этого не делал.

    Проверка ломанием: уберите `AvitoAccount.is_service.is_(False)` из
    `refresh_due_accounts` — тест краснеет на походе в Авито.
    """
    from app import cli

    async with db_sessionmaker() as session:
        stub, _ = await cli._seed_smoke_account(session, None)
        # Ровно состояние 11 августа: заглушку включили, токен давно истёк.
        stub.status = "active"
        stub.token_expires_at = datetime.now(UTC) - timedelta(hours=3)
        await session.commit()

    походы: list[str] = []

    class _Client:
        """Считает походы в Авито. Исключение здесь не годится: обход ловит
        ошибку каждого аккаунта отдельно («ошибка одного не прерывает
        остальные»), и тест на возвращённом числе прошёл бы при сломанной
        выборке. Проверяем сам факт обращения."""

        async def client_credentials_token(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            походы.append("client_credentials")
            return {"access_token": "x", "expires_in": 86400}

        async def refresh_token(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            походы.append("refresh")
            return {"access_token": "x", "refresh_token": "y", "expires_in": 86400}

    monkeypatch.setattr(svc.AvitoClient, "fresh", staticmethod(lambda _db: _ready(_Client())))
    async with db_sessionmaker() as session:
        обновлено = await svc.refresh_due_accounts(session, _FakeRedis())
    assert походы == [], f"в Авито сходили за токеном служебной заглушки: {походы}"
    assert обновлено == 0


# --- классификатор отдельно ---------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AvitoUnavailable(), False),
        (OSError("boom"), False),
        (AvitoApiError("500", status=500), False),
        (AvitoApiError("429", status=429), False),
        (AvitoApiError("нет статуса"), False),
        (AvitoAuthError(status=401), True),
        (AvitoAuthError(status=403), True),
        (AvitoApiError("400", status=400), True),
    ],
)
def test_keys_rejected_classifier(exc: BaseException, expected: bool) -> None:
    assert svc._keys_rejected(exc) is expected


# --- кнопка в уведомлении ------------------------------------------------------


async def test_notification_button_repairs_keys_channel_without_avito(
    monkeypatch: pytest.MonkeyPatch, db_sessionmaker: Any, keys_account_factory: Any
) -> None:
    """Кнопка под уведомлением чинит канал на ключах на месте.

    Уводить человека на страницу согласия Авито тут не за чем: доступ не
    терялся, ключи те же самые. Прежнее поведение просило подтвердить то, что
    и так подтверждено, а канал оставался немым.
    """
    from app.models import User

    account = await keys_account_factory(status="needs_reauth", expired=True)

    class _Client:
        async def client_credentials_token(self, *_a: Any, **_kw: Any) -> dict[str, Any]:
            return {"access_token": "fresh", "expires_in": 86400}

    monkeypatch.setattr(svc.AvitoClient, "fresh", staticmethod(lambda _db: _ready(_Client())))

    async with db_sessionmaker() as session:
        actor = User(
            email="admin@example.com",
            full_name="Админ",
            role="admin",
            password_hash="x",
            is_active=True,
        )
        session.add(actor)
        await session.commit()
        await session.refresh(actor)

        result = await svc.reconnect_account_action(
            session, _FakeRedis(), entity_id=str(account.id), actor=actor
        )

    assert "url" not in result, "канал на ключах не должен уводить на страницу согласия"
    assert result["account"]["status"] == "active"
    assert "снова на связи" in result["message"]
