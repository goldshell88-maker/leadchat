"""Назначение операторов на каналы Авито (план 7.2, разбор Jivo 15 §2.2).

Зачем это существует. У заказчика девять каналов Авито и тринадцать
операторов, и у каждого канала свой набор людей: «! Парт - 7 / Ист - В43
МНЧ !» ведут одни, «Парт - 723 БЕЛЫЙ» — другие. В Jivo это экран «Назначить
операторов на канал» со списком сотрудников и галочками. Очередь «Входящие»
(7.1) без этой связи показывает всем всё — оператор листает чужие обращения и
берёт не свои.

ГЛАВНОЕ ПРАВИЛО МОДУЛЯ
----------------------
**Канал без назначенных операторов доступен ВСЕМ операторам, а не никому.**

Оно записано ровно один раз — в :func:`visible_accounts_condition` — и оттуда
его берут обе точки фильтрации очереди (``inbox.visible_queue_condition`` и
``inbox.eligible_operator_ids``). Обратное прочтение стоило бы одного деплоя:
таблица назначений пуста, фильтрация включается, очередь у всех тринадцати
пустеет, и обращения виснут молча — без ошибки, без исключения, просто ничего
не приходит. Поэтому у правила есть отдельные тесты, юнитом и на настоящем
PostgreSQL, и трогать его в одном месте, забыв про второе, нельзя физически.

ЧТО СЧИТАЕТСЯ «НАЗНАЧЕННЫМ ОПЕРАТОРОМ»
--------------------------------------
Не любая строка связи, а строка ЖИВОГО оператора: сотрудник активен и его
роль умеет отвечать клиентам (право ``messages:send``, 01 §12). Разница не
теоретическая. Назначили на канал троих, все трое уволились и отключены — при
подсчёте «по строкам» канал остался бы «назначенным», спрятался от всех живых
операторов и его обращения не увидел бы никто. При подсчёте «по живым»
последний ушедший возвращает канал в состояние «открыт всем», то есть в
безопасное. Это же определение обязано быть общим у видимости очереди и у
эскалации «отказались все»: разъезд означал бы, что эскалация ждёт отказа от
людей, которые диалога не видят.

ПОЛНАЯ ЗАМЕНА НАБОРА, А НЕ ДОБАВИТЬ/УДАЛИТЬ
-------------------------------------------
:func:`set_operators` принимает НАБОР целиком — ровно то, что показывает
экран с галочками (и ровно как ``PUT /bots/{id}/accounts``, 01 §8.5). При
инкрементальных операциях два администратора, правящие один канал
одновременно, получают набор, которого не хотел ни один из них: первый снял
Петрова, второй в это же время добавил Сидорова — и «снятие» второго запроса
приходит на список, где Петрова уже нет.

Транзакции — как везде (08 §8.1): функции пишут в переданную сессию и ничего
не коммитят; commit и публикацию кадров делает вызывающий.
"""

import uuid
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import AccountOperator, AvitoAccount, User
from app.services import conversations as convs
from app.services.audit import write_audit
from app.services.user_ref import normalize_department, user_ref_parts
from app.ws.presence import presence_map

log = structlog.get_logger("app.account_operators")

# Сколько аватарок показывает карточка канала до «+N» (как в Jivo: «+4», «+7»).
PREVIEW_LIMIT = 3

AUDIT_ACTION = "account.operators_changed"


# ------------------------------------------------------------ предикаты доступа
#
# Всё, что ниже, — КУСКИ SQL, а не выборки. Фильтрация очереди обязана
# уезжать в базу целиком: очередь бывает длинной, и постфильтрация в Python
# означала бы «прочитать страницу, выбросить половину и отдать неполную».


def _live_operator() -> list[sa.ColumnElement[bool]]:
    """«Живой оператор» — тот, кому система вправе давать диалоги.

    Условие берётся целиком из ``conversations.operator_pool_conditions``, а не
    собирается здесь: тех же выборок в системе четыре, и своя копия разъехалась
    бы с остальными на первой же правке. 7.4 это уже проверил — он добавил в
    условие третью составляющую (``handles_conversations``), и все четыре места
    получили её разом.
    """
    return convs.operator_pool_conditions()


