#!/usr/bin/env bash
# ============================================================================
# LeadChat — деплой на VPS (docs/05-DEPLOY-OPS.md §5.3).
#
#   deploy.sh <image_tag>
#
# Вызывается из CI по ssh (.github/workflows/deploy.yml) либо руками на сервере.
# Идемпотентен: повторный запуск с тем же тегом безопасен.
#
# Что делает:
#   0. Блокировка (два деплоя одновременно не поедут), запоминает текущий тег.
#   1. docker compose pull новых образов по тегу.
#   2. alembic upgrade head — ДО обновления кода (миграции expand-contract).
#   3. Поочерёдный перезапуск: worker -> scheduler -> api -> nginx,
#      каждый шаг ждёт healthy, прежде чем идти дальше.
#   4. Smoke: /api/health отвечает ok и отдаёт ИМЕННО задеплоенный тег.
#   5. Провал любого шага -> автооткат на предыдущий тег; итог виден в логе
#      выкатки (внешних каналов у системы нет — docs/23).
#
# ВАЖНО про миграции: вниз-миграции в проде запрещены. Откат откатывает ТОЛЬКО
# образы; работоспособность старого кода на новой схеме обеспечивает правило
# expand-contract (05 §5.3).
# ============================================================================
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/srv/leadchat}"
COMPOSE_FILE="${COMPOSE_FILE:-${DEPLOY_DIR}/docker-compose.yml}"
ENV_FILE="${DEPLOY_DIR}/.env"
STATE_DIR="${DEPLOY_DIR}/.state"
PREV_TAG_FILE="${STATE_DIR}/previous_tag"
LOCK_FILE="/tmp/leadchat-deploy.lock"
LOG_FILE="${DEPLOY_LOG:-/var/log/leadchat-deploy.log}"

# health-check после деплоя: 12 попыток × 5 с = 60 с (05 §5.3)
HEALTH_TRIES="${HEALTH_TRIES:-12}"
HEALTH_SLEEP="${HEALTH_SLEEP:-5}"

TAG="${1:-}"
IS_ROLLBACK="${LEADCHAT_ROLLBACK:-0}"

# --- логирование: и в stdout (его видит CI), и в файл на сервере -------------
if ! touch "$LOG_FILE" 2>/dev/null; then
    LOG_FILE="${DEPLOY_DIR}/deploy.log"
    touch "$LOG_FILE" 2>/dev/null || LOG_FILE=/dev/null
fi

