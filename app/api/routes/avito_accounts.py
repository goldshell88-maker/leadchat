"""Назначение операторов на каналы Авито (15 §2.2, план 7.2).

Экран из Jivo, которого в LeadChat не было: «Назначить операторов на
„! Парт - 7 / Ист - В43 МНЧ !"» — список сотрудников с галочками. У заказчика
девять каналов и тринадцать операторов, и у каждого канала свой набор людей.
Пока привязки нет, очередь «Входящие» (план 7.1) показывает всем всё: человек,
отвечающий за один поток, листает чужие обращения и берёт не свои.

Четыре ручки:

* `GET  /avito-accounts/{id}/operators` — состояние экрана целиком: кто
  назначен плюс ВЕСЬ список сотрудников с пометкой «можно ли назначить».
  Право `accounts:read` — admin и head;
* `PUT  /avito-accounts/{id}/operators` — полная замена набора. Право
  `accounts:manage`, а оно есть только у admin (01 §12, DESIGN §5.1):
  состав операторов решает, кому попадут обращения и чью переписку человек
  увидит, — это раздача доступа, а не настройка интерфейса;
* `GET  /me/channels` — свои каналы, блок профиля. Любая авторизованная роль:
  `accounts:read` есть у админа и руководителя, а знать свой список должен как
  раз менеджер;
* `GET  /users/{id}/accounts` — тот же ответ про другого сотрудника (карточка
  человека в «Команде»), поэтому уже под `accounts:read`.

**Правило совместимости, ради которого всё написано так.** Канал, на который не
назначен ни один живой оператор, доступен ВСЕМ операторам, а не никому. Иначе
первое же включение фильтрации оставило бы девять каналов заказчика без
очереди — обращения повисли бы молча, без единой ошибки в логах. Здесь оно
видно в ``count == 0`` свода и в ``access="open"`` профиля, а исполняется
ровно один раз — в ``account_operators.visible_accounts_condition``, откуда
его берут обе точки фильтрации очереди (``inbox.visible_queue_condition`` и
``inbox.eligible_operator_ids``). Повторять правило здесь нельзя: две копии
разъедутся, и очередь начнёт врать эскалации.

Раскладка по слоям как везде (08 §8.1): вся работа со связью оператор↔канал —
в `app/services/account_operators.py`, включая проверки набора и запись в
журнал; ручки остаются тонкими и владеют транзакцией (commit делает ручка).
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db, get_redis, has_permission, require_permission
from app.core.errors import ApiError
from app.models import AvitoAccount, User
from app.schemas.account_operators import (
    ChannelOperatorsOut,
    ChannelOperatorsSaved,
    ChannelOperatorsSummary,
    ChannelOperatorsWrite,
    MyChannelsOut,
)
from app.services import account_operators as operators_service
from app.services import avito_accounts as accounts_service
from app.services import channel_health

router = APIRouter()

read_accounts = require_permission("accounts:read")  # admin + head (01 §12)
manage_accounts = require_permission("accounts:manage")  # только admin


# --- операторы канала --------------------------------------------------------


class BulkChange(BaseModel):
    account_id: uuid.UUID
    user_id: uuid.UUID
    assigned: bool


class BulkWrite(BaseModel):
    """Пачка изменений «человек на канале».

    ⚠ ДЕЛЬТЫ, А НЕ ПОЛНЫЕ НАБОРЫ, И ЭТО НЕ МЕЛОЧЬ. Полный набор на канал — это
    гонка: пока администратор смотрел на решётку, кто-то мог поменять состав
    другого канала, и отправка «как было на экране» затёрла бы чужую правку.

    Потолок 1000: полная решётка это 35 каналов на 35 человек, и даже
    «переставить всех везде» в него укладывается с запасом.
    """

    changes: list[BulkChange] = Field(default_factory=list, max_length=1000)


@router.get("/avito-accounts/operators/grid")
async def operators_grid(
    user: User = Depends(read_accounts),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Решётка «люди × каналы» одним ответом (просьба владельца 04.09).

    Три запроса к базе вместо тридцати пяти открытий поканального экрана,
    каждое из которых читает таблицу сотрудников целиком.
    """
    return await operators_service.grid(db, redis)


