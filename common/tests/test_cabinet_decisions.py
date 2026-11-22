import hashlib
import json

import pytest

from kd import admin_proposal as admin_proposal_store
from kd import boss_board, boss_promote, delegation
from kd.cabinet import api, decisions
from cabinet_env import CabinetEnv
from admin_samples import ENABLED, INVENTORY, action as action_data


def test_sysadmin_opens_a_plan_decision_only_for_its_own_card(tmp_path, admin_proposal):
    env = CabinetEnv(tmp_path)
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    payload = admin_proposal(
        env, "KD-193"
    )  # writes the proposal file as the agent, returns the payload
    out = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": payload,
        },
    )
    d = out["decision"]
    assert d["type"] == "admin_plan" and d["state"] == "open" and d["step_up"] is True
    msgs = env.store.messages("KD-193")
    assert msgs[-1]["kind"] == "decision" and msgs[-1]["author"] == "agent:sysadmin"
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-dev",
            {
                "op": "decision.open",
                "type": "admin_plan",
                "card": "KD-193",
                "payload": payload,
            },
        )
    assert exc.value.kind == "refused"


def test_challenge_binds_decision_user_revision_and_expiry(tmp_path):
    env = CabinetEnv(tmp_path)
    d = decisions.Decisions(env.store)
    card = env.card(project="digipaddock")
    one = d.open(
        "question",
        card=card.id,
        payload={"text": "Deploy now?"},
        requested_by="agent:dev",
        now=env.now,
    )
    a = d.digest(
        one, user="owner", expires="2026-10-15T10:00:00Z", card_revision=card.revision
    )
    assert a != d.digest(
        one, user="anna", expires="2026-10-15T10:00:00Z", card_revision=card.revision
    )
    assert a != d.digest(
        one,
        user="owner",
        expires="2026-10-15T10:00:00Z",
        card_revision=card.revision + 1,
    )
    two = d.open(
        "question",
        card=card.id,
        payload={"text": "And tests?"},
        requested_by="agent:dev",
        now=env.now,
    )
    batch = d.challenge(
        [one["id"], two["id"]],
        user="owner",
        expires="x",
        revisions={card.id: card.revision},
    )
    assert batch == d.challenge(
        [two["id"], one["id"]],
        user="owner",
        expires="x",
        revisions={card.id: card.revision},
    )
    assert d.challenge(
        [one["id"]], user="owner", expires="x", revisions={card.id: card.revision}
    ) == d.digest(one, user="owner", expires="x", card_revision=card.revision)


def test_digests_match_challenge(tmp_path):
    """a target recomputes the signed value from each decision's hex digest; ``challenge`` is built from
    the same digests (one: itself; several: sha256 over the sorted hex)."""
    env = CabinetEnv(tmp_path)
    d = decisions.Decisions(env.store)
    card = env.card(project="digipaddock")
    one = d.open("question", card=card.id, payload={"text": "Deploy now?"}, requested_by="agent:dev", now=env.now)
    two = d.open("question", card=card.id, payload={"text": "And tests?"}, requested_by="agent:dev", now=env.now)
    ids, rev = [one["id"], two["id"]], {card.id: card.revision}
    digests = d.digests(ids, user="owner", expires="x", revisions=rev)
    assert len(digests) == 2 and digests[0] != digests[1] and all(len(h) == 64 for h in digests)
    assert bytes.fromhex(digests[0]) == d.digest(one, user="owner", expires="x", card_revision=card.revision)
    assert d.digests(ids[::-1], user="owner", expires="x", revisions=rev) == digests[::-1]  # in ``ids`` order
    assert d.challenge(ids, user="owner", expires="x", revisions=rev) == hashlib.sha256(
        "".join(sorted(digests)).encode("ascii")
    ).digest()
    assert d.challenge(ids[:1], user="owner", expires="x", revisions=rev) == bytes.fromhex(digests[0])
    with pytest.raises(decisions.DecisionError):
        d.digests([], user="owner", expires="x", revisions=rev)


def test_approving_a_plan_writes_the_grant_with_the_signature_and_promotes_the_card(
    tmp_path, admin_proposal
):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    payload = admin_proposal(env, "KD-193")
    d = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": payload,
        },
    )["decision"]
    out = env.call(
        "digi-cabinet",
        {
            "op": "decision.approve",
            "ids": [d["id"]],
            "user": "owner",
            "signature": {"id": "cred", "response": {}},
            "credential": 1,
            "revisions": {"KD-193": env.store.card("KD-193").revision},
            "ttl_hours": 24,
        },
    )
    assert (
        out["decisions"][0]["state"] == "approved" and out["decisions"][0]["grant_id"]
    )
    grant = env.admin.store.load("KD-193")
    assert (
        grant.state == "active"
        and grant.signature
        and grant.approved_by == "user:owner"
    )
    assert (
        grant.signer == "cred"
        and grant.expires_at - grant.approved_at
        == __import__("datetime").timedelta(hours=24)
    )
    assert env.store.card("KD-193").status == "To Do"
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-cabinet",
            {
                "op": "decision.approve",
                "ids": [d["id"]],
                "user": "owner",
                "signature": {},
                "credential": 1,
                "revisions": {"KD-193": 1},
            },
        )
    assert exc.value.kind == "refused"  # already decided


def test_a_changed_card_scope_refuses_the_approval(tmp_path, admin_proposal):
    env = CabinetEnv(tmp_path)
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    payload = admin_proposal(env, "KD-193")
    d = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": payload,
        },
    )["decision"]
    stale = env.store.card("KD-193").revision
    env.put("KD-193", description="changed by the agent")
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-cabinet",
            {
                "op": "decision.approve",
                "ids": [d["id"]],
                "user": "owner",
                "signature": {},
                "credential": 1,
                "revisions": {"KD-193": stale},
            },
        )
    assert exc.value.kind == "conflict" and env.admin.store.load("KD-193") is None
    current = env.store.card(
        "KD-193"
    ).revision  # the page reloads: the scope hash itself no longer matches
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-cabinet",
            {
                "op": "decision.approve",
                "ids": [d["id"]],
                "user": "owner",
                "signature": {"id": "k"},
                "credential": 1,
                "revisions": {"KD-193": current},
            },
        )
    assert exc.value.kind == "conflict" and env.admin.store.load("KD-193") is None


