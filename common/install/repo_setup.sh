#!/usr/bin/env bash
# Module setup of capability repo.read (spec 5.4), run as root by new_agent.sh:
#   repo_setup.sh install|check|disable <agent> <user>
# install: the repo service user (<prefix>-repo) with its own read-only deploy key, the checkouts directory,
#   the kd-repo-sync units and a first synchronization. Idempotent. The agent's read ACL on the checkouts is
#   the plan's grant step, applied after this script. Exit 3: the key is not a Deploy Key of every project yet —
#   the owner's step, the key printed.
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: stop the host-wide timer only after its last installed reader; user, key and checkouts stay.
set -euo pipefail
REPO=/opt/digipaddock
KNOWN=$REPO/common/install/forgejo_known_hosts
PROJECTS=/etc/digipaddock/repos.json
CHECKOUTS=/srv/digipaddock-repos
UNITS=/etc/systemd/system

mode=${1:-} agent=${2:-} user=${3:-}
case $mode in
  install|check|disable) ;;
  *) echo "usage: repo_setup.sh install|check|disable <agent> <user>" >&2; exit 2 ;;
esac
[[ $agent =~ ^[a-z][a-z0-9-]{0,31}$ && $user =~ ^[a-z][a-z0-9-]{0,31}$ ]] \
  || { echo "usage: repo_setup.sh install|check|disable <agent> <user>" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
# root's cwd (/root 0700) is closed to runuser of the repo user, and python3 - looks for modules in cwd first
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# the host's names of the commands this installer links on PATH
REPO_SYNC_LINK=/usr/local/bin/$(kd_cmd kd-repo-sync)
FORGEJO_URL=$(kd_host forge_ssh_url) || { echo "host/host.json is not readable (forge_ssh_url)" >&2; exit 1; }  # ssh://…/{repo}.git
repo=$(kd_user repo)
HOME_DIR=/home/$repo
KEY=$HOME_DIR/.ssh/repo_sync_ed25519
# Same ssh options as kd-release: only the host key pinned in the repository, no system known_hosts.
SYNC_SSH="ssh -i $KEY -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=$KNOWN -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o UpdateHostKeys=no"

problems=()
want() {  # want <path> <owner:group> <mode>: a real directory or file with exactly this owner and mode
  local path=$1 owner=$2 perm=$3
  if [ -L "$path" ] || [ ! -e "$path" ]; then problems+=("$path: missing or a symlink"); return; fi
  local have
  have=$(stat -c '%U:%G %a' "$path")
  [ "$have" = "$owner $perm" ] || problems+=("$path: $have, must be $owner $perm")
}

names() {  # the project names of repos.json, one per line (fails closed on a broken file)
  python3 -I -c 'import json, sys; d = json.load(open(sys.argv[1])); print("\n".join(sorted(d)))' "$PROJECTS"
}

check() {
  if ! id "$repo" >/dev/null 2>&1; then
    problems+=("no user $repo")
  else
    [ "$(getent passwd "$repo" | cut -d: -f7)" = /usr/sbin/nologin ] || problems+=("$repo: shell is not nologin")
    want "$HOME_DIR" "$repo:$repo" 700
    want "$KEY" "$repo:$repo" 600
  fi
  want "$PROJECTS" root:root 644
  want "$CHECKOUTS" "$repo:$repo" 750
  local name
  while read -r name; do
    [ -n "$name" ] || continue
    want "$CHECKOUTS/$name" "$repo:$repo" 750
    [ -d "$CHECKOUTS/$name/.git" ] && [ ! -L "$CHECKOUTS/$name/.git" ] || problems+=("$CHECKOUTS/$name: not a clone")
  done < <(names)
  [ "$(readlink "$REPO_SYNC_LINK" || true)" = "$REPO/common/bin/kd-repo-sync" ] || problems+=("$REPO_SYNC_LINK: not a link to the code")
  for unit in digipaddock-repo-sync.service digipaddock-repo-sync.timer; do
    kd_render "$REPO/common/systemd/$unit" | cmp -s - "$UNITS/$unit" || problems+=("$UNITS/$unit: differs from the code")
  done
  systemctl is-enabled --quiet digipaddock-repo-sync.timer || problems+=("digipaddock-repo-sync.timer is disabled")
  systemctl is-active --quiet digipaddock-repo-sync.timer || problems+=("digipaddock-repo-sync.timer is not running")
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'repo.read: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

install_user_and_key() {
  if ! id "$repo" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$repo"
  fi
  chown "$repo:$repo" "$HOME_DIR"
  chmod 0700 "$HOME_DIR"
  # Made by the service user, never by root: the private key is readable by nobody else.
  runuser -u "$repo" -- bash -euc '
    key=$1
    umask 077
    mkdir -p "$(dirname "$key")"
    chmod 0700 "$(dirname "$key")"
    [ -f "$key" ] || ssh-keygen -q -t ed25519 -N "" -C "digipaddock-repo-sync@$(hostname -s)" -f "$key"
  ' _ "$KEY"
}

preflight() {
  # Asked only for a repository without a checkout yet: a reinstall with the copies in place must not depend on
  # Forgejo being reachable. The owner's step: Forgejo → <repo> → Settings → Deploy Keys → Add, without write.
  local name repo_path missing=0
  while IFS=$'\t' read -r name repo_path; do
    [ -n "$name" ] || continue
    [ ! -d "$CHECKOUTS/$name" ] || continue
    # --exit-code: a reachable repository without the branch the sync clones (refs/heads/main) is a preflight error too
    if ! runuser -u "$repo" -- env GIT_SSH_COMMAND="$SYNC_SSH" timeout 60 git ls-remote --exit-code "${FORGEJO_URL//\{repo\}/$repo_path}" refs/heads/main >/dev/null; then
      echo "key $KEY does not read branch main of $repo_path (on 'Host key verification failed' above — check $KNOWN; code 2 — there is no branch main). Owner's step: Forgejo → $repo_path → Settings → Deploy Keys → Add Deploy Key, without write access, the key:" >&2
      cat "$KEY.pub" >&2
      missing=1
    fi
  done < <(python3 -I -c 'import json, sys; d = json.load(open(sys.argv[1])); [print(k, v["repo"], sep="\t") for k, v in sorted(d.items())]' "$PROJECTS")
  # 3 — the owner's step: new_agent.sh and bootstrap.sh say so instead of «failed»
  [ "$missing" = 0 ] || exit 3
}

install_all() {
  [ -s "$KNOWN" ] || { echo "no $KNOWN — the Forgejo host key is not deployed" >&2; exit 1; }
  [ -f "$PROJECTS" ] || { echo "no $PROJECTS — the project registry is installed by forge_setup.sh (the dev agent)" >&2; exit 1; }
  install_user_and_key
  preflight
  install -d -o "$repo" -g "$repo" -m 0750 "$CHECKOUTS"
  ln -sfn "$REPO/common/bin/kd-repo-sync" "$REPO_SYNC_LINK"
  kd_install_rendered "$REPO/common/systemd/digipaddock-repo-sync.service" "$UNITS/digipaddock-repo-sync.service" 0644
  kd_install_rendered "$REPO/common/systemd/digipaddock-repo-sync.timer" "$UNITS/digipaddock-repo-sync.timer" 0644
  systemctl daemon-reload
  systemctl enable --now digipaddock-repo-sync.timer
  # The first copies now (oneshot, synchronous): the agent's grant and first session find them in place.
  systemctl start digipaddock-repo-sync.service
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable)
    if python3 -I -c 'import sys; sys.path.insert(0, "/opt/digipaddock/common"); from kd.repo_sync import has_other_readers; sys.exit(1 if has_other_readers(sys.argv[1]) else 0)' "$agent"; then
      if systemctl cat digipaddock-repo-sync.timer >/dev/null 2>&1; then systemctl disable --now digipaddock-repo-sync.timer; fi
    fi ;;
esac
