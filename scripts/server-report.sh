#!/usr/bin/env bash
#
# Read-only overview of a LeadChat server: what runs there, what takes up disk
# space and what can grow without limit. Nothing on the server is changed, and
# values that look like credentials are masked in the output.
#
# Usage: sudo bash server-report.sh > report-$(hostname).txt

set -uo pipefail  # no -e: one failing probe must not cut the report short

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
readonly MASK_SECRETS="${SCRIPT_DIR}/lib/mask-secrets.pl"
readonly FS_EXCLUDES=(-x tmpfs -x devtmpfs -x overlay -x squashfs)

section() { printf '\n=== %s ===\n' "$1"; }
have() { command -v "$1" >/dev/null 2>&1; }
human_sizes() { numfmt --to=iec --field=1 --delimiter=$'\t' 2>/dev/null || cat; }

system_info() {
  section "Система"
  hostname
  (. /etc/os-release && echo "$PRETTY_NAME")
  uname -r
  uptime
  free -h
  if have timedatectl; then
    timedatectl | grep -E 'Local time|Universal time|Time zone|synchronized'
  else
    date
  fi
}

disk_usage() {
  section "Диски"
  df -hT "${FS_EXCLUDES[@]}"
  df -i "${FS_EXCLUDES[@]}"

  section "Крупнейшие каталоги"
  timeout 300 du -xh --max-depth=3 / 2>/dev/null | sort -rh | head -30

  section "Файлы больше 100 МБ"
  find / -xdev -type f -size +100M -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -30 | human_sizes

  section "Дампы, бэкапы и архивы больше 10 МБ"
  find / -xdev -type f -size +10M \
    \( -name '*.sql' -o -name '*.sql.gz' -o -name '*.dump' -o -name '*.bak' -o -name '*.backup' \
       -o -name '*.tar' -o -name '*.tar.gz' -o -name '*.tgz' -o -name '*.zip' -o -name '*.gz' \) \
    -not -path '/var/lib/docker/*' -not -path '/usr/*' -printf '%s\t%TY-%Tm-%Td\t%p\n' 2>/dev/null \
    | sort -rn | head -30 | human_sizes
}