def test_partner_may_answer_a_question_but_not_approve_a_plan(tmp_path, admin_proposal):
    env = CabinetEnv(tmp_path)
    env.users.create(
        "anna", "Анна", roles=[("partner", ["web", "admin"])], actor="root"
    )
    card = env.card(project="web", assignee="@dev")
    q = env.call(
        "digi",
        {
            "op": "decision.open",
            "type": "question",
            "agent": "dev",
            "card": card.id,
            "payload": {"text": "Pay?"},
        },
    )["decision"]
    out = env.call(
        "digi-cabinet",
        {
            "op": "decision.reply",
            "id": q["id"],
            "user": "anna",
            "text": "Да, до пятницы",
        },
    )
    assert (
        out["decision"]["state"] == "approved"
        and out["decision"]["reply"] == "Да, до пятницы"
    )
    assert env.store.messages(card.id)[-1]["author"] == "user:anna"
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    d = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": admin_proposal(env, "KD-193"),
        },
    )["decision"]
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-cabinet",
            {
                "op": "decision.approve",
                "ids": [d["id"]],
                "user": "anna",
                "signature": {},
                "credential": 1,
                "revisions": {"KD-193": env.store.card("KD-193").revision},
            },
        )
    assert exc.value.kind == "refused"


def test_expiry_closes_open_decisions_with_a_ttl(tmp_path):
    env = CabinetEnv(tmp_path)
    d = decisions.Decisions(env.store)
    card = env.card()
    q = d.open(
        "question",
        card=card.id,
        payload={"text": "?"},
        requested_by="agent:dev",
        now=env.now,
    )
    p = d.open(
        "promote",
        card=card.id,
        payload={"proposal_id": "a" * 32, "proposal_sha256": "b" * 64},
        requested_by="agent:boss",
        now=env.now,
    )
    gone = d.expire("2026-10-18T10:00:01Z")
    assert [x["id"] for x in gone] == [p["id"]] and d.get(q["id"])["state"] == "open"
    assert (
        d.get(p["id"])["state"] == "expired"
        and env.store.messages(card.id)[-1]["payload"]["state"] == "expired"
    )


# --- beyond the plan's list ---------------------------------------------------------------------------


def _owner(env):
    env.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")


def test_a_plan_decision_must_match_the_stored_proposal(tmp_path, admin_proposal):
    env = CabinetEnv(tmp_path)
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    payload = admin_proposal(env, "KD-193")
    for bad in (
        {**payload, "proposal_sha256": "0" * 64},
        {**payload, "card_scope_sha256": "0" * 64},
        {**payload, "extra": 1},
        {**payload, "task_id": "KD-194"},
    ):
        with pytest.raises(api.ApiError) as exc:
            env.call(
                "digi-sysadmin",
                {
                    "op": "decision.open",
                    "type": "admin_plan",
                    "card": "KD-193",
                    "payload": bad,
                },
            )
        assert exc.value.kind in ("conflict", "invalid"), bad
    for kind in ("question", "promote", "nonsense"):
        with pytest.raises(api.ApiError):
            env.call(
                "digi-sysadmin",
                {
                    "op": "decision.open",
                    "type": kind,
                    "card": "KD-193",
                    "payload": payload,
                },
            )


def test_reject_and_ask_need_text_and_reach_the_thread(tmp_path, admin_proposal):
    env = CabinetEnv(tmp_path)
    _owner(env)
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    d = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": admin_proposal(env, "KD-193"),
        },
    )["decision"]
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-cabinet",
            {"op": "decision.ask", "id": d["id"], "user": "owner", "text": " "},
        )
    assert exc.value.kind == "invalid"
    asked = env.call(
        "digi-cabinet",
        {"op": "decision.ask", "id": d["id"], "user": "owner", "text": "Зачем шаг 2?"},
    )
    assert (
        asked["decision"]["state"] == "open"
        and asked["decision"]["question"] == "Зачем шаг 2?"
    )
    last = env.store.messages("KD-193")[-1]
    assert last["author"] == "user:owner" and last["payload"]["text"] == "Зачем шаг 2?"
    rejected = api.dispatch(
        env.ctx,
        api.Principal("user", -1, "owner"),
        {"op": "decision.reject", "id": d["id"], "text": "Не сейчас"},
    )["decision"]
    assert rejected["state"] == "rejected" and rejected["decided_by"] == "user:owner"
    assert env.admin.store.load("KD-193") is None
    assert [e["kind"] for e in env.store.events_since(0)][-1] == "decision.rejected"


def test_a_question_waits_for_its_agents_words_in_the_thread(tmp_path):
    # "A question to @boss" shows as awaited until the agent that asked for the decision writes in the thread
    env = CabinetEnv(tmp_path)
    card = env.card(project="admin", assignee="@sysadmin")
    ledger = decisions.Decisions(env.store)
    d = ledger.open("promote", card=card.id, payload={}, requested_by="agent:boss", now=env.now)
    assert ledger.asked_open(d) is False
    d = ledger.ask(d["id"], user="owner", text="Почему сейчас?", now=env.now)
    assert ledger.asked_open(d) is True
    env.store.add_message(card.id, author="agent:boss", kind="tool_call", body="kd-task view")
    env.store.add_message(card.id, author="agent:sysadmin", kind="text", body="не мне")
    assert ledger.asked_open(d) is True  # a tool call, another agent: no answer yet
    env.store.add_message(card.id, author="agent:boss", kind="text", body="Потому что повторяется")
    assert ledger.asked_open(d) is False
    d = ledger.ask(d["id"], user="owner", text="А ещё?", now=env.now)
    assert ledger.asked_open(d) is True


