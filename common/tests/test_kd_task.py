"""kd-task, the worker's board client, run as the base user against the cabinet."""
import io
import json
import shutil
import sys
from pathlib import Path

import os
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

from kd.cabinet import client, pages, probe_cards, snapshot  # noqa: E402
from kd.specializations import SPECS_ROOT, load_registry, note  # noqa: E402

FIXTURE_AGENTS = Path(__file__).resolve().parent / "fixtures" / "policy" / "agents"
REG = load_registry(SPECS_ROOT, require_root_owner=False)
SYSADMIN = {"KD_AGENT": "sysadmin", "KD_CALLER_USER": "digi-sysadmin"}
ANALYST = {"KD_AGENT": "analyst", "KD_CALLER_USER": "digi-analyst"}
BOSS = {"KD_AGENT": "boss", "KD_CALLER_USER": "digi-boss"}


class Env:
    """A cabinet store reached in-process as the base user (the executor principal)."""

    def __init__(self, tmp_path, mod):
        self.mod = mod
        self.repo = tmp_path / "repo"  # the deployed tree: manifests and the specialization registry
        for name in ("analyst", "boss", "dev", "sysadmin"):
            (self.repo / "agents" / name).mkdir(parents=True)
            shutil.copyfile(FIXTURE_AGENTS / name / "agent.json", self.repo / "agents" / name / "agent.json")
        shutil.copytree(SPECS_ROOT, self.repo / "common" / "specializations")
        # The agents' homes: dev holds the installed copies of its type's texts (new_agent.sh --install).
        self.agents_root = tmp_path / "agents-root"
        self.installed = self.agents_root / "dev" / "playbooks" / "spec"
        self.installed.mkdir(parents=True)
        for spec in REG.specs.values():
            if spec.type == "developer":
                shutil.copyfile(SPECS_ROOT / spec.file, self.installed / f"{spec.name}.md")
        self.cab = CabinetEnv(tmp_path / "cab", repo_root=self.repo)
        self.client = client.LocalClient(self.cab.ctx, self.cab.principal("digi"))
        self.env = {"KD_AGENT": "dev", "KD_CALLER_USER": "digi-dev"}

    def main(self, *argv, env=None):
        out, err = io.StringIO(), io.StringIO()
        rc = self.mod.main(list(argv), env=self.env if env is None else env, client=self.client,
                           registry_path=str(self.cab.registry), repo_root=str(self.repo), require_root_owner=False,
                           agents_root=str(self.agents_root), out=out, err=err)
        return rc, out.getvalue(), err.getvalue()

    def record(self, card_id):
        return self.cab.store.card(card_id)

    def changes(self):
        return [e for e in self.cab.store.events_since(0) if e["kind"] == "card.changed"]

    def created(self):
        return [e for e in self.cab.store.events_since(0) if e["kind"] == "card.created"]


@pytest.fixture
def env(tmp_path, kd_task):
    return Env(tmp_path, kd_task)


def test_approved_card_selects_checked_docs_and_writing_skill_without_owner_question(env):
    skill = REG.skills["technical-writing"]
    installed = env.agents_root / "dev/playbooks/skill"
    installed.mkdir()
    shutil.copyfile(SPECS_ROOT / skill.file, installed / "technical-writing.md")
    card = env.cab.card(assignee="@dev", labels=["spec:implementer"])
    rc, output, error = env.main("edit", card.id, "--spec", "docs", "--add-label", "skill:technical-writing")
    assert rc == 0, error
    actual = env.record(card.id)
    assert actual.status == "To Do" and set(actual.labels) == {"spec:docs", "skill:technical-writing"}
    assert REG.specs["docs"].text in output and skill.text in output
    assert skill.sha256 in output
    events = [m for m in env.cab.store.thread_messages(actual.thread) if m["payload"].get("activity")]
    assert len(events) == 1 and events[0]["payload"]["activity"]["state"] == "selected"
    assert events[0]["payload"]["activity"]["spec"] == "docs"


def test_selection_refuses_a_stale_installed_skill_without_changing_the_card(env):
    installed = env.agents_root / "dev/playbooks/skill"
    installed.mkdir()
    (installed / "technical-writing.md").write_text("stale", encoding="utf-8")
    card = env.cab.card(assignee="@dev", labels=["spec:implementer"])
    rc, _, error = env.main("edit", card.id, "--spec", "docs", "--add-label", "skill:technical-writing")
    assert rc == 2 and "reinstall" in error
    assert env.record(card.id).revision == card.revision


def test_instruction_selection_and_its_visible_event_are_atomic(env, monkeypatch):
    card = env.cab.card(assignee="@dev", labels=["spec:implementer"])
    def unavailable(*args, **kwargs):
        raise RuntimeError("event storage unavailable")
    monkeypatch.setattr(env.cab.store, "add_thread_message", unavailable)
    with pytest.raises(RuntimeError, match="event storage"):
        env.main("edit", card.id, "--spec", "docs")
    assert env.record(card.id).labels == card.labels and env.record(card.id).revision == card.revision


