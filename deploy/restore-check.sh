#!/usr/bin/env bash
# ============================================================================
# LeadChat — квартальное «пожарное учение» по восстановлению
# (docs/05-DEPLOY-OPS.md §6.3, docs/07-TESTING-SECURITY.md §4.8 B3).
#
#   restore-check.sh                     # ОФСАЙТНАЯ копия (умолчание учения)
#   restore-check.sh --local             # самый свежий дамп из BACKUP_DIR
#   restore-check.sh <путь_к_дампу>      # конкретный файл
#
# Отличие от backup-verify.sh: тот молча гоняется по cron раз в месяц и шлёт
# алерт; этот запускается руками раз в квартал и печатает ОТЧЁТ для тикета —
# что восстановилось, за сколько, и что ещё нужно проверить руками.
#
# ПОЧЕМУ УЧЕНИЕ БЕРЁТ ОФСАЙТ, А НЕ ЛОКАЛЬНЫЙ ДАМП. Обе автоматические проверки
# — и ежемесячная (backup-verify.sh), и это учение — открывали дамп из
# BACKUP_DIR, то есть с ТОГО ЖЕ ДИСКА, что и боевая база. А восстанавливаться
# придётся из офсайтной копии: именно её достают, когда диска, сервера или
# аккаунта у поставщика больше нет. Эту копию не открывал никто и ни разу —
# ни автоматика, ни человек. Всё, что о ней известно, — что `rclone copy`
# вернул ноль; целостность файла, его читаемость pg_restore'ом, полнота схемы
# так и остались непроверенными. «Бэкап, который ни разу не восстанавливали,
# — это лотерейный билет», и до сих пор учение проверяло не тот билет.
#
# Откат на локальный дамп остался, но он ГРОМКИЙ: молчаливая подмена источника
# вернула бы ровно ту же дыру, только теперь с зелёным отчётом об учении.
#
# Целевой RTO — 2 часа на полное восстановление сервиса (§6.3).
# Скрипт замеряет только шаг «БД из дампа» — самый длинный автоматический кусок.
#
# Боевую БД не трогает: всё происходит в одноразовом контейнере.
# ============================================================================
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/srv/leadchat}"
ENV_FILE="${DEPLOY_DIR}/.env"
CONTAINER="pg_restore_check_$$"
PG_IMAGE="${PG_IMAGE:-postgres:16-alpine}"

# Как и в backup.sh: заданное в окружении значение приоритетнее .env —
# иначе операционную настройку не поправить, не трогая файл с секретами.
_PRESET_RCLONE_REMOTE="${RCLONE_REMOTE:-}"
_PRESET_BACKUP_DIR="${BACKUP_DIR:-}"
[ -f "$ENV_FILE" ] && { set -a; . "$ENV_FILE"; set +a; }
if [ -n "${_PRESET_RCLONE_REMOTE}" ]; then RCLONE_REMOTE="${_PRESET_RCLONE_REMOTE}"; fi
if [ -n "${_PRESET_BACKUP_DIR}" ]; then BACKUP_DIR="${_PRESET_BACKUP_DIR}"; fi
BACKUP_DIR="${BACKUP_DIR:-/var/backups/leadchat}"
# Секретная половина ключа офсайт-копий. На сервере её нет — на учение ключ
# кладут на время (RUNBOOK-DEPLOY §10.2).
AGE_IDENTITY="${BACKUP_AGE_IDENTITY:-$HOME/.config/leadchat/backup-age-key.txt}"

OFFSITE_TMP=""     # каталог со скачанной копией; убирается в cleanup

# -v обязателен: анонимный том PGDATA иначе остаётся на диске после каждого
# прогона (проверено на проде — по ~80 МБ за учение).
#
# Скачанная офсайтная копия убирается здесь же: дамп базы клиента не должен
# оставаться лежать в /tmp после учения.
cleanup() {
    docker rm -f -v "$CONTAINER" >/dev/null 2>&1 || true
    if [ -n "$OFFSITE_TMP" ]; then rm -rf "$OFFSITE_TMP"; fi
    return 0
}
trap cleanup EXIT

