"""Conversations (01 §5).

Чтение (все роли, `conversations:read`): список, деталь, лента, отметка
прочитанного, история клиента. Управление (`conversations:manage` —
admin/head/manager, observer 403): смена статуса §5.4 и назначение/передача
§5.5. Отправка сообщений и заметки — соседние модули (01 §6).

Транзакционный порядок везде один (08 §8.1): строка диалога `FOR UPDATE` →
запись → commit → публикация в Pub/Sub. Событий на одно действие три, и это
намеренно: `message:new` рисует системную запись в открытой ленте,
`conversation:assigned` даёт новому ответственному ⚑/звук (хаб подставляет
`is_for_you` каждому получателю), `conversation:updated` чинит строку списка
у всех остальных.

Вкладка «Входящие» (15 §2.1, план 7.1) читается отсюда как `?tab=inbox` —
пятым значением к `my|all|new|closed`. Это ВТОРОЙ вход в тот же список: у
рабочего места есть свой ресурс очереди (`GET /inbox`, routes/inbox.py) со
своим кэшем и своей судьбой строки, а `?tab=inbox` нужен клиентам, которые
уже умеют вкладки, — дельта-синку десктопа (04 §5.4) и догону после обрыва
WS (01 §11.7). Дублируется при этом путь, а не логика: обе ручки зовут одну
`services.inbox.list_inbox`, поэтому состав строки, порядок «дольше всех
ждущий первым» и фильтры у них общие по построению.

Очередь трогают и обычные ручки этого модуля — и потому публикуют её кадры:
передача уводит диалог из очереди у всех тринадцати (`inbox:claimed`), а
снятие ответственного возвращает его туда же (`inbox:released`). Иначе
руководитель разобрал бы затор, а строка осталась бы висеть у операторов.
"""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis, has_permission, require_permission
from app.core.errors import ApiError
from app.models import Client, Conversation, Message, User
from app.services import (
    app_settings,
    dialogs_export,
    notifications,
    participants,
    pins,
    read_markers,
)
from app.services import conversation_status as status_dict
from app.services import conversation_table as table
from app.services import conversations as convs
from app.services import stats as stats_svc
from app.services import transfer as transfer_svc
from app.services.audit import write_audit
from app.services.client_enrich import enqueue_enrich_client
from app.services.user_ref import user_ref
from app.ws.events import publish_event
from app.ws.hub import INBOX_CLAIMED, publish_inbox_released

router = APIRouter()

read_perm = require_permission("conversations:read")
# Таблица — управленческий отчёт: показывает скорость ответа по операторам,
# а не рабочий экран.
# ⚠ 27.08 право отвязано от статистики (решение владельца «сделать доступной
# для всех»). Выдать менеджеру `stats:all` было нельзя — вместе с разбором ему
# открылись бы Статистика по всем и Живая лента. Своё право есть у всех ролей.
table_perm = require_permission("dialogs:read")
manage_perm = require_permission("conversations:manage")
# Здесь было `send_perm = require_permission("messages:send")` — им охранялись
# ручки отложки, единственные в этом файле, которым нужно было право «отвечать
# клиенту», а не «управлять диалогами». Отложку сняли 12 августа, охранять
# стало нечего.

MAX_COMMENT_LEN = 1000

# Пятая вкладка списка (15 §2.1). Значение живёт константой, потому что его
# знают трое: эта ручка, `services.inbox.list_inbox` и фронт.
INBOX_TAB = "inbox"

#: Фильтр «Статус» — собран из общего словаря. Его спрашивают ТРИ ручки:
#: список диалогов, таблица «Разбор» и её выгрузка.
#:
#: ⚠ ДВЕ ПОСЛЕДНИЕ ЕГО НЕ ПРОВЕРЯЛИ, И ЭТО БЫЛ ПРОПУСК, А НЕ РЕШЕНИЕ. Замер боя
#: 14 августа: `?status=zzz` в таблице отвечал 200 и ПУСТОЙ страницей — значение
#: уходило в `WHERE status = 'zzz'` как есть и не совпадало ни с чем. Человек
#: видит пустую таблицу и делает единственный доступный ему вывод: диалогов
#: нет. Рядом, в этом же сервисе, неизвестное имя КОЛОНКИ отбивается словами
#: (`check_sort` отвечает 400 со списком допустимого) — то есть в одной
#: выборке одна ошибка называется вслух, а вторая проглатывается.
STATUS_FILTER_PATTERN = f"^({'|'.join(status_dict.STATUSES)})$"


# --- схемы запросов ----------------------------------------------------------


class StatusPatch(BaseModel):
    #: ЛИШНИЕ ПОЛЯ — ОТКАЗ, А НЕ МОЛЧАЛИВОЕ ЗАБВЕНИЕ, и заведено это ровно
    #: сейчас, вместе со снятием отложки и результата обращения.
    #:
    #: По умолчанию pydantic незнакомые ключи ОТБРАСЫВАЕТ. До этой правки тело
    #: возило `snooze_until`, `outcome` и `outcome_amount_rub`; открытые вкладки
    #: и десктопы обновляются не в момент выката, и запросы прежней формы будут
    #: идти ещё сутки. Без `forbid` диспетчер выбрал бы «Выезд назначен»,
    #: получил бы 200 и ушёл — а в базе не осталось бы ничего. Тихое «ок» на
    #: невыполненное хуже отказа: отказ виден, и по нему обновляют вкладку.
    #:
    #: Приём в проекте уже принят — `avito_connect`, `internal`, `support`,
    #: `schemas/bots`.
    model_config = ConfigDict(extra="forbid")

    #: Значения перечислены ЛИТЕРАЛАМИ, потому что `Literal` требует констант
    #: времени импорта, а собранный из кортежа тип теряет схему OpenAPI. Это
    #: третье и последнее место, где состав словаря записан не ссылкой; два
    #: других — `CheckConstraint` модели и регулярка фильтра ниже. Все три
    #: сверяются с `services.conversation_status.STATUSES` охранным тестом
    #: `tests/unit/test_conversation_status.py`, и молча разъехаться не могут.
    status: Literal["new", "in_progress", "waiting_client", "closed"]

    # ЗАКРЫТИЕ ТЕПЕРЬ ОДНОШАГОВОЕ. Здесь были `snooze_until` (срок отложки) и
    # пара `outcome` / `outcome_amount_rub` («Чем закончилось обращение?»).
    # Оба набора сняты 12 августа решением владельца. Колонки `outcome*` в базе
    # остались вместе с записанным — просто их больше никто не заполняет.


