"""Inbound consumer over Redis Streams (08 §2.3–2.4).

Consumer group ``workers`` on stream ``webhooks:avito``; entries stuck in
PEL > 60 s are reclaimed via XAUTOCLAIM; ≥ 5 deliveries → DLQ
``webhooks:avito:dlq``. Raw payload lands in ``webhook_raw_log`` BEFORE
parsing (риск №2 DESIGN §7). Pipeline guarantee: at-least-once delivery +
idempotent insert = exactly-once по факту (INT-2/INT-8).

``parse_webhook`` comes from the OAuth zone's adapter
(app.integrations.avito.adapter — пишется параллельно); until it lands, a
local minimal parser of the Avito Messenger v3 webhook format keeps the
dev pipeline working.
"""

import asyncio
import contextlib
import json
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

import httpx
import structlog
from redis.asyncio import Redis
from sqlalchemy import select, update

from app.core import trace
from app.core.config import settings
from app.integrations.avito.adapter import AvitoAdapter
from app.integrations.avito.errors import WebhookParseError
from app.models import AvitoAccount, WebhookRawLog
from app.scheduler.partitions import PartitionCoverage
from app.services import app_settings, dialect
from app.services.inbound import apply_inbound_event

log = structlog.get_logger("app.workers.inbound")


#: Память «какие месяцы уже покрыты» на весь процесс воркера: тяжёлый лок
#: `CREATE TABLE ... PARTITION OF` берётся один раз на месяц, а не на сообщение.
_PARTITIONS = PartitionCoverage()

STREAM = "webhooks:avito"
GROUP = "workers"
DLQ = "webhooks:avito:dlq"
#
# ЧТО ЛЕЖИТ В DLQ И КТО ЭТО ЧИТАЕТ.
#
# Сюда попадает вебхук, который не удалось обработать пять раз подряд
# (`_reclaim_stuck` ниже). Это НЕ мусор и не отладочный слепок: это входящее
# сообщение живого клиента, которое до диспетчера не дошло.
#
# «И УЖЕ НЕ ДОЙДЁТ» — так здесь было написано, и это перестало быть правдой
# 12 августа: `replay_dlq` ниже возвращает такие записи в обработку, и то, что
# легло сюда из-за временной беды (база была недоступна, Авито отвечал 500,
# воркер перезапускали), доезжает до диспетчеров само.
#
# До 11 августа в поток только писали. Читателя не было ни одного (проверено
# grep'ом по app/, deploy/ и frontend/), `/health` смотрел лишь основной поток,
# а единственным следом потери оставалась строка `webhook.moved_to_dlq` в
# журнале — то есть обращения копились невидимо, и узнать о них можно было
# только вручную сделав XLEN. Теперь длину и возраст последней записи отдаёт
# `GET /health/deep` полями `queue.dlq` и `queue.dlq_age_sec`
# (`app/api/routes/health.py`, константа `DLQ` там — тот же литерал).
#
# ПОЧЕМУ У ПОТОКА НЕТ `maxlen`. Обрезка по длине выглядит аккуратной уборкой, а
# на деле это молчаливое удаление самых старых потерянных обращений — ровно то,
# что мы здесь и пытаемся перестать делать. Поток растёт медленно (пять
# неудачных доставок подряд — редкость), и пока он читается глазами через
# `/deep`, честнее хранить всё.
MAX_DELIVERIES = 5  # после пятой доставки — DLQ (runbook 05 §8.3)
CLAIM_IDLE_MS = 60_000  # pending старше минуты считаем зависшим
READ_COUNT, READ_BLOCK_MS = 10, 5_000

