"""Логи шлюза: structlog, JSON в stdout — journald подхватывает как есть.

Та же дисциплина, что у LeadChat (`app/core/logging.py`): ключи и токены до
строки лога не доходят (`scrub_secrets`), тела запросов и адреса клиентов не
пишутся никогда — в событиях только имя провайдера, `kind`, статус и время.
"""

from __future__ import annotations

import logging

import structlog
from structlog.typing import EventDict, WrappedLogger

SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "token",
    "secret",
    "x-api-key",
    "cookie",
    "set-cookie",
}


def scrub_secrets(logger: WrappedLogger, method_name: str, event_dict: EventDict) -> EventDict:
    for key in list(event_dict):
        if key.lower() in SECRET_KEYS:
            event_dict[key] = "[redacted]"
    return event_dict


def configure_logging(level_name: str = "INFO") -> None:
    level = getattr(logging, level_name.upper(), logging.INFO)
    structlog.configure(
        processors=[
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            scrub_secrets,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )
