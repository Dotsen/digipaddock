import pytest

from kd import boss_board as bb

ROUTES = {
    "digipaddock": "@dev",
    "admin": "@sysadmin",
    "web": "@owner",
    "alert": "@owner",
}
ROUTING = bb.routing_from(ROUTES, ["dev", "sysadmin", "boss"])


def record(task_id="KD-7", **over):
    """A card as the cabinet returns it (``CardRecord.to_dict``); unassigned To Do by default."""
    base = {"id": task_id, "revision": 3, "status": "To Do", "assignee": "", "project": "digipaddock", "priority": "",
            "labels": [], "servers": [], "dependencies": [], "title": f"Карточка {task_id}"}
    base.update(over)
    return base


def card(task_id="KD-7", **over):
    return bb.card_from(record(task_id, **over))


def req(op, c=None, **fields):
    base = {"op": op, "actor": "boss", "reason": "по таблице routes"}
    if c is not None:
        base.update(task=c.id, revision=c.revision)
    base.update(fields)
    return base


def comment(text):
    return {"comment": {"text": text, "author": "@boss"}}


# --- reading a card -------------------------------------------------------------------------


def test_card_from_reads_the_stored_row():
    c = card(status="Blocked", assignee="@dev", labels=["duplicate-of:KD-3", "infra"], dependencies=["KD-3", "kd-4"],
             priority="high", revision=9)
    assert (c.status, c.assignees, c.project, c.priority, c.revision) == ("Blocked", ("@dev",), "digipaddock", "high", 9)
    assert c.labels == ("duplicate-of:KD-3", "infra")
    assert c.dependencies == ("KD-3", "KD-4")
    assert card(assignee="").assignees == ()


def test_card_from_shows_the_servers_as_labels():
    # a card server is a card_servers row; the rules keep judging it as a server: label.
    c = card(labels=["infra"], servers=["node-c", "node-l"])
    assert c.labels == ("infra", "server:node-c", "server:node-l")


@pytest.mark.parametrize("over", [{"id": "KD-0"}, {"id": "KD-7\n"}, {"revision": 0}, {"revision": "3"},
                                  {"revision": True}, {"revision": None}])
def test_card_from_refuses_a_bad_record(over):
    with pytest.raises(bb.Refused):
        bb.card_from(record(**over))
    with pytest.raises(bb.Refused):
        bb.card_from(["KD-7"])


# --- the finite operations ------------------------------------------------------------------


def test_operation_set_is_finite_with_no_generic_edit():
    assert bb.OPERATIONS == (
        "comment",
        "assign",
        "set-project",
        "set-priority",
        "set-labels",
        "set-dependency",
        "create-inbox",
        "create-child",
        "unblock",
        "promote",
    )


@pytest.mark.parametrize(
    "op",
    [
        "edit",
        "set-status",
        "done",
        "review",
        "approve",
        "move",
        "archive",
        "attention-ack",
        None,
    ],
)
def test_unknown_operations_are_refused(op):
    c = card()
    with pytest.raises(bb.Refused, match="unknown operation"):
        bb.plan(req(op, c), ROUTING, c)


def test_comment_is_signed_and_authored_by_boss():
    c = card()
    edit = bb.plan(req("comment", c, reason="status?"), ROUTING, c)
    assert edit == bb.Edit("KD-7", 3, comment("[boss] status?"))


def test_assign_replaces_assignee_with_reason():
    c = card(assignee="@dev")
    edit = bb.plan(req("assign", c, assignee="@sysadmin", reason="работа на серверах"), ROUTING, c)
    assert edit.changes == {"assignee": "@sysadmin", **comment("[boss] assign @sysadmin: работа на серверах")}


@pytest.mark.parametrize(
    "target",
    ["@boss", "@nobody", "dev", "@dev,@sysadmin", ["@sysadmin"], None],
)
def test_assign_refuses_unknown_targets_and_self(target):
    c = card()
    with pytest.raises(bb.Refused):
        bb.plan(req("assign", c, assignee=target), ROUTING, c)


def test_assign_to_owner_is_allowed():
    c = card()
    assert bb.plan(req("assign", c, assignee="@owner"), ROUTING, c).changes["assignee"] == "@owner"


