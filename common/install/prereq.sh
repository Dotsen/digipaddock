#!/usr/bin/env bash
# DigiPaddock: what the factory host needs before its first tree, Ubuntu 24.04.
# usage: bash common/install/prereq.sh check|install [--hindsight]
# check: the distribution's packages (PACKAGES), Node.js of the pinned major or newer with npm, and with --hindsight
#   the PostgreSQL cluster <v>/main and postgresql-<v>-pgvector of the pinned minimum (common/install/versions.json).
#   Exit 0 — all there; 1 — one line per missing piece and the exact command that brings it.
# install (root): apt-get install of the missing distribution packages only, then check. Node.js and PostgreSQL come
#   from their vendors' apt repositories (NodeSource, PGDG): this script prints their commands, never adds a source —
#   adding a foreign repository to root's apt is the owner's call; exit 3 while one of them is missing.
set -euo pipefail
TREE=$(CDPATH= cd -P -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." >/dev/null && pwd -P)
PACKAGES="git nginx certbot bubblewrap socat jq acl python3-venv curl sudo openssh-client"

usage() { echo "usage: prereq.sh check|install [--hindsight]" >&2; exit 2; }
mode=${1:-}
case $mode in check|install) ;; *) usage ;; esac
hindsight=""
[ $# -le 2 ] || usage
if [ $# -eq 2 ]; then [ "$2" = --hindsight ] || usage; hindsight=1; fi
[ "$mode" = check ] || [ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
version() { /usr/bin/python3 -I "$TREE/common/kd/install_catalog.py" version "$1"; }
node_major=$(version node_major) && pg=$(version postgresql) && pgvector_min=$(version pgvector_min) \
  || { echo "the version catalog is not readable" >&2; exit 1; }

installed() { [ "$(dpkg-query -W -f='${Status}' "$1" 2>/dev/null)" = "install ok installed" ]; }
missing_packages() {
  local p out=()
  for p in $PACKAGES; do installed "$p" || out+=("$p"); done
  echo "${out[*]}"
}

check() {
  local problems=() missing have major
  missing=$(missing_packages)
  [ -z "$missing" ] || problems+=("missing packages: $missing — apt-get install -y $missing (or prereq.sh install)")
  have=$(node --version 2>/dev/null || true)
  major=${have#v}; major=${major%%.*}
  if ! [[ $major =~ ^[0-9]+$ ]] || [ "$major" -lt "$node_major" ]; then
    problems+=("Node.js ${have:-none} — $node_major or newer is required (NodeSource): curl -fsSL https://deb.nodesource.com/setup_$node_major.x -o /tmp/nodesource_setup.sh && bash /tmp/nodesource_setup.sh && apt-get install -y nodejs")
  fi
  command -v npm >/dev/null || problems+=("no npm — it comes with nodejs from NodeSource")
  if [ -n "$hindsight" ]; then
    local pgdg="apt-get install -y postgresql-common && /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y && apt-get install -y postgresql-$pg postgresql-$pg-pgvector"
    if ! pg_lsclusters --no-header 2>/dev/null | awk '{print $1"/"$2}' | grep -qxF "$pg/main"; then
      problems+=("no PostgreSQL cluster $pg/main (PGDG): $pgdg")
    fi
    have=$(dpkg-query -W -f='${Version}' "postgresql-$pg-pgvector" 2>/dev/null || true)
    if [ -z "$have" ] || ! dpkg --compare-versions "$have" ge "$pgvector_min"; then
      problems+=("postgresql-$pg-pgvector ${have:-none} — $pgvector_min or newer is required (PGDG): $pgdg")
    fi
  fi
  if [ ${#problems[@]} -gt 0 ]; then
    printf 'prereq: %s\n' "${problems[@]}" >&2
    return 1
  fi
  echo "prereq: ok"
}

if [ "$mode" = install ]; then
  missing=$(missing_packages)
  if [ -n "$missing" ]; then
    # a fresh cloud image's package lists are stale or empty
    apt-get update -q
    # shellcheck disable=SC2086 # names of PACKAGES, one word each
    DEBIAN_FRONTEND=noninteractive apt-get install -y -q $missing
  fi
  # the distribution's part is in; what is still missing is the vendors' — the owner's step, not a failure
  check || exit 3
  exit 0
fi
check
