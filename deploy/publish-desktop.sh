#!/usr/bin/env bash
# ============================================================================
# LeadChat — публикация десктоп-релиза в /var/leadchat/download
# (docs/04-DESKTOP.md §6.1, docs/05-DEPLOY-OPS.md §3, регламент —
#  docs/12-DESKTOP-RELEASE.md).
#
#   deploy/publish-desktop.sh [опции] <каталог_артефактов>
#
# Каталог артефактов — плоский, ровно в том виде, в каком его собирает
# .github/workflows/desktop-release.yml (шаг «Собрать dist-release»):
#
#   dist-release/
#   ├── latest.json                         манифест tauri-updater
#   ├── LeadChat_1.4.2_x64-setup.exe        NSIS  — сотрудники + автообновление
#   ├── LeadChat_1.4.2_x64-setup.exe.sig    minisign-подпись (обязательна)
#   ├── LeadChat_1.4.2_x64_ru-RU.msi        MSI   — только GPO/Intune
#   └── LeadChat_1.4.2_x64_ru-RU.msi.sig
#
# Этот же скрипт вызывает CI — реализация выкладки одна на оба пути, поэтому
# ручной релиз с локальной Windows-машины (12 §6) делает ровно то же самое.
#
# ПОРЯДОК ОПЕРАЦИЙ ЖЁСТКИЙ (иначе клиент словит битое обновление):
#   1. бинарники и подписи        — новые файлы, старые не трогаем
#   2. симлинк LeadChat-Setup.exe — атомарный `mv -T` (кнопка на /login)
#   3. latest.json                — последним, атомарным `mv`
# Между шагами 1 и 3 существующие клиенты видят старый манифест и старый файл —
# то есть ничего не ломается. Обратный порядок дал бы окно, в котором манифест
# обещает версию, которой на сервере ещё нет.
#
# Опции:
#   --host HOST         VPS (env DEPLOY_HOST, по умолчанию домен прода)
#   --user USER         пользователь ssh (env DEPLOY_USER, по умолчанию deploy)
#   --dir DIR           каталог раздачи (env DOWNLOAD_DIR, /var/leadchat/download)
#   --base-url URL      публичный origin для проверки (env PUBLIC_BASE_URL)
#   --skip-verify       не ходить curl'ом после выкладки
#   --dry-run           показать план и выйти, ничего не менять
#   -y, --yes           не спрашивать подтверждение (режим CI)
#   -h, --help          эта справка
#
# Коды возврата: 0 — выложено; 1 — ошибка; 2 — ошибка в аргументах/артефактах.
# ============================================================================
set -euo pipefail

# Адрес по умолчанию — тот, что РАБОТАЕТ. Домен chat.partner-lead-centre.ru
# сюда не ведёт: его A-запись указывает на <сторонний сервер>, где живёт
# FreeScout — другой хелпдеск с собственным сертификатом на это же имя.
# Пока домен не направят на прод, умолчание обязано быть рабочим, иначе
# скрипт молча пойдёт в чужую систему.
DEPLOY_HOST="${DEPLOY_HOST:-188-225-34-82.sslip.io}"
DEPLOY_USER="${DEPLOY_USER:-deploy}"
DOWNLOAD_DIR="${DOWNLOAD_DIR:-/var/leadchat/download}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:-}"
STABLE_NAME="LeadChat-Setup.exe"

ASSUME_YES=0
DRY_RUN=0
SKIP_VERIFY=0
SRC=""

log()  { printf '%s  %s\n' "$(date -u +%FT%TZ)" "$*"; }
warn() { printf '%s  ~~ %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
fail() { printf '%s  !! %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
# справка = шапка файла: одно описание, которое нечему разъехаться с кодом
usage() { awk 'NR == 1 { next } /^#/ { sub(/^# ?/, ""); print; next } { exit }' "$0"; }

die() { fail "$1"; exit "${2:-1}"; }

# --- аргументы --------------------------------------------------------------
while [ $# -gt 0 ]; do
    case "$1" in
        --host)      DEPLOY_HOST="${2:-}"; shift 2 ;;
        --user)      DEPLOY_USER="${2:-}"; shift 2 ;;
        --dir)       DOWNLOAD_DIR="${2:-}"; shift 2 ;;
        --base-url)  PUBLIC_BASE_URL="${2:-}"; shift 2 ;;
        --skip-verify) SKIP_VERIFY=1; shift ;;
        --dry-run)   DRY_RUN=1; shift ;;
        -y|--yes)    ASSUME_YES=1; shift ;;
        -h|--help)   usage; exit 0 ;;
        -*)          usage >&2; die "неизвестная опция: $1" 2 ;;
        *)
            [ -z "$SRC" ] || { usage >&2; die "лишний аргумент: $1" 2; }
            SRC="$1"; shift ;;
    esac
