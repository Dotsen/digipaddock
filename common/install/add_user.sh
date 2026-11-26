#!/usr/bin/env bash
# Prepare an agent's dedicated Linux user with Claude Code and the Hindsight plugin, or
# grant/revoke its policy-derived board ACL. The default mode retains the legacy
# combined behavior; A08 uses --prepare and --grant separately so ACL comes last.
# No sudo groups. Home changes run as the home owner, never as root.
set -euo pipefail
REPO=/opt/digipaddock
u=${1:?usage: add_user.sh <linux-user> [--prepare|--grant|--revoke]}
mode=${2:---all}
[[ $# -le 2 && $mode =~ ^--(all|prepare|grant|revoke)$ ]] || { echo "usage: add_user.sh <linux-user> [--prepare|--grant|--revoke]" >&2; exit 2; }
# shellcheck source=lib/naming.sh
source "$REPO/common/install/lib/naming.sh"
base=$(kd_user base)
[[ $u =~ ^$base-[a-z][a-z0-9-]{0,22}$ ]] || { echo "the user must be $base-<agent>: $u" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
id "$base" >/dev/null 2>&1 || { echo "run stage_setup.sh first" >&2; exit 1; }

# 0. The board grant is decided in Python before anything changes: the board entry of the compiled policy of the
# manifest that runs as this user (board.read → read; board.write and board.coordinate add nothing). No
#    manifest, an unknown capability or broken registry data is a refusal, and nothing is created. The shell only
#    checks the form of the answer and applies it in step 2.
grant=$(python3 -I -B -c 'import sys; sys.path.insert(0, sys.argv[1] + "/common"); from kd.board_grant import main; sys.exit(main(sys.argv[1:]))' "$REPO" "$u") \
  || { echo "the board access of $u is not computed (reason above) — nothing changed" >&2; exit 1; }
grant_re='^(none|read) (/[A-Za-z0-9._/-]+)$'
[[ $grant =~ $grant_re ]] || { echo "invalid answer of kd.board_grant for $u — nothing changed" >&2; exit 1; }
access=${BASH_REMATCH[1]}
backlog=${BASH_REMATCH[2]}

# Revocation is independent of the desired grant. It removes a stale named entry when
# a manifest is narrowed from board.read to no board access. The board owner performs
# the recursive walk on its own files; no home, login or token is touched.
revoke_board() {
  command -v setfacl >/dev/null || { echo "setfacl is required to revoke board access" >&2; return 1; }
  setfacl -x "u:$u,d:u:$u" "$backlog"
  runuser -u "$base" -- bash -euc '
    cd /
    find "$1" -mindepth 1 -xdev \( -type d ! -user "$4" -prune \) \
      -o \( -type d -user "$4" -exec setfacl -x "$2" {} + \) \
      -o \( -type f -user "$4" -exec setfacl -x "$3" {} + \)
  ' _ "$backlog" "u:$u,d:u:$u" "u:$u" "$base"
}

if [ "$mode" = --revoke ]; then
  id "$u" >/dev/null 2>&1 && revoke_board
  exit 0
fi

# 1. A user without sudo; the homes of the base ($base) and of the new user are closed from each other (0750, each changes its own).
#    --user-group: the agent's installation needs a personal group (memory/ — install -g "$user")
id "$u" >/dev/null 2>&1 || useradd --create-home --user-group --shell /bin/bash "$u"
if id -nG "$u" | tr ' ' '\n' | grep -qx -e sudo -e admin -e wheel; then echo "$u must not be in sudo/admin/wheel" >&2; exit 1; fi
runuser -u "$u" -- chmod 0750 "/home/$u"
runuser -u "$base" -- chmod 0750 "/home/$base"
# The board is the cabinet: an agent user reads it and writes through kd-task over the socket of
# kd-cabinet.
bash "$REPO/common/install/cabinet_setup.sh" grant "$u"

if [ "$mode" = --prepare ]; then
  # Keep the new user unprivileged until the orchestrator finishes policy files.
  revoke_board
fi

# 2. Board: exactly the grant from step 0, on Backlog.md's directory (its parent, the board root, is root 0755, so
# the base user cannot swap it). no agent user gets write (every board write goes through the kd-task
# executor under the base user). backlog/ is the cabinet's nightly Markdown snapshot. read is a named
#    read-only entry; none sets no entry. Root sets the ACL on backlog/ itself only: the contents belong to the base user, and a recursive walk as root could follow a symlink swapped in between lstat and setxattr, so
#    the base user sets it on its own files only (-user "$base": other users' files inherit the default ACL). Directories of
#    other users are not opened (-prune): a mode-000 one would make find fail with EACCES on every rerun. cd /:
#    runuser without -l keeps root's cwd (/root 0700), and GNU find returns there before every -exec.
if [ "$mode" = --all ] || [ "$mode" = --grant ]; then
if [ "$mode" = --grant ] || [ "$access" = none ]; then revoke_board; fi
case $access in
  read)
    dir_acl="u:$u:rX,d:u:$u:rX"
    file_acl="u:$u:rX"
    ;;
  none)
    dir_acl=
    file_acl=
    ;;
  *) exit 1 ;;
esac
if [ -n "$dir_acl" ]; then
  command -v setfacl >/dev/null || DEBIAN_FRONTEND=noninteractive apt-get install -y acl
  setfacl -m "$dir_acl" "$backlog"
  runuser -u "$base" -- bash -euc '
    cd /
    find "$1" -mindepth 1 -xdev \( -type d ! -user "$4" -prune \) \
      -o \( -type d -user "$4" -exec setfacl -m "$2" {} + \) \
      -o \( -type f -user "$4" -exec setfacl -m "$3" {} + \)
  ' _ "$backlog" "$dir_acl" "$file_acl" "$base"
fi
# Postflight: the user must not be able to write anything in backlog/, whatever group, ACL or mode is left over.
# find runs as that user from / (root's cwd may be /root 0700); a directory it cannot list is skipped
# (2>/dev/null, || true), but find tests the directory itself before reading it.
w=$(cd / && runuser -u "$u" -- find "$backlog" -writable -print -quit 2>/dev/null || true)
if [ -n "$w" ]; then
  echo "$u can write to ${w//[^[:print:]]/?} though its board access is $access: check getfacl and the groups of $u" >&2
  exit 1
fi

if [ "$mode" = --grant ]; then exit 0; fi
fi

# 3. Claude Code: the one root-owned install every agent runs (claude_setup.sh: the pinned build, its sha256
#    checked), never a copy in the home — the binary gets the host's token, and the home is the agent's to write
#    (KD-9474). The copy an older install put in the home goes, removed by its owner: nothing runs it any more.
bash "$REPO/common/install/claude_setup.sh" install
runuser -u "$u" -- bash -c 'cd ~ && rm -f -- .local/bin/claude && rm -rf -- .local/share/claude'
runuser -u "$u" -- bash -c 'mkdir -p ~/.config && chmod 0700 ~/.config'

# 4. The Hindsight memory (the same server and key as the base user's; the bank — by the agent's folder)
# shellcheck source=lib/hindsight_client.sh
source "$REPO/common/install/lib/hindsight_client.sh"
hindsight_client_for "$u" || { if [ "$mode" = --prepare ]; then echo "Hindsight preparation failed" >&2; exit 1; fi; echo "warning: the Hindsight client for $u did not install — rerun add_user.sh" >&2; }

echo "claude ($u): $(cd / && runuser -u "$u" -- env DISABLE_AUTOUPDATER=1 /opt/digipaddock-claude/claude --version)"
