"""Очередь «Входящие» с явным принятием диалога (план 7.1, разбор Jivo 15 §2.1).

Зачем это существует. Сейчас в LeadChat действует правило «кто первым ответил,
тот и ведёт» (01 §6.2). На двух менеджерах оно работает; на тринадцати
операторах и девяти каналах (15 §1) оно означает ровно две беды: двое пишут
одному клиенту, а третьего клиента не берёт никто. Поэтому диалог больше не
сваливается всем сразу — он ждёт в очереди, оператор жмёт «Принять» или
«Отклонить», и только принятый диалог уходит в «Мои».

Модель состояния — поля очереди у ``conversations``, а не пятый статус; полное
обоснование в шапке миграции 0007. Здесь важно одно следствие: **диалог в
очереди — это диалог без хозяина**, а не диалог в особом статусе. Условие
очереди собрано в :func:`queue_condition` и живёт ровно в одном месте — оно же
предикат частичных индексов миграции.

Главное свойство модуля — АТОМАРНОСТЬ принятия. :func:`claim` делает один
``UPDATE ... WHERE claimed_by_id IS NULL`` и смотрит на число затронутых строк.
Не «SELECT, проверил, UPDATE»: между чтением и записью успевает вклиниться
второй оператор, и на тринадцати операторах это не теория — это регулярное
событие. Проверяется настоящим параллелизмом на настоящем PostgreSQL
(``tests/integration/test_inbox_race.py``), а не рассуждением.

Транзакции — как везде (08 §8.1): функции этого модуля пишут в переданную
сессию и НИЧЕГО не коммитят и ничего не публикуют. Кадры в браузер собирает и
шлёт вызывающий — ручки очереди (``app/api/routes/inbox.py``) и входящий
конвейер (``inbox:new``, см. :func:`inbox_frame`), — строго после commit'а.
Порядок захвата блокировок тот же, что и в остальном коде (08 §8.4): сначала
``conversations``, потом ``messages``.

Чего этот модуль не делает: не решает, КОГДА диалог попадает в очередь. Это
решают точки входа диалога — inbound-конвейер и передача от бота, — и для них
здесь есть :func:`enter_queue` и :func:`return_to_queue` (см. cross-boundary).
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, NoReturn

import sqlalchemy as sa
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import Client, Conversation, ConversationDecline, Message, User
from app.services import account_operators as acc_ops
from app.services import conversation_status as status_dict
from app.services import conversations as convs
from app.services import notifications as notify_svc
from app.services.audit import write_audit
from app.services.user_ref import user_ref
from app.ws.events import iso

log = structlog.get_logger("app.inbox")

# Имена оставлены (их знает половина модуля), но значения теперь берутся из
# общего словаря `services.conversation_status` — своей копии тут больше нет.
# Она была одной из девяти, и именно такие «константы поближе» и дают продукту
# девять словарей одного и того же.
CLOSED = "closed"
IN_PROGRESS = "in_progress"
NEW = "new"
assert {CLOSED, IN_PROGRESS, NEW} <= set(status_dict.STATUSES), (
    "константы очереди разъехались со словарём статусов"
)

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_DECLINE_REASON_LEN = 500

# Событие центра уведомлений для эскалации «отказались все» (14 §2.3).
# Зарегистрировано в каталоге ``notifications.KINDS`` — там же severity,
# audience и правило склейки. Вызов в :func:`_escalate` всё равно передаёт их
# ЯВНО: событие обязано вести себя правильно и в том случае, если запись из
# каталога однажды переедет или потеряется, а не молча превратиться в «info
# никому». Расхождение между этими двумя местами ловит тест каталога.
ESCALATION_KIND = "conversation.unclaimed"


# --------------------------------------------------------------- условие очереди


def queue_condition() -> sa.ColumnElement[bool]:
    """Диалог ждёт принятия. Один источник правды для выборок и индексов 0007.

    Четыре условия, каждое закрывает свой класс данных (подробно — в шапке
    миграции): нет принявшего, нет ответственного (страховка от старого пути
    «кто первым ответил» и от диалогов, которые ведут прямо сейчас), диалог
    вообще ставили в очередь, диалог не закрыт.
    """
    return sa.and_(
        Conversation.claimed_by_id.is_(None),
        Conversation.assignee_id.is_(None),
        Conversation.offered_at.is_not(None),
        Conversation.status != CLOSED,
        # ⚠ 16.08, решение владельца «бот — отдельный сотрудник, которому не мешают»:
        # диалог, который прямо сейчас ведёт бот, из очереди убран — операторы его
        # не видят, автораздача не выдаёт, сторожа не звенят. Всё, что построено на
        # queue_condition, получает правило автоматически. При handoff бот ставит
        # bot_active=False — диалог сам возвращается в очередь (offered_at на месте).
        Conversation.bot_active.is_(False),
    )


#: Сколько живёт отказ (требование заказчика от 13 августа: «вернулся в течение 3 минут»).
#:
#: ПОЧЕМУ КОНСТАНТА, А НЕ НАСТРОЙКА. Настройкой это стало бы полем, которое заполняют один
#: раз и забывают, а цена ошибки несимметрична: поставь час — и диалог, от которого человек
#: отказался, час никому из отказавшихся не виден; поставь десять секунд — и кнопка
#: «Отклонить» перестаёт что-либо значить. Три минуты — это «уберите с глаз, я сейчас занят»,
#: и другого смысла у неё нет. Появится второй сценарий — тогда и настройка.
DECLINE_TTL = timedelta(minutes=3)


def _decline_is_active(user_id: uuid.UUID, now: datetime) -> sa.ColumnElement[bool]:
    """«Отказ этого сотрудника ещё действует» — то есть моложе :data:`DECLINE_TTL`.

    ⚠ ВЕТКИ ДИАЛЕКТА ЗДЕСЬ БОЛЬШЕ НЕТ, И ЭТО НЕ УПУЩЕНИЕ. Раньше признак жил массивом в
    самой строке диалога, а массивы у PostgreSQL и SQLite разные — отсюда две ветки.
    Коррелированный ``EXISTS`` со сравнением даты компилируется одинаково в обоих, потому
    что ничего диалектного в нём нет.

    ⚠ ``now`` ПАРАМЕТРОМ, А НЕ ``func.now()``. Тесты двигают время сами и проверяют границу
    в обе стороны; с серверным ``now()`` проверить «через три минуты вернулось» можно было
    бы только ожиданием в три минуты.

    ⚠ ВТОРОЕ УСЛОВИЕ — ``declined_at > offered_at`` — И ОНО ЗАМЕНЯЕТ СОБОЙ ОЧИСТКУ. Диалог
    возвращается в очередь из девяти разных мест (новое сообщение клиента, снятие
    ответственного, уход оператора из сети, удаление сотрудника…), и каждое из них ставит
    ``offered_at`` заново. Отказ, сделанный в ПРОШЛЫЙ заход, к новому отношения не имеет:
    «оператор, отказавшийся вчера, должен увидеть диалог опять» — это правило было и
    держалось обнулением массива в каждом из тех мест.

    Повторять эту россыпь для второй таблицы значило бы завести девять мест, где легко
    забыть строку, — и забыли бы в первой же новой ветке возврата. Сравнение с ``offered_at``
    даёт то же самое одним условием и не требует помнить о нём вообще.
    """
    return sa.exists().where(
        sa.and_(
            ConversationDecline.conversation_id == Conversation.id,
            ConversationDecline.user_id == user_id,
            ConversationDecline.declined_at > now - DECLINE_TTL,
            # ⚠ «НЕ РАНЬШЕ», А НЕ «ПОЗЖЕ». В бою эти два момента не совпадают никогда:
            # постановку в очередь и нажатие «Отклонить» разделяют секунды. А вот при
            # строгом «позже» отказ, пришедший в ту же метку времени, что и постановка,
            # молча не срабатывал бы — и кнопка не делала бы ничего. Из двух возможных
            # ошибок на равенстве эта дороже: она ломает нажатие, а обратная всего лишь
            # прячет диалог у отказавшегося максимум на три минуты.
            ConversationDecline.declined_at >= Conversation.offered_at,
        )
    )


def visible_queue_condition(
    db: AsyncSession, user: User, *, now: datetime | None = None
) -> sa.ColumnElement[bool]:
    """Очередь ГЛАЗАМИ конкретного оператора: свои каналы, без отклонённого им.

    Два сужения, и оба обязаны быть в SQL, а не в Python: очередь бывает
    длинной, а постфильтрация страницы отдала бы неполную страницу и неверный
    счётчик.

    1. **Отклонённое им** прячется здесь, а не в интерфейсе: иначе оператор
       будет натыкаться на один и тот же диалог до конца смены. С 13 августа —
       ТОЛЬКО НА ТРИ МИНУТЫ (:data:`DECLINE_TTL`, требование заказчика): «у того,
       кто нажал, вернётся в очередь; тот, кто не нажимал, так и останется».
       Отказ — это «уберите с глаз, я сейчас занят», а не «никогда больше».
    2. **Чужие каналы** (7.2). У девяти каналов заказчика свои наборы людей
       (15 §2.2); оператор видит в очереди диалоги своих каналов ПЛЮС каналов,
       на которые не назначен никто. Правило «канал без назначенных доступен
       всем» живёт в ``account_operators.visible_accounts_condition`` — там же,
       откуда его берёт :func:`eligible_operator_ids`, чтобы видимость и
       эскалация не могли разъехаться.

    Кому очередь не сужается — администратору (ему нужно видеть всё, он же
    разбирает эскалацию), руководителю и наблюдателю (они из очереди не
    берут, а смотрят за ней). Список и причины — в
    ``account_operators.sees_all_channels``.
    """
    conds = [queue_condition(), sa.not_(_decline_is_active(user.id, now or datetime.now(UTC)))]
    if not acc_ops.sees_all_channels(user):
        conds.append(acc_ops.visible_accounts_condition(Conversation.account_id, user.id))
    общая = sa.and_(*conds)

    # ⚠ ПЕРЕДАННОЕ МНЕ — ТОЖЕ МОЯ ОЧЕРЕДЬ (просьба владельца 31.08: «когда
    # передаёшь диалог, пусть он показывается во Входящих у того, кому
    # передаю»).
    #
    # Переданный диалог до сих пор попадал только во вкладку «Мои»
    # (`participants.mine_condition`), а туда человек заглядывает, чтобы
    # ПРОДОЛЖИТЬ работу, а не чтобы взять новую. Предложение о передаче — это
    # ровно «вот работа, решите» — то же самое, зачем существуют «Входящие».
    # Пока его там не было, диалог ждал, пока получатель случайно посмотрит в
    # другую вкладку.
    #
    # Ветка ЛИЧНАЯ и в общее `queue_condition` не уходит намеренно: тем
    # условием пользуются автораздача, сторожа возврата и эскалация, а
    # предложение передачи адресовано ОДНОМУ человеку. Попади оно в общую
    # очередь — диалог, который передали Ивану, забрал бы Пётр, и передача
    # перестала бы что-либо значить.
    #
    # Канальное сужение к этой ветке не применяется тоже: передают руками и
    # осознанно, а «канал не ваш» здесь означало бы, что коллеге нельзя
    # передать диалог, потому что он не в списке операторов канала, — а
    # передача и есть способ обойти это законно.
    моё_предложение = sa.and_(
        Conversation.transfer_to_id == user.id,
        Conversation.status != CLOSED,
    )
    return sa.or_(общая, моё_предложение)


def is_waiting(conv: Conversation) -> bool:
    """Тот же предикат на питоне — для уже загруженной строки.

    ⚠ ПРО ПЕРЕДАЧУ ЗДЕСЬ НЕТ НИЧЕГО, И ЭТО НЕ ПРОПУСК. Функция отвечает на
    вопрос «диалог ничей и ждёт кого угодно» — им пользуются автораздача и
    сторожа. Предложение передачи адресовано одному человеку и живёт в личной
    ветке `visible_queue_condition`; смешай их — и переданный Ивану диалог
    забрал бы Пётр.
    """
    return (
        conv.claimed_by_id is None
        and conv.assignee_id is None
        and conv.offered_at is not None
        and conv.status != CLOSED
        and not conv.bot_active  # ведёт бот — в очереди не показывается (16.08)
    )


# ------------------------------------------------------------------- время ожидания


def waiting_seconds(conv: Conversation, *, now: datetime | None = None) -> int | None:
    """Сколько диалог ждёт принятия. None — в очереди не стоял."""
    offered = notify_svc.as_utc(conv.offered_at)
    if offered is None:
        return None
    return max(0, int(((now or datetime.now(UTC)) - offered).total_seconds()))


def queue_anchor(conv: Conversation, last_message: Message | None) -> tuple[datetime, str]:
    """Момент, от которого строка очереди ждёт, — тот же, что у плашки в списке.

    `waiting_since` с подсказкой о последнем сообщении клиента (если превью —
    его), иначе `offered_at`; id в хвосте — для устойчивого порядка. Наивное
    время (SQLite в тестах) приводится к UTC, чтобы сравнение не падало.
    """
    начало = status_dict.waiting_since(
        conv,
        last_client_message_at=(
            last_message.created_at
            if last_message is not None and last_message.direction == "in"
            else None
        ),
    )
    якорь = notify_svc.as_utc(начало) or notify_svc.as_utc(conv.offered_at)
    return (якорь or datetime.max.replace(tzinfo=UTC), str(conv.id))


def format_wait(seconds: int) -> str:
    """Ожидание человеческим языком — для системной записи в ленте."""
    if seconds < 60:
        return f"{seconds} с"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    days, hours = divmod(hours, 24)
    return f"{days} дн {hours} ч" if hours else f"{days} дн"


# ----------------------------------------------------- вход и возврат в очередь
#
# Обе функции — чистые: меняют поля переданной строки и ничего не пишут. Их
# зовут ЧУЖИЕ зоны (inbound-конвейер, передача от бота, снятие ответственного),
# и логика «что значит стоять в очереди» обязана быть у них общей с выборкой.


def clear_auto_assignment(conv: Conversation) -> None:
    """Снять отметку «система отдала, человек ещё не взялся» (7.7).

    Зовётся отовсюду, где диалог перестаёт быть «выданным и нетронутым»: когда
    оператор ответил, когда его передали руками, когда он вернулся в очередь,
    когда сменили статус. Отдельная функция, а не присваивание на месте, —
    чтобы точки снятия было видно поиском по имени: пропущенная означает
    диалог, который система отберёт у человека, уже взявшегося за него.

    Страховка на случай пропуска всё равно есть: сторож возврата (7.7) не
    трогает диалог, в котором после отметки было хоть одно исходящее оператора.
    """
    conv.auto_assigned_at = None


def clear_awaiting(conv: Conversation) -> None:
    """Ожидание закончилось. Отдельная функция, а не присваивание на месте, —
    чтобы точки снятия было видно поиском по имени: пропущенная означает
    диалог, который вечно «ждёт ответа» и вечно о себе напоминает."""
    conv.awaiting_since = None


def leave_queue(conv: Conversation) -> None:
    """Убрать диалог из очереди, НЕ назначая никому (чёрный список, docs/19).

    Отличается от `return_to_queue` ровно наоборот: тот кладёт диалог обратно
    в очередь, а этот забирает оттуда без хозяина. Диалог остаётся видимым,
    находится поиском и открывается — он просто перестаёт требовать внимания.

    Ответственный не ставится намеренно. Поставь мы кого-нибудь — человек
    получил бы в «Мои» диалог, которого не брал, и вопрос «почему это у
    меня» вместо сэкономленного времени.
    """
    conv.offered_at = None
    conv.claimed_by_id = None
    conv.claimed_at = None
    conv.escalated_at = None
    # Диалог перестал требовать внимания — значит и «ждёт ответа» с него
    # снимается: иначе чёрный список молча оставлял бы вечно ждущего клиента.
    clear_awaiting(conv)


def enter_queue(conv: Conversation, *, now: datetime | None = None) -> None:
    """Поставить диалог в очередь: новый диалог, возврат клиента, передача от бота.

    Отказы обнуляются намеренно: клиент написал снова — это новое ожидание, и
    оператор, отказавшийся вчера, должен увидеть диалог опять.

    ``assignee_id`` не трогаем: им владеет вызывающий (inbound снимает его сам
    при возврате клиента). Если ответственный остался, диалог в очередь просто
    не попадёт — второй замок условия очереди.
    """
    conv.offered_at = now or datetime.now(UTC)
    conv.claimed_by_id = None
    conv.claimed_at = None
    conv.declined_by = []  # новый список — иначе ORM не увидит изменения
    conv.escalated_at = None
    clear_auto_assignment(conv)


async def assign_by_distribution(
    db: AsyncSession,
    conv: Conversation,
    user: User,
    *,
    load_before: int | None = None,
    now: datetime | None = None,
) -> None:
    """Отдать обращение оператору автоматически (docs/18).

    Отличие от «Принять» ровно одно и важное: там решение принял человек, здесь
    — система. Поэтому в ленту ложится системная запись с ИМЕНЕМ получателя, а
    в журнал — отдельное действие: через неделю на вопрос «почему этот диалог у
    Петрова» должен быть ответ, а не догадка.

    Поля очереди снимаются те же, что при принятии: диалог из неё уходит и у
    остальных двенадцати человек исчезает. `claimed_by_id` при этом НЕ
    ставится: его смысл — «человек нажал кнопку», и приписывать нажатие тому,
    кто ничего не нажимал, значило бы врать журналу.
    """
    now = now or datetime.now(UTC)
    conv.assignee_id = user.id
    conv.offered_at = None
    conv.declined_by = []
    conv.escalated_at = None
    # Отметка «отдала система, человек ещё не взялся» — по ней диалог вернётся
    # в очередь, если получатель уйдёт из сети, не притронувшись к нему (7.7).
    conv.auto_assigned_at = now
    # А эта — «системе пришлось отдать диалог именно ему», и живёт она на
    # человеке (#35). По ней автораздача решает, кому дольше не доставалось.
    # Ставится ЗДЕСЬ, потому что здесь событие и происходит: раньше эту
    # величину выводили запросом по всей истории диалогов, и выводили неверно —
    # `max(updated_at)` меняется от любого сообщения, а не от выдачи.
    #
    # При ручном приёме («Принять» во «Входящих») отметка НЕ ставится
    # намеренно: человек взял диалог сам, и уступать ему очередь на
    # автораздачу не за что.
    user.last_assigned_at = now
    # Автораздача берёт диалог из очереди, а в очереди может лежать только
    # `new`: у отложенного есть хозяин по построению. Общий вход всё равно
    # общий — четыре копии этого `if` с разными наборами исходных статусов и
    # были причиной, по которой `ensure_in_progress` появился.
    await convs.ensure_in_progress(db, conv, source="distribution", actor_id=None, now=now)

    convs.add_system_message(db, conv, f"Диалог распределён: {user.full_name}")

    await write_audit(
        db,
        user_id=None,  # решение системы, а не сотрудника
        action="conversation.auto_assigned",
        entity="conversation",
        entity_id=str(conv.id),
        details={
            "assignee_id": str(user.id),
            "assignee_name": user.full_name,
            # Нагрузка получателя на момент выбора: без неё разбор «почему
            # именно ему» упирается в то, что нагрузка уже изменилась.
            "load_before": load_before,
            "waited_seconds": waiting_seconds(conv, now=now),
        },
    )
    log.info(
        "distribution.assigned",
        conversation_id=str(conv.id),
        assignee_id=str(user.id),
        load_before=load_before,
    )


def return_to_queue(
    conv: Conversation, *, now: datetime | None = None, keep_declines: bool = True
) -> None:
    """Вернуть принятый диалог в очередь: «передумал» (:func:`release`),
    «снять ответственного» из передачи (01 §5.5) и сторожа освобождения.

    Место в очереди — от того, как давно клиент ждёт ответа сейчас
    (`awaiting_since`), а не от первого захода диалога в очередь. Диалог,
    принятый утром и три часа ведшийся, вставал в очередь с утренним временем:
    администраторам тут же уходило «ждёт в очереди 3 ч», а взявший читал в
    ленте «Ждал 3 ч». Ответ уже получил — ждать нечего, отсчёт с возврата.
    Для «принял и сразу передумал» это то же самое: клиент ждёт с того же
    сообщения, с которого диалог впервые встал в очередь.

    Отказы по умолчанию сохраняются: отказавшийся не обязан видеть диалог
    снова только потому, что его кто-то подержал и вернул.
    """
    now = now or datetime.now(UTC)
    conv.claimed_by_id = None
    conv.claimed_at = None
    conv.assignee_id = None
    # ⚠ ЗАВИСШЕЕ ПРЕДЛОЖЕНИЕ ПЕРЕДАЧИ СНИМАЕТСЯ ВМЕСТЕ С ОТВЕТСТВЕННЫМ
    # (боевой случай 28.08).
    #
    # Диалог уезжает в ОБЩУЮ очередь — то есть перестаёт быть чьим-либо. Висящее
    # при этом предложение конкретному человеку противоречит самому смыслу
    # возврата, и на бою это кончилось тупиком: диалог стоял во «Входящих» с
    # плашкой «Иванов Иван → Петров Пётр: ждёт подтверждения», принять его
    # было нечем, и владельцу пришлось закрыть его руками и искать заново.
    #
    # Отдельная функция `transfer.clear`, а не четыре присваивания: полей
    # четыре, и забытый `transfer_comment` остался бы висеть объяснением к
    # предложению, которого уже нет.
    from app.services import transfer as _transfer  # noqa: PLC0415 — цикл импортов

    _transfer.clear(conv)
    clear_auto_assignment(conv)
    # ОТЛОЖКУ СНИМАЕМ ВСЕГДА, даже у закрытого. Диалог уезжает в общую очередь,
    # то есть перестаёт быть чьим-либо; оставленный срок разбудил бы его через
    # час второй раз — уже ничьим и, возможно, уже принятым другим человеком.
    status_dict.clear_snooze(conv)
    if conv.status != CLOSED:
        status_dict.set_status(conv, NEW, now=now)
    conv.offered_at = notify_svc.as_utc(conv.awaiting_since) or now
    if not keep_declines:
        conv.declined_by = []
        conv.escalated_at = None


# ------------------------------------------------------------------------ выборка


@dataclass(slots=True)
class InboxFilters:
    """Фильтры вкладки «Входящие». Растёт в 7.2 (каналы оператора)."""

    account_id: uuid.UUID | None = None
    limit: int = DEFAULT_LIMIT
    offset: int = 0


def inbox_item(
    conv: Conversation,
    *,
    account: Any,
    client: Any,
    last_message: Message | None,
    now: datetime | None = None,
    users: dict[uuid.UUID, User] | None = None,
    viewer_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """Строка очереди: обычный ConversationOut (01 §5.1) + поля ожидания.

    Форма берётся из ``conversation_out``, а не собирается заново: у списка
    диалогов и у очереди не должно быть шанса разойтись в полях клиента,
    объявления и последнего сообщения.
    """
    # У строки очереди ответственного нет, кроме одного случая: передача,
    # предложенная смотрящему. Тогда диалог ещё за передающим, и строка обязана
    # это показать вместе с именами, а не выглядеть ничейной.
    users = users or {}
    out = convs.conversation_out(
        conv,
        account=account,
        client=client,
        assignee=users.get(conv.assignee_id) if conv.assignee_id else None,
        last_message=last_message,
        transferred_to_me=viewer_id is not None and conv.transfer_to_id == viewer_id,
        users=users,
    )
    waited = waiting_seconds(conv, now=now)
    out.update(
        {
            "offered_at": iso(conv.offered_at),
            "waiting_seconds": waited,
            "waiting_human": format_wait(waited) if waited is not None else None,
            "declined_count": len(conv.declined_by or []),
            # «Отказались все, кому диалог доступен» — пометка для очереди (7.1).
            "escalated": conv.escalated_at is not None,
        }
    )
    return out


async def inbox_frame(
    db: AsyncSession, conv: Conversation, *, now: datetime | None = None
) -> dict[str, Any]:
    """Строка очереди для ОДНОГО диалога — тело кадра ``inbox:new`` (08 §5).

    Нужна там, где диалог встаёт в очередь не из ручки очереди, а из конвейера:
    входящее сообщение (новый чат, вернувшийся клиент) и передача от бота. У
    только что подключившегося оператора этой строки нет вовсе, поэтому кадр
    везёт диалог целиком, и собран он обязан быть ТЕМ ЖЕ кодом, что и строки
    ``GET /inbox`` — иначе список после вставки поедет другими полями.

    Зовётся ДО commit'а (строка ещё в транзакции, связанные сущности видны), а
    публикуется готовый словарь строго после (08 §8.1).
    """
    related = await convs._load_related(db, [conv])
    строка = inbox_item(
        conv,
        account=related["accounts"].get(conv.account_id),
        client=related["clients"].get(conv.client_id),
        last_message=related["last_messages"].get(conv.id),
        now=now,
    )
    # ⚠ БЕЙДЖ НЕПРОЧИТАННОГО — СОСТОЯНИЕ ЧЕЛОВЕКА, А КАДР ОДИН НА ВСЕХ.
    #
    # `conversation_out` кладёт сюда колонку `conversations.unread_count`. В
    # ручках поверх неё ложится пер-юзерный счёт (`apply_unread_counts`), а у
    # кадра такой возможности нет: он широковещательный, одно тело на всех
    # допущенных операторов. Хуже того, колонку сегодня НИКТО НЕ УВЕЛИЧИВАЕТ —
    # с переездом на маркеры чтения её только обнуляют (новый диалог, закрытие,
    # миграция 0026). То есть в кадре ехало либо ноль, либо число, замёрзшее
    # до переезда, — и фронт ставил его в строку очереди всем подряд
    # (`insertInboxRow` дубли не сливает, а пропускает).
    #
    # Ноль здесь — не «прочитано», а «кадр про это не знает»: настоящий счёт
    # приедет ближайшей выборкой списка, где он считается по маркеру смотрящего.
    строка["unread_count"] = 0
    return строка


async def list_inbox(
    db: AsyncSession,
    user: User,
    filters: InboxFilters | None = None,
    *,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Очередь диалогов, ждущих принятия: items + total.

    Сортировка одна и не настраивается: дольше всех ждущий — первым. Это не
    украшение, а суть очереди — оператор жмёт «Принять» на верхней строке и
    берёт того, кто ждёт дольше всех.

    «ЖДЁТ» — ГЛАЗАМИ КЛИЕНТА, А НЕ ОЧЕРЕДИ (владелец 14.09: «входящие идут
    вразнобой — 1 ч, 2 ч, 1 ч, 45 мин»). Раньше порядок шёл по `offered_at`
    — когда диалог встал в очередь, — а плашка в строке считается от
    `waiting_since`, момента, с которого ждёт клиент (канон
    `conversation_status.waiting_since`). Диалог, вернувшийся в очередь час
    назад с клиентом, написавшим два часа назад, стоял ниже свежих. Теперь
    выборка идёт по `awaiting_since` (серверная отметка ожидания клиента),
    а страница досортировывается тем же расчётом, что и плашка, — с учётом
    последнего сообщения клиента, которое уже загружено для превью.

    Читать очередь может любой, кто читает диалоги: руководителю полезно
    видеть, что именно не разбирается, и передать диалог руками (01 §5.5).
    Принимать — нет; это проверяет :func:`claim`.
    """
    filters = filters or InboxFilters()
    now = now or datetime.now(UTC)
    limit = max(1, min(filters.limit, MAX_LIMIT))
    offset = max(0, filters.offset)

    conds: list[sa.ColumnElement[bool]] = [visible_queue_condition(db, user, now=now)]
    if filters.account_id is not None:
        conds.append(Conversation.account_id == filters.account_id)

    total = (
        await db.execute(select(sa.func.count()).select_from(Conversation).where(*conds))
    ).scalar_one()
    rows = list(
        (
            await db.execute(
                select(Conversation)
                .where(*conds)
                # `offered_at` — запасной якорь для строк без отметки ожидания
                # (заведены до 7.1); id в хвосте — стабильная пагинация: у
                # диалогов, пришедших одним пакетом, метка совпадает до
                # микросекунды.
                .order_by(
                    sa.func.coalesce(Conversation.awaiting_since, Conversation.offered_at).asc(),
                    Conversation.offered_at.asc(),
                    Conversation.id,
                )
                .limit(limit)
                .offset(offset)
            )
        ).scalars()
    )

    # Батч-загрузчик списка диалогов: один и тот же набор связанных сущностей,
    # один и тот же запрет N+1 (01 §5.1). Своей копии заводить нельзя — разъедется.
    related = await convs._load_related(db, rows)
    rows.sort(key=lambda conv: queue_anchor(conv, related["last_messages"].get(conv.id)))
    items = [
        inbox_item(
            conv,
            account=related["accounts"].get(conv.account_id),
            client=related["clients"].get(conv.client_id),
            last_message=related["last_messages"].get(conv.id),
            now=now,
            users=related["users"],
            viewer_id=user.id,
        )
        for conv in rows
    ]
    return items, total


