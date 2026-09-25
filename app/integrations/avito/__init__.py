"""Интеграция с Авито: HTTP-клиент, парсеры payload'ов, rate-limit бюджет.

Публичная поверхность для воркеров/сервисов (DESIGN §1.6):
клиент — HTTP и коды ошибок; адаптер — нормализация в InboundEvent;
жизненный цикл токенов — app/services/avito_accounts.py.
"""

from app.integrations.avito.adapter import AvitoAdapter, ChatInfo, InboundEvent
from app.integrations.avito.client import AvitoClient, build_authorize_url
from app.integrations.avito.errors import (
    AvitoApiError,
    AvitoAuthError,
    RateLimited,
    TokenRevokedError,
    WebhookParseError,
)
from app.integrations.avito.ratelimit import AvitoRateLimiter

__all__ = [
    "AvitoAdapter",
    "AvitoApiError",
    "AvitoAuthError",
    "AvitoClient",
    "AvitoRateLimiter",
    "ChatInfo",
    "InboundEvent",
    "RateLimited",
    "TokenRevokedError",
    "WebhookParseError",
    "build_authorize_url",
]