SPECS = {"@dev": frozenset({"implementer", "reviewer", "test-author", "tester", "docs"}),
         "@sysadmin": frozenset(), "@boss": frozenset()}


def test_assign_with_spec_writes_assignee_and_label_once():
    plan = bb.plan({"op": "assign", "actor": "boss", "task": "KD-7", "revision": 3, "reason": "r",
                    "assignee": "@dev", "spec": "implementer"}, ROUTING, card(labels=["spec:docs"]),
                   specs={"@dev": {"implementer", "docs"}})
    assert plan.changes == {"assignee": "@dev", "labels_add": ["spec:implementer"], "labels_remove": ["spec:docs"],
                            "comment": {"text": "[boss] assign @dev (spec:implementer): r", "author": "@boss"}}
    assert plan.revision == 3 and plan.guards == {}


def test_assign_with_spec_sets_assignee_and_label_in_one_edit():
    c = card(labels=["spec:implementer", "x"])
    edit = bb.plan(req("assign", c, assignee="@dev", spec="test-author"), ROUTING, c, specs=SPECS)
    assert (edit.changes["assignee"], edit.changes["labels_add"], edit.changes["labels_remove"]) == (
        "@dev", ["spec:test-author"], ["spec:implementer"])
    assert edit.changes["comment"]["text"].startswith("[boss] assign @dev (spec:test-author): ")
    # The same assignee with a new label is a change; the same assignee and label is not.
    same = card(assignee="@dev", labels=["spec:implementer"])
    assert bb.plan(req("assign", same, assignee="@dev", spec="reviewer"), ROUTING, same,
                   specs=SPECS).changes["labels_add"] == ["spec:reviewer"]
    with pytest.raises(bb.Refused, match="nothing to change"):
        bb.plan(req("assign", same, assignee="@dev", spec="implementer"), ROUTING, same, specs=SPECS)


def test_assign_without_spec_drops_a_label_the_target_cannot_follow():
    # Final review: the receiver never sees a new assignee with a specialization it does not hold
    # (spec 5.8.5) — one write, no second set-labels.
    c = card(assignee="@dev", labels=["spec:test-author", "x"])
    edit = bb.plan(req("assign", c, assignee="@sysadmin"), ROUTING, c, specs=SPECS)
    assert (edit.changes["assignee"], edit.changes["labels_remove"]) == ("@sysadmin", ["spec:test-author"])
    kept = card(assignee="@sysadmin", labels=["spec:test-author"])
    changes = bb.plan(req("assign", kept, assignee="@dev"), ROUTING, kept, specs=SPECS).changes
    assert changes["assignee"] == "@dev" and "labels_remove" not in changes
    with pytest.raises(bb.Refused, match="no specialization registry"):
        bb.plan(req("assign", c, assignee="@sysadmin"), ROUTING, c)
    plain = card(assignee="@dev")
    assert bb.plan(req("assign", plain, assignee="@sysadmin"), ROUTING, plain).changes["assignee"] == "@sysadmin"


@pytest.mark.parametrize("assignee, spec, why", [
    ("@dev", "task-brief", "not a specialization of @dev"),
    ("@sysadmin", "implementer", "not a specialization of @sysadmin"),
    ("@dev", "Bad Name", "invalid spec"),
    ("@owner", "implementer", "not a specialization of @owner"),
])
def test_assign_refuses_a_spec_the_target_does_not_have(assignee, spec, why):
    c = card()
    with pytest.raises(bb.Refused, match=why):
        bb.plan(req("assign", c, assignee=assignee, spec=spec), ROUTING, c, specs=SPECS)
    with pytest.raises(bb.Refused, match="no specialization registry"):
        bb.plan(req("assign", c, assignee="@dev", spec="reviewer"), ROUTING, c)


def test_set_project_only_from_routes():
    c = card(status="Inbox")
    assert bb.plan(req("set-project", c, project="admin"), ROUTING, c).changes["project"] == "admin"
    for bad in ["digipaddock", "secrets", "admin\n", ["admin"]]:
        with pytest.raises(bb.Refused):
            bb.plan(req("set-project", c, project=bad), ROUTING, c)


