"""Разбор карточки по расшифровке голосового (19.09).

КОГДА. `workers/transcribe.transcribe_voice` записала расшифровку ГОТОВО и
закоммитила её (`_записать_исход` → True) — и строго после этого поставила
задачу `voice_card_extract` под именем `voicecard:{message_id}`
(`voice.enqueue_voice_card`). Повтор расшифровки после ГОТОВО (фаза 1)
перепоставляет её же: воркер мог умереть между commit'ом и постановкой.

ЗАЧЕМ ОТДЕЛЬНАЯ ЗАДАЧА, А НЕ ХВОСТ РАСШИФРОВКИ. Упади разбор внутри
`transcribe_voice`, ARQ повторил бы ЕЁ, а фаза 1 вышла бы на `done`:
расшифровка есть, номер в карточку не попал бы никогда. У своей задачи свои
попытки, своя область Sentry и своё имя (дедупликация постановки).

ЧТО. Не своя копия разбора, а обёртка догона `cards_catchup.replay_conversation`
(тот же путь, что живой приём и историческая дверь N29): хвост диалога от
голосового до конца переписки (`before=None`, как у N29). ХВОСТ, А НЕ ОДНА
РЕПЛИКА: пока запись считалась, клиент мог дописать «кв 7» — живой путь тогда
строки не нашёл и потерял часть; а часть, уже дописанная текстом ПОЗЖЕ
голосового, без хвоста была бы перебита старым «квартира 3»
(`refine_address_parts` сторожа времени не имеет). ДО КОНЦА, А НЕ СУТКИ: у
старого голосового (досчёт `voice_repair` до 30 дней) потолок `когда + 24 ч`
возвращал бы «кв 7» из окна, но не «кв 9», дописанное через двое суток, —
поздняя часть откатывалась бы к старой и в строке, и в автоадресе карточки
(ревью 19.09, C1); у свежего оба хвоста совпадают — позже него реплик ещё нет.
«Позднее побеждает» держится одной хронологией. Что живой путь ставил бы
задачами, догон отдаёт в `CatchupResult.geocode_ids/llm_reads`
(`со_следствиями=True` — копилка открыта только здесь); ставить ли,
решается здесь: у СВЕЖЕГО голосового (`СВЕЖЕЕ`) — карта и модель сразу, как у
текста; старому — семантика догона: карту делает `geo_repair` в своём темпе.
Объединение двойников отсюда не ставится: пару с основным из голоса найдёт
ночной `merge_backlog` под своими сторожами. Кадр `client:updated` — один на
диалог и только живому, как у догона.

⚠ ПРОСТЫЕ ЗНАЧЕНИЯ СНИМАЮТСЯ ДО ХВОСТА, `msg` ПОСЛЕ НЕ ТРОГАЕТСЯ. Сбой любой
реплики внутри `replay_conversation` → rollback + `expunge_all`: `msg` протух и
отвязан, чтение `msg.conversation_id` упало бы DetachedInstanceError ПОСЛЕ уже
закоммиченных строк — кадр и задачи потерялись бы, а повтор упал бы там же
(скептик 19.09, возр. 3).

Все постановки и кадр — после выхода из `async with factory()`: строки уже в
базе, модель Whisper к этому моменту выгружена, замок расшифровки отпущен.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from arq import Retry
from redis.asyncio import Redis
from sqlalchemy.exc import InterfaceError, OperationalError

from app.core.observability import with_job_scope
from app.models import Conversation
from app.services import app_settings, inbound, voice
from app.services.geocode_queue import enqueue_autofill, enqueue_geocode, enqueue_llm_read
from app.workers.cards_catchup import _aware, _конечная_карточка, replay_conversation

log = structlog.get_logger("app.workers.voice_card")

#: «Живое» голосовое: расшифровано в пределах суток от отправки. Старше —
#: история (досчёт `voice_repair` до 30 дней): модель и карта сразу не
#: ставятся — карту делает `geo_repair` в своём темпе, как у `backfill`.
СВЕЖЕЕ = timedelta(hours=24)
#: Сбой соединения с базой — повтор задачи через полминуты (регистрация
#: `max_tries=3` в `workers/main.py`); разбор детерминирован, третья попытка
#: упадёт там же и оставит след в Sentry. Сбой ВНУТРИ хвоста догон не глотает
#: (`replay_conversation` отдаёт `OperationalError`/`InterfaceError` наверх) —
#: иначе разбор самого голосового терялся бы под «done» (ревью 19.09, C2).
RETRY_DEFER_SEC = 30


@with_job_scope
async def voice_card_extract(
    ctx: dict[str, Any], message_id: uuid.UUID, created_at: datetime
) -> str:
    """ARQ-задача. `created_at` едет ради одной партиции (как у расшифровки)."""
    factory = ctx["db_session_factory"]
    redis: Redis = ctx["redis"]
    try:
        async with factory() as db, app_settings.one_pass():
            msg = await voice.load_message(db, message_id, created_at)
            if msg is None or msg.direction != "in" or msg.sender_type != "client":
                return "nothing"
            речь = inbound.client_speech(msg)
            if not речь.spoken:
                # Тело есть — его разобрал живой путь; или расшифровки нет
                # (`failed`/`too_long`/NULL) — разбирать нечего.
                return "no_speech"
            conversation_id = msg.conversation_id
            когда = _aware(msg.created_at)
            итог = await replay_conversation(db, conversation_id, since=когда, со_следствиями=True)
            if итог is None:
                return "gone"
            conv = await db.get(Conversation, conversation_id)
            client = await _конечная_карточка(db, conv.client_id) if conv is not None else None
    except (OperationalError, InterfaceError) as exc:
        log.warning(
            "voice_card.db_unavailable",
            message_id=str(message_id),
            error=type(exc).__name__,
        )
        raise Retry(defer=RETRY_DEFER_SEC) from exc
    свежее = datetime.now(UTC) - когда <= СВЕЖЕЕ
    # --- строго после commit'а и вне сессии -----------------------------------
    if свежее:
        for cid in итог.geocode_ids:
            await enqueue_geocode(redis, cid)
        for mid in итог.llm_reads:
            await enqueue_llm_read(redis, conversation_id=conversation_id, message_id=mid)
    if итог.geopoint_ready:
        # Как у догона: строка `exact` от точки Авито — одна автозапись на диалог.
        await enqueue_autofill(redis, conversation_id)
    if итог.reason is not None and итог.live and conv is not None and client is not None:
        await inbound._известить_о_клиенте(redis, conv, client, reason=итог.reason)
    # Поля названы `tail_*` намеренно: `with_phone` считает каждую реплику хвоста
    # с номером, а не «в голосовом был номер». Критерий «номер из голоса
    # поднялся» — строка `client_phone_candidates.message_id = <id голосового>`.
    log.info(
        "voice_card.done",
        message_id=str(message_id),
        conversation_id=str(conversation_id),
        tail_messages=итог.messages,
        tail_phones=итог.with_phone,
        tail_addresses=итог.with_address,
        reason=итог.reason,
        fresh=свежее,
        geocode=len(итог.geocode_ids),
        llm=len(итог.llm_reads),
    )
    return "done" if итог.messages else "empty"