log()  { printf '%s  %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_FILE"; }
fail() { printf '%s  !! %s\n' "$(date -u +%FT%TZ)" "$*" | tee -a "$LOG_FILE" >&2; }

usage() {
    fail "usage: deploy.sh <image_tag>   (тег = git SHA или vX.Y.Z, как в GHCR)"
    exit 2
}
[ -n "$TAG" ] || usage
[ -f "$COMPOSE_FILE" ] || { fail "нет ${COMPOSE_FILE}"; exit 2; }
[ -f "$ENV_FILE" ] || { fail "нет ${ENV_FILE} — заполните из .env.prod.example"; exit 2; }

cd "$DEPLOY_DIR"
mkdir -p "$STATE_DIR"

# --- один деплой за раз -----------------------------------------------------
# flock есть в util-linux, то есть на любом Ubuntu; на macOS (локальная отладка
# скрипта) его нет — тогда просто работаем без блокировки.
#
# ВАЖНО: при автооткате rollback() запускает ЭТОТ ЖЕ скрипт дочерним процессом,
# пока родитель ещё держит блокировку. Дочерний `exec 9>` открывает файл заново
# (новое open file description), поэтому `flock -n` у него гарантированно
# провалился бы и откат молча выходил бы с кодом 75, ничего не откатив.
# Поэтому во ВЛОЖЕННОМ запуске (LEADCHAT_LOCK_INHERITED=1 ставит только сам
# rollback()) блокировку не берём — её держит родительский процесс.
# Ручной откат через rollback.sh сюда не попадает: там родителя нет и лок
# берётся как обычно.
if [ "${LEADCHAT_LOCK_INHERITED:-0}" = "1" ]; then
    log "вложенный запуск (автооткат) — лок держит родительский деплой"
elif command -v flock >/dev/null 2>&1; then
    exec 9>"$LOCK_FILE"
    if ! flock -n 9; then
        fail "другой деплой уже идёт (${LOCK_FILE}) — выходим"
        exit 75   # EX_TEMPFAIL
    fi
else
    fail "flock не найден — деплой идёт без защиты от параллельного запуска"
fi

DC=(docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE")

# --- переменные окружения деплоя (домен, алерты) ----------------------------
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a
# Адрес по умолчанию — тот, что РАБОТАЕТ. Домен chat.partner-lead-centre.ru
# сюда не ведёт: его A-запись указывает на <сторонний сервер>, где живёт
# FreeScout — другой хелпдеск с собственным сертификатом на это же имя.
# Пока домен не направят на прод, умолчание обязано быть рабочим, иначе
# скрипт молча пойдёт в чужую систему.
DOMAIN="${DOMAIN:-188-225-34-82.sslip.io}"
HEALTH_URL="${HEALTH_URL:-https://${DOMAIN}/api/health}"

# значение IMAGE_TAG из .env без кавычек и пробелов
current_tag() { grep -E '^IMAGE_TAG=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d "\"' "; }

# Идемпотентная запись IMAGE_TAG в .env (строки может не быть вовсе).
# Без `sed -i`: у GNU и BSD sed разный синтаксис флага, а скрипт полезно уметь
# прогонять и на макбуке. Финальный `cat >` пишет через тот же inode —
# права 600 и владелец файла сохраняются.
set_tag() {
    local tag="$1" tmp
    tmp="$(mktemp)"
    if grep -qE '^IMAGE_TAG=' "$ENV_FILE"; then
        awk -v t="$tag" '/^IMAGE_TAG=/ { print "IMAGE_TAG=" t; next } { print }' "$ENV_FILE" > "$tmp"
    else
        { cat "$ENV_FILE"; printf 'IMAGE_TAG=%s\n' "$tag"; } > "$tmp"
    fi
    cat "$tmp" > "$ENV_FILE"
    rm -f "$tmp"
    # ОБЯЗАТЕЛЬНО: скрипт выше сделал `set -a; source .env`, то есть старый
    # IMAGE_TAG уже экспортирован в окружение. При интерполяции compose
    # переменная окружения ПЕРЕБИВАЕТ --env-file — без этой строки pull и
    # up тянули бы предыдущий тег, а деплой «проходил» бы вхолостую.
    export IMAGE_TAG="$tag"
}

# Ждём healthy у сервиса. Сервисы без healthcheck считаются готовыми в running.
wait_healthy() {
    local svc="$1" tries="${2:-40}" cid st
    for ((i = 1; i <= tries; i++)); do
        cid="$("${DC[@]}" ps -q "$svc" 2>/dev/null | head -1)"
        if [ -n "$cid" ]; then
            st="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$cid" 2>/dev/null || echo unknown)"
            case "$st" in
                healthy|running) log "   ${svc}: ${st}"; return 0 ;;
                unhealthy)       fail "   ${svc}: unhealthy"; return 1 ;;
            esac
        fi
        sleep 3
    done
    fail "   ${svc}: не дождались healthy за $((tries * 3)) с"
    return 1
}

smoke_health() {
    local body
    for ((i = 1; i <= HEALTH_TRIES; i++)); do
        sleep "$HEALTH_SLEEP"
        body="$(curl -fsS --max-time 10 "$HEALTH_URL" 2>/dev/null || true)"
        if [ -n "$body" ] && printf '%s' "$body" | grep -q '"status":[[:space:]]*"ok"'; then
            # версия обязана совпасть с задеплоенным тегом — иначе снаружи
            # отвечает старый контейнер (05 §7.2 / 07 §6 SM-1)
            if printf '%s' "$body" | grep -q "\"version\":[[:space:]]*\"${TAG}\""; then
                log "   health ok: ${body}"
                return 0
            fi
            log "   health ok, но версия ещё не ${TAG}: ${body}"
        fi
    done
    fail "   health-check не прошёл за $((HEALTH_TRIES * HEALTH_SLEEP)) с (${HEALTH_URL})"
    return 1
}

rollback() {
    local prev="$1"
    if [ "$IS_ROLLBACK" = "1" ]; then
        fail "откат уже выполнялся и тоже упал — дальше руками (runbook 05 §8)"
        return 1
    fi
    if [ -z "$prev" ] || [ "$prev" = "$TAG" ]; then
        fail "предыдущий тег неизвестен — автооткат невозможен"
        return 1
    fi
    fail "откатываемся на ${prev}"
    # LEADCHAT_LOCK_INHERITED=1 — дочерний процесс не пытается взять flock,
    # который прямо сейчас держим мы (иначе откат вышел бы с кодом 75).
    LEADCHAT_ROLLBACK=1 LEADCHAT_LOCK_INHERITED=1 "$0" "$prev"
}