# --- разбор отстойника (FUNC-53) ---------------------------------------------
#
# ВИДЕТЬ ЧИСЛО И РАЗОБРАТЬ НАКОПЛЕННОЕ — РАЗНЫЕ ВЕЩИ, И ВТОРОГО НЕ БЫЛО.
#
# 11 августа длину DLQ показали в `/health/deep`, и это закрыло ровно половину
# беды: потери перестали быть невидимыми. Читателя у потока по-прежнему не было
# ни одного — сообщения живых клиентов лежали в Redis навсегда, а единственным
# способом их достать оставался человек с `redis-cli` и знанием формата записи.
# То есть система умела сказать «потеряно 14 обращений» и не умела ничего с
# ними сделать.
#
# ПОЧЕМУ ПОВТОР САМ, А НЕ КНОПКА. Подавляющее большинство записей попадает сюда
# по ВРЕМЕННОЙ причине: база была недоступна пять минут, Авито отвечал 500,
# воркер перезапускали. Через полчаса те же самые записи обрабатываются с
# первого раза. Ждать от человека нажатия ради этого — значит терять обращения
# по выходным. Обработка идемпотентна (вставка по `external_message_id`),
# поэтому повтор безопасен: дубль сообщения он не создаст.
#
# ПОЧЕМУ ПОПЫТКИ СЧИТАНЫ. Есть и второй род записей — те, что не обработаются
# никогда (диалог ссылается на удалённый аккаунт, поле не лезет в колонку).
# Гонять их по кругу вечно — это ровный поток ошибок в журнале, в котором
# перестают замечать настоящие. После :data:`DLQ_MAX_REPLAYS` запись
# откладывается: она остаётся в потоке (мы ничего не удаляем молча) и попадает
# в отчёт как «разобрать руками».
#
# ПОПЫТКИ ЖИВУТ ОТДЕЛЬНО ОТ ЗАПИСИ. Запись потока неизменяема — дописать в неё
# счётчик нельзя, а переложить запись в новую значило бы менять её
# идентификатор при каждой попытке и потерять связь с журналом.
DLQ_TRIES = "webhooks:avito:dlq:tries"  # hash: id записи -> сколько раз повторяли
DLQ_MAX_REPLAYS = 3
DLQ_REPLAY_BATCH = 50  # ПОПЫТОК за заход; остальное подождёт следующего
#: Сколько записей за заход разрешено ПРОСМОТРЕТЬ. Отложенные попыток не
#: тратят, но лежат в начале потока и читаются каждый раз: без потолка заход
#: перебирал бы всю накопленную свалку ради пятидесяти живых записей.
DLQ_SCAN_LIMIT = 500
DLQ_REPLAY_EVERY_SEC = 300
#: Разбирает ОДИН воркер за раз. Реплики делят поток группой, но у DLQ группы
#: нет: без лока каждая реплика тащила бы те же записи и они обрабатывались бы
#: по числу реплик. Идемпотентность спасёт от дублей, но не от лишней работы.
DLQ_REPLAY_LOCK = "lock:dlq:replay"
DLQ_TRIES_TTL = 7 * 24 * 3600


# ЗДЕСЬ ЖИЛ ВТОРОЙ `WebhookParseError` — свой класс с тем же именем, что и
# канонический в `app/integrations/avito/errors.py`. Снят 23.08. Он был не просто
# лишним: `parse_webhook` (адаптер) кидает КАНОНИЧЕСКИЙ, а ловил `except` ниже
# ЛОКАЛЬНЫЙ — разбирало это только потому, что канонический наследует
# `ValueError`, который в том же кортеже. Один случайный шаг (сменить базовый
# класс на Exception) — и воркер падал бы на каждом кривом вебхуке вместо ack.


@dataclass
class FallbackInboundEvent:
    """Minimal normalized event mirroring the adapter zone's InboundEvent
    (app.integrations.avito.adapter) — used only if that module is absent."""

    external_chat_id: str
    external_message_id: str
    author_id: int
    text: str | None
    created_at: datetime
    client_name: str | None = None
    item_title: str | None = None
    item_url: str | None = None
    item_price: str | None = None
    attachments: list[Any] = field(default_factory=list)
    kind: str = "message"

    @property
    def message_id(self) -> str:
        return self.external_message_id

    @property
    def chat_id(self) -> str:
        return self.external_chat_id


