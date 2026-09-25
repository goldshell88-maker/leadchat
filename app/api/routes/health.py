"""GET /api/health и GET /api/health/deep (01 §1.1, 05 §7.2).

Оба — без auth, оба всегда 200: состояние живёт в теле, а не в HTTP-коде.
Uptime-Kuma проверяет тело по ключевому слову, docker healthcheck — лёгкий
``/api/health``; «глубокий» вызывается раз в 5 минут и стоит нескольких
агрегатов.

Секретов в ответе нет по построению: только числа и булевы флаги — ни имён
аккаунтов, ни id, ни DSN (05 §7.2 «без деталей-имён; агрегаты не секретны»).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, get_redis
from app.core.config import settings
from app.db import session as db_mod
from app.models import AvitoAccount, Message, WebhookRawLog

router = APIRouter()
log = structlog.get_logger("app.health")

STREAM = "webhooks:avito"  # 08 §2.3 — тот же поток, что читает воркер
GROUP = "workers"
DLQ = "webhooks:avito:dlq"  # тот же литерал, что в app/workers/inbound.py
SCHEDULER_HEARTBEAT_KEY = "scheduler:alive"  # 08 §6, SM-7
TOKENS_EXPIRING_WINDOW = timedelta(hours=2)


@router.get("/api/health")
async def health(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    db_ok = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:  # degradation, not a 500
        db_ok = False
        log.warning("health.db_down", error=type(exc).__name__)

    redis_ok = True
    try:
        await redis.ping()
    except Exception as exc:
        redis_ok = False
        log.warning("health.redis_down", error=type(exc).__name__)

    return {
        "status": "ok" if db_ok and redis_ok else "degraded",
        "db": db_ok,
        "redis": redis_ok,
        "version": settings.version,
    }


# --------------------------------------------------------------------- deep


def _age_seconds(value: datetime | None, *, now: datetime | None = None) -> int | None:
    """Возраст отметки в секундах; naive-время считаем UTC (SQLite)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    delta = (now or datetime.now(UTC)) - value
    return max(0, int(delta.total_seconds()))


def _stream_id_age_seconds(entry_id: Any) -> int | None:
    """Возраст записи Streams по её id ``<ms>-<seq>``."""
    if entry_id is None:
        return None
    raw = entry_id.decode() if isinstance(entry_id, bytes) else str(entry_id)
    try:
        ms = int(raw.split("-", 1)[0])
    except (ValueError, AttributeError):
        return None
    return max(0, int(datetime.now(UTC).timestamp() - ms / 1000))


async def _queue_probe(redis: Redis) -> dict[str, Any]:
    """Отставание очереди + сводка XPENDING по группе воркеров (05 §7.2).

    ``len`` — это НЕ XLEN: Redis Streams ничего не удаляют сами, и длина стрима
    только растёт (за всё время существования сервиса), поэтому порог
    ``health_queue_len_red`` на ней срабатывал бы навсегда после первой тысячи
    вебхуков. Отставание = ``lag`` группы (записи, ещё не выданные консьюмерам)
    + ``pending`` (выданные, но не подтверждённые). Полную длину отдаём
    отдельным полем ``stream_len`` — она нужна при разборе инцидентов.

    ``dlq`` — брошенные вебхуки (``webhooks:avito:dlq``). До 11 августа в этот
    поток только писали: воркер после пятой неудачной доставки перекладывал
    запись туда и подтверждал её в основном потоке (``_reclaim_stuck``), а
    читателя у DLQ не было ни одного. То есть сообщение клиента исчезало из
    системы совсем, и единственным следом оставалась строка в журнале, которую
    никто не ищет. Здесь это число становится видимым: сколько обращений
    потеряно всего (``dlq``) и как давно потеряли в последний раз
    (``dlq_age_sec``).

    ПОЧЕМУ СЧЁТЧИК ЗДЕСЬ, А НЕ ОТДЕЛЬНАЯ ПРОВЕРКА В ``jobs/watchdog.py``.
    Потому что этого достаточно, чтобы новость дошла до человека, и по дороге
    ничего не ломается. Внешняя проверка со второго сервера
    (``deploy/healthcheck-alert.sh``, шаг 2) дёргает эту ручку, и на любом
    ответе кроме ``"status": "ok"`` кладёт в тревогу ТЕЛО ЦЕЛИКОМ — то есть
    ``queue.dlq`` и ``queue.dlq_age_sec`` приезжают в сообщение сами, без
    единой новой строки в скрипте. Сторож же живёт внутри планировщика: он не
    увидел бы потерю, если планировщик встал, — а встать он мог по той же
    причине, по которой вебхуки и не разобрались. Наблюдатель снаружи в этом
    случае честнее.
    """
    out: dict[str, Any] = {
        "len": 0,
        "pending": 0,
        "oldest_pending_sec": 0,
        "stream_len": 0,
        "dlq": 0,
        "dlq_age_sec": None,
    }
    try:
        out["stream_len"] = int(await redis.xlen(STREAM))
    except Exception as exc:  # потока ещё нет / redis лёг
        log.warning("health.queue_len_failed", error=type(exc).__name__)
    try:
        out["dlq"] = int(await redis.xlen(DLQ))
        if out["dlq"]:
            # Возраст берём из id последней записи: DLQ пишется XADD'ом без
            # явного id, поэтому его метка времени и есть момент потери.
            newest = await redis.xrevrange(DLQ, count=1)
            if newest:
                out["dlq_age_sec"] = _stream_id_age_seconds(newest[0][0])
    except Exception as exc:  # потока ещё нет (норма) / redis лёг
        log.warning("health.dlq_len_failed", error=type(exc).__name__)
    try:
        groups = await redis.xinfo_groups(STREAM)
        stream_info = await redis.xinfo_stream(STREAM)
    except Exception:
        groups, stream_info = [], {}
    for group in groups or []:
        name = group.get("name")
        if (name.decode(errors="replace") if isinstance(name, bytes) else str(name)) != GROUP:
            continue
        # entries-added минус entries-read — то же, что `lag` у Redis 7, но
        # считается одинаково на любом сервере (у эмуляторов `lag` бывает
        # приблизительным). Отрицательного значения быть не может: группа не
        # может прочитать больше, чем добавлено.
        added = stream_info.get("entries-added")
        read = group.get("entries-read")
        if added is not None:
            out["len"] = max(0, int(added) - int(read or 0))
        else:  # старый Redis без счётчиков — берём то, что есть
            out["len"] = int(group.get("lag") or 0)
        break
    try:
        summary = await redis.xpending(STREAM, GROUP)
    except Exception:
        return out  # NOGROUP — консьюмер-группа ещё не создана, это не авария
    if not summary:
        return out
    if not isinstance(summary, dict):
        return out
    out["pending"] = int(summary.get("pending") or 0)
    out["len"] += out["pending"]  # отставание = не выдано + выдано, но не подтверждено
    if out["pending"]:
        # `min` — самая старая запись в PEL: её id несёт время создания в мс
        out["oldest_pending_sec"] = _stream_id_age_seconds(summary.get("min")) or 0
    return out