logs_and_caches() {
  section "Журналы"
  du -sh /var/log 2>/dev/null
  have journalctl && journalctl --disk-usage 2>/dev/null
  find /var/log -xdev -type f -size +50M -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -15 | human_sizes
  local dir
  for dir in /root/.pm2 /home/*/.pm2; do
    [[ -d $dir/logs ]] || continue
    du -sh "$dir/logs"
    if [[ -d $dir/modules/pm2-logrotate ]]; then
      echo "  pm2-logrotate: установлен"
    else
      echo "  pm2-logrotate: НЕ установлен, логи pm2 растут без ограничений"
    fi
  done
  if [[ -d /var/lib/docker/containers ]]; then
    echo "Логи docker-контейнеров:"
    find /var/lib/docker/containers -name '*-json.log' -printf '%s\t%p\n' 2>/dev/null | sort -rn | head -10 | human_sizes
  fi

  section "Зависимости и кэши"
  find / -xdev -type d -name node_modules -prune -not -path '/usr/*' -print0 2>/dev/null \
    | xargs -0 -r du -sh 2>/dev/null | sort -rh | head -15
  du -sh /var/cache/apt /root/.npm /root/.cache/pip /root/.cache/yarn /home/*/.npm /home/*/.cache/pip /tmp 2>/dev/null
  have docker && docker system df 2>/dev/null
}

databases() {
  section "Базы данных"
  du -sh /var/lib/postgresql /var/lib/mysql /var/lib/mongodb /var/lib/redis 2>/dev/null
  if have psql && id postgres >/dev/null 2>&1; then
    local db
    timeout 20 sudo -u postgres psql -AXtc \
      "select datname || E'\t' || pg_size_pretty(pg_database_size(datname)) from pg_database
       where not datistemplate order by pg_database_size(datname) desc" 2>/dev/null
    while read -r db; do
      [[ -n $db ]] || continue
      echo "Крупнейшие таблицы в $db:"
      timeout 20 sudo -u postgres psql -AXt -d "$db" -c \
        "select '  ' || relname || E'\t' || pg_size_pretty(pg_total_relation_size(relid)) || E'\t~' || n_live_tup || ' строк'
         from pg_stat_user_tables order by pg_total_relation_size(relid) desc limit 10" 2>/dev/null
    done < <(timeout 20 sudo -u postgres psql -AXtc "select datname from pg_database where not datistemplate and datname <> 'postgres'" 2>/dev/null)
  fi
  if have mysql; then
    timeout 20 mysql -Nse \
      "select table_schema, table_name, round((data_length + index_length) / 1024 / 1024, 1) as mb
       from information_schema.tables where table_schema not in ('mysql','sys','information_schema','performance_schema')
       order by mb desc limit 15" 2>/dev/null
  fi
}

services() {
  section "Сервисы"
  if have pm2 && have node; then
    # shellcheck disable=SC2016  # JavaScript template literal
    pm2 jlist 2>/dev/null | node -e '
      let raw = "";
      process.stdin.on("data", (chunk) => (raw += chunk)).on("end", () => {
        for (const app of JSON.parse(raw)) {
          const env = app.pm2_env;
          const uptime = env.status === "online" ? Math.round((Date.now() - env.pm_uptime) / 36e5) + " ч" : "-";
          const memory = Math.round(((app.monit && app.monit.memory) || 0) / 1048576) + " МБ";
          console.log(`pm2  ${app.name}  ${env.status}  рестартов: ${env.restart_time}  аптайм: ${uptime}  память: ${memory}  ${env.pm_cwd}`);
        }
      });' 2>/dev/null
  fi
  if have systemctl; then
    local unit dir
    while read -r unit _; do
      dir=$(systemctl show -p WorkingDirectory --value "$unit" 2>/dev/null)
      if [[ -n $dir && $dir != / ]]; then
        echo "systemd  $unit  $dir  рестартов: $(systemctl show -p NRestarts --value "$unit" 2>/dev/null)"
      fi
    done < <(systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null)
  fi
  have docker && docker ps --format 'docker  {{.Names}}  {{.Status}}  {{.Image}}' 2>/dev/null

  section "Задачи по расписанию"
  local user
  while IFS=: read -r user _; do
    crontab -l -u "$user" 2>/dev/null | grep -Ev '^\s*(#|$)' | sed "s/^/crontab $user: /"
  done </etc/passwd
  grep -Ehv '^\s*(#|$)' /etc/cron.d/* 2>/dev/null | sed 's/^/cron.d: /'
  have systemctl && systemctl list-timers --no-pager 2>/dev/null | head -20

  section "Открытые порты"
  ss -tlnp 2>/dev/null
}

security() {
  section "Базовая защита"
  have sshd && sshd -T 2>/dev/null | grep -Ei '^(port|permitrootlogin|passwordauthentication|pubkeyauthentication|maxauthtries) '
  if have ufw; then ufw status verbose 2>/dev/null; else echo "ufw не установлен"; fi
  if have fail2ban-client; then fail2ban-client status 2>/dev/null; else echo "fail2ban не установлен"; fi
  if have apt; then
    echo "Доступно обновлений пакетов: $(apt list --upgradable 2>/dev/null | grep -c upgradable)"
    if dpkg -s unattended-upgrades >/dev/null 2>&1; then
      echo "unattended-upgrades: установлен"
    else
      echo "unattended-upgrades: не установлен, обновления безопасности не ставятся автоматически"
    fi
  fi
}

report() {
  system_info
  disk_usage
  logs_and_caches
  databases
  services
  security
}

if [[ ! -f $MASK_SECRETS ]]; then
  echo "Не найден $MASK_SECRETS, запускайте скрипт из клона репозитория" >&2
  exit 1
fi
(( EUID == 0 )) || echo "Внимание: без sudo часть данных будет недоступна" >&2

report 2>&1 | perl -0777 -p "$MASK_SECRETS"
