"""kd-boss-board, the coordinator's board executor, run as the base user against the cabinet."""
import dataclasses
import io
import json
import os
import re
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

from kd.cabinet import client  # noqa: E402
from kd.specializations import SPECS_ROOT  # noqa: E402

FIXTURE_AGENTS = Path(__file__).resolve().parent / "fixtures" / "policy" / "agents"
NOW = 1_800_000_000.0
EVENTS = [
    {"id": "a" * 32, "state": "pending", "level": "attention", "rule": "blocked", "subject": "KD-7",
     "revision": "5", "created": "2026-10-08T09:00:00Z"},
    {"id": "b" * 32, "state": "pending", "level": "attention", "rule": "panel_absent", "subject": "sysadmin",
     "revision": "2026-10-08T08:00:00Z", "created": "2026-10-08T09:00:00Z"},
]


class Spy:
    """The executor's client, with a hook before a write and a log of the calls."""

    def __init__(self, inner):
        self.inner, self.calls, self.before_write = inner, [], None

    def call(self, request):
        self.calls.append(request)
        if self.before_write and not request["op"].startswith(("card.get", "attention.", "project.list")):
            hook, self.before_write = self.before_write, None
            hook(request)
        return self.inner.call(request)


class Env:
    def __init__(self, tmp_path, mod):
        self.mod = mod
        self.repo = tmp_path / "repo"  # the deployed tree: manifests and the specialization registry
        for name in ("analyst", "boss", "dev", "sysadmin"):
            (self.repo / "agents" / name).mkdir(parents=True)
            shutil.copyfile(FIXTURE_AGENTS / name / "agent.json", self.repo / "agents" / name / "agent.json")
        shutil.copytree(SPECS_ROOT, self.repo / "common" / "specializations")
        self.cab = CabinetEnv(tmp_path / "cab", repo_root=self.repo)
        self.client = Spy(client.LocalClient(self.cab.ctx, self.cab.principal("digi")))
        self.audit = tmp_path / "boss.jsonl"
        self.tmp = tmp_path

    def main(self, *argv, **over):
        out, err = io.StringIO(), io.StringIO()
        cabinet_json = self.tmp / "cabinet.json"
        cabinet_json.write_text('{"schema_version": 1, "origin": "https://cab.example"}', encoding="utf-8")
        kw = dict(client=self.client, registry_path=str(self.cab.registry),
                  audit_path=str(self.audit), caller="digi-boss", now=lambda: NOW, repo_root=str(self.repo),
                  require_root_owner=False, out=out, err=err, cabinet_json=str(cabinet_json))
        kw.update(over)
        if "proposals" in over:  # the cabinet checks a decision against the same proposal store
            self.cab.ctx.promote = dataclasses.replace(self.cab.promote, proposals=over["proposals"])
        return self.mod.main(list(argv), **kw), out.getvalue(), err.getvalue()

    def record(self, card_id):
        return self.cab.store.card(card_id)

    def writes(self):
        """The board writes of the code under test (the fixtures write as the owner)."""
        return [e for e in self.cab.store.events_since(0)
                if e["kind"] in ("card.changed", "card.created") and e["actor"] != "user:owner"]

    def audit_records(self):
        if not self.audit.exists():
            return []
        return [json.loads(line) for line in self.audit.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def env(tmp_path, kd_boss_board):
    return Env(tmp_path, kd_boss_board)


def test_assign_goes_through_the_cabinet_with_the_given_revision(env):
    card = env.cab.card(status="Inbox", assignee="")
    rc, out, err = env.main("assign", "--card", card.id, "--revision", str(card.revision), "--reason", "r",
                            "--assignee", "@dev")
    assert rc == 0 and env.record(card.id).assignee == "@dev"
    rc, _, err = env.main("assign", "--card", card.id, "--revision", str(card.revision), "--reason", "r",
                          "--assignee", "@sysadmin")
    assert rc == 3 and "re-read" in err  # stale revision


def test_assign_runs_one_revision_checked_write_and_audits_both_revisions(env):
    card = env.cab.card()
    rc, out, err = env.main("assign", "--card", card.id, "--revision", str(card.revision), "--assignee", "@sysadmin",
                            "--reason", "работа на серверах")
    assert rc == 0 and err == ""
    assert out == f"ok {card.id} assign revision {card.revision + 1}\n"
    (write,) = env.writes()
    assert (write["actor"], write["card"]) == ("agent:boss", card.id)
    after = env.record(card.id)
    assert after.assignee == "@sysadmin"
    assert [m["author"] for m in env.cab.store.messages(card.id)] == ["agent:boss"]
    intent, result = env.audit_records()
    assert (intent["phase"], intent["revision"], intent["caller"]) == ("intent", card.revision, "digi-boss")
    assert intent["reason"] == "работа на серверах" and intent["id"] == result["id"]
    assert (result["phase"], result["status"], result["newRevision"]) == ("result", 0, card.revision + 1)
    # the typed call carries no actor: the cabinet names BOSS from the channel's executor
    (sent,) = [c for c in env.client.calls if c["op"].startswith("boss.")]
    assert sent["op"] == "boss.assign" and sent["agent"] == "boss" and "actor" not in sent


def test_assign_with_spec_is_one_audited_write(env):
    card = env.cab.card()
    rc, out, _ = env.main("assign", "--card", card.id, "--revision", str(card.revision), "--assignee", "@dev",
                          "--spec", "reviewer", "--reason", "ревью")
    assert rc == 0 and out.startswith(f"ok {card.id} assign revision ")
    assert "spec:reviewer" in env.record(card.id).labels and len(env.writes()) == 1
    fresh = env.cab.card()  # a fresh card: the refusal below is about the label, not the revision
    rc, _, err = env.main("assign", "--card", fresh.id, "--revision", str(fresh.revision), "--assignee", "@dev",
                          "--spec", "task-brief", "--reason", "x")
    assert rc == 2 and "not a specialization of @dev" in err and len(env.writes()) == 1


def test_specs_lists_the_installed_specializations_per_agent(env):
    rc, out, _ = env.main("specs")
    assert rc == 0 and out == (
        "@analyst: task-brief (default), brief-critic, brief-review, commit-review, copywriting, cost-report,"
        " data-health, onboarding, release-check, support-draft, ux-review\n@boss: —\n"
        "@dev: implementer (default), code-critic, docs, reviewer, site-copy, test-author, tester, ui-design\n"
        "@sysadmin: capacity, drift-audit, incident-triage, post-deploy, vpn-deploy\n"
    )
    assert env.client.calls == [] and env.audit_records() == []


def test_assign_without_spec_drops_the_label_the_target_cannot_follow(env):
    card = env.cab.card(labels=["spec:test-author"])
    rc, out, _ = env.main("assign", "--card", card.id, "--revision", str(card.revision), "--assignee", "@sysadmin",
                          "--reason", "серверная работа")
    assert rc == 0 and out.startswith(f"ok {card.id} assign revision ")
    after = env.record(card.id)
    assert after.assignee == "@sysadmin" and "spec:test-author" not in after.labels


def test_a_broken_registry_refuses_assign_only_when_a_specialization_is_involved(env):
    (env.repo / "common" / "specializations" / "specializations.json").write_text("{", encoding="utf-8")
    card = env.cab.card(assignee="")
    rc, _, err = env.main("assign", "--card", card.id, "--revision", str(card.revision), "--assignee", "@dev",
                          "--spec", "reviewer", "--reason", "x")
    assert rc == 1 and "registry" in err and env.writes() == []
    rc, _, _ = env.main("assign", "--card", card.id, "--revision", str(card.revision), "--assignee", "@dev",
                        "--reason", "x")
    assert rc == 0  # no --spec and no spec: label: the registry is not needed
    labelled = env.cab.card(assignee="", labels=["spec:reviewer"])
    rc, _, err = env.main("assign", "--card", labelled.id, "--revision", str(labelled.revision), "--assignee", "@dev",
                          "--reason", "x")
    assert rc == 1 and "registry" in err and len(env.writes()) == 1  # a labelled card needs the registry


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["edit", "--card", "KD-1"],
        ["set-status", "--card", "KD-1", "--revision", "1", "--reason", "x"],
        ["comment", "--card", "KD-1", "--revision", "1"],
        ["comment", "--card", "KD-1", "--revision", "1", "--reason", "x", "extra"],
        ["comment", "--card", "KD-1", "--revision", "1", "--reason", "x", "--status", "Done"],
        ["comment", "--card", "KD-1", "--rev", "1", "--reason", "x"],
        ["comment", "--card", "KD-1", "--revision", "a" * 64, "--reason", "x"],
        ["comment", "--card", "KD-1", "--revision", "01", "--reason", "x"],
        ["create-inbox", "--title", "x", "--project", "digipaddock", "--reason", "x", "--status", "To Do"],
        ["assign", "--card", "KD-1; rm -rf /", "--revision", "1", "--assignee", "@sysadmin", "--reason", "x"],
        ["assign", "--card", "$(id)", "--revision", "1", "--assignee", "@sysadmin", "--reason", "x"],
        ["--help"],
    ],
)
def test_unknown_operations_extra_args_bad_revisions_and_shell_are_refused(env, argv):
    env.cab.card()
    rc, out, err = env.main(*argv)
    assert rc == 2 and out == "" and env.writes() == [] and env.audit_records() == []


