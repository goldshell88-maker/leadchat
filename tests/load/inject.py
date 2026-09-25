"""Серверная половина нагрузочного прогона (docs/07-TESTING-SECURITY.md §3).

Зачем отдельный скрипт, если сценарий — k6 (tests/load/release.js):

1. Управляющая плоскость fake-avito (`/_control/*`) наружу не опубликована —
   вброс «через fake-avito» физически возможен только изнутри сети compose.
2. Часы. Сквозная задержка «вброс → сообщение в БД» меряется одними часами
   только если инъектор и наблюдатель живут на одной машине. k6 с ноутбука
   меряет клиентскую задержку (плюс интернет-плечо), этот скрипт — серверную.
3. Серверные метрики §3.2 (XLEN/XPENDING, pg_stat_activity, pending-сообщения)
   снимаются тем же процессом с той же временной сеткой.

Запуск — одноразовым контейнером из боевого образа, программа идёт по stdin,
на сервере ничего не устанавливается и не остаётся:

    cd /srv/leadchat && docker compose --env-file .env.prod \
      -f docker-compose.prod.yml -f docker-compose.override.yml \
      run --rm --no-deps -T -e PROFILE=smoke --entrypoint python api - < inject.py

Профили: PROFILE=smoke — четверть нагрузки 2 минуты; PROFILE=full — профиль
§3.1 (разгон 5 мин → плато 50/мин 20 мин → спайк 250/мин 2 мин → 50/мин 3 мин).

Все созданные данные помечены MARK (по умолчанию LOADTEST) — и в тексте
сообщения, и в имени клиента: уборка идёт по этой метке.
"""

from __future__ import annotations

import asyncio
import json
import os
import statistics
import sys
import time
from typing import Any

import asyncpg
import httpx
from redis.asyncio import Redis

MARK = os.environ.get("MARK", "LOADTEST")
PROFILE = os.environ.get("PROFILE", "smoke")
FAKE_AVITO = os.environ.get("FAKE_AVITO_URL", "http://fake-avito:8020")
AVITO_USER_ID = int(os.environ.get("AVITO_USER_ID", "111222333"))
CHAT_POOL = int(os.environ.get("CHAT_POOL", "50"))
SAMPLE_SECONDS = int(os.environ.get("SAMPLE_SECONDS", "10"))
DRAIN_SECONDS = int(os.environ.get("DRAIN_SECONDS", "60"))
STREAM, GROUP = "webhooks:avito", "workers"

# (целевая скорость в минуту, длительность в секундах) — линейный разгон
PROFILES: dict[str, list[tuple[int, int]]] = {
    "probe": [(12, 20)],  # проверка самого скрипта
    "smoke": [(13, 30), (13, 90)],
    "full": [(50, 300), (50, 1200), (250, 120), (50, 180)],
    "mini": [(50, 60), (250, 60), (50, 60)],
}


def now_ms() -> int:
    return int(time.time() * 1000)


def _group_name(group: dict[str, Any]) -> str:
    name = group.get("name")
    return name.decode(errors="replace") if isinstance(name, bytes) else str(name)


def dsn() -> str:
    raw = os.environ["DATABASE_URL"]
    return raw.replace("postgresql+asyncpg://", "postgresql://").replace(
        "postgres+asyncpg://", "postgresql://"
    )


