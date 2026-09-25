"""Исходящие сообщения и заметки — write-side ленты (01 §6.2–§6.4, 08 §8.3).

Порядок операций жёсткий и одинаковый для сообщения и заметки:

    acquire_idempotency (Redis, ВНЕ транзакции)
      -> одна транзакция: FOR UPDATE диалога + вставка + автоназначение
         + mute_bot + last_message_at + audit
      -> commit
      -> publish_event (message:new, conversation:updated)
      -> enqueue deliver_message

Обратный порядок не чинится ничем (08 §8.1 п.4): событие о незакоммиченных
данных = 404 на детали у фронта, джоба доставки = отправка того, чего нет.

Идемпотентность (01 §1.6, 08 §8.3): ключ ``idem:msg:{conv}:{cmid}``,
``SET NX EX 86400``. Дыра «SET прошёл, commit не дошёл» закрыта веткой
«ключ есть, сообщения нет» — ключ перезанимается новым id, для клиента это
первая успешная попытка.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

import sqlalchemy as sa
import structlog
from arq.connections import ArqRedis
from arq.constants import default_queue_name, expires_extra_ms
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.models import AvitoAccount, Conversation, Message, MessageIdempotency, User
from app.services import conversation_status as status_dict
from app.services import conversations as convs
from app.services import media
from app.services.audit import write_audit
from app.services.conversations import message_out
from app.services.user_ref import user_ref
from app.ws.events import iso, publish_event

log = structlog.get_logger("app.messages")

IDEM_TTL_SECONDS = 86_400  # 24 ч — покрывает офлайн-очередь десктопа (01 §1.6)
MAX_TEXT_LENGTH = 4000  # предел здравого смысла; воркер режет по 1000 (01 §6.2)
DELIVER_JOB = "deliver_message"


def utcnow() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ ARQ enqueue


def as_arq(redis: Redis) -> ArqRedis:
    """Постановка задач через ТОТ ЖЕ Redis, что инжектится в endpoint.

    ``ArqRedis`` — тонкий сабкласс ``redis.asyncio.Redis``; ``enqueue_job``
    использует только ``pipeline`` и три атрибута конфигурации. Доучиваем
    ими обычный клиент вместо создания второго пула на каждый запрос: и
    соединение переиспользуется, и тесты (fakeredis в override'е
    ``get_redis``) видят реально поставленную задачу.
    """
    if not isinstance(redis, ArqRedis):
        redis.job_serializer = None  # type: ignore[attr-defined]
        redis.job_deserializer = None  # type: ignore[attr-defined]
        redis.default_queue_name = default_queue_name  # type: ignore[attr-defined]
        redis.expires_extra_ms = expires_extra_ms  # type: ignore[attr-defined]
    return cast(ArqRedis, redis)


async def enqueue_deliver(
    redis: Redis, message_id: uuid.UUID, *, job_id: str | None = None
) -> bool:
    """``deliver_message(message_id)`` в ARQ. Строго после commit'а (08 §8.1).

    ``_job_id`` дедуплицирует двойную постановку одной и той же отправки;
    ``/retry`` (01 §6.3) передаёт новый id — счётчик попыток обнуляется.

    Возвращает False, если задача НЕ встала. Вызывающий обязан на это ответить —
    см. ниже, почему прежнее «просто залогировать» не работало.
    """
    try:
        await ArqRedis.enqueue_job(
            as_arq(redis), DELIVER_JOB, message_id, _job_id=job_id or f"deliver:{message_id}"
        )
        return True
    except Exception:  # noqa: BLE001 — очередь недоступна: сообщение уже в БД
        # ⚠ ЗДЕСЬ СТОЯЛО «пузырь останется pending; ретрай руками или следующая
        # постановка» — и ОБОИХ путей восстановления в системе нет.
        #
        #   • «следующая постановка»: повторный POST с тем же
        #     `client_message_id` уходит в ветку replay и задачу не ставит;
        #   • «ретрай руками»: `/retry` работает только со статусом `failed`
        #     (см. проверку в `retry_message`), а тут `pending`.
        #
        # Значит ответ клиенту оставался «Отправляется» НАВСЕГДА, и допихнуть
        # его было нечем. Ронять запрос после успешного commit'а по-прежнему
        # нельзя — сообщение в базе есть. Поэтому просто говорим правду
        # вызывающему, а он помечает отправку неудавшейся: оператор увидит
        # красное и нажмёт «Повторить».
        log.exception("message.enqueue_failed", message_id=str(message_id))
        return False


async def mark_enqueue_failed(db: AsyncSession, message_id: uuid.UUID) -> bool:
    """Пометить отправку неудавшейся, когда задача доставки НЕ встала в очередь.

    ⚠ ЗАЧЕМ ОТДЕЛЬНАЯ ФУНКЦИЯ. Сообщение уже закоммичено, и ронять запрос нельзя.
    Но и оставлять его «Отправляется» нельзя тем более: `pending` не видит ни
    `/retry` (он берёт только `failed`), ни повторный POST (уходит в replay), ни
    красная метка диалога (`refresh_undelivered` считает по `failed`). Ответ
    клиенту оставался в подвешенном состоянии навсегда, и никто об этом не знал.

    Своей транзакцией: вызывается уже ПОСЛЕ commit'а основной.
    """
    # ⚠ НЕ `db.get`: у `messages` СОСТАВНОЙ первичный ключ (id, created_at) —
    # таблица секционирована по дате, и `db.get(Message, id)` падает с
    # InvalidRequestError. Ровно это уже ловили на бою 18.08 в `voice_url`
    # (routes/messages.py), но страховка, написанная позже, снова взяла `get`.
    #
    # Цена ошибки здесь выше, чем там: функция вызывается ИМЕННО ТОГДА, когда
    # задача доставки не встала в очередь. Падая, она оставляла сообщение
    # навсегда в `pending` — его не видит ни `/retry`, ни красная метка, — а
    # фронт на 500 находил в ленте уже опубликованного близнеца и показывал
    # «Сообщение ушло, клиент его получил». Клиент не получал ничего.
    msg = (
        await db.execute(sa.select(Message).where(Message.id == message_id).limit(1))
    ).scalar_one_or_none()
    if msg is None or msg.delivery_status != "pending":
        return False
    # Текст ошибки в модели не хранится — его несёт кадр `message:status`
    # (так же поступает `_fail` в воркере доставки). Статус — из
    # `undelivered_status`: вопрос системы долгом не становится.
    msg.delivery_status = undelivered_status(msg)
    await db.commit()
    return True


# ------------------------------------------------------------- идемпотентность


@dataclass(slots=True)
class IdempotencyOutcome:
    """Результат ``acquire_idempotency``: либо новый id, либо готовый реплей."""

    message_id: uuid.UUID
    existing: Message | None

    @property
    def replay(self) -> bool:
        return self.existing is not None


def idempotency_key(conversation_id: uuid.UUID, client_message_id: str) -> str:
    return f"idem:msg:{conversation_id}:{client_message_id}"


async def acquire_idempotency(
    redis: Redis,
    db: AsyncSession,
    conversation_id: uuid.UUID,
    client_message_id: str,
    text: str,
) -> IdempotencyOutcome:
    """SET NX EX 86400 (08 §8.3). Реплей -> уже созданное сообщение."""
    key = idempotency_key(conversation_id, client_message_id)
    new_id = uuid.uuid4()
    if await redis.set(key, str(new_id), nx=True, ex=IDEM_TTL_SECONDS):
        return IdempotencyOutcome(message_id=new_id, existing=None)

    raw = await redis.get(key)
    if isinstance(raw, bytes):
        raw = raw.decode()
    existing: Message | None = None
    existing_id: uuid.UUID | None = None
    if raw:
        try:
            existing_id = uuid.UUID(raw)
        except ValueError:
            existing_id = None  # мусор в ключе — считаем, что сообщения нет
    if existing_id is not None:
        existing = (
            await db.execute(select(Message).where(Message.id == existing_id))
        ).scalar_one_or_none()

    if existing is None:
        # Ключ есть, сообщения нет: прошлая попытка упала между SET NX и
        # commit'ом. Перезанимаем ключ — для клиента это первая попытка.
        await redis.set(key, str(new_id), ex=IDEM_TTL_SECONDS)
        return IdempotencyOutcome(message_id=new_id, existing=None)

    if (existing.body or "") != text:
        raise ApiError(
            "idempotency_mismatch",
            "Тот же client_message_id с другим текстом — проверьте генерацию идентификатора",
            status=409,
            details={"message_id": str(existing.id)},
        )
    return IdempotencyOutcome(message_id=existing.id, existing=existing)


# -------------------------------------------------------------- сериализация


def message_out_full(
    msg: Message,
    sender: User | None = None,
    *,
    client_message_id: str | None = None,
    quoted: Message | None = None,
) -> dict[str, Any]:
    """MessageOut (01 §6.1) + client_message_id из тела запроса.

    Базовый сериализатор (services/conversations.py) уже отдаёт колонку и
    подписанные вложения; здесь остаётся один случай — реплей и первый ответ
    на POST, где client_message_id известен из запроса и должен вернуться
    даже раньше, чем строка перечитается из БД.
    """
    out = message_out(msg, sender, quoted=quoted)
    out["attachments"] = media.sign_attachments(msg.attachments)
    out["client_message_id"] = client_message_id or msg.client_message_id
    return out


def _set_client_message_id(msg: Message, client_message_id: str) -> None:
    """tempId фронта в ``messages.client_message_id`` (колонка из миграции
    0003): склейка оптимистичного пузыря (01 §1.6) и второй эшелон
    идемпотентности в БД (08 §8.2)."""
    msg.client_message_id = client_message_id


# ----------------------------------------------------------------- проверки


async def get_conversation_for_update(db: AsyncSession, conversation_id: uuid.UUID) -> Conversation:
    """Строка диалога под ``FOR UPDATE`` — порядок захвата 08 §8.4:
    сначала conversations, потом messages.

    ``populate_existing`` тут не украшение. Замка ждут ровно затем, чтобы
    увидеть работу соседа, а сессия к этому моменту уже держит свою копию
    диалога: отправка читает его через ``db.get`` ещё до ключа идемпотентности.
    Без перечитки SQLAlchemy отдаёт из identity map ту самую копию, снятую ДО
    замка, — и ожидание становится бессмысленным: проверки идут по старым
    полям (сосед закрыл диалог, а мы всё равно шлём), а запись возвращает
    статус и assignee к тому, что было до соседа. Для человека это выглядит
    как «диалог сам отдался обратно» и ответ клиенту в закрытую переписку.
    """
    conv = (
        await db.execute(
            select(Conversation)
            .where(Conversation.id == conversation_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if conv is None:
        raise ApiError("not_found", "Диалог не найден", status=404)
    return conv


def validate_text(text: str, *, allow_empty: bool = False) -> str:
    text = (text or "").strip()
    if not text and not allow_empty:
        raise ApiError(
            "validation_error",
            "Пустое сообщение",
            status=400,
            details={"fields": [{"field": "text", "rule": "required", "message": ""}]},
        )
    if len(text) > MAX_TEXT_LENGTH:
        raise ApiError(
            "payload_too_large",
            f"Текст длиннее {MAX_TEXT_LENGTH} символов",
            status=413,
            details={"limit": MAX_TEXT_LENGTH, "length": len(text)},
        )
    return text


async def _assert_may_take_this_channel(db: AsyncSession, conv: Conversation, user: User) -> None:
    """Взять НИЧЕЙНЫЙ диалог ответом можно только на своём канале (7.2).

    ⚠ ВТОРАЯ ДВЕРЬ В ВЛАДЕНИЕ ДИАЛОГОМ, И У НЕЁ НЕ БЫЛО ЗАМКА (28.08).
    Кнопка «Принять» спрашивает `assert_can_take_account` — «этот канал ваш?»
    (`inbox.claim`). Путь «ответил — значит принял» (01 §6.2) назначает
    ответственного ТЕМИ ЖЕ полями и вообще без этого вопроса: во всём
    приложении страж звался ровно из одного места. Список «Все» по каналам не
    сужается, так что чужой диалог виден и открывается обычным нажатием —
    менеджер отвечал клиенту чужого канала и становился хозяином обращения
    без единой ошибки на экране. Фильтр очереди при этом оставался
    оформлением: обойти его можно было не ссылкой, а списком.

    ⚠ СПРАШИВАЕМ ТОЛЬКО У НИЧЕЙНОГО, И ЭТО НЕ ОСЛАБЛЕНИЕ. Ответ в диалог, у
    которого хозяин ЕСТЬ, запрещать нечем и не нужно: «диспетчер подхватывает
    клиента коллеги, ушедшего на обед, — это нормальная работа» (см. ниже,
    `foreign_reply`). Дефект был именно в присвоении: ничейный диалог тем же
    движением уходил в собственность. Заодно это бережёт живую работу — сняв
    человека с канала, администратор не отнимает у него право дописать
    клиентам, за которых он уже отвечает.

    ⚠ ПРИЗНАК «ВЕДЁТ ДИАЛОГИ» ЗДЕСЬ НАМЕРЕННО НЕ ПРОВЕРЯЕТСЯ. Так решено и
    записано в `inbox._assert_takes_part_in_distribution`: «ответ клиенту —
    сознательное вмешательство в конкретный диалог, а не участие в раздаче, и
    бросить клиента без хозяина после ответа хуже». Здесь закрывается только
    канальная ось.
    """
    if conv.assignee_id is not None:
        return
    from app.services import account_operators as acc_ops  # noqa: PLC0415 — цикл импорта

    await acc_ops.assert_can_take_account(db, user, conv.account_id)


async def assert_sendable(
    db: AsyncSession, conv: Conversation, *, allow_closed: bool = False
) -> AvitoAccount:
    """Диалог закрыт -> 422; аккаунт не active -> 409 (01 §6.2).

    ``allow_closed`` — для ``/retry`` (01 §6.3): там из ошибок объявлены
    только ``not_failed`` и ``account_needs_reauth``; уже созданное
    сообщение доехать обязано, даже если диалог успели закрыть.
    """
    if conv.status == "closed" and not allow_closed:
        raise ApiError(
            "unprocessable",
            "Диалог закрыт — верните его в работу, чтобы ответить",
            status=422,
            details={"reason": "conversation_closed"},
        )
    account = await db.get(AvitoAccount, conv.account_id)
    if account is None or account.status != "active":
        raise ApiError(
            "account_needs_reauth",
            "Аккаунт Авито требует переподключения — обратитесь к администратору",
            status=409,
            details={"account_id": str(conv.account_id)},
        )
    return account


# ------------------------------------------------- побочные эффекты отправки


async def _mute_bot(db: AsyncSession, conv: Conversation, user: User) -> bool:
    """02 §2.6: оператор написал — бот замолкает НАВСЕГДА.

    Тело живёт в :func:`app.bots.runtime.mute_bot` — там же типизированное
    ``bot_vars`` и запись ``bot.muted`` (только когда бот действительно вёл
    диалог). Импорт локальный: движок ходит обратно сюда за ``as_arq``, и на
    верхнем уровне это цикл импортов.
    """
    from app.bots.runtime import mute_bot

    return await mute_bot(db, conv, by_user=user)


async def take_over_from_bot(
    db: AsyncSession, conv: Conversation, user: User
) -> tuple[bool, bool, bool]:
    """Забрать диалог у бота, НЕ написав клиенту.

    ⚠ ЗАПРОС ВЛАДЕЛЬЦА 29.08. Заглушить бота умел ровно один путь — отправка
    сообщения (02 §2.6): чтобы вмешаться, оператор был обязан что-то написать
    клиенту. А вмешаться часто нужно молча — посмотреть переписку, дособрать
    данные, позвонить. Единственной альтернативой было выключить бота ЦЕЛИКОМ,
    на всех диалогах сразу (`POST /bots/{id}/disable`), что несоразмерно.

    Делаем ровно то же, что делает ответ, минус сам ответ: диалог назначается
    на того, кто забрал («кто взял — тот и ведёт», 01 §6.2), и бот замолкает
    навсегда. Порядок тот же, что в `create_message`: сначала назначение,
    потом мьют — чтобы `bot.muted` в журнале уже имел ответственного.

    Возвращает (диалог изменился, ушёл из очереди, бот действительно вёл).
    """
    # ⚠ ТОТ ЖЕ ЗАМОК, ЧТО У ОТВЕТА (аудит 30.08).
    #
    # 28.08 путь «ответил — значит принял» получил стража: ничейный диалог
    # берётся только на СВОЁМ канале, потому что список «Все» по каналам не
    # сужается и чужой диалог открывается обычным нажатием. Эта ручка, добавленная
    # 29.08, делает ровно то же присвоение — и звала `_auto_assign` напрямую, в
    # обход стража. Менеджер одного канала мог открыть во «Всех» диалог чужого
    # канала, который ведёт бот, нажать «Забрать у бота» и стать его хозяином.
    #
    # Семантика присвоения здесь та же, значит и вопрос обязан быть тот же.
    await _assert_may_take_this_channel(db, conv, user)
    conversation_changed, claimed_from_queue = await _auto_assign(db, conv, user)
    bot_was_active = await _mute_bot(db, conv, user)
    return conversation_changed or bot_was_active, claimed_from_queue, bot_was_active


async def _auto_assign(db: AsyncSession, conv: Conversation, user: User) -> tuple[bool, bool]:
    """«Кто взял — тот и ведёт» (01 §6.2): new/без ответственного -> на себя.

    Второй элемент — «диалог при этом ушёл из очереди «Входящие»». Ответ в
    непринятый диалог и есть принятие (7.1): писать клиенту, не взяв диалог,
    нельзя по определению — а раз так, диалог обязан исчезнуть из очереди у
    остальных двенадцати НЕМЕДЛЕННО, кадром `inbox:claimed`. Без этого строка
    висела бы у них до перезапроса: нажали бы «Принять» и получили отказ —
    ровно то столкновение, ради которого 7.1 и делалась.

    Почему не 422 «сначала примите диалог»: путь «ответил» существует в
    системе с первого спринта (01 §6.2), им пользуются шаблоны, десктоп и
    любой клиент, написанный до 7.1. Запрет сломал бы их все ради правила,
    которое интерфейс и так соблюдает (кнопки «Принять/Отклонить» вместо поля
    ввода), а честная семантика у ответа одна: ответил — значит принял.
    """
    changed = False
    claimed_from_queue = False
    # Ответ снимает отметку «система отдала, человек ещё не взялся» (7.7).
    # Оператор ответил клиенту — значит он в диалоге, и отбирать его при уходе
    # из сети нельзя: человек вернётся и продолжит. Отметка снимается ИМЕННО
    # здесь, а не по факту открытия диалога: открыть можно и мельком.
    if conv.auto_assigned_at is not None and conv.assignee_id == user.id:
        from app.services.inbox import clear_auto_assignment

        clear_auto_assignment(conv)
        changed = True
    if conv.assignee_id is None:
        conv.assignee_id = user.id
        changed = True
        if conv.offered_at is not None and conv.claimed_by_id is None:
            # Диалог стоял в очереди — фиксируем принятие теми же полями, что и
            # кнопка «Принять» (services.inbox.claim), чтобы «кто принял» и
            # «когда принял» не зависели от того, каким путём это случилось.
            conv.claimed_by_id = user.id
            conv.claimed_at = utcnow()
            claimed_from_queue = True
        await write_audit(
            db,
            user_id=user.id,
            action="conversation.assigned",
            entity="conversation",
            entity_id=str(conv.id),
            details={
                "assignee_id": str(user.id),
                # ответственного не было — иначе это уже передача (01 §5.5)
                "prev_assignee_id": None,
                "by": "self",  # 06 §0.3: «взял в работу»
                # Источник различает «принял из очереди» и «ответил первым» —
                # в журнале это разные события смены (01 §9.7).
                "source": "inbox" if claimed_from_queue else "reply",
            },
        )
    # ПЕРВЫЙ ОТВЕТ ОПЕРАТОРА БУДИТ ДИАЛОГ — через общий вход (docs/38 §3).
    #
    # Здесь стоял свой `if conv.status == "new"` — одна из четырёх копий
    # одного правила, разъехавшихся по сервисам. `ensure_in_progress` добавляет
    # к «новому» ещё и «отложенный»: оператор вернулся к отложенному диалогу и
    # написал в него — значит откладывать больше нечего, и ждать срока
    # возврата тем более.
    #
    # «Ждёт клиента» этот путь НЕ трогает, и это самое важное здесь. Написать
    # клиенту, не забирая ход себе, — законное действие («напоминаю про
    # завтра»); сбрасывай мы статус на каждом нашем сообщении, значение
    # обесценилось бы за день.
    changed = await convs.ensure_in_progress(db, conv, source="reply", actor_id=user.id) or changed
    return changed, claimed_from_queue


async def _sender_of(db: AsyncSession, msg: Message, fallback: User) -> User:
    """Автор сообщения для MessageOut. На реплее (01 §1.6) тело обязано быть
    идентичным первому ответу — значит и sender берём из самого сообщения."""
    if msg.sender_user_id and msg.sender_user_id != fallback.id:
        sender = await db.get(User, msg.sender_user_id)
        if sender is not None:
            return sender
    return fallback


# ------------------------------------------------------------------ создание


@dataclass(slots=True)
class CreatedMessage:
    message: Message
    conversation: Conversation
    sender: User
    client_message_id: str
    replay: bool
    conversation_changed: bool
    # Ответ увёл диалог из очереди «Входящие» (7.1): ручке нужно опубликовать
    # `inbox:claimed`, иначе строка останется в очереди у остальных.
    claimed_from_queue: bool = False
    # Ответил НЕ ответственный за диалог (SCEN-49). Ответственный при этом не
    # меняется — см. `_auto_assign`; поле нужно, чтобы расхождение было видно
    # снаружи, а не только в журнале.
    foreign_reply: bool = False
    #: Цитируемое сообщение, если ответ был на конкретное (просьба владельца
    #: 02.09). Несём ОБЪЕКТОМ, а не перечитываем: он уже поднят из базы при
    #: проверке принадлежности, и второй запрос ради того же — лишний.
    quoted: Message | None = None


async def resolve_reply_to(
    db: AsyncSession, conv: Conversation, reply_to_id: uuid.UUID | None
) -> Message | None:
    """Проверить цитируемое сообщение и вернуть его.

    ⚠ ПРОВЕРКА ПРИНАДЛЕЖНОСТИ — НЕ ФОРМАЛЬНОСТЬ, А ЗАЩИТА ОТ УТЕЧКИ ПЕРЕПИСКИ.
    Идентификатор приходит с клиента, а цитата показывается текстом в ленте.
    Прими мы чужой `id` — и в диалог одного человека уехал бы кусок разговора с
    другим, вместе с его словами. У сообщений нет отдельной проверки прав:
    право читать переписку даёт диалог, поэтому и цитата обязана быть из ЭТОГО
    диалога.

    ⚠ ЗАМЕТКИ ЦИТИРОВАТЬ НЕЛЬЗЯ. Заметка — внутренняя запись, её не видят
    наблюдатели без права `notes:read`. Цитата же едет в общем поле сообщения и
    прав не спрашивает: через неё снимок заметки утёк бы мимо ограничения.

    ⚠ ВОЗВРАЩАЕТСЯ САМ ОБЪЕКТ, А НЕ ЕГО КЛЮЧ. Он всё равно поднят из базы этой
    проверкой, и он же нужен кадру `message:new`: у второго диспетчера, который
    смотрит тот же диалог, ответ обязан появиться СО СВЯЗЬЮ, а не голым. Второй
    запрос ради того же был бы лишним.

    В записи сохраняется ПАРА `(id, created_at)`: `messages` партиционирована
    помесячно, ключ составной, и без даты выборка цитаты обошла бы все 28
    партиций (миграция 0060).
    """
    if reply_to_id is None:
        return None
    строка = (
        await db.execute(
            select(Message).where(
                Message.id == reply_to_id,
                Message.conversation_id == conv.id,
            )
        )
    ).scalar_one_or_none()
    if строка is None:
        raise ApiError(
            "validation_error",
            "Сообщение, на которое отвечаете, не найдено в этом диалоге",
            status=422,
            details={
                "fields": [
                    {"field": "reply_to_id", "rule": "not_in_conversation", "message": "чужое"}
                ]
            },
        )
    if строка.direction == "note":
        raise ApiError(
            "validation_error",
            "На заметку нельзя ответить: её не видят все, кто видит переписку",
            status=422,
            details={
                "fields": [{"field": "reply_to_id", "rule": "note", "message": "это заметка"}]
            },
        )
    return строка


async def create_outbound_message(
    db: AsyncSession,
    redis: Redis,
    *,
    conversation_id: uuid.UUID,
    user: User,
    text: str,
    client_message_id: str,
    attachments: list[dict[str, Any]] | None = None,
    reply_to_id: uuid.UUID | None = None,
) -> CreatedMessage:
    """POST /conversations/{id}/messages (01 §6.2) — всё до публикации событий."""
    # Проверки до захвата ключа идемпотентности: на мусорный запрос ключ
    # ставить незачем. Реплей же обязан вернуть сообщение даже если диалог
    # успели закрыть — поэтому он проверяется отдельной веткой ниже.
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise ApiError("not_found", "Диалог не найден", status=404)

    outcome = await acquire_idempotency(redis, db, conv.id, client_message_id, text)
    if outcome.existing is not None:
        return CreatedMessage(
            message=outcome.existing,
            conversation=conv,
            sender=await _sender_of(db, outcome.existing, user),
            client_message_id=client_message_id,
            replay=True,
            quoted=await quoted_of(db, outcome.existing),
            conversation_changed=False,
        )

    # Строка диалога под FOR UPDATE — сначала conversations, потом messages
    # (порядок захвата 08 §8.4): гонка «оператор пишет vs бот шлёт vs реопен».
    conv = await get_conversation_for_update(db, conv.id)
    await assert_sendable(db, conv)
    await _assert_may_take_this_channel(db, conv, user)

    # Цитата разрешается ОДИН РАЗ, здесь, и уже под захваченной строкой диалога:
    # сообщение не может уехать из диалога между проверкой и записью.
    цитата = await resolve_reply_to(db, conv, reply_to_id)

    now = utcnow()
    msg = Message(
        id=outcome.message_id,
        conversation_id=conv.id,
        external_message_id=None,
        direction="out",
        sender_type="operator",
        sender_user_id=user.id,
        body=text,
        attachments=attachments or [],
        delivery_status="pending",  # итог придёт WS-событием message:status
        created_at=now,
        reply_to_id=цитата.id if цитата else None,
        reply_to_created_at=цитата.created_at if цитата else None,
    )
    _set_client_message_id(msg, client_message_id)
    db.add(msg)

    # ВТОРОЙ ЭШЕЛОН — тот, что действительно работает (#25).
    #
    # Первый рубеж, ключ в Redis, имеет ветку восстановления: «ключ есть, а
    # сообщения по нему нет — значит прошлая попытка умерла, перезанимаем».
    # Отличить «умерла» от «ещё летит» она не умеет, и две одновременные
    # отправки с одним идентификатором дают ровно её: первая захватила ключ и
    # пишет, вторая читает — а её транзакция ещё не закоммичена, — и заводит
    # ВТОРОЕ сообщение. Клиент получает один и тот же ответ дважды.
    #
    # Строка ниже пишется В ТОЙ ЖЕ транзакции, что и сообщение, поэтому
    # проигравшая попытка упирается в первичный ключ независимо от Redis и от
    # того, в какой момент она подоспела.
    #
    # Прежний «второй эшелон» — уникальный индекс на `messages` — не ловил
    # ничего: он включает `created_at` (требование партиционирования), а у
    # повторной попытки время другое, и тройка снова уникальна.
    db.add(
        MessageIdempotency(
            conversation_id=conv.id,
            client_message_id=client_message_id,
            message_id=msg.id,
        )
    )
    try:
        await db.flush()
    except IntegrityError:
        # Гонку выиграл кто-то другой. Для клиента это не ошибка: он просил
        # отправить сообщение — сообщение отправлено, просто не нашей
        # попыткой. Возвращаем победителя тем же кодом, что и обычный реплей.
        # ИДЕНТИФИКАТОР БЕРЁМ ИЗ АРГУМЕНТА, А НЕ ИЗ ORM-ОБЪЕКТА.
        # `rollback` истекает атрибуты загруженных объектов, и обращение к
        # `conv.id` после него полезло бы в базу вне асинхронного контекста —
        # MissingGreenlet вместо честного ответа. Поймано тестом.
        await db.rollback()
        winner = await _message_by_client_id(db, conversation_id, client_message_id)
        if winner is None:
            raise
        conv = await db.get(Conversation, conversation_id)
        assert conv is not None
        return CreatedMessage(
            message=winner,
            conversation=conv,
            sender=await _sender_of(db, winner, user),
            client_message_id=client_message_id,
            replay=True,
            conversation_changed=False,
            quoted=await quoted_of(db, winner),
        )

    # КТО ВЁЛ ДИАЛОГ В МОМЕНТ, КОГДА В НЕГО НАПИСАЛИ (SCEN-49).
    #
    # Снимок берётся до `_auto_assign` намеренно, хотя сегодня ответ от этого
    # не зависит: та ставит ответственного только БЕСХОЗНОМУ диалогу, а у
    # бесхозного новый ответственный — сам автор, и «чужим» ответ не станет ни
    # при каком порядке. Держим снимок впереди потому, что стоит `_auto_assign`
    # получить хоть одну ветку переназначения — и вычисленный после неё признак
    # начнёт молча отвечать «свой» на каждый чужой ответ.
    #
    # Отвечать в чужой диалог не запрещено и запрещать нечем: диспетчер
    # подхватывает клиента коллеги, ушедшего на обед, — это нормальная работа.
    # Плохо было другое: не оставалось НИКАКОГО следа. Ответственный не
    # менялся, «Мои» и статистика первого ответа оставались за первым, а
    # человек, за которым диалог числится, не узнавал, что клиенту уже
    # ответили, — и отвечал вторым. Ровно та пара ответов, из-за которой
    # SCEN-48 и SCEN-49 попали в один список.
    owner_before = conv.assignee_id
    foreign_reply = owner_before is not None and owner_before != user.id

    conversation_changed, claimed_from_queue = await _auto_assign(db, conv, user)
    if await _mute_bot(db, conv, user):  # 02 §2.6 — audit `bot.muted` пишет mute_bot
        conversation_changed = True
    if foreign_reply:
        # Журнал (01 §9.7): без этой записи разбор «кто ответил клиенту дважды»
        # упирается в то, что сообщение есть, а события смены нет вовсе.
        await write_audit(
            db,
            user_id=user.id,
            action="conversation.foreign_reply",
            entity="conversation",
            entity_id=str(conv.id),
            details={"assignee_id": str(owner_before), "sender_id": str(user.id)},
        )
    conv.last_message_at = now
    # Ответили — клиент больше не ждёт. Снимается ЗДЕСЬ, а не в `_auto_assign`:
    # ответить может и тот, кто диалог не берёт (диалог уже за кем-то), и
    # ожидание при этом всё равно закончилось.
    conv.awaiting_since = None
    await db.commit()

    # Строго ПОСЛЕ commit'а (08 §8.1). Живёт здесь, а не в ручке, потому что
    # прежний ответственный известен только тут: наружу он не выходит, а тащить
    # его через `CreatedMessage` ради одного вызова — заводить поле, которое
    # обязан не забыть каждый следующий вызывающий.
    if foreign_reply and owner_before is not None:
        await publish_foreign_reply(redis, conv, sender=user, assignee_id=owner_before)

    return CreatedMessage(
        message=msg,
        conversation=conv,
        sender=user,
        client_message_id=client_message_id,
        replay=False,
        conversation_changed=conversation_changed,
        claimed_from_queue=claimed_from_queue,
        foreign_reply=foreign_reply,
        quoted=цитата,
    )


async def _message_by_client_id(
    db: AsyncSession, conversation_id: uuid.UUID, client_message_id: str
) -> Message | None:
    """Победитель гонки — по записи второго эшелона, а не по самому сообщению.

    Искать напрямую в `messages` нельзя: таблица партиционирована, и запрос
    без времени пошёл бы по всем партициям. Запись эшелона хранит нужный
    идентификатор, а по нему сообщение достаётся первичным ключом.
    """
    row = await db.get(MessageIdempotency, (conversation_id, client_message_id))
    if row is None:
        return None
    return (
        await db.execute(select(Message).where(Message.id == row.message_id))
    ).scalar_one_or_none()


async def create_note(
    db: AsyncSession,
    redis: Redis,
    *,
    conversation_id: uuid.UUID,
    user: User,
    text: str,
    client_message_id: str,
    attachments: list[dict[str, Any]] | None = None,
) -> CreatedMessage:
    """POST /conversations/{id}/notes (01 §6.4).

    В Авито не уходит, ``delivery_status='delivered'`` сразу. Бота НЕ глушит
    (02 §2.6: «заметки бота не глушат») и диалог не назначает — это не ответ
    клиенту. Закрытый диалог заметку принимает: комментарий постфактум
    легален.
    """
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        raise ApiError("not_found", "Диалог не найден", status=404)
    outcome = await acquire_idempotency(redis, db, conv.id, client_message_id, text)
    if outcome.existing is not None:
        return CreatedMessage(
            message=outcome.existing,
            conversation=conv,
            sender=await _sender_of(db, outcome.existing, user),
            client_message_id=client_message_id,
            replay=True,
            quoted=await quoted_of(db, outcome.existing),
            conversation_changed=False,
        )

    conv = await get_conversation_for_update(db, conv.id)
    now = utcnow()
    msg = Message(
        id=outcome.message_id,
        conversation_id=conv.id,
        external_message_id=None,
        direction="note",
        sender_type="operator",
        sender_user_id=user.id,
        body=text,
        # ЗДЕСЬ СТОЯЛ ЖЁСТКИЙ ПУСТОЙ СПИСОК, И ФАЙЛ ИСЧЕЗАЛ (#30). Композер
        # советует прикладывать файлы именно к заметке — раз так, они обязаны
        # в ней и оставаться. Заметка никуда не уходит, поэтому доставлять
        # вложение некуда: оно просто хранится и видно команде.
        attachments=list(attachments or []),
        delivery_status="delivered",
        created_at=now,
    )
    _set_client_message_id(msg, client_message_id)
    db.add(msg)
    # last_message_at не двигаем (это метка последнего сообщения КЛИЕНТУ),
    # но строку диалога помечаем изменённой — иначе догон по ?updated_since=
    # (01 §11.7) не увидит новую заметку.
    conv.updated_at = now
    await db.commit()

    return CreatedMessage(
        message=msg,
        conversation=conv,
        sender=user,
        client_message_id=client_message_id,
        replay=False,
        conversation_changed=False,
    )


# ------------------------------------------------- ответ не ушёл клиенту (#26)


async def refresh_undelivered(db: AsyncSession, conv: Conversation) -> bool:
    """Пересчитать `conversations.undelivered_at`. Возвращает: значение менялось.

    ОДНА ФУНКЦИЯ НА ВСЕ ТРИ ПУТИ — провал доставки, повтор, удаление
    неотправленного. Признак можно было бы править инкрементами («упало —
    ставим, повторили — снимаем»), но у диалога неотправленных может быть
    несколько, и любой пропущенный путь оставил бы красную метку на диалоге,
    где всё давно доставлено. Такую метку человек перестаёт замечать за неделю,
    и тогда она не сработает в тот единственный раз, ради которого заводилась.
    Один запрос по одному диалогу на редком пути — приемлемая цена за то, что
    значение не может разойтись с правдой.

    Берётся САМОЕ РАННЕЕ неотправленное: «висит с 14:32» — это про первый
    непрошедший ответ, а не про последний.
    """
    earliest = await db.scalar(
        select(sa.func.min(Message.created_at)).where(
            Message.conversation_id == conv.id,
            Message.direction == "out",
            Message.delivery_status == "failed",
        )
    )
    if conv.undelivered_at == earliest:
        return False
    conv.undelivered_at = earliest
    return True


def undelivered_status(msg: Message) -> str:
    """Какой статус получает исходящее, которое НЕ ушло клиенту.

    `failed` — долг: красная метка диалога (`refresh_undelivered`), «Повторить»,
    «Снять». Вопрос системы об адресе (`('out','system')`, workers/address_ask.py)
    долгом не становится: повторять его некому и незачем (следующее входящее
    поставит свою проверку), а метку снять оператор не может — retry/dismiss
    отдают 403 при `sender_user_id IS NULL`. Сразу `dismissed`: строка остаётся
    в ленте с серой подписью «Не доставлено», диалог в долгу не числится.

    ОДНА функция на двух писателей — `deliver._fail` и `mark_enqueue_failed`:
    правило «кто становится долгом» живёт в одном месте.
    """
    if msg.direction == "out" and msg.sender_type == "system":
        return "dismissed"
    return "failed"


async def restore_awaiting(db: AsyncSession, conv: Conversation) -> bool:
    """ПЕРЕСЧИТАТЬ отметку «клиент ждёт» из переписки: и вернуть, и снять.

    ⚠ ИМЯ ОСТАЛОСЬ ПРЕЖНИМ, А СМЫСЛ РАСШИРЕН 02.09 — по обратной связи от
    диспетчеров: «чат прочитан, отвечен, но тайминг висит 1 мин. Даже при
    обновлении страницы».

    ЧТО ПРОИСХОДИЛО. Отметка гаснет при нажатии «Отправить», а если доставка
    провалилась — эта функция её ВОЗВРАЩАЛА (иначе клиент числился отвеченным,
    не получив ответа). Но ветка УСПЕШНОЙ доставки отметку не трогала вовсе.
    Значит путь «отправили → доставка сорвалась → повторили → дошло» оставлял
    диалог вечно ждущим: погасить его было больше некому, и обновление страницы
    не помогало — на сервере так и стояло.

    ⚠ ПОЭТОМУ ФУНКЦИЯ ТЕПЕРЬ СИММЕТРИЧНА. Отметка не хранится и не
    «запоминается перед отправкой» — она ВЫЧИСЛЯЕТСЯ из переписки, и вычисление
    одно на оба исхода доставки. Две функции («вернуть» и «снять») разъехались
    бы на первой же правке: это ровно тот класс, на котором проект уже горел.

    Граница — последний ДОСТАВЛЕННЫЙ ответ оператора или бота; `system`
    (вопрос об адресе, workers/address_ask.py) не граница: клиент по-прежнему
    ждёт человека. Ждёт клиент с первого своего сообщения после этой границы
    (то же правило «с первого, а не с последнего», что и у входящих). Нет
    таких сообщений — не ждёт никто, и отметка снимается.

    ⚠ ПЕРВАЯ ПОЛОВИНА ЭТОЙ РАБОТЫ (её довод сохранён целиком). `awaiting_since`
    снимается в момент нажатия «Отправить» — ещё до попытки доставки. Если
    доставка провалилась, отметка не возвращалась: для системы клиент был
    отвечен. Значит на такой диалог НИКОГДА не срабатывал сторож «клиент ждёт
    15 минут» — ровно на том, где он нужнее всего.

    Если отметка уже стоит (клиент успел написать снова), берём более раннюю:
    ждать он начал раньше, чем написал повторно.

    Возвращает True, если отметка изменилась.
    """
    boundary = await db.scalar(
        select(sa.func.max(Message.created_at)).where(
            Message.conversation_id == conv.id,
            Message.direction == "out",
            Message.delivery_status == "delivered",
            # Вопрос системы об адресе (workers/address_ask.py) — не ответ клиенту:
            # клиент по-прежнему ждёт человека. Оператор и бот — как были.
            Message.sender_type != "system",
        )
    )
    conds = [Message.conversation_id == conv.id, Message.direction == "in"]
    if boundary is not None:
        conds.append(Message.created_at > boundary)
    since = await db.scalar(select(sa.func.min(Message.created_at)).where(*conds))
    if since is None:
        # Клиент после нашего доставленного ответа не писал — ждать ему нечего.
        # ⚠ ИМЕННО ЗДЕСЬ СНИМАЕТСЯ ОТМЕТКА, и без этой ветки диалог, дошедший
        # со второй попытки, висел «ждёт» до скончания века.
        if conv.awaiting_since is None:
            return False
        conv.awaiting_since = None
        return True
    if conv.awaiting_since is not None and conv.awaiting_since <= since:
        return False
    conv.awaiting_since = since
    return True


# -------------------------------------------------------------------- retry


def may_fix_undelivered(msg: Message, user: User) -> bool:
    """Кто может повторить или снять неотправленное: автор и администратор.

    Реплика бота автора не имеет — её может повторить любой оператор диалога.
    Иначе красная метка «ответ не дошёл» на реплике бота не снималась никем.
    """
    if user.role == "admin" or msg.sender_user_id == user.id:
        return True
    return msg.sender_user_id is None and msg.sender_type == "bot"


async def quoted_of(db: AsyncSession, msg: Message) -> Message | None:
    """Цитата одного сообщения — для ответов ручек, где лента не перечитывается."""
    if msg.reply_to_id is None:
        return None
    return (await convs._quoted_for(db, [msg])).get(msg.reply_to_id)


async def reset_for_retry(
    db: AsyncSession, redis: Redis, *, message_id: uuid.UUID, user: User
) -> Message:
    """POST /messages/{id}/retry (01 §6.3): failed -> pending + новая джоба."""
    msg = (await db.execute(select(Message).where(Message.id == message_id))).scalar_one_or_none()
    if msg is None:
        raise ApiError("not_found", "Сообщение не найдено", status=404)
    if not may_fix_undelivered(msg, user):
        raise ApiError(
            "forbidden", "Повторить отправку может только автор сообщения или админ", status=403
        )

    # Порядок захвата 08 §8.4: conversations, затем messages.
    conv = await get_conversation_for_update(db, msg.conversation_id)
    await assert_sendable(db, conv, allow_closed=True)
    # ``populate_existing`` — по той же причине, что и у диалога выше: строка
    # сообщения уже прочитана в начале функции, без замка. Без перечитки
    # проверка ниже смотрит на копию ДО замка, и два одновременных
    # «Повторить» оба видят failed — обе поставят джобу, и клиент получит
    # одно и то же сообщение дважды.
    msg = (
        await db.execute(
            select(Message)
            .where(Message.id == message_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    if msg.direction != "out" or msg.delivery_status != "failed":
        raise ApiError(
            "unprocessable",
            "Повторить можно только неотправленное сообщение",
            status=422,
            details={"reason": "not_failed", "delivery_status": msg.delivery_status},
        )
    msg.delivery_status = "pending"
    # Красная метка в списке снимается СРАЗУ, а не по итогу повтора (#26):
    # оператор нажал «Повторить» — сообщение снова в пути, и висящая метка
    # означала бы «сделай что-нибудь» там, где делать уже нечего. Если повтор
    # тоже не пройдёт, метку вернёт `_fail` — она не теряется, а ждёт исхода.
    await refresh_undelivered(db, conv)
    await db.commit()
    return msg


async def dismiss_failed(db: AsyncSession, *, message_id: uuid.UUID, user: User) -> Message:
    """POST /messages/{id}/dismiss — снять неотправленное с учёта.

    ⚠ ЗАЧЕМ ЭТО ПОЯВИЛОСЬ (жалоба владельца 08.09: «статус „не отправлено“ не
    пропадает»). Красную метку диалога ставит `refresh_undelivered` по наличию
    сообщения со статусом `failed`. Выхода из этого состояния было ровно два:
    удачный повтор или ничего. А повтор помогает не всегда: канал Авито мог
    отвалиться навсегда, диалог — закрыться, текст — устареть. Живой случай на
    снимке: оператор не стал повторять, а набрал тот же текст заново и отправил
    новым сообщением — оно ушло, а старое осталось `failed`, и метка «ответ не
    ушёл» повисла на диалоге навсегда.

    Метка, которая не гаснет, перестаёт значить хоть что-то: человек перестаёт
    её замечать за неделю — и она не сработает в тот единственный раз, ради
    которого заведена. Ровно об этом предупреждает шапка `refresh_undelivered`.

    ⚠ СООБЩЕНИЕ НЕ УДАЛЯЕТСЯ И НЕ ПРЯЧЕТСЯ. Оно остаётся в переписке с
    пометкой «снято»: клиент его не получил, и это факт разговора, который
    может понадобиться при разборе. Меняется только одно — диалог перестаёт
    числиться в долгу.

    Статус кладём новым значением `dismissed`. Миграция не нужна: колонка
    объявлена текстом ровно с этим расчётом (`models/message.py`).
    """
    msg = (await db.execute(select(Message).where(Message.id == message_id))).scalar_one_or_none()
    if msg is None:
        raise ApiError("not_found", "Сообщение не найдено", status=404)
    if not may_fix_undelivered(msg, user):
        raise ApiError("forbidden", "Снять сообщение может только его автор или админ", status=403)

    # Порядок захвата 08 §8.4: conversations, затем messages. Тот же, что у
    # повтора: две кнопки на одном сообщении не должны разъехаться.
    conv = await get_conversation_for_update(db, msg.conversation_id)
    msg = (
        await db.execute(
            select(Message)
            .where(Message.id == message_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one()

    if msg.direction != "out" or msg.delivery_status != "failed":
        raise ApiError(
            "unprocessable",
            "Снять можно только неотправленное сообщение",
            status=422,
            details={"reason": "not_failed", "delivery_status": msg.delivery_status},
        )
    msg.delivery_status = "dismissed"
    await refresh_undelivered(db, conv)
    await db.commit()
    return msg


# ------------------------------------------------------------------- события


async def publish_message_new(
    redis: Redis, created: CreatedMessage, *, unread_delta: int = 0
) -> None:
    """message:new (01 §11.3). Свой же пузырь приходит другим вкладкам и
    коллегам; observer заметок не получает — фильтр в Hub'е (08 §5.3)."""
    conv = created.conversation
    await publish_event(
        redis,
        "message:new",
        {
            "conversation_id": str(conv.id),
            "message": message_out_full(
                created.message,
                created.sender,
                client_message_id=created.client_message_id,
                quoted=created.quoted,
            ),
            "conversation_patch": {
                "unread_delta": unread_delta,
                "last_message_at": iso(conv.last_message_at),
                "status": conv.status,
                # ⚠ ОТВЕТИЛИ — ШКАЛА ОЖИДАНИЯ ОБЯЗАНА ПОГАСНУТЬ (28.08, жалоба
                # с бою: «диалог отвечен, но висит у всех как неотвеченный»).
                #
                # В базе всё было верно: замер боя дал 56 диалогов с нашим
                # последним сообщением и НИ ОДНОГО с непогашенным `awaiting_since`
                # — снимает его `send` строкой `conv.awaiting_since = None`.
                # Врал экран: этот кадр нёс статус и время, но не нёс ожидание, а
                # строка списка держит его с последней полной загрузки. Оранжевая
                # метка продолжала расти у всех тринадцати, пока список не
                # перезапросят целиком. На снимке от диспетчера это видно прямо:
                # внизу серверная сводка «2 ждут ответа», а меток в списке три.
                #
                # Шлём КАНОНИЧЕСКИЙ расчёт, а не сырое поле: у `waiting_since`
                # ровно одно определение на весь продукт, и второе, собранное
                # здесь из `awaiting_since`, разъехалось бы с ним по статусу.
                "waiting_since": iso(status_dict.waiting_since(conv)),
            },
        },
    )