async def _accounts_probe(db: AsyncSession) -> dict[str, int]:
    """Аккаунты Авито по статусам + токены, истекающие в ближайшие 2 часа."""
    rows = (
        await db.execute(select(AvitoAccount.status, func.count()).group_by(AvitoAccount.status))
    ).all()
    by_status = {str(status): int(count) for status, count in rows}
    expiring = (
        await db.execute(
            select(func.count())
            .select_from(AvitoAccount)
            .where(
                AvitoAccount.status == "active",
                AvitoAccount.token_expires_at < datetime.now(UTC) + TOKENS_EXPIRING_WINDOW,
            )
        )
    ).scalar_one()
    return {
        "active": by_status.get("active", 0),
        "needs_reauth": by_status.get("needs_reauth", 0),
        "disabled": by_status.get("disabled", 0),
        "tokens_expiring_2h": int(expiring),
    }


async def _delivery_probe(db: AsyncSession) -> dict[str, int]:
    """messages.delivery_status='failed' за последний час (05 §7.2)."""
    since = datetime.now(UTC) - timedelta(hours=1)
    failed = (
        await db.execute(
            select(func.count())
            .select_from(Message)
            .where(Message.delivery_status == "failed", Message.created_at >= since)
        )
    ).scalar_one()
    return {"failed_last_hour": int(failed)}


async def _webhooks_probe(db: AsyncSession) -> dict[str, int | None]:
    """Возраст последнего принятого вебхука (конвейер приёма жив?)."""
    last = (await db.execute(select(func.max(WebhookRawLog.received_at)))).scalar()
    return {"last_inbound_age_sec": _age_seconds(last)}


async def _scheduler_probe(redis: Redis) -> dict[str, Any]:
    """Heartbeat планировщика: ключ обновляется раз в 30 с, EX 120 (08 §6)."""
    raw = await redis.get(SCHEDULER_HEARTBEAT_KEY)
    if raw is None:
        return {"alive": False, "heartbeat_age_sec": None}
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        beat = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return {"alive": True, "heartbeat_age_sec": None}
    age = _age_seconds(beat)
    return {
        "alive": age is not None and age <= settings.scheduler_heartbeat_max_age_seconds,
        "heartbeat_age_sec": age,
    }


