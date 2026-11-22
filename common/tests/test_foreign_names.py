"""a host that names commands and cards its own way (naming.json "commands": "acme", "cards": "AC").

What an agent reads and types there — its texts, its policy and settings, its PATH, the rules floor — names no
command the repository's way but the files of the release tree, and gives a card's form as the host's (AC-<n>); a
numbered card is the platform's own history and stays. The names are read once, at import, so the plans
are built in a child process with that host's naming.json."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
AGENTS = ("analyst", "boss", "dev", "sysadmin")
CHILD = r"""
import json, sys, tempfile
from pathlib import Path
import test_new_agent_plan as t
from kd import naming
from kd.install_plan import render_plan

out = {"floor": naming.rewrite(naming.render((t.REPO / "common/claude/managed-settings.template.json").read_text("utf-8")))}
with tempfile.TemporaryDirectory() as d:
    host = t._make_host(Path(d))
    for name in sys.argv[1:]:
        plan = host.plan(name)
        out[name] = {
            "files": {f.path: f.content.decode("utf-8") for f in plan.files},
            "text": render_plan(plan),
            "links": [a.target for a in plan.actions if a.kind == "link"],
        }
print(json.dumps(out))
"""
# A file of the release tree keeps its repository name; nothing else may.
TREE = re.compile(r"(?:common|agents/[a-z][a-z0-9-]*)/bin/kd-[a-z0-9-]+")
REPOSITORY_NAMES = re.compile(r"(?<![A-Za-z0-9_.-])kd-[a-z0-9]|(?<![A-Za-z0-9_])KD-(?:<|[nmN]\b)")


def _leftovers(text):
    return sorted({m.group(0) for m in REPOSITORY_NAMES.finditer(TREE.sub("", text))})


@pytest.fixture(scope="module")
def acme(tmp_path_factory):
    naming_json = tmp_path_factory.mktemp("acme") / "naming.json"
    naming_json.write_text(json.dumps({"schema_version": 1, "prefix": "digi", "commands": "acme", "cards": "AC"}))
    env = {
        **os.environ,
        "KD_NAMING_JSON": str(naming_json),
        "KD_HOST_JSON": str(REPO / "host" / "host.example.json"),
        "PYTHONPATH": os.pathsep.join([str(REPO / "common"), str(REPO / "common" / "tests")]),
    }
    run = subprocess.run([sys.executable, "-c", CHILD, *AGENTS], cwd=REPO, env=env, capture_output=True, text=True,
                         encoding="utf-8", timeout=300)
    assert run.returncode == 0, run.stderr[-3000:]
    return json.loads(run.stdout)


@pytest.mark.parametrize("name", AGENTS)
def test_an_agents_install_names_the_hosts_commands_and_cards(acme, name):
    plan = acme[name]
    for path, content in plan["files"].items():
        assert not _leftovers(content), (path, _leftovers(content))
    assert not _leftovers(plan["text"]), _leftovers(plan["text"])
    assert "acme-task" in plan["text"]


def _allowed(plan):
    """The host's commands the settings allow by rule (an agent with a guard allows its commands there instead)."""
    allowed = set()
    for path, content in plan["files"].items():
        if path.endswith("/settings.json"):
            rules = json.loads(content).get("permissions", {}).get("allow", [])
            allowed |= {m.group(1) for rule in rules for m in [re.match(r"Bash\((acme-[a-z0-9-]+)", rule)] if m}
    return allowed


@pytest.mark.parametrize("name", AGENTS)
def test_every_command_the_policy_allows_is_on_the_agents_path(acme, name):
    on_path = {link.rsplit("/", 1)[-1] for link in acme[name]["links"]}
    assert _allowed(acme[name]) <= on_path, sorted(_allowed(acme[name]) - on_path)
    assert any(_allowed(acme[n]) for n in AGENTS)


def test_the_rules_floor_names_the_hosts_commands(acme):
    assert not _leftovers(acme["floor"]), _leftovers(acme["floor"])
