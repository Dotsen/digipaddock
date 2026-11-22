"""the worker's finite board operations (BOSS spec 5.8.8, 5.8.5); change dictionaries."""
import sys
from pathlib import Path

import pytest

from kd import boss_board, worker_board as wb

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_boss_board import card  # noqa: E402  the same Card snapshots


def test_signed_adds_the_agents_signature_once():
    assert wb.signed("dev", "x") == "[dev] x"
    assert wb.signed("dev", "[dev] x") == "[dev] x"
    assert wb.signed("dev", "[boss] x") == "[dev] [boss] x"  # someone else's signature is just text


def test_claim_takes_a_free_to_do_card_with_the_callers_name():
    c = card()
    edit = wb.plan("claim", {}, agent="dev", card=c)
    assert edit == boss_board.Edit(c.id, c.revision, {"status": "In Progress", "assignee": "@dev"})


def test_claim_of_my_own_to_do_card_is_fine():
    c = card(assignee="@dev")
    assert wb.plan("claim", {}, agent="dev", card=c).changes == {"status": "In Progress", "assignee": "@dev"}


@pytest.mark.parametrize("status", ["Inbox", "In Progress", "Blocked", "Review", "Done"])
def test_claim_only_from_to_do(status):
    with pytest.raises(wb.Refused, match="not in To Do"):
        wb.plan("claim", {}, agent="dev", card=card(status=status))


def test_claim_of_someone_elses_card_is_refused():
    with pytest.raises(wb.Refused, match="already assigned to @sysadmin"):
        wb.plan("claim", {}, agent="dev", card=card(assignee="@sysadmin"))


def test_claimable_judges_the_card_before_its_label():
    # Review minor: the executor asks this first, so a foreign label on a card that is not
    # claimable never produces the advice "hand the card to @boss".
    with pytest.raises(wb.Refused, match="already assigned to @sysadmin"):
        wb.claimable(card(assignee="@sysadmin"), "@dev")
    with pytest.raises(wb.Refused, match="not in To Do"):
        wb.claimable(card(status="Inbox"), "@dev")
    assert wb.claimable(card(), "@dev") is None


SPECS = {"@dev": frozenset({"implementer", "reviewer", "test-author", "tester", "docs"}),
         "@analyst": frozenset({"task-brief"}), "@sysadmin": frozenset(), "@boss": frozenset()}


def test_claim_records_the_specialization_note_signed():
    c = card(status="To Do", assignee="@dev", labels=["spec:reviewer"])
    edit = wb.plan("claim", {"note": "spec reviewer v1 sha256=" + "a" * 64}, agent="dev", card=c)
    assert edit.changes == {"status": "In Progress", "assignee": "@dev",
                            "notes_append": "[dev] spec reviewer v1 sha256=" + "a" * 64}
    plain = wb.plan("claim", {}, agent="dev", card=c)
    assert plain.changes == {"status": "In Progress", "assignee": "@dev"}
    with pytest.raises(wb.Refused, match="field not allowed"):
        wb.plan("claim", {"spec": "reviewer"}, agent="dev", card=c)


def _edit(request, agent="dev", specs=None, **card_kw):
    c = card(**card_kw)
    return c, wb.plan("edit", request, agent=agent, card=c, specs=specs)


def test_edit_spec_on_my_draft_must_be_mine():
    _, edit = _edit({"spec": "reviewer"}, status="Inbox", assignee="@dev", specs=SPECS)
    assert edit.changes == {"labels_add": ["spec:reviewer"]}
    with pytest.raises(wb.Refused, match="not a specialization of @dev"):
        _edit({"spec": "task-brief"}, status="Inbox", assignee="@dev", specs=SPECS)
    with pytest.raises(wb.Refused, match="no specialization registry"):
        _edit({"spec": "reviewer"}, status="Inbox", assignee="@dev")


