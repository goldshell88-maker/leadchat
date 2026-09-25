"""Раздел «Лид-бот»: связь, настройки, проверка, тестовый разговор, журнал.

ПРАВО ТО ЖЕ, ЧТО У БОТОВ (`bots:manage`, только администратор). Здесь меняется
адрес чужого сервиса, токен к нему и — главное — включается автоответ живым
клиентам девяти аккаунтов. Дать это руководителю значило бы дать право пустить
чужой регламент в переписку, не отвечая за неё.

⚠ ТОКЕН НЕ ВОЗВРАЩАЕТСЯ НИКОГДА. Наружу уходит только «задан или нет»
(`leadbot.safe_dump`). Он открывает чужому сервису всю переписку клиентов, и
отдать его в браузер — значит положить его в историю запросов, в кэш вкладки и
в чужие снимки экрана.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.bots import leadbot
from app.bots.runtime import (
    ENTRY_BLOCKS,
    bot_entry_block,
    flush_outbox,
    get_bot_for_conversation,
    release_bot_dialogs,
)
from app.core.errors import ApiError
from app.models import AvitoAccount, Conversation
from app.models.leadbot import OUTCOME_LABELS
from app.models.user import User
from app.services import leadbot_admin, leadbot_log
from app.services.audit import write_audit

router = APIRouter()

manage = require_permission("bots:manage")


# ------------------------------------------------------------------ настройки


class SettingsPatch(BaseModel):
    """Тело `PATCH /leadbot`. `None` в поле означает «не трогали»."""

    enabled: bool | None = None
    mode: str | None = None
    context_messages: int | None = None
    account_ids: list[uuid.UUID] | None = None


class ConnectionPut(BaseModel):
    """Адрес и токен той стороны.

    `token=None` — «оставить прежний»: экран не знает нынешнего значения и не
    имеет права его затереть, просто сохранив форму с пустым полем. Пустая
    строка, наоборот, означает осознанное «убрать токен».
    """

    url: str = Field(default="", max_length=500)
    token: str | None = Field(default=None, max_length=500)


@router.get("/leadbot")
async def get_leadbot(
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Всё состояние раздела одним ответом."""
    return await leadbot_admin.overview(db)