def test_a_question_goes_only_where_an_agent_reads_it(tmp_path):
    # a question wakes the agent that asked for the decision, on its card
    from kd.cabinet import monitor_trust

    env = CabinetEnv(tmp_path)
    _owner(env)
    ledger = decisions.Decisions(env.store)
    trust = ledger.open("monitor_trust", card="", payload={"kind": "trust", "cidr": "10.0.0.0/8"},
                        requested_by="user:owner", now=env.now, thread=monitor_trust.thread_of(env.ctx))
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-cabinet", {"op": "decision.ask", "id": trust["id"], "user": "owner", "text": "Зачем?"})
    assert exc.value.kind == "refused" and ledger.get(trust["id"])["question"] == ""


def test_revoking_an_approved_plan_revokes_its_grant(tmp_path, admin_proposal):
    env = CabinetEnv(tmp_path)
    _owner(env)
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    d = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": admin_proposal(env, "KD-193"),
        },
    )["decision"]
    env.call(
        "digi-cabinet",
        {
            "op": "decision.approve",
            "ids": [d["id"]],
            "user": "owner",
            "signature": {"id": "k"},
            "credential": 1,
            "revisions": {"KD-193": env.store.card("KD-193").revision},
        },
    )
    out = env.call(
        "digi-cabinet", {"op": "decision.revoke", "id": d["id"], "user": "owner"}
    )
    assert (
        out["decision"]["state"] == "revoked"
        and env.admin.store.load("KD-193").state == "revoked"
    )
    journal = (
        (tmp_path / "admin" / "grants" / "decisions.jsonl")
        .read_text("utf-8")
        .splitlines()
    )
    assert json.loads(journal[-1])["by"] == "user:owner"


def test_a_critical_action_is_approved_signed_and_consumed_by_exec_once(
    tmp_path, admin_proposal
):
    env = CabinetEnv(tmp_path)
    _owner(env)
    env.put("KD-193", project="admin", assignee="@sysadmin", status="To Do")
    admin_proposal(env, "KD-193")  # the card's server label
    from kd.cabinet import client as cabinet_client

    card = cabinet_client.admin_card(env.client("digi-cabinet"), "KD-193")
    action = admin_proposal_store.submit_action(
        env.admin.paths.proposals,
        json.dumps(action_data(task_id="KD-193")).encode("utf-8"),
        agent="sysadmin",
        card=card,
        inventory=INVENTORY,
        enabled=ENABLED,
        now=env.when(),
        deny=lambda command, alias: None,
    )
    payload = {
        "task_id": "KD-193",
        "proposal_id": action.proposal_id,
        "proposal_sha256": action.sha256,
        "card_scope_sha256": card.scope_sha256,
    }
    d = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_critical",
            "card": "KD-193",
            "payload": payload,
        },
    )["decision"]
    assert d["step_up"] is True and d["expires"] == "2026-10-14T10:30:00Z"
    out = env.call(
        "digi-cabinet",
        {
            "op": "decision.approve",
            "ids": [d["id"]],
            "user": "owner",
            "signature": {"id": "k"},
            "credential": 1,
            "revisions": {"KD-193": env.store.card("KD-193").revision},
        },
    )
    grant_id = out["decisions"][0]["grant_id"]
    consume = {
        "op": "action.consume",
        "task_id": "KD-193",
        "proposal_id": action.proposal_id,
        "action_sha256": env.admin.actions.load(
            "KD-193", action.proposal_id
        ).action_sha256,
    }
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-sysadmin", consume)
    assert exc.value.kind == "refused"
    assert env.call("digi-exec", consume)["action_id"] == grant_id
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-exec", consume)
    assert exc.value.kind == "refused"


def test_a_new_boss_proposal_supersedes_the_open_decision_of_the_same_kind(tmp_path):
    # BOSS proposes the same card again (a new revision or reason) — the older open decision expires, so
    # the
    # Inbox holds one decision per card and kind, and no reminder goes out for a dead one
    env = CabinetEnv(tmp_path)
    card = env.card(project="web", status="Inbox", assignee="@owner", title="Счёт")

    def open_(kind, reason):
        record = env.store.card(card.id)
        propose = (
            boss_promote.propose if kind == "promote" else boss_promote.propose_delegate
        )
        extra = () if kind == "promote" else (delegation.Limits(5, 3, 7),)
        proposal = propose(
            boss_board.card_from(record.to_dict()),
            env.ctx.routing(),
            record.revision,
            reason,
            *extra,
            now=env.epoch(),
            root=env.promote.proposals,
            decisions=env.promote.decisions,
        )
        return env.call(
            "digi",
            {
                "op": "decision.open",
                "type": kind,
                "agent": "boss",
                "card": card.id,
                "payload": {
                    "proposal_id": proposal.id,
                    "proposal_sha256": proposal.sha256,
                },
            },
        )["decision"]

    first, goal = open_("promote", "готово"), open_("delegate", "большая цель")
    second = open_("promote", "готово, срочно")
    listed = {
        s: {
            d["id"]
            for d in env.call("digi-cabinet", {"op": "decision.list", "state": s})[
                "decisions"
            ]
        }
        for s in ("open", "expired")
    }
    assert listed == {"open": {second["id"], goal["id"]}, "expired": {first["id"]}}


