#!/usr/bin/env bash
# ============================================================================
# LeadChat — регрессионный smoke после деплоя (docs/07-TESTING-SECURITY.md §6).
#
#   deploy/smoke.sh [base_url]
#   make smoke                       # то же с дефолтами
#
# Быстрая версия «одним curl'ом», без Python-окружения проекта. Канонический
# набор — pytest tests/smoke -m smoke (07 §6): он строже (схемы, ping/pong,
# одноразовость тикета, настоящая ARQ-джоба при SMOKE_REDIS_URL) и его гоняет
# деплой-workflow.
#
# 16 проверок SM-1…SM-16 за ≤ 5 минут. Красный smoke = откат образа.
#
# ЖЁСТКОЕ ПРАВИЛО: прод не шлёт ничего в реальный Авито. Здесь только
# внутренние тракты и служебные сущности; ни одной операции, которая бы
# ушла наружу к клиенту.
#
# Переменные окружения:
#   SMOKE_BASE_URL          https://188-225-34-82.sslip.io (см. умолчание ниже)
#   EXPECTED_VERSION        тег, который обязан отдавать /api/health (SM-1)
#   SMOKE_EMAIL / SMOKE_PASSWORD          учётка smoke@leadchat.local (SM-2…)
#   SMOKE_ADMIN_EMAIL / SMOKE_ADMIN_PASSWORD  админ для SM-10
#   SMOKE_REAUTH_BEFORE     каналы в needs_reauth ДО выкатки (id через запятую, SM-10)
#   SMOKE_CONVERSATION_ID   служебный диалог SMOKE-CONV для SM-5
#
# Коды возврата: 0 — всё зелёное; 1 — есть провал.
# Проверка, для которой ещё нет ручки на бэкенде или не заданы креды,
# помечается SKIP и не роняет прогон. Флаг --strict превращает SKIP в FAIL
# (используем как гейт перед релизом, 07 §7).
# ============================================================================
set -uo pipefail

STRICT=0
ARGS=()
for a in "$@"; do
    case "$a" in
        --strict) STRICT=1 ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) ARGS+=("$a") ;;
    esac
done

# Адрес по умолчанию — тот, что РАБОТАЕТ. Домен chat.partner-lead-centre.ru
# сюда не ведёт: его A-запись указывает на <сторонний сервер>, где живёт
# FreeScout — другой хелпдеск с собственным сертификатом на это же имя.
# Пока домен не направят на прод, умолчание обязано быть рабочим, иначе
# скрипт молча пойдёт в чужую систему.
BASE="${ARGS[0]:-${SMOKE_BASE_URL:-https://188-225-34-82.sslip.io}}"
BASE="${BASE%/}"
CURL=(curl -sS --max-time 15)

PASS=0; FAILED=0; SKIPPED=0
ok()   { printf '  \033[32mPASS\033[0m  %-6s %s\n' "$1" "$2"; PASS=$((PASS + 1)); }
bad()  { printf '  \033[31mFAIL\033[0m  %-6s %s\n' "$1" "$2"; FAILED=$((FAILED + 1)); }
skip() {
    if [ "$STRICT" = "1" ]; then
        printf '  \033[31mFAIL\033[0m  %-6s %s (--strict: SKIP считается провалом)\n' "$1" "$2"
        FAILED=$((FAILED + 1))
    else
        printf '  \033[33mSKIP\033[0m  %-6s %s\n' "$1" "$2"; SKIPPED=$((SKIPPED + 1))
    fi
}

jget() { # jget '<json>' 'path.to.key' — печатает значение или пусто
    python3 -c '
import json,sys
try: data = json.loads(sys.argv[1])
except Exception: sys.exit(0)
for part in sys.argv[2].split("."):
    if isinstance(data, dict) and part in data: data = data[part]
    else: sys.exit(0)
print(data if not isinstance(data,(dict,list)) else json.dumps(data))' "$1" "$2" 2>/dev/null
}

echo "LeadChat smoke — ${BASE}"
echo "============================================================"

# --------------------------------------------------------------------------
# SM-1: /api/health — 200, status=ok, db/redis живы, версия = задеплоенной
# --------------------------------------------------------------------------
HEALTH="$("${CURL[@]}" "${BASE}/api/health" 2>/dev/null)"
H_STATUS="$(jget "$HEALTH" status)"
if [ "$H_STATUS" = "ok" ]; then
    if [ "$(jget "$HEALTH" db)" != "True" ] && [ "$(jget "$HEALTH" db)" != "true" ]; then
        bad SM-1 "status=ok, но db=false: ${HEALTH}"
    elif [ -n "${EXPECTED_VERSION:-}" ] && [ "$(jget "$HEALTH" version)" != "$EXPECTED_VERSION" ]; then
        bad SM-1 "версия '$(jget "$HEALTH" version)' != ожидаемой '${EXPECTED_VERSION}' — снаружи отвечает старый контейнер"
    else
        ok SM-1 "health: ${HEALTH}"
    fi
else
    bad SM-1 "health не ok: ${HEALTH:-<нет ответа>}"
fi

# --------------------------------------------------------------------------
# SM-2: логин smoke-пользователя — access-JWT + refresh-cookie с флагами
# --------------------------------------------------------------------------
ACCESS=""
if [ -z "${SMOKE_EMAIL:-}" ] || [ -z "${SMOKE_PASSWORD:-}" ]; then
    skip SM-2 "не заданы SMOKE_EMAIL/SMOKE_PASSWORD"
