#!/usr/bin/env bash
# Module setup of capability servers.execute (spec 5.4, M02), run as root by new_agent.sh:
#   exec_setup.sh install|check|disable <agent> <user>
# install: the executor's exec key (the exec user makes it), the execution journal that only the exec user writes (the
#   agent reads it through the compiled read grant), its logrotate, the Pult token taken back from the exec user
# (reports go to the card's thread), the exec user in the group digipaddock-cabinet (the board's socket,
#
#   kd-admin-run reads its card and posts its notices through the cabinet), and the
# stores of the task approval (the owner decides in the cabinet since the change): the plan directory the agent
#   writes and only the cabinet reads, the grant directory only the cabinet writes and the exec user reads. Idempotent.
# User names come from naming.sh: <prefix>-exec, <prefix>-cabinet and the base user.
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: nothing — the capability's channel files and sudoers line are the switch; key, journal and the approval
#   stores stay.
# The exec user itself is the capability's executor user, created by new_agent.sh before this script; the cabinet
# user and its group come from common/install/cabinet_setup.sh install.
set -euo pipefail
JOURNAL=/var/log/digipaddock
LOGROTATE=/etc/logrotate.d/digipaddock
PULT=/etc/digipaddock/pult.env
CABINET=/opt/digipaddock/common/install/cabinet_setup.sh
PROPOSALS=/var/lib/digipaddock/admin-proposals
GRANTS=/var/lib/digipaddock/admin-grants

mode=${1:-} agent=${2:-} user=${3:-}
case $mode in
  install|check|disable) ;;
  *) echo "usage: exec_setup.sh install|check|disable <agent> <user>" >&2; exit 2 ;;
esac
[[ $agent =~ ^[a-z][a-z0-9-]{0,31}$ && $user =~ ^[a-z][a-z0-9-]{0,31}$ ]] \
  || { echo "usage: exec_setup.sh install|check|disable <agent> <user>" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
# shellcheck source=../../../common/install/lib/naming.sh
. /opt/digipaddock/common/install/lib/naming.sh
base=$(kd_user base)
exec_user=$(kd_user exec)
cabinet=$(kd_user cabinet)
EXEC_HOME=/home/$exec_user
KEY=$EXEC_HOME/.ssh/agent_exec_ed25519
ROTATE="/var/log/digipaddock/exec.jsonl {
    su $exec_user $base
    monthly
    rotate 24
    compress
    missingok
    notifempty
    create 0640 $exec_user $base
}"

problems=()
want() {  # want <path> <owner:group> <mode> [<mode>…]: a real file or directory with exactly this owner and a mode
  local path=$1 owner=$2 perm=$3 m
  if [ -L "$path" ] || [ ! -e "$path" ]; then problems+=("$path: missing or a symlink"); return; fi
  local have
  have=$(stat -c '%U:%G %a' "$path")
  for m in "${@:3}"; do [ "$have" != "$owner $m" ] || return 0; done
  problems+=("$path: $have, want $owner $perm")
}

check() {
  want "$EXEC_HOME/.ssh" "$exec_user:$exec_user" 700
  want "$KEY" "$exec_user:$exec_user" 600
  want "$JOURNAL" "$exec_user:$base" 2750
  # 650: a journal the rotation made under the agent's read grant (the directory's default ACL) shows x in its ACL
  # mask once setfacl recomputes it; nobody but the exec user writes either way
  want "$JOURNAL/exec.jsonl" "$exec_user:$base" 640 650
  grep -qsx "    su $exec_user $base" "$LOGROTATE" || problems+=("$LOGROTATE: no journal rotation as $exec_user")
  if [ -f "$PULT" ]; then ! runuser -u "$exec_user" -- test -r "$PULT" || problems+=("$exec_user can read $PULT"); fi
  [[ " $(id -nG "$exec_user") " == *" digipaddock-cabinet "* ]] || problems+=("$exec_user is not in group digipaddock-cabinet")
  if ! id "$cabinet" >/dev/null 2>&1; then
    problems+=("no user $cabinet: run common/install/cabinet_setup.sh install first")
  else
    want "$PROPOSALS" "$user:$cabinet" 2750
    want "$GRANTS" "$cabinet:$exec_user" 2750
    runuser -u "$cabinet" -- test -r "$PROPOSALS" -a -x "$PROPOSALS" || problems+=("$cabinet cannot read $PROPOSALS")
    ! runuser -u "$cabinet" -- test -w "$PROPOSALS" || problems+=("$cabinet can write to $PROPOSALS")
    runuser -u "$exec_user" -- test -r "$GRANTS" -a -x "$GRANTS" || problems+=("$exec_user cannot read $GRANTS")
    ! runuser -u "$user" -- test -w "$GRANTS" || problems+=("$user can write to $GRANTS")
    ! runuser -u "$exec_user" -- test -w "$GRANTS" || problems+=("$exec_user can write to $GRANTS")
    ! runuser -u "$exec_user" -- test -r "$PROPOSALS" || problems+=("$exec_user can read $PROPOSALS")
    # the texts the owner signed, kept by the cabinet beside their grants for the executor
    local copy
    for copy in "$GRANTS"/*.signed-*.json; do
      [ -e "$copy" ] || [ -L "$copy" ] || continue
      want "$copy" "$cabinet:$exec_user" 640
      runuser -u "$exec_user" -- test -r "$copy" || problems+=("$exec_user cannot read $copy")
      ! runuser -u "$user" -- test -w "$copy" || problems+=("$user can write to $copy")
    done
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'servers.execute: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

install_all() {
  id "$cabinet" >/dev/null 2>&1 || { echo "no user $cabinet: run common/install/cabinet_setup.sh install first" >&2; exit 1; }
  install -d -o "$exec_user" -g "$exec_user" -m 0700 "$EXEC_HOME/.ssh"
  stage=$(kd_host stage_alias)  # its own assignment: set -e stops on an unreadable host.json (review M9)
  [ -f "$KEY" ] || runuser -u "$exec_user" -- ssh-keygen -q -t ed25519 -N "" -C "agent-exec@$stage" -f "$KEY"
  # Only the exec user writes the journal; setgid: rotated files keep the group.
  install -d -o "$exec_user" -g "$base" -m 2750 "$JOURNAL"
  [ -f "$JOURNAL/exec.jsonl" ] || install -o "$exec_user" -g "$base" -m 0640 /dev/null "$JOURNAL/exec.jsonl"
  chown "$exec_user:$base" "$JOURNAL/exec.jsonl"
  chmod 0640 "$JOURNAL/exec.jsonl"
  printf '%s\n' "$ROTATE" > "$LOGROTATE"
  # The board is the cabinet; a running executor sees the group after its next start.
  bash "$CABINET" grant "$exec_user"
  install -d -o root -g root -m 0755 /var/lib/digipaddock
  # The agent writes its plans (kd-admin-propose); the cabinet reads them by the setgid group (the cabinet user's own
  # group, nobody else is in it).
  install -d -o "$user" -g "$cabinet" -m 2750 "$PROPOSALS"
  # Only the cabinet writes grants (the owner's decision); the exec user reads them by the setgid group (kd-admin-run).
  install -d -o "$cabinet" -g "$exec_user" -m 2750 "$GRANTS"
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable) ;;  # the channel files and the sudoers line of the capability are the switch; everything here stays
esac