def test_boss_promote_is_opened_by_the_executor_and_approved_without_step_up(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    card = env.card(project="web", status="Inbox", assignee="@owner", title="Счёт")
    record = env.store.card(card.id)
    proposal = boss_promote.propose(
        boss_board.card_from(record.to_dict()),
        env.ctx.routing(),
        record.revision,
        "готово к работе",
        now=env.epoch(),
        root=env.promote.proposals,
        decisions=env.promote.decisions,
    )
    payload = {"proposal_id": proposal.id, "proposal_sha256": proposal.sha256}
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi",
            {
                "op": "decision.open",
                "type": "promote",
                "agent": "dev",
                "card": card.id,
                "payload": payload,
            },
        )
    assert exc.value.kind == "refused"
    d = env.call(
        "digi",
        {
            "op": "decision.open",
            "type": "promote",
            "agent": "boss",
            "card": card.id,
            "payload": payload,
        },
    )["decision"]
    assert d["step_up"] is False and d["requested_by"] == "agent:boss"
    listed = api.dispatch(
        env.ctx,
        api.Principal("user", -1, "anna"),
        {"op": "decision.list", "state": "open"},
    )
    assert [x["id"] for x in listed["decisions"]] == [d["id"]]
    out = env.call(
        "digi-cabinet",
        {
            "op": "decision.approve",
            "ids": [d["id"]],
            "user": "anna",
            "signature": None,
            "credential": None,
            "revisions": {card.id: record.revision},
        },
    )
    assert out["decisions"][0]["state"] == "approved"
    assert env.store.card(card.id).status == "To Do"
    assert (
        boss_promote.decision(proposal.id, env.promote.decisions)["caller"]
        == "user:anna"
    )


def test_step_up_types_need_a_signature(tmp_path, admin_proposal):
    env = CabinetEnv(tmp_path)
    _owner(env)
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    d = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": admin_proposal(env, "KD-193"),
        },
    )["decision"]
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-cabinet",
            {
                "op": "decision.approve",
                "ids": [d["id"]],
                "user": "owner",
                "signature": None,
                "credential": None,
                "revisions": {"KD-193": env.store.card("KD-193").revision},
            },
        )
    assert exc.value.kind == "refused" and env.admin.store.load("KD-193") is None
    with pytest.raises(
        api.ApiError
    ) as exc:  # only the HTTP layer, after it checked the assertion
        api.dispatch(
            env.ctx,
            api.Principal("user", -1, "owner"),
            {
                "op": "decision.approve",
                "ids": [d["id"]],
                "user": "owner",
                "signature": {"id": "k"},
                "credential": 1,
                "revisions": {"KD-193": env.store.card("KD-193").revision},
            },
        )
    assert exc.value.kind == "refused"


def test_an_expired_decision_cannot_be_approved(tmp_path, admin_proposal):
    env = CabinetEnv(tmp_path)
    _owner(env)
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    d = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": admin_proposal(env, "KD-193"),
        },
    )["decision"]
    env.now = "2026-10-17T10:00:00Z"
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi-cabinet",
            {
                "op": "decision.approve",
                "ids": [d["id"]],
                "user": "owner",
                "signature": {"id": "k"},
                "credential": 1,
                "revisions": {"KD-193": env.store.card("KD-193").revision},
            },
        )
    assert exc.value.kind == "refused" and env.admin.store.load("KD-193") is None
    assert [
        x["id"] for x in env.call("root", {"op": "decision.expire"})["decisions"]
    ] == [d["id"]]


def test_the_agent_reads_only_its_own_question(tmp_path):
    env = CabinetEnv(tmp_path)
    mine = env.card(project="digipaddock", assignee="@dev")
    q = env.call(
        "digi",
        {
            "op": "decision.open",
            "type": "question",
            "agent": "dev",
            "card": mine.id,
            "payload": {"text": "Ок?"},
        },
    )["decision"]
    assert (
        env.call("digi", {"op": "decision.get", "id": q["id"], "agent": "dev"})[
            "decision"
        ]["state"]
        == "open"
    )
    with pytest.raises(api.ApiError) as exc:
        env.call("digi", {"op": "decision.get", "id": q["id"], "agent": "sysadmin"})
    assert exc.value.kind == "not_found"
    other = env.card(project="admin", assignee="@sysadmin")
    with pytest.raises(api.ApiError) as exc:
        env.call(
            "digi",
            {
                "op": "decision.open",
                "type": "question",
                "agent": "dev",
                "card": other.id,
                "payload": {"text": "?"},
            },
        )
    assert exc.value.kind == "refused"


def test_asking_again_returns_the_open_decision_and_a_new_plan_supersedes_the_old(
    tmp_path, admin_proposal
):
    from admin_samples import proposal

    env = CabinetEnv(tmp_path)
    env.put("KD-193", project="admin", assignee="@sysadmin", status="Inbox")
    payload = admin_proposal(env, "KD-193")
    first = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": payload,
        },
    )["decision"]
    again = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": payload,
        },
    )["decision"]
    assert again["id"] == first["id"]
    from kd.cabinet import client as cabinet_client
    from admin_samples import CATALOG
    from kd import admin_operations as ops

    card = cabinet_client.admin_card(env.client("digi-cabinet"), "KD-193")
    plan = admin_proposal_store.submit(
        env.admin.paths.proposals,
        json.dumps(proposal(task_id="KD-193", intent="Только перезапуск")).encode(
            "utf-8"
        ),
        agent="sysadmin",
        card=card,
        catalog=ops.Catalog.from_dict(json.loads(json.dumps(CATALOG))),
        inventory=INVENTORY,
        enabled=ENABLED,
        now=env.when(),
    )
    second = env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": "KD-193",
            "payload": {**payload, "proposal_sha256": plan.sha256},
        },
    )["decision"]
    assert second["id"] != first["id"]
    assert (
        env.call("root", {"op": "decision.get", "id": first["id"]})["decision"]["state"]
        == "expired"
    )
    assert [
        d["id"]
        for d in env.call("root", {"op": "decision.list", "state": "open"})["decisions"]
    ] == [second["id"]]