def test_an_unreachable_cabinet_is_an_error_and_nothing_is_audited(env):
    card = env.cab.card()

    class Down:
        def call(self, request):
            raise client.CabinetError("unavailable", "board unavailable (FileNotFoundError)")

    rc, _, err = env.main("comment", "--card", card.id, "--revision", str(card.revision), "--reason", "x",
                          client=Down())
    assert rc == 1 and "unavailable" in err and env.audit_records() == []


def test_stale_revision_is_refused_before_any_write_or_audit(env):
    card = env.cab.card()
    env.cab.store.edit_card(card.id, card.revision, {"status": "In Progress"}, actor="user:owner")
    rc, _, err = env.main("comment", "--card", card.id, "--revision", str(card.revision), "--reason", "x")
    assert rc == 3 and "re-read" in err and env.audit_records() == []
    assert [c["op"] for c in env.client.calls] == ["project.list", "card.get"]  # the routes are read first


def test_card_changed_between_read_and_write_is_a_conflict_with_audit(env):
    card = env.cab.card()
    # someone edits after the executor's checks: the cabinet decides again under its lock
    env.client.before_write = lambda _: env.cab.store.edit_card(card.id, card.revision, {"status": "In Progress"},
                                                                actor="user:owner")
    rc, _, err = env.main("comment", "--card", card.id, "--revision", str(card.revision), "--reason", "x")
    assert rc == 3 and "changed" in err
    assert env.cab.store.messages(card.id) == []
    assert [r["phase"] for r in env.audit_records()] == ["intent", "result"]
    assert env.audit_records()[1]["status"] == 3


def test_a_board_failure_during_the_write_is_audited_and_sanitised(env):
    card = env.cab.card()

    class Flaky:
        def call(self, request):
            if request["op"] in ("card.get", "project.list"):
                return env.client.call(request)
            raise client.CabinetError("unavailable", "\x1b[31mbusy\x1b[0m")

    rc, _, err = env.main("comment", "--card", card.id, "--revision", str(card.revision), "--reason", "x",
                          client=Flaky())
    assert rc == 1 and "\x1b" not in err and "busy" in err
    assert [(r["phase"], r.get("status")) for r in env.audit_records()] == [("intent", None), ("result", 1)]


