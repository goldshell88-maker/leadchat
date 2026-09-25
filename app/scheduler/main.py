"""Scheduler process — ``python -m app.scheduler.main`` (08 §6).

Jobs (все coalesce=True, max_instances=1):
- heartbeat: SET scheduler:alive EX 120 каждые 30 с (healthcheck 05 §2.2);
- partitions: месячные партиции messages — при старте (КРИТИЧНО) и ежесуточно;
- token_refresh: каждые 30 мин, refresh_due_accounts из OAuth-зоны
  (пока модуль не готов — warning, процесс живёт);
- reconcile: каждые RECONCILE_INTERVAL_SECONDS — enqueue ARQ-задачи по
  активным аккаунтам (dedup через _job_id внутри тика);
- raw_log_cleanup: webhook_raw_log старше 30 дней — ежесуточно;
- retention_cleanup: audit_log старше года, message_idempotency старше
  месяца — ежесуточно;
- watchdog_fast / watchdog_daily: внешний сторож (14 §2.1) — раз в 5 минут и
  раз в сутки в 06:15 UTC (09:15 МСК);
- notifications_cleanup: уведомления с истёкшим TTL — ежесуточно;
- bot_phantom: раз в полчаса — диалоги, где движок насчитал реплику бота,
  которой в переписке нет (инвариант `bot_msgs_row`);
- bot_deadlines: раз в 5 минут — потерянные дедлайны `ask`/`menu`
  ставятся заново, через час диалог уходит людям;
- address_funnel_weekly / address_rule_policy_weekly: по понедельникам —
  воронка адресов и лестница политик правил адреса (пакет 6.0а).

Автозакрытия диалогов по таймауту НЕТ (решение владельца №2).
"""

import asyncio
import signal
from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.events import EVENT_JOB_ERROR, EVENT_JOB_MISSED, JobExecutionEvent
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from arq.connections import ArqRedis, RedisSettings, create_pool
from sqlalchemy import select, text

from app.core import redis as redis_mod
from app.core.config import settings
from app.core.logging import configure_logging
from app.core.observability import init_sentry
from app.db import session as db_mod
from app.integrations.avito import client as avito_client
from app.models import AvitoAccount
from app.scheduler import catch_up
from app.scheduler.jobs import address_funnel as address_funnel_jobs
from app.scheduler.jobs import address_judge as address_judge_jobs
from app.scheduler.jobs import address_own_addresses as address_own_jobs
from app.scheduler.jobs import awaiting as awaiting_jobs
from app.scheduler.jobs import bot_deadlines as bot_deadlines_jobs
from app.scheduler.jobs import bot_phantom as bot_phantom_jobs
from app.scheduler.jobs import bot_stuck as bot_stuck_jobs
from app.scheduler.jobs import geo_repair as geo_repair_jobs
from app.scheduler.jobs import merge_backlog as merge_backlog_jobs
from app.scheduler.jobs import reclaim as reclaim_jobs
from app.scheduler.jobs import rule_policy_weekly as rule_policy_jobs
from app.scheduler.jobs import stats as stats_jobs
from app.scheduler.jobs import voice_repair as voice_repair_jobs
from app.scheduler.jobs import watchdog as watchdog_jobs
from app.scheduler.partitions import ensure_message_partitions

log = structlog.get_logger("app.scheduler")

DEFAULTS = {"coalesce": True, "max_instances": 1, "misfire_grace_time": 300}

arq_pool: ArqRedis | None = None


async def heartbeat() -> None:
    """Контракт healthcheck (05 §2.2): ключ обновляется каждые 30 с, EX 120 —
    два пропущенных прогона подряд валят docker-healthcheck (SM-7)."""
    redis = redis_mod.get_client()
    await redis.set("scheduler:alive", datetime.now(UTC).isoformat(), ex=120)


async def token_refresh() -> None:
    """Refresh аккаунтов с истекающими токенами (DESIGN §1.5/§8.1).
    Модуль пишется параллельно в OAuth-зоне — до его появления warning."""
    try:
        from app.services.avito_accounts import refresh_due_accounts
    except ImportError:
        log.warning("scheduler.token_refresh_unavailable", reason="avito_accounts not ready")
        return
    try:
        async with db_mod.session_scope() as db:
            await refresh_due_accounts(db, redis_mod.get_client())
    except Exception:
        log.exception("scheduler.token_refresh_failed")


