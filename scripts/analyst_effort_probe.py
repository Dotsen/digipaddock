"""The analyst's effort probe: medium against high on fixed brief snapshots — kd.effort_probe.

On the factory's host (docs/superpowers/kd9044-effort-probe-protocol.md has the full order):

    python3 -I /opt/digipaddock/scripts/analyst_effort_probe.py extract --out <dir> \\
        --brief 1 --brief 1 --draft 5 --transcripts <the analyst's session transcripts> \\
        --repo digipaddock=/srv/digipaddock-repos/digipaddock@6d4994a
    cd / && runuser -u digi-analyst -- python3 -I /opt/digipaddock/scripts/analyst_effort_probe.py run \\
        --snapshots <dir> --out <dir> --model claude-opus-5-5 --efforts medium,high --repeats 3 \\
        --hide KD-<n> --hide KD-<n> --hide KD-<n>
    python3 -I /opt/digipaddock/scripts/analyst_effort_probe.py summary <dir>/runs.jsonl
"""

import sys

sys.path.insert(0, "/opt/digipaddock/common")

from kd import effort_probe  # noqa: E402

if __name__ == "__main__":
    sys.exit(effort_probe.main())
