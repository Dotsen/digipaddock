#!/usr/bin/env bash
# add_repo.sh <project> <owner/repo> [--release digipaddock [--role code|overlay]] [--allow-hooks]
# [--base <branch>] [--targets <branch>[,<branch>…]] [--up-to-date yes|no] [--up-to-date-path <glob>]…
# attach a project to dev. Use --role overlay for the deployment repository that kd-release deploys with
# the code. Each release has one entry per role. The schema 2 project passport records base, targets,
# merge.require_up_to_date and merge.up_to_date_paths. Unspecified flags keep the existing values for the
# same repository, including cabinet reattachments. Root edits checks and merge_effects in the registry.
# Invalid entries are rejected before writing.
# add_repo.sh --remove <project> removes its registry entry and keeps the clones in work/. Run as root.
# Clone as the agent user with its account key. Root owns /etc/digipaddock/repos.json at mode 0644. The
# adjacent forge/ key directory is left alone. new_agent.sh dev creates work/. Exit codes used by
# kd-repo-attach: 0 attached, 2 invalid arguments or unknown board project, 3 clone path occupied, 4 hooks
# or .mcp.json without --allow-hooks, 5 missing dev account key, 6 clone or fetch refused, 7 detach refused
# for a release project, 1 other failure.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
REPOS=/etc/digipaddock/repos.json
agent_dir=/srv/digipaddock-agents/dev
lib=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)  # agents/dev of this tree: the registry's own checks

write_table() {  # the table of projects for the agent (root; CLAUDE.md refers to it)
  install -d -o root -g root -m 0755 "$agent_dir/playbooks"
  python3 - "$REPOS" "$agent_dir/playbooks/projects.md" <<'PY'
import json, os, sys
repos, out = sys.argv[1:]
with open(repos, encoding="utf-8") as f:
    data = json.load(f)
lines = ["# Projects", "",
         "| board project | repository | clone | base | targets | required checks | current base for merge "
         "| release by kd-merge --release |", "|---|---|---|---|---|---|---|---|"]
for p in sorted(data):
    it = data[p]
    base = it.get("base", "main")
    targets = ", ".join(f"`{t}`" for t in it.get("targets", [base]))
    profiles = (it.get("checks") or {}).get("profiles") or {}
    need = ", ".join(f"`{n}`" for n, s in profiles.items() if isinstance(s, dict) and s.get("required") is True) or "—"
    merge = it.get("merge") or {}
    paths = ", ".join(f"`{g}`" for g in merge.get("up_to_date_paths") or ())
    current = "yes" if merge.get("require_up_to_date", True) else f"when touching {paths}" if paths else "no"
    lines.append(f"| `{p}` | `{it['repo']}` | `{it['dir']}` | `{base}` | {targets} | {need} | {current} "
                 f"| {'yes' if it.get('release') else 'no'} |")
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
os.chmod(tmp, 0o644)
os.replace(tmp, out)
PY
}

