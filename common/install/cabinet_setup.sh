#!/usr/bin/env bash
# The owner cabinet (the board, the web and decisions), run as root:
#   cabinet_setup.sh install [--origin https://<host>]   (stage_setup.sh step 9a; kd-release after every release)
#   cabinet_setup.sh check|disable
#   cabinet_setup.sh grant <user>                          (add_user.sh for an agent user, exec_setup.sh for the exec user)
# install: refuses before changing anything without Node.js >= 20, or without cabinet.json and --origin (the first
#   install names the origin people open; another origin later is refused — passkeys are bound to it). Then: the
#   service user (<prefix>-cabinet: system, no login, no home) and the group digipaddock-cabinet of the board's socket with the
#   base user, the exec user when present and every agent user of /etc/digipaddock/agents.json; the data directory;
#   the stores of the owner's decisions (BOSS proposals read by ACL, decisions and delegation grants written by the
#   cabinet, an append ACL on the BOSS audit journal; the sysadmin's plan and grant directories are its module's,
#   exec_setup.sh); the retired approval service and its user go (what it owned becomes the cabinet's); the venv of the
#   web service (pip only when its hashed lock changed); cabinet.json once, cabinet.env (session secret, VAPID public
#   key) and push.env (the VAPID private key: the push sender's, <prefix>-push, the one cabinet process with network)
#   once; the frontend built as the base user and served from a root-owned copy (only when its sources changed); the
#   units rendered with this host's names; nginx (board_setup.sh); everything enabled, a running service restarted on
#   the new code. Inside the sysadmin monitor's rollout window. Idempotent; ends with check.
# check: the same state, changing nothing; the board answers kd-cabinet cards as the base user, the web answers
#   /api/health on its socket; exit 1 with reasons.
# disable: every unit of the cabinet goes off — the board and the web are down for everyone; the data stays.
# grant <user>: the user joins digipaddock-cabinet. Running processes of that user keep their groups until they restart.
set -euo pipefail
REPO=/opt/digipaddock
UNITS=/etc/systemd/system
STATE=/var/lib/digipaddock
DATA=/var/lib/digipaddock/cabinet
VENV=/var/lib/digipaddock/venv/cabinet
REQUIREMENTS=$REPO/common/cabinet/requirements.txt
BUILD_REQUIREMENTS=$REPO/common/cabinet/requirements-build.txt
WEB=/var/lib/digipaddock/cabinet-web
WEB_SRC=$REPO/common/cabinet/web
# what the build reads: no tests' runners, no node_modules, no dist
WEB_FILES="package.json package-lock.json index.html vite.config.ts svelte.config.js tsconfig.json tsconfig.sw.json src public messages"
SETTINGS=/etc/digipaddock/cabinet.json
SECRETS=/etc/digipaddock/cabinet.env
PUSH_SECRETS=/etc/digipaddock/push.env
GROUP=digipaddock-cabinet
SOCKET=/run/digipaddock-cabinet/local.sock
HTTP_SOCKET=/run/digipaddock-cabinet-http/http.sock
PROPOSALS=$STATE/admin-proposals
GRANTS=$STATE/admin-grants
PROMOTIONS=$STATE/boss-promotions
BOARD_LOG=/var/log/digipaddock-board
NODE_MIN=20
ORIGIN_RE='^https://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?$'
CABINET_UNITS="digipaddock-openai-bridge.service digipaddock-model-keys.socket digipaddock-model-keys.service digipaddock-cabinet.socket digipaddock-cabinet-http.socket digipaddock-cabinet.service digipaddock-cabinet-push.service digipaddock-cabinet-export.service digipaddock-cabinet-export.timer digipaddock-board-sync.path digipaddock-board-grants.path digipaddock-board-deliver.service digipaddock-board-deliver.timer digipaddock-alert-watch.service digipaddock-alert-watch.timer digipaddock-hindsight-status.service digipaddock-hindsight-status.timer"
ENABLED="digipaddock-openai-bridge.service digipaddock-model-keys.socket digipaddock-cabinet.socket digipaddock-cabinet-http.socket digipaddock-cabinet-push.service digipaddock-cabinet-export.timer digipaddock-board-sync.path digipaddock-board-grants.path digipaddock-board-deliver.timer digipaddock-alert-watch.timer digipaddock-hindsight-status.timer"

