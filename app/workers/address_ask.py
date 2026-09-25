"""Задача «один вопрос об адресе» — лестница замков и запись (владелец 18.09).

Кто ставит: `services/inbound.apply_inbound_event` после commit'а входящего,
через `services/address_ask.enqueue_address_ask` с задержкой
`address_ask.delay_sec` (умолчание 600 с). Задача на КАЖДОЕ входящее, а не
одна на диалог с самопереносом: серия из пяти реплик даёт пять дешёвых задач —
четыре выходят на `too_soon` (один SELECT после реестра полей), пятая решает.

Все замки — здесь, под `SELECT … FOR UPDATE` диалога, в ОДНОЙ транзакции
(образец `bots/runtime._run_tick`). Порядок — дешёвые замки по полям, затем
один SELECT последнего сообщения, и только потом аккаунт, бот и Redis.
Замок по строкам адреса — `services/address_ask.candidate_lock`, один с сухим
прогоном: глушит строка со степенью; строка, которую карта ещё проверяет, —
самоповтор; окончательный отказ без степени — не замок (контракт п.2/п.5).

Исходящее пишется прямой вставкой `Message` с `sender_type='system'` (не
`'bot'`: пару `('out','bot')` статистика и «Диалоги бота» читают как «бот
отвечал»), после commit'а — кадр `message:new` и `messages.enqueue_deliver`.
НЕ `deliver_message` напрямую и НЕ `send_bot_message`.

Итог задачи — имя замка из `ЗАМКИ`, `retry` или `sent`. В журнал — причины и
числа, никаких тел.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from arq.jobs import Job, JobStatus
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bots.handoff import message_new_event
from app.bots.runtime import bot_entry_block, get_bot_for_conversation
from app.core.observability import with_job_scope
from app.models import AvitoAccount, Client, Conversation, Message
from app.services import address_ask, app_settings, voice
from app.services import messages as messages_svc
from app.services.audit import write_audit
from app.services.avito_accounts import get_backfill_state
from app.services.cards_catchup import job_id as catchup_job_id
from app.ws.events import publish_event

log = structlog.get_logger("app.address_ask")

#: Реестр замков (как `runtime.ENTRY_BLOCKS`): ключ — в лог и итог задачи,
#: подпись — для людей. Сторож в test_address_ask_1809: каждая причина из
#: `_skip`/`_повтор`/`field_lock`/`card_lock`/`candidate_lock` есть здесь.
ЗАМКИ: dict[str, str] = {
    "disabled": "вопрос выключен в настройках (или выключен разбор адресов)",
    "text_unrecognizable": "текст вопроса без слова об адресе — ответ не распознался бы",
    "no_conversation": "диалога нет",
    "closed": "диалог закрыт",
    "already_asked": "система уже спрашивала в этом диалоге",
    "claimed_by_human": "диалог принят человеком — ведёт человек",
    "bot_leads": "диалог ведёт бот (или войдёт следующим ходом)",
    "no_messages": "в диалоге нет переписки",
    "answered": "последнее слово не за клиентом",
    "too_soon": "клиент писал моложе задержки — человек ещё может ответить",
    "stale": "задача опоздала к плановому моменту больше чем на час — разговор ушёл",
    "account_inactive": "аккаунт Авито не активен",
    "history_loading": "история чата ещё едет или догоняется разбором — адрес может быть в ней",
    "client_blocked": (
        "клиент в чёрном списке — команда решила не работать, система не заговаривает первой"
    ),
    "card_has_address": "адрес уже в карточке",
    "candidate_exists": "строка адреса со степенью в диалоге уже есть",
    "geo_pending": "карта ещё проверяет строку адреса",
    "llm_reading": "модель ещё читает реплику",
    "transcribing": (
        "расшифровка голосового ещё считается или разбирается — адрес может быть в ней"
    ),
    "asked_in_feed": (
        "об адресе (подъезде, этаже, квартире, улице, «куда подъехать») уже спрашивали в ленте"
    ),
    "declined": "клиент отказался или отменил — спрашивать адрес незачем",
    "not_described": "клиент ещё ничего не описал",
}
#: Задача проснулась после простоя воркера — не спрашиваем. Считается от
#: ПЛАНОВОГО момента `реплика + delay_sec`, а не от реплики: иначе при
#: `delay_sec` у верхней границы ручки (3600) `too_soon` (< delay) и `stale`
#: (> час) смыкались бы, и вопрос не ушёл бы никогда.
MAX_AGE = timedelta(hours=1)
#: Сколько последних входящих проверять на «модель читает» (= `_ПОТОЛОК_ДИАЛОГА`
#: у workers/address_llm.py: больше чтений на диалог в сутки не бывает).
LLM_CHECK_LIMIT = 5
#: Переходные замки (история едет / модель читает): самоповтор через минуту,
#: не больше трёх раз — покрывает `convhist` одного чата и чтение модели.
RETRY_DEFER_SEC = 60
RETRY_MAX = 3
#: См. шапку: не `'bot'`.
SENDER = "system"
#: Задача ARQ «в полёте»: стоит в очереди, отложена или выполняется.
#: ⚠ `complete` НЕ в наборе: `backfill_conversation` зарегистрирован БЕЗ
#: `keep_result=0`, результат лежит час и час отвечает `complete` — иначе замок
#: держал бы диалог час после конца подгрузки.
_В_ПОЛЁТЕ = frozenset({JobStatus.deferred, JobStatus.queued, JobStatus.in_progress})


def _skip(reason: str, *, conversation_id: str, message_id: str, attempt: int, **extra: Any) -> str:
    log.info(
        "address_ask.skipped",
        conversation_id=conversation_id,
        message_id=message_id,
        reason=reason,
        attempt=attempt,
        **extra,
    )
    return reason


async def _job_in_flight(redis: Redis, job_id: str) -> bool:
    """`arq.jobs.Job.status` читает `arq:result:`, `arq:in-progress:` и zscore
    очереди — не ключ `arq:job:`. Ошибка Redis — False с warning (как
    `runtime._идёт_подгрузка`: не смогли спросить — не блокируем)."""
    try:
        return await Job(job_id, redis).status() in _В_ПОЛЁТЕ
    except Exception as exc:  # noqa: BLE001
        log.warning("address_ask.job_status_failed", job_id=job_id, error=type(exc).__name__)
        return False


async def _история_едет(redis: Redis, conv: Conversation) -> bool:
    """Подгрузка аккаунта идёт, ИЛИ история этого чата (`convhist:{id}`) в полёте,
    ИЛИ её догон разбором (`cardcatch:{id}`, N29) — строки адреса появятся после него."""
    try:
        state = await get_backfill_state(redis, conv.account_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("address_ask.backfill_state_failed", error=type(exc).__name__)
        state = {}
    if (state or {}).get("status") == "running":
        return True
    if await _job_in_flight(redis, f"convhist:{conv.id}"):
        return True
    return await _job_in_flight(redis, catchup_job_id(conv.id))


async def _модель_читает(redis: Redis, message_ids: list[uuid.UUID]) -> bool:
    """`llm-addr:{id}` в полёте у одной из последних реплик. Завершённое
    чтение видно строкой `source='llm'` → `candidate_exists`."""
    for message_id in message_ids:
        if await _job_in_flight(redis, f"llm-addr:{message_id}"):
            return True
    return False


async def _голос_считается(redis: Redis, rows: Sequence[address_ask.InboundRow]) -> bool:
    """Расшифровка голосового из последних реплик или её разбор — в полёте
    (`voice:{id}`, `voice:{id}:repair`, `voicecard:{id}`; имена — из
    `services/voice`, не руками). Только ARQ, не колонка: NULL в базе — и «в
    очереди за досчётом», и «Whisper выключен», и «не дождалась замка» (досчёт
    возьмёт через ≤10 мин); `running` — и «считает», и «воркер умер, попытки
    кончились». Состояние очереди отличает их само, а `keep_result=0` у всех
    трёх задач отпускает замок сразу по завершении. На 600-й секунде
    `client_described` считает голосовое описанием — без этого замка вопрос
    ушёл бы тому, кто только что наговорил адрес (19.09)."""
    for r in rows[:LLM_CHECK_LIMIT]:
        if not voice.has_voice(r.attachments):
            continue
        for job_id in (*voice.transcribe_job_ids(r.id), voice.VOICE_CARD_JOB_ID.format(r.id)):
            if await _job_in_flight(redis, job_id):
                return True
    return False


async def _бот_войдёт(db: AsyncSession, conv: Conversation, account: AvitoAccount) -> bool:
    """`conv.bot_active` уже отсечён `field_lock`. Suggest-бот пишет заметки,
    клиенту не говорит — блокировать нельзя; auto-бот, у которого нет замка
    входа, войдёт следующим ходом (на 600-й секунде это бывает, когда его тик
    ждёт историю до 10 мин). `muted`/`handoff_done`/`not_scheduled` → False."""
    bot = await get_bot_for_conversation(db, conv, account=account)
    if bot is None or not bot.is_enabled or str(getattr(bot, "mode", "") or "") != "auto":
        return False
    return await bot_entry_block(db, conv, account=account, bot=bot) is None


def _записать_вопрос(db: AsyncSession, conv: Conversation, text: str, *, now: datetime) -> Message:
    """Строка `('out','system','pending')` + отметка «спросили». `awaiting_since`,
    `unread_count`, `bot_active`, `bot_vars`, `offered_at` — НЕ трогаем."""
    msg = Message(
        id=uuid.uuid4(),
        conversation_id=conv.id,
        external_message_id=None,
        direction="out",
        sender_type=SENDER,
        sender_user_id=None,
        body=text,
        attachments=[],
        delivery_status="pending",
        created_at=now,
    )
    db.add(msg)
    conv.address_asked_at = now
    conv.last_message_at = now
    conv.updated_at = now
    return msg


async def _после_commit(db: AsyncSession, redis: Redis, conv: Conversation, msg: Message) -> None:
    """Кадр → задача доставки, как в `runtime.flush_outbox`. Задача не встала —
    `mark_enqueue_failed` (для `system` кладёт `dismissed`, не `failed`)."""
    try:
        await publish_event(redis, "message:new", message_new_event(conv, msg))
    except Exception:  # noqa: BLE001 — кадр best effort, сообщение уже в базе
        log.exception("address_ask.publish_failed", message_id=str(msg.id))
    if not await messages_svc.enqueue_deliver(redis, msg.id):
        await messages_svc.mark_enqueue_failed(db, msg.id)


@with_job_scope
async def address_ask_run(
    ctx: dict[str, Any], *, conversation_id: str, message_id: str, attempt: int = 0
) -> str:
    """Решить, спрашивать ли адрес, и спросить. Итог — имя замка из ЗАМКИ,
    'retry' (переходный замок, задача переставила себя) или 'sent'."""
    factory, redis = ctx["db_session_factory"], ctx["redis"]
    try:
        attempt = int(attempt)
    except (TypeError, ValueError):
        attempt = 0
    conv_id, msg_id = uuid.UUID(conversation_id), uuid.UUID(message_id)

    def skip(reason: str, **extra: Any) -> str:
        return _skip(
            reason,
            conversation_id=conversation_id,
            message_id=message_id,
            attempt=attempt,
            **extra,
        )

    async def повтор(reason: str) -> str:
        if attempt >= RETRY_MAX:
            return skip(reason)
        встала = await address_ask.enqueue_address_ask(
            redis,
            conversation_id=conv_id,
            message_id=msg_id,
            defer_sec=RETRY_DEFER_SEC,
            attempt=attempt + 1,
        )
        if not встала:
            return skip(reason)
        log.info(
            "address_ask.retry",
            conversation_id=conversation_id,
            message_id=message_id,
            reason=reason,
            attempt=attempt + 1,
        )
        return "retry"

    async with factory() as db:
        async with db.begin():
            s = address_ask.settings_from(await app_settings.get_all(db))
            if not s.enabled:
                return skip("disabled")
            if not address_ask.text_is_recognizable(s.text):
                # Таблицу правили руками мимо ручки: ответ клиента не поднялся бы адресом.
                log.warning("address_ask.text_unrecognizable")
                return skip("text_unrecognizable")
            conv = (
                await db.execute(
                    select(Conversation).where(Conversation.id == conv_id).with_for_update()
                )
            ).scalar_one_or_none()
            if conv is None:
                return skip("no_conversation")
            # «Сейчас» — ПОСЛЕ захвата замка строки: под FOR UPDATE задача могла
            # простоять за соседней (бот, приём), и возраст реплики, посчитанный
            # до ожидания, был бы моложе настоящего — ложный `too_soon`.
            now = datetime.now(UTC)
            if замок := address_ask.field_lock(conv):
                return skip(замок)
            последнее = await address_ask.last_in_feed(db, conv.id, before=now)
            if последнее is None:
                return skip("no_messages")
            направление, когда = последнее
            if направление != "in":
                return skip("answered")
            возраст = now - когда
            задержка = timedelta(seconds=s.delay_sec)
            if возраст < задержка:
                return skip("too_soon")
            if возраст > задержка + MAX_AGE:
                return skip("stale")
            account = await db.get(AvitoAccount, conv.account_id)
            if account is None or account.status != "active":
                # То же, что `messages.assert_sendable`, без ApiError.
                return skip("account_inactive")
            if await _бот_войдёт(db, conv, account):
                return skip("bot_leads")
            if await _история_едет(redis, conv):
                # Постановка идёт из-под открытой транзакции, но это Redis, не
                # база, и в базе задача к этому моменту ничего не написала.
                return await повтор("history_loading")
            client = await db.get(Client, conv.client_id)
            if замок := address_ask.card_lock(client):
                return skip(замок)
            строки = await address_ask.address_rows(
                db, client_id=conv.client_id, conversation_id=conv.id, since=now - address_ask.ОКНО
            )
            замок_строк = address_ask.candidate_lock(строки)
            if замок_строк == address_ask.GEO_PENDING_LOCK:
                # Карта ещё не сказала — ждём, как историю; окончательный отказ
                # без степени сюда не попадает: это и есть повод спросить.
                return await повтор(замок_строк)
            if замок_строк is not None:
                return skip(замок_строк)
            входящие = await address_ask.inbound_rows(
                db, conv.id, since=now - address_ask.ОКНО, before=now
            )
            if await _модель_читает(redis, [r.id for r in входящие[:LLM_CHECK_LIMIT]]):
                return await повтор("llm_reading")
            if await _голос_считается(redis, входящие):
                # После `RETRY_MAX` — честный `skip("transcribing")`, как у
                # истории: лучше не спросить у того, чья расшифровка ещё в
                # работе, чем спросить у назвавшего адрес.
                return await повтор("transcribing")
            if await address_ask.asked_in_feed(db, conv.id, before=now):
                return skip("asked_in_feed")
            if address_ask.client_declined(входящие):
                return skip("declined")
            описал, знаков = address_ask.client_described(входящие, min_chars=s.min_chars)
            if not описал:
                # `chars=` — распределение за первый день и есть калибровка порога.
                return skip("not_described", chars=знаков)
            msg = _записать_вопрос(db, conv, s.text, now=now)
            await write_audit(
                db,
                user_id=None,
                action="conversation.address_asked",
                entity="conversation",
                entity_id=str(conv.id),
                details={"message_id": str(msg.id), "delay_sec": s.delay_sec, "chars": знаков},
            )
        await _после_commit(db, redis, conv, msg)
    log.info(
        "address_ask.sent",
        conversation_id=conversation_id,
        message_id=str(msg.id),
        attempt=attempt,
    )
    return "sent"