def test_audit_failure_writes_nothing_to_the_board(env, tmp_path):
    card = env.cab.card()
    rc, _, err = env.main("comment", "--card", card.id, "--revision", str(card.revision), "--reason", "x",
                          audit_path=str(tmp_path / "missing-dir" / "boss.jsonl"))
    assert rc == 1 and "audit" in err and env.writes() == []


def test_a_recreated_audit_journal_keeps_the_group_class_for_the_approval_service(tmp_path, monkeypatch):
    # Under the directory's default ACL the create mode's group bits become the ACL mask, so the
    # service's inherited u:<approve>:rw stays effective on a journal the board owner recreates
    from kd import boss_exec

    modes = []
    real = os.open

    def spy(path, flags, mode=0o777, *a, **kw):
        modes.append(mode)
        return real(path, flags, mode, *a, **kw)

    monkeypatch.setattr(boss_exec.os, "open", spy)
    boss_exec.Audit(str(tmp_path / "boss.jsonl")).close()
    assert modes == [0o660]


def test_rate_limits_hourly_changes_and_daily_cards(env, kd_boss_board):
    card = env.cab.card()
    comment = ("comment", "--card", card.id, "--revision", str(card.revision), "--reason", "x")
    old = [{"phase": "intent", "at": NOW - 600, "op": "comment"}] * kd_boss_board.PER_HOUR
    env.audit.write_text("".join(json.dumps(r) + "\n" for r in old), encoding="utf-8")
    rc, _, err = env.main(*comment)
    assert rc == 5 and env.writes() == []
    older = [{"phase": "intent", "at": NOW - 7200, "op": "comment"}] * kd_boss_board.PER_HOUR
    env.audit.write_text("".join(json.dumps(r) + "\n" for r in older), encoding="utf-8")
    assert env.main(*comment)[0] == 0
    cards = [{"phase": "intent", "at": NOW - 7200, "op": "create-inbox"}] * kd_boss_board.CREATES_PER_DAY
    env.audit.write_text("garbage\n" + "".join(json.dumps(r) + "\n" for r in cards), encoding="utf-8")
    rc, _, _ = env.main("create-inbox", "--title", "x", "--project", "digipaddock", "--reason", "x",
                        "--token", "f" * 32)
    assert rc == 5


def _look(env, intent, project):
    """prepare-create first; the token and the candidate ids it printed."""
    rc, out, err = env.main("prepare-create", "--intent", intent, "--project", project)
    assert rc == 0, err
    lines = out.splitlines()
    return lines[0].split()[1], [line.split()[0] for line in lines[1:]]


def test_create_inbox_reports_new_card_and_revision(env):
    env.cab.card()
    token, seen = _look(env, "Разбить на части", "web")
    rc, out, err = env.main("create-inbox", "--title", "Разбить KD-1", "--project", "web", "--reason",
                            "заявка владельцу", "--token", token, *[a for c in seen for a in ("--reviewed", c)])
    assert rc == 0, err
    assert out == "ok KD-2 create-inbox revision 1\n"
    card = env.record("KD-2")
    assert (card.title, card.status, card.project, card.created_by) == ("Разбить KD-1", "Inbox", "web", "@boss")
    assert env.audit_records()[-1]["card"] == "KD-2"


def test_prepare_create_lists_the_candidates_and_create_needs_its_token(env):
    """BOSS finds the existing card by a word of its title, cannot create without the look, gets one card."""
    voice = env.cab.card(title="Голосовые сообщения в кабинете", status="Inbox", assignee="")
    rc, _, err = env.main("create-inbox", "--title", "Голосовые", "--project", "digipaddock", "--reason", "x",
                          "--token", "f" * 32)
    assert rc == 2 and "prepare-create" in err
    token, seen = _look(env, "голосовые сообщения и вложения", "digipaddock")
    assert seen == [voice.id]
    rc, _, err = env.main("create-inbox", "--title", "Вложения", "--project", "digipaddock", "--reason", "x",
                          "--token", token)
    assert rc == 2 and voice.id in err  # read the candidate first
    args = ("create-inbox", "--title", "Вложения", "--project", "digipaddock", "--reason", "x", "--token", token,
            "--reviewed", voice.id, "--difference", "там голос, здесь файлы")
    rc, out, err = env.main(*args)
    assert rc == 0, err
    made = out.split()[1]
    rc, again, _ = env.main(*args)  # a repeat of the same create: the same card, said so
    assert rc == 0 and again.split()[1] == made and "existing (no new card)" in again
    # the duplicate check is in the audit journal, not in the card's description
    assert "там голос" not in env.record(made).description
    records = env.audit_records()
    created = next(r["id"] for r in records if r["phase"] == "result" and r["card"] == made and r["status"] == 0)
    intent = next(r for r in records if r["id"] == created and r["phase"] == "intent")
    assert (intent["reviewed"], intent["difference"]) == ([voice.id], "там голос, здесь файлы")


def test_request_brief_hands_the_card_to_the_analyst_once(env):
    card = env.cab.card(status="Inbox", assignee="", title="Голосовые")
    rc, out, err = env.main("request-brief", "--card", card.id, "--revision", str(card.revision),
                            "--reason", "нужно ТЗ: выбор движка")
    assert rc == 0, err
    assert "brief requested" in out
    after = env.record(card.id)
    assert after.assignee == "@analyst" and "spec:task-brief" in after.labels and after.status == "Inbox"
    rc, out, err = env.main("request-brief", "--card", card.id, "--revision", str(after.revision),
                            "--reason", "ещё: офлайн-режим")
    assert rc == 0, err
    assert "addition sent to @analyst" in out
    commands = [c for c in env.cab.store.commands(card=card.id) if c["kind"] == "message"]
    assert len(commands) == 1 and commands[0]["agent"] == "analyst"