else
    COOKIE_HDRS="$(mktemp)"
    LOGIN="$("${CURL[@]}" -D "$COOKIE_HDRS" -X POST "${BASE}/api/v1/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"${SMOKE_EMAIL}\",\"password\":\"${SMOKE_PASSWORD}\"}" 2>/dev/null)"
    ACCESS="$(jget "$LOGIN" access_token)"
    SETC="$(grep -i '^set-cookie:' "$COOKIE_HDRS" || true)"
    rm -f "$COOKIE_HDRS"
    if [ -z "$ACCESS" ]; then
        bad SM-2 "логин не удался: ${LOGIN:0:200}"
    elif ! printf '%s' "$SETC" | grep -qi 'httponly' \
        || ! printf '%s' "$SETC" | grep -qi 'secure' \
        || ! printf '%s' "$SETC" | grep -qi 'samesite'; then
        bad SM-2 "refresh-cookie без нужных флагов (нужны HttpOnly+Secure+SameSite): ${SETC}"
    else
        ok SM-2 "логин ок, refresh-cookie с HttpOnly/Secure/SameSite"
    fi
fi
AUTH=(-H "Authorization: Bearer ${ACCESS}")

# --------------------------------------------------------------------------
# SM-3: список диалогов — 200 и валидная схема (items + page)
# --------------------------------------------------------------------------
if [ -z "$ACCESS" ]; then
    skip SM-3 "нет токена (SM-2)"
else
    CONV="$("${CURL[@]}" "${AUTH[@]}" "${BASE}/api/v1/conversations?limit=1" 2>/dev/null)"
    if [ -n "$(jget "$CONV" page.limit)" ] && printf '%s' "$CONV" | grep -q '"items"'; then
        ok SM-3 "GET /conversations: схема валидна (items + page)"
    else
        bad SM-3 "неожиданный ответ: ${CONV:0:200}"
    fi
fi

# --------------------------------------------------------------------------
# SM-4: WebSocket — тикет + upgrade через nginx (101 Switching Protocols)
# SM-5: тракт «запись -> realtime» (нужен служебный диалог SMOKE-CONV)
# --------------------------------------------------------------------------
ws_probe() { # ws_probe <base> <token> [conversation_id]
    python3 - "$@" <<'PY'
import base64, json, os, socket, ssl, sys, time, urllib.parse, urllib.request, uuid

base, token = sys.argv[1], sys.argv[2]
conv = sys.argv[3] if len(sys.argv) > 3 else ""

def http(path, method="GET", body=None):
    req = urllib.request.Request(base + path, method=method)
    req.add_header("Authorization", "Bearer " + token)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, data, timeout=10) as r:
        raw = r.read().decode() or "{}"
        return r.status, json.loads(raw) if raw.strip().startswith(("{", "[")) else {}

# 1. одноразовый тикет (01 §11.1)
try:
    _, t = http("/api/v1/ws/ticket", "POST")
except Exception as exc:                       # noqa: BLE001
    print("TICKET_FAIL", exc); sys.exit(3)
ticket = t.get("ticket")
if not ticket:
    print("TICKET_EMPTY", t); sys.exit(3)

# 2. ручной WS-handshake — проверяем именно связку nginx(upgrade) + api
u = urllib.parse.urlsplit(base)
host, port = u.hostname, u.port or (443 if u.scheme == "https" else 80)
key = base64.b64encode(os.urandom(16)).decode()
raw = socket.create_connection((host, port), timeout=10)
sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if u.scheme == "https" else raw
sock.sendall((
    f"GET /api/v1/ws?ticket={ticket} HTTP/1.1\r\n"
    f"Host: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
    f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
).encode())
sock.settimeout(10)
head = b""
while b"\r\n\r\n" not in head:
    chunk = sock.recv(4096)
    if not chunk:
        break
    head += chunk
if b"101" not in head.split(b"\r\n", 1)[0]:
    print("UPGRADE_FAIL", head.split(b"\r\n", 1)[0].decode(errors="replace")); sys.exit(4)
print("UPGRADE_OK")

if not conv:
    sys.exit(0)

# 3. SM-5: пишем заметку и ждём событие в сокете (< 2 c по 07 §6)
# Тело — ровно по контракту 01 §6.4: `text` + `client_message_id`. С прежним
# {"body": ...} ручка отвечала 422, скрипт считал это «ручки ещё нет» и красил
# SM-5 в SKIP — то есть самый важный тракт (API -> БД -> Pub/Sub -> WS) не
# проверялся вообще, а прогон выглядел зелёным.
try:
    status, _ = http(
        f"/api/v1/conversations/{conv}/notes",
        "POST",
        {"text": "SMOKE ping", "client_message_id": str(uuid.uuid4())},
    )
except urllib.error.HTTPError as exc:
    print("NOTE_HTTP", exc.code); sys.exit(5)
except Exception as exc:                       # noqa: BLE001
    print("NOTE_FAIL", exc); sys.exit(5)