@router.post("/avito-accounts/operators/bulk")
async def operators_bulk(
    body: BulkWrite,
    user: User = Depends(manage_accounts),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Применить пачку изменений одной транзакцией.

    ⚠ ЗАЧЕМ ЭТО ЕСТЬ. Подключить одного человека ко всем каналам стоило
    администратору 64 нажатий: развернуть строку, «Назначить», галочка,
    «Сохранить» — и так тридцать пять раз. Дословно: «я вручную по 30 раз
    захожу и тыкаю».

    ⚠ ОТВЕТ НАЗЫВАЕТ КАНАЛЫ, ОСТАВШИЕСЯ БЕЗ ЖИВЫХ ОПЕРАТОРОВ. Пустой набор
    означает «канал открыт ВСЕМ», а не «закрыт»: снятие последнего человека
    РАСШИРЯЕТ доступ. Массовое действие обязано сказать это вслух — иначе
    администратор, сузивший (как ему казалось) доступ, откроет канал всей
    смене и не узнает об этом.
    """
    итог = await operators_service.apply_bulk(
        db,
        [(c.account_id, c.user_id, c.assigned) for c in body.changes],
        actor=user,
    )
    await db.commit()
    return итог


@router.get("/avito-accounts/{account_id}/operators", response_model=ChannelOperatorsOut)
async def list_channel_operators(
    account_id: uuid.UUID,
    user: User = Depends(read_accounts),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ChannelOperatorsOut:
    """Кто назначен на канал плюс весь список сотрудников (15 §2.2).

    Руководителю ручка открыта на чтение: разбирать затор в очереди — его
    работа, а для этого надо видеть, кому канал вообще виден. Менять набор он
    не может — это `accounts:manage`, только admin.

    Список приезжает вместе с галочками одним ответом: два запроса,
    выполненные в разном порядке, дают галочки на людях, которых нет в списке.
    Пустой ``assigned_ids`` — канал открыт всем (правило совместимости).
    """
    data = await operators_service.channel_operators(db, redis, account_id)
    return ChannelOperatorsOut.model_validate(data)


@router.put("/avito-accounts/{account_id}/operators", response_model=ChannelOperatorsSaved)
async def replace_channel_operators(
    account_id: uuid.UUID,
    body: ChannelOperatorsWrite,
    user: User = Depends(manage_accounts),
    db: AsyncSession = Depends(get_db),
) -> ChannelOperatorsSaved:
    """Полная замена набора операторов канала (экран с галочками, 15 §2.2).

    Тело — итоговый список: кого в нём нет, тот с канала снимается. Пустой
    список разрешён и означает «канал общий» — его обращения снова видят все
    операторы, а не никто.

    Отказы приходят из сервиса и всегда `422` с машиночитаемым `reason`
    (`user_not_found`, `user_inactive`, `cannot_answer_clients`): молча
    отбросить неизвестный id значит показать админу «сохранено» и оставить
    канал без половины смены. Сервис же пишет и журнал — «кто, какой канал,
    кого добавил и кого убрал» (01 §9.7); дублировать запись здесь нельзя.

    В ответе — свод для карточки: модалка закрывается, а строка «Операторы: N»
    под карточкой должна обновиться без второго запроса.
    """
    result = await operators_service.set_operators(
        db, account_id, list(body.operator_ids), actor=user
    )
    await db.commit()
    summary = (await operators_service.operators_summary(db, [result.account.id])).get(
        result.account.id, {"count": 0, "preview": []}
    )
    return ChannelOperatorsSaved(operators=ChannelOperatorsSummary.model_validate(summary))


# --- каналы одного сотрудника ------------------------------------------------


@router.get("/me/channels", response_model=MyChannelsOut)
async def list_my_channels(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyChannelsOut:
    """Свои каналы — блок профиля (11 §4.3).

    Права на это отдельного нет и быть не должно: `accounts:read` есть у
    админа и руководителя, а вопрос «почему мне приходят одни обращения и не
    приходят другие» задаёт как раз менеджер. Свой список человек обязан
    уметь проверить сам, не спрашивая администратора.
    """
    return MyChannelsOut.model_validate({"items": await operators_service.channels_of(db, user)})


@router.get("/users/{user_id}/accounts", response_model=MyChannelsOut)
async def list_operator_channels(
    user_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MyChannelsOut:
    """Каналы другого сотрудника — карточка человека в «Команде» (11 §4.2).

    Чужой список — это карта доступа к переписке клиентов, поэтому право
    `accounts:read` (admin и head). Свой список остаётся доступен любому:
    ручка не должна отвечать 403 на вопрос человека о самом себе — иначе
    фронту пришлось бы выбирать путь по роли, а не по смыслу действия.
    """
    if user_id != user.id and not has_permission(user, "accounts:read"):
        raise ApiError("forbidden", status=403)
    target = user if user_id == user.id else await db.get(User, user_id)
    if target is None:
        raise ApiError("not_found", "Сотрудник не найден", status=404)
    return MyChannelsOut.model_validate({"items": await operators_service.channels_of(db, target)})


@router.get("/avito-accounts/{account_id}/subscriptions")
async def account_subscriptions(
    account_id: uuid.UUID,
    user: User = Depends(manage_accounts),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Кто СЕЙЧАС подписан на события этого аккаунта в Авито (#39).

    ЗАЧЕМ ЭТО ЕСТЬ. На аккаунтах заказчика работает JivoChat, и главный
    неотвеченный вопрос переезда — что случится с её подпиской, когда
    подпишемся мы: подписки сосуществуют, наша перебивает чужую или чужая
    перебивает нашу. От ответа зависит, можно ли вести пилот на двух-трёх
    диспетчерах параллельно с работающим Jivo, или переезд обязан быть
    одномоментным для всей смены.

    Ручка ТОЛЬКО ЧИТАЕТ. Она и есть способ узнать ответ, ничего не сломав:
    видно и число подписок, и их адреса — свой мы узнаем по домену.

    ПОЧЕМУ ЭТО ВАЖНО ЗНАТЬ ДО ПОДКЛЮЧЕНИЯ. Подключение аккаунта в интерфейсе
    регистрирует наш вебхук сразу и без вопросов. То есть подключить боевой
    аккаунт «просто посмотреть» нельзя — это уже опыт над работающим Jivo.
    Здесь же аккаунт трогать не нужно: достаточно ключей приложения.

    ИТОГ ЗАПОМИНАЕТСЯ (12 августа). Сам поход в Авито делает
    `channel_health.audit_subscription`, и он же кладёт ответ в Redis — тот
    самый, из которого карточка канала пишет «сверка 16:54, расхождений нет».
    Раньше человек, нажавший «проверить подписку», видел ответ и закрывал
    вкладку: система про эту проверку не узнавала ничего, и через минуту
    карточка снова гадала по времени тишины.
    """
    account = await db.get(AvitoAccount, account_id)
    if account is None:
        raise ApiError("not_found", "Аккаунт не найден", status=404)

    audit = await channel_health.audit_subscription(db, redis, account)
    if audit.result == channel_health.AUDIT_UNKNOWN:
        # «Не смогли спросить» человеку отвечаем отказом, а не пустым списком:
        # пустой список читается как «подписок нет», то есть как ответ на
        # вопрос, на который мы ответа не получили.
        raise ApiError(
            "bad_gateway",
            "Авито сейчас не отвечает — попробуйте позже",
            status=502,
            details={"reason": "avito_unavailable"},
        )

    ours = accounts_service.webhook_url_for(account)
    return {
        "subscriptions": audit.items,
        # Свой адрес отдаём рядом, чтобы человеку не пришлось сличать длинные
        # строки глазами: вопрос ведь не «сколько их», а «наша тут одна или
        # рядом с чужой».
        "our_url": ours,
        "ours_present": audit.result == channel_health.AUDIT_OURS,
        "others": [item for item in audit.items if item.get("url") != ours],
        # Время сверки — то же, что покажет карточка канала. Без него две
        # правды об одной проверке разъезжаются: здесь «только что», а там
        # «вчера в 09:15».
        "checked_at": audit.checked_at.isoformat() if audit.checked_at else None,
    }