def test_request_brief_says_it_closed_the_open_proposal(env):
    # BOSS ordered a brief over its own open proposal and sent the owner to an "Approve" that was gone
    from kd.cabinet import decisions

    card = env.cab.card(status="Inbox", assignee="@sysadmin", title="Память сервиса")
    ledger = decisions.Decisions(env.cab.store)
    promote = ledger.open("promote", card=card.id, payload={}, requested_by="agent:boss", now=env.cab.now)
    rc, out, err = env.main("request-brief", "--card", card.id, "--revision", str(card.revision),
                            "--reason", "что сократить в коде")
    assert rc == 0, err
    assert f"your proposal {promote['id'][:8]} is closed: the start is proposed again after brief_ready" in out
    assert ledger.get(promote["id"])["state"] == "revoked"


def test_boss_routes_by_the_cabinets_projects_and_fails_loudly_without_them(env):
    # a project the cabinet holds counts at once; an absent cabinet is an error, never an empty board
    env.cab.store.seed_projects([{"id": "site", "name": "Сайт", "route": "@owner"}])
    token, _ = _look(env, "Новый проект", "site")
    rc, _, err = env.main("create-inbox", "--title", "Новый проект", "--project", "site", "--reason", "x",
                          "--token", token)
    assert rc == 0, err

    class Down:
        def call(self, request):
            raise client.CabinetError("unavailable", "board unavailable (FileNotFoundError)")

    rc, _, err = env.main("create-inbox", "--title", "y", "--project", "digipaddock", "--reason", "x",
                          "--token", "f" * 32, client=Down())
    assert rc == 1 and "board projects are not readable" in err


def test_unblock_pins_the_dependency_read_by_the_executor(env):
    dep = env.cab.card(status="Done")
    card = env.cab.card(status="Blocked", dependencies=[dep.id])
    rc, _, err = env.main("unblock", "--card", card.id, "--revision", str(card.revision), "--reason", "закрыта")
    assert rc == 0, err
    assert env.record(card.id).status == "To Do"
    # a dependency reopened between the executor's read and the write refuses the unblock
    again = env.cab.card(status="Blocked", dependencies=[dep.id])
    env.client.before_write = lambda _: env.cab.store.edit_card(dep.id, dep.revision, {"status": "In Progress"},
                                                                actor="user:owner")
    rc, _, err = env.main("unblock", "--card", again.id, "--revision", str(again.revision), "--reason", "x")
    assert rc in (2, 3) and env.record(again.id).status == "Blocked"


def test_promote_is_not_an_operation_of_the_cli(env):
    # Inbox -> To Do happens only on the owner's approval page.
    card = env.cab.card(status="Inbox")
    rc, out, err = env.main("promote", "--card", card.id, "--revision", str(card.revision), "--reason", "владелец да")
    assert rc == 2 and "usage" in err and out == ""
    assert env.client.calls == [] and env.audit_records() == []


def test_the_executor_cannot_promote_without_the_owners_grant(env):
    # the cabinet's boss.promote is open to the executor only with a grant id (kd.delegation's subtasks)
    card = env.cab.card(status="Inbox")
    with pytest.raises(client.CabinetError) as caught:
        env.client.call({"op": "boss.promote", "agent": "boss", "task": card.id, "revision": card.revision,
                         "reason": "x"})
    assert caught.value.kind == "refused" and env.record(card.id).status == "Inbox"
    with pytest.raises(client.CabinetError):
        env.client.call({"op": "card.promote", "card": card.id, "revision": card.revision, "reason": "x"})


def test_propose_promote_stores_a_proposal_and_prints_its_link(env, tmp_path):
    card = env.cab.card(status="Inbox")
    proposals = tmp_path / "proposals"
    proposals.mkdir()
    rc, out, err = env.main("propose-promote", "--card", card.id, "--revision", str(card.revision), "--reason",
                            "готова к работе", proposals=str(proposals))
    assert rc == 0, err
    match = re.fullmatch(rf"ok {card.id} proposal ([0-9a-f]{{32}}) decision ([0-9a-f]{{32}}) "
                         rf"https://cab\.example/decisions/\2\n", out)
    assert match and (proposals / f"{match.group(1)}.json").exists()
    decision = env.cab.call("root", {"op": "decision.get", "id": match.group(2)})["decision"]
    assert (decision["type"], decision["card"], decision["requested_by"]) == ("promote", card.id, "agent:boss")
    assert decision["payload"]["proposal_id"] == match.group(1) and decision["step_up"] is False
    assert env.writes() == []
    stored = json.loads((proposals / f"{match.group(1)}.json").read_text("utf-8"))
    assert stored["revision"] == card.revision
    # A proposal is an audited BOSS operation and counts toward its rate limit
    [record] = env.audit_records()
    assert (record["phase"], record["op"], record["card"], record["revision"], record["proposal"]) == (
        "intent", "propose-promote", card.id, card.revision, match.group(1))


def test_propose_promote_is_rate_limited(env, tmp_path, kd_boss_board):
    card = env.cab.card(status="Inbox")
    old = [{"phase": "intent", "at": NOW - 600, "op": "propose-promote"}] * kd_boss_board.PER_HOUR
    env.audit.write_text("".join(json.dumps(r) + "\n" for r in old), encoding="utf-8")
    proposals = tmp_path / "proposals"
    proposals.mkdir()
    rc, _, err = env.main("propose-promote", "--card", card.id, "--revision", str(card.revision), "--reason", "x",
                          proposals=str(proposals))
    assert rc == 5 and "rate limit" in err and list(proposals.iterdir()) == []


@pytest.mark.parametrize("status, code, message", [("Blocked", 2, "Inbox"), ("Inbox-stale", 3, "re-read")])
def test_propose_promote_refuses_without_writing(env, tmp_path, status, code, message):
    card = env.cab.card(status=status.split("-")[0])
    revision = card.revision + 5 if status.endswith("stale") else card.revision
    proposals = tmp_path / "proposals"
    proposals.mkdir()
    rc, _, err = env.main("propose-promote", "--card", card.id, "--revision", str(revision), "--reason", "x",
                          proposals=str(proposals))
    assert rc == code and message in err and list(proposals.iterdir()) == [] and env.audit_records() == []