# ЗДЕСЬ ЖИЛ `_fallback_parse_webhook` — вторая, урезанная копия контракта Авито,
# и `try/except ImportError` вокруг настоящего адаптера. Написано это было, когда
# зона адаптера ещё не существовала («once it is present»). Она существует давно,
# и запасной разборщик с тех пор не выполнялся ни разу — зато держал в репозитории
# второе описание формата вебхука, которое никто не обновлял вместе с первым.
#
# `FallbackInboundEvent` рядом ОСТАВЛЕН намеренно: он живой — им пользуется
# `tests/unit/test_phone_from_text.py` как лёгкой заглушкой события, и на него
# ссылается докстринг `services/inbound`.

parse_webhook = AvitoAdapter.parse_webhook


def _consumer_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


async def _ensure_group(redis) -> None:
    """Idempotent group creation. id='0' — не терять записи, добавленные до группы."""
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:  # noqa: BLE001 — redis error strings, not classes
        if "BUSYGROUP" not in str(e):
            raise


async def inbound_consumer_loop(ctx: dict) -> None:
    """Background task of the ARQ process (08 §2.3). One per process; worker
    replicas form the consumer group and share the stream automatically."""
    redis = ctx["redis"]
    await _ensure_group(redis)
    me = _consumer_name()
    # Первый разбор отстойника — не сразу при старте, а через обычный
    # промежуток: перезапуск воркера часто идёт вместе с той самой поломкой,
    # из-за которой записи туда и попали (база поднимается, миграция едет), и
    # повтор в эту секунду сжёг бы попытки впустую.
    dlq_last_run = time.monotonic()
    while not ctx["shutdown"].is_set():
        try:
            await _reclaim_stuck(ctx, me)  # XAUTOCLAIM + DLQ
            dlq_last_run = await _maybe_replay_dlq(ctx, dlq_last_run)  # FUNC-53
            resp = await redis.xreadgroup(
                GROUP, me, {STREAM: ">"}, count=READ_COUNT, block=READ_BLOCK_MS
            )
            for _stream, entries in resp or []:
                for entry_id, fields in entries:
                    await _handle_entry(ctx, entry_id, fields)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("inbound.loop_error")  # Sentry later; не даём циклу умереть
            await asyncio.sleep(1)


async def remove_consumer(redis: Redis) -> None:
    """Убрать своё имя из группы потока. Зовётся ИЗ `shutdown`, не из цикла.

    ⚠ ЗАЧЕМ ВООБЩЕ. Имя потребителя — это `<hostname>:<pid>`, а контейнер
    пересоздаётся на каждой выкатке, 10-20 раз в сутки. Записи о прежних
    потребителях в группе не исчезают сами: на 03.09 их накопилось 162 при
    одном живом, простой старейшего 7,6 суток. Каждую потом просматривает
    `XAUTOCLAIM`.

    ⚠ ПОЧЕМУ НЕ В КОНЦЕ ЦИКЛА, ГДЕ ЭТОТ КОД СТОЯЛ СНАЧАЛА. Не работало ни
    разу. `shutdown` сперва поднимает флаг, но тут же ДЕЛАЕТ `cancel()`, а
    цикл в этот момент висит на `xreadgroup(block=...)`: отмена прилетает
    внутрь ожидания, `except CancelledError: raise` выпускает её наружу — и
    всё, что написано ПОСЛЕ `while`, не исполняется. В бою счётчик после
    выкатки вырос со 162 до 163 вместо того, чтобы уменьшиться. Здесь же
    отмена уже позади, соединение с Redis ещё живо, и делать уборку можно
    спокойно.

    ⚠ УДАЛЯЕМ ТОЛЬКО ПУСТОГО. `XGROUP DELCONSUMER` уносит вместе с именем его
    PEL — обращения, взятые в работу и не подтверждённые. Их после этого не
    перехватит никто: `XAUTOCLAIM` ищет ИМЕННО в чужих PEL. Потерянное
    обращение Авито — потерянный клиент, и цена ошибки несимметрична: лишнее
    имя в группе стоит микросекунды.

    ⚠ ОСТАТОК СПРАШИВАЕМ У `XINFO CONSUMERS`, А НЕ У `XPENDING`. Здесь стоял
    разбор ответа `xpending` как списка КОРТЕЖЕЙ — redis-py отдаёт список
    СЛОВАРЕЙ. Обращение по индексу роняло KeyError, его глотал `except`, и
    уборка молча не делалась. Хуже: тест на сохранение непустого потребителя
    от этого зеленел — имя оставалось, но не потому, что сработал замок, а
    потому, что до замка не доходило.
    """
    me = _consumer_name()
    try:
        свои = [c for c in await redis.xinfo_consumers(STREAM, GROUP) if c.get("name") == me]
        мои = int(свои[0].get("pending") or 0) if свои else 0
        if not свои:
            return
        if мои == 0:
            await redis.xgroup_delconsumer(STREAM, GROUP, me)
            log.info("inbound.consumer_removed", consumer=me)
        else:
            log.warning("inbound.consumer_kept", consumer=me, pending=мои)
    except Exception:
        # Уборка не имеет права мешать остановке: не вышло — останется лишнее
        # имя, и это ровно та беда, которую мы чиним, а не новая.
        log.warning("inbound.consumer_cleanup_failed", consumer=me, exc_info=True)