async def publish_claimed_by_reply(redis: Redis, conv: Conversation, user: User) -> None:
    """`inbox:claimed` для диалога, который увёл из очереди ОТВЕТ (7.1).

    Кадр тот же, что у кнопки «Принять» (`app/api/routes/inbox.py`), и патч
    собран тем же кодом (`services.inbox.claimed_patch`): у остальных
    двенадцати строка очереди обязана исчезнуть одинаково — независимо от
    того, нажал коллега «Принять» или просто ответил клиенту. Разные кадры на
    одно и то же событие означали бы, что половина вкладок про это не узнает.
    """
    from app.services.inbox import claimed_patch, waiting_seconds
    from app.ws.hub import INBOX_CLAIMED

    patch = claimed_patch(conv, user)
    await publish_event(
        redis,
        INBOX_CLAIMED,
        {
            "conversation_id": str(conv.id),
            "claimed_by": user_ref(user),
            "claimed_at": iso(conv.claimed_at),
            "waited_seconds": waiting_seconds(conv),
            "conversation_patch": patch,
        },
    )


async def publish_foreign_reply(
    redis: Redis, conv: Conversation, *, sender: User, assignee_id: uuid.UUID
) -> None:
    """Ответственному — что в его диалоге ответил кто-то другой (SCEN-49).

    Кадр `notify` без `id` (14 §4) — то есть тост, а не строка колокольчика.
    Выбрано намеренно: событие срочное и одноразовое, важно оно ровно в те
    полминуты, пока человек не начал печатать второй ответ. Строка в центре
    уведомлений жила бы сутки и к вечеру превратилась бы в шум — а шум там уже
    один раз похоронил настоящую тревогу (см. `trimRecent` во фронте).

    `only_user` — потому что это личное сообщение одному человеку, а не
    новость для команды: остальным одиннадцати знать, кто кому помог, незачем.
    """
    await publish_event(
        redis,
        "notify",
        {
            "level": "warning",
            "title": "В вашем диалоге ответил коллега",
            "text": f"{sender.full_name} написал(а) клиенту — диалог числится за вами",
        },
        only_user=str(assignee_id),
    )