class AssignRequest(BaseModel):
    assignee_id: uuid.UUID | None  # required, но nullable: null — снять назначение
    comment: str | None = Field(default=None, max_length=MAX_COMMENT_LEN)

    @field_validator("comment")
    @classmethod
    def _strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        return v.strip() or None


# --- публикация событий ------------------------------------------------------


def _conversation_patch(detail: dict[str, Any]) -> dict[str, Any]:
    """Дельта строки списка (01 §11.3) — только изменённые этим действием поля.

    Берём их из уже собранной детали, а не собираем заново: у HTTP-ответа и у
    WS-кадра не должно быть шанса разойтись.

    `in_inbox` едет в каждой дельте вместе со статусом и ответственным, потому
    что меняется ровно ими (7.1): назначили — диалог ушёл из очереди, сняли
    ответственного — вернулся, закрыли — исчез. Без него открытый на экране
    диалог, который руководитель только что кому-то передал, остался бы с
    кнопками «Принять/Отклонить».
    """
    return {
        "status": detail["status"],
        # Время входа в статус едет вместе со статусом — иначе строка контекста
        # «В работе у Иванова · 12 мин» у второго оператора продолжала бы
        # считать от прошлого состояния до ближайшего перезапроса.
        "status_since": detail["status_since"],
        "assignee": detail["assignee"],
        "in_inbox": detail["in_inbox"],
    }


def _reject_inbox_filters(**given: object) -> None:
    """Фильтры, которых у очереди нет, — отказ, а не тихое игнорирование.

    Очередь умеет ровно один фильтр — по аккаунту (плюс канальный в 7.2).
    Остальные сузили бы выдачу, а сервис очереди их не знает: молча
    проигнорировать значит показать БОЛЬШЕ, чем человек попросил. Ровно так
    руководитель выбрал бы в «Менеджер ▾» Анну, увидел бы полную очередь и
    решил, что это очередь Анны.

    Отдельно про `assignee_id`/`unassigned`: они очереди не просто незнакомы,
    а противоречат ей — в очереди лежат диалоги без ответственного.

    `updated_since` в список НЕ входит: он ничего не сужает по смыслу
    (это догон после reconnect'а, 01 §11.7), и его игнорирование стоит лишнего
    трафика, но не врёт. Отказ здесь сломал бы догон очереди на живом фронте.
    """
    conflicting = sorted(name for name, value in given.items() if value)
    if not conflicting:
        return
    raise ApiError(
        "validation_error",
        "Эти фильтры неприменимы к вкладке «Входящие»",
        status=400,
        details={
            "fields": [
                {
                    "field": name,
                    "rule": "unsupported_on_tab",
                    "message": "неприменим при tab=inbox",
                }
                for name in conflicting
            ]
        },
    )


async def _publish_change(
    redis: Redis,
    conv: Conversation,
    system_message: Message,
    patch: dict[str, Any],
) -> None:
    """Строго ПОСЛЕ commit'а (08 §8.1 п.4)."""
    await publish_event(
        redis,
        "message:new",
        {
            "conversation_id": str(conv.id),
            "message": convs.message_out(system_message),
            "conversation_patch": patch,
        },
    )
    await publish_event(
        redis,
        "conversation:updated",
        {"conversation_id": str(conv.id), "patch": patch},
    )


# --- чтение ------------------------------------------------------------------


