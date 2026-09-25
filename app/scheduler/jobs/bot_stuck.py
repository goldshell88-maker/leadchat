"""Возврат диалогов зависшего бота (аудит 16.08, находка №1).

ЗАЧЕМ. «Бот — отдельный сотрудник»: пока он ведёт диалог (``bot_active``),
обращение убрано из очереди — операторы его не видят, автораздача не выдаёт,
сторожа молчат. Ровно поэтому мёртвый бот — худшая из аварий: упал воркер,
перезапустился процесс посреди тика, лид-бот навсегда недоступен — и клиент
остаётся в комнате, куда не заглядывает никто. Ни одна из существующих
страховок это не ловит: handoff при недоступности делает сам движок, а здесь
умер именно движок.

ПРИЗНАК ЗАВИСАНИЯ. Последнее сообщение диалога — ВХОДЯЩЕЕ, и оно лежит без
ответа дольше :data:`BOT_STUCK_MINUTES` при активном боте. Живой бот реагирует
на входящее ближайшим тиком (секунды): десять минут тишины после реплики
клиента — это не «думает», это «не работает». Обратные ситуации критерий
намеренно НЕ трогает:

* бот спросил и ждёт клиента (ask с таймаутом на часы) — последним лежит
  ИСХОДЯЩЕЕ, диалог не подпадает;
* клиент молчит после пинга — тоже исходящее, не подпадает.

Диалог возвращается В ОБЩУЮ ОЧЕРЕДЬ, а не «доигрывается»: чинить состояние
сценария за мёртвый процесс — гадание, а клиент уже прождал десять минут.
Человек увидит переписку целиком и продолжит по месту.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from apscheduler.triggers.interval import IntervalTrigger

from app.core import redis as redis_mod
from app.db import session as db_mod
from app.models import Conversation, Message
from app.services import inbox as inbox_svc
from app.services.audit import write_audit
from app.services.conversation_status import set_status
from app.ws.events import publish_event
from app.ws.hub import publish_inbox_new

log = structlog.get_logger("app.bot_stuck")

#: Сколько входящее может лежать без реакции активного бота. Живой бот
#: отвечает ближайшим тиком; десять минут — с запасом на любые очереди и
#: ретраи, но всё ещё меньше, чем клиент готов ждать молча.
BOT_STUCK_MINUTES = 10

#: Порция за проход — по тем же соображениям, что и в reclaim: если воркер
#: лежал час, возвращать сотни диалогов одним залпом кадров нельзя.
BATCH = 50


async def release_stuck_bots() -> int:
    """Точка входа планировщика: своя сессия, свой Redis (стиль reclaim)."""
    redis = redis_mod.get_client()
    async with db_mod.session_scope() as db:
        frames = await release_in_session(db, redis)
        await db.commit()

    for frame in frames:
        # Строка очереди — только тем диалогам, которые в неё встали.
        if frame["inbox"] is not None:
            await publish_inbox_new(
                redis,
                frame["inbox"]["conversation"],
                eligible_operator_ids=frame["inbox"].get("eligible"),
            )
        await publish_event(redis, "conversation:updated", frame["patch"])

    if frames:
        log.warning("bot_stuck.released", returned=len(frames))
    return len(frames)


async def release_in_session(
    db: Any, redis: Any, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Снять ``bot_active`` с зависших диалогов и вернуть их в очередь.

    Ничего не коммитит и не публикует — транзакцией и кадрами владеет
    вызывающий (08 §8.1)."""
    moment = now or datetime.now(UTC)
    cutoff = moment - timedelta(minutes=BOT_STUCK_MINUTES)

    last_in = (
        sa.select(sa.func.max(Message.created_at))
        .where(Message.conversation_id == Conversation.id, Message.direction == "in")
        .scalar_subquery()
    )
    last_out = (
        sa.select(sa.func.max(Message.created_at))
        .where(Message.conversation_id == Conversation.id, Message.direction == "out")
        .scalar_subquery()
    )

    candidates = list(
        (
            await db.execute(
                sa.select(Conversation)
                .where(
                    Conversation.bot_active.is_(True),
                    Conversation.status != "closed",
                    last_in.is_not(None),
                    last_in < cutoff,
                    sa.or_(last_out.is_(None), last_out < last_in),
                )
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    frames: list[dict[str, Any]] = []
    for conv in candidates:
        conv.bot_active = False
        # ⚠ КАДР ОБЯЗАН ОПИСЫВАТЬ ТО, ЧТО СЛУЧИЛОСЬ, А НЕ ОДНУ ИЗ ДВУХ ВЕТОК.
        #
        # Ветка ниже намеренно не трогает владельца: зависшего бота у ЖИВОГО
        # человека отбирать не за что (в отличие от `reclaim`, где диалог
        # забирают у ушедшего из сети). А кадр собирался из литералов
        # `assignee: None` и `in_inbox: True` безусловно — и у диспетчера, который
        # ведёт диалог, тот на глазах становился ничьим «Новым», а у остальных
        # во «Входящих» появлялась строка на диалог, которого в очереди нет.
        # Та же беда была в `bots/handoff.py`, починена там же 23.08.
        ушёл_в_очередь = conv.assignee_id is None and conv.claimed_by_id is None
        if ушёл_в_очередь:
            set_status(conv, "new", now=moment)
            inbox_svc.enter_queue(conv, now=moment)
        conv.updated_at = moment
        await write_audit(
            db,
            user_id=None,
            action="conversation.bot_stuck_released",
            entity="conversation",
            entity_id=str(conv.id),
        )
        патч: dict[str, Any] = {
            "bot_active": False,
            "status": conv.status,
            "in_inbox": ушёл_в_очередь,
        }
        # `assignee` кладём ТОЛЬКО когда его действительно сняли. Ключа нет —
        # фронт оставит прежнего (`mergeConversationPatch` трогает поле лишь
        # при `!== undefined`), и это ровно то, что произошло в базе.
        if ушёл_в_очередь:
            патч["assignee"] = None
        frames.append(
            {
                # ⚠ кадр СО СПИСКОМ ДОПУЩЕННЫХ (аудит 19.08): без него строка
                # очереди чужого канала уезжала всем менеджерам. Собирается ДО
                # commit'а (так требует докстринг `inbox_frame_addressed`), но
                # только когда диалог правда встал в очередь.
                "inbox": (
                    await inbox_svc.inbox_frame_addressed(db, conv, now=moment)
                    if ушёл_в_очередь
                    else None
                ),
                # конверт по 01 §11.3 — как в reclaim.py; плоский кадр фронт
                # молча выбрасывал (аудит синхронизации 16.08)
                "patch": {"conversation_id": str(conv.id), "patch": патч},
            }
        )
    return frames


def register(scheduler: Any) -> None:
    """Раз в две минуты: дешёвый запрос, а цена пропуска — невидимый клиент."""
    scheduler.add_job(
        release_stuck_bots,
        IntervalTrigger(minutes=2),
        id="bot_stuck",
        max_instances=1,
        coalesce=True,
    )
