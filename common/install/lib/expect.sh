# Rollout window of the sysadmin monitor: what a root installer changes on this host while it runs is taken
# into the monitor's security baseline (kd-monitor expect begin|end) instead of raising sec:* alerts; changes made
# outside the window still alert. Source it from an entry script and re-run the script inside the window:
#   kd_expect_run "<label>" bash "$0" "$@"; exit $?
# Inside an open window (KD_EXPECT_OPEN=1, nested installers) and for non-root it just runs the command. The window
# never fails an install: no monitor, no sysadmin user or no snapshot — a warning, and the command runs as usual.
if ! declare -F kd_user >/dev/null; then
  # shellcheck source=naming.sh
  . "$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null && pwd -P)/naming.sh"
fi

kd_expect_monitor() {  # begin|end <label>: kd-monitor under the sysadmin's own user, with its home and nothing else
  local user home monitor
  monitor=/usr/local/bin/$(kd_cmd kd-monitor) || return 1  # the host's name of the command
  [ -x "$monitor" ] || return 1
  user=$(kd_user agent sysadmin 2>/dev/null) || return 1
  home=$(getent passwd "$user" | cut -d: -f6) || return 1
  [ -n "$home" ] || return 1
  runuser -u "$user" -- env -i PATH=/usr/local/bin:/usr/bin:/bin HOME="$home" LANG=C.UTF-8 \
    "$monitor" expect "$1" --label "$2"
}

kd_expect_run() {  # <label> <command…>
  local label=$1 rc=0 opened=0
  shift
  if [ -n "${KD_EXPECT_OPEN-}" ] || [ "$(id -u)" != 0 ]; then
    "$@" || rc=$?
    return $rc
  fi
  if kd_expect_monitor begin "$label"; then
    opened=1
  else
    echo "warning: the monitor's rollout window did not open — this install's changes will raise sec alerts" >&2
  fi
  KD_EXPECT_OPEN=1 "$@" || rc=$?
  if [ $opened = 1 ] && ! kd_expect_monitor end "$label"; then
    echo "warning: the monitor's rollout window did not close — it expires by itself, the changes will raise sec alerts" >&2
  fi
  return $rc
}