@pytest.mark.parametrize("status", ["To Do", "In Progress", "Blocked"])
def test_lead_selects_instructions_without_changing_approved_scope_or_status(status):
    _, edit = _edit({"spec": "docs"}, status=status, assignee="@dev", labels=["spec:implementer"], specs=SPECS)
    assert edit.changes == {"labels_add": ["spec:docs"], "labels_remove": ["spec:implementer"]}
    with pytest.raises(wb.Refused, match="not your card"):
        _edit({"spec": "docs"}, status=status, assignee="@analyst", specs=SPECS)


@pytest.mark.parametrize("status", ["Review", "Done", "Cancelled"])
def test_instruction_selection_does_not_reopen_finished_or_submitted_work(status):
    with pytest.raises(wb.Refused, match="draft or active"):
        _edit({"spec": "docs"}, status=status, assignee="@dev", specs=SPECS)


def test_notes_and_comment_are_signed_by_the_executor():
    c, edit = _edit({"append_notes": ["done step 1", "[dev] step 2"], "comment": "вопрос"},
                    status="In Progress", assignee="@dev")
    assert edit.changes == {"notes_append": "[dev] done step 1\n[dev] step 2",
                            "comment": {"text": "[dev] вопрос", "author": "@dev"}}


@pytest.mark.parametrize("src,dst", [("In Progress", "Review"), ("In Progress", "Blocked"), ("In Progress", "Done"),
                                     ("Review", "In Progress"), ("Review", "Done"), ("Blocked", "In Progress")])
def test_allowed_transitions_on_my_card(src, dst):
    _, edit = _edit({"status": dst}, status=src, assignee="@dev")
    assert edit.changes == {"status": dst}


@pytest.mark.parametrize("src,dst,why", [
    ("Inbox", "To Do", "only the owner's page"), ("Inbox", "In Progress", "only the owner's page"),
    ("Inbox", "Review", "only the owner's page"), ("Inbox", "Done", "only the owner's page"),
    ("To Do", "In Progress", "take it with kd-task claim"), ("Blocked", "Review", "not allowed"),
    ("Done", "In Progress", "not allowed"), ("Review", "To Do", "not allowed"), ("In Progress", "Inbox", "not allowed"),
])
def test_refused_transitions(src, dst, why):
    with pytest.raises(wb.Refused, match=why):
        _edit({"status": dst}, status=src, assignee="@dev")


def test_draft_fields_only_on_my_inbox_card():
    _, edit = _edit({"description": "ТЗ", "ac": ["проверка"], "spec": "implementer"},
                    status="Inbox", assignee="@dev", labels=["spec:task-brief"], specs=SPECS)
    assert edit.changes == {"ac_add": ["проверка"], "description": "ТЗ", "labels_add": ["spec:implementer"],
                            "labels_remove": ["spec:task-brief"]}
    with pytest.raises(wb.Refused, match="only on your draft in Inbox"):
        _edit({"description": "x"}, status="In Progress", assignee="@dev")
    with pytest.raises(wb.Refused, match="set with --spec"):
        _edit({"add_label": ["spec:x"]}, status="Inbox", assignee="@dev")


def test_acceptance_indexes_become_numbers():
    _, edit = _edit({"check_ac": ["1", 2], "uncheck_ac": ["3"]}, status="In Progress", assignee="@dev")
    assert edit.changes == {"ac_check": [1, 2], "ac_uncheck": [3]}
    _, edit = _edit({"remove_ac": ["2"]}, status="Inbox", assignee="@dev")
    assert edit.changes == {"ac_remove": [2]}
    for bad in (["0"], ["x"], [True], ["1000"]):
        with pytest.raises(wb.Refused, match="index must be 1..999"):
            _edit({"check_ac": bad}, status="In Progress", assignee="@dev")


