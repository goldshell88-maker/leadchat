#!/usr/bin/env bash
# Установка/обновление шлюза на Амстердаме (docs/46-ШЛЮЗ-API.md §4).
#
# Запуск с мака из корня репозитория:  bash gateway/deploy.sh
# Что делает: rsync исходника в /opt/leadchat-gateway/src, venv на системном
# python3 (нужен пакет python3.14-venv — ставится apt один раз), pip по пинам,
# юнит systemd, перезапуск, проверка /health с самого Амстердама.
# Чего НЕ делает: не трогает /opt/leadchat-gateway/.env — ключи туда кладёт
# владелец (или труба сервер→сервер, см. спецификацию), файл 0600.
set -euo pipefail

HOST="${GATEWAY_HOST_SSH:-leadchat-watch}"
DIR="/opt/leadchat-gateway"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="${REPO_ROOT}/gateway"

say() { printf '\033[36m▸ %s\033[0m\n' "$*"; }
ok() { printf '\033[32m✓ %s\033[0m\n' "$*"; }
die() { printf '\033[31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

[ -f "${SRC}/leadchat_gateway/main.py" ] || die "нет ${SRC}/leadchat_gateway/main.py — запускать из корня LeadChat"

VERSION="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || echo dev)"
if [ -n "$(git -C "${REPO_ROOT}" status --porcelain -- gateway 2>/dev/null)" ]; then
    VERSION="${VERSION}-dirty"
fi
printf '%s\n' "${VERSION}" > "${SRC}/VERSION"
say "версия ${VERSION}"

say "проверка ssh ${HOST}"
ssh -o BatchMode=yes -o ConnectTimeout=20 "${HOST}" 'test -d /opt' >/dev/null || die "ssh ${HOST} недоступен"

say "rsync → ${HOST}:${DIR}/src/"
ssh "${HOST}" "mkdir -p ${DIR}/src"
rsync -az --delete \
    --exclude '__pycache__' --exclude '.pytest_cache' --exclude 'tests' --exclude '.ruff_cache' \
    --exclude '.env' --exclude '.env.*' --exclude '.venv' --exclude '.mypy_cache' \
    -e 'ssh -o BatchMode=yes -o ConnectTimeout=20' \
    "${SRC}/" "${HOST}:${DIR}/src/"
ok "исходник на месте"

say "venv, зависимости, юнит"
ssh "${HOST}" bash -s <<'REMOTE'
set -euo pipefail
DIR=/opt/leadchat-gateway
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
    echo "  ставлю python3-venv (ensurepip)…"
    export DEBIAN_FRONTEND=noninteractive
    apt-get install -y -q "python3.$(python3 -c 'import sys;print(sys.version_info[1])')-venv" >/dev/null
fi
if [ ! -x "${DIR}/.venv/bin/python" ]; then
    python3 -m venv "${DIR}/.venv"
fi
"${DIR}/.venv/bin/pip" install -q --disable-pip-version-check -r "${DIR}/src/requirements.txt"
install -m 644 "${DIR}/src/deploy/leadchat-gateway.service" /etc/systemd/system/leadchat-gateway.service
# Каталог с кодом и venv читают все: юнит работает под DynamicUser, а не под
# root, и 750 root:root не пустил бы его ни в WorkingDirectory, ни к uvicorn.
# Секрет только в .env — он 0600 и читается самим systemd до сброса прав.
chmod 755 "${DIR}"
if [ ! -f "${DIR}/.env" ]; then
    echo "  ⚠ ${DIR}/.env нет — создаю пустой (0600); ключи и GATEWAY_TOKEN впишите отдельно"
    echo "    и после этого: systemctl restart leadchat-gateway (окружение читается при старте)"
    ( umask 077; : > "${DIR}/.env" )
fi
chmod 600 "${DIR}/.env"
systemctl daemon-reload
systemctl enable leadchat-gateway >/dev/null 2>&1 || true
systemctl restart leadchat-gateway
sleep 2
if ! systemctl is-active --quiet leadchat-gateway; then
    journalctl -u leadchat-gateway -n 30 --no-pager
    echo "юнит не поднялся" >&2
    exit 1
fi
curl -fsS --max-time 5 http://10.10.0.2:8792/health
echo
REMOTE
ok "шлюз отвечает на /health"