def channel_has_operators(account_id: Any) -> sa.ColumnElement[bool]:
    """EXISTS: у канала есть хотя бы один ЖИВОЙ назначенный оператор.

    ``account_id`` — колонка (коррелированный подзапрос, как в фильтре
    очереди) либо конкретное значение.
    """
    return (
        select(sa.literal(1))
        .select_from(AccountOperator)
        .join(User, User.id == AccountOperator.user_id)
        .where(AccountOperator.account_id == account_id, *_live_operator())
        .exists()
    )


def operator_assigned(account_id: Any, user_id: uuid.UUID) -> sa.ColumnElement[bool]:
    """EXISTS: этот сотрудник назначен на этот канал.

    Join к ``users`` здесь не нужен: спрашивают всегда про того, кто пришёл с
    живым токеном, а роль проверяет вызывающий (``inbox._assert_can_take``).
    Попадание точное — по первичному ключу ``(account_id, user_id)``.
    """
    return (
        select(sa.literal(1))
        .select_from(AccountOperator)
        .where(
            AccountOperator.account_id == account_id,
            AccountOperator.user_id == user_id,
        )
        .exists()
    )


def visible_accounts_condition(account_id: Any, user_id: uuid.UUID) -> sa.ColumnElement[bool]:
    """ЕДИНСТВЕННОЕ место, где записано правило доступа оператора к каналу.

    «Мой канал ИЛИ канал, на который не назначен никто живой». Второе слагаемое
    и есть правило совместимости из шапки модуля.

    Форма — ``OR`` двух ``EXISTS``, а не ``account_id IN (SELECT ...)``,
    сознательно: коррелированный ``EXISTS`` оставляет ведущим сканом частичный
    индекс очереди (``ix_conversations_inbox_wait``, миграция 0007) и
    проверяется для десятков уже отобранных строк. ``IN`` со списком каналов
    даёт планировщику повод собрать хэш по ``conversations`` целиком — на 454
    тысячах диалогов (15 §1) это чтение архива ради счётчика вкладки.
    """
    return sa.or_(
        operator_assigned(account_id, user_id),
        sa.not_(channel_has_operators(account_id)),
    )


def sees_all_channels(user: User) -> bool:
    """Кому назначения не сужают очередь.

    Три случая, и все три — не поблажка, а обязанность:

    * **администратор** — ему нужно видеть всё, включая то, что не разбирает
      никто; он же получает эскалацию «отказались все» и назначает вручную;
    * **руководитель** и **наблюдатель** — они диалоги из очереди не берут
      вовсе (нет права ``messages:send``), а смотрят за тем, как она
      разбирается. Сузить очередь тому, кто не может её разобрать, значит
      ослепить надзор ради фильтра, который ему ничего не экономит. Новых
      данных они при этом не получают: читать все диалоги может любая роль
      (01 §12), очередь — их подмножество.

    Остаётся ровно менеджер — тот, у кого очередь личная и есть.
    """
    return user.role == "admin" or not convs.can_answer_clients(user.role)


# ------------------------------------------------------------------------ чтение


async def operator_ids_for_account(db: AsyncSession, account_id: uuid.UUID) -> set[str]:
    """Живые операторы канала — строками id (формат ``declined_by``).

    Пустое множество означает «на канал не назначен никто живой», то есть
    «канал открыт всем». Разворачивать это в «всех операторов» здесь нельзя:
    вызывающий должен видеть разницу между «канал ничей» и «канал ведёт один
    человек» (``inbox.eligible_operator_ids`` на ней и стоит).
    """
    rows = await db.execute(
        select(User.id)
        .join(AccountOperator, AccountOperator.user_id == User.id)
        .where(AccountOperator.account_id == account_id, *_live_operator())
    )
    return {str(i) for i in rows.scalars()}


