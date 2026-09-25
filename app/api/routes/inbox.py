"""Очередь «Входящие» — явное принятие диалога (15 §2.1, план 7.1).

Зачем: при тринадцати операторах и девяти каналах правило «кто первым
ответил, тот и ведёт» ломается — двое пишут одному клиенту, а третьего не
берёт никто. Диалог сначала ждёт в очереди, оператор жмёт «Принять» или
«Отклонить», принятый уходит в «Мои» и у остальных показывается занятым.

**Два входа в один список — и почему это не дубль.** Очередь читается и как
`GET /inbox`, и как `GET /conversations?tab=inbox`. Обе ручки тонкие и обе
зовут одну функцию выборки `services.inbox.list_inbox`: дублируется путь, а
не логика. Оба входа нужны по делу:

* `GET /inbox` — рабочий ресурс очереди. У неё своя выборка (ничей диалог,
  ждущий решения), свой порядок (дольше всех ждущий — первым, в списке
  диалогов порядок обратный) и своя судьба строки: приняли — она исчезает у
  всех тринадцати сразу. На фронте это отдельный кэш, и события очереди не
  должны инвалидировать вкладки «Мои/Все/Новые/Закрытые», а те — очередь.
* `?tab=inbox` — та же очередь как ПЯТАЯ ВКЛАДКА того же списка, чтобы
  клиенты, которые уже умеют вкладки (дельта-синк десктопа 04 §5.4, догон
  после reconnect'а 01 §11.7), не заводили ради неё второй механизм.

Чего мы не сделали — и это главное: собственной сборки фильтров, своей
пагинации и своей формы элемента. Их ровно по одной штуке на систему.

**Права.** Читать очередь может любая роль (`conversations:read` — у всех,
01 §12): руководителю полезно видеть, что очередь растёт, и разобрать её
передачей (01 §5.5). Принимать, отклонять и возвращать может тот, кто
отвечает клиентам — `messages:send`, то есть администратор и менеджер.
Руководитель получает `403 read_only_role` (фронт рисует плашку «Режим
просмотра»), наблюдатель — `403 forbidden`. Это не ветвление в ручке, а
ровно то, что делает `require_permission`; сервис проверяет то же самое
второй раз, потому что его зовут не только отсюда.

**Атомарность.** Принятие — гонка по построению: на тринадцати операторах
двое жмут «Принять» одновременно регулярно. Побеждает ровно один, второй
получает `409 already_claimed` с именем занявшего. Решает это не ручка, а
``services.inbox.claim`` одним `UPDATE ... WHERE claimed_by_id IS NULL`.
Сравнить-потом-записать здесь было бы дырой: между `SELECT` и `UPDATE`
живёт `await`, и планировщик asyncio с удовольствием туда встанет.

**Порядок один на все ручки (08 §8.1):** запись → commit → публикация в
Pub/Sub. Кадры собираются здесь, как `_publish_change` в
routes/conversations.py, но по патчам из сервиса (`claimed_patch`,
`queue_patch`) — чтобы у HTTP-ответа и у WS-кадра не было шанса разойтись.
Публикуем только после commit'а: иначе браузер успевает показать принятый
диалог, который откатился.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, require_permission
from app.models import User
from app.schemas.inbox import (
    ClaimOut,
    DeclineOut,
    DeclineRequest,
    InboxCountOut,
    ReleaseOut,
)
from app.services import conversations as convs
from app.services import inbox as svc
from app.services import notifications as notify_svc
from app.services import read_markers
from app.services.user_ref import user_ref
from app.ws.events import iso, publish_event
from app.ws.hub import (
    CLAIM_PERMISSION,
    INBOX_CLAIMED,
    INBOX_DECLINED,
    publish_inbox_released,
)

router = APIRouter()

read_perm = require_permission("conversations:read")
# Принять/отклонить/вернуть — только тот, кто отвечает клиентам (01 §12).
# Право берём из каталога хаба, чтобы фильтр `can_claim` в WS-кадре и фильтр
# HTTP-ручки не могли разъехаться: разъезд означал бы кнопку «Принять»,
# которая отвечает 403.
claim_perm = require_permission(CLAIM_PERMISSION)
#: ⚠ У ВОЗВРАТА В ОЧЕРЕДЬ — СВОЁ ПРАВО, И ОНО ТОЛЬКО У АДМИНИСТРАТОРА
#: (решение владельца 28.08). Довод целиком — в каталоге прав `core/rbac.py`.
#: Коротко: возврат снимает ответственного и отдаёт тринадцати диалог, с
#: которым человек уже поговорил; оператору для «я сейчас занят» есть
#: «Отклонить», и это про диалог, ЕЩЁ не начатый.
release_perm = require_permission("conversations:release")


async def _counters(db: AsyncSession, user: User) -> InboxCountOut:
    """Счётчик очереди ГЛАЗАМИ этого человека (отклонённое им не считается).

    Берём разбивку (`inbox_counts`), а не одно число: `escalated` едет в том
    же ответе, потому что бейдж «никто не берёт» и бейдж «в очереди N» рисует
    один и тот же компонент — второй запрос ради второй цифры не нужен.
    """
    counts = await svc.inbox_counts(db, user)
    return InboxCountOut(count=counts["waiting"], escalated=counts["escalated"])


# --- чтение ------------------------------------------------------------------


@router.get("/inbox")
async def list_inbox(
    account_id: uuid.UUID | None = Query(None, description="Канал Авито"),
    limit: int = Query(svc.DEFAULT_LIMIT, ge=1, le=svc.MAX_LIMIT),
    offset: int = Query(0, ge=0),
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Очередь диалогов, ждущих принятия. Конверт — как у списка (01 §5.1).

    Форма ответа `{items, page}` совпадает с `GET /conversations` намеренно:
    строки очереди рисует тот же компонент списка, и вторая форма означала бы
    вторую ветку рендера. Отличия элемента — только добавленные поля ожидания
    (`waiting_seconds`, `escalated`), их собирает `services.inbox.inbox_item`.

    Права — `conversations:read`: смотреть очередь может любая роль, включая
    наблюдателя и руководителя (последнему она нужна, чтобы разобрать затор
    передачей, 01 §5.5). Принимать может не каждый — это ниже.
    """
    items, total = await svc.list_inbox(
        db, user, svc.InboxFilters(account_id=account_id, limit=limit, offset=offset)
    )
    # Бейдж непрочитанного — состояние человека, а не диалога (01 §5.1): тот же
    # пер-юзерный маркер, что и в списке диалогов. Своей ветки у очереди нет.
    items = await read_markers.apply_unread_counts(db, redis, user.id, items)
    return {"items": items, "page": {"limit": limit, "offset": offset, "total": total}}


