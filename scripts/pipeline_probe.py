"""The pipeline probe (B01–B06) on a copy of the cabinet base — kd.pipeline_probe.

On the cabinet's host, before the release (docs/superpowers/pipeline-b-stage-protocol.md):

    python3 -I -c 'import sqlite3; sqlite3.connect("file:/var/lib/digipaddock/cabinet/cabinet.db?mode=ro",
        uri=True).backup(sqlite3.connect("/tmp/pb-copy.db"))'
    python3 -I /opt/digipaddock/scripts/pipeline_probe.py run /tmp/pb-copy.db; rm -f /tmp/pb-copy.db*

Every line ends with "ok" and the exit code is 0 — B passed; a "FAIL" line says why. The copy is migrated and
written to; the live base is refused.
"""

import sys

sys.path.insert(0, "/opt/digipaddock/common")

from kd import pipeline_probe  # noqa: E402

if __name__ == "__main__":
    sys.exit(pipeline_probe.main())