done

[ -n "$SRC" ] || { usage >&2; die "не указан каталог артефактов" 2; }
[ -d "$SRC" ] || die "нет каталога ${SRC}" 2
[ -n "$DEPLOY_HOST" ] || die "пустой --host" 2
[ -n "$DEPLOY_USER" ] || die "пустой --user" 2
[ -n "$DOWNLOAD_DIR" ] || die "пустой --dir" 2
SRC="${SRC%/}"
DOWNLOAD_DIR="${DOWNLOAD_DIR%/}"
: "${PUBLIC_BASE_URL:=https://${DEPLOY_HOST}}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL%/}"
REMOTE="${DEPLOY_USER}@${DEPLOY_HOST}"

command -v ssh >/dev/null 2>&1 || die "нет ssh в PATH" 2

# --- 1. что именно публикуем ------------------------------------------------
# Ровно один NSIS-инсталлятор: два в каталоге = собрали дважды/смешали релизы,
# и непонятно, на какой из них должен указывать манифест.
shopt -s nullglob
setups=("$SRC"/*-setup.exe)
[ ${#setups[@]} -eq 1 ] || die "ожидал ровно один *-setup.exe в ${SRC}, нашёл ${#setups[@]}" 2
SETUP_PATH="${setups[0]}"
SETUP_NAME="$(basename "$SETUP_PATH")"

# Подпись — не «желательно», а условие работоспособности автообновления:
# клиент проверяет minisign зашитым pubkey и без .sig обновление не поставит.
[ -f "${SETUP_PATH}.sig" ] || die "нет подписи ${SETUP_NAME}.sig — релиз собран без TAURI_SIGNING_PRIVATE_KEY?" 2
[ -s "${SETUP_PATH}.sig" ] || die "подпись ${SETUP_NAME}.sig пустая" 2

MANIFEST="${SRC}/latest.json"
[ -f "$MANIFEST" ] || die "нет ${MANIFEST} (его генерирует desktop/scripts/make-latest-json.mjs)" 2

msis=("$SRC"/*.msi)
if [ ${#msis[@]} -eq 0 ]; then
    warn "в ${SRC} нет MSI — развёртывание через GPO этим релизом не покрыто"
else
    for msi in "${msis[@]}"; do
        if [ ! -f "${msi}.sig" ]; then
            warn "нет подписи для $(basename "$msi") — MSI едет через GPO, апдейтер его не трогает"
        fi
    done
fi

# --- 2. манифест обязан соответствовать артефактам --------------------------
# Рассинхрон манифеста и файлов — самый дорогой класс ошибок релиза: он
# проявляется не у нас, а у клиента, молчаливым отказом обновиться.
command -v python3 >/dev/null 2>&1 || die "нужен python3 для проверки latest.json" 2
VERSION="$(
    SETUP_NAME="$SETUP_NAME" python3 - "$MANIFEST" "${SETUP_PATH}.sig" <<'PY'
import json, os, sys
from urllib.parse import unquote

manifest, sig_path = sys.argv[1], sys.argv[2]
setup = os.environ["SETUP_NAME"]
try:
    with open(manifest, encoding="utf-8") as fh:
        data = json.load(fh)
except (OSError, ValueError) as exc:
    sys.exit(f"latest.json не читается: {exc}")

errs = []
version = data.get("version") or ""
if not version:
    errs.append("нет поля version")
plat = (data.get("platforms") or {}).get("windows-x86_64") or {}
url = plat.get("url") or ""
signature = (plat.get("signature") or "").strip()
if not url:
    errs.append("нет platforms.windows-x86_64.url")
elif unquote(url.rsplit("/", 1)[-1]) != setup:
    # имя файла в манифесте percent-encoded — сравниваем декодированное
    errs.append(f"url {url} не указывает на {setup}")
if not signature:
    errs.append("нет platforms.windows-x86_64.signature")
else:
    with open(sig_path, encoding="utf-8") as fh:
        if signature != fh.read().strip():
            errs.append(f"signature в манифесте не совпадает с {setup}.sig")
if version and version not in setup:
    errs.append(f"версия {version} не встречается в имени файла {setup}")
if errs:
    sys.exit("latest.json: " + "; ".join(errs))
print(version)
PY
)" || die "манифест не прошёл проверку (см. выше)" 2
[ -n "$VERSION" ] || die "не удалось определить версию из latest.json" 2

# --- 3. план ----------------------------------------------------------------
UPLOADS=("$SETUP_PATH" "${SETUP_PATH}.sig")
if [ ${#msis[@]} -gt 0 ]; then
    for msi in "${msis[@]}"; do
        UPLOADS+=("$msi")
        if [ -f "${msi}.sig" ]; then
            UPLOADS+=("${msi}.sig")
        fi
    done
fi

cat <<EOF
== публикация LeadChat ${VERSION} ==
  откуда   : ${SRC}
  куда     : ${REMOTE}:${DOWNLOAD_DIR}/
  origin   : ${PUBLIC_BASE_URL}
  файлы    : $(printf '%s ' "${UPLOADS[@]##*/}")
  симлинк  : ${STABLE_NAME} -> ${SETUP_NAME}
  манифест : latest.json (последним, атомарно)