def test_only_a_comment_on_someone_elses_card():
    _, edit = _edit({"comment": "вижу"}, status="In Progress", assignee="@sysadmin")
    assert edit.changes == {"comment": {"text": "[dev] вижу", "author": "@dev"}}
    with pytest.raises(wb.Refused, match="not your card"):
        _edit({"comment": "вижу", "status": "Review"}, status="In Progress", assignee="@sysadmin")
    with pytest.raises(wb.Refused, match="not your card"):
        _edit({"append_notes": ["x"]}, status="In Progress", assignee="")


def test_dependencies_labels_and_summary():
    _, edit = _edit({"status": "Blocked", "dep": ["KD-9"], "comment": "waits for KD-9",
                     "add_label": ["server:node-c"], "final_summary": "итог"},
                    status="In Progress", assignee="@dev")
    assert edit.changes == {"status": "Blocked", "summary": "[dev] итог", "dependencies": ["KD-9"],
                            "labels_add": ["server:node-c"],
                            "comment": {"text": "[dev] waits for KD-9", "author": "@dev"}}
    with pytest.raises(wb.Refused, match="depend on itself"):
        _edit({"dep": ["KD-7"]}, status="In Progress", assignee="@dev")
    with pytest.raises(wb.Refused, match="nothing to change"):
        _edit({"add_label": ["x"]}, status="In Progress", assignee="@dev", labels=["x"])


def test_clear_deps_empties_the_list_only_when_there_is_one():
    _, edit = _edit({"clear_deps": True}, status="In Progress", assignee="@dev", dependencies=["KD-3"])
    assert edit.changes == {"dependencies": []}
    with pytest.raises(wb.Refused, match="nothing to change"):
        _edit({"clear_deps": True}, status="In Progress", assignee="@dev")


@pytest.mark.parametrize("text", ["", "-s Done", "a\x00b", "x" * 4001,
                                  "ok\n## Description\nx", "## Definition of Done", "a\n   #### b", "a\n<!-- b -->"])
def test_text_is_bounded_plain_and_not_a_flag(text):
    with pytest.raises(wb.Refused):
        _edit({"append_notes": [text]}, status="In Progress", assignee="@dev")


def test_a_comment_cannot_open_a_section_on_someone_elses_card():
    # Final review: a comment is allowed on any card and lands in its md export as it is, so a line that is a
    # markdown heading would start a new section of the exported card, or a planted "Definition of Done"
    # would read as part of the approval scope.
    with pytest.raises(wb.Refused, match="plain text"):
        _edit({"comment": "ok\n## Definition of Done\n- [ ] planted"}, status="To Do", assignee="@sysadmin")
    # Plain multi-line text, and a # that is not a heading, still pass.
    _, plan = _edit({"comment": "line one\nline two\nissue #12, C# and #hashtag"}, status="To Do",
                    assignee="@sysadmin")
    assert plan.changes["comment"]["text"].endswith("issue #12, C# and #hashtag")


def test_unknown_fields_are_refused():
    with pytest.raises(wb.Refused, match="field not allowed: title"):
        _edit({"title": "x"}, status="In Progress", assignee="@dev")


PROJECTS = frozenset({"digipaddock", "admin", "web"})
TARGETS = frozenset({"@dev", "@sysadmin", "@boss"})


def test_create_lands_in_inbox_on_the_caller_with_a_signed_description():
    create = wb.plan("create", {"title": " Заголовок ", "project": "admin", "description": "что и зачем",
                                "label": ["server:node-c"], "ac": ["проверено"], "dep": ["KD-3"]},
                     agent="sysadmin", projects=PROJECTS)
    assert create == boss_board.Create({
        "title": "Заголовок", "status": "Inbox", "project": "admin", "assignee": "@sysadmin",
        "created_by": "@sysadmin", "description": "[sysadmin] что и зачем", "acceptance": ["проверено"],
        "labels": ["server:node-c"], "dependencies": ["KD-3"]})


