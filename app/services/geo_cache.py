"""Кэш ответов карт: один и тот же запрос — один поход к API (владелец 13.09:
«оптимизировать запросы, чтобы тратить меньше ресурса»).

Дом по адресу не меняется месяцами, а один и тот же запрос уходит к карте
по многу раз: перепроверка после дописанного посёлка, повтор клиентом того же
адреса, починка планировщика, дубли строк. Ответ (сырой JSON, до разбора)
лежит в Redis :data:`TTL` суток под ключом от текста запроса; попадание не
считается в суточные потолки — счётчики зовутся только при настоящем походе.

Кэшируются только удачные ответы (200 и JSON): отказ карты повторяется сам.
Без Redis (тесты чистых функций, CLI без привязки) кэш молчит — функции
интеграций работают как раньше.
"""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from typing import Any

import structlog
from redis.asyncio import Redis

log = structlog.get_logger()

#: Неделя для ответа с домом; пустой ответ («карта ещё не знает») — сутки:
#: перепроверка через неделю обязана снова спросить карту (ревью 13.09).
TTL = 7 * 24 * 3600
TTL_EMPTY = 24 * 3600
#: Боевой Redis — 512 МБ с `noeviction`: переполнение уронило бы ВСЕ записи
#: (ревью 13.09). Значения ужимаются zlib (JSON ужимается в 5–10 раз), DaData
#: — до нужных полей; сверх этого потолка на ключ — не кэшируем.
MAX_VALUE = 64 * 1024
_redis: Redis | None = None


def bind(redis: Redis | None) -> None:
    """Привязать Redis — зовёт воркер в начале задачи; None — отвязать."""
    global _redis
    _redis = redis


def key(provider: str, payload: Any) -> str:
    сырой = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return f"geo:cache:{provider}:{hashlib.sha1(сырой.encode()).hexdigest()}"


async def get(provider: str, payload: Any) -> Any | None:
    """Сохранённый ответ или None. Ошибка Redis — как промах."""
    if _redis is None:
        return None
    try:
        сырой = await _redis.get(key(provider, payload))
    except Exception as exc:  # noqa: BLE001
        log.warning("geo_cache.read_failed", provider=provider, error=type(exc).__name__)
        return None
    if not сырой:
        return None
    try:
        # Клиент Redis отдаёт строки (decode_responses): сжатое лежит в base64.
        return json.loads(zlib.decompress(base64.b64decode(сырой)))
    except (ValueError, zlib.error, TypeError):
        return None


async def put(provider: str, payload: Any, value: Any, *, empty: bool = False) -> None:
    """Запомнить ответ; `empty` — карта ничего полезного не нашла: срок короче."""
    if _redis is None:
        return
    try:
        сжатый = base64.b64encode(
            zlib.compress(json.dumps(value, ensure_ascii=False).encode(), 6)
        ).decode()
        if len(сжатый) > MAX_VALUE:
            log.info("geo_cache.too_big", provider=provider, size=len(сжатый))
            return
        await _redis.set(key(provider, payload), сжатый, ex=TTL_EMPTY if empty else TTL)
    except Exception as exc:  # noqa: BLE001
        log.warning("geo_cache.write_failed", provider=provider, error=type(exc).__name__)
