"""Задача «модель читает адрес» — второе мнение к правилам (владелец 13.09).

Кто ставит: `inbound` — когда правила ничего не нашли, а реплика похожа на
адрес (`address_llm.looks_like_address`); `workers/geocode` — когда правила
нашли, а карта отказала (улица есть, дома нет; улица не совпала; не найдено):
может, правила прочитали не то («11 улица Зеленогорская» из «часов 11 улица
Зеленогорская»). Строка модели — обычная строка-кандидат с `source='llm'`: её
так же проверяет карта, так же показывает карточка, так же автозапись кладёт
только `exact`.

⚠ ПОТОЛОК В СУТКИ считается по запросам к OpenRouter (и к следующей модели
после отказа первой), ключ `geo:llm:calls:<день>`; бесплатные модели
отдают немного запросов в минуту, поэтому в очередь задача встаёт с именем
по сообщению — один запрос на реплику, повторы не плодятся.
"""

from __future__ import annotations

import re
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis

from app.core.observability import with_job_scope
from app.integrations import gateway, openrouter
from app.models import Client, ClientAddressCandidate, Conversation, Message
from app.models.client import CANDIDATE_REJECTED, CANDIDATE_SOURCE_LLM
from app.services import address_llm, address_parse, app_settings, inbound, voice
from app.services.clients import record_address_candidate
from app.services.conversations import conversation_city
from app.services.geocode_queue import enqueue_geocode
from app.ws.events import publish_event

log = structlog.get_logger()
#: Реплики клиента за это окно уходят модели: адрес называют в одной-двух,
#: но пункт или город могли быть раньше («Новое Заозерье» → «Рябиновая 8»).
ОКНО = timedelta(hours=24)
#: Чтений на диалог в сутки: адрес в диалоге один, пять попыток — с запасом.
_ПОТОЛОК_ДИАЛОГА = 5


def llm_calls_key(day: datetime | None = None) -> str:
    return "geo:llm:calls:" + (day or datetime.now(UTC)).strftime("%Y%m%d")


async def llm_calls_today(redis: Redis) -> int:
    try:
        return int(await redis.get(llm_calls_key()) or 0)
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_address.counter_read_failed", error=type(exc).__name__)
        return 0


def _слова(текст: str | None) -> list[str]:
    return [w.lower() for w in re.findall(r"[0-9]+|[a-zа-яё]+", текст or "", re.IGNORECASE)]


def _то_же(row: ClientAddressCandidate, found: address_llm.Found) -> bool:
    """Строка с теми же словами улицы, домом, видом и не беднее пунктом —
    уже есть: «ул.Ленина, 5» и «ул Ленина, 5» одно и то же (ревью 13.09)."""
    if row.kind != found.kind:
        return False
    if row.kind == address_parse.KIND_HOUSE:
        то_же = address_parse.same_address(row.street, row.house, found.street, found.house)
    else:
        то_же = _слова(row.street) == _слова(found.street) and _слова(row.value) == _слова(
            found.value
        )
    return то_же and (not found.settlement or bool(row.settlement))