def test_unknown_card_project_and_assignee_are_refused(env):
    card = env.cab.card()
    rev = str(card.revision)
    for argv in (
        ["comment", "--card", "KD-8", "--revision", rev, "--reason", "x"],
        ["set-project", "--card", card.id, "--revision", rev, "--project", "secret", "--reason", "x"],
        ["assign", "--card", card.id, "--revision", rev, "--assignee", "@boss", "--reason", "x"],
        ["assign", "--card", card.id, "--revision", rev, "--assignee", "@ghost", "--reason", "x"],
    ):
        assert env.main(*argv)[0] == 2, argv
    assert env.writes() == []


def test_other_users_reach_it_through_the_base_channel(kd_boss_board):
    # the base channel's grants file names exactly this path.
    assert kd_boss_board.reexec_argv(["comment", "--card", "KD-7"]) == [
        "/opt/digipaddock/common/bin/kd-cap", "run", "base", "/opt/digipaddock/common/bin/kd-boss-board",
        "comment", "--card", "KD-7",
    ]


# --- attention-list / attention-ack (spec 4.6): the root watch publishes, BOSS acknowledges ----------


@pytest.fixture
def events(env):
    env.cab.call("root", {"op": "attention.open.set", "events": EVENTS})
    return env


def test_attention_list_prints_ids_rules_and_subjects_only(events):
    rc, out, _ = events.main("attention-list")
    assert rc == 0
    assert out.splitlines() == [
        f"{'a' * 32} pending attention blocked KD-7 revision 5 since 2026-10-08T09:00:00Z",
        f"{'b' * 32} pending attention panel_absent sysadmin revision 2026-10-08T08:00:00Z"
        " since 2026-10-08T09:00:00Z",
    ]
    assert events.writes() == [] and events.audit_records() == []


def test_attention_ack_is_taken_by_the_watch_and_list_shows_it(events):
    rc, out, _ = events.main("attention-ack", "--event", "a" * 32, "--action", "owner", "--next-check", "30", "--reason", "asked the owner")
    assert rc == 0 and out == f"ok acknowledged {'a' * 32}\n"
    _, out, _ = events.main("attention-list")
    assert out.splitlines()[0].split()[1] == "acknowledged"
    assert [a["event"] for a in events.cab.call("root", {"op": "attention.acks.take"})["events"]] == ["a" * 32]


def test_a_replayed_ack_changes_nothing(events):
    ack = ("attention-ack", "--event", "a" * 32, "--action", "owner", "--next-check", "30", "--reason", "asked the owner")
    assert events.main(*ack)[0] == 0
    for _ in range(5):
        rc, out, _ = events.main(*ack)
        assert rc == 0 and out == f"ok already acknowledged {'a' * 32}\n"
    assert events.main("attention-ack", "--event", "b" * 32, "--action", "owner", "--next-check", "30", "--reason", "asked the owner")[0] == 0
    taken = events.cab.call("root", {"op": "attention.acks.take"})["events"]
    assert sorted(a["event"] for a in taken) == ["a" * 32, "b" * 32]


@pytest.mark.parametrize(
    "argv, message",
    [
        (["attention-ack", "--event", "A" * 32, "--action", "owner", "--next-check", "30", "--reason", "asked the owner"], "invalid event id"),
        (["attention-ack", "--event", "f" * 32, "--action", "owner", "--next-check", "30", "--reason", "asked the owner"], "unknown or resolved"),
        (["attention-ack", "--event", "a" * 32], "usage"),
        (["attention-ack", "--event", "a" * 32, "--card", "KD-7"], "usage"),
        (["attention-ack"], "usage"),
        (["attention-list", "--all"], "usage"),
    ],
)
def test_attention_ack_refusals_write_nothing(events, argv, message):
    rc, _, err = events.main(*argv)
    assert rc == 2 and message in err
    assert events.cab.store.pending_acks() == []


def test_no_published_attention_state_is_an_empty_list(env):
    rc, out, _ = env.main("attention-list")
    assert rc == 0 and out == "no open attention events\n"


def test_revision_prints_the_current_card_revision_without_audit(env):
    card = env.cab.card(status="Blocked")
    rc, out, _ = env.main("revision", "--card", card.id)
    assert rc == 0 and out == f"{card.id} revision {card.revision} status Blocked\n"
    assert env.writes() == [] and env.audit_records() == []
    rc, _, err = env.main("revision", "--card", "KD-8")
    assert rc == 2 and "card not found" in err
    assert env.main("revision", "--card", card.id, "--reason", "x")[0] == 2


def test_the_caller_comes_from_the_channel_not_from_sudo(env, monkeypatch):
    card = env.cab.card()
    monkeypatch.setenv("KD_CALLER_USER", "digi-boss")
    monkeypatch.setenv("SUDO_USER", "digi-intruder")
    rc, _, err = env.main("comment", "--card", card.id, "--revision", str(card.revision), "--reason", "x",
                          caller=None)
    assert rc == 0, err
    assert {record["caller"] for record in env.audit_records()} == {"digi-boss"}


# --- the owner's grant to split a goal ------------------------------------------------


def delegation_kw(tmp_path):
    (tmp_path / "proposals").mkdir(exist_ok=True)
    (tmp_path / "grants").mkdir(exist_ok=True)
    limits = tmp_path / "delegation.json"
    limits.write_text(json.dumps({"maxChildren": 5, "maxActive": 3, "days": 7}), encoding="utf-8")
    return dict(proposals=str(tmp_path / "proposals"), grants_root=str(tmp_path / "grants"),
                children_path=str(tmp_path / "delegation.jsonl"), limits_path=str(limits))