EOF

if [ "$DRY_RUN" = "1" ]; then
    log "--dry-run: ничего не меняем"
    exit 0
fi

if [ "$ASSUME_YES" != "1" ]; then
    if [ -t 0 ]; then
        printf 'Публикуем? [y/N] '
        read -r answer
        case "$answer" in
            [yY] | [yY][eE][sS]) : ;;
            *) log "отменено"; exit 0 ;;
        esac
    else
        die "неинтерактивный запуск без --yes" 2
    fi
fi

# --- 4. заливка -------------------------------------------------------------
# rsync есть не везде (в git-bash на релизной Windows-машине его обычно нет) —
# тогда работаем обычным scp. Разницы для нас нет: файлы новые, докачивать
# нечего, а --delete здесь запрещён в любом случае (см. ниже).
put() {
    if command -v rsync >/dev/null 2>&1; then
        # без --delete: старые версии на сервере остаются намеренно — на них
        # откатываются (12 §5) и на них ещё могут стоять ссылки у сотрудников.
        # --chmod не используем: rsync на macOS (openrsync) такой опции не знает,
        # права выставляем отдельным chmod уже на сервере.
        rsync -av "$@" "${REMOTE}:${DOWNLOAD_DIR}/"
    else
        scp -p "$@" "${REMOTE}:${DOWNLOAD_DIR}/"
    fi
}

# Переменные раскрываются на нашей стороне намеренно: удалённой стороне уходит
# уже готовая команда с конкретными путями.
# shellcheck disable=SC2029
remote() { ssh "$REMOTE" "$@"; }

log "1/4 бинарники и подписи -> ${DOWNLOAD_DIR}/"
remote "mkdir -p '${DOWNLOAD_DIR}'"
put "${UPLOADS[@]}"

# rsync -a / scp -p сохраняют локальные права. Собранное в git-bash на Windows
# или под жёстким umask может приехать нечитаемым для nginx (он в контейнере и
# ходит другим uid, монтирование ro) — выставляем 644 явно.
NAMES=""
for f in "${UPLOADS[@]}"; do
    NAMES="${NAMES} '$(basename "$f")'"
done
remote "cd '${DOWNLOAD_DIR}' && chmod 644 ${NAMES}"

# Файл на месте и не обрезан? Сравниваем размер: подпись считается от полного
# файла, битая заливка = отказ обновления у всех клиентов сразу.
LOCAL_SIZE="$(wc -c < "$SETUP_PATH" | tr -d ' ')"
REMOTE_SIZE="$(remote "wc -c < '${DOWNLOAD_DIR}/${SETUP_NAME}'" | tr -d ' \r')"
[ "$LOCAL_SIZE" = "$REMOTE_SIZE" ] || die "размер на сервере ${REMOTE_SIZE} != локального ${LOCAL_SIZE} — заливка неполная"

