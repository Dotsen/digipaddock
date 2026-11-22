#!/usr/bin/env bash
# session_host_setup.sh: the session host and which agents it runs; root only.
#   install          the host's units (rendered: the socket's group is the cabinet user's), the config
#                    /etc/digipaddock/session-host.json with no agent when it is missing (root 0644), the socket
#                    enabled and started; then check
#   check            the same state, changing nothing; exit 1 with the reasons
#   disable          the socket and the host stop; the listed agents stay listed and their commands wait in the cabinet
#   enable <agent> [--slots N]
#                    the agent runs on the host: generated runtime and a Claude login required — the host's token
# (the catalog secret claude-token) or the user's own; it is listed with one slot (or N);
# an agent already listed with --slots only gets its number of slots changed
#   release <agent>  off the session host: unlisted first (the cabinet cancels its queued commands), its hosted
# sessions stop; the agent does not run until enable (the host is the only runtime)
# A worker job's, a login's and a hosted session's claude run as the same user; the host's sessions are units of
# digipaddock-sessions.slice (stopping the slice stops them all).
set -euo pipefail
REPO=/opt/digipaddock
CONFIG=/etc/digipaddock/session-host.json
UNITS=/etc/systemd/system
HOST_UNITS="digipaddock-session-host.socket digipaddock-session-host.service"
SOCKET=/run/digipaddock-session-host/host.sock
# the host's Claude token (kd.claude_token) — systemd reads it as root for every session unit
TOKEN=/etc/digipaddock/claude.env
USAGE="usage: session_host_setup.sh install | check | disable | enable <agent> [--slots N] | release <agent>"