async def _handle_entry(ctx: dict, entry_id: str, fields: dict) -> None:
    """Processing error => NO ack: entry stays in PEL and is replayed by
    _reclaim_stuck (after 5 deliveries — DLQ). Success or conscious skip => ack."""
    try:
        await process_webhook_entry(ctx, entry_id, fields)
    except Exception:
        log.exception("webhook.process_failed", stream_id=entry_id)
        return
    await ctx["redis"].xack(STREAM, GROUP, entry_id)


async def replay_dlq(ctx: dict) -> tuple[int, int]:
    """Разобрать отстойник: вернуть в работу то, что теперь обрабатывается.

    Возвращает ``(поднято, отложено)``: сколько обращений всё-таки дошло до
    диспетчеров и сколько признано неразбираемыми (см. :data:`DLQ_MAX_REPLAYS`).

    ЗАПИСЬ УДАЛЯЕТСЯ ТОЛЬКО ПОСЛЕ УСПЕХА, и порядок здесь не переставить:
    сперва обработка, потом `XDEL`. Обратный порядок в момент падения воркера
    терял бы обращение окончательно и молча — ровно то, ради чего DLQ и
    заводили.

    ИДЕНТИФИКАТОР БЕРЁМ ИСХОДНЫЙ (`orig_id`), а не идентификатор записи в
    отстойнике. По нему в `webhook_raw_log` лежит строка с сырым телом, и
    повтор обязан дописать результат В НЕЁ, а не завести вторую запись про то
    же сообщение клиента: иначе разбор «что нам прислали» показывал бы одно
    сообщение дважды с разными пометками.

    ЧИТАЕМ СТРАНИЦАМИ И СЧИТАЕМ ПОПЫТКИ, А НЕ ПРОСМОТРЕННЫЕ ЗАПИСИ. Отложенные
    (`DLQ_MAX_REPLAYS` исчерпан) из потока не удаляются и остаются в его
    НАЧАЛЕ — они самые старые. Возьми мы просто первые пятьдесят записей,
    десяток неразбираемых навсегда закрыл бы собой всё, что легло позже:
    сегодняшнее обращение, попавшее в отстойник из-за пятиминутного простоя
    базы, не разобралось бы никогда — при том что оно-то как раз поднимается.
    """
    redis = ctx["redis"]
    recovered = parked = 0
    attempted = scanned = 0
    start = "-"

    while attempted < DLQ_REPLAY_BATCH and scanned < DLQ_SCAN_LIMIT:
        entries = await redis.xrange(DLQ, min=start, count=DLQ_REPLAY_BATCH)
        if not entries:
            break
        for entry_id, fields in entries:
            scanned += 1
            tries = int(await redis.hget(DLQ_TRIES, entry_id) or 0)
            if tries >= DLQ_MAX_REPLAYS:
                parked += 1
                continue
            if attempted >= DLQ_REPLAY_BATCH:
                break
            attempted += 1
            orig_id = fields.get("orig_id") or entry_id
            try:
                await process_webhook_entry(ctx, orig_id, fields)
            except Exception:
                tries = await redis.hincrby(DLQ_TRIES, entry_id, 1)
                await redis.expire(DLQ_TRIES, DLQ_TRIES_TTL)
                if tries >= DLQ_MAX_REPLAYS:
                    parked += 1
                    # error => алерт: дальше это обращение не разберётся само,
                    # и смотреть на него придётся человеку. Идентификатор в
                    # строке — то, по чему запись находят в потоке.
                    log.error("webhook.dlq_needs_hands", stream_id=orig_id, dlq_id=entry_id)
                else:
                    log.warning("webhook.dlq_replay_failed", stream_id=orig_id, tries=tries)
                continue
            await redis.xdel(DLQ, entry_id)
            await redis.hdel(DLQ_TRIES, entry_id)
            recovered += 1
            log.info("webhook.dlq_recovered", stream_id=orig_id, dlq_id=entry_id)
        start = _after(entries[-1][0])

    if recovered or parked:
        log.info("webhook.dlq_replayed", recovered=recovered, parked=parked, scanned=scanned)
    return recovered, parked


