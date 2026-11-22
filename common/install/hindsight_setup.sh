#!/usr/bin/env bash
# Hindsight, the agents' memory server, run as root:
#   hindsight_setup.sh install|check|disable
# install: brings the server to the code's, every step idempotent, so a run that stopped half-way is finished by the
#   next: PostgreSQL's config (the cluster restarts only for one that differs), the role hindsight with its settings,
#   the database and its extensions, the models in the base user's cache (loaded offline; only a missing one is
#   fetched), the units rendered, the services enabled and started. Made once and then kept, as on a live host: the
#   venv /opt/hindsight/venv-<version> (torch from the CPU index, hindsight-api pinned in
#   common/install/versions.json) behind the link /opt/hindsight/venv, and the env from
#   common/hindsight/coding-agent.env.example with a fresh database password and tenant key (root 0600, written
#   whole). An env made by an older tree gets the example's settings it lacks appended — a value already there,
#   the secrets included, is never changed — and only then do the two services restart; otherwise a running
#   server restarts for nothing. Then check. PostgreSQL and pgvector themselves are only checked
#   (prereq.sh --hindsight): their vendor's apt repository is the owner's call. Network: PyPI, the PyTorch index and
#   huggingface.co during install only.
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: the services and the backup timer stop and are disabled; the database, venv and env stay.
set -euo pipefail
TREE=$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd -P)
HS=$TREE/common/hindsight
ENV=/etc/hindsight/coding-agent.env
OPT=/opt/hindsight
VENV=$OPT/venv
UNITS=/etc/systemd/system
BACKUP=/usr/local/sbin/hindsight-backup.sh
HEALTH=http://127.0.0.1:9077/health
USAGE="usage: hindsight_setup.sh install|check|disable"

mode=${1:-}
case $mode in
  install|check|disable) ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ $# -eq 1 ] || { echo "$USAGE" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
# root's cwd (/root 0700) is closed to runuser of postgres and of the base user
cd /
# shellcheck source=lib/naming.sh
. "$TREE/common/install/lib/naming.sh"
version() { /usr/bin/python3 -I "$TREE/common/kd/install_catalog.py" version "$1"; }
pg=$(version postgresql) && api=$(version hindsight_api) && torch=$(version torch) \
  || { echo "the version catalog is unreadable" >&2; exit 1; }
CONF=/etc/postgresql/$pg/main/conf.d/hindsight.conf
base=$(kd_user base)
# the dumps' directory is the host's (host.json backup_dir); the backup unit gets the same value rendered
DUMPS=$(/usr/bin/python3 -I "$TREE/common/kd/naming.py" host backup_dir)/hindsight

psql_root() { runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 "$@"; }
ask() { runuser -u postgres -- psql -X -Atq "$@" 2>/dev/null || true; }
env_value() { sed -n "s/^$1=//p" "$ENV"; }

settings_of() {
  grep -v -e '^[[:space:]]*#' -e '^[[:space:]]*$' "$1"
}

# the settings, not the bytes: a header comment of an older tree is not a difference
same_conf() {
  [ -f "$CONF" ] && [ "$(settings_of "$HS/postgresql-hindsight.conf")" = "$(settings_of "$CONF")" ]
}

put_rendered() {
  kd_render "$1" | cmp -s - "$2" || kd_install_rendered "$1" "$2" 0644
}

put_plain() {
  cmp -s "$1" "$2" || install -o root -g root -m "$3" "$1" "$2"
}

