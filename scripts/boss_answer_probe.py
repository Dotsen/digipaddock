"""The BOSS answer probe: the audit's question on the two replicas — kd.boss_probe.

On the factory's host, after the release and the reinstall of boss (docs/superpowers/pipeline-a-stage-protocol.md):

    cd / && runuser -u digi-boss -- python3 -I /opt/digipaddock/scripts/boss_answer_probe.py run \\
        --instructions /srv/digipaddock-agents/boss/CLAUDE.md --out /tmp/a03 --model claude-sonnet-5-5 \\
        --grader claude-haiku-5-5 --repeats 3
    python3 -I /opt/digipaddock/scripts/boss_answer_probe.py summary /tmp/a03/runs.jsonl

«A03 passed» only when every check holds in every run; the answers are /tmp/a03/answer-<n>.md.
"""

import sys

sys.path.insert(0, "/opt/digipaddock/common")

from kd import boss_probe  # noqa: E402

if __name__ == "__main__":
    sys.exit(boss_probe.main())