@pytest.mark.parametrize("status", ["To Do", "In Progress", "Review", "Blocked", "Done", "Cancelled"])
def test_set_project_moves_only_an_inbox_card(status):
    # the owner started the card in its project: its repository, its route, who sees it
    c = card(status=status)
    with pytest.raises(bb.Refused, match="only in Inbox"):
        bb.plan(req("set-project", c, project="admin"), ROUTING, c)


def test_set_priority_only_configured_values():
    c = card(priority="low")
    assert bb.plan(req("set-priority", c, priority="high"), ROUTING, c).changes["priority"] == "high"
    for bad in ["low", "urgent", "", 1]:
        with pytest.raises(bb.Refused):
            bb.plan(req("set-priority", c, priority=bad), ROUTING, c)


def test_set_labels_adds_and_removes_only_effective_changes():
    c = card(labels=["infra", "old"])
    edit = bb.plan(req("set-labels", c, add=["infra", "server:node-c"], remove=["old", "gone"]), ROUTING, c)
    assert edit.changes == {"labels_add": ["server:node-c"], "labels_remove": ["old"],
                            **comment("[boss] labels +server:node-c -old: по таблице routes")}


def test_set_labels_takes_the_incident_label_and_nothing_like_it():
    # P-<n> links a card to the monitor's incident; other capitals stay refused
    c = card()
    edit = bb.plan(req("set-labels", c, add=["P-463"], remove=[]), ROUTING, c)
    assert edit.changes["labels_add"] == ["P-463"]
    for bad in ("P-0", "P-", "P-12a", "Prod", "P-463,P-1", "P-1234567890"):
        with pytest.raises(bb.Refused):
            bb.plan(req("set-labels", c, add=[bad], remove=[]), ROUTING, c)


def test_set_labels_adds_a_spec_label_only_of_the_assignees_registry():
    c = card(assignee="@dev", labels=["spec:docs"])
    edit = bb.plan(req("set-labels", c, add=["spec:reviewer"], remove=["spec:docs"]), ROUTING, c, specs=SPECS)
    assert (edit.changes["labels_add"], edit.changes["labels_remove"]) == (["spec:reviewer"], ["spec:docs"])
    nobody = card()  # no assignee yet: a specialization some agent holds
    assert bb.plan(req("set-labels", nobody, add=["spec:docs"], remove=[]), ROUTING, nobody,
                   specs=SPECS).changes["labels_add"] == ["spec:docs"]
    # a label nobody's specialization names is still dropped without the registry (spec_mismatch's fix)
    odd = card(assignee="@dev", labels=["spec:made-up"])
    assert bb.plan(req("set-labels", odd, add=[], remove=["spec:made-up"]), ROUTING, odd).changes[
        "labels_remove"] == ["spec:made-up"]


@pytest.mark.parametrize("over, add, remove, why", [
    ({"assignee": "@dev"}, ["spec:made-up"], [], "not a specialization of @dev"),
    ({"assignee": "@sysadmin"}, ["spec:reviewer"], [], "not a specialization of @sysadmin"),
    ({}, ["spec:made-up"], [], "no agent's specialization"),
    ({"assignee": "@dev"}, ["spec:Reviewer"], [], "invalid spec"),
    ({"assignee": "@dev", "labels": ["spec:docs"]}, ["spec:reviewer"], [], "one spec: label"),
    ({"assignee": "@dev"}, ["spec:docs", "spec:reviewer"], [], "one spec: label"),
])
def test_set_labels_refuses_a_spec_label_the_registry_does_not_know(over, add, remove, why):
    c = card(**over)
    with pytest.raises(bb.Refused, match=why):
        bb.plan(req("set-labels", c, add=add, remove=remove), ROUTING, c, specs=SPECS)
    with pytest.raises(bb.Refused, match="no specialization registry"):
        bb.plan(req("set-labels", c, add=add, remove=remove), ROUTING, c)


def test_duplicate_label_does_not_become_a_dependency():
    c = card()
    edit = bb.plan(req("set-labels", c, add=["duplicate-of:KD-3"], remove=[]), ROUTING, c)
    assert edit.changes["labels_add"] == ["duplicate-of:KD-3"]
    assert "dependencies" not in edit.changes
    assert edit.guards == {}


