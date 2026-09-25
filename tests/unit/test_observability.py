"""Sentry (05 §7.1): без DSN — no-op, с DSN — событие без секретов."""

import pytest

from app.core import observability as obs
from app.core.config import settings


@pytest.fixture(autouse=True)
def _reset():
    obs.reset_for_tests()
    yield
    obs.reset_for_tests()


def test_init_is_noop_without_dsn(monkeypatch):
    """Dev/CI: пустой SENTRY_DSN не должен ни падать, ни ходить в сеть."""
    monkeypatch.setattr(settings, "sentry_dsn", "")
    assert obs.init_sentry("api") is False


def test_init_survives_missing_sdk(monkeypatch):
    """sentry-sdk не установлен — сервис обязан подняться (warning, не ImportError)."""
    monkeypatch.setattr(settings, "sentry_dsn", "https://key@example.invalid/1")
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "sentry_sdk":
            raise ImportError("no sentry_sdk here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert obs.init_sentry("worker") is False


def test_scrub_redacts_headers_cookies_and_body():
    event = {
        "request": {
            "headers": {"Authorization": "Bearer real-token", "User-Agent": "curl"},
            "cookies": {"refresh_token": "rt-secret"},
            "data": {"password": "hunter2", "email": "user@example.com"},
            "query_string": "token=abc&tab=all",
        },
        "extra": {"webhook_secret": "whsec-real", "account_id": "b2a4c6e8"},
    }
    out = obs.scrub_secrets(event, None)

    assert out["request"]["headers"]["Authorization"] == obs.REDACTED
    assert out["request"]["headers"]["User-Agent"] == "curl"  # не секрет — оставляем
    assert out["request"]["cookies"]["refresh_token"] == obs.REDACTED
    assert out["request"]["data"]["password"] == obs.REDACTED
    assert out["request"]["data"]["email"] == "user@example.com"
    assert "abc" not in out["request"]["query_string"]
    assert "tab=all" in out["request"]["query_string"]
    assert out["extra"]["webhook_secret"] == obs.REDACTED
    assert out["extra"]["account_id"] == "b2a4c6e8"


def test_scrub_goes_into_nested_structures():
    event = {"extra": {"payload": {"items": [{"access_token": "at", "id": 7}]}}}
    out = obs.scrub_secrets(event, None)
    item = out["extra"]["payload"]["items"][0]
    assert item["access_token"] == obs.REDACTED
    assert item["id"] == 7


def test_scrub_keeps_event_without_request():
    assert obs.scrub_secrets({"message": "hi"}, None) == {"message": "hi"}


# --- ARQ-задачи в отдельном scope (05 §7.1) ----------------------------------


class _Scope:
    def __init__(self) -> None:
        self.tags: dict[str, str] = {}

    def set_tag(self, key: str, value: str) -> None:
        self.tags[key] = value


class _FakeSdk:
    """Минимальный стенд sentry_sdk: нас интересуют scope и его теги."""

    def __init__(self) -> None:
        self.scopes: list[_Scope] = []

    def new_scope(self):
        import contextlib

        scope = _Scope()
        self.scopes.append(scope)

        @contextlib.contextmanager
        def _cm():
            yield scope

        return _cm()


@pytest.fixture
def fake_sdk(monkeypatch) -> _FakeSdk:
    sdk = _FakeSdk()
    monkeypatch.setattr(obs, "_active_sdk", lambda: sdk)
    return sdk


async def test_job_scope_tags_job_account_and_conversation(fake_sdk):
    """05 §7.1: по событию из воркера видно, какой аккаунт/диалог пострадал."""
    import uuid

    account_id = uuid.uuid4()

    @obs.with_job_scope
    async def reconcile_account(ctx: dict, account_id) -> str:
        return "done"

    assert await reconcile_account({"job_id": "arq:1", "job_try": 2}, account_id) == "done"
    (scope,) = fake_sdk.scopes
    assert scope.tags["job"].endswith("reconcile_account")
    assert scope.tags["account_id"] == str(account_id)
    assert scope.tags["job_try"] == "2"
    assert scope.tags["job_id"] == "arq:1"


async def test_job_scope_reads_keyword_arguments_too(fake_sdk):
    conversation_id = "c-42"

    @obs.with_job_scope
    async def enrich_client(ctx: dict, conversation_id) -> None:
        return None

    await enrich_client({}, conversation_id=conversation_id)
    (scope,) = fake_sdk.scopes
    assert scope.tags["conversation_id"] == "c-42"


async def test_each_job_run_gets_its_own_scope(fake_sdk):
    """Изоляция: теги одной задачи не протекают в события следующей."""

    @obs.with_job_scope
    async def deliver_message(ctx: dict, message_id) -> None:
        return None

    await deliver_message({}, "m-1")
    await deliver_message({}, "m-2")
    assert [s.tags["message_id"] for s in fake_sdk.scopes] == ["m-1", "m-2"]


async def test_job_scope_is_transparent_without_sentry():
    """Без поднятого клиента обёртка бесплатна и ничего не меняет."""

    @obs.with_job_scope
    async def export_stats(ctx: dict, job_id: str, params: dict) -> str:
        return f"{job_id}:{params['format']}"

    assert obs._active_sdk() is None  # Sentry в юнитах не инициализирован
    assert await export_stats({}, "j-1", {"format": "csv"}) == "j-1:csv"


async def test_job_scope_keeps_the_queue_name(fake_sdk):
    """ARQ берёт имя задачи из __qualname__ — обёртка не смеет его менять."""
    from app.services import stats as st
    from app.workers.main import WorkerSettings

    assert st.export_stats.__qualname__ == st.EXPORT_JOB
    assert st.export_stats in WorkerSettings.functions

    # ⚠ ЗАДАЧА С СОБСТВЕННЫМИ НАСТРОЙКАМИ ЛЕЖИТ В СПИСКЕ КАК `Function`.
    # Так зарегистрирован `enrich_client` (`keep_result=0`, чтобы отказ не
    # блокировал повтор на час). Имя при этом НЕ меняется: `arq.worker.func`
    # берёт его из того же `__qualname__` корутины — и проверка ниже сторожит
    # именно это, потому что постановка задачи идёт по строке имени.
    from app.workers.main import registered_job_names

    assert registered_job_names() >= {
        "deliver_message",
        "enrich_client",
        "reconcile_account",
        "backfill_account",
        "export_stats",
    }


def test_every_registered_arq_job_is_wrapped_in_a_scope():
    """Страж 05 §7.1: новая задача без обёртки = событие без тегов job/account."""
    from app.workers.main import WorkerSettings

    # ⚠ ЗАДАЧА МОЖЕТ БЫТЬ ЗАПИСАНА ДВУМЯ СПОСОБАМИ, И ПРОВЕРЯТЬ НАДО ОБА.
    #
    # Обычно в списке лежит сама корутина. Но задаче могут понадобиться свои
    # настройки — например `enrich_client` регистрируется через
    # `arq.worker.func(..., keep_result=0)`, чтобы отказ не блокировал повтор
    # на час. Тогда в списке лежит `Function`, а корутина — внутри него.
    #
    # Раньше тест этого не знал и падал с `AttributeError` на самом первом
    # таком случае. Инвариант при этом цел: обёртка `@with_job_scope` никуда не
    # девается, просто прячется на уровень глубже.
    def _корутина(f):
        return getattr(f, "coroutine", f)

    def _имя(f):
        c = _корутина(f)
        return getattr(c, "__qualname__", None) or getattr(f, "name", repr(f))

    unwrapped = [
        _имя(f)
        for f in WorkerSettings.functions
        if getattr(_корутина(f), "__wrapped__", None) is None
    ]
    assert unwrapped == [], f"ARQ-задачи без @with_job_scope: {unwrapped}"


# --- адрес клиента не уезжает в Sentry вместе с URL карты (11.09; с 16.09 карта
# — это шлюз Амстердама, хост берётся из GATEWAY_URL) ------------------------


def test_спан_к_карте_теряет_строку_запроса():
    from app.core.observability import REDACTED, scrub_geo_transaction

    событие = {
        "spans": [
            {
                "description": "GET http://gw.test/geo/nominatim?street=1+улица+Звенигородская&city=Орск",
                "data": {
                    "url": "http://gw.test/geo/nominatim",
                    "http.query": "street=1+улица+Звенигородская",
                },
            },
            {
                "description": "GET https://api.avito.ru/messenger/v2/chats",
                "data": {"http.query": "limit=1"},
            },
        ]
    }
    после = scrub_geo_transaction(событие)
    карта, авито = после["spans"]
    assert "Звенигородск" not in карта["description"]
    assert REDACTED in карта["description"]
    assert "http.query" not in карта["data"]
    # Чужие спаны не тронуты.
    assert авито["data"]["http.query"] == "limit=1"


def test_хлебная_крошка_к_карте_теряет_адрес():
    from app.core.observability import scrub_secrets

    событие = {
        "breadcrumbs": {
            "values": [
                {
                    "category": "httplib",
                    "data": {
                        "url": "http://gw.test/geo/yandex?geocode=Орск,+улица+Ленина+5",
                        "http.query": "geocode=Орск",
                    },
                }
            ]
        }
    }
    после = scrub_secrets(событие)
    крошка = после["breadcrumbs"]["values"][0]["data"]
    assert "Ленина" not in крошка["url"]
    assert "http.query" not in крошка


def test_локальные_переменные_кадров_в_sentry_не_уходят(monkeypatch):
    """В локалях задачи адреса — разобранный адрес клиента и ключ Яндекса;
    фильтр по именам полей их не видит (ревью 11.09)."""
    import types

    from app.core import observability as obs
    from app.core.config import settings

    obs.reset_for_tests()
    monkeypatch.setattr(settings, "sentry_dsn", "https://key@example.invalid/1")
    пойманные: dict = {}

    def fake_init(**kw):
        пойманные.update(kw)

    fake = types.SimpleNamespace(init=fake_init, set_tag=lambda *a, **k: None)
    monkeypatch.setitem(__import__("sys").modules, "sentry_sdk", fake)
    assert obs.init_sentry("worker") is True
    assert пойманные.get("include_local_variables") is False
    obs.reset_for_tests()
