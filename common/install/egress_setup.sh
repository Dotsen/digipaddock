#!/usr/bin/env bash
# Probe exits (the agent autonomy design, piece 2; docs/probe-egress.md), run as root:
#   egress_setup.sh install|check|disable
# install: tinyproxy from the distribution with its own unit masked first (it never starts), the configs of
#   host/egress.json rendered into /etc/digipaddock/egress (root 0755, files 0644), the logs
#   /var/log/digipaddock-egress (root 0755; each <exit>.log made first, tinyproxy:<observe> 0640 — tinyproxy itself
#   creates 0600 — so kd-host-read reads it, and tinyproxy can plant no link in a root directory), one
#   digipaddock-egress@<exit>.service per exit enabled and restarted; an exit no longer in the file is stopped and
#   disabled. No host/egress.json — no exits: every instance stops. Idempotent.
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: every exit stops and is disabled (the kill switch of all exits; one exit — systemctl stop
#   digipaddock-egress@<exit>). The package, configs and logs stay.
set -euo pipefail
REPO=/opt/digipaddock
ETC=/etc/digipaddock/egress
LOGS=/var/log/digipaddock-egress
UNIT=digipaddock-egress@.service
SYSTEMD=/etc/systemd/system
USAGE="usage: egress_setup.sh install|check|disable"

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
observe=$(kd_user observe)

egress_py() { /usr/bin/python3 -I -c 'import sys; sys.path.insert(0, "/opt/digipaddock/common"); from kd import egress; sys.exit(egress.main(sys.argv[1:]))' "$@"; }
owner_mode() { stat -c '%U:%G %a' "$1" 2>/dev/null || true; }
# "<exit> <port>" per line of host/egress.json; an assignment first, so a broken file stops the script (pipefail)
exits_now() { egress_py list; }
running() { systemctl list-units --all --plain --no-legend 'digipaddock-egress@*.service' | awk '{print $1}' \
  | sed -n 's/^digipaddock-egress@\(.*\)\.service$/\1/p'; }

problems=()
check() {
  local listed name port fresh
  command -v tinyproxy >/dev/null 2>&1 || problems+=("no tinyproxy")
  [ "$(systemctl is-enabled tinyproxy.service 2>/dev/null || true)" = masked ] \
    || problems+=("tinyproxy.service must be masked (the distribution's own proxy never runs)")
  [ "$(owner_mode "$ETC")" = "root:root 755" ] || problems+=("$ETC: must be root:root 755")
  [ "$(owner_mode "$LOGS")" = "root:root 755" ] || problems+=("$LOGS: must be root:root 755")
  cmp -s "$REPO/common/systemd/$UNIT" "$SYSTEMD/$UNIT" || problems+=("$SYSTEMD/$UNIT differs from the release's")
  listed=$(exits_now) || { problems+=("host/egress.json is broken"); listed=""; }
  fresh=$(mktemp -d)
  egress_py render --out "$fresh" --log-dir "$LOGS" >/dev/null 2>&1 || true
  while read -r name port; do
    [ -n "$name" ] || continue
    for suffix in conf filter; do
      cmp -s "$fresh/$name.$suffix" "$ETC/$name.$suffix" || problems+=("$ETC/$name.$suffix is not the current render")
    done
    [ "$(owner_mode "$LOGS/$name.log")" = "tinyproxy:$observe 640" ]       || problems+=("$LOGS/$name.log: must be tinyproxy:$observe 640")
    systemctl is-active --quiet "digipaddock-egress@$name.service" || problems+=("digipaddock-egress@$name is not active")
    ss -ltnH "sport = :$port" | grep -q '127.0.0.1:' || problems+=("exit $name does not listen on 127.0.0.1:$port")
  done <<<"$listed"
  rm -rf "$fresh"
  for name in $(running); do
    grep -q "^$name " <<<"$listed" || problems+=("digipaddock-egress@$name runs but is not in host/egress.json")
  done
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'egress: %s\n' "${problems[@]}" >&2
    exit 1
  fi
  echo "egress: ok"
}

install_all() {
  local listed name port log
  id "$observe" >/dev/null 2>&1 || {
    echo "no user $observe — first new_agent.sh --install <agent> with host.read" >&2; exit 1; }
  listed=$(exits_now)
  if ! command -v tinyproxy >/dev/null 2>&1; then
    systemctl mask tinyproxy.service >/dev/null
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q tinyproxy >/dev/null
  fi
  systemctl mask tinyproxy.service >/dev/null
  systemctl stop tinyproxy.service 2>/dev/null || true
  install -d -o root -g root -m 0755 "$ETC"
  install -d -o root -g root -m 0755 "$LOGS"
  install -o root -g root -m 0644 "$REPO/common/systemd/$UNIT" "$SYSTEMD/$UNIT"
  rm -f "$ETC"/*.conf "$ETC"/*.filter
  egress_py render --out "$ETC" --log-dir "$LOGS"
  chmod 0644 "$ETC"/* 2>/dev/null || true
  systemctl daemon-reload
  for name in $(running); do
    grep -q "^$name " <<<"$listed" || systemctl disable --now "digipaddock-egress@$name.service" >/dev/null 2>&1 || true
  done
  while read -r name port; do
    [ -n "$name" ] || continue
    log="$LOGS/$name.log"
    [ -f "$log" ] || install -o tinyproxy -g "$observe" -m 0640 /dev/null "$log"
    chown tinyproxy:"$observe" "$log" && chmod 0640 "$log"
    systemctl enable "digipaddock-egress@$name.service" >/dev/null 2>&1
    systemctl restart "digipaddock-egress@$name.service"
  done <<<"$listed"
  sleep 1
}

disable_all() {
  local name
  for name in $(running); do
    systemctl disable --now "digipaddock-egress@$name.service" >/dev/null 2>&1 || true
  done
  echo "egress: every probe exit is stopped"
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable) disable_all ;;
esac