@router.patch("/leadbot")
async def patch_leadbot(
    body: SettingsPatch,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Режим, глубина контекста, каналы, включение."""
    bot = await leadbot_admin.ensure_system_bot(db)
    before = {a.id for a in await leadbot_admin.bound_accounts(db, bot)}
    # Автоответ → подсказка: диалоги, которые вёл лид-бот, уходят людям до смены
    # режима (как в `replace_bot` ботов, проверка 24.09).
    to_suggest = await release_bot_dialogs(
        db,
        account_ids=before if bot.mode == "auto" and body.mode == "suggest" else set(),
        reason="bot_to_suggest",
    )
    try:
        await leadbot_admin.apply(
            db,
            enabled=body.enabled,
            mode=body.mode,
            context_messages=body.context_messages,
            account_ids=body.account_ids,
        )
    except ValueError as exc:
        # Откат ОБЯЗАТЕЛЕН до ответа: часть каналов могла успеть переписаться
        # до того, как встретился занятый. Отказ обязан быть полным — иначе
        # человек увидит ошибку, а половина изменений останется.
        await db.rollback()
        reason = str(exc)
        if reason.startswith("busy:"):
            # Человеческими словами и с причиной: голый «409» на экране
            # настроек читается как поломка, а это не поломка — это занятый
            # канал, и решение принимает человек. `ApiError`, а не
            # `HTTPException`: последний наш обработчик приводит к общему
            # тексту по статусу, и объяснение терялось целиком.
            raise ApiError(
                "account_busy",
                "Канал уже обслуживает другой бот — отключите его там",
                status=409,
            ) from exc
        if reason == "context_messages":
            # Называем границы: «Недопустимое значение» на числовом поле не
            # говорит человеку, что именно поправить, и он пробует наугад.
            raise ApiError(
                "bad_value",
                f"Сколько реплик отправлять: от {leadbot_admin.MIN_CONTEXT_MESSAGES} "
                f"до {leadbot_admin.MAX_CONTEXT_MESSAGES}",
                status=422,
            ) from exc
        raise ApiError("bad_value", "Недопустимое значение", status=422) from exc
    # Включение и режим — самые дорогие решения на этом экране: одно пускает
    # чужой регламент к живым клиентам, второе решает, увидит ли клиент текст
    # вообще. Оба обязаны иметь автора и время.
    await write_audit(
        db,
        user_id=user.id,
        action="leadbot.updated",
        details={
            k: v
            for k, v in {
                "enabled": body.enabled,
                "mode": body.mode,
                "context_messages": body.context_messages,
                "accounts": len(body.account_ids) if body.account_ids is not None else None,
            }.items()
            if v is not None
        },
    )
    # Выключили лид-бота или сняли каналы — диалоги, которые он вёл, уходят
    # людям сразу (проверка 24.09), а не висят скрытыми из очереди.
    after = {a.id for a in await leadbot_admin.bound_accounts(db, bot)}
    released = (before - after) | (after if body.enabled is False else set())
    outbox = await release_bot_dialogs(db, account_ids=released)
    await db.commit()
    await flush_outbox({"redis": redis}, to_suggest)
    await flush_outbox({"redis": redis}, outbox)
    return await leadbot_admin.overview(db)


@router.put("/leadbot/connection")
async def put_connection(
    body: ConnectionPut,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Сохранить адрес и токен.

    Владелец: «сделай, чтобы api внутри бота можно было менять». До сих пор
    адрес и токен задавались только переменными окружения — то есть менялись
    выкаткой, инженером и с перезапуском.
    """
    try:
        await leadbot.save(db, actor_id=user.id, url=body.url, token=body.token)
    except ValueError as exc:
        # ⚠ БЕЗ ЭТОГО ПЕРЕХВАТА ОПЕЧАТКА В ТОКЕНЕ = «ВНУТРЕННЯЯ ОШИБКА».
        # `save` проверяет две бытовые вещи: адрес без схемы и токен с
        # кириллицей (русская «с» вместо латинской при наборе руками —
        # заголовки HTTP это latin-1, и такой токен роняет запрос ещё до сети).
        # Обе — ошибки человека в форме, и обе до сегодняшнего дня выглядели
        # как поломка системы: 500 и «Внутренняя ошибка» вместо строчки о том,
        # что именно не так. Текст берём из исключения: он уже написан
        # по-русски и по делу.
        await db.rollback()
        raise ApiError("bad_value", str(exc), status=422) from exc
    await write_audit(
        db,
        user_id=user.id,
        action="leadbot.connection_updated",
        # ⚠ ЗНАЧЕНИЯ ТОКЕНА В ЖУРНАЛЕ НЕТ. Только факт: меняли или нет.
        details={"url": body.url.strip(), "token_changed": body.token is not None},
    )
    await db.commit()
    return await leadbot_admin.overview(db)


@router.post("/leadbot/connection/reset")
async def reset_connection(
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Вернуться к тому, что задано в окружении при развёртывании."""
    await leadbot.reset(db)
    await write_audit(db, user_id=user.id, action="leadbot.connection_reset", details={})
    await db.commit()
    return await leadbot_admin.overview(db)


# ------------------------------------------------------------------ проверка


@router.post("/leadbot/probe")
async def probe(
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Проверить связь. Никого не задевает: диалога нет, доставки нет."""
    return await leadbot_admin.probe(db)


class TestLine(BaseModel):
    role: str = Field(default="user")
    content: str = Field(default="", max_length=2000)


class TestChatBody(BaseModel):
    dialog: list[TestLine] = Field(default_factory=list, max_length=40)
    item_title: str | None = Field(default=None, max_length=300)


@router.post("/leadbot/test")
async def test_chat(
    body: TestChatBody,
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Тестовый разговор с лид-ботом.

    ⚠ НИ ОДНО СООБЩЕНИЕ ОТСЮДА НЕ УХОДИТ КЛИЕНТУ. Здесь нет диалога, нет
    аккаунта и нет доставки — только вызов чужой ручки и показ того, что она
    вернула. Ради этого свойства тест и заводится: посмотреть, что бот
    отвечает, ДО того как пустить его к живым людям.

    В журнал работы это не пишется: журнал про живых клиентов, и подмешивать в
    него проверки значило бы испортить единственный источник ответа на вопрос
    «как бот вёл себя в бою».
    """
    return await leadbot_admin.test_chat(
        db,
        dialog=[{"role": line.role, "content": line.content} for line in body.dialog],
        item_title=body.item_title,
    )


# -------------------------------------------------------------------- журнал


@router.get("/leadbot/calls")
async def calls(
    limit: int = Query(default=100, ge=1, le=500),
    account_id: uuid.UUID | None = Query(default=None),
    only_trouble: bool = Query(default=False),
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Журнал обращений — что спрашивали, что ответил, чем кончилось."""
    rows = await leadbot_log.recent(
        db, limit=limit, account_id=account_id, only_trouble=only_trouble
    )
    return {
        "items": [
            {
                "id": str(row.id),
                "at": row.created_at.isoformat(),
                "conversation_id": str(row.conversation_id) if row.conversation_id else None,
                "account_id": str(row.account_id) if row.account_id else None,
                "request_id": row.request_id,
                "question": row.question,
                "reply": row.reply,
                "layer": row.layer,
                "flag": row.flag,
                "confidence": float(row.confidence) if row.confidence is not None else None,
                "needs_operator": row.needs_operator,
                "outcome": row.outcome,
                # Подпись едет с сервера: один словарь на сервер и на экран,
                # иначе перевод исходов разойдётся между ними.
                "outcome_label": OUTCOME_LABELS.get(row.outcome, row.outcome),
                "escalation": (
                    {
                        "reason": row.escalation_reason,
                        "label": row.escalation_label,
                        "deadline_min": row.escalation_deadline_min,
                    }
                    if row.escalation_reason or row.escalation_label
                    else None
                ),
                "lead_ready": row.lead_ready,
                "warnings": row.warnings or [],
                "ms": row.ms,
                "error": row.error,
            }
            for row in rows
        ]
    }


# Почему бот не вошёл в диалог — человеческим языком (`runtime.ENTRY_BLOCKS`).
#
# ⚠ ЖАЛОБА ВЛАДЕЛЬЦА 19.08: «включил бота, он ничего не взял; включил
# подсказки — тоже ничем не помог». Бот вёл себя ПРАВИЛЬНО: он входит только
# в свежий диалог и только туда, где человек ещё не отвечал. За те часы новых
# диалогов не было — все обращения приходили в существующие, которые уже вёл
# человек. Но узнать это было неоткуда: журнал обращений пуст, и пустота
# читается как поломка. Своей копии словаря здесь нет: копия отстала от
# причин тика, и «claimed_by_human» уходил на экран машинным кодом.
@router.get("/leadbot/silence")
async def silence(
    limit: int = Query(default=20, ge=1, le=100),
    user: User = Depends(manage),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Почему бот молчит: что он ведёт сейчас и что не взял — с причиной.

    Отвечает на единственный вопрос владельца, на который в системе не отвечало
    ничто: «я его включил, а он ничего не взял — почему?». Причина считается
    ТЕМ ЖЕ кодом, который решает пускать бота (`bot_entry_block`), а не
    отдельной догадкой: разъехаться им негде.
    """
    каналы = (
        (await db.execute(sa.select(AvitoAccount).where(AvitoAccount.is_service.is_(False))))
        .scalars()
        .all()
    )
    ведёт = (
        (
            await db.execute(
                sa.select(Conversation)
                .where(Conversation.bot_active.is_(True))
                .order_by(Conversation.last_message_at.desc().nullslast())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    последние = (
        (
            await db.execute(
                sa.select(Conversation)
                .where(Conversation.bot_active.is_(False))
                .order_by(Conversation.last_message_at.desc().nullslast())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    не_взял: list[dict[str, Any]] = []
    for conv in последние:
        бот = await get_bot_for_conversation(db, conv)
        причина = await bot_entry_block(db, conv, bot=бот)
        не_взял.append(
            {
                "conversation_id": str(conv.id),
                "status": conv.status,
                "last_message_at": (
                    conv.last_message_at.isoformat() if conv.last_message_at else None
                ),
                "reason": причина,
                "reason_label": ENTRY_BLOCKS.get(причина or "", причина or "бот может войти"),
            }
        )

    return {
        "channels": [
            {
                "id": str(a.id),
                "title": a.title,
                "bot_attached": a.bot_id is not None,
                "status": a.status,
            }
            for a in каналы
        ],
        "in_progress": [
            {
                "conversation_id": str(c.id),
                "status": c.status,
                "last_message_at": c.last_message_at.isoformat() if c.last_message_at else None,
            }
            for c in ведёт
        ],
        "not_taken": не_взял,
    }
