"""The one-lead probe on a copy of the cabinet base — kd.one_lead_probe.

On the cabinet's host, after the release (docs/superpowers/one-lead-stage-protocol.md):

    python3 -I -c 'import sqlite3; sqlite3.connect("file:/var/lib/digipaddock/cabinet/cabinet.db?mode=ro",
        uri=True).backup(sqlite3.connect("/tmp/ol-copy.db"))'
    python3 -I /opt/digipaddock/scripts/one_lead_probe.py run /tmp/ol-copy.db; rm -f /tmp/ol-copy.db*

Every line ends with "ok" and the exit code is 0 — D02 (2) passed; L03 prints the C01 counts. A "FAIL" line says
why. The copy is migrated and written to; the live base is refused.
"""

import sys

sys.path.insert(0, "/opt/digipaddock/common")

from kd import one_lead_probe  # noqa: E402

if __name__ == "__main__":
    sys.exit(one_lead_probe.main())