def test_claim_is_revision_checked_and_names_the_channel_agent(env):
    card = env.cab.card(assignee="")
    rc, out, _ = env.main("claim", card.id)
    assert rc == 0 and out == f"ok {card.id} claim revision {card.revision + 1} spec implementer v1\n"
    after = env.record(card.id)
    assert (after.status, after.assignee) == ("In Progress", "@dev")
    assert after.notes == f"[dev] {note(REG.specs['implementer'])}"
    assert [e["actor"] for e in env.changes()] == ["agent:dev"]


def test_claim_follows_the_label_and_refuses_a_foreign_or_double_one(env):
    card = env.cab.card(labels=["spec:test-author", "server:node-c"])
    rc, out, _ = env.main("claim", card.id)
    assert rc == 0 and out.endswith(" spec test-author v1\n")
    assert env.record(card.id).notes == f"[dev] {note(REG.specs['test-author'])}"
    for labels in (["spec:task-brief"], ["spec:bogus"], ["spec:reviewer", "spec:docs"]):
        other = env.cab.card(labels=labels)
        rc, _, err = env.main("claim", other.id)
        assert rc == 2 and "hand the card to @boss" in err
    assert len(env.changes()) == 1


def test_claim_without_a_specialization_writes_no_note(env):
    card = env.cab.card(assignee="@sysadmin", project="admin")
    rc, out, _ = env.main("claim", card.id, env=SYSADMIN)
    assert rc == 0 and out == f"ok {card.id} claim revision 2\n"
    assert env.record(card.id).notes == ""


def test_claim_decides_the_note_again_after_a_concurrent_label_change(env):
    card = env.cab.card()
    real, seen = env.client.call, []

    def racing(request):
        if request["op"] == "worker.claim" and not seen:
            seen.append(request["note"])  # BOSS sets the label meanwhile
            env.cab.store.edit_card(card.id, None, {"labels_add": ["spec:reviewer"]}, actor="agent:boss")
        return real(request)

    env.client.call = racing
    rc, out, _ = env.main("claim", card.id)
    assert rc == 0 and out.endswith(f" spec reviewer v{REG.specs['reviewer'].version}\n")
    assert seen == [note(REG.specs["implementer"])]  # the first decision was refused by the revision
    assert env.record(card.id).notes == f"[dev] {note(REG.specs['reviewer'])}"


def test_claim_rereads_after_a_conflict_and_gives_up_after_three(env):
    card = env.cab.card(labels=["spec:implementer"])
    calls = {"n": 0}
    real = env.client.call

    def racing(request):
        if request["op"] == "worker.claim":
            calls["n"] += 1
            env.cab.store.edit_card(card.id, None, {"notes_append": "x"}, actor="user:owner")
        return real(request)

    env.client.call = racing
    rc, out, err = env.main("claim", card.id)
    assert rc == 3 and calls["n"] == 3 and "changed 3 times" in err
    assert env.record(card.id).status == "To Do"


def test_claim_refuses_when_the_installed_text_is_not_the_registry_text(env):
    # The note records the hash of the text the agent follows — its installed copy.
    card = env.cab.card()
    (env.installed / "implementer.md").write_bytes(b"# Specialization: implementer (v1)\nold\n")
    rc, _, err = env.main("claim", card.id)
    assert rc == 1 and "reinstall" in err and "playbooks/spec/implementer.md" in err and env.changes() == []
    (env.installed / "implementer.md").unlink()
    rc, _, err = env.main("claim", card.id)
    assert rc == 1 and "reinstall" in err and env.changes() == []
    other = env.cab.card(assignee="@sysadmin")  # no specialization, nothing installed, nothing to compare
    assert env.main("claim", other.id, env=SYSADMIN)[0] == 0


def test_the_registry_is_read_only_when_a_specialization_is_involved(env):
    # claim always decides a specialization (the type's default), so it fails closed on a broken registry
    # before the board is written; the other operations need it only for a spec: label or --spec.
    (env.repo / "common" / "specializations" / "specializations.json").write_text("{", encoding="utf-8")
    card = env.cab.card()
    rc, _, err = env.main("claim", card.id)
    assert rc == 1 and "registry" in err and env.changes() == []
    mine = env.cab.card(status="In Progress")
    assert env.main("edit", mine.id, "--append-notes", "x")[0] == 0
    assert env.main("edit", mine.id, "--spec", "docs")[0] == 1
    assert env.main("handoff", mine.id, "--to", "@boss")[0] == 0
    labelled = env.cab.card(status="In Progress", labels=["spec:implementer"])
    assert env.main("handoff", labelled.id, "--to", "@boss")[0] == 1
    assert env.main("create", "Новая", "--project", "digipaddock", "--label", "a")[0] == 0
    assert env.main("create", "Новая", "--project", "digipaddock", "--label", "spec:docs")[0] == 1


