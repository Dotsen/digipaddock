#!/usr/bin/env bash
# The prepared runtime of kd-e2e, run as root:
#   e2e_runtime_setup.sh install   (kd-release after every release, when this host has the runtime; the factory's
#                                   component e2e-runtime; the first install by hand)
#   e2e_runtime_setup.sh check
# install: the runtime of the deployed release (/opt/digipaddock and its REVISION) at
#   /var/lib/digipaddock/e2e/runtime: app/ — the release's tree with the cabinet web built (npm ci --ignore-scripts,
#   npm run build) by the base user in its build directory (npm runs package code — never root's), and an empty tmp/;
#   python/ — a copy of the cabinet's venv; browsers/ — Chromium of the locked Playwright, downloaded by the base user
#   once per Playwright version (the runtime's copy is reused while the version stays); revision.json — the release's
#   full commit (from kd-release's mirror of the project released to /opt/digipaddock, by the repos registry, else
#   its read-only copy). Assembled beside the runtime,
#   checked by the executor's own rules (kd.e2e.runtime_info), swapped in whole: root-owned, no group/other write.
#   Nothing to do when the runtime already is the release's. Network: the npm registry and Playwright's CDN. Ends
#   with check.
# check: the runtime is the deployed release's and passes kd.e2e.runtime_info; exit 1 with the reason (stale — an
#   older release's runtime, kd-e2e would test old code).
set -euo pipefail
REPO=/opt/digipaddock
E2E=/var/lib/digipaddock/e2e
RUNTIME=$E2E/runtime
NEW=$E2E/runtime.new
OLD=$E2E/runtime.old
BUILD=$E2E/build
VENV=/var/lib/digipaddock/venv/cabinet
REPOS=/etc/digipaddock/repos.json
MIRRORS=/var/lib/digipaddock/mirror  # kd-release's mirrors: <repository name>.git
COPIES=/srv/digipaddock-repos  # the read-only copies: <project>
TARGET=digipaddock  # kd-release's release target of /opt/digipaddock (its RELEASES)
WEB=common/cabinet/web
USAGE="usage: e2e_runtime_setup.sh install|check"

mode=${1:-}
case $mode in
  install|check) ;;
  *) echo "$USAGE" >&2; exit 2 ;;