@pytest.mark.parametrize(
    "add,remove",
    [
        (["a,b"], []),
        (["Infra"], []),
        (["duplicate-of:KD-7"], []),
        (["duplicate-of:x"], []),
        (["x"], ["x"]),
        (["x", "x"], []),
        (["infra"], []),
        ([], ["absent"]),
        ([f"l{i}" for i in range(bb.LABELS_MAX + 1)], []),
        ("infra", []),
    ],
)
def test_set_labels_refusals(add, remove):
    c = card(labels=["infra"])
    with pytest.raises(bb.Refused):
        bb.plan(req("set-labels", c, add=add, remove=remove), ROUTING, c)


def test_set_dependency_writes_the_full_structured_list():
    c = card(dependencies=["KD-3", "KD-4"])
    edit = bb.plan(req("set-dependency", c, add=["KD-9"], remove=["KD-3"]), ROUTING, c)
    assert edit.changes["dependencies"] == ["KD-4", "KD-9"]
    edit = bb.plan(req("set-dependency", c, add=[], remove=["KD-3", "KD-4"]), ROUTING, c)
    assert edit.changes["dependencies"] == []


@pytest.mark.parametrize(
    "add,remove",
    [
        (["KD-7"], []),
        (["KD-3"], []),
        ([], ["KD-5"]),
        (["KD-1,KD-2"], []),
        (["kd-5"], []),
        ([], []),
    ],
)
def test_set_dependency_refusals(add, remove):
    c = card(dependencies=["KD-3"])
    with pytest.raises(bb.Refused):
        bb.plan(req("set-dependency", c, add=add, remove=remove), ROUTING, c)


def test_set_dependency_refuses_more_than_can_be_pinned():
    c = card(dependencies=[f"KD-{i}" for i in range(1, bb.DEPENDENCIES_MAX + 1)])
    with pytest.raises(bb.Refused):
        bb.plan(req("set-dependency", c, add=["KD-99"], remove=[]), ROUTING, c)


LOOK = {"token": "f" * 32, "reviewed": [], "difference": ""}  # prepare-create's look, nothing similar


def test_create_inbox_has_fixed_status_and_routed_project():
    create = bb.plan(req("create-inbox", title="Разбить KD-7", project="digipaddock", **LOOK), ROUTING)
    assert create == bb.Create({"title": "Разбить KD-7", "status": "Inbox", "project": "digipaddock",
                                "description": "[boss] по таблице routes", "created_by": "@boss"})


def test_create_inbox_keeps_the_duplicate_check_out_of_the_description():
    # the owner reads the card; what BOSS compared stays in the audit journal
    look = {"token": "f" * 32, "reviewed": ["KD-9032"], "difference": "там голос, здесь файлы"}
    create = bb.plan(req("create-inbox", title="Вложения", project="digipaddock", **look), ROUTING)
    assert create.values["description"] == "[boss] по таблице routes"


@pytest.mark.parametrize(
    "fields",
    [
        {"title": "--status Done", "project": "digipaddock", **LOOK},
        {"title": "", "project": "digipaddock", **LOOK},
        {"title": "x\ny", "project": "digipaddock", **LOOK},
        {"title": "x" * (bb.TITLE_MAX + 1), "project": "digipaddock", **LOOK},
        {"title": "x", "project": "nope", **LOOK},
        {"title": "x", "project": "digipaddock", "status": "To Do", **LOOK},
        {"title": "x", "project": "digipaddock", "assignee": "@sysadmin", **LOOK},
        {"title": "x", "project": "digipaddock", "task": "KD-7", **LOOK},
        {"title": "x", **LOOK},
        {"title": "x", "project": "digipaddock"},  # no look first
        {"title": "x", "project": "digipaddock", **{**LOOK, "token": "nope"}},
        {"title": "x", "project": "digipaddock", **{**LOOK, "reviewed": ["rm -rf"]}},
    ],
)
def test_create_inbox_refusals(fields):
    with pytest.raises(bb.Refused):
        bb.plan(req("create-inbox", **fields), ROUTING)


CHILD = {"goal": "KD-7", "grant": "a" * 32, "title": "Часть A", "project": "digipaddock", "assignee": "@dev"}


