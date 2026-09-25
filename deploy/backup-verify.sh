#!/usr/bin/env bash
# ============================================================================
# LeadChat — ежемесячная проверка восстановления (docs/05-DEPLOY-OPS.md §6.3).
# cron: 0 6 5 * *  /srv/leadchat/deploy/backup-verify.sh >> /var/log/leadchat-backup.log 2>&1
#
# «Бэкап, который ни разу не восстанавливали, — это лотерейный билет.»
# Скрипт поднимает ОДНОРАЗОВЫЙ postgres, восстанавливает последний дамп,
# считает строки и проверяет свежесть данных. Боевую БД не трогает вообще.
#
# Зелёный результат = дамп читается, схема цела, счётчики правдоподобны,
# последнее сообщение не старше 2 суток. Иначе — алерт и exit 1.
# ============================================================================
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/srv/leadchat}"
ENV_FILE="${DEPLOY_DIR}/.env"
CONTAINER="pg_verify_$$"

cd "$DEPLOY_DIR"
# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

BACKUP_DIR="${BACKUP_DIR:-/var/backups/leadchat}"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"
MAX_AGE_DAYS="${VERIFY_MAX_AGE_DAYS:-2}"

log() { printf '%s  %s\n' "$(date -u +%FT%TZ)" "$*"; }

# --- уведомление в центр внутри системы (docs/14-NOTIFICATIONS.md §2.1) ------
# Тот же контракт, что у backup.sh: отсюда уходит вид события и одна строка
# подробности, заголовок и важность выбирает сервер (вид `restore_check.failed`,
# см. KNOWN_KINDS в app/api/routes/internal.py). Это ЕДИНСТВЕННЫЙ адресат
# скрипта, кроме лога: внешних каналов у системы нет по решению заказчика
# от 8 августа.
#
# Адрес — loopback: публичный маршрут к /api/v1/internal/ закрыт (nginx 404),
# порт api опубликован на 127.0.0.1. Функция ВСЕГДА возвращает 0 — иначе при
# `set -e` она сама сработала бы ERR-trap'ом.
INTERNAL_NOTIFY_URL="${INTERNAL_NOTIFY_URL:-http://127.0.0.1:8000/api/v1/internal/notify}"

notify_center() {  # $1 — вид события, $2 — подробность
    [ -n "${INTERNAL_SERVICE_TOKEN:-}" ] || return 0
    local detail payload
    detail=$(printf '%s' "${2:-}" | tr -d '"\\' | tr '\n\t' '  ' | cut -c1-450)
    payload=$(printf '{"kind":"%s","source":"backup-verify.sh","detail":"%s"}' "$1" "${detail}")
    curl -fsS --max-time 10 -X POST "${INTERNAL_NOTIFY_URL}" \
        -H 'Content-Type: application/json' \
        -H "X-Internal-Token: ${INTERNAL_SERVICE_TOKEN}" \
        --data "${payload}" >/dev/null 2>&1 \
        || log "!! уведомление в центр не ушло (${1}) — ${INTERNAL_NOTIFY_URL}"
    return 0
}

# $1 — текст провала: в лог и в центр уведомлений; $2 — вид события.
# Копия, из которой нельзя восстановиться, — «backup.verify_failed», критичное:
# под «restore_check.failed» (важное) она тонула среди важных (проверка 24.09).
# Сбой самой проверки (скрипт упал, витрина не успела) остаётся важным.
fail_center() {
    log "$1"
    notify_center "${2:-restore_check.failed}" "$1"
}

# -v обязателен: у образа postgres анонимный том с PGDATA, и без него каждый
# прогон оставлял бы на диске ~80 МБ мусора (за год — гигабайт на пустом месте).
cleanup() { docker rm -f -v "$CONTAINER" >/dev/null 2>&1 || true; }
trap cleanup EXIT
# В лог пишем всегда: центр может быть недоступен вместе с системой, и без этой
# строки провал проверки восстановления выглядел бы как её отсутствие.
trap 'rc=$?; log "!! ПРОВАЛ: строка ${LINENO}, код ${rc}"; fail_center "🔴 backup-verify: скрипт упал на строке ${LINENO} (rc=${rc})"' ERR

# Здесь берётся ЛОКАЛЬНЫЙ дамп, и это осознанно: ежемесячная проверка гоняется
# по cron на самом сервере и обязана быть дешёвой — качать копию из облака
# каждый месяц значит платить за трафик и однажды отключить проверку целиком.
#
# Но локальный дамп лежит на ТОМ ЖЕ диске, что и боевая база, а восстанавливать
# будут из офсайтной копии. Её открывает квартальное учение — restore-check.sh
# без аргументов (см. его шапку). Если убрать это и оттуда, офсайт снова не
# откроет никто и никогда: «rclone copy вернул ноль» — не то же самое, что
# «файл читается pg_restore'ом».
LATEST=$(ls -1t "${BACKUP_DIR}"/db_*.dump 2>/dev/null | head -1 || true)
if [ -z "$LATEST" ]; then
    fail_center "🔴 backup-verify: в ${BACKUP_DIR} нет ни одного дампа" backup.verify_failed
    exit 1
