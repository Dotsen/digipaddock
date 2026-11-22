#!/usr/bin/env bash
# release_switch.sh <rev> — the server side of a DigiPaddock release: a tar from stdin → a new release → an atomic change of the symlink.
# The server side of kd-release (there are no other releases). A break before the switch does not leave the server without code.
# release_switch.sh --rollback <dir> — the symlink back to the stored release <dir> (a directory under the base, with REVISION);
# a rollback deletes no releases. Both modes write $base/PREVIOUS — the directory that was current before the switch.
# Codes: 2 — arguments, 3 — symlinks in the archive, 4 — the lock is taken, 5 — the rollback directory is not under the base or has no REVISION.
set -euo pipefail
base=${KD_RELEASE_BASE:-/opt/digipaddock-releases}
link=${KD_RELEASE_LINK:-/opt/digipaddock}
mode=switch
if [ "${1:-}" = --rollback ]; then
  [ $# -eq 2 ] || { echo "usage: release_switch.sh --rollback <dir>" >&2; exit 2; }
  mode=rollback
else
  rev=${1:?usage: release_switch.sh <rev> < archive.tar}
  [[ $rev =~ ^[0-9a-f]{7,40}$ ]] || { echo "rev — hex 7–40: $rev" >&2; exit 2; }
fi
mkdir -p "$base"
# one release at a time: a release and a rollback do not switch /opt at once (the lock is in root's directory)
exec 9>"$base/.lock"
flock -w 300 9 || { echo "release_switch: another release has been running for over 300 s" >&2; exit 4; }
previous=$(readlink "$link" || true)
if [ "$mode" = rollback ]; then
  target=$(realpath -e -- "$2" 2>/dev/null) || { echo "release_switch: no directory $2" >&2; exit 5; }
  base_real=$(realpath -e -- "$base")
  name=${target#"$base_real"/}
  if [ "$name" = "$target" ] || [ -z "$name" ] || [[ $name == */* ]] || [ ! -f "$target/REVISION" ]; then
    echo "release_switch: $target is not a release under $base_real (a release directory with REVISION is required)" >&2
    exit 5
  fi
  ln -sfn "$target" "$link.tmp"
  mv -T "$link.tmp" "$link"
  case $previous in ""|"$target") ;; *) echo "$previous" > "$base/PREVIOUS" ;; esac
  echo "REVISION $(cat "$link/REVISION") (rollback) on $(hostname) → $target"
  exit 0
fi
new=$base/${rev:0:7}-$(date +%Y%m%d%H%M%S)
mkdir "$new"
# any failure before the symlink switch — the half-built release is removed and does not count among the 3 stored
trap 'rm -rf "$new"' EXIT
tar -x -C "$new" --no-same-owner --no-same-permissions
# a symlink from the archive would lead root's operations and the reading of the release outside it — such a release is not installed
if [ -n "$(find "$new" -type l | head -1)" ]; then
  echo "release_switch: symlinks in the archive — release rejected" >&2
  exit 3
fi
echo "$rev" > "$new/REVISION"
if [ "$(id -u)" = 0 ]; then chown -hR root:root "$new"; fi
chmod -R u=rwX,go=rX "$new"
find "$new" -type f \( -path '*/bin/*' -o -name '*.sh' -o -name agent_ro_gate.py \) -exec chmod 0755 {} +
if [ -d "$link" ]; then
  if [ ! -L "$link" ]; then mv "$link" "$base/legacy-$(date +%s)"; fi
fi
ln -sfn "$new" "$link.tmp"
mv -T "$link.tmp" "$link"
trap - EXIT
if [ -n "$previous" ]; then echo "$previous" > "$base/PREVIOUS"; fi
# the code is already switched: a cleanup failure is no reason to report "the release failed"
# the release the session host runs from ($base/HOST) is not removed even beyond the three: its code and files are under it
pin=$(cat "$base/HOST" 2>/dev/null || true)
keep=${pin:+/$(basename "$pin")/}
ls -1dt "$base"/*/ | tail -n +4 | awk -v keep="$keep" 'keep == "" || index($0, keep) == 0' | xargs -r rm -rf \
  || echo "warning: old releases not removed" >&2
echo "REVISION $(cat "$link/REVISION") on $(hostname) → $(readlink "$link")"