def read_frame(deadline):
    sock.settimeout(max(0.2, deadline - time.time()))
    hdr = sock.recv(2)
    if len(hdr) < 2:
        return None
    ln = hdr[1] & 0x7F
    if ln == 126:
        ln = int.from_bytes(sock.recv(2), "big")
    elif ln == 127:
        ln = int.from_bytes(sock.recv(8), "big")
    buf = b""
    while len(buf) < ln:
        part = sock.recv(ln - len(buf))
        if not part:
            break
        buf += part
    return buf

deadline = time.time() + 5
while time.time() < deadline:
    try:
        payload = read_frame(deadline)
    except Exception:                          # noqa: BLE001
        break
    if payload and b"message" in payload:
        print("REALTIME_OK", payload[:120].decode(errors="replace")); sys.exit(0)
print("REALTIME_TIMEOUT"); sys.exit(6)
PY
}

if [ -z "$ACCESS" ]; then
    skip SM-4 "нет токена (SM-2)"
    skip SM-5 "нет токена (SM-2)"
else
    WSOUT="$(ws_probe "$BASE" "$ACCESS" 2>&1)"; WSRC=$?
    case "$WSRC" in
        0|6) ok SM-4 "WS upgrade через nginx: 101" ;;
        3)   skip SM-4 "нет ручки /api/v1/ws/ticket: ${WSOUT}" ;;
        *)   bad SM-4 "WS не поднялся: ${WSOUT}" ;;
    esac

    if [ -z "${SMOKE_CONVERSATION_ID:-}" ]; then
        skip SM-5 "не задан SMOKE_CONVERSATION_ID (служебный диалог SMOKE-CONV)"
    else
        R5="$(ws_probe "$BASE" "$ACCESS" "$SMOKE_CONVERSATION_ID" 2>&1)"; RC5=$?
        case "$RC5" in
            0) ok SM-5 "запись -> Pub/Sub -> WS доехало: ${R5}" ;;
            5) skip SM-5 "ручка заметок ещё не задеплоена: ${R5}" ;;
            *) bad SM-5 "событие не пришло в WS: ${R5}" ;;
        esac
    fi
fi

# --------------------------------------------------------------------------
# SM-6: очередь и воркер; SM-7: планировщик — оба видны снаружи только через
# /api/health/deep (05 §7.2). Ручки ещё нет -> SKIP (см. cross-boundary).
# --------------------------------------------------------------------------
DEEP_CODE="$("${CURL[@]}" -o /tmp/lc_deep.$$ -w '%{http_code}' "${BASE}/api/health/deep" 2>/dev/null)"
DEEP="$(cat /tmp/lc_deep.$$ 2>/dev/null)"; rm -f /tmp/lc_deep.$$
if [ "$DEEP_CODE" != "200" ]; then
    skip SM-6 "/api/health/deep недоступен (код ${DEEP_CODE})"
    skip SM-7 "/api/health/deep недоступен (код ${DEEP_CODE})"
else
    # Живость воркера меряем НЕзакрытыми записями, а не длиной стрима:
    # queue.len — это XLEN, он растёт вместе с трафиком до подрезки и сам по
    # себе не значит «воркер умер» (размер хвоста — отдельный алерт 05 §7.2).
    # Красный smoke означает откат образа, поэтому судим по PEL.
    QLEN="$(jget "$DEEP" queue.len)"; QOLD="$(jget "$DEEP" queue.oldest_pending_sec)"
    QPEND="$(jget "$DEEP" queue.pending)"
    if [ -z "$QLEN" ]; then
        skip SM-6 "в /api/health/deep нет поля queue.len"
    elif [ "${QOLD:-0}" -lt 300 ] && [ "${QPEND:-0}" -lt 500 ]; then
        ok SM-6 "воркер разбирает очередь: pending=${QPEND:-0}, oldest_pending=${QOLD:-0}s (len=${QLEN})"
    else
        bad SM-6 "воркер не разбирает очередь: pending=${QPEND:-?}, oldest_pending=${QOLD:-?}s (runbook 05 §8.3)"
    fi

    # Имя поля — из контракта /api/health/deep (05 §7.2): heartbeat_age_sec.
    # С прежним alive_age_sec проверка всегда уходила в SKIP, и умерший
    # планировщик (а с ним и refresh токенов Авито) не ловился вообще.
    SCH="$(jget "$DEEP" scheduler.heartbeat_age_sec)"
    if [ -z "$SCH" ]; then
        skip SM-7 "в /api/health/deep нет поля scheduler.heartbeat_age_sec"
    elif [ "$SCH" -lt 120 ]; then
        ok SM-7 "scheduler жив: heartbeat ${SCH}s назад"
    else
        bad SM-7 "scheduler молчит ${SCH}s (> 2 мин) — умрёт refresh токенов (runbook 05 §8.2)"
    fi
fi

# --------------------------------------------------------------------------
# SM-8: webhook-endpoint без секрета -> 403 (07 §4.4 W1). Наружу ничего не шлёт.
# --------------------------------------------------------------------------
WH_CODE="$("${CURL[@]}" -o /dev/null -w '%{http_code}' -X POST \
    "${BASE}/api/hooks/avito/00000000-0000-0000-0000-000000000000" \
    -H 'Content-Type: application/json' -d '{"smoke":true}' 2>/dev/null)"
if [ "$WH_CODE" = "403" ]; then
    ok SM-8 "webhook без секрета -> 403"
else
    bad SM-8 "webhook без секрета вернул ${WH_CODE}, ожидался 403"
