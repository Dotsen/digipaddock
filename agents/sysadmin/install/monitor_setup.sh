#!/usr/bin/env bash
# Module setup of capability monitor.manage (spec 5.4, M02), run as root by new_agent.sh:
#   monitor_setup.sh install|check|disable <agent> <user>
# install: the monitor state owned by the agent user; the kd-monitor units (the tick every 5 minutes) and the
#   kd-monitor-check units (the cabinet's "check now" every 15 s), each with a drop-in that runs it as
#   the agent user (KD_AGENT: whom it wakes); both timers on. The timers are stopped while the state changes owner, so
# no tick writes into a half-moved state; a running tick is let finish (killed, it would page). Idempotent.
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: the monitor stops; its state stays.
set -euo pipefail
REPO=/opt/digipaddock
SYS=$REPO/agents/sysadmin
STATE=/var/lib/digipaddock/monitor
UNITS=/etc/systemd/system
DROPIN=/etc/systemd/system/digipaddock-monitor.service.d/user.conf
CHECK_DROPIN=/etc/systemd/system/digipaddock-monitor-check.service.d/user.conf
UNIT_FILES="digipaddock-monitor.service digipaddock-monitor.timer digipaddock-monitor-check.service digipaddock-monitor-check.timer"

mode=${1:-} agent=${2:-} user=${3:-}
case $mode in
  install|check|disable) ;;
  *) echo "usage: monitor_setup.sh install|check|disable <agent> <user>" >&2; exit 2 ;;
esac
[[ $agent =~ ^[a-z][a-z0-9-]{0,31}$ && $user =~ ^[a-z][a-z0-9-]{0,31}$ ]] \
  || { echo "usage: monitor_setup.sh install|check|disable <agent> <user>" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
# Units carry naming tokens (@base@): installed and compared rendered.
# shellcheck source=../../../common/install/lib/naming.sh
. "$REPO/common/install/lib/naming.sh"

problems=()
want() {  # want <path> <owner:group> <mode>: a real directory with exactly this owner and mode
  local path=$1 owner=$2 perm=$3
  if [ -L "$path" ] || [ ! -d "$path" ]; then problems+=("$path: missing or a symlink"); return; fi
  local have
  have=$(stat -c '%U:%G %a' "$path")
  [ "$have" = "$owner $perm" ] || problems+=("$path: $have, want $owner $perm")
}

want_dropin() {  # want_dropin <file>: the unit runs as the agent user and wakes the agent
  local file=$1
  [ -f "$file" ] && grep -qx "User=$user" "$file" || problems+=("$file: no User=$user")
  [ -f "$file" ] && grep -qx "Environment=KD_AGENT=$agent" "$file" \
    || problems+=("$file: no Environment=KD_AGENT=$agent")
}

check() {
  want "$STATE" "$user:$user" 750
  local stray
  stray=$(find "$STATE" -xdev ! -user "$user" -print -quit 2>/dev/null || true)
  [ -z "$stray" ] || problems+=("$stray: owner is not $user")
  want_dropin "$DROPIN"
  want_dropin "$CHECK_DROPIN"
  for unit in $UNIT_FILES; do
    kd_render "$SYS/systemd/$unit" | cmp -s - "$UNITS/$unit" || problems+=("$UNITS/$unit differs from the repository")
  done
  for timer in digipaddock-monitor.timer digipaddock-monitor-check.timer; do
    systemctl is-enabled --quiet "$timer" || problems+=("$timer is not enabled")
    systemctl is-active --quiet "$timer" || problems+=("$timer is not active")
  done
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'monitor.manage: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

write_dropin() {  # write_dropin <file>: atomically, root-owned
  local file=$1 tmp
  install -d -o root -g root -m 0755 "$(dirname "$file")"
  tmp=$(mktemp "$(dirname "$file")/.user.XXXXXX")
  printf "[Service]\nUser=%s\nGroup=%s\nEnvironment=KD_AGENT=%s\n" "$user" "$user" "$agent" > "$tmp"
  chmod 0644 "$tmp"
  mv -f "$tmp" "$file"
}

wait_idle() {  # a running tick finishes — stopped mid-run it is a failed unit and a kd-alert page; no longer
  # than the units' TimeoutStartSec (620 s), after which the stop in install_all ends it as before
  local unit state i
  for unit in digipaddock-monitor.service digipaddock-monitor-check.service; do
    for ((i = 0; i < 620; i++)); do
      state=$(systemctl show -p ActiveState --value "$unit" 2>/dev/null || true)
      case $state in
        activating|active|deactivating|reloading) sleep 1 ;;
        *) break ;;
      esac
    done
  done
}

install_all() {
  [ ! -L "$STATE" ] || { echo "$STATE is a symlink" >&2; exit 1; }
  # No tick while the state changes owner; the timers come back at the end. A running tick is let finish.
  systemctl stop digipaddock-monitor.timer digipaddock-monitor-check.timer 2>/dev/null || true
  wait_idle
  systemctl stop digipaddock-monitor.service digipaddock-monitor-check.service 2>/dev/null || true
  install -d -o root -g root -m 0755 /var/lib/digipaddock
  install -d -o "$user" -g "$user" -m 0750 "$STATE"
  # The legacy owner (the base user) hands the files over; -h changes a link itself, never what it points to.
  chown -hR "$user:$user" "$STATE"
  for unit in $UNIT_FILES; do
    kd_install_rendered "$SYS/systemd/$unit" "$UNITS/$unit" 0644
  done
  write_dropin "$DROPIN"
  write_dropin "$CHECK_DROPIN"
  systemctl daemon-reload
  systemctl enable --now digipaddock-monitor.timer digipaddock-monitor-check.timer
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable)
    systemctl disable --now digipaddock-monitor.timer digipaddock-monitor-check.timer digipaddock-monitor.service digipaddock-monitor-check.service \
      2>/dev/null || true
    ;;
esac
