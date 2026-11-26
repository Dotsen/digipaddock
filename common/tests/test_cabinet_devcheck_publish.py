"""``kd-forge check --publish`` (KD-9460): the lead orders the publish of its PR with the check; a green end
publishes it here with no turn of the lead, and the lead's wait on the check goes on as the wait for the owner's
decision (or the code council's round). Whatever the cabinet cannot carry on wakes the lead as before."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import council_env as ce  # noqa: E402
import test_cabinet_devchecks as dc  # noqa: E402
from test_cabinet_devchecks import HEAD, ask, ended  # noqa: E402
from test_cabinet_outcomes import begin, end, finish  # noqa: E402
from test_cabinet_prs import _approve, _pr  # noqa: E402

from kd import board  # noqa: E402
from kd.cabinet import api, continuations, devchecks, prs_api, waits  # noqa: E402

env, cab = dc.env, dc.cab  # the devchecks' fixtures: KD-50 of @dev in work, its contour


def row(**over):
    """The PR as the forge executor reads it with the check: the check's head, its required checks."""
    base = _pr(
        "KD-50",
        head=HEAD,
        repo="acme/app",
        number=7,
        url="https://forge.example/acme/app/pulls/7",
        checks=["unit"],
        target="main",
        effect="",
    )
    base.update(over)
    return base


def waiting(cab, op_id, card="KD-50"):
    """The lead's turn on the card ends on the check's wait."""
    record = cab.store.card(card)
    command, session = begin(cab, record)
    finish(
        cab,
        record,
        outcome="wait_dependency",
        note="waits for the check",
        operations=[op_id],
    )
    end(cab, command, session)
    return command


def waits_of(cab, command):
    return [
        (w["kind"], w["ref"], w["state"]) for w in waits.of_command(cab.store, command)
    ]


def queued(cab, card="KD-50"):
    return [
        c
        for c in cab.store.commands(card=card, states=["queued"])
        if c["agent"] == "dev"
    ]


def pr_decisions(cab, card="KD-50", state="open"):
    found = cab.call("root", {"op": "decision.list", "card": card, "state": state})[
        "decisions"
    ]
    return [d for d in found if d["type"] == "pr_approve"]


def test_a_green_check_publishes_the_pr_and_the_wait_goes_on_to_the_owners_decision(
    cab,
):
    answer = ask(cab, publish=row())
    assert answer["created"] is True and answer["publish"] is True
    op = answer["operation"]
    command = waiting(cab, op["id"])
    ended(cab, op["id"])
    [decision] = pr_decisions(cab)
    assert decision["payload"]["head"] == HEAD and decision["payload"]["checks"] == [
        "unit"
    ]
    assert decision["requested_by"] == "agent:dev"
    assert cab.store.pr("acme/app", 7, diff=False)["head"] == HEAD
    assert waits_of(cab, command) == [("decision", decision["id"], "open")]
    assert queued(cab) == []  # no turn of the lead: the owner decides first
    followed = [e for e in cab.store.events_since(0) if e["kind"] == "wait.followed"]
    assert [
        (e["card"], e["payload"]["to_kind"], e["payload"]["to_ref"]) for e in followed
    ] == [("KD-50", "decision", decision["id"])]
    assert cab.store.get_state(devchecks.ORDER + op["id"]) is None
    # the owner's approval is the one wake, the wait's continuation goes in it
    _approve(cab, decision)
    wakes = queued(cab)
    assert [c["text"] for c in wakes] == [
        board.PR_WAKE["merge"].format(id="KD-50", pr=7, sha7=HEAD[:7])
    ]
    assert waits_of(cab, command) == [("decision", decision["id"], "resolved")]


def test_the_order_is_kept_with_the_check_until_its_end(cab):
    op = ask(cab, publish=row())["operation"]
    kept = cab.store.get_state(devchecks.ORDER + op["id"])
    assert kept == {
        "KD-50": {"agent": "dev", "row": prs_api.validate(row()), "before": None}
    }  # the cabinet had no row of the PR when it was ordered
    again = ask(
        cab, publish=row(title="KD-50: a better title")
    )  # the same check, a newer order
    assert again["created"] is False and again["publish"] is True
    assert (
        cab.store.get_state(devchecks.ORDER + op["id"])["KD-50"]["row"]["title"]
        == "KD-50: a better title"
    )
    assert (
        ask(cab)["publish"] is False
    )  # a repeat without an order leaves the kept one as it is
    assert (
        cab.store.get_state(devchecks.ORDER + op["id"])["KD-50"]["row"]["title"]
        == "KD-50: a better title"
    )


