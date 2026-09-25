#!/usr/bin/env bash
# Хостовая часть наблюдения за прогоном — docs/07-TESTING-SECURITY.md §3.2.
#
# Снимает то, что не видно изнутри контейнера: CPU/RAM/сеть по контейнерам и
# счётчик перезапусков (критерий §3.3 п.3 «ни один контейнер не перезапустился»).
# Очередь, PEL и pg_stat_activity снимает tests/load/inject.py — они там на
# одной временной сетке с задержками доставки.
#
#   ./watch.sh [интервал_сек] [файл]      # Ctrl-C или kill — остановка
#
# Формат строки: NDJSON, одна выборка — один объект.

set -euo pipefail
INTERVAL="${1:-15}"
OUT="${2:-/tmp/leadchat-load-stats.ndjson}"

echo "watch.sh: интервал ${INTERVAL}s, вывод ${OUT}" >&2
: > "$OUT"

while true; do
  TS=$(date -u +%FT%TZ)
  STATS=$(docker stats --no-stream --format \
    '{"name":"{{.Name}}","cpu":"{{.CPUPerc}}","mem":"{{.MemUsage}}","mem_perc":"{{.MemPerc}}","net":"{{.NetIO}}","pids":"{{.PIDs}}"}' \
    | paste -sd, -)
  # printf с \n обязателен: paste -sd, склеивает СТРОКИ, а без перевода строки
  # все объекты оказываются в одной строке и склеиваются без запятых — JSON бьётся.
  RESTARTS=$(docker ps --format '{{.Names}}' \
    | xargs -r -I{} sh -c 'printf "{\"name\":\"%s\",\"restarts\":%s,\"started\":\"%s\"}\n" \
        "{}" "$(docker inspect -f "{{.RestartCount}}" {})" "$(docker inspect -f "{{.State.StartedAt}}" {})"' \
    | paste -sd, -)
  echo "{\"ts\":\"$TS\",\"stats\":[$STATS],\"containers\":[$RESTARTS]}" >> "$OUT"
  sleep "$INTERVAL"
done