async def inbox_count(
    db: AsyncSession,
    user: User,
    filters: InboxFilters | None = None,
    *,
    now: datetime | None = None,
) -> int:
    """Число диалогов, ждущих ЭТОГО оператора, — бейдж вкладки «Входящие».

    Счётчик персональный: отклонённое этим оператором в него не входит — иначе
    бейдж горит на диалогах, которых человек в очереди не увидит.
    """
    return (await inbox_counts(db, user, filters, now=now))["waiting"]


async def inbox_counts(
    db: AsyncSession,
    user: User,
    filters: InboxFilters | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, int]:
    """То же с разбивкой: сколько ждёт и сколько уже эскалировано (14 §3).

    Отдельная функция, а не расширение :func:`inbox_count`: бейдж вкладки — это
    одно число, и ручка счётчика не должна платить вторым запросом за цифру,
    которую показывает только шапка очереди.
    """
    filters = filters or InboxFilters()
    conds: list[sa.ColumnElement[bool]] = [visible_queue_condition(db, user, now=now)]
    if filters.account_id is not None:
        conds.append(Conversation.account_id == filters.account_id)
    total = (
        await db.execute(select(sa.func.count()).select_from(Conversation).where(*conds))
    ).scalar_one()
    escalated = (
        await db.execute(
            select(sa.func.count())
            .select_from(Conversation)
            .where(*conds, Conversation.escalated_at.is_not(None))
        )
    ).scalar_one()
    return {"waiting": int(total), "escalated": int(escalated)}