@router.get("/conversations")
async def list_conversations(
    tab: str = Query("all", description="my | all | new | closed | any | inbox (15 §2.1)"),
    q: str | None = Query(None),
    # Состояние диалога — измерение, ортогональное вкладке: вкладка отвечает
    # «чей диалог», статус — «в каком он состоянии». Заданный явно, он
    # переопределяет умолчание вкладки «кроме закрытых».
    # Регулярка собрана из общего словаря, а не выписана третьей копией:
    # именно она отвергала бы новые значения с 400 до того, как их кто-то
    # успел выбрать в фильтре.
    status: str | None = Query(None, pattern=STATUS_FILTER_PATTERN),
    account_id: uuid.UUID | None = Query(None),
    assignee_id: uuid.UUID | None = Query(None),
    # «Без ответственного» в фильтре «Менеджер ▾» у руководителя (11 §2.5.2).
    unassigned: bool = Query(False),
    unread_only: bool = Query(False),
    # «Ждут ответа» — переход по числу «не отвечено» с вкладки (жалоба 02.09).
    waiting_only: bool = Query(False),
    tag: str | None = Query(None),
    updated_since: datetime | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if tab == INBOX_TAB:
        # Очередь «Входящие» (15 §2.1). Отличается у неё только выборка строк,
        # поэтому в сервис очереди уходит ровно она — пагинация, применение
        # read-маркеров и форма ответа ниже остаются общими (см. шапку модуля).
        _reject_inbox_filters(
            assignee_id=assignee_id,
            unassigned=unassigned,
            q=q,
            unread_only=unread_only,
            # Во «Входящих» ждут ВСЕ по определению: фильтр «ждут ответа» там
            # не сузил бы ничего, а молча проигнорированный — показал бы полную
            # очередь под видом отфильтрованной.
            waiting_only=waiting_only,
            tag=tag,
            # Статус очереди незнаком: в ней лежат и «новые», и «в работе»
            # (диалог мог вернуться от бота), а «закрытых» не бывает вовсе.
            # Молча проигнорировать значит показать БОЛЬШЕ, чем просили.
            status=status,
        )
        # Импорт локальный сознательно: этот модуль монтируется в main.py без
        # защиты, и сломанный/отсутствующий сервис очереди на верхнем уровне
        # унёс бы вместе с собой ВЕСЬ список диалогов — вкладки «Мои», «Все»,
        # «Новые» и «Закрытые», которые к очереди отношения не имеют.
        from app.services import inbox as inbox_svc  # noqa: PLC0415

        items, total = await inbox_svc.list_inbox(
            db,
            user,
            inbox_svc.InboxFilters(account_id=account_id, limit=limit, offset=offset),
        )
    else:
        items, total = await convs.list_conversations(
            db,
            user,
            tab=tab,
            q=q,
            status=status,
            account_id=account_id,
            assignee_id=assignee_id,
            unassigned=unassigned,
            unread_only=unread_only,
            waiting_only=waiting_only,
            tag=tag,
            updated_since=updated_since,
            limit=limit,
            offset=offset,
            transferred=await convs.transferred_ids(redis, user.id),  # ⚑ (01 §5.1)
        )
    # «Прочитано» — состояние человека, а не диалога (01 §5.1): бейдж считаем
    # от пер-юзерного маркера. Диалоги без маркера сохраняют счётчик из колонки.
    items = await read_markers.apply_unread_counts(db, redis, user.id, items)
    return {"items": items, "page": {"limit": limit, "offset": offset, "total": total}}


@router.get("/conversations/counts")
async def conversations_counts(
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Числа для вкладок «Мои» и «Все»: сколько всего и сколько ждут ответа.

    Объявлена ВЫШЕ `/conversations/{conversation_id}` намеренно — по той же
    причине, что и «table»: FastAPI сопоставляет маршруты по порядку, и при
    обратном порядке «counts» уехало бы в параметр пути и вернуло 422 на
    непохожий на uuid идентификатор.

    Право то же, что и на чтение списка: числа не показывают ничего, чего
    смотрящий не увидел бы, открыв вкладку.
    """
    return await convs.tab_counts(db, user)


@router.get("/conversations/table")
async def conversations_table(
    status: str | None = Query(None, pattern=STATUS_FILTER_PATTERN),
    account_id: uuid.UUID | None = Query(None),
    assignee_id: uuid.UUID | None = Query(None),
    tag: str | None = Query(None),
    bot_active: bool | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    q: str | None = Query(None),
    sort: str = Query(table.DEFAULT_SORT),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    limit: int = Query(table.PAGE_LIMIT_DEFAULT, ge=1, le=table.PAGE_LIMIT_MAX),
    offset: int = Query(0, ge=0),
    user: User = Depends(table_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Таблица диалогов с метриками (план 7.3).

    ОБЪЯВЛЕНА ВЫШЕ `/conversations/{conversation_id}` намеренно: FastAPI
    сопоставляет маршруты по порядку, и при обратном порядке «table» уехало бы
    в параметр `conversation_id` и упало на разборе uuid. Ошибка была бы
    неочевидной — 422 про «неверный uuid» на совершенно правильном адресе.
    """
    page = await table.query_table(
        db,
        table.TableFilters(
            status=status,
            account_id=account_id,
            assignee_id=assignee_id,
            tag=tag,
            bot_active=bot_active,
            date_from=date_from,
            date_to=date_to,
            q=q,
        ),
        sort=sort,
        direction=direction,  # type: ignore[arg-type]
        limit=limit,
        offset=offset,
        # Сортировка по метрике берёт ключ из витрины статистики, пока известно,
        # когда её пересчитали (`conversation_table._first_response_sort_expr`).
        mv_refreshed_at=(
            await stats_svc.refreshed_moment(redis) if sort in table.METRIC_SORTABLE else None
        ),
    )
    return page


@router.post("/conversations/table/export", status_code=202)
async def start_table_export(
    status: str | None = Query(None, pattern=STATUS_FILTER_PATTERN),
    account_id: uuid.UUID | None = Query(None),
    assignee_id: uuid.UUID | None = Query(None),
    tag: str | None = Query(None),
    bot_active: bool | None = Query(None),
    date_from: datetime | None = Query(None),
    date_to: datetime | None = Query(None),
    q: str | None = Query(None),
    sort: str = Query(table.DEFAULT_SORT),
    direction: str = Query("desc", pattern="^(asc|desc)$"),
    user: User = Depends(table_perm),
    redis: Redis = Depends(get_redis),
) -> dict[str, str]:
    """Та же таблица целиком — CSV для Excel, собирает воркер (проверка 24.09).

    Фильтры и сортировка те же, что у экранной ручки: выгрузка, отличающаяся
    от увиденного, заставляет решить, что врёт экран. Ответ — `202 {job_id}`;
    статус и подписанная ссылка — `GET .../export/{job_id}`. Отказы: `409`,
    пока идёт другая выгрузка этого человека, и `429` за пределом суточной
    квоты — общие с выгрузкой статистики (`services/dialogs_export.py`).
    """
    job_id = await dialogs_export.create_job(
        redis,
        user_id=user.id,
        filters=table.TableFilters(
            status=status,
            account_id=account_id,
            assignee_id=assignee_id,
            tag=tag,
            bot_active=bot_active,
            date_from=date_from,
            date_to=date_to,
            q=q,
        ),
        sort=sort,
        direction=direction,
    )
    return {"job_id": job_id}


@router.get("/conversations/table/export/{job_id}")
async def table_export_status(
    job_id: str,
    user: User = Depends(table_perm),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Статус выгрузки — только автору; чужая и несуществующая неотличимы (404)."""
    return await stats_svc.export_status(redis, job_id, user_id=user.id)


@router.get("/conversations/table/export", include_in_schema=False)
async def conversations_table_export_moved(_: User = Depends(table_perm)) -> None:
    """Прежняя синхронная выгрузка (до 24.09).

    Вкладка, открытая до выкатки, всё ещё зовёт этот адрес. Без ручки она
    получила бы 405 и тост «Запрос завершился с кодом 405»; с ней — слова о
    том, что делать.
    """
    raise ApiError(
        "gone",
        "Выгрузка теперь собирается в фоне — обновите страницу и нажмите «Выгрузить CSV» ещё раз",
        status=410,
    )


# --- позвать коллегу в диалог (docs/19) -------------------------------------


class InviteRequest(BaseModel):
    user_id: uuid.UUID
    reason: str | None = Field(default=None, max_length=MAX_COMMENT_LEN)


@router.post("/conversations/{conversation_id}/participants")
async def invite_participant(
    conversation_id: uuid.UUID,
    body: InviteRequest,
    user: User = Depends(require_permission("messages:send")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Позвать коллегу, НЕ отдавая диалог.

    Ответственный не меняется — за клиента по-прежнему отвечает тот, кто вёл.
    Позванный получает уведомление и видит диалог в своих «Моих».
    """
    conv = await convs.get_conversation_for_update(db, conversation_id)
    who = await convs.resolve_assignee(db, body.user_id)
    if who is None:
        raise ApiError("not_found", "Сотрудник не найден", status=404)

    before = set(await participants.participant_ids(db, conv.id))
    await participants.invite(db, conv, who=who, actor=user, reason=body.reason)
    added = who.id not in before

    notified = None
    if added:
        msg = convs.add_system_message(
            db,
            conv,
            f"{user.full_name} позвал(а) в диалог: {who.full_name}"
            + (f". {body.reason}" if body.reason else ""),
        )
        await write_audit(
            db,
            user_id=user.id,
            action="conversation.participant_invited",
            entity="conversation",
            entity_id=str(conv.id),
            details={"user_id": str(who.id), "reason": bool(body.reason)},
        )
        # Уведомление обязательно: без него зовут в пустоту. Диалог не
        # становится «его» — он лишь появляется в «Моих», а это заметно
        # только тому, кто туда смотрит именно сейчас.
        notified = await notifications.notify(
            db,
            kind="conversation.invited",
            recipient_id=who.id,
            body=(
                f"{user.full_name} просит посмотреть" + (f": {body.reason}" if body.reason else "")
            ),
            entity_type="conversation",
            entity_id=str(conv.id),
        )
    else:
        msg = None

    await db.commit()
    if notified is not None:
        await notifications.deliver(redis, notified)
    if msg is not None:
        await publish_event(
            redis,
            "message:new",
            {"conversation_id": str(conv.id), "message": convs.message_out(msg)},
        )

    return {"participants": await participants.view(db, conv.id)}


@router.post("/conversations/{conversation_id}/enter")
async def enter_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(require_permission("messages:send")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Я ОТКРЫЛ чужой рабочий диалог — покажи его и в моих «Моих».

    ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 03.09: «сделай так, чтобы можно было спокойно заходить
    в чужой диалог, который в работе у другого человека, и он появлялся так же
    у тебя в „Мои"… чтобы оба человека понимали, что у кого в работе».

    ⚠ ТИХО. Ни уведомления, ни строки в ленте, ни звука, ни записи в журнал:
    открытие чужого диалога — не событие, о котором стоит будить тринадцать
    человек. Соседняя ручка «позвать» шумит намеренно и правильно: там одного
    человека зовёт другой. Пройди вход через неё — на каждое открытие летело бы
    уведомление самому себе и строка «Иванов позвал(а) в диалог: Иванов».

    ⚠ ОТВЕЧАЕТ ПО-ПРЕЖНЕМУ ОДИН. Вход кладёт диалог в список, но не делает
    вошедшего ответственным и не даёт ему прав «своего»: закрепить чужой диалог
    может только позванный (`services/pins.py`), а число «ждут вашего ответа»
    считает то, за что человек отвечает (`own_condition`).

    Повтор безопасен: составной ключ не даст завести вторую строку, а свой
    диалог и вовсе ничего не пишет.
    """
    conv = await convs.get_conversation_for_update(db, conversation_id)
    вошёл = await participants.enter(db, conv, who=user)
    if вошёл:
        await db.commit()
        # Кадр — только тем, у кого этот диалог открыт: хозяин видит, что
        # рядом появился коллега, и это ровно то, о чём просил владелец
        # («чтобы оба понимали»). Списки при этом не трогаем — «Мои»
        # обновятся обычным перезапросом у самого вошедшего.
        await publish_event(
            redis,
            "conversation:participants",
            {
                "conversation_id": str(conv.id),
                "participants": await participants.view(db, conv.id),
            },
        )
    return {"entered": вошёл, "participants": await participants.view(db, conv.id)}


@router.delete("/conversations/{conversation_id}/participants/{user_id}")
async def remove_participant(
    conversation_id: uuid.UUID,
    user_id: uuid.UUID,
    user: User = Depends(require_permission("messages:send")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Выйти самому или убрать позванного.

    Кто угодно из участвующих: позвавший передумал, позванный посмотрел и
    закончил. Спрашивать разрешения не у кого — ответственность всё это время
    лежала на одном человеке, и она не менялась.
    """
    conv = await convs.get_conversation_for_update(db, conversation_id)
    # Вид участия — ДО удаления: после него строки нет и спросить не у кого.
    сам_зашёл = await participants.is_guest(db, conv.id, user_id) and user_id == user.id
    removed = await participants.leave(db, conv, user_id=user_id)
    if removed and сам_зашёл:
        # ⚠ ЗАШЁЛ ТИХО — ТИХО И ВЫШЕЛ (04.09). Вход не пишет в ленту ни строки
        # (см. `participants.enter`), и запись о выходе оказалась бы новостью о
        # событии, начала которого никто не видел: хозяин читает «Иванов вышел
        # из диалога», не зная, что Иванов туда заходил. Приглашение — другое
        # дело: его объявляли, и уход из него объявляют тоже.
        await db.commit()
    elif removed:
        who = await db.get(User, user_id)
        name = who.full_name if who else "сотрудник"
        text = (
            f"{name} вышел(а) из диалога"
            if user_id == user.id
            else f"{user.full_name} убрал(а) из диалога: {name}"
        )
        msg = convs.add_system_message(db, conv, text)
        await db.commit()
        await publish_event(
            redis,
            "message:new",
            {"conversation_id": str(conv.id), "message": convs.message_out(msg)},
        )
    return {"participants": await participants.view(db, conv.id)}


# --- передача с подтверждением (требование заказчика от 7 августа) ----------


async def _transfer_reply(
    db: AsyncSession,
    redis: Redis,
    conv: Conversation,
    system_message: Message,
    *,
    notify_user_id: uuid.UUID | None,
    event: str,
    notified: Sequence[notifications.NotifyResult] = (),
    outcome: str,
    actor: User,
) -> dict[str, Any]:
    """Общий хвост принятия, отказа и отмены: commit, кадры, ответ.

    Кадров два, и оба обязательны. Системная запись рисуется в открытой
    ленте, а `conversation:updated` чинит строку списка у ОБОИХ участников:
    у одного диалог появляется в «Моих», у другого исчезает пометка «ждёт
    подтверждения». Без второго кадра передающий продолжал бы видеть
    «отправлено» до перезагрузки страницы.

    ⚠ `notified` — ИТОГ `notify`, КОТОРЫЙ НАДО ДОСТАВИТЬ. Запись центра
    уведомлений ложится в ту же транзакцию, что и сам отказ, а кадр в браузер
    обязан уйти строго ПОСЛЕ commit'а — иначе человек получит весть о событии,
    которого в базе ещё нет. Отказ от передачи этот шаг терял: строка в базе
    появлялась, а тоста и обновления колокольчика не было ни у кого. Соседние
    ручки в этом же файле делают ровно так, как здесь.
    """
    await db.commit()
    for result in notified:
        await notifications.deliver(redis, result)
    detail = await convs.conversation_detail(db, conv)
    patch = _conversation_patch(detail) | {"transfer": detail.get("transfer")}
    await publish_event(
        redis,
        "message:new",
        {
            "conversation_id": str(conv.id),
            "message": convs.message_out(system_message),
            "conversation_patch": patch,
        },
    )
    await publish_event(
        redis,
        event,
        {
            "conversation_id": str(conv.id),
            "patch": patch,
            # Кому это персонально важно: хаб подставит `is_for_you`, и
            # только у него зазвенит.
            "for_user_id": str(notify_user_id) if notify_user_id else None,
            # Чем кончилось предложение — для Живой ленты: по одной заплатке
            # `transfer: null` принятие, отказ и отмену не различить, и лента
            # о них молчала (проверка 24.09).
            "transfer_outcome": outcome,
            "transfer_actor": {"id": str(actor.id), "full_name": actor.full_name},
        },
    )
    return detail


async def _write_notices(
    db: AsyncSession, conv: Conversation, notices: Sequence[transfer_svc.Notice]
) -> list[notifications.NotifyResult]:
    """Вести о судьбе предложения — в ту же транзакцию, что и само решение."""
    return [
        await notifications.notify(
            db,
            kind=n.kind,
            recipient_id=n.recipient_id,
            title=n.title,
            body=n.body,
            entity_type="conversation",
            entity_id=str(conv.id),
            dedup_key=n.dedup_key,
        )
        for n in notices
    ]


async def _settle_offer_notice(db: AsyncSession, conv: Conversation) -> None:
    """Погасить «Вам передали диалог»: предложение решено, и строка с ним в
    колокольчике получателя больше не зовёт к действию."""
    await notifications.resolve_for_entity(
        db,
        entity_type="conversation",
        entity_id=str(conv.id),
        kinds=(transfer_svc.OFFERED,),
    )


async def _drop_stale_offer(
    db: AsyncSession, redis: Redis, conv: Conversation, *, recipient_id: uuid.UUID
) -> None:
    """Сохранить снятие устаревшего предложения, которое сделал ``accept``.

    Без этого снятие откатывалось вместе с отказом: кнопка «Принять» висела до
    таймаута и на каждое нажатие отвечала тем же отказом.
    """
    await _settle_offer_notice(db, conv)
    await db.commit()
    await convs.clear_transferred(redis, recipient_id, conv.id)
    detail = await convs.conversation_detail(db, conv)
    await publish_event(
        redis,
        "conversation:updated",
        {
            "conversation_id": str(conv.id),
            "patch": _conversation_patch(detail) | {"transfer": None},
        },
    )


@router.post("/conversations/{conversation_id}/transfer/accept")
async def accept_transfer(
    conversation_id: uuid.UUID,
    user: User = Depends(require_permission("messages:send")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Принять переданный диалог — только тот, кому его предложили.

    До этого момента диалог числится за передающим: ответственность не может
    повиснуть в воздухе между двумя людьми.
    """
    from app.services import transfer as transfer_svc

    conv = await convs.get_conversation_for_update(db, conversation_id)
    try:
        previous_id = transfer_svc.accept(conv, actor=user)
    except ApiError as exc:
        if (exc.details or {}).get("reason") in transfer_svc.STALE_REASONS:
            await _drop_stale_offer(db, redis, conv, recipient_id=user.id)
        raise
    msg = convs.add_system_message(db, conv, f"Диалог принят: {user.full_name}")
    await write_audit(
        db,
        user_id=user.id,
        action="conversation.transfer_accepted",
        entity="conversation",
        entity_id=str(conv.id),
        details={"from_id": str(previous_id) if previous_id else None},
    )
    # У диалога новый хозяин: «Клиент ждёт вашего ответа» прежнего владельца и
    # «Вам передали диалог» получателя больше не про них.
    await notifications.resolve_conversation(db, conv.id)
    await _settle_offer_notice(db, conv)
    return await _transfer_reply(
        db,
        redis,
        conv,
        msg,
        notify_user_id=previous_id,
        event="conversation:updated",
        outcome="accepted",
        actor=user,
    )


@router.post("/conversations/{conversation_id}/transfer/decline")
async def decline_transfer(
    conversation_id: uuid.UUID,
    user: User = Depends(require_permission("messages:send")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Отказаться от передачи. Диалог остаётся у того, кто передавал.

    Передавшему это сообщается обязательно: он ждёт ответа и должен узнать,
    что диалог по-прежнему его. Молчаливый отказ означал бы клиента, которого
    оба считают чужим.
    """
    conv = await convs.get_conversation_for_update(db, conversation_id)
    owner_id = conv.transfer_from_id or conv.transfer_by_id
    offered_at = conv.transfer_at
    offered_by = transfer_svc.decline(conv, actor=user)
    msg = convs.add_system_message(db, conv, f"Отказ от передачи: {user.full_name}")
    await write_audit(
        db,
        user_id=user.id,
        action="conversation.transfer_declined",
        entity="conversation",
        entity_id=str(conv.id),
        details={"offered_by_id": str(offered_by) if offered_by else None},
    )
    # Узнать об отказе обязаны и владелец (диалог по-прежнему его, клиент ждёт),
    # и предлагавший, если это был не владелец. Весть — в ту же транзакцию.
    owner = await db.get(User, owner_id) if owner_id else None
    notices = transfer_svc.outcome_notices(
        "conversation.transfer_declined",
        conv_id=conv.id,
        offered_at=offered_at,
        owner_id=owner_id,
        offered_by=offered_by,
        owner_name=owner.full_name if owner else "прежним сотрудником",
        what=f"{user.full_name} не принял(а) диалог.",
    )
    notified = await _write_notices(db, conv, [n for n in notices if n.recipient_id != user.id])
    await _settle_offer_notice(db, conv)
    return await _transfer_reply(
        db,
        redis,
        conv,
        msg,
        notify_user_id=offered_by,
        event="conversation:updated",
        notified=notified,
        outcome="declined",
        actor=user,
    )


@router.post("/conversations/{conversation_id}/transfer/cancel")
async def cancel_transfer(
    conversation_id: uuid.UUID,
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Отменить своё предложение передачи (просьба владельца 04.09).

    «Нужно будет сделать, чтобы можно было отменять передачу, если я случайно
    начал передавать не тому человеку». Обратная половина — отказ получателя —
    была с 7 августа, а забрать ошибку назад передающий не мог ничем: сиди и
    жди, пока посторонний человек откажется, или проси его в мессенджере.
    Пятнадцать минут диалог при этом числится за тобой, и клиент ждёт.

    ⚠ ДВЕРЬ — `conversations:read`, А ПРАВИЛО ВНУТРИ (`transfer.cancel`), И ЭТО
    НЕ НЕБРЕЖНОСТЬ. Напрашивалось `messages:send`, как у accept/decline, но
    руководитель отправлять не может, а ПЕРЕДАВАТЬ может: `/assign` спрашивает
    `conversations:manage`, которое у него есть. Повесь мы отмену на отправку —
    единственный человек, чью передачу нельзя отменить, оказался бы тот, кто её
    сделал. Наблюдатель сюда доходит и получает честные 403 от правила: своих
    передач у него не бывает, `conversations:manage` тоже нет.
    """
    conv = await convs.get_conversation_for_update(db, conversation_id)
    offered_at = conv.transfer_at
    offered_to, offered_by = transfer_svc.cancel(
        conv, actor=user, may_manage_any=has_permission(user, "conversations:manage")
    )
    msg = convs.add_system_message(db, conv, f"Передача отменена: {user.full_name}")
    await write_audit(
        db,
        user_id=user.id,
        action="conversation.transfer_cancelled",
        entity="conversation",
        entity_id=str(conv.id),
        details={
            "to_id": str(offered_to) if offered_to else None,
            "by_id": str(offered_by) if offered_by else None,
        },
    )
    # ⚠ ПОЛУЧАТЕЛЮ ОБЯЗАНЫ СКАЗАТЬ, что от него больше ничего не ждут. Ему уже
    # пришло «Вам передали диалог» (`conversation.assigned` из `/assign`), и без
    # второй вести он останется с открытым долгом: диалога нет ни в его «Моих»,
    # ни в очереди — проверить, жив ли ещё запрос, ему негде. Кадр эту работу не
    # делает: он живёт секунду и не переживает ни обрыва связи, ни закрытой
    # вкладки, а отменяют передачу как раз тогда, когда человек ещё не смотрел.
    #
    # Кладём в ТУ ЖЕ транзакцию (`notify`, не `notify_now`), как соседний отказ:
    # отмена и весть о ней обязаны попасть в базу вместе.
    notified: list[notifications.NotifyResult] = []
    if offered_to is not None and offered_to != user.id:
        notified = await _write_notices(
            db,
            conv,
            [
                transfer_svc.withdrawn_notice(
                    conv_id=conv.id,
                    offered_at=offered_at,
                    recipient_id=offered_to,
                    title="Передачу отменили — принимать нечего",
                    body=(
                        f"{user.full_name} отменил(а) передачу диалога. "
                        "Отвечать по нему не нужно: он остался за прежним сотрудником."
                    ),
                )
            ],
        )
    await _settle_offer_notice(db, conv)
    if offered_to is not None:
        # ⚑ «вам передали» в списке получателя (11 §2.1) гаснет только при
        # открытии диалога. Отказ и принятие через это открытие и проходят, а
        # отмена — нет: получатель в неё не вовлечён вовсе, и флажок остался бы
        # висеть на диалоге, предложения по которому больше нет.
        #
        # Гасим ДО commit'а намеренно. После него между записью и снятием
        # флажка уезжает кадр `conversation:updated`, список по нему
        # перезапрашивается — и вернулся бы с ⚑ ещё раз, теперь уже до конца
        # смены. Цена обратного порядка — потерянный флажок, если commit
        # упадёт; цена прямого — вечная пометка о несуществующей передаче.
        await convs.clear_transferred(redis, offered_to, conv.id)
    return await _transfer_reply(
        db,
        redis,
        conv,
        msg,
        notify_user_id=offered_to,
        event="conversation:updated",
        notified=notified,
        outcome="cancelled",
        actor=user,
    )


@router.get("/conversations/{conversation_id}")
async def get_conversation_detail(
    conversation_id: uuid.UUID,
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    conv = await convs.get_conversation(db, conversation_id)
    flagged = str(conv.id) in await convs.transferred_ids(redis, user.id)
    detail = await convs.conversation_detail(db, conv, transferred_to_me=flagged, viewer=user)
    detail["unread_count"] = await read_markers.unread_for_conversation(db, redis, user.id, conv)
    if flagged:
        # «⚑ гаснет после открытия диалога» (11 §2.1) — отдаём флаг в этом
        # ответе и снимаем: следующий запрос списка уже без флажка.
        await convs.clear_transferred(redis, user.id, conv.id)

    # ⚠ ОТКРЫЛИ ДИАЛОГ — САМОЕ ВРЕМЯ СПРОСИТЬ ПРО ПРОФИЛЬ КЛИЕНТА.
    #
    # Обогащение добирает ссылку на входящем сообщении, и для живой переписки
    # этого хватает. Но у закрытого диалога входящих больше не будет НИКОГДА —
    # значит и ссылка там не появится никогда, а смотрят такие карточки как раз
    # при разборе: «кто это был, что за человек». Жалоба владельца 02.09 пришла
    # ровно с такой карточки — диалог закрыт 28 августа.
    #
    # Открытие диалога — честный повод: спрашиваем ровно про тех, на кого
    # человек СЕЙЧАС смотрит, а не обходим 56 тысяч карточек, из которых
    # откроют десяток. Повод гаснет после первого вопроса (`profile_checked_at`),
    # поэтому на второе открытие того же диалога запроса уже не будет.
    #
    # Задача идёт в очередь, ответ её не ждёт: карточка дорисуется кадром
    # `conversation:updated` через секунду, а не задержит открытие.
    if conv.client_id is not None:
        клиент = await db.get(Client, conv.client_id)
        if клиент is not None and клиент.profile_checked_at is None:
            await enqueue_enrich_client(redis, conv.id)
    return detail


@router.get("/conversations/{conversation_id}/client-history")
async def get_client_history(
    conversation_id: uuid.UUID,
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Прошлые диалоги этого клиента для правой карточки (01 §5.6)."""
    conv = await convs.get_conversation(db, conversation_id)
    return await convs.client_history(db, conv)


@router.get("/conversations/{conversation_id}/messages")
async def list_messages(
    conversation_id: uuid.UUID,
    before: str | None = Query(None),
    after: str | None = Query(None),
    limit: int = Query(50, ge=1, le=convs.MAX_MESSAGES_LIMIT),
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    conv = await convs.get_conversation(db, conversation_id)
    return await convs.list_messages(
        db,
        conv,
        before=before,
        after=after,
        limit=limit,
        # Заметки отдаём по праву `notes:read` (01 §6.1, §13): у observer его
        # нет — фильтр в запросе, а не в UI, поэтому заметка не уезжает даже
        # в курсорах и счётчиках has_more. То же правило у WS (Hub, 08 §5.3).
        include_notes=has_permission(user, "notes:read"),
        # Служебные записи Авито — по настройке, одной на систему. Фильтр здесь, а
        # не в интерфейсе, по той же причине, что и у заметок: иначе курсоры и
        # `has_more` считали бы скрытое, и лента подгружалась бы пустыми страницами.
        include_avito_system=not bool(await app_settings.get(db, app_settings.AVITO_SYSTEM_HIDDEN)),
    )


@router.post("/conversations/{conversation_id}/read", status_code=204)
async def mark_read(
    conversation_id: uuid.UUID,
    user: User = Depends(read_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> Response:
    """Отметить диалог прочитанным ТЕКУЩИМ пользователем (01 §5.3).

    Маркер пер-юзерный (`read:{user_id}` в Redis), колонка
    `conversations.unread_count` намеренно не трогается: она осталась
    fallback'ом для тех, у кого маркера ещё нет. Раньше здесь обнулялся
    глобальный счётчик и кадр уезжал ВСЕМ — руководитель, заглянувший в чужую
    переписку, гасил бейдж менеджеру, который её не читал.
    """
    conv = await convs.get_conversation(db, conversation_id)
    before = await read_markers.get_marker(redis, user.id, conv.id)
    after = await read_markers.mark_read(db, redis, user.id, conv)
    await convs.clear_transferred(redis, user.id, conv.id)  # диалог открыт — ⚑ гаснет
    if before != after:  # идемпотентно: повторный /read кадров не плодит
        # Синхронизация ТОЛЬКО своих вкладок (десктоп + веб одного человека).
        await publish_event(
            redis,
            "conversation:updated",
            {"conversation_id": str(conv.id), "patch": {"unread_count": 0}},
            only_user=str(user.id),
        )
    return Response(status_code=204)


# --- управление (conversations:manage) ---------------------------------------


@router.patch("/conversations/{conversation_id}/status")
async def patch_status(
    conversation_id: uuid.UUID,
    body: StatusPatch,
    user: User = Depends(manage_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """PATCH /conversations/{id}/status (01 §5.4) → объект диалога."""
    conv = await convs.get_conversation_for_update(db, conversation_id)
    if body.status == "closed" and transfer_svc.is_pending(conv):
        # Закрытие молча отменило бы передачу: получатель остался бы с кнопкой
        # «Принять», а передающий — в уверенности, что отдал диалог.
        raise ApiError(
            "unprocessable",
            "Диалог ждёт ответа на передачу. Отмените передачу или дождитесь ответа коллеги",
            status=422,
            details={"reason": "transfer_pending"},
        )
    # Кого закрытие затронет — читаем ДО смены статуса: закрытие ответственного
    # не снимает, но полагаться на это в чужом коде значит завести четвёртое
    # место, которое сломается от правки в первом.
    прежний_хозяин = conv.assignee_id
    system_message = await convs.change_status(db, conv, new_status=body.status, actor=user)

    # ⚠ ХОЗЯИН УЗНАЁТ, ЧТО ЕГО ДИАЛОГ ЗАКРЫЛ КОЛЛЕГА (просьба владельца 03.09).
    #
    # Кадры о закрытии веерные и одинаковые для всех: отличить «закрыл я» от
    # «закрыл коллега» по ним нельзя. Без этой строки человек видит только, что
    # поле заперлось, диалог ушёл из «Моих», а признак «в работе у вас» погас, —
    # и читает это как поломку.
    #
    # Своё закрытие молчит: иначе сообщение получили бы все обычные закрытия
    # (14 639 за месяц против 16 чужих), и вот ЭТО была бы помеха.
    закрыл_коллега = None
    if body.status == "closed" and прежний_хозяин is not None and прежний_хозяин != user.id:
        закрыл_коллега = await notifications.notify(
            db,
            kind="conversation.closed_by_other",
            body=f"{user.full_name} закрыл(а) диалог, который вели вы",
            recipient_id=прежний_хозяин,
            entity_id=str(conv.id),
        )
    await db.commit()
    if закрыл_коллега is not None:
        await notifications.deliver(redis, закрыл_коллега)

    detail = await convs.conversation_detail(db, conv)
    await _publish_change(redis, conv, system_message, _conversation_patch(detail))
    if conv.assignee_id is None and detail.get("in_inbox"):
        # closed→in_progress у ничейного диалога возвращает его в очередь, но
        # conversation:updated строк во «Входящие» не вставляет — без этого
        # кадра клиент ждал невидимым (аудит синхронизации 16.08; образец —
        # ветка released в assign_conversation)
        from app.services import inbox as inbox_svc  # noqa: PLC0415 — как в списке выше

        await publish_inbox_released(
            redis,
            conversation=detail,
            released_by=user_ref(user),
            offered_at=detail.get("offered_at"),
            conversation_patch=_conversation_patch(detail),
            # Без списка допущенных кадр по правилу совместимости уедет каждому
            # подключённому — строка чужого канала со звонком (см. помощник).
            eligible_operator_ids=await inbox_svc.eligible_operator_ids(db, conv),
        )
    return detail


# ЗДЕСЬ БЫЛИ `POST` И `DELETE /conversations/{id}/snooze` — «Отложить до…» и
# «Вернуть сейчас» (docs/38 §7). Сняты 12 августа решением владельца вместе со
# всем статусом «Отложен». Обе ручки ходили в `change_status`, и после снятия
# статуса им нечего было бы туда передать.


@router.post("/conversations/{conversation_id}/assign")
async def assign_conversation(
    conversation_id: uuid.UUID,
    body: AssignRequest,
    user: User = Depends(manage_perm),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """POST /conversations/{id}/assign (01 §5.5) → {conversation, system_message}."""
    # Диалог сначала: несуществующий id должен дать 404, а не 422 по получателю.
    conv = await convs.get_conversation_for_update(db, conversation_id)
    # «Стоял ли он в очереди ДО передачи» — снимаем здесь, потому что после
    # записи ответ на этот вопрос уже другой (7.1, см. публикацию ниже).
    from app.services import inbox as inbox_svc  # noqa: PLC0415 — как в списке выше

    was_waiting = inbox_svc.is_waiting(conv)
    # ⚠ ТОЛЬКО ТОМУ, КТО В СЕТИ (решение владельца 28.08). Довод и все оговорки —
    # в докстринге `resolve_assignee`; здесь важно, что проверка стоит на СЕРВЕРЕ,
    # а не только в списке окна: список грузится один раз при открытии, и человек
    # успевает уйти домой, пока оператор дописывает комментарий.
    assignee = await convs.resolve_assignee(
        db, body.assignee_id, redis=redis, require_online=True, actor_id=user.id
    )
    pending_to, pending_at = conv.transfer_to_id, conv.transfer_at
    system_message = await convs.assign_conversation(
        db, conv, assignee=assignee, actor=user, comment=body.comment
    )
    offered = transfer_svc.is_pending(conv)

    notified: list[notifications.NotifyResult] = []
    # Прежнее предложение решено этим назначением: переадресовано, перезаписано
    # или снято прямым назначением. Его получатель обязан узнать, что принимать
    # больше нечего, иначе у него висят кнопки, которые всегда отказывают.
    if pending_to is not None and (conv.transfer_to_id, conv.transfer_at) != (
        pending_to,
        pending_at,
    ):
        await _settle_offer_notice(db, conv)
    displaced = pending_to not in (None, user.id, conv.assignee_id, conv.transfer_to_id)
    if displaced and pending_to is not None:
        if offered:
            title, why = "Передачу переадресовали", "передал(а) диалог другому сотруднику"
        elif assignee is None:
            title, why = "Передачу отменили", "вернул(а) диалог в очередь"
        else:
            title, why = "Передачу отменили", f"назначил(а) диалог: {assignee.full_name}"
        notified += await _write_notices(
            db,
            conv,
            [
                transfer_svc.withdrawn_notice(
                    conv_id=conv.id,
                    offered_at=pending_at,
                    recipient_id=pending_to,
                    title=f"{title} — принимать нечего",
                    body=f"{user.full_name} {why}. Отвечать по нему не нужно.",
                )
            ],
        )

    # Уведомление получателю предложения обязательно: пока он не принял,
    # диалога нет в его «Моих», и узнать о передаче ему больше неоткуда.
    # Ключ склейки — на предложение: повторная передача того же диалога после
    # отказа обязана дойти, а не лечь в прошлую строку.
    if offered and conv.transfer_to_id:
        notified.append(
            await notifications.notify(
                db,
                kind=transfer_svc.OFFERED,
                recipient_id=conv.transfer_to_id,
                body=(
                    f"{user.full_name} передаёт вам диалог"
                    + (f". {body.comment}" if body.comment else "")
                ),
                entity_type="conversation",
                entity_id=str(conv.id),
                dedup_key=transfer_svc.notice_key(transfer_svc.OFFERED, conv.id, conv.transfer_at),
            )
        )

    await db.commit()
    for result in notified:
        await notifications.deliver(redis, result)

    if displaced and pending_to is not None:
        await convs.clear_transferred(redis, pending_to, conv.id)
    if assignee is not None and assignee.id != user.id:
        # ⚑ у получателя — до тех пор, пока он не откроет диалог (11 §2.1)
        await convs.mark_transferred(redis, assignee.id, conv.id)

    detail = await convs.conversation_detail(db, conv)
    # `transfer` едет всегда: у получателя с открытым диалогом по нему
    # появляется полоса «Принять / Отказаться», у прежнего — исчезает.
    patch = _conversation_patch(detail) | {"transfer": detail.get("transfer")}
    await publish_event(
        redis,
        "message:new",
        {
            "conversation_id": str(conv.id),
            "message": convs.message_out(system_message),
            "conversation_patch": patch,
        },
    )
    await publish_event(
        redis,
        "conversation:assigned",
        {
            "conversation_id": str(conv.id),
            # хаб дополняет кадр персональным is_for_you (01 §11.3, 08 §5.3)
            "assignee": user_ref(assignee),
            "assigned_by": user_ref(user),
            "comment": body.comment,
            # Предложение ждёт «Принять / Отказаться»; прямое назначение — нет.
            "offer": offered,
        },
    )
    await publish_event(
        redis,
        "conversation:updated",
        {"conversation_id": str(conv.id), "patch": patch},
    )
    # Передача — это ещё и способ РАЗОБРАТЬ очередь (01 §5.5): руководитель
    # видит, что диалог никто не берёт, и назначает его руками. Для остальных
    # операторов это ровно то же событие, что чужое «Принять», и кадр обязан
    # быть тем же — иначе строка останется в двенадцати очередях, и следующий
    # клик по «Принять» вернёт 409 по диалогу, где уже идёт разговор.
    if was_waiting and assignee is not None:
        await publish_event(
            redis,
            INBOX_CLAIMED,
            {
                "conversation_id": str(conv.id),
                "claimed_by": user_ref(assignee),
                "claimed_at": None,  # диалог не приняли, а вручили — хронометража нет
                "waited_seconds": None,
                "conversation_patch": patch,
            },
        )
    elif assignee is None and detail["in_inbox"]:
        # Обратное действие: сняли ответственного — диалог вернулся в очередь и
        # обязан появиться у всех. Кадр несёт диалог ЦЕЛИКОМ: у того, кто
        # подключился после его исчезновения, этой строки нет вовсе.
        await publish_inbox_released(
            redis,
            conversation=detail,
            released_by=user_ref(user),
            offered_at=detail["offered_at"],
            conversation_patch=patch,
            eligible_operator_ids=await inbox_svc.eligible_operator_ids(db, conv),
        )
    return {"conversation": detail, "system_message": convs.message_out(system_message)}


# --- закрепить у себя (требование заказчика от 7 августа) --------------------


@router.post("/conversations/{conversation_id}/bot/mute")
async def take_conversation_from_bot(
    conversation_id: uuid.UUID,
    user: User = Depends(require_permission("messages:send")),
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Забрать диалог у бота, ничего не написав клиенту (запрос владельца 29.08).

    Право то же, что у отправки сообщения: раньше единственным способом
    заглушить бота было ответить клиенту, и кнопка не должна требовать больше
    прав, чем путь, который она заменяет.

    Повторное нажатие — не ошибка: кнопка одна и та же, а «бот и так молчал»
    это нормальный исход, а не сбой (тот же довод, что у открепления).
    """
    from app.services.messages import publish_claimed_by_reply, take_over_from_bot

    conv = await convs.get_conversation_for_update(db, conversation_id)
    changed, claimed_from_queue, bot_was_active = await take_over_from_bot(db, conv, user)
    await db.commit()

    if changed:
        detail = await convs.conversation_detail(db, conv)
        patch = _conversation_patch(detail)
        await publish_event(
            redis,
            "conversation:updated",
            {"conversation_id": str(conv.id), "patch": patch},
        )
    if claimed_from_queue:
        # У остальных строка очереди обязана исчезнуть тем же кадром, что и
        # при «Принять»: иначе половина вкладок про это не узнает (7.1).
        await publish_claimed_by_reply(redis, conv, user)
    return {"bot_active": False, "taken": bool(bot_was_active)}


@router.post("/conversations/{conversation_id}/pin")
async def pin_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(require_permission("conversations:read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Закрепить диалог у СЕБЯ.

    Право — «читать диалоги», то есть у всех. Закрепление ничего не меняет ни
    в диалоге, ни у коллег: это отметка человека в собственном списке, и
    спрашивать на неё разрешение не у кого. Ограничения — свои диалоги и
    потолок — живут в сервисе, потому что это правила отметки, а не доступа.
    """
    conv = await convs.get_conversation(db, conversation_id)
    await pins.pin(db, conv, user)
    await db.commit()
    return {"pinned": True}


@router.delete("/conversations/{conversation_id}/pin")
async def unpin_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(require_permission("conversations:read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Открепить. Отсутствие закрепления — не ошибка: кнопка одна и та же."""
    await pins.unpin(db, conversation_id, user.id)
    await db.commit()
    return {"pinned": False}
