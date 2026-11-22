"""every board command an agent's instructions name in backticks is one its capabilities allow."""

import json
import re
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "common"))

from kd import agent_text  # noqa: E402

AGENTS = ("analyst", "boss", "dev", "sysadmin")
FORM = re.compile(r"`(kd-(?:task|work|boss-board)) ([a-z][a-z-]*)")
OUTCOMES = ("continue", "wait_user", "wait_dependency", "wait_until", "review", "done")


def allowed(agent: str) -> dict:
    capabilities = json.loads((ROOT / "agents" / agent / "agent.json").read_text("utf-8"))["capabilities"]
    out: dict = {}
    for name in capabilities:
        found = json.loads((ROOT / "common" / "agent-capabilities" / f"{name}.json").read_text("utf-8"))
        for wrapper in found.get("wrappers") or []:
            out.setdefault(wrapper["command"], set()).update(wrapper.get("subcommands") or [])
    return out


def _include_tree(tmp: Path) -> Path:
    """The tree the texts' includes come from: this one, or — in the code repository without the overlay
    — one with the example of the sysadmin's server table."""
    if (ROOT / "agents/sysadmin/servers.md").is_file():
        return ROOT
    (tmp / "agents/sysadmin").mkdir(parents=True, exist_ok=True)
    (tmp / "agents/sysadmin/servers.md").write_bytes((ROOT / "agents/sysadmin/servers.example.md").read_bytes())
    return tmp


def text_of(agent: str) -> str:
    capabilities = json.loads((ROOT / "agents" / agent / "agent.json").read_text("utf-8"))["capabilities"]
    with tempfile.TemporaryDirectory() as tmp:
        return agent_text.render_instruction_files(ROOT / "agents" / agent / "CLAUDE.md", agent=agent,
                                                   owner_language="ru", board_write="board.write" in capabilities,
                                                   tree=_include_tree(Path(tmp)))


FENCE = re.compile(r"^```.*?$(.*?)^```", re.MULTILINE | re.DOTALL)
FENCED_FORM = re.compile(r"^\s*(kd-(?:task|work|boss-board)) ([a-z][a-z-]*)", re.MULTILINE)


def forms(text: str) -> set:
    found = {(command, sub) for command, sub in FORM.findall(text)}
    for block in FENCE.findall(text):  # a form inside a ``` block is one the agent types too
        found |= {(command, sub) for command, sub in FENCED_FORM.findall(block)}
    return found


@pytest.mark.parametrize("agent", AGENTS)
def test_every_board_form_in_a_text_is_allowed(agent):
    permitted = allowed(agent)
    named = forms(text_of(agent))
    assert named, agent
    refused = sorted(f"{c} {s}" for c, s in named if s not in permitted.get(c, ()))
    assert refused == [], f"{agent}: its text names forms its capabilities refuse"


def test_the_check_catches_an_unknown_form():
    assert forms("run `kd-task frobnicate KD-1` now") == {("kd-task", "frobnicate")}
    assert "frobnicate" not in allowed("dev")["kd-task"]


def test_the_boss_text_does_not_carry_the_probe_answer():
    """A03 must not pass by copying: the coordinator's example answer is of an unrelated card."""
    text = text_of("boss")
    for phrase in ("PR #18", "KD-9041", "KD-9032", "текст аналитика", "вторая часть", "Голос"):
        assert phrase not in text, phrase


def test_who_merges_a_changed_text_reads_the_text_itself():
    """view is a snapshot: a revision conflict and the card's meaning are read with kd-task scope."""
    analyst = text_of("analyst")
    conflict = next(line for line in analyst.splitlines() if "exit code 3 means the owner changed" in line)
    assert "kd-task scope" in conflict.split("exit code 3 means the owner changed", 1)[1][:200]
    routing = (ROOT / "agents" / "boss" / "playbooks" / "routing.md").read_text("utf-8").splitlines()[2]
    assert "kd-task scope KD-<n>" in routing


def test_the_check_reads_fenced_blocks():
    """a form inside a ``` block is a form the agent will type."""
    assert forms("Run:\n```\nkd-task frobnicate KD-1\n  kd-work finish KD-1\n```\n") == {
        ("kd-task", "frobnicate"), ("kd-work", "finish")}


# --- every turn on an own card ends with its outcome ------------------------------------
def test_the_common_text_names_every_outcome():
    common = (ROOT / "common" / "agent-template" / "CLAUDE.common.md").read_text("utf-8")
    board_work = common.split("## Board work", 1)[1].split("\n## ", 1)[0]
    assert "`kd-work finish KD-<n> --outcome" in board_work
    for outcome in OUTCOMES:
        assert f"`{outcome}`" in board_work, outcome
    assert "kd-task waits" in common


@pytest.mark.parametrize("agent", ("analyst", "dev", "sysadmin"))
def test_a_worker_text_ends_its_turns_with_finish(agent):
    assert "kd-work finish KD-<n> --outcome" in text_of(agent)


def test_the_coordinator_finishes_its_own_cards_with_its_own_wrapper():
    boss = text_of("boss")
    assert "kd-boss-board finish --card KD-<n> --outcome" in boss and "kd-work finish" not in boss
    assert "next (recorded)" in boss


def test_dev_no_longer_waits_by_blocking_the_card():
    dev = text_of("dev")
    assert "keep the card `kd-task edit KD-<n> --status Blocked" not in dev
    assert "--status Done" not in dev and "--status Review --final-summary" not in dev
    assert "--outcome wait_user" in dev and "--outcome done" in dev


def test_sysadmin_and_analyst_wait_by_finish():
    sysadmin, analyst = text_of("sysadmin"), text_of("analyst")
    assert "--outcome wait_dependency" in sysadmin and "--outcome review" in sysadmin
    assert "--outcome wait_user" in analyst
    handoff = next(line for line in analyst.splitlines() if "Hand the card over in one write" in line)
    assert "without `kd-work finish`" in handoff


def test_every_turn_that_owes_an_outcome_has_a_valid_one():
    """A failed plan step, a dependency the owner declined and a goal whose children are all
    Done each say how the turn ends — none of them leaves an outcome the cabinet refuses."""
    sysadmin, boss = text_of("sysadmin"), text_of("boss")
    assert ("the first failed step — stop, ask the owner on the card (`kd-work ask KD-<n>`) and end the turn with "
            "`--outcome wait_user`") in sysadmin
    assert 'kd-task edit KD-<n> --status "In Progress" --clear-deps' in sysadmin
    assert "all its children are Done" in boss and "end the turn without `finish`" in boss
