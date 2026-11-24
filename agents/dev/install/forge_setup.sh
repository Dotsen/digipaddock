#!/usr/bin/env bash
# Module setup of capability forge.review (spec 5.4), run as root by new_agent.sh:
#   forge_setup.sh install|check|disable <agent> <user>
# install: Forgejo deploy-key preflight, secrets directory and project registry, the forge user in the group digipaddock-cabinet
# (merge and release reports go to the card's thread; an older install's Pult token ACL is removed), the
#   merge/comment journal, kd-release's request directory handed to the forge user (kd-merge --release) and the
#   deploys reader's units (digipaddock-deploys.timer, the cabinet's «Deploys»). The release service itself is the
#   host's (stage_setup.sh). Idempotent.
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: the request directory goes back to root — dev requests no release; root's releases go on (the release
#   switch is the host's path unit). Secrets, journal and the deploys reader (the owner's page, not dev's) stay.
# The forge user (<prefix>-forge, naming.sh) is the capability's executor user, created by new_agent.sh
# before
# this script.
set -euo pipefail
REPO=/opt/digipaddock
DEV=$REPO/agents/dev
KNOWN=$REPO/common/install/forgejo_known_hosts
MIRRORS=/var/lib/digipaddock/mirror
SECRETS=/etc/digipaddock/forge
PROJECTS=/etc/digipaddock/repos.json
PULT=/etc/digipaddock/pult.env
CABINET=$REPO/common/install/cabinet_setup.sh
JOURNAL=/var/log/digipaddock-forge
LOGROTATE=/etc/logrotate.d/digipaddock-forge
UNITS=/etc/systemd/system
DEPLOYS_UNITS="digipaddock-deploys.service digipaddock-deploys.timer"
# Same ssh options as kd-release: only the host key pinned in the repository, no system known_hosts.
ROOT_SSH="ssh -i /root/.ssh/forgejo_deploy -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=$KNOWN -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o UpdateHostKeys=no"

mode=${1:-} agent=${2:-} user=${3:-}
case $mode in
  install|check|disable) ;;
  *) echo "usage: forge_setup.sh install|check|disable <agent> <user>" >&2; exit 2 ;;
