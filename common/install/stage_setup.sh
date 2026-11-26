#!/usr/bin/env bash
# DigiPaddock: the shared foundation on the factory host. Idempotent, as root.
# usage: bash /opt/digipaddock/common/install/stage_setup.sh [--prefix <prefix>] [--commands <c>] [--cards <C>]
#                                                             [--origin https://<host>]
#   --prefix — the prefix of the host's users (digi by default), --commands — of the commands (kd: kd-task),
#   --cards — of a card's number (KD: KD-12): written to /etc/digipaddock/naming.json at the first installation; on
#   an installed host they do not change.
#   --origin — the cabinet's address: cabinet.json keeps it, passkeys are bound to it; by default —
#   cabinet_origin from host/host.json.
#   --check — only a check of what is installed (changes nothing): code 0 — everything is in place, 1 — a line per failure.
# Claude Code is root's one install for every agent and the model probe (claude_setup.sh install: the build of
# common/install/versions.json, its sha256 checked), never one in a home (KD-9474).
set -euo pipefail
REPO=/opt/digipaddock
NAMING=/etc/digipaddock/naming.json
BOARD=/srv/digipaddock-agents/board

ORIGIN_RE='^https://[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?(:[0-9]{1,5})?$'
usage() {
  echo "usage: stage_setup.sh [--prefix <a-z, then a-z0-9; 2–8 characters>] [--commands <the same>]" \
    "[--cards <A-Z, then A-Z0-9; 2–6 characters>] [--origin https://<cabinet address>] | --check" >&2
  exit 2
}
prefix="" commands="" cards="" origin="" check=""
while [ $# -gt 0 ]; do
  case $1 in
    --prefix) [ $# -ge 2 ] && [[ $2 =~ ^[a-z][a-z0-9]{1,7}$ ]] || usage; prefix=$2; shift 2 ;;
    --commands) [ $# -ge 2 ] && [[ $2 =~ ^[a-z][a-z0-9]{1,7}$ ]] || usage; commands=$2; shift 2 ;;
    --cards) [ $# -ge 2 ] && [[ $2 =~ ^[A-Z][A-Z0-9]{1,5}$ ]] || usage; cards=$2; shift 2 ;;
    --origin) [ $# -ge 2 ] && [[ $2 =~ $ORIGIN_RE ]] || usage; origin=$2; shift 2 ;;
    --check) check=1; shift ;;
    *) usage ;;
  esac
done
[ -z "$check" ] || [ -z "$prefix$commands$cards$origin" ] || usage
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
# the factory host — host/host.json of the release tree; behind NAT — by its cabinet's DNS
kd_factory_host || exit 1