log "2/4 симлинк ${STABLE_NAME} -> ${SETUP_NAME}"
# ln -sf по существующему симлинку не атомарен (unlink + symlink): в окне между
# ними кнопка «Скачать» на /login отдала бы 404. Поэтому создаём временный
# симлинк и переставляем его rename'ом — он атомарен в пределах каталога.
# -T (GNU) страхует от случая, когда цель оказалась каталогом: без него mv
# положил бы симлинк ВНУТРЬ него. На busybox опции нет — там обычный mv -f.
remote "set -e
        cd '${DOWNLOAD_DIR}'
        if [ -d '${STABLE_NAME}' ] && [ ! -L '${STABLE_NAME}' ]; then
            echo '${STABLE_NAME} — каталог, а должен быть симлинком' >&2
            exit 1
        fi
        ln -sfn '${SETUP_NAME}' '.${STABLE_NAME}.new'
        mv -T '.${STABLE_NAME}.new' '${STABLE_NAME}' 2>/dev/null ||
            mv -f '.${STABLE_NAME}.new' '${STABLE_NAME}'"

log "3/4 манифест latest.json (последним)"
# Предыдущий манифест сохраняем: откат релиза — это `mv latest.json.prev
# latest.json` на сервере, без пересборки и без доступа к ключу подписи (12 §5).
remote "[ -f '${DOWNLOAD_DIR}/latest.json' ] && cp -p '${DOWNLOAD_DIR}/latest.json' '${DOWNLOAD_DIR}/latest.json.prev' || true"
if command -v rsync >/dev/null 2>&1; then
    rsync -av "$MANIFEST" "${REMOTE}:${DOWNLOAD_DIR}/latest.json.new"
else
    scp -p "$MANIFEST" "${REMOTE}:${DOWNLOAD_DIR}/latest.json.new"
fi
# Права выставляем ДО подстановки: после mv файл уже боевой, и окна, в котором
# манифест лежит на месте, но не читается nginx'ом, быть не должно.
# Сам mv в пределах каталога — атомарный rename: клиент читает либо старый
# манифест целиком, либо новый целиком, но никогда не половину.
remote "chmod 644 '${DOWNLOAD_DIR}/latest.json.new' &&
        mv -f '${DOWNLOAD_DIR}/latest.json.new' '${DOWNLOAD_DIR}/latest.json'"

# --- 5. проверка снаружи ----------------------------------------------------
if [ "$SKIP_VERIFY" = "1" ]; then
    log "4/4 проверка пропущена (--skip-verify)"
    log "готово: LeadChat ${VERSION} выложен"
    exit 0
fi

command -v curl >/dev/null 2>&1 || { warn "нет curl — проверку пропускаем"; exit 0; }

log "4/4 проверка через ${PUBLIC_BASE_URL}"
rc=0
check_head() { # check_head <url> <что проверяем>
    local code
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -I -L "$1" || echo 000)"
    if [ "$code" = "200" ]; then
        printf '  ok   %s (%s)\n' "$2" "$code"
    else
        printf '  FAIL %s (HTTP %s) %s\n' "$2" "$code" "$1" >&2
        rc=1
    fi
}

SERVED="$(curl -sS --max-time 30 "${PUBLIC_BASE_URL}/download/latest.json" || true)"
SERVED_VER="$(printf '%s' "$SERVED" | python3 -c 'import json,sys
try: print(json.load(sys.stdin).get("version",""))
except Exception: print("")' 2>/dev/null || true)"
if [ "$SERVED_VER" = "$VERSION" ]; then
    printf '  ok   latest.json отдаёт %s\n' "$VERSION"
else
    printf '  FAIL latest.json отдаёт "%s", ожидали "%s"\n' "$SERVED_VER" "$VERSION" >&2
    rc=1
fi

check_head "${PUBLIC_BASE_URL}/download/${SETUP_NAME}" "инсталлятор скачивается"
check_head "${PUBLIC_BASE_URL}/download/${SETUP_NAME}.sig" "подпись доступна"
check_head "${PUBLIC_BASE_URL}/download/${STABLE_NAME}" "стабильная ссылка (кнопка на /login)"

if [ "$rc" != "0" ]; then
    fail "выкладка прошла, но проверка снаружи красная — смотрите nginx и права на ${DOWNLOAD_DIR}"
    fail "откат манифеста: ssh ${REMOTE} \"mv ${DOWNLOAD_DIR}/latest.json.prev ${DOWNLOAD_DIR}/latest.json\""
    exit 1
fi

log "готово: LeadChat ${VERSION} выложен и отдаётся"
