"""Журнал работы лид-бота: по строке на каждое обращение.

ЗАЧЕМ ОН ЕСТЬ. Владелец: «сделай подробное логирование, чтобы было всё видно».
Видно до сих пор было не всё и не всем. Лид-бот отдаёт в `meta` слой, который
ответил (роутер регламента или модель), эскалацию с причиной и сроком реакции,
готовность заявки, предупреждения и время. LeadChat часть этого писал в поток
логов, остальное терял. Поток логов лежит на боевом сервере и читается через
ssh — то есть доступен ровно одному человеку в проекте, и то не с телефона.

ЧТО ЗДЕСЬ НЕ ХРАНИТСЯ. Переписка. Вопрос и ответ ложатся ОБРЕЗАННЫМИ до
`TEXT_LIMIT`: журнал отвечает на «почему бот ответил так», а полная переписка
уже лежит в `messages`. Вторая её копия — это второе место, откуда переписка
клиентов способна утечь, и второе место, которое надо чистить по сроку.

ПОЧЕМУ ЗАПИСЬ НЕ ИМЕЕТ ПРАВА УРОНИТЬ ТИК. Решение владельца №4: недоступность
ИИ никогда не блокирует доставку сообщений. Журнал — побочная польза; если он
не записался, клиент всё равно обязан получить ответ. Поэтому здесь всё
завёрнуто и наружу не летит ничего.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.leadbot import LeadbotReply
from app.models.leadbot import TEXT_LIMIT, LeadbotCall

log = structlog.get_logger("app.leadbot_log")


def _cut(text: Any) -> str | None:
    """Строка не длиннее `TEXT_LIMIT`, с многоточием на месте отрезанного."""
    if not isinstance(text, str):
        return None
    clean = text.strip()
    if not clean:
        return None
    return clean if len(clean) <= TEXT_LIMIT else clean[: TEXT_LIMIT - 1] + "…"


def _confidence(value: Any) -> Decimal | None:
    """Уверенность как ровно два знака — столько же, сколько в колонке."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    # Прижимаем к 0..1 тем же правилом, что и движок: значения вне диапазона
    # приходили от чужого сервиса и раньше, и падать из-за них журнал не должен.
    number = max(0.0, min(1.0, number))
    return Decimal(f"{number:.2f}")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def build(
    reply: LeadbotReply,
    *,
    outcome: str,
    conversation_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
    question: str | None,
    now: datetime | None = None,
) -> LeadbotCall:
    """Собрать запись журнала из ответа лид-бота и решения движка.

    РАЗДЕЛЕНИЕ РОЛЕЙ, РАДИ КОТОРОГО ФУНКЦИЯ ПРИНИМАЕТ ДВОИХ. `reply` знает, что
    ответил лид-бот; `outcome` знает, что с этим сделал движок. Одно без другого
    бесполезно: «двухсотый ответ» и «клиент его получил» — разные события, и
    расходятся они ровно в интересных случаях (уверенность ниже порога — ответ
    был, клиент не получил ничего).
    """
    meta = reply.meta
    escalation = meta.get("escalation")
    escalation = escalation if isinstance(escalation, dict) else {}
    warnings = meta.get("warnings")
    answer = reply.answer or {}

    return LeadbotCall(
        id=uuid.uuid4(),
        created_at=now or datetime.now(UTC),
        conversation_id=conversation_id,
        account_id=account_id,
        request_id=reply.request_id or None,
        question=_cut(question),
        reply=_cut(answer.get("reply")),
        layer=_cut(meta.get("layer")),
        flag=_cut(meta.get("flag")),
        confidence=_confidence(answer.get("confidence")),
        needs_operator=(bool(answer.get("needs_operator")) if "needs_operator" in answer else None),
        outcome=outcome,
        # Причина и подпись — РАЗНЫЕ поля у лид-бота: `reason` машинный
        # («visit_failed»), `label` человеческий («срыв визита»). Экран
        # показывает подпись, отбор идёт по причине; свести их в одно значило бы
        # либо показывать машинное, либо отбирать по переводу.
        escalation_reason=_cut(escalation.get("reason")),
        escalation_label=_cut(escalation.get("label")),
        escalation_deadline_min=_int_or_none(escalation.get("deadline_min")),
        lead_ready=bool(meta.get("lead_ready")),
        # ⚠ ТОЛЬКО ИМЕНА ПОЛЕЙ ЗАЯВКИ, НИКОГДА САМА ЗАЯВКА. В `meta.lead` лежит
        # телефон клиента, а журнал читают шире, чем переписку (docs/42 §6.2).
        warnings=[str(w) for w in warnings] if isinstance(warnings, list) else [],
        ms=reply.ms,
        error=reply.error or None,
    )


async def record(
    db: AsyncSession,
    reply: LeadbotReply,
    *,
    outcome: str,
    conversation_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
    question: str | None,
    now: datetime | None = None,
) -> None:
    """Записать обращение. Не бросает НИЧЕГО — см. шапку файла."""
    try:
        db.add(
            build(
                reply,
                outcome=outcome,
                conversation_id=conversation_id,
                account_id=account_id,
                question=question,
                now=now,
            )
        )
        await db.flush()
    except Exception as exc:  # noqa: BLE001 — журнал не имеет права уронить тик
        # `flush` мог оставить сессию в нерабочем состоянии, а тик обязан
        # доехать до конца: клиент ждёт ответа, а не нашей бухгалтерии.
        log.warning("leadbot.journal_failed", error=type(exc).__name__, outcome=outcome)


async def recent(
    db: AsyncSession,
    *,
    limit: int = 100,
    account_id: uuid.UUID | None = None,
    only_trouble: bool = False,
) -> list[LeadbotCall]:
    """Последние обращения, свежие сверху.

    `only_trouble` — «покажи, где что-то пошло не так»: недоступность и
    выброшенный за уверенность ответ. Это самый частый вопрос к журналу, и под
    него в базе стоит частичный индекс (миграция 0038).
    """
    from app.models.leadbot import OUTCOME_LOW_CONFIDENCE, OUTCOME_UNAVAILABLE

    query = select(LeadbotCall).order_by(LeadbotCall.created_at.desc())
    if account_id is not None:
        query = query.where(LeadbotCall.account_id == account_id)
    if only_trouble:
        query = query.where(LeadbotCall.outcome.in_((OUTCOME_UNAVAILABLE, OUTCOME_LOW_CONFIDENCE)))
    return list((await db.execute(query.limit(max(1, min(limit, 500))))).scalars().all())
