"""Sentry для всех трёх Python-процессов (05 §7.1).

Одна функция инициализации на api / worker / scheduler / cli:

    from app.core.observability import init_sentry
    init_sentry("api")

Свойства, на которые здесь всё держится:

* **Без DSN — молча ничего не делаем.** Dev, CI и юнит-тесты не должны
  ни ходить в сеть, ни падать из-за отсутствия пакета: ``sentry-sdk``
  импортируется защищённо, и его отсутствие — это ``warning`` в лог, а не
  ImportError на старте приложения.
* **Секреты не утекают.** ``before_send`` прогоняет событие через тот же
  список ключей, что и фильтр structlog (05 §7.3, ``SECRET_KEYS``):
  заголовки, cookie, тело запроса, query-string, ``extra`` и ``contexts``.
  ``send_default_pii=False`` — тела и заголовки Sentry сам не тянет.
* **Идемпотентность.** Повторный вызов (например, lifespan в тестах) не
  переинициализирует клиент.

``environment`` = ENV, ``release`` = APP_VERSION или IMAGE_TAG (тег образа =
git SHA), ``traces_sample_rate`` = SENTRY_TRACES_SAMPLE_RATE — всё из настроек
(имена переменных — контракт 05 §4).
"""

import functools
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

import structlog

from app.core.config import settings
from app.core.logging import SECRET_KEYS

log = structlog.get_logger("app.observability")

REDACTED = "[redacted]"
_MAX_SCRUB_DEPTH = 6  # события Sentry вложены неглубоко; страховка от циклов

_initialized = False


def _is_secret(key: Any) -> bool:
    return isinstance(key, str) and key.lower() in SECRET_KEYS


def _scrub(value: Any, depth: int = 0) -> Any:
    """Рекурсивно затереть значения секретных ключей в структуре события."""
    if depth >= _MAX_SCRUB_DEPTH:
        return value
    if isinstance(value, dict):
        return {k: (REDACTED if _is_secret(k) else _scrub(v, depth + 1)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, depth + 1) for v in value]
    if isinstance(value, tuple):
        return tuple(_scrub(v, depth + 1) for v in value)
    return value


def _scrub_query_string(qs: Any) -> Any:
    """query_string приходит строкой — разбираем и собираем обратно."""
    if not isinstance(qs, str) or not qs:
        return qs
    pairs = parse_qsl(qs, keep_blank_values=True)
    if not pairs:
        return qs
    return urlencode([(k, REDACTED if _is_secret(k) else v) for k, v in pairs])


#: Хосты карт: в строке запроса к ним — адрес клиента, а это персональные
#: данные. Sentry записывает исходящие HTTP в хлебные крошки и спаны вместе с
#: URL целиком; для этих хостов оставляем только хост. С 16.09 все походы к
#: картам идут через шлюз Амстердама (docs/46) телом POST, а не строкой
#: запроса, — но правило держим и для него: адрес шлюза единственный, и
#: подставить его в строку запроса завтра может любая новая ручка.
def geo_hosts() -> tuple[str, ...]:
    хост = urlsplit(settings.gateway_url.strip()).hostname if settings.gateway_url else None
    return (хост,) if хост else ()


def _без_адреса(url: Any) -> Any:
    if not isinstance(url, str):
        return url
    for host in geo_hosts():
        if host in url:
            return f"https://{host}/{REDACTED}"
    return url


def _scrub_geo_breadcrumbs(crumbs: Any) -> Any:
    if not isinstance(crumbs, dict) or not isinstance(crumbs.get("values"), list):
        return crumbs
    for crumb in crumbs["values"]:
        data = crumb.get("data") if isinstance(crumb, dict) else None
        if isinstance(data, dict) and "url" in data:
            data["url"] = _без_адреса(data["url"])
            if data["url"].endswith(REDACTED):
                data.pop("http.query", None)
    return crumbs


def scrub_geo_transaction(
    event: dict[str, Any], hint: dict[str, Any] | None = None
) -> dict[str, Any]:
    """``before_send_transaction``: спаны к картам — без строки запроса.

    Интеграция httpx пишет в спан описание «GET <url>» и `http.query`; для
    хостов карт там лежит улица и дом клиента. Оставляем хост.
    """
    for span in event.get("spans") or []:
        if not isinstance(span, dict):
            continue
        описание = span.get("description")
        if isinstance(описание, str) and any(h in описание for h in geo_hosts()):
            span["description"] = _без_адреса(описание)
            data = span.get("data")
            if isinstance(data, dict):
                data.pop("http.query", None)
                if "url" in data:
                    data["url"] = _без_адреса(data["url"])
    return event


def scrub_secrets(event: dict[str, Any], hint: dict[str, Any] | None = None) -> dict[str, Any]:
    """``before_send``: тот же фильтр секретов, что у логгера (05 §7.1/§7.3)."""
    if "breadcrumbs" in event:
        event["breadcrumbs"] = _scrub_geo_breadcrumbs(event["breadcrumbs"])
    request = event.get("request")
    if isinstance(request, dict):
        if isinstance(request.get("cookies"), dict):
            # cookie целиком: там refresh-токен сессии (01 §1.2)
            request["cookies"] = dict.fromkeys(request["cookies"], REDACTED)
        for field in ("headers", "data", "env"):
            if field in request:
                request[field] = _scrub(request[field])
        if "query_string" in request:
            request["query_string"] = _scrub_query_string(request["query_string"])
    for field in ("extra", "contexts", "tags", "breadcrumbs"):
        if field in event:
            event[field] = _scrub(event[field])
    return event


