"""The single point of publication into Pub/Sub 'events' (08 §5.3).

Called strictly AFTER the commit of the transaction that produced the
event (08 §8.1). ``meta`` is an internal routing envelope — the Hub strips
it before sending frames to clients (01 §11.2 clients only ever see
``{type, ts, data}``).
"""

import json
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

EVENTS_CHANNEL = "events"


def utcnow_iso() -> str:
    """ISO 8601 UTC with the Z suffix (01 §1.5)."""
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


async def publish_event(
    redis: Redis,
    type_: str,
    data: dict[str, Any],
    *,
    audience: str | None = None,
    exclude_user: str | None = None,
    only_user: str | None = None,
) -> None:
    """``only_user`` — кадр уезжает ТОЛЬКО сессиям этого пользователя.

    Нужен персональным состояниям (read-маркеры 01 §5.1/§5.3): «прочитано» —
    состояние человека, поэтому синхронизировать надо его вкладки, а не гасить
    бейдж всей команде.
    """
    evt: dict[str, Any] = {"type": type_, "ts": utcnow_iso(), "data": data}
    if audience or exclude_user or only_user:
        evt["meta"] = {
            "audience": audience,
            "exclude_user": exclude_user,
            "only_user": only_user,
        }
    await redis.publish(EVENTS_CHANNEL, json.dumps(evt, ensure_ascii=False))
