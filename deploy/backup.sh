#!/usr/bin/env bash
# ============================================================================
# LeadChat — ежедневный бэкап (docs/05-DEPLOY-OPS.md §6.2).
# cron: 30 3 * * *  /srv/leadchat/deploy/backup.sh >> /var/log/leadchat-backup.log 2>&1
#
# Что делает:
#   0. Проверяет свободное место (<15% — алерт, §7.4).
#   1. pg_dump -Fc (custom, сжатие zstd; и схема, и данные) в ${BACKUP_DIR}.
#   2. Санити: дамп читается pg_restore --list и содержит данные ключевых таблиц.
#   3. Офсайт: дамп шифруется (age) и уезжает в S3; первого числа — ещё и
#      месячная копия. Открытый дамп остаётся только на этом диске.
#   4. Media: зашифрованный архив каталога вложений — туда же.
#   5. Ротация: локально 14 суточных, офсайт 30 суточных + 12 месячных;
#      снимков перед выкаткой (BACKUP_LABEL=deploy) — только последние 3.
#
# Любое падение -> ГРОМКАЯ строка в лог + уведомление в центр + ненулевой код.
# «Громкая строка» здесь не косметика: центр живёт внутри системы и вместе с ней
# же может лежать, а без записи в лог падение бэкапа выглядит как его отсутствие.
# Redis НЕ бэкапится сознательно (§6.4): очередь догонит reconciliation.
# ============================================================================
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/srv/leadchat}"
COMPOSE_FILE="${COMPOSE_FILE:-${DEPLOY_DIR}/docker-compose.yml}"
ENV_FILE="${DEPLOY_DIR}/.env"

cd "$DEPLOY_DIR"

# Операционные (не секретные) переменные, заданные в окружении — например прямо
# в строке cron рядом с DEPLOY_DIR — имеют приоритет над .env. Это позволяет
# поправить путь бэкапов или адрес офсайта, не редактируя файл с секретами.
_PRESET_RCLONE_REMOTE="${RCLONE_REMOTE:-}"
_PRESET_BACKUP_DIR="${BACKUP_DIR:-}"

# shellcheck disable=SC1090
set -a; source "$ENV_FILE"; set +a

if [ -n "${_PRESET_RCLONE_REMOTE}" ]; then RCLONE_REMOTE="${_PRESET_RCLONE_REMOTE}"; fi
if [ -n "${_PRESET_BACKUP_DIR}" ]; then BACKUP_DIR="${_PRESET_BACKUP_DIR}"; fi

BACKUP_DIR="${BACKUP_DIR:-/var/backups/leadchat}"
BACKUP_KEEP_DAILY="${BACKUP_KEEP_DAILY:-14}"
BACKUP_KEEP_OFFSITE_DAILY="${BACKUP_KEEP_OFFSITE_DAILY:-30}"
BACKUP_KEEP_OFFSITE_MONTHLY="${BACKUP_KEEP_OFFSITE_MONTHLY:-12}"   # месяцев
# Снимок ПЕРЕД ВЫКАТКОЙ (ship.sh, шаг 1) приходит с BACKUP_LABEL=deploy и
# живёт по своим правилам: имя db_<штамп>_deploy.dump, офсайт не нужен (его
# работа — откатить миграцию через минуту на этом же хосте, а от гибели диска
# страхует ночная копия), и хранятся только последние BACKUP_KEEP_LABELLED.
# Без этого каждая выкатка оставляла полный дамп на 14 суток, как ночной:
# 12.09 таких снимков лежало 165 на 10 ГБ против 15 ночных — диск 83 %, а
# каждый из них ещё и уезжал в S3 и на второй сервер.
BACKUP_LABEL="${BACKUP_LABEL:-}"
BACKUP_KEEP_LABELLED="${BACKUP_KEEP_LABELLED:-3}"
# Нижняя граница размера — страховка от обрезанного файла, НЕ от пустой базы:
# дамп схемы с партициями и без данных ≈ 32 КБ, поэтому порог в 1 МБ до запуска
# команды валил бы каждый ночной прогон. Содержательную проверку делает
# оглавление (MIN_DUMP_TOC_ENTRIES + REQUIRED_TABLES) — она не зависит от объёма.
MIN_DUMP_BYTES="${MIN_DUMP_BYTES:-20000}"
MIN_DUMP_TOC_ENTRIES="${MIN_DUMP_TOC_ENTRIES:-50}"
REQUIRED_TABLES="${REQUIRED_TABLES:-users avito_accounts conversations messages}"
# Публичный ключ: сервер умеет зашифровать копию, но не расшифровать её.
# Секретная половина хранится вне сервера (RUNBOOK-DEPLOY §10.2).
AGE_RECIPIENTS="${BACKUP_AGE_RECIPIENTS:-${DEPLOY_DIR}/deploy/backup-age-recipients.txt}"
# Зашифрованные копии ждут здесь отправки во вторую страну (push-offsite.sh).
OFFSITE_STAGE="${BACKUP_DIR}/offsite"

