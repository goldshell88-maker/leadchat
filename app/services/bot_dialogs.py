"""Экран «Диалоги бота» (docs/45) — где бот сработал плохо.

ЧТО ЭТО. Третий взгляд на те же диалоги: не лента для работы и не таблица для
разбора, а надзор за ботом. Отвечает на один вопрос — где бот навредил, — и
только на него.

ПОЧЕМУ ОТДЕЛЬНЫЙ МОДУЛЬ, А НЕ КОЛОНКА В `conversation_table`
------------------------------------------------------------
Колонка «чем кончилось» требует заглянуть в `audit_log` за каждой строкой.
«Разбору диалогов» она не нужна, а платить за неё пришлось бы на каждом его
открытии — при том что таблица уже держит потолок стоимости как главное своё
ограничение (см. её шапку). Поэтому отбор и сортировка берутся оттуда целиком,
а обогащение живёт здесь.

ЦЕНА
----
Страница — это запрос таблицы (уже с потолками) плюс ОДИН запрос в `audit_log`
на все пятьдесят строк сразу, а не по запросу на строку. Счётчики плиток —
шесть `count(*)`, каждый на индексе `idx_audit_entity`. Замер обязателен перед
сдачей: 29 августа `/stats/summary` отвечал девять секунд ровно потому, что
запрос выглядел безобидно.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots import handoff
from app.models import AuditLog, AvitoAccount, Client, Conversation, Message
from app.services import conversation_table as table

#: События журнала, по которым виден путь бота в диалоге.
СОБЫТИЯ = handoff.BOT_EVENTS


def _группа_причины(action: str, reason: str | None) -> str | None:
    """Вид сбоя по записи журнала. ``None`` — штатная работа, не сбой."""
    if action == handoff.STUCK_ACTION:
        return "stuck"
    for группа in handoff.ALL_OUTCOMES:
        if reason and reason in handoff.reasons_of(группа):
            return группа
    return None


async def _последние(db: AsyncSession, ids: list[uuid.UUID]) -> dict[str, tuple[str, str | None]]:
    """Последняя запись журнала о боте для каждого диалога страницы.

    ⚠ ОДИН ЗАПРОС НА ВСЮ СТРАНИЦУ, А НЕ ПО ЗАПРОСУ НА СТРОКУ. Пятьдесят
    отдельных обращений к журналу — это N+1, который на боевом объёме
    превращает открытие экрана в секунды ожидания.

    ⚠ БЕРЁМ ПОСЛЕДНЮЮ. Диалог может вернуться в очередь и бот вступит снова;
    первая запись рассказала бы про позапрошлый заход.
    """
    if not ids:
        return {}
    строки = [str(i) for i in ids]
    ранг = (
        sa.func.row_number()
        .over(partition_by=AuditLog.entity_id, order_by=AuditLog.created_at.desc())
        .label("ранг")
    )
    под = (
        sa.select(AuditLog.entity_id, AuditLog.action, AuditLog.details, ранг)
        .where(
            AuditLog.entity == "conversation",
            AuditLog.entity_id.in_(строки),
            AuditLog.action.in_(СОБЫТИЯ),
        )
        .subquery()
    )
    res = await db.execute(
        sa.select(под.c.entity_id, под.c.action, под.c.details).where(под.c.ранг == 1)
    )
    итог: dict[str, tuple[str, str | None]] = {}
    for entity_id, action, details in res.all():
        reason = (details or {}).get("reason") if isinstance(details, dict) else None
        итог[str(entity_id)] = (str(action), reason)
    return итог


def _исход(запись: tuple[str, str | None] | None, status: str) -> dict[str, Any]:
    """Что показать в колонке «чем кончилось».

    ⚠ «ЗАКРЫТ БЕЗ ПЕРЕДАЧИ» — НЕ ТО ЖЕ, ЧТО ПЛИТКА «БОТ ЗАКРЫЛ САМ». Плитка
    считает случаи, когда бот сказал «не помогу» и записал почему. Здесь
    записи о передаче нет вовсе: закрыл человек или уборка очереди. Слить их
    одним словом значило бы приписать боту чужие закрытия.
    """
    if запись is None:
        if status == "closed":
            return {"group": None, "label": "закрыт без передачи"}
        return {"group": None, "label": "ещё ведёт"}
    action, reason = запись
    if action == handoff.STUCK_ACTION:
        return {"group": "stuck", "label": "бот завис"}
    if action == handoff.BOT_CLOSED_ACTION and reason in handoff.CLOSE_REASON_LABELS:
        # Бот закрыл сам, и это не сбой: заявка собрана, клиент завершил,
        # шаг сценария (проверка 24.09).
        return {"group": None, "label": handoff.CLOSE_REASON_LABELS[reason]}
    # `reason_label` уже умеет печатать неизвестный код как есть — второе
    # такое поведение заводить незачем.
    return {"group": _группа_причины(action, reason), "label": handoff.reason_label(reason or "")}


async def _счётчики(db: AsyncSession, filters: table.TableFilters) -> list[dict[str, Any]]:
    """Счётчики групп. Порядок — из `handoff`, не по величине.

    ⚠ У КАЖДОЙ ГРУППЫ ЕСТЬ ВИД, И ЭКРАН ОБЯЗАН ИХ РАЗЛИЧАТЬ. «Сбой» — это
    зацикливание, отказ, зависание. «Ёмкость» — «AI не уверен»: бот сработал
    правильно, просто не потянул случай. Смешать их в один ряд значило бы
    назвать сбоем самое частое и самое здоровое поведение бота.
    """
    плитки: list[dict[str, Any]] = []
    for группа in handoff.ALL_OUTCOMES:
        свои = table.TableFilters(
            **{**filters.__dict__, "bot_touched": True, "bot_outcome": группа}
        )
        всего = await db.scalar(table._select_narrowed(db, свои, sa.func.count()).order_by(None))
        плитки.append(
            {
                "group": группа,
                "label": handoff.outcome_label(группа),
                "count": int(всего or 0),
                "kind": "capacity" if группа in handoff.CAPACITY_ORDER else "failure",
            }
        )
    return плитки


async def page(
    db: AsyncSession,
    filters: table.TableFilters,
    *,
    limit: int = table.PAGE_LIMIT_DEFAULT,
    offset: int = 0,
) -> dict[str, Any]:
    """Страница экрана: строки таблицы + исход у каждой + счётчики плиток."""
    свои = table.TableFilters(**{**filters.__dict__, "bot_touched": True})
    стр = await table.query_table(db, свои, limit=limit, offset=offset)

    ids = [uuid.UUID(str(r["id"])) for r in стр["items"]]
    последние = await _последние(db, ids)
    for r in стр["items"]:
        r["outcome"] = _исход(последние.get(str(r["id"])), str(r.get("status") or ""))

    # Счётчики считаются БЕЗ отбора по плитке: иначе выбранная плитка
    # показывала бы своё число, а остальные — нули, и вернуться было бы некуда.
    без_плитки = table.TableFilters(**{**filters.__dict__, "bot_outcome": None})
    стр["counters"] = await _счётчики(db, без_плитки)
    return стр


# ------------------------------------------------------------------ «сейчас»

#: Потолок живого списка. Больше на экран всё равно не помещается, а
#: неограниченный запрос однажды встретит день, когда бот ведёт сотни.
ЖИВЫХ_МАКСИМУМ = 50


async def live(db: AsyncSession) -> dict[str, Any]:
    """Что бот ведёт ПРЯМО СЕЙЧАС: число и сам список.

    ⚠ ТОЧНЫМ ЗАПРОСОМ, А НЕ АРИФМЕТИКОЙ ПО СОБЫТИЯМ. В приложении уже есть
    живые кадры, и счётчик «Входящих» двигается по ним на ±1 — но там иначе
    нельзя: очередь у каждого своя, и общего числа сервер не знает. Здесь
    число общее, а `bot_active` покрыт индексом `ix_conversations_bot_active`,
    то есть точный ответ стоит копейки. Пропущенный кадр в схеме «±1» уводит
    счётчик тихо, и заметить это нечем.

    ⚠ `bot_active`, А НЕ «БОТ ПИСАЛ». Это разные вопросы: экран ниже разбирает
    случившееся, а здесь — кто у бота в руках сию минуту.
    """
    строки = (
        await db.execute(
            sa.select(
                Conversation.id,
                Conversation.status,
                Conversation.last_message_at,
                Conversation.status_since,
                Client.name.label("client_name"),
                AvitoAccount.title.label("account_title"),
            )
            .select_from(Conversation)
            .join(Client, Client.id == Conversation.client_id)
            .join(AvitoAccount, AvitoAccount.id == Conversation.account_id)
            .where(Conversation.bot_active.is_(True))
            .order_by(Conversation.last_message_at.desc())
            .limit(ЖИВЫХ_МАКСИМУМ)
        )
    ).all()

    всего = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Conversation)
        .where(Conversation.bot_active.is_(True))
    )

    ids = [r.id for r in строки]
    реплики = await _реплики_бота(db, ids)
    последние = await _последняя_реплика(db, ids)

    return {
        "count": int(всего or 0),
        "items": [
            {
                "id": str(r.id),
                "client_name": r.client_name,
                "account_title": r.account_title,
                # `status_since` — «с какого момента в этом состоянии». Это и
                # есть честный ответ на «сколько уже идёт»: тем же полем
                # карточка клиента считает «Уже 22 мин».
                "since": (r.status_since.isoformat() if r.status_since else None),
                "last_message_at": (r.last_message_at.isoformat() if r.last_message_at else None),
                "bot_replies": реплики.get(r.id, 0),
                "last_bot_text": последние.get(r.id),
            }
            for r in строки
        ],
    }


async def _реплики_бота(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, int]:
    """Сколько раз бот написал в каждом из этих диалогов — одним запросом."""
    if not ids:
        return {}
    res = await db.execute(
        sa.select(Message.conversation_id, sa.func.count())
        .where(Message.conversation_id.in_(ids), Message.sender_type == "bot")
        .group_by(Message.conversation_id)
    )
    return {cid: int(n) for cid, n in res.all()}


async def _последняя_реплика(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Последняя фраза бота в каждом диалоге — чтобы плохую формулировку было
    видно списком, не открывая каждый.

    Тот же приём с `row_number`, что и у :func:`_последние`: один запрос на всю
    страницу вместо запроса на строку.
    """
    if not ids:
        return {}
    ранг = (
        sa.func.row_number()
        .over(partition_by=Message.conversation_id, order_by=Message.created_at.desc())
        .label("ранг")
    )
    под = (
        sa.select(Message.conversation_id, Message.body, ранг)
        .where(Message.conversation_id.in_(ids), Message.sender_type == "bot")
        .subquery()
    )
    res = await db.execute(sa.select(под.c.conversation_id, под.c.body).where(под.c.ранг == 1))
    return {cid: (body or "") for cid, body in res.all()}
