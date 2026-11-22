#!/usr/bin/env bash
# Module setup of capability git.develop (spec 5.4), run as root by new_agent.sh:
#   develop_setup.sh install|check|disable <agent> <user>
# install: the agent's Forgejo SSH key and Host block (made by the agent's user), the test-tools venv, work/tmp/ for
#   PR bodies, replies and attachments (the developer-v1 sandbox writes only work/). Idempotent.
# check: the same state, changing nothing; exit 1 with the reasons.
# disable: nothing — key, venv and texts are data; the grants are gone already.
set -euo pipefail
REPO=/opt/digipaddock
KNOWN=$REPO/common/install/forgejo_known_hosts
AGENTS=/srv/digipaddock-agents

mode=${1:-} agent=${2:-} user=${3:-}
case $mode in
  install|check|disable) ;;
  *) echo "usage: develop_setup.sh install|check|disable <agent> <user>" >&2; exit 2 ;;
esac
[[ $agent =~ ^[a-z][a-z0-9-]{0,31}$ && $user =~ ^[a-z][a-z0-9-]{0,31}$ ]] \
  || { echo "usage: develop_setup.sh install|check|disable <agent> <user>" >&2; exit 2; }
[ "$mode" != disable ] || exit 0
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
FORGEJO_HOST=$(/usr/bin/python3 -I "$REPO/common/kd/naming.py" host forge_host) \
  || { echo "host/host.json is not readable (forge_host)" >&2; exit 1; }  # the release tree's deployment values
# root's cwd (/root 0700) is closed to runuser, and python3 - looks for modules in cwd first
cd /
# The agent's own test tools (requirements-dev.txt: pytest>=8, vermin); agent-launch finds the venv by the agent's name.
VENV=/opt/digipaddock-venv/$agent
STAMP=$VENV/.kd-requirements.sha256
TEXTS=$AGENTS/$agent/work/tmp

install_key() {
  # Made by the agent's user, atomically; the host key comes from the repository, never trust on first use.
  runuser -u "$user" -- bash -euc '
    agent=$1 home=$2 host=$3 known=$4
    umask 077
    mkdir -p "$home/.ssh"
    chmod 0700 "$home/.ssh"
    key=$home/.ssh/${agent}_ed25519
    [ -f "$key" ] || ssh-keygen -q -t ed25519 -N "" -C "$agent@$(hostname -s)" -f "$key"
    cfg=$home/.ssh/config
    if ! grep -qs "^Host $host\$" "$cfg"; then
      tmp=$(mktemp "$home/.ssh/.config.XXXXXX")
      { [ -f "$cfg" ] && cat "$cfg"; printf "\nHost %s\n  IdentityFile %s\n  IdentitiesOnly yes\n  BatchMode yes\n  UserKnownHostsFile %s\n  GlobalKnownHostsFile /dev/null\n  StrictHostKeyChecking yes\n  UpdateHostKeys no\n" "$host" "$key" "$known"; } > "$tmp"
      chmod 0600 "$tmp"
      mv -f "$tmp" "$cfg"
    fi
  ' _ "$agent" "/home/$user" "$FORGEJO_HOST" "$KNOWN"
}

build_venv() {
  # Outside the code directory (a release does not replace it), root:root with no group/other write: agent-launch puts
  # its bin/ first in the agent's PATH and hooks outside the sandbox take python3 from there, so a write by the agent
  # would be a way out of it. --system-site-packages keeps system packages visible to tests. A rerun upgrades in place.
  install -d -o root -g root -m 0755 /opt/digipaddock-venv
  [ ! -L "$VENV" ] || { echo "$VENV is a symlink" >&2; exit 1; }
  # set -e does not act inside an if condition: steps only through &&
  # The web service's packages (the hashed lock common/cabinet/requirements.txt; http-ece builds from its checked
  # source with the locked setuptools) follow the tools in pip calls of their own: a hashed lock inside
  # requirements-dev.txt would put pip into --require-hashes mode for the unpinned tools too (stage, 15.10).
  if ! ( umask 022 && python3 -m venv --system-site-packages "$VENV" \
         && "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-input --upgrade -r "$REPO/requirements-dev.txt" \
         && "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-input --require-hashes --only-binary=:all: -r "$REPO/common/cabinet/requirements-build.txt" \
         && "$VENV/bin/pip" install --quiet --disable-pip-version-check --no-input --require-hashes --only-binary=:all: --no-binary=http-ece --no-build-isolation -r "$REPO/common/cabinet/requirements.txt" ); then
    echo "the test tools venv $VENV is not built (it needs the python3-venv package and access to PyPI; the cause is in the output above): fix it and run the install again" >&2
    exit 1
  fi
  cat "$REPO/requirements-dev.txt" "$REPO/common/cabinet/requirements-build.txt" "$REPO/common/cabinet/requirements.txt" | sha256sum > "$STAMP"
  chown -hR root:root "$VENV"
  chmod -R go-w "$VENV"
}

# A reinstall leaves a working venv alone unless requirements-dev.txt or the web lock changed: no PyPI round trip,
# so an outage there cannot take a running agent down.
venv_current() {
  [ ! -L "$VENV" ] && [ -f "$STAMP" ] && [ ! -L "$STAMP" ] \
    && [ "$(cat "$REPO/requirements-dev.txt" "$REPO/common/cabinet/requirements-build.txt" "$REPO/common/cabinet/requirements.txt" | sha256sum)" = "$(cat "$STAMP")" ] \
    && "$VENV/bin/python" -I -c 'import pytest, xdist, vermin, fastapi' 2>/dev/null
}

problems=()
check() {
  runuser -u "$user" -- test -f "/home/$user/.ssh/${agent}_ed25519" || problems+=("no key /home/$user/.ssh/${agent}_ed25519")
  runuser -u "$user" -- grep -qs "^Host $FORGEJO_HOST\$" "/home/$user/.ssh/config" || problems+=("no Host $FORGEJO_HOST in /home/$user/.ssh/config")
  if [ -L "$VENV" ] || [ "$(stat -c %U "$VENV/bin" 2>/dev/null || true)" != root ]; then
    problems+=("$VENV: missing, a symlink or not root's")
  else
    runuser -u "$user" -- "$VENV/bin/python" -I -c 'import sys, pytest, xdist, vermin; sys.exit(int(pytest.__version__.split(".")[0]) < 8)' \
      || problems+=("$VENV of $user lacks pytest>=8, pytest-xdist or vermin")
    w=$(runuser -u "$user" -- find "$VENV" -writable -print -quit)
    [ -z "$w" ] || problems+=("$user can write to $w: the venv must be root's only")
  fi
  runuser -u "$user" -- test -d "$TEXTS" -a ! -L "$TEXTS" -a -w "$TEXTS" || problems+=("no directory $TEXTS")
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'git.develop: %s\n' "${problems[@]}" >&2
    exit 1
  fi
}

case $mode in
  install)
    [ -r "$REPO/requirements-dev.txt" ] || { echo "no file $REPO/requirements-dev.txt: deploy the code first" >&2; exit 1; }
    [ -s "$KNOWN" ] || { echo "no $KNOWN: the Forgejo host key is not deployed" >&2; exit 1; }
    install_key
    venv_current || build_venv
    # work/ belongs to the agent: its user makes the directory, root never writes there by name.
    runuser -u "$user" -- mkdir -p -m 0755 "$TEXTS"
    check
    echo "the key of account $agent (Forgejo → $agent → Settings → SSH Keys):"
    runuser -u "$user" -- cat "/home/$user/.ssh/${agent}_ed25519.pub"
    ;;
  check) check ;;
esac
