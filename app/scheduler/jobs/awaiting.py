"""Клиент ждёт ответа во взятом диалоге: напоминание и эскалация.

1. ``REMIND_AFTER`` — личное напоминание тому, кто ведёт диалог.
2. ``ESCALATE_AFTER`` — рассылка руководителям. Если диалог ведёт сам
   руководитель (или администратор, он видит ту же рассылку), её нет: личное
   напоминание он уже получил, а выше него в системе никого.

Напоминания идут лестницей с удвоением ожидания (15 → 30 → 60 → 120 → 240 мин,
дальше раз в :data:`STEP_CAP`): повтор означает «клиент ждёт вдвое дольше», а
не «прошла минута». Шаг обязан быть меньше окна склейки важности ``warning``,
иначе каждая ступень заводит новую строку.

Диалог этот сторож не отбирает. Возврат диалогов отсутствующих сотрудников —
работа ``reclaim.release_unavailable``: у неё выключатель, список исключений и
выдержка, которые обещает экран «Распределение». Второй путь в обход них забирал
диалоги у исключённых, отнимал только что взятый диалог при обрыве связи и
молча снимал висящую передачу.

Здесь же — сторож очереди (:func:`check_queue`) и истечение отказов
(:func:`check_expired_declines`).
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from apscheduler.triggers.interval import IntervalTrigger

from app.core import redis as redis_mod
from app.db.session import session_scope
from app.models import Client, Conversation, ConversationDecline, User
from app.models.notification import Notification
from app.services import inbox as inbox_svc
from app.services import notifications as notify_svc
from app.ws.events import publish_event
from app.ws.hub import INBOX_NEW

log = structlog.get_logger(__name__)

JOB_ID = "awaiting_reply"
QUEUE_JOB_ID = "queue_unclaimed"
DECLINE_JOB_ID = "declines_expired"

#: Через сколько напомнить оператору. Пятнадцать минут — не норматив ответа, а
#: срок, после которого молчание перестаёт быть «сейчас допишу».
REMIND_AFTER = timedelta(minutes=15)

#: Через сколько сказать руководителю. Полчаса совпадает с порогом, который в
#: системе уже объявлен (`conversation.no_reply`, 14 §2.3), — заводить второй
#: срок для того же явления значило бы спорить с самим собой.
ESCALATE_AFTER = timedelta(minutes=30)

#: Через сколько сказать, что диалог в очереди никто не взял. Десять минут —
#: замер по живому корпусу, а не вежливость: ответ за пять минут даёт 19.6%
#: телефонов, через полчаса — 10%, через час-три — 8.1%. Подробнее — в докстринге
#: :func:`check_queue`.
QUEUE_WAIT_AFTER = timedelta(minutes=10)

#: Потолок шага лестницы напоминаний. До него шаг равен уже прожитому ожиданию
#: (то есть удваивает его), дальше — фиксирован. Обязан быть МЕНЬШЕ окна
#: склейки повторов у важности этих событий, иначе каждая ступень заводит новую
#: строку вместо повтора существующей; проверяется тестом.
STEP_CAP = timedelta(hours=4)

#: Сколько диалогов за прогон. Не потолок работы, а защита от того, чтобы
#: минутная задача не превратилась в получасовую после длинных выходных:
#: остаток разберётся следующей минутой.
BATCH = 200


def _reminders_due(waited: timedelta, first: timedelta) -> int:
    """Лестница ступеней с потолком :data:`STEP_CAP`.

    Сам расчёт переехал 14 августа в центр уведомлений
    (``notifications.reminders_due``): у сторожевых видов лестницы не было
    вовсе, счётчик рос каждые пять минут, и заводить ей ВТОРУЮ реализацию
    значило бы гарантированно разъехаться на первой же правке шага.

    Обёртка остаётся: она держит здешний потолок и здешнее имя, на которое
    смотрят и вызывающий код, и тесты про пересменку.
    """
    return notify_svc.reminders_due(waited, first, step_cap=STEP_CAP)


async def _already_sent(
    db: Any, kind: str, keys: list[str], moment: datetime
) -> dict[tuple[str, uuid.UUID | None], int]:
    """Сколько раз строка с таким ключом склейки уже показывалась человеку.

    Ключ ответа — пара «ключ склейки × адресат»: у `conversation.awaiting_you`
    ключ склейки одинаков для всех, кто вёл диалог по очереди, а строк у него
    столько, сколько было владельцев (адресное уведомление склеивается только
    в пределах одного получателя, см. `notifications._find_open_duplicate`).

    ОКНО И КЛЮЧ БЕРУТСЯ ИЗ ЦЕНТРА УВЕДОМЛЕНИЙ, А НЕ ПОВТОРЯЮТСЯ ЗДЕСЬ ЧИСЛОМ.
    Этот запрос обязан находить ровно ту строку, которую обновит ``notify``:
    разъедься условия — и лестница ступеней начнёт либо молчать (нашли чужую
    старую строку), либо звонить каждую минуту (не нашли живую). Поэтому окно
    считается от важности вида по `DEDUP_WINDOWS`, а ключ склейки вызывающий
    передаёт в ``notify`` явным `dedup_key` — тем же самым.
    """
    if not keys:
        return {}
    spec = notify_svc.spec_for(kind)
    severity = spec.severity if spec else "info"
    window = notify_svc.DEDUP_WINDOWS[severity]
    rows = (
        await db.execute(
            sa.select(
                Notification.dedup_key,
                Notification.recipient_id,
                sa.func.max(Notification.repeat_count),
            )
            .where(
                Notification.kind == kind,
                Notification.dedup_key.in_(keys),
                Notification.last_seen_at >= moment - window,
            )
            .group_by(Notification.dedup_key, Notification.recipient_id)
        )
    ).all()
    return {(key, recipient): int(count) for key, recipient, count in rows}


def _client_label(name: str | None, item_title: str | None) -> str:
    """Как назвать того, кто ждёт, чтобы две строки в колокольчике различались.

    Имя клиента — первый выбор: диспетчер узнаёт разговор по нему. Его может
    не быть (Авито отдаёт имя не всегда), тогда называем объявление — в этом
    деле оно и есть суть разговора («Ремонт стиральной машины»). Длинные
    заголовки режем: строка уведомления живёт в узком выпадающем списке.
    """
    if name and name.strip():
        return name.strip()
    title = (item_title or "").strip()
    if title:
        return f"Клиент по объявлению «{title[:40]}»"
    return "Клиент"


async def check_awaiting(now: datetime | None = None) -> int:
    """Один проход. Возвращает, сколько диалогов ждут ответа."""
    moment = now or datetime.now(UTC)
    redis = redis_mod.get_client()
    touched = 0

    async with session_scope() as db:
        candidates = list(
            (
                await db.execute(
                    sa.select(Conversation)
                    .where(
                        Conversation.awaiting_since.is_not(None),
                        Conversation.awaiting_since < moment - REMIND_AFTER,
                        # Только взятые: за очередью следит check_queue.
                        Conversation.assignee_id.is_not(None),
                        Conversation.status != inbox_svc.CLOSED,
                    )
                    .order_by(Conversation.awaiting_since)
                    .limit(BATCH)
                )
            )
            .scalars()
            .all()
        )
        if not candidates:
            return 0

        owners = {c.assignee_id for c in candidates if c.assignee_id is not None}
        owner_rows = (
            await db.execute(
                sa.select(User.id, User.full_name, User.role).where(User.id.in_(owners))
            )
        ).all()
        names: dict[uuid.UUID, str] = {row.id: row.full_name for row in owner_rows}
        # Кто из владельцев сам читает рассылку руководителям — по той же таблице
        # ролей, что и центр уведомлений, а не по своей копии.
        head_readers: set[uuid.UUID] = {
            row.id for row in owner_rows if "head" in notify_svc.visible_audiences(row.role)
        }
        client_rows = (
            await db.execute(
                sa.select(Client.id, Client.name).where(
                    Client.id.in_({c.client_id for c in candidates})
                )
            )
        ).all()
        client_names: dict[uuid.UUID, str | None] = {r.id: r.name for r in client_rows}

        remind_keys = {c.id: f"conversation.awaiting_you:conversation:{c.id}" for c in candidates}
        escalate_keys = {c.id: f"conversation.no_reply:conversation:{c.id}" for c in candidates}
        reminded_before = await _already_sent(
            db, "conversation.awaiting_you", list(remind_keys.values()), moment
        )
        escalated_before = await _already_sent(
            db, "conversation.no_reply", list(escalate_keys.values()), moment
        )

        delivers: list[Any] = []
        for conv in candidates:
            started = conv.awaiting_since
            owner_id = conv.assignee_id
            assert started is not None and owner_id is not None  # условие выборки
            waited = moment - started.replace(tzinfo=UTC)
            waited_text = inbox_svc.format_wait(int(waited.total_seconds()))
            who = _client_label(client_names.get(conv.client_id), conv.item_title)
            touched += 1

            if _reminders_due(waited, REMIND_AFTER) > reminded_before.get(
                (remind_keys[conv.id], owner_id), 0
            ):
                delivers.append(
                    await notify_svc.notify(
                        db,
                        kind="conversation.awaiting_you",
                        recipient_id=owner_id,
                        body=f"{who} ждёт ответа {waited_text}",
                        entity_type="conversation",
                        entity_id=str(conv.id),
                        dedup_key=remind_keys[conv.id],
                        now=moment,
                    )
                )

            if waited < ESCALATE_AFTER or owner_id in head_readers:
                continue
            if _reminders_due(waited, ESCALATE_AFTER) <= escalated_before.get(
                (escalate_keys[conv.id], None), 0
            ):
                continue
            delivers.append(
                await notify_svc.notify(
                    db,
                    kind="conversation.no_reply",
                    body=(
                        f"{who} ждёт ответа {waited_text}, "
                        f"не отвечает {names.get(owner_id, 'сотрудник')}"
                    ),
                    entity_type="conversation",
                    entity_id=str(conv.id),
                    dedup_key=escalate_keys[conv.id],
                    now=moment,
                )
            )

        await db.commit()

    for result in delivers:
        await notify_svc.deliver(redis, result)
    if touched:
        log.info("awaiting.checked", touched=touched)
    return touched


async def check_queue(now: datetime | None = None) -> int:
    """Диалог стоит в очереди, и его никто не взял. Один проход.

    ЗАЧЕМ ОТДЕЛЬНЫЙ ПРОХОД, ЕСЛИ ВЫШЕ УЖЕ ЕСТЬ «КЛИЕНТ ЖДЁТ». Тот проход по
    построению смотрит только ВЗЯТЫЕ диалоги (`assignee_id is not null`), и в его
    шапке написано «за стоящими в очереди следит своя эскалация». Своя эскалация —
    это `conversation.unclaimed`, и она срабатывает ровно в одном случае: диалог
    предложили и от него ОТКАЗАЛИСЬ все, кому он был доступен (`inbox._escalate`).
    Отказ — действие. Диалога, на который просто никто не посмотрел, не касается
    ни один из двух сторожей.

    Пока в системе тринадцать диспетчеров и включена автораздача, дыра невелика:
    диалог кому-то предложен, у него горит счётчик. Но `distribution.enabled` на
    боевой системе выключен, работает один человек, и проверка живой базы
    12 августа показала это в чистом виде: два диалога стояли в очереди 7 и 4 часа
    (девять непрочитанных в одном), и система не сказала об этом ни строчки —
    ни одного уведомления за сутки. Клиент в это время писал «Стоимость?».

    ПОРОГ. Десять минут — не норматив вежливости, а замер по живому корпусу
    (13 852 диалога): ответ за пять минут даёт 19.6% телефонов, через полчаса —
    10%, через час-три — 8.1%. Позже десяти минут звонить уже поздно, раньше —
    шум на каждое сообщение.

    ВИД СОБЫТИЯ ПЕРЕИСПОЛЬЗОВАН НАМЕРЕННО. `conversation.unclaimed` — «Диалог
    никто не принял» — правда и здесь, а ключ склейки тот же, что у отказной
    ветки. Значит диалог, который сперва провисел, а потом получил отказ от всех,
    даст ОДНУ строку с растущим счётчиком повторов, а не две про одно и то же.
    """
    moment = now or datetime.now(UTC)
    redis = redis_mod.get_client()
    sent = 0

    async with session_scope() as db:
        candidates = list(
            (
                await db.execute(
                    sa.select(Conversation)
                    .where(
                        inbox_svc.queue_condition(),
                        Conversation.offered_at < moment - QUEUE_WAIT_AFTER,
                    )
                    .order_by(Conversation.offered_at)
                    .limit(BATCH)
                )
            )
            .scalars()
            .all()
        )
        if not candidates:
            return 0

        client_rows = (
            await db.execute(
                sa.select(Client.id, Client.name).where(
                    Client.id.in_({c.client_id for c in candidates})
                )
            )
        ).all()
        client_names: dict[uuid.UUID, str | None] = {r.id: r.name for r in client_rows}

        keys = {c.id: f"{inbox_svc.ESCALATION_KIND}:{c.id}" for c in candidates}
        sent_before = await _already_sent(
            db, inbox_svc.ESCALATION_KIND, list(keys.values()), moment
        )

        delivers: list[Any] = []
        for conv in candidates:
            waited_s = inbox_svc.waiting_seconds(conv, now=moment) or 0
            waited = timedelta(seconds=waited_s)
            # Та же лестница, что у личных напоминаний: 10 минут, 20, 40, 80…
            # Ровный такт «раз в десять минут» на брошенном на ночь диалоге дал бы
            # к утру полсотни повторов и научил бы пролистывать эту строку.
            already = sent_before.get((keys[conv.id], None), 0)
            if _reminders_due(waited, QUEUE_WAIT_AFTER) <= already:
                continue

            who = client_names.get(conv.client_id) or "клиент"
            delivers.append(
                await notify_svc.notify(
                    db,
                    kind=inbox_svc.ESCALATION_KIND,
                    severity="warning",
                    audience="admin",
                    title="Диалог никто не принял",
                    body=(
                        f"{who} ждёт в очереди {inbox_svc.format_wait(waited_s)} — "
                        f"диалог не взял никто. Откройте «Входящие» и возьмите его."
                    ),
                    entity_type="conversation",
                    entity_id=str(conv.id),
                    dedup_key=keys[conv.id],
                    now=moment,
                )
            )
            sent += 1

        await db.commit()

    for result in delivers:
        await notify_svc.deliver(redis, result)

    if sent:
        log.info("queue.unclaimed_alerted", sent=sent)
    return sent


#: Ширина окна поиска истёкших отказов. Такт задания — минута, поэтому окна в две
#: минуты хватает с запасом: пропущенный такт (перезапуск, задержка) догоняется
#: следующим, а один и тот же отказ дважды не объявится — второй раз он уже старше окна.
DECLINE_LOOKBACK = timedelta(minutes=2)


async def check_expired_declines(now: datetime | None = None) -> int:
    """Отказ истёк — вернуть диалог в очередь ТОМУ, КТО ОТКАЗЫВАЛСЯ (требование 13.08).

    ⚠ ЗАЧЕМ ЗАДАНИЕ, ЕСЛИ ФИЛЬТР ОЧЕРЕДИ И ТАК СЧИТАЕТ ПО ВРЕМЕНИ. Потому что фильтр
    отрабатывает при ЗАПРОСЕ, а запроса не будет: список входящих во фронте не опрашивается
    (``staleTime`` тридцать секунд, ``refetchInterval`` не задан, ``refetchOnWindowFocus``
    выключен). Оператор сидит в открытой вкладке, и без толчка снаружи диалог вернулся бы
    к нему только после перезагрузки страницы. То есть функция была бы формально сделана и
    практически невидима.

    ⚠ КАДР АДРЕСНЫЙ (``only_user``), а не широковещательный. Диалог вернулся ровно у одного
    человека — у того, чей отказ истёк. Остальные его и не теряли: отказ персональный. Пошли
    мы общий ``inbox:new`` — у двенадцати других строка вставилась бы повторно, да ещё со
    звуком нового диалога.

    ⚠ ПРОВЕРЯЕМ ОЧЕРЕДЬ ЗАНОВО, А НЕ ВЕРИМ СТРОКЕ ОТКАЗА. За три минуты диалог могли принять,
    закрыть или передать. Объявить возврат такого — значит показать человеку в очереди то,
    чего там нет, и он нажмёт «Принять» и получит ошибку.
    """
    moment = now or datetime.now(UTC)
    redis = redis_mod.get_client()
    порог = moment - inbox_svc.DECLINE_TTL
    sent = 0

    async with session_scope() as db:
        истёкшие = list(
            (
                await db.execute(
                    sa.select(ConversationDecline)
                    .where(
                        ConversationDecline.declined_at <= порог,
                        ConversationDecline.declined_at > порог - DECLINE_LOOKBACK,
                    )
                    .limit(BATCH)
                )
            )
            .scalars()
            .all()
        )
        if not истёкшие:
            return 0

        строки = {
            c.id: c
            for c in (
                await db.execute(
                    sa.select(Conversation).where(
                        Conversation.id.in_({d.conversation_id for d in истёкшие}),
                        inbox_svc.queue_condition(),
                    )
                )
            )
            .scalars()
            .all()
        }
        кадры: list[tuple[str, dict[str, Any]]] = []
        for отказ in истёкшие:
            conv = строки.get(отказ.conversation_id)
            if conv is None:
                continue  # диалог уже приняли, закрыли или передали — возвращать нечего
            кадры.append(
                (
                    str(отказ.user_id),
                    {
                        "conversation_id": str(conv.id),
                        "conversation": await inbox_svc.inbox_frame(db, conv, now=moment),
                        # ⚠ ПОМЕТКА «ЭТО ВОЗВРАТ» НУЖНА РАДИ ЗВУКА. Кадр `inbox:new` во
                        # фронте звонит — так оператор узнаёт о новом клиенте, не глядя в
                        # экран. Но здесь клиент не новый: этот диалог человек уже видел и
                        # сам от него отказался три минуты назад. Звонок на него означал бы
                        # ложную тревогу каждые три минуты у всех, кто пользуется отказом.
                        "returned": True,
                    },
                )
            )

    # Публикуем ПОСЛЕ закрытия сессии — как и весь остальной код (08 §8.1).
    for кому, кадр in кадры:
        await publish_event(redis, INBOX_NEW, кадр, only_user=кому)
        sent += 1
    if sent:
        log.info("declines.expired.returned", count=sent)
    return sent


def register(scheduler: Any) -> None:
    """Раз в минуту — тем же тактом, что и возврат розданных: сроки здесь
    измеряются минутами, и чаще смотреть незачем."""
    scheduler.add_job(
        check_awaiting,
        IntervalTrigger(minutes=1),
        id=JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    # Очередь смотрим тем же тактом и по тем же причинам. Отдельным заданием, а
    # не хвостом предыдущего: у них разные выборки и разные адресаты, а падение
    # одного не должно уносить второе.
    scheduler.add_job(
        check_queue,
        IntervalTrigger(minutes=1),
        id=QUEUE_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
    # Истёкшие отказы — тем же тактом. Срок отказа три минуты, минутная точность
    # возврата человеку незаметна, а более частый такт означал бы запрос к базе
    # каждые несколько секунд ради события, которого обычно нет.
    scheduler.add_job(
        check_expired_declines,
        IntervalTrigger(minutes=1),
        id=DECLINE_JOB_ID,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=300,
    )