def test_create_child_lands_in_inbox_on_the_routed_agent_naming_goal_and_grant():
    create = bb.plan(req("create-child", **CHILD), ROUTING)
    assert create == bb.Create({
        "title": "Часть A", "status": "Inbox", "project": "digipaddock", "assignee": "@dev", "created_by": "@boss",
        "description": "[boss] Подзадача цели KD-7 (разрешение владельца aaaaaaaa): по таблице routes",
        "parent": "KD-7"})


@pytest.mark.parametrize(
    "over",
    [
        {"assignee": "@sysadmin"},
        {"project": "web", "assignee": "@owner"},
        {"project": "nope"},
        {"grant": "x"},
        {"goal": "KD-0"},
        {"title": "-x"},
        {"status": "To Do"},
    ],
)
def test_create_child_refusals(over):
    with pytest.raises(bb.Refused):
        bb.plan(req("create-child", **{**CHILD, **over}), ROUTING)


# --- forbidden fields, actor, reason, revision ---------------------------------------------


@pytest.mark.parametrize(
    "op,fields",
    [
        ("comment", {"status": "Done"}),
        ("comment", {"title": "x"}),
        ("comment", {"description": "x"}),
        ("comment", {"ac": "x"}),
        ("assign", {"assignee": "@sysadmin", "status": "In Progress"}),
        ("set-project", {"project": "admin", "assignee": "@sysadmin"}),
        ("set-priority", {"priority": "high", "labels": ["x"]}),
        ("set-labels", {"add": ["x"], "remove": [], "dep": "KD-3"}),
        ("unblock", {"status": "Done"}),
        ("unblock", {"status": "In Progress"}),
        ("promote", {"status": "In Progress"}),
        ("promote", {"assignee": "@dev"}),
    ],
)
def test_forbidden_fields_are_refused(op, fields):
    c = card(status="Blocked", dependencies=["KD-3"])
    with pytest.raises(bb.Refused, match="field not allowed"):
        bb.plan(req(op, c, **fields), ROUTING, c, {"KD-3": card("KD-3", status="Done")})


@pytest.mark.parametrize("actor", ["owner", "dev", "@boss", "", None])
def test_actor_must_be_boss(actor):
    c = card()
    with pytest.raises(bb.Refused, match="actor"):
        bb.plan(req("comment", c, actor=actor), ROUTING, c)


@pytest.mark.parametrize(
    "reason",
    ["", "   ", "two\nlines", "esc\x1b[31m", "x" * (bb.REASON_MAX + 1), None, ["x"]],
)
def test_reason_is_required_single_line_and_bounded(reason):
    c = card()
    with pytest.raises(bb.Refused, match="reason"):
        bb.plan(req("comment", c, reason=reason), ROUTING, c)


def test_reason_is_only_the_comments_text():
    c = card()
    edit = bb.plan(req("comment", c, reason="--status Done --title x"), ROUTING, c)
    assert edit.changes == comment("[boss] --status Done --title x")


def test_missing_revision_or_card_is_refused():
    c = card()
    request = req("comment", c)
    del request["revision"]
    with pytest.raises(bb.Refused, match="missing field"):
        bb.plan(request, ROUTING, c)
    with pytest.raises(bb.Refused, match="snapshot"):
        bb.plan(req("comment", c), ROUTING, None)
    for bad in ("A" * 64, "3", 0, True, None):
        with pytest.raises(bb.Refused, match="invalid revision"):
            bb.plan(req("comment", c, revision=bad), ROUTING, c)
    with pytest.raises(bb.Refused, match="another card"):
        bb.plan(req("comment", c, task="KD-8"), ROUTING, c)


def test_stale_revision_is_refused():
    with pytest.raises(bb.StaleRevision):
        bb.plan({"op": "comment", "actor": "boss", "task": "KD-7", "revision": 2, "reason": "r"}, ROUTING, card())


def test_stale_revision_is_refused_before_any_plan():
    old = card()
    fresh = card(status="In Progress", revision=4)
    with pytest.raises(bb.StaleRevision):
        bb.plan(req("assign", old, assignee="@sysadmin"), ROUTING, fresh)


# --- unblock --------------------------------------------------------------------------------