# --check: what this script installs, read only
stage_check() {
  local problems=() base have name link unit
  [ -f "$NAMING" ] || { echo "stage: no $NAMING — stage_setup.sh has not run" >&2; return 1; }
  base=$(kd_user base)
  if ! id "$base" >/dev/null 2>&1; then
    problems+=("no user $base")
  elif id -nG "$base" | tr ' ' '\n' | grep -qx -e sudo -e admin -e wheel; then
    problems+=("$base is in group sudo/admin/wheel")
  fi
  command -v jq >/dev/null || problems+=("no jq")
  [ -d "$BOARD/.git" ] || problems+=("no git of the nightly snapshot $BOARD")
  [ -d /var/lib/digipaddock/attention ] || problems+=("no /var/lib/digipaddock/attention")
  # Claude Code is claude_setup.sh's own check, a step of its own in bootstrap.sh base: a new pinned build must not
  # fail this one, whose fix reruns this whole script and so the cabinet's setup
  if [ -d /etc/needrestart/conf.d ]; then
    cmp -s "$REPO/common/needrestart/digipaddock-sessions.conf" /etc/needrestart/conf.d/digipaddock-sessions.conf \
      || problems+=("the needrestart config differs from the code")
  fi
  [ "$(readlink /usr/local/bin/agent-launch || true)" = "$REPO/common/bin/agent-launch" ] \
    || problems+=("/usr/local/bin/agent-launch does not point to $REPO/common/bin/agent-launch")
  for name in kd-alert kd-usage kd-board kd-task kd-release kd-secret; do
    link=/usr/local/bin/$(kd_cmd "$name")
    [ "$(readlink "$link" || true)" = "$REPO/common/bin/$name" ] || problems+=("$link does not point to $REPO/common/bin/$name")
  done
  for unit in digipaddock-usage.timer digipaddock-board-sync.path digipaddock-board-sync.timer digipaddock-release.path; do
    { systemctl is-enabled --quiet "$unit" && systemctl is-active --quiet "$unit"; } || problems+=("$unit is not enabled")
  done
  # the release service: requests root's or the forge module's user's, never anyone's to read
  have=$(stat -c '%G %a' /var/lib/digipaddock/release 2>/dev/null || true)
  { [ ! -L /var/lib/digipaddock/release ] && [ "$have" = "root 750" ]; } \
    || problems+=("/var/lib/digipaddock/release: ${have:-missing}, expected group root, 750")
  [ -d /var/lib/digipaddock/release-result ] || problems+=("no /var/lib/digipaddock/release-result")
  [ -d /var/lib/digipaddock/mirror ] || problems+=("no /var/lib/digipaddock/mirror")
  local mirror
  for mirror in /var/lib/digipaddock/mirror/*.git; do
    [ ! -e "$mirror" ] || [ "$(git -C "$mirror" config --get remote.origin.mirror 2>/dev/null || true)" = true ] \
      || problems+=("$mirror: not a mirror")
  done
  [ -f /root/.ssh/forgejo_deploy ] || problems+=("no /root/.ssh/forgejo_deploy — the Forgejo read key of kd-release")
  bash "$REPO/common/install/cabinet_setup.sh" check || problems+=("cabinet_setup.sh check failed (lines above)")
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'stage: %s\n' "${problems[@]}" >&2
    return 1
  fi
  echo "stage: ok"
}
if [ -n "$check" ]; then
  stage_check
  exit $?
fi
origin=${origin:-$(kd_host cabinet_origin)}
# Everything the installation puts in place is ours: the monitor accepts it into the baseline without alarms; before
# naming.json there is no window.
if [ -z "${KD_EXPECT_OPEN-}" ]; then
  # shellcheck source=lib/expect.sh
  . "$REPO/common/install/lib/expect.sh"
  kd_expect_run "stage_setup.sh $* (REVISION $(cat "$REPO/REVISION" 2>/dev/null || echo '?'))" bash "$REPO/common/install/stage_setup.sh" "$@"
  exit $?
fi
tmp=""
trap '[ -z "$tmp" ] || rm -rf "$tmp"' EXIT

# 0. The host's names: users, commands, cards are written once, before the first name; from then on
#    all names come from naming.sh. Another value on an installed host is a refusal: a rename is a migration, not a
#    repeated installation.
install -d -o root -g root -m 0755 /srv/digipaddock-agents /etc/digipaddock
if [ ! -e "$NAMING" ] && [ ! -L "$NAMING" ]; then
  tmp=$(mktemp)
  printf '{"schema_version": 1, "prefix": "%s", "commands": "%s", "cards": "%s"}\n' \
    "${prefix:-digi}" "${commands:-kd}" "${cards:-KD}" > "$tmp"
  # naming.load() is the one rule: users named like commands, a prefix of the host's own tools (git-…) — refused
  KD_NAMING_JSON="$tmp" kd_names >/dev/null \
    || { echo "the host's names are invalid (the users' prefix equals the commands' prefix, or the commands' prefix is taken)" >&2; exit 2; }
  install -o root -g root -m 0644 "$tmp" "$NAMING"
  rm -f "$tmp"; tmp=""
fi
# the host's names of the commands this installer links on PATH
ALERT_LINK=/usr/local/bin/$(kd_cmd kd-alert)
USAGE_LINK=/usr/local/bin/$(kd_cmd kd-usage)
BOARD_LINK=/usr/local/bin/$(kd_cmd kd-board)
TASK_LINK=/usr/local/bin/$(kd_cmd kd-task)
RELEASE_LINK=/usr/local/bin/$(kd_cmd kd-release)
SECRET_LINK=/usr/local/bin/$(kd_cmd kd-secret)
base=$(kd_user base)
read -r _ host_commands host_cards < <(kd_names)
[ -z "$prefix" ] || [ "$prefix" = "$base" ] \
  || { echo "the host's prefix is $base, not $prefix: an installed host's prefix does not change" >&2; exit 1; }
[ -z "$commands" ] || [ "$commands" = "$host_commands" ] \
  || { echo "the host's commands are $host_commands-…, not $commands-…: they do not change on an installed host" >&2; exit 1; }
[ -z "$cards" ] || [ "$cards" = "$host_cards" ] \
  || { echo "the host's cards are $host_cards-<n>, not $cards-<n>: they do not change on an installed host" >&2; exit 1; }
as_agent() { sudo -iu "$base" bash -o pipefail -c "$1"; }

# 1. The base user — without sudo; the agents' folders are in a chain of root's directories (an agent cannot swap its folder)
id "$base" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$base"
if id -nG "$base" | tr ' ' '\n' | grep -qx -e sudo -e admin -e wheel; then
  echo "$base must not be in group sudo/admin/wheel" >&2; exit 1
fi

# 2. jq — agents parse JSON without python3 -c (that one is a permission prompt for the owner on every read)
command -v jq >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y jq

# 3b. The board is the cabinet: cabinet_setup.sh installs it at step 9. Here — the directory of the board's nightly
# snapshot in Backlog.md files and its git: only root writes (kd-cabinet export), so no directory on this path belongs
# to another user. chmod keeps the agents' default read ACL on backlog/ (add_user.sh --grant).
install -d -o root -g root -m 0755 /srv/digipaddock-agents/board /srv/digipaddock-agents/board/backlog /srv/digipaddock-agents/board/backlog/tasks
# Audit journal of kd-boss-board (BOSS B02): written by the executor as the base user, never by the coordinator.
install -d -o "$base" -g "$base" -m 0750 /var/log/digipaddock-board
if [ ! -d "$BOARD/.git" ]; then
  git -C /srv/digipaddock-agents/board init -q
  git -C "$BOARD" config user.name kd-board
  stage=$(kd_host stage_alias)  # its own assignment: set -e stops on an unreadable host.json (review M9)
  git -C "$BOARD" config user.email "digipaddock-board@$stage"
fi
git config --global --get-all safe.directory | grep -qxF /srv/digipaddock-agents/board || git config --global --add safe.directory /srv/digipaddock-agents/board

# 4. Claude Code: root's one install for every agent and the model probe (KD-9474), never in a home — the pinned
#    build, its sha256 checked; a home's copy an older install left is not run by anything
bash "$REPO/common/install/claude_setup.sh" install

# 6. The shared floor of Claude Code rules (root) — a template with the host's names; render_host.sh installs it
#    together with the units (step 9)

# 7. needrestart does not restart the agents' session host
if [ -d /etc/needrestart/conf.d ]; then
  install -o root -g root -m 0644 "$REPO/common/needrestart/digipaddock-sessions.conf" /etc/needrestart/conf.d/digipaddock-sessions.conf
fi

# 8. Shared scripts
ln -sfn "$REPO/common/bin/agent-launch" /usr/local/bin/agent-launch
ln -sfn "$REPO/common/bin/kd-alert" "$ALERT_LINK"
ln -sfn "$REPO/common/bin/kd-usage" "$USAGE_LINK"
ln -sfn "$REPO/common/bin/kd-board" "$BOARD_LINK"
ln -sfn "$REPO/common/bin/kd-task" "$TASK_LINK"
ln -sfn "$REPO/common/bin/kd-release" "$RELEASE_LINK"
ln -sfn "$REPO/common/bin/kd-secret" "$SECRET_LINK"  # the catalog's secrets on the host itself

# 9. The units and the floor of rules with the host's names (render_host.sh: the list of units, which of them are only
#    installed, daemon-reload)
bash "$REPO/common/install/render_host.sh"
# 9a. The board is the cabinet: a service user, the socket's group, units, the nightly snapshot, the web and nginx;
#     the cabinet's address — at the first installation (--origin)
bash "$REPO/common/install/cabinet_setup.sh" install ${origin:+--origin "$origin"}
install -d -o root -g root -m 0750 /var/lib/digipaddock/board
# attention: only root writes (watch); the setgid group of the base user — kd-boss-board reads, does not write
install -d -o root -g "$base" -m 2750 /var/lib/digipaddock/attention
systemctl enable --now digipaddock-usage.timer
systemctl enable --now digipaddock-board-sync.path digipaddock-board-sync.timer
# 9b. A release on request — an update of the host, not of an agent: on any host. The request directory is root's until
#     the dev agent's forge module gives it to its user (kd-merge --release): it cannot be taken back here. The outcomes
#     and the mirrors are root's; kd-release clones a repository's mirror at the first request. render_host.sh installs
#     the units (step 9).
[ -d /var/lib/digipaddock/release ] || install -d -o root -g root -m 0750 /var/lib/digipaddock/release
install -d -o root -g root -m 0755 /var/lib/digipaddock/release-result
install -d -o root -g root -m 0700 /var/lib/digipaddock/mirror
systemctl enable --now digipaddock-release.path

echo "claude: $(as_agent 'DISABLE_AUTOUPDATER=1 /opt/digipaddock-claude/claude --version')"

# 6b. The agents' Hindsight memory: the plugin and the config for every agent user (the base one and those from
# /etc/digipaddock/agents.json) — only when the server is already on the factory host. At the end of the script and
# without exit on a network failure: the units and the floor of rules are already in place by then.
# shellcheck source=lib/hindsight_client.sh
source "$REPO/common/install/lib/hindsight_client.sh"
users=$(python3 -I -c 'import sys; sys.path.insert(0, sys.argv[1]); from kd import agents; print(" ".join(agents.users()))' "$REPO/common") \
  || { echo "warning: /etc/digipaddock/agents.json is unreadable — Hindsight for $base only" >&2; users=$base; }
for u in $users; do
  id "$u" >/dev/null 2>&1 || { echo "warning: no user $u (add_user.sh) — Hindsight skipped" >&2; continue; }
  hindsight_client_for "$u" || echo "warning: the Hindsight client for $u did not install — rerun stage_setup.sh" >&2
done