def _after(entry_id: str) -> str:
    """Следующий возможный идентификатор потока — начало следующей страницы.

    Через соседний номер, а не через исключающий диапазон `(id`: тот появился
    только в Redis 6.2, и зависеть от версии сервера ради одного символа
    незачем. Порядковый номер в паре `<мс>-<номер>` целый и растёт на единицу,
    поэтому «следующий за ним» вычисляется точно, а не приблизительно.
    """
    ms, _, seq = str(entry_id).partition("-")
    return f"{ms}-{int(seq) + 1}" if seq else entry_id


async def _maybe_replay_dlq(ctx: dict, last_run: float) -> float:
    """Раз в :data:`DLQ_REPLAY_EVERY_SEC` — разбор отстойника. Возвращает время захода.

    Под локом и с проглатыванием ошибки: разбор старых потерь не имеет права
    ни задваиваться на репликах, ни останавливать приём СЕГОДНЯШНИХ сообщений.
    """
    now = time.monotonic()
    if now - last_run < DLQ_REPLAY_EVERY_SEC:
        return last_run
    redis = ctx["redis"]
    # Лок с запасом по времени: заход по 50 записей укладывается в секунды, а
    # умерший держатель не должен запирать разбор до конца света.
    if not await redis.set(DLQ_REPLAY_LOCK, "1", nx=True, ex=DLQ_REPLAY_EVERY_SEC):
        return now
    try:
        await replay_dlq(ctx)
    except Exception:
        log.exception("webhook.dlq_replay_error")
    finally:
        await redis.delete(DLQ_REPLAY_LOCK)
    return now


async def _reclaim_stuck(ctx: dict, me: str) -> None:
    """Recover stuck pending entries (воркер умер между XREADGROUP и XACK —
    INT-8). Delivery counters come from XPENDING (XAUTOCLAIM их не отдаёт)."""
    redis = ctx["redis"]
    pending = await redis.xpending_range(
        STREAM, GROUP, min="-", max="+", count=200, idle=CLAIM_IDLE_MS
    )
    if not pending:
        return
    counts = {p["message_id"]: p["times_delivered"] for p in pending}
    start = "0-0"
    while True:
        start, entries, *_ = await redis.xautoclaim(
            STREAM, GROUP, me, min_idle_time=CLAIM_IDLE_MS, start_id=start, count=50
        )
        if not entries:
            break
        for entry_id, entry_fields in entries:
            if counts.get(entry_id, 0) >= MAX_DELIVERIES:  # poison message
                await redis.xadd(
                    DLQ,
                    {**entry_fields, "orig_id": entry_id, "failed_at": str(int(time.time()))},
                )
                await redis.xack(STREAM, GROUP, entry_id)
                log.error("webhook.moved_to_dlq", stream_id=entry_id)  # error => алерт
            else:
                await _handle_entry(ctx, entry_id, entry_fields)


