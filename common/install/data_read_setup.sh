#!/usr/bin/env bash
# Module setup of capability data.read, run as root by new_agent.sh:
#   data_read_setup.sh install|check|disable <agent> <user>
# install: the data key of the data user (<prefix>-data, created by new_agent.sh as the capability's executor user,
#   home 0700: no agent reads the key), its known_hosts (deploy_target.sh pins each server's host key there), the
#   journal that only the data user writes and the host reader (<prefix>-observe) reads through kd-host-read (root
#   "data"), its rotation. The key's public half goes to the servers with deploy_target.sh (agent-ro, forced command
#   agent-ro-gate --data). Idempotent.
# check: the same state, changing nothing, and that the agent's user cannot read the key; exit 1 with the reasons.
# disable: the agent's running kd-data calls stop; its channel grant is removed by new_agent.sh itself (the switch:
#   rm /etc/digipaddock/cap/<agent>/data.json). Key and journal stay.
set -euo pipefail
REPO=/opt/digipaddock
JOURNAL=/var/log/digipaddock-data
ROTATE_FILE=/etc/logrotate.d/digipaddock-data

usage() { echo "usage: data_read_setup.sh install|check|disable <agent> <user>" >&2; exit 2; }
mode=${1:-} agent=${2:-} user=${3:-}
case $mode in install|check|disable) ;; *) usage ;; esac
[[ $agent =~ ^[a-z][a-z0-9-]{0,31}$ && $user =~ ^[a-z][a-z0-9-]{0,31}$ ]] || usage
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# Owner and mode of what lies in a service user's directory go through a descriptor that never follows a link.
owned() { /usr/bin/python3 -I "$REPO/common/kd/owned_file.py" "$@"; }
data=$(kd_user data)
observe=$(kd_user observe)
HOME_DIR=/home/$data
KEY=$HOME_DIR/.ssh/agent_data_ed25519
ROTATE="$JOURNAL/data.jsonl {
    su $data $observe
    monthly
    rotate 24
    compress
    missingok
    notifempty
    create 0640 $data $observe
}"

problems=()
want() {  # want <path> <owner:group> <mode>: a real file or directory with exactly this owner and mode
  local path=$1 owner=$2 perm=$3 have
  if [ -L "$path" ] || [ ! -e "$path" ]; then problems+=("$path: missing or a symlink"); return; fi
  have=$(stat -c '%U:%G %a' "$path")
  [ "$have" = "$owner $perm" ] || problems+=("$path: $have, want $owner $perm")
}

check() {
  id "$data" >/dev/null 2>&1 || { echo "data.read: no user $data" >&2; exit 1; }
  id "$observe" >/dev/null 2>&1 || problems+=("no user $observe (the journal's reader)")
  want "$HOME_DIR" "$data:$data" 700
  want "$HOME_DIR/.ssh" "$data:$data" 700
  want "$KEY" "$data:$data" 600
  want "$JOURNAL" "$data:$observe" 2750
  want "$JOURNAL/data.jsonl" "$data:$observe" 640
  [ "$(cat "$ROTATE_FILE" 2>/dev/null)" = "$ROTATE" ] || problems+=("$ROTATE_FILE: differs from the code")
  ! runuser -u "$user" -- test -r "$KEY" || problems+=("$user can read $KEY")
  ! runuser -u "$user" -- test -w "$JOURNAL/data.jsonl" || problems+=("$user can write $JOURNAL/data.jsonl")
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'data.read: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

case $mode in
  install)
    id "$data" >/dev/null 2>&1 || { echo "data.read: no user $data (new_agent.sh creates it first)" >&2; exit 1; }
    id "$observe" >/dev/null 2>&1 || { echo "data.read: no user $observe: install an agent with host.read first" >&2; exit 1; }
    chmod 0700 "$HOME_DIR"
    owned dir "$HOME_DIR/.ssh" "$data" "$data" 700
    stage=$(kd_host stage_alias)  # its own assignment: set -e stops on an unreadable host.json
    [ -f "$KEY" ] || runuser -u "$data" -- ssh-keygen -q -t ed25519 -N "" -C "agent-data@$stage" -f "$KEY"
    owned file "$HOME_DIR/.ssh/known_hosts" "$data" "$data" 644
    # Only the data user writes the journal; setgid: rotated files keep the reader's group.
    install -d -o "$data" -g "$observe" -m 2750 "$JOURNAL"  # in the root-owned /var/log: no one swaps it
    owned file "$JOURNAL/data.jsonl" "$data" "$observe" 640
    printf '%s\n' "$ROTATE" > "$ROTATE_FILE"
    check
    echo "data.read: $agent reads the servers' databases through $data"
    ;;
  check) check ;;
  disable)
    # exact prefix: the agent's own data channel calls only
    systemctl stop "digipaddock-cap-$agent-data@*.service" 2>/dev/null || true
    ;;
esac