def test_create_with_a_spec_label_checks_the_registry(env):
    rc, out, _ = env.main("create", "Новая", "--project", "digipaddock", "--label", "spec:docs")
    assert (rc, out) == (0, "ok KD-1 create\n")
    rc, _, err = env.main("create", "Новая", "--project", "digipaddock", "--label", "spec:task-brief")
    assert rc == 2 and "not a specialization of @dev" in err


def test_a_foreign_card_is_refused_before_its_label_is_judged(env):
    card = env.cab.card(assignee="@sysadmin", labels=["spec:task-brief"])
    rc, _, err = env.main("claim", card.id)
    assert rc == 2 and "already assigned" in err and "hand the card" not in err and env.changes() == []


def test_handoff_and_edit_spec_use_the_targets_registry(env):
    card = env.cab.card(status="In Progress", assignee="@sysadmin")
    rc, _, err = env.main("handoff", card.id, "--to", "@dev", "--spec", "task-brief", env=SYSADMIN)
    assert rc == 2 and "not a specialization of @dev" in err
    rc, out, _ = env.main("handoff", card.id, "--to", "@dev", "--spec", "reviewer", "--comment", "готово",
                          env=SYSADMIN)
    assert rc == 0 and "spec:reviewer" in env.record(card.id).labels
    inbox = env.cab.card(status="Inbox")
    assert env.main("edit", inbox.id, "--spec", "docs")[0] == 0
    assert env.main("edit", inbox.id, "--spec", "task-brief")[0] == 2


def test_claim_refusals_write_nothing(env):
    inbox = env.cab.card(status="Inbox")
    rc, _, err = env.main("claim", inbox.id)
    assert rc == 2 and "not in To Do" in err
    foreign = env.cab.card(assignee="@sysadmin")
    assert env.main("claim", foreign.id)[0] == 2
    assert env.main("claim", "KD-99")[0] == 2
    assert env.main("claim", "KD-7; rm -rf /")[0] == 2
    assert env.changes() == []


def test_edit_signs_and_moves_my_card(env):
    card = env.cab.card(status="In Progress", acceptance=["builds"])
    rc, out, _ = env.main("edit", card.id, "--status", "Review", "--append-notes", "PR open", "--check-ac", "1")
    assert (rc, out) == (0, f"ok {card.id} edit revision 2\n")
    after = env.record(card.id)
    assert (after.status, after.notes, after.acceptance[0].checked) == ("Review", "[dev] PR open", True)


def test_create_lands_in_inbox_and_prints_the_new_id(env):
    rc, out, _ = env.main("create", "Новая", "--project", "digipaddock", "--description", "зачем", "--label", "a",
                          "--to", "@boss")
    assert (rc, out) == (0, "ok KD-1 create\n")
    card = env.record("KD-1")
    assert (card.status, card.assignee, card.description, card.labels) == ("Inbox", "@boss", "[dev] зачем", ("a",))
    assert [e["actor"] for e in env.created()] == ["agent:dev"]


def test_handoff_is_one_edit(env):
    card = env.cab.card(status="In Progress", assignee="@sysadmin", labels=["spec:implementer"])
    rc, out, _ = env.main("handoff", card.id, "--to", "@dev", "--spec", "reviewer", "--comment", "готово",
                          env=SYSADMIN)
    assert (rc, out) == (0, f"ok {card.id} handoff -> @dev revision 2\n")
    after = env.record(card.id)
    assert (after.assignee, after.labels) == ("@dev", ("spec:reviewer",))
    (message,) = env.cab.store.messages(card.id)
    assert (message["author"], message["body"]) == ("agent:sysadmin",
                                                    "[sysadmin] handoff -> @dev (spec:reviewer): готово")


def test_handoff_with_a_label_is_still_one_edit(env):
    card = env.cab.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    rc, out, err = env.main("handoff", card.id, "--to", "@dev", "--spec", "implementer", "--add-label", "brief-ready",
                            "--comment", "ТЗ готово", env=ANALYST)
    assert (rc, out) == (0, f"ok {card.id} handoff -> @dev revision 2\n"), err
    after = env.record(card.id)
    assert (after.status, set(after.labels)) == ("Inbox", {"spec:implementer", "brief-ready"})
    assert env.cab.store.messages(card.id)[0]["body"] == "[analyst] handoff -> @dev (spec:implementer): ТЗ готово"


def test_a_concurrent_change_makes_the_executor_decide_again(env):
    card = env.cab.card()
    real = env.client.call

    def rival(request):
        if request["op"] == "worker.claim":
            env.cab.store.edit_card(card.id, None, {"assignee": "@sysadmin"}, actor="agent:boss")
        return real(request)

    env.client.call = rival
    rc, _, err = env.main("claim", card.id)
    assert rc == 2 and "already assigned to @sysadmin" in err and len(env.changes()) == 1


