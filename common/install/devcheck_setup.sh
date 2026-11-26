#!/usr/bin/env bash
# dev.check : the user a candidate's tests run as and the places of the checks,
# run as root:
#   devcheck_setup.sh install|check|disable
# install: the ci user (<prefix>-ci, nologin, no supplementary groups: never the cabinet's socket, the agents' group or
#   the journal; no capability channel), the check directories /var/lib/digipaddock/devcheck (root 0755: the
#   runner makes one checkout per check in it, owned by the ci user) with the dependency cache in cache/ (root 0755,
#   sealed entries), the logs /var/log/digipaddock-devcheck (root:<observe> 0750: kd-host-read reads them) and the
#   token's directory /etc/digipaddock/devcheck (root 0700). The token itself (FORGE_TOKEN= of the ci account in
#   Forgejo: commit statuses) is root's to put there, with put_secret.sh. Idempotent; needs the observe user
#   (host.read of an agent, new_agent.sh). The runner (kd-operation-run dev.check) comes with the release tree.
#   The known-vulnerability check (the step vulns, kd-vulnscan): osv-scanner of versions.json, its release binary
#   checked against the pinned sha256, in $OSV_BIN; the OSV database's directory $OSV_DIR (the ci user's: its
#   daily download), the scan's $SCAN_WORK and the seen findings' $STATE_DIR (root's); the units
#   digipaddock-vulnscan.service/.timer, the timer on, and a first run started now — until its database is there,
#   the vulns step of every PR is red.
#   Every unit the ci user runs in starts as root and enters a pid namespace of its own (unshare, then setpriv to the
#   ci user, kd/ci_unit.py): check names a missing tool.
# check: the same state, changing nothing; the token missing is a warning (checks end environment_not_ready until
#   it is there), and so is a database not downloaded yet. Exit 1 with the reasons.
# disable: every check contour goes into maintenance (kd-cabinet contour maintenance): no new check starts, a running
#   one ends. The user, the directories, the logs and the token stay; back — kd-cabinet contour release <name>.
set -euo pipefail
REPO=/opt/digipaddock
WORK=/var/lib/digipaddock/devcheck
LOGS=/var/log/digipaddock-devcheck
TOKEN=/etc/digipaddock/devcheck/digipaddock-ci.env
ETC=$(dirname "$TOKEN")
HOME_DIR=$WORK/home
OSV_BIN=/usr/local/lib/digipaddock/osv-scanner
OSV_DIR=$WORK/osv
SCAN_WORK=$WORK/vulnscan
STATE_DIR=/var/lib/digipaddock/vulnscan
SYSTEMD=/etc/systemd/system
VULNSCAN_UNITS="digipaddock-vulnscan.service digipaddock-vulnscan.timer"
USAGE="usage: devcheck_setup.sh install|check|disable"

mode=${1:-}
case $mode in
  install|check|disable) ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ $# -eq 1 ] || { echo "$USAGE" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
ci=$(kd_user ci)
observe=$(kd_user observe)

owner_mode() { stat -c '%U:%G %a' "$1" 2>/dev/null || true; }
catalog() { python3 -I "$REPO/common/kd/install_catalog.py" version "$1"; }
osv_arch() {
  case $(uname -m) in
    x86_64) echo amd64 ;;
    aarch64) echo arm64 ;;
    *) return 1 ;;
  esac
}
osv_sha() { sha256sum "$OSV_BIN" 2>/dev/null | cut -d' ' -f1 || true; }