local_dump() { ls -1t "${BACKUP_DIR}"/db_*.dump 2>/dev/null | head -1 || true; }

# Кладёт офсайтную копию в OFFSITE_TMP и заполняет DUMP. 0 — получилось.
# Значение НЕ печатается в stdout: путь нужен вызывающему, а объяснения —
# человеку, и смешивать их в одном канале означало бы разбирать вывод.
fetch_offsite() {
    if [ -z "${RCLONE_REMOTE:-}" ]; then
        echo "!! офсайт: RCLONE_REMOTE не задан" >&2; return 1
    fi
    if ! command -v rclone >/dev/null 2>&1; then
        echo "!! офсайт: rclone не установлен" >&2; return 1
    fi
    if ! command -v age >/dev/null 2>&1; then
        echo "!! офсайт: age не установлен — копию нечем расшифровать" >&2; return 1
    fi
    if [ ! -r "$AGE_IDENTITY" ]; then
        echo "!! офсайт: нет ключа расшифровки ${AGE_IDENTITY}" >&2; return 1
    fi
    local name
    # Имена дампов — db_ГГГГММДД_ЧЧММ.dump.age, поэтому лексикографический
    # порядок совпадает с хронологическим: `sort | tail -1` — самый свежий.
    # Времени изменения объекта в хранилище не спрашиваем: у S3 оно меняется
    # при перекладывании между классами хранения и врёт про возраст данных.
    name="$(rclone lsf "${RCLONE_REMOTE}/db/" --include 'db_*.dump.age' 2>/dev/null \
            | sort | tail -1)"
    if [ -z "$name" ]; then
        echo "!! офсайт: в ${RCLONE_REMOTE}/db/ нет ни одной зашифрованной копии" >&2; return 1
    fi
    OFFSITE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/leadchat-restore.XXXXXX")"
    echo "качаем офсайтную копию ${name}…"
    rclone copyto "${RCLONE_REMOTE}/db/${name}" "${OFFSITE_TMP}/${name}" || return 1
    if ! age -d -i "$AGE_IDENTITY" -o "${OFFSITE_TMP}/${name%.age}" "${OFFSITE_TMP}/${name}"; then
        echo "!! офсайт: ${name} не расшифровывается этим ключом" >&2; return 1
    fi
    rm -f "${OFFSITE_TMP}/${name}"
    DUMP="${OFFSITE_TMP}/${name%.age}"
    return 0
}

DUMP=""
SOURCE=""
FELL_BACK=0
case "${1:-}" in
    "")
        # Умолчание учения — офсайт. Провал скачивания сам по себе находка:
        # именно так и выглядит «копии нет», если узнать об этом в день аварии.
        if fetch_offsite; then
            SOURCE="ОФСАЙТ ${RCLONE_REMOTE}/db"
        else
            DUMP="$(local_dump)"
            SOURCE="локальный ${BACKUP_DIR} (офсайт взять НЕ УДАЛОСЬ)"
            FELL_BACK=1
        fi
        ;;
    --local)
        DUMP="$(local_dump)"
        SOURCE="локальный ${BACKUP_DIR} (по явной просьбе --local)"
        ;;
    *)
        DUMP="$1"
        SOURCE="указан руками"
        ;;
esac
[ -n "$DUMP" ] && [ -f "$DUMP" ] || { echo "!! дамп не найден: ${DUMP:-<пусто>}" >&2; exit 2; }

hr() { printf '%s\n' "------------------------------------------------------------"; }

echo "LeadChat — учение по восстановлению"
hr
echo "источник: ${SOURCE}"
echo "дамп:     ${DUMP}"
DUMP_BYTES=$( (stat -c%s "$DUMP" 2>/dev/null || stat -f%z "$DUMP") )
if [ "$DUMP_BYTES" -ge 1048576 ]; then
    echo "размер:   $((DUMP_BYTES / 1048576)) MB"
else
    echo "размер:   $((DUMP_BYTES / 1024)) KB"   # до запуска команды дамп «весит» десятки КБ
fi
echo "дата:     $(date -u -r "$DUMP" +%FT%TZ 2>/dev/null || date -u +%FT%TZ)"
echo "образ:    ${PG_IMAGE}"
hr

