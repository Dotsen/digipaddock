#!/usr/bin/env bash
# A dump of the hindsight database (as postgres, a timer at 04:30 in the host's zone): atomically, checked with
# pg_restore -l, 7 copies locally; the directory is backup_dir from host.json (the deployment's backup mirror takes it),
# the unit passes it in HINDSIGHT_DUMPS.
set -euo pipefail
dir=${HINDSIGHT_DUMPS:?нет HINDSIGHT_DUMPS — запускает hindsight-backup.service}
f="$dir/hindsight-$(date +%F_%H%M).dump"
trap 'rm -f "$f.tmp"' EXIT
pg_dump -Fc -d hindsight -f "$f.tmp"
pg_restore -l "$f.tmp" >/dev/null
mv "$f.tmp" "$f"
ls -1t "$dir"/hindsight-*.dump | tail -n +8 | xargs -r rm -f
echo "hindsight-backup: $(basename "$f") $(stat -c %s "$f") байт"