# ------------------------------------------------------------------------- права


def _assert_can_take(user: User) -> None:
    """Кто может принимать и отклонять — те, кто отвечает клиентам.

    Критерий — право ``messages:send`` из матрицы (01 §12), а не сравнение роли
    со строкой: наблюдатель только читает, руководитель клиентам не пишет и
    берёт диалог не «на себя», а передаёт (01 §5.5). Новая роль без права
    отвечать не должна молча получить кнопку «Принять».
    """
    if convs.can_answer_clients(user.role):
        return
    raise ApiError(
        "forbidden",
        "Принять диалог может только тот, кто отвечает клиентам — назначьте менеджера",
        status=403,
        details={"reason": "cannot_answer_clients", "role": user.role},
    )


def _assert_takes_part_in_distribution(user: User) -> None:
    """Второе условие ПРИНЯТИЯ: человек вообще участвует в раздаче диалогов (7.4).

    ЧТО БЫЛО. Признак `handles_conversations` проверяли четыре выборки
    «кому система вправе ДАТЬ диалог» (`convs.operator_pool_conditions`), а
    «Принять» во «Входящих» — ни одна. На боевой системе это дало ровно то,
    ради чего признак и заводили: у «Администратора Lead Partner» галка «Ведёт
    диалоги» снята, а в 14:27 он взял из очереди четыре диалога
    (`conversation.assigned`, `by: self`, `source: inbox`). Дальше система
    сама себе противоречила: список «кому передать» его не показывал, и
    карточка диалога писала «Этого сотрудника больше нет среди назначаемых» —
    про человека, у которого прямо сейчас четыре диалога.

    ПОЧЕМУ ЗАПРЕТ ЗДЕСЬ, А НЕ ВОЗВРАТ ЕГО В СПИСОК НАЗНАЧАЕМЫХ. Развилка была
    именно такая, и второй путь хуже. Список назначаемых отвечает на вопрос
    «кому МОЖНО дать диалог»; пустить туда человека на том основании, что
    диалоги у него уже есть, — значит сделать из последствия ошибки её
    оправдание: система предложила бы отдать ему пятый диалог, потому что
    четыре она уже отдала зря. Очередь же — это раздача, просто ручная:
    диалог в ней ничей, и «Принять» отдаёт его точно так же, как автораздача.
    Признак снимает человека с раздачи — значит и отсюда.

    ЧТО ПРИ ЭТОМ НЕ ОТНИМАЕТСЯ. Право отвечать даёт роль, и оно остаётся
    целиком (`can_answer_clients`, docstring `operator_pool_conditions`):
    администратор, которого позвали разобрать затор, пишет клиенту в диалоге,
    где он ответственный или куда его назначили. `release` эту проверку не
    зовёт намеренно — те четыре диалога надо уметь вернуть в очередь, иначе
    правка заперла бы их у человека, который их не ведёт. `decline` тоже: он
    ничего не отдаёт, а эскалация «отказались все» его и так не ждёт
    (`eligible_operator_ids` считает по тому же пулу).

    ГЕРМЕТИЧНЫМ ЭТОТ БАРЬЕР НЕ БУДЕТ, и врать об этом не надо: путь «ответил —
    значит принял» (`services.messages`, 01 §6.2) назначает диалог тому, кто
    написал в него первым, независимо от признака. Это осознанно оставлено
    как есть: ответ клиенту — сознательное вмешательство в конкретный диалог,
    а не участие в раздаче, и бросить клиента без хозяина после ответа хуже.
    Здесь закрывается кнопка «Принять» — то есть механизм раздачи.
    """
    if user.handles_conversations:
        return
    raise ApiError(
        "forbidden",
        "Вы выведены из работы с диалогами — брать их из очереди нельзя. "
        "Диалог возьмёт оператор; если нужно вмешаться, назначьте ответственного",
        status=403,
        details={"reason": "does_not_handle_conversations", "role": user.role},
    )