def test_opening_and_answering_a_question_add_thread_messages(tmp_path):
    """The question and its answer reach an open thread as message.added events."""
    env = CabinetEnv(tmp_path)
    card = env.card(status="Inbox", assignee="@analyst")
    start = env.store.last_seq()
    book = decisions.Decisions(env.store)
    d = book.open(
        "question", card=card.id, payload={"text": "?"}, requested_by="agent:analyst", now=env.now
    )
    book.reply(d["id"], user="owner", text="да", now=env.now)
    events = env.store.events_since(start)
    assert [e["kind"] for e in events] == [
        "message.added",
        "decision.opened",
        "message.added",
        "decision.approved",
    ]
    assert events[0]["payload"]["via"] == "decision"
    assert events[0]["payload"]["decision"] == d["id"]
    assert events[0]["actor"] == "agent:analyst" and events[2]["actor"] == "user:owner"
    assert events[0]["card"] == card.id


def test_the_same_answer_twice_is_one_decision(tmp_path):
    """a resent answer (a timeout after the commit) changes nothing; another text is refused."""
    env = CabinetEnv(tmp_path)
    card = env.card(status="Inbox", assignee="@analyst")
    book = decisions.Decisions(env.store)
    d = book.open("question", card=card.id, payload={"text": "?"}, requested_by="agent:analyst", now=env.now)
    book.reply(d["id"], user="owner", text="да", now=env.now)
    start = env.store.last_seq()
    again = book.reply(d["id"], user="owner", text="да", now=env.now)
    assert again["state"] == "approved" and again["reply"] == "да"
    assert env.store.last_seq() == start
    with pytest.raises(decisions.DecisionError):
        book.reply(d["id"], user="owner", text="нет", now=env.now)
    with pytest.raises(decisions.DecisionError):
        book.reply(d["id"], user="anna", text="да", now=env.now)


def test_a_transition_event_names_who_asked(tmp_path):
    env = CabinetEnv(tmp_path)
    card = env.card(status="Inbox", assignee="@analyst")
    book = decisions.Decisions(env.store)
    d = book.open("question", card=card.id, payload={"text": "?"}, requested_by="agent:analyst", now=env.now)
    book.reply(d["id"], user="owner", text="да", now=env.now)
    event = env.store.events_since(0)[-1]
    assert event["kind"] == "decision.approved"
    assert event["payload"] == {"decision": d["id"], "type": "question", "requested_by": "agent:analyst"}


# --- the answer to a form is a structure with its readable text ------------------------
def _form_question(env, payload=None, *, card=None):
    from question_samples import form

    if not env.users.get("owner"):
        _owner(env)
    card = card or env.card(status="Inbox", assignee="@dev")
    return card, env.call(
        "digi",
        {"op": "decision.open", "type": "question", "agent": "dev", "card": card.id,
         "payload": payload if payload is not None else form()},
    )["decision"]


def _reply(env, q, **body):
    return env.call("digi-cabinet", {"op": "decision.reply", "id": q["id"], "user": "owner", **body})["decision"]


def _sha(payload):
    from kd.cabinet import questions

    return questions.form_sha(payload)


def test_a_form_answer_is_stored_with_its_readable_text(tmp_path):
    from kd.cabinet import questions

    env = CabinetEnv(tmp_path)
    card, q = _form_question(env)
    start = env.store.last_seq()
    chosen = {"method": {"selected_option_ids": ["ssh"]}}
    d = _reply(env, q, answers=chosen, comment=" Срок — неделя ", form_sha256=_sha(q["payload"]))
    normal = questions.validate_answer(q["payload"], chosen, "")
    assert d["state"] == "approved"
    assert d["answer"] == {
        "schema_version": 1, "origin": "agent", "form_sha256": _sha(q["payload"]),
        "answers": normal, "comment": "Срок — неделя",
    }
    assert d["reply"] == questions.readable(q["payload"], normal, "Срок — неделя")
    assert d["reply"] == "1. Как проверять?: Через SSH\n2. Что ещё учесть?: —\n\nСрок — неделя"
    assert [e["kind"] for e in env.store.events_since(start)].count("decision.approved") == 1
    assert env.store.messages(card.id)[-1]["body"] == d["reply"]


def test_the_same_form_answer_again_is_the_same_decision(tmp_path):
    env = CabinetEnv(tmp_path)
    _, q = _form_question(env)
    body = {"answers": {"method": {"selected_option_ids": ["http"]}}, "form_sha256": _sha(q["payload"])}
    first = _reply(env, q, **body)
    start = env.store.last_seq()
    assert _reply(env, q, **body) == first
    assert env.store.last_seq() == start


def test_another_answer_to_an_answered_form_is_refused(tmp_path):
    env = CabinetEnv(tmp_path)
    _, q = _form_question(env)
    sha = _sha(q["payload"])
    _reply(env, q, answers={"method": {"selected_option_ids": ["http"]}}, form_sha256=sha)
    with pytest.raises(api.ApiError) as exc:
        _reply(env, q, answers={"method": {"selected_option_ids": ["ssh"]}}, form_sha256=sha)
    assert exc.value.kind == "refused"


def test_an_answer_to_another_form_is_a_conflict(tmp_path):
    env = CabinetEnv(tmp_path)
    _, q = _form_question(env)
    with pytest.raises(api.ApiError) as exc:
        _reply(env, q, answers={"method": {"selected_option_ids": ["http"]}}, form_sha256="0" * 64)
    assert exc.value.kind == "conflict"
    assert env.call("root", {"op": "decision.get", "id": q["id"]})["decision"]["state"] == "open"


def test_a_plain_text_reply_to_an_agent_form_is_refused(tmp_path):
    env = CabinetEnv(tmp_path)
    _, q = _form_question(env)
    with pytest.raises(api.ApiError) as exc:
        _reply(env, q, text="да")
    assert exc.value.kind == "invalid" and "answer the form" in str(exc.value)
    with pytest.raises(api.ApiError) as exc:  # a required question left out
        _reply(env, q, answers={}, form_sha256=_sha(q["payload"]))
    assert exc.value.kind == "invalid" and "method is required" in str(exc.value)