problems=()
check() {
  bash "$TREE/common/install/prereq.sh" check --hindsight >/dev/null \
    || problems+=("prerequisites not ready — the prereq lines above")
  same_conf || problems+=("$CONF: settings differ from $HS/postgresql-hindsight.conf")
  [ "$(ask -c "select 1 from pg_roles where rolname = 'hindsight'")" = 1 ] || problems+=("no role hindsight")
  [ "$(ask -c "select 1 from pg_database where datname = 'hindsight'")" = 1 ] || problems+=("no database hindsight")
  [ "$(ask -d hindsight -c "select string_agg(extname, ' ' order by extname) from pg_extension
                            where extname in ('vector', 'pg_trgm')")" = "pg_trgm vector" ] \
    || problems+=("the database hindsight lacks the extensions vector and pg_trgm")
  if [ ! -x "$VENV/bin/hindsight-api" ]; then
    problems+=("no $VENV/bin/hindsight-api")
  else
    local have
    have=$("$VENV/bin/python" -I -c 'import importlib.metadata as m; print(m.version("hindsight-api"))' 2>/dev/null || true)
    [ "$have" = "$api" ] || problems+=("hindsight-api ${have:-?}, the version catalog has $api")
  fi
  if [ ! -f "$ENV" ]; then
    problems+=("no $ENV")
  else
    [ "$(stat -c '%U:%G %a' "$ENV")" = "root:root 600" ] || problems+=("$ENV: expected root:root 600")
    local key
    for key in HINDSIGHT_API_TENANT_API_KEY HINDSIGHT_API_DATABASE_URL; do
      grep -q "^$key=." "$ENV" || problems+=("$ENV: no $key")
    done
    ! grep -q '@db_password@\|@tenant_key@' "$ENV" || problems+=("$ENV: a placeholder of the example is left")
    local missing
    missing=$(missing_env_lines | cut -d= -f1 | paste -sd' ' -)
    [ -z "$missing" ] || problems+=("$ENV: lacks $missing of the example — hindsight_setup.sh install adds them")
  fi
  local unit
  for unit in hindsight-api.service hindsight-worker.service hindsight-backup.timer; do
    kd_render "$HS/$unit" | cmp -s - "$UNITS/$unit" || problems+=("$UNITS/$unit: differs from the code")
    systemctl is-enabled --quiet "$unit" || problems+=("$unit is disabled")
  done
  kd_render "$HS/hindsight-backup.service" | cmp -s - "$UNITS/hindsight-backup.service" \
    || problems+=("$UNITS/hindsight-backup.service: differs from the code")
  cmp -s "$HS/hindsight-backup.sh" "$BACKUP" || problems+=("$BACKUP: differs from the code")
  [ -d "$DUMPS" ] || problems+=("$DUMPS: no dump directory (backup_dir in host.json)")
  systemctl is-active --quiet hindsight-worker.service || problems+=("hindsight-worker is not running")
  if systemctl is-active --quiet hindsight-api.service; then
    curl -fsS -m 10 -o /dev/null "$HEALTH" || problems+=("hindsight-api does not answer on $HEALTH")
  else
    problems+=("hindsight-api is not running — journalctl -u hindsight-api")
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'hindsight: %s\n' "${problems[@]}" >&2
    exit 1
  fi
  echo "hindsight: ok ($api, PostgreSQL $pg)"
}

# a new env: the role's password set to the one it carries, a new tenant key
write_env() {
  local password key tmp
  password=$(openssl rand -hex 24)
  key=$(openssl rand -hex 32)
  install -d -o root -g root -m 0755 "$(dirname "$ENV")"
  tmp=$(umask 077; mktemp "$ENV.XXXXXX")
  sed -e "s|@db_password@|$password|" -e "s|@tenant_key@|$key|" "$HS/coding-agent.env.example" > "$tmp"
  chown root:root "$tmp"
  chmod 0600 "$tmp"
  # the env is the one keeper of the password: a role left without its env gets the new one
  printf "ALTER ROLE hindsight LOGIN PASSWORD '%s';\n" "$password" | psql_root || { rm -f "$tmp"; exit 1; }
  mv -f "$tmp" "$ENV"
}

# the example's settings an env made by an older tree lacks, one line each; a secret's line is never among them
# (the env keeps its own, and check says when one is missing)
missing_env_lines() {
  local line key
  while IFS= read -r line || [ -n "$line" ]; do
    case $line in '' | '#'* | *@db_password@* | *@tenant_key@*) continue ;; esac
    key=${line%%=*}
    grep -q "^$key=" "$ENV" || printf '%s\n' "$line"
  done < "$HS/coding-agent.env.example"
}