mode=${1-} agent=${2-} slots=""
case $mode in
  install | check | disable) [ $# -eq 1 ] || { echo "$USAGE" >&2; exit 2; } ;;
  enable)
    [[ ($# -eq 2 || ($# -eq 4 && $3 == --slots && $4 =~ ^[1-9]$)) && $agent =~ ^[a-z][a-z0-9_-]{0,31}$ ]]       || { echo "$USAGE" >&2; exit 2; }
    slots=${4-}
    ;;
  release) [[ $# -eq 2 && $agent =~ ^[a-z][a-z0-9_-]{0,31}$ ]] || { echo "$USAGE" >&2; exit 2; } ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# the host's names of the commands this installer links on PATH
SESSION_HOST_LINK=/usr/local/bin/$(kd_cmd kd-session-host)

token_state() {  # "" — the host's token is as kd-secret puts it; "missing"; else why not (the value never printed)
  if [ ! -e "$TOKEN" ] && [ ! -L "$TOKEN" ]; then echo missing; return 0; fi
  if [ ! -f "$TOKEN" ] || [ -L "$TOKEN" ] || [ "$(stat -c %u:%a "$TOKEN")" != "0:600" ]; then
    echo "not a regular root file with mode 600"
    return 0
  fi
  # one CLAUDE_CODE_OAUTH_TOKEN=<bare value> line: systemd and the worker runner read it alike
  /usr/bin/python3 -I "$REPO/common/kd/claude_token.py" check "$TOKEN" || true
}

config() {  # init | add <agent> | remove <agent> | list — kd.session_config, never a broken file
  /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv.pop(1)); from kd.session_config import main; sys.exit(main(sys.argv[1:]))' \
    "$REPO/common" "$@"
}

problems=()
check() {
  local unit
  for unit in $HOST_UNITS; do
    [ -f "$UNITS/$unit" ] || problems+=("$unit is not installed")
  done
  systemctl is-enabled --quiet digipaddock-session-host.socket || problems+=("digipaddock-session-host.socket is disabled")
  systemctl is-active --quiet digipaddock-session-host.socket || problems+=("digipaddock-session-host.socket is not running")
  if [ -S "$SOCKET" ]; then
    local have
    have=$(stat -c '%U:%G %a' "$SOCKET")
    [ "$have" = "root:$(kd_user cabinet) 660" ] || problems+=("$SOCKET: $have, expected root:$(kd_user cabinet) 660")
  else
    problems+=("$SOCKET: no socket")
  fi
  if [ -f "$CONFIG" ] && [ ! -L "$CONFIG" ]; then
    [ "$(stat -c '%U:%G %a' "$CONFIG")" = "root:root 644" ] || problems+=("$CONFIG: not root:root 644")
    config list >/dev/null || problems+=("$CONFIG is unreadable")
  else
    problems+=("$CONFIG: missing or a symlink")
  fi
  local token
  token=$(token_state)  # missing is fine: the agents' own logins
  case $token in "" | missing) ;; *) problems+=("$TOKEN: $token — put it again: $(kd_cmd kd-secret) put claude-token") ;; esac
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'session host: %s\n' "${problems[@]}" >&2
    return 1
  fi
  echo "session host: ok ($(config list | tr '\n' ' '))"
}

in_sessions() {  # the process (its /proc/<pid>/cgroup) runs in the host's slice
  grep -q '/digipaddock-sessions\.slice/' "$1" 2>/dev/null
}

stop_sessions() {  # the agent's claude in the host's slice; its other claude (a login) is not a session
  local user pid pids=() i
  user=$(kd_user agent "$agent")
  for pid in $(pgrep -u "$user" -x claude || true); do
    in_sessions "/proc/$pid/cgroup" && pids+=("$pid")
  done
  [ ${#pids[@]} -gt 0 ] || return 0
  kill -TERM "${pids[@]}" 2>/dev/null || true
  for i in $(seq 1 20); do
    kill -0 "${pids[@]}" 2>/dev/null || return 0
    sleep 0.5
  done
  kill -KILL "${pids[@]}" 2>/dev/null || true
}

case $mode in
  install)
    for unit in $HOST_UNITS; do
      kd_install_rendered "$REPO/common/systemd/$unit" "$UNITS/$unit" 0644
    done
    install -d -o root -g root -m 0755 /etc/digipaddock
    install -d -o root -g root -m 0711 /etc/digipaddock/llm  # provider keys (kd-secret put llm-<kind>); the cabinet sees names only
    ln -sfn "$REPO/common/bin/kd-session-host" "$SESSION_HOST_LINK"  # root's status|resume <agent>
    config init
    systemctl daemon-reload
    systemctl enable --now digipaddock-session-host.socket
    if systemctl is-active --quiet digipaddock-session-host.service; then systemctl restart digipaddock-session-host.service; fi
    check
    ;;
  check)
    check
    ;;
  disable)
    systemctl disable --now digipaddock-session-host.socket digipaddock-session-host.service || true
    ;;
  enable)
    if [ -n "$slots" ] && [ -f "$CONFIG" ] && config list | grep -q "^$agent "; then
      config add "$agent" "$slots"  # already in the host: only its number of slots changes
      echo "session host: $agent — slots: $slots"
      exit 0
    fi
    [ "$(/usr/bin/python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from kd import agents; print(agents.runtime_of(sys.argv[2]))' "$REPO/common" "$agent")" = generated ] \
      || { echo "session host: $agent is not on the generated runtime" >&2; exit 1; }
    user=$(kd_user agent "$agent")
    home=$(getent passwd "$user" | cut -d: -f6)
    [ -n "$home" ] || { echo "session host: $agent has no user $user" >&2; exit 1; }
    token=$(token_state)
    if [ "$token" = missing ]; then  # no host token: the user's own login
      [ -f "$home/.claude/.credentials.json" ] \
        || { echo "session host: $agent has no Claude login — put the host's token: $(kd_cmd kd-secret) put claude-token" >&2; exit 1; }
    elif [ -n "$token" ]; then  # a broken one would override that login in every session
      echo "session host: $TOKEN: $token — put it again: $(kd_cmd kd-secret) put claude-token" >&2
      exit 1
    fi
    [ -f "$CONFIG" ] || { echo "session host: run session_host_setup.sh install first" >&2; exit 1; }
    config add "$agent" ${slots:+"$slots"}
    echo "session host: $agent — in the cabinet's sessions"
    ;;
  release)
    config remove "$agent"
    sleep 2  # the cabinet and the host re-read the config (by its mtime) before the sessions go
    stop_sessions
    echo "session host: $agent released from the session host — it does not run until enable"
    ;;
esac