def test_a_parsed_question_takes_its_form_or_text(tmp_path):
    from kd.cabinet.questions_legacy import recognize
    from question_samples import LEGACY

    env = CabinetEnv(tmp_path)
    _, q = _form_question(env, {"text": LEGACY})
    parsed = recognize(LEGACY)
    d = _reply(env, q, answers={"q1": {"selected_option_ids": ["o2"]}}, form_sha256=_sha(parsed))
    assert d["answer"]["origin"] == "parsed" and d["answer"]["form_sha256"] == _sha(parsed)
    assert d["reply"] == "1. Как проверять?: Б\n2. Что ещё учесть?: —"
    _, other = _form_question(env, {"text": LEGACY}, card=env.card(status="Inbox", assignee="@dev"))
    assert (_reply(env, other, text="А, остальное — как считаешь")["answer"]) is None
    _, plain = _form_question(env, {"text": "Ок?"}, card=env.card(status="Inbox", assignee="@dev"))
    with pytest.raises(api.ApiError) as exc:
        _reply(env, plain, answers={}, form_sha256="0" * 64)
    assert exc.value.kind == "invalid" and "no form" in str(exc.value)


def test_a_form_answer_wakes_the_asker_once(tmp_path):
    from kd.cabinet import commands

    env = CabinetEnv(tmp_path)
    commands.install(env.store, lambda agent: agent == "dev")
    card, q = _form_question(env)
    body = {"answers": {"method": {"selected_option_ids": ["http"]}}, "form_sha256": _sha(q["payload"])}
    _reply(env, q, **body)
    _reply(env, q, **body)
    assert [c["kind"] for c in env.store.commands(card=card.id)].count("answer") == 1


# --- an approval wakes the agent that asked for it -------------------------------------------------------
def _approve(env, d):
    return env.call(
        "digi-cabinet",
        {"op": "decision.approve", "ids": [d["id"]], "user": "owner", "signature": {"id": "k"}, "credential": 1,
         "revisions": {d["card"]: env.store.card(d["card"]).revision}},
    )


def _plan_decision(env, admin_proposal, status):
    from kd.cabinet import commands

    _owner(env)
    commands.install(env.store, lambda agent: agent == "sysadmin")
    env.put("KD-193", project="admin", assignee="@sysadmin", status=status)
    payload = admin_proposal(env, "KD-193")
    return env.call(
        "digi-sysadmin", {"op": "decision.open", "type": "admin_plan", "card": "KD-193", "payload": payload}
    )["decision"]


@pytest.mark.parametrize("status", ["In Progress", "To Do", "Inbox"])
def test_an_approved_plan_wakes_the_sysadmin_once(tmp_path, admin_proposal, status):
    # the card was already In Progress — no status change, so nothing woke the sysadmin after the approval
    from kd import board

    env = CabinetEnv(tmp_path)
    d = _plan_decision(env, admin_proposal, status)
    before = {c["id"] for c in env.store.commands(card="KD-193")}  # a card put in To Do is a cue of its own
    _approve(env, d)
    # an Inbox card's promote is part of the approval: no second wake
    [command] = [c for c in env.store.commands(card="KD-193") if c["id"] not in before]
    assert (command["agent"], command["kind"]) == ("sysadmin", "assigned")
    assert command["text"] == board.PLAN_WAKE.format(id="KD-193")
    assert command["initiator"] == "user:owner"
    assert board.PLAN_WAKE == ("[board] {id}: the owner approved your plan in the cabinet — take the card with "
                               "kd-task claim {id} if it is in To Do, then run its steps")


def test_an_approved_critical_command_wakes_the_sysadmin_with_its_id(tmp_path, admin_proposal):
    from kd import board
    from kd.cabinet import client as cabinet_client
    from kd.cabinet import commands

    env = CabinetEnv(tmp_path)
    _owner(env)
    commands.install(env.store, lambda agent: agent == "sysadmin")
    env.put("KD-193", project="admin", assignee="@sysadmin", status="In Progress")
    admin_proposal(env, "KD-193")
    card = cabinet_client.admin_card(env.client("digi-cabinet"), "KD-193")
    action = admin_proposal_store.submit_action(
        env.admin.paths.proposals, json.dumps(action_data(task_id="KD-193")).encode("utf-8"), agent="sysadmin",
        card=card, inventory=INVENTORY, enabled=ENABLED, now=env.when(), deny=lambda command, alias: None,
    )
    payload = {"task_id": "KD-193", "proposal_id": action.proposal_id, "proposal_sha256": action.sha256,
               "card_scope_sha256": card.scope_sha256}
    d = env.call(
        "digi-sysadmin", {"op": "decision.open", "type": "admin_critical", "card": "KD-193", "payload": payload}
    )["decision"]
    _approve(env, d)
    [command] = env.store.commands(card="KD-193")
    # the approval lives 15 minutes, so its wake is urgent — the host starts it at once
    assert (command["agent"], command["kind"]) == ("sysadmin", "urgent")
    assert command["text"] == board.ACTION_WAKE.format(id="KD-193", action=action.proposal_id)
    assert board.ACTION_WAKE == ("[board] {id}: the owner approved your critical command {action} in the cabinet "
                                 "— run it once with kd-admin-critical --task {id} --action {action}")


def test_a_refused_plan_wakes_nobody(tmp_path, admin_proposal):
    env = CabinetEnv(tmp_path)
    d = _plan_decision(env, admin_proposal, "In Progress")
    env.call("digi-cabinet", {"op": "decision.reject", "id": d["id"], "user": "owner", "text": "не сейчас"})
    assert env.store.commands(card="KD-193") == []


