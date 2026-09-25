#!/bin/sh
# ============================================================================
# Фоновый reload nginx каждые 6 часов — подхват продлённого сертификата
# без даунтайма и без рестарта контейнера (05 §2.2).
#
# Почему не `command:` в compose, как в исходном наброске 05 §2.2:
# штатный entrypoint образа nginx выполняет /docker-entrypoint.d/* (в том числе
# envsubst по templates/) ТОЛЬКО если команда начинается с "nginx". Подменив
# command на `sh -c "...loop... & nginx"`, мы бы молча потеряли подстановку
# ${DOMAIN}/${MEDIA_SIGN_KEY} и конфиг вообще не собрался бы.
# Поэтому цикл живёт здесь: скрипт стартует до exec nginx, уходит в фон и
# переусыновляется мастер-процессом.
# ============================================================================
set -eu

RELOAD_INTERVAL="${NGINX_RELOAD_INTERVAL:-6h}"

# stdout/stderr цикла закрыты намеренно: иначе фоновый процесс держит открытым
# pipe контейнера, и одноразовые запуски (docker run … nginx -t) висят вечно.
# Сам факт reload виден в логах контейнера — nginx пишет "signal process started".
(
    while :; do
        sleep "$RELOAD_INTERVAL"
        if nginx -t; then
            nginx -s reload
        fi
    done
) >/dev/null 2>&1 &

echo "[reload-loop] запущен, интервал ${RELOAD_INTERVAL}"