#: Шаг разноса сверок по времени внутри одного интервала (замер 06.09).
#:
#: Тридцать четыре канала при шаге 8 с растягиваются на ~270 с из 300 — весь
#: интервал занят ровно, а одновременно идёт не больше пяти сверок. Шире шаг —
#: хвост уедет в следующий интервал (безопасно: имя задачи несёт номер
#: интервала, сама сверка идемпотентна — но оценка «не больше пяти» перестанет
#: быть верной); уже — вернётся залп.
RECONCILE_STAGGER_STEP = timedelta(seconds=8)


async def enqueue_reconcile_all() -> None:
    """По ARQ-задаче на каждый активный аккаунт; _job_id — дедуп внутри одного
    тика (08 §4.1). i-я задача ставится с отсрочкой i × RECONCILE_STAGGER_STEP.

    ⚠ НЕ ЗАЛПОМ (замер боя 06.09, журнал воркера за 12–24 ч). Все 34 сверки
    вставали в очередь одной пачкой раз в 300 с, и первые 60 с после
    `scheduler.reconcile_enqueued` воркер разбирал их вперёд живой работы: путь
    «вебхук → publish message:new» шёл p50 116 мс / p99 510 (n=417) против
    p50 25 / p99 78 вне порыва. Входящее доходило до экрана в 4,6 раза дольше
    18 % суток, доставка ответов стартовала вдвое позже. Отсрочка задаётся
    очереди (`_defer_by`), а не ожиданием в самой задаче: ARQ хранит момент
    старта счётом в `arq:queue` и до него задачу не трогает.

    ⚠ ПОЧЕМУ НЕ СЕМАФОР ВНУТРИ `reconcile_account`. Ожидание семафора шло бы
    внутри `job_timeout=300` (ARQ оборачивает задачу в `wait_for`): хвост
    очереди ждал бы своей очереди дольше, чем ему отпущено на работу, и падал
    бы по таймауту, ничего не сверив.

    В `_job_id` обязан входить номер интервала. ARQ отказывает в постановке не
    только пока job в очереди, но и пока в Redis лежит его *результат*
    (`keep_result`, по умолчанию час). С постоянным `reconcile:{account_id}`
    это превращало «раз в 5 минут» в «раз в час»: 54 постановки давали 5
    прогонов, и потерянные вебхуками сообщения догонялись через ~65 минут
    вместо ≤ 5 (07 §5 сценарий 34). Сама задача идемпотентна, а `job_timeout`
    равен интервалу — пересечение прогонов безопасно.
    """
    assert arq_pool is not None
    async with db_mod.session_scope() as db:
        ids = (
            (
                await db.execute(
                    select(AvitoAccount.id).where(
                        AvitoAccount.status == "active",
                        # ЗАГЛУШКА В СВЕРКУ НЕ ХОДИТ.
                        #
                        # НАЙДЕНО В ЖУРНАЛЕ ПРОДА 12 августа: 832 строки ошибок
                        # за двое суток, и все — служебный канал SMOKE-ACCOUNT.
                        # Его одноразовый refresh-токен давно сгорел, сверка
                        # падает трассировкой каждые несколько минут и будет
                        # падать вечно.
                        #
                        # Шум не безобиден: ровно так на этой системе уже
                        # хоронили настоящие отказы — 112 ложных тревог
                        # закрыли собой два реальных провала резервной копии, и
                        # заметили их только неделю спустя.
                        #
                        # ПРОВЕРЯТЬ НА ЗАГЛУШКЕ ВСЁ РАВНО НЕЧЕГО. Боевые каналы
                        # обновляют доступ СВОИМИ ключами (`client_credentials`,
                        # см. `avito_accounts.refresh_tokens`), а заглушка —
                        # одноразовым refresh-токеном: это разные пути в коде.
                        # Сверка на ней проверяла ветку, которой в бою нет.
                        #
                        # Сторож уже исключает служебные каналы этим же
                        # признаком (`watchdog._real_channels_condition`) — и
                        # по той же причине.
                        AvitoAccount.is_service.is_(False),
                    )
                )
            )
            .scalars()
            .all()
        )
    bucket = int(datetime.now(UTC).timestamp()) // settings.reconcile_interval_seconds
    for i, account_id in enumerate(ids):
        await arq_pool.enqueue_job(
            "reconcile_account",
            account_id,
            _job_id=f"reconcile:{account_id}:{bucket}",
            # Первая — сразу: нулевую отсрочку ARQ считает её отсутствием.
            _defer_by=i * RECONCILE_STAGGER_STEP,
        )
    if ids:
        log.info("scheduler.reconcile_enqueued", accounts=len(ids))


