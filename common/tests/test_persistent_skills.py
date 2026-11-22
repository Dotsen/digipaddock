import dataclasses
import json
import shutil
from pathlib import Path

import pytest

from kd import persistent_skills as ps, specializations as sp


@pytest.fixture
def registry():
    return sp.load_registry(require_root_owner=False)


def test_shared_selection_validates_count_allowlist_duplicates_and_conflicts(registry):
    spec = registry.specs["implementer"]
    assert sp.selected_skills(registry, spec, ()) == ()
    assert [
        s.name
        for s in sp.selected_skills(registry, spec, ["python-backend", "frontend"])
    ] == ["frontend", "python-backend"]
    for names, message in [
        (["frontend"] * 2, "duplicate"),
        (["frontend", "python-backend", "security-review"], "at most"),
        (["unknown"], "unknown"),
        (["frontend\n"], "bad skill"),
    ]:
        with pytest.raises(sp.SpecError, match=message):
            sp.selected_skills(registry, spec, names)
    with pytest.raises(sp.SpecError, match="compatible"):
        sp.selected_skills(registry, registry.specs["docs"], ["frontend"])
    changed = dict(registry.skills)
    changed["frontend"] = dataclasses.replace(
        changed["frontend"], conflicts=("python-backend",)
    )
    with pytest.raises(sp.SpecError, match="conflicts"):
        sp.selected_skills(
            dataclasses.replace(registry, skills=changed),
            spec,
            ["frontend", "python-backend"],
        )


@pytest.fixture
def installed(tmp_path, registry):
    repo = tmp_path / "repo"
    shutil.copytree(sp.SPECS_ROOT, repo / "common" / "specializations")
    manifest = Path(__file__).parents[2] / "agents" / "dev" / "agent.json"
    (repo / "agents" / "dev").mkdir(parents=True)
    shutil.copyfile(manifest, repo / "agents" / "dev" / "agent.json")
    root = tmp_path / "installed"
    for kind, items in [
        ("spec", registry.specs.values()),
        ("skill", registry.skills.values()),
    ]:
        target = root / "dev" / "playbooks" / kind
        target.mkdir(parents=True, exist_ok=True)
        for item in items:
            (target / (item.name + ".md")).write_bytes(
                (sp.SPECS_ROOT / item.file).read_bytes()
            )
    return repo, root


class Cabinet:
    def __init__(self):
        self.card = {
            "id": "KD-1",
            "revision": 1,
            "assignee": "@dev",
            "labels": ["spec:implementer", "skill:frontend"],
            "notes": "",
        }

    def call(self, request):
        if request["op"] == "card.edit":
            if "notes_append" in request:
                notes = self.card["notes"]
                self.card["notes"] = (notes + "\n" if notes else "") + request["notes_append"]
            else:
                self.card.update(request["changes"])
            self.card["revision"] += 1
        return {"card": dict(self.card)}


def test_every_turn_rechecks_selection_and_installed_bytes(installed, registry):
    repo, root = installed
    cabinet = Cabinet()

    def turn():
        return ps.turn_instructions(
            cabinet,
            "dev",
            "KD-1",
            repo_root=repo,
            agents_root=root,
            require_root_owner=False,
        )

    text = turn()
    assert registry.skills["frontend"].text in text
    assert registry.skills["frontend"].sha256 in text
    assert "Read these installed files" in text
    assert "Do not ask the owner to choose internal instructions" in text
    assert "docs with technical-writing" in text
    assert "Compatible skills: localization, technical-writing" in text
    cabinet.card["labels"] = ["spec:implementer", "skill:security-review"]
    assert registry.skills["frontend"].text not in turn()
    assert registry.skills["security-review"].text in turn()
    (root / "dev/playbooks/skill/security-review.md").write_text(
        "changed", encoding="utf-8"
    )
    with pytest.raises(sp.SpecError, match="reinstall"):
        turn()


def test_selection_is_not_inherited_by_helper_or_chat(installed):
    repo, root = installed
    cabinet = Cabinet()
    cabinet.card["assignee"] = "@analyst"
    assert "not assigned" in ps.turn_instructions(
        cabinet,
        "dev",
        "KD-1",
        repo_root=repo,
        agents_root=root,
        require_root_owner=False,
    )
    assert (
        ps.turn_instructions(cabinet, "dev", "", repo_root=repo, agents_root=root) == ""
    )