fi

# --------------------------------------------------------------------------
# SM-9: статика фронтенда — index.html отдаётся и ссылается на живой бандл
# --------------------------------------------------------------------------
INDEX="$("${CURL[@]}" "${BASE}/" 2>/dev/null)"
ASSET="$(printf '%s' "$INDEX" | grep -o '/assets/[A-Za-z0-9_.-]*\.js' | head -1)"
if [ -z "$ASSET" ]; then
    bad SM-9 "в index.html нет ссылки на хэшированный бандл /assets/*.js"
else
    # ⚠ КОММЕНТАРИИ РАЗМЕТКИ В БОЕВУЮ СТРАНИЦУ НЕ УЕЗЖАЮТ (просьба владельца
    # 08.09: «убери эти мусорные надписи»). `index.html` объясняет сам себя
    # подробно — почему тема ставится до отрисовки, откуда ширина рельсы, —
    # и всё это Vite копировал как есть: открывший «просмотр кода» получал
    # полтора десятка абзацев внутренней переписки с путями к файлам и
    # именами сторожей. Убирает их плагин сборки (`src/build/безКомментариев`),
    # а плагин легко потерять при правке конфига — и заметить это можно было
    # бы только глазами, открыв исходник страницы.
    A_CODE="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "${BASE}${ASSET}" 2>/dev/null)"
    if [ "$A_CODE" != "200" ]; then
        bad SM-9 "бандл ${ASSET} отдаёт ${A_CODE} — index.html и статика из разных сборок"
    elif printf '%s' "$INDEX" | grep -q '<!--'; then
        bad SM-9 "в боевой index.html остались комментарии разметки — плагин очистки потерялся"
    elif printf '%s' "$INDEX" | grep -q '/\*'; then
        bad SM-9 "в боевой index.html остались комментарии встроенных стилей — плагин очистки задел не всё"
    else
        ok SM-9 "статика: index.html -> ${ASSET} (200), комментариев ни в разметке, ни в стилях нет"
    fi
fi

# --------------------------------------------------------------------------
# SM-10: ни один боевой аккаунт Авито не в needs_reauth (регресс деплоя
#        не убил refresh токенов). Только чтение, наружу ничего не уходит.
#
# ⚠ ОТВЕТ СНАЧАЛА ПРОВЕРЯЕТСЯ НА ГОДНОСТЬ, И ЭТО НЕ ПРИДИРКА. Прежняя версия
# судила по одному признаку: «есть ли в теле слово needs_reauth». Тело `403
# forbidden` этого слова тоже не содержит — то есть учётка без права
# `accounts:read` красила проверку ЗЕЛЁНЫМ, не прочитав ни одного канала. Ровно
# так это и выглядело бы 23 августа: роль у служебной учётки оказалась
# `manager`, и «needs_reauth нет» означало бы «нам не дали посмотреть».
# --------------------------------------------------------------------------
if [ -z "${SMOKE_ADMIN_EMAIL:-}" ] || [ -z "${SMOKE_ADMIN_PASSWORD:-}" ]; then
    skip SM-10 "не заданы SMOKE_ADMIN_EMAIL/SMOKE_ADMIN_PASSWORD"
else
    ALOGIN="$("${CURL[@]}" -X POST "${BASE}/api/v1/auth/login" -H 'Content-Type: application/json' \
        -d "{\"email\":\"${SMOKE_ADMIN_EMAIL}\",\"password\":\"${SMOKE_ADMIN_PASSWORD}\"}" 2>/dev/null)"
    ATOKEN="$(jget "$ALOGIN" access_token)"
    if [ -z "$ATOKEN" ]; then
        bad SM-10 "учётка SM-10 не залогинилась: ${ALOGIN:0:200}"
    else
        ACC_CODE="$("${CURL[@]}" -o /tmp/lc_acc.$$ -w '%{http_code}' \
            -H "Authorization: Bearer ${ATOKEN}" "${BASE}/api/v1/avito-accounts" 2>/dev/null)"
        ACC="$(cat /tmp/lc_acc.$$ 2>/dev/null)"; rm -f /tmp/lc_acc.$$
        # Новые и прежние каналы в needs_reauth — относительно снимка ДО
        # выкатки (`SMOKE_REAUTH_BEFORE` пишет ship.sh; разбор — в
        # deploy/reauth_diff.py). Без снимка прежних нет: запуск руками судит
        # как раньше, по любому каналу.
        REAUTH="$(printf '%s' "$ACC" | python3 deploy/reauth_diff.py "${SMOKE_REAUTH_BEFORE:-}" 2>/dev/null)"
        REAUTH_NEW="${REAUTH%% *}"
        REAUTH_OLD="${REAUTH##* }"
        if [ "$ACC_CODE" = "403" ]; then
            bad SM-10 "у учётки ${SMOKE_ADMIN_EMAIL} нет права accounts:read — нужна роль head (seed-smoke --admin-password)"
        elif [ "$ACC_CODE" != "200" ]; then
            bad SM-10 "список каналов ответил ${ACC_CODE}: ${ACC:0:200}"
        elif ! printf '%s' "$ACC" | grep -q '"items"' || [ -z "$REAUTH" ]; then
            bad SM-10 "неожиданный ответ списка каналов: ${ACC:0:200}"
        elif [ "$REAUTH_NEW" = "0" ] && [ "$REAUTH_OLD" = "0" ]; then
            ok SM-10 "аккаунты Авито: needs_reauth нет"
        elif [ "$REAUTH_NEW" = "0" ]; then
            ok SM-10 "после выкатки новых needs_reauth нет; до неё ждали переподключения: ${REAUTH_OLD} — runbook 05 §8.2"
        else
            bad SM-10 "после выкатки выпали в needs_reauth: ${REAUTH_NEW} (до неё было ${REAUTH_OLD}) — runbook 05 §8.2"
        fi
    fi
