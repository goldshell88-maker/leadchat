# 05 — Инфраструктура, деплой и эксплуатация

> Источник истины по архитектуре — [DESIGN.md](../DESIGN.md) (разделы 1.1, 2, 9).
> Этот документ детализирует: как всё это собирается, запускается, обновляется, бэкапится, мониторится и чинится.
> Целевая среда: **один VPS в РФ** (4 vCPU / 8 GB RAM / 160 GB NVMe — с запасом на рост), Ubuntu 24.04 LTS, Docker Engine 27+ и docker compose v2.27+.

## Содержание

1. [Раскладка на сервере](#1-раскладка-на-сервере)
2. [Docker Compose и Dockerfile](#2-docker-compose-и-dockerfile)
3. [nginx](#3-nginx)
4. [Переменные окружения (.env.example)](#4-переменные-окружения)
5. [CI/CD (GitHub Actions)](#5-cicd)
6. [Бэкапы](#6-бэкапы)
7. [Мониторинг и логи](#7-мониторинг-и-логи)
8. [Runbook типовых инцидентов](#8-runbook)

---

## 1. Раскладка на сервере

```
/opt/leadchat/                  # деплой-директория (владелец: deploy)
├── docker-compose.yml
├── .env                        # секреты, chmod 600, вне git
├── nginx/
│   ├── nginx.conf
│   └── conf.d/leadchat.conf
└── bin/
    ├── deploy.sh               # вызывается из CI по ssh
    ├── backup.sh               # cron, ежедневно
    ├── backup-verify.sh        # cron, ежемесячно
    └── media-gc.sh             # чистка осиротевших вложений

/var/leadchat/media/            # вложения (bind-mount: api rw, nginx ro)
/var/leadchat/download/         # инсталляторы десктопа (NSIS + MSI) + latest.json (заливает CI, см. 04-DESKTOP.md §6.1)
/var/leadchat/certbot/          # webroot для ACME-челленджей
/var/backups/leadchat/          # локальные дампы БД (до отправки офсайт)
/var/lib/docker/volumes/…       # pgdata, redisdata — управляет Docker
```

Первичная подготовка VPS (один раз, руками):

```bash
# пользователь деплоя без sudo-пароля только на нужные команды
adduser deploy && usermod -aG docker deploy
mkdir -p /opt/leadchat /var/leadchat/{media,download,certbot} /var/backups/leadchat
chown -R deploy:deploy /opt/leadchat /var/leadchat /var/backups/leadchat

# firewall: наружу только 22/80/443
ufw allow 22/tcp && ufw allow 80/tcp && ufw allow 443/tcp && ufw enable

# fail2ban для sshd — ставим из коробки
apt install -y fail2ban

# первичный выпуск сертификата ДО старта nginx с TLS-конфигом (bootstrap):
docker run --rm -p 80:80 \
  -v /etc/letsencrypt:/etc/letsencrypt \
  certbot/certbot certonly --standalone \
  -d chat.partner-lead-centre.ru \
  --email admin@partner-lead-centre.ru --agree-tos --no-eff-email
```

После bootstrap-выпуска сертификат живёт в `/etc/letsencrypt`, дальше его продлевает контейнер `certbot` через webroot (см. compose ниже) — nginx перезапускать не нужно, он перечитывает сертификаты по расписанию (reload каждые 6 часов, см. команду сервиса nginx).

---

## 2. Docker Compose и Dockerfile

### 2.1. Принципы

- **Один образ `leadchat-api` для трёх процессов**: `api` (uvicorn), `worker` (ARQ), `scheduler` (APScheduler). Различие — только команда запуска. Меньше образов — меньше рассинхрона кода.
- **Образ `leadchat-nginx`** содержит собранную React-статику (multi-stage: node → nginx). Фронт и конфиг nginx версионируются вместе с бэкендом одним тегом.
- Postgres и Redis наружу не торчат (только внутренняя сеть), наружу смотрит один nginx.
- Все сервисы — `restart: unless-stopped`, healthchecks, лимиты памяти (OOM одного контейнера не должен уронить соседей).
- Redis — с AOF-персистентностью (`appendonly yes`): очередь Redis Streams обязана переживать рестарт контейнера. При этом Redis **не бэкапится** (см. раздел 6.4) — персистентность и бэкап это разные гарантии.

### 2.2. `docker-compose.yml` (полный, прод)

```yaml
name: leadchat

x-logging: &default-logging
  driver: json-file
  options:
    max-size: "20m"
    max-file: "5"

x-app-common: &app-common
  image: ghcr.io/lead-partner/leadchat-api:${IMAGE_TAG:-latest}
  env_file: .env
  restart: unless-stopped
  logging: *default-logging
  networks: [internal]
  depends_on:
    postgres:
      condition: service_healthy
    redis:
      condition: service_healthy

services:
  api:
    <<: *app-common
    command: >
      uvicorn app.main:app --host 0.0.0.0 --port 8000
      --workers ${WEB_CONCURRENCY:-2} --proxy-headers --forwarded-allow-ips "*"
    networks: [internal, edge]
    volumes:
      - /var/leadchat/media:/var/leadchat/media          # приём вложений (rw)
    healthcheck:
      # без curl в образе — проверяем питоном
      test: ["CMD", "python", "-c",
             "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/api/health', timeout=3).status==200 else 1)"]
      interval: 15s
      timeout: 5s
      retries: 4
      start_period: 20s
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 1024M }

  worker:
    <<: *app-common
    command: arq app.workers.main.WorkerSettings
    volumes:
      - /var/leadchat/media:/var/leadchat/media          # скачивание вложений из Авито
    healthcheck:
      # штатный health-check ARQ: читает health_check_key из Redis
      test: ["CMD", "arq", "--check", "app.workers.main.WorkerSettings"]
      interval: 30s
      timeout: 10s
      retries: 3
      start_period: 15s
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 768M }
    # при росте нагрузки: docker compose up -d --scale worker=3
    # (consumer group Redis Streams сама разделит поток между репликами)

  scheduler:
    <<: *app-common
    command: python -m app.scheduler.main
    healthcheck:
      # scheduler раз в 30 сек обновляет Redis-ключ scheduler:alive (SET ... EX 120);
      # тот же ключ проверяет деплой-smoke SM-7 (07-TESTING-SECURITY §6)
      test: ["CMD", "python", "-c",
             "import os,sys,redis; sys.exit(0 if redis.Redis.from_url(os.environ['REDIS_URL']).exists('scheduler:alive') else 1)"]
      interval: 60s
      timeout: 5s
      retries: 3
      start_period: 30s
    deploy:
      resources:
        limits: { cpus: "0.5", memory: 384M }

  postgres:
    image: postgres:16-alpine
    restart: unless-stopped
    logging: *default-logging
    networks: [internal]
    environment:
      POSTGRES_DB: ${POSTGRES_DB}
      POSTGRES_USER: ${POSTGRES_USER}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}
    command: >
      postgres
      -c shared_buffers=1GB
      -c effective_cache_size=3GB
      -c maintenance_work_mem=256MB
      -c work_mem=16MB
      -c max_connections=120
      -c wal_compression=on
      -c log_min_duration_statement=500
      -c timezone=UTC
    volumes:
      - pgdata:/var/lib/postgresql/data
      - ${BACKUP_DIR:-/var/backups/leadchat}:/backups   # сюда пишет pg_dump из backup.sh
                                                        # (на текущем проде BACKUP_DIR=/var/leadchat/backups)
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}"]
      interval: 10s
      timeout: 5s
      retries: 5
    deploy:
      resources:
        limits: { cpus: "2.0", memory: 2048M }

  redis:
    image: redis:7-alpine
    restart: unless-stopped
    logging: *default-logging
    networks: [internal]
    command: >
      redis-server
      --appendonly yes
      --appendfsync everysec
      --maxmemory 512mb
      --maxmemory-policy noeviction
    # noeviction обязателен: в Redis лежит ОЧЕРЕДЬ (Streams) и denylist refresh-токенов —
    # молча выкидывать ключи нельзя. Кэши держим с явным TTL и укладываемся в лимит.
    volumes:
      - redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 10s
      timeout: 3s
      retries: 5
    deploy:
      resources:
        limits: { cpus: "1.0", memory: 640M }

  nginx:
    image: ghcr.io/lead-partner/leadchat-nginx:${IMAGE_TAG:-latest}
    restart: unless-stopped
    logging: *default-logging
    networks: [edge]
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - /etc/letsencrypt:/etc/letsencrypt:ro
      - /var/leadchat/certbot:/var/www/certbot:ro
      - /var/leadchat/media:/var/leadchat/media:ro
      - /var/leadchat/download:/var/leadchat/download:ro
    # reload каждые 6ч — подхват продлённого сертификата без даунтайма
    command: >
      /bin/sh -c "while :; do sleep 6h; nginx -s reload; done & nginx -g 'daemon off;'"
    healthcheck:
      test: ["CMD", "nginx", "-t"]
      interval: 30s
      timeout: 5s
      retries: 3
    depends_on:
      api:
        condition: service_healthy
    deploy:
      resources:
        limits: { cpus: "0.5", memory: 256M }

  certbot:
    image: certbot/certbot
    restart: unless-stopped
    logging: *default-logging
    volumes:
      - /etc/letsencrypt:/etc/letsencrypt
      - /var/leadchat/certbot:/var/www/certbot
    entrypoint: >
      /bin/sh -c "trap exit TERM;
      while :; do certbot renew --webroot -w /var/www/certbot --quiet; sleep 12h & wait $${!}; done"
    deploy:
      resources:
        limits: { cpus: "0.2", memory: 128M }

networks:
  edge:        # nginx <-> api; наружу торчит только nginx
  internal:    # api/worker/scheduler <-> postgres/redis; без выхода к nginx

volumes:
  pgdata:
  redisdata:
```

Замечания:

- `deploy.resources.limits` в docker compose v2 применяется и без Swarm (это лимиты cgroups). Сумма лимитов памяти (~5.2 GB) оставляет запас ОС и pagecache на 8-гиговом VPS.
- `--forwarded-allow-ips "*"` у uvicorn безопасен, потому что порт 8000 не публикуется наружу — до api достаёт только nginx по сети `edge`.
- Alembic-миграции **не** запускаются автоматом при старте контейнера — только явным шагом деплоя (см. 5.3). Это осознанно: автоматическая миграция при каждом рестарте — источник гонок при `--scale worker=3`.

### 2.3. `Dockerfile` (api/worker/scheduler — один образ)

```dockerfile
# ---------- builder ----------
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# слой зависимостей кэшируется отдельно от кода
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY alembic.ini ./
COPY alembic ./alembic
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# ---------- runtime ----------
FROM python:3.12-slim

RUN groupadd -r leadchat && useradd -r -g leadchat -d /app leadchat \
 && mkdir -p /var/leadchat/media && chown leadchat:leadchat /var/leadchat/media

WORKDIR /app
COPY --from=builder --chown=leadchat:leadchat /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    TZ=UTC

USER leadchat
EXPOSE 8000

# команда задаётся в compose (uvicorn / arq / scheduler)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 2.4. `Dockerfile.nginx` (фронтенд + nginx)

```dockerfile
# ---------- frontend build ----------
FROM node:22-slim AS front

WORKDIR /front
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

COPY frontend/ ./
ARG VITE_SENTRY_DSN
ARG VITE_APP_VERSION
ENV VITE_SENTRY_DSN=$VITE_SENTRY_DSN VITE_APP_VERSION=$VITE_APP_VERSION
RUN npm run build            # -> /front/dist

# ---------- nginx ----------
FROM nginx:1.27-alpine

RUN rm /etc/nginx/conf.d/default.conf
COPY deploy/nginx/nginx.conf /etc/nginx/nginx.conf
COPY deploy/nginx/conf.d/leadchat.conf /etc/nginx/conf.d/leadchat.conf
COPY --from=front /front/dist /usr/share/nginx/html
```

---

## 3. nginx

Два файла: базовый `nginx.conf` (глобальные настройки, зоны rate-limit) и виртуальный хост `conf.d/leadchat.conf`.

### 3.1. `deploy/nginx/nginx.conf`

```nginx
user  nginx;
worker_processes  auto;
error_log  /var/log/nginx/error.log warn;
pid        /var/run/nginx.pid;

events {
    worker_connections 4096;
}

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;

    # --- логи: JSON, чтобы складывались в общий пайплайн (см. раздел 7.3) ---
    log_format json_combined escape=json
      '{"ts":"$time_iso8601","remote_addr":"$remote_addr",'
      '"method":"$request_method","uri":"$uri","status":$status,'
      '"bytes":$body_bytes_sent,"rt":$request_time,'
      '"upstream_rt":"$upstream_response_time","ua":"$http_user_agent"}';
    access_log /var/log/nginx/access.log json_combined;

    sendfile           on;
    tcp_nopush         on;
    keepalive_timeout  65;
    server_tokens      off;

    gzip               on;
    gzip_types         text/plain text/css application/json application/javascript
                       image/svg+xml application/wasm;
    gzip_min_length    1024;

    # --- зоны rate-limit (сами limit_req — в locations) ---
    # логин: брутфорс-защита; 10 попыток/мин с одного IP — вровень с прикладным
    # лимитом 01-API-SPEC §1.7 (прикладной отдаёт 403 account_locked, nginx-зона —
    # страховка от явного флуда, 429)
    limit_req_zone $binary_remote_addr zone=login:10m   rate=10r/m;
    # вебхуки: защита от флуда в публичный endpoint; лимит щедрый —
    # это не защита от Авито, а защита от сканеров/ботов
    limit_req_zone $binary_remote_addr zone=hooks:10m   rate=50r/s;
    # общий API: страховка от залипшего клиента/скрипта
    limit_req_zone $binary_remote_addr zone=api:10m     rate=30r/s;
    limit_req_status 429;

    # WebSocket: карта для заголовка Connection
    map $http_upgrade $connection_upgrade {
        default upgrade;
        ''      close;
    }

    include /etc/nginx/conf.d/*.conf;
}
```

### 3.2. `deploy/nginx/conf.d/leadchat.conf`

```nginx
upstream leadchat_api {
    server api:8000;
    keepalive 32;
}

# ---------- HTTP: только ACME и редирект ----------
server {
    listen 80;
    server_name chat.partner-lead-centre.ru;

    location /.well-known/acme-challenge/ {
        root /var/www/certbot;
    }

    location / {
        return 301 https://$host$request_uri;
    }
}

# ---------- HTTPS ----------
server {
    listen 443 ssl;
    http2 on;
    server_name chat.partner-lead-centre.ru;

    ssl_certificate     /etc/letsencrypt/live/chat.partner-lead-centre.ru/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/chat.partner-lead-centre.ru/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_ciphers         ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES256-GCM-SHA384:ECDHE-RSA-AES256-GCM-SHA384:ECDHE-ECDSA-CHACHA20-POLY1305:ECDHE-RSA-CHACHA20-POLY1305;
    ssl_prefer_server_ciphers off;
    ssl_session_cache   shared:SSL:10m;
    ssl_session_timeout 1d;

    # HSTS: включаем сразу, preload — после месяца стабильной работы
    add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff always;
    add_header X-Frame-Options DENY always;
    add_header Referrer-Policy strict-origin-when-cross-origin always;

    # дефолтный лимит тела: обычные API-запросы маленькие
    client_max_body_size 2m;

    # ---------- API ----------
    location /api/ {
        limit_req zone=api burst=60 nodelay;

        proxy_pass http://leadchat_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout  60s;
        proxy_send_timeout  60s;
    }

    # логин: жёсткий rate-limit (дублирует лимит на бэкенде, см. DESIGN.md §9 / 01 §1.7)
    location = /api/v1/auth/login {
        limit_req zone=login burst=10;

        proxy_pass http://leadchat_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
    }

    # вебхуки Авито: свой rate-limit + лимит тела 1 МБ (DESIGN.md §9)
    location /api/hooks/ {
        limit_req zone=hooks burst=100 nodelay;
        client_max_body_size 1m;

        proxy_pass http://leadchat_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        # вебхук обязан отвечать быстро; если api завис — лучше быстрый 504,
        # Авито ретраит, а reconciliation-поллинг всё равно догонит
        proxy_read_timeout 10s;
    }

    # загрузка вложений менеджером: POST /api/v1/media (multipart, 01 §6.5), лимит 20 МБ
    location = /api/v1/media {
        client_max_body_size 20m;

        proxy_pass http://leadchat_api;
        proxy_http_version 1.1;
        proxy_set_header Connection "";
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_request_buffering off;      # стримим тело сразу в api
    }

    # ---------- WebSocket ----------
    location /api/ws {
        access_log off;                   # одноразовый тикет в query — в логах ему делать нечего (01 §11.1)
        proxy_pass http://leadchat_api;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-Proto https;
        # держим соединение: клиент шлёт прикладной ping каждые 25 сек, сервер закрывает
        # сокет после 60 сек тишины (01-API-SPEC §11.5) — таймаут nginx с запасом
        proxy_read_timeout  90s;
        proxy_send_timeout  90s;
    }

    # ---------- Media: подписанные ссылки (DESIGN §1.4, 01 §6.1/§6.5) ----------
    # Файлы отдаёт сам nginx, без похода в Python: API после RBAC-проверки диалога
    # кладёт в attachments[].url подписанную ссылку
    #   /api/v1/media/{relpath}?sig=<md5>&exp=<unix_ts>   (TTL 1 час, 01 §6.1)
    # Тем же механизмом подписываются файлы экспорта статистики (06-STATS-REPORTS §4.5, TTL 24 ч).
    location /api/v1/media/ {
        secure_link $arg_sig,$arg_exp;
        # MEDIA_SIGN_KEY подставляется из .env при сборке образа (envsubst);
        # тот же ключ использует signed_media_url() в API (см. 3.3)
        secure_link_md5 "$secure_link_expires$uri MEDIA_SIGN_KEY";
        if ($secure_link = "")  { return 403; }   # подписи нет или битая
        if ($secure_link = "0") { return 410; }   # ссылка истекла — фронт перезапросит деталь
        alias /var/leadchat/media/;
        add_header Cache-Control "private, max-age=3600";
        types { }                              # не угадываем mime,
        default_type application/octet-stream; # Content-Type ставится по расширению в API-ссылке
    }
    # Альтернатива — X-Accel-Redirect (поход в FastAPI на каждый файл ради проверки JWT).
    # Выбраны подписанные ссылки — решение DESIGN §1.4: доступ к вложению выдаётся вместе
    # с диалогом (ссылки генерирует API уже после RBAC-проверки), короткий TTL ограничивает
    # окно утечки, а раздача файлов вообще не нагружает Python.

    # ---------- /download: инсталлятор десктопа + манифест автообновления ----------
    # Раскладку каталога пишет CI десктопа (04-DESKTOP §6.1): NSIS — основной канал
    # (стабильное имя-симлинк LeadChat-Setup.exe), MSI — только для GPO/Intune.
    location = /download { return 302 /download/LeadChat-Setup.exe; }
    location /download/ {
        # /download/latest.json — манифест tauri-updater
        # /download/LeadChat_x.y.z_x64-setup.exe (+ .sig), *.msi — версионированные файлы
        alias /var/leadchat/download/;
        autoindex off;
        location ~ \.json$ { add_header Cache-Control "no-cache"; }   # latest.json всегда свежий
        location ~ \.(exe|msi|sig)$ { add_header Cache-Control "public, max-age=3600"; }
    }

    # ---------- React-статика ----------
    # хэшированные ассеты Vite: кэшируем навсегда
    location /assets/ {
        root /usr/share/nginx/html;
        add_header Cache-Control "public, max-age=31536000, immutable";
        try_files $uri =404;
    }

    # SPA-fallback: любые маршруты (/chats/123, /settings/…) -> index.html
    location / {
        root /usr/share/nginx/html;
        try_files $uri /index.html;
        add_header Cache-Control "no-cache";   # index.html всегда свежий
    }
}
```

### 3.3. Генерация подписанных ссылок в FastAPI (для полноты картины)

```python
# app/core/media_sign.py — формат в точности тот, что проверяет secure_link_md5 из 3.2
import base64, hashlib, time
from app.core.config import settings

def signed_media_url(relpath: str, ttl: int = 3600) -> str:
    exp = int(time.time()) + ttl
    raw = f"{exp}/api/v1/media/{relpath} {settings.media_sign_key}"
    sig = base64.urlsafe_b64encode(hashlib.md5(raw.encode()).digest()).rstrip(b"=").decode()
    return f"/api/v1/media/{relpath}?sig={sig}&exp={exp}"
```

Ссылки генерирует API в момент сериализации `attachments[].url` (01 §6.1) — уже после
RBAC-проверки доступа к диалогу; загрузка вложений — `POST /api/v1/media` (multipart,
лимит 20 МБ — 01 §6.5 и `client_max_body_size` в 3.2, значения обязаны совпадать).
Раскладка файлов в `/var/leadchat/media`: `YYYY/MM/<2 hex>/<uuid>.<ext>` — по месяцам (совпадает с партициями `messages`, удобно архивировать) и с fan-out по первым hex-символам, чтобы не упереться в миллион файлов в одном каталоге.

---

## 4. Переменные окружения

`.env.example` лежит в корне репозитория; на сервере копируется в `/opt/leadchat/.env` (chmod 600) и заполняется вручную. **В git реальный `.env` не попадает никогда.**

```bash
# ============================================================
# LeadChat — переменные окружения (прод)
# Скопируйте в .env и заполните. Генерация секретов:
#   openssl rand -base64 32
# ============================================================

# --- Общее ---
ENV=production                    # production | staging | development
DOMAIN=chat.partner-lead-centre.ru
LOG_LEVEL=INFO                    # DEBUG в проде запрещён (утечёт лишнее в логи)
TZ=UTC                            # всё в UTC; отображение МСК — забота фронтенда

# --- Образы (подставляет CI при деплое) ---
IMAGE_TAG=latest                  # тег = короткий git SHA, например a1b2c3d

# --- PostgreSQL ---
POSTGRES_DB=leadchat
POSTGRES_USER=leadchat
POSTGRES_PASSWORD=CHANGE_ME       # openssl rand -base64 24
# async-драйвер обязателен (SQLAlchemy 2 async, DESIGN.md §2)
DATABASE_URL=postgresql+asyncpg://leadchat:CHANGE_ME@postgres:5432/leadchat
DB_POOL_SIZE=10                   # на процесс; api(2 воркера)+worker+scheduler < max_connections=120

# --- Redis ---
REDIS_URL=redis://redis:6379/0
# один инстанс, номера "логических" применений фиксируем префиксами ключей,
# НЕ разными db (Streams+PubSub+кэш живут в db 0 — так проще мониторить)

# --- Авито OAuth (кабинет разработчика developers.avito.ru) ---
AVITO_CLIENT_ID=CHANGE_ME
AVITO_CLIENT_SECRET=CHANGE_ME
# базовый URL API — параметром, чтобы в dev/тестах подменять на мок (07-TESTING-SECURITY.md)
AVITO_API_BASE=https://api.avito.ru
# базовый URL страницы OAuth-авторизации — тоже параметром: в тестах подменяется
# на страницу-заглушку fake-avito (07-TESTING-SECURITY.md §2)
AVITO_AUTH_URL=https://avito.ru/oauth

# --- Шифрование токенов Авито (DESIGN.md §1.5) ---
# 32 байта base64 для AES-256-GCM. Потеря ключа = переподключение ВСЕХ аккаунтов
# через OAuth (данные в БД не теряются). Хранить копию в менеджере секретов компании.
TOKEN_ENC_KEY=CHANGE_ME           # openssl rand -base64 32

# --- Auth (DESIGN.md §9) ---
JWT_SECRET=CHANGE_ME              # openssl rand -base64 64; подпись access-JWT (HS256)
JWT_ACCESS_TTL_SECONDS=900        # 15 минут
REFRESH_TTL_DAYS=14
# SSO с основным сайтом (задел, DESIGN.md §6). Пусто = SSO выключено.
# SSO_SHARED_SECRET — снято 23.08.2026 вместе с полем конфигурации: у задела нет кода (DESIGN §6)

# --- AI (боты, DESIGN.md §4.5) ---
ANTHROPIC_API_KEY=CHANGE_ME
# ЕДИНСТВЕННОЕ отличие прод-окружения от dev во всей AI-подсистеме.
# С петербургского сервера api.anthropic.com отвечает 403, поэтому в проде тут
# стоит адрес прокси на нидерландском сервере (http://<IP наблюдателя>:8080).
# Переменную получают api и worker — они читают один и тот же .env
# (x-app-common в docker-compose.prod.yml); реальные вызовы идут только из
# воркера. В коде значение уходит в AsyncAnthropic(base_url=...) — app/bots/ai.py.
# Недоступность прокси бот переживает: шаг ai_answer делает handoff(ai_unavailable)
# и оставляет в диалоге заметку «AI недоступен», а админ видит bot.ai_call_failed
# в логах и Sentry (раздел 8).
ANTHROPIC_BASE_URL=https://api.anthropic.com
AI_MODEL_ANSWER=claude-sonnet-5         # шаг ai_answer
AI_MODEL_CLASSIFY=claude-haiku-4-5      # классификация негатива, извлечение сущностей
AI_TIMEOUT_SECONDS=10                   # недоступность AI не блокирует доставку
AI_MAX_TOKENS_ANSWER=1024
AI_MAX_TOKENS_CLASSIFY=256
AI_FAKE=0                               # 1 — детерминированная заглушка без сети (CI/демо)

# --- Media ---
MEDIA_ROOT=/var/leadchat/media
MEDIA_MAX_SIZE_MB=20              # лимит 01 §6.5; client_max_body_size в nginx — то же значение
MEDIA_SIGN_KEY=CHANGE_ME          # подпись ссылок secure_link (раздел 3.2); openssl rand -hex 16

# --- Мониторинг ---
SENTRY_DSN=                       # DSN проекта leadchat-backend (api+worker+scheduler)
SENTRY_TRACES_SAMPLE_RATE=0.1
# push-URL монитора Uptime-Kuma для канарейки вебхуков (раздел 7.2)
KUMA_WEBHOOK_CANARY_URL=
# Telegram-алерты из скриптов (backup.sh и т.п.): бот + chat_id админ-чата
ALERT_TG_BOT_TOKEN=
ALERT_TG_CHAT_ID=

# --- Бэкапы (читает deploy/backup.sh) ---
BACKUP_DIR=/var/leadchat/backups  # каталог должен быть смонтирован в postgres как /backups
BACKUP_KEEP_DAILY=14              # локальная ротация: 14 суточных дампов
# ВНИМАНИЕ: имя бакета у S3-провайдера может быть не человеческим (Timeweb
# выдаёт UUID). Значение обязано существовать: rclone с no_check_bucket=true
# бакет не создаёт, а отвечает NoSuchBucket 404 — и офсайт-копии просто нет.
# Проверка: rclone lsd offsite:  ->  увидеть свой бакет в списке.
RCLONE_REMOTE=offsite:<имя-бакета>       # настроенный remote rclone (S3-совместимый)

# --- Тюнинг ---
WEB_CONCURRENCY=2                 # uvicorn-воркеров в контейнере api
ARQ_MAX_JOBS=50                   # параллельных задач в worker
RECONCILE_INTERVAL_SECONDS=300    # reconciliation-поллинг (DESIGN.md §1.3)
```

Фронтенду переменные передаются **на этапе сборки** (Vite инлайнит `VITE_*`): `VITE_SENTRY_DSN`, `VITE_APP_VERSION` — это build-args в `Dockerfile.nginx`, задаёт CI. Runtime-конфига у SPA нет: API на том же домене под `/api`, ничего настраивать не нужно.

Секреты в GitHub Actions (Settings → Secrets): `GHCR_TOKEN` (или стандартный `GITHUB_TOKEN` с правом packages), `VPS_SSH_KEY`, `VPS_HOST`, `VPS_USER`, `SENTRY_DSN_FRONTEND`, `SENTRY_AUTH_TOKEN` (загрузка sourcemaps), `TAURI_SIGNING_PRIVATE_KEY` (см. 04-DESKTOP.md).

---

## 5. CI/CD

Три workflow: `ci.yml` (тесты на каждый PR/push), `deploy.yml` (сборка образов и выкладка), `desktop-release.yml` (инсталляторы десктопа — описан в 04-DESKTOP.md §7, здесь не дублируем).

### 5.1. `.github/workflows/ci.yml`

```yaml
name: CI
on:
  pull_request:
  push:
    branches: [main]

jobs:
  backend:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_DB: leadchat_test
          POSTGRES_USER: test
          POSTGRES_PASSWORD: test
        ports: ["5432:5432"]
        options: >-
          --health-cmd "pg_isready -U test" --health-interval 5s
          --health-timeout 5s --health-retries 10
      redis:
        image: redis:7-alpine
        ports: ["6379:6379"]
        options: >-
          --health-cmd "redis-cli ping" --health-interval 5s
          --health-timeout 3s --health-retries 10
    env:
      DATABASE_URL: postgresql+asyncpg://test:test@localhost:5432/leadchat_test
      REDIS_URL: redis://localhost:6379/0
      TOKEN_ENC_KEY: dGVzdC1rZXktMzItYnl0ZXMtdGVzdC1rZXktMzIhIQ==
      JWT_SECRET: test-secret
      AVITO_API_BASE: http://localhost:9    # реальный Авито в тестах недостижим намеренно
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
        with: { version: "0.5.x" }
      - run: uv sync --frozen
      - run: uv run ruff check app tests
      - run: uv run ruff format --check app tests
      - run: uv run mypy app
      - run: uv run alembic upgrade head        # миграции применяются с нуля — это тоже тест
      - run: uv run pytest -q --cov=app --cov-fail-under=75

  frontend:
    runs-on: ubuntu-latest
    defaults: { run: { working-directory: frontend } }
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: { node-version: 22, cache: npm, cache-dependency-path: frontend/package-lock.json }
      - run: npm ci
      - run: npm run lint                        # eslint
      - run: npx tsc --noEmit
      - run: npm run test -- --run               # vitest
      - run: npm run build                       # сборка тоже обязана проходить в CI
```

### 5.2. `.github/workflows/deploy.yml`

Деплой — по пушу тега `v*` (релиз) или вручную (`workflow_dispatch`) для hotfix из main.

```yaml
name: Deploy
on:
  push:
    tags: ["v*"]
  workflow_dispatch:

concurrency: deploy-production      # два деплоя одновременно не поедут

jobs:
  build-push:
    runs-on: ubuntu-latest
    permissions: { contents: read, packages: write }
    outputs:
      tag: ${{ steps.meta.outputs.tag }}
    steps:
      - uses: actions/checkout@v4
      - id: meta
        run: echo "tag=$(git rev-parse --short HEAD)" >> "$GITHUB_OUTPUT"
      - uses: docker/login-action@v3
        with:
          registry: ghcr.io
          username: ${{ github.actor }}
          password: ${{ secrets.GITHUB_TOKEN }}
      - uses: docker/setup-buildx-action@v3
      - name: Build & push api
        uses: docker/build-push-action@v6
        with:
          context: .
          file: Dockerfile
          push: true
          tags: |
            ghcr.io/lead-partner/leadchat-api:${{ steps.meta.outputs.tag }}
            ghcr.io/lead-partner/leadchat-api:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max
      - name: Build & push nginx (frontend внутри)
        uses: docker/build-push-action@v6
        with:
          context: .
          file: Dockerfile.nginx
          push: true
          build-args: |
            VITE_SENTRY_DSN=${{ secrets.SENTRY_DSN_FRONTEND }}
            VITE_APP_VERSION=${{ steps.meta.outputs.tag }}
          tags: |
            ghcr.io/lead-partner/leadchat-nginx:${{ steps.meta.outputs.tag }}
            ghcr.io/lead-partner/leadchat-nginx:latest
          cache-from: type=gha
          cache-to: type=gha,mode=max

  deploy:
    needs: build-push
    runs-on: ubuntu-latest
    environment: production          # required reviewers — по желанию
    steps:
      - uses: webfactory/ssh-agent@v0.9.0
        with:
          ssh-private-key: ${{ secrets.VPS_SSH_KEY }}
      - name: Deploy
        run: |
          ssh -o StrictHostKeyChecking=accept-new \
            ${{ secrets.VPS_USER }}@${{ secrets.VPS_HOST }} \
            "/opt/leadchat/bin/deploy.sh ${{ needs.build-push.outputs.tag }}"
```

### 5.3. `/opt/leadchat/bin/deploy.sh` и стратегия миграций

```bash
#!/usr/bin/env bash
# deploy.sh <image_tag> — вызывается CI по ssh. Идемпотентен.
set -euo pipefail
cd /opt/leadchat
TAG="${1:?usage: deploy.sh <image_tag>}"

echo "== deploy ${TAG} =="
sed -i "s/^IMAGE_TAG=.*/IMAGE_TAG=${TAG}/" .env

docker compose pull --quiet

# 1. Миграции — ДО обновления кода, старые контейнеры ещё работают.
#    Поэтому миграции обязаны быть expand-contract (см. ниже).
docker compose run --rm --no-deps api alembic upgrade head

# 2. Фоновые процессы: короткий простой не виден пользователям —
#    очередь в Redis Streams подождёт, недоставленные вебхуки догонит consumer group.
docker compose up -d worker scheduler

# 3. API: пересоздание контейнера ~5–10 сек. TanStack Query на фронте ретраит
#    запросы, WebSocket переподключается сам (03-FRONTEND.md) — для внутреннего
#    инструмента этого достаточно. Вебхуки Авито в эту щель получат 502 —
#    Авито ретраит, плюс reconciliation-поллинг закрывает остаток (DESIGN.md §1.3).
docker compose up -d api

# 4. nginx (новая статика фронтенда). Пересоздание nginx рвёт TCP-коннекты
#    на ~1 сек; браузеры молча повторяют.
docker compose up -d nginx

# 5. Smoke: не вышли на health за 60 сек — откат на предыдущий тег руками
#    (см. rollback ниже) и алерт.
for i in $(seq 1 12); do
  sleep 5
  if curl -fsS https://chat.partner-lead-centre.ru/api/health >/dev/null; then
    echo "== OK: ${TAG} =="
    exit 0
  fi
done
echo "!! health-check failed after deploy ${TAG}" >&2
exit 1
```

**Правила zero-downtime миграций (expand-contract), обязательны к соблюдению:**

1. Одна миграция — только *добавляющие* изменения (новая таблица, nullable-колонка, индекс `CONCURRENTLY`). Старый код должен работать на новой схеме.
2. Удаление/переименование колонки — минимум через один релиз после того, как код перестал её использовать (expand в релизе N, contract в N+1).
3. Backfill данных — не в миграции, а ARQ-задачей после деплоя (миграция с `UPDATE` на миллионы строк messages повесит таблицу).
4. `CREATE INDEX CONCURRENTLY` — в миграции с `autocommit` (Alembic: `op.get_bind().execution_options(isolation_level="AUTOCOMMIT")`).
5. Новая месячная партиция `messages` создаётся не миграцией, а scheduler'ом (за 7 дней до начала месяца) — деплой не должен быть условием работоспособности записи.

**Откат**: `./bin/deploy.sh <предыдущий_tag>` — образы в GHCR никогда не перезаписываются (тег = SHA). Откат схемы БД не делаем (вниз-миграции в проде запрещены); благодаря expand-contract старый код работает на новой схеме.

---

## 6. Бэкапы

> **Фактическая раскладка боевого сервера (проверено 2026-08-05).** Каталог
> деплоя — `/srv/leadchat` (не `/opt/leadchat`), скрипты лежат в
> `/srv/leadchat/deploy/`, дампы — в `/var/leadchat/backups`, офсайт-бакет
> Timeweb S3 называется UUID-ом (`offsite:afbac24b-221c-44f1-bd88-6df6fd6c65ba`),
> человеческих имён их S3 не даёт. TZ сервера — `Europe/Moscow`, поэтому cron
> считает по МСК, а скрипты печатают время в UTC: ночной прогон `30 3` МСК
> виден в логе как `00:30Z`. Пути и имена в примерах ниже приведены к этой
> реальности; при переезде на другой сервер меняются `DEPLOY_DIR`,
> `COMPOSE_FILE` и `RCLONE_REMOTE` в crontab (Приложение А), а не скрипты.

### 6.1. Что бэкапим

| Что | Как | Частота | Ротация | Офсайт |
|---|---|---|---|---|
| PostgreSQL | `pg_dump -Fc` (custom, сжатый) | ежедневно 03:30 МСК (00:30 UTC) | 14 суточных локально; офсайт хранит 30 суточных + 12 месячных | rclone → S3-совместимое хранилище |
| Media (`/var/leadchat/media`) | `rclone sync` тем же прогоном, сразу после дампа (инкрементально по сути — файлы иммутабельны) | ежедневно 03:30 МСК | зеркало (мы не удаляем media, кроме media-gc) | тот же remote |
| `.env` и конфиги `/srv/leadchat` | вручную в менеджер секретов компании при каждом изменении | по факту изменения | — | менеджер секретов |
| Релизы десктопа (`/var/leadchat/download`) | пересобираемы из git-тега | — | — | не нужно |

### 6.2. `deploy/backup.sh` (cron: `30 3 * * *` от пользователя деплоя)

Рабочая версия — в репозитории (`deploy/backup.sh`), ниже — её логика и те
места, где «очевидное» решение оказалось неверным.

```bash
# 0. Диск: <15% свободного — алерт, <5% — не начинаем дамп (§7.4)
# 1. Дамп: -Fc уже сжат (zlib), содержит и схему, и данные, восстанавливается pg_restore
docker compose … exec -T postgres pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
  -Fc --no-owner -f "/backups/${DUMP}"

# 2. Санити — СТРУКТУРНАЯ, а не по размеру:
#    - размер больше MIN_DUMP_BYTES (20 000: страховка от обрезанного файла);
#    - pg_restore --list читается и в оглавлении >= 50 записей;
#    - в оглавлении есть TABLE DATA по users / avito_accounts / conversations /
#      messages (у messages совпадение по префиксу: она партиционирована, и в
#      оглавлении лежат messages_y2026m08 и т.д.);
#    - дамп вдвое меньше предыдущего -> предупреждение (не падение).
# 3. Офсайт: rclone copy; 1-го числа — ещё и в db-monthly/
# 4. Media: rclone sync, но ТОЛЬКО если в MEDIA_ROOT есть файлы, и с --max-delete
# 5. Ротация: локально find -mtime +14 -delete; офсайт rclone delete --min-age 30d
```

Замечания (часть — оплачена шишками на этом самом сервере):

- **Порог размера не должен быть абсолютной константой в мегабайтах.** До
  запуска команды дамп пустой схемы весит ~32 КБ, и порог «1 МБ» из первой
  редакции валил каждый ночной прогон на шаге 2 — дамп создавался, но офсайт
  и ротация не выполнялись ни разу. Содержательную проверку делает оглавление;
  размер оставлен только как грубая страховка от обрезанного файла.
- **Скрипт обязан кричать в лог, а не только в Telegram.** `ALERT_TG_*` могут
  быть пустыми (сейчас так и есть), и тогда `alert()` — no-op: падение выглядит
  ровно как отсутствие падения. Поэтому каждый провал пишет строку
  `!! ПРОВАЛ: …` в `/var/log/leadchat-backup.log`.
- **`rclone sync` с пустого источника стирает офсайт.** Если `MEDIA_ROOT` пуст
  или не смонтирован, `sync` честно приводит удалённую копию к пустому виду.
  Поэтому: пустой источник — пропускаем шаг, непустой — синхронизируем с
  `--max-delete ${MEDIA_MAX_DELETE:-50}`.
- **Класс хранения per-object Timeweb S3 не принимает** (`InvalidArgument 400`):
  `--s3-storage-class STANDARD_IA` из первой редакции убран, класс задаётся на
  уровне бакета в панели.
- `pg_dump` на наших объёмах (~10 000 сообщений/сутки, партиции по месяцам) — минуты; на горизонте 2–3 лет останется в пределах получаса. Переход на `pgBackRest`/WAL-архив имеет смысл, только если RPO «сутки» перестанет устраивать — для внутреннего инструмента он приемлем.
- Remote `offsite:` настраивается `rclone config` один раз (любой S3-совместимый провайдер, географически отдельный от VPS). Ключи rclone лежат в `~/.config/rclone/rclone.conf`, chmod 600. Бакет Timeweb называется UUID-ом — имя задаётся не нами, поэтому `RCLONE_REMOTE` прописан в crontab (Приложение А) и там же правится.
- Операционные переменные (`DEPLOY_DIR`, `COMPOSE_FILE`, `RCLONE_REMOTE`, `BACKUP_DIR`), заданные в окружении, **приоритетнее** `.env`: так их правят, не открывая файл с секретами.
- Тестовые/стейджинговые дампы в тот же bucket не кладём.

### 6.3. Проверка восстановления — ежемесячно, автоматически

Бэкап, который ни разу не восстанавливали, — это лотерейный билет. `deploy/backup-verify.sh` (cron: `0 6 5 * *` — 5-го числа):

> **Прогон 2026-08-05 (факт, не план).** Дамп скачан из S3 через `rclone`
> (побайтово совпал с локальным по md5), восстановлен в отдельный контейнер
> `postgres:16-alpine` на `127.0.0.1:15433` — боевой стек не трогали. Совпали:
> 12 таблиц, 52 индекса, 11 внешних ключей, 2 партиции `messages`, версия
> alembic `0004`, а также md5 содержимого `users`, `messages`, `conversations`
> — с боевой базой один в один. Восстановление БД заняло **3 секунды**
> (при пустой базе; целевой RTO сервиса — 2 часа). Расхождение было только по
> `audit_log` (63 против 79) — в базу писали во время проверки, для суточного
> дампа это ожидаемо.
>
> Две ловушки, найденные тем же прогоном и уже закрытые в скриптах:
> одноразовый `postgres` оставлял после себя анонимный том на ~80 МБ
> (`docker run --rm` его не удаляет) — теперь контейнер снимается
> `docker rm -f -v` по trap'у; и `rclone check` для media ругался на
> «расхождение с офсайтом» там, где вложений ещё просто нет.

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /srv/leadchat
set -a; source .env; set +a

LATEST=$(ls -1t "${BACKUP_DIR}"/db_*.dump | head -1)

# поднимаем одноразовый postgres, восстанавливаем, гоняем санити-SQL.
# Без --rm намеренно: контейнер снимает trap'ом `docker rm -f -v`, иначе
# анонимный том с PGDATA (~80 МБ) остаётся на диске после каждого прогона.
docker run -d --name pg_verify -e POSTGRES_PASSWORD=verify \
  -v "${BACKUP_DIR}:/backups:ro" postgres:16-alpine
until docker exec pg_verify pg_isready -U postgres >/dev/null 2>&1; do sleep 2; done

docker exec pg_verify createdb -U postgres verify
docker exec pg_verify pg_restore -U postgres -d verify --no-owner "/backups/$(basename "$LATEST")"

RESULT=$(docker exec pg_verify psql -U postgres -d verify -tAc "
  SELECT 'users='   || (SELECT count(*) FROM users)          || ' ' ||
         'convs='   || (SELECT count(*) FROM conversations)  || ' ' ||
         'msgs='    || (SELECT count(*) FROM messages)       || ' ' ||
         'last_msg='|| coalesce((SELECT max(created_at)::date::text FROM messages), 'none')")
docker rm -f -v pg_verify   # -v: иначе останется анонимный том с PGDATA

# свежесть: последнее сообщение в дампе не старше 2 суток
LAST=$(echo "$RESULT" | grep -o 'last_msg=[0-9-]*' | cut -d= -f2)
if [ -z "$LAST" ] || [ "$(date -d "$LAST" +%s)" -lt "$(date -d '2 days ago' +%s)" ]; then
  curl -fsS "https://api.telegram.org/bot${ALERT_TG_BOT_TOKEN}/sendMessage" \
    -d chat_id="${ALERT_TG_CHAT_ID}" \
    -d text="🔴 backup-verify: восстановление подозрительно (${RESULT})" >/dev/null
  exit 1
fi
curl -fsS "https://api.telegram.org/bot${ALERT_TG_BOT_TOKEN}/sendMessage" \
  -d chat_id="${ALERT_TG_CHAT_ID}" -d text="✅ backup-verify OK: ${RESULT}" >/dev/null
```

Раз в квартал — ручная «пожарная учение»: полное восстановление на чистой VM по инструкции (docker compose + .env из менеджера секретов + последний дамп + rclone media), замер времени. Целевой RTO — 2 часа. Скрипт учения — `deploy/restore-check.sh` (печатает отчёт для тикета); запускать так:

```bash
DEPLOY_DIR=/srv/leadchat /srv/leadchat/deploy/restore-check.sh
# или по конкретному дампу, в том числе скачанному из офсайта:
rclone copy offsite:<bucket>/db/db_YYYYMMDD_HHMM.dump /tmp/drill/
DEPLOY_DIR=/srv/leadchat BACKUP_DIR=/tmp/drill /srv/leadchat/deploy/restore-check.sh
```

### 6.4. Что НЕ бэкапим и почему

- **Redis** — весь контент восстановим или одноразов:
  - очередь `webhooks:avito` (Streams): недообработанное догонит reconciliation-поллинг по `unread_only=true` (DESIGN.md §1.3), идемпотентность по `external_message_id` защищает от дублей;
  - Pub/Sub — эфемерный по определению;
  - кэш, rate-limit-счётчики, OAuth-`state` — одноразовые с TTL;
  - denylist refresh-JWT — при потере худшее последствие: разлогиненные сессии живут до конца TTL (14 дней); принимаем риск, при желании можно принудительно ротировать `JWT_SECRET` (разлогинит всех).
  AOF включён для переживания *рестарта*, не для disaster recovery.
- **pgdata как файлы** — бэкап файлов работающего Postgres без WAL-механики даёт битые копии; только `pg_dump`.
- **Образы Docker** — в GHCR, теги иммутабельны.
- **Токены Авито отдельно от БД** — они и так в дампе (зашифрованные); без `TOKEN_ENC_KEY` бесполезны, поэтому ключ хранится в менеджере секретов, а не рядом с дампами.

---

## 7. Мониторинг и логи

### 7.1. Sentry

Один аккаунт (или self-hosted), три проекта: `leadchat-backend`, `leadchat-frontend`, `leadchat-desktop`.

```python
# app/core/observability.py — общая инициализация для api, worker, scheduler
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.asyncpg import AsyncPGIntegration

def init_sentry(settings, component: str):        # component: api|worker|scheduler
    if not settings.sentry_dsn:
        return
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.env,
        release=settings.image_tag,               # тег образа = git SHA
        traces_sample_rate=settings.sentry_traces_sample_rate,
        integrations=[FastApiIntegration(), AsyncPGIntegration()],
        # секреты не должны утечь в события
        before_send=scrub_secrets,                # тот же фильтр, что у логгера (7.3)
    )
    sentry_sdk.set_tag("component", component)
```

**Состояние на 2026-08-05 (проверено на проде).** `SENTRY_DSN` пуст — Sentry
выключен, и это не ломает ничего: `init_sentry()` в api/worker/scheduler
возвращает `False`, пишет `sentry.disabled` в debug и идёт дальше. С заданным
DSN одноразовый контейнер поднял клиент за миллисекунды, подтянул интеграции
`fastapi`, `starlette`, `asyncio`, `asyncpg`, `redis`, `sqlalchemy`, `arq`,
`httpx`, `logging`, а `before_send` затёр `authorization`, cookie, `password`
и `token=` в query-string. Осталось только завести проект и вписать DSN.

Одна оговорка: `release` берётся из `APP_VERSION`/`IMAGE_TAG`, а на этом
сервере образы собираются локально с тегом `prod` — в Sentry все деплои
сольются в один релиз `prod`. Как только появится реестр (или на время без
него — `APP_VERSION=$(git rev-parse --short HEAD)` в `.env`), релизы разъедутся.

- В worker каждая ARQ-задача оборачивается `sentry_sdk.new_scope()` с тегами `job`, `account_id`, `conversation_id` — по событию сразу видно, какой аккаунт/диалог пострадал.
- Frontend: `@sentry/react` с `VITE_SENTRY_DSN`, `release: VITE_APP_VERSION`; sourcemaps загружает CI (`SENTRY_AUTH_TOKEN`), в проде sourcemaps с сервера не раздаются.
- Правила алертов в Sentry: новая ошибка (first seen) → Telegram; более 50 событий одной ошибки за час → Telegram.

### 7.2. Uptime-Kuma

Ставится **не на этот VPS** (иначе умрёт вместе с ним) — на любой другой хост/дешёвую VM. Мониторы:

| Монитор | Тип | Порог/интервал | Что ловит |
|---|---|---|---|
| `https://<домен>/` | HTTPS + cert expiry | 60 c; алерт за 14 дней до истечения серта | nginx, DNS, TLS, доступность VPS |
| `GET /api/health` | HTTP keyword `"status":"ok"` | 60 c | процесс api жив, БД и Redis пингуются |
| `GET /api/health/deep` | HTTP keyword `"status":"ok"` | 5 мин | лаг очереди, протухающие токены, failed-доставки (см. ниже) |
| Webhook-канарейка | **Push-монитор** | grace 30 мин | конвейер вебхуков end-to-end |
| `GET /download/latest.json` | HTTP 200 | 15 мин | раздача десктоп-обновлений |

> **Ключевое слово — только `"status":"ok"`, не `ok`.** Обе ручки всегда
> отвечают HTTP 200 (деградация видна лишь в теле), а подстрока `ok` есть в
> самих именах полей — `webhooks`, `tokens_expiring_2h`. Монитор с keyword `ok`
> был бы зелёным даже при `"status":"degraded"`, то есть бесполезен.

> **Пока Uptime-Kuma негде поднять** её роль исполняет
> `deploy/healthcheck-alert.sh` из cron каждые 5 минут (Приложение А):
> `/api/health` и `/api/health/deep` по ключу `"status":"ok"`, срок
> сертификата, свободное место, свежесть последнего дампа (>30 ч — алерт).
> Антидребезг: сообщение при смене состояния и повтор не чаще раза в час,
> состояние — в `~/.leadchat/health.state`, лог — `/var/log/leadchat-health.log`.
> Ограничение честное и принципиальное: скрипт живёт на том же VPS, поэтому
> смерть самого VPS он не заметит — внешний монитор он не заменяет, а лишь
> закрывает всё остальное до его появления.
> Доставка в Telegram включается заполнением `ALERT_TG_BOT_TOKEN` и
> `ALERT_TG_CHAT_ID`; пока они пусты, скрипт пишет в лог и ничего не шлёт.

**`/api/health`** (без auth, лёгкий — его же использует docker healthcheck):

```json
{ "status": "ok", "db": true, "redis": true, "version": "a1b2c3d" }
```

**`/api/health/deep`** (без auth, но без деталей-имён; агрегаты не секретны):

```json
{
  "status": "ok",                       // "degraded", если любой пункт ниже красный
  "checks_failed": [],                  // имена упавших датчиков (пустой = все опросились)
  "db": true, "redis": true,
  "queue": { "len": 3, "pending": 0, "oldest_pending_sec": 0, "stream_len": 84210 },
  // len = ОТСТАВАНИЕ (lag группы workers + pending), а НЕ длина стрима: Redis
  // Streams не удаляют записи сами, XLEN растёт всё время жизни сервиса и порог
  // HEALTH_QUEUE_LEN_RED на нём срабатывал бы навсегда. stream_len — полная
  // длина, для разбора инцидентов (сам стрим подрезается WEBHOOK_STREAM_MAXLEN).
  "accounts": { "active": 12, "needs_reauth": 0, "disabled": 0, "tokens_expiring_2h": 0 },
  "delivery": { "failed_last_hour": 0 },              // messages.delivery_status='failed'
  "webhooks": { "last_inbound_age_sec": 42 },         // возраст последнего входящего
  "scheduler": { "alive": true, "heartbeat_age_sec": 27 },        // ключ scheduler:alive в Redis
  "pool": { "size": 10, "checkedin": 4, "checkedout": 1, "overflow": -5 }
}
```

Каждый датчик изолирован: упавший отдаёт `null`/дефолт, попадает в
`checks_failed` и красит ответ в `degraded`, но всю ручку не роняет — иначе
мониторинг слепнет ровно тогда, когда он нужнее всего.

Правила «красного»: `queue.len > 1000` или `oldest_pending_sec > 300` или `needs_reauth > 0` или `failed_last_hour > 10` (плюс db/redis/scheduler и любой упавший датчик). Пороги вынесены в настройки (`health_*_red`). Kuma с keyword-проверкой `"status":"ok"` превращает это в алерт без отдельной алерт-системы.

**Webhook-канарейка**: после каждого успешно обработанного входящего события worker дёргает `KUMA_WEBHOOK_CANARY_URL` (push-монитор, не чаще раза в минуту, fire-and-forget). Ночью входящих может не быть — поэтому reconciliation-поллинг (он ходит в Авито каждые 5 минут) тоже пингует канарейку при успешном опросе. Итог: канарейка молчит > 30 минут ⇒ сломан либо приём вебхуков, либо worker, либо доступ к API Авито — во всех трёх случаях надо просыпаться (runbook 8.1).

Уведомления Kuma — в тот же Telegram-чат админов.

### 7.3. Структурные логи

Все три Python-процесса пишут JSON в stdout через `structlog` (ротацию делает Docker: `max-size: 20m × 5`). Формат события — плоский JSON: `ts`, `level`, `event`, `component` + контекстные поля.

Что логируем обязательно:

| Точка | event | Поля |
|---|---|---|
| Вебхук принят (gateway) | `webhook.received` | `account_id`, `bytes`, `stream_id` (id записи в Streams) |
| Вебхук отвергнут | `webhook.rejected` | `account_id`, `reason` (`bad_secret`/`too_large`/`unknown_account`), `remote_addr` |
| Вебхук обработан (worker) | `webhook.processed` | `account_id`, `conversation_id`, `external_message_id`, `duplicate` (bool), `latency_ms` (от `xadd` до коммита) |
| Отправка сообщения | `message.deliver` | `message_id`, `conversation_id`, `account_id`, `attempt`, `status_code`, `outcome` (`delivered`/`retry`/`failed`), `retry_in_sec` |
| Refresh токена | `token.refresh` | `account_id`, `outcome` (`ok`/`needs_reauth`/`lock_busy`), `expires_at` |
| Reconciliation | `reconcile.run` | `account_id`, `chats_checked`, `messages_recovered` |
| AI-вызов | `ai.call` | `model`, `step` (`ai_answer`/`classify`), `conversation_id`, `latency_ms`, `outcome`, `input_tokens`, `output_tokens` |
| Auth | `auth.login` / `auth.failed` | `user_id`/`email_hash`, `remote_addr` |

Чего в логах **не бывает никогда** (фильтр-процессор structlog, он же `before_send` Sentry):

```python
# app/core/logging.py
SECRET_KEYS = {"access_token", "refresh_token", "authorization", "password",
               "secret", "webhook_secret", "api_key", "token", "cookie", "set-cookie"}

def scrub_secrets(logger, method, event_dict):
    for k in list(event_dict):
        if k.lower() in SECRET_KEYS:
            event_dict[k] = "[redacted]"
    # тела сообщений клиентов в логи тоже не пишем (персональные данные):
    # логируем только id и длину, текст живёт в БД
    if "body" in event_dict:
        event_dict["body_len"] = len(event_dict.pop("body") or "")
    return event_dict
```

Сырые payload'ы вебхуков (риск №2 из DESIGN.md §7 — «формат меняется») складываются в таблицу `webhook_raw_log` (jsonb + created_at) с TTL-очисткой scheduler'ом через 30 дней — это данные для отладки, а не логи, и в stdout они не пишутся.

Смотреть логи на сервере: `docker compose logs -f --tail=200 worker | jq -r 'select(.event=="message.deliver")'` — jq поставлен на VPS.

### 7.4. Сводка алертов

| Сигнал | Источник | Канал | Порог |
|---|---|---|---|
| Аккаунт `needs_reauth` | `/api/health/deep` → Kuma; плюс продукт сам уведомляет админов в UI/трей (DESIGN.md §1.5) | Telegram | мгновенно |
| Очередь растёт | `/api/health/deep` (`queue.len`, `oldest_pending_sec`) | Telegram | len>1000 или pending>5 мин |
| Failed-доставки | `/api/health/deep` (`delivery.failed_last_hour`) | Telegram | >10/час |
| Вебхуки молчат | канарейка Kuma | Telegram | 30 мин тишины |
| Ошибки кода | Sentry | Telegram | new issue / 50 в час |
| Серт истекает | Kuma cert check | Telegram | за 14 дней |
| Бэкап упал / восстановление битое | backup.sh / backup-verify.sh | Telegram + строка `!! ПРОВАЛ` в `/var/log/leadchat-backup.log` | по факту |
| Бэкап молчит (нет свежего дампа) | healthcheck-alert.sh | Telegram | дамп старше 30 ч |
| Диск | `node_exporter` не ставим; проверка df в backup.sh перед дампом и в healthcheck-alert.sh каждые 5 мин | Telegram | <15% |

> **Все строки этой таблицы сейчас доставляются «в лог, но не в чат»:**
> `ALERT_TG_BOT_TOKEN` и `ALERT_TG_CHAT_ID` в `.env.prod` пусты, а Sentry и
> Uptime-Kuma ещё не заведены. Скрипты к этому готовы (заполнить две
> переменные — и алерты пойдут), но до запуска команды это обязательный пункт:
> мониторинг, о котором никто не узнаёт, эквивалентен его отсутствию.

---

## 8. Runbook

Общие входные точки диагностики (запомнить наизусть). Все команды ниже
**проверены на боевом сервере 2026-08-05** — копируются и работают как есть:

```bash
cd /srv/leadchat
# Голый `docker compose ps` здесь НЕ работает: файла docker-compose.yml нет,
# прод собран из двух файлов. Поэтому первым делом заводим алиас:
alias dc='docker compose --env-file /srv/leadchat/.env.prod \
  -f /srv/leadchat/docker-compose.prod.yml \
  -f /srv/leadchat/docker-compose.override.yml'

dc ps                                  # кто жив, кто рестартится
dc logs --tail=100 api worker scheduler
curl -sk https://<адрес прода>/api/health/deep | jq   # текущий домен — sslip.io
dc exec -T redis redis-cli XLEN webhooks:avito
dc exec -T redis redis-cli XINFO GROUPS webhooks:avito         # группа называется workers
dc exec -T postgres psql -U leadchat -d leadchat
```

**Чего в этом runbook пока нет — и не надо искать.** Команд
`python -m app.cli avito subscriptions|resubscribe|refresh`, `reconcile`,
`replay-raw`, `replay-dlq` **не существует**: в CLI реализованы только
`create-admin`, `invite`, `seed-smoke`, `sentry-test` (`dc exec -T api python
-m app.cli --help`). Ниже по тексту для каждого такого места указан рабочий
обходной путь. Довести CLI до описанного — отдельная задача, до неё часть
шагов делается через UI и планировщик.

### 8.1. «Вебхуки перестали приходить»

Симптом: канарейка Kuma молчит; менеджеры говорят «новые сообщения не появляются» (или появляются с задержкой до 5 минут — значит, работает только reconciliation, а вебхуки мертвы).

Диагностика сверху вниз — где обрывается конвейер `Авито → nginx → api → Streams → worker → БД`:

1. **Доходят ли запросы до nginx?**
   ```bash
   # access.log внутри контейнера — симлинк на /dev/stdout, поэтому `tail` по
   # файлу молчит ВСЕГДА (и это не значит, что запросов нет). Читаем логи демона:
   dc logs --tail=2000 nginx | grep '/api/hooks/'
   ```
   Формат строки — JSON: `{"ts":…,"remote_addr":…,"method":"POST","uri":"/api/hooks/avito/<id>","status":200,"rt":0.010,"req_id":…}`, поэтому фильтровать по статусу удобно как `grep '"status":403'`.
   - Есть записи со статусом 200 → приём работает, иди к шагу 4 (worker).
   - Есть записи с 403 → секрет не сходится: аккаунт переподключали и `webhook_secret` сменился, а в Авито остался старый URL. Перерегистрировать webhook (шаг 3).
   - Есть 429 → сработал rate-limit зоны `hooks` — смотри, кто флудит (`remote_addr`); если это легитимный всплеск от Авито, поднять `rate` в nginx.conf.
   - Есть 502/504 → api лежит или не отвечает за 10 сек: `dc ps api`, `dc logs api`. Рестарт: `dc restart api`.
   - Записей нет вообще → шаг 2.
2. **Снаружи вообще доступны?** С любой другой машины:
   ```bash
   curl -si https://<адрес прода>/api/health
   ```
   - Не отвечает → VPS/сеть/DNS/nginx: `dc ps nginx`, `ufw status`, панель хостера.
   - Отвечает → проблема на стороне подписки Авито: шаг 3.
3. **Жива ли подписка в Авито?** CLI-команд `avito subscriptions/resubscribe`
   **нет** (см. врезку выше). Рабочий путь — переподключение аккаунта из UI:
   `/settings/accounts` → «Переподключить» (`POST /api/v1/avito-accounts/{id}/reconnect`);
   OAuth-callback заново регистрирует webhook (`AvitoClient.register_webhook`)
   и ставит аккаунту `active`. Токены руками не копируем и в консоль не выводим.
   Если API Авито отвечает 401 → это на самом деле инцидент 8.2 (токен).
   Помнить: Авито сам отключает вебхуки, которые долго отвечали ошибками — после любого нашего даунтайма > пары часов подписку стоит перерегистрировать превентивно для всех аккаунтов (сейчас — по одному через UI).
4. **Кладётся ли в очередь и разбирается ли?**
   ```bash
   dc exec -T redis redis-cli XINFO GROUPS webhooks:avito # lag = сколько НЕ разобрано
   dc exec -T redis redis-cli XPENDING webhooks:avito workers
   dc exec -T redis redis-cli XLEN webhooks:avito:dlq     # «отравленные» события
   ```
   `lag` растёт, pending висит → инцидент 8.3 («очередь растёт»).
   **XLEN здесь не показатель**: стрим не очищается по мере разбора (записи
   живут до подрезки по `WEBHOOK_STREAM_MAXLEN`), его длина растёт всегда.
   Очередь пуста, но сообщений в БД нет → смотри `webhook.processed`/ошибки в логах worker; возможно, Авито сменил формат payload (риск №2) — тогда сырые события лежат в `webhook_raw_log` (таблица есть, TTL 30 дней), чинить `AvitoAdapter.parse_webhook`. Команды `replay-raw` нет: пока реиграется вручную — `XADD webhooks:avito` с телом из `webhook_raw_log`.
5. **После починки** — догнать пропущенное. Команды `reconcile --all` **нет**;
   reconciliation и так идёт сама: планировщик каждые `RECONCILE_INTERVAL_SECONDS`
   (сейчас 300 с) ставит по ARQ-задаче на каждый активный аккаунт — в логах это
   `scheduler.reconcile_enqueued`:
   ```bash
   dc logs --tail=200 scheduler | grep reconcile_enqueued
   ```
   То есть максимум ожидания — 5 минут; форсировать нечем, и это приемлемо.
   Идемпотентность по `external_message_id` гарантирует отсутствие дублей.

### 8.2. «Токен аккаунта протух» (`needs_reauth`)

Симптом: алерт `needs_reauth > 0`; в `/settings/accounts` аккаунт красный; исходящие по этому аккаунту падают в `failed`.

1. Определить масштаб:
   ```sql
   SELECT id, title, status, token_expires_at FROM avito_accounts ORDER BY status;
   ```
   - Один аккаунт `needs_reauth` → вероятно, на стороне Авито отозвали доступ (сменили пароль, отвязали приложение) — это штатно, шаг 3.
   - Несколько/все сразу → проблема у нас, шаг 2.
2. Массовое протухание — три типовые причины:
   - **scheduler лежал** дольше запаса (refresh за 2 часа до истечения, TTL 24 ч): `dc ps scheduler`, логи, рестарт. Отдельной команды `avito refresh` **нет**: обновлением занимается job `token_refresh` планировщика — он крутится каждые 30 минут и сам подхватит аккаунты, у которых `token_expires_at` близко. Поэтому лечение — поднять scheduler и убедиться по `/api/health/deep` (`scheduler.alive`, `accounts.tokens_expiring_2h`), что он снова успевает.
     Если refresh проходит — access был протухшим, но refresh_token жив; всё восстановится без переподключений.
   - **refresh_token сожжён двойным использованием** (refresh у Авито одноразовый). В логах — два `token.refresh` по одному аккаунту почти одновременно с разных компонентов ⇒ не сработал Redis-лок (`lock:token:{id}`). Такое чинится только переподключением (шаг 3), а причину гонки — искать и закрывать (это баг).
   - **Сменили `AVITO_CLIENT_SECRET`** в кабинете разработчика, но не в `.env` → все refresh падают. Обновить `.env`, `dc up -d api worker scheduler`.
3. Переподключение (только админ, только через UI — руками токены не вставляем):
   `/settings/accounts` → «Переподключить» → OAuth-консент Авито → callback сам сохранит новую пару токенов, перерегистрирует webhook и поставит `active`.
4. После восстановления: входящие, накопившиеся за простой, догонит сама reconciliation в течение 5 минут (`scheduler.reconcile_enqueued` в логах — CLI-команды `reconcile` нет); проверить в UI, что «повторить» на failed-исходящих доставляет.

Профилактика: `tokens_expiring_2h` в `/api/health/deep` не должен быть ненулевым дольше одного цикла планировщика (30 мин) — если это происходит регулярно, значит scheduler не успевает или падает, разбираться до того, как протухнет.

### 8.3. «Очередь растёт»

Симптом: алерт `queue.len > 1000` или `oldest_pending_sec > 300`.

1. Жив ли worker и успевает ли:
   ```bash
   dc ps worker
   dc exec -T worker arq --check app.workers.main.WorkerSettings
   #  -> Health check successful: … j_complete=7 j_failed=0 j_ongoing=0 queued=0
   dc exec -T redis redis-cli XPENDING webhooks:avito workers
   ```
   - Worker мёртв/рестартится по кругу → логи (`dc logs --tail=200 worker`): OOM (лимит 768M — виден в `docker inspect … OOMKilled`), необработанное исключение при старте, недоступная БД.
   - Worker жив, но pending висит на одних и тех же id → **poison message**, шаг 3.
2. Worker жив и разбирает, но медленно (len растёт, pending маленький) — не успевает по throughput:
   - Проверить, где тормозит: `webhook.processed.latency_ms` в логах. Обычные виновники: медленная БД (см. `log_min_duration_statement=500` в логах postgres — распухший индекс, отсутствие свежей партиции `messages`), таймауты походов в API Авито (сетевая деградация), AI-вызовы (должны быть с таймаутом 10 сек и не на критическом пути — если блокируют, это баг).
   - Быстрое лекарство — горизонтально:
     ```bash
     dc up -d --scale worker=3 --no-recreate
     ```
     Consumer group раздаст поток по репликам. Следить за пулом соединений БД (3×`DB_POOL_SIZE` + api + scheduler < `max_connections=120`).
3. Poison message (одна запись убивает обработчик по кругу):
   ```bash
   # кто именно: посмотреть первые зависшие
   dc exec -T redis redis-cli XPENDING webhooks:avito workers - + 10
   dc exec -T redis redis-cli XRANGE webhooks:avito <id> <id>
   ```
   Worker обязан сам (это требование к коду, проверяемое тестом): после N=5 неудачных доставок события — переложить его в стрим `webhooks:avito:dlq` и ack'нуть. Если этот механизм не сработал — вручную: `XACK webhooks:avito workers <id>` + сохранить тело события в файл для разбора. После фикса парсера реиграть из dlq **пока нечем — команды `replay-dlq` нет**; вручную: прочитать `XRANGE webhooks:avito:dlq - +` и вернуть тела в основной поток `XADD webhooks:avito '*' …`. Проверить, что DLQ пуст: `dc exec -T redis redis-cli XLEN webhooks:avito:dlq`.
4. Если Redis сам близок к `maxmemory` (очередь на миллионы записей): **не поднимать maxmemory бездумно** — сначала понять, почему не разбирается (шаги 1–3). Streams с политикой `noeviction` при упоре в память начнёт отдавать ошибки записи в gateway — вебхук ответит 500, Авито будет ретраить, reconciliation подстрахует; данные не теряются, но конвейер стоит.
5. После рассасывания очереди вернуть масштаб (`--scale worker=1`), написать postmortem-заметку: очередь не растёт «просто так».

### 8.4. «Диск заполнился media»

Симптом: алерт «<15% свободного»; в худшем случае — ошибки записи у api (загрузка вложений) и postgres.

1. Быстро понять, кто съел:
   ```bash
   df -h /
   sudo du -sh /var/leadchat/media /var/leadchat/backups /var/lib/docker 2>/dev/null
   docker system df                              # отдельно смотреть Build Cache
   du -sh /var/leadchat/media/*/ | sort -h       # по месяцам — раскладка YYYY/MM
   ```
   (`/var/lib/docker` читается только root'ом — без `sudo` `du` покажет 4 КБ и собьёт с толку.)
2. **Мгновенно освободить (безопасное, в порядке приоритета):**
   - Build cache сборок на сервере: `docker builder prune -af` — на 2026-08-05 это 2.1 ГБ из 7.9 ГБ занятых, самый крупный и самый бесполезный кусок (образы собираются прямо на VPS, пока нет реестра).
   - Старые локальные дампы: они уже офсайт — `find /var/leadchat/backups -name 'db_*.dump' -mtime +3 -delete` (временно ужать ротацию с 14 до 3 суток).
   - Docker-мусор: `docker image prune -af --filter "until=168h"` (старые слои образов после деплоев; текущие теги не тронет).
   - Осиротевшие тома от проверок восстановления: `docker volume ls -qf dangling=true` и `docker volume rm …` (по ~80 МБ за прогон, если запускали backup-verify старой версией без `-v`).
   - Логи контейнеров ротируются сами (`json-file`, 20m×5 на сервис — потолок ~800 МБ на весь стек), но проверить: `sudo du -ch /var/lib/docker/containers/*/*-json.log | tail -1`.
3. **Если съело действительно media:**
   - Осиротевшие файлы (файл есть, записи в `messages.attachments` нет — остатки прерванных загрузок): `deploy/media-gc.sh` сверяет диск с БД и удаляет сирот старше 7 дней. **Скрипта пока нет** (появится вместе с медиа-спринтом, строка в crontab закомментирована намеренно) — до тех пор сирот ищут руками сверкой листинга каталога с `messages.attachments`.
   - Архивирование старых месяцев: файлы иммутабельны и уже зеркалированы офсайт (ежедневный `rclone sync`). Политика: месяцы старше 18 месяцев можно переводить в «только офсайт»:
     ```bash
     # проверить, что офсайт-копия месяца полная, и удалить локально
     rclone check /var/leadchat/media/2025/01 ${RCLONE_REMOTE}/media/2025/01 --one-way \
       && rm -rf /var/leadchat/media/2025/01
     ```
     После этого запросы на такие вложения будут отдавать 404 — приемлемо для полуторагодовалых диалогов внутреннего инструмента; при необходимости конкретный месяц возвращается `rclone copy` за минуты. (Прозрачная отдача из офсайта — кандидат в фазу 2.0, см. DESIGN.md §1.4 про замену storage-класса.)
4. **Долгосрочно:** расширить диск у хостера (LVM/resize2fs без переезда) — правильное решение, если media растёт от бизнеса, а не от мусора. Прикинуть темп: `du -s` по месяцам из шага 1 даёт трендовую скорость роста.
5. Проверить последствия: если диск успел упереться в 100%, postgres мог остановиться — `dc ps`, `dc logs postgres`; после освобождения места он поднимется сам (`restart: unless-stopped`), затем `curl -sk https://<адрес прода>/api/health/deep | jq` и подождать один цикл reconciliation (5 минут).

**Запас по времени (замер 2026-08-05, команда ещё не запущена).** Диск 77 ГБ,
занято 7.9 ГБ (из них 2.1 ГБ — build cache), свободно 69 ГБ; база — 9 МБ,
media — 0 байт. Проектные 10 000 сообщений/сутки дают примерно 5 МБ/сутки в
базе (~1.8 ГБ/год вместе с индексами и партициями) — это не проблема ни на
горизонте полугода, ни на горизонте трёх лет. Единственный реальный
потребитель — **media**: при 5% сообщений с вложением по 300 КБ это ~150
МБ/сутки, то есть ~27 ГБ за полгода, и тогда диск подойдёт к границе примерно
через год. Отсюда два вывода: (1) до появления реальной статистики вложений
любые оценки — гадание, поэтому смотреть `du -sh /var/leadchat/media` раз в
месяц; (2) алерт «<15% свободного» уже работает в двух местах (backup.sh перед
дампом и healthcheck-alert.sh каждые 5 минут), и он сработает задолго до
остановки postgres.

### 8.5. «Бот молчит» / «бот всё отдаёт оператору»

Симптом: диалоги, которые должен вести бот, сразу уходят в очередь с заметкой «🤖 AI недоступен, передал оператору». Продукт при этом **не сломан**: недоступность AI никогда не блокирует доставку сообщений (DESIGN 4.5) — бот делает `handoff(reason="ai_unavailable")` и диалог получает живой человек. Но чинить это надо, иначе бот бесполезен.

> На текущем проде AI выключен целиком: `ANTHROPIC_API_KEY` пуст, а
> `ANTHROPIC_BASE_URL` в `.env.prod` вообще не задан. То есть шаг `ai_answer`
> всегда делает `handoff(ai_unavailable)` — это ожидаемое поведение, а не
> инцидент. Проверки ниже осмысленны после того, как ключ и прокси заведут.

```bash
# 1. Задачи движка вообще исполняются? Их регистрирует app/workers/main.py.
dc logs --tail=200 worker | grep -E 'bot_step|bot_ask_timeout'

# 2. Что именно отвечает модель. `bot.ai_call_failed` — сеть/таймаут/429/5xx;
#    `bot.ai_breaker_open` — предохранитель (10 отказов за 5 минут) уже сработал.
dc logs --tail=500 worker | grep -E 'bot\.ai_'

# 3. Проверить сам прокси из контейнера воркера (403 = ходим напрямую, а не через него).
dc exec -T worker sh -lc 'echo $ANTHROPIC_BASE_URL'
dc exec -T worker sh -lc 'curl -s -o /dev/null -w "%{http_code}\n" \
  -X POST "$ANTHROPIC_BASE_URL/v1/messages" -H "x-api-key: $ANTHROPIC_API_KEY" \
  -H "anthropic-version: 2023-06-01" -H "content-type: application/json" \
  -d "{\"model\":\"$AI_MODEL_CLASSIFY\",\"max_tokens\":8,\"messages\":[{\"role\":\"user\",\"content\":\"ok\"}]}"'
```

Разбор по коду ответа: `403` — запрос ушёл мимо прокси (пустой или неверный `ANTHROPIC_BASE_URL`), из Петербурга `api.anthropic.com` отвечает именно так; `000`/таймаут — прокси не поднят или закрыт фаерволом; `401` — протух `ANTHROPIC_API_KEY`; `429` — упёрлись в лимит, поможет снижение нагрузки, а не рестарт. После починки предохранитель сбрасывается первым же успешным вызовом — рестарт воркера не нужен, но он безвреден.

Быстрая проверка без прода: `POST /api/v1/bots/sandbox/start` с `ai_mode: "real"` из-под админа. `503 upstream_unavailable` — ключа нет вовсе; ответ с `ai_call` в трассировке — модель отвечает и дело не в ней.

### 8.6. Мини-карта «что дёргать» (шпаргалка)

(`dc` — алиас из шапки раздела 8; голый `docker compose` в `/srv/leadchat` не работает.)

| Симптом | Первая команда | Runbook |
|---|---|---|
| Сообщения не приходят | `dc logs nginx \| grep '/api/hooks/'` | 8.1 |
| Аккаунт красный в настройках | `SELECT status FROM avito_accounts` | 8.2 |
| Алерт queue.len | `dc exec -T redis redis-cli XINFO GROUPS webhooks:avito` (поле `lag`) | 8.3 |
| Алерт disk | `df -h; sudo du -sh /var/leadchat/media` | 8.4 |
| Бот всё отдаёт оператору | `dc logs worker \| grep bot.ai_` | 8.5 |
| Всё лежит | `dc ps` → `dc logs` | по месту |
| Бэкап молчит | `tail -20 /var/log/leadchat-backup.log` | 6.2 |
| Непонятно | `curl -sk https://<адрес прода>/api/health/deep \| jq` | по красному полю |

---

## Приложение А. Cron пользователя деплоя (`crontab -l`)

Эталон — `deploy/crontab.leadchat` в репозитории; ставится одной командой
`crontab /srv/leadchat/deploy/crontab.leadchat`. Установлено на проде
2026-08-05 и проверено по `/var/log/syslog` (задание отработало в первый же
пятиминутный слот).

```cron
SHELL=/bin/bash
MAILTO=""
DEPLOY_DIR=/srv/leadchat
COMPOSE_FILE=/srv/leadchat/docker-compose.prod.yml:/srv/leadchat/docker-compose.override.yml
RCLONE_REMOTE=offsite:afbac24b-221c-44f1-bd88-6df6fd6c65ba

# бэкап БД + media офсайт (раздел 6.2). Время — МСК: 03:30 МСК = 00:30 UTC
30 3 * * *  /srv/leadchat/deploy/backup.sh >> /var/log/leadchat-backup.log 2>&1
# ежемесячная проверка восстановления (раздел 6.3)
0 6 5 * *   /srv/leadchat/deploy/backup-verify.sh >> /var/log/leadchat-backup.log 2>&1
# мониторинг вместо Uptime-Kuma, пока её негде поднять (раздел 7.2)
*/5 * * * * /srv/leadchat/deploy/healthcheck-alert.sh >> /var/log/leadchat-health.log 2>&1
# чистка осиротевших media, еженедельно — ВЫКЛЮЧЕНО: media-gc.sh ещё не написан
# 0 5 * * 1   /srv/leadchat/deploy/media-gc.sh >> /var/log/leadchat-media.log 2>&1
# чтобы логи самих скриптов не росли бесконечно
0 4 1 * *   find /var/log -maxdepth 1 -name 'leadchat-*.log' -size +50M -exec truncate -s 0 {} \;
```

Логи скриптов (`/var/log/leadchat-*.log`) заводятся от root'а и отдаются
пользователю деплоя: `sudo touch /var/log/leadchat-health.log && sudo chown
leadchat:leadchat /var/log/leadchat-health.log` — иначе cron-задание молча
не сможет писать в `/var/log`.

## Приложение Б. Чек-лист выкатки первого прода

1. [ ] DNS: A-запись `chat.partner-lead-centre.ru` → IP VPS; TTL 300 на время запуска.
2. [ ] VPS подготовлен по разделу 1 (deploy-пользователь, ufw, каталоги, bootstrap-сертификат).
3. [ ] `.env` заполнен, секреты сгенерированы, копия в менеджере секретов компании.
4. [ ] `rclone config` настроен, `rclone lsd offsite:` работает.
5. [ ] GitHub Secrets заведены (5.2), первый тег `v0.1.0` запушен, деплой прошёл, `/api/health` зелёный.
6. [ ] Создан первый админ: `docker compose exec api python -m app.cli create-admin --email ...`.
7. [ ] Подключён тестовый аккаунт Авито, входящее сообщение доехало до UI < 2 сек.
8. [ ] Uptime-Kuma: все 5 мониторов зелёные, тестовый алерт в Telegram доставлен.
9. [ ] Sentry: тестовое исключение (`python -m app.cli sentry-test`) видно в проекте.
10. [ ] `backup.sh` прогнан вручную, дамп появился офсайт; `backup-verify.sh` зелёный.
11. [ ] Прогнан runbook 8.1 шаг 3 (resubscribe) на тестовом аккаунте — команда работает.
12. [ ] Cron заведён (Приложение А).