def test_new_registry_version_requires_reinstall_before_the_next_turn(installed):
    repo, root = installed
    cabinet = Cabinet()

    def turn():
        return ps.turn_instructions(
            cabinet,
            "dev",
            "KD-1",
            repo_root=repo,
            agents_root=root,
            require_root_owner=False,
        )

    turn()
    unchanged_revision = cabinet.card["revision"]
    turn()
    assert cabinet.card["revision"] == unchanged_revision
    registry_root = repo / "common" / "specializations"
    index_path = registry_root / "specializations.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    original = registry_root / index["skills"]["frontend"]["file"]
    updated = registry_root / "skills/frontend.v2.md"
    updated.write_text(
        original.read_text(encoding="utf-8").replace(
            "# Skill: frontend (v1)", "# Skill: frontend (v2)", 1
        ),
        encoding="utf-8",
        newline="\n",
    )
    index["skills"]["frontend"].update(version=2, file="skills/frontend.v2.md")
    index_path.write_text(json.dumps(index), encoding="utf-8")
    with pytest.raises(sp.SpecError, match="reinstall"):
        turn()
    assert cabinet.card["revision"] == unchanged_revision
    shutil.copyfile(updated, root / "dev/playbooks/skill/frontend.md")
    current = sp.load_registry(registry_root, require_root_owner=False).skills[
        "frontend"
    ]
    text = turn()
    assert current.version == 2 and current.sha256 in text and current.text in text
    history = [
        line
        for line in cabinet.card["notes"].splitlines()
        if line.startswith("[instruction-selection] ")
    ]
    assert len(history) == 2
    assert (
        json.loads(history[-1].removeprefix("[instruction-selection] "))["skills"][
            "frontend"
        ]["version"]
        == 2
    )


@pytest.mark.parametrize(
    ("changes", "error"),
    [
        (
            {"assignee": "@analyst", "labels": ["spec:copywriting", "skill:python-backend"]},
            "not a specialization|compatible",
        ),
        (
            {"labels": ["spec:implementer", "skill:frontend", "skill:frontend"]},
            "duplicate skill",
        ),
        (
            {"labels": ["spec:implementer", " skill:frontend ", "skill:frontend"]},
            "duplicate skill",
        ),
    ],
)
def test_board_refuses_invalid_selection_changes_atomically(tmp_path, changes, error):
    from cabinet_env import CabinetEnv
    from kd.cabinet import client

    repo = tmp_path / "repo"
    fixtures = Path(__file__).parent / "fixtures/policy/agents"
    shutil.copytree(fixtures, repo / "agents")
    shutil.copytree(sp.SPECS_ROOT, repo / "common/specializations")
    env = CabinetEnv(tmp_path / "cabinet", repo_root=repo)
    card = env.card(assignee="@dev", labels=["spec:implementer", "skill:frontend"])
    root = env.client("root")
    with pytest.raises(client.CabinetError, match=error):
        root.call(
            {
                "op": "card.edit",
                "card": card.id,
                "revision": card.revision,
                "changes": changes,
            }
        )
    actual = env.store.card(card.id)
    assert actual.assignee == "@dev" and actual.revision == card.revision


class RecordingCabinet(Cabinet):
    def __init__(self, fail_edit=None):
        super().__init__()
        self.edits, self.fail_edit = [], fail_edit

    def call(self, request):
        if request["op"] == "card.edit":
            self.edits.append(request)
            if self.fail_edit:
                raise self.fail_edit
        return super().call(request)


def _turn(cabinet, installed, **kw):
    repo, root = installed
    return ps.turn_instructions(cabinet, "dev", "KD-1", repo_root=repo, agents_root=root,
                                require_root_owner=False, **kw)


def test_an_unchanged_selection_is_not_resent_to_the_same_process(installed, registry):
    """Review I3: the full texts go once per process and selection; later turns get the checked provenance."""
    cabinet, delivered = RecordingCabinet(), {}
    full = _turn(cabinet, installed, delivered=delivered)
    assert registry.skills["frontend"].text in full
    again = _turn(cabinet, installed, delivered=delivered)
    assert registry.skills["frontend"].text not in again
    assert registry.skills["frontend"].sha256 in again and "unchanged" in again
    assert registry.skills["frontend"].text in _turn(cabinet, installed, delivered={})  # a new or resumed process
    cabinet.card["labels"] = ["spec:implementer", "skill:security-review"]
    assert registry.skills["security-review"].text in _turn(cabinet, installed, delivered=delivered)