# COMPOSE_FILE может перечислять несколько файлов через ':' — как это делает
# сам docker compose (прод собран из prod.yml + override.yml).
IFS=':' read -r -a _compose_files <<< "$COMPOSE_FILE"
DC=(docker compose)
for _f in "${_compose_files[@]}"; do DC+=(-f "$_f"); done
DC+=(--env-file "$ENV_FILE")

STAMP=$(date -u +%Y%m%d_%H%M)
DUMP="db_${STAMP}${BACKUP_LABEL:+_${BACKUP_LABEL}}.dump"

log() { printf '%s  %s\n' "$(date -u +%FT%TZ)" "$*"; }

# --- уведомление в центр внутри системы (docs/14-NOTIFICATIONS.md §2.1) -------
# Скрипт живёт на хосте и код приложения импортировать не может, поэтому
# сообщает о себе служебной ручкой POST /internal/notify с сервисным токеном.
# Заголовок и важность выбирает СЕРВЕР по виду события — отсюда уходит только
# вид и подробность одной строкой, поэтому админ читает «резервное копирование
# не выполнилось», а не «rc=2 at line 118».
#
# Три правила, без которых эта строка сделала бы хуже, чем её отсутствие:
#   1. нет токена — молча выходим (dev-хост, свежий сервер);
#   2. любая ошибка curl гасится: уведомление не имеет права ронять бэкап;
#   3. функция ВСЕГДА возвращает 0 — иначе она сама сработала бы ERR-trap'ом.
#
# Адрес по умолчанию — loopback, а НЕ https://${DOMAIN}. Публичный маршрут к
# этой ручке закрыт: nginx отдаёт на /api/v1/internal/ 404 (ручка принимает
# сервисный токен — публичный путь к ней лишняя мишень), а порт api опубликован
# на 127.0.0.1 (docker-compose.prod.yml). Скрипт живёт на том же хосте, ему
# публичный edge не нужен. Второй довод против домена: на https-адресе легко
# поймать 301, а curl без -L на редиректе молча НИЧЕГО не отправляет.
INTERNAL_NOTIFY_URL="${INTERNAL_NOTIFY_URL:-http://127.0.0.1:8000/api/v1/internal/notify}"

notify_center() {  # $1 — вид события, $2 — подробность (необязательно)
    [ -n "${INTERNAL_SERVICE_TOKEN:-}" ] || return 0
    local detail payload
    # Кавычки и переводы строк из сообщений bash сломали бы JSON — вычищаем.
    detail=$(printf '%s' "${2:-}" | tr -d '"\\' | tr '\n\t' '  ' | cut -c1-450)
    payload=$(printf '{"kind":"%s","source":"backup.sh","detail":"%s"}' "$1" "${detail}")
    curl -fsS --max-time 10 -X POST "${INTERNAL_NOTIFY_URL}" \
        -H 'Content-Type: application/json' \
        -H "X-Internal-Token: ${INTERNAL_SERVICE_TOKEN}" \
        --data "${payload}" >/dev/null 2>&1 \
        || log "!! уведомление в центр не ушло (${1}) — ${INTERNAL_NOTIFY_URL}"
    return 0
}