def test_a_closed_card_closes_its_open_decisions(tmp_path):
    """a card that reaches Done or Cancelled has nothing left to decide — its open questions (and any
    other open decision) are withdrawn in the same write, with a line in the thread."""
    env = CabinetEnv(tmp_path)
    d = decisions.Decisions(env.store)
    card = env.card(status="In Progress", assignee="@dev")
    q = d.open("question", card=card.id, payload={"text": "?"}, requested_by="agent:dev", now=env.now)
    env.store.edit_card(card.id, None, {"status": "Review"}, actor="user:owner")
    assert d.get(q["id"])["state"] == "open"
    env.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")
    closed = d.get(q["id"])
    assert closed["state"] == "revoked" and closed["decided_by"] == "user:owner"
    last = [m for m in env.store.thread_messages(env.store.card(card.id).thread) if m["kind"] == "decision"][-1]
    assert last["payload"]["key"] == "sys.decision.card_closed" and last["payload"]["params"]["status"] == "Done"
    assert "Done" in closed["reply"]
    other = env.card(status="In Progress", assignee="@dev")
    q2 = d.open("question", card=other.id, payload={"text": "?"}, requested_by="agent:dev", now=env.now)
    env.store.edit_card(other.id, None, {"status": "Cancelled", "outcome": "cancelled",
                                         "comment": {"text": "не нужно", "author": "@owner"}}, actor="user:owner")
    assert d.get(q2["id"])["state"] == "revoked"


# --- a plan of test environments is approved by the rule, not by the owner ---------------------------------
def _zone(env, tmp_path, status="Inbox", data=None, again=False):
    """The sysadmin's card with a stored test-zone plan; the payload of its admin_plan decision (``again`` — the
    next plan of the same card)."""
    import copy

    from admin_samples import ZONE_CATALOG, zone_proposal
    from kd import admin_operations as ops
    from kd.cabinet import client as cabinet_client

    catalog = lambda: ops.Catalog.from_dict(copy.deepcopy(ZONE_CATALOG))  # noqa: E731
    env.admin.catalog = catalog
    if not again:
        _owner(env)
        env.put("KD-193", project="admin", assignee="@sysadmin", status=status, servers=["node-c"])
    card = cabinet_client.admin_card(env.client("digi-cabinet"), "KD-193")
    plan = admin_proposal_store.submit(
        env.admin.paths.proposals, json.dumps(data or zone_proposal(task_id="KD-193")).encode("utf-8"),
        agent="sysadmin", card=card, catalog=catalog(), inventory=INVENTORY, enabled=ENABLED, now=env.when())
    return {"task_id": "KD-193", "proposal_sha256": plan.sha256, "card_scope_sha256": card.scope_sha256}


def _open_plan(env, payload):
    return env.call("digi-sysadmin", {"op": "decision.open", "type": "admin_plan", "card": "KD-193",
                                      "payload": payload})


def test_a_test_zone_plan_is_approved_by_the_rule_without_asking_the_owner(tmp_path):
    from kd import admin_grant
    from kd.cabinet import commands

    env = CabinetEnv(tmp_path)
    commands.install(env.store, lambda agent: agent == "sysadmin")
    payload = _zone(env, tmp_path)
    seq = env.store.events_since(0, 1000)[-1]["seq"]
    out = _open_plan(env, payload)
    grant = env.admin.store.load("KD-193")
    assert grant.approved_by == admin_grant.TEST_ZONE and grant.signature == ""
    assert out["approved"] == {"grant": grant.grant_id, "expires": admin_grant.fmt(grant.expires_at),
                               "envs": ["shop-test"]}
    d = out["decision"]
    assert (d["type"], d["state"], d["decided_by"], d["grant_id"]) == (
        "admin_plan", "approved", "rule:test-env", grant.grant_id)
    assert d["requested_by"] == "agent:sysadmin" and d["signature"] is None
    # nothing waits for the owner, nothing rings, nothing wakes: the agent is told in the answer it is reading
    assert env.call("digi-cabinet", {"op": "decision.list", "state": "open"})["decisions"] == []
    kinds = [e["kind"] for e in env.store.events_since(seq, 1000)]
    assert "admin.plan.rule_approved" in kinds
    assert not {"decision.opened", "decision.approved"} & set(kinds)
    assert [c for c in env.store.commands(card="KD-193") if c["kind"] == "assigned"] == []
    assert env.store.card("KD-193").status == "To Do"
    last = env.store.messages("KD-193")[-1]
    assert last["kind"] == "decision" and last["author"] == "system"
    assert last["payload"]["key"] == "sys.decision.rule_approved"
    assert last["payload"]["params"] == {"envs": "shop-test", "steps": 2}
    assert last["payload"]["state"] == "approved" and last["payload"]["decision"] == d["id"]


def test_a_plan_with_a_step_outside_the_zone_goes_to_the_owner(tmp_path):
    from admin_samples import ZONE_STEPS, explain, zone_proposal

    env = CabinetEnv(tmp_path)
    mixed = zone_proposal(task_id="KD-193", operations=ZONE_STEPS + [
        explain({"server": "node-c", "verb": "service.restart", "unit": "nginx.service"})])
    out = _open_plan(env, _zone(env, tmp_path, data=mixed))
    assert "approved" not in out and out["decision"]["state"] == "open" and out["decision"]["step_up"] is True
    assert env.admin.store.load("KD-193") is None and env.store.card("KD-193").status == "Inbox"


def test_proposing_the_same_test_zone_plan_again_answers_the_same_approval(tmp_path):
    env = CabinetEnv(tmp_path)
    payload = _zone(env, tmp_path, status="To Do")
    first = _open_plan(env, payload)
    lines = len(env.store.messages("KD-193"))
    again = _open_plan(env, payload)
    assert again["decision"]["id"] == first["decision"]["id"] and again["approved"] == first["approved"]
    assert len(env.store.messages("KD-193")) == lines  # no second line in the thread


def test_the_owner_revokes_a_rule_approval_like_his_own(tmp_path):
    env = CabinetEnv(tmp_path)
    d = _open_plan(env, _zone(env, tmp_path, status="To Do"))["decision"]
    out = env.call("digi-cabinet", {"op": "decision.revoke", "id": d["id"], "user": "owner"})
    assert out["decision"]["state"] == "revoked" and env.admin.store.load("KD-193").state == "revoked"


