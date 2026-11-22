"""The stages probe on a copy of the cabinet base — kd.stages_probe.

On the cabinet's host (docs/superpowers/pipeline-def-stage-protocol.md):

    python3 -I -c 'import sqlite3; sqlite3.connect("file:/var/lib/digipaddock/cabinet/cabinet.db?mode=ro",
        uri=True).backup(sqlite3.connect("/tmp/sp-copy.db"))'
    python3 -I /opt/digipaddock/scripts/stages_probe.py run /tmp/sp-copy.db; rm -f /tmp/sp-copy.db*

Every line ends with "ok" and the exit code is 0 — B passed; a "FAIL" line says why. The copy is migrated and
written to; the live base is refused.
"""

import sys

sys.path.insert(0, "/opt/digipaddock/common")

from kd import stages_probe  # noqa: E402

if __name__ == "__main__":
    sys.exit(stages_probe.main())