fail() { log "!! ПРОВАЛ: $*"; notify_center "backup.failed" "$*"; exit 1; }
trap 'rc=$?; log "!! ПРОВАЛ: команда на строке ${LINENO} вернула ${rc}"; notify_center "backup.failed" "команда на строке ${LINENO} вернула ${rc}"' ERR

filesize() { stat -c%s "$1" 2>/dev/null || stat -f%z "$1"; }  # GNU || BSD
human() { local b=$1; if [ "$b" -ge 1048576 ]; then echo "$((b / 1048576)) MB"; else echo "$((b / 1024)) KB"; fi; }
# stdin -> зашифрованный файл $1. Имя появляется только целиком записанным:
# push-offsite.sh не должен увидеть недописанную копию.
encrypt_to() { age -R "${AGE_RECIPIENTS}" -o "$1.part" && mv -f "$1.part" "$1"; }

# --- 0. Диск -----------------------------------------------------------------
FREE_PCT=$(df -P "$BACKUP_DIR" | awk 'NR==2 {gsub("%","",$5); print 100-$5}')
log "свободно на диске: ${FREE_PCT}%"
if [ "$FREE_PCT" -lt 15 ]; then
    notify_center "disk.low" "свободно ${FREE_PCT}% на разделе ${BACKUP_DIR} (порог 15%)"
fi
if [ "$FREE_PCT" -lt 5 ]; then
    fail "меньше 5% свободного — дамп не начинаем, чтобы не добить диск"
fi

# --- 1. Дамп -----------------------------------------------------------------
# -Fc содержит схему и данные, восстанавливается pg_restore. Сжатие — zstd
# (PostgreSQL 16): на боевой базе 12.09 — 78 МБ против 97 МБ у zlib при той
# же скорости (10 с); zstd:15 даёт ещё 3 МБ за 74 с — не стоит того. Все
# читатели дампа (backup-verify, restore-check, второй сервер) — тот же образ
# postgres:16-alpine, zstd там есть.
# ${BACKUP_DIR} смонтирован в контейнер postgres как /backups (compose).
DUMP_COMPRESS="${DUMP_COMPRESS:-zstd:9}"
log "pg_dump (${DUMP_COMPRESS}) -> ${BACKUP_DIR}/${DUMP}"
"${DC[@]}" exec -T postgres pg_dump -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" \
    -Fc --no-owner --compress="${DUMP_COMPRESS}" -f "/backups/${DUMP}"

# --- 2. Санити ---------------------------------------------------------------
SIZE=$(filesize "${BACKUP_DIR}/${DUMP}")
if [ "${SIZE}" -lt "${MIN_DUMP_BYTES}" ]; then
    fail "дамп подозрительно мал: ${SIZE} байт (порог ${MIN_DUMP_BYTES})"
fi

# Оглавление: читается ли файл вообще и есть ли в нём данные ключевых таблиц.
# Это и есть содержательная проверка «дамп не пустышка» — в отличие от размера
# она одинаково работает на пустой базе и на базе в 100 ГБ.
TOC=$("${DC[@]}" exec -T postgres pg_restore --list "/backups/${DUMP}")
TOC_ENTRIES=$(printf '%s\n' "$TOC" | grep -c '^[0-9]' || true)
if [ "${TOC_ENTRIES}" -lt "${MIN_DUMP_TOC_ENTRIES}" ]; then
    fail "оглавление дампа подозрительно короткое: ${TOC_ENTRIES} записей (порог ${MIN_DUMP_TOC_ENTRIES})"
