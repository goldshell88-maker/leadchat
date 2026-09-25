"""Structured logging: structlog with JSON output to stdout (05 §7.3).

Every process (api / worker / scheduler) uses the same configuration; log
rotation is Docker's job. Secrets never reach the log stream — see
``scrub_secrets`` (the same set is used as Sentry ``before_send`` later).
"""

import logging
import os
import secrets
from contextvars import ContextVar

import structlog
from structlog.typing import EventDict, WrappedLogger

from app.core.config import settings

# Keys that must never appear in logs with real values (05 §7.3)
SECRET_KEYS = {
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "secret",
    "webhook_secret",
    "api_key",
    "apikey",  # ключ Яндекс Геокодера едет параметром запроса
    "token",
    "cookie",
    "set-cookie",
}

# Request id, bound by middleware in app.main; read by error handlers.
request_id_var: ContextVar[str] = ContextVar("request_id", default="")


def new_request_id() -> str:
    return "req_" + secrets.token_hex(4)


def scrub_secrets(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if key.lower() in SECRET_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(component: str = "api") -> None:
    level = getattr(logging, settings.log_level.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            scrub_secrets,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Одна запись — один write() вместе с переводом строки. PrintLogger по
        # умолчанию пишет текст и «\n» двумя вызовами, и у api с двумя
        # процессами на одном stdout записи перемежались: «A B\n\n» — 1 503
        # склеенные строки в суточном архиве (проверка 24.09).
        logger_factory=structlog.WriteLoggerFactory(),
        # ЗАПОМИНАТЬ ЛИ ЦЕПОЧКУ ОБРАБОТЧИКОВ НА ЛОГГЕРЕ. В бою — да: кэш
        # экономит на КАЖДОЙ строке лога, а строк десятки тысяч в сутки.
        #
        # НО В ТЕСТАХ ЭТОТ КЭШ ЛОМАЕТ ЗАХВАТ ЛОГОВ, и ломает молча. Логгер
        # модуля (`log = structlog.get_logger(...)`) запоминает цепочку при
        # ПЕРВОМ использовании. Если он успел поработать в обычном тесте
        # раньше, `structlog.testing.capture_logs()` подменяет глобальную
        # настройку уже впустую: строки идут по запомненной боевой цепочке — в
        # stdout, — а захват возвращает пустой список. Тест читает это как
        # «в журнале ни строчки» и падает, рассказывая про дефект, которого нет.
        #
        # Найдено 12 августа на первом прогоне ВСЕГО набора одним процессом:
        # интеграционные тесты собираются раньше модульных, поднимают
        # приложение (и эту настройку с кэшем), а через пять минут падает
        # `test_the_skipped_chat_leaves_a_trace_in_the_journal` — который в
        # одиночку и своим файлом зелёный, потому что там первое использование
        # логгера случается уже внутри захвата.
        #
        # Поэтому решение вынесено наружу: `conftest.py` ставит LOG_CACHE=0, и
        # захват работает независимо от того, кто отработал раньше. Умолчание —
        # боевое.
        cache_logger_on_first_use=os.getenv("LOG_CACHE", "1") != "0",
    )
    structlog.contextvars.bind_contextvars(component=component)
