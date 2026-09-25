"""Зависший вебхук: пять попыток — и в отстойник, а не по кругу навсегда.

`_reclaim_stuck` — единственное место, которое поднимает записи, брошенные
умершим воркером, и единственное, которое переносит «отравленную» запись в
`webhooks:avito:dlq`. До сих пор его не вызывал НИ ОДИН тест: в `tests/` не было
ни одного обращения к нему. То есть путь, по которому обращение живого клиента
уходит из основного потока в отстойник, держался на одном чтении глазами.

Проверяем три свойства, каждое из которых ломается молча:
  * запись, доставленную пять раз, переносим в отстойник и подтверждаем в
    основном потоке — иначе она вернётся сюда же на следующем проходе и будет
    ходить по кругу вечно;
  * тело переносим ЦЕЛИКОМ и с обратным адресом (`orig_id`, `failed_at`) —
    разбор отстойника (`replay_dlq`) читает именно его;
  * запись, доставленную меньше пяти раз, в отстойник НЕ отправляем: почти все
    попадают сюда по временной причине и со второй попытки проходят.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from app.workers import inbound as worker

pytestmark = pytest.mark.anyio

ТЕЛО = {"payload": {"type": "message", "value": {"id": "am-1", "chat_id": "c-1"}}}


async def _положить_и_взять(redis: Any, *, доставок: int) -> str:
    """Запись в потоке, взятая группой ``доставок`` раз и ни разу не подтверждённая."""
    await redis.xgroup_create(worker.STREAM, worker.GROUP, id="0", mkstream=True)
    entry_id = await redis.xadd(worker.STREAM, {"body": json.dumps(ТЕЛО)})
    await redis.xreadgroup(worker.GROUP, "мёртвый", {worker.STREAM: ">"}, count=10)
    for _ in range(доставок - 1):
        # Каждый повторный захват группой увеличивает `times_delivered` — ровно то,
        # по чему `_reclaim_stuck` и отличает отравленную запись от обычной.
        await redis.xclaim(
            worker.STREAM, worker.GROUP, "мёртвый", min_idle_time=0, message_ids=[entry_id]
        )
    return str(entry_id)


@pytest.fixture
def стенд(redis: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Две поправки к стенду — и обе про сам стенд, а не про проверяемый код.

    1. В бою «зависшей» запись становится через минуту простоя; в тесте — сразу.

    2. ⚠ У `fakeredis` СЛОМАН КУРСОР `XAUTOCLAIM`, И БЕЗ ЭТОГО ТЕСТ ВИСНЕТ.
       Настоящий Redis отдаёт вторым значением идентификатор, С КОТОРОГО
       продолжать обход, и по кругу PEL возвращает `0-0`. `fakeredis` отдаёт
       идентификатор САМОЙ записи, и следующий вызов приносит её же — цикл
       `while True` в `_reclaim_stuck` крутится вечно. Проверено отдельным
       прогоном: четыре вызова подряд, каждый вернул ту же запись.
       Поэтому подменяем ровно обход: одна страница со всеми зависшими, дальше
       пусто, — как ведёт себя настоящий Redis, когда PEL меньше `count`.
       Хранилище остаётся настоящим: и `xadd` в отстойник, и `xack`, и
       `xpending_range` — это `fakeredis`, а не наши представления о нём.
    """
    monkeypatch.setattr(worker, "CLAIM_IDLE_MS", 0)

    выдано = False

    async def _xautoclaim(*_a: Any, **kwargs: Any) -> tuple[str, list[Any]]:
        nonlocal выдано
        if выдано:
            return "0-0", []
        выдано = True
        строки = await redis.xrange(worker.STREAM)
        return "0-0", строки

    monkeypatch.setattr(redis, "xautoclaim", _xautoclaim)


async def test_отравленная_запись_уезжает_в_отстойник(redis, db_sessionmaker, стенд):
    entry_id = await _положить_и_взять(redis, доставок=worker.MAX_DELIVERIES)
    await asyncio.sleep(0.05)  # запись обязана успеть «простоять»

    await worker._reclaim_stuck({"redis": redis, "db_session_factory": db_sessionmaker}, "живой")

    строки = await redis.xrange(worker.DLQ)
    assert len(строки) == 1, "обращение клиента исчезло, не доехав до отстойника"
    _, поля = строки[0]
    assert json.loads(поля["body"]) == ТЕЛО, "в отстойник уехало не то тело"
    assert поля["orig_id"] == entry_id, "без обратного адреса разбор не поймёт, что поднимает"
    assert поля["failed_at"].isdigit()

    # И подтверждена в основном потоке: иначе вернётся сюда же на следующем проходе.
    висит = await redis.xpending_range(worker.STREAM, worker.GROUP, min="-", max="+", count=10)
    assert висит == [], "запись осталась в PEL — она будет ходить по кругу вечно"


async def test_запись_с_запасом_попыток_в_отстойник_не_едет(
    redis, db_sessionmaker, стенд, monkeypatch
):
    """Почти все зависшие проходят со второй попытки — отправлять их в отстойник рано."""
    await _положить_и_взять(redis, доставок=worker.MAX_DELIVERIES - 1)
    await asyncio.sleep(0.05)

    обработано: list[str] = []

    async def _подмена(ctx: dict, entry_id: str, fields: dict) -> None:  # noqa: ANN401
        обработано.append(entry_id)

    monkeypatch.setattr(worker, "_handle_entry", _подмена)
    await worker._reclaim_stuck({"redis": redis, "db_session_factory": db_sessionmaker}, "живой")

    assert await redis.xrange(worker.DLQ) == [], "запись с запасом попыток отправлена в отстойник"
    assert обработано, "и обработать её тоже не попытались — она просто потерялась"
