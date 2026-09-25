"""Догон карточки по прожитой переписке одного диалога (N29, 19.09).

КОГДА. `avito_accounts._backfill_chat` вставил реплики клиента (импорт канала,
кнопка «Загрузить историю», `backfill_conversation` после первого дошедшего
вебхука) и после commit'а поставил эту задачу с `since` = время самой ранней из
них в окне `cards_catchup.ОКНО_ДОГОНА`.

ЧТО. Все входящие клиента диалога с `created_at >= since` — хронологически, тем же
путём, что живой приём (`inbound.replay_card_extraction`), с `now` = время каждой
реплики. ВЕСЬ ХВОСТ, А НЕ ТОЛЬКО ВСТАВЛЕННОЕ: правила зависят от контекста — «кв 7»
ищет строку, заведённую раньше; строчная «садовая 12» — вопрос оператора за сутки;
пункт — соседние реплики. Живая реплика, уже разобранная приёмом, при повторе даёт
то же самое (ключ строки ON CONFLICT, части того же сообщения, условный UPDATE
основного номера).

ЧЕГО НЕ ДЕЛАЕТ. Не ставит проверку по карте (строки `pending` берёт починка
`scheduler/jobs/geo_repair` — новые вперёд, DaData за вычетом запаса живым),
чтение моделью, вопрос клиенту, объединение двойников; не публикует кадр на
каждое сообщение — один `client:updated` на диалог, и только живому
(`status != closed`): в закрытый архив импорта смотреть некому, а оператору
открытого диалога телефон иначе виден только после F5 (жалоба 02.09).
Что живой путь поставил бы задачами (строки на карту, чтение моделью),
`replay_conversation` лишь СОБИРАЕТ в `CatchupResult.geocode_ids/llm_reads`
(19.09) — и только по просьбе вызывающего (`со_следствиями=True`): для разбора
голосового (`workers/voice_card`), где у свежей реплики карта и модель обещаны
сразу. Задача истории их не читает, поэтому и не считает: ворота модели — это
запрос к ленте на каждую реплику (ревью 19.09, D1).
Автозапись ставит В ОДНОМ СЛУЧАЕ — геоточка Авито с координатами: её строка
рождается `exact`, починка карты такие не берёт, и без постановки отсюда
карточка оставалась бы пустой (ревью 19.09). Одна `addr-fill:{conv}` на диалог;
годность и давность решает сама `autofill_address`.

ИЗОЛЯЦИЯ. Одна реплика — одна транзакция: сбой разбора одной не уносит остальные
и не держит замки на строках клиента дольше миллисекунд (живой приём пишет в те
же строки). После отката объекты сессии протухают, и ленивое чтение падает
MissingGreenlet (cli backfill-cards, 19.09) — диалог и карточка перечитываются.
Изолируется СБОЙ РАЗБОРА, не сбой соединения: `OperationalError`/`InterfaceError`
уходят наверх (ревью 19.09, C2) — следующая реплика упала бы там же, а
`message_failed` на полхвоста и «done» сверху выглядели бы как «разобрано».
Задача голоса на них просит Retry; задача истории (`max_tries=1`) падает со
следом в Sentry, и историю добирает `backfill-cards`.
После каждого commit'а карточка и диалог перечитываются тоже (`db.refresh`):
сессия `expire_on_commit=False`, и без этого решения по следующей реплике шли бы
по снимку до чужих записей — оператора, автозаписи, объединения (ревью 19.09).
Повторов ARQ нет (`max_tries=1`): разбор детерминирован, второй заход упал бы там
же; итог — в журнале `cards_catchup.done`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import with_job_scope
from app.models import Client, Conversation, Message
from app.services import app_settings, inbound
from app.services.conversations import AVITO_SYSTEM_PREFIX
from app.services.geocode_queue import enqueue_autofill

log = structlog.get_logger("app.workers.cards_catchup")

#: Порядок причин кадра по силе: как `clients._ПРИЧИНЫ` у телефона, дальше адрес.
_ПРИЧИНЫ = (
    "phone_captured",
    "phone_extra_added",
    "phone_suggested",
    "address_suggested",
    "address_refined",
)


@dataclass(frozen=True, slots=True)
class CatchupResult:
    """Итог догона одного диалога — для журнала, кадра и автозаписи по геоточке."""

    messages: int
    with_phone: int
    with_address: int
    reason: str | None
    live: bool
    geopoint_ready: bool
    #: Что живой путь поставил бы задачами после commit'а (по репликам хвоста):
    #: строки на карту и реплики на чтение моделью. Копятся только при
    #: `со_следствиями=True` — у разбора голосового (`workers/voice_card`);
    #: задача истории их не ставит и не считает (шапка) — у неё здесь пусто.
    geocode_ids: tuple[uuid.UUID, ...] = ()
    llm_reads: tuple[uuid.UUID, ...] = ()


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _сильнее(текущая: str | None, новая: str | None) -> str | None:
    if новая is None or новая not in _ПРИЧИНЫ:
        return текущая
    if текущая is None or _ПРИЧИНЫ.index(новая) < _ПРИЧИНЫ.index(текущая):
        return новая
    return текущая


async def _конечная_карточка(db: AsyncSession, client_id: uuid.UUID) -> Client | None:
    """Карточка диалога, а если её объединили — победитель: до конечной по цепочке
    `merged_into_id`, с тем же потолком глубины, что у `inbound._upsert_client`."""
    client = await db.get(Client, client_id)
    for _ in range(5):
        if client is None or client.merged_into_id is None:
            break
        следующая = await db.get(Client, client.merged_into_id)
        if следующая is None or следующая.id == client.id:
            break
        client = следующая
    return client


async def replay_conversation(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    since: datetime,
    before: datetime | None = None,
    со_следствиями: bool = False,
) -> CatchupResult | None:
    """Хвост диалога от `since` тем же путём, что живой приём. ``None`` — диалога или
    карточки нет. Коммит после каждой реплики (см. шапку). ``со_следствиями`` —
    копить `geocode_ids/llm_reads` (только если вызывающий их поставит)."""
    conv = await db.get(Conversation, conversation_id)
    if conv is None:
        return None
    client = await _конечная_карточка(db, conv.client_id)
    if client is None:
        return None
    client_id = client.id
    условия = [
        Message.conversation_id == conversation_id,
        Message.direction == "in",
        Message.sender_type == "client",
        Message.created_at >= since,
        # Геоточка Авито приходит без тела — только вложением.
        sa.or_(Message.body.is_not(None), Message.attachments != []),
    ]
    if before is not None:
        условия.append(Message.created_at <= before)
    # Сначала только ключи в хронологии, объекты — по одному: откат после сбоя гасит
    # всё загруженное в сессии (cli backfill-cards, 19.09).
    #
    # ⚠ ПАРА (id, created_at), А НЕ ОДИН id. У `messages` составной первичный ключ:
    # таблица секционирована по дате, и `db.get(Message, id)` падает
    # InvalidRequestError — бой 18.08 (предупреждения в services/messages.py и
    # routes/messages.py; страховка, написанная позже, снова взяла `get`). Дата
    # нужна и планировщику: без неё — обход всех секций.
    пары = (
        await db.execute(
            select(Message.id, Message.created_at)
            .where(*условия)
            .order_by(Message.created_at.asc(), Message.id)
        )
    ).all()
    messages = with_phone = with_address = 0
    reason: str | None = None
    geopoint_ready = False
    geocode_ids: list[uuid.UUID] = []
    llm_reads: list[uuid.UUID] = []
    for mid, at in пары:
        try:
            msg = await db.get(Message, (mid, at))
            # Служебную запись Авито живой путь уводит в отдельную ветку до разбора
            # (`apply_inbound_event`, первая проверка) — здесь тот же порог.
            if msg is None or (msg.body or "").startswith(AVITO_SYSTEM_PREFIX):
                continue
            when = _aware(msg.created_at)
            if со_следствиями:
                # Полная форма: те же телефон → адрес, плюс следствия (карта,
                # модель) для вызывающего; счётчики ниже — по трёхполевой `.card`.
                полный = await inbound.replay_card_extraction_full(db, conv, client, msg, now=when)
            else:
                # Копилку не открываем: ворота модели — запрос к ленте, а читать
                # их некому. Пустые следствия в той же обёртке, чтобы ниже был
                # один путь.
                полный = inbound.CardReplayFull(
                    card=await inbound.replay_card_extraction(db, conv, client, msg, now=when),
                    geocode_ids=(),
                    llm_read_wanted=False,
                )
            итог = полный.card
            await db.commit()
            # Снимок карточки и диалога живёт ОДНУ транзакцию (ревью 19.09). Фабрика
            # сессий — `expire_on_commit=False`, а догон — десятки commit'ов на диалог:
            # между двумя репликами оператор вписывает адрес руками, автозапись кладёт
            # строку в поле, ночной обход объединяет карточки. По протухшему объекту
            # `refresh_auto_address` перезаписал бы текст оператора (в снимке ещё
            # `address_set_at is None`), а квартира из «кв 7» не дошла бы до карточки
            # (в снимке ещё `address is None`). Две выборки по первичному ключу на реплику.
            await db.refresh(client)
            await db.refresh(conv)
            if client.merged_into_id is not None:
                # Карточку объединили на ходу — дальше пишем в победителя, как живой
                # путь (`inbound._upsert_client` идёт по той же цепочке).
                client = await _конечная_карточка(db, client.id) or client
                client_id = client.id
        except (OperationalError, InterfaceError):
            # Сбой соединения — не сбой разбора одной реплики (шапка, «Изоляция»):
            # следующая реплика шла бы по тому же соединению и упала бы там же, а
            # вызывающий получил бы «done» без строк. Задача голоса на это просит
            # Retry; `message_failed` здесь не пишем — это не изъян разбора.
            raise
        except Exception:
            await db.rollback()
            log.exception(
                "cards_catchup.message_failed",
                conversation_id=str(conversation_id),
                message_id=str(mid),
            )
            db.expunge_all()
            conv = await db.get(Conversation, conversation_id)
            client = await db.get(Client, client_id)
            if conv is None or client is None:
                break
            continue
        messages += 1
        with_phone += int(итог.phone_reason is not None)
        with_address += int(итог.address_reason is not None)
        geopoint_ready = geopoint_ready or итог.geopoint_ready
        reason = _сильнее(_сильнее(reason, итог.phone_reason), итог.address_reason)
        geocode_ids.extend(полный.geocode_ids)
        if полный.llm_read_wanted:
            llm_reads.append(mid)
    return CatchupResult(
        messages=messages,
        with_phone=with_phone,
        with_address=with_address,
        reason=reason,
        live=conv is not None and conv.status != "closed",
        geopoint_ready=geopoint_ready,
        geocode_ids=tuple(geocode_ids),
        llm_reads=tuple(llm_reads),
    )


@with_job_scope
async def catchup_conversation_cards(
    ctx: dict[str, Any], conversation_id: uuid.UUID, since: str
) -> str:
    """ARQ-задача. `since` — ISO-время (строкой: аргументы задачи — простые значения)."""
    factory = ctx["db_session_factory"]
    redis: Redis = ctx["redis"]
    нижняя = _aware(datetime.fromisoformat(since))
    async with factory() as db, app_settings.one_pass():
        итог = await replay_conversation(db, conversation_id, since=нижняя)
        if итог is None:
            log.info("cards_catchup.gone", conversation_id=str(conversation_id))
            return "gone"
        conv = await db.get(Conversation, conversation_id)
        client = await _конечная_карточка(db, conv.client_id) if conv is not None else None
    log.info(
        "cards_catchup.done",
        conversation_id=str(conversation_id),
        since=since,
        messages=итог.messages,
        phones=итог.with_phone,
        addresses=итог.with_address,
        reason=итог.reason,
        geopoint=итог.geopoint_ready,
    )
    if итог.geopoint_ready:
        # Строка `exact` от точки Авито: карта к ней не придёт — автозапись ставим
        # сами, одну на диалог (дедуп `addr-fill:{conv}`); годность решит воркер.
        await enqueue_autofill(redis, conversation_id)
    if итог.reason is not None and итог.live and conv is not None and client is not None:
        # Один кадр на диалог и только живому — единственный писатель кадра карточки
        # остаётся в `inbound` (прецедент чтения приватного имени: `cli.run_backfill_cards`).
        await inbound._известить_о_клиенте(redis, conv, client, reason=итог.reason)
    return "done" if итог.messages else "empty"