esac
[[ $agent =~ ^[a-z][a-z0-9-]{0,31}$ && $user =~ ^[a-z][a-z0-9-]{0,31}$ ]] \
  || { echo "usage: forge_setup.sh install|check|disable <agent> <user>" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
# root's cwd (/root 0700) is closed to runuser of the forge user, and python3 - looks for modules in cwd first
cd /
# shellcheck source=../../../common/install/lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
forge=$(kd_user forge)

problems=()
want() {  # want <path> <owner:group> <mode> [<mode>…]: a real directory or file with exactly this owner and a mode
  local path=$1 owner=$2 perm=$3 m
  if [ -L "$path" ] || [ ! -e "$path" ]; then problems+=("$path: missing or a symlink"); return; fi
  local have
  have=$(stat -c '%U:%G %a' "$path")
  for m in "${@:3}"; do [ "$have" != "$owner $m" ] || return 0; done
  problems+=("$path: $have, needs $owner $perm")
}

release_repos() {  # owner/name of every project with a release in the registry (none on a fresh host)
  [ -f "$PROJECTS" ] || return 0
  python3 -I -c 'import json, sys
data = json.load(open(sys.argv[1], encoding="utf-8"))
print("\n".join(sorted({e["repo"] for e in data.values() if isinstance(e, dict) and e.get("release") and isinstance(e.get("repo"), str)})))' "$PROJECTS"
}

check() {
  want "$SECRETS" "root:$forge" 750
  want "$PROJECTS" root:root 644
  [[ " $(id -nG "$forge") " == *" digipaddock-cabinet "* ]] || problems+=("$forge is not in group digipaddock-cabinet")
  if [ -f "$PULT" ]; then ! runuser -u "$forge" -- test -r "$PULT" || problems+=("$forge reads $PULT"); fi
  want "$JOURNAL" "$forge:$user" 2750
  # 650: a journal the rotation made under the agent's read grant (the directory's default ACL) shows x in its ACL
  # mask once setfacl recomputes it; nobody but the forge user writes either way
  want "$JOURNAL/forge.jsonl" "$forge:$user" 640 650
  grep -qs "^    su $forge $user\$" "$LOGROTATE" || problems+=("$LOGROTATE: no journal rotation with group $user")
  want /var/lib/digipaddock/release "$forge:root" 750
  local unit
  for unit in $DEPLOYS_UNITS; do
    kd_render "$REPO/common/systemd/$unit" | cmp -s - "$UNITS/$unit" || problems+=("$UNITS/$unit: differs from the code")
  done
  systemctl is-enabled --quiet digipaddock-deploys.timer || problems+=("digipaddock-deploys.timer is disabled")
  systemctl is-active --quiet digipaddock-deploys.timer || problems+=("digipaddock-deploys.timer is not running")
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'forge.review: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

install_journal() {
  # Own directory: the forge user writes, the agent reads by group (setgid: new and rotated files keep the group).
  install -d -o "$forge" -g "$user" -m 2750 "$JOURNAL"
  [ -e "$JOURNAL/forge.jsonl" ] || [ -L "$JOURNAL/forge.jsonl" ] \
    || install -o "$forge" -g "$user" -m 0640 /dev/null "$JOURNAL/forge.jsonl"
  # The directory belongs to the forge user: root fixes the file through a descriptor that never follows a symlink,
  # or a planted link would hand someone else's file to the forge user.
  python3 -I - "$JOURNAL/forge.jsonl" "$forge" "$user" <<'JOURNAL'
import grp, os, pwd, stat, sys
path, user, group = sys.argv[1:]
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
except OSError as exc:
    sys.exit(f"{path}: {exc.strerror} (a symlink?): the journal is left untouched, check it by hand")
try:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        sys.exit(f"{path}: not a regular file")
    os.fchown(fd, pwd.getpwnam(user).pw_uid, grp.getgrnam(group).gr_gid)
    os.fchmod(fd, 0o640)
finally:
    os.close(fd)
JOURNAL
  local tmp
  tmp=$(mktemp)
  cat > "$tmp" <<EOF
$JOURNAL/forge.jsonl {
    su $forge $user
    monthly
    rotate 24
    compress
    missingok
    notifempty
    create 0640 $forge $user
}
EOF
  install -o root -g root -m 0644 "$tmp" "$LOGROTATE"
  rm -f "$tmp"
}

own_requests() {
  # kd-release's request directory (stage_setup.sh makes it root's): the forge user writes kd-merge --release's
  # requests and the shared .lock there. Results, mirrors and the service are the host's.
  install -d -o root -g root -m 0755 /var/lib/digipaddock
  install -d -o "$forge" -g root -m 0750 /var/lib/digipaddock/release
}

install_deploys() {
  # The «Deploys» page's reader: the projects' Actions runs into the cabinet, under the forge user, every 45 s.
  local unit
  for unit in $DEPLOYS_UNITS; do
    kd_install_rendered "$REPO/common/systemd/$unit" "$UNITS/$unit" 0644
  done
  systemctl daemon-reload
  systemctl enable --now digipaddock-deploys.timer
}

install_all() {
  [ -f /root/.ssh/forgejo_deploy ] || { echo "no /root/.ssh/forgejo_deploy: the Forgejo read key for kd-release" >&2; exit 1; }
  [ -s "$KNOWN" ] || { echo "no $KNOWN: the Forgejo host key is not deployed" >&2; exit 1; }
  # The release key reads every release repository (a read-only Deploy Key, the owner's step, spec 12.1). Asked only
  # before a repository's first clone: a reinstall with the mirror in place must not depend on Forgejo being reachable.
  local repo url
  for repo in $(release_repos); do
    [ ! -d "$MIRRORS/${repo##*/}.git" ] || continue
    url=$(kd_host forge_ssh_url) || { echo "the Forgejo address is not read (host/host.json)" >&2; exit 1; }
    url=${url//\{repo\}/$repo}
    if ! GIT_SSH_COMMAND="$ROOT_SSH" timeout 60 git ls-remote "$url" refs/heads/main >/dev/null; then
      echo "the key /root/.ssh/forgejo_deploy does not read $repo (on 'Host key verification failed' above, compare $KNOWN). The owner's step (dev agent spec §12.1): Forgejo → $repo → Settings → Deploy Keys → Add Deploy Key, without write access, the key:" >&2
      cat /root/.ssh/forgejo_deploy.pub >&2
      exit 1
    fi
  done
  # Secrets: only *.env, put by the owner with put_secret.sh. The project registry sits next to them, readable by all.
  install -d -o root -g "$forge" -m 0750 "$SECRETS"
  # Token files are named after the Forgejo accounts (host/host.json forge_dev_user, forge_merge_user), not after the agent.
  dev_token="$(kd_host forge_dev_user).env"
  merge_token="$(kd_host forge_merge_user).env"
  for token in "$dev_token" "$merge_token"; do
    [ -f "$SECRETS/$token" ] || echo "no token $SECRETS/$token: put_secret.sh $(kd_host stage_alias) $SECRETS/$token root:$forge 0640 < tmp/secrets/$token" >&2
  done
  [ -f "$PROJECTS" ] || printf '{}\n' > "$PROJECTS"
  chown root:root "$PROJECTS"
  chmod 0644 "$PROJECTS"
  # Reports go to the card's thread through the cabinet: the forge user joins its socket's group (a running
  # executor sees it after its next start).
  bash "$CABINET" grant "$forge"
  install_journal
  own_requests
  install_deploys
  if python3 -I -c 'import json, sys; sys.exit(0 if "digipaddock" in json.load(open(sys.argv[1])) else 1)' "$PROJECTS"; then
    runuser -u "$forge" -- "$DEV/bin/forge_exec.py" --agent setup selfcheck digipaddock \
      || echo "warning: the selfcheck of main's protection failed (see above): kd-merge will refuse until the main rule is as in the dev agent spec §4.1" >&2
  fi
}

case $mode in
  install)
    install_all
    check
    ;;
  check) check ;;
  disable)
    # dev requests no release: the directory and the shared .lock are root's again (kd-cabinet hands the lock to
    # whoever owns the directory); root's releases go on
    if [ -d /var/lib/digipaddock/release ] && [ ! -L /var/lib/digipaddock/release ]; then
      chown root:root /var/lib/digipaddock/release
    fi ;;
esac