def test_the_provenance_is_appended_not_rewritten(installed):
    """Review I1: the notes are never rewritten whole (their size limit, a concurrent writer)."""
    cabinet = RecordingCabinet()
    cabinet.card["notes"] = "x" * 30000
    _turn(cabinet, installed)
    (edit,) = cabinet.edits
    assert "changes" not in edit and edit["notes_append"].startswith("[instruction-selection] ")


def test_a_failed_provenance_write_does_not_fail_the_turn(installed, registry, capsys):
    from kd.cabinet.client import CabinetError

    cabinet = RecordingCabinet(fail_edit=CabinetError("conflict", "revision"))
    text = _turn(cabinet, installed, delivered={})
    assert registry.skills["frontend"].text in text
    assert "provenance not recorded" in capsys.readouterr().err


def test_root_appends_to_the_notes_without_a_size_limit_on_the_rest(tmp_path):
    from cabinet_env import CabinetEnv

    env = CabinetEnv(tmp_path / "cabinet")
    card = env.card()
    card = env.store.edit_card(card.id, card.revision, {"notes_append": "y" * 25000}, actor="user:owner")
    root = env.client("root")
    root.call({"op": "card.edit", "card": card.id, "revision": card.revision, "notes_append": "[mark] 1"})
    assert env.store.card(card.id).notes.endswith("\n[mark] 1")
    with pytest.raises(Exception, match="notes_append"):
        root.call({"op": "card.edit", "card": card.id, "revision": card.revision + 1, "notes_append": "z" * 3000})


def _skills_env(tmp_path):
    from cabinet_env import CabinetEnv

    repo = tmp_path / "repo"
    shutil.copytree(Path(__file__).parent / "fixtures/policy/agents", repo / "agents")
    shutil.copytree(sp.SPECS_ROOT, repo / "common/specializations")
    return CabinetEnv(tmp_path / "cabinet", repo_root=repo)


def test_a_handoff_drops_the_skills_the_receiver_cannot_take(tmp_path):
    """Review I2: a skill: label travels like spec: — dropped in the same write, never a dead end."""
    env = _skills_env(tmp_path)
    card = env.card(status="In Progress", assignee="@dev", labels=["skill:frontend", "brief-ready"])
    env.call("digi", {"op": "worker.handoff", "agent": "dev", "card": card.id, "to": "@boss", "comment": "yours"})
    after = env.store.card(card.id)
    assert after.assignee == "@boss" and after.labels == ("brief-ready",)


def test_a_new_skill_on_a_receiver_that_cannot_take_it_is_still_refused(tmp_path):
    from kd.cabinet import client

    env = _skills_env(tmp_path)
    card = env.card(assignee="@dev", labels=[])
    with pytest.raises(client.CabinetError, match="compatible"):
        env.client("root").call({"op": "card.edit", "card": card.id, "revision": card.revision,
                                 "changes": {"assignee": "@boss", "labels": ["skill:frontend"]}})
    assert env.store.card(card.id).assignee == "@dev"


def test_a_skill_the_receiver_holds_travels_with_the_card(tmp_path):
    env = _skills_env(tmp_path)
    card = env.card(assignee="@boss", labels=[])
    env.client("root").call({"op": "card.edit", "card": card.id, "revision": card.revision,
                             "changes": {"assignee": "@dev", "labels": ["skill:frontend"]}})
    card = env.store.card(card.id)
    env.client("root").call({"op": "card.edit", "card": card.id, "revision": card.revision,
                             "changes": {"assignee": "@owner"}})
    assert env.store.card(card.id).labels == ("skill:frontend",)  # off the agents: kept for the next start
    # review I-1: the card off the agents stays editable with the skill it kept
    card = env.store.card(card.id)
    env.client("root").call({"op": "card.edit", "card": card.id, "revision": card.revision,
                             "changes": {"status": "In Progress"}})
    assert env.store.card(card.id).status == "In Progress"
    assert env.store.card(card.id).labels == ("skill:frontend",)