@with_job_scope
async def llm_address_read(
    ctx: dict[str, Any],
    *,
    conversation_id: str,
    message_id: str,
    candidate_id: str | None = None,
) -> str:
    """Прочитать адрес моделью и завести строку-кандидат.

    Итоги: `disabled` (нет ключа/выключено), `limit` (потолок), `nothing`
    (модель адреса не увидела или его нет в тексте), `same` (то же, что уже
    есть), `recorded` (строка заведена и ушла карте), `failed` (модели не
    ответили).
    """
    factory, redis = ctx["db_session_factory"], ctx["redis"]
    conv_id, msg_id = uuid.UUID(conversation_id), uuid.UUID(message_id)
    await gateway.refresh_status()  # ключи читателей знает только шлюз (16.09)
    async with factory() as db:
        # Главный выключатель «разбирать адрес» главнее переключателя модели:
        # выключили разбор — не читает никто (ревью 13.09).
        if (
            not openrouter.enabled()
            or not await app_settings.get(db, app_settings.ADDRESS_DETECT_ENABLED)
            or not await app_settings.get(db, app_settings.ADDRESS_LLM_ENABLED)
        ):
            return "disabled"
        потолок = await app_settings.get(db, app_settings.ADDRESS_LLM_DAILY_LIMIT)
        if потолок is not None and await llm_calls_today(redis) >= int(потолок):
            return "limit"
        # Политика правил разбора — один раз на задачу: сверка чтения модели с
        # правилами идёт под той же политикой, что разбор на живом пути.
        правила_разбора = await inbound.parse_rules(db)
        conv = await db.get(Conversation, conv_id)
        # Таблица сообщений секционирована по дате — `db.get` по одному id падает.
        msg = (
            await db.execute(sa.select(Message).where(Message.id == msg_id).limit(1))
        ).scalar_one_or_none()
        if conv is None or msg is None or msg.direction != "in":
            return "nothing"
        client = await db.get(Client, conv.client_id)
        if client is None:
            return "nothing"
        city = conversation_city(conv)
        когда = msg.created_at if msg.created_at.tzinfo else msg.created_at.replace(tzinfo=UTC)
        # Речь клиента, а не тело: у голосового тело пусто, текст лежит в
        # расшифровке. Без этого модель получала бы ПУСТУЮ реплику — и по нашей
        # постановке из разбора голосового, и при перечитывании после отказа
        # карты (`workers/geocode` ставит `llm_read` любой строке `source != llm`).
        # Условие одно на соседей и на саму реплику (`voice.speech_sql` /
        # `voice.speech_of`): разойдись они — контекст видел бы одно, а
        # читаемая реплика другое.
        реплики = list(
            (
                await db.execute(
                    sa.select(voice.speech_sql())
                    .where(
                        Message.conversation_id == conv_id,
                        Message.direction == "in",
                        Message.created_at <= msg.created_at,
                        Message.created_at >= когда - ОКНО,
                    )
                    .order_by(Message.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        тексты = [т for т in реплики if т]
        речь = inbound.client_speech(msg).text
        if речь and речь not in тексты:
            тексты.append(речь)
        # Что уже есть у клиента в этом диалоге — чтобы не заводить то же самое.
        свои = list(
            (
                await db.execute(
                    sa.select(ClientAddressCandidate).where(
                        ClientAddressCandidate.client_id == client.id,
                        ClientAddressCandidate.conversation_id == conv_id,
                        ClientAddressCandidate.status != CANDIDATE_REJECTED,
                    )
                )
            )
            .scalars()
            .all()
        )

    # Одна реплика читается один раз в сутки, один диалог — не больше
    # _ПОТОЛОК_ДИАЛОГА раз: повторная проверка карты той же строки и клиент,
    # пишущий «адрес» в каждой реплике, не выедают общий потолок (ревью 13.09).
    try:
        if not await redis.set(f"geo:llm:read:{msg_id}", 1, nx=True, ex=24 * 3600):
            return "same"
        ключ_диалога = f"geo:llm:conv:{conv_id}:{datetime.now(UTC):%Y%m%d}"
        await redis.set(ключ_диалога, 0, nx=True, ex=2 * 24 * 3600)
        if int(await redis.incr(ключ_диалога)) > _ПОТОЛОК_ДИАЛОГА:
            return "limit"
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_address.guard_failed", error=type(exc).__name__)
        return "failed"

    if any(r.source == CANDIDATE_SOURCE_LLM and r.message_id == msg_id for r in свои):
        # Эту реплику модель уже читала — второй запрос ничего не добавит.
        return "same"

    async def _посчитать() -> None:
        try:
            await redis.set(llm_calls_key(), 0, nx=True, ex=2 * 24 * 3600)
            await redis.incr(llm_calls_key())
        except Exception as exc:  # noqa: BLE001
            log.warning("llm_address.counter_failed", error=type(exc).__name__)

    try:
        данные, модель = await openrouter.chat_json(
            address_llm.SYSTEM_PROMPT,
            address_llm.build_user_message(тексты, city.name if city else None),
            on_request=_посчитать,
        )
    except openrouter.OpenRouterError as exc:
        log.warning("llm_address.failed", kind=exc.kind, status=exc.status)
        # Прочтения не было — замок и место в потолке диалога возвращаем
        # (проверка 24.09). Иначе одна сетевая ошибка или 429 у всех трёх
        # моделей разом лишала реплику чтения насовсем: замок живёт сутки, а
        # через сутки реплика уже старше окна, в которое модель её читает.
        try:
            await redis.delete(f"geo:llm:read:{msg_id}")
            await redis.decr(ключ_диалога)
        except Exception as release_exc:  # noqa: BLE001
            log.warning("llm_address.release_failed", error=type(release_exc).__name__)
        return "failed"
    чтение = address_llm.parse_reading(данные)
    found = (
        address_llm.to_found(чтение, тексты, rules=правила_разбора) if чтение is not None else None
    )
    log.info(
        "llm_address.read",
        conversation_id=conversation_id,
        model=модель,
        confidence=чтение.confidence if чтение else None,
        accepted=found is not None,
        after_candidate=candidate_id,
    )
    if found is None:
        return "nothing"
    if any(_то_же(r, found) for r in свои):
        return "same"
    async with factory() as db:
        client = await db.get(Client, conv.client_id)
        if client is None:
            return "nothing"
        записано = await record_address_candidate(
            db,
            client=client,
            conversation_id=conv_id,
            message_id=msg_id,
            message_at=msg.created_at,
            found=found,
            source=CANDIDATE_SOURCE_LLM,
            # Время реплики, как у строк правил: автозапись берёт только
            # строки моложе суток, и чтение старой истории не должно их
            # омолаживать (ревью 13.09).
            now=msg.created_at,
        )
        await db.commit()
    if записано.candidate_id is not None and записано.перепроверить:
        await enqueue_geocode(redis, записано.candidate_id)
    try:
        await publish_event(
            redis,
            "client:updated",
            {
                "client_id": str(client.id),
                "conversation_id": conversation_id,
                "reason": "address_suggested" if записано.впервые else "address_refined",
            },
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("llm_address.frame_not_published", error=type(exc).__name__)
    return "recorded"
