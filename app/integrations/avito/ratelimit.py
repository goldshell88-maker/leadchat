"""Пер-аккаунтный rate-limit бюджет к API Авито (08 §4.3).

Фиксированное окно 60 с в Redis (`ratelimit:avito:{account}:{min}`),
глобальное на аккаунт — окно честно делится между репликами worker'а.
Два класса трафика:

- interactive (deliver_message, действия из UI) — весь бюджет;
- bulk (backfill, reconciliation) — 80%: интерактиву всегда остаётся запас.

`acquire()` блокирует корутину до появления бюджета — вызывающий код
о 429 не думает. 429 от Авито всё равно обрабатывается отдельно
(клиент бросает RateLimited): лимитер — предохранитель, Retry-After — истина.
"""

from __future__ import annotations

import asyncio
import random
import time

from redis.asyncio import Redis

# Бюджет консервативный; реальную квоту Messenger API уточнить в кабинете
# разработчика (developers.avito.ru) при старте — дисклеймер DESIGN §8.
BUDGET_PER_MIN = 500  # всего запросов в минуту на аккаунт
BULK_SHARE = 0.8  # bulk-потребители не занимают больше 80% бюджета
WINDOW_TTL = 120  # ключ живёт два окна — гарантированно переживает своё


class AvitoRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def acquire(self, account_id: str, *, bucket: str = "interactive") -> None:
        limit = BUDGET_PER_MIN if bucket == "interactive" else int(BUDGET_PER_MIN * BULK_SHARE)
        while True:
            key = f"ratelimit:avito:{account_id}:{int(time.time() // 60)}"
            # ⚠ СРОК СТАВИТСЯ ВМЕСТЕ С РОЖДЕНИЕМ КЛЮЧА, ОДНОЙ ТРАНЗАКЦИЕЙ.
            # Здесь стояло `incr`, а затем `if n == 1: expire` — и между двумя
            # командами есть щель: умри процесс в ней, и ключ останется БЕЗ
            # СРОКА навсегда. Это не гипотеза: в бою 05.09 найден
            # `ratelimit:avito:85ea8246-…:29797514` с TTL = -1, значением 87 и
            # номером окна на 8,8 суток старше текущего. Такой ключ не мешает
            # (окно в имени сменилось), но остаётся в памяти навечно, и каждая
            # смерть воркера в неудачный момент добавляет следующий.
            pipe = self.redis.pipeline()
            pipe.set(key, 0, ex=WINDOW_TTL, nx=True)
            pipe.incr(key)
            n = (await pipe.execute())[-1]
            if n <= limit:
                return
            await self.redis.decr(key)  # не съедаем чужой бюджет, ждём окно
            await asyncio.sleep(1 + random.random())