fi
# Совпадение по префиксу — намеренно: messages партиционирована, и в оглавлении
# есть не «TABLE DATA public messages», а её партиции (messages_y2026m08 и т.д.).
#
# БЕЗ КАНАЛА, и это не вкусовщина. Здесь было
# `printf '%s\n' "$TOC" | grep -q ...`, и оно ЛОЖНО ПАДАЛО. `grep -q` выходит
# на первом совпадении и закрывает канал; `printf`, если ещё не дописал,
# получает SIGPIPE и возвращает 141 — а при `set -o pipefail` это статус всей
# цепочки. То есть таблица НАЙДЕНА, а проверка сообщает «в дампе нет данных».
#
# Пока оглавление было коротким (~160 записей, 12 КБ), всё влезало в буфер
# канала одним куском, printf успевал завершиться, и ошибка не проявлялась. С
# ростом до 26 партиций оглавление стало 45 КБ — и проверка начала валить
# каждый прогон на первой же таблице, чьи строки лежат в начале списка
# (`messages` «находился» только потому, что его партиции в самом конце).
#
# `case` работает со строкой в памяти: канала нет, гонки нет, SIGPIPE неоткуда
# взяться.
for _t in ${REQUIRED_TABLES}; do
    case "$TOC" in
        *"TABLE DATA public ${_t}"*) : ;;
        *) fail "в дампе нет данных таблицы ${_t} — восстанавливать будет нечего" ;;
    esac
done

# Резкое похудение относительно предыдущего дампа — повод посмотреть глазами,
# но не повод ронять бэкап (легально бывает после чистки партиций).
PREV=$(ls -1t "${BACKUP_DIR}"/db_*.dump 2>/dev/null | sed -n 2p || true)
if [ -n "${PREV}" ]; then
    PREV_SIZE=$(filesize "${PREV}")
    if [ "${SIZE}" -lt $((PREV_SIZE / 2)) ]; then
        log "!! дамп вдвое меньше предыдущего: ${SIZE} против ${PREV_SIZE} байт"
        # Копия сделалась, но выглядит подозрительно: это не провал, а повод
        # посмотреть, не пропали ли данные. Раньше об этом узнавал только лог.
        notify_center "backup.shrunk" "новый дамп ${SIZE} байт против ${PREV_SIZE} у вчерашнего"
    fi
fi
log "дамп ок: $(human "${SIZE}"), ${TOC_ENTRIES} записей в оглавлении, ключевые таблицы на месте"

# --- 3. Офсайт ---------------------------------------------------------------
if [ -n "${BACKUP_LABEL}" ]; then
    log "снимок «${BACKUP_LABEL}»: офсайт и media не трогаем — это работа ночного прогона"
elif [ -n "${RCLONE_REMOTE:-}" ]; then
    # Без шифрования копия наружу не уходит: открытый дамп в чужом хранилище —
    # это вся переписка клиентов у любого, кто получит доступ к бакету.
    command -v age >/dev/null 2>&1 || fail "не установлен age — офсайт без шифрования не отправляем"
    [ -s "${AGE_RECIPIENTS}" ] || fail "нет ключа шифрования ${AGE_RECIPIENTS}"
    mkdir -p "${OFFSITE_STAGE}"
    chmod 700 "${OFFSITE_STAGE}"

    log "офсайт: шифрую дамп"
    encrypt_to "${OFFSITE_STAGE}/${DUMP}.age" < "${BACKUP_DIR}/${DUMP}"
    log "офсайт: ${RCLONE_REMOTE}/db/"
    # Класс хранения на Timeweb S3 задаётся на уровне бакета (Cold/Premium),
    # per-object storage class их API отвергает с InvalidArgument 400.
    rclone copy "${OFFSITE_STAGE}/${DUMP}.age" "${RCLONE_REMOTE}/db/" ${RCLONE_EXTRA_ARGS:-}
    if [ "$(date -u +%d)" = "01" ]; then
        log "офсайт: месячная копия"
        rclone copy "${OFFSITE_STAGE}/${DUMP}.age" "${RCLONE_REMOTE}/db-monthly/"
    fi

    # --- 4. Media ---------------------------------------------------------------
    # Архив целиком, а не синхронизация по файлам: так вложения тоже уходят
    # зашифрованными, и пустой каталог не может стереть офсайт-копию.
    MEDIA="${MEDIA_ROOT:-/var/leadchat/media}"
    if [ -z "$(find "${MEDIA}" -type f -print -quit 2>/dev/null)" ]; then
        log "офсайт: media пуст (${MEDIA}) — архив не собираем"
    else
        MEDIA_ARCHIVE="media_${STAMP}.tar.age"
        tar -C "${MEDIA}" -cf - . | encrypt_to "${OFFSITE_STAGE}/${MEDIA_ARCHIVE}"
        log "офсайт: ${RCLONE_REMOTE}/media-archive/${MEDIA_ARCHIVE}"
        rclone copy "${OFFSITE_STAGE}/${MEDIA_ARCHIVE}" "${RCLONE_REMOTE}/media-archive/"
        rclone delete "${RCLONE_REMOTE}/media-archive/" --min-age "${BACKUP_KEEP_OFFSITE_DAILY}d"
    fi