fi

# --------------------------------------------------------------------------
# SM-11: версия В БАНДЛЕ совпадает с версией api. Иначе предупреждение
#        «вкладка работает на старой сборке» не сработает никогда.
#
# ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА, ЕСЛИ ЕСТЬ SM-1. SM-1 сверяет тег КОНТЕЙНЕРА, а
# заглушка живёт в БАНДЛЕ: выкатка полгода собирала образ веба без
# --build-arg VITE_APP_VERSION, в код уезжало умолчание «dev», и isStaleTab()
# на заглушке молчит по построению. Снаружи всё выглядело исправным, а вкладка,
# открытая до выкатки, ломалась на первом переходе в ленивый раздел —
# /assets/ отдаётся с immutable и try_files =404.
#
# Проверяем ровно то, чего не видно ниоткуда больше: что в бандле НЕ заглушка
# и что она совпадает с тем, что отдаёт /api/health.
# --------------------------------------------------------------------------
if [ -z "${ASSET:-}" ]; then
    skip SM-11 "бандл не найден (SM-9)"
else
    BUNDLE="$("${CURL[@]}" "${BASE}${ASSET}" 2>/dev/null)"
    H_VER="$(jget "$HEALTH" version)"
    if [ -z "$H_VER" ] || [ "$H_VER" = "prod" ] || [ "$H_VER" = "dev" ]; then
        skip SM-11 "api отдаёт версию '${H_VER:-<пусто>}' — сверять не с чем"
    elif printf '%s' "$BUNDLE" | grep -qF "\"${H_VER}\""; then
        ok SM-11 "версия бандла = версии api (${H_VER})"
    elif printf '%s' "$BUNDLE" | grep -qF '"dev"'; then
        bad SM-11 "в бандле версия-заглушка «dev» — веб собран без --build-arg VITE_APP_VERSION, предупреждение о старой вкладке выключено"
    else
        bad SM-11 "версии api '${H_VER}' в бандле нет — фронт и бэкенд из разных сборок"
    fi
fi

# --------------------------------------------------------------------------
# SM-12: Content-Security-Policy доехала до боя и не ослаблена
#
# ЗАЧЕМ ОТДЕЛЬНАЯ ПРОВЕРКА. Тесты фронта стерегут ТЕКСТ политики в репозитории,
# но правило этого проекта — «коммит не доказывает, что код в бою». Заголовок
# живёт в сниппете, который подключается в каждый location отдельно: достаточно
# одного location со своим `add_header`, где сниппет забыли, — и на части
# страниц защиты не будет, а снаружи это ничем не видно.
#
# Проверяем три вещи, каждая из которых означает «защиты нет»:
# заголовок пришёл; в script-src нет 'unsafe-inline'; хеш встроенного скрипта
# темы на месте (без него браузер откажется его выполнять у КАЖДОГО человека).
# --------------------------------------------------------------------------
CSP="$("${CURL[@]}" -s -D - -o /dev/null "${BASE}/" 2>/dev/null \
    | tr -d '\r' | grep -i '^content-security-policy:' | head -1)"
if [ -z "$CSP" ]; then
    bad SM-12 "Content-Security-Policy наружу не отдаётся — вторая линия обороны от чужого скрипта отсутствует"
else
    CSP_SCRIPT="$(printf '%s' "$CSP" | grep -o "script-src[^;]*")"
    if printf '%s' "$CSP_SCRIPT" | grep -q "unsafe-inline"; then
        bad SM-12 "в script-src на проде стоит 'unsafe-inline' — политика ослаблена, чужой скрипт выполнится"
    elif ! printf '%s' "$CSP_SCRIPT" | grep -q "sha256-"; then
        bad SM-12 "в script-src нет хеша встроенного скрипта — тема не применится, у всех вспышка светлого экрана"
    elif ! printf '%s' "$CSP" | grep -o "img-src[^;]*" | grep -q "avito"; then
        bad SM-12 "img-src не пускает домены Авито — у смены пропадут фотографии и аватары (сбой 01.09)"
    elif ! printf '%s' "$CSP" | grep -o "media-src[^;]*" | grep -q "avito"; then
        bad SM-12 "media-src не пускает домены Авито — не будут играть голосовые (сбой 01.09)"
    else
        ok SM-12 "CSP в бою: script-src закрыт, хеш темы и картинки Авито на месте"
    fi
fi

