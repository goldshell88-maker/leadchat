#!/usr/bin/env bash
# ============================================================================
# LeadChat — минимальный мониторинг доступности (docs/05-DEPLOY-OPS.md §7.2).
#
# Это ЗАМЕНА Uptime-Kuma на время, пока её негде поднять. Ограничение честное:
# скрипт живёт на том же VPS, что и сервис, поэтому смерть самого VPS он не
# заметит (о ней некому будет написать). Всё остальное — процесс api, БД,
# Redis, очередь, токены, доставку, срок сертификата и диск — ловит.
# Как только появится внешний хост, монитор переезжает туда, а эта строка
# из cron убирается (§7.2, таблица мониторов).
#
# cron: */5 * * * *  DEPLOY_DIR=/srv/leadchat /srv/leadchat/deploy/healthcheck-alert.sh
#
# Находки уходят в центр уведомлений внутри системы: внешних каналов у
# системы нет по решению заказчика от 8 августа (docs/23).
# Пока переменные пусты, скрипт работает «вхолостую»: проверяет и пишет в лог,
# но никуда не отправляет — включение = заполнить две переменные в .env.
#
# Антидребезг: сообщение уходит при СМЕНЕ состояния (ok->плохо и обратно) и
# повторяется не чаще раза в REALERT_MINUTES минут. Состояние — в STATE_FILE.
# ============================================================================
set -uo pipefail   # без -e: скрипт-мониторинг обязан дойти до конца сам

DEPLOY_DIR="${DEPLOY_DIR:-/srv/leadchat}"
ENV_FILE="${ENV_FILE:-${DEPLOY_DIR}/.env}"
STATE_FILE="${STATE_FILE:-${HOME:-/tmp}/.leadchat/health.state}"   # каталог доступен deploy-пользователю без sudo
REALERT_MINUTES="${REALERT_MINUTES:-60}"
CURL_TIMEOUT="${CURL_TIMEOUT:-15}"
DISK_MIN_FREE_PCT="${DISK_MIN_FREE_PCT:-15}"
# ⚠ ПОРОГ ОБЯЗАН НАСТУПАТЬ ПОСЛЕ НАЧАЛА ОКНА ПРОДЛЕНИЯ, А НЕ ЧЕРЕЗ ДВЕ НЕДЕЛИ
# ПОСЛЕ НЕГО (разбор 03.09). Let's Encrypt продлевает за 30 суток до конца; при
# пороге в 14 тревога о несостоявшемся продлении приходила бы на ШЕСТНАДЦАТЫЙ
# день молчания — сертификат к тому времени уже две недели как должен был
# обновиться. 25 суток дают запас: продление начинается на 30-м дне, и если оно
# не прошло, через пять суток об этом уже известно, а до аварии ещё месяц.
CERT_MIN_DAYS="${CERT_MIN_DAYS:-25}"

if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    set -a; . "$ENV_FILE"; set +a
fi
BASE_URL="${HEALTHCHECK_BASE_URL:-${PUBLIC_BASE_URL:-https://localhost}}"

log() { printf '%s  %s\n' "$(date -u +%FT%TZ)" "$*"; }

# --- Центр уведомлений в самом приложении (14 §2.1) --------------------------
# Тот же контракт, что у backup.sh: отсюда уходит только ВИД события и одна
# строка подробности, заголовок и важность выбирает сервер. Список допустимых
# видов — KNOWN_KINDS в app/api/routes/internal.py.
#
# Три правила, без которых эта функция сделала бы хуже, чем её отсутствие:
#   1. нет токена — молча выходим (dev-хост, свежий сервер);
#   2. любая ошибка curl гасится: уведомление не имеет права ронять мониторинг;
#   3. функция ВСЕГДА возвращает 0.
#
# Адрес по умолчанию — loopback: ручка /api/v1/internal/ на публичный edge не
# выпущена (nginx отдаёт на неё 404), порт api опубликован на 127.0.0.1.
# ВНЕШНЕМУ наблюдателю со второго сервера (14 §5) адрес надо задать явно —
# туннелем или отдельным allow-list'ом.
INTERNAL_NOTIFY_URL="${INTERNAL_NOTIFY_URL:-http://127.0.0.1:8000/api/v1/internal/notify}"

