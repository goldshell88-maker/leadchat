"""Возврат розданных диалогов, за которые никто не взялся (план 7.7).

ЗАЧЕМ. Автораспределение (7.6) отдаёт обращение оператору, который в этот
момент в сети. Дальше возможны два исхода: человек берётся за диалог — или
уходит. Ушёл, не притронувшись, — и клиент оказывается в худшем из состояний:
он не в очереди, где его видят все тринадцать, а «у оператора», которого нет
за столом, и не ждёт никого конкретно. В очереди его хотя бы заметно.

Поэтому диалог возвращается во «Входящие». Не «переназначается сразу другому»:
пока идёт возврат, обстановка могла измениться — кто-то вышел, у кого-то
кончилось место, — и решать это должен тот же движок раздачи на общих
основаниях, а не сторож своими руками.

ЧТО ИМЕННО ВОЗВРАЩАЕТСЯ — ГРАНИЦА ЗДЕСЬ ГЛАВНОЕ
------------------------------------------------
Только диалоги, которые (1) отдала СИСТЕМА, (2) человек НЕ ТРОНУЛ и (3)
получатель СЕЙЧАС НЕ В СЕТИ. Каждое условие обязательно, и вот почему нельзя
ослабить ни одно:

* не «все диалоги офлайн-оператора» — у него есть диалоги, взятые руками и
  ведущиеся неделю; отбирать их, когда человек ушёл домой, значит рвать
  переписку у клиента на середине;
* не «все розданные системой» — тот, кто уже ответил клиенту, ведёт диалог, и
  забирать его посреди разговора хуже, чем не забрать вовсе;
* не «офлайн хоть на секунду» — обрыв связи на минуту случается постоянно, и
  возврат по нему устроил бы карусель. Отсюда выдержка: диалог должен пролежать
  нетронутым дольше :data:`RECLAIM_AFTER_MINUTES`.

Признак «отдала система и человек не тронул» — поле
``conversations.auto_assigned_at``, которое ставит раздача и снимает любое
действие оператора (ответ, статус, передача). Сторож НЕ полагается на него
одно: он дополнительно проверяет, что после отметки не было ни одного
исходящего оператора. Пропущенная точка снятия отметки — ошибка вероятная (их
пять в четырёх модулях), и цена у неё — отобранный у работающего человека
диалог; проверка по факту переписки эту ошибку обезвреживает.

ПОЧЕМУ СТОРОЖ, А НЕ РЕАКЦИЯ НА СОБЫТИЕ «УШЁЛ ИЗ СЕТИ»
------------------------------------------------------
Событие ``presence:online``/offline существует, и реагировать на него было бы
быстрее. Но оно живёт в том процессе, где был сокет: упал процесс — событие не
дошло, и диалоги остались висеть навсегда. Это ровно тот случай, когда терять
нельзя: авария на сервере и есть самая частая причина, по которой все
операторы разом «уходят из сети». Периодическая проверка переживает любое
падение — она просто отработает в следующий раз.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from apscheduler.triggers.interval import IntervalTrigger

from app.core import redis as redis_mod
from app.db import session as db_mod
from app.models import Conversation, Message, User
from app.services import app_settings
from app.services import conversation_status as status_dict
from app.services import conversations as convs
from app.services import inbox as inbox_svc
from app.services import notifications as notify_svc
from app.services.audit import write_audit
from app.ws import presence
from app.ws.events import iso, publish_event
from app.ws.hub import publish_inbox_new
from app.ws.presence import presence_map, presence_status_map

log = structlog.get_logger("app.reclaim")

#: Сколько диалог должен пролежать нетронутым, прежде чем его заберут.
#:
#: Три минуты — компромисс между двумя ошибками. Меньше — и обычный обрыв связи
#: (или обед с закрытой крышкой ноутбука) начнёт гонять диалоги туда-обратно.
#: Больше — и клиент столько же лишних минут ждёт впустую. Присутствие само по
#: себе гаснет не мгновенно (у него свой TTL и отсрочка на переподключение), так
#: что фактическая задержка складывается из обеих величин.
RECLAIM_AFTER_MINUTES = 3

#: Сколько диалогов забираем за один проход. Ограничение не про
#: производительность, а про взрыв: если разом отвалились все тринадцать
#: операторов (упала сеть в офисе), возвращать разумно порциями — иначе один
#: проход публикует сотни кадров в браузеры и рассылает столько же уведомлений.
BATCH = 100


async def reclaim_abandoned() -> int:
    """Точка входа планировщика: своя сессия, свой Redis.

    Тонкая обёртка вокруг :func:`reclaim_in_session` намеренно. Логика возврата
    — это набор решений «забрать / не забрать», и проверять их надо на обычной
    сессии теста, а не через глобальные соединения процесса. Смешай их в одну
    функцию — и единственным способом её протестировать станет подмена
    глобального движка, то есть проверка не логики, а способа открыть сессию.
    """
    redis = redis_mod.get_client()
    async with db_mod.session_scope() as db:
        frames = await reclaim_in_session(db, redis)
        await db.commit()

    # Кадры — строго ПОСЛЕ commit'а (08 §8.1) и обязательно: без них диалог
    # возвращается в базе, но не на экранах. У бывшего владельца он остался бы
    # висеть в «Моих», а во «Входящих» у остальных не появился бы до
    # перезагрузки страницы — то есть функция «работает», а человек этого не
    # видит и продолжает считать диалог чужим.
    for frame in frames:
        await publish_inbox_new(
            redis,
            frame["inbox"]["conversation"],
            eligible_operator_ids=frame["inbox"].get("eligible"),
        )
        await publish_event(redis, "conversation:updated", frame["patch"])

    if frames:
        log.info("reclaim.done", returned=len(frames))
    return len(frames)


async def reclaim_in_session(
    db: Any, redis: Any, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Вернуть во «Входящие» розданные и нетронутые диалоги офлайн-операторов.

    Ничего не коммитит и ничего не публикует — транзакцией и кадрами владеет
    вызывающий (08 §8.1). Возвращает готовые тела кадров: собрать их надо ДО
    commit'а, пока связанные сущности видны в этой же транзакции, а отправить
    — строго после.
    """
    cutoff = (now or datetime.now(UTC)) - timedelta(minutes=RECLAIM_AFTER_MINUTES)
    frames: list[dict[str, Any]] = []

    # Отметка «выдан и нетронут» у диалога, где оператор уже ответил (ответ из
    # приложения Авито её не снимает), — снимаем у всех, а не только у тех, кого
    # нет в сети: иначе такие отметки копились у сидящих в сети и забивали пачку.
    ответил = (
        sa.select(Message.conversation_id)
        .where(
            Message.conversation_id == Conversation.id,
            Message.direction == "out",
            Message.sender_type == "operator",
            Message.created_at >= Conversation.auto_assigned_at,
        )
        .exists()
    )
    снято = await db.execute(
        sa.update(Conversation)
        .where(Conversation.auto_assigned_at.is_not(None), ответил)
        .values(auto_assigned_at=None)
        .execution_options(synchronize_session=False)
    )
    if снято.rowcount:
        log.warning("reclaim.stale_marks_cleared", count=снято.rowcount)

    условия = (
        Conversation.auto_assigned_at.is_not(None),
        Conversation.auto_assigned_at < cutoff,
        Conversation.assignee_id.is_not(None),
        Conversation.status != inbox_svc.CLOSED,
        # Висящей передачей владеет expire_transfers.
        Conversation.transfer_to_id.is_(None),
    )
    # Сначала — кого нет в сети, потом пачка только их диалогов. Пачка по всем
    # владельцам забивалась диалогами тех, кто в сети, и диалог ушедшего не
    # возвращался никогда (та же болезнь, что у освобождения до 2978a71).
    #
    # «Отошёл» здесь — присутствие: `presence_map` отвечает «приложение
    # открыто». Автораздача отошедшему новых не даёт, а начатое у него этот
    # сторож не отбирает (это делает освобождение, по своим правилам).
    owners = sorted(
        (await db.execute(sa.select(Conversation.assignee_id).where(*условия).distinct()))
        .scalars()
        .all()
    )
    if not owners:
        return frames
    online = await presence_map(redis, owners)
    offline = [uid for uid in owners if not online.get(uid)]
    if not offline:
        return frames
    candidates = list(
        (
            await db.execute(
                sa.select(Conversation)
                .where(*условия, Conversation.assignee_id.in_(offline))
                .order_by(Conversation.auto_assigned_at)
                .limit(BATCH)
                # Решение и запись — одним куском: строку, которую прямо сейчас
                # держит человек (передаёт, принимает), пропускаем до следующего
                # прохода, иначе наш UPDATE лёг бы поверх его commit'а.
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    for conv in candidates:
        previous_owner = conv.assignee_id
        inbox_svc.return_to_queue(conv, keep_declines=True)
        inbox_svc.clear_auto_assignment(conv)
        await write_audit(
            db,
            user_id=None,  # решение системы, а не сотрудника
            action="conversation.reclaimed",
            entity="conversation",
            entity_id=str(conv.id),
            details={"previous_assignee_id": str(previous_owner), "reason": "operator_offline"},
        )
        frames.append(
            {
                # ⚠ кадр СО СПИСКОМ ДОПУЩЕННЫХ (аудит 19.08): без него строка
                # очереди чужого канала уезжала всем менеджерам
                "inbox": await inbox_svc.inbox_frame_addressed(db, conv, now=now),
                # Бывшему владельцу диалог обязан исчезнуть из «Моих»: без этой
                # заплатки он останется в списке с его именем, и человек будет
                # уверен, что диалог всё ещё за ним.
                "patch": {
                    "conversation_id": str(conv.id),
                    "patch": {"status": conv.status, "assignee": None, "in_inbox": True},
                },
            }
        )

    return frames


async def expire_transfers() -> int:
    """Снять предложения передачи, на которые никто не ответил (требование
    заказчика от 7 августа).

    «Висит вечно» здесь равно «потеряли»: передавший считает, что отдал, и
    больше на диалог не смотрит, а получатель его не видел вовсе. Через
    пятнадцать минут предложение снимается, и диалог честно остаётся у того,
    за кем и числился всё это время.

    Живёт рядом с возвратом брошенных диалогов, потому что делает то же самое
    по сути — чинит состояние, в котором клиент ждёт, а человека за диалогом
    фактически нет. Отдельной функцией, а не веткой внутри: выборки разные, и
    смешивать их значило бы делать лишний обход по каждой минуте.
    """
    from app.services import transfer as transfer_svc

    redis = redis_mod.get_client()
    frames: list[dict[str, Any]] = []
    notices: list[tuple[uuid.UUID, transfer_svc.Notice]] = []
    recipients: list[tuple[uuid.UUID, uuid.UUID]] = []
    async with db_mod.session_scope() as db:
        rows = list(
            (
                await db.execute(
                    # Замок по той же причине, что и у выборки выше: между
                    # чтением и записью человек успевает принять или отклонить
                    # предложение, а `UPDATE` ложится сверху без условий.
                    # Передачи работают всегда, независимо от выключателя
                    # автораздачи, — здесь гонка достижима каждый день.
                    sa.select(Conversation)
                    .where(transfer_svc.expired_condition())
                    .limit(BATCH)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        owner_ids = {c.transfer_from_id or c.transfer_by_id for c in rows} - {None}
        owner_names = {
            u.id: u.full_name
            for u in (await db.execute(sa.select(User).where(User.id.in_(owner_ids)))).scalars()
        }
        for conv in rows:
            offered_to = conv.transfer_to_id
            offered_by = conv.transfer_by_id
            owner_id = conv.transfer_from_id or offered_by
            offered_at = conv.transfer_at
            transfer_svc.clear(conv)
            await write_audit(
                db,
                user_id=None,  # решение системы: никто не ответил
                action="conversation.transfer_expired",
                entity="conversation",
                entity_id=str(conv.id),
                details={
                    "to_id": str(offered_to) if offered_to else None,
                    "by_id": str(offered_by) if offered_by else None,
                },
            )
            frames.append(
                {
                    "conversation_id": str(conv.id),
                    # `transfer: null` — главное поле кадра: у обоих участников
                    # полоса должна исчезнуть. Без него передающий и через час
                    # видел бы «ждёт подтверждения» от человека, которого уже
                    # никто не ждёт.
                    "patch": {"transfer": None},
                    "for_user_id": str(offered_by) if offered_by else None,
                    # Для Живой ленты: предложение истекло, диалог остался где был.
                    "transfer_outcome": "expired",
                    "transfer_actor": None,
                }
            )
            # Владелец и предлагавший узнают, что диалог остался где был: полоса,
            # исчезнувшая с соседней вкладки, — не сообщение. Получатель узнаёт,
            # что принимать нечего: иначе у него висит карточка с кнопками.
            notices.extend(
                (conv.id, n)
                for n in transfer_svc.outcome_notices(
                    "conversation.transfer_expired",
                    conv_id=conv.id,
                    offered_at=offered_at,
                    owner_id=owner_id,
                    offered_by=offered_by,
                    owner_name=owner_names.get(owner_id, "прежним сотрудником")
                    if owner_id
                    else "прежним сотрудником",
                    what="Никто не подтвердил приём за пятнадцать минут.",
                )
            )
            if offered_to is not None:
                notices.append(
                    (
                        conv.id,
                        transfer_svc.withdrawn_notice(
                            conv_id=conv.id,
                            offered_at=offered_at,
                            recipient_id=offered_to,
                            title="Время на приём передачи вышло",
                            body="Предложение снято: диалог остался за прежним сотрудником.",
                        ),
                    )
                )
                recipients.append((offered_to, conv.id))
            await notify_svc.resolve_for_entity(
                db,
                entity_type="conversation",
                entity_id=str(conv.id),
                kinds=(transfer_svc.OFFERED,),
            )
        await db.commit()

    # Уведомления — после commit'а и своей сессией: центр коммитит сам.
    # send_notification не пробрасывает ошибку центра: сторож, упавший на
    # попытке сообщить, перестал бы снимать просроченные предложения вовсе.
    if notices:
        from app.services.support import WARNING, NotificationDraft, send_notification

        async with db_mod.session_scope() as ndb:
            for conv_id, n in notices:
                await send_notification(
                    ndb,
                    redis,
                    NotificationDraft(
                        kind=n.kind,
                        severity=WARNING if n.kind != transfer_svc.WITHDRAWN else "info",
                        title=n.title,
                        body=n.body,
                        dedup_key=n.dedup_key,
                        audience=None,
                        recipient_id=n.recipient_id,
                        entity_type="conversation",
                        entity_id=str(conv_id),
                    ),
                )
    for recipient_id, conv_id in recipients:
        await convs.clear_transferred(redis, recipient_id, conv_id)

    for frame in frames:
        await publish_event(redis, "conversation:updated", frame)

    if frames:
        log.info("transfer.expired", count=len(frames))
    return len(frames)


# =========================== диалоги того, кого нет за столом (просьба 03.09)

#: Сколько человек должен быть недоступен, прежде чем его диалоги освободятся.
#:
#: ⚠ ПЯТНАДЦАТЬ, А НЕ ТРИ, КАК У СОСЕДНЕГО СТОРОЖА, И ЭТО НЕ ОПЕЧАТКА. Тот
#: забирает диалог, к которому человек не притронулся, — там ошибиться нечем.
#: Здесь забирается РАБОТА: обед, звонок клиенту, отход к принтеру не должны
#: стоить диспетчеру его диалогов. Пятнадцать минут — это уже не отлучка.
UNAVAILABLE_AFTER_MINUTES = 15

#: Отметка «с какого момента человек недоступен» — в Redis, ставит её сам
#: сторож.
#:
#: ⚠ ПОЧЕМУ НЕ ИЗ ПРИСУТСТВИЯ. У присутствия нет «с какого момента»: офлайн —
#: это ОТСУТСТВИЕ ключа, а не значение. Записывать отметку в момент разрыва
#: сокета нельзя по той же причине, по которой весь этот модуль сделан
#: сторожем, а не подпиской на событие: упал процесс — событие не дошло.
#:
#: Сторож наблюдает сам: увидел недоступного впервые — поставил отметку,
#: увидел вернувшегося — снял.
#:
#: ⚠ ЧТО БЫВАЕТ ПРИ ПЕРЕЗАПУСКЕ — ПРОВЕРЕНО НА БОЮ 03.09, А НЕ ПРЕДПОЛОЖЕНО.
#: Отметка живёт в Redis и перезапуск ПРИЛОЖЕНИЯ переживает: после выкатки
#: отметка, поставленная до неё, осталась на месте. Отсчёт начинается заново
#: только если потеряна сама Redis.
#:
#: Опасным это не делает вот что: ключ присутствия держится TTL'ом
#: (`ws_presence_ttl_seconds`, 210 с) и разрывом сокета НЕ стирается, а вкладки
#: переподключаются за секунды. То есть к первому проходу после выкатки люди
#: снова «в сети», их отметки снимаются, и массового отбора не происходит.
#: Проверено: после выкатки 03.09 сторож ничего не освободил, а отметка
#: отошедшего сохранилась и досчитала свои пятнадцать минут.
#:
#: Здесь стояло «после перезапуска сервера отметок нет» — это было
#: предположением, и оно неверно. Разница важна: на ней держится ответ на
#: вопрос «не отберёт ли выкатка диалоги у всех разом».
_UNAVAILABLE_KEY = "presence:unavailable_since:{user_id}"
#: TTL с запасом: отметка нужна только чтобы пережить порог.
_UNAVAILABLE_TTL_SECONDS = UNAVAILABLE_AFTER_MINUTES * 60 * 4

RELEASE_JOB_ID = "release_unavailable"


async def _unavailable_since(
    redis: Any, user_id: uuid.UUID, *, online: bool, now: datetime
) -> datetime | None:
    """С какого момента человек недоступен. ``None`` — он на месте."""
    key = _UNAVAILABLE_KEY.format(user_id=user_id)
    if online:
        await redis.delete(key)
        return None
    # `nx=True` — отметку ставит ПЕРВЫЙ проход, увидевший отсутствие; следующие
    # её не сдвигают, иначе отсчёт начинался бы заново каждую минуту и порог не
    # наступал бы никогда.
    await redis.set(key, now.isoformat(), nx=True, ex=_UNAVAILABLE_TTL_SECONDS)
    raw = await redis.get(key)
    if not raw:
        return None
    try:
        отметка = datetime.fromisoformat(raw)
    except ValueError:
        # Мусор в ключе — не повод отбирать диалоги: ставим заново и ждём.
        await redis.delete(key)
        return None
    return отметка if отметка.tzinfo else отметка.replace(tzinfo=UTC)


async def release_unavailable_owners() -> int:
    """Освободить диалоги тех, кого нет за столом (просьба владельца 03.09).

    ⚠ ЖАЛОБА ДОСЛОВНО: «диалоги не уходят из моих, когда я не в сети, и если бы
    клиент ответил, то мы бы потеряли диалог».

    Это ДРУГАЯ беда, чем у соседнего `reclaim_abandoned`. Тот возвращает
    диалоги, которых человек не касался. Здесь речь о работе, которую он вёл:
    ушёл со смены — и его двадцать диалогов остались за ним. Клиент пишет в
    любой из них, а видит это только отсутствующий: во «Входящих» диалога нет,
    у остальных двенадцати его нет тоже.

    ⚠ ЭТО ОСОЗНАННЫЙ ПЕРЕСМОТР РЕШЕНИЯ, ЗАПИСАННОГО В `reclaim_in_session`.
    Там сказано: «отошёл» считается присутствием, иначе обед стоил бы человеку
    всей его работы, а клиент получил бы нового собеседника с середины
    разговора. Довод верен ровно для ОДНОГО способа освобождения — «отдать
    другому». Здесь способ другой, и он снимает возражение:

    * диалог, где ждёт КЛИЕНТ, уходит во «Входящие» — там его видят все
      тринадцать, и это строго лучше, чем висеть у отсутствующего;
    * диалог, где ждём НЕ МЫ (мы ответили, клиент молчит), просто ЗАКРЫВАЕТСЯ.
      Клиенту при этом не происходит ничего: закрытие внутреннее. Напишет он
      снова — диалог сам переоткроется и попадёт во «Входящие»
      (`services/inbound`: `assignee_id = None`, статус `new`, `enter_queue`),
      а бывшему владельцу уйдёт уведомление «клиент вернулся».

    То есть «оборвать разговор на середине» здесь не происходит: разговор либо
    виден всем, либо возобновится сам.

    ⚠ ВЫКЛЮЧАЕТСЯ НАСТРОЙКОЙ `release_unavailable.enabled` (просьба владельца
    06.09: «нужно сделать её выключаемой»). Живёт она в ручке и на экране
    автораздачи — это третье решение о том, у кого лежит диалог. Где стоит
    проверка и почему НЕ перед выборкой — в `release_in_session`.

    ⚠ ЧЕГО ЭТОТ СТОРОЖ НЕ ТРОГАЕТ.
    * Диалоги с активным ботом: бот отвечает клиенту сам, отсутствие человека
      ничего не решает.
    * Диалоги с висящим предложением передачи (`transfer_to_id`): ими владеет
      `expire_transfers`, и два сторожа на одну строку — это гонка.
    * Диалоги без ответственного и уже закрытые: освобождать нечего.
    """
    redis = redis_mod.get_client()
    async with db_mod.session_scope() as db:
        frames = await release_in_session(db, redis)
        await db.commit()

    for frame in frames:
        if frame.get("inbox") is not None:
            await publish_inbox_new(
                redis,
                frame["inbox"]["conversation"],
                eligible_operator_ids=frame["inbox"].get("eligible"),
            )
        if frame.get("message") is not None:
            # Запись в ленте открытого диалога: вернувшийся хозяин читает, куда
            # и почему делся его диалог.
            await publish_event(
                redis,
                "message:new",
                {
                    "conversation_id": frame["patch"]["conversation_id"],
                    "message": convs.message_out(frame["message"]),
                },
            )
        await publish_event(redis, "conversation:updated", frame["patch"])

    if frames:
        log.info(
            "release_unavailable.done",
            released=len(frames),
            to_inbox=sum(1 for f in frames if f.get("inbox") is not None),
        )
    return len(frames)


async def release_in_session(
    db: Any, redis: Any, *, now: datetime | None = None
) -> list[dict[str, Any]]:
    """Решение и запись одним куском; кадры собираются до commit'а (08 §8.1)."""
    сейчас = now or datetime.now(UTC)
    условия = (
        Conversation.assignee_id.is_not(None),
        Conversation.status != inbox_svc.CLOSED,
        # Бот отвечает сам — отсутствие человека клиенту не мешает.
        Conversation.bot_active.is_(False),
        # Передача в полёте: этой строкой владеет `expire_transfers`.
        Conversation.transfer_to_id.is_(None),
    )

    # ⚠ СНАЧАЛА — ЧЬИ ДИАЛОГИ ПОРА ОСВОБОЖДАТЬ, ПОТОМ — САМИ ДИАЛОГИ (24.09).
    #
    # Раньше пачка в BATCH строк бралась по всем владельцам сразу (старые
    # вперёд), а исключённые и «на месте» отсеивались уже в цикле. Исключённые
    # держат диалоги сколько угодно долго, и их старые диалоги занимали всю
    # пачку: в бою 24.09 все 100 из 223 принадлежали исключённым, и диалоги
    # остальных сотрудников сторож не видел вовсе — ни освободить, ни даже
    # поставить их владельцам отметку «недоступен с». Жалоба владельца:
    # «сломалась функция освобождать диалоги сотрудника, который не в сети».
    владельцы = sorted(
        (await db.execute(sa.select(Conversation.assignee_id).where(*условия).distinct()))
        .scalars()
        .all()
    )
    if not владельцы:
        return []
    # ⚠ СТАТУС, А НЕ «ПОДКЛЮЧЁН ЛИ». `presence_map` возвращает bool и «отошёл»
    # от «на месте» не отличает — а здесь вся задача как раз в том, чтобы
    # отличать. Тот же выбор сделан в автораздаче (`services/distribution`).
    статусы = await presence_status_map(redis, владельцы)
    доступен = {uid: статусы.get(uid) == presence.ONLINE for uid in владельцы}

    порог = timedelta(minutes=UNAVAILABLE_AFTER_MINUTES)
    недоступен_с: dict[uuid.UUID, datetime | None] = {}
    for uid in владельцы:
        недоступен_с[uid] = await _unavailable_since(redis, uid, online=доступен[uid], now=сейчас)

    # ⚠ ВЫКЛЮЧАТЕЛЬ СТОИТ ПОСЛЕ ОТМЕТОК, А НЕ ПЕРЕД ВЫБОРКОЙ, И ЭТО РЕШЕНИЕ.
    # Отметки «недоступен с» выше ставятся и снимаются при выключенном стороже
    # так же, как при включённом. Стой проверка раньше — на время выключения
    # отметки замирали бы: у вернувшегося не снялась бы старая (TTL — час), у
    # ушедшего не встала бы новая. Первый проход после включения судил бы по
    # этому мусору: вернувшийся и снова отошедший на минуту потерял бы диалоги
    # за ПРОШЛУЮ отлучку, а ушедший домой три часа назад держал бы их ещё
    # пятнадцать минут. С живыми отметками включение решает по настоящему
    # времени отсутствия: кто давно ушёл — освобождается, кто на месте — нет,
    # и ничего не уезжает «у всех разом».
    #
    # Из базы каждый проход, без кэша, — по тому же доводу, что
    # `distribution.is_enabled`: «выключил» обязано значить «выключилось».
    if not await app_settings.get(db, app_settings.RELEASE_UNAVAILABLE_ENABLED):
        return []

    # ⚠ ИСКЛЮЧЕНИЯ ЧИТАЮТСЯ ПОСЛЕ ВЫКЛЮЧАТЕЛЯ И ДО ВЫБОРКИ — один запрос на
    # проход, а не на диалог. Список меняется реже, чем идут проходы (раз в
    # минуту), но кэшировать его нельзя по тому же доводу, что и выключатель:
    # «добавил исключение» обязано значить «исключение работает», а не
    # «заработает, когда перезапустят планировщик».
    неприкосновенные = app_settings.parse_exempt_ids(
        await app_settings.get(db, app_settings.RELEASE_UNAVAILABLE_EXEMPT)
    )
    # ⚠ ИСКЛЮЧЁННЫЙ НЕ ТЕРЯЕТ ДИАЛОГ, СКОЛЬКО БЫ ЕГО НИ НЕ БЫЛО. У части людей
    # работа устроена так, что отсутствие в системе — это работа: они у
    # клиента, на выезде, у телефона. Отдать их диалог «тому, кто на месте»
    # значит потерять контекст разговора, а не ускорить ответ.
    давно_нет = [
        uid
        for uid in владельцы
        if uid not in неприкосновенные
        and (отметка := недоступен_с.get(uid)) is not None
        and сейчас - отметка >= порог
    ]
    if not давно_нет:
        return []

    кандидаты = list(
        (
            await db.execute(
                sa.select(Conversation)
                .where(*условия, Conversation.assignee_id.in_(давно_нет))
                .order_by(Conversation.last_message_at)
                .limit(BATCH)
                # Тот же довод, что у соседнего сторожа: решение и запись обязаны
                # быть одним куском, а строку, которую держит человек, пропускаем
                # до следующего прохода.
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )

    имена = {
        u.id: u.full_name
        for u in (await db.execute(sa.select(User).where(User.id.in_(давно_нет)))).scalars()
    }
    frames: list[dict[str, Any]] = []
    for conv in кандидаты:
        владелец = conv.assignee_id
        отметка = недоступен_с.get(владелец) if владелец is not None else None
        if отметка is None:  # выборка уже отфильтрована по отметке; проверка — для типов
            continue
        прежний_статус = conv.status
        кто = имена.get(владелец, "Сотрудник")

        # Тот же вопрос, что задаёт `conversations._waiting_condition` выборке,
        # только про одну строку: расходиться этим двум нельзя.
        ждёт_клиент = (
            conv.awaiting_since is not None and conv.status in status_dict.STATUS_SHOWS_WAIT
        )
        минут = int((сейчас - отметка).total_seconds() // 60)

        if ждёт_клиент:
            # Клиент ждёт нас — во «Входящие», к тринадцати парам глаз.
            inbox_svc.return_to_queue(conv, keep_declines=True)
            inbox_svc.clear_auto_assignment(conv)
            запись = convs.add_system_message(
                db,
                conv,
                f"Диалог возвращён во «Входящие»: {кто} недоступен(на) {минут} мин",
            )
            действие = "conversation.reclaimed"
            подробности = {
                "previous_assignee_id": str(владелец),
                "reason": "owner_unavailable_waiting",
                "unavailable_minutes": минут,
            }
            кадр_очереди = await inbox_svc.inbox_frame_addressed(db, conv, now=сейчас)
            патч = {
                "conversation_id": str(conv.id),
                "patch": {"status": conv.status, "assignee": None, "in_inbox": True},
            }
        else:
            # Ждём не мы — закрываем.
            #
            # ⚠ ОТВЕТСТВЕННЫЙ СОХРАНЯЕТСЯ, И ЭТО НЕ НЕДОСМОТР. Он — единственная
            # запись о том, КТО вёл этот разговор; сняв его, мы обнулили бы
            # отчёт по сотруднику. Из рабочего вида диалог всё равно уходит:
            # вкладка «Мои» по умолчанию закрытые не показывает.
            status_dict.set_status(conv, inbox_svc.CLOSED, now=сейчас)
            # Та же уборка, что у закрытия руками: ожидание, непрочитанное,
            # закрепления, метки очереди, бот.
            await convs.clean_up_closed(db, conv, by_user=None)
            запись = convs.add_system_message(
                db,
                conv,
                f"Диалог закрыт автоматически: {кто} недоступен(на) {минут} мин. "
                "Напишет клиент — диалог откроется снова",
            )
            действие = "conversation.status_changed"
            подробности = {
                "from": прежний_статус,
                "to": inbox_svc.CLOSED,
                "by": "system",
                # ⚠ `previous_assignee_id`, А НЕ `assignee_id`, И ЭТО НЕ
                # ПЕРЕИМЕНОВАНИЕ РАДИ КРАСОТЫ.
                #
                # Отчёт «Закрыто» по менеджеру фильтрует события ровно по полю
                # `details->>'assignee_id'` (см. `services/stats`). Положи сюда
                # это имя — и каждое автозакрытие зачтётся человеку как его
                # работа, хотя закрыл диалог сторож, а человека в этот момент
                # не было за столом. Число «закрыл за смену» раздулось бы у
                # того, кто раньше всех ушёл.
                #
                # В общем счёте «Закрыто за период» эти события остаются: диалог
                # действительно закрыт. Персональной заслугой — нет.
                "previous_assignee_id": str(владелец),
                "reason": "owner_unavailable_answered",
                "unavailable_minutes": минут,
            }
            кадр_очереди = None
            патч = {
                "conversation_id": str(conv.id),
                "patch": {
                    "status": conv.status,
                    "status_since": iso(conv.status_since),
                    "in_inbox": False,
                    "unread_count": 0,
                },
            }

        await write_audit(
            db,
            user_id=None,  # решение системы, а не сотрудника
            action=действие,
            entity="conversation",
            entity_id=str(conv.id),
            details=подробности,
        )
        frames.append({"inbox": кадр_очереди, "patch": патч, "message": запись})

    return frames


DEFAULTS: dict[str, Any] = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}

JOB_ID = "reclaim_abandoned"
TRANSFER_JOB_ID = "expire_transfers"


def register(scheduler: Any) -> None:
    """Раз в минуту. Чаще незачем: выдержка всё равно измеряется минутами."""
    scheduler.add_job(reclaim_abandoned, IntervalTrigger(minutes=1), id=JOB_ID, **DEFAULTS)
    # Просроченные предложения передачи — тем же тактом и по той же причине:
    # без этой строки предложение висит вечно, а «вечно» означает потерянного
    # клиента, за которым формально никого нет.
    scheduler.add_job(expire_transfers, IntervalTrigger(minutes=1), id=TRANSFER_JOB_ID, **DEFAULTS)
    # Диалоги отсутствующего — тем же тактом. Порог измеряется минутами, и
    # ежеминутный проход нужен не для скорости, а чтобы отметка «недоступен с»
    # ставилась вовремя: она и есть отсчёт.
    scheduler.add_job(
        release_unavailable_owners, IntervalTrigger(minutes=1), id=RELEASE_JOB_ID, **DEFAULTS
    )