class Run:
    def __init__(self) -> None:
        self.injected: list[dict[str, Any]] = []  # {token, t0, ok, ms}
        self.seen: dict[str, int] = {}  # token -> задержка до publish, мс
        self.samples: list[dict[str, Any]] = []
        self.chat_ids: list[str] = []
        self.tasks: set[asyncio.Task[None]] = set()  # держим ссылки: иначе GC съест

    # ------------------------------------------------------------ инъекция --
    async def inject_one(self, client: httpx.AsyncClient, idx: int) -> None:
        t0 = now_ms()
        token = f"{MARK}-SRV|{t0}|{idx}"
        body: dict[str, Any] = {
            "account_user_id": AVITO_USER_ID,
            "author": f"{MARK} Клиент {idx % CHAT_POOL}",
            "author_id": 900000000 + (idx % CHAT_POOL),
            "text": token,
        }
        if self.chat_ids:
            body["chat_id"] = self.chat_ids[idx % len(self.chat_ids)]
        else:  # пул не создался (мок недоступен) — вброс всё равно должен идти
            body["item"] = {"id": 3060161080, "title": f"{MARK} лот", "price_string": "1 ₽"}
        ok, delivered = False, False
        try:
            r = await client.post("/_control/incoming", json=body, timeout=15)
            ok = r.status_code == 200
            if ok:
                data = r.json()
                delivered = bool(data.get("webhook", {}).get("delivered"))
                if len(self.chat_ids) < CHAT_POOL and data.get("chat_id") not in self.chat_ids:
                    self.chat_ids.append(data["chat_id"])
        except Exception as exc:  # noqa: BLE001 — фиксируем факт отказа, не падаем
            print(f"inject error: {exc}", file=sys.stderr)
        self.injected.append(
            {"token": token, "t0": t0, "ok": ok, "delivered": delivered, "ms": now_ms() - t0}
        )

    async def prepare_pool(self, client: httpx.AsyncClient) -> None:
        """Пул диалогов создаётся ДО прогона, по одному.

        Иначе гонка: первый же ответ мока делает пул непустым, все остальные
        вбросы уходят в него, и вся нагрузка садится на один-два диалога —
        это и нереалистично, и превращает прогон в тест блокировок одной строки.
        """
        for i in range(CHAT_POOL):
            r = await client.post(
                "/_control/incoming",
                json={
                    "account_user_id": AVITO_USER_ID,
                    "author": f"{MARK} Клиент {i}",
                    "author_id": 900000000 + i,
                    "text": f"{MARK}-SEED|{now_ms()}|{i}",
                    "item": {"id": 3060161080, "title": f"{MARK} лот", "price_string": "1 ₽"},
                },
                timeout=15,
            )
            if r.status_code == 200:
                self.chat_ids.append(r.json()["chat_id"])
            await asyncio.sleep(0.2)
        print(f"пул диалогов: {len(self.chat_ids)}", file=sys.stderr, flush=True)

    async def injector(self) -> None:
        stages = PROFILES[PROFILE]
        idx, prev_rate = 0, float(stages[0][0])
        async with httpx.AsyncClient(base_url=FAKE_AVITO) as client:
            await self.prepare_pool(client)
            for target, seconds in stages:
                started = time.monotonic()
                while True:
                    elapsed = time.monotonic() - started
                    if elapsed >= seconds:
                        break
                    rate = prev_rate + (target - prev_rate) * (elapsed / seconds)
                    rate = max(rate, 1.0)
                    task = asyncio.create_task(self.inject_one(client, idx))
                    self.tasks.add(task)
                    task.add_done_callback(self.tasks.discard)
                    idx += 1
                    await asyncio.sleep(60.0 / rate)
                prev_rate = float(target)

    # ------------------------------------------------- наблюдатель pub/sub --
    async def watcher(self, redis: Redis) -> None:
        """`events` публикуется строго ПОСЛЕ коммита (08 §8.1) — момент появления
        кадра и есть момент, когда сообщение стало видимым в БД и уехало в WS."""
        pubsub = redis.pubsub()
        await pubsub.subscribe("events")
        async for raw in pubsub.listen():
            if raw["type"] != "message":
                continue
            try:
                evt = json.loads(raw["data"])
            except ValueError:
                continue
            if evt.get("type") != "message:new":
                continue
            body = str((evt.get("data") or {}).get("message", {}).get("body") or "")
            if not body.startswith(f"{MARK}-SRV|"):
                continue
            parts = body.split("|")
            if len(parts) >= 2 and body not in self.seen:
                self.seen[body] = now_ms() - int(parts[1])

    # ---------------------------------------------------------- сэмплирование
    async def sampler(self, redis: Redis, pool: asyncpg.Pool) -> None:
        """Крутится до cancel() — в том числе весь добор после спайка: именно
        там видно, за сколько разбирается очередь (критерий §3.3 п.3)."""
        while True:
            sample: dict[str, Any] = {"t": time.strftime("%H:%M:%S")}
            try:
                # xlen — длина стрима целиком (он не подрезается, см. отчёт),
                # реальный лаг очереди — lag группы + pending
                sample["xlen"] = await redis.xlen(STREAM)
                groups = await redis.xinfo_groups(STREAM)
                grp: dict[str, Any] = next((g for g in groups if _group_name(g) == GROUP), {})
                sample["lag"] = int(grp.get("lag") or 0)
                pend = await redis.xpending(STREAM, GROUP)
                sample["xpending"] = pend.get("pending", 0) if isinstance(pend, dict) else 0
                sample["dlq"] = await redis.xlen("webhooks:avito:dlq")
                sample["arq_queue"] = await redis.zcard("arq:queue")
            except Exception as exc:  # noqa: BLE001
                sample["redis_error"] = str(exc)
            try:
                async with pool.acquire() as conn:
                    rows = await conn.fetch(
                        "select state, count(*) n,"
                        " coalesce(max(extract(epoch from now()-state_change)),0) max_age"
                        " from pg_stat_activity where datname=current_database() group by state"
                    )
                    # extract(epoch ...) приезжает Decimal — в JSON он не сериализуется
                    sample["pg"] = {
                        (r["state"] or "null"): {
                            "n": int(r["n"]),
                            "max_age_s": round(float(r["max_age"]), 1),
                        }
                        for r in rows
                    }
                    sample["msg_pending"] = await conn.fetchval(
                        "select count(*) from messages where delivery_status='pending'"
                    )
            except Exception as exc:  # noqa: BLE001
                sample["pg_error"] = str(exc)
            self.samples.append(sample)
            print(
                "sample " + json.dumps(sample, ensure_ascii=False, default=str),
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(SAMPLE_SECONDS)

    # ------------------------------------------------------------- итоги ----
    async def report(self, redis: Redis, pool: asyncpg.Pool) -> dict[str, Any]:
        lat = sorted(self.seen.values())
        inj_ms = sorted(x["ms"] for x in self.injected)

        def pct(data: list[int], p: float) -> float | None:
            if not data:
                return None
            k = max(0, min(len(data) - 1, int(round(p / 100 * (len(data) - 1)))))
            return data[k]

        async with pool.acquire() as conn:
            in_db = await conn.fetchval(
                "select count(*) from messages where direction='in' and body like $1",
                f"{MARK}-SRV|%",
            )
            out_rows = await conn.fetch(
                "select delivery_status, count(*) n from messages"
                " where direction='out' and body like $1 group by 1",
                f"{MARK}%",
            )
            convs = await conn.fetchval(
                "select count(distinct conversation_id) from messages where body like $1",
                f"{MARK}%",
            )
        return {
            "profile": PROFILE,
            "injected": len(self.injected),
            "injected_ok": sum(1 for x in self.injected if x["ok"]),
            "webhook_delivered": sum(1 for x in self.injected if x["delivered"]),
            "inject_call_ms": {
                "p50": pct(inj_ms, 50),
                "p95": pct(inj_ms, 95),
                "max": inj_ms[-1] if inj_ms else None,
            },
            "e2e_to_db_ms": {
                "n": len(lat),
                "p50": pct(lat, 50),
                "p95": pct(lat, 95),
                "p99": pct(lat, 99),
                "max": lat[-1] if lat else None,
                "avg": round(statistics.fmean(lat), 1) if lat else None,
            },
            "lost": len(self.injected) - len(self.seen),
            "messages_in_db": in_db,
            "outbound_status": {r["delivery_status"]: r["n"] for r in out_rows},
            "conversations_created": convs,
            "stream_len_max": max((s.get("xlen", 0) for s in self.samples), default=0),
            "queue_lag_max": max((s.get("lag", 0) for s in self.samples), default=0),
            "pending_max": max((s.get("xpending", 0) for s in self.samples), default=0),
            "dlq": self.samples[-1].get("dlq") if self.samples else None,
            "samples": self.samples,
        }


async def main() -> None:
    redis = Redis.from_url(os.environ.get("REDIS_URL", "redis://redis:6379/0"))
    pool = await asyncpg.create_pool(dsn(), min_size=1, max_size=2)
    run = Run()
    watcher = asyncio.create_task(run.watcher(redis))
    sampler = asyncio.create_task(run.sampler(redis, pool))
    await run.injector()
    print(f"инъекция закончена, добор {DRAIN_SECONDS} с", file=sys.stderr, flush=True)
    await asyncio.sleep(DRAIN_SECONDS)
    watcher.cancel()
    sampler.cancel()
    report = await run.report(redis, pool)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    await pool.close()
    await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
