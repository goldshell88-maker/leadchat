"""Кадры WS о карточке клиента — общие для ручек и воркера.

⚠ ВСЕ КАДРЫ — СТРОГО ПОСЛЕ COMMIT'А (08 §8.1). Кадр до commit'а заставляет
экран перечитать старую строку, и правка «появляется только после F5».

⚠ ПЕРСОНАЛЬНЫХ ДАННЫХ В КАДРАХ НЕТ. `client:updated` говорит «перезапросите
личность этого клиента»; `conversation:updated` несёт заплатку строки списка —
имя и телефон там и так стоят у каждой строки. Сам номер вторым каналом не
рассылаем: у карточки есть своя ручка с правами.

⚠ КАДР НЕ ИМЕЕТ ПРАВА УРОНИТЬ УЖЕ СДЕЛАННУЮ ПРАВКУ: ловим широко, но не молча.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Conversation
from app.ws.events import publish_event

log = structlog.get_logger("app.clients_events")

#: Диалогов, по которым уходит заплатка строки списка: у постоянного клиента
#: их бывают сотни, а на экране видны свежие.
PATCH_LIMIT = 100


async def publish_client_updated(
    redis: Redis, client_id: uuid.UUID, conversation_id: uuid.UUID | None, *, reason: str
) -> None:
    """`client:updated` — «перезапросите личность этого клиента»."""
    try:
        await publish_event(
            redis,
            "client:updated",
            {
                "client_id": str(client_id),
                "conversation_id": str(conversation_id) if conversation_id else None,
                "reason": reason,
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "client.updated_not_published",
            client_id=str(client_id),
            reason=reason,
            error=str(exc),
        )


async def publish_client_patch(
    db: AsyncSession,
    redis: Redis,
    client_id: uuid.UUID,
    patch: dict[str, Any],
    *,
    skip: frozenset[uuid.UUID] = frozenset(),
) -> None:
    """`conversation:updated {client: …}` по свежим диалогам клиента.

    Имя и пометка «нежелательный» стоят в каждой строке списка у всех
    операторов — без кадра правка была видна только автору до перезагрузки
    (аудит синхронизации 16.08; образец кадра — client_enrich).
    """
    ids = (
        (
            await db.execute(
                sa.select(Conversation.id)
                .where(Conversation.client_id == client_id)
                .order_by(Conversation.last_message_at.desc().nullslast())
                .limit(PATCH_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    await publish_conversation_patches(redis, [i for i in ids if i not in skip], patch)


async def publish_conversation_patches(
    redis: Redis, conversation_ids: list[uuid.UUID], patch: dict[str, Any]
) -> None:
    for cid in conversation_ids[:PATCH_LIMIT]:
        try:
            await publish_event(
                redis,
                "conversation:updated",
                {"conversation_id": str(cid), "patch": {"client": patch}},
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "conversation.patch_not_published", conversation_id=str(cid), error=str(exc)
            )


async def publish_merge(
    redis: Redis,
    *,
    winner: dict[str, Any],
    loser_id: uuid.UUID,
    moved: list[uuid.UUID],
    reason: str,
) -> None:
    """Кадры после объединения / разъединения: обе карточки и переехавшие диалоги.

    `winner` — `short_view` победителя: заплатка строки списка несёт
    идентификатор карточки, имя и телефон — ровно то, что строка показывает, и
    то, по чему открытый диалог понимает, что переехал (тост на экране).
    """
    первый = moved[0] if moved else None
    await publish_client_updated(redis, uuid.UUID(winner["id"]), первый, reason=reason)
    await publish_client_updated(redis, loser_id, первый, reason=reason)
    await publish_conversation_patches(
        redis,
        moved,
        {"id": winner["id"], "name": winner.get("name"), "phone": winner.get("phone")},
    )


async def publish_left_queue(redis: Redis, conv: Conversation) -> None:
    """Строка ушла из «Входящих» у всех — без принявшего (`claimed_by: null`).

    Тот же кадр шлёт `inbound`, когда коллега ответил из приложения Авито.
    Сбой публикации правку не отменяет: коллеги увидят её при сверке.
    """
    try:
        await publish_event(
            redis,
            "inbox:claimed",
            {
                "conversation_id": str(conv.id),
                "claimed_by": None,
                "claimed_at": None,
                "waited_seconds": None,
                "conversation_patch": {"in_inbox": False, "status": conv.status},
            },
        )
    except RedisError as exc:
        log.warning(
            "clients.left_queue_not_published", conversation_id=str(conv.id), error=str(exc)
        )