async def publish_conversation_updated(redis: Redis, conv: Conversation, user: User) -> None:
    """conversation:updated после автоназначения (01 §11.3)."""
    await publish_event(
        redis,
        "conversation:updated",
        {
            "conversation_id": str(conv.id),
            "patch": {
                "status": conv.status,
                "assignee": user_ref(user) if conv.assignee_id == user.id else None,
                "bot_active": conv.bot_active,
            },
        },
    )


async def publish_status(
    redis: Redis,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    delivery_status: str,
    error: str | None = None,
    error_code: str | None = None,
    conversation_patch: dict[str, Any] | None = None,
) -> None:
    """message:status (01 §11.3) — итог доставки; ``error`` только при failed.

    ``conversation_patch`` — то же поле и тот же смысл, что у ``message:new``:
    вместе с судьбой пузыря едет то, что от неё изменилось В СТРОКЕ СПИСКА
    (#26 — «ответ не ушёл»). Отдельным событием это слать нельзя: два кадра на
    одно событие приходят порознь, и полсекунды между ними строка показывала бы
    успешно отвеченный диалог.
    """
    data: dict[str, Any] = {
        "conversation_id": str(conversation_id),
        "message_id": str(message_id),
        "delivery_status": delivery_status,
    }
    if conversation_patch:
        data["conversation_patch"] = conversation_patch
    if error:
        data["error"] = error
    if error_code:
        data["error_code"] = error_code
    await publish_event(redis, "message:status", data)