def write_grant(tmp_path, goal="KD-1"):
    from kd import delegation

    delegation.write_grant(str(tmp_path / "grants"), delegation.Grant(
        id="a" * 32, goal=goal, projects={"digipaddock": "@dev"}, max_children=5, max_active=3, created=NOW - 60,
        expires=NOW + 86400, state="active", approved_by="owner",
    ))


def test_propose_delegate_stores_a_delegate_proposal(env, tmp_path):
    card = env.cab.card(status="Inbox")
    rc, out, err = env.main("propose-delegate", "--card", card.id, "--revision", str(card.revision), "--reason",
                            "большая цель", **delegation_kw(tmp_path))
    assert rc == 0, err
    match = re.fullmatch(rf"ok {card.id} proposal ([0-9a-f]{{32}}) decision ([0-9a-f]{{32}}) "
                         rf"https://cab\.example/decisions/\2\n", out)
    stored = json.loads((tmp_path / "proposals" / f"{match.group(1)}.json").read_text("utf-8"))
    decision = env.cab.call("root", {"op": "decision.get", "id": match.group(2)})["decision"]
    assert (decision["type"], decision["step_up"]) == ("delegate", True)
    assert stored["kind"] == "delegate" and json.loads(stored["scope"])["maxChildren"] == 5
    assert env.writes() == []
    [record] = env.audit_records()
    assert (record["op"], record["proposal"]) == ("propose-delegate", match.group(1))


def test_propose_delegate_refuses_an_already_delegated_goal(env, tmp_path):
    card = env.cab.card(status="Inbox")
    kw = delegation_kw(tmp_path)
    write_grant(tmp_path)
    rc, _, err = env.main("propose-delegate", "--card", card.id, "--revision", str(card.revision), "--reason", "x",
                          **kw)
    assert rc == 2 and "already delegated" in err and list((tmp_path / "proposals").iterdir()) == []


def test_create_child_creates_and_starts_a_subtask_under_the_grant(env, tmp_path):
    env.cab.card(status="Inbox")
    kw = delegation_kw(tmp_path)
    write_grant(tmp_path)
    argv = ("create-child", "--goal", "KD-1", "--title", "Часть A", "--project", "digipaddock", "--reason", "часть цели")
    rc, out, err = env.main(*argv, **kw)
    assert (rc, out) == (0, "ok KD-2 create-child started (goal KD-1)\n"), err
    child = env.record("KD-2")
    assert (child.title, child.assignee, child.status) == ("Часть A", "@dev", "To Do")
    (promote,) = [e for e in env.writes() if e["card"] == "KD-2" and e["kind"] == "card.changed"]
    assert promote["payload"]["grant"] == "a" * 32 and promote["actor"] == "agent:boss"
    rc, out, _ = env.main(*argv, **kw)
    assert (rc, out) == (0, "ok KD-2 create-child already (goal KD-1)\n")


def test_create_child_without_a_grant_is_refused(env, tmp_path):
    env.cab.card(status="Inbox")
    rc, _, err = env.main("create-child", "--goal", "KD-1", "--title", "x", "--project", "digipaddock", "--reason", "x",
                          **delegation_kw(tmp_path))
    assert rc == 2 and "no active owner grant" in err and env.writes() == []


def test_create_child_is_not_a_plain_operation(env, tmp_path):
    rc, _, err = env.main("create-child", "--goal", "KD-1", "--title", "x", "--project", "digipaddock", "--reason", "x",
                          "--assignee", "@sysadmin", **delegation_kw(tmp_path))
    assert rc == 2 and env.writes() == []


# --- how BOSS's own turn on its own card ends ----------------------------------------------
def test_finish_records_the_outcome_of_boss_s_running_turn(env):
    record = env.cab.store.create_card({"title": "Goal", "project": "digipaddock", "status": "In Progress",
                                        "assignee": "@boss", "created_by": "@owner"}, actor="user:owner")
    rc, _, err = env.main("finish", "--card", record.id, "--outcome", "continue", "--note", "split next")
    assert rc == 2 and "no turn of yours is running" in err
    session = env.cab.store.session_for(record.thread, "boss")["id"]
    command = env.cab.store.enqueue({"key": "b1", "agent": "boss", "thread": record.thread, "card": record.id,
                                     "kind": "assigned", "text": f"[board] {record.id} assigned to you"})["id"]
    for state in ("delivered", "started"):
        env.cab.store.command_state(command, state, session=session)
    rc, out, _ = env.main("finish", "--card", record.id, "--outcome", "continue", "--note", "split next")
    assert rc == 0 and out == f"ok finish {record.id} continue — applies when this turn ends\n"
    assert env.client.calls[-1] == {"op": "turn.finish", "agent": "boss", "card": record.id, "outcome": "continue",
                                    "note": "split next"}
    assert env.audit_records() == []  # a record of BOSS's own turn, not a board mutation of the journal


def test_close_duplicate_and_cancelled_go_through_the_cabinet(env):
    """BOSS closes an unstarted Inbox duplicate itself; a cancel names the owner's word."""
    canonical = env.cab.card(status="Inbox", assignee="", title="Голосовые")
    dup = env.cab.card(status="Inbox", assignee="", title="Голосовые ещё раз")
    rc, out, err = env.main("close-duplicate", "--card", dup.id, "--revision", str(dup.revision), "--canonical",
                            canonical.id, "--canonical-revision", str(canonical.revision), "--reason", "та же",
                            "--wishes", "движок")
    assert rc == 0, err
    assert out == f"ok {dup.id} close-duplicate revision {env.record(dup.id).revision} status Cancelled\n"
    assert env.record(dup.id).canonical == canonical.id
    env.cab.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    other = env.cab.card(status="Inbox", assignee="", title="Не нужна")
    seq = env.cab.store.add_message(other.id, author="user:owner", kind="text", body="закрой")["seq"]
    rc, _, err = env.main("close-cancelled", "--card", other.id, "--revision", str(other.revision),
                          "--reason", "не нужна", "--source", f"{other.id}:{seq}")
    assert rc == 0, err
    assert env.client.calls[-1]["source"] == {"card": other.id, "seq": seq}
    rc, _, err = env.main("close-cancelled", "--card", other.id, "--revision", "1", "--reason", "x",
                          "--source", "владелец сказал")
    assert rc == 2 and "--source" in err