@router.get("/inbox/count", response_model=InboxCountOut)
async def get_inbox_count(
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
) -> InboxCountOut:
    """Бейдж вкладки «Входящие» (11 §2.1).

    Отдельная ручка нужна потому, что бейдж живёт дольше открытого списка: он
    висит на вкладке, когда открыты «Мои», и восстанавливается после
    reconnect'а WS (01 §11.7), где пропущенные кадры очереди инкрементами уже
    не догнать.
    """
    return await _counters(db, user)


# --- действия (messages:send) ------------------------------------------------


@router.post("/conversations/{conversation_id}/claim", response_model=ClaimOut)
async def claim_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(claim_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ClaimOut:
    """«Принять Диалог» → диалог целиком + свежий счётчик очереди.

    Ровно один из двух одновременно нажавших получает 200, второй — 409
    `already_claimed` с именем победителя (детали собирает сервис).
    """
    result = await svc.claim(db, conversation_id, user)
    await db.commit()

    conv = result.conversation
    detail = await convs.conversation_detail(db, conv)
    patch = svc.claimed_patch(conv, user)

    # Три кадра на одно действие — как у назначения (01 §11.3): системная
    # запись в открытой ленте, исчезновение из очереди у всех остальных,
    # починка строки списка у тех, кто очередь не смотрит.
    await publish_event(
        redis,
        "message:new",
        {
            "conversation_id": str(conv.id),
            "message": convs.message_out(result.system_message),
            "conversation_patch": patch,
        },
    )
    await publish_event(
        redis,
        INBOX_CLAIMED,
        {
            "conversation_id": str(conv.id),
            # `claimed_by`, а не `assignee`: это «кто вынул диалог из
            # очереди» — факт, который последующая передача (01 §5.5) не
            # переписывает. Тем же именем зовётся колонка и `details` ошибки
            # 409, так что фронт читает одно слово во всех трёх местах.
            "claimed_by": user_ref(user),
            "claimed_at": iso(conv.claimed_at),
            "waited_seconds": result.waited_seconds,
            "conversation_patch": patch,
        },
    )
    await publish_event(
        redis, "conversation:updated", {"conversation_id": str(conv.id), "patch": patch}
    )
    return ClaimOut(conversation=detail, **(await _counters(db, user)).model_dump())


@router.post("/conversations/{conversation_id}/decline", response_model=DeclineOut)
async def decline_conversation(
    conversation_id: uuid.UUID,
    body: DeclineRequest | None = None,
    user: User = Depends(claim_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> DeclineOut:
    """«Отклонить» — диалог уходит из МОЕЙ очереди и остаётся в общей.

    Тело необязательно целиком (`body=None`), не только поле `reason`: кнопка
    «Отклонить» шлёт запрос без тела, и требовать `{}` ради единственного
    опционального поля значило бы ловить 400 на пустом клике.

    Широковещательного «диалог занят» здесь нет и быть не может: у коллег
    очередь не изменилась. Уходит только системная запись в ленту (коллеги
    должны видеть, кто отказался и почему) и адресный кадр отказавшемуся.
    """
    reason = body.reason if body else None
    result = await svc.decline(db, conversation_id, user, reason)
    await db.commit()

    conv = result.conversation
    counters = await _counters(db, user)

    if not result.already_declined and result.system_message is not None:
        await publish_event(
            redis,
            "message:new",
            {
                "conversation_id": str(conv.id),
                "message": convs.message_out(result.system_message),
                "conversation_patch": {"escalated": result.escalated},
            },
        )
    # Кадр адресный (`only_user`) — ровно как read-маркер в 01 §5.3: отказ это
    # состояние человека, а не диалога. Адресность даёт побочный плюс — в нём
    # можно везти абсолютный счётчик, он честен для единственного получателя.
    #
    # Про имя `escalated` здесь — оно ФЛАГ, и это не оплошность. В кадре о
    # диалоге поля называются как у самого диалога: строка очереди и патч несут
    # `escalated: bool` («этого не берёт никто»), и кадр обязан говорить с
    # фронтом на том же языке. Число с тем же именем живёт только в счётчиках
    # (`InboxCountOut.escalated` — сколько таких диалогов в очереди), и в теле
    # ответа, где рядом стоят оба смысла, флаг поэтому зовётся `escalated_now`.
    # Соответствие «флаг кадра ↔ `escalated_now` ответа» проверяется тестом,
    # чтобы одну из сторон нельзя было переименовать в одиночку.
    #
    # Счётчика брошенных в кадре нет намеренно: у отказавшегося он от его же
    # отказа не меняется — диалог, который он только что эскалировал, из его
    # очереди ушёл вместе с отказом. Возить всегда одно и то же число значило бы
    # приглашать фронт на него опереться.
    await publish_event(
        redis,
        INBOX_DECLINED,
        {
            "conversation_id": str(conv.id),
            "declined_by": user_ref(user),
            "reason": reason,
            "count": counters.count,
            "declined_count": result.declined_count,
            "escalated": result.escalated,
        },
        only_user=str(user.id),
    )
    if result.escalated:
        # «Отказались все» — диалог остаётся в очереди и обязан быть в ней
        # заметным: брошенный клиент хуже спорной пометки.
        await publish_event(
            redis,
            "conversation:updated",
            {"conversation_id": str(conv.id), "patch": {"escalated": True}},
        )
    if result.notification is not None:
        await notify_svc.deliver(redis, result.notification)

    return DeclineOut(
        conversation_id=conv.id,
        declined=True,
        already_declined=result.already_declined,
        reason=reason,
        escalated_now=result.escalated,
        **counters.model_dump(),
    )


@router.post("/conversations/{conversation_id}/decline/undo", response_model=DeclineOut)
async def undo_decline_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(claim_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> DeclineOut:
    """Забрать отказ обратно — диалог возвращается в МОЮ очередь.

    Отказ стал одним нажатием, значит промахнуться легко, а последствие было
    необратимым: обращение навсегда уходило из очереди отказавшегося. Тост
    «Диалог отклонён · Вернуть» превращает это в поправимое.

    Кадры зеркальны отказу: системная запись в ленту всем (коллеги видят и
    отказ, и его отмену — иначе в ленте останется только половина истории), и
    адресный кадр вернувшему со свежим счётчиком его очереди.
    """
    result = await svc.undo_decline(db, conversation_id, user)
    await db.commit()

    conv = result.conversation
    counters = await _counters(db, user)

    if result.system_message is not None:
        await publish_event(
            redis,
            "message:new",
            {
                "conversation_id": str(conv.id),
                "message": convs.message_out(result.system_message),
                "conversation_patch": {"escalated": False},
            },
        )
        # Пометка «никто не берёт» снята — сказать об этом надо всем, у кого
        # диалог в списке, а не только вернувшему.
        await publish_event(
            redis,
            "conversation:updated",
            {"conversation_id": str(conv.id), "patch": {"escalated": False}},
        )

    await publish_event(
        redis,
        INBOX_DECLINED,
        {
            "conversation_id": str(conv.id),
            "declined_by": user_ref(user),
            "reason": None,
            "count": counters.count,
            "declined_count": result.declined_count,
            "escalated": False,
            # Отличает возврат от отказа: кадр тот же, смысл обратный.
            "undone": True,
        },
        only_user=str(user.id),
    )

    return DeclineOut(
        conversation_id=conv.id,
        declined=False,
        already_declined=False,
        reason=None,
        escalated_now=False,
        **counters.model_dump(),
    )


@router.post("/conversations/{conversation_id}/release", response_model=ReleaseOut)
async def release_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(release_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> ReleaseOut:
    """Вернуть принятый диалог в очередь — «я не тот, кто нужен».

    ⚠ ТОЛЬКО АДМИНИСТРАТОР (решение владельца 28.08). Раньше ручка висела на
    `messages:send`, то есть была у каждого, кто умеет отвечать клиенту.
    Автоматике это не мешает: бот при передаче человеку и оба сторожа зовут
    `inbox.return_to_queue` в сервисном слое, мимо HTTP и мимо прав.

    Отличие от `POST /conversations/{id}/assign` с `assignee_id: null`
    (01 §5.5) — в намерении: снятие назначения руководитель делает над ЧУЖИМ
    диалогом, а возврат — над своим. Диалог возвращается свободным, и у всех операторов он снова
    появляется в очереди — поэтому в кадре он едет ЦЕЛИКОМ: у подключившегося
    после принятия этой строки нет вовсе, вставлять в список нечего.
    """
    result = await svc.release(db, conversation_id, user)
    await db.commit()

    conv = result.conversation
    detail = await convs.conversation_detail(db, conv)
    patch: dict[str, Any] = result.patch or svc.queue_patch(conv)
    # Кому этот КАНАЛ доступен: без списка кадр по правилу совместимости уедет
    # каждому подключённому — см. `publish_inbox_released`.
    допущенные = await svc.eligible_operator_ids(db, conv)

    await publish_event(
        redis,
        "message:new",
        {
            "conversation_id": str(conv.id),
            "message": convs.message_out(result.system_message),
            "conversation_patch": patch,
        },
    )
    await publish_inbox_released(
        redis,
        conversation=detail,
        released_by=user_ref(user),
        offered_at=iso(conv.offered_at),
        conversation_patch=patch,
        eligible_operator_ids=допущенные,
    )
    await publish_event(
        redis, "conversation:updated", {"conversation_id": str(conv.id), "patch": patch}
    )
    return ReleaseOut(conversation=detail, **(await _counters(db, user)).model_dump())


# РАЗГРУЗКИ ОЧЕРЕДИ ЗДЕСЬ БОЛЬШЕ НЕТ (требование владельца от 11 августа, №8).
#
# Стояли две ручки — `GET /inbox/stale` и `POST /inbox/close-stale`: показать,
# сколько диалогов молчит дольше выбранного срока, и закрыть их пачкой. Владелец
# отказался от возможности целиком: «функция не нужна вообще». Массовое закрытие
# в живой системе — операция, у которой нет отмены, а очередь заказчик разбирает
# руками и знает её содержимое лучше любого срока в днях.
#
# ОБЫЧНОЕ ЗАКРЫТИЕ ДИАЛОГА ЭТИМ НЕ ЗАТРОНУТО — проверено перед удалением, а не
# предположено: оно идёт другим путём, `PATCH /conversations/{id}/status` →
# `services.conversations.change_status`, и с массовым закрытием не делило ни
# одной строки кода. Единственными потребителями `services.queue_cleanup` были
# эти две ручки и ночное задание планировщика — поэтому 22.08 сам модуль удалён:
# 226 строк без единого вызывающего читались как работающая функция.