def test_read_commands_need_board_read_and_writes_need_board_write(env):
    card = env.cab.card(assignee="@boss", status="In Progress")
    rc, out, _ = env.main("view", card.id, env=BOSS)
    assert rc == 0 and card.id in out and "In Progress" in out
    rc, _, err = env.main("edit", card.id, "--comment", "hi", env=BOSS)
    assert rc == 2 and "board.write" in err and env.changes() == []


def test_view_shows_the_snapshot_and_scope_the_sections(env):
    card = env.cab.card(status="In Progress", description="зачем", acceptance=["builds"], labels=["a"])
    env.main("edit", card.id, "--comment", "начал")
    rc, out, _ = env.main("view", card.id)
    assert rc == 0
    assert out.splitlines()[0] == f"{card.id} · In Progress · @dev · Card [a]"
    assert f"@dev {env.cab.now} text: [dev] начал" in out and "## Description" not in out
    rc, out, _ = env.main("scope", card.id)
    assert rc == 0 and "## Description\n\nзачем" in out and "- [ ] #1 builds" in out


def test_view_prints_the_snapshot(env):
    card = probe_cards.kd9041_like(env.cab.store)
    rc, out, _ = env.main("view", card)
    assert rc == 0 and len(out.encode("utf-8")) <= snapshot.LIMIT
    assert "next (derived): turn_ended" in out and "## Comments" not in out and "Bash: python" not in out


def test_view_json(env):
    card = probe_cards.kd9041_like(env.cab.store)
    rc, out, _ = env.main("view", card, "--json")
    assert rc == 0 and out.count("\n") == 1 and len(out.encode("utf-8")) <= snapshot.LIMIT
    found = json.loads(out)
    assert found["v"] == 1 and found["next"]["what"] == "turn_ended" and found["prs"][0]["state"] == "merged"


def long_scope(env):
    description = "".join(f"Раздел {i}: поведение 🚀 и пределы.\n" for i in range(900))
    return env.cab.card(status="In Progress", description=description, acceptance=["Отчёт"])


def test_scope_reads_to_the_end_by_cursors(env):
    card = long_scope(env)
    pages_seen, argv = [], ["scope", card.id]
    while True:
        rc, out, _ = env.main(*argv)
        assert rc == 0 and len(out.encode("utf-8")) <= snapshot.LIMIT
        pages_seen.append(out)
        last = out.splitlines()[-1]
        if last.startswith("end ("):
            break
        assert last.startswith(f"more: kd-task scope {card.id} --cursor ")
        argv = last[len("more: kd-task "):].split()
    assert len(pages_seen) > 2 and "- [ ] #1 Отчёт" in pages_seen[-1]


def test_a_changed_scope_exits_3(env):
    card = long_scope(env)
    _, out, _ = env.main("scope", card.id)
    cursor = out.splitlines()[-1].split("--cursor ")[1]
    env.cab.store.edit_card(card.id, None, {"description": "другое"}, actor="user:owner")
    rc, _, err = env.main("scope", card.id, "--cursor", cursor)
    assert rc == 3 and f"kd-task scope {card.id}" in err


def test_bad_cursor_is_refused(env):
    card = long_scope(env)
    for cursor in ("nope", "abc:1", "0123456789ab:-1"):
        rc, _, err = env.main("scope", card.id, "--cursor", cursor)
        assert rc == 2 and "cursor" in err
    assert kd_task_cursor() == pages.CURSOR.pattern


def kd_task_cursor():
    import importlib.machinery
    import importlib.util
    path = str(Path(__file__).resolve().parents[1] / "bin" / "kd-task")
    spec = importlib.util.spec_from_loader("kd_task_probe", importlib.machinery.SourceFileLoader("kd_task_probe", path))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.pages.CURSOR.pattern


def test_thread_before_and_tools(env):
    card = probe_cards.kd9041_like(env.cab.store)
    rc, out, _ = env.main("thread", card)
    assert rc == 0 and out.startswith("start of thread") and out.rstrip().endswith("end of thread")
    rc, out, _ = env.main("thread", card, "--tools")
    first = out.splitlines()[0]
    assert rc == 0 and first.startswith(f"older: kd-task thread {card} --before ") and first.endswith(" --tools")
    rc, older, _ = env.main(*first[len("older: kd-task "):].split())
    assert rc == 0 and "· Bash: python" in older and len(older.encode("utf-8")) <= snapshot.LIMIT
    assert env.main("thread", card, "--before", "5", "--after", "1")[0] == 2