# --------------------------------------------------------------------------
# SM-13: /sw.js отдаётся браузеру как javascript
#
# ⚠ ЭТА ПРОВЕРКА РОДИЛАСЬ ИЗ БОЕВОГО ЗАМЕРА 07.09, А НЕ ИЗ ОСТОРОЖНОСТИ.
# До правки конфига /sw.js попадал под SPA-fallback nginx и отдавался как
# index.html: код 200, тип text/html, 14 103 байта. Снаружи всё выглядело
# исправным — «файл есть, отвечает двухсоткой», — а браузер такой ответ
# регистрировать отказывается: воркер не встаёт, кнопки «Принять»/«Отклонить»
# в уведомлении не появляются, и узнать об этом можно только из консоли
# вкладки конкретного человека.
#
# Ровно поэтому судим по ТИПУ, а не по коду ответа: код был правильный и тогда.
# Третьим шагом смотрим внутрь файла — что это наш воркер, а не что угодно
# другое с верным типом (стоит, например, промахнуться каталогом в `root`).
# --------------------------------------------------------------------------
SW_BODY="$(mktemp)"
SW_META="$("${CURL[@]}" -o "$SW_BODY" -w '%{http_code} %{content_type}' "${BASE}/sw.js" 2>/dev/null)"
SW_CODE="${SW_META%% *}"; SW_TYPE="${SW_META#* }"
if [ "$SW_CODE" != "200" ]; then
    bad SM-13 "/sw.js отдаёт ${SW_CODE} — сервис-воркер не встанет, уведомления останутся без кнопок"
elif ! printf '%s' "$SW_TYPE" | grep -qi 'javascript'; then
    bad SM-13 "/sw.js отдаётся с типом '${SW_TYPE}' вместо javascript — браузер откажет регистрации (нужен location = /sw.js в шаблоне nginx)"
elif ! grep -q 'notificationclick' "$SW_BODY"; then
    bad SM-13 "по /sw.js лежит не наш воркер: нет обработчика notificationclick"
else
    ok SM-13 "/sw.js: 200, ${SW_TYPE}, воркер уведомлений на месте"
fi
rm -f "$SW_BODY"

# --------------------------------------------------------------------------
# SM-15: остальные заголовки защиты доехали до боя
#
# ⚠ ЭТА ПРОВЕРКА РОДИЛАСЬ ИЗ ЧУЖОГО ОТЧЁТА, КОТОРЫЙ ОКАЗАЛСЯ НЕВЕРЕН (08.09).
# Владельцу показали разбор «защита средняя, 6 из 10»: нет X-Frame-Options,
# можно встроить в чужую страницу, CORS открыт, HSTS и CSP «неизвестно».
# Замер на бою в тот же час: `x-frame-options: DENY` отдаётся, `frame-ancestors
# 'none'` в политике стоит, чужому Origin `access-control-allow-origin` не
# приходит вовсе. Разбор просто НЕ УМЕЛ читать заголовки ответа — он видел
# только то, что доступно скрипту на странице, и всё невидимое записал в «нет».
#
# Спорить с такими отчётами на словах бесполезно, поэтому здесь стоит замер:
# он говорит про БОЙ и печатает, что именно пришло.
# --------------------------------------------------------------------------
SEC_HDRS="$("${CURL[@]}" -s -D - -o /dev/null "${BASE}/" 2>/dev/null | tr -d '\r' | tr 'A-Z' 'a-z')"
SEC_MISS=""
for H in "x-frame-options: deny" "strict-transport-security:" "x-content-type-options: nosniff" \
         "referrer-policy:" "cross-origin-opener-policy: same-origin" "permissions-policy:"; do
    printf '%s' "$SEC_HDRS" | grep -q "^${H}" || SEC_MISS="${SEC_MISS} [${H%%:*}]"
done
# Чужой Origin не должен получать разрешения: именно это и приняли за «CORS открыт».
CORS_ALIEN="$("${CURL[@]}" -s -D - -o /dev/null -H 'Origin: https://chuzhoy.example' "${BASE}/api/health" 2>/dev/null \
    | tr -d '\r' | grep -ci '^access-control-allow-origin:')"

if [ -n "$SEC_MISS" ]; then
    bad SM-15 "заголовки защиты не пришли:${SEC_MISS} — снаружи это выглядит как «защиты нет», и в чужом отчёте так и напишут"
elif [ "$CORS_ALIEN" != "0" ]; then
    bad SM-15 "чужому Origin отдан access-control-allow-origin — чужая страница сможет читать ответы API от имени диспетчера"
elif ! printf '%s' "$SEC_HDRS" | grep -q "frame-ancestors 'none'"; then
    bad SM-15 "в CSP пропал frame-ancestors 'none' — приложение можно встроить в чужую страницу поверх невидимой кнопки"
elif ! printf '%s' "$SEC_HDRS" | grep -q "permissions-policy:.*usb=()"; then
    bad SM-15 "Permissions-Policy сузилась до старого короткого списка — расширение 08.09 потерялось"
elif ! printf '%s' "$SEC_HDRS" | grep -q "frame-src 'none'"; then
    bad SM-15 "в CSP пропал frame-src 'none' — в нашу страницу снова можно встроить чужую"
elif ! printf '%s' "$SEC_HDRS" | grep -q "script-src-attr 'none'"; then
    bad SM-15 "в CSP пропал script-src-attr 'none' — обработчик строкой в разметке снова выполнится"
else
    ok SM-15 "X-Frame-Options DENY, HSTS, frame-ancestors и frame-src none, script-src-attr none, широкая Permissions-Policy; чужому Origin CORS не отдаётся"
fi