def test_a_rule_approval_closes_the_open_question_of_an_older_plan(tmp_path, admin_proposal):
    import copy

    from admin_samples import CATALOG
    from kd import admin_operations as ops

    env = CabinetEnv(tmp_path)
    env.put("KD-193", project="admin", assignee="@sysadmin", status="To Do")
    old = _open_plan(env, admin_proposal(env, "KD-193"))["decision"]  # an ordinary plan: the owner is asked
    assert old["state"] == "open"
    out = _open_plan(env, _zone(env, tmp_path, status="To Do"))
    assert out["decision"]["state"] == "approved"
    assert env.call("digi-cabinet", {"op": "decision.get", "id": old["id"]})["decision"]["state"] == "expired"
    env.admin.catalog = lambda: ops.Catalog.from_dict(copy.deepcopy(CATALOG))


def test_only_the_sysadmins_own_card_reaches_the_rule(tmp_path):
    env = CabinetEnv(tmp_path)
    payload = _zone(env, tmp_path)
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-dev", {"op": "decision.open", "type": "admin_plan", "card": "KD-193", "payload": payload})
    assert exc.value.kind == "refused" and env.admin.store.load("KD-193") is None


def test_after_the_owners_revoke_the_next_test_zone_plan_asks_him(tmp_path):
    """A revoked rule approval is not handed out again on the next propose — the plan opens the
    owner's decision like any other."""
    env = CabinetEnv(tmp_path)
    payload = _zone(env, tmp_path, status="To Do")
    first = _open_plan(env, payload)["decision"]
    env.call("digi-cabinet", {"op": "decision.revoke", "id": first["id"], "user": "owner"})
    out = _open_plan(env, payload)
    assert "approved" not in out and out["decision"]["state"] == "open" and out["decision"]["id"] != first["id"]
    assert env.admin.store.load("KD-193").state == "revoked"


def test_the_rules_start_of_a_card_is_not_the_owners_word(tmp_path):
    """The rule moves the Inbox card to To Do — nothing in the thread or the events says the
    owner did it."""
    env = CabinetEnv(tmp_path)
    payload = _zone(env, tmp_path)
    seq = env.store.events_since(0, 1000)[-1]["seq"]
    count = len(env.store.messages("KD-193"))
    _open_plan(env, payload)
    assert env.store.card("KD-193").status == "To Do"
    written = env.store.messages("KD-193")[count:]
    assert written and not [m for m in written if m["author"].startswith("user:")]
    moved = [m for m in written if "To Do" in (m.get("body") or "")]
    assert moved and moved[0]["author"] == "system" and "[owner]" not in moved[0]["body"]
    assert not [e for e in env.store.events_since(seq, 1000) if e["actor"].startswith("user:")]


def test_the_rule_does_not_start_a_card_that_carries_a_brief(tmp_path):
    """Starting a card approves its brief and grants the brief's "Operations" in the name of the
    person who started it — a rule is no person: such a card's plan goes to the owner, nothing is approved."""
    from kd.cabinet import briefs

    env = CabinetEnv(tmp_path)
    payload = _zone(env, tmp_path)
    env.store.edit_card("KD-193", None, {"labels_add": ["brief-ready"]}, actor="agent:sysadmin")
    assert briefs.state(env.store, env.store.card("KD-193"))["state"] == "ready"
    out = _open_plan(env, payload)
    assert "approved" not in out and out["decision"]["state"] == "open"
    assert env.store.card("KD-193").status == "Inbox" and env.admin.store.load("KD-193") is None
    last = briefs.latest(env.store, "KD-193")
    assert last is None or not last["approved_by"]


def test_a_promote_by_rule_refuses_a_card_with_a_brief_and_approves_none(tmp_path):
    """The cabinet's own stop — whatever the caller checked, ``card.promote`` with a rule never
    approves a brief, and names the rule only quietly."""
    from kd.cabinet import briefs

    env = CabinetEnv(tmp_path)
    card = env.briefed(project="admin")
    card = env.store.edit_card(card.id, None, {"assignee": "@sysadmin"}, actor="user:owner")
    ask = {"op": "card.promote", "card": card.id, "revision": card.revision, "reason": "правило", "quiet": True,
           "rule": "rule:test-env"}
    with pytest.raises(Exception, match="brief"):
        env.call("digi-cabinet", ask)
    assert env.store.card(card.id).status == "Inbox" and not briefs.latest(env.store, card.id)["approved_by"]
    plain = env.card(status="Inbox", project="admin", assignee="@sysadmin")
    for bad in ({"rule": "rule:other", "quiet": True}, {"rule": "rule:test-env", "quiet": False},
                {"rule": "rule:test-env", "quiet": True, "by": "user:anna"}):
        with pytest.raises(Exception):
            env.call("digi-cabinet", {"op": "card.promote", "card": plain.id, "revision": plain.revision,
                                      "reason": "правило", **bad})
    assert env.store.card(plain.id).status == "Inbox"


def test_the_rule_answering_a_live_approval_still_closes_an_older_open_question(tmp_path):
    """Seen on the stand: the zone plan, then a plan with a step outside the zone (the owner
    is asked), then the zone plan again — the question about the plan that is no longer proposed is closed."""
    from admin_samples import ZONE_STEPS, explain, zone_proposal

    env = CabinetEnv(tmp_path)
    payload = _zone(env, tmp_path, status="To Do")
    first = _open_plan(env, payload)
    mixed = zone_proposal(task_id="KD-193", operations=ZONE_STEPS + [
        explain({"server": "node-c", "verb": "service.restart", "unit": "nginx.service"})])
    asked = _open_plan(env, _zone(env, tmp_path, data=mixed, again=True))["decision"]
    assert asked["state"] == "open"
    again = _open_plan(env, _zone(env, tmp_path, again=True))
    assert again["decision"]["id"] == first["decision"]["id"] and again["approved"] == first["approved"]
    assert env.call("digi-cabinet", {"op": "decision.get", "id": asked["id"]})["decision"]["state"] == "expired"
    assert env.call("digi-cabinet", {"op": "decision.list", "state": "open"})["decisions"] == []