else
    log "!! RCLONE_REMOTE не задан — офсайт-копии НЕТ, дамп только локально"
    notify_center "backup.offsite_missing" "RCLONE_REMOTE не задан, дамп остался только на сервере"
fi

# --- 5. Ротация --------------------------------------------------------------
log "ротация: локально ${BACKUP_KEEP_DAILY} суточных"
find "${BACKUP_DIR}" -name 'db_*.dump' -mtime "+${BACKUP_KEEP_DAILY}" -delete
# Помеченные снимки (db_<штамп>_<метка>.dump) — только последние N, в любой
# прогон: ночной тоже подчищает за выкатками. Шаблон из «?» не задевает
# ночные db_ГГГГММДД_ЧЧММ.dump. `ls` без совпадений вернул бы 2 и при
# pipefail сработал бы ERR-trap — отсюда `|| true` внутри группы.
log "ротация: помеченных снимков не больше ${BACKUP_KEEP_LABELLED}"
{ ls -1t "${BACKUP_DIR}"/db_????????_????_*.dump 2>/dev/null || true; } \
    | tail -n +$((BACKUP_KEEP_LABELLED + 1)) | xargs -r rm -f --
# Зашифрованные копии нужны на диске только до отправки во вторую страну;
# двое суток — запас на повторный ручной запуск push-offsite.sh.
if [ -d "${OFFSITE_STAGE}" ]; then
    find "${OFFSITE_STAGE}" -maxdepth 1 -name '*.age*' -mtime +1 -delete
fi
# Снимок перед выкаткой в облако не ходит вовсе (см. §3): иначе сбой S3 валил
# выкатку и поднимал ложное «копия не создана», хотя файл снимка уже записан.
if [ -z "${BACKUP_LABEL}" ] && [ -n "${RCLONE_REMOTE:-}" ]; then
    log "ротация офсайт: ${BACKUP_KEEP_OFFSITE_DAILY}d суточные, ${BACKUP_KEEP_OFFSITE_MONTHLY} месячных"
    rclone delete "${RCLONE_REMOTE}/db/" --min-age "${BACKUP_KEEP_OFFSITE_DAILY}d"
    # 12 месячных копий: всё, что старше года, уходит
    rclone delete "${RCLONE_REMOTE}/db-monthly/" --min-age "$((BACKUP_KEEP_OFFSITE_MONTHLY * 31))d"
fi

log "backup OK: ${DUMP} ($(human "${SIZE}"))"
# Отметка успеха — не для показа человеку, а для сторожа (14 §2.1): скрипт,
# который вообще не запустился, о себе не сообщит, и заметить это можно только
# по ТИШИНЕ. Сторож ждёт эту отметку не дольше 26 часов.
#
# ⚠ ТОЛЬКО НОЧНОЙ ПРОГОН (24.09). Снимок перед выкаткой тоже доходил сюда, а
# выкатки идут по нескольку раз в день: пропади ночная строка cron — сторож
# молчал бы сколько угодно, хотя в облако и на второй сервер не уезжало ничего.
if [ -z "${BACKUP_LABEL}" ]; then
    notify_center "backup.ok" "${DUMP}, $(human "${SIZE}")"
fi