def test_create_checks_a_spec_label_against_the_targets_registry():
    base = {"title": "x", "project": "digipaddock"}
    create = wb.plan("create", {**base, "label": ["spec:reviewer", "a"]}, agent="dev", projects=PROJECTS, specs=SPECS)
    assert create.values["labels"] == ["spec:reviewer", "a"]
    for req, why in ((dict(base, label=["spec:task-brief"]), "not a specialization of @dev"),
                     (dict(base, label=["spec:reviewer"], to="@boss"), "not a specialization of @boss"),
                     (dict(base, label=["spec:reviewer", "spec:docs"]), "one spec: label")):
        with pytest.raises(wb.Refused, match=why):
            wb.plan("create", req, agent="dev", projects=PROJECTS, specs=SPECS)
    with pytest.raises(wb.Refused, match="no specialization registry"):
        wb.plan("create", dict(base, label=["spec:reviewer"]), agent="dev", projects=PROJECTS)
    assert wb.plan("create", dict(base, label=["a"]), agent="dev", projects=PROJECTS)  # no registry needed


def test_create_for_the_coordinator_is_the_only_other_target():
    create = wb.plan("create", {"title": "x", "project": "web", "to": "@boss"}, agent="sysadmin", projects=PROJECTS)
    assert create.values["assignee"] == "@boss" and "description" not in create.values
    for to in ("@dev", "@owner", "dev"):
        with pytest.raises(wb.Refused, match="only yourself or @boss"):
            wb.plan("create", {"title": "x", "project": "web", "to": to}, agent="sysadmin", projects=PROJECTS)


@pytest.mark.parametrize("req,why", [
    ({"title": "x" * 101, "project": "admin"}, "title"),
    ({"title": "-s Done", "project": "admin"}, "title"),
    ({"title": "x", "project": "nope"}, "unknown project"),
    ({"title": "x", "project": "admin", "status": "To Do"}, "field not allowed: status"),
    ({"title": "x", "project": "admin", "label": ["a,b"]}, "invalid label"),
])
def test_create_refusals(req, why):
    with pytest.raises(wb.Refused, match=why):
        wb.plan("create", req, agent="dev", projects=PROJECTS)


def test_handoff_changes_assignee_and_spec_in_one_edit():
    c = card(status="Inbox", assignee="@analyst", labels=["spec:task-brief", "x"])
    edit = wb.plan("handoff", {"to": "@dev", "spec": "implementer", "comment": "ТЗ готово"},
                   agent="analyst", card=c, targets=TARGETS | {"@analyst"}, specs=SPECS)
    assert edit == boss_board.Edit(c.id, c.revision, {
        "assignee": "@dev", "labels_add": ["spec:implementer"], "labels_remove": ["spec:task-brief"],
        "comment": {"text": "[analyst] handoff -> @dev (spec:implementer): ТЗ готово", "author": "@analyst"}})


def test_handoff_adds_a_plain_label_in_the_same_edit():
    # Final review: the analyst marks the brief ready and hands the card over in one write,
    # so no intermediate revision exists for the brief_ready rule to propose the card to the analyst.
    c = card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    edit = wb.plan("handoff", {"to": "@dev", "spec": "implementer", "add_label": ["brief-ready"]},
                   agent="analyst", card=c, targets=TARGETS | {"@analyst"}, specs=SPECS)
    assert edit.changes == {"assignee": "@dev", "labels_add": ["spec:implementer", "brief-ready"],
                            "labels_remove": ["spec:task-brief"],
                            "comment": {"text": "[analyst] handoff -> @dev (spec:implementer)", "author": "@analyst"}}
    held = card(status="Inbox", assignee="@analyst", labels=["brief-ready"])
    edit = wb.plan("handoff", {"to": "@dev", "add_label": ["brief-ready"]}, agent="analyst", card=held,
                   targets=TARGETS | {"@analyst"}, specs=SPECS)
    assert edit.changes == {"assignee": "@dev", "comment": {"text": "[analyst] handoff -> @dev", "author": "@analyst"}}
    for labels, why in (([("spec:reviewer")], "spec"), (["bad label"], "invalid label"), ("brief-ready", "at most")):
        with pytest.raises(wb.Refused, match=why):
            wb.plan("handoff", {"to": "@dev", "add_label": labels}, agent="analyst", card=c,
                    targets=TARGETS | {"@analyst"}, specs=SPECS)