def test_a_red_check_publishes_nothing_and_wakes_the_lead(cab):
    op = ask(cab, publish=row())["operation"]
    command = waiting(cab, op["id"])
    ended(cab, op["id"], "checks_failed")
    assert cab.store.pr("acme/app", 7, diff=False) is None and pr_decisions(cab) == []
    assert waits_of(cab, command) == [("operation", op["id"], "rejected")]
    [wake] = queued(cab)
    assert wake["text"].startswith(continuations.ENDED.split("(")[0].format(id="KD-50"))
    assert cab.store.get_state(devchecks.ORDER + op["id"]) is None


def test_a_check_that_passed_already_orders_nothing(cab):
    op = ask(cab)["operation"]
    ended(cab, op["id"])
    again = ask(cab, publish=row())
    assert (
        again["created"] is False and again["publish"] is False
    )  # kd-forge publishes itself, as before
    assert cab.store.get_state(devchecks.ORDER + op["id"]) is None


def test_a_refused_publish_leaves_nothing_and_wakes_the_lead(cab, monkeypatch):
    op = ask(cab, publish=row())["operation"]
    command = waiting(cab, op["id"])
    real = prs_api._publish

    def refusing(ctx, principal, request):
        real(
            ctx, principal, request
        )  # it wrote, then refused: the savepoint takes it all back
        raise api.ApiError("refused", "no")

    monkeypatch.setattr(prs_api, "_publish", refusing)
    ended(cab, op["id"])
    assert cab.store.pr("acme/app", 7, diff=False) is None and pr_decisions(cab) == []
    assert waits_of(cab, command) == [("operation", op["id"], "resolved")]
    assert [c["text"] for c in queued(cab)] == [
        continuations.RESOLVED.format(id="KD-50")
    ]
    assert operations_state(cab, op["id"]) == "succeeded"


def operations_state(cab, op_id):
    return cab.store._conn.execute(
        "SELECT state FROM operations WHERE id=?", (op_id,)
    ).fetchone()[0]


def test_a_head_another_signature_may_carry_to_wakes_the_lead(cab):
    signed = cab.call("digi-forge", {"op": "pr.publish", "pr": row(head="e" * 40)})[
        "decision"
    ]
    _approve(cab, signed)
    for command in queued(cab):
        for state in ("delivered", "started", "done"):
            cab.store.command_state(command["id"], state)
    op = ask(cab, publish=row())["operation"]
    command = waiting(cab, op["id"])
    ended(cab, op["id"])
    assert (
        cab.store.pr("acme/app", 7, diff=False)["head"] == "e" * 40
    )  # kd-forge publish asks kd-review: the lead's
    assert waits_of(cab, command) == [("operation", op["id"], "resolved")]
    assert [c["text"] for c in queued(cab)] == [
        continuations.RESOLVED.format(id="KD-50")
    ]


def test_a_superseded_check_that_still_reports_green_publishes_nothing(cab):
    """dev pushed H2 and ordered its check: the H1 check the host already ran is flagged superseded — its late
    green must not bring H1 back into the cabinet nor open the owner's decision on it."""
    old = ask(cab, publish=row())["operation"]
    devchecks.delivered(cab.ctx, old["id"])
    newer = "e" * 40
    ask(cab, head=newer, publish=row(head=newer))
    assert dc.operations.row(cab.store, old["id"])["params"]["cancel"] == "superseded"
    ended(cab, old["id"])
    assert cab.store.pr("acme/app", 7, diff=False) is None and pr_decisions(cab) == []


def test_a_pr_that_changed_in_the_cabinet_since_the_order_is_not_published(cab):
    op = ask(cab, publish=row())["operation"]
    command = waiting(cab, op["id"])
    cab.call(
        "digi-forge", {"op": "pr.publish", "pr": row(state="closed")}
    )  # closed while the check ran
    ended(cab, op["id"])
    assert cab.store.pr("acme/app", 7, diff=False)["state"] == "closed"  # not reopened
    assert pr_decisions(cab) == []
    assert waits_of(cab, command) == [("operation", op["id"], "resolved")]
    assert [c["text"] for c in queued(cab)] == [
        continuations.RESOLVED.format(id="KD-50")
    ]