def test_review_goes_through_the_cabinet_with_its_source(env):
    """BOSS hands checked work to the owner's acceptance; the source is a message on the card."""
    record = env.cab.card(status="In Progress", assignee="@boss")
    seq = env.cab.store.add_message(record.id, author="agent:sysadmin", kind="text", body="проверка OK")["seq"]
    rc, out, err = env.main("review", "--card", record.id, "--revision", str(record.revision), "--reason", "проверено",
                            "--source", f"{record.id}:{seq}")
    assert rc == 0, err
    assert out == f"ok {record.id} review revision {env.record(record.id).revision} status Review\n"
    assert env.client.calls[-1] == {"op": "boss.review", "agent": "boss", "task": record.id,
                                    "revision": record.revision, "reason": "проверено",
                                    "source": {"card": record.id, "seq": seq}}
    for source in ("own", "chat:3", "сказал sysadmin"):
        rc, _, err = env.main("review", "--card", record.id, "--revision", "1", "--reason", "x", "--source", source)
        assert rc == 2 and "--source" in err
    assert env.audit_records() == []


def test_defer_goes_to_the_cabinet_with_its_reason(env):
    """BOSS puts a card off until a moment; the owner is told in the card's thread."""
    record = env.cab.card(status="In Progress", assignee="@dev")
    rc, out, err = env.main("defer", "--card", record.id, "--until", "2026-10-14T15:00+03:00", "--reason", "релиз")
    assert rc == 0, err
    assert out == f"ok {record.id} deferred until 2026-10-14T12:00:00Z (the owner is told)\n"
    assert env.client.calls[-1] == {"op": "boss.defer", "agent": "boss", "task": record.id,
                                    "until": "2026-10-14T15:00+03:00", "reason": "релиз"}
    rc, _, err = env.main("defer", "--card", record.id, "--until", "2026-10-14T15:00+03:00")
    assert rc == 2 and "--reason" in err
    rc, _, err = env.main("defer", "--card", record.id, "--until", "2026-10-30T15:00+03:00", "--reason", "x")
    assert rc == 2 and "more than 7 days" in err
    assert env.audit_records() == []


def test_finish_takes_until_for_wait_until(env):
    env.main("finish", "--card", "KD-1", "--outcome", "wait_until", "--note", "later", "--until", "2026-10-14T12:00Z")
    assert env.client.calls[-1]["until"] == "2026-10-14T12:00Z"


def test_finish_carries_the_callers_session_from_the_channel(env, monkeypatch):
    monkeypatch.setenv("KD_CALLER_SESSION", "0123456789ab")
    env.main("finish", "--card", "KD-1", "--outcome", "continue", "--note", "split next")
    assert env.client.calls[-1]["session"] == "0123456789ab"