# appends them, the file written whole as write_env does; true when it added any
add_env_settings() {
  local missing tmp
  missing=$(missing_env_lines)
  [ -n "$missing" ] || return 1
  tmp=$(umask 077; mktemp "$ENV.XXXXXX")
  { sed -e '$a\' "$ENV"; printf '%s\n' "$missing"; } > "$tmp"
  chown root:root "$tmp"
  chmod 0600 "$tmp"
  mv -f "$tmp" "$ENV"
  echo "hindsight: $ENV — added $(printf '%s\n' "$missing" | cut -d= -f1 | paste -sd' ' -)"
}

# the locale archive is newer than the running server: the server does not see a locale generated after its start
locale_newer_than_server() {
  local archive=/usr/lib/locale/locale-archive started
  [ -f "$archive" ] || return 1
  started=$(ask -c "select extract(epoch from pg_postmaster_start_time())::bigint")
  [ -n "$started" ] && [ "$(stat -c %Y "$archive")" -gt "$started" ]
}

# PostgreSQL's side, every install: each step only when it differs or is missing
server_db() {
  if ! same_conf; then
    install -o root -g root -m 0644 "$HS/postgresql-hindsight.conf" "$CONF"
    systemctl restart "postgresql@$pg-main.service"
  fi
  locale -a | grep -qix 'en_US.utf8' || locale-gen en_US.UTF-8
  [ "$(ask -c "select 1 from pg_roles where rolname = 'hindsight'")" = 1 ] || psql_root -c "CREATE ROLE hindsight LOGIN"
  if [ "$(ask -c "select 1 from pg_database where datname = 'hindsight'")" != 1 ]; then
    # generated by this run or an earlier one while the server ran: invalid LC_COLLATE locale name without a restart
    locale_newer_than_server && systemctl restart "postgresql@$pg-main.service"
    psql_root -c "CREATE DATABASE hindsight OWNER hindsight ENCODING 'UTF8' LC_COLLATE 'en_US.UTF-8'
                  LC_CTYPE 'en_US.UTF-8' TEMPLATE template0"
  fi
  psql_root -d hindsight -c "CREATE EXTENSION IF NOT EXISTS vector; CREATE EXTENSION IF NOT EXISTS pg_trgm;"
  # the live server's role settings (search depth of the vector index, long consolidation statements)
  psql_root -c "ALTER ROLE hindsight SET hnsw.ef_search = 200" \
    -c "ALTER ROLE hindsight SET hnsw.iterative_scan = strict_order" \
    -c "ALTER ROLE hindsight SET hnsw.max_scan_tuples = 4000" \
    -c "ALTER ROLE hindsight SET statement_timeout = '600s'" \
    -c "ALTER ROLE hindsight SET pg_trgm.similarity_threshold = 0.15"
}

make_venv() {
  local dir=$OPT/venv-$api
  rm -rf "$dir"
  install -d -o root -g root -m 0755 "$OPT"
  python3 -m venv "$dir"
  "$dir/bin/pip" install -q --disable-pip-version-check --no-input --upgrade pip
  "$dir/bin/pip" install -q --disable-pip-version-check --no-input \
    --index-url https://download.pytorch.org/whl/cpu "torch==$torch"
  "$dir/bin/pip" install -q --disable-pip-version-check --no-input --only-binary=:all: "hindsight-api==$api"
  chmod -R go+rX "$dir"
  [ -L "$VENV" ] || rm -rf "$VENV"  # a broken venv without hindsight-api, never a link's target
  ln -sfn "$dir" "$VENV"
}

# the models where the server reads them offline (the base user's cache): loaded from it when there, else fetched
warm_models() {
  local load=('import sys; from sentence_transformers import SentenceTransformer, CrossEncoder; SentenceTransformer(sys.argv[1]); CrossEncoder(sys.argv[2])'
              "$(env_value HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL)" "$(env_value HINDSIGHT_API_RERANKER_LOCAL_MODEL)")
  local home
  home=$(getent passwd "$base" | cut -d: -f6)
  runuser -u "$base" -- env HOME="$home" HF_HUB_OFFLINE=1 "$VENV/bin/python" -I -c "${load[@]}" >/dev/null 2>&1 && return 0
  runuser -u "$base" -- env HOME="$home" HF_HUB_OFFLINE=0 "$VENV/bin/python" -I -c "${load[@]}"
}

install_all() {
  bash "$TREE/common/install/prereq.sh" check --hindsight \
    || { echo "hindsight: PostgreSQL $pg and pgvector first — the commands above" >&2; exit 1; }
  server_db
  # the password, the tenant key and the venv: made once, a server that has them keeps them
  [ -x "$VENV/bin/hindsight-api" ] || make_venv
  [ -f "$ENV" ] || write_env
  local env_added=0
  add_env_settings && env_added=1
  warm_models
  local unit
  for unit in hindsight-api.service hindsight-worker.service hindsight-backup.timer; do
    put_rendered "$HS/$unit" "$UNITS/$unit"
  done
  put_rendered "$HS/hindsight-backup.service" "$UNITS/hindsight-backup.service"
  put_plain "$HS/hindsight-backup.sh" "$BACKUP" 0755
  if [ ! -d "$DUMPS" ]; then
    # an existing parent (a deployment's backup tree) keeps its owner and mode
    [ -d "$(dirname "$DUMPS")" ] || install -d -o root -g root -m 0755 "$(dirname "$DUMPS")"
    install -d -o postgres -g postgres -m 0700 "$DUMPS"
  fi
  systemctl daemon-reload
  systemctl enable --now hindsight-api.service hindsight-worker.service hindsight-backup.timer
  # a running server reads its env at start: the settings just added take effect only now
  [ "$env_added" = 0 ] || systemctl restart hindsight-api.service hindsight-worker.service
  local i
  for i in $(seq 1 60); do  # the first start loads torch and the models: up to five minutes
    curl -fsS -m 5 -o /dev/null "$HEALTH" && break
    sleep 5
  done
  echo "hindsight: the next stage_setup.sh installs the agents' clients (bootstrap.sh does it itself)"
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable)
    if systemctl cat hindsight-api.service >/dev/null 2>&1; then
      systemctl disable --now hindsight-api.service hindsight-worker.service hindsight-backup.timer
    fi
    ;;
esac