async def publish_transcript(
    redis: Redis,
    *,
    conversation_id: uuid.UUID,
    message_id: uuid.UUID,
    voice_transcript: str | None,
    voice_transcript_status: str | None,
) -> None:
    """message:transcript — расшифровка голосового досчиталась или не вышла.

    Строение как у ``message:status``: адрес сообщения плюс те два поля
    строки, которые изменились. Зовётся строго ПОСЛЕ commit'а (08 §8.1): кадр
    до commit'а заставил бы экран перечитать ещё старую строку — ровно та
    беда, что была с телефоном клиента 02.09.

    ⚠ ЗАЧЕМ КАДР, ЕСЛИ ТЕКСТ И ТАК ЕДЕТ С СООБЩЕНИЕМ. Whisper считает запись
    секунды-минуты, дольше, чем человек смотрит на пузырь. Без кадра текст
    приезжал только со следующим открытием диалога — и владелец, глядя на
    два голосовых без единой строки, спрашивал, «где находится расшифровка».

    Уезжает и при ``failed`` / ``too_long`` (текст ``None``): подпись под
    проигрывателем обязана смениться с «готовится» на честный исход, иначе
    «готовится» висит до F5.
    """
    await publish_event(
        redis,
        "message:transcript",
        {
            "conversation_id": str(conversation_id),
            "message_id": str(message_id),
            "voice_transcript": voice_transcript,
            "voice_transcript_status": voice_transcript_status,
        },
    )