def test_message_and_result(env):
    card = env.cab.card(status="In Progress", acceptance=["Отчёт"])
    seq = env.cab.store.add_thread_message(card.thread, author="agent:dev", kind="text", body="итог " * 3000)["seq"]
    rc, out, _ = env.main("message", card.id, str(seq))
    assert rc == 0 and out.startswith(f"#{seq} @dev ") and f"more: kd-task message {card.id} {seq} --cursor " in out
    rc, out, _ = env.main("result", card.id)
    assert rc == 0 and "## Summary\n\n(none)" in out and "- [ ] #1 Отчёт" in out
    rc, _, err = env.main("message", card.id, str(seq + 50))
    assert rc == 2 and f"no message {seq + 50}" in err


def test_list_by_parent_and_deps(env):
    goal = env.cab.card(title="Goal")
    child = env.cab.store.edit_card(env.cab.card(title="Child").id, None, {"parent": goal.id}, actor="user:owner")
    dep = env.cab.card(title="Dep")
    blocked = env.cab.card(title="Blocked", dependencies=[dep.id])
    rc, out, _ = env.main("list", "--parent", goal.id)
    assert rc == 0 and out.splitlines() == [f"{child.id} · To Do · @dev · Child"]
    rc, out, _ = env.main("list", "--deps-of", blocked.id)
    assert rc == 0 and out.splitlines() == [f"{dep.id} · To Do · @dev · Dep"]


def test_list_and_search(env):
    env.cab.card(title="Alpha one")
    env.cab.card(title="Beta", status="Review")
    rc, out, _ = env.main("list", "-s", "Review")
    assert rc == 0 and out.splitlines() == ["KD-2 · Review · @dev · Beta"]
    assert "KD-1" in env.main("search", "alpha")[1]
    assert env.main("list", "-a", "@nobody") == (0, "", "")


def test_projects_lists_the_cabinets_table_and_a_new_one_counts_at_once(env):
    # kd-task projects (id · name · route); a project the table gains is taken by create without a restart
    rc, out, _ = env.main("projects")
    assert rc == 0 and out.splitlines() == [
        "admin · ADMIN · @sysadmin", "alert · ALERT · @owner", "digipaddock · DIGIPADDOCK · @dev", "web · WEB · @owner",
    ]
    assert env.main("create", "Сайт: форма", "--project", "site")[0] == 2
    env.cab.store.seed_projects([{"id": "site", "name": "Сайт", "route": "@dev"}])
    assert "site · Сайт · @dev" in env.main("projects")[1]
    rc, out, err = env.main("create", "Сайт: форма", "--project", "site")
    assert rc == 0, err
    card = env.record(out.split()[1])
    assert (card.project, card.status) == ("site", "Inbox")


def test_projects_without_the_cabinet_is_an_error(env):
    env.client.call = lambda request: (_ for _ in ()).throw(client.CabinetError("unavailable", "board unavailable"))
    rc, out, err = env.main("projects")
    assert (rc, out) == (1, "") and "board unavailable" in err


def test_board_down_is_an_error_not_a_traceback(env):
    env.client.call = lambda request: (_ for _ in ()).throw(client.CabinetError("unavailable", "board unavailable"))
    rc, _, err = env.main("list")
    assert rc == 1 and "board unavailable" in err


@pytest.mark.parametrize("channel", [{}, {"KD_AGENT": "dev", "KD_CALLER_USER": ""},
                                     {"KD_AGENT": "dev", "KD_CALLER_USER": "digi-sysadmin"},
                                     {"KD_AGENT": "probe", "KD_CALLER_USER": "digi"}])
def test_the_channel_names_the_caller_or_nothing_runs(env, channel):
    card = env.cab.card()
    rc, _, _ = env.main("claim", card.id, env=channel)
    assert rc == 2 and env.changes() == []
    assert env.main("view", card.id, env=channel)[0] == 2


@pytest.mark.parametrize("argv", [["claim"], ["edit", "KD-1"], ["edit", "KD-1", "--title", "x"],
                                  ["edit", "KD-1", "--if-revision", "x"], ["create", "x"], ["handoff", "KD-1"],
                                  ["view"], ["search"], ["list", "--bogus"], ["nope"]])
def test_usage_and_unknown_flags_are_refused(env, argv):
    env.cab.card(status="In Progress")
    rc, _, _ = env.main(*argv)
    assert rc == 2 and env.changes() == []


def test_reexec_goes_through_the_base_channel(kd_task):
    assert kd_task.reexec_argv(["claim", "KD-7"]) == ["/opt/digipaddock/common/bin/kd-cap", "run", "base",
                                                      "/opt/digipaddock/common/bin/kd-task", "claim", "KD-7"]


def test_edit_sends_the_revision_the_agent_read(env):
    """The description is written only over the revision the agent read."""
    card = env.cab.card(status="Inbox", assignee="@analyst")
    rc, out, err = env.main("edit", card.id, "--revision", "1", "--description", "ТЗ", env=ANALYST)
    assert (rc, out) == (0, f"ok {card.id} edit revision 2\n"), err
    env.cab.store.edit_card(card.id, 2, {"description": "владелец поправил"}, actor="user:owner")
    rc, _, err = env.main("edit", card.id, "--revision", "2", "--description", "старое", env=ANALYST)
    assert rc == 3 and f"kd-task view {card.id}" in err
    assert env.record(card.id).description == "владелец поправил"
    rc, _, err = env.main("edit", card.id, "--description", "без ревизии", env=ANALYST)
    assert rc == 2 and "--revision" in err


