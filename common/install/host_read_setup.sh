#!/usr/bin/env bash
# Module setup of capability host.read, run as root by new_agent.sh:
#   host_read_setup.sh install|check|disable <agent> <user>
#   host_read_setup.sh off|on <agent>          (root, by hand: the owner's switch, spec 7)
# install: the host reader (<prefix>-observe, created by new_agent.sh as the capability's executor user) joins
#   systemd-journal and gets read ACLs on the catalog's platform directories and on the cabinet's nginx logs
#   (/var/log/nginx/board, a default ACL: rotated and new files keep it); no agent user, nor the base user, is in
#   adm or systemd-journal (removed if found). The agent's own groups, files and sandbox are not touched. Idempotent;
#   an agent switched off stays off.
# check: the same state, changing nothing, plus what the host reader must never read; exit 1 with the reasons.
# disable: the lifecycle step (new_agent.sh revokes before every install, and on --disable): the agent's running
#   host calls stop; its channel grant is removed by new_agent.sh itself. It writes no switch: otherwise every
# reinstall would turn the reads off (as happened on the factory host).
# off: the owner's switch (spec 7) — the agent goes into /etc/digipaddock/host-read.json "disabled", which the
#   executor reads on every call, and its running host calls stop; nothing is held by the agent's own processes,
#   so its sessions need no restart; install never lifts it. on: lift the switch.
# install also makes the private permission audit's directory (the base user's, 0700) and its rotation: every agent's
# PermissionRequest hook writes there through the base channel.
set -euo pipefail
REPO=/opt/digipaddock
STATE=/etc/digipaddock/host-read.json
LIB=/var/lib/digipaddock
NGINX_LOGS=/var/log/nginx/board
# The private permission audit (kd-permlog through the base channel): the base user writes, root reads, no agent.
AUDIT_DIR=/var/log/digipaddock-permission
AUDIT_ROTATE=/etc/logrotate.d/digipaddock-permission
# The catalog's directories under $LIB (common/coordination/host-read.json); granted through acl_walk.py, which
# opens every entry by descriptor and never follows a link (admin-proposals is an agent's tree).
GRANT_DIRS=(admin-proposals attention boss-promotions/grants boss-promotions/proposals release)
# Never readable by the host reader: signatures, secrets, other agents' state, mixed and audit logs; check adds the
# homes of every agent and of the exec, forge and repo users (logins and keys).
NEVER=("$LIB/admin-grants" "$LIB/grants" "$LIB/cabinet" "$LIB/monitor" "$LIB/mirror" "$LIB/worker" "$LIB/review"
  "$LIB/signers" "$LIB/boss-promotions/decisions" /var/log/syslog /var/log/auth.log /var/log/digipaddock-permission
  /etc/hindsight /etc/digipaddock/push.env /etc/digipaddock/cabinet.env /etc/digipaddock/pult.env
  /etc/digipaddock/claude.env /etc/digipaddock/llm /etc/digipaddock/review)

usage() { echo "usage: host_read_setup.sh install|check|disable <agent> <user> | off|on <agent>" >&2; exit 2; }
mode=${1:-} agent=${2:-} user=${3:-}
case $mode in
  install|check|disable) [[ $user =~ ^[a-z][a-z0-9-]{0,31}$ ]] || usage ;;
  off|on) [ -z "$user" ] || usage ;;
  *) usage ;;
esac
[[ $agent =~ ^[a-z][a-z0-9-]{0,31}$ ]] || usage
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
observe=$(kd_user observe)
base=$(kd_user base)

state() {  # state add|remove <agent>: edit the disabled list atomically, root:root 0644
  /usr/bin/python3 -I - "$STATE" "$1" "$2" <<'PY'
import json, os, sys
path, op, agent = sys.argv[1:]
try:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    data = {"disabled": []}
disabled = set(data.get("disabled") or [])
(disabled.add if op == "add" else disabled.discard)(agent)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump({"disabled": sorted(disabled)}, f)
    f.write("\n")
os.chown(tmp, 0, 0)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
PY
}

platform_users() {  # the base user, every agent user of the host and the one being installed
  echo "$base"
  # kd.agents: no agents.json is no agent yet (a new host's first install), a broken one fails
  /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from kd import agents; print("\n".join(agents.users()))' "$REPO/common"
  [ -z "$user" ] || echo "$user"
}

in_group() { [[ " $(id -nG "$1" 2>/dev/null) " == *" $2 "* ]]; }

# a failed read of agents.json stops the script here (set -e, pipefail), never a check over the base user alone
users=$(platform_users | sort -u)

