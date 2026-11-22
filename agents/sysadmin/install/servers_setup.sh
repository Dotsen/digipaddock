#!/usr/bin/env bash
# Module setup of capability servers.read (spec 5.4, M02), run as root by new_agent.sh:
#   servers_setup.sh install|check|disable <agent> <user>
# install: the read-only SSH key of ssh-ro in the agent user's ~/.ssh. A key still held by the legacy user (the base user, the
#   sysadmin's user before M02) moves over unchanged with its known_hosts, so the targets' agent-ro gates keep accepting
#   it; only with no key anywhere a new one is made (then deploy_target.sh must run for every target). Idempotent.
# check: the key is the agent user's own, the legacy user holds none; exit 1 with the reasons.
# disable: nothing — the key is the agent's data, ssh-ro is gone with the wrappers.
# Root never reads or writes the key: the legacy user reads its files, the agent user writes its own (runuser), so a
# link planted in either home cannot point root anywhere.
set -euo pipefail
KEY=agent_ro_ed25519
ENABLED=/etc/digipaddock/servers.enabled  # the targets ssh-ro accepts, one alias per line (deploy_target.sh)

mode=${1:-} agent=${2:-} user=${3:-}
case $mode in
  install|check|disable) ;;
  *) echo "usage: servers_setup.sh install|check|disable <agent> <user>" >&2; exit 2 ;;
esac
[[ $agent =~ ^[a-z][a-z0-9-]{0,31}$ && $user =~ ^[a-z][a-z0-9-]{0,31}$ ]] \
  || { echo "usage: servers_setup.sh install|check|disable <agent> <user>" >&2; exit 2; }
# shellcheck source=../../../common/install/lib/naming.sh
. /opt/digipaddock/common/install/lib/naming.sh
COMMENT=agent-ro@$(kd_host stage_alias) || { echo "host.json: no stage_alias" >&2; exit 1; }
LEGACY_USER=$(kd_user base)
[ "$user" != "$LEGACY_USER" ] || { echo "servers.read: the agent user must not be $LEGACY_USER" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /
OLD=/home/$LEGACY_USER/.ssh
NEW=/home/$user/.ssh

own() {  # own <user> <path>: a regular file of that user, not a link
  runuser -u "$1" -- test -f "$2" -a ! -L "$2"
}

copy() {  # copy <file> <mode>: from the legacy user's ~/.ssh to the agent user's, atomically
  runuser -u "$LEGACY_USER" -- cat "$OLD/$1" | runuser -u "$user" -- sh -c '
    set -e; umask 077
    t=$(mktemp "$1/.kd-move.XXXXXX")
    trap "rm -f \"\$t\"" EXIT
    cat > "$t"
    chmod "$3" "$t"
    mv -f "$t" "$1/$2"
    trap - EXIT' _ "$NEW" "$1" "$2"
}

same() {  # same <file>: the legacy and the agent copies are byte-identical
  runuser -u "$LEGACY_USER" -- cat "$OLD/$1" | runuser -u "$user" -- cmp -s - "$NEW/$1"
}

drop_legacy() {  # the key pair leaves the legacy home; its known_hosts is not a secret and stays
  runuser -u "$LEGACY_USER" -- rm -f "$OLD/$KEY" "$OLD/$KEY.pub"
}

move_key() {
  runuser -u "$user" -- sh -c 'set -e; umask 077; mkdir -p "$1"; chmod 0700 "$1"' _ "$NEW"
  if own "$user" "$NEW/$KEY"; then
    if own "$LEGACY_USER" "$OLD/$KEY"; then
      same "$KEY" && same "$KEY.pub" \
        || { echo "two different ro keys: $OLD/$KEY and $NEW/$KEY; keep one by hand" >&2; exit 1; }
      drop_legacy
    fi
  elif own "$LEGACY_USER" "$OLD/$KEY"; then
    copy "$KEY" 0600
    copy "$KEY.pub" 0644
    if own "$LEGACY_USER" "$OLD/known_hosts" && ! own "$user" "$NEW/known_hosts"; then
      copy known_hosts 0644
    fi
    same "$KEY" && same "$KEY.pub" || { echo "the ro key was not moved to $NEW unchanged" >&2; exit 1; }
    drop_legacy
    echo "the ro key moved from $OLD to $NEW (the targets do not change)"
  else
    runuser -u "$user" -- ssh-keygen -q -t ed25519 -N "" -C "$COMMENT" -f "$NEW/$KEY"
    echo "new ro key $NEW/$KEY: the targets' gates do not know it; run deploy_target.sh <alias> for each target" >&2
  fi
}

problems=()
check() {
  own "$user" "$NEW/$KEY" || problems+=("no ro key $NEW/$KEY")
  own "$user" "$NEW/$KEY.pub" || problems+=("no $NEW/$KEY.pub")
  # deploy_target.sh pins each target's host key there, after this install: a host with no target yet has
  # none
  if grep -qs '[^[:space:]]' "$ENABLED"; then
    own "$user" "$NEW/known_hosts" || problems+=("no $NEW/known_hosts (deploy_target.sh)")
  fi
  [ "$(runuser -u "$user" -- stat -c %a "$NEW/$KEY" 2>/dev/null || true)" = 600 ] || problems+=("$NEW/$KEY: mode is not 600")
  [ ! -e "$OLD/$KEY" ] && [ ! -L "$OLD/$KEY" ] || problems+=("the ro key is still with $LEGACY_USER: $OLD/$KEY")
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'servers.read: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

case $mode in
  install)
    move_key
    check
    ;;
  check) check ;;
  disable) ;;
esac