def _integrations() -> list[Any]:
    """Интеграции, которые есть в установленной версии SDK.

    Набор из 05 §7.1 плюс asyncio/redis/sqlalchemy: каждая импортируется
    отдельно — состав модулей sentry-sdk меняется от версии к версии, и одна
    отсутствующая интеграция не должна лишать нас всей телеметрии.
    """
    found: list[Any] = []
    wanted = (
        ("sentry_sdk.integrations.fastapi", "FastApiIntegration"),
        ("sentry_sdk.integrations.starlette", "StarletteIntegration"),
        ("sentry_sdk.integrations.asyncio", "AsyncioIntegration"),
        ("sentry_sdk.integrations.redis", "RedisIntegration"),
        ("sentry_sdk.integrations.sqlalchemy", "SqlalchemyIntegration"),
        ("sentry_sdk.integrations.asyncpg", "AsyncPGIntegration"),
    )
    import importlib

    for module_name, class_name in wanted:
        try:
            module = importlib.import_module(module_name)
            found.append(getattr(module, class_name)())
        except Exception as exc:  # noqa: BLE001 — интеграции опциональны
            log.debug(
                "sentry.integration_skipped", integration=class_name, error=type(exc).__name__
            )
    return found


def init_sentry(component: str) -> bool:
    """Инициализировать Sentry для процесса. True — клиент поднят.

    ``component``: api | worker | scheduler | cli — уходит тегом, чтобы в
    Sentry было видно, какой процесс сломался.
    """
    global _initialized
    if _initialized:
        return True
    if not settings.sentry_dsn:
        log.debug("sentry.disabled", component=component)  # DSN не задан — штатный dev-режим
        return False
    try:
        # sentry-sdk теперь в зависимостях проекта, но импорт остаётся
        # защищённым: урезанная сборка без пакета обязана подниматься.
        import sentry_sdk
    except ImportError:
        # Пакет не установлен (dev-окружение без monitoring-экстры) — сервис
        # обязан подняться без него.
        log.warning("sentry.sdk_missing", component=component)
        return False

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.env,
        release=settings.version,  # APP_VERSION, иначе IMAGE_TAG (= git SHA)
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=_integrations(),
        # before_send в стабах SDK объявлен через TypedDict Event; наш фильтр
        # работает с обычным dict (его же гоняют юнит-тесты) — приводим тип.
        before_send=cast(Any, scrub_secrets),
        before_send_transaction=cast(Any, scrub_geo_transaction),
        send_default_pii=False,  # ни тел, ни заголовков «за компанию»
        # ⚠ ЛОКАЛЬНЫЕ ПЕРЕМЕННЫЕ КАДРОВ НЕ ОТПРАВЛЯЮТСЯ (ревью 11.09). В них —
        # разобранный адрес клиента, ответы карты и ключ Яндекса из параметров
        # запроса; фильтр по именам полей их не видит. Цена — меньше подсказок
        # в трассировке; персональные данные дороже.
        include_local_variables=False,
    )
    sentry_sdk.set_tag("component", component)
    _initialized = True
    log.info("sentry.initialized", component=component, environment=settings.env)
    return True


def reset_for_tests() -> None:
    """Сбросить флаг однократной инициализации (используется в тестах)."""
    global _initialized
    _initialized = False


# ----------------------------------------------------------- ARQ job scopes

# Теги, которые 05 §7.1 требует у события из воркера: по нему сразу видно,
# какой аккаунт/диалог пострадал. Берём их из ИМЁН параметров задачи, поэтому
# работает и для позиционного, и для именованного вызова.
JOB_TAG_PARAMS: tuple[str, ...] = ("account_id", "conversation_id", "message_id", "job_id")


def _active_sdk() -> Any | None:
    """Модуль sentry_sdk, если клиент реально поднят; иначе None.

    Пока Sentry не инициализирован (dev, CI, юниты) обёртка задачи обязана
    быть бесплатной — ни импорта, ни scope'ов.
    """
    if not _initialized:
        return None
    try:
        import sentry_sdk
    except ImportError:  # pragma: no cover — init_sentry без пакета не взлетает
        return None
    return sentry_sdk


def _job_tags(
    signature: inspect.Signature, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> dict[str, str]:
    """account_id / conversation_id / … из фактических аргументов задачи."""
    try:
        bound = signature.bind(*args, **kwargs)
    except TypeError:  # pragma: no cover — кривой вызов упадёт ниже, в самой задаче
        return {}
    tags = {
        name: str(bound.arguments[name])
        for name in JOB_TAG_PARAMS
        if bound.arguments.get(name) is not None
    }
    ctx = args[0] if args and isinstance(args[0], dict) else None
    if ctx:
        for name in ("job_id", "job_try"):
            if name not in tags and ctx.get(name) is not None:
                tags[name] = str(ctx[name])
    return tags


def with_job_scope[**P, R](func: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
    """Обернуть ARQ-задачу в отдельный ``sentry_sdk.new_scope()`` (05 §7.1).

    Без изоляции теги одной задачи протекают в события следующей — воркер
    живёт долго и крутит задачи в одном процессе.

    ``functools.wraps`` здесь не косметика: ARQ берёт имя задачи в очереди из
    ``__qualname__``, и без переноса имени обёртка переименовала бы задачу
    (например, ``export_stats`` — контракт с ``services.stats.EXPORT_JOB``).
    """
    signature = inspect.signature(func)

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
        sdk = _active_sdk()
        if sdk is None:
            return await func(*args, **kwargs)
        with sdk.new_scope() as scope:
            scope.set_tag("job", func.__qualname__)
            for name, value in _job_tags(signature, args, kwargs).items():
                scope.set_tag(name, value)
            return await func(*args, **kwargs)

    return wrapper
