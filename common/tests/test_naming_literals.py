"""Repository guards: no pre-rename user or agent name outside the allowed places, and no name of the
first deployment or of the retired runtime anywhere."""

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile(
    r"agent-(?:dev|boss|sysadmin|exec|forge|worker)"
    r"|\b(?:User|Group)=agent\b(?![-\w])|/home/agent(?![-\w])|-[iu]{1,2} agent(?![-\w])"
    r"|[\"'](?:user|run_as)[\"']\s*:\s*[\"']agent[\"']|~agent(?![-\w])"
)
# Names that keep the old spelling: the key comments on target servers.
ALLOWED = (
    "agent-exec@",  # the exec key's comment on targets (agent-exec@<factory host>), not a user name
    "agent-exec-gate",  # the exec key's forced command on targets, beside agent-ro-gate: not a user name
    "agent-exec-policy",  # the exec gate's policy module next to it on targets: not a user name
    "digipaddock-devcheck",  # the dev.check logs, /var/log/digipaddock-devcheck: not the old agent
)
# Files that must name the old users: the frozen-denylist equality test and the tests that refuse an old name.
EXEMPT = {
    "agents/sysadmin/tests/test_denylist_rename.py",
    "common/tests/test_naming.py",
    "common/tests/test_add_user.py",
    "common/tests/test_naming_literals.py",
}


def _tracked(*paths, exempt=EXEMPT):
    run = subprocess.run(
        ["git", "ls-files", "-z", *paths],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    if run.returncode != 0:  # a copy of the tree outside git (scripts/remote_pytest.sh)
        pytest.skip("not a git work tree")
    return [p for p in run.stdout.split("\0") if p and p not in exempt and (ROOT / p).is_file()]


def test_no_old_names_outside_the_allowed_places():
    hits = []
    for rel in _tracked("common", "agents", "scripts", "tests", "pytest.ini"):
        try:
            text = (ROOT / rel).read_text("utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for allowed in ALLOWED:
                line = line.replace(allowed, "")
            if FORBIDDEN.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not hits, f"{len(hits)} lines:\n" + "\n".join(hits[:60])


# Production code names users only through kd.naming, naming.sh or tokens: the prefix is host configuration.
PREFIX_LITERAL = re.compile(
    r"\bkit-(?:dev|boss|sysadmin|analyst|exec|forge|repo|cabinet)\b|\bUser=digi\b|/home/digi\b|\"digi\"|-u digi\b"
)
PROD = (
    "common/kd/",
    "common/bin/",
    "common/install/",
    "common/systemd/",
    "common/claude/",
    "common/agent-",
    "agents/boss/agent.json",
    "agents/dev/bin/",
    "agents/dev/dev_agent/",
    "agents/dev/install/",
    "agents/sysadmin/bin/",
    "agents/sysadmin/sysadmin/",
    "agents/sysadmin/install/",
)


def test_production_code_spells_no_prefix():
    hits = [
        f"{rel}:{n}"
        for rel in _tracked("common", "agents", "scripts", "tests", "pytest.ini")
        if rel.startswith(PROD) and rel != "common/naming.default.json"
        for n, line in enumerate(
            (ROOT / rel).read_text("utf-8", errors="replace").splitlines(), 1
        )
        if PREFIX_LITERAL.search(line)
    ]
    assert not hits, hits


# the code names neither its first deployment nor the retired panel runtime, and spells commands and cards
# with the repository's own prefixes (kd-, KD-); a host's own names are configuration (naming.json, host.json).
RETIRED = re.compile(r"(?i:kit" r"chain|her" r"dr)|\bkh-[a-z]|\bKH-(?:[0-9]|n\b|\[)"
                     r"|\bk" r"h\b|\bk" r"h_|\bK" r"H_|\bK" r"h-")  # and the package and variables before kd (04.11.2026)
# Values, not names: stored or held elsewhere since before the rename, so they keep the old spelling.
KEPT = (
    'b"kh-cabinet-user|"',  # the salt of a passkey's user handle: the browser keeps the handle with the key
)


def test_no_deployment_or_retired_names_anywhere():
    hits = []
    for rel in _tracked(exempt={"common/tests/test_naming_literals.py"}):
        if RETIRED.search(rel):
            hits.append(rel)
        try:
            text = (ROOT / rel).read_text("utf-8")
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for kept in KEPT:
                line = line.replace(kept, "")
            if RETIRED.search(line):
                hits.append(f"{rel}:{n}: {line.strip()[:120]}")
    assert not hits, f"{len(hits)} lines:\n" + "\n".join(hits[:60])
