"""an agent text never tells the agent to change a card's description or AC without --revision.

The executor refuses such a write (it needs the revision the agent read), so an instruction without it is a
command that always fails first.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TEXTS = ("agents", "common/agent-template", "common/specializations")
SCOPE = re.compile(r"kd-task edit\b[^`\n]*?(--description|--ac\b)")


def test_scope_edits_in_agent_texts_carry_a_revision():
    bad = []
    for folder in TEXTS:
        for path in (ROOT / folder).rglob("*.md"):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for command in re.findall(r"kd-task edit[^`\n]*", line):
                    if SCOPE.search(command) and "--revision" not in command:
                        bad.append(f"{path.relative_to(ROOT)}:{n}: {command.strip()}")
    assert bad == []