problems=()
check() {
  id "$observe" >/dev/null 2>&1 || problems+=("no user $observe")
  in_group "$observe" systemd-journal || problems+=("$observe is not in group systemd-journal")
  ! in_group "$observe" adm || problems+=("$observe is in group adm")
  while read -r u; do
    for g in adm systemd-journal; do ! in_group "$u" "$g" || problems+=("$u is in group $g"); done
  done <<< "$users"
  local never=("${NEVER[@]}") u h
  for u in $users "$(kd_user exec)" "$(kd_user forge)" "$(kd_user repo)"; do
    h=$(getent passwd "$u" | cut -d: -f6) || true
    case $h in /|/nonexistent|'') ;; *) never+=("$h") ;; esac
  done
  for d in "${GRANT_DIRS[@]}"; do
    [ ! -d "$LIB/$d" ] || runuser -u "$observe" -- test -r "$LIB/$d" -a -x "$LIB/$d" || problems+=("$observe cannot read $LIB/$d")
  done
  [ -d "$NGINX_LOGS" ] || problems+=("no $NGINX_LOGS")
  getfacl -cpd "$NGINX_LOGS" 2>/dev/null | grep -qx "user:$observe:r--" || problems+=("$NGINX_LOGS: no default ACL for $observe")
  # the files, not the listing: a 0755 directory (/etc/hindsight, a home) may be listed, its secrets never read;
  # root lists them (two levels: ~/.ssh/<key>, ~/.claude/.credentials.json), the reader tests them in one call
  local readable
  readable=$(for p in "${never[@]}"; do
      if [ -d "$p" ]; then find "$p" -maxdepth 2 -type f -print0 2>/dev/null; elif [ -e "$p" ]; then printf '%s\0' "$p"; fi
    done | runuser -u "$observe" -- /usr/bin/python3 -I -c \
      'import os,sys; [print(p) for p in sys.stdin.read().split("\0") if p and os.access(p, os.R_OK)]')
  while read -r p; do [ -z "$p" ] || problems+=("$observe can read $p"); done <<< "$readable"
  [ "$(stat -c '%U:%G %a' "$AUDIT_DIR" 2>/dev/null)" = "$base:$base 700" ] || problems+=("$AUDIT_DIR: not $base:$base 700")
  kd_render "$REPO/common/logrotate/digipaddock-permission" | cmp -s - "$AUDIT_ROTATE" || problems+=("$AUDIT_ROTATE: differs from the code")
  if [ -e "$STATE" ]; then
    [ "$(stat -c '%U:%G %a' "$STATE")" = "root:root 644" ] || problems+=("$STATE: not root:root 644")
    /usr/bin/python3 -I -c 'import json,sys; d=json.load(open(sys.argv[1])); assert isinstance(d["disabled"], list)' "$STATE" \
      2>/dev/null || problems+=("$STATE: not JSON with a disabled list")
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'host.read: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

case $mode in
  install)
    usermod -aG systemd-journal "$observe"
    gpasswd -d "$observe" adm >/dev/null 2>&1 || true
    while read -r u; do
      for g in adm systemd-journal; do
        if in_group "$u" "$g"; then gpasswd -d "$u" "$g" >/dev/null; echo "host.read: $u removed from group $g"; fi
      done
    done <<< "$users"
    for d in "${GRANT_DIRS[@]}"; do
      [ -d "$LIB/$d" ] || continue
      /usr/bin/python3 -I "$REPO/common/kd/acl_walk.py" "$LIB/$d" "$observe"
    done
    install -d -o root -g adm -m 0755 "$NGINX_LOGS"
    setfacl -d -m "u:$observe:r" "$NGINX_LOGS"
    find "$NGINX_LOGS" -maxdepth 1 -type f -exec setfacl -m "u:$observe:r" {} +
    install -d -o "$base" -g "$base" -m 0700 "$AUDIT_DIR"
    kd_install_rendered "$REPO/common/logrotate/digipaddock-permission" "$AUDIT_ROTATE" 0644
    [ -e "$STATE" ] || state remove "$agent"
    check
    if /usr/bin/python3 -I -c 'import json,sys; sys.exit(sys.argv[2] not in json.load(open(sys.argv[1]))["disabled"])' \
        "$STATE" "$agent"; then
      echo "host.read: $agent stays off (to turn on: host_read_setup.sh on $agent)"
    else
      echo "host.read: $agent reads the host through $observe"
    fi
    ;;
  check) check ;;
  disable)
    # exact prefix: the agent's own host channel calls only
    systemctl stop "digipaddock-cap-$agent-host@*.service" 2>/dev/null || true
    ;;
  off)
    state add "$agent"
    systemctl stop "digipaddock-cap-$agent-host@*.service" 2>/dev/null || true
    echo "host.read: host reads of $agent are off (to turn on: host_read_setup.sh on $agent)"
    ;;
  on)
    state remove "$agent"
    echo "host.read: host reads of $agent are on"
    ;;
esac