async def assigned_user_ids(db: AsyncSession, account_id: uuid.UUID) -> list[uuid.UUID]:
    """Все назначенные на канал — как есть, включая отключённых.

    Экрану назначения нужна правда о галочках, а не отфильтрованный список:
    иначе сохранение формы молча снимет с канала человека, который сейчас в
    отпуске и отключён.
    """
    rows = await db.execute(
        select(AccountOperator.user_id)
        .where(AccountOperator.account_id == account_id)
        .order_by(AccountOperator.user_id)
    )
    return list(rows.scalars())


def candidate_out(user: User, *, is_online: bool = False) -> dict[str, Any]:
    """Строка списка сотрудников на экране назначения (11 §4.1, тип
    ``ChannelOperatorCandidate``).

    ``can_be_operator`` считает СЕРВЕР и отдаёт готовым: фронт не должен
    повторять правило про роли — в 7.4 «оператор» станет отдельным
    переключателем, независимым от роли, и повторённое правило разъедется.
    """
    # ⚠ 18.08: «ведёт диалоги» (7.4) — часть правила, а не украшение. Без него
    # канал, «закреплённый» за одним человеком, на деле открыт всем: раздача
    # смотрит handles_conversations, а этот экран его игнорировал — и
    # администратор, снявший тумблер, продолжал числиться оператором канала.
    can = (
        user.deleted_at is None
        and not user.is_service
        and user.is_active
        and convs.can_answer_clients(user.role)
        and user.handles_conversations
    )
    # ⚠ «УДАЛЁН» ПРОВЕРЯЕТСЯ ПЕРВЫМ, И ЭТО НЕ ПОРЯДОК РАДИ ПОРЯДКА. Удаление
    # ставит `deleted_at` и `is_active=False` соседними строками, поэтому при
    # обратном порядке удалённому доставалась подпись «Сотрудник отключён» —
    # то есть совет «включите его», которого выполнить нельзя: включение
    # удалённого отвечает отказом. Ровно это и прочитал владелец: «пишут, что
    # отключён, хотя на деле нет».
    if can:
        reason = None
    elif user.deleted_at is not None:
        reason = "Сотрудник удалён"
    elif user.is_service:
        reason = "Служебная учётная запись"
    elif not user.is_active:
        reason = "Сотрудник отключён"
    elif not convs.can_answer_clients(user.role):
        reason = "Роль не отвечает клиентам"
    else:
        reason = "Не ведёт диалоги"
    return {
        "id": str(user.id),
        "full_name": user.full_name,
        "role": user.role,
        "is_active": user.is_active,
        "is_online": is_online,
        "can_be_operator": can,
        "reason": reason,
        # Через ту же чистку, что и у `user_ref`: на боевых данных
        # встречается «Диспетчер  МНЧ» с двумя пробелами, и подпись
        # «(Диспетчер  МНЧ)» в решётке выглядит опечаткой экрана.
        "department": normalize_department(user.department),
    }