# ------------------------------------------------------------------- вспомогательное


async def _reload(db: AsyncSession, conversation_id: uuid.UUID) -> Conversation | None:
    """Перечитать строку МИМО identity map.

    Обязательно ``populate_existing``: проигравший гонку уже держит свой объект
    диалога в сессии, и обычный ``db.get`` вернул бы ему его же устаревшую
    копию — с ``claimed_by_id IS NULL``. Тогда сообщение об ошибке называло бы
    победителем «никого».
    """
    return (
        (
            await db.execute(
                select(Conversation)
                .where(Conversation.id == conversation_id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )


async def _holder(db: AsyncSession, conv: Conversation) -> User | None:
    holder_id = conv.claimed_by_id or conv.assignee_id
    return await db.get(User, holder_id) if holder_id else None


async def _raise_not_claimable(db: AsyncSession, conv: Conversation, user: User) -> NoReturn:
    """Единый разбор «почему диалог взять нельзя» — понятным текстом.

    ``already_claimed`` — код, ради которого всё и затевалось: второй оператор
    обязан увидеть ИМЯ того, кто успел, а не «конфликт данных».
    """
    if conv.status == CLOSED:
        raise ApiError(
            "unprocessable",
            "Диалог закрыт — принимать нечего",
            status=422,
            details={"reason": "conversation_closed"},
        )
    holder = await _holder(db, conv)
    if holder is None:
        # Ни закрыт, ни занят — значит диалог в очередь и не ставили.
        raise ApiError(
            "unprocessable",
            "Диалог не ждёт принятия",
            status=422,
            details={"reason": "not_in_inbox"},
        )
    raise ApiError(
        "already_claimed",
        f"Диалог уже принят: {holder.full_name}"
        if holder.id != user.id
        else "Вы уже приняли этот диалог",
        status=409,
        details={
            "reason": "already_claimed",
            "claimed_by": user_ref(holder),
            # Фронту достаточно этого флага, чтобы второй клик по «Принять»
            # своей же кнопкой не выглядел ошибкой (11 §2).
            "mine": holder.id == user.id,
        },
    )


# ----------------------------------------------------------------------- принятие


@dataclass(slots=True)
class ClaimResult:
    conversation: Conversation
    system_message: Message
    actor: User
    waited_seconds: int | None = None


async def claim(
    db: AsyncSession, conversation_id: uuid.UUID, user: User, *, now: datetime | None = None
) -> ClaimResult:
    """АТОМАРНОЕ принятие диалога. Успех — диалог ваш; иначе ``already_claimed``.

    Гонка решается ОДНИМ оператором SQL::

        UPDATE conversations SET claimed_by_id = :me, assignee_id = :me, ...
         WHERE id = :id AND claimed_by_id IS NULL AND assignee_id IS NULL
           AND status <> 'closed'

    и проверкой числа затронутых строк. Второй оператор упирается в блокировку
    строки, после коммита первого перечитывает условие уже по новому снимку,
    получает ноль строк — и видит «диалог уже принят: Имя».

    Почему не «SELECT, проверил, UPDATE»: между чтением и записью помещается
    вся вторая транзакция целиком. На тринадцати операторах это не редкий
    случай, а норма дня.

    ``SELECT`` перед обновлением здесь всё-таки есть — ровно для двух вещей:
    честный 404 на несуществующий диалог и значение ``from`` для записи журнала.
    Решение о том, КТО взял диалог, он не принимает и принять не может: его
    результат нигде не проверяется как условие.

    ``offered_at IS NOT NULL`` в условие захвата НЕ входит намеренно, хотя в
    условие очереди входит. «Взять свободный диалог по ссылке» должно работать
    и для строк, которые в очередь не ставили (легаси до 7.1, путь создания,
    который забыли научить :func:`enter_queue`): запретить это значило бы
    получить диалог, который никому не показывается и который никто не может
    взять. В очереди он от этого не появляется — предикат выборки свой.

    Повтор захвата (ровно один) нужен для третьей перестановки, кроме «взял» и
    «не успел»: диалог освободили между нашим ``UPDATE`` и перечиткой. Без
    повтора проигравший получал бы 422 «диалог не ждёт принятия» — сообщение,
    которое в этот момент уже неправда.
    """
    _assert_can_take(user)
    # Роль разрешает отвечать, признак — участвовать в раздаче. У принятия
    # обязаны совпасть оба: см. `_assert_takes_part_in_distribution`.
    _assert_takes_part_in_distribution(user)
    now = now or datetime.now(UTC)

    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise ApiError("not_found", "Диалог не найден", status=404)

    # «ЭТОТ КАНАЛ ВАШ?» — ПРОВЕРКА, КОТОРОЙ ЗДЕСЬ НЕ БЫЛО.
    #
    # НАЙДЕНО 12 августа обходом кода. `assert_can_take_account` был написан,
    # снабжён докстрингом с объяснением, ЗАЧЕМ он нужен, и покрыт восемью
    # тестами — но не вызывался НИ ИЗ ОДНОЙ боевой строки. Тесты звали его
    # напрямую и потому были зелёными: проверялся сам страж, а не то, что он
    # стоит на дороге.
    #
    # Его же докстринг и описывает цену пропуска: «Очередь уже отфильтрована,
    # но прятать кнопку недостаточно: id диалога виден в ссылке, в кадре
    # `inbox:new` и в списке "Все", а принятие — обычный POST. Без этой
    # проверки фильтр очереди остаётся оформлением, а не правилом». Так оно и
    # было: у заказчика девять каналов и тринадцать диспетчеров, назначенных
    # по каналам, и любой из них мог принять чужой диалог по прямой ссылке.
    #
    # Место выбрано до захвата: отказывать надо ДО того, как строка помечена
    # нашей, иначе придётся откатывать уже сделанное назначение.

    await acc_ops.assert_can_take_account(db, user, conv.account_id)

    previous_status = conv.status
    waited = waiting_seconds(conv, now=now)

    async def _try_take() -> int:
        result = await db.execute(
            sa.update(Conversation)
            .where(
                Conversation.id == conversation_id,
                Conversation.claimed_by_id.is_(None),
                Conversation.assignee_id.is_(None),
                Conversation.status != CLOSED,
            )
            .values(
                claimed_by_id=user.id,
                claimed_at=now,
                # Принятие И назначает: дальше диалог живёт по обычным правилам —
                # виден во вкладке «Мои», доступен передаче (01 §5.5), считается
                # статистикой. Очередь не заводит параллельной вселенной.
                assignee_id=user.id,
                status=IN_PROGRESS,
                # `status_since` пишется здесь ЯВНО, а не через
                # `conversation_status.set_status`: захват — единственный путь
                # смены статуса одним `UPDATE ... WHERE`, и он такой намеренно
                # (это арбитр гонки, см. докстринг). Разбирать строку в Python,
                # чтобы позвать общую функцию, значило бы разменять
                # атомарность на единообразие.
                status_since=now,
                # И отложку гасим тем же запросом. Отложенный диалог в очереди
                # лежать не может по построению — у него есть хозяин, — но
                # если он туда попал, взять его должно быть можно, и взятым он
                # обязан оказаться без срока возврата.
                snoozed_until=None,
                snoozed_by_id=None,
                snooze_reason=None,
                # «Никто не берёт» перестало быть правдой: пометка скрывала
                # счётчик непрочитанного в строке уже принятого диалога.
                escalated_at=None,
                # человек взял диалог — бот отходит тем же атомарным ударом:
                # иначе оба писали клиенту параллельно (аудит 16.08)
                bot_active=False,
            )
            .execution_options(synchronize_session=False)
        )
        # rowcount живёт на CursorResult; типизированный Result его не обещает.
        return int(getattr(result, "rowcount", 0))

    taken = await _try_take()
    if taken != 1:
        fresh = await _reload(db, conversation_id)
        if fresh is None:
            raise ApiError("not_found", "Диалог не найден", status=404)
        # Один повтор перед тем, как объяснять отказ. Между UPDATE'ом и
        # перечиткой диалог могли ОСВОБОДИТЬ (коллега передумал, руководитель
        # снял ответственного), и тогда объяснение оказалось бы враньём:
        # «диалог не ждёт принятия» про диалог, который стоит в очереди прямо
        # сейчас. Повтор оставляет ровно два честных исхода — либо взял, либо
        # «уже принят: Имя». Второго повтора нет намеренно: это не спинлок, а
        # разрешение одной конкретной перестановки.
        if fresh.claimed_by_id is None and fresh.assignee_id is None and fresh.status != CLOSED:
            taken = await _try_take()
            if taken == 1:
                log.info(
                    "inbox.claim_won_on_retry",
                    conversation_id=str(conversation_id),
                    user_id=str(user.id),
                )
                # Строка успела пожить своей жизнью — журнал и «ждал N» берём
                # от неё, а не от снимка, прочитанного до гонки.
                previous_status = fresh.status
                waited = waiting_seconds(fresh, now=now)
    if taken != 1:
        log.info("inbox.claim_lost", conversation_id=str(conversation_id), user_id=str(user.id))
        holder_row = await _reload(db, conversation_id)
        if holder_row is None:
            raise ApiError("not_found", "Диалог не найден", status=404)
        await _raise_not_claimable(db, holder_row, user)

    conv = await _reload(db, conversation_id)
    assert conv is not None  # строку только что обновили в этой же транзакции

    # СНИМАЕМ СВОЙ ОТКАЗ — И ТОЛЬКО СВОЙ.
    #
    # Отказ персональный: он прячет диалог из «Входящих» того, кто отказался.
    # Снимался он в `enter_queue`, `return_to_queue` и автораздаче — но НЕ при
    # взятии. Получалось противоречие: человек диалог ВЗЯЛ, а система
    # продолжала считать, что он от него отказался.
    #
    # Цена на боевой системе (12 августа): клиент Иван отклонён в 03:25,
    # дальше диалог трижды принимали и возвращали в очередь — и он всё равно
    # оставался невидимым во «Входящих», вися ничьим с тикающим таймером
    # 7 часов 37 минут. Взять его можно было только случайно найдя во вкладке
    # «Все», а снять отказ — исключительно кнопкой в тосте на несколько секунд.
    #
    # ЧУЖИЕ ОТКАЗЫ НЕ ТРОГАЕМ, и это не мелочь. Первая версия правки обнуляла
    # весь список прямо в атомарном UPDATE — и её поймал существующий тест
    # `test_release_keeps_the_declines`: «отказавшийся не обязан видеть диалог
    # снова только потому, что его кто-то подержал и вернул». Тест прав: отказ
    # принадлежит тому, кто отказался, и стирать его чужим действием нельзя.
    #
    # Пишется ПОСЛЕ захвата, а не внутри него, намеренно: тот UPDATE — арбитр
    # гонки, и класть в него dialect-специфичный `array_remove` значило бы
    # усложнять единственное место, где решается «кто успел первым». Строка
    # здесь уже наша, второй записи ничто не мешает.
    mine = str(user.id)
    declines = [str(x) for x in (conv.declined_by or [])]
    if mine in declines:
        conv.declined_by = [x for x in declines if x != mine]

    body = f"Диалог принят: {user.full_name}"
    if waited is not None:
        body = f"{body}. Ждал {format_wait(waited)}"
    # Своей копии системной записи не заводим: правило «last_message_at не
    # двигаем» (01 §5.1) должно жить в одном месте на весь бэкенд.
    system_message = convs.add_system_message(db, conv, body)

    await write_audit(
        db,
        user_id=user.id,
        action="conversation.assigned",
        entity="conversation",
        entity_id=str(conv.id),
        details={
            "assignee_id": str(user.id),
            "prev_assignee_id": None,  # в очереди ответственного не бывает
            "by": "self",  # 06 §0.3: «Диалог взят в работу»
            "source": "inbox",  # принят из очереди, а не по первому ответу
            "waited_seconds": waited,
        },
    )
    if previous_status != IN_PROGRESS:
        await write_audit(
            db,
            user_id=user.id,
            action="conversation.status_changed",
            entity="conversation",
            entity_id=str(conv.id),
            details={
                "from": previous_status,
                "to": IN_PROGRESS,
                "by": "operator",
                "assignee_id": str(user.id),
            },
        )
    # Диалог принят — значит «его никто не принял» перестало быть правдой.
    # Гасим ЗДЕСЬ, а не в роуте: принять диалог можно и из очереди, и из ленты,
    # и оба пути проходят через эту функцию. См. `notifications.resolve_conversation`.
    await notify_svc.resolve_conversation(db, conv.id)

    await db.flush()
    return ClaimResult(
        conversation=conv, system_message=system_message, actor=user, waited_seconds=waited
    )


# --------------------------------------------------------------------- отклонение


@dataclass(slots=True)
class DeclineResult:
    conversation: Conversation
    actor: User
    system_message: Message | None = None
    already_declined: bool = False
    escalated: bool = False
    declined_count: int = 0
    notification: notify_svc.NotifyResult | None = None


def _clean_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    reason = reason.strip()
    if not reason:
        return None
    if len(reason) > MAX_DECLINE_REASON_LEN:
        raise ApiError(
            "payload_too_large",
            f"Причина длиннее {MAX_DECLINE_REASON_LEN} символов",
            status=413,
            details={"limit": MAX_DECLINE_REASON_LEN, "length": len(reason)},
        )
    return reason


async def inbox_frame_addressed(
    db: AsyncSession, conv: Conversation, *, now: datetime | None = None
) -> dict[str, Any]:
    """Готовый кадр `inbox:new` ВМЕСТЕ со списком тех, кому он предназначен.

    ⚠ ЗАЧЕМ ЭТО ЗАВЕДЕНО (аудит 19.08). Канальный фильтр кадров существовал и
    был мёртв: из семи боевых публикаторов `inbox:new` список допущенных
    проставлял РОВНО ОДИН. Пустой список по правилу совместимости означает
    «канал открыт всем» — и кадр очереди чужого канала уезжал каждому. Менеджер
    слышал звук на чужого клиента, видел чужую строку, жал «Принять» и получал
    403; своя строка тонула в чужом потоке. На двух каналах это терпимо, на
    девяти — то самое смешение аккаунтов, ради которого фильтр и писали.

    Собирать кадр и список раздельно в шести местах — верный способ снова
    разъехаться, поэтому здесь они собираются вместе и всегда.
    """
    return {
        "conversation": await inbox_frame(db, conv, now=now),
        "eligible": sorted(await eligible_operator_ids(db, conv)),
    }


async def inbox_frames_addressed(
    db: AsyncSession, диалоги: list[Conversation], *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """То же, что :func:`inbox_frame_addressed`, но на ПАЧКУ диалогов.

    ⚠ ЗАЧЕМ. Поштучный вариант делает на каждый диалог свой `_load_related`
    (аккаунт, клиент, ответственный, последнее сообщение) и свой запрос
    допущенных — пять-шесть рейсов до базы на строку. В одном месте это
    оказалось внутри цикла по ВСЕМ открытым диалогам удаляемого сотрудника, а
    предела у той выборки нет: у диспетчера с тремя сотнями диалогов удаление
    превращалось в полторы тысячи запросов в одном запросе ручки. Ни ошибки, ни
    строчки в журнале — администратор просто смотрит на крутящуюся кнопку и
    жмёт её второй раз.

    Связанные сущности берём ОДНИМ заходом (тот же `_load_related`, что у
    страницы списка), а допущенных — по УНИКАЛЬНЫМ каналам: каналов единицы, а
    диалогов на них сотни, и список допущенных зависит только от канала
    (см. :func:`eligible_operator_ids`).

    Порядок сохраняем: кадры уезжают в том же порядке, в каком пришли диалоги.
    """
    if not диалоги:
        return []
    related = await convs._load_related(db, диалоги)
    по_каналам: dict[uuid.UUID, list[str]] = {}
    for conv in диалоги:
        if conv.account_id not in по_каналам:
            по_каналам[conv.account_id] = sorted(await eligible_operator_ids(db, conv))
    return [
        {
            "conversation": inbox_item(
                conv,
                account=related["accounts"].get(conv.account_id),
                client=related["clients"].get(conv.client_id),
                last_message=related["last_messages"].get(conv.id),
                now=now,
            )
            | {"unread_count": 0},  # кадр один на всех — см. `inbox_frame`
            "eligible": по_каналам[conv.account_id],
        }
        for conv in диалоги
    ]


async def eligible_operator_ids(db: AsyncSession, conv: Conversation) -> set[str]:
    """Кому диалог реально доступен — операторы ЕГО канала (7.2).

    От этого множества зависит эскалация «отказались все»: она срабатывает,
    когда отказались все, кому диалог доступен. До 7.2 здесь возвращались все
    активные сотрудники, умеющие отвечать клиентам, — и на девяти каналах это
    означало бы, что диалог с пометкой «никто не берёт» ждёт отказа от
    тринадцати человек, десять из которых его в очереди даже не видят. Такая
    эскалация не сработает никогда, то есть её нет.

    Пустой набор назначений на канале означает «канал доступен ВСЕМ
    операторам» (правило совместимости, ``account_operators``) — тогда состав
    ровно тот, что был до 7.2. Ради этого случая fallback и написан явно: без
    него первое же включение фильтрации выключило бы эскалацию на всех
    каналах, где никого не назначили.

    «Оператор канала» здесь и «канал доступен мне» в
    :func:`visible_queue_condition` — одно определение (активен + право
    ``messages:send`` + назначен). Разъезд означал бы диалог, который
    эскалация считает разобранным, а очередь никому не показывает.
    """
    assigned = await acc_ops.operator_ids_for_account(db, conv.account_id)
    if assigned:
        return assigned
    rows = await db.execute(select(User.id).where(*convs.operator_pool_conditions()))
    return {str(i) for i in rows.scalars()}


async def decline(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    user: User,
    reason: str | None = None,
    *,
    now: datetime | None = None,
) -> DeclineResult:
    """Отказаться вести диалог: он остаётся в очереди, но не у отказавшегося.

    Отказ персональный и хранится в ``declined_by``: если прятать диалог только
    в интерфейсе, оператор будет натыкаться на него после каждого обновления
    списка.

    Когда отказались ВСЕ, кому диалог доступен, диалог не исчезает (это был бы
    брошенный клиент) — он остаётся в очереди с пометкой, а администраторы
    получают уведомление (14). Пометка ставится один раз: следующий отказ
    нового сотрудника не должен звонить второй раз.

    Строка берётся ``FOR UPDATE`` (08 §8.4): ``declined_by`` — список, и без
    блокировки два одновременных отказа затирают друг друга.
    """
    _assert_can_take(user)
    now = now or datetime.now(UTC)
    reason = _clean_reason(reason)

    conv = await convs.get_conversation_for_update(db, conversation_id)
    if not is_waiting(conv):
        await _raise_not_claimable(db, conv, user)

    declined = [str(x) for x in (conv.declined_by or [])]

    # ⚠ ИДЕМПОТЕНТНОСТЬ СЧИТАЕТСЯ ПО ДЕЙСТВУЮЩЕМУ ОТКАЗУ, А НЕ ПО СПИСКУ ОТКАЗЫВАВШИХСЯ.
    # Раньше хватало проверки «я есть в declined_by», потому что отказ был вечным. Теперь он
    # живёт три минуты, и та же проверка сломала бы саму задачу: диалог вернулся человеку в
    # очередь, он отказался второй раз — а система отвечает «уже отказывался» и НИЧЕГО не
    # прячет. Дубль — это два нажатия подряд (двойной клик, две вкладки), и отличает его
    # именно свежесть предыдущего отказа.
    существующий = await db.scalar(
        select(ConversationDecline).where(
            ConversationDecline.conversation_id == conv.id,
            ConversationDecline.user_id == user.id,
        )
    )
    # ⚠ ``as_utc``: SQLite юнит-тестов отдаёт наивные метки, сравнение с aware падает
    # TypeError'ом (07 §1.1). Тот же приём, что и во всех остальных сравнениях времени.
    прежний_момент = notify_svc.as_utc(существующий.declined_at) if существующий else None
    if прежний_момент is not None and прежний_момент > now - DECLINE_TTL:
        return DeclineResult(
            conversation=conv,
            actor=user,
            already_declined=True,
            declined_count=len(declined),
        )
    if существующий is None:
        db.add(ConversationDecline(conversation_id=conv.id, user_id=user.id, declined_at=now))
    else:
        существующий.declined_at = now  # отказ продлевается, а не заводится второй строкой

    # ⚠ МАССИВ ПРОДОЛЖАЕМ ВЕСТИ, И ЭТО НЕ ДУБЛЬ ХРАНЕНИЯ. Он отвечает на другой вопрос — «кто
    # вообще отказывался», и по нему считается эскалация «отказались ВСЕ, кому диалог доступен»
    # (14 §2.3). Считай круг по истекающим отказам — и через три минуты после последнего
    # отказа круг рассыпался бы сам, а администраторов не позвали бы никогда.
    # Плюс регламент выкатки: старый код на откате продолжает читать именно этот массив.
    if str(user.id) not in declined:
        declined.append(str(user.id))
        conv.declined_by = declined  # новый список — иначе ORM не увидит изменения

    body = f"Диалог отклонён: {user.full_name}"
    if reason:
        body = f"{body}. Причина: {reason}"
    system_message = convs.add_system_message(db, conv, body)

    # Отказ — решение человека о клиенте, и в журнале ему место рядом с
    # принятием: «диалог висел час, потому что от него отказались четверо» — это
    # разбор смены, а не строка в ленте одного диалога (01 §9.7).
    await write_audit(
        db,
        user_id=user.id,
        action="conversation.declined",
        entity="conversation",
        entity_id=str(conv.id),
        details={
            "reason": reason,
            "declined_count": len(declined),
            "waited_seconds": waiting_seconds(conv, now=now),
        },
    )

    escalated = False
    notification: notify_svc.NotifyResult | None = None
    if conv.escalated_at is None:
        eligible = await eligible_operator_ids(db, conv)
        if eligible and eligible <= set(declined):
            conv.escalated_at = now
            escalated = True
            notification = await _escalate(db, conv, reason=reason, now=now)

    await db.flush()
    return DeclineResult(
        conversation=conv,
        actor=user,
        system_message=system_message,
        escalated=escalated,
        declined_count=len(declined),
        notification=notification,
    )


async def undo_decline(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    user: User,
    *,
    now: datetime | None = None,
) -> DeclineResult:
    """Забрать отказ обратно (UX-аудит, docs/17 §Т7).

    Зачем это появилось. Отказ стал одним нажатием (Ctrl+Backspace), то есть
    промахнуться теперь легко — а последствие у промаха необратимое: диалог
    навсегда уходит из МОЕЙ очереди, и вернуть его себе нечем. Коллегам он
    остаётся виден, так что клиент не брошен, но конкретный оператор теряет
    обращение, которое собирался взять. Отмена делает риск обратимым.

    Времени на отмену НЕ ограничиваем. Соблазн «только 5 секунд, пока висит
    тост» понятен, но опоздавший отказ ничем не вреден: пока диалог ждёт в
    очереди, вернуть его себе безопасно в любой момент, а лишнее ограничение
    пришлось бы объяснять человеку в тот момент, когда он и так ошибся.

    ЭСКАЛАЦИЯ. Если мой отказ был последним и включил пометку «никто не
    берёт», отмена её СНИМАЕТ: утверждение «все отказались» перестало быть
    правдой, а держать на диалоге неверную пометку — хуже, чем не ставить её
    вовсе. Уже отправленное администраторам уведомление, разумеется, не
    отзывается; если потом откажутся снова, эскалация сработает заново и
    склеится с прежней строкой по тому же ключу (14 §4).
    """
    _assert_can_take(user)
    now = now or datetime.now(UTC)

    conv = await convs.get_conversation_for_update(db, conversation_id)
    # Диалог мог уйти из очереди, пока висел тост: его приняли, закрыли,
    # передали. Тогда возвращать нечего, и сказать об этом надо прямо.
    if not is_waiting(conv):
        await _raise_not_claimable(db, conv, user)

    declined = [str(x) for x in (conv.declined_by or [])]
    if str(user.id) not in declined:
        # Идемпотентность — как у самого отказа: повторная отмена не плодит
        # записей в ленте и не считается ошибкой.
        return DeclineResult(
            conversation=conv,
            actor=user,
            already_declined=False,
            declined_count=len(declined),
        )

    declined.remove(str(user.id))
    conv.declined_by = declined  # новый список — иначе ORM не увидит изменения
    # Снимаем и строку с моментом: без неё отмена вернула бы диалог в очередь только по
    # истечении трёх минут, то есть кнопка «Вернуть» три минуты ничего бы не делала.
    await db.execute(
        sa.delete(ConversationDecline).where(
            ConversationDecline.conversation_id == conv.id,
            ConversationDecline.user_id == user.id,
        )
    )

    was_escalated = conv.escalated_at is not None
    if was_escalated:
        conv.escalated_at = None

    system_message = convs.add_system_message(db, conv, f"Отказ отменён: {user.full_name}")

    await write_audit(
        db,
        user_id=user.id,
        action="conversation.decline_undone",
        entity="conversation",
        entity_id=str(conv.id),
        details={
            "declined_count": len(declined),
            "escalation_cleared": was_escalated,
        },
    )

    await db.flush()
    return DeclineResult(
        conversation=conv,
        actor=user,
        system_message=system_message,
        escalated=False,
        declined_count=len(declined),
    )


async def _escalate(
    db: AsyncSession, conv: Conversation, *, reason: str | None, now: datetime
) -> notify_svc.NotifyResult:
    """«Диалог никто не принял» — администраторам через центр уведомлений (14).

    Уведомление адресное по сущности: ключ склейки содержит id диалога, поэтому
    десять брошенных диалогов дадут десять строк, а повторная эскалация того же
    диалога — одну (14 §4).
    """
    client = await db.get(Client, conv.client_id)
    who = (client.name if client and client.name else None) or "клиент"
    waited = waiting_seconds(conv, now=now)
    body = f"Все операторы отказались от диалога — {who} ждёт"
    if waited is not None:
        body = f"{body} {format_wait(waited)}"
    body = f"{body}. Назначьте ответственного вручную."
    if reason:
        body = f"{body} Последняя причина отказа: {reason}"
    return await notify_svc.notify(
        db,
        kind=ESCALATION_KIND,
        severity="warning",
        audience="admin",
        title="Диалог никто не принял",
        body=body,
        entity_type="conversation",
        entity_id=str(conv.id),
        dedup_key=f"{ESCALATION_KIND}:{conv.id}",
        now=now,
    )


# ------------------------------------------------------------------------ возврат


@dataclass(slots=True)
class ReleaseResult:
    conversation: Conversation
    actor: User
    system_message: Message
    previous_status: str = NEW
    previous_assignee_id: uuid.UUID | None = None
    patch: dict[str, Any] = field(default_factory=dict)


async def release(
    db: AsyncSession, conversation_id: uuid.UUID, user: User, *, now: datetime | None = None
) -> ReleaseResult:
    """Вернуть принятый диалог в очередь — «передумал».

    Возвращает тот, кто держит диалог. Администратор может вернуть любой:
    у него и так есть передача (01 §5.5), а запрет тут означал бы «диалог
    уехавшего сотрудника не вытащить никак». Руководителю этот путь не нужен —
    он снимает ответственного передачей и делает это осознанно.

    Право проверяется здесь, а не только зависимостью ручки: сервис зовут не
    из одного места, и «вернуть в очередь» — то же действие над диалогом
    клиента, что принять и отклонить. Без этой строки наблюдателя отсекал бы
    один-единственный `Depends`, а руководитель, у которого путь возврата не
    предусмотрен вовсе, снимал бы ответственного мимо передачи (01 §5.5).
    """
    _assert_can_take(user)
    now = now or datetime.now(UTC)
    conv = await convs.get_conversation_for_update(db, conversation_id)

    holder_id = conv.claimed_by_id or conv.assignee_id
    if holder_id is None:
        raise ApiError(
            "unprocessable",
            "Диалог и так никем не принят",
            status=422,
            details={"reason": "not_claimed"},
        )
    if holder_id != user.id and user.role != "admin":
        holder = await _holder(db, conv)
        raise ApiError(
            "forbidden",
            "Вернуть диалог в очередь может тот, кто его принял",
            status=403,
            details={
                "reason": "not_holder",
                "claimed_by": user_ref(holder),
            },
        )

    previous_status = conv.status
    previous_assignee_id = conv.assignee_id
    return_to_queue(conv, now=now)

    body = f"Диалог возвращён во «Входящие»: {user.full_name}"
    system_message = convs.add_system_message(db, conv, body)

    await write_audit(
        db,
        user_id=user.id,
        action="conversation.assigned",
        entity="conversation",
        entity_id=str(conv.id),
        details={
            "assignee_id": None,  # 06 §0.3 → «С диалога снят ответственный»
            "prev_assignee_id": str(previous_assignee_id) if previous_assignee_id else None,
            "by": "self" if holder_id == user.id else "head",
            "source": "inbox",
        },
    )
    if previous_status != conv.status:
        await write_audit(
            db,
            user_id=user.id,
            action="conversation.status_changed",
            entity="conversation",
            entity_id=str(conv.id),
            details={
                "from": previous_status,
                "to": conv.status,
                "by": "operator",
                "assignee_id": None,
            },
        )
    await db.flush()
    return ReleaseResult(
        conversation=conv,
        actor=user,
        system_message=system_message,
        previous_status=previous_status,
        previous_assignee_id=previous_assignee_id,
        patch=queue_patch(conv),
    )


# ------------------------------------------------------- патчи строки для кадров
#
# Публикация — строго ПОСЛЕ commit'а (08 §8.1); её делает вызывающий endpoint
# (`app/api/routes/inbox.py`, каталог имён — `app/ws/hub.py`). Здесь только
# СОСТАВ дельты, потому что ровно та же дельта уходит в HTTP-ответ: собранная
# в двух местах, она разъезжается на первой же правке.


def queue_patch(conv: Conversation) -> dict[str, Any]:
    """Дельта строки списка (01 §11.3) — только то, что меняет очередь."""
    return {
        "status": conv.status,
        "assignee": None,
        "in_inbox": is_waiting(conv),
        "offered_at": iso(conv.offered_at),
        "escalated": conv.escalated_at is not None,
    }


def claimed_patch(conv: Conversation, actor: User) -> dict[str, Any]:
    return {
        "status": conv.status,
        "status_since": iso(conv.status_since),
        "assignee": user_ref(actor),
        "escalated": False,
        # Диалог занят: остальные тринадцать обязаны увидеть это немедленно —
        # ради этого вся 7.1 и делалась.
        "in_inbox": False,
        "claimed_by": user_ref(actor),
        "claimed_at": iso(conv.claimed_at),
    }