usage() { echo "usage: cabinet_setup.sh install [--origin https://<host>] | check | disable | grant <user>" >&2; exit 2; }
mode=${1:-}
origin=""
case $mode in
  install)
    case $# in
      1) ;;
      3) { [ "$2" = --origin ] && [[ $3 =~ $ORIGIN_RE ]]; } || usage; origin=$3 ;;
      *) usage ;;
    esac ;;
  check|disable) [ $# -eq 1 ] || usage ;;
  grant) { [ $# -eq 2 ] && [[ $2 =~ ^[a-z][a-z0-9-]{0,31}$ ]]; } || usage ;;
  *) usage ;;
esac
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
# root's cwd (/root 0700) is closed to runuser of the base user, and python3 looks for modules in cwd first
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# the host's names of the commands this installer links on PATH
CABINET_LINK=/usr/local/bin/$(kd_cmd kd-cabinet)
NGINX_SITE=/etc/nginx/sites-available/$(kd_host cabinet_host).conf || { echo "host/host.json is unreadable" >&2; exit 1; }
# Everything install changes is ours: the monitor takes it into its baseline; kd-release runs it too.
if [ "$mode" = install ] && [ -z "${KD_EXPECT_OPEN-}" ]; then
  # shellcheck source=lib/expect.sh
  . "$REPO/common/install/lib/expect.sh"
  kd_expect_run "cabinet_setup.sh $* (REVISION $(cat "$REPO/REVISION" 2>/dev/null || echo '?'))" \
    bash "$REPO/common/install/cabinet_setup.sh" "$@"
  exit $?
fi
base=$(kd_user base)
cabinet=$(kd_user cabinet)
push=$(kd_user push)

problems=()
want() {  # want <path> <owner:group> <mode>: a real directory, file or socket with exactly this owner and mode
  local path=$1 owner=$2 perm=$3
  if [ -L "$path" ] || [ ! -e "$path" ]; then problems+=("$path: missing or a symlink"); return; fi
  local have
  have=$(stat -c '%U:%G %a' "$path")
  [ "$have" = "$owner $perm" ] || problems+=("$path: $have, expected $owner $perm")
}

members() {  # every user that reaches the board, one per line (fails closed on a broken agents.json)
  printf '%s\n' "$base"
  local u
  u=$(kd_user exec)
  if id "$u" >/dev/null 2>&1; then printf '%s\n' "$u"; fi
  # the host reader asks only whose factory operation's journal it is (operation.reader)
  u=$(kd_user observe)
  if id "$u" >/dev/null 2>&1; then printf '%s\n' "$u"; fi
  python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from kd import agents; print("\n".join(agents.users()))' "$REPO/common"
}

in_group() { [[ " $(id -nG "$1") " == *" $GROUP "* ]]; }

sources_sha() {  # one hash over the frontend's sources as the build reads them
  # shellcheck disable=SC2086 # WEB_FILES is a list of names
  (cd "$WEB_SRC" && find $WEB_FILES -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum | sha256sum | cut -d' ' -f1)
}

requirements_sha() { cat "$BUILD_REQUIREMENTS" "$REQUIREMENTS" | sha256sum | cut -d' ' -f1; }

check() {
  local u
  for u in "$cabinet" "$push"; do
    if ! id "$u" >/dev/null 2>&1; then
      problems+=("no user $u")
    else
      [ "$(getent passwd "$u" | cut -d: -f7)" = /usr/sbin/nologin ] || problems+=("$u: shell is not nologin")
    fi
  done
  ! id "$push" >/dev/null 2>&1 || in_group "$push" || problems+=("$push is not in group $GROUP")
  want "$SECRETS" "root:$cabinet" 640
  want "$PUSH_SECRETS" "root:$push" 640
  want /etc/digipaddock/llm root:root 711
  want /etc/digipaddock/model-key-signers root:root 700
  [ "$(readlink /usr/local/bin/$(kd_cmd kd-model-keys) || true)" = "$REPO/common/bin/kd-model-keys" ] \
    || problems+=("kd-model-keys: not a link to the code")
  [ "$(readlink /usr/local/bin/$(kd_cmd kd-signers) || true)" = "$REPO/common/bin/kd-signers" ] \
    || problems+=("kd-signers: not a link to the code")
  if ! getent group "$GROUP" >/dev/null; then
    problems+=("no group $GROUP")
  else
    local list
    list=$(members) || { problems+=("/etc/digipaddock/agents.json is unreadable"); list=$base; }
    while read -r u; do
      [ -n "$u" ] || continue
      if ! id "$u" >/dev/null 2>&1; then problems+=("no user $u"); continue; fi
      in_group "$u" || problems+=("$u is not in group $GROUP")
    done < <(printf '%s\n' "$list" | sort -u)
  fi
  want "$DATA" "$cabinet:$cabinet" 750
  want "$SOCKET" "$cabinet:$GROUP" 660
  want "$HTTP_SOCKET" "$cabinet:www-data" 660
  [ "$(readlink "$CABINET_LINK" || true)" = "$REPO/common/bin/kd-cabinet" ] || problems+=("$CABINET_LINK: not a link to the code")
  # the stores of the owner's decisions
  want "$PROMOTIONS/proposals" "$base:$base" 750
  want "$PROMOTIONS/decisions" "$cabinet:$cabinet" 710
  want "$PROMOTIONS/grants" "$cabinet:$base" 2750
  runuser -u "$cabinet" -- test -r "$PROMOTIONS/proposals" -a -x "$PROMOTIONS/proposals" \
    || problems+=("$cabinet cannot read $PROMOTIONS/proposals")
  ! runuser -u "$cabinet" -- test -w "$PROMOTIONS/proposals" || problems+=("$cabinet can write to $PROMOTIONS/proposals")
  runuser -u "$base" -- test -x "$PROMOTIONS/decisions" || problems+=("$base cannot traverse $PROMOTIONS/decisions")
  ! runuser -u "$base" -- test -r "$PROMOTIONS/decisions" || problems+=("$base can list $PROMOTIONS/decisions")
  ! runuser -u "$base" -- test -w "$PROMOTIONS/decisions" || problems+=("$base can write to $PROMOTIONS/decisions")
  runuser -u "$base" -- test -r "$PROMOTIONS/grants" -a -x "$PROMOTIONS/grants" || problems+=("$base cannot read $PROMOTIONS/grants")
  ! runuser -u "$base" -- test -w "$PROMOTIONS/grants" || problems+=("$base can write to $PROMOTIONS/grants")
  runuser -u "$cabinet" -- test -w "$BOARD_LOG/boss.jsonl" || problems+=("$cabinet cannot write $BOARD_LOG/boss.jsonl")
  ! runuser -u "$cabinet" -- test -w "$BOARD_LOG" || problems+=("$cabinet can write to $BOARD_LOG")
  getfacl -p --omit-header "$BOARD_LOG" 2>/dev/null | grep -qx "default:user:$cabinet:rw-" \
    || problems+=("$BOARD_LOG: no default ACL for $cabinet — it cannot write a recreated boss.jsonl")
  local boss
  boss=$(kd_user agent boss)
  if id "$boss" >/dev/null 2>&1; then
    ! runuser -u "$boss" -- test -w "$PROMOTIONS/proposals" || problems+=("$boss can write to $PROMOTIONS/proposals")
    ! runuser -u "$boss" -- test -x "$PROMOTIONS/decisions" || problems+=("$boss can see $PROMOTIONS/decisions")
    ! runuser -u "$boss" -- test -x "$PROMOTIONS/grants" || problems+=("$boss can see $PROMOTIONS/grants")
  fi
  # the sysadmin's stores, where its module made them (exec_setup.sh checks the rest)
  if [ -d "$GRANTS" ]; then [ "$(stat -c %U "$GRANTS")" = "$cabinet" ] || problems+=("$GRANTS: owner is not $cabinet"); fi
  if [ -d "$PROPOSALS" ]; then [ "$(stat -c %G "$PROPOSALS")" = "$cabinet" ] || problems+=("$PROPOSALS: group is not $cabinet"); fi
  # the web: venv, settings the cabinet itself can load, the built frontend
  [ -x "$VENV/bin/python3" ] || problems+=("no $VENV")
  [ "$(cat "$VENV/.requirements.sha256" 2>/dev/null || true)" = "$(requirements_sha)" ] \
    || problems+=("$VENV: dependencies do not match $REQUIREMENTS")
  want "$SETTINGS" root:root 644
  want "$SECRETS" "root:$cabinet" 640
  runuser -u "$cabinet" -- "$VENV/bin/python3" -I -c \
    'import sys; sys.path.insert(0, sys.argv[1]); from kd.cabinet import config; config.load()' "$REPO/common" \
    >/dev/null 2>&1 || problems+=("$cabinet cannot load $SETTINGS and $SECRETS")
  [ -f "$WEB/dist/index.html" ] || problems+=("no frontend build $WEB/dist")
  [ ! -d "$WEB/dist" ] || [ -z "$(find "$WEB/dist" ! -user root -print -quit)" ] || problems+=("$WEB/dist: not everything is owned by root")
  [ "$(cat "$WEB/.source.sha256" 2>/dev/null || true)" = "$(sources_sha)" ] || problems+=("$WEB/dist: not built from this code")
  local unit code
  for unit in $CABINET_UNITS; do
    kd_render "$REPO/common/systemd/$unit" | cmp -s - "$UNITS/$unit" || problems+=("$UNITS/$unit: differs from the code")
  done
  for unit in $ENABLED; do
    systemctl is-enabled --quiet "$unit" || problems+=("$unit is disabled")
    systemctl is-active --quiet "$unit" || problems+=("$unit is not running")
  done
  for unit in digipaddock-cabinet-proxy.conf digipaddock-cabinet-headers.conf; do
    cmp -s <(kd_render "$REPO/common/nginx/$unit") "/etc/nginx/$unit" || problems+=("/etc/nginx/$unit: differs from the code")
  done
  grep -qsF "root $WEB/dist;" "$NGINX_SITE" || problems+=("nginx: the site does not serve the cabinet ($NGINX_SITE)")
  runuser -u "$base" -- "$CABINET_LINK" cards >/dev/null || problems+=("kd-cabinet cards as $base does not answer")
  code=$(curl -sS --max-time 20 --unix-socket "$HTTP_SOCKET" -o /dev/null -w '%{http_code}' http://localhost/api/health 2>/dev/null || true)
  [ "$code" = 200 ] || problems+=("cabinet web: /api/health on $HTTP_SOCKET — '$code', expected 200")
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'cabinet: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

need_node() {  # Node.js >= NODE_MIN and npm: the frontend is built here
  local version major
  version=$(node --version 2>/dev/null) || { echo "no Node.js — ≥ $NODE_MIN is required (the cabinet's frontend build)" >&2; exit 1; }
  major=${version#v}
  major=${major%%.*}
  { [[ $major =~ ^[0-9]+$ ]] && [ "$major" -ge "$NODE_MIN" ]; } \
    || { echo "Node.js $version — ≥ $NODE_MIN is required (the cabinet's frontend build)" >&2; exit 1; }
  command -v npm >/dev/null || { echo "no npm — it is required with Node.js" >&2; exit 1; }
}

need_settings() {  # the origin: given once, kept in cabinet.json; another one is refused
  [ -e "$SETTINGS" ] || [ -n "$origin" ] \
    || { echo "no $SETTINGS — first install: cabinet_setup.sh install --origin https://<cabinet address>" >&2; exit 1; }
  [ -n "$origin" ] && [ -e "$SETTINGS" ] || return 0
  local have
  have=$(python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("origin", ""))' "$SETTINGS")
  [ "$have" = "$origin" ] || {
    echo "$SETTINGS has origin $have: another origin invalidates the passkeys — change it by hand only" >&2
    exit 1
  }
}

stores() {  # the stores of the owner's decisions, written by the cabinet
  command -v setfacl >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y -q acl
  install -d -o root -g root -m 0755 "$STATE" "$PROMOTIONS"
  # BOSS writes proposals (kd-boss-board propose-*, as the base user); the cabinet reads them.
  install -d -o "$base" -g "$base" -m 0750 "$PROMOTIONS/proposals"
  setfacl -m "u:$cabinet:rx" -m "d:u:$cabinet:r" "$PROMOTIONS/proposals"
  # The decisions are the cabinet's alone; the base user only traverses them: it asks whether one proposal is decided,
  # so a decided one is never reused (kd-boss-board propose-promote); it cannot list, read or write them.
  install -d -o "$cabinet" -g "$cabinet" -m 0700 "$PROMOTIONS/decisions"
  setfacl -m "u:$base:x" "$PROMOTIONS/decisions"
  # Delegation grants: written only by the cabinet (the owner's decision), read by the base user through the setgid
  # group (BOSS's executor checks the file belongs to the cabinet); BOSS's own user never reaches them.
  install -d -o "$cabinet" -g "$base" -m 2750 "$PROMOTIONS/grants"
  # The cabinet appends to the BOSS audit journal (intent and result of the promote) and may not create, replace or
  # remove anything in its directory: the journal is created here, by its owner's rules. The default ACL keeps that
  # right on a journal the base user recreates (kd.boss_exec.Audit creates it 0660: the mask lets rw through).
  install -d -o "$base" -g "$base" -m 0750 "$BOARD_LOG"
  [ -e "$BOARD_LOG/boss.jsonl" ] || install -o "$base" -g "$base" -m 0640 /dev/null "$BOARD_LOG/boss.jsonl"
  setfacl -m "u:$cabinet:rx" -m "d:u:$cabinet:rw" "$BOARD_LOG"
  setfacl -m "u:$cabinet:rw" "$BOARD_LOG/boss.jsonl"
}

venv() {  # the web service's own Python packages (the lock common/cabinet/requirements.txt), root-owned
  dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'install ok installed' \
    || DEBIAN_FRONTEND=noninteractive apt-get install -y -q python3-venv
  install -d -o root -g root -m 0755 "$(dirname "$VENV")"
  [ -x "$VENV/bin/python3" ] || python3 -m venv "$VENV"
  local sha
  sha=$(requirements_sha)
  [ "$(cat "$VENV/.requirements.sha256" 2>/dev/null || true)" != "$sha" ] || return 0
  # every package hash-checked; http-ece ships only as source and builds with the locked setuptools,
  # never with a build backend fetched unchecked for an isolated build
  "$VENV/bin/pip" install --disable-pip-version-check --no-input -q --require-hashes --only-binary=:all: -r "$BUILD_REQUIREMENTS"
  "$VENV/bin/pip" install --disable-pip-version-check --no-input -q --require-hashes --only-binary=:all: --no-binary=http-ece --no-build-isolation -r "$REQUIREMENTS"
  printf '%s\n' "$sha" > "$VENV/.requirements.sha256"
}

settings() {  # cabinet.json (public), cabinet.env (session secret, VAPID public key), push.env (VAPID private key) — each made once
  local tmp keys subject
  if [ ! -e "$SETTINGS" ]; then
    tmp=$(mktemp)
    python3 -I -c 'import json, sys; json.dump({"origin": sys.argv[1], "timezone": "UTC"}, sys.stdout)' \
      "$origin" > "$tmp"
    install -o root -g root -m 0644 "$tmp" "$SETTINGS"
    rm -f "$tmp"
  fi
  # a new secret signs everyone out, a new VAPID key drops every push subscription: never remade
  if [ ! -e "$SECRETS" ]; then
    keys=$("$VENV/bin/python3" -I -c \
      'import sys; sys.path.insert(0, sys.argv[1]); from kd.cabinet.config import generate_vapid; print(*generate_vapid())' \
      "$REPO/common")
    subject=$(python3 -I -c 'import json, sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["origin"])' "$SETTINGS")
    tmp=$(umask 077; mktemp)
    {
      printf 'CABINET_SECRET=%s\n' "$(openssl rand -hex 32)"
      printf 'VAPID_PUBLIC_KEY=%s\n' "${keys#* }"
      printf 'VAPID_SUBJECT=%s\n' "$subject"
    } > "$tmp"
    install -o root -g "$cabinet" -m 0640 "$tmp" "$SECRETS"
    rm -f "$tmp"
    tmp=$(umask 077; mktemp)
    printf 'VAPID_PRIVATE_KEY=%s\nVAPID_SUBJECT=%s\n' "${keys% *}" "$subject" > "$tmp"
    install -o root -g "$push" -m 0640 "$tmp" "$PUSH_SECRETS"
    rm -f "$tmp"
  fi
  # a cabinet.env from before the split holds the private key too: push.env takes it, the pair stays
  if [ ! -e "$PUSH_SECRETS" ]; then
    grep -q '^VAPID_PRIVATE_KEY=' "$SECRETS" || { echo "no VAPID_PRIVATE_KEY in $SECRETS — cannot build push.env" >&2; exit 1; }
    tmp=$(umask 077; mktemp)
    grep -E '^VAPID_(PRIVATE_KEY|SUBJECT)=' "$SECRETS" > "$tmp"
    install -o root -g "$push" -m 0640 "$tmp" "$PUSH_SECRETS"
    rm -f "$tmp"
  fi
  chown root:"$cabinet" "$SECRETS"
  chmod 0640 "$SECRETS"
  chown root:"$push" "$PUSH_SECRETS"
  chmod 0640 "$PUSH_SECRETS"
}

build_web() {  # the frontend: built by the base user (npm runs package code — never root's), served as root's copy
  local src=$WEB/src sha
  sha=$(sources_sha)
  if [ -f "$WEB/dist/index.html" ] && [ "$(cat "$WEB/.source.sha256" 2>/dev/null || true)" = "$sha" ]; then return 0; fi
  install -d -o root -g root -m 0755 "$WEB"
  install -d -o "$base" -g "$base" -m 0755 "$src"
  # the sources anew, node_modules kept (npm ci replaces it anyway, from its cache)
  runuser -u "$base" -- find "$src" -mindepth 1 -maxdepth 1 ! -name node_modules -exec rm -rf {} +
  # shellcheck disable=SC2086 # WEB_FILES is a list of names
  (cd "$WEB_SRC" && tar -cf - $WEB_FILES) | runuser -u "$base" -- tar -xf - -C "$src" --no-same-owner --no-same-permissions
  runuser -u "$base" -- env -i HOME="/home/$base" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
    bash -c 'cd "$1" && npm ci --ignore-scripts --no-audit --no-fund && npm run build' _ "$src"
  rm -rf "$WEB/dist.new" "$WEB/dist.old"
  # dist is the base user's: a link (dist itself or inside it) would point root's chown and chmod at whatever it
  # names (review 04.11.2026). Refused before the copy, and root's own copy is checked again before any change:
  # nothing but regular files and directories, so no swap between the two checks reaches root's commands
  if [ -L "$src/dist" ] || [ ! -d "$src/dist" ]; then
    echo "$src/dist is not a directory of the build — not installing" >&2
    exit 1
  fi
  cp -R --no-dereference "$src/dist" "$WEB/dist.new"
  if [ -L "$WEB/dist.new" ] || [ -n "$(find "$WEB/dist.new" ! -type f ! -type d -print -quit)" ]; then
    rm -rf "$WEB/dist.new"
    echo "the built web (dist/) holds something other than files and folders — not installing" >&2
    exit 1
  fi
  chown -hR root:root "$WEB/dist.new"
  chmod -R u=rwX,go=rX "$WEB/dist.new"
  if [ -d "$WEB/dist" ]; then mv "$WEB/dist" "$WEB/dist.old"; fi
  mv "$WEB/dist.new" "$WEB/dist"
  rm -rf "$WEB/dist.old"
  printf '%s\n' "$sha" > "$WEB/.source.sha256"
}

install_all() {
  id "$base" >/dev/null 2>&1 || { echo "no base user $base — run stage_setup.sh first" >&2; exit 1; }
  local list unit
  list=$(members) || { echo "/etc/digipaddock/agents.json is unreadable — nothing changed" >&2; exit 1; }
  need_node
  need_settings
  local u
  for u in "$cabinet" "$push"; do
    id "$u" >/dev/null 2>&1 || useradd --system --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$u"
  done
  groupadd -f -r "$GROUP"
  usermod -a -G "$GROUP" "$push"
  while read -r u; do
    [ -n "$u" ] || continue
    if id "$u" >/dev/null 2>&1; then
      usermod -a -G "$GROUP" "$u"
    else
      echo "warning: no user $u — not added to $GROUP" >&2
    fi
  done < <(printf '%s\n' "$list" | sort -u)
  install -d -o "$cabinet" -g "$cabinet" -m 0750 "$DATA"
  ln -sfn "$REPO/common/bin/kd-cabinet" "$CABINET_LINK"
  install -d -o root -g root -m 0711 /etc/digipaddock/llm
  install -d -o root -g root -m 0700 /etc/digipaddock/model-key-signers
  ln -sfn "$REPO/common/bin/kd-model-keys" /usr/local/bin/$(kd_cmd kd-model-keys)
  ln -sfn "$REPO/common/bin/kd-signers" /usr/local/bin/$(kd_cmd kd-signers)  # root's signing keys sync
  stores
  venv
  settings
  build_web
  for unit in $CABINET_UNITS; do
    kd_install_rendered "$REPO/common/systemd/$unit" "$UNITS/$unit" 0644
  done
  rm -f "$UNITS/digipaddock-board-grants.service"  # renamed to digipaddock-board-deliver.service
  systemctl daemon-reload
  # shellcheck disable=SC2086 # ENABLED is a list of units
  systemctl enable --now $ENABLED
  # new code and units: a running service restarts with both sockets (they keep the callers waiting), the push worker
  # and the paths re-read theirs
  # the cabinet's approver (review_setup.sh) runs on: the new code's Approve is what the new kd-merge
  # counts;
  # first, so the new cabinet never hands a signature to the old one
  systemctl try-restart digipaddock-model-keys.service
  systemctl try-restart digipaddock-openai-bridge.service
  if systemctl is-active --quiet digipaddock-review.service; then systemctl restart digipaddock-review.service; fi
  if systemctl is-active --quiet digipaddock-cabinet.service; then systemctl restart digipaddock-cabinet.service; fi
  systemctl restart digipaddock-cabinet-push.service digipaddock-board-sync.path digipaddock-board-grants.path
  # nginx last: the web's socket is there when the site starts sending to it
  bash "$REPO/common/install/board_setup.sh"
}

grant() {
  local user=$1
  getent group "$GROUP" >/dev/null || { echo "no group $GROUP — run cabinet_setup.sh install first" >&2; exit 1; }
  id "$user" >/dev/null 2>&1 || { echo "no user $user" >&2; exit 1; }
  usermod -a -G "$GROUP" "$user"
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable)
    if systemctl cat digipaddock-cabinet.socket >/dev/null 2>&1; then
      # shellcheck disable=SC2086 # ENABLED is a list of units
      systemctl disable --now $ENABLED digipaddock-cabinet.service
      systemctl stop digipaddock-model-keys.service
    fi
    ;;
  grant) grant "$2" ;;
esac