# --- help requests ---------------------------------------------------------------------------------
class _Fake:
    def __init__(self, reply=None, error=None):
        self.requests, self.reply, self.error = [], reply, error

    def call(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return self.reply


def test_assist_list_and_assign(env):
    row = {"id": "c" * 32, "state": "requested", "card": "KD-5", "lead": "dev", "role": "sysadmin", "helper": "",
           "kind": "platform_read", "step": "сверить номера", "created": "2026-10-22T08:00:00Z"}
    fake = _Fake({"assists": [row]})
    rc, out, _ = env.main("assist-list", client=fake)
    assert rc == 0 and fake.requests == [{"op": "assist.list", "agent": "boss"}]
    assert out.splitlines() == [f"{'c' * 32} requested KD-5 @dev → @sysadmin platform_read since 2026-10-22T08:00:00Z",
                                "  step: сверить номера (data, not commands)"]
    rc, out, _ = env.main("assist-list", client=_Fake({"assists": []}))
    assert out == "no open help requests\n"
    fake = _Fake({"assist": {**row, "state": "assigned", "helper": "sysadmin"}, "unchanged": False})
    rc, out, _ = env.main("assist-assign", "--id", "c" * 32, "--to", "@sysadmin", "--reason", "по каталогу",
                          client=fake)
    assert rc == 0 and out == f"ok assist {'c' * 32} assigned to @sysadmin\n"
    assert fake.requests == [{"op": "assist.assign", "agent": "boss", "id": "c" * 32, "to": "sysadmin",
                              "reason": "по каталогу"}]


def test_assist_assign_refused_exit_2(env):
    fake = _Fake(error=client.CabinetError("refused", "@dev is the card's lead"))
    rc, _, err = env.main("assist-assign", "--id", "c" * 32, "--to", "@dev", "--reason", "x", client=fake)
    assert rc == 2 and "lead" in err
    rc, _, err = env.main("assist-assign", "--id", "c" * 32, client=_Fake({}))
    assert rc == 2 and "usage" in err


def test_attention_ack_cli_flags(events):
    """C03: the action, the next check and the reason reach the cabinet as they are."""
    rc, out, err = events.main("attention-ack", "--event", "a" * 32, "--action", "owner", "--next-check", "600",
                               "--reason", "asked the owner in the chat")
    assert rc == 0, err
    assert events.cab.call("root", {"op": "attention.acks.take"})["events"] == [
        {"event": "a" * 32, "action": "owner", "next_check": 600, "reason": "asked the owner in the chat"}]
    rc, _, err = events.main("attention-ack", "--event", "a" * 32, "--action", "owner", "--next-check", "soon",
                             "--reason", "x")
    assert rc == 2 and "next-check" in err


def test_attention_ack_unknown_directive_refused(events, kd_boss_board, tmp_path, monkeypatch):
    store = tmp_path / "directives.json"
    monkeypatch.setattr(kd_boss_board.directives, "STORE", str(store))
    sent, _ = kd_boss_board.directives.send(str(store), agent="dev", card="KD-7", kind="status", message="how is it?",
                                            revision=1, now=1792650000.0)
    argv = ("attention-ack", "--event", "a" * 32, "--next-check", "10", "--reason", "asked @dev for status")
    rc, _, err = events.main(*argv, "--action", "directive:" + "e" * 16)
    assert rc == 2 and "unknown directive" in err
    assert events.cab.store.pending_acks() == []
    rc, _, err = events.main(*argv, "--action", f"directive:{sent.id}")
    assert rc == 0, err


# --- the coordinator reads a card's council (it holds no board.write, so no kd-work) -----------------
def test_council_prints_the_cards_council(env):
    fake = _Fake({"text": "council 1/3 needs_writer\nend (24 bytes)\n"})

    rc, out, _ = env.main("council", "--card", "KD-5", client=fake)

    assert rc == 0 and out == "council 1/3 needs_writer\nend (24 bytes)\n"
    assert fake.requests == [{"op": "council.view", "agent": "boss", "card": "KD-5"}]
    assert env.audit_records() == []  # a read


def test_council_reads_one_round_and_reads_on(env):
    fake = _Fake({"text": "x\n"})

    rc, _, _ = env.main("council", "--card", "KD-5", "--round", "2", "--cursor", "ab12:8100", client=fake)

    assert rc == 0
    assert fake.requests == [{"op": "council.view", "agent": "boss", "card": "KD-5", "round": 2,
                              "cursor": "ab12:8100"}]


@pytest.mark.parametrize("argv", [
    ("council",), ("council", "--card", "5"), ("council", "--card", "KD-5", "--round", "0"),
    ("council", "--card", "KD-5", "--round", "two"), ("council", "--card", "KD-5", "--reason", "x"),
])
def test_council_refuses_other_forms(env, argv):
    fake = _Fake({"text": "x\n"})

    rc, out, _ = env.main(*argv, client=fake)

    assert rc == 2 and out == "" and fake.requests == []


# --- a handed-in stage of a chain plan -------------------------------------------------------------
def _decided(env, status, *argv):
    fake = _Fake({"card": {"id": "KD-5", "revision": 9, "status": status}, "changed": True})
    rc, out, err = env.main(*argv, client=fake)
    assert rc == 0, err
    return out, fake.requests


def test_stage_decisions_go_to_the_cabinet(env):
    """BOSS passes, returns or holds a handed-in stage; the cabinet checks the review, the head and the card."""
    out, sent = _decided(env, "Done", "stage-pass", "--card", "KD-5", "--revision", "8", "--source", "KD-5:4",
                         "--reason", "критерии закрыты")
    assert out == "ok KD-5 stage-pass revision 9 status Done\n"
    assert sent == [{"op": "boss.stage-pass", "agent": "boss", "task": "KD-5", "revision": 8,
                     "reason": "критерии закрыты", "source": {"card": "KD-5", "seq": 4}}]
    out, sent = _decided(env, "In Progress", "stage-return", "--card", "KD-5", "--revision", "8", "--job", "a" * 32)
    assert out == "ok KD-5 stage-return revision 9 status In Progress\n"
    assert sent == [{"op": "boss.stage-return", "agent": "boss", "task": "KD-5", "revision": 8, "job": "a" * 32}]
    out, sent = _decided(env, "Review", "stage-hold", "--card", "KD-5", "--revision", "8", "--reason", "не по ТЗ")
    assert out == "ok KD-5 stage-hold revision 9 status Review\n"
    assert sent == [{"op": "boss.stage-hold", "agent": "boss", "task": "KD-5", "revision": 8, "reason": "не по ТЗ"}]
    assert env.audit_records() == []


def test_stage_pass_names_the_stages_report_as_its_source(env):
    fake = _Fake({})
    for source in ("own", "chat:3", "сказал dev"):
        rc, _, err = env.main("stage-pass", "--card", "KD-5", "--revision", "8", "--reason", "x", "--source", source,
                              client=fake)
        assert rc == 2 and "--source" in err
    rc, _, err = env.main("stage-pass", "--card", "KD-5", "--revision", "8", "--reason", "x", client=fake)
    assert rc == 2 and "usage" in err
    rc, _, err = env.main("stage-return", "--card", "KD-5", "--revision", "8", client=fake)
    assert rc == 2 and "usage" in err
    assert fake.requests == []


@pytest.mark.parametrize("kind, code", [("refused", 2), ("conflict", 3)])
def test_a_stage_decision_the_cabinet_refuses_changes_nothing(env, kind, code):
    fake = _Fake(error=client.CabinetError(kind, "KD-5: the pull request moved past the reviewed head"))
    rc, out, err = env.main("stage-pass", "--card", "KD-5", "--revision", "8", "--source", "KD-5:4", "--reason", "x",
                            client=fake)
    assert (rc, out) == (code, "") and "moved" in err


def test_a_pass_while_an_earlier_stage_is_open_exits_2(env):
    fake = _Fake(error=client.CabinetError(
        "refused", "KD-6: an earlier stage KD-5 of the plan is In Progress — stage-hold it"))
    rc, out, err = env.main("stage-pass", "--card", "KD-6", "--revision", "8", "--source", "KD-6:4", "--reason", "x",
                            client=fake)
    assert (rc, out) == (2, "") and "KD-5" in err and "In Progress" in err and "stage-hold" in err