# --------------------------------------------------------------------------
# SM-14: пробы сканеров получают 404, а разделы приложения — оболочку
#
# ⚠ ПРОВЕРЯЮТСЯ ОБЕ СТОРОНЫ ОДНОГО ПРАВИЛА, И ВТОРАЯ ВАЖНЕЕ ПЕРВОЙ.
# До 08.09 SPA-заглушка отвечала 200 на всё подряд: замер на бою дал
# `/.env`, `/.git/config`, `/wp-login.php` — три раза «200 text/html 14103b».
# Секретов там нет, лежит оболочка приложения, но сканер читает 200 как
# «файл есть» и продолжает перебор.
#
# Заслон при этом опасен ровно тем, чем полезен: правило по расширению в
# nginx СИЛЬНЕЕ префикса `location /api/`, и написанное на один символ шире
# оно съедает выдачу вложений. Клиент присылает файлы со своими именами, и
# «отчёт.log» ушёл бы в 404 вместо файла — тихо, до первой жалобы.
# Поэтому проверка требует ТРЁХ вещей сразу: проба закрыта, раздел
# приложения открыт, путь вложения до заслона не доходит (у него свой ответ
# от проверки подписи — 403 или 410, но НЕ 404 от нашего правила).
#
# ⚠ ИМЕНА ПЕРЕМЕННЫХ ЛАТИНИЦЕЙ. Комментарии в этом проекте по-русски, но
# `bash` не берёт кириллицу в ИМЯ переменной: `ПРОБА=да` он читает как команду
# «ПРОБА=да» и падает на «command not found», а `bash -n` этого не видит —
# синтаксически строка верна. Проверено здесь же 08.09.
# --------------------------------------------------------------------------
PROBE_BAD=""
for PROBE_PATH in "/.env" "/.git/config" "/wp-login.php"; do
    PROBE_CODE="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "${BASE}${PROBE_PATH}" 2>/dev/null)"
    [ "$PROBE_CODE" = "404" ] || PROBE_BAD="${PROBE_BAD} ${PROBE_PATH}=${PROBE_CODE}"
done
SPA_CODE="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "${BASE}/dialogs" 2>/dev/null)"
MEDIA_CODE="$("${CURL[@]}" -o /dev/null -w '%{http_code}' "${BASE}/api/v1/media/otchet.log" 2>/dev/null)"
ROBOTS="$("${CURL[@]}" -o /dev/null -w '%{http_code} %{content_type}' "${BASE}/robots.txt" 2>/dev/null)"

if [ -n "$PROBE_BAD" ]; then
    bad SM-14 "пробы сканеров отвечают не 404:${PROBE_BAD} — SPA-заглушка снова отдаёт оболочку на любой путь"
elif [ "$SPA_CODE" != "200" ]; then
    bad SM-14 "/dialogs отдаёт ${SPA_CODE} вместо оболочки — заслон от сканеров съел разделы приложения, ссылки из мессенджера не открываются"
elif [ "$MEDIA_CODE" = "404" ]; then
    bad SM-14 "путь вложения /api/v1/media/otchet.log отдаёт 404 — правило по расширению перебило location /api/ и глотает файлы клиентов (ждём 403/410 от проверки подписи)"
elif ! printf '%s' "$ROBOTS" | grep -q '^200 text/plain'; then
    bad SM-14 "/robots.txt отдаёт '${ROBOTS}' вместо '200 text/plain' — роботу снова показывают HTML и он читает это как «индексировать можно»"
else
    ok SM-14 "пробы закрыты, разделы открыты, вложения проходят (${MEDIA_CODE}), robots.txt на месте"
fi

