# 08 — Ядро бэкенда

> Источник истины по архитектуре — [DESIGN.md](../DESIGN.md) (разделы 1, 2, 8).
> Этот документ закрывает этапы 0–2 плана DESIGN §7: скелет FastAPI-приложения,
> inbound-конвейер вебхуков, воркеры доставки/reconciliation/backfill, WebSocket Hub,
> scheduler-процесс и сервисный CLI.
> Контракты, которым документ подчиняется и которые здесь **не** пересматриваются:
> REST/WS — [01-API-SPEC](01-API-SPEC.md); сервисы `api`/`worker`/`scheduler`, env-переменные,
> healthcheck — [05-DEPLOY-OPS](05-DEPLOY-OPS.md); интерфейс `bot_step` — [02-BOT-ENGINE](02-BOT-ENGINE.md);
> события `audit_log` — [06-STATS-REPORTS §0.3](06-STATS-REPORTS.md); интеграционные сценарии
> INT-1…INT-10 — [07-TESTING-SECURITY §1.2](07-TESTING-SECURITY.md).

## Содержание

1. [Структура FastAPI-приложения](#1-структура-fastapi-приложения)
2. [Inbound-конвейер: вебхук → Streams → воркер → БД](#2-inbound-конвейер)
3. [Воркер `deliver_message`](#3-воркер-deliver_message)
4. [Reconciliation и backfill истории](#4-reconciliation-и-backfill)
5. [WebSocket Hub](#5-websocket-hub)
6. [Scheduler-процесс](#6-scheduler-процесс)
7. [CLI (`python -m app.cli`)](#7-cli)
8. [Транзакционные границы и идемпотентность](#8-транзакционные-границы-и-идемпотентность)

---

## 1. Структура FastAPI-приложения

### 1.1. Дерево пакетов `app/`

Один пакет `app` — один Docker-образ на три процесса (05 §2.1): `api` (uvicorn `app.main:app`),
`worker` (`arq app.workers.main.WorkerSettings`), `scheduler` (`python -m app.scheduler.main`).
Разделение — только точками входа; общий код (модели, сервисы, интеграции) один.

```
app/
├── main.py                     # FastAPI(); lifespan (engine, Redis, WS Hub); include_router'ы
├── api/
│   ├── deps.py                 # Depends: get_db, get_redis, get_current_user, require_permission
│   └── routes/
│       ├── auth.py             # 01 §2: login/refresh/logout/invite/me
│       ├── users.py            # 01 §3
│       ├── avito_connect.py    # 01 §4.2–4.4: OAuth-флоу (код DESIGN §8.1)
│       ├── avito_accounts.py   # 01 §4.1, §4.5–4.7
│       ├── conversations.py    # 01 §5
│       ├── messages.py         # 01 §6.1–6.4 (+ mute_bot из 02 §2.6)
│       ├── media.py            # 01 §6.5 (подпись ссылок — core/media_sign.py, 05 §3.3)
│       ├── templates.py        # 01 §7
│       ├── bots.py             # 01 §8 + песочница 02 §5.3
│       ├── stats.py            # 01 §9 / 06 §4
│       ├── audit.py            # 01 §9.7
│       ├── presence.py         # PUT /presence (01 §11.6)
│       ├── webhooks.py         # POST /api/hooks/avito/{account_id} — gateway (DESIGN §8.3, 01 §10)
│       ├── ws.py               # GET /api/ws + POST /api/v1/ws/ticket (01 §11)
│       └── health.py           # /api/health, /api/health/deep (05 §7.2)
├── core/
│   ├── config.py               # Settings (pydantic-settings, env из 05 §4) — §1.3
│   ├── db.py                   # async engine + session factory (pool_size = DB_POOL_SIZE)
│   ├── redis.py                # процесс-синглтон redis.asyncio.Redis (REDIS_URL)
│   ├── crypto.py               # AES-256-GCM encrypt/decrypt (TOKEN_ENC_KEY, DESIGN §1.5)
│   ├── security.py             # JWT HS256, argon2id, refresh-ротация, invite-токены
│   ├── rbac.py                 # ROLE_PERMISSIONS: dict[str, frozenset[str]] (01 §12)
│   ├── errors.py               # ApiError + exception-handler'ы → envelope 01 §1.3
│   ├── pagination.py           # keyset-курсоры и offset-страницы (01 §1.4)
│   ├── logging.py              # structlog + scrub_secrets (05 §7.3)
│   ├── observability.py        # init_sentry(component=...) (05 §7.1)
│   └── media_sign.py           # signed_media_url (05 §3.3)
├── models/                     # SQLAlchemy 2 (async), схема = DESIGN §4.4
│   ├── base.py                 # DeclarativeBase + naming_convention (§1.4)
│   ├── user.py  account.py  client.py  conversation.py  message.py
│   ├── bot.py  template.py  audit.py
│   └── webhook_raw.py          # webhook_raw_log (§2.6)
├── schemas/                    # Pydantic-модели запросов/ответов (формы — 01)
├── services/                   # бизнес-логика, общая для routes и workers
│   ├── conversations.py        # upsert_conversation, смена статуса, назначение
│   ├── messages.py             # create_outbound_message, insert_message_idempotent
│   ├── clients.py              # upsert_client, maybe_extract_phone
│   ├── audit.py                # write_audit(...) — контракт 06 §0.3
│   ├── events.py               # publish_event → Pub/Sub 'events' (§5.3)
│   └── idempotency.py          # client_message_id (01 §1.6, код — §8.3)
├── integrations/
│   └── avito/
│       ├── oauth.py            # build_authorize_url, exchange_code, refresh_tokens (DESIGN §8.1)
│       ├── adapter.py          # AvitoAdapter: ChannelAdapter (DESIGN §1.6, §8.2)
│       ├── ratelimit.py        # пер-аккаунтный бюджет запросов (§4.3)
│       └── errors.py           # RateLimited, NeedsReauth, AvitoServerError
├── bots/                       # движок сценариев — раскладка нормативно в 02 §6
├── ws/
│   ├── hub.py                  # Hub: реестр сокетов процесса, dispatch (§5)
│   └── presence.py             # presence в Redis (§5.5)
├── workers/
│   ├── main.py                 # WorkerSettings для ARQ (§2.3)
│   ├── inbound.py              # консьюмер Redis Streams + apply_inbound_event (§2)
│   ├── deliver.py              # deliver_message (§3)
│   ├── reconcile.py            # reconcile_account (§4.1)
│   ├── backfill.py             # backfill_history (§4.2)
│   └── exports.py              # export_stats (06 §5.3)
├── scheduler/
│   ├── main.py                 # APScheduler-процесс (§6.2)
│   └── jobs/
│       ├── heartbeat.py  tokens.py  reconcile.py
│       ├── partitions.py       # партиции messages (§6.3)
│       ├── cleanup.py          # webhook_raw_log 30 дней, экспорты 7 дней (§6.4)
│       └── stats.py            # refresh MV — код в 06 §3.2
└── cli/
    └── __main__.py             # python -m app.cli … (§7)
```

Рядом в корне репозитория: `alembic.ini`, `alembic/` (§1.4), `pyproject.toml`/`uv.lock`
(собираются в образ по 05 §2.3), `tests/` (07 §1).

### 1.2. Принципы DI (Depends)

Никаких DI-фреймворков: штатный `Depends` FastAPI + модульные синглтоны для
инфраструктуры. Правила:

- **Инфраструктура** (`engine`, `Redis`, `Hub`) создаётся один раз на процесс в
  `lifespan` приложения (или `on_startup` ARQ) и живёт как атрибут модуля.
- **Сессия БД** — одна на запрос, через генератор-dependency; транзакции — явные
  (`async with db.begin()`, §8.1), «авто-commit в конце запроса» запрещён.
- **Пользователь и права** — цепочка `get_current_user → require_permission(...)`;
  роль всегда читается из БД, не из JWT (01 §1.2).

```python
# app/api/deps.py
from typing import AsyncIterator
from fastapi import Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.core import db as db_mod, redis as redis_mod
from app.core.errors import ApiError
from app.core.rbac import ROLE_PERMISSIONS
from app.core.security import decode_access_jwt

bearer = HTTPBearer(auto_error=False)

async def get_db() -> AsyncIterator["AsyncSession"]:
    async with db_mod.session_factory() as session:
        yield session                      # транзакции открывает вызывающий код (§8.1)

def get_redis() -> "Redis":
    return redis_mod.client                # процесс-синглтон

async def get_current_user(cred: HTTPAuthorizationCredentials | None = Depends(bearer),
                           db=Depends(get_db), redis=Depends(get_redis)) -> "User":
    if cred is None:
        raise ApiError("unauthorized", status=401)
    payload = decode_access_jwt(cred.credentials)          # exp/подпись -> 401 unauthorized
    if await redis.exists(f"revoked_users:{payload['sub']}"):
        raise ApiError("unauthorized", status=401)         # мгновенный разлогин (01 §1.2)
    user = await db.get(User, UUID(payload["sub"]))
    if user is None or not user.is_active:
        raise ApiError("forbidden", status=403)
    return user

def require_permission(perm: str):
    """Фабрика dependency: require_permission('messages:send') — 01 §12."""
    async def dep(user=Depends(get_current_user)) -> "User":
        if perm not in ROLE_PERMISSIONS[user.role]:
            if perm == "messages:send" and user.role == "head":
                raise ApiError("read_only_role", status=403)   # плашка «Режим просмотра» (01 §12)
            raise ApiError("forbidden", status=403)
        return user
    return dep
```

В воркерах и scheduler'е тех же зависимостей нет — там инфраструктура передаётся через
`ctx` ARQ (`ctx["db_session_factory"]`, `ctx["redis"]`, `ctx["arq"]` — имена из 02 §2.3)
либо импортируется из `app.core.*` напрямую.

### 1.3. Настройки (pydantic-settings)

Единственный источник конфигурации — env-переменные из 05 §4 (имена менять нельзя:
их читает Compose, CI и runbook). Поля класса = те же имена в lowercase:

```python
# app/core/config.py
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    """Все имена = .env.example из 05-DEPLOY-OPS §4 (case-insensitive)."""
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Общее ---
    env: str = "development"                 # production | staging | development
    domain: str = "chat.partner-lead-centre.ru"
    log_level: str = "INFO"
    image_tag: str = "dev"                   # тег образа = git SHA; отдаётся в /api/health

    # --- Хранилища ---
    database_url: str                        # postgresql+asyncpg://...
    db_pool_size: int = 10
    redis_url: str = "redis://redis:6379/0"

    # --- Авито ---
    avito_client_id: str
    avito_client_secret: str
    avito_api_base: str = "https://api.avito.ru"   # в dev/тестах — fake-avito (07 §2)
    avito_auth_url: str = "https://avito.ru/oauth" # OAuth-страница; в тестах — заглушка fake-avito (07 §2)

    # --- Крипто/Auth ---
    token_enc_key: str                       # base64, 32 байта — AES-256-GCM (DESIGN §1.5)
    jwt_secret: str
    jwt_access_ttl_seconds: int = 900
    refresh_ttl_days: int = 14
    # sso_shared_secret — снято 23.08.2026: у задела SSO нет кода (DESIGN §6)

    # --- AI ---
    anthropic_api_key: str = ""
    ai_model_answer: str = "claude-sonnet-5"
    ai_model_classify: str = "claude-haiku-4-5"
    ai_timeout_seconds: int = 10

    # --- Media ---
    media_root: str = "/var/leadchat/media"
    media_max_size_mb: int = 20
    media_sign_key: str

    # --- Мониторинг ---
    sentry_dsn: str = ""
    sentry_traces_sample_rate: float = 0.1
    kuma_webhook_canary_url: str = ""
    alert_tg_bot_token: str = ""
    alert_tg_chat_id: str = ""

    # --- Тюнинг ---
    web_concurrency: int = 2
    arq_max_jobs: int = 50
    reconcile_interval_seconds: int = 300

settings = Settings()
```

Валидация происходит при импорте: процесс с незаполненным обязательным секретом
не стартует (падает на старте контейнера, а не посреди ночи на первом запросе).

### 1.4. Alembic

**Раскладка** (копируется в образ, 05 §2.3):

```
alembic.ini                  # script_location = alembic; больше ничего интересного
alembic/
├── env.py                   # async-режим: engine из settings.database_url;
│                            # target_metadata = app.models.base.Base.metadata
└── versions/
    ├── 0001_init.py         # вся схема DESIGN §4.4 + webhook_raw_log (§2.6)
    ├── 0002_stats.py        # этап 3: business_seconds_between, индексы, MV (06 §2.1, §3.1–3.2)
    └── ...                  # префикс NNNN_ = revision id; линейная история, ветки запрещены
```

**Naming convention** — обязательна с первой миграции, иначе autogenerate плодит
безымянные констрейнты и diff'ы на ровном месте:

```python
# app/models/base.py
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)
```

**Правила autogenerate** (`alembic revision --autogenerate` — черновик, не истина;
каждую миграцию читаем глазами):

1. `compare_type=True`, `compare_server_default=True` в `env.py`.
2. `include_object` исключает из сравнения: месячные партиции `messages_y*`
   (их создаёт scheduler, §6.3 — Alembic про них не знает), materialized view
   `mv_conversation_stats` и её индексы (руками, код в 06 §3.2).
3. Руками, не автогенерацией, пишутся: `PARTITION BY RANGE` для `messages`
   (autogenerate его не умеет), `GENERATED ALWAYS AS (to_tsvector(...))`-колонка
   `search`, частичный уникальный индекс идемпотентности (§8.2), функции
   (`business_seconds_between`, 06 §2.1).
4. `CREATE INDEX CONCURRENTLY` — только в миграции с autocommit:
   `op.get_bind().execution_options(isolation_level="AUTOCOMMIT")` (05 §5.3, правило 4).
5. Каждая миграция — expand-contract (05 §5.3, правила 1–3): деплой запускает
   `alembic upgrade head` **до** обновления кода, старый код обязан работать на новой
   схеме. Вниз-миграции в проде запрещены — `downgrade()` пишем только для dev.
6. Backfill данных в миграциях запрещён — это ARQ-задача после деплоя (05 §5.3, правило 3).

**Сиды.** Данные в миграциях не создаём. Всё сидовое — через CLI (§7):

- `python -m app.cli create-admin --email ...` — первый администратор
  (чек-лист выкатки 05 Приложение Б, п.6);
- `python -m app.cli seed-smoke` — сидовый пользователь `smoke@leadchat.local`
  и служебный диалог `SMOKE-CONV` для деплой-smoke (07 §6); идемпотентна.

---

## 2. Inbound-конвейер

### 2.1. Обзор и гарантии

```
Авито ─▶ nginx ─▶ gateway (api)          ─▶ Redis Stream webhooks:avito
                  · secret, ≤1 МБ, 200 OK      · consumer group "workers"
                                               · XREADGROUP / XACK
                                          ─▶ ARQ-worker (консьюмер в том же процессе)
                                               · webhook_raw_log (30 дней)
                                               · parse_webhook → apply_inbound_event
                                               · PostgreSQL (одна транзакция)
                                               · audit_log (контракт 06 §0.3)
                                          ─▶ Pub/Sub 'events' → WS Hub (§5)
                                          ─▶ enqueue bot_step (02 §2.3)
                  зависшие pending ─▶ XAUTOCLAIM ─▶ повтор; ≥5 доставок ─▶ webhooks:avito:dlq
```

Гарантия конвейера: **at-least-once доставка** (consumer group + отсутствие ack при
ошибке) **+ идемпотентность записи** (уникальный индекс по `external_message_id`,
§8.2) **= exactly-once по факту** — ровно то, что проверяют INT-2 и INT-8 (07 §1.2).

### 2.2. Gateway

Код endpoint'а — DESIGN §8.3, контракт — 01 §10; здесь только требования к реализации:

- Gateway **не пишет в PostgreSQL и не ходит в Авито** (SLA p99 < 50 мс, 01 §10).
  Единственные операции: чтение `avito_accounts` (одна строка по PK; допустимо
  кэшировать `webhook_secret` в памяти процесса с TTL 60 с), `XADD`, и два ключа Redis:
  `webhook_last:{account_id}` (метка последнего вебхука — её отдаёт 01 §4.1) и лог-событие
  `webhook.received` (05 §7.3).
- Ответ всегда мгновенный `200 {"ok": true}` после `XADD` — даже на мусорный payload.
  Не-200 только: `403` (секрет), `413` (> 1 МБ), `429` (лимит 01 §1.7).
- Поля записи в стриме: `{"account_id": "<uuid>", "payload": "<json-строка>"}` —
  ровно как в DESIGN §8.3.

### 2.3. Redis Streams: параметры и место консьюмера

| Параметр | Значение | Откуда |
|---|---|---|
| Стрим | `webhooks:avito` | DESIGN §8.3, runbook 05 §8.1/8.3 |
| Consumer group | `workers` | runbook 05 §8.3 (`XPENDING webhooks:avito workers`) |
| Имя консьюмера | `{hostname}:{pid}` | уникально при `--scale worker=3` (05 §2.2) |
| Чтение | `XREADGROUP ... COUNT 10 BLOCK 5000` | латентность ≤ 5 с при пустом стриме |
| Подтверждение | `XACK` после успешной обработки **или** осознанного скипа | §2.4 |
| Восстановление | `XAUTOCLAIM min-idle-time 60s` | зависшие pending умерших воркеров |
| Максимум доставок | **5**, дальше → DLQ | runbook 05 §8.3, п.3 |
| DLQ | `webhooks:avito:dlq` | там же; реплей — `python -m app.cli replay-dlq` (§7) |

Консьюмер живёт **внутри ARQ-процесса** `worker`: `on_startup` ARQ запускает его
фоновой asyncio-задачей. Так «worker-пул» из DESIGN §1.1 остаётся одним контейнером,
`arq --check` (healthcheck 05 §2.2) покрывает оба контура, а `--scale worker=3` даёт
и больше консьюмеров группы, и больше исполнителей ARQ-задач.

```python
# app/workers/main.py
import asyncio
from arq.connections import RedisSettings, create_pool

from app.core.config import settings
from app.core.observability import init_sentry
from app.bots.runtime import bot_step, bot_ask_timeout          # 02 §2.3–2.4
from app.workers.deliver import deliver_message                 # §3
from app.workers.reconcile import reconcile_account             # §4.1
from app.workers.backfill import backfill_history               # §4.2
from app.workers.exports import export_stats                    # 06 §5.3
from app.workers.inbound import inbound_consumer_loop           # §2.4

async def smoke_noop(ctx) -> str:          # SM-6: «worker жив и разбирает очередь» (07 §6)
    return "ok"

async def startup(ctx):
    init_sentry(settings, component="worker")                   # 05 §7.1
    ctx["db_session_factory"] = make_session_factory()          # app.core.db
    ctx["redis"] = make_redis_client()                          # app.core.redis
    ctx["arq"] = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    ctx["shutdown"] = asyncio.Event()
    ctx["inbound_task"] = asyncio.create_task(inbound_consumer_loop(ctx))

async def shutdown(ctx):
    ctx["shutdown"].set()
    ctx["inbound_task"].cancel()

class WorkerSettings:
    functions = [deliver_message, bot_step, bot_ask_timeout,
                 reconcile_account, backfill_history, export_stats, smoke_noop]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_jobs = settings.arq_max_jobs        # ARQ_MAX_JOBS=50 (05 §4)
    job_timeout = 300
    max_tries = 5                           # общий потолок; deliver_message управляет ретраями сам (§3)
```

### 2.4. Консьюмер: полный код

```python
# app/workers/inbound.py
import asyncio, contextlib, json, os, socket, time
from uuid import UUID

import httpx, structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.core.config import settings
from app.integrations.avito.adapter import AvitoAdapter
from app.models.webhook_raw import WebhookRawLog
from app.services.events import publish_event
from app.services.audit import write_audit
from app.bots.runtime import should_run_bot                     # 02 §2.2

log = structlog.get_logger()

STREAM = "webhooks:avito"
GROUP = "workers"
DLQ = "webhooks:avito:dlq"
MAX_DELIVERIES = 5            # после пятой неудачной доставки — DLQ (runbook 05 §8.3)
CLAIM_IDLE_MS = 60_000        # pending старше минуты считаем зависшим
READ_COUNT, READ_BLOCK_MS = 10, 5_000

def _consumer_name() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"

async def _ensure_group(redis) -> None:
    """Идемпотентное создание группы. id='0' — не терять записи, добавленные до группы."""
    try:
        await redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
    except Exception as e:
        if "BUSYGROUP" not in str(e):
            raise

async def inbound_consumer_loop(ctx) -> None:
    """Фоновая задача ARQ-процесса (§2.3). Одна на процесс; реплики worker'а
    образуют consumer group и делят поток автоматически."""
    redis = ctx["redis"]
    await _ensure_group(redis)
    me = _consumer_name()
    while not ctx["shutdown"].is_set():
        try:
            await _reclaim_stuck(ctx, me)                       # XAUTOCLAIM + DLQ
            resp = await redis.xreadgroup(GROUP, me, {STREAM: ">"},
                                          count=READ_COUNT, block=READ_BLOCK_MS)
            for _stream, entries in resp or []:
                for entry_id, fields in entries:
                    await _handle_entry(ctx, entry_id, fields)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("inbound.loop_error")                 # Sentry; не даём циклу умереть
            await asyncio.sleep(1)

async def _handle_entry(ctx, entry_id: str, fields: dict) -> None:
    """Ошибка обработки => НЕ ack: запись остаётся в PEL и будет переиграна
    _reclaim_stuck (после 5 доставок уедет в DLQ). Успех и осознанный скип => ack."""
    try:
        await process_webhook_entry(ctx, entry_id, fields)
    except Exception:
        log.exception("webhook.process_failed", stream_id=entry_id)
        return
    await ctx["redis"].xack(STREAM, GROUP, entry_id)

async def _reclaim_stuck(ctx, me: str) -> None:
    """Восстановление зависших pending (воркер умер между XREADGROUP и XACK — INT-8).
    XAUTOCLAIM не возвращает счётчик доставок, поэтому счётчики берём из XPENDING."""
    redis = ctx["redis"]
    pending = await redis.xpending_range(STREAM, GROUP, min="-", max="+",
                                         count=200, idle=CLAIM_IDLE_MS)
    if not pending:
        return
    counts = {p["message_id"]: p["times_delivered"] for p in pending}
    start = "0-0"
    while True:
        start, entries, _deleted = await redis.xautoclaim(
            STREAM, GROUP, me, min_idle_time=CLAIM_IDLE_MS, start_id=start, count=50)
        if not entries:
            break
        for entry_id, fields in entries:
            if counts.get(entry_id, 0) >= MAX_DELIVERIES:       # poison message
                await redis.xadd(DLQ, {**fields, "orig_id": entry_id,
                                       "failed_at": str(int(time.time()))})
                await redis.xack(STREAM, GROUP, entry_id)
                log.error("webhook.moved_to_dlq", stream_id=entry_id)   # error => Sentry-алерт
            else:
                await _handle_entry(ctx, entry_id, fields)

async def process_webhook_entry(ctx, entry_id: str, fields: dict) -> None:
    """Разбор одной записи стрима. Исключение => редоставка (см. _handle_entry)."""
    db, redis = ctx["db_session_factory"](), ctx["redis"]
    account_id = UUID(fields["account_id"])
    payload = json.loads(fields["payload"])

    # 1. Сырец — в webhook_raw_log ДО парсинга (риск №2 DESIGN §7): даже если парсер
    #    упадёт, payload сохранён и доступен для `replay-raw`. Отдельная короткая
    #    транзакция; дедуп повторных доставок — по stream_id (§2.6).
    async with db.begin():
        await db.execute(pg_insert(WebhookRawLog).values(
            account_id=account_id, stream_id=entry_id, payload=payload,
        ).on_conflict_do_nothing(index_elements=["stream_id"]))

        account = await get_account(db, account_id)

    if account is None or account.status == "disabled":
        return                              # smoke-заглушка/отключённый аккаунт: ack без обработки (07 SM-8)

    event = AvitoAdapter.parse_webhook(payload)     # DESIGN §8.3; кривой формат -> исключение -> PEL -> DLQ
    if event is None:
        return                              # не-«сообщение» (read-receipt и пр.) — игнор по 01 §10
    if event.author_id == account.avito_user_id:
        return                              # эхо собственного исходящего (DESIGN §8.3)

    inserted = await apply_inbound_event(ctx, account, event)   # §2.5

    log.info("webhook.processed",                               # контракт логов 05 §7.3
             account_id=str(account_id), external_message_id=event.message_id,
             duplicate=not inserted,
             latency_ms=int(time.time() * 1000) - int(entry_id.split("-")[0]))
    await ping_canary(redis)                                    # 05 §7.2

async def ping_canary(redis) -> None:
    """Push-канарейка Uptime-Kuma: не чаще раза в минуту, fire-and-forget (05 §7.2)."""
    if not settings.kuma_webhook_canary_url:
        return
    if await redis.set("canary:webhook", "1", nx=True, ex=60):
        with contextlib.suppress(Exception):
            async with httpx.AsyncClient(timeout=3) as http:
                await http.get(settings.kuma_webhook_canary_url)
```

### 2.5. `apply_inbound_event` — единая точка записи входящего

Одна функция для всех трёх источников входящих — вебхук, reconciliation (§4.1),
backfill (§4.2). Благодаря этому идемпотентность, реопен, телефоны, события и запуск
бота ведут себя одинаково независимо от того, каким путём сообщение добралось до нас.

```python
# app/workers/inbound.py (продолжение)
async def apply_inbound_event(ctx, account, event, *,
                              run_bot: bool = True,
                              publish: bool = True,
                              backfill: bool = False) -> bool:
    """Применить нормализованное входящее событие (InboundEvent из адаптера).
    Возвращает True, если сообщение реально вставлено (False — дубль).
    Вся запись — ОДНА транзакция; publish/enqueue — строго после commit (§8.1)."""
    db, redis = ctx["db_session_factory"](), ctx["redis"]

    async with db.begin():
        client = await upsert_client(db, event)                  # clients: UNIQUE(channel, external_id)
        conv = await upsert_conversation(                        # conversations: UNIQUE(channel, external_chat_id);
            db, account, client, event,                          # существующая строка берётся FOR UPDATE (§8.4)
            initial_status="closed" if backfill else "new")      # история не должна засыпать очередь «Новые»
        inserted = await insert_message_idempotent(db, conv, event)  # ON CONFLICT DO NOTHING (§8.2)
        if not inserted:
            return False                                         # дубль: ретрай вебхука/реконсиляция

        if conv.last_message_at is None or event.created_at > conv.last_message_at:
            conv.last_message_at = event.created_at

        if conv.status == "closed" and not backfill:             # клиент вернулся (DESIGN §8.3, INT-4)
            conv.status, conv.assignee_id = "new", None
            await write_audit(db, user_id=None,                  # контракт 06 §0.3: reopen = ДВА события
                action="conversation.status_changed", entity="conversation",
                entity_id=str(conv.id),
                details={"from": "closed", "to": "new", "by": "system", "assignee_id": None})
            await write_audit(db, user_id=None,
                action="conversation.reopened", entity="conversation",
                entity_id=str(conv.id), details={"client_id": str(client.id)})

        await maybe_extract_phone(db, conv, client, event.text)  # телефон -> карточка; при ПЕРВОМ
                                                                 # заполнении -> audit client.phone_captured
                                                                 # (source="regex", 06 §0.3); INT-7

        bot = await get_bot(db, account.bot_id) if (run_bot and account.bot_id) else None
        start_bot = run_bot and should_run_bot(conv, account, bot)   # 02 §2.2

    if publish:                                                  # после commit'а — иначе фронт получит
        await publish_event(redis, "message:new", {              # событие о невидимых данных (§8.1)
            "conversation_id": str(conv.id),
            "message": message_out(conv, event),                 # полный MessageOut (01 §6.1)
            "conversation_patch": {                              # 01 §11.3
                "last_message_at": iso(conv.last_message_at),
                "status": conv.status,
                "unread_delta": 1,
            },
        })
    if start_bot:
        await ctx["arq"].enqueue_job("bot_step", conv.id, event.text)   # интерфейс 02 §2.3
    return True
```

Замечания:

- Запуск бота — **после** commit'а: `bot_step` начнётся с `SELECT ... FOR UPDATE`
  свежей строки и увидит вставленное сообщение (02 §2.3).
- `maybe_extract_phone` пишет `client.phone_captured` только при первом заполнении
  `clients.phone`; уже подтверждённый номер не перезаписывается (правило INT-7).
- `message_out` сериализует под `MessageOut` из 01 §6.1: `direction="in"`,
  `sender_type="client"`, `client_message_id=null`.

### 2.6. Таблица `webhook_raw_log`

Хранилище сырых payload'ов на 30 дней (риск №2 DESIGN §7 «формат вебхуков меняется»;
05 §7.3 требует держать их в БД, не в stdout-логах). Пишет — воркер (§2.4), gateway
к PostgreSQL не прикасается.

```sql
-- часть миграции 0001_init
CREATE TABLE webhook_raw_log (
  id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  account_id uuid,                          -- без FK: лог обязан переживать любые манипуляции с аккаунтом
  stream_id  text NOT NULL,                 -- id записи в webhooks:avito — дедуп повторных доставок
  payload    jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_webhook_raw_stream ON webhook_raw_log (stream_id);
CREATE INDEX idx_webhook_raw_created ON webhook_raw_log (created_at);
CREATE INDEX idx_webhook_raw_account_created ON webhook_raw_log (account_id, created_at);
```

Retention — job scheduler'а (§6.4), ежедневно:

```sql
DELETE FROM webhook_raw_log WHERE created_at < now() - interval '30 days';
```

Объёмы (~10 000 строк/сутки × 30) не требуют партиционирования; обычный `DELETE` по
индексу `created_at` укладывается в секунды. Реплей после починки парсера —
`python -m app.cli replay-raw --since ...` (§7, runbook 05 §8.1 шаг 4).

---

## 3. Воркер `deliver_message`

Контур доставки исходящих (DESIGN §8.2, 01 §6.2): endpoint мгновенно отвечает
`pending`, доставку делает ARQ-задача, итог уходит WS-событием `message:status`.
Требования 07 §2.3 (поведение на 401/429/500/slow) — прямо в коде:

```python
# app/workers/deliver.py
import random
from uuid import UUID

import httpx, structlog
from arq import Retry

from app.integrations.avito.adapter import AvitoAdapter
from app.integrations.avito.errors import RateLimited, NeedsReauth, AvitoServerError
from app.integrations.avito.ratelimit import avito_limiter          # §4.3
from app.services.events import publish_event

log = structlog.get_logger()

MAX_TRIES = 5                 # DESIGN §8.2 / 01 §6.2: «5 ретраев экспоненциально»
AVITO_TEXT_LIMIT = 1000       # лимит мессенджера Авито ~1000 символов — длинные режем (01 §6.2)
REAUTH_ERROR = "Аккаунт Авито требует переподключения"

def backoff(attempt: int) -> int:
    """Экспоненциальный backoff с джиттером: ~2, 8, 32, 128 сек."""
    return min(300, int(2 * 4 ** (attempt - 1) * (0.8 + random.random() * 0.4)))

def split_text(text: str, limit: int) -> list[str]:
    """Режем по границе строк/слов, не по середине слова."""
    if len(text) <= limit:
        return [text]
    parts, rest = [], text
    while rest:
        cut = rest.rfind("\n", 0, limit) if len(rest) > limit else len(rest)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    return [p for p in parts if p]

async def deliver_message(ctx, message_id: UUID) -> None:
    """Идемпотентна к повторной постановке: доставляет только pending-сообщения."""
    db, redis = ctx["db_session_factory"](), ctx["redis"]
    attempt = ctx["job_try"]                                    # 1..MAX_TRIES (ARQ)

    # --- Фаза 1: прочитать и проверить (короткая транзакция) ---
    async with db.begin():
        msg = await get_message_for_update(db, message_id)      # SELECT ... FOR UPDATE (§8.4)
        if msg is None or msg.delivery_status != "pending":
            return                                              # уже delivered/failed — гасим дубль джобы
        conv = await db.get(Conversation, msg.conversation_id)
        account = await get_account(db, conv.account_id)
        if account.status != "active":                          # needs_reauth | disabled
            await fail_message(db, msg, REAUTH_ERROR)
    if msg.delivery_status == "failed":
        await _publish_status(redis, conv, msg, "failed", error=REAUTH_ERROR)
        return

    # --- Фаза 2: поход в Авито — строго ВНЕ транзакции (§8.1) ---
    adapter = AvitoAdapter(db, redis)
    try:
        await avito_limiter.acquire(account.id, bucket="interactive")   # §4.3
        external_id = None
        for part in split_text(msg.body, AVITO_TEXT_LIMIT):
            # внутри send_message: одна попытка авто-рефреша токена на 401 (DESIGN §8.2)
            external_id = await adapter.send_message(account, conv.external_chat_id,
                                                     part, msg.attachments)
    except RateLimited as e:
        # 429: ждём не меньше Retry-After, сообщение остаётся pending (07 §2.3);
        # попытка НЕ считается сожжённой зря — Retry её инкрементирует, поэтому
        # берём максимум из Retry-After и штатного backoff
        delay = max(e.retry_after, backoff(attempt))
        log.warning("message.deliver", message_id=str(message_id), attempt=attempt,
                    status_code=429, outcome="retry", retry_in_sec=delay)
        raise Retry(defer=delay)
    except NeedsReauth:
        # refresh провалился, account уже помечен needs_reauth (DESIGN §8.1);
        # событие account:needs_reauth админам публикует refresh_tokens
        await _fail_and_publish(ctx, message_id, REAUTH_ERROR)
        return
    except (httpx.HTTPError, AvitoServerError) as e:
        outcome = "retry" if attempt < MAX_TRIES else "failed"
        log.warning("message.deliver", message_id=str(message_id), attempt=attempt,
                    status_code=getattr(e, "status_code", None), outcome=outcome,
                    retry_in_sec=backoff(attempt) if outcome == "retry" else None)
        if attempt >= MAX_TRIES:
            await _fail_and_publish(ctx, message_id,
                                    "Авито: сообщение не доставлено после 5 попыток")
            return
        raise Retry(defer=backoff(attempt))                     # экспоненциальный повтор

    # --- Фаза 3: зафиксировать успех ---
    async with db.begin():
        msg = await get_message_for_update(db, message_id)
        msg.delivery_status = "delivered"
        msg.external_message_id = external_id                   # эхо-вебхук отсечётся по author_id
    await _publish_status(redis, conv, msg, "delivered")
    log.info("message.deliver", message_id=str(message_id), attempt=attempt,
             outcome="delivered")

async def _fail_and_publish(ctx, message_id: UUID, error: str) -> None:
    db, redis = ctx["db_session_factory"](), ctx["redis"]
    async with db.begin():
        msg = await get_message_for_update(db, message_id)
        if msg is None or msg.delivery_status != "pending":
            return
        conv = await db.get(Conversation, msg.conversation_id)
        await fail_message(db, msg, error)                      # delivery_status='failed'
    await _publish_status(redis, conv, msg, "failed", error=error)

async def _publish_status(redis, conv, msg, status: str, error: str | None = None) -> None:
    """WS-событие message:status (01 §11.3): итог доставки, error — только при failed."""
    data = {"conversation_id": str(conv.id), "message_id": str(msg.id),
            "delivery_status": status}
    if error:
        data["error"] = error
    await publish_event(redis, "message:status", data)
```

Дополнения к контракту:

- **`POST /messages/{id}/retry`** (01 §6.3): endpoint ставит `delivery_status='pending'`
  и заново enqueue'ит `deliver_message` — новая джоба, `job_try` начинается с 1
  («счётчик ретраев обнуляется»).
- **`failed_last_hour`** для `/api/health/deep` (05 §7.2) считается по
  `messages.delivery_status='failed'` — воркер ничего дополнительно не пишет.
- Сообщения бота идут тем же контуром (`send_bot_message` → `deliver_message`, 02 §2.3) —
  отдельного пути доставки у бота нет.

---

## 4. Reconciliation и backfill

### 4.1. Reconciliation-поллинг (каждые 5 минут)

Страховка недоставленных вебхуков (DESIGN §1.3). Расписание живёт в scheduler'е
(интервал `RECONCILE_INTERVAL_SECONDS=300`, 05 §4), исполнение — в worker'е:
scheduler ставит по ARQ-задаче на каждый активный аккаунт.

```python
# app/scheduler/jobs/reconcile.py
async def enqueue_reconcile_all() -> None:
    async with session_factory() as db:
        ids = (await db.execute(
            select(AvitoAccount.id).where(AvitoAccount.status == "active"))).scalars().all()
    for account_id in ids:
        # _job_id: если прошлый прогон по аккаунту ещё идёт — новый НЕ ставится (дедуп ARQ)
        await arq_pool.enqueue_job("reconcile_account", account_id,
                                   _job_id=f"reconcile:{account_id}")
```

```python
# app/workers/reconcile.py
async def reconcile_account(ctx, account_id: UUID) -> None:
    """Догоняет сообщения, не дошедшие вебхуками. Идемпотентна: повторный прогон
    не создаёт ни дублей (уникальный индекс §8.2), ни повторных событий
    (публикация только для реально вставленных строк). Контракт — INT-3 (07 §1.2)."""
    db, redis = ctx["db_session_factory"](), ctx["redis"]
    async with db.begin():
        account = await get_account(db, account_id)
    if account is None or account.status != "active":
        return                                     # disabled/needs_reauth не опрашиваем (01 §4.5)

    adapter = AvitoAdapter(db, redis)
    chats_checked = recovered = 0
    # GET /messenger/v2/accounts/{uid}/chats?unread_only=true (DESIGN §1.3) — бюджет bulk (§4.3)
    async for chat in adapter.fetch_chats(account, unread_only=True):
        chats_checked += 1
        async with db.begin():
            conv = await find_conversation(db, "avito", chat.external_chat_id)
        # быстрый отсев: если последнее сообщение чата уже у нас — историю не качаем
        if conv is not None and conv.last_message_at is not None \
           and chat.last_message_at <= conv.last_message_at:
            continue
        since = conv.last_message_at if conv else None
        async for event in adapter.fetch_history(account, chat, since=since):
            if event.author_id == account.avito_user_id:
                continue                           # своё исходящее — как в §2.4
            if await apply_inbound_event(ctx, account, event):   # тот же путь, что вебхук (§2.5):
                recovered += 1                     # событие в WS, бот, реопен — всё идентично

    log.info("reconcile.run", account_id=str(account_id),        # контракт логов 05 §7.3
             chats_checked=chats_checked, messages_recovered=recovered)
    await ping_canary(redis)     # ночью вебхуков нет — канарейку кормит reconciliation (05 §7.2)
```

Свойства:

- **Идемпотентность** — трёхслойная: отсев по `last_message_at`, `since`-курсор,
  и на самом дне — уникальный индекс `external_message_id` (повторный прогон = 0 новых
  строк, ассерт INT-3).
- Восстановленное сообщение проходит через `apply_inbound_event` с дефолтами —
  т.е. **запускает бота и публикует события точно так же, как вебхук**. Сообщение,
  доехавшее reconciliation'ом, отличается для системы только задержкой ≤ 5 мин.
- После инцидента догон запускается руками, не дожидаясь цикла:
  `python -m app.cli reconcile --all` (runbook 05 §8.1, шаг 5).

### 4.2. Backfill истории при подключении аккаунта

Ставится callback'ом OAuth (`enqueue_history_backfill`, DESIGN §8.1) как ARQ-задача
`backfill_history` с `_job_id=f"backfill:{account_id}"` (двойное подключение не породит
два прогона).

```python
# app/workers/backfill.py
PROGRESS_TTL = 7 * 24 * 3600

async def backfill_history(ctx, account_id: UUID) -> None:
    """Выкачивает существующие чаты аккаунта (DESIGN §1.2), чтобы менеджеры видели
    контекст до внедрения LeadChat. Резюмируема: прогресс (offset страницы чатов) —
    в Redis `backfill:{account_id}`; после падения воркера задача продолжает с
    последней страницы. Полный повторный прогон дублей не создаёт (§8.2)."""
    db, redis = ctx["db_session_factory"](), ctx["redis"]
    async with db.begin():
        account = await get_account(db, account_id)
    if account is None or account.status != "active":
        return

    adapter = AvitoAdapter(db, redis)
    key = f"backfill:{account_id}"
    offset = int(await redis.get(key) or 0)
    loaded_convs = 0

    while True:
        # GET /messenger/v2/accounts/{uid}/chats — постранично, бюджет bulk (§4.3)
        chats = await adapter.fetch_chats_page(account, offset=offset, limit=100)
        if not chats:
            break
        for chat in chats:
            async for event in adapter.fetch_history(account, chat, since=None):
                if event.author_id == account.avito_user_id:
                    # исходящие из истории тоже сохраняем: это контекст переписки
                    await insert_outbound_history(ctx, account, chat, event)
                    continue
                await apply_inbound_event(ctx, account, event,
                                          run_bot=False,    # история не будит ботов
                                          publish=False,    # и не спамит WS тысячами message:new
                                          backfill=True)    # новые диалоги создаются status='closed'
            if chat.has_unread:
                # в чате реально ждут ответа — поднять из архива в очередь
                await reopen_conversation(ctx, account, chat)   # closed -> new + audit-пара (§2.5)
            loaded_convs += 1
        offset += len(chats)
        await redis.set(key, offset, ex=PROGRESS_TTL)

    await redis.delete(key)
    await publish_event(redis, "notify", {                      # тост админам (01 §11.3)
        "level": "info", "title": "История загружена",
        "text": f"Аккаунт «{account.title}»: загружено {loaded_convs} диалогов",
        "audience_hint": "admin",
    }, audience="admin")
```

Ключевые решения:

- **Боты не запускаются** на исторических сообщениях (`run_bot=False`) — иначе бот
  начал бы «здороваться» в диалогах трёхмесячной давности.
- **По-сообщенческие WS-события не публикуются** (`publish=False`) — фронт узнаёт о
  завершении одним `notify`; список диалогов он и так перезапросит.
- Диалоги из истории создаются `status='closed'` (архив, DESIGN §1.4 — «видеть
  контекст»), кроме чатов с непрочитанными входящими: их backfill переоткрывает
  штатной процедурой реопена, и они попадают в «Новые».
- Исходящие из истории пишутся с `sender_type='operator'`, `sender_user_id=NULL`
  (отправлены до LeadChat — автора не знаем), `delivery_status='delivered'`.

### 4.3. Rate-limit бюджет к API Авито

Все вызовы Авито любого происхождения (доставка, reconciliation, backfill, проверка
вебхука) идут через один пер-аккаунтный лимитер — иначе backfill свежеподключённого
аккаунта съест квоту, и у **всех** воркеров начнутся 429.

```python
# app/integrations/avito/ratelimit.py
import asyncio, random, time

# Бюджет консервативный; реальную квоту Messenger API уточнить в кабинете
# разработчика (developers.avito.ru) при старте — см. дисклеймер DESIGN §8
BUDGET_PER_MIN = 500          # всего запросов в минуту на аккаунт
BULK_SHARE = 0.8              # bulk-потребители не занимают больше 80% бюджета

class AvitoRateLimiter:
    """Фиксированное окно 60 с в Redis. Два класса трафика:
    - interactive (deliver_message, действия из UI) — доступен весь бюджет;
    - bulk (backfill, reconciliation) — только 80%: интерактиву всегда остаётся запас.
    acquire() блокирует корутину до появления бюджета — вызывающий код не думает о 429."""

    def __init__(self, redis):
        self.redis = redis

    async def acquire(self, account_id, *, bucket: str = "interactive") -> None:
        limit = BUDGET_PER_MIN if bucket == "interactive" else int(BUDGET_PER_MIN * BULK_SHARE)
        while True:
            key = f"ratelimit:avito:{account_id}:{int(time.time() // 60)}"
            n = await self.redis.incr(key)
            if n == 1:
                await self.redis.expire(key, 120)
            if n <= limit:
                return
            await self.redis.decr(key)          # не съедаем чужой бюджет, ждём окно
            await asyncio.sleep(1 + random.random())

avito_limiter: AvitoRateLimiter                 # инициализируется на старте процесса
```

Правила:

- `AvitoAdapter` вызывает `acquire()` перед **каждым** HTTP-запросом; класс трафика
  передаёт вызывающий контур (`deliver_message` → `interactive`; `fetch_*` в
  reconciliation/backfill → `bulk`). Refresh токенов вне бюджета — единичные запросы.
- Ответ 429 от Авито всё равно обрабатывается (адаптер бросает
  `RateLimited(retry_after)`): лимитер — предохранитель, `Retry-After` — истина.
  Bulk-контуры на `RateLimited` просто спят `retry_after` и продолжают с места остановки.
- Лимитер глобальный на аккаунт, а не на процесс: окно в Redis честно делится между
  репликами worker'а при `--scale worker=3`.

---

## 5. WebSocket Hub

### 5.1. Процессная модель при `WEB_CONCURRENCY=2`

Контейнер `api` — это `WEB_CONCURRENCY=2` процесса uvicorn (05 §2.2/§4). Сокеты
пользователей распределяются между процессами произвольно (nginx → uvicorn master),
поэтому:

- **Каждый процесс держит свой Hub** — локальный реестр только своих соединений.
  Межпроцессного реестра сокетов нет и не нужно.
- **Каждый Hub подписан на один и тот же Pub/Sub канал `events`** (DESIGN §1.1).
  Публикатор (worker, scheduler, соседний api-процесс) ничего не знает о том, где
  чей сокет: он публикует событие один раз, каждый Hub доставляет его своим.
- Всё межпроцессное состояние (presence, тикеты) — в Redis.

```
worker ──▶ PUBLISH events ──┬──▶ api-процесс #1: Hub -> локальные сокеты
scheduler ─▶ PUBLISH events ─┤
api #2 ────▶ PUBLISH events ─┴──▶ api-процесс #2: Hub -> локальные сокеты
```

### 5.2. Тикет-авторизация `/ws/ticket`

Контракт — 01 §11.1 (одноразовый тикет, TTL 60 с, `GETDEL`):

```python
# app/api/routes/ws.py
import secrets

@router.post("/ws/ticket")                       # /api/v1/ws/ticket; право: любой аутентифицированный
async def ws_ticket(user=Depends(get_current_user), redis=Depends(get_redis)):
    ticket = "wst_" + secrets.token_urlsafe(36)
    await redis.set(f"ws_ticket:{ticket}", str(user.id), ex=60)
    return {"ticket": ticket, "expires_in": 60}

@router.websocket("/ws")                         # /api/ws — вне /v1 (01 §1.1)
async def ws_endpoint(ws: WebSocket, ticket: str | None = None,
                      db=Depends(get_db), redis=Depends(get_redis)):
    user_id = await redis.getdel(f"ws_ticket:{ticket}") if ticket else None
    if not user_id:
        await ws.close(code=4401)                # тикет невалиден — до первого кадра (01 §11.1)
        return
    user = await db.get(User, UUID(user_id.decode() if isinstance(user_id, bytes) else user_id))
    if user is None or not user.is_active:
        await ws.close(code=4403)
        return
    await ws.accept()
    session = hub.attach(ws, user)               # локальный реестр процесса (§5.3)
    await presence_connected(redis, user.id, session.conn_id)    # §5.5
    try:
        while True:
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=60)
            except asyncio.TimeoutError:         # 60 с ни одного кадра (01 §11.5)
                await ws.close(code=4408)
                return
            await handle_client_frame(session, raw, redis)
    except WebSocketDisconnect:
        pass
    finally:
        hub.detach(session)
        asyncio.create_task(presence_disconnected(redis, user.id, session.conn_id))
```

Свойства из контракта, за которые отвечает эта реализация: тикет строго одноразовый
(`GETDEL`); протухание access-JWT сокет **не** рвёт (аутентификация — тикетом на всю
жизнь соединения); деактивация/logout закрывает сокет `4403` (§5.4); в nginx на
`location /api/ws` выключен access_log (05 §3.2).

### 5.3. Hub: реестр, канал `events`, фильтрация по правам

Формат публикации — внутренний envelope поверх кадра 01 §11.2: служебное поле `meta`
хаб использует для маршрутизации и **вырезает** перед отправкой клиенту.

```python
# app/services/events.py
async def publish_event(redis, type_: str, data: dict, *,
                        audience: str | None = None,
                        exclude_user: str | None = None) -> None:
    """Единственная точка публикации в Pub/Sub 'events'. Вызывается строго ПОСЛЕ
    commit'а транзакции, породившей событие (§8.1)."""
    evt = {"type": type_, "ts": utcnow_iso(), "data": data}
    if audience or exclude_user:
        evt["meta"] = {"audience": audience, "exclude_user": exclude_user}
    await redis.publish("events", json.dumps(evt, ensure_ascii=False))
```

```python
# app/ws/hub.py
import json, uuid
from dataclasses import dataclass, field

@dataclass
class Session:
    ws: "WebSocket"
    user_id: "UUID"
    full_name: str
    role: str                                   # снимок на момент подключения (см. ниже)
    conn_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    conversation_id: "UUID | None" = None       # одна активная подписка (01 §11.4)

    async def send(self, frame: dict) -> None:
        await self.ws.send_text(json.dumps(frame, ensure_ascii=False))

class Hub:
    """Один экземпляр на uvicorn-процесс. Запускается из lifespan api-процесса."""

    def __init__(self, redis):
        self.redis = redis
        self.sessions: dict[str, Session] = {}          # conn_id -> Session

    def attach(self, ws, user) -> Session:
        s = Session(ws=ws, user_id=user.id, full_name=user.full_name, role=user.role)
        self.sessions[s.conn_id] = s
        return s

    def detach(self, s: Session) -> None:
        self.sessions.pop(s.conn_id, None)

    async def run(self) -> None:
        """Подписка процесса на Pub/Sub 'events' (§5.1). Живёт всё время процесса."""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe("events")
        async for raw in pubsub.listen():
            if raw["type"] != "message":
                continue
            try:
                evt = json.loads(raw["data"])
            except ValueError:
                continue
            await self.dispatch(evt)

    async def dispatch(self, evt: dict) -> None:
        meta = evt.pop("meta", None) or {}

        if evt["type"].startswith("control:"):          # служебные, клиенту не уходят
            if evt["type"] == "control:revoked":        # деактивация/logout/смена роли
                for s in [x for x in self.sessions.values()
                          if str(x.user_id) == evt["data"]["user_id"]]:
                    code = evt["data"].get("code", 4403)
                    await s.ws.close(code=code)         # 4403 -> /login; 4401 -> reconnect (01 §11.7)
            return

        for s in list(self.sessions.values()):
            if not self._allowed(s, evt, meta):
                continue
            try:
                await s.send(self._personalize(s, evt))
            except Exception:
                self.detach(s)                          # мёртвый сокет — вычистить молча

    def _allowed(self, s: Session, evt: dict, meta: dict) -> bool:
        """Фильтрация по правам и адресности. Дефолт 01 §11.3: события диалогов
        получают ВСЕ подключённые (чтение всех диалогов есть у всех ролей)."""
        t, d = evt["type"], evt.get("data", {})
        if meta.get("exclude_user") == str(s.user_id):
            return False
        if meta.get("audience") == "admin" and s.role != "admin":
            return False
        # заметки не доставляются observer-сессиям (01 §6.1/§11.3; UAT-29, 07 §5)
        if t == "message:new" and d.get("message", {}).get("direction") == "note" \
           and s.role == "observer":
            return False
        if t == "account:needs_reauth" and s.role != "admin":    # только админам (01 §11.3)
            return False
        if t == "typing":                                        # только подписчикам диалога (01 §11.3)
            cid = str(s.conversation_id) if s.conversation_id else None
            return d.get("conversation_id") == cid
        return True

    def _personalize(self, s: Session, evt: dict) -> dict:
        """Пер-получательские поля: is_for_you в conversation:assigned (01 §11.3)."""
        if evt["type"] == "conversation:assigned":
            assignee = (evt["data"].get("assignee") or {}).get("id")
            return {**evt, "data": {**evt["data"],
                                    "is_for_you": assignee == str(s.user_id)}}
        return evt
```

Клиентские кадры (01 §11.4):

```python
# app/ws/hub.py (продолжение)
async def handle_client_frame(session: Session, raw: str, redis) -> None:
    try:
        frame = json.loads(raw)
        ftype = frame["type"]
    except (ValueError, KeyError, TypeError):
        await session.send({"type": "error", "data": {"code": "bad_frame"}})   # сокет не рвём
        return

    if ftype == "ping":
        await session.send({"type": "pong", "ts": utcnow_iso(),
                            "data": frame.get("data")})
        await presence_heartbeat(redis, session.user_id, session.conn_id)      # §5.5
    elif ftype == "subscribe":
        cid = (frame.get("data") or {}).get("conversation_id")
        session.conversation_id = UUID(cid) if cid else None    # новая подписка заменяет старую
    elif ftype == "typing":
        cid = (frame.get("data") or {}).get("conversation_id")
        if cid:
            await publish_event(redis, "typing",
                {"conversation_id": cid, "source": "operator",
                 "user": {"id": str(session.user_id), "full_name": session.full_name}},
                exclude_user=str(session.user_id))              # отправителю не возвращаем
    else:
        await session.send({"type": "error", "data": {"code": "bad_frame"}})
```

Кто публикует `control:revoked`: `POST /users/{id}/deactivate` и `POST /auth/logout`
(код 4403 — «уйти на /login», 01 §11.7), а также `PATCH /users/{id}` при смене роли
(код **4401** — клиент возьмёт новый тикет и переподключится уже с новой ролью;
иначе `role`-снимок в `Session` жил бы до конца соединения и, например, разжалованный
в observer продолжал бы получать заметки).

### 5.4. События ролей и деактивация

Сводка «кто что получает» (реализована в `_allowed`, источники — 01 §11.3 и 07 §5):

| Событие | admin | head | manager | observer |
|---|:-:|:-:|:-:|:-:|
| `message:new` (in/out/system) | ✅ | ✅ | ✅ | ✅ |
| `message:new` (`direction: note`) | ✅ | ✅ | ✅ | ❌ |
| `message:status`, `conversation:updated`, `conversation:assigned` | ✅ | ✅ | ✅ | ✅ |
| `typing` | только подписанные на диалог сессии, без отправителя | | | |
| `presence:online` | ✅ | ✅ | ✅ | ✅ |
| `account:needs_reauth` | ✅ | ❌ | ❌ | ❌ |
| `notify` (`audience_hint`/`meta.audience`) | по адресату | | | |

### 5.5. Presence

Контракт: 01 §11.6 (ключ `presence:{user_id}` с TTL 90 с, продление на ping) и
01 §11.3 (`offline` публикуется через 30 с после закрытия последнего сокета).
Сокеты одного пользователя могут жить в разных процессах — реестр соединений в Redis:

```python
# app/ws/presence.py
import asyncio, time

GRACE_OFFLINE = 30                    # 01 §11.3: grace на reconnect
CONN_TTL = 90                         # соединение без ping 90 с считается мёртвым

async def presence_connected(redis, user_id, conn_id: str) -> None:
    first = await redis.zcard(f"presence:conns:{user_id}") == 0
    await redis.zadd(f"presence:conns:{user_id}", {conn_id: time.time()})
    await redis.set(f"presence:{user_id}", "online", ex=CONN_TTL)
    if first:
        await publish_event(redis, "presence:online",
                            {"user_id": str(user_id), "status": "online"})

async def presence_heartbeat(redis, user_id, conn_id: str) -> None:
    """Вызывается на каждый прикладной ping (раз в 25 с, 01 §11.5)."""
    await redis.zadd(f"presence:conns:{user_id}", {conn_id: time.time()})
    status = await redis.get(f"presence:{user_id}") or "online"
    await redis.set(f"presence:{user_id}", status, ex=CONN_TTL)   # продление TTL

async def presence_disconnected(redis, user_id, conn_id: str) -> None:
    await redis.zrem(f"presence:conns:{user_id}", conn_id)
    await asyncio.sleep(GRACE_OFFLINE)
    # вычищаем соединения, не пинговавшие CONN_TTL (умершие процессы)
    await redis.zremrangebyscore(f"presence:conns:{user_id}", "-inf",
                                 time.time() - CONN_TTL)
    if await redis.zcard(f"presence:conns:{user_id}") == 0:       # последний сокет ушёл
        await redis.delete(f"presence:{user_id}")
        await publish_event(redis, "presence:online",
                            {"user_id": str(user_id), "status": "offline"})
```

`PUT /api/v1/presence {"status": "away"}` (01 §11.6) пишет `away` в `presence:{user_id}`
(с тем же TTL) и публикует `presence:online` со статусом `away`. `is_online` в
`GET /users` (01 §3.1) — это `EXISTS presence:{user_id}`.

---

## 6. Scheduler-процесс

### 6.1. Полный список задач

Один процесс `python -m app.scheduler.main` (05 §2.2), APScheduler `AsyncIOScheduler`.
Все задачи — `coalesce=True, max_instances=1` (пропущенные прогоны схлопываются,
параллельных экземпляров нет).

| Job id | Расписание | Что делает | Контракт |
|---|---|---|---|
| `heartbeat` | каждые 30 с | `SET scheduler:alive <iso-now> EX 120` | healthcheck 05 §2.2, smoke SM-7 (07 §6) |
| `token_refresh` | каждые 30 мин | refresh аккаунтов с `token_expires_at < now()+2h` | DESIGN §1.5/§8.1 |
| `reconcile` | каждые `RECONCILE_INTERVAL_SECONDS` (300) с | enqueue `reconcile_account` по активным аккаунтам | §4.1, DESIGN §1.3 |
| `partitions` | ежедневно 02:40 UTC | партиции `messages` на текущий + следующий месяц | 05 §5.3 п.5, INT-10 |
| `raw_log_cleanup` | ежедневно 03:10 UTC | `DELETE FROM webhook_raw_log` старше 30 дней | §2.6, DESIGN §7 |
| `exports_cleanup` | ежедневно 03:20 UTC | файлы экспорта старше 7 суток из `EXPORT_DIR` | 06 §5.4 |
| `stats_mv_refresh` | ежечасно HH:05 | `REFRESH MATERIALIZED VIEW CONCURRENTLY mv_conversation_stats` | код — 06 §3.2 (регистрируется как есть) |

Не задачи scheduler'а (чтобы не искать их здесь): бэкапы и media-gc — cron хоста
(05 §6, Приложение А); таймауты `ask`/`menu` — отложенные ARQ-джобы (02 §2.4);
GC неприкреплённых `media_id` — TTL в Redis + media-gc.sh (01 §6.5).

### 6.2. `app/scheduler/main.py`

```python
# app/scheduler/main.py
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from arq.connections import RedisSettings, create_pool

from app.core.config import settings
from app.core.logging import init_logging
from app.core.observability import init_sentry
from app.scheduler.jobs import heartbeat, tokens, reconcile, partitions, cleanup, stats

DEFAULTS = dict(coalesce=True, max_instances=1, misfire_grace_time=300)

async def main() -> None:
    init_logging(settings)
    init_sentry(settings, component="scheduler")            # 05 §7.1
    reconcile.arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(heartbeat.beat, IntervalTrigger(seconds=30),
                      id="heartbeat", **DEFAULTS)
    scheduler.add_job(tokens.refresh_expiring_tokens, IntervalTrigger(minutes=30),
                      id="token_refresh", **DEFAULTS)
    scheduler.add_job(reconcile.enqueue_reconcile_all,
                      IntervalTrigger(seconds=settings.reconcile_interval_seconds),
                      id="reconcile", **DEFAULTS)
    scheduler.add_job(partitions.ensure_message_partitions, CronTrigger(hour=2, minute=40),
                      id="partitions", **DEFAULTS)
    scheduler.add_job(cleanup.cleanup_webhook_raw_log, CronTrigger(hour=3, minute=10),
                      id="raw_log_cleanup", **DEFAULTS)
    scheduler.add_job(cleanup.cleanup_exports, CronTrigger(hour=3, minute=20),
                      id="exports_cleanup", **DEFAULTS)
    stats.register(scheduler)                               # stats_mv_refresh, код в 06 §3.2

    scheduler.start()
    await asyncio.Event().wait()                            # живём вечно; SIGTERM гасит контейнер

if __name__ == "__main__":
    asyncio.run(main())
```

```python
# app/scheduler/jobs/heartbeat.py
from datetime import datetime, timezone
from app.core.redis import client as redis

async def beat() -> None:
    """Контракт healthcheck (05 §2.2): ключ обновляется каждые 30 с, EX 120 —
    два пропущенных прогона подряд валят docker-healthcheck и smoke SM-7."""
    await redis.set("scheduler:alive",
                    datetime.now(timezone.utc).isoformat(), ex=120)
```

```python
# app/scheduler/jobs/tokens.py
async def refresh_expiring_tokens() -> None:
    """DESIGN §1.5: выбрать активные аккаунты с истечением < 2 часов и обновить.
    refresh_tokens() — код DESIGN §8.1: лок lock:token:{id} (refresh одноразовый),
    провал -> status='needs_reauth' + событие account:needs_reauth админам (§5.4)."""
    async with session_factory() as db:
        accounts = (await db.execute(
            select(AvitoAccount).where(
                AvitoAccount.status == "active",
                AvitoAccount.token_expires_at < utcnow() + timedelta(hours=2)))
        ).scalars().all()
    for account in accounts:
        try:
            async with session_factory() as db:
                await refresh_tokens(account, db, redis)        # app/integrations/avito/oauth.py
        except Exception:
            log.exception("token.refresh", account_id=str(account.id), outcome="error")
            # не прерываем цикл: остальные аккаунты должны обновиться
```

### 6.3. Партиции `messages` — DDL-job

Правило 5 из 05 §5.3: партиция следующего месяца создаётся scheduler'ом заранее,
деплой не является условием работоспособности записи. Ежедневный прогон с
`IF NOT EXISTS` гарантирует запас ≥ 7 дней тривиально — партиция следующего месяца
существует всегда (это же закрывает INT-10).

```python
# app/scheduler/jobs/partitions.py
from datetime import date
from sqlalchemy import text
from app.core.db import engine

def _month_bounds(d: date) -> tuple[date, date]:
    start = d.replace(day=1)
    end = (start.replace(year=start.year + 1, month=1)
           if start.month == 12 else start.replace(month=start.month + 1))
    return start, end

async def ensure_message_partitions() -> None:
    """Партиции messages на текущий и следующий месяц. Идемпотентна.
    Имя: messages_y2026m09; границы — календарный месяц по UTC (created_at в UTC)."""
    d = date.today().replace(day=1)
    async with engine.begin() as conn:
        for _ in range(2):                                  # текущий + следующий
            start, end = _month_bounds(d)
            name = f"messages_y{start.year}m{start.month:02d}"
            await conn.execute(text(
                f"CREATE TABLE IF NOT EXISTS {name} PARTITION OF messages "
                f"FOR VALUES FROM ('{start}') TO ('{end}')"))
            d = end
```

Первые партиции (текущий и следующий месяц) создаёт миграция `0001_init` — чтобы
система писала сообщения до первого прогона scheduler'а.

### 6.4. Очистки

```python
# app/scheduler/jobs/cleanup.py
from pathlib import Path
import time
from sqlalchemy import text
from app.core.db import engine

EXPORT_DIR = Path("/var/leadchat/media/exports")            # 06 §5.3
EXPORT_TTL_DAYS = 7                                         # 06 §5.4
RAW_LOG_TTL_DAYS = 30                                       # DESIGN §7 риск №2

async def cleanup_webhook_raw_log() -> None:
    async with engine.begin() as conn:
        res = await conn.execute(text(
            "DELETE FROM webhook_raw_log "
            "WHERE created_at < now() - make_interval(days => :d)"), {"d": RAW_LOG_TTL_DAYS})
    log.info("cleanup.webhook_raw_log", deleted=res.rowcount)

async def cleanup_exports() -> None:
    """Файлы экспорта статистики старше 7 суток (06 §5.4). Подписанные ссылки на них
    (TTL 24 ч) к этому моменту давно истекли."""
    deadline = time.time() - EXPORT_TTL_DAYS * 86400
    deleted = 0
    for f in EXPORT_DIR.glob("leadchat-stats_*"):
        if f.stat().st_mtime < deadline:
            f.unlink(missing_ok=True)
            deleted += 1
    log.info("cleanup.exports", deleted=deleted)
```

---

## 7. CLI

Точка входа — `python -m app.cli` (Typer; работает внутри контейнера:
`docker compose exec api python -m app.cli ...`). На эти команды ссылается runbook
05 §8 и чек-лист выкатки 05 Приложение Б — сигнатуры фиксированы.

| Команда | Сигнатура | Что делает |
|---|---|---|
| `create-admin` | `create-admin --email EMAIL [--full-name NAME]` | Создаёт пользователя `role='admin'` и печатает одноразовую invite-ссылку (01 §2.4) — пароль админ ставит сам через браузер; пароли в CLI не вводятся и не печатаются. Если email занят — перевыпускает invite (для восстановления доступа). `audit_log: user.invited` |
| `reconcile` | `reconcile (--account UUID \| --all)` | Немедленно ставит `reconcile_account` (§4.1) по одному/всем активным аккаунтам, не дожидаясь 5-минутного цикла. Runbook 05 §8.1 шаг 5, §8.2 шаг 4 |
| `backfill` | `backfill --account UUID [--restart]` | Ставит `backfill_history` (§4.2). `--restart` сбрасывает курсор `backfill:{id}` и прогоняет историю заново (дублей не будет — §8.2) |
| `avito subscriptions` | `avito subscriptions --account UUID` | Показывает подписки вебхуков глазами Авито (`GET .../v1/subscriptions`); токен наружу не печатается. Runbook 05 §8.1 шаг 3 |
| `avito resubscribe` | `avito resubscribe (--account UUID \| --all)` | Перерегистрирует webhook (повтор `setup_webhook`, DESIGN §8.3). `--all` — после даунтайма > пары часов (runbook 05 §8.1) |
| `avito refresh` | `avito refresh --account UUID` | Принудительный refresh пары токенов через штатный `refresh_tokens` (с локом). Runbook 05 §8.2 |
| `replay-dlq` | `replay-dlq [--limit N]` | Возвращает события из `webhooks:avito:dlq` в основной стрим (после починки парсера). Runbook 05 §8.3 шаг 3 |
| `replay-raw` | `replay-raw --since ISO [--account UUID]` | Прогоняет payload'ы из `webhook_raw_log` заново через `parse_webhook → apply_inbound_event` (идемпотентно). Runbook 05 §8.1 шаг 4 |
| `seed-smoke` | `seed-smoke` | Идемпотентно создаёт `smoke@leadchat.local` (role=manager, скрыт из UI-списков) и служебный диалог `SMOKE-CONV` (07 §6) |
| `sentry-test` | `sentry-test` | Бросает тестовое исключение в Sentry (чек-лист 05 Приложение Б п.9) |

Каркас и два репрезентативных примера:

```python
# app/cli/__main__.py
import asyncio
import typer

app = typer.Typer(help="Сервисные команды LeadChat (runbook 05 §8)")
avito = typer.Typer(help="Операции с API Авито")
app.add_typer(avito, name="avito")

def run(coro):
    return asyncio.run(coro)

@app.command("create-admin")
def create_admin(email: str = typer.Option(...),
                 full_name: str = typer.Option("Администратор")):
    """Первый администратор: INSERT + одноразовая invite-ссылка на stdout."""
    async def _run():
        async with session_factory() as db, db.begin():
            user = await upsert_admin(db, email=email, full_name=full_name)
            url, expires = await issue_invite(db, redis, user)          # 01 §2.4/§3.2
            await write_audit(db, user_id=None, action="user.invited",
                              entity="user", entity_id=str(user.id),
                              details={"role": "admin", "by": "cli"})
        typer.echo(f"Invite URL (одноразовая, до {expires}):\n{url}")
    run(_run())

@app.command("replay-dlq")
def replay_dlq(limit: int = typer.Option(100)):
    """Вернуть события из webhooks:avito:dlq в основной стрим. Запись удаляется
    из DLQ только после успешного XADD — команду можно прерывать."""
    async def _run():
        entries = await redis.xrange("webhooks:avito:dlq", "-", "+", count=limit)
        for entry_id, fields in entries:
            fields.pop("orig_id", None); fields.pop("failed_at", None)
            await redis.xadd("webhooks:avito", fields)
            await redis.xdel("webhooks:avito:dlq", entry_id)
        typer.echo(f"replayed: {len(entries)}")
    run(_run())

if __name__ == "__main__":
    app()
```

---

## 8. Транзакционные границы и идемпотентность

### 8.1. Правила транзакций

1. **Транзакции всегда явные**: `async with db.begin()`. Session factory создаётся с
   `expire_on_commit=False`; никакого autocommit/autoflush-магии.
2. **Одно бизнес-событие = одна транзакция.** Примеры границ:
   - входящее сообщение: `upsert_client + upsert_conversation + insert_message +
     реопен + телефон + audit_log` — атомарно (§2.5, ровно как в DESIGN §8.3);
   - `POST /conversations/{id}/messages`: создание сообщения + автоназначение
     (`new → in_progress`, 01 §6.2) + `mute_bot` (02 §2.6) — одна транзакция;
   - тик бота: вся работа `bot_step` — одна транзакция с `FOR UPDATE` (02 §2.3).
3. **Внешний I/O внутри транзакции запрещён**: походы в Авито/Claude/Kuma — только
   между транзакциями (образец — три фазы `deliver_message`, §3). Держать строку
   `FOR UPDATE` на время HTTP-вызова с таймаутом 15 с — верный способ положить пул.
4. **Pub/Sub и enqueue — строго после commit.** Событие о незакоммиченных данных
   приводит к тому, что фронт запрашивает деталь и получает 404, а `bot_step`
   стартует до видимости сообщения. Порядок всегда: `commit → publish_event →
   enqueue_job`.
5. Если после commit'а publish/enqueue упал — не страшно: WS-клиенты догонят через
   `updated_since` (01 §11.7), недоставленную джобу бота поднимет следующее входящее,
   недоставленное `message:status` фронт увидит при перезапросе. Обратный порядок
   (publish до commit) не чинится ничем — поэтому правило жёсткое.

### 8.2. Уникальные индексы — второй эшелон идемпотентности

Redis-ключи и `since`-курсоры — оптимизация; последняя линия обороны от дублей всегда
в PostgreSQL:

| Инвариант | Механизм |
|---|---|
| Одно входящее сообщение — одна строка (ретраи вебхука, reconciliation, replay) | `CREATE UNIQUE INDEX ON messages (conversation_id, external_message_id, created_at) WHERE external_message_id IS NOT NULL` + вставка `ON CONFLICT DO NOTHING` (DESIGN §4.4, 01 §10, INT-2) |
| Один диалог на внешний чат | `conversations UNIQUE (channel, external_chat_id)`; upsert через `ON CONFLICT` |
| Один клиент на внешний id | `clients UNIQUE (channel, external_id)` |
| Один аккаунт Авито | `avito_accounts.avito_user_id UNIQUE` (reconnect матчится по нему, 01 §4.4) |
| Один сотрудник на email | `users.email citext UNIQUE` |
| Сырец вебхука пишется один раз | `webhook_raw_log.stream_id UNIQUE` (§2.6) |

Паттерн upsert-функций (`upsert_client`, `upsert_conversation`):
`INSERT ... ON CONFLICT (…) DO NOTHING RETURNING id`, при конфликте — `SELECT`
существующей строки (для `conversations` — с `FOR UPDATE`, §8.4). Это переживает
гонку двух воркеров, одновременно создающих один диалог: выигравший вставляет,
проигравший читает.

### 8.3. `client_message_id` (контракт 01 §1.6)

```python
# app/services/idempotency.py
IDEM_TTL = 86_400            # 24 ч — покрывает офлайн-очередь десктопа (01 §1.6)

async def acquire_idempotency(redis, db, conv_id, payload) -> "Message | None":
    """None => первая попытка, создаём сообщение (id уже зарезервирован в ключе).
    Message => реплей: вернуть 200 + X-Idempotent-Replay: true с ЭТИМ сообщением."""
    key = f"idem:msg:{conv_id}:{payload.client_message_id}"
    new_id = uuid4()
    if await redis.set(key, str(new_id), nx=True, ex=IDEM_TTL):
        payload.message_id = new_id                 # endpoint создаст message с этим id
        return None

    existing_id = await redis.get(key)
    msg = (await db.execute(
        select(Message).where(Message.id == UUID(existing_id)))).scalar_one_or_none()
    if msg is None:
        # ключ есть, сообщения нет: прошлая попытка упала между SET NX и commit.
        # Перезанимаем ключ и создаём заново — для клиента это первая успешная попытка.
        await redis.set(key, str(new_id), ex=IDEM_TTL)
        payload.message_id = new_id
        return None
    if msg.body != payload.text:
        raise ApiError("idempotency_mismatch", status=409)      # баг фронта, его надо увидеть
    return msg
```

Порядок в endpoint'е: `acquire_idempotency` (Redis, вне транзакции) → транзакция
создания → commit → enqueue `deliver_message`. Тот же механизм обязателен для заметок
(`POST .../notes`, 01 §1.6). Дыра «SET прошёл, commit нет» закрыта веткой `msg is None`.

### 8.4. Конкурентность: локи и порядок захвата

| Лок | Ключ / механизм | Держатель | Зачем |
|---|---|---|---|
| Refresh токена | `SET NX lock:token:{account_id} EX 30` | scheduler-job, реактивный 401-путь адаптера | refresh_token Авито одноразовый — двойное использование сжигает его (DESIGN §1.5; тест 07 §1.1.4) |
| Тик бота | `SET NX lock:bot:{conversation_id} EX 30` | `bot_step` | сериализация тиков при шквале входящих (02 §2.3); не взял — перепостановка через 1 с |
| Строка диалога | `SELECT ... FOR UPDATE` | `bot_step`, `apply_inbound_event` (существующий диалог), endpoints статуса/назначения/отправки | гонка «оператор написал vs бот шлёт» (02 §2.6), реопен vs назначение |
| Строка сообщения | `SELECT ... FOR UPDATE` | `deliver_message`, `/retry` | двойная постановка джобы не должна дать двойную отправку (§3) |
| Дедуп фоновых прогонов | ARQ `_job_id` (`reconcile:{id}`, `backfill:{id}`) | scheduler / CLI | второй прогон не стартует поверх идущего (§4) |

Правила, которые ревьюим на PR:

1. **Порядок захвата строк** фиксированный: сначала `conversations`, потом `messages`.
   Обратный порядок в любом месте = будущий deadlock.
2. Redis-лок берётся **до** открытия транзакции (образец — `bot_step`, 02 §2.3), и
   всегда с TTL: упавший воркер не оставляет вечных локов.
3. `FOR UPDATE` — только точечно по PK/уникальному ключу, никогда по диапазону.
4. Долгие операции (backfill, экспорт) не держат ни локов, ни транзакций дольше
   одной порции данных: транзакция на чат/страницу, не на весь прогон.

### 8.5. Каталог ключей Redis (сводно)

Все применения Redis живут в db 0 и различаются префиксами (05 §4). Владелец —
кто пишет ключ.

| Ключ | Тип / TTL | Владелец | Назначение |
|---|---|---|---|
| `webhooks:avito` | stream | gateway | очередь вебхуков (§2) |
| `webhooks:avito:dlq` | stream | консьюмер | poison messages (§2.4) |
| `events` | pub/sub канал | все процессы | события реального времени (§5) |
| `idem:msg:{conv}:{cmid}` | string, 24 ч | api | идемпотентность отправки (§8.3) |
| `lock:token:{account_id}` | string, 30 с | oauth | одноразовый refresh (§8.4) |
| `lock:bot:{conversation_id}` | string, 30 с | bot_step | сериализация тиков (02 §2.3) |
| `ws_ticket:{ticket}` | string, 60 с | api | тикет WS (§5.2) |
| `presence:{user_id}` | string, 90 с | hub | статус online/away (§5.5) |
| `presence:conns:{user_id}` | zset | hub | реестр сокетов пользователя между процессами (§5.5) |
| `revoked_users:{user_id}` | string, 15 мин | api | мгновенный разлогин (01 §1.2) |
| `read:{user_id}` | hash | api | read-маркеры непрочитанных (01 §5.1) |
| `webhook_last:{account_id}` | string | gateway | метка последнего вебхука (01 §4.1) |
| `oauth_state:{state}` | string, 10 мин | api | CSRF OAuth-флоу (DESIGN §8.1) |
| `invite:{token}` | string, 72 ч | api | одноразовые invite (01 §2.4) |
| `ratelimit:avito:{account}:{min}` | counter, 120 с | лимитер | бюджет запросов к Авито (§4.3) |
| `backfill:{account_id}` | string, 7 дн | backfill | курсор резюмирования (§4.2) |
| `canary:webhook` | string, 60 с | воркеры | троттлинг пинга канарейки (§2.4) |
| `scheduler:alive` | string, 120 с | scheduler | heartbeat (§6.2) |
| `sandbox:{admin_id}:{uuid}` | string, 1 ч | api | песочница ботов (02 §5.3) |
| `stats:refreshed_at`, `stats:export:*`, `stats:my:*` | см. 06 | scheduler/api/worker | статистика (06 §3.2, §5, §6) |

---

*Остальные документы серии: 01-API-SPEC (REST/WS-контракт), 02-BOT-ENGINE (движок
сценариев — `bot_step`, на который ссылается §2.5), 03-FRONTEND (потребитель
WS-событий Hub §5 и REST-контракта), 04-DESKTOP (outbox/синк десктопа поверх того же
API), 05-DEPLOY-OPS (процессы, env, runbook — использует CLI из §7), 06-STATS-REPORTS
(audit-события из §2.5, jobs из §6), 07-TESTING-SECURITY (INT-1…10 проверяют конвейер
§2–§4, SM-тесты — §5–§6).*