async def channel_operators(
    db: AsyncSession, redis: Redis | None, account_id: uuid.UUID
) -> dict[str, Any]:
    """Всё, что нужно экрану «Назначить операторов на канал» — одним ответом.

    ``assigned_ids`` + полный список сотрудников с пометкой, кого назначать
    можно. Отдельной ручки «дай сотрудников» экран не зовёт: два запроса,
    выполненные в разном порядке, дают галочки на людях, которых нет в списке.
    """
    await _get_account(db, account_id)
    порядок_набора = await assigned_user_ids(db, account_id)
    назначенные = set(порядок_набора)
    все = list((await db.execute(select(User).order_by(User.full_name, User.id))).scalars())
    # ⚠ ИНВАРИАНТ: КТО В НАБОРЕ — ТОТ И В СПИСКЕ. Иначе экран запирается намертво.
    #
    # ЖАЛОБА ВЛАДЕЛЬЦА 03.09: «не могу подключать сотрудников, сломалось — пишут,
    # что отключён, хотя на деле нет».
    #
    # Как это работало. Удалённых отсюда вычеркнули (чтобы не обещать назначение,
    # которого не будет), а `assigned_ids` отдавались как есть. Получался
    # идентификатор БЕЗ СТРОКИ: галочка стоит, а снять её не с чего. Форма шлёт
    # набор целиком, сервер отвечает 422 — и канал становится несохраняемым
    # навсегда: ни добавить оператора, ни убрать.
    #
    # ЗАМЕР БОЯ: так заперты 14 каналов из 14. Держат их двое удалённых
    # сотрудников — один на тринадцати каналах, второй на служебном.
    #
    # Экран к такой строке готов: снять галочку у неназначаемого можно, поставить
    # обратно нельзя (`CandidateRow`, `locked = !assignable && !checked`). Не
    # хватало ровно строки.
    #
    # Фильтр остаётся для тех, кого в наборе НЕТ: удалённый и служебный в общем
    # списке — обещание, которое нельзя выполнить.
    #
    rows = [u for u in все if u.id in назначенные or (not u.is_service and u.deleted_at is None)]
    online = await presence_map(redis, [u.id for u in rows]) if redis is not None else {}
    return {
        "assigned_ids": [str(i) for i in порядок_набора],
        "candidates": [candidate_out(u, is_online=online.get(u.id, False)) for u in rows],
    }