notify_center() {  # $1 — вид события, $2 — подробность (необязательно)
    [ -n "${INTERNAL_SERVICE_TOKEN:-}" ] || return 0
    local detail payload
    # Кавычки и переводы строк из сообщений bash сломали бы JSON — вычищаем.
    detail=$(printf '%s' "${2:-}" | tr -d '"\\' | tr '\n\t' '  ' | cut -c1-450)
    payload=$(printf '{"kind":"%s","source":"healthcheck-alert.sh","detail":"%s"}' "$1" "${detail}")
    # Без -L намеренно: на редиректе curl молча НЕ повторил бы POST, и событие
    # потерялось бы тихо. Пусть лучше будет видно в логе.
    curl -fsS --max-time 10 -X POST "${INTERNAL_NOTIFY_URL}" \
        -H 'Content-Type: application/json' \
        -H "X-Internal-Token: ${INTERNAL_SERVICE_TOKEN}" \
        --data "${payload}" >/dev/null 2>&1 \
        || log "   !! уведомление в центр не ушло (${1}) — ${INTERNAL_NOTIFY_URL}"
    return 0
}

problems=()

# --- 1. /api/health — процесс api, БД, Redis ---------------------------------
# Ключевое слово именно "status":"ok": подстрока "ok" встречается в самих
# именах полей ("webhooks", "tokens_expiring_2h") и потому проверкой не является.
H=$(curl -sk --max-time "$CURL_TIMEOUT" -w '\n%{http_code}' "${BASE_URL}/api/health" 2>/dev/null)
H_CODE=$(printf '%s' "$H" | tail -1)
H_BODY=$(printf '%s' "$H" | sed '$d')
if [ "$H_CODE" != "200" ]; then
    problems+=("/api/health отвечает HTTP ${H_CODE:-нет ответа}")
    # «Система не отвечает снаружи» (14 §2.1) обязан сообщать ВНЕШНИЙ
    # наблюдатель: проверка с самого сервера не переживает его падения, а с
    # живого сервера этот вид означал бы «не отвечает сам себе» — новость
    # верная, но событие для неё другое. EXTERNAL_MONITOR=1 ставит второй
    # сервер (Нидерланды, 14 §5) в своей строке crontab.
    if [ "${EXTERNAL_MONITOR:-0}" = "1" ]; then
        notify_center "system.unreachable" "${BASE_URL}/api/health отвечает HTTP ${H_CODE:-нет ответа}"
    fi
elif ! printf '%s' "$H_BODY" | grep -q '"status":[[:space:]]*"ok"'; then
    problems+=("/api/health не ok: ${H_BODY}")
fi

# --- 1а. Шлюз внешних API на Амстердаме (docs/46) ----------------------------
# Без него молчат все помощники адреса и ответы ботов; сам он из интернета
# недостижим, поэтому спросить его может только этот сервер — по мосту.
if [ -n "${GATEWAY_URL:-}" ]; then
    G=$(curl -s --max-time "$CURL_TIMEOUT" -w '\n%{http_code}' "${GATEWAY_URL%/}/health" 2>/dev/null)
    G_CODE=$(printf '%s' "$G" | tail -1)
    G_BODY=$(printf '%s' "$G" | sed '$d')
    if [ "$G_CODE" != "200" ]; then
        problems+=("шлюз API ${GATEWAY_URL%/}/health отвечает HTTP ${G_CODE:-нет ответа}")
    elif ! printf '%s' "$G_BODY" | grep -q '"ok":[[:space:]]*true'; then
        problems+=("шлюз API не ok: ${G_BODY}")
    fi
fi

# --- 2. /api/health/deep — очередь, токены, доставка, приём -------------------
D=$(curl -sk --max-time "$CURL_TIMEOUT" -w '\n%{http_code}' "${BASE_URL}/api/health/deep" 2>/dev/null)
D_CODE=$(printf '%s' "$D" | tail -1)
D_BODY=$(printf '%s' "$D" | sed '$d')
if [ "$D_CODE" != "200" ]; then
    problems+=("/api/health/deep отвечает HTTP ${D_CODE:-нет ответа}")
elif ! printf '%s' "$D_BODY" | grep -q '"status":[[:space:]]*"ok"'; then
    # в теле уже есть checks_failed — кладём его целиком, разбираться по 05 §8
    problems+=("/api/health/deep degraded: ${D_BODY}")
fi

