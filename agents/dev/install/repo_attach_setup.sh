#!/usr/bin/env bash
# kd-repo-attach: the root unit that binds and unbinds a project's repository on the owner's request in the
# cabinet — only add_repo.sh's logic. Run as root:
#   repo_attach_setup.sh install|check|disable
# install: the doorbell directory /var/lib/digipaddock/repo-attach (root:<cabinet> 0770: the cabinet touches
#   `request`, root takes it off), the link /usr/local/bin/kd-repo-attach, the units, digipaddock-repo-attach.path enabled
#   and started; the cabinet is restarted when the directory is new (its unit opens the directory for writing only
#   if it is there at its start). Idempotent; needs the cabinet and the dev agent (add_repo.sh) installed first.
# Runs under the monitor's expectation (kd_expect_run).
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: the path unit goes off; requests stay waiting in the cabinet, the registry and the clones stay.
set -euo pipefail
REPO=/opt/digipaddock
UNITS=/etc/systemd/system
BELL_DIR=/var/lib/digipaddock/repo-attach
BIN=$REPO/agents/dev/bin/kd-repo-attach
USAGE="usage: repo_attach_setup.sh install|check|disable"

mode=${1:-}
case $mode in
  install|check|disable) ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ $# -eq 1 ] || { echo "$USAGE" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
# shellcheck source=../../../common/install/lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# the host's names of the commands this installer links on PATH
REPO_ATTACH_LINK=/usr/local/bin/$(kd_cmd kd-repo-attach)
# Everything install changes is ours: the monitor takes it into its baseline.
if [ "$mode" = install ] && [ -z "${KD_EXPECT_OPEN-}" ]; then
  # shellcheck source=../../../common/install/lib/expect.sh
  . "$REPO/common/install/lib/expect.sh"
  kd_expect_run "repo_attach_setup.sh $* (REVISION $(cat "$REPO/REVISION" 2>/dev/null || echo '?'))" \
    bash "$REPO/agents/dev/install/repo_attach_setup.sh" "$@"
  exit $?
fi
cabinet=$(kd_user cabinet)

problems=()
check() {
  id "$cabinet" >/dev/null 2>&1 || problems+=("no user $cabinet: the cabinet is not installed")
  [ -f "$REPO/agents/dev/install/add_repo.sh" ] || problems+=("no add_repo.sh in $REPO")
  if [ -L "$BELL_DIR" ] || [ ! -d "$BELL_DIR" ]; then
    problems+=("$BELL_DIR: missing or a symlink")
  else
    local have
    have=$(stat -c '%U:%G %a' "$BELL_DIR")
    [ "$have" = "root:$cabinet 770" ] || problems+=("$BELL_DIR: $have, needs root:$cabinet 770")
  fi
  [ "$(readlink "$REPO_ATTACH_LINK" || true)" = "$BIN" ] || problems+=("$REPO_ATTACH_LINK: not a link to the code")
  local unit
  for unit in digipaddock-repo-attach.path digipaddock-repo-attach.service; do
    kd_render "$REPO/common/systemd/$unit" | cmp -s - "$UNITS/$unit" || problems+=("$UNITS/$unit: differs from the code")
  done
  systemctl is-enabled --quiet digipaddock-repo-attach.path || problems+=("digipaddock-repo-attach.path is disabled")
  systemctl is-active --quiet digipaddock-repo-attach.path || problems+=("digipaddock-repo-attach.path is not running")
  systemctl show -p ReadWritePaths --value digipaddock-cabinet.service | grep -q "$BELL_DIR" \
    || problems+=("digipaddock-cabinet.service has no ReadWritePaths=$BELL_DIR: run cabinet_setup.sh install first")
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'kd-repo-attach: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

install_all() {
  id "$cabinet" >/dev/null 2>&1 || { echo "no user $cabinet: run cabinet_setup.sh install first" >&2; exit 1; }
  [ -d /srv/digipaddock-agents/dev/work ] || { echo "no dev: run new_agent.sh --install dev first" >&2; exit 1; }
  local fresh=0
  [ -d "$BELL_DIR" ] || fresh=1
  install -d -o root -g "$cabinet" -m 0770 "$BELL_DIR"
  ln -sfn "$BIN" "$REPO_ATTACH_LINK"
  local unit
  for unit in digipaddock-repo-attach.path digipaddock-repo-attach.service; do
    kd_install_rendered "$REPO/common/systemd/$unit" "$UNITS/$unit" 0644
  done
  systemctl daemon-reload
  systemctl enable --now digipaddock-repo-attach.path
  # the cabinet opens the doorbell directory for writing only if it was there at its start
  [ "$fresh" = 0 ] || systemctl try-restart digipaddock-cabinet.service
  # requests made before the unit was there: take them now
  systemctl start --no-block digipaddock-repo-attach.service
}

case $mode in
  install)
    install_all
    check ;;
  check) check ;;
  disable)
    if systemctl cat digipaddock-repo-attach.path >/dev/null 2>&1; then
      systemctl disable --now digipaddock-repo-attach.path
    fi ;;
esac