def test_handoff_takes_the_revision_too(env):
    card = env.cab.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    rc, _, err = env.main("handoff", card.id, "--revision", "7", "--to", "@dev", "--spec", "implementer", env=ANALYST)
    assert rc == 3 and "changed since revision 7" in err


def test_chat_prints_after_seq(env):
    store = env.cab.store
    thread = store.agent_thread("analyst")
    for text in ("first", "second", "third"):
        store.add_thread_message(thread, author="user:owner", kind="text", body=text)
    rc, out, _ = env.main("chat", env=ANALYST)
    assert rc == 0 and [line.split(": ", 1)[1] for line in out.splitlines()] == ["first", "second", "third"]
    assert out.startswith("#1 user:owner ")
    rc, out, _ = env.main("chat", "--after", "2", env=ANALYST)
    assert rc == 0 and out.splitlines() == [f"#3 user:owner {env.cab.now}: third"]


def test_chat_in_a_monitor_session_reads_monitor_and_answers_in_its_turn(env):
    """The channel's session of the call picks the thread — "Monitor" in its own session; a post there is
    refused: the turn's text is the answer, a post would be a second message."""
    store = env.cab.store
    command = store.enqueue({"key": "m1", "agent": "analyst", "thread": store.agent_thread("analyst", "monitor"),
                             "card": "", "kind": "signal", "text": "[монитор] x", "initiator": "digi-analyst"})
    session = store.session_for(command["thread"], "analyst")
    store.add_thread_message(command["thread"], author="user:owner", kind="text", body="в мониторе")
    store.add_thread_message(store.agent_thread("analyst"), author="user:owner", kind="text", body="в чате")
    in_monitor = {**ANALYST, "KD_CALLER_SESSION": session["id"][:12]}
    rc, out, _ = env.main("chat", env=in_monitor)
    assert rc == 0 and out.rstrip().endswith("в мониторе") and "в чате" not in out
    rc, out, err = env.main("chat", "--post", "смотрю", env=in_monitor)
    assert rc != 0 and "your turn's text" in err
    assert store.thread_messages(command["thread"])[-1]["body"] == "в мониторе"
    rc, out, _ = env.main("chat", env=ANALYST)
    assert rc == 0 and "в чате" in out and "в мониторе" not in out


def test_chat_in_chat_reaches_the_chat_from_a_monitor_session(env):
    """The coordinator's attention tick runs in its "Monitor"; what the owner needs it reads and writes in
    its chat — only when it says so."""
    store = env.cab.store
    command = store.enqueue({"key": "a1", "agent": "boss", "thread": store.agent_thread("boss", "monitor"),
                             "card": "", "kind": "signal", "text": "[attention] x", "initiator": "root"})
    session = store.session_for(command["thread"], "boss")
    store.add_thread_message(store.agent_thread("boss"), author="user:owner", kind="text", body="KD-9 отложи")
    in_monitor = {**BOSS, "KD_CALLER_SESSION": session["id"][:12]}
    rc, out, err = env.main("chat", "--in-chat", env=in_monitor)
    assert rc == 0 and out.rstrip().endswith("KD-9 отложи"), err
    rc, out, err = env.main("chat", "--in-chat", "--post", "KD-9 ждёт вашего решения", env=in_monitor)
    assert (rc, out) == (0, "ok chat #2\n"), err
    last = store.thread_messages(store.agent_thread("boss"))[-1]
    assert (last["author"], last["body"]) == ("agent:boss", "KD-9 ждёт вашего решения")
    assert all(m["author"] != "agent:boss" for m in store.thread_messages(command["thread"]))
    rc, out, err = env.main("chat", "--in-chat", "--message", "1", env=in_monitor)
    assert rc == 0 and "KD-9 отложи" in out, err


def test_chat_post_is_authored_by_the_agent(env):
    rc, out, _ = env.main("chat", "--post", "Готово, ТЗ в карточке", env=ANALYST)
    assert rc == 0 and out == "ok chat #1\n"
    [message] = env.cab.store.thread_messages(env.cab.store.agent_thread("analyst"))
    assert message["author"] == "agent:analyst" and message["body"] == "Готово, ТЗ в карточке"


def test_chat_needs_only_board_read(env):
    """the coordinator holds board.read without board.write and still answers in its own chat."""
    store = env.cab.store
    store.add_thread_message(store.agent_thread("boss"), author="user:owner", kind="text", body="как дела?")
    rc, out, err = env.main("chat", env=BOSS)
    assert rc == 0 and out.endswith(": как дела?\n"), err
    rc, out, err = env.main("chat", "--post", "Всё идёт", env=BOSS)
    assert (rc, out) == (0, "ok chat #2\n"), err
    assert store.thread_messages(store.agent_thread("boss"))[-1]["author"] == "agent:boss"


