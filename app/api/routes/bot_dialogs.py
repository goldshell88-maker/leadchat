"""Экран «Диалоги бота» (docs/45) — надзор администратора за работой бота.

ЗАЧЕМ ЭТО НУЖНО. Владелец ловил бота на плохих решениях поштучно, открывая
диалоги руками: 26 августа бот верно отказал по битой матрице — и через пять
минут сам же дожал «Ну что, расскажете, что случилось?». Находилось это
случайно, потому что экрана, на котором работу бота видно списком, не было:
«Разбор диалогов» показывает всё вперемешку и на вопрос «где бот навредил» не
отвечает.

ПОЧЕМУ ОТДЕЛЬНАЯ РУЧКА, А НЕ ФЛАГ У ТАБЛИЦЫ. Права разные. Таблица открыта по
`dialogs:read` — её видят руководитель, менеджер и наблюдатель. Этот экран
владелец просил сделать админским, и `bots:manage` есть только у роли `admin`.
Флаг внутри общей ручки означал бы, что право проверяется по значению
параметра, — а это ровно тот вид проверки, который однажды забывают.
"""

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_permission
from app.bots import handoff
from app.models import User
from app.services import bot_dialogs
from app.services import conversation_table as table

router = APIRouter()

#: Право админское: у роли `head` его нет, у `manager` и `observer` тем более.
bot_perm = require_permission("bots:manage")


@router.get("/bot-dialogs")
async def list_bot_dialogs(
    account_id: uuid.UUID | None = Query(None),
    outcome: str | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    limit: int = Query(table.PAGE_LIMIT_DEFAULT, ge=1, le=table.PAGE_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    user: User = Depends(bot_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Диалоги, которые вёл бот, с исходом у каждого и счётчиками плиток.

    ⚠ НЕИЗВЕСТНЫЙ `outcome` НЕ МОЛЧИТ. Пустой отбор дал бы полную выборку, то
    есть экран показал бы все диалоги под подписью «Бот закрыл сам». Лучше
    честный отказ: опечатка в адресе видна сразу, а не через неделю в отчёте.
    """
    if outcome is not None and outcome not in handoff.OUTCOME_GROUPS:
        from app.core.errors import ApiError

        raise ApiError(
            "validation_error",
            "Неизвестный вид сбоя",
            status=400,
            details={"known": list(handoff.OUTCOME_ORDER)},
        )
    return await bot_dialogs.page(
        db,
        table.TableFilters(
            account_id=account_id,
            bot_outcome=outcome,
            date_from=date_from,
            date_to=date_to,
        ),
        limit=limit,
        offset=offset,
    )


@router.get("/bot-dialogs/live")
async def bot_dialogs_live(
    user: User = Depends(bot_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Что бот ведёт прямо сейчас — число и список.

    ⚠ ОТДЕЛЬНО ОТ `/bot-dialogs`, ПОТОМУ ЧТО ЕЁ ОПРАШИВАЮТ ЧАСТО. Экран
    обновляет живую полосу раз в десять секунд; тянуть ради этого таблицу с
    метриками и семь счётчиков значило бы платить за разбор прошлого каждые
    десять секунд ради одного числа про настоящее.
    """
    return await bot_dialogs.live(db)
