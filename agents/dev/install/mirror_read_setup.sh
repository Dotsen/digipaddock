#!/usr/bin/env bash
# Module setup of capability git.mirror, run as root by new_agent.sh after git.develop's
# and forge.review's:
#   mirror_read_setup.sh install|check|disable <agent> <user>
# install: the forge user's own read-only key for the code mirror (host value mirror_remote) and the bare copy the
#   forge executor fetches branches into (kd-mirror-fetch <branch>), owned by the forge user; in the clone of the released
#   code repository (repos.json: release digipaddock, role code) the remote github = that copy's path, fetching its
#   branches into refs/remotes/github/* without tags, and the copy as a safe.directory of the agent's git — both
#   written by the agent's user. The agent reads the copy by the plan's read grant (resource code-mirror); its sandbox
#   reaches no mirror host and holds no key for it. The key's public half is printed: the mirror takes it as a
#   read-only deploy key. Idempotent; a host without the clone yet gets the remote at the next install.
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: nothing — without the capability the agent has no kd-mirror-fetch and no read grant on the copy.
set -euo pipefail
REPO=/opt/digipaddock
FORGE_DIR=/etc/digipaddock/forge
KEY=$FORGE_DIR/mirror_ed25519
MIRROR_ROOT=/srv/digipaddock-mirror
COPY=$MIRROR_ROOT/code.git
REPOS=/etc/digipaddock/repos.json
AGENTS=/srv/digipaddock-agents
REMOTE=github

mode=${1:-} agent=${2:-} user=${3:-}
case $mode in
  install|check|disable) ;;
  *) echo "usage: mirror_read_setup.sh install|check|disable <agent> <user>" >&2; exit 2 ;;
esac
[[ $agent =~ ^[a-z][a-z0-9-]{0,31}$ && $user =~ ^[a-z][a-z0-9-]{0,31}$ ]] \
  || { echo "usage: mirror_read_setup.sh install|check|disable <agent> <user>" >&2; exit 2; }
[ "$mode" != disable ] || exit 0
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
# root's cwd (/root 0700) is closed to runuser, and python3 - looks for modules in cwd first
cd /
# shellcheck source=../../../common/install/lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
forge=$(kd_user forge)
MIRROR_URL=$(/usr/bin/python3 -I "$REPO/common/kd/naming.py" host mirror_remote) \
  || { echo "host/host.json is not readable (mirror_remote)" >&2; exit 1; }
# the clone of the released code repository, relative to the agent's folder ("" — not connected yet)
CLONE=$(/usr/bin/python3 -I - "$REPOS" <<'PY'
import json, sys
try:
    with open(sys.argv[1], encoding="utf-8") as f:
        data = json.load(f)
except FileNotFoundError:
    data = {}
dirs = [it.get("dir") for it in data.values()
        if isinstance(it, dict) and it.get("release") == "digipaddock" and it.get("role", "code") == "code"]
print(dirs[0] if len(dirs) == 1 and isinstance(dirs[0], str) else "")
PY
) || { echo "$REPOS is not readable" >&2; exit 1; }
[ -z "$CLONE" ] || [[ $CLONE =~ ^work/[A-Za-z0-9_.-]{1,64}$ && $CLONE != *..* ]] \
  || { echo "$REPOS: the clone folder is not valid: $CLONE" >&2; exit 1; }
DIR=${CLONE:+$AGENTS/$agent/$CLONE}
# the forge user's git in its own copy: no system or global config, no hook
FORGE_GIT=(env -i PATH=/usr/bin:/bin HOME=/nonexistent GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null
           git -c core.hooksPath=/dev/null)

# the forge user's own key: made by root in the forge's secrets folder, handed to the forge user
install_key() {
  [ -d "$FORGE_DIR" ] && [ ! -L "$FORGE_DIR" ] || { echo "no $FORGE_DIR: install forge.review first" >&2; exit 1; }
  if [ ! -f "$KEY" ]; then
    ssh-keygen -q -t ed25519 -N "" -C "digipaddock-mirror-read" -f "$KEY"
  fi
  chown -h "$forge:$forge" "$KEY" "$KEY.pub"
  chmod 0600 "$KEY"
  chmod 0644 "$KEY.pub"
}

# the bare copy: its folder root's, the repository the forge user's, made by the forge user
install_copy() {
  install -d -o root -g root -m 0755 "$MIRROR_ROOT"
  [ ! -L "$COPY" ] || { echo "$COPY is a symlink — not installing" >&2; exit 1; }
  install -d -o "$forge" -g "$forge" -m 0755 "$COPY"
  [ -f "$COPY/HEAD" ] || runuser -u "$forge" -- "${FORGE_GIT[@]}" init --quiet --bare "$COPY"
}

install_remote() {
  # In the clone, by the agent's user: its own config, which root never writes by name. The copy is the forge
  # user's: the agent's git reads it only as a named safe.directory (its global config), never trusting the rest.
  runuser -u "$user" -- bash -euc '
    dir=$1 name=$2 url=$3
    g() { GIT_CONFIG_NOSYSTEM=1 git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
    g -C "$dir" config "remote.$name.url" "$url"
    g -C "$dir" config "remote.$name.fetch" "+refs/heads/*:refs/remotes/$name/*"
    g -C "$dir" config "remote.$name.tagOpt" --no-tags
    git config --global --get-all safe.directory | grep -qxF "$url" || git config --global --add safe.directory "$url"
  ' _ "$DIR" "$REMOTE" "$COPY"
}

problems=()
check() {
  if [ -L "$KEY" ] || [ ! -f "$KEY" ] || [ "$(stat -c '%U %a' "$KEY")" != "$forge 600" ]; then
    problems+=("$KEY: missing, a symlink, or not $forge 0600")
  fi
  if [ -L "$COPY" ] || [ ! -f "$COPY/HEAD" ] || [ "$(stat -c '%U' "$COPY")" != "$forge" ]; then
    problems+=("$COPY: not a bare repository of $forge")
  fi
  if [ -n "$DIR" ] && [ -d "$DIR/.git" ]; then
    have=$(runuser -u "$user" -- git -C "$DIR" config --get "remote.$REMOTE.url" || true)
    [ "$have" = "$COPY" ] || problems+=("$DIR has no remote $REMOTE = $COPY")
    runuser -u "$user" -- git config --global --get-all safe.directory | grep -qxF "$COPY" \
      || problems+=("$user's git does not name $COPY as a safe.directory")
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'git.mirror: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

case $mode in
  install)
    install_key
    install_copy
    if [ -n "$DIR" ] && [ -d "$DIR/.git" ]; then
      install_remote
    else
      echo "$agent has no clone of the release code yet: the remote $REMOTE comes with the next install after add_repo.sh"
    fi
    check
    echo "the forge executor's key for the mirror $MIRROR_URL: a deploy key there, read-only (Settings → Deploy keys, without Allow write access):"
    cat "$KEY.pub"
    ;;
  check) check ;;
esac