@router.get("/api/health/deep")
async def health_deep(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Глубокая проверка (05 §7.2): лаг очереди, токены, доставка, приём.

    Каждая проба изолирована: упавший датчик отдаёт ``null``/дефолт и метит
    ответ ``degraded``, но не роняет всю ручку — иначе мониторинг слепнет
    ровно в тот момент, когда он нужнее всего.
    """
    body: dict[str, Any] = {}
    failed: list[str] = []

    db_ok = True
    try:
        await db.execute(text("SELECT 1"))
    except Exception as exc:
        db_ok = False
        log.warning("health.deep.db_down", error=type(exc).__name__)

    redis_ok = True
    try:
        await redis.ping()
    except Exception as exc:
        redis_ok = False
        log.warning("health.deep.redis_down", error=type(exc).__name__)

    body["db"] = db_ok
    body["redis"] = redis_ok

    probes: list[tuple[str, Callable[[], Awaitable[dict[str, Any]]], dict[str, Any]]] = [
        (
            "queue",
            lambda: _queue_probe(redis),
            {
                "len": None,
                "pending": None,
                "oldest_pending_sec": None,
                "stream_len": None,
                "dlq": None,
                "dlq_age_sec": None,
            },
        ),
        (
            "accounts",
            lambda: _accounts_probe(db),
            {"active": None, "needs_reauth": None, "tokens_expiring_2h": None},
        ),
        ("delivery", lambda: _delivery_probe(db), {"failed_last_hour": None}),
        ("webhooks", lambda: _webhooks_probe(db), {"last_inbound_age_sec": None}),
        ("scheduler", lambda: _scheduler_probe(redis), {"alive": False, "heartbeat_age_sec": None}),
    ]
    for name, probe, fallback in probes:
        try:
            body[name] = await probe()
        except Exception as exc:
            failed.append(name)
            body[name] = fallback
            log.warning("health.deep.probe_failed", probe=name, error=type(exc).__name__)

    body["pool"] = db_mod.pool_status()

    red = _red_flags(body, db_ok=db_ok, redis_ok=redis_ok, failed=failed)
    # Ключ "status" идёт первым — Kuma сверяет тело по ключевому слову. `red` —
    # КАКИЕ правила покраснели: без него degraded в журнале проверки приходилось
    # восстанавливать пересчётом правил по числам (проверка 24.09).
    return {"status": "degraded" if red else "ok", "checks_failed": failed, "red": red, **body}


def _red_flags(
    body: dict[str, Any], *, db_ok: bool, redis_ok: bool, failed: list[str]
) -> list[str]:
    """Правила «красного» из 05 §7.2 (пороги вынесены в настройки)."""
    red: list[str] = []
    if not db_ok:
        red.append("db")
    if not redis_ok:
        red.append("redis")
    if failed:
        red.extend(failed)

    # ⚠ ПУЛ СОЕДИНЕНИЙ — ПРАВИЛО ПОЯВИЛОСЬ ПОСЛЕ БОЕВОГО ИНЦИДЕНТА 02.09.
    #
    # В тот день сторож писал «OK: health/deep зелёные» ровно в те минуты,
    # когда пул был выбран целиком: 166 ошибок «QueuePool limit reached», 1 100
    # брошенных запросов и 105 ПОТЕРЯННЫХ ВЕБХУКОВ Авито — сообщений живых
    # людей. Числа пула он при этом отдавал: `body["pool"]` был на месте, но
    # ни одно правило на них не смотрело.
    #
    # Зелёный сигнал означал не «всё хорошо», а «никто не смотрел туда, где
    # плохо». Это и чинится здесь.
    #
    # ⚠ ПОРОГ — ДОЛЯ, А НЕ ЧИСЛО. `pool_size` и `max_overflow` настраиваются, и
    # правило, зашитое в абсолютных единицах, разъехалось бы с настройкой молча.
    #
    # ⚠ ПОТОЛОК — ИЗ НАСТРОЕК, А НЕ ИЗ `pool.overflow()` (проверка 24.09). Тот
    # отдаёт ТЕКУЩЕЕ число соединений сверх пула: пока пул не наполнен, оно
    # отрицательное (−7 при трёх открытых). Потолок выходил «80 % от уже
    # открытых», и 8 занятых из 15 возможных давали ложное degraded — четыре
    # из шести тревог пула с 15.09.
    пул = body.get("pool") or {}
    потолок = settings.db_pool_size + settings.db_max_overflow
    занято = пул.get("checkedout") or 0
    if потолок > 0 and занято >= потолок * settings.health_pool_busy_red:
        red.append("pool.checkedout")

    queue = body.get("queue") or {}

    if (queue.get("len") or 0) > settings.health_queue_len_red:
        red.append("queue.len")
    if (queue.get("oldest_pending_sec") or 0) > settings.health_oldest_pending_sec_red:
        red.append("queue.oldest_pending_sec")
    # Потерянный вебхук — это несостоявшийся клиент, поэтому порога «сколько
    # потерь считать бедой» здесь нет: красное с первой же записи. Гасит его
    # только время (health_dlq_fresh_sec_red), и причина такого выбора описана
    # у самой настройки. Возраст неизвестен (id записи не разобрался) —
    # считаем потерю свежей: молчать в сомнительном случае здесь дороже.
    dlq_age = queue.get("dlq_age_sec")
    if (queue.get("dlq") or 0) > 0 and (
        dlq_age is None or dlq_age <= settings.health_dlq_fresh_sec_red
    ):
        red.append("queue.dlq")

    accounts = body.get("accounts") or {}
    if (accounts.get("needs_reauth") or 0) > 0:
        red.append("accounts.needs_reauth")

    delivery = body.get("delivery") or {}
    if (delivery.get("failed_last_hour") or 0) > settings.health_failed_last_hour_red:
        red.append("delivery.failed_last_hour")

    if settings.health_deep_check_scheduler and not (body.get("scheduler") or {}).get("alive"):
        red.append("scheduler")
    return red