fi
log "проверяем ${LATEST}"

START=$(date +%s)

# Без --rm: контейнер (вместе с его анонимным томом) снимает cleanup по trap,
# а --rm удалил бы контейнер раньше и том остался бы висеть.
# ⚠ НАСТРОЙКИ ЗАДАНЫ ЯВНО, И ЭТО НЕ УКРАШЕНИЕ. С умолчаниями образа
# (`shared_buffers` 128 МБ, `work_mem` 4 МБ) обновление витрины в проверочной
# базе шло 831 секунду при 100 % ядра — против 11-13 секунд в бою (замер
# 03.09). Разница вся в памяти: LATERAL'ы витрины на четырёх мегабайтах
# уходят на диск. Ежемесячная проверка не имеет права съедать ядро боевого
# сервера на четверть часа — она делит его с диспетчерами.
docker run -d --name "$CONTAINER" \
    -e POSTGRES_PASSWORD=verify \
    -v "${BACKUP_DIR}:/backups:ro" "$PG_IMAGE" \
    -c shared_buffers=512MB \
    -c work_mem=64MB \
    -c maintenance_work_mem=256MB \
    -c fsync=off -c full_page_writes=off \
    >/dev/null

for _ in $(seq 1 60); do
    docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 && break
    sleep 2
done

docker exec "$CONTAINER" createdb -U postgres verify

# ⚠ ОДНА ОШИБКА pg_restore ЗДЕСЬ ОЖИДАЕМА, И ИЗ-ЗА НЕЁ ПРОВЕРКА НЕ РАБОТАЛА
# НИ РАЗУ (03.09). Дамп содержит `REFRESH MATERIALIZED VIEW
# mv_conversation_stats`, а витрина через функцию `stats_work_hour` читает
# таблицу `app_settings`. Порядок восстановления pg_dump'у неизвестен —
# зависимость спрятана в теле функции, — и REFRESH выполняется раньше данных.
#
# Восстановление при этом проходит ЦЕЛИКОМ: все таблицы совпадают с боем
# цифра в цифру, пустой остаётся только витрина (учение 19.08, записано в
# `restore-check.sh`). Лечится одной командой, она ниже.
#
# А вот проверка на этом умирала. `pg_restore` возвращает 1, `set -e` валит
# скрипт на этой строке — ДО единой проверки, — и наружу уходило «🔴
# backup-verify: скрипт упал». Сторож, который кричит всегда, хуже
# отсутствующего: первая же настоящая поломка копий утонула бы в этом крике.
#
# Поэтому: код возврата разбираем, а не глотаем. Знакомая ошибка про витрину
# — не повод для тревоги; ЛЮБАЯ другая — повод.
# ⚠ ФОРМА `if ! ...` ВЫБРАНА НАРОЧНО. Ловушка ERR (строка 65) срабатывает на
# любой неудаче отдельной командой — `set +e` её НЕ гасит, проверено прогоном
# 03.09: скрипт всё равно рапортовал «упал на строке». В условии же неудача —
# законный исход, и ловушка молчит.
#
# ⚠ ИМЕНА ПЕРЕМЕННЫХ ЛАТИНИЦЕЙ. Кириллическое имя bash не принимает вовсе:
# `FOO=1: command not found` — и скрипт валится уже на разборе ошибки.
RESTORE_ERR="$(mktemp)"
RESTORE_RC=0
if ! docker exec "$CONTAINER" pg_restore -U postgres -d verify --no-owner \
    "/backups/$(basename "$LATEST")" 2>"$RESTORE_ERR"; then
    RESTORE_RC=1
fi
if [ "$RESTORE_RC" -ne 0 ]; then
    errs=$(grep -c '^pg_restore: error' "$RESTORE_ERR" 2>/dev/null || true)
    about_mv=$(grep -c 'mv_conversation_stats' "$RESTORE_ERR" 2>/dev/null || true)
    if [ "${errs:-0}" -gt 1 ] || [ "${about_mv:-0}" -eq 0 ]; then
        log "!! pg_restore упал не на витрине:"
        sed -n '1,20p' "$RESTORE_ERR" | while IFS= read -r line; do log "   $line"; done
        rm -f "$RESTORE_ERR"
        fail_center "🔴 backup-verify: дамп не восстанавливается" backup.verify_failed
        exit 1
    fi
    log "витрина при восстановлении не обновилась — это ожидаемо, обновляем отдельно"