START=$(date +%s)

echo "[1/4] поднимаем одноразовый postgres…"
docker run -d --name "$CONTAINER" -e POSTGRES_PASSWORD=verify \
    -v "$(dirname "$DUMP"):/backups:ro" "$PG_IMAGE" >/dev/null
for _ in $(seq 1 60); do
    docker exec "$CONTAINER" pg_isready -U postgres >/dev/null 2>&1 && break
    sleep 2
done

echo "[2/4] pg_restore…"
docker exec "$CONTAINER" createdb -U postgres verify
# ⚠ ОДНА ОШИБКА pg_restore ОЖИДАЕМА: REFRESH витрины mv_conversation_stats идёт
# раньше данных app_settings, которые она читает через stats_work_hour (разбор —
# в backup-verify.sh). pg_restore возвращает 1, и под `set -e` учение умирало
# сразу после «[2/4]» — без проверок, без ИТОГ и без пометки «не засчитано»
# (проверка 24.09). Знакомую ошибку прощаем, любую другую — нет.
RESTORE_ERR="$(mktemp)"
RESTORE_RC=0
if ! docker exec "$CONTAINER" pg_restore -U postgres -d verify --no-owner \
    "/backups/$(basename "$DUMP")" 2>"$RESTORE_ERR"; then
    RESTORE_RC=1
fi
if [ "$RESTORE_RC" -ne 0 ]; then
    errs=$(grep -c '^pg_restore: error' "$RESTORE_ERR" 2>/dev/null || true)
    about_mv=$(grep -c 'mv_conversation_stats' "$RESTORE_ERR" 2>/dev/null || true)
    if [ "${errs:-0}" -gt 1 ] || [ "${about_mv:-0}" -eq 0 ]; then
        echo "   !! pg_restore упал не на витрине:"
        sed -n '1,20p' "$RESTORE_ERR"
        rm -f "$RESTORE_ERR"
        echo "   !! УЧЕНИЕ НЕ ЗАСЧИТАНО: дамп не восстанавливается"
        exit 1
    fi
    echo "   витрина при восстановлении не обновилась — это ожидаемо, обновляем ниже"
fi
rm -f "$RESTORE_ERR"
# ANALYZE и витрина — часть восстановления, а не формальность: без статистики
# планировщик работает вслепую (обновление витрины шло 831 с против долей
# секунды после ANALYZE, замер 03.09), а без витрины отчёты показывают нули.
echo "   ANALYZE и обновление витрины…"
docker exec "$CONTAINER" psql -U postgres -d verify -q -c "ANALYZE;"
docker exec "$CONTAINER" psql -U postgres -d verify -q \
    -c "REFRESH MATERIALIZED VIEW mv_conversation_stats;"
RESTORE_SEC=$(( $(date +%s) - START ))

echo "[3/4] санити-проверки схемы и данных…"
hr
docker exec "$CONTAINER" psql -U postgres -d verify -P pager=off -c "
  SELECT 'таблиц в public'      AS показатель,
         count(*)::text          AS значение FROM information_schema.tables
         WHERE table_schema='public'
  UNION ALL SELECT 'пользователей',  (SELECT count(*)::text FROM users)
  UNION ALL SELECT 'аккаунтов Авито',(SELECT count(*)::text FROM avito_accounts)
  UNION ALL SELECT 'диалогов',       (SELECT count(*)::text FROM conversations)
  UNION ALL SELECT 'сообщений',      (SELECT count(*)::text FROM messages)
  UNION ALL SELECT 'партиций messages',
                   (SELECT count(*)::text FROM pg_inherits
                     WHERE inhparent = 'messages'::regclass)
  UNION ALL SELECT 'последнее сообщение',
                   (SELECT coalesce(max(created_at)::text,'нет') FROM messages);"
hr

echo "[4/4] офсайт-копия media…"
MEDIA_LOCAL="${MEDIA_ROOT:-/var/leadchat/media}"
if [ -z "$(find "$MEDIA_LOCAL" -type f -print -quit 2>/dev/null)" ]; then
    echo "   пропущено: локальных media нет — сравнивать не с чем"
