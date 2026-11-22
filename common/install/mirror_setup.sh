#!/usr/bin/env bash
# GitHub mirror of the public code repository, root only:
#   mirror_setup.sh install|check|disable
# install: the key directory /etc/digipaddock/mirror (the owner puts the GitHub deploy key with write access,
#   github_deploy, there beforehand — no key, exit 3), the state directory, /usr/local/bin/kd-mirror, the units
#   and the timer on. Idempotent; a disabled mirror comes back on.
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: the disabled flag (kd-mirror pushes nothing, even started by kd-release) and the timer off; the key,
#   the bare mirror and state.json stay.
set -euo pipefail
REPO=/opt/digipaddock
KEYS=/etc/digipaddock/mirror
KEY=$KEYS/github_deploy
STATE=/var/lib/digipaddock/github-mirror
UNITS=/etc/systemd/system

mode=${1:-}
case $mode in
  install|check|disable) [ $# -eq 1 ] || { echo "usage: mirror_setup.sh install|check|disable" >&2; exit 2; } ;;
  *) echo "usage: mirror_setup.sh install|check|disable" >&2; exit 2 ;;
esac
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# the host's names of the commands this installer links on PATH
MIRROR_LINK=/usr/local/bin/$(kd_cmd kd-mirror)

problems=()
want() {  # want <path> <owner:group> <mode>: a real directory or file with exactly this owner and mode
  local path=$1 owner=$2 perm=$3
  if [ -L "$path" ] || [ ! -e "$path" ]; then problems+=("$path: missing or a symlink"); return; fi
  local have
  have=$(stat -c '%U:%G %a' "$path")
  [ "$have" = "$owner $perm" ] || problems+=("$path: $have, must be $owner $perm")
}

check() {
  want "$KEYS" root:root 700
  want "$KEY" root:root 600
  want "$STATE" root:root 700
  [ -s "$REPO/common/install/github_known_hosts" ] || problems+=("no github_known_hosts in the code")
  [ "$(readlink "$MIRROR_LINK" || true)" = "$REPO/common/bin/kd-mirror" ] || problems+=("$MIRROR_LINK: not a link to the code")
  if [ -e "$STATE/state.json" ]; then
    python3 -I -c 'import json, sys; d = json.load(open(sys.argv[1])); assert isinstance(d, dict)' "$STATE/state.json" 2>/dev/null \
      || problems+=("$STATE/state.json is not readable")
  fi
  [ ! -e "$KEYS/disabled" ] || problems+=("the mirror is disabled ($KEYS/disabled)")
  local unit
  for unit in digipaddock-mirror.service digipaddock-mirror.timer; do
    kd_render "$REPO/common/systemd/$unit" | cmp -s - "$UNITS/$unit" || problems+=("$UNITS/$unit: differs from the code")
  done
  systemctl is-enabled --quiet digipaddock-mirror.timer || problems+=("digipaddock-mirror.timer is disabled")
  systemctl is-active --quiet digipaddock-mirror.timer || problems+=("digipaddock-mirror.timer is not running")
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'mirror: %s\n' "${problems[@]}" >&2
    exit 1
  fi
  echo "mirror: ready"
}

install_all() {
  install -d -o root -g root -m 0700 "$KEYS"
  if [ ! -f "$KEY" ] || [ -L "$KEY" ]; then
    echo "no $KEY. Owner's step: an ed25519 key with write access — GitHub → the mirror repository → Settings → Deploy keys → Add (Allow write access); put the private part in $KEY (root, 0600) and run install again" >&2
    exit 3
  fi
  chown root:root "$KEY"
  chmod 0600 "$KEY"
  install -d -o root -g root -m 0700 "$STATE"
  rm -f "$KEYS/disabled"
  ln -sfn "$REPO/common/bin/kd-mirror" "$MIRROR_LINK"
  kd_install_rendered "$REPO/common/systemd/digipaddock-mirror.service" "$UNITS/digipaddock-mirror.service" 0644
  kd_install_rendered "$REPO/common/systemd/digipaddock-mirror.timer" "$UNITS/digipaddock-mirror.timer" 0644
  systemctl daemon-reload
  systemctl enable --now digipaddock-mirror.timer
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable)
    install -d -o root -g root -m 0700 "$KEYS"
    touch "$KEYS/disabled"
    if systemctl cat digipaddock-mirror.timer >/dev/null 2>&1; then systemctl disable --now digipaddock-mirror.timer; fi
    echo "mirror: disabled (the key, the mirror and state.json stay; to restore — mirror_setup.sh install)"
    ;;
esac