fi
rm -f "$RESTORE_ERR"

# ⚠ ANALYZE ПОСЛЕ ВОССТАНОВЛЕНИЯ — НЕ ФОРМАЛЬНОСТЬ. У свежевосстановленной
# базы статистики нет ВООБЩЕ, и планировщик выбирает планы вслепую. Замер
# 03.09: обновление витрины без `ANALYZE` шло 831 с при настройках образа и
# всё ещё сотни секунд с поднятой памятью — против 11-13 с в бою. Разница не
# в железе, а в том, что боевая база проанализирована автовакуумом.
#
# Это же относится и к НАСТОЯЩЕМУ восстановлению: поднять базу из дампа и
# сразу пустить на неё людей — значит отдать им систему, работающую в разы
# медленнее обычного, без единой ошибки в логах.
log "собираем статистику (ANALYZE) — без неё планировщик работает вслепую"
docker exec "$CONTAINER" psql -U postgres -d verify -q -c "ANALYZE"

# ⚠ ОБНОВЛЯЕМ ВИТРИНУ И ПРОВЕРЯЕМ ЕЁ. Это не «доведение до зелёного», а
# проверка того самого шага, который в аварии придётся делать руками: если
# `REFRESH` не пройдёт (уехала функция, разъехалась схема), человек узнает
# об этом сегодня, а не в день восстановления.
# Потолок по времени: проверка не имеет права идти бесконечно на боевой
# машине. Не уложились — это находка сама по себе, а не повод ждать дальше.
if ! docker exec "$CONTAINER" psql -U postgres -d verify -q \
    -c "SET statement_timeout = '600s'" \
    -c "REFRESH MATERIALIZED VIEW mv_conversation_stats"; then
    fail_center "🔴 backup-verify: витрина статистики не обновилась за 10 минут"
    exit 1
fi
MV_ROWS=$(docker exec "$CONTAINER" psql -U postgres -d verify -tAc \
    "SELECT count(*) FROM mv_conversation_stats")
if [ "${MV_ROWS:-0}" -lt 1 ]; then
    fail_center "🔴 backup-verify: витрина статистики пуста после REFRESH (${MV_ROWS})"
    exit 1
fi
log "витрина обновлена: строк ${MV_ROWS}"

RESULT=$(docker exec "$CONTAINER" psql -U postgres -d verify -tAc "
  SELECT 'users='   || (SELECT count(*) FROM users)          || ' ' ||
         'convs='   || (SELECT count(*) FROM conversations)  || ' ' ||
         'msgs='    || (SELECT count(*) FROM messages)       || ' ' ||
         'last_msg='|| coalesce((SELECT max(created_at)::date::text FROM messages), 'none')")
ELAPSED=$(( $(date +%s) - START ))
log "результат: ${RESULT} (восстановление ${ELAPSED} c)"

# --- проверка 1: в базе вообще есть пользователи (иначе дамп не тот) ---------
USERS=$(printf '%s' "$RESULT" | grep -o 'users=[0-9]*' | cut -d= -f2)
if [ -z "$USERS" ] || [ "$USERS" -lt 1 ]; then
    fail_center "🔴 backup-verify: в восстановленной базе нет пользователей (${RESULT})" backup.verify_failed
    exit 1
fi

# --- проверка 2: свежесть (последнее сообщение не старше MAX_AGE_DAYS) -------
# 'none' допустим только на совсем пустой системе — тогда это тоже повод
# посмотреть глазами, но не будить ночью: шлём предупреждение, не ошибку.
LAST=$(printf '%s' "$RESULT" | grep -o 'last_msg=[0-9-]*' | cut -d= -f2 || true)
if [ -z "$LAST" ]; then
    fail_center "дамп восстановился, но сообщений в нём нет (${RESULT})" backup.verify_failed
    exit 0
fi
if [ "$(date -d "$LAST" +%s)" -lt "$(date -d "${MAX_AGE_DAYS} days ago" +%s)" ]; then
    fail_center "🔴 backup-verify: восстановление подозрительно — данные старше ${MAX_AGE_DAYS} сут (${RESULT})" backup.verify_failed
    exit 1
fi

# Об УСПЕХЕ не уведомляем: то же правило, что у backup.ok в
# app/api/routes/internal.py — центр, в котором каждую ночь появляется «всё
# хорошо», перестают открывать, и «всё плохо» тонет вместе с остальным.
log "backup-verify OK: ${RESULT}, восстановление ${ELAPSED} c"
log "backup-verify OK"
