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
# check: the same state, changing nothing; the token missing is a warning (checks end environment_not_ready until
#   it is there). Exit 1 with the reasons.
# disable: every check contour goes into maintenance (kd-cabinet contour maintenance): no new check starts, a running
#   one ends. The user, the directories, the logs and the token stay; back — kd-cabinet contour release <name>.
set -euo pipefail
REPO=/opt/digipaddock
WORK=/var/lib/digipaddock/devcheck
LOGS=/var/log/digipaddock-devcheck
TOKEN=/etc/digipaddock/devcheck/digipaddock-ci.env
ETC=$(dirname "$TOKEN")
HOME_DIR=$WORK/home
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
