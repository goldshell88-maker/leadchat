"""Подбор потерянных дедлайнов бота (проверка 24.09).

ЗАЧЕМ. Дедлайн шага `ask`/`menu` живёт задачей ARQ `bot_ask_timeout`, и эта
задача теряется: три заминки базы подряд (`runtime.ЛИМИТ_ПОВТОРОВ_ДЕДЛАЙНА`),
отказ очереди при постановке (`flush_outbox` пишет его только в лог), Redis,
поднятый без данных. Диалог остаётся `bot_active` — скрытым из «Входящих», —
а последнее слово за ботом: сторож зависших (`bot_stuck`) ловит «клиент
написал, бот молчит» и сюда не смотрит. Клиент, не ответивший на вопрос бота,
не возвращался в очередь никогда.

ЧТО ДЕЛАЕТ. Диалог с `bot_active`, чей дедлайн истёк дольше :data:`GRACE`
назад, получает задачу дедлайна заново — с тем же токеном. Решает задача, как
в первый раз: клиент ответил — «answered», ожидание сменилось — «stale», иначе
ветка `on_timeout` сценария (дожим, передача). Повтор безопасен: задача
самоаннулируется по токену.

Дедлайн, истёкший дольше :data:`GIVE_UP` назад, значит, что и повторы не
сдвинули диалог: сломан сам путь сценария. Тогда диалог уходит людям обычной
передачей «клиент не ответил в отведённое время» — с заметкой и кадрами.

БОТ ВЕДЁТ, НО НИЧЕГО НЕ ЖДЁТ. С 24.09 тик — две транзакции, между ними модель
думает без блокировки строки. Упади процесс между ними (выкатка, сбой базы на
второй транзакции) — диалог остаётся с `bot_active` без ожидания и без срока,
последняя реплика за ботом: ни этот сторож по сроку, ни сторож зависших его не
видят. Такое состояние у живого автоответа длится секунды; простоявшее дольше
:data:`STALL` — сбой, и диалог уходит людям с причиной `bot_stalled`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import handoff
from app.bots.runtime import BOT_TIMEOUT_JOB, flush_outbox
from app.bots.state import BotState, Outbox, parse_iso
from app.core import redis as redis_mod
from app.db import session as db_mod
from app.models import Conversation

log = structlog.get_logger("app.bot_deadlines")

#: Срок без задачи. Повторы задачи укладываются в секунды, окно серии — в
#: полминуты: через десять минут после срока задачи уже нет.
GRACE = timedelta(minutes=10)
#: Повторы не сдвинули диалог за час — клиента отдаём людям.
GIVE_UP = timedelta(hours=1)
#: Бот ведёт диалог без ожидания дольше этого — тик оборвался посередине.
STALL = timedelta(minutes=10)
INTERVAL_MINUTES = 5
#: Порция за проход. Первыми — давно молчащие диалоги: у них и сроки старше.
BATCH = 200
JOB_ID = "bot_deadlines"


async def pick_up_lost_deadlines() -> int:
    """Точка входа планировщика: своя сессия, свой Redis (стиль `bot_stuck`)."""
    redis = redis_mod.get_client()
    async with db_mod.session_scope() as db:
        outbox, picked = await sweep_in_session(db)
        await db.commit()
    await flush_outbox({"redis": redis}, outbox)
    return picked


async def sweep_in_session(db: AsyncSession, *, now: datetime | None = None) -> tuple[Outbox, int]:
    """Поставить потерянные дедлайны заново, безнадёжные — отдать людям.

    Ничего не коммитит и не публикует — транзакцией и кадрами владеет
    вызывающий (08 §8.1). Возвращает эффекты и число подобранных диалогов."""
    moment = now or datetime.now(UTC)
    outbox = Outbox()
    rows = (
        (
            await db.execute(
                sa.select(Conversation)
                .where(Conversation.bot_active.is_(True), Conversation.status != "closed")
                .order_by(Conversation.last_message_at.asc().nullsfirst())
                .limit(BATCH)
                # Диалог под тиком пропускаем: живой тик и есть ответ на вопрос.
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    requeued = released = 0
    for conv in rows:
        state = BotState.from_conv(conv)
        waiting = state.waiting
        if waiting is None:
            last_step = parse_iso(state.last_step_at)
            if not state.handoff_done() and last_step is not None and moment - last_step >= STALL:
                await handoff.do_handoff(
                    db, conv, state, outbox, reason="bot_stalled", bot_id=state.bot_id, now=moment
                )
                conv.bot_vars = state.dump()
                released += 1
            continue
        deadline = parse_iso(waiting.deadline)
        if deadline is None or moment - deadline < GRACE:
            continue
        if moment - deadline < GIVE_UP:
            outbox.job(BOT_TIMEOUT_JOB, conv.id, waiting.token)
            requeued += 1
            continue
        await handoff.do_handoff(
            db, conv, state, outbox, reason="ask_timeout", bot_id=state.bot_id, now=moment
        )
        conv.bot_vars = state.dump()
        released += 1
    if requeued or released:
        log.warning("bot.deadlines_picked_up", requeued=requeued, released=released)
    return outbox, requeued + released


def register(scheduler: Any) -> None:
    """Раз в пять минут: запрос дешёвый, а цена пропуска — невидимый клиент."""
    scheduler.add_job(
        pick_up_lost_deadlines,
        IntervalTrigger(minutes=INTERVAL_MINUTES),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