#: Сколько каналов сторож догрузки трогает за один тик.
#:
#: Не тридцать: заходы загрузки идут в том же воркере, что доставка исходящих и
#: сверка. Запусти мы их все разом, история заняла бы весь пул и ответы клиентам
#: встали бы в очередь за прошлогодней перепиской. Три канала за тик — это
#: полная догрузка всех тридцати меньше чем за час при пятиминутном тике, и при
#: этом две трети пула всегда свободны для живой работы.
BACKFILL_SUPERVISE_BATCH = 3

#: Насколько устаревшим должен быть ход работы, чтобы счесть заход умершим.
#:
#: Живой заход обновляет ключ после каждого чата, а сам длится не дольше
#: `BACKFILL_SLICE_SECONDS` (240 с). Берём двойной запас плюс две минуты:
#: перезапустить работающую загрузку не страшно — множество разобранных чатов
#: общее, — но лишний обход списка стоит запросов к Авито.
BACKFILL_STALE_AFTER = timedelta(seconds=600)


async def enqueue_backfill_supervise() -> None:
    """ДОГРУЗКА ИСТОРИИ ИДЁТ САМА — МАССОВО И ПОСТОЯННО (просьба владельца 28.08).

    ⚠ ЧТО БЫЛО. Загрузку запускал ТОЛЬКО человек: при подключении канала или
    руками через `load-history`. Ничто её не возобновляло — ни после срыва, ни
    после того, как воркер убил заход по таймауту (см. `BACKFILL_SLICE_SECONDS`
    в services/avito_accounts.py).

    Замер боя 28.08 по тридцати активным каналам: десять показывали «сорвалась»
    на ~290 из 1100 чатов, двадцать — «не идёт», то есть не начинали ни разу.
    Жалоба владельца дословно: «плохо работает сгрузка диалогов… сделай её
    массовой и постоянной».

    КОГО ТРОГАЕМ. Канал активный, не служебный, история целиком не загружена
    (`history_loaded_at` пуст) — и при этом прямо сейчас не грузится: либо хода
    работы нет вовсе, либо он застыл.

    КОГО НЕ ТРОГАЕМ, И ЭТО ВАЖНЕЕ:

    * канал, у которого человек нажал «Остановить». Просьба остановиться —
      решение человека, и сторож, отменяющий его каждые пять минут, хуже
      отсутствия сторожа: кнопка перестаёт работать, а почему — не видно;
    * канал, где заход идёт прямо сейчас: второй заход ничего не сломает
      (множество разобранных чатов общее), но зря сходит за списком чатов;
    * канал в фазе «census»: перепись длинная и своего хода не двигает, так что
      по времени обновления она неотличима от застывшей загрузки. Ждём.
    """
    assert arq_pool is not None
    try:
        from app.services.avito_accounts import (
            DEFAULT_HISTORY_DEPTH,
            _parse_progress,
            _progress_key,
            _stop_key,
        )
    except ImportError:
        log.warning("scheduler.backfill_supervise_unavailable")
        return

    redis = redis_mod.get_client()
    async with db_mod.session_scope() as db:
        rows = (
            (
                await db.execute(
                    select(AvitoAccount.id).where(
                        AvitoAccount.status == "active",
                        # Заглушка истории не имеет — тот же довод, что у сверки.
                        AvitoAccount.is_service.is_(False),
                        AvitoAccount.history_loaded_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )

    now = datetime.now(UTC)
    поставлено = 0
    for account_id in rows:
        if поставлено >= BACKFILL_SUPERVISE_BATCH:
            break
        if await redis.get(_stop_key(account_id)) is not None:
            continue  # человек нажал «Остановить» — его решение старше нашего
        состояние = _parse_progress(await redis.get(_progress_key(account_id)))
        if состояние is not None and _заход_живой(состояние, now):
            continue
        # `_job_id` уникален намеренно: ARQ держит ключ выполненной задачи час
        # (`keep_result`) и постоянный идентификатор молча отбросил бы — сторож
        # работал бы вхолостую ровно так же тихо, как вставала загрузка.
        await arq_pool.enqueue_job(
            "backfill_account",
            account_id,
            DEFAULT_HISTORY_DEPTH,
            _job_id=f"backfill:{account_id}:{int(now.timestamp())}",
        )
        поставлено += 1
    if поставлено:
        log.info("scheduler.backfill_supervised", enqueued=поставлено, unfinished=len(rows))


def _заход_живой(состояние: dict, now: datetime) -> bool:
    """Идёт ли загрузка прямо сейчас — по фазе и свежести хода работы.

    Отдельной функцией, чтобы решение «трогать или нет» проверялось тестом без
    поднятия планировщика, очереди и базы.
    """
    if состояние.get("phase") == "census":
        return True  # перепись длинная и своего хода не двигает — ждём
    отметка = состояние.get("updated_at")
    if not isinstance(отметка, str):
        return False  # хода нет — считаем заход умершим, поднимаем заново
    try:
        момент = datetime.fromisoformat(отметка)
    except ValueError:
        return False
    if момент.tzinfo is None:
        момент = момент.replace(tzinfo=UTC)
    return now - момент < BACKFILL_STALE_AFTER


async def cleanup_webhook_raw_log() -> None:
    """Retention сырых payload'ов — 30 дней (08 §2.6, DESIGN §7 риск №2)."""
    # component="scheduler": потолок запроса в 15 с задуман только для веб-
    # процесса, а подпись в pg_stat_activity — единственный способ понять,
    # чьи соединения висят (разбор 03.09, см. app/workers/main.py).
    engine = db_mod.init_engine(component="scheduler")
    async with engine.begin() as conn:
        res = await conn.execute(
            text(
                "DELETE FROM webhook_raw_log WHERE received_at < now() - make_interval(days => :d)"
            ),
            {"d": settings.webhook_raw_log_ttl_days},
        )
    log.info("cleanup.webhook_raw_log", deleted=res.rowcount)


async def cleanup_retention() -> None:
    """Срок хранения журналов, которые иначе росли бы вечно (владелец 12.09:
    «умная очистка, чтобы не занимало много места»).

    `audit_log` — год (AUDIT_LOG_TTL_DAYS), `message_idempotency` — месяц
    (MESSAGE_IDEMPOTENCY_TTL_DAYS). Сырые вебхуки и уведомления убирают свои
    задания. Переписка (`messages`) не убирается никогда — это и есть данные.
    """
    from sqlalchemy import delete

    from app.models import AuditLog, MessageIdempotency

    now = datetime.now(UTC)
    итог: dict[str, int] = {}
    try:
        async with db_mod.transaction() as db:
            for имя, модель, дней in (
                ("audit_log", AuditLog, settings.audit_log_ttl_days),
                ("message_idempotency", MessageIdempotency, settings.message_idempotency_ttl_days),
            ):
                res = await db.execute(
                    delete(модель).where(модель.created_at < now - timedelta(days=дней))
                )
                итог[имя] = getattr(res, "rowcount", 0) or 0
    except Exception:
        log.exception("cleanup.retention_failed")
        return
    log.info("cleanup.retention_done", **итог)


async def cleanup_expired_notifications() -> None:
    """Retention центра уведомлений — 90 дней (14 §4, DEFAULT_TTL_DAYS).

    Без этой уборки таблица растёт вечно: на выдачу просроченное не влияет
    (его скрывает предикат живости), но место оно занимает. Отметки прочтения
    уезжают каскадом по внешнему ключу.
    """
    from app.services.notifications import cleanup_expired

    try:
        # transaction(), а не session_scope(): DELETE обязан быть закоммичен,
        # иначе уборка каждую ночь честно считает строки и каждую ночь их
        # откатывает.
        async with db_mod.transaction() as db:
            deleted = await cleanup_expired(db)
    except Exception:
        log.exception("cleanup.notifications_failed")
        return
    log.info("cleanup.notifications_done", deleted=deleted)


def on_job_event(event: JobExecutionEvent) -> None:
    """Сбой и пропуск задачи — строкой JSON в общий журнал.

    Сам APScheduler пишет исключение задачи через stdlib logging, который здесь
    не настроен: текст уходил в stderr мимо журнала JSON, и падающую каждый
    прогон задачу нельзя было найти ни поиском, ни сводкой (проверка 24.09).
    """
    if event.code == EVENT_JOB_MISSED:
        log.warning(
            "scheduler.job_missed",
            job_id=event.job_id,
            scheduled_for=event.scheduled_run_time.isoformat(),
        )
        return
    exc = event.exception
    log.error(
        "scheduler.job_failed",
        job_id=event.job_id,
        error=repr(exc),
        exc_info=(type(exc), exc, exc.__traceback__) if exc else None,
    )


def build_scheduler() -> AsyncIOScheduler:
    """Все расписания процесса. Отдельной функцией — чтобы состав job'ов
    проверялся тестом, а не только глазами при чтении ``main()``."""
    # Пояс планировщика на готовый CronTrigger НЕ действует: без своего timezone
    # триггер берёт пояс контейнера, а в бою там TZ=Europe/Moscow — все ночные
    # задачи шли на три часа раньше записанного. Поэтому UTC передаётся каждому
    # триггеру явно (сторож — tests/unit/test_cron_utc_2409.py).
    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_listener(on_job_event, EVENT_JOB_ERROR | EVENT_JOB_MISSED)
    # Метка каждого прогона по расписанию — для догона после перезапуска.
    catch_up.remember_runs(scheduler)
    scheduler.add_job(heartbeat, IntervalTrigger(seconds=30), id="heartbeat", **DEFAULTS)
    scheduler.add_job(token_refresh, IntervalTrigger(minutes=30), id="token_refresh", **DEFAULTS)
    scheduler.add_job(
        enqueue_reconcile_all,
        IntervalTrigger(seconds=settings.reconcile_interval_seconds),
        id="reconcile",
        **DEFAULTS,
    )
    # Догрузка истории идёт сама: сторож поднимает каналы, у которых она не
    # закончена, — после срыва, после конца захода по времени и просто потому,
    # что её никогда не запускали. Без этой строки загрузка снова становится
    # ручной, а именно от ручной владелец и отказался 28.08.
    scheduler.add_job(
        enqueue_backfill_supervise,
        IntervalTrigger(seconds=settings.backfill_supervise_interval_seconds),
        id="backfill_supervise",
        **DEFAULTS,
    )
    scheduler.add_job(
        ensure_message_partitions,
        CronTrigger(hour=2, minute=40, timezone="UTC"),
        id="partitions",
        **DEFAULTS,
    )
    scheduler.add_job(
        cleanup_webhook_raw_log,
        CronTrigger(hour=3, minute=10, timezone="UTC"),
        id="raw_log_cleanup",
        **DEFAULTS,
    )
    scheduler.add_job(
        cleanup_retention,
        CronTrigger(hour=3, minute=20, timezone="UTC"),
        id="retention_cleanup",
        **DEFAULTS,
    )
    # Статистика (06 §3.2/§5.4): ежечасный REFRESH MV + уборка файлов выгрузок.
    # Без этой регистрации /stats/* отдают данные на момент миграции, а
    # `refreshed_at` навсегда остаётся null.
    stats_jobs.register(scheduler)
    # Спринт 7 (14 §2.1): внешний сторож — быстрые проверки раз в 5 минут и
    # суточная (сертификат, бэкап) в 06:15 UTC = 09:15 МСК. Без этой строки
    # процесс живёт, а сторож не запускается НИКОГДА.
    watchdog_jobs.register(scheduler)
    # Возврат розданных и нетронутых диалогов (7.7). Без этой строки
    # автораспределение отдаёт диалог ушедшему оператору навсегда: клиент
    # лежит «у него», где его не видит никто, — хуже, чем в общей очереди.
    reclaim_jobs.register(scheduler)
    # НОЧНОЙ УБОРКИ ОЧЕРЕДИ ЗДЕСЬ БОЛЬШЕ НЕТ (требование владельца от 11
    # августа, №8). Задание `queue_cleanup` в 04:20 UTC закрывало пачкой
    # непринятые диалоги, в которых клиент молчал дольше выбранного срока.
    # Владелец отказался от разгрузки очереди целиком — и от кнопки, и от
    # автоматики: в живой переписке пачечное закрытие необратимо, а очередь
    # разбирают руками.
    # Клиент написал в рабочий диалог и ждёт. Без этой строки оператор,
    # отошедший от стола, оставляет клиента в пустоте: диалог за человеком,
    # а значит не в очереди, где его видят все тринадцать.
    awaiting_jobs.register(scheduler)
    # Зависший бот (16.08): диалог с bot_active и входящим без ответа дольше
    # десяти минут возвращается в очередь. Без этой строки мёртвый воркер
    # оставляет клиента в комнате, куда не заглядывает никто.
    bot_stuck_jobs.register(scheduler)
    # Потерянный дедлайн бота (24.09): диалог с bot_active и истёкшим сроком
    # ожидания получает задачу дедлайна заново, а через час уходит людям. Без
    # этой строки клиент, не ответивший на вопрос бота, скрыт навсегда:
    # последнее слово за ботом, и сторож зависших сюда не смотрит.
    bot_deadlines_jobs.register(scheduler)
    # Счёт есть, реплики нет (08.09): `bot_msgs_row > 0` без единой реплики бота
    # в переписке. Без этой строки беда снова станет невидимой — на бою она
    # прожила пять суток и 127 диалогов, и заметил её только ручной сличкой
    # счётчика с лентой.
    bot_phantom_jobs.register(scheduler)
    # Несостоявшиеся расшифровки (05.09). Без этой строки записи, сгоревшие на
    # средовом отказе, остаются `failed` навсегда: поставить расшифровку заново
    # умеет только конвейер приёма, а он срабатывает один раз — в момент
    # прихода сообщения.
    voice_repair_jobs.register(scheduler)
    geo_repair_jobs.register(scheduler)
    merge_backlog_jobs.register(scheduler)
    # Воронка адресов (18.09): раз в неделю — сколько адресов из переписки
    # дошло до карточки и с какой степенью. Без этой строки автопривязка
    # работает вслепую: падение доли заметит только человек, сверяя карточки
    # руками.
    address_funnel_jobs.register(scheduler)
    # Свои адреса (пакет 7а, Q24): раз в неделю перед воронкой — какие дома
    # операторы сами называют клиентам в разных диалогах. Без этой строки
    # сторож эха адреса мастерской знает только список, который владелец
    # вписал руками, а адрес пункта приёма, повторённый клиентом, уедет в его
    # карточку и карта его подтвердит.
    address_own_jobs.register(scheduler)
    # Лестница политик правил адреса (пакет 6.0а, 20.09): раз в неделю после
    # воронки — по интервалу Уилсона поднять или опустить правило, при `off`
    # сказать администраторам, чем снять записанное. Без этой строки решение
    # владельца «автоматика без человека, точность только растёт» держится
    # только на настройке руками.
    rule_policy_jobs.register(scheduler)
    # Судья карточек (пакет 6.0б, 21.09): каждую ночь модель сверяет решения
    # правил с речью клиента и пишет вердикт в след строки. Без этой строки
    # лестница выше не видит ни одного осуждённого решения и правила из тени
    # не выходят.
    address_judge_jobs.register(scheduler)
    # ЗДЕСЬ СТОЯЛ СТОРОЖ ВОЗВРАТА ОТЛОЖЕННЫХ (`jobs/snooze.py`, docs/38 §7).
    # Снят 12 августа вместе со всей отложкой — решением владельца. Оставить
    # его без статуса `snoozed` было бы хуже, чем бесполезно: минутный запрос
    # к базе, который по построению не может ничего найти.
    # Retention центра уведомлений (14 §4): 03:50 UTC — в том же ночном окне,
    # что и остальные уборки, но не одновременно с ними.
    scheduler.add_job(
        cleanup_expired_notifications,
        CronTrigger(hour=3, minute=50, timezone="UTC"),
        id="notifications_cleanup",
        **DEFAULTS,
    )
    return scheduler


def _install_stop_handlers(
    loop: asyncio.AbstractEventLoop, scheduler: AsyncIOScheduler, stop: asyncio.Event
) -> None:
    """SIGTERM/SIGINT → штатная остановка, а не SIGKILL через десять секунд.

    ⚠ PID 1 БЕЗ ОБРАБОТЧИКА НЕ УМИРАЕТ ОТ SIGTERM. Здесь стояло голое
    `await asyncio.Event().wait()` с пометкой «SIGTERM гасит контейнер». В
    контейнере python — PID 1 (`command: python -m app.scheduler.main`, без
    entrypoint'а), а ядро не доставляет PID 1 сигнал, на который тот не повесил
    обработчик; свой обработчик Python ставит только на SIGINT. Замер боя
    05.09: КАЖДАЯ остановка планировщика — это 10 с ожидания `docker stop` и
    SIGKILL (exit 137). APScheduler не завершался, задача в полёте рвалась на
    середине, соединения к базе и Redis бросались незакрытыми, и каждая
    выкатка была длиннее на десять секунд.

    Оба сигнала: SIGTERM шлёт `docker stop`, SIGINT — Ctrl+C при локальном
    запуске. Повторный сигнал обязан быть безвредным: второй `shutdown()`
    бросил бы SchedulerNotRunningError прямо в цикле событий.

    Обработчик появляется только после старта — пока процесс ждёт базу или
    строит партиции, его ещё нет. Это окно закрывает `init: true` в compose:
    tini пересылает SIGTERM дочернему python, а не-PID-1 умирает от него сам.
    """

    def _stop(sig: signal.Signals) -> None:
        if stop.is_set():
            return
        log.info("scheduler.stop_requested", signal=sig.name)
        # wait=False: AsyncIOExecutor ждать всё равно не умеет (его shutdown
        # отменяет задачи при любом wait), а лишняя пауза — те же секунды
        # выкатки. Отменённая задача откатывается своим session_scope.
        scheduler.shutdown(wait=False)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _stop, sig)


async def main() -> None:
    global arq_pool
    configure_logging(component="scheduler")
    init_sentry("scheduler")  # 05 §7.1: без SENTRY_DSN — no-op
    db_mod.init_engine(component="scheduler")
    redis_mod.init_client()
    arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))

    try:
        # Пульс прежнего процесса — до первого своего: по нему догон знает,
        # был ли тот жив в плановое время задачи (`scheduler/catch_up.py`).
        alive_until = await catch_up.previous_heartbeat(redis_mod.get_client())
        # Партиции — сразу при старте (08 §6.3: запись не должна ждать первого крона)
        await ensure_message_partitions()
        await heartbeat()

        scheduler = build_scheduler()
        scheduler.start()
        log.info("scheduler.started", jobs=[j.id for j in scheduler.get_jobs()])
        # Прогон, чьё время пришлось на перезапуск, иначе потерян до следующего
        # периода (недельные замеры — на неделю, проверка 24.09).
        caught = await catch_up.catch_up_missed(
            scheduler, redis_mod.get_client(), alive_until=alive_until
        )
        if caught:
            log.info("scheduler.caught_up", jobs=caught)
        stop = asyncio.Event()
        _install_stop_handlers(asyncio.get_running_loop(), scheduler, stop)
        await stop.wait()
    finally:
        # Тот же порядок, что у воркера (workers/main.py::shutdown): сперва
        # очередь, потом Redis, последней — база. До 05.09 закрытия не было
        # вовсе: процесс жил до SIGKILL, и соединения обрывало ядро.
        # Общий httpx-клиент к Авито (client.http_client) закрывается в каждом
        # процессе, который им пользуется: планировщик ходит в Авито при
        # обновлении токенов. Без этого сокеты живут до смерти процесса —
        # безвредно, но закрывать своё принято там же, где открыли.
        await avito_client.close_http_client()
        await arq_pool.aclose()
        await redis_mod.close_client()
        await db_mod.dispose_engine()
        log.info("scheduler.stopped")


if __name__ == "__main__":
    asyncio.run(main())