# Мёртвый планировщик — отдельная новость (14 §2.1): без него встают партиции,
# обновление токенов Авито и сверка пропущенных сообщений. Внешняя проверка
# нужна потому, что сам планировщик о своей смерти сообщить не может: у него
# ровно та же проблема, что у молчащего бэкапа.
if printf '%s' "$D_BODY" | grep -q '"scheduler":[[:space:]]*{[^}]*"alive":[[:space:]]*false'; then
    problems+=("планировщик не подаёт признаков жизни (scheduler.alive=false)")
    notify_center "scheduler.dead" "внешняя проверка ${BASE_URL}/api/health/deep: scheduler.alive=false"
fi

# --- 3. Срок сертификата -----------------------------------------------------
HOST=$(printf '%s' "$BASE_URL" | sed -E 's#^https?://##; s#/.*##')
END=$(echo | openssl s_client -servername "$HOST" -connect "${HOST}:443" 2>/dev/null \
        | openssl x509 -noout -enddate 2>/dev/null | cut -d= -f2)
if [ -n "$END" ]; then
    DAYS=$(( ( $(date -d "$END" +%s) - $(date +%s) ) / 86400 ))
    [ "$DAYS" -lt "$CERT_MIN_DAYS" ] && problems+=("сертификат ${HOST} истекает через ${DAYS} дн.")
else
    problems+=("не удалось прочитать сертификат ${HOST}")
fi

# --- 4. Диск -----------------------------------------------------------------
FREE_PCT=$(df -P / | awk 'NR==2 {gsub("%","",$5); print 100-$5}')
[ "$FREE_PCT" -lt "$DISK_MIN_FREE_PCT" ] && problems+=("на диске свободно ${FREE_PCT}% (порог ${DISK_MIN_FREE_PCT}%, runbook §8.4)")

# --- 5. Свежесть бэкапа ------------------------------------------------------
# Молчащий бэкап — такой же инцидент, как лежащий сервис, только замечают его
# в тот единственный день, когда дамп нужен.
BACKUP_DIR="${BACKUP_DIR:-/var/backups/leadchat}"
# Только ночные db_ГГГГММДД_ЧЧММ.dump: снимки перед выкаткой (…_deploy.dump)
# и ручные идут по нескольку раз в день и прятали бы пропавшую ночную копию.
LATEST=$(ls -1t "${BACKUP_DIR}"/db_????????_????.dump 2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    problems+=("в ${BACKUP_DIR} нет ни одного дампа")
else
    AGE_H=$(( ( $(date +%s) - $(stat -c%Y "$LATEST") ) / 3600 ))
    [ "$AGE_H" -gt 30 ] && problems+=("последний дамп старше ${AGE_H} ч (${LATEST})")
fi

# --- Итог + антидребезг ------------------------------------------------------
mkdir -p "$(dirname "$STATE_FILE")" 2>/dev/null
PREV_STATE=$(cut -d' ' -f1 "$STATE_FILE" 2>/dev/null || echo "unknown")
PREV_TS=$(cut -d' ' -f2 "$STATE_FILE" 2>/dev/null || echo 0)
NOW=$(date +%s)

if [ ${#problems[@]} -eq 0 ]; then
    log "OK: health/deep зелёные, сертификат и диск в норме, бэкап свежий"
    if [ "$PREV_STATE" = "bad" ]; then
        # Восстановление отмечаем только в логе. Отдельного вида «полегчало» в
        # центре нет и заводить его не стоит: администратор и так видит, что
        # прежнее уведомление больше не повторяется.
        log "   (было плохо — восстановилось)"
    fi
    printf 'ok %s\n' "$NOW" > "$STATE_FILE"
    exit 0
fi

log "ПРОБЛЕМЫ:"
printf '   • %s\n' "${problems[@]}"

# Сводку целиком в центр НЕ шлём, и это не забывчивость. Каждую беду из этого
# списка сторож внутри системы замечает сам и называет своими словами (диск,
# сертификат, свежесть копии, планировщик); дубль сводкой означал бы два
# уведомления об одном событии. Отсюда уходит только то, чего сторож увидеть
# не может, потому что для этого надо быть снаружи: система не отвечает
# (строка 98) и планировщик молчит (строка 121).
if [ "$PREV_STATE" != "bad" ] || [ $(( (NOW - PREV_TS) / 60 )) -ge "$REALERT_MINUTES" ]; then
    printf 'bad %s\n' "$NOW" > "$STATE_FILE"
else
    log "   (повтор подавлен: прошло меньше ${REALERT_MINUTES} мин)"
fi
exit 1