def test_a_pr_published_before_the_order_is_published_on_its_new_head(cab):
    cab.call(
        "digi-forge", {"op": "pr.publish", "pr": row(head="e" * 40)}
    )  # the head the owner had remarks on
    op = ask(cab, publish=row())["operation"]
    command = waiting(cab, op["id"])
    ended(cab, op["id"])
    [decision] = pr_decisions(cab)
    assert decision["payload"]["head"] == HEAD
    assert waits_of(cab, command) == [("decision", decision["id"], "open")]


def test_a_card_that_left_the_lead_publishes_nothing(cab):
    op = ask(cab, publish=row())["operation"]
    order = cab.store.get_state(devchecks.ORDER + op["id"])["KD-50"]
    cab.put("KD-50", status="Blocked")
    assert prs_api.publish_ordered(cab.ctx, order["row"], "dev") is None
    cab.put("KD-50", status="In Progress", assignee="@analyst")
    assert prs_api.publish_ordered(cab.ctx, order["row"], "dev") is None


def test_another_cards_wait_on_the_same_check_ends_as_before(cab):
    cab.put("KD-51", project="digipaddock", assignee="@dev", status="In Progress")
    for command in cab.store.commands(card="KD-51", states=["queued"]):
        for state in ("delivered", "started", "done"):
            cab.store.command_state(command["id"], state)
    op = ask(cab, publish=row())["operation"]
    assert (
        ask(cab, card="KD-51")["operation"]["id"] == op["id"]
    )  # joined from KD-51, nothing ordered there
    mine, theirs = waiting(cab, op["id"]), waiting(cab, op["id"], card="KD-51")
    ended(cab, op["id"])
    [decision] = pr_decisions(cab)
    assert waits_of(cab, mine) == [("decision", decision["id"], "open")]
    assert waits_of(cab, theirs) == [("operation", op["id"], "resolved")]
    assert [c["text"] for c in queued(cab, "KD-51")] == [
        continuations.RESOLVED.format(id="KD-51")
    ]


def test_a_council_round_the_publish_starts_is_what_the_wait_goes_on_to(
    cab, monkeypatch
):
    op = ask(cab, publish=row())["operation"]
    command = waiting(cab, op["id"])
    council = {"id": "f" * 32, "state": "running"}
    monkeypatch.setattr(
        prs_api,
        "publish_ordered",
        lambda ctx, r, agent, before: {"pr": r, "decision": None, "council": council},
    )
    ended(cab, op["id"])
    assert waits_of(cab, command) == [("council", council["id"], "open")]
    assert queued(cab) == []


@pytest.mark.parametrize(
    "over",
    [
        {"head": "e" * 40},
        {"number": 8},
        {"repo": "acme/other"},
        {"card": "KD-51"},
        {"target": "dev"},
        {"state": "closed"},
        {"checks": []},
        {"checks": ["unit", "e2e"]},
        {"head": "short"},
    ],
)
def test_an_order_names_the_pr_of_the_check(cab, over):
    with pytest.raises(api.ApiError) as exc:
        ask(cab, publish=row(**over))
    assert exc.value.kind == "invalid", exc.value
    assert cab.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 0


# --- the code council: the round the publish starts, findings the lead owes --------------------------------
@pytest.fixture
def council_cab(tmp_path):
    return ce.make(tmp_path)


def test_publish_ordered_starts_the_code_councils_round(council_cab):
    card = ce.coding(council_cab)
    answer = prs_api.publish_ordered(council_cab.ctx, ce.pull(card.id), "dev")
    assert answer["decision"] is None and answer["council"]["state"] == "running"


def test_publish_ordered_waits_for_the_answers_to_the_councils_findings(council_cab):
    card = ce.coding(council_cab)
    ce.publish(council_cab, ce.pull(card.id))
    found = ce.play(
        council_cab,
        card.id,
        ce.block("with_fixes", [ce.finding()]),
        ce.block("ready"),
        kind="code",
    )
    assert found["state"] == "needs_writer"
    fix = ce.pull(card.id, head="c" * 40)
    before = prs_api.current(
        council_cab.store, fix
    )  # the head the council read, as at the order
    assert prs_api.publish_ordered(council_cab.ctx, fix, "dev", before) is None
    assert (
        ce.council_of(council_cab, card.id, "code")["round"] == 1
    )  # no round started on the unanswered
    ce.started(council_cab, card.id, agent="dev")
    ce.reply(council_cab, card.id, {"r1.f1": "fixed"}, agent="dev")
    answer = prs_api.publish_ordered(council_cab.ctx, fix, "dev", before)
    assert (answer["council"]["state"], answer["council"]["round"]) == ("running", 2)