# ============================== ход деплоя ==================================
PREV="$(current_tag)"
log "== deploy ${TAG} (текущий: ${PREV:-неизвестен}, rollback=${IS_ROLLBACK}) =="

# 1. Тег в .env + образы
set_tag "$TAG"
log "1/6 pull образов ${TAG}"
if ! "${DC[@]}" pull --quiet; then
    fail "pull не удался (нет такого тега в GHCR? нет доступа?)"
    set_tag "${PREV:-latest}"
    exit 1
fi

# 2. Инфраструктура должна быть жива до миграций (важно для самого первого деплоя).
#    Откат тут бессмыслен: postgres/redis идут с фиксированных тегов и от нашего
#    IMAGE_TAG не зависят — если они не поднялись, откат образа не поможет.
log "2/6 postgres + redis"
"${DC[@]}" up -d postgres redis
if ! wait_healthy postgres 40 || ! wait_healthy redis 20; then
    set_tag "${PREV:-latest}"
    fail "БД или Redis не поднялись — деплой остановлен, код не трогали"
    exit 1
fi

# 3. Миграции — ДО обновления кода, старые контейнеры ещё работают.
#    Поэтому миграции обязаны быть expand-contract (05 §5.3).
log "3/6 alembic upgrade head"
if ! "${DC[@]}" run --rm --no-deps api alembic upgrade head; then
    fail "миграции упали — код не трогаем, схема БД в прежнем состоянии"
    set_tag "${PREV:-latest}"
    exit 1
fi

# 4. Поочерёдный перезапуск. Порядок: фоновые -> api -> nginx.
#    Фоновые процессы: короткий простой не виден пользователям — очередь в
#    Redis Streams подождёт, consumer group догонит.
log "4/6 worker"
"${DC[@]}" up -d --no-deps worker
wait_healthy worker 30 || { rollback "$PREV"; exit 1; }

log "4/6 scheduler"
"${DC[@]}" up -d --no-deps scheduler
wait_healthy scheduler 30 || { rollback "$PREV"; exit 1; }

# api: пересоздание контейнера ~5–10 с. TanStack Query ретраит запросы,
# WebSocket переподключается сам (03-FRONTEND). Вебхуки Авито в эту щель
# получат 502 — Авито ретраит, плюс reconciliation закрывает остаток.
log "5/6 api"
"${DC[@]}" up -d --no-deps api
wait_healthy api 40 || { rollback "$PREV"; exit 1; }

# nginx: новая статика фронта. Плюс обязательный reload — upstream `api:8000`
# резолвится один раз при старте nginx, а у пересозданного контейнера api
# новый IP; без reload часть запросов уходила бы в никуда.
log "5/6 nginx"
"${DC[@]}" up -d --no-deps nginx
wait_healthy nginx 30 || { rollback "$PREV"; exit 1; }
"${DC[@]}" exec -T nginx nginx -s reload >/dev/null 2>&1 \
    && log "   nginx reload (перечитал upstream api)" \
    || log "   nginx reload пропущен (контейнер только что пересоздан — resolve свежий)"

"${DC[@]}" up -d certbot >/dev/null 2>&1 || true

# 6. Smoke снаружи, через боевой домен
log "6/6 smoke ${HEALTH_URL}"
if ! smoke_health; then
    "${DC[@]}" ps | tee -a "$LOG_FILE" || true
    rollback "$PREV"
    exit 1
fi

# Успех: фиксируем предыдущий тег как точку отката.
# При откате этого НЕ делаем: точкой отката стал бы тот самый сломанный тег,
# с которого мы только что убежали.
if [ "$IS_ROLLBACK" != "1" ] && [ -n "$PREV" ]; then
    printf '%s\n' "$PREV" > "$PREV_TAG_FILE"
fi

# Старые слои: чистим только явно устаревшие (текущие теги не трогает)
docker image prune -af --filter "until=168h" >/dev/null 2>&1 || true

if [ "$IS_ROLLBACK" = "1" ]; then
    log "== OK: откат на ${TAG} успешен, /api/health зелёный =="
else
    log "== OK: задеплоен ${TAG}, /api/health зелёный =="
fi
exit 0