# --------------------------------------------------------------------------
# SM-16: security.txt отдаётся и разбирается (RFC 9116)
#
# ⚠ СУДИМ ПО ТИПУ И СОДЕРЖИМОМУ, А НЕ ПО КОДУ ОТВЕТА. Проверка «пришло 200»
# зеленеет на НЕСУЩЕСТВУЮЩЕМ файле: замер на бою 09.09 — `/security.txt`
# отдавал `200 text/html 5143b`, то есть оболочку приложения. Файла нет,
# двухсотка есть, сторож доволен.
#
# ⚠ СРОК ГОДНОСТИ НЕ ВАЛИТ ВЫКАТКУ, И ЭТО РЕШЕНИЕ. Просроченный файл по RFC
# недействителен, но красный smoke означает ОТКАТ ОБРАЗОВ, а откат от
# просроченной даты не лечит: в прежнем образе лежит тот же файл с той же
# датой. Хуже того, встала бы выкатка срочной правки, к этому файлу отношения
# не имеющей. Поэтому дата — жёлтая: за 60 дней и после истечения печатается
# предупреждение, но выкатку оно не останавливает.
#
# ⚠ ОТДЕЛЬНОЙ ТРЕВОГИ В ЦЕНТРЕ УВЕДОМЛЕНИЙ НЕТ, И ЭТО ВЗВЕШЕНО. Она была бы
# честнее (не зависит от частоты выкаток), но стоит нового вида события,
# который тянет за собой каталог центра, объединение типов на фронте и свои
# проверки — четыре файла ради условия, которое здесь и так видно жёлтым при
# каждой выкатке, а выкаток за последние 30 дней было 595. Шестьдесят дней
# предупреждений — достаточный зов; заведём тревогу, если окажется, что нет.
#
# ⚠ АРИФМЕТИКА ДАТЫ — СВОЯ, БЕЗ `date -d` И `date -j`. Первого нет в busybox
# боевого образа, второго нет в GNU: скрипт гоняется и с рабочего Мака, и с
# сервера, и «работает у меня» здесь ничего не значит.
# Смещение часового пояса арифметика не учитывает: при порогах в 30 и 60
# суток разница в часах ничего не решает, а разбор смещения — это ещё одна
# ветка, которую пришлось бы стеречь.
sec_field() { # sec_field '<текст>' '<имя>' — значение поля ОТ НАЧАЛА СТРОКИ
    # Якорь на начало строки обязателен: без него сторож зеленеет на строке
    # комментария, где имя поля просто упомянуто. Флаг I — имена полей
    # регистронезависимы (§2.2), и он есть и в BSD sed, и в GNU.
    printf '%s' "$1" | sed -n "s/^[[:space:]]*$2[[:space:]]*:[[:space:]]*\\(.*\\)/\\1/Ip" | head -1 | tr -d '\r'
}
iso_epoch() { # iso_epoch 2027-09-01T00:00:00Z — секунды от эпохи, целой арифметикой
    printf '%s' "$1" | awk -F'[-T:Z+]' '
        NF < 3 { exit 1 }
        {
            y = $1 + 0; m = $2 + 0; d = $3 + 0; H = $4 + 0; M = $5 + 0; S = $6 + 0
            if (y < 1970 || m < 1 || m > 12 || d < 1 || d > 31) exit 1
            # алгоритм «дней от гражданской даты» (Howard Hinnant): без таблиц
            # високосных лет и без библиотечных вызовов
            yy = y - (m <= 2)
            era = int((yy >= 0 ? yy : yy - 399) / 400)
            yoe = yy - era * 400
            doy = int((153 * (m + (m > 2 ? -3 : 9)) + 2) / 5) + d - 1
            doe = yoe * 365 + int(yoe / 4) - int(yoe / 100) + doy
            print (era * 146097 + doe - 719468) * 86400 + H * 3600 + M * 60 + S
        }'
}

SEC_URL="${BASE}/.well-known/security.txt"
SEC_HDR=$(curl -sk --max-time 15 -o /tmp/lc_sec.txt -D - "$SEC_URL" 2>/dev/null)
SEC_CODE=$(printf '%s' "$SEC_HDR" | awk 'tolower($1) ~ /^http/ {c=$2} END {print c}')
SEC_TYPE=$(printf '%s' "$SEC_HDR" | tr -d '\r' | awk 'tolower($1) == "content-type:" {print tolower($2)}' | head -1)
SEC_BODY=$(cat /tmp/lc_sec.txt 2>/dev/null)
SEC_CONTACT=$(sec_field "$SEC_BODY" "Contact")
SEC_EXPIRES=$(sec_field "$SEC_BODY" "Expires")
SEC_EPOCH=$(iso_epoch "$SEC_EXPIRES" 2>/dev/null || true)

if [ "$SEC_CODE" != "200" ]; then
    bad SM-16 "${SEC_URL} отвечает ${SEC_CODE:-нет ответа} — адреса для связи по уязвимостям в бою нет"
elif [ "${SEC_TYPE#text/plain}" = "$SEC_TYPE" ]; then
    bad SM-16 "security.txt отдаётся как '${SEC_TYPE:-без типа}' вместо text/plain — RFC 9116 §3 требует text/plain; charset=utf-8"
elif [ -z "$SEC_CONTACT" ]; then
    bad SM-16 "в security.txt нет строки Contact — файл есть, а написать по нему некуда"
elif [ -z "$SEC_EXPIRES" ]; then
    bad SM-16 "в security.txt нет обязательного Expires (§2.5.5) — файл формально недействителен"
elif [ -z "$SEC_EPOCH" ]; then
    bad SM-16 "Expires='${SEC_EXPIRES}' не разбирается как дата — непонятое обязано быть провалом, а не тихим пропуском"
else
    SEC_LEFT=$(( (SEC_EPOCH - $(date -u +%s)) / 86400 ))
    if [ "$SEC_LEFT" -lt 0 ]; then
        ok SM-16 "security.txt отдаётся, Contact на месте"
        printf '  \033[33mWARN\033[0m  %-6s %s\n' "SM-16" "срок годности истёк ${SEC_LEFT#-} дн. назад — по RFC файл недействителен; продлите Expires (откат образов этого не лечит, поэтому не FAIL)"
    elif [ "$SEC_LEFT" -lt 60 ]; then
        ok SM-16 "security.txt отдаётся, Contact на месте"
        printf '  \033[33mWARN\033[0m  %-6s %s\n' "SM-16" "до конца срока ${SEC_LEFT} дн. — пора продлить Expires и проверить, что адрес из Contact жив"
    else
        ok SM-16 "security.txt: 200 text/plain, Contact на месте, до конца срока ${SEC_LEFT} дн."
    fi
fi

echo "============================================================"
printf 'итог: \033[32m%d pass\033[0m, \033[31m%d fail\033[0m, \033[33m%d skip\033[0m\n' \
    "$PASS" "$FAILED" "$SKIPPED"
[ "$FAILED" -eq 0 ] || exit 1