def test_unblock_moves_blocked_to_todo_and_pins_every_dependency():
    dep3 = card("KD-3", status="Done", revision=5)
    dep4 = card("KD-4", status="Done", revision=6)
    c = card(status="Blocked", dependencies=["KD-3", "KD-4"])
    edit = bb.plan(req("unblock", c, reason="зависимости закрыты"), ROUTING, c, {"KD-3": dep3, "KD-4": dep4})
    assert edit.changes == {"status": "To Do", **comment("[boss] unblock: KD-3, KD-4 Done: зависимости закрыты")}
    assert edit.guards == {"KD-3": 5, "KD-4": 6}


@pytest.mark.parametrize(
    "status,deps,dep_status,given",
    [
        ("In Progress", ["KD-3"], "Done", ["KD-3"]),
        ("Inbox", ["KD-3"], "Done", ["KD-3"]),
        ("Blocked", [], "Done", []),
        ("Blocked", ["KD-3"], "Review", ["KD-3"]),
        ("Blocked", ["KD-3"], "To Do", ["KD-3"]),
        ("Blocked", ["KD-3", "KD-4"], "Done", ["KD-3"]),
        ("Blocked", ["KD-3"], "Done", ["KD-3", "KD-4"]),
        ("Blocked", ["KD-3", "KD-3"], "Done", ["KD-3"]),
        ("Blocked", ["see KD-3"], "Done", ["KD-3"]),
    ],
)
def test_unblock_refusals(status, deps, dep_status, given):
    snaps = {d: card(d, status=dep_status) for d in given}
    c = card(status=status, dependencies=deps)
    with pytest.raises(bb.Refused):
        bb.plan(req("unblock", c), ROUTING, c, snaps)


def test_unblock_refuses_a_snapshot_of_another_card():
    c = card(status="Blocked", dependencies=["KD-3"])
    other = card("KD-5", status="Done")
    with pytest.raises(bb.Refused):
        bb.plan(req("unblock", c), ROUTING, c, {"KD-3": other})


# --- promote (asked of the owner by the policy) ---------------------------------------------


def test_promote_moves_inbox_to_todo_with_a_signed_comment():
    c = card(status="Inbox", assignee="@sysadmin")
    edit = bb.plan(req("promote", c, reason="владелец согласен"), ROUTING, c)
    assert edit == bb.Edit("KD-7", 3, {"status": "To Do", **comment("[boss] To Do: владелец согласен")})


@pytest.mark.parametrize(
    "status,assignee",
    [
        ("To Do", "@dev"),
        ("Blocked", "@dev"),
        ("Done", "@dev"),
        ("Inbox", ""),
        ("Inbox", "@boss"),
        ("Inbox", "@stranger"),
    ],
)
def test_promote_refusals(status, assignee):
    c = card(status=status, assignee=assignee)
    with pytest.raises(bb.Refused):
        bb.plan(req("promote", c), ROUTING, c)


def test_promote_refuses_a_project_outside_routes():
    c = card(status="Inbox", assignee="@dev", project="misc")
    with pytest.raises(bb.Refused, match="project"):
        bb.plan(req("promote", c), ROUTING, c)


# --- routing --------------------------------------------------------------------------------


def test_routing_never_offers_boss_and_includes_owner():
    assert "@boss" not in ROUTING.assignees
    assert {"@owner", "@dev", "@sysadmin"} <= ROUTING.assignees


@pytest.mark.parametrize(
    "routes,agents",
    [
        ({"digipaddock": "@ghost"}, ["dev"]),
        ({"digipaddock": "@boss"}, ["boss"]),
        ({"Digipaddock": "@owner"}, []),
        (["digipaddock"], []),
        ({"digipaddock": "@owner"}, ["Bad Name"]),
    ],
)
def test_routing_refuses_unknown_targets_and_bad_shapes(routes, agents):
    with pytest.raises(bb.Refused):
        bb.routing_from(routes, agents)


def test_a_board_without_projects_routes_nothing():
    # a new cabinet's table may be empty; that is no project, not a broken board
    routing = bb.routing_from({}, ["dev"])
    assert routing.projects == {} and "@dev" in routing.assignees


def test_create_child_sets_the_goal_as_parent():
    """a subtask BOSS makes is the goal's child on the board, not only in its description."""
    create = bb.plan(req("create-child", **CHILD), ROUTING)
    assert create.values["parent"] == "KD-7"