problems=()
check() {
  if ! id "$ci" >/dev/null 2>&1; then
    problems+=("no user $ci")
  else
    [ "$(getent passwd "$ci" | cut -d: -f7)" = /usr/sbin/nologin ] || problems+=("$ci: shell is not nologin")
    [ "$(getent passwd "$ci" | cut -d: -f6)" = "$HOME_DIR" ] || problems+=("$ci: home directory is not $HOME_DIR")
    [ "$(id -nG "$ci")" = "$ci" ] || problems+=("$ci: extra groups: $(id -nG "$ci")")
  fi
  [ "$(owner_mode "$WORK")" = "root:root 755" ] || problems+=("$WORK: must be root:root 755")
  [ "$(owner_mode "$WORK/cache")" = "root:root 755" ] || problems+=("$WORK/cache: must be root:root 755")
  [ "$(owner_mode "$LOGS")" = "root:$observe 750" ] || problems+=("$LOGS: must be root:$observe 750")
  [ "$(owner_mode "$ETC")" = "root:root 700" ] || problems+=("$ETC: must be root:root 700")
  # the dependency builds (npm ci, a venv per lock hash) run with these
  command -v npm >/dev/null 2>&1 || problems+=("no npm")
  /usr/bin/python3 -I -c 'import ensurepip, venv' 2>/dev/null || problems+=("no python3 -m venv")
  # every unit of the ci user enters a pid namespace of its own through these (kd/ci_unit.py): without them none starts
  for tool in /usr/bin/unshare /usr/bin/setpriv /usr/bin/env; do
    [ -x "$tool" ] || problems+=("no $tool (util-linux, coreutils)")
  done
  arch=$(osv_arch) || arch=none
  if [ "$arch" = none ]; then
    problems+=("osv-scanner: no release binary for $(uname -m)")
  else
    [ "$(osv_sha)" = "$(catalog "osv_scanner_sha256_$arch")" ] ||
      problems+=("$OSV_BIN: not osv-scanner $(catalog osv_scanner) of versions.json (sha256)")
  fi
  [ "$(owner_mode "$OSV_DIR")" = "$ci:$ci 755" ] || problems+=("$OSV_DIR: must be $ci:$ci 755")
  [ "$(owner_mode "$SCAN_WORK")" = "root:root 755" ] || problems+=("$SCAN_WORK: must be root:root 755")
  [ "$(owner_mode "$STATE_DIR")" = "root:root 700" ] || problems+=("$STATE_DIR: must be root:root 700")
  for unit in $VULNSCAN_UNITS; do
    [ -e "$SYSTEMD/$unit" ] || problems+=("no $SYSTEMD/$unit")
  done
  systemctl is-enabled --quiet digipaddock-vulnscan.timer 2>/dev/null || problems+=("digipaddock-vulnscan.timer is not enabled")
  [ -e "$OSV_DIR/current" ] ||
    echo "devcheck: warning: no OSV database yet — the vulns step is red until digipaddock-vulnscan.service downloads it" >&2
  if [ ! -e "$TOKEN" ]; then
    echo "devcheck: warning: no $TOKEN — checks end in environment_not_ready (put_secret.sh)" >&2
  else
    [ "$(owner_mode "$TOKEN")" = "root:root 600" ] || problems+=("$TOKEN: must be root:root 600")
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'devcheck: %s\n' "${problems[@]}" >&2
    exit 1
  fi
  echo "devcheck: ok"
}

install_all() {
  id "$observe" >/dev/null 2>&1 || {
    echo "no user $observe — first new_agent.sh --install <agent> with host.read" >&2; exit 1; }
  if ! id "$ci" >/dev/null 2>&1; then
    useradd --system --no-create-home --home-dir "$HOME_DIR" --user-group --shell /usr/sbin/nologin "$ci"
  fi
  install -d -o root -g root -m 0755 "$WORK"
  install -d -o root -g root -m 0755 "$WORK/cache"
  install -d -o root -g "$observe" -m 0750 "$LOGS"
  install -d -o root -g root -m 0700 "$ETC"
  install_osv
  install -d -o "$ci" -g "$ci" -m 0755 "$OSV_DIR"
  install -d -o root -g root -m 0755 "$SCAN_WORK"
  install -d -o root -g root -m 0700 "$STATE_DIR"
  for unit in $VULNSCAN_UNITS; do
    kd_install_rendered "$REPO/common/systemd/$unit" "$SYSTEMD/$unit" 0644
  done
  systemctl daemon-reload
  systemctl enable --now digipaddock-vulnscan.timer
  # the first database now, not at the timer's hour
  systemctl start --no-block digipaddock-vulnscan.service
}

install_osv() {
  # the pinned release binary, never one whose sha256 differs from versions.json
  local arch version want tmp
  arch=$(osv_arch) || { echo "osv-scanner: no release binary for $(uname -m)" >&2; exit 1; }
  version=$(catalog osv_scanner)
  want=$(catalog "osv_scanner_sha256_$arch")
  [ "$(osv_sha)" != "$want" ] || return 0
  tmp=$(mktemp)
  if ! curl -fsSL --proto '=https' --retry 3 -o "$tmp" \
      "https://github.com/google/osv-scanner/releases/download/v$version/osv-scanner_linux_$arch"; then
    rm -f "$tmp"
    echo "osv-scanner $version: download failed" >&2
    exit 1
  fi
  if [ "$(sha256sum "$tmp" | cut -d' ' -f1)" != "$want" ]; then
    rm -f "$tmp"
    echo "osv-scanner $version: the download's sha256 is not the one of versions.json" >&2
    exit 1
  fi
  install -d -o root -g root -m 0755 "$(dirname "$OSV_BIN")"
  install -o root -g root -m 0755 "$tmp" "$OSV_BIN"
  rm -f "$tmp"
}

disable_all() {
  # an assignment, not the loop's list: with pipefail a cabinet that does not answer stops the script here
  local names name
  names=$(kd-cabinet contour list | cut -d' ' -f1)
  for name in $names; do
    kd-cabinet contour maintenance "$name" --reason "devcheck_setup.sh disable"
  done
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable)
    disable_all
    ;;
esac
