#!/usr/bin/env bash
# Claude Code for every agent, run as root (KD-9474):
#   claude_setup.sh install|check
# The one binary every agent session (agent-launch), worker job (kd-worker-run) and model probe runs, with the
# host's Claude token or a provider's key on a descriptor: $BIN, root:root 0755 in the root-owned $DIR, outside every
# agent's home — an agent that escaped its sandbox cannot replace it (kd.claude_code names the same path).
# install: the vendor's release binary of the version of common/install/versions.json (claude_code) for this
#   machine's architecture, downloaded from the vendor's release bucket and checked against the sha256 pinned there
#   (claude_code_sha256_amd64 | _arm64) before it replaces $BIN in one rename; nothing downloaded is run, the
#   vendor's install script never. A binary that already is the pinned build stays as it is. Then check.
# check: $DIR a directory root:root 755, $BIN a file root:root 755 that is the pinned build (sha256); changes
#   nothing; exit 1 with a line per problem.
set -euo pipefail
REPO=/opt/digipaddock
DIR=/opt/digipaddock-claude
BIN=$DIR/claude
RELEASES=https://downloads.claude.ai/claude-code-releases
USAGE="usage: claude_setup.sh install|check"

mode=${1:-}
case $mode in
  install|check) ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ $# -eq 1 ] || { echo "$USAGE" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
cd /

catalog() { /usr/bin/python3 -I "$REPO/common/kd/install_catalog.py" version "$1"; }
owner_mode() { stat -c '%U:%G %a %F' "$1" 2>/dev/null || true; }
sha() { sha256sum "$1" 2>/dev/null | cut -d' ' -f1 || true; }

# The pinned build of this machine: $version, $key (the architecture of versions.json), $platform (the vendor's
# name of the glibc build) and $want (its sha256).
pinned() {
  case $(uname -m) in
    x86_64) key=amd64 platform=linux-x64 ;;
    aarch64) key=arm64 platform=linux-arm64 ;;
    *) echo "claude: no pinned Claude Code build for $(uname -m)" >&2; exit 1 ;;
  esac
  version=$(catalog claude_code)
  want=$(catalog "claude_code_sha256_$key")
  [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ && $want =~ ^[0-9a-f]{64}$ ]] \
    || { echo "claude: versions.json pins no Claude Code build for $key" >&2; exit 1; }
}

check() {
  local problems=()
  pinned
  [ "$(owner_mode "$DIR")" = "root:root 755 directory" ] || problems+=("$DIR: must be a directory root:root 755")
  if [ "$(owner_mode "$BIN")" != "root:root 755 regular file" ]; then
    problems+=("$BIN: must be a file root:root 755 — claude_setup.sh install")
  elif [ "$(sha "$BIN")" != "$want" ]; then
    problems+=("$BIN: not Claude Code $version of versions.json (sha256) — claude_setup.sh install")
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'claude: %s\n' "${problems[@]}" >&2
    exit 1
  fi
  echo "claude: ok ($version)"
}

install_bin() {
  local tmp
  pinned
  [ ! -L "$DIR" ] || { echo "claude: $DIR is a link — remove it first" >&2; exit 1; }
  install -d -o root -g root -m 0755 "$DIR"
  if [ "$(owner_mode "$BIN")" = "root:root 755 regular file" ] && [ "$(sha "$BIN")" = "$want" ]; then
    return 0
  fi
  # in $DIR itself: the rename onto $BIN stays on one filesystem, so a launch sees the old binary or the new one
  tmp=$(mktemp -p "$DIR" .claude.XXXXXX)
  trap 'rm -f "$tmp"' EXIT
  if ! curl -fsSL --proto '=https' --retry 3 -o "$tmp" "$RELEASES/$version/$platform/claude"; then
    echo "claude: Claude Code $version ($platform): download failed" >&2
    exit 1
  fi
  if [ "$(sha "$tmp")" != "$want" ]; then
    echo "claude: Claude Code $version ($platform): the download's sha256 is not the one of versions.json" >&2
    exit 1
  fi
  chown root:root "$tmp"
  chmod 0755 "$tmp"
  mv -fT "$tmp" "$BIN"
  trap - EXIT
}

case $mode in
  install)
    install_bin
    check
    ;;
  check) check ;;
esac