elif [ -z "${RCLONE_REMOTE:-}" ] || ! command -v rclone >/dev/null 2>&1 \
        || ! command -v age >/dev/null 2>&1 || [ ! -r "$AGE_IDENTITY" ]; then
    echo "   пропущено: не настроены rclone, RCLONE_REMOTE, age или ключ расшифровки"
else
    ARCHIVE="$(rclone lsf "${RCLONE_REMOTE}/media-archive/" --include 'media_*.tar.age' 2>/dev/null \
               | sort | tail -1)"
    [ -n "$OFFSITE_TMP" ] || OFFSITE_TMP="$(mktemp -d "${TMPDIR:-/tmp}/leadchat-restore.XXXXXX")"
    if [ -z "$ARCHIVE" ]; then
        echo "   !! в ${RCLONE_REMOTE}/media-archive/ нет ни одного архива вложений"
    elif ! rclone copyto "${RCLONE_REMOTE}/media-archive/${ARCHIVE}" "${OFFSITE_TMP}/${ARCHIVE}" \
            || ! age -d -i "$AGE_IDENTITY" -o "${OFFSITE_TMP}/media.tar" "${OFFSITE_TMP}/${ARCHIVE}"; then
        echo "   !! архив ${ARCHIVE} не скачался или не расшифровался"
    else
        # Сравниваем с файлами, которые уже были на диске к сборке архива:
        # появившиеся позже в нём быть и не должны.
        STAMP_PART="${ARCHIVE#media_}"; STAMP_PART="${STAMP_PART%.tar.age}"
        BUILT_AT="${STAMP_PART:0:4}-${STAMP_PART:4:2}-${STAMP_PART:6:2} ${STAMP_PART:9:2}:${STAMP_PART:11:2} UTC"
        EXPECTED=$(find "$MEDIA_LOCAL" -type f ! -newermt "$BUILT_AT" | wc -l)
        GOT=$(tar -tf "${OFFSITE_TMP}/media.tar" | grep -vc '/$' || true)
        if [ "${GOT:-0}" -ge "$EXPECTED" ]; then
            echo "   media: в ${ARCHIVE} файлов ${GOT}, на диске к его сборке ${EXPECTED} — копия полная"
        else
            echo "   !! media: в ${ARCHIVE} файлов ${GOT}, а на диске к его сборке было ${EXPECTED} — разобраться (07 §4.8 B4)"
        fi
    fi
fi

TOTAL=$(( $(date +%s) - START ))
hr
echo "ИТОГ"
echo "  источник дампа:      ${SOURCE}"
echo "  восстановление БД:   ${RESTORE_SEC} c"
echo "  всего по скрипту:    ${TOTAL} c"
echo "  целевой RTO сервиса: 7200 c (2 часа, 05 §6.3)"
if [ "$FELL_BACK" = "1" ]; then
    # Зелёный отчёт про локальный дамп отвечает не на тот вопрос: он говорит,
    # что читается копия с диска, который в аварии и погибнет. Учение при
    # таком исходе НЕ ЗАСЧИТАНО — иначе тикет закроют, а офсайт так и
    # останется ни разу не открытым.
    echo "  !! УЧЕНИЕ НЕ ЗАСЧИТАНО: офсайтную копию открыть не удалось,"
    echo "     проверен локальный дамп — то есть не тот, из которого будут восстанавливаться"
fi
hr
cat <<'CHECKLIST'
Что доделать РУКАМИ и записать в тикет учения (07 §4.8 B3):
  [ ] ANALYZE и обновление витрины скрипт сделал сам (шаг 2). В настоящей
      аварии их тоже не пропускать: без ANALYZE система после восстановления
      работает в разы медленнее без единой ошибки в логах, а без витрины
      любой отчёт показывает нули.
  [ ] поднять API против восстановленной БД, залогиниться, открыть диалог
  [ ] проверить, что токены Авито расшифровываются (нужен TOKEN_ENC_KEY
      из менеджера секретов — без него дамп бесполезен, 05 §6.4)
  [ ] восстановить одно вложение из офсайта и открыть его (B4)
  [ ] зафиксировать реальное время «от нуля до рабочего сервиса» и сравнить с RTO
CHECKLIST
