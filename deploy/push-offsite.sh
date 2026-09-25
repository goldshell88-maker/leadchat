#!/usr/bin/env bash
# ============================================================================
# LeadChat — вторая копия бэкапа в другую страну (05 §6.3).
#
#   BACKUP_DIR=/var/leadchat/backups ./push-offsite.sh
#
# Хранилище Timeweb стоит у того же поставщика, что и сервер: отказ на его
# стороне (блокировка аккаунта, сбой региона) уносит и прод, и бэкапы разом.
# Эта копия едет на нидерландский сервер по мосту WireGuard — другая страна,
# другой поставщик, другая учётка.
#
# Отправляет то, что backup.sh уже зашифровал (BACKUP_DIR/offsite): дамп базы
# и архив вложений. Открытых копий на втором сервере не бывает.
# Идемпотентен: повторный запуск с теми же файлами ничего не делает.
# ============================================================================
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/var/leadchat/backups}"
STAGE="${BACKUP_DIR}/offsite"
OFFSITE_HOST="${OFFSITE_HOST:-10.10.0.2}"     # адрес напарника в мосте wg0
OFFSITE_USER="${OFFSITE_USER:-root}"
OFFSITE_DIR="${OFFSITE_DIR:-/var/backups/leadchat-offsite}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/backup_push}"
KEEP_DAYS="${KEEP_DAYS:-30}"

SSH_OPTS=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=10
          -o StrictHostKeyChecking=accept-new)

# ОТКАЗ ВТОРОЙ КОПИИ ДОЛЖЕН БЫТЬ СЛЫШЕН (проверка 24.09). Раньше fail() только
# печатал строку в лог, и затяжной отказ — отозванный ключ, лежащий мост, диск
# напарника — никто бы не заметил, пока копия не понадобится. Из .env берётся
# ОДИН токен уведомлений, а не весь файл: общие переменные вроде BACKUP_DIR
# переучили бы скрипт молча (см. crontab.leadchat).
ENV_FILE="${ENV_FILE:-/srv/leadchat/.env}"
if [ -z "${INTERNAL_SERVICE_TOKEN:-}" ] && [ -r "$ENV_FILE" ]; then
    INTERNAL_SERVICE_TOKEN="$(sed -n 's/^INTERNAL_SERVICE_TOKEN=//p' "$ENV_FILE" | tail -1 | tr -d "\"'")"
fi
INTERNAL_NOTIFY_URL="${INTERNAL_NOTIFY_URL:-http://127.0.0.1:8000/api/v1/internal/notify}"

log()    { printf '%s  офсайт: %s\n' "$(date -u +%FT%TZ)" "$*"; }
notify_center() {  # $1 — вид события, $2 — подробность
    [ -n "${INTERNAL_SERVICE_TOKEN:-}" ] || return 0
    local detail payload
    detail=$(printf '%s' "${2:-}" | tr -d '"\\' | tr '\n\t' '  ' | cut -c1-450)
    payload=$(printf '{"kind":"%s","source":"push-offsite.sh","detail":"%s"}' "$1" "${detail}")
    curl -fsS --max-time 10 -X POST "${INTERNAL_NOTIFY_URL}" \
        -H 'Content-Type: application/json' \
        -H "X-Internal-Token: ${INTERNAL_SERVICE_TOKEN}" \
        --data "${payload}" >/dev/null 2>&1 \
        || log "уведомление в центр не ушло (${1})"
    return 0
}
fail()   {
    printf '%s  !! офсайт: %s\n' "$(date -u +%FT%TZ)" "$*" >&2
    notify_center "backup.second_copy_failed" "$*"
    exit 1
}
trap 'rc=$?; notify_center "backup.second_copy_failed" "команда на строке ${LINENO} вернула ${rc}"' ERR
remote() { ssh "${SSH_OPTS[@]}" "${OFFSITE_USER}@${OFFSITE_HOST}" "$@"; }
newest() { ls -1t "$STAGE"/$1 2>/dev/null | head -1 || true; }
size_of() { stat -c %s "$1" 2>/dev/null || stat -f %z "$1"; }

# Файл едет под временным именем, а sha256 сверяется уже после переименования:
# доехавшая битой копия не должна остаться под правильным именем.
send() {
    local file="$1" name local_sha remote_sha
    name="$(basename "$file")"
    local_sha="$(sha256sum "$file" | cut -d' ' -f1)"
    remote_sha="$(remote "sha256sum '$OFFSITE_DIR/$name' 2>/dev/null | cut -d' ' -f1" || true)"
    if [ "$local_sha" = "$remote_sha" ]; then
        log "$name уже на месте, контрольная сумма совпадает"
        return 0
    fi
    log "отправляю $name ($(( $(size_of "$file") / 1024 )) КБ) на ${OFFSITE_HOST}"
    scp "${SSH_OPTS[@]}" -q "$file" "${OFFSITE_USER}@${OFFSITE_HOST}:${OFFSITE_DIR}/${name}.part"
    remote "mv '$OFFSITE_DIR/${name}.part' '$OFFSITE_DIR/$name'"
    remote_sha="$(remote "sha256sum '$OFFSITE_DIR/$name' | cut -d' ' -f1")"
    if [ "$local_sha" != "$remote_sha" ]; then
        remote "rm -f '$OFFSITE_DIR/$name'"
        fail "контрольные суммы $name разошлись, битая копия удалена"
    fi
    log "$name доставлен, sha256 совпал"
}

[ -r "$SSH_KEY" ] || fail "нет ключа $SSH_KEY"

# Шаблон из «?» — только ночные копии: снимки перед выкаткой наружу не ездят.
DUMP="$(newest 'db_????????_????.dump.age')"
[ -n "$DUMP" ] || fail "в $STAGE нет зашифрованной ночной копии — backup.sh отработал?"
# Обрезанный файл не отправляем: заменить хорошую копию плохой хуже, чем не
# отправить ничего. Порог низкий: база заказчика пока маленькая.
SIZE="$(size_of "$DUMP")"
[ "$SIZE" -ge 10240 ] || fail "$(basename "$DUMP") подозрительно мал ($SIZE Б) — не отправляю"

remote true 2>/dev/null || fail "нет связи с ${OFFSITE_HOST} по мосту (wg0 поднят?)"
remote "mkdir -p '$OFFSITE_DIR'"

send "$DUMP"

MEDIA="$(newest 'media_*.tar.age')"
if [ -n "$MEDIA" ]; then
    send "$MEDIA"
else
    log "архива вложений нет — вложения не отправлены"
fi

# Открытые db_*.dump на той стороне — копии, сделанные до шифрования;
# они уходят тем же сроком, что и остальные.
remote "find '$OFFSITE_DIR' -maxdepth 1 -type f \\( -name 'db_*.dump' -o -name 'db_*.dump.age' \
    -o -name 'media_*.tar.age' \\) -mtime +${KEEP_DAYS} -delete"

COUNT="$(remote "ls -1 '$OFFSITE_DIR'/db_*.dump.age 2>/dev/null | wc -l")"
log "готово: зашифрованных копий на ${OFFSITE_HOST} — ${COUNT}, храним ${KEEP_DAYS} суток"