def test_handoff_carries_the_briefs_size_replacing_an_earlier_one():
    """the size goes in the handoff's own write even when the card has it (the version it records carries
    it), an earlier size is replaced, and one handoff gives one size."""
    c = card(status="Inbox", assignee="@analyst", labels=["spec:task-brief", "size:S", "size:L"])
    edit = wb.plan("handoff", {"to": "@dev", "spec": "implementer", "add_label": ["brief-ready", "size:S"]},
                   agent="analyst", card=c, targets=TARGETS | {"@analyst"}, specs=SPECS)
    assert edit.changes["labels_add"] == ["spec:implementer", "brief-ready", "size:S"]
    assert edit.changes["labels_remove"] == ["spec:task-brief", "size:L"]
    with pytest.raises(wb.Refused, match="one size"):
        wb.plan("handoff", {"to": "@dev", "add_label": ["size:S", "size:M"]}, agent="analyst", card=c,
                targets=TARGETS | {"@analyst"}, specs=SPECS)


def test_handoff_spec_must_be_one_of_the_targets_specializations():
    c = card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    for to, spec, why in (("@dev", "task-brief", "not a specialization of @dev"),
                          ("@boss", "reviewer", "not a specialization of @boss"),
                          ("@dev", "bogus", "not a specialization of @dev")):
        with pytest.raises(wb.Refused, match=why):
            wb.plan("handoff", {"to": to, "spec": spec}, agent="analyst", card=c,
                    targets=TARGETS | {"@analyst"}, specs=SPECS)
    with pytest.raises(wb.Refused, match="no specialization registry"):
        wb.plan("handoff", {"to": "@dev", "spec": "implementer"}, agent="analyst", card=c,
                targets=TARGETS | {"@analyst"})


def test_handoff_without_spec_keeps_only_a_label_the_target_holds():
    # Final review (spec 5.8.5): the receiver never sees a specialization it does not hold.
    c = card(status="In Progress", assignee="@dev", labels=["spec:implementer"])
    edit = wb.plan("handoff", {"to": "@boss"}, agent="dev", card=c, targets=TARGETS, specs=SPECS)
    assert edit.changes == {"assignee": "@boss", "labels_remove": ["spec:implementer"],
                            "comment": {"text": "[dev] handoff -> @boss", "author": "@dev"}}
    held = card(status="Inbox", assignee="@analyst", labels=["spec:implementer"])
    edit = wb.plan("handoff", {"to": "@dev"}, agent="analyst", card=held, targets=TARGETS | {"@analyst"}, specs=SPECS)
    assert edit.changes == {"assignee": "@dev", "comment": {"text": "[analyst] handoff -> @dev", "author": "@analyst"}}
    with pytest.raises(wb.Refused, match="no specialization registry"):
        wb.plan("handoff", {"to": "@boss"}, agent="dev", card=c, targets=TARGETS)
    plain = card(status="In Progress", assignee="@dev")
    assert wb.plan("handoff", {"to": "@boss"}, agent="dev", card=plain, targets=TARGETS).changes["assignee"] == "@boss"


@pytest.mark.parametrize("to,assignee,why", [
    ("@dev", "@dev", "yourself"), ("@owner", "@dev", "unknown target"), ("@ghost", "@dev", "unknown target"),
    ("@boss", "@sysadmin", "not your card"), ("@boss", "", "not your card"),
])
def test_handoff_refusals(to, assignee, why):
    c = card(status="In Progress", assignee=assignee)
    with pytest.raises(wb.Refused, match=why):
        wb.plan("handoff", {"to": to}, agent="dev", card=c, targets=TARGETS)