async def _mark_raw(
    ctx: dict, stream_id: str, *, processed: bool = False, error: str | None = None
) -> None:
    """Отметка в сыром журнале — своей короткой транзакцией, best-effort.

    ⚠ ПРОГЛОТИТЬ ОШИБКУ ЗДЕСЬ МОЖНО, ПРОМОЛЧАТЬ О НЕЙ — НЕЛЬЗЯ (разбор 03.09).
    Раньше всё тело стояло под `contextlib.suppress(Exception)`: ни строки в
    журнале, ни следа. А колонка `error`, которую пишет ЕДИНСТВЕННО эта
    функция, — то самое, по чему считает тревогу сторож «Авито поменял
    формат» (`scheduler/jobs/watchdog.py::check_inbound_unparsed`: `count(*)
    WHERE error IS NOT NULL`).

    Получался заслон, который снимается ровно теми условиями, ради которых
    заведён: база заикнулась — отметка не легла — счётчик остался нулевым —
    сторож доложил, что всё хорошо. Молча.

    Само проглатывание остаётся: отметка в журнале не имеет права уронить
    разбор обращения клиента. Уходит только молчание.
    """
    factory = ctx["db_session_factory"]
    try:
        async with factory() as db, db.begin():
            await db.execute(
                update(WebhookRawLog)
                .where(WebhookRawLog.stream_id == stream_id)
                .values(processed=processed, error=error)
            )
    except Exception:
        # error, а не warning: пока эта строка не легла, сторож разбора слеп.
        log.error(
            "webhook.raw_mark_failed",
            stream_id=stream_id,
            processed=processed,
            had_error=error is not None,
            exc_info=True,
        )


