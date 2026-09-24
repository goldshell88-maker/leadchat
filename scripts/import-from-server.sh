#!/usr/bin/env bash
#
# Publishes an application directory from a server to the LeadChat repository.
#
# The application directory is only read. A sanitised copy is committed to the
# branch import/<component> of a temporary clone: dependencies, build output,
# logs, uploaded files, databases and key material stay behind, .env files are
# replaced by .env.example with the values removed, and hard-coded secrets are
# masked. The branch is reviewed before anything reaches main.
#
# Usage:
#   import-from-server.sh                          list service directories on this host
#   import-from-server.sh <app-dir> <component>    publish <app-dir> as <component>/

set -Eeuo pipefail

readonly REPO_URL="git@github.com:goldshell88-maker/leadchat.git"
readonly REPO_WEB="https://github.com/goldshell88-maker/leadchat"
readonly DEPLOY_KEY="${HOME}/.ssh/leadchat_deploy"
readonly MAX_FILE_SIZE_MB=5
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
readonly SCRIPT_DIR
readonly MASK_SECRETS="${SCRIPT_DIR}/lib/mask-secrets.pl"

# rsync filter rules. Anything matched here never leaves the server.
readonly FILTER_RULES=(
  # Version control, editors, OS
  '- .git/' '- .hg/' '- .svn/' '- .idea/' '- .vscode/' '- .DS_Store' '- Thumbs.db' '- *.swp' '- *~'
  # Dependencies and build output
  '- node_modules/' '- bower_components/' '- vendor/' '- venv/' '- .venv/' '- __pycache__/' '- *.py[cod]'
  '- .pytest_cache/' '- .mypy_cache/' '- .ruff_cache/' '- dist/' '- build/' '- .next/' '- .nuxt/'
  '- .output/' '- .cache/' '- coverage/'
  # Runtime data: logs, uploaded files, databases, backups, archives
  '- logs/' '- log/' '- *.log' '- tmp/' '- temp/' '- *.pid' '- *.sock'
  '- uploads/' '- upload/' '- attachments/' '- /storage/' '- /media/' '- /data/'
  '- backup*/' '- dump*/' '- *.bak' '- *.backup' '- *.dump' '- *.sql.gz' '- *.old' '- *.orig'
  '- *.sqlite' '- *.sqlite3' '- *.db' '- *.db-journal' '- db.json' '- *.csv' '- *.xls' '- *.xlsx'
  '- *.zip' '- *.tar' '- *.tar.gz' '- *.tgz' '- *.rar' '- *.7z'
  # Secrets and credentials
  '- .env' '- .env.*' '- *.env' '- *.pem' '- *.key' '- *.p12' '- *.pfx' '- *.jks' '- *.keystore'
  '- id_rsa*' '- id_ed25519*' '- *.ppk' '- .npmrc' '- .pypirc' '- .netrc' '- .htpasswd'
  '- credentials*.json' '- service-account*.json' '- token.json' '- *.session' '- *.session-journal'
)

EXCLUDED=()
OVERSIZED=()
DUMPS=()
ENV_EXAMPLES=()
MASKED=()
WORKDIR=""

info() { printf '%s\n' "$*"; }
warn() { printf 'Внимание: %s\n' "$*" >&2; }
die() { printf 'Ошибка: %s\n' "$*" >&2; exit 1; }

usage() {
  cat >&2 <<EOF
Использование:
  $0                          показать каталоги сервисов на этом сервере
  $0 <каталог> <компонент>    выгрузить каталог в папку <компонент>/ репозитория

Пример:
  $0 /var/www/leadchat leadchat
EOF
  exit 2
}