if [ "${1:-}" = --remove ]; then
  [ $# -eq 2 ] || { echo "usage: add_repo.sh --remove <project>" >&2; exit 2; }
  project=$2
  [[ $project =~ ^[a-z][a-z0-9-]{0,31}$ ]] || { echo "bad project name: $project" >&2; exit 2; }
  [ -e "$REPOS" ] || { echo "project $project was not attached"; exit 0; }
  python3 - "$REPOS" "$project" <<'UNREG'
import json, os, sys
path, project = sys.argv[1:]
with open(path, encoding="utf-8") as f:
    data = json.load(f)
item = data.get(project)
if item is None:
    print(f"project {project} was not attached")
    sys.exit(0)
if isinstance(item, dict) and item.get("release"):
    print(f"project {project} is released by kd-merge --release: detach it only by editing {path} by hand", file=sys.stderr)
    sys.exit(7)
del data[project]
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.write("\n")
os.chmod(tmp, 0o644)
os.replace(tmp, path)
UNREG
  write_table
  echo "project $project detached; the clones in $agent_dir/work stay"
  exit 0
fi

project=${1:?usage: add_repo.sh <project> <owner/repo> [--release digipaddock [--role code|overlay]] [--allow-hooks] [--base <branch>] [--targets <branch>[,…]] [--up-to-date yes|no] [--up-to-date-path <glob>]…}; repo=${2:?owner/repo}; shift 2
release=""; role=code; allow_hooks=0; base=""; targets=""; current=""; paths=()
is_branch() { [[ $1 =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$ && $1 != *..* && $1 != *.lock ]]; }  # = repos.is_branch
while [ $# -gt 0 ]; do
  case $1 in
    --release) [ $# -ge 2 ] || { echo "--release requires a name" >&2; exit 2; }; release=$2; shift 2 ;;
    --role) [ $# -ge 2 ] || { echo "--role requires code or overlay" >&2; exit 2; }; role=$2; shift 2 ;;
    --allow-hooks) allow_hooks=1; shift ;;
    --base) [ $# -ge 2 ] && is_branch "$2" || { echo "--base requires a branch name" >&2; exit 2; }; base=$2; shift 2 ;;
    --targets)
      [ $# -ge 2 ] || { echo "--targets requires comma-separated branches" >&2; exit 2; }
      IFS=, read -r -a list <<< "$2"
      [ "${2: -1}" != , ] && [ ${#list[@]} -gt 0 ] || { echo "--targets: empty branch" >&2; exit 2; }
      for t in "${list[@]}"; do is_branch "$t" || { echo "--targets: bad branch '$t'" >&2; exit 2; }; done
      targets=$2; shift 2 ;;
    --up-to-date)
      [ $# -ge 2 ] && [[ $2 == yes || $2 == no ]] || { echo "--up-to-date: yes or no" >&2; exit 2; }
      current=$2; shift 2 ;;
    --up-to-date-path) [ $# -ge 2 ] && [ -n "$2" ] || { echo "--up-to-date-path requires a path" >&2; exit 2; }
      paths+=("$2"); shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
[[ $project =~ ^[a-z][a-z0-9-]{0,31}$ ]] || { echo "bad project name: $project" >&2; exit 2; }
[[ $repo =~ ^[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,100}$ ]] || { echo "the repository is owner/repo: $repo" >&2; exit 2; }
[ -z "$release" ] || [ "$release" = digipaddock ] || { echo "release: only digipaddock so far" >&2; exit 2; }
case $role in code|overlay) ;; *) echo "role: code or overlay" >&2; exit 2 ;; esac
[ "$role" = code ] || [ -n "$release" ] || { echo "--role overlay: only with --release" >&2; exit 2; }
# the project must exist on the board: board projects are the cabinet's table; a cabinet that did not answer is
# an explicit error
python3 -I - "$project" /opt/digipaddock/common /run/digipaddock-cabinet/local.sock <<'BOARD' || exit 2
import sys
project, common, sock = sys.argv[1:]
sys.path.insert(0, common)
from kd.cabinet import client
try:
    routes = client.routes(client.Client(sock))
except client.CabinetError as exc:
    sys.exit(f"the cabinet did not answer: the board's projects are not read ({exc}); start kd-cabinet first")
if project not in routes:
    sys.exit(f"project {project} is not among the board's projects (kd-task projects)")
BOARD
name=${repo##*/}; dir=work/$name
# the same rules as repos.load (DIR_RE, no ..): an entry the registry rejects would break every project at once
[[ $dir =~ ^work/[A-Za-z0-9_.-]{1,64}$ && $dir != *..* && $dir != work/. ]] || { echo "the repository name does not fit a work/ folder: $name" >&2; exit 2; }
[ -d "$agent_dir/work" ] || { echo "no $agent_dir/work: run new_agent.sh --install dev first" >&2; exit 1; }
# the forge address and the commit e-mail — host/host.json of the release tree
host_value() { python3 -I /opt/digipaddock/common/kd/naming.py host "$1"; }
url=$(host_value forge_ssh_url) && email=$(host_value dev_email) && name=$(host_value forge_dev_user) || { echo "host/host.json is not readable" >&2; exit 1; }
url=${url//\{repo\}/$repo}
# the agent's user comes from the registry /etc/digipaddock/agents.json (new_agent.sh recorded dev → <prefix>-dev)
user=$(python3 -I -c 'import sys; sys.path.insert(0, "/opt/digipaddock/common"); from kd import agents; print(agents.user_of("dev"))') \
  || { echo "the agent registry /etc/digipaddock/agents.json is not readable" >&2; exit 1; }
id "$user" >/dev/null 2>&1 || { echo "no user $user: run new_agent.sh --install dev first" >&2; exit 1; }
key=/home/$user/.ssh/dev_ed25519
[ -f "$key" ] && [ ! -L "$key" ] || { echo "no key of the dev account ($user): run forge_setup.sh (new_agent.sh --install dev) first" >&2; exit 5; }
# the clone (or the check of an existing one) runs only as the agent's user; the dev account's key, the Forgejo
# host key from the repository
runuser -u "$user" -- bash -euc '
  cd /  # the cwd of root (/root 0700) is closed to the agent user
  dir=$1; url=$2; key=$3; email=$4; name=$5
  export GIT_SSH_COMMAND="ssh -i $key -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile=/opt/digipaddock/common/install/forgejo_known_hosts -o StrictHostKeyChecking=yes -o GlobalKnownHostsFile=/dev/null -o UpdateHostKeys=no"
  # An existing clone belongs to the agent: its .git/config is written by the agent, and this runs outside the
  # sandbox. Here git in it only reads and sets config (no hook, no fsmonitor, no system config); the access check is
  # a ls-remote of the URL from /, where no repository config applies (review 04.11.2026).
  g() { GIT_CONFIG_NOSYSTEM=1 git -c core.hooksPath=/dev/null -c core.fsmonitor=false "$@"; }
  if [ -d "$dir/.git" ]; then
    have=$(g -C "$dir" remote get-url origin)
    [ "$have" = "$url" ] || { echo "$dir already has another origin: $have" >&2; exit 3; }
    g ls-remote --quiet "$url" HEAD >/dev/null || { echo "dev has no access to $url (ls-remote)" >&2; exit 6; }
  elif [ -e "$dir" ] || [ -L "$dir" ]; then
    echo "$dir is taken but is not a clone: the repository name matches another folder" >&2; exit 3
  else
    git clone --quiet "$url" "$dir" || { echo "dev has no access to $url (clone)" >&2; exit 6; }
  fi
  g -C "$dir" config user.name "$name"
  g -C "$dir" config user.email "$email"
  g -C "$dir" config push.default nothing
' _ "$agent_dir/$dir" "$url" "$key" "$email" "$name"
# Claude Code loads a repository's hooks and MCP even without trust in the folder — a foreign repository only
# with explicit consent
if [ "$allow_hooks" = 0 ]; then
  if [ -e "$agent_dir/$dir/.mcp.json" ] || grep -lq '"hooks"' "$agent_dir/$dir"/.claude/settings*.json 2>/dev/null; then
    echo "the repository has .mcp.json or hooks in .claude/settings*.json: attach it with --allow-hooks after a review" >&2
    exit 4
  fi
fi
python3 - "$lib" "$REPOS" "$project" "$repo" "$dir" "$release" "$role" "$base" "$targets" "$current" \
  ${paths[@]+"${paths[@]}"} <<'REG' || exit $?
import json, os, sys
lib, path, project, repo, folder, release, role, base, targets, current = sys.argv[1:11]
paths = sys.argv[11:]
sys.path.insert(0, lib)
from dev_agent import repos as registry
data = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
old = data.get(project)
# the passport of the same repository stays; what the flags name replaces it
item = {k: v for k, v in old.items() if k not in ("repo", "dir", "release", "role")} \
    if isinstance(old, dict) and old.get("repo") == repo else {}
item.update({"repo": repo, "dir": folder})
if base:
    item["base"] = base
if targets:
    item["targets"] = targets.split(",")
if current or paths:
    merge = dict(item.get("merge") or {})
    if current:
        merge["require_up_to_date"] = current == "yes"
    if paths:
        merge["up_to_date_paths"] = paths
    item["merge"] = merge
if release:
    item["release"] = release
    # one repository of each role per release (repos.load refuses more — every project would break at once)
    other = [p for p, it in data.items() if p != project and isinstance(it, dict) and it.get("release") == release
             and it.get("role", "code") == role]
    if other:
        print(f"release {release}: role {role} already belongs to project {other[0]}", file=sys.stderr)
        sys.exit(2)
    if role != "code":
        item["role"] = role
data[project] = item
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
    f.write("\n")
try:  # what repos.load would refuse breaks every project at once — it is not written
    registry.load(tmp)
except registry.RepoError as exc:
    os.unlink(tmp)
    print(exc, file=sys.stderr)
    sys.exit(2)
os.chmod(tmp, 0o644)
os.replace(tmp, path)
REG
write_table
echo "project $project → $agent_dir/$dir ($repo)${release:+, release $release, role $role}"