async def process_webhook_entry(ctx: dict, entry_id: str, fields: dict) -> None:
    """One stream entry. Uncontrolled exception => redelivery (см. _handle_entry);
    parse errors are controlled: raw payload сохранён, entry ack'ается."""
    factory, redis = ctx["db_session_factory"], ctx["redis"]
    account_id = UUID(fields["account_id"])
    raw_payload = fields["payload"]

    # 1. Raw payload into webhook_raw_log BEFORE parsing (риск №2 DESIGN §7);
    #    отдельная короткая транзакция, дедуп повторных доставок по stream_id.
    try:
        payload = json.loads(raw_payload)
        payload_for_log = payload
    except ValueError:
        payload, payload_for_log = None, {"_raw": str(raw_payload)}
    # ⚠ ОДНО ЧТЕНИЕ НАСТРОЕК НА ВСЮ ЗАПИСЬ СТРИМА (29.08).
    #
    # За настройками на этом пути ходят четыре независимых слоя: подробный след
    # (ниже), разбор телефона, автораздача и сборка строки очереди «Входящие».
    # Ни один про другие не знает, и до сегодня каждый читал `app_settings` сам
    # — четыре обращения к базе на КАЖДОЕ сообщение клиента.
    #
    # Граница снимка — сессия этой записи: дольше он не живёт и жить не должен,
    # это данные, прочитанные через `db`. Следующее сообщение читает настройки
    # заново, поэтому «выключил — выключилось немедленно» остаётся правдой
    # (`app.services.app_settings.one_pass`).
    async with factory() as db, app_settings.one_pass():
        async with db.begin():
            insert = dialect.insert(db)
            await db.execute(
                insert(WebhookRawLog)
                .values(account_id=account_id, stream_id=entry_id, payload=payload_for_log)
                .on_conflict_do_nothing(index_elements=["stream_id"])
            )
            account = (
                await db.execute(select(AvitoAccount).where(AvitoAccount.id == account_id))
            ).scalar_one_or_none()
            # СРОК ПОДРОБНОГО СЛЕДА — ВНУТРИ ЭТОЙ ТРАНЗАКЦИИ, А НЕ ПЕРЕД
            # `apply_inbound_event`, И ЭТО НЕ МЕЛОЧЬ.
            #
            # Чтение настройки открывает транзакцию неявно, а `apply_inbound_event`
            # начинает свою (`async with db.begin()`) — и падает с «A transaction
            # is already begun». То есть КАЖДОЕ входящее сообщение переставало бы
            # доезжать. Ровно так уже падал тик бота (см. тот же довод в
            # `app/bots/runtime.py`), и ровно так это чуть не повторилось здесь:
            # поймано на стенде, до боевого.
            #
            # Половина пути живёт в этом процессе, и без обновления она молчала
            # бы при включённом следе. Это чтение — ЕДИНСТВЕННОЕ обращение за
            # настройками на всю запись: снимок прохода отдаёт его же и разбору
            # телефона, и автораздаче, и строке очереди (см. `one_pass` выше).
            await trace.refresh(db)

        if payload is None:
            await _mark_raw(ctx, entry_id, error="invalid_json")
            return
        if account is None or account.status == "disabled":
            await _mark_raw(ctx, entry_id, error="account_missing_or_disabled")
            return  # smoke-заглушка / отключённый аккаунт: ack без обработки (07 SM-8)

        try:
            event = parse_webhook(payload)
        except (WebhookParseError, ValueError, KeyError, TypeError) as exc:
            # контролируемый исход (07 §1.1.2): сырец в логе, entry ack'ается
            log.warning("webhook.parse_failed", stream_id=entry_id, error=str(exc))
            await _mark_raw(ctx, entry_id, error=f"parse: {exc}")
            return
        if event is None or getattr(event, "kind", "message") != "message":
            await _mark_raw(ctx, entry_id, processed=True)  # не-«сообщение» — игнор (01 §10)
            return

        # ⚠ ПАРТИЦИЯ ПОД ДАТУ СОБЫТИЯ — И НА ЖИВОМ ПУТИ ТОЖЕ (18.08).
        # `messages` секционирована по `created_at`, окно партиций — 24 месяца
        # назад и месяц вперёд. Загрузка истории и сверка страхуются
        # PartitionCoverage, а вебхук — нет: событие со старой меткой (архивный
        # чат, переигранное событие, сбитые часы на той стороне) роняло вставку
        # с «no partition found», запись не ack'алась, пять доставок подряд
        # бились об одну ошибку и уходили в DLQ — сообщение клиента терялось
        # молча. Память объекта делает повтор бесплатным (лок берётся раз на
        # месяц), поэтому цена страховки — ноль запросов в обычном потоке.
        await _PARTITIONS.ensure(getattr(event, "created_at", None))
        inserted = await apply_inbound_event(db, redis, account, event)

    await _mark_raw(ctx, entry_id, processed=True)
    log.info(
        "webhook.processed",  # контракт логов 05 §7.3
        account_id=str(account_id),
        external_message_id=str(getattr(event, "message_id", None)),
        duplicate=not inserted,
        latency_ms=int(time.time() * 1000) - int(entry_id.split("-")[0]),
    )
    await ping_canary(redis)  # 05 §7.2


# ЗДЕСЬ ЖИЛ `_raw_log_insert` — посимвольная копия `services.dialect.insert`.
# Снят 23.08. Шапка `dialect.py` заведена ровно затем, чтобы выбор диалекта жил
# в одном месте, и прямо называет эту копию: «до сегодня выбор был скопирован в
# приём входящих». Копия пережила свою же отмену.


async def ping_canary(redis) -> None:
    """Uptime-Kuma push canary: не чаще раза в минуту, fire-and-forget (05 §7.2)."""
    if not settings.kuma_webhook_canary_url:
        return
    if await redis.set("canary:webhook", "1", nx=True, ex=60):
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient(timeout=3) as http:
                await http.get(settings.kuma_webhook_canary_url)