require_commands() {
  local cmd missing=()
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  if (( ${#missing[@]} )); then
    die "не установлены: ${missing[*]} (Debian/Ubuntu: apt install ${missing[*]})"
  fi
}

list_service_dirs() {
  info "Сервисы и их рабочие каталоги:"
  if command -v pm2 >/dev/null 2>&1 && command -v node >/dev/null 2>&1; then
    # shellcheck disable=SC2016  # JavaScript template literal
    pm2 jlist 2>/dev/null | node -e '
      let raw = "";
      process.stdin.on("data", (chunk) => (raw += chunk)).on("end", () => {
        for (const app of JSON.parse(raw)) {
          console.log(`  pm2      ${app.name.padEnd(28)} ${app.pm2_env.pm_cwd}`);
        }
      });' 2>/dev/null || true
  fi
  if command -v systemctl >/dev/null 2>&1; then
    local unit dir
    while read -r unit _; do
      dir=$(systemctl show -p WorkingDirectory --value "$unit" 2>/dev/null || true)
      if [[ -n $dir && $dir != / ]]; then
        printf '  systemd  %-28s %s\n' "$unit" "$dir"
      fi
    done < <(systemctl list-units --type=service --state=running --no-legend --plain 2>/dev/null)
  fi
  if command -v docker >/dev/null 2>&1; then
    docker ps --format '  docker   {{.Names}}  {{.Label "com.docker.compose.project.working_dir"}}' 2>/dev/null || true
  fi

  info ""
  info "Каталоги с проектами:"
  find /root /home /opt /srv /var/www -maxdepth 4 -type f \
    \( -name package.json -o -name requirements.txt -o -name pyproject.toml -o -name composer.json -o -name go.mod \) \
    -not -path '*/node_modules/*' -printf '  %h\n' 2>/dev/null | sort -u
  info ""
  info "Запустите: $0 <каталог> <компонент>, например: $0 /var/www/leadchat leadchat"
}

ensure_deploy_key() {
  if [[ ! -f $DEPLOY_KEY ]]; then
    mkdir -p -- "$(dirname -- "$DEPLOY_KEY")"
    chmod 700 -- "$(dirname -- "$DEPLOY_KEY")"
    ssh-keygen -q -t ed25519 -N '' -C "leadchat-deploy@$(hostname)" -f "$DEPLOY_KEY"
  fi

  local reply
  reply=$(ssh -i "$DEPLOY_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new \
    -o BatchMode=yes -T git@github.com 2>&1 || true)
  if [[ $reply == *"successfully authenticated"* ]]; then
    return
  fi

  cat >&2 <<EOF
GitHub не принимает ключ этого сервера. Добавьте его как Deploy key:
  1. Откройте ${REPO_WEB}/settings/keys/new
  2. Title: $(hostname)
  3. Key:   $(cat -- "${DEPLOY_KEY}.pub")
  4. Отметьте «Allow write access» и нажмите «Add key».
Затем запустите скрипт ещё раз.
EOF
  exit 1
}

copy_tree() {
  local src=$1 dest=$2 status=0 line
  printf '%s\n' "${FILTER_RULES[@]}" >"$WORKDIR/filters"

  rsync -a --filter="merge $WORKDIR/filters" --debug=FILTER1 -- "$src/" "$dest/" \
    >"$WORKDIR/rsync.log" 2>&1 || status=$?
  case $status in
    0) ;;
    23 | 24) warn "часть файлов не удалось прочитать:"; grep '^rsync' "$WORKDIR/rsync.log" >&2 || true ;;
    *) cat -- "$WORKDIR/rsync.log" >&2; die "rsync завершился с кодом $status" ;;
  esac

  while IFS= read -r line; do
    if [[ $line =~ hiding\ (file|directory)\ (.+)\ because\ of\ pattern ]]; then
      EXCLUDED+=("${BASH_REMATCH[2]}")
    fi
  done <"$WORKDIR/rsync.log"
}

drop_large_files() {
  local dest=$1 file
  while IFS= read -r -d '' file; do
    OVERSIZED+=("${file#"$dest"/}")
    rm -f -- "$file"
  done < <(find "$dest" -type f -size +"${MAX_FILE_SIZE_MB}"M -print0)
}

# Database dumps sometimes carry the .sql extension of schema files.
drop_sql_dumps() {
  local dest=$1 file inserts
  while IFS= read -r -d '' file; do
    inserts=$(grep -ci '^insert into' -- "$file" || true)
    if (( inserts > 20 )) || grep -qiE '^-- (mysql dump|postgresql database dump)|^copy .* from stdin;' -- "$file"; then
      DUMPS+=("${file#"$dest"/}")
      rm -f -- "$file"
    fi
  done < <(find "$dest" -type f -name '*.sql' -print0)
}

# Every directory that had .env files gets a .env.example listing the variable names.
write_env_examples() {
  local src=$1 dest=$2 path dir target
  local -A env_dirs=()
  for path in "${EXCLUDED[@]}"; do
    case ${path##*/} in
      .env | .env.* | *.env) env_dirs[$(dirname -- "$path")]=1 ;;
    esac
  done

  for dir in "${!env_dirs[@]}"; do
    [[ $dir == . ]] && dir=""
    target="$dest${dir:+/$dir}/.env.example"
    # shellcheck disable=SC2016  # Perl code
    find "$src${dir:+/$dir}" -maxdepth 1 -type f \( -name '.env' -o -name '.env.*' -o -name '*.env' \) -print0 \
      | sort -z \
      | xargs -0 -r perl -ne 'print "$1=\n" if /^\s*(?:export\s+)?([A-Za-z_]\w*)\s*=/ && !$seen{$1}++' \
      >"$target"
    ENV_EXAMPLES+=("${target#"$dest"/}")
  done
}

mask_secrets() {
  local dest=$1 count file
  while IFS=$'\t' read -r count file; do
    MASKED+=("$(printf '%4d  %s' "$count" "${file#"$dest"/}")")
  done < <(grep -rlIZ '' -- "$dest" | xargs -0 -r perl -0777 -i -p "$MASK_SECRETS")
}

print_list() {
  local title=$1 limit=40
  shift
  (( $# )) || return 0
  info "$title"
  printf '  %s\n' "${@:1:limit}"
  if (( $# > limit )); then
    info "  …и ещё $(( $# - limit ))"
  fi
}

print_summary() {
  local src=$1 component=$2 dest=$3 files size
  files=$(find "$dest" -type f | wc -l)
  size=$(du -sh -- "$dest" | cut -f1)

  info ""
  info "Источник:   $(hostname):$src"
  info "Назначение: $component/ в ветке import/$component"
  info "Файлов:     $files ($size)"
  info ""
  print_list "Не выгружены (зависимости, логи, данные, секреты):" "${EXCLUDED[@]}"
  print_list "Больше ${MAX_FILE_SIZE_MB} МБ, не выгружены:" "${OVERSIZED[@]}"
  print_list "Дампы баз данных, не выгружены:" "${DUMPS[@]}"
  print_list "Созданы .env.example (только имена переменных):" "${ENV_EXAMPLES[@]}"
  print_list "Секреты заменены на <REDACTED> (замен, файл):" "${MASKED[@]}"
  info ""
}

confirm() {
  local reply
  read -r -p "$1 [y/N] " reply || return 1
  [[ $reply == [yYдД]* ]]
}

commit_message() {
  local src=$1 component=$2
  printf 'Import %s from %s\n\n' "$component" "$(hostname)"
  printf 'Snapshot of %s taken %s.\n' "$src" "$(date -u '+%Y-%m-%d %H:%M UTC')"
  if (( ${#EXCLUDED[@]} )); then
    printf '\nLeft on the server:\n'
    printf '  %s\n' "${EXCLUDED[@]:0:200}"
  fi
  if (( ${#OVERSIZED[@]} + ${#DUMPS[@]} )); then
    printf '\nLarge files and database dumps left on the server:\n'
    printf '  %s\n' "${OVERSIZED[@]}" "${DUMPS[@]}"
  fi
  if (( ${#MASKED[@]} )); then
    printf '\nSecrets masked:\n'
    printf '  %s\n' "${MASKED[@]}"
  fi
}

publish() {
  local src=$1 component=$2 repo="$WORKDIR/repo" branch="import/$2"
  git -C "$repo" checkout -q -b "$branch"
  git -C "$repo" add -A -- "$component"
  if git -C "$repo" diff --cached --quiet; then
    info "В репозитории уже эта версия, загружать нечего."
    return
  fi
  commit_message "$src" "$component" >"$WORKDIR/commit-message"
  git -C "$repo" -c user.name="LeadChat import" -c user.email="import@$(hostname)" \
    commit -q -F "$WORKDIR/commit-message"
  git -C "$repo" push -q --force origin "HEAD:refs/heads/$branch"
  info "Готово: ${REPO_WEB}/tree/$branch"
}

main() {
  if (( $# == 0 )); then
    list_service_dirs
    return
  fi
  (( $# == 2 )) || usage
  (( BASH_VERSINFO[0] > 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] >= 4) )) || die "нужен bash 4.4 или новее"

  local src component dest
  src=$(realpath -e -- "$1" 2>/dev/null) || die "каталог не найден: $1"
  [[ -d $src ]] || die "это не каталог: $src"
  component=$2
  [[ $component =~ ^[a-z0-9][a-z0-9-]*$ ]] || die "имя компонента: строчная латиница, цифры и дефис, например leadchat"
  [[ -f $MASK_SECRETS ]] || die "не найден $MASK_SECRETS, запускайте скрипт из клона репозитория"

  require_commands git rsync perl ssh ssh-keygen
  ensure_deploy_key
  export GIT_SSH_COMMAND="ssh -i '$DEPLOY_KEY' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"

  WORKDIR=$(mktemp -d)
  trap 'rm -rf -- "$WORKDIR"' EXIT

  info "Клонирую репозиторий…"
  git clone -q --depth 1 "$REPO_URL" "$WORKDIR/repo"
  dest="$WORKDIR/repo/$component"
  rm -rf -- "$dest"
  mkdir -p -- "$dest"

  info "Копирую $src…"
  copy_tree "$src" "$dest"
  drop_large_files "$dest"
  drop_sql_dumps "$dest"
  write_env_examples "$src" "$dest"
  mask_secrets "$dest"

  print_summary "$src" "$component" "$dest"
  confirm "Загрузить в GitHub, ветка import/$component?" || die "отменено"
  publish "$src" "$component"
}

main "$@"