esac
[ $# -eq 1 ] || { echo "$USAGE" >&2; exit 2; }
[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }
# root's cwd (/root 0700) is closed to runuser of the base user, and python looks for modules in cwd first
cd /
# shellcheck source=lib/naming.sh
. "$REPO/common/install/lib/naming.sh"
base=$(kd_user base)

sources() {  # where the released project's history is, by the repos registry: kd-release's mirror, then the copy
  /usr/bin/python3 -I -c '
import json, sys
for project, entry in sorted(json.load(open(sys.argv[1])).items()):
    if isinstance(entry, dict) and entry.get("release") == sys.argv[2] and isinstance(entry.get("repo"), str):
        print(sys.argv[3] + "/" + entry["repo"].split("/", 1)[-1] + ".git")
        print(sys.argv[4] + "/" + project)
' "$REPOS" "$TARGET" "$MIRRORS" "$COPIES"
}

full_commit() {  # the release's 40-hex commit: REVISION holds its first 7
  local rev found where places
  rev=$(cat "$REPO/REVISION")
  [[ $rev =~ ^[0-9a-f]{7,40}$ ]] || { echo "REVISION is not hex: $rev" >&2; return 1; }
  places=$(sources) || { echo "$REPOS is not readable" >&2; return 1; }
  while read -r where; do
    [ -n "$where" ] && [ -d "$where" ] || continue
    found=$(git -c safe.directory="$where" -C "$where" rev-parse --verify --quiet "$rev^{commit}" 2>/dev/null) || continue
    if [[ $found =~ ^[0-9a-f]{40}$ ]]; then echo "$found"; return 0; fi
  done <<< "$places"
  echo "commit $rev is not in the project history ($REPOS, release $TARGET)" >&2
  return 1
}

runtime_commit() {  # the runtime's commit by the executor's own checks, or the reason on stderr
  /usr/bin/python3 -I -c '
import sys
sys.path.insert(0, sys.argv[1] + "/common")
from kd import e2e
try:
    print(e2e.runtime_info(sys.argv[2])["commit"])
except e2e.Unavailable as exc:
    sys.exit(str(exc))
' "$REPO" "$1"
}

check() {
  local want have
  want=$(full_commit) || exit 1
  have=$(runtime_commit "$RUNTIME") || exit 1
  if [ "$have" != "$want" ]; then
    echo "e2e runtime stale: ${have:0:7} ≠ deployed ${want:0:7} — kd-e2e checks old code (e2e_runtime_setup.sh install)" >&2
    exit 1
  fi
  if [ -L "$E2E/runs" ] || [ ! -d "$E2E/runs" ] || [ "$(stat -c '%U %a' "$E2E/runs")" != "$base 700" ]; then
    echo "$E2E/runs: missing, a link, or not $base 0700 — kd-e2e cannot keep a run (e2e_runtime_setup.sh install)" >&2
    exit 1
  fi
  echo "e2e runtime: ${have:0:7}"
}

build_web() {  # the release's web in the base user's build directory: fresh sources, npm ci from the lock, the build
  local src=$BUILD/web
  # entries inside $BUILD are the base user's: made and emptied by it, never shaped by root
  install -d -o "$base" -g "$base" -m 0755 "$BUILD"
  runuser -u "$base" -- mkdir -p "$src"
  runuser -u "$base" -- find "$src" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
  (cd "$REPO/$WEB" && tar --exclude=./node_modules --exclude=./dist -cf - .) \
    | runuser -u "$base" -- tar -xf - -C "$src" --no-same-owner --no-same-permissions
  runuser -u "$base" -- env -i HOME="/home/$base" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
    bash -c 'cd "$1" && npm ci --ignore-scripts --no-audit --no-fund && npm run build' _ "$src"
}

playwright_version() {  # read by the base user (its tree), checked before root uses it
  local version
  version=$(runuser -u "$base" -- env -i PATH=/usr/local/bin:/usr/bin:/bin \
    node -e 'process.stdout.write(require(process.argv[1] + "/node_modules/playwright-core/package.json").version)' "$1")
  [[ $version =~ ^[0-9][0-9A-Za-z.-]{0,63}$ ]] || { echo "Playwright version not recognized: ${version:0:80}" >&2; return 1; }
  echo "$version"
}

browsers() {  # Chromium of this Playwright: the runtime's copy while the version stays, else downloaded anew
  local version=$1 cache=$BUILD/browsers
  if [ "$(cat "$RUNTIME/browsers/.playwright" 2>/dev/null || true)" = "$version" ]; then
    cp -a "$RUNTIME/browsers" "$NEW/browsers"
    return 0
  fi
  if [ "$(runuser -u "$base" -- cat "$cache/.playwright" 2>/dev/null || true)" != "$version" ]; then
    runuser -u "$base" -- rm -rf "$cache"
    runuser -u "$base" -- mkdir -p "$cache"
    runuser -u "$base" -- env -i HOME="/home/$base" PATH=/usr/local/bin:/usr/bin:/bin LANG=C.UTF-8 \
      PLAYWRIGHT_BROWSERS_PATH="$cache" bash -c 'cd "$1" && node node_modules/@playwright/test/cli.js install chromium' \
      _ "$BUILD/web"
    printf '%s\n' "$version" | runuser -u "$base" -- tee "$cache/.playwright" >/dev/null
  fi
  install -d -m 0755 "$NEW/browsers"
  from_build "$cache" "$NEW/browsers" .
}

# A tree of the base user's build, copied into root's new runtime: read by the base user, so a folder of it swapped
# for a link while the copy runs points at nothing the base user could not read already (review 04.11.2026); tar
# extracts its links last, as links, and the checks below refuse the ones that are not the build's own.
from_build() {
  local src=$1 dest=$2
  shift 2
  runuser -u "$base" -- tar -C "$src" -cf - "$@" | tar -xf - -C "$dest" --no-same-owner
}

install_runtime() {
  id "$base" >/dev/null 2>&1 || { echo "no base user $base" >&2; exit 1; }
  [ -x "$VENV/bin/python" ] || { echo "no cabinet venv $VENV — run cabinet_setup.sh install first" >&2; exit 1; }
  command -v npm >/dev/null || { echo "no npm" >&2; exit 1; }
  command -v bwrap >/dev/null || { echo "no bwrap — kd-e2e does not start without it" >&2; exit 1; }
  local want have version
  want=$(full_commit) || exit 1
  install -d -o root -g root -m 0755 "$E2E"
  # kd-e2e runs as the base user and keeps its run folders here (kd.e2e.RUNS): its own, closed to others
  [ ! -L "$E2E/runs" ] || { echo "$E2E/runs is a link — not installing" >&2; exit 1; }
  install -d -o "$base" -g "$base" -m 0700 "$E2E/runs"
  exec 9>"$E2E/.install.lock"
  flock -w 300 9 || { echo "another e2e runtime install has been running for over 300 s" >&2; exit 4; }
  have=$(runtime_commit "$RUNTIME" 2>/dev/null || true)
  if [ "$have" = "$want" ]; then check; return 0; fi
  build_web
  version=$(playwright_version "$BUILD/web") || exit 1
  rm -rf "$NEW" "$OLD"
  install -d -o root -g root -m 0755 "$NEW" "$NEW/app"
  (cd "$REPO" && tar --exclude="./$WEB/node_modules" --exclude="./$WEB/dist" -cf - .) | tar -xf - -C "$NEW/app"
  from_build "$BUILD/web" "$NEW/app/$WEB" node_modules dist
  install -d -m 0755 "$NEW/app/tmp"
  cp -a "$VENV" "$NEW/python"
  browsers "$version"
  # what came from the build is the base user's: no link where none is expected (node_modules' own relative links
  # are bounded by runtime_info), and root's files are written fresh, never through an entry that was copied
  if [ -L "$NEW/browsers" ] || [ -n "$(find "$NEW/browsers" "$NEW/app/$WEB/dist" -type l -print -quit)" ]; then
    rm -rf "$NEW"
    echo "links in the built runtime (browsers/ or dist/) — not installing" >&2
    exit 1
  fi
  rm -f "$NEW/browsers/.playwright"
  printf '%s\n' "$version" > "$NEW/browsers/.playwright"
  printf '{"commit": "%s"}\n' "$want" > "$NEW/revision.json"
  chown -hR root:root "$NEW"
  chmod -R go-w "$NEW"
  runtime_commit "$NEW" >/dev/null || { rm -rf "$NEW"; exit 1; }
  if [ -d "$RUNTIME" ]; then mv "$RUNTIME" "$OLD"; fi
  mv "$NEW" "$RUNTIME"
  rm -rf "$OLD"
  check
}

case $mode in
  install) install_runtime ;;
  check) check ;;
esac
