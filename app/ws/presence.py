"""Presence in Redis (08 §5.5; contract 01 §11.6 / §11.3).

Sockets of one user may live in different processes — the connection
registry is a Redis zset, the status key ``presence:{user_id}`` has a TTL
prolonged on every application-level ping.
"""

import asyncio
import time
import uuid
from collections.abc import Sequence

from redis.asyncio import Redis

from app.core.config import settings
from app.ws.events import publish_event


def _conns_key(user_id: uuid.UUID | str) -> str:
    return f"presence:conns:{user_id}"


def _status_key(user_id: uuid.UUID | str) -> str:
    return f"presence:{user_id}"


def _active_key(user_id: uuid.UUID | str) -> str:
    return f"presence:last_active:{user_id}"


#: Сколько человек считается активным после последнего «на месте» с любого
#: устройства. Ровно порог авто-«отошёл» на фронте (`автоОтошёл.ts`, 30 минут):
#: два порога на одно понятие разошлись бы при первой правке.
ACTIVE_WINDOW_SECONDS = 30 * 60


#: Значения ключа `presence:{user_id}`. Отсутствие ключа = «не в сети»:
#: третьего значения не заводим, потому что «нет ключа» и так означает ровно
#: это, а два способа сказать одно и то же расходятся при первой же правке.
ONLINE = "online"
AWAY = "away"
PRESENCE_STATUSES: tuple[str, ...] = (ONLINE, AWAY)


async def presence_map[K: (uuid.UUID | str)](redis: Redis, user_ids: Sequence[K]) -> dict[K, bool]:
    """Кто ПОДКЛЮЧЁН — без разбора «на месте» и «отошёл» (01 §3.1/§11.6).

    Отвечает на вопрос «приложение открыто и отвечает на пинги». Именно это
    нужно сторожам возврата: отошедший на обед сидит за столом, и отбирать у
    него диалоги не за что.

    Кому нужно РАЗЛИЧАТЬ статусы — берёт `presence_status_map` ниже. Развести
    их пришлось потому, что здесь стояло `bool(value)`, и значение ключа
    выбрасывалось: положи в него «away», ни один из семи потребителей не
    заметил бы разницы.
    """
    ids = list(user_ids)
    if not ids:
        return {}
    values = await redis.mget([_status_key(uid) for uid in ids])
    return {uid: bool(value) for uid, value in zip(ids, values, strict=True)}


async def presence_status_map[K: (uuid.UUID | str)](
    redis: Redis, user_ids: Sequence[K]
) -> dict[K, str | None]:
    """Кто в каком состоянии: ``"online"`` | ``"away"`` | ``None`` (не в сети)."""
    ids = list(user_ids)
    if not ids:
        return {}
    values = await redis.mget([_status_key(uid) for uid in ids])
    return {uid: (value or None) for uid, value in zip(ids, values, strict=True)}


async def mark_active(redis: Redis, user_id: uuid.UUID | str) -> None:
    """Человека только что видели активным (на месте) — на ЛЮБОМ устройстве."""
    await redis.set(_active_key(user_id), str(time.time()), ex=ACTIVE_WINDOW_SECONDS * 2)


async def recently_active(redis: Redis, user_id: uuid.UUID | str) -> bool:
    """Активность человека моложе порога авто-«отошёл» — с любого устройства."""
    raw = await redis.get(_active_key(user_id))
    try:
        seen = float(raw) if raw else 0.0
    except ValueError:
        return False
    return time.time() - seen < ACTIVE_WINDOW_SECONDS


async def set_presence(redis: Redis, user_id: uuid.UUID | str, status: str) -> None:
    """Человек сам выбрал состояние (PUT /presence, 01 §11.6).

    Пишется в тот же ключ с тем же TTL: отдельного хранилища «отошёл» нет
    намеренно. Статус имеет смысл только пока приложение открыто — у
    закрывшего ноутбук нет ни «на месте», ни «отошёл», он просто не в сети, и
    ключ исчезает сам.
    """
    if status not in PRESENCE_STATUSES:
        raise ValueError(f"неизвестный статус присутствия: {status!r}")
    # ⚠ СОБЫТИЕ — ТОЛЬКО НА СМЕНУ ЗНАЧЕНИЯ (01.09).
    #
    # Здесь публиковалось на КАЖДЫЙ PUT. Пока состояние меняли руками, это было
    # безобидно: два-три раза за смену. Но с 01.09 авто-«отошёл» ПОДТВЕРЖДАЕТ
    # статус раз в две минуты — иначе его затирает `presence_connected` после
    # любого обрыва. Тринадцать отсутствующих за ночь дали бы тысячи кадров, а
    # лента событий держит 300 строк без склейки повторов: к утру в ней не
    # осталось бы ничего, кроме «Пётр Ковалёв — отошёл».
    #
    # TTL продлеваем ВСЕГДА (в этом весь смысл подтверждения), публикуем — нет.
    было = await redis.get(_status_key(user_id))
    await redis.set(_status_key(user_id), status, ex=settings.ws_presence_ttl_seconds)
    if было != status:
        await publish_event(redis, "presence:online", {"user_id": str(user_id), "status": status})


async def presence_connected(redis: Redis, user_id: uuid.UUID | str, conn_id: str) -> None:
    first = await redis.zcard(_conns_key(user_id)) == 0
    await redis.zadd(_conns_key(user_id), {conn_id: time.time()})
    # ВЫБРАННЫЙ ЧЕЛОВЕКОМ СТАТУС НЕ ЗАТИРАЕТСЯ ПОДКЛЮЧЕНИЕМ.
    #
    # Здесь безусловно писалось "online", и строка стояла ВНЕ `if first` — то
    # есть при каждом новом сокете. А сокет переподключается постоянно:
    # моргнула сеть, сервер разорвал соединение по таймауту кадров, человек
    # открыл вторую вкладку. «Отошёл», поставленный руками, слетал бы через
    # минуту — и человек молча возвращался бы в автораздачу, не зная об этом.
    #
    # Статус, который сам себя сбрасывает, хуже отсутствующего: на него
    # рассчитывают, а он не работает.
    current = await redis.get(_status_key(user_id))
    status = current if current in PRESENCE_STATUSES else ONLINE
    await redis.set(_status_key(user_id), status, ex=settings.ws_presence_ttl_seconds)
    if first:
        await publish_event(redis, "presence:online", {"user_id": str(user_id), "status": status})


async def presence_heartbeat(redis: Redis, user_id: uuid.UUID | str, conn_id: str) -> None:
    """Called on every application-level ping (01 §11.5 — every 25 s)."""
    await redis.zadd(_conns_key(user_id), {conn_id: time.time()})
    status = await redis.get(_status_key(user_id)) or "online"
    await redis.set(_status_key(user_id), status, ex=settings.ws_presence_ttl_seconds)


async def presence_disconnected(redis: Redis, user_id: uuid.UUID | str, conn_id: str) -> None:
    await redis.zrem(_conns_key(user_id), conn_id)
    await asyncio.sleep(settings.ws_presence_offline_grace_seconds)  # reconnect grace (01 §11.3)
    # sweep connections that stopped pinging (dead processes)
    await redis.zremrangebyscore(
        _conns_key(user_id), "-inf", time.time() - settings.ws_presence_ttl_seconds
    )
    if await redis.zcard(_conns_key(user_id)) == 0:  # the last socket is gone
        await redis.delete(_status_key(user_id))
        await publish_event(
            redis, "presence:online", {"user_id": str(user_id), "status": "offline"}
        )