def test_chat_long_message_reads_in_full(env):
    """A chat line cuts a long message; the cut names the command that reads it whole, page by page."""
    store = env.cab.store
    text = "\n".join(f"строка {i} " + "ж" * 80 for i in range(120))  # ~20 KB: three pages
    store.add_thread_message(store.agent_thread("boss"), author="user:owner", kind="text", body=text)
    rc, out, _ = env.main("chat", env=BOSS)
    assert rc == 0 and out.rstrip("\n").endswith("… (kd-task chat --message 1)")
    argv, pieces = ["chat", "--message", "1"], []
    while True:
        rc, out, err = env.main(*argv, env=BOSS)
        assert rc == 0 and len(out.encode("utf-8")) <= snapshot.LIMIT, err
        body, last = out.rstrip("\n").rsplit("\n", 1)
        pieces.append(body + "\n")
        if not last.startswith("more: "):
            assert last.startswith("end (")
            break
        assert last.startswith("more: kd-task chat --message 1 --cursor ")
        argv = ["chat", "--message", "1", "--cursor", last.split()[-1]]
    assert len(pieces) > 1
    assert "".join(pieces) == f"#1 user:owner {env.cab.now} text\n{text}\n"
    rc, _, err = env.main("chat", "--message", "9", env=BOSS)
    assert rc == 2 and "no message 9" in err
    store.add_thread_message(store.agent_thread("dev"), author="user:owner", kind="text", body="for dev")
    rc, out, _ = env.main("chat", "--message", "1", env=ANALYST)
    assert rc == 2 and "for dev" not in out  # the agent's own chat only, as the channel names it


def test_chat_other_agents_chat_refused(env):
    store = env.cab.store
    store.add_thread_message(store.agent_thread("dev"), author="user:owner", kind="text", body="for dev")
    rc, out, err = env.main("chat", "--agent", "dev", env=ANALYST)
    assert rc == 2 and "for dev" not in out  # the agent comes from the channel, never from an argument
    rc, out, _ = env.main("chat", env=ANALYST)
    assert rc == 0 and out == ""


def test_list_is_bounded_and_pages_by_after(env):
    ids = [env.cab.card(title="Длинная карточка " + "д" * 100, status="In Progress").id for _ in range(80)]
    seen, argv = [], ["list", "-s", "In Progress"]
    while True:
        rc, out, _ = env.main(*argv)
        assert rc == 0 and len(out.encode("utf-8")) <= snapshot.LIMIT
        lines = out.splitlines()
        seen += [line.split(" · ")[0] for line in lines if line.startswith("KD-")]
        if not lines[-1].startswith("more: "):
            break
        assert lines[-1].startswith("more: kd-task list -s 'In Progress' --after KD-")
        argv = ["list", "-s", "In Progress", "--after", lines[-1].rsplit(" ", 1)[1]]
    assert seen == ids


def test_search_and_chat_are_bounded(env):
    for _ in range(50):
        env.cab.card(title="Поиск " + "п" * 110)
    rc, out, _ = env.main("search", "Поиск")
    assert rc == 0 and len(out.encode("utf-8")) <= snapshot.LIMIT and "narrow the search" in out.splitlines()[-1]
    for i in range(20):
        assert env.main("chat", "--post", f"{i} " + "ч" * 3000)[0] == 0
    rc, out, _ = env.main("chat")
    assert rc == 0 and len(out.encode("utf-8")) <= snapshot.LIMIT and " 19 " in out.splitlines()[-1]


def test_zero_is_a_value_not_a_missing_flag(env):
    card = env.cab.card(status="In Progress")
    for i in range(40):
        env.cab.store.add_thread_message(card.thread, author="user:owner", kind="text", body=f"m{i} " + "ё" * 400)
    rc, out, _ = env.main("thread", card.id, "--after", "0")
    assert rc == 0 and out.startswith("start of thread") and "newer: " in out
    rc, _, err = env.main("message", card.id, "0")
    assert rc == 2 and "no message 0" in err


