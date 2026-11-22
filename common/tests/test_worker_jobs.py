"""C04: admission by card (kd.worker_jobs); the job itself lives in the cabinet."""

import json

import pytest

from kd import worker_packs, worker_slots
from kd import worker_jobs as wj
from kd.admin_task import Card
from kd.worker_policy import Slot

REG = worker_packs.load_packs(worker_packs.PACKS_ROOT, require_root_owner=False)
D1, D2 = Slot.parse("dev", "worker1"), Slot.parse("dev", "worker2")
CONFIG = worker_slots.SlotConfig("boss", ("dev",), (D1, D2), 12, 3, 60)
BOTH = worker_slots.SlotConfig("boss", ("dev", "analyst"), (D1, D2, Slot.parse("analyst", "worker1")), 12, 3, 60)
REPOS = frozenset({"digipaddock"})
TYPES = {"dev": "developer", "analyst": "analyst", "sysadmin": "sysadmin", "boss": "coordinator"}
SKILL = sorted(REG.specs["implementer"].skills)[0]


def make_card(card_id="KD-121", status="To Do", assignees=("dev",), project="digipaddock"):
    # admin_task.from_record strips the "@": assignees are plain agent names
    return Card(
        task_id=card_id, title=f"Card {card_id}", status=status, project=project,
        assignees=tuple(assignees), servers=(), labels=(), sections={}, revision="0" * 64, path="x",
    )


def req(card="KD-121", spec="implementer", skills=(SKILL,), brief="Fix the parser."):
    return wj.Request(card, spec, tuple(skills), brief)


def admit(r, *, config=CONFIG, **card):
    return wj.admit(r, make_card(r.card, **card), repos=REPOS, registry=REG, config=config, types=TYPES)


def refused(code, fn, *args, **kwargs):
    with pytest.raises(wj.JobError) as exc:
        fn(*args, **kwargs)
    assert exc.value.code == code, exc.value
    return str(exc.value)


# --- admission: the card names the agent, the agent's type names the rules ------------------

@pytest.mark.parametrize(
    "card, needle",
    [
        ({"assignees": ("sysadmin",)}, "no worker slots for @sysadmin"),
        ({"assignees": ("analyst",)}, "no worker slots for @analyst"),
        ({"assignees": ()}, "exactly one assignee"),
        ({"assignees": ("dev", "analyst")}, "exactly one assignee"),
        ({"assignees": ("Dev",)}, "exactly one assignee"),
        ({"assignees": ("@dev",)}, "exactly one assignee"),
        ({"assignees": ("owner",)}, "no worker slots for @owner"),
    ],
)
def test_only_an_agent_with_slots_is_admitted(card, needle):
    assert needle in refused(wj.EXIT_REFUSED, admit, req(), **card)


@pytest.mark.parametrize(
    "card, needle",
    [
        ({"status": "Inbox"}, "not approved"),
        ({"status": "Blocked"}, "not approved"),
        ({"status": "Review"}, "not approved"),
        ({"status": "Done"}, "not approved"),
        ({"project": "admin"}, "project admin is not a registered repository"),
    ],
)
def test_a_developer_job_needs_an_approved_card_of_a_registered_repository(card, needle):
    assert needle in refused(wj.EXIT_REFUSED, admit, req(), **card)


@pytest.mark.parametrize("status", ["To Do", "In Progress"])
def test_an_approved_developer_card_is_admitted_with_its_limits_and_hashes(status):
    got = admit(req(), status=status)
    spec = REG.specs["implementer"]
    assert (got.agent, got.type, got.repo, got.writer) == ("dev", "developer", "digipaddock", True)
    assert got.limits == {"write": "repo", "capabilities": [], "network": "none"}
    assert got.prompt.spec == "implementer" and got.prompt.sha256 == spec.sha256
    assert dict((n, h) for n, _, h in got.prompt.skills) == {SKILL: REG.skills[SKILL].sha256}
    assert admit(req(spec="reviewer", skills=())).writer is False


def test_an_analyst_job_is_admitted_on_any_open_card_without_effects():
    r = req(spec="task-brief", skills=())
    for status in ("Inbox", "To Do", "In Progress", "Review", "Blocked"):
        got = admit(r, config=BOTH, assignees=("analyst",), status=status, project="web")
        assert (got.agent, got.type, got.repo, got.writer) == ("analyst", "analyst", None, False)
        assert got.limits["write"] == "none"
    assert "not approved" in refused(wj.EXIT_REFUSED, admit, r, config=BOTH, assignees=("analyst",), status="Done")


def test_a_type_outside_this_delivery_is_refused():
    cfg = worker_slots.SlotConfig("boss", ("sysadmin",), (Slot.parse("sysadmin", "worker1"),), 12, 3, 60)
    assert "sysadmin workers" in refused(wj.EXIT_REFUSED, admit, req(), config=cfg, assignees=("sysadmin",))
    assert "manifest" in refused(wj.EXIT_ERROR, wj.admit, req(), make_card(), repos=REPOS, registry=REG, config=CONFIG, types={})


@pytest.mark.parametrize(
    "r",
    [
        req(spec="admin"),
        req(spec="task-brief", skills=()),  # an analyst specialization on a developer card
        req(spec="docs", skills=(SKILL,)),  # docs takes no skill
        req(skills=(SKILL, SKILL)),
        req(skills=(SKILL, "../x")),
        req(brief=""),
        req(brief="x" * (worker_packs.MAX_BRIEF + 1)),
        req(card="KD-121", spec="implementer\n"),
    ],
)
def test_spec_skills_and_brief_must_fit_the_registry_of_the_agents_type(r):
    refused(wj.EXIT_REFUSED, admit, r)


def test_registered_repos_reads_only_project_names(tmp_path):
    path = tmp_path / "repos.json"
    path.write_text(json.dumps({"digipaddock": {"repo": "o/r", "dir": "work/R"}}), "utf-8")
    assert wj.registered_repos(str(path)) == frozenset({"digipaddock"})
    path.write_text(json.dumps({"../x": {}}), "utf-8")
    refused(wj.EXIT_ERROR, wj.registered_repos, str(path))


# --- the definition: the whole job, as the cabinet keeps it and the runner reads it -----------

def test_definition_carries_the_admission_and_the_pool_limits():
    r = req()
    got = admit(r)
    job = wj.definition(r, got, job_id="a" * 32, caller="digi-boss", requester="boss",
                        config=worker_slots.SlotConfig("boss", ("dev",), (D1, D2), 12, 3, 60, model="sonnet"))
    assert set(job) == {"id", "card", "agent", "type", "repo", "spec", "skills", "prompt", "limits", "brief", "caller",
                        "requester", "writer", "timeout", "model"}
    assert (job["id"], job["card"], job["agent"], job["type"], job["repo"]) == ("a" * 32, "KD-121", "dev", "developer",
                                                                                "digipaddock")
    assert (job["spec"], job["skills"], job["brief"]) == ("implementer", [SKILL], "Fix the parser.")
    assert job["prompt"] == got.prompt.provenance() and job["limits"] == got.limits
    assert (job["caller"], job["requester"], job["writer"]) == ("digi-boss", "boss", True)
    assert (job["timeout"], job["model"]) == (3600, "sonnet")
    json.dumps(job)  # it travels as JSON: through the cabinet socket and the runner's stdin


def test_the_job_table_is_gone():
    for name in ("Jobs", "table", "submit", "claim_slot", "start_job", "finish_job", "lose_job", "cancel", "view",
                 "Journal", "unit_name", "pick_slot", "EXIT_BUSY", "JOBS", "AUDIT"):
        assert not hasattr(wj, name), name