async def operators_summary(
    db: AsyncSession, account_ids: list[uuid.UUID]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Сводки «сколько назначено + первые аватарки» СРАЗУ для списка каналов.

    Батчем, а не по аккаунту: карточек девять, и девять запросов ради счётчика
    — это тот самый N+1, который в списке диалогов запрещён явно (01 §5.1).

    ``count: 0`` читается экраном как «канал открыт всем» — то же правило, что
    и в фильтре очереди, и подпись на карточке обязана быть именно такой.
    """
    if not account_ids:
        return {}
    rows = (
        await db.execute(
            select(AccountOperator.account_id, User.id, User.full_name, User.department)
            .join(User, User.id == AccountOperator.user_id)
            .where(AccountOperator.account_id.in_(account_ids), *_live_operator())
            .order_by(AccountOperator.account_id, User.full_name, User.id)
        )
    ).all()
    out: dict[uuid.UUID, dict[str, Any]] = {aid: {"count": 0, "preview": []} for aid in account_ids}
    for account_id, user_id, full_name, department in rows:
        item = out[account_id]
        item["count"] += 1
        if len(item["preview"]) < PREVIEW_LIMIT:
            item["preview"].append(user_ref_parts(user_id, full_name, department))
    return out


async def channels_of(db: AsyncSession, user: User) -> list[dict[str, Any]]:
    """«Мои каналы» — ответ оператору на вопрос «почему я вижу не всё».

    ``access``: ``assigned`` — меня назначили, ``open`` — на канал не назначен
    никто, поэтому он открыт всем. Тому, кто видит очередь целиком
    (:func:`sees_all_channels`), все каналы приходят как ``open``: это и есть
    правда про его доступ.

    Отключённые каналы (``status = 'disabled'``) в список не попадают: новых
    обращений по ним не будет, а в списке они выглядели бы как рабочие.
    """
    everything = sees_all_channels(user)
    rows = (
        await db.execute(
            select(
                AvitoAccount.id,
                AvitoAccount.title,
                AvitoAccount.lead_origin,
                operator_assigned(AvitoAccount.id, user.id).label("mine"),
                channel_has_operators(AvitoAccount.id).label("has_ops"),
            )
            .where(AvitoAccount.status != "disabled")
            .order_by(AvitoAccount.title, AvitoAccount.id)
        )
    ).all()
    items: list[dict[str, Any]] = []
    for account_id, title, lead_origin, mine, has_ops in rows:
        if not everything and has_ops and not mine:
            continue
        items.append(
            {
                "id": str(account_id),
                "title": title,
                # Источник рядом с названием — просьба владельца 03.09; ту же
                # подпись собирает фронт в списках выбора каналов.
                "lead_origin": lead_origin,
                "access": "assigned" if (mine and not everything) else "open",
            }
        )
    return items


# ------------------------------------------------------------------------ запись


@dataclass(slots=True)
class AssignResult:
    account: AvitoAccount
    operator_ids: list[uuid.UUID] = field(default_factory=list)
    added: list[uuid.UUID] = field(default_factory=list)
    removed: list[uuid.UUID] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


async def _get_account(db: AsyncSession, account_id: uuid.UUID) -> AvitoAccount:
    account = await db.get(AvitoAccount, account_id)
    if account is None:
        raise ApiError("not_found", "Канал не найден", status=404)
    return account


async def _validate_operators(db: AsyncSession, wanted: list[uuid.UUID]) -> list[User]:
    """Назначать можно только тех, кто действительно отвечает клиентам.

    Проверка на сервере, а не «в интерфейсе такой галочки нет»: назначенный
    наблюдатель не увидит канала в очереди (роль отсекает
    ``inbox._assert_can_take``), но СЧИТАТЬСЯ назначенным будет — и канал
    спрячется от настоящих операторов. Тихое исчезновение обращений вместо
    честной ошибки формы.
    """
    if not wanted:
        return []
    rows = list((await db.execute(select(User).where(User.id.in_(wanted)))).scalars())
    found = {u.id: u for u in rows}
    missing = [str(i) for i in wanted if i not in found]
    if missing:
        raise ApiError(
            "unprocessable",
            "Сотрудник не найден",
            status=422,
            details={"reason": "user_not_found", "user_ids": missing},
        )
    удалённые = [u for u in rows if u.deleted_at is not None]
    if удалённые:
        # Отдельная причина, а не «отключён»: включить удалённого нельзя, и
        # совет «сначала включите его» вёл в тупик.
        raise ApiError(
            "unprocessable",
            "Сотрудник удалён и на канал не назначается: "
            + ", ".join(u.full_name for u in удалённые),
            status=422,
            details={"reason": "user_deleted", "user_ids": [str(u.id) for u in удалённые]},
        )
    inactive = [u for u in rows if not u.is_active]
    if inactive:
        raise ApiError(
            "unprocessable",
            "Сотрудник отключён — сначала включите его, потом назначайте на канал: "
            + ", ".join(u.full_name for u in inactive),
            status=422,
            details={"reason": "user_inactive", "user_ids": [str(u.id) for u in inactive]},
        )
    not_operators = [u for u in rows if not convs.can_answer_clients(u.role)]
    if not_operators:
        raise ApiError(
            "unprocessable",
            "Эти сотрудники не отвечают клиентам и не могут вести канал: "
            + ", ".join(f"{u.full_name} ({u.role})" for u in not_operators),
            status=422,
            details={
                "reason": "cannot_answer_clients",
                "user_ids": [str(u.id) for u in not_operators],
            },
        )
    return rows


async def release_user_from_channels(
    db: AsyncSession, user: User, *, actor: User
) -> list[uuid.UUID]:
    """Снять сотрудника со ВСЕХ каналов. Зовётся при удалении сотрудника.

    ⚠ ПРИЧИНА БОЕВОГО ТУПИКА 03.09. Удаление ставило `deleted_at`, возвращало
    диалоги в очередь — и оставляло строки в `account_operators`. Строка
    ссылается на человека, которого больше нет: назначить его заново нельзя,
    а форма назначения из-за него переставала сохраняться. За месяцы так
    заперло 14 каналов из 14.

    ⚠ ТОЛЬКО УДАЛЕНИЕ, НЕ ОТКЛЮЧЕНИЕ. Отключение обратимо — отпуск, болезнь,
    разбор происшествия, — и связь с каналом переживает его намеренно: человека
    включают обратно, и он возвращается на свои каналы без единого щелчка.
    Снеси мы связь при отключении, возвращение молча оставило бы канал общим.

    ⚠ ЖИВОЙ РАСКЛАД ОТ ЭТОГО НЕ МЕНЯЕТСЯ. Все боевые выборки считают операторов
    по ЖИВОМУ пользователю (`_live_operator`), и удалённый в них не входил и
    до уборки. То есть эта функция приводит таблицу в согласие с тем, как
    система уже себя ведёт, а не меняет доступ к каналам.
    """
    rows = await db.execute(
        sa.delete(AccountOperator)
        .where(AccountOperator.user_id == user.id)
        .returning(AccountOperator.account_id)
        .execution_options(synchronize_session=False)
    )
    account_ids = list(rows.scalars())
    if not account_ids:
        return []
    # Журнал — по каждому каналу, тем же действием, что и правка руками: разбор
    # «почему обращения этого канала перестали приходить Петрову» ищет одну
    # строку, а не две разных.
    for account_id in account_ids:
        await write_audit(
            db,
            user_id=actor.id,
            action=AUDIT_ACTION,
            entity="avito_account",
            entity_id=str(account_id),
            details={
                "added": [],
                "removed": [str(user.id)],
                "reason": "user_deleted",
                "released_user": user.full_name,
            },
        )
    return account_ids


async def grid(db: AsyncSession, redis: Redis | None) -> dict[str, Any]:
    """Решётка «люди × каналы» одним ответом — для массового назначения.

    ⚠ ПРОСЬБА ВЛАДЕЛЬЦА 04.09: «продумай, как мне быстро можно было подключать
    или отключать людей на всех аккаунтах — сейчас я вручную по 30 раз захожу и
    тыкаю, неудобно». Замер боя объясняет, почему это болит: 35 каналов, 35
    человек, и в среднем один человек назначен на 29,7 канала из 35. То есть
    норма здесь — «почти все на почти всех», а единица работы — ЧЕЛОВЕК, тогда
    как весь нынешний экран построен вокруг канала.

    ⚠ СЫРЫЕ ПАРЫ, А НЕ «КОМУ ВИДЕН КАНАЛ». Готовая функция `channels_of` для
    решётки не годится: она вычёркивает отключённые каналы и разворачивает
    администратору всё в «открыт всем». Решётке нужна правда о галочках —
    включая отключённых людей и отключённые каналы, — иначе сохранение молча
    снимет с канала отпускника.
    """
    аккаунты = list(
        (
            await db.execute(select(AvitoAccount).order_by(AvitoAccount.title, AvitoAccount.id))
        ).scalars()
    )
    люди = list((await db.execute(select(User).order_by(User.full_name, User.id))).scalars())
    пары = list(
        (await db.execute(select(AccountOperator.account_id, AccountOperator.user_id))).all()
    )
    online = await presence_map(redis, [u.id for u in люди]) if redis is not None else {}
    назначенные = {u for _, u in пары}
    return {
        "accounts": [
            {
                "id": str(a.id),
                "title": a.title,
                "lead_origin": a.lead_origin,
                "status": a.status,
                "is_service": a.is_service,
            }
            for a in аккаунты
        ],
        # Тот же `candidate_out`, что и у поканального экрана: правило «кого
        # можно назначить» обязано быть одно, иначе два экрана разойдутся.
        # Показываем только тех, кто назначаем ИЛИ уже где-то назначен —
        # остальные строки решётки были бы обещанием, которого не выполнить.
        "users": [
            candidate_out(u, is_online=online.get(u.id, False))
            for u in люди
            if not u.is_service and (u.deleted_at is None or u.id in назначенные)
        ],
        "assigned": [{"account_id": str(a), "user_id": str(u)} for a, u in пары],
    }


async def apply_bulk(
    db: AsyncSession,
    changes: list[tuple[uuid.UUID, uuid.UUID, bool]],
    *,
    actor: User,
) -> dict[str, Any]:
    """Применить пачку изменений «человек на канале» одной транзакцией.

    ⚠ ДЕЛЬТЫ, А НЕ ПОЛНЫЕ НАБОРЫ. Полный набор на канал — это гонка: пока
    администратор смотрел на решётку, кто-то мог поменять состав другого
    канала, и отправка «как было на экране» затёрла бы чужую правку. Дельта
    меняет ровно то, что человек тронул.

    ⚠ ГОДНОСТЬ СУДИТСЯ ОДИН РАЗ И ТЕМ ЖЕ ПРАВИЛОМ. Добавлять можно только тех,
    кого можно назначить (`_validate_operators`) — иначе массовый экран тихо
    заведёт назначения, которых поканальный не допускает, и число на карточке
    разойдётся с галочками.

    ⚠ ВОЗВРАЩАЕМ, КАКИЕ КАНАЛЫ ОСТАЛИСЬ БЕЗ ЖИВЫХ ОПЕРАТОРОВ. Пустой набор
    означает «канал открыт ВСЕМ», а не «закрыт»: снятие последнего человека
    РАСШИРЯЕТ доступ. В поканальной модалке об этом предупреждают отдельно, и
    массовое действие обязано сказать то же самое — но уже по факту.
    """
    добавить = [(a, u) for a, u, on in changes if on]
    снять = [(a, u) for a, u, on in changes if not on]
    if добавить:
        await _validate_operators(db, list({u for _, u in добавить}))

    for account_id, user_id in снять:
        await db.execute(
            sa.delete(AccountOperator).where(
                AccountOperator.account_id == account_id,
                AccountOperator.user_id == user_id,
            )
        )
    if добавить:
        # ⚠ ON CONFLICT DO NOTHING, А НЕ ПРОВЕРКА ПЕРЕД ВСТАВКОЙ. Составной ключ
        # запрещает дубль, и голая вставка на гонке двух администраторов дала бы
        # пятисотку вместо тихого «уже назначен».
        await db.execute(
            pg_insert(AccountOperator)
            .values([{"account_id": a, "user_id": u} for a, u in добавить])
            .on_conflict_do_nothing(index_elements=["account_id", "user_id"])
        )
    await db.flush()

    затронутые = sorted({a for a, _, _ in changes}, key=str)
    сводка = await operators_summary(db, затронутые)
    строки = (
        await db.execute(
            select(AvitoAccount.id, AvitoAccount.title).where(AvitoAccount.id.in_(затронутые))
        )
    ).all()
    названия: dict[uuid.UUID, str] = {r.id: r.title for r in строки}
    for account_id in затронутые:
        добавлено = [str(u) for a, u in добавить if a == account_id]
        снято = [str(u) for a, u in снять if a == account_id]
        await write_audit(
            db,
            user_id=actor.id,
            action=AUDIT_ACTION,
            entity="avito_account",
            entity_id=str(account_id),
            details={
                "title": названия.get(account_id),
                "added": добавлено,
                "removed": снято,
                "reason": "bulk",
                "count": сводка.get(account_id, {}).get("count", 0),
            },
        )
    открыты_всем = [
        названия.get(a) or str(a) for a in затронутые if сводка.get(a, {}).get("count", 0) == 0
    ]
    return {
        "applied": len(changes),
        "accounts_touched": len(затронутые),
        "opened_to_all": открыты_всем,
    }


async def set_operators(
    db: AsyncSession,
    account_id: uuid.UUID,
    operator_ids: list[uuid.UUID],
    *,
    actor: User,
) -> AssignResult:
    """Заменить набор операторов канала целиком (галочки экрана назначения).

    Идемпотентно: тот же набор не пишет ни строки в связь, ни строки в журнал.
    Экран сохраняют и без изменений — «открыл, посмотрел, нажал Сохранить», — и
    засорять этим разбор смены нечем.

    Пустой набор разрешён и означает «канал открыт всем» (см. шапку). Это
    нормальная операция, а не ошибка формы: так канал возвращают в общий пул.
    """
    account = await _get_account(db, account_id)
    wanted = list(dict.fromkeys(operator_ids))  # порядок сохранён, дубли галочек убраны

    current = set(await assigned_user_ids(db, account_id))
    target = set(wanted)
    added = [i for i in wanted if i not in current]
    removed = sorted(current - target, key=str)
    # ⚠ СУДИМ ТОЛЬКО ДОБАВЛЯЕМЫХ, А НЕ ВЕСЬ НАБОР. Здесь стояло
    # `_validate_operators(db, wanted)`, и в этом была вторая половина боевого
    # тупика: сохранение спотыкалось о человека, который в наборе УЖЕ ЛЕЖИТ.
    # Пока он годен — вопроса нет; стал негоден (уволили, сняли роль) — и канал
    # переставал сохраняться целиком, вместе с добавлением и снятием всех
    # прочих. Унаследованное не пересуживается: снять его — как раз то, зачем
    # администратор сюда и пришёл.
    #
    # Добавить негодного с нуля по-прежнему нельзя, и это верное правило:
    # закреплено отдельными проверками.
    await _validate_operators(db, added)

    if removed:
        await db.execute(
            sa.delete(AccountOperator).where(
                AccountOperator.account_id == account_id,
                AccountOperator.user_id.in_(removed),
            )
        )
    for user_id in added:
        db.add(AccountOperator(account_id=account_id, user_id=user_id))

    result = AssignResult(account=account, operator_ids=wanted, added=added, removed=removed)
    if result.changed:
        # В журнал — состав ЦЕЛИКОМ плюс дельта. «Кого сняли» отвечает на
        # вопрос разбора «почему обращения этого канала перестали приходить
        # Петрову», а полный состав — на вопрос «как канал выглядел после
        # правки», не требуя склеивать историю из десяти строк.
        await write_audit(
            db,
            user_id=actor.id,
            action="account.operators_changed",
            entity="avito_account",
            entity_id=str(account_id),
            details={
                "title": account.title,
                "operator_ids": [str(i) for i in wanted],
                "added": [str(i) for i in added],
                "removed": [str(i) for i in removed],
                "count": len(wanted),
            },
        )
        log.info(
            "account_operators.changed",
            account_id=str(account_id),
            added=len(added),
            removed=len(removed),
            total=len(wanted),
        )
    await db.flush()
    return result


# ---------------------------------------------------------------------- проверка


async def assert_can_take_account(db: AsyncSession, user: User, account_id: uuid.UUID) -> None:
    """«Этот канал ваш?» — страж принятия диалога из чужой очереди.

    Очередь уже отфильтрована (``inbox.visible_queue_condition``), но прятать
    кнопку недостаточно: id диалога виден в ссылке, в кадре ``inbox:new`` и в
    списке «Все», а принятие — обычный POST. Без этой проверки фильтр очереди
    остаётся оформлением, а не правилом.

    Текст ошибки называет причину человеческим языком: «канал не ваш» — это не
    сбой, и оператор должен понять, что делать (позвать администратора), а не
    жать кнопку второй раз.
    """
    if sees_all_channels(user):
        return
    live = await operator_ids_for_account(db, account_id)
    # Пусто — канал открыт всем (правило совместимости). Тем же множеством
    # пользуется ``inbox.eligible_operator_ids``: «кому диалог доступен» и
    # «кто вправе его принять» обязаны быть одним ответом, иначе появится
    # диалог, который эскалация считает чужим, а принятие — своим.
    if not live or str(user.id) in live:
        return
    account = await db.get(AvitoAccount, account_id)
    raise ApiError(
        "forbidden",
        f"Диалог канала «{account.title}» ведут другие операторы"
        if account is not None
        else "Диалог другого канала — принять его может назначенный оператор",
        status=403,
        details={
            "reason": "channel_not_assigned",
            "account_id": str(account_id),
            "account_title": account.title if account is not None else None,
        },
    )