def test_waits_form(env):
    from kd.cabinet import decisions

    card = env.cab.card(status="In Progress")
    store = env.cab.store
    asked = decisions.Decisions(store).open("question", card=card.id, payload={"text": "?"},
                                            requested_by="agent:dev", now=env.cab.now)["id"]
    store._conn.execute(
        "INSERT INTO turn_outcomes (command, card, thread, agent, outcome, mode, note, basis, state, created, updated)"
        " VALUES ('c1',?,?,'dev','wait_dependency','all','n','{}','final',?,?)",
        (card.id, card.thread, env.cab.now, env.cab.now))
    for i in range(150):
        store._conn.execute(
            "INSERT INTO waits (id, command, card, agent, kind, ref, blocking, mode, state, created, updated)"
            " VALUES (?,'c1',?,'dev','card',?,1,'all','open',?,?)",
            (f"w{i:03d}", card.id, f"KD-{1000 + i}", f"2026-10-14T09:{i // 60:02d}:{i % 60:02d}Z", env.cab.now))
    seen, argv = [], ["waits", card.id]
    while True:
        rc, out, _ = env.main(*argv)
        assert rc == 0 and len(out.encode("utf-8")) <= snapshot.LIMIT
        lines = out.splitlines()
        seen += [line for line in lines[1:-1]]
        if lines[-1].startswith("end ("):
            break
        assert lines[-1].startswith(f"more: kd-task waits {card.id} --after ")
        argv = lines[-1][len("more: kd-task "):].split()
    assert lines[-1] == "end (151 items)" and len(seen) == 151
    assert seen[0].startswith("wait card KD-1149 open blocking by @dev since 2026-10-14T09:02:29Z")
    assert seen[-1] == f"decision question {asked} open by @dev since {env.cab.now}"
    rc, _, err = env.main("waits", card.id, "--after", "x")
    assert rc == 2


def test_edit_stages_is_a_scope_change_the_cabinet_checks(env):
    """The analyst's --stages goes with the revision it read; the cabinet checks the plan."""
    card = env.cab.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    stage = {"key": "a", "title": "Модель", "role": "@dev", "spec": "implementer", "description": "Схема.",
             "acceptance": ["Тест проходит"], "depends": []}
    rc, _, err = env.main("edit", card.id, "--stages", json.dumps([stage]), env=ANALYST)
    assert rc == 2 and "--revision" in err
    rc, out, err = env.main("edit", card.id, "--revision", "1", "--stages", json.dumps([stage]), env=ANALYST)
    assert (rc, out) == (0, f"ok {card.id} edit revision 2\n"), err
    assert env.record(card.id).stages[0]["key"] == "a"
    rc, _, err = env.main("edit", card.id, "--revision", "2", "--stages", json.dumps([{**stage, "acceptance": []}]),
                          env=ANALYST)
    assert rc == 2 and "criterion" in err
    rc, _, err = env.main("edit", card.id, "--revision", "2", "--stages", "{}", env=ANALYST)
    assert rc == 2 and "JSON list" in err


def test_edit_empty_operations_and_stages_reach_the_cabinet(env):
    """``--operations '[]'`` and ``--stages '[]'`` clear the brief's steps instead of being dropped."""
    card = env.cab.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    stage = {"key": "a", "title": "Модель", "role": "@dev", "spec": "implementer", "description": "Схема.",
             "acceptance": ["Тест проходит"], "depends": []}
    step = {"verb": "agent.reinstall", "target": "factory", "agents": ["dev"], "what": "w", "effect": "e",
            "risk": {"level": "low", "why": "y"}, "rollback": "r"}
    catalog = env.repo / "common" / "coordination" / "factory.json"
    catalog.parent.mkdir(parents=True)
    shutil.copyfile(Path(__file__).resolve().parents[1] / "coordination" / "factory.json", catalog)
    rc, _, err = env.main("edit", card.id, "--revision", "1", "--stages", json.dumps([stage]),
                          "--operations", json.dumps([step]), env=ANALYST)
    assert rc == 0, err
    record = env.record(card.id)
    assert record.stages and record.operations
    rc, _, err = env.main("edit", card.id, "--revision", str(record.revision), "--stages", "[]",
                          "--operations", "[]", env=ANALYST)
    assert rc == 0, err
    record = env.record(card.id)
    assert (tuple(record.stages), tuple(record.operations)) == ((), ())
    _, _, request = env.mod.parse(["edit", card.id, "--stages", "[]", "--operations", "[]"])
    assert request == {"stages": [], "operations": []}
    _, _, request = env.mod.parse(["edit", card.id, "--comment", "x"])
    assert request == {"comment": "x"}  # the repeated flags' empty defaults still stay out


def test_claim_refuses_an_installed_text_under_a_linked_directory(env, tmp_path):
    """Review M4: claim checks the installed copy as the session host does — a linked directory is refused."""
    import shutil

    card = env.cab.card()
    elsewhere = tmp_path / "elsewhere"
    shutil.copytree(env.installed, elsewhere)
    shutil.rmtree(env.installed)
    try:
        os.symlink(elsewhere, env.installed, target_is_directory=True)
    except OSError:
        pytest.skip("no symlinks here")
    rc, _, err = env.main("claim", card.id)
    assert rc == 1 and "reinstall" in err and env.changes() == []


def test_list_knows_no_milestone_filter(env):
    # milestones are gone, and with them the filter
    rc, out, err = env.main("list", "-m", "m-1")
    assert rc == 2 and out == "" and "-m" in err
