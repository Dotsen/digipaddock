"""dev's pull requests on the board — the forge executor publishes them, the cabinet keeps the latest of each
and opens one ``pr_approve`` decision per head; the owner's signed approval records its mode and wakes dev."""

import base64

import pytest

from kd.cabinet import api, passkeys, prs_api as prs
from cabinet_env import CabinetEnv

HEAD, HEAD2 = "a" * 40, "b" * 40


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    e.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    return e


def _pr(card, head=HEAD, **over):
    pr = {
        "repo": "owner/repo", "project": "digipaddock", "number": 12, "card": card, "title": f"{card}: a change",
        "url": "https://forge.example/owner/repo/pulls/12", "head": head, "state": "open",
        "files": [{"path": "common/kd/x.py", "status": "modified", "additions": 3, "deletions": 1},
                  {"path": "docs/x.md", "status": "added", "additions": 10, "deletions": 0}],
        "cage": ["common/kd/x.py"], "diff": "diff --git a/common/kd/x.py b/common/kd/x.py\n+x\n", "truncated": False,
        "release": True,
    }
    pr.update(over)
    return pr


def _publish(env, pr, user="digi-forge"):
    return env.call(user, {"op": "pr.publish", "pr": pr})


def _open_pr_decisions(env, card):
    return [d for d in env.call("root", {"op": "decision.list", "card": card, "state": "open"})["decisions"]
            if d["type"] == "pr_approve"]


def test_publish_opens_one_decision_per_head(env):
    card = env.card(status="Review")
    first = _publish(env, _pr(card.id))
    decision = first["decision"]
    assert decision["type"] == "pr_approve" and decision["requested_by"] == "agent:dev"
    assert decision["payload"] == {"kind": "pr_approve", "repo": "owner/repo", "project": "digipaddock", "number": 12,
                                   "head": HEAD, "url": "https://forge.example/owner/repo/pulls/12",
                                   "title": f"{card.id}: a change", "release": True, "target": "main", "effect": "",
                                   "checks": []}
    assert first["pr"]["head"] == HEAD and first["pr"]["cage"] == ["common/kd/x.py"]
    assert "diff" not in first["pr"]
    again = _publish(env, _pr(card.id))
    assert again["decision"]["id"] == decision["id"]
    assert len(_open_pr_decisions(env, card.id)) == 1
    newer = _publish(env, _pr(card.id, head=HEAD2))
    assert newer["decision"]["id"] != decision["id"]
    assert [d["id"] for d in _open_pr_decisions(env, card.id)] == [newer["decision"]["id"]]
    assert env.call("root", {"op": "decision.get", "id": decision["id"]})["decision"]["state"] == "expired"
    row = env.store.pr("owner/repo", 12)
    assert row["head"] == HEAD2 and row["diff"].startswith("diff --git") and row["release"] is True
    kinds = [e["kind"] for e in env.store.events_since(0)]
    assert kinds.count("pr.published") == 3


def test_publish_keeps_an_approved_head(env):
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id))["decision"]
    env.store._conn.execute("UPDATE decisions SET state='approved' WHERE id=?", (decision["id"],))
    again = _publish(env, _pr(card.id))
    assert again["decision"]["id"] == decision["id"] and again["decision"]["state"] == "approved"
    assert _open_pr_decisions(env, card.id) == []


@pytest.mark.parametrize("over", [
    {"repo": "no-slash"}, {"repo": "a/b/c"}, {"number": 0}, {"number": "12"}, {"head": "a" * 39},
    {"head": "A" * 40}, {"state": "draft"}, {"files": [{"path": "x", "status": "modified", "additions": 1,
                                                       "deletions": 0}] * 1001},
    {"cage": ["not/among/files.py"]}, {"diff": "x" * (384 * 1024 + 1)}, {"url": "http://forge.example/x"},
    {"title": "x" * 301}, {"truncated": "no"}, {"release": 1}, {"project": "Bad Project"},
    {"files": [{"path": "", "status": "modified", "additions": 1, "deletions": 0}]},
    {"target": "../x"}, {"target": ""}, {"target": "x.lock"}, {"target": 1}, {"effect": "x" * 301}, {"effect": 1},
    {"checks": ["Unit"]}, {"checks": "unit"}, {"checks": ["unit", "unit"]}, {"extra": 1},
])
def test_publish_validates_the_pr(env, over):
    card = env.card(status="Review")
    with pytest.raises(api.ApiError) as exc:
        _publish(env, _pr(card.id, **over))
    assert exc.value.kind == "invalid"


def test_publish_needs_an_agent_card(env):
    with pytest.raises(api.ApiError) as exc:
        _publish(env, _pr("KD-999"))
    assert exc.value.kind == "not_found"
    for assignee in ("@owner", ""):
        card = env.card(status="Review", assignee=assignee)
        with pytest.raises(api.ApiError) as exc:
            _publish(env, _pr(card.id))
        assert exc.value.kind == "refused"


def test_only_forge_publishes(env):
    card = env.card(status="Review")
    for user in ("digi-dev", "digi", "digi-cabinet", "digi-exec"):
        with pytest.raises(api.ApiError) as exc:
            _publish(env, _pr(card.id), user=user)
        assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, api.Principal("user", -1, "owner"), {"op": "pr.publish", "pr": _pr(card.id)})
    assert exc.value.kind == "refused"


@pytest.mark.parametrize("state", ["closed", "merged"])
def test_closed_pr_releases_accept_and_expires_the_decision(env, state):
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id))["decision"]
    if state == "closed":  # a re-publish of a PR closed in Forgejo
        out = _publish(env, _pr(card.id, state="closed"))
        assert out["decision"] is None
    else:  # kd-merge after its merge
        out = env.call("digi-forge", {"op": "pr.state", "repo": "owner/repo", "number": 12, "state": "merged"})
    assert out["pr"]["state"] == state
    assert env.call("root", {"op": "decision.get", "id": decision["id"]})["decision"]["state"] == "expired"
    assert env.store.prs(card=card.id, state="open") == []
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-forge", {"op": "pr.state", "repo": "owner/repo", "number": 13, "state": "merged"})
    assert exc.value.kind == "not_found"
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-forge", {"op": "pr.state", "repo": "owner/repo", "number": 12, "state": "open"})
    assert exc.value.kind == "invalid"


def test_an_ended_pr_is_recorded_whoever_holds_its_card(env):
    # final review: a PR closed after its card went back to the owner still ends its decision
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id))["decision"]
    env.store.edit_card(card.id, None, {"assignee": "@owner"}, actor="user:owner")
    out = _publish(env, _pr(card.id, state="closed"))
    assert out["decision"] is None and out["pr"]["state"] == "closed"
    assert env.call("root", {"op": "decision.get", "id": decision["id"]})["decision"]["state"] == "expired"
    with pytest.raises(api.ApiError) as exc:  # an open PR still needs an agent to ask for the approval
        _publish(env, _pr(card.id, head=HEAD2))
    assert exc.value.kind == "refused"


def test_a_pr_moved_to_another_card_ends_the_old_cards_decision(env):
    first, second = env.card(status="Review"), env.card(status="Review")
    old = _publish(env, _pr(first.id))["decision"]
    new = _publish(env, _pr(second.id, title=f"{second.id}: a change"))["decision"]
    assert env.call("root", {"op": "decision.get", "id": old["id"]})["decision"]["state"] == "expired"
    assert _open_pr_decisions(env, first.id) == [] and [d["id"] for d in _open_pr_decisions(env, second.id)] == [
        new["id"]]


def test_the_cabinet_records_a_pr_that_ended_in_forgejo(env):
    # the web asks kd-review before a signature; a PR merged or closed there is recorded by the cabinet itself
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id))["decision"]
    out = env.call("digi-cabinet", {"op": "pr.state", "repo": "owner/repo", "number": 12, "state": "closed"})
    assert out["pr"]["state"] == "closed"
    assert env.call("root", {"op": "decision.get", "id": decision["id"]})["decision"]["state"] == "expired"
    for user in ("digi-dev", "digi"):
        with pytest.raises(api.ApiError) as exc:
            env.call(user, {"op": "pr.state", "repo": "owner/repo", "number": 12, "state": "merged"})
        assert exc.value.kind == "refused"


def test_signer_keys_are_root_only(env):
    for n, signing in ((1, True), (2, False)):
        registered = passkeys.Registered(credential_id=bytes([n]) * 16, public_key=bytes([n]) * 77, sign_count=0,
                                         transports=[])
        row = env.users.add_credential("owner", registered, f"key {n}", env.now)
        if not signing:
            env.store._conn.execute("UPDATE credentials SET signing_enabled=0 WHERE id=?", (row["id"],))
    keys = env.call("root", {"op": "signer.keys", "user": "owner"})["keys"]
    b64 = lambda raw: base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")  # noqa: E731
    assert keys == [{"credential_id": b64(b"\x01" * 16), "public_key": b64(b"\x01" * 77), "name": "key 1",
                     "created": env.now}]
    for user in ("digi-cabinet", "digi-forge", "digi"):
        with pytest.raises(api.ApiError) as exc:
            env.call(user, {"op": "signer.keys", "user": "owner"})
        assert exc.value.kind == "refused"
    with pytest.raises(api.ApiError) as exc:
        env.call("root", {"op": "signer.keys", "user": "nobody-here"})
    assert exc.value.kind == "not_found"


# --- the owner's answer (Task 3) ---------------------------------------------------------------------------------
from kd import board, session_config  # noqa: E402
from kd.cabinet import flow  # noqa: E402

BINDING = {"expires": "2026-10-14T10:05:00Z", "nonce": "00" * 16}


def _hosted(env):
    api.install_host(env.ctx, lambda: session_config.HostConfig({"dev": 2}, 600))


def _approve(env, decision, mode="merge", review=None, **extra):
    card = env.store.card(decision["card"])
    request = {"op": "decision.approve", "ids": [decision["id"]], "user": "owner",
               "revisions": {card.id: card.revision}, "signature": {"id": "key", "binding": {**BINDING, "mode": mode}},
               "credential": None, **extra}
    if review is not False:
        request["review"] = review or {"id": 77, "head": decision["payload"]["head"]}
    return env.call("digi-cabinet", request)


def test_approve_records_mode_and_review_and_wakes_dev(env):
    _hosted(env)
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id))["decision"]
    before = len(env.store.commands(card=card.id))
    [done] = _approve(env, decision)["decisions"]
    assert done["state"] == "approved" and done["grant_id"] == "77"
    assert done["answer"] == {"mode": "merge", "review": 77}
    event = [e for e in env.store.events_since(0) if e["kind"] == "decision.approved"][-1]
    assert event["payload"]["mode"] == "merge" and event["payload"]["pr"] == 12 and event["payload"]["head"] == HEAD
    queued = env.store.commands(card=card.id)[before:]
    assert [(c["agent"], c["kind"]) for c in queued] == [("dev", "message")]
    assert queued[0]["text"] == board.PR_WAKE["merge"].format(id=card.id, pr=12, sha7=HEAD[:7])


def test_release_mode_needs_a_release_project(env):
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id, release=False))["decision"]
    with pytest.raises(api.ApiError) as exc:
        _approve(env, decision, mode="release")
    assert exc.value.kind == "invalid"
    for bad in ("deploy", None):
        with pytest.raises(api.ApiError) as exc:
            _approve(env, decision, mode=bad)
        assert exc.value.kind == "invalid"
    assert _approve(env, decision, mode="approve")["decisions"][0]["answer"]["mode"] == "approve"


def test_check_runs_no_effect(env):
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id))["decision"]
    out = _approve(env, decision, review=False, check=True)
    assert out["checked"] is True and out["decisions"] == []
    assert env.call("root", {"op": "decision.get", "id": decision["id"]})["decision"]["state"] == "open"


@pytest.mark.parametrize("review", [False, {"id": 0, "head": HEAD}, {"id": "77", "head": HEAD}, {"id": 77, "head": HEAD2},
                                    {"id": 77}])
def test_approve_without_review_is_refused(env, review):
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id))["decision"]
    with pytest.raises(api.ApiError) as exc:
        _approve(env, decision, review=review)
    assert exc.value.kind == "invalid"
    assert env.call("root", {"op": "decision.get", "id": decision["id"]})["decision"]["state"] == "open"


def test_a_pr_is_approved_alone_and_with_a_signature(env):
    card = env.card(status="Review")
    one = _publish(env, _pr(card.id))["decision"]
    other_card = env.card(status="Review")
    two = _publish(env, _pr(other_card.id, number=13))["decision"]
    request = {"op": "decision.approve", "ids": [one["id"], two["id"]], "user": "owner",
               "revisions": {card.id: card.revision, other_card.id: other_card.revision},
               "signature": {"id": "key", "binding": {**BINDING, "mode": "merge"}}, "review": {"id": 1, "head": HEAD}}
    with pytest.raises(api.ApiError) as exc:
        env.call("digi-cabinet", request)
    assert exc.value.kind == "invalid"
    with pytest.raises(api.ApiError) as exc:
        _approve(env, one, signature=None)
    assert exc.value.kind == "refused"


def test_reject_wakes_dev_with_remarks(env):
    _hosted(env)
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id))["decision"]
    before = len(env.store.commands(card=card.id))
    env.call("digi-cabinet", {"op": "decision.reject", "id": decision["id"], "user": "owner",
                             "text": "Поправь имя функции"})
    event = [e for e in env.store.events_since(0) if e["kind"] == "decision.rejected"][-1]
    assert event["payload"]["pr"] == 12
    queued = env.store.commands(card=card.id)[before:]
    assert [(c["agent"], c["kind"], c["text"]) for c in queued] == [
        ("dev", "message", board.PR_REMARKS_WAKE.format(id=card.id, pr=12))]


def test_the_wake_texts_are_fixed():
    assert board.PR_WAKE == {
        "approve": "[board] {id}: the owner approved PR #{pr} on {sha7} in the cabinet — approve only: merge when he "
                   "says so in the thread",
        "merge": "[board] {id}: the owner approved PR #{pr} on {sha7} in the cabinet and asks to merge it — kd-forge pr "
                 "for the full head, then kd-merge",
        "release": "[board] {id}: the owner approved PR #{pr} on {sha7} in the cabinet and asks to merge and release it "
                   "— kd-forge pr for the full head, then kd-merge --release",
        # a chain plan with other pull requests still open: the release is one, after the plan's last merge
        "release_plan": "[board] {id}: the owner approved PR #{pr} on {sha7} in the cabinet and asks to merge and release "
                        "— a chain plan with other pull requests open: merge every pull request of the plan in the "
                        "brief's order with kd-merge (no --release), then release once: kd-merge --release on this "
                        "pull request, merged by then",
    }
    assert board.PR_REMARKS_WAKE == ("[board] {id}: the owner sent remarks on PR #{pr} — read the thread and kd-forge "
                                     "comments, fix and publish again")
    event = {"kind": "decision.approved", "card": "KD-5", "actor": "user:owner",
             "payload": {"decision": "d" * 32, "type": "pr_approve", "requested_by": "agent:dev", "pr": 3,
                         "head": HEAD, "mode": "approve"}}
    changes = board.changes_from([event])
    assert board.wake_items(changes) == [{"agent": "dev", "card": "KD-5", "kind": "message",
                                          "text": board.PR_WAKE["approve"].format(id="KD-5", pr=3, sha7=HEAD[:7])}]
    assert board.coordinator_events(changes) is False
    # another type, a bad mode or a requester that is no agent: no wake
    for payload in ({"type": "promote"}, {"mode": "deploy"}, {"requested_by": "user:owner"}, {"pr": "3"}):
        assert board.wake_items(board.changes_from([{**event, "payload": {**event["payload"], **payload}}])) == []


def _person(user):
    return api.Principal("user", -1, user)


def test_accept_is_off_while_the_pr_is_open(env):
    card = env.card(status="Review")
    _publish(env, _pr(card.id))
    actions = {a["id"]: a for a in api.dispatch(env.ctx, _person("owner"), {"op": "card.get", "card": card.id})["actions"]}
    assert actions["accept"]["enabled"] is False and actions["accept"]["reason"] == flow.PR_OPEN
    assert actions["accept"]["params"] == {"pr": 12} and actions["accept"]["primary"] is False
    assert actions["return"]["enabled"] is True
    with pytest.raises(api.ApiError) as exc:
        api.dispatch(env.ctx, _person("owner"), {"op": "user.card.act", "card": card.id, "action": "accept",
                                                  "revision": env.store.card(card.id).revision})
    assert exc.value.reason == flow.PR_OPEN
    env.call("digi-forge", {"op": "pr.state", "repo": "owner/repo", "number": 12, "state": "merged"})
    actions = {a["id"]: a for a in api.dispatch(env.ctx, _person("owner"), {"op": "card.get", "card": card.id})["actions"]}
    assert actions["accept"]["enabled"] is True and actions["accept"]["primary"] is True


def test_accept_says_the_pr_is_approved_while_it_waits_for_the_merge(env):
    # final review: after the signed approval the PR is still open — the reason says whose move it is
    card = env.card(status="Review")
    decision = _publish(env, _pr(card.id))["decision"]
    _approve(env, decision)
    actions = {a["id"]: a for a in api.dispatch(env.ctx, _person("owner"), {"op": "card.get", "card": card.id})["actions"]}
    assert actions["accept"]["enabled"] is False and actions["accept"]["reason"] == flow.PR_APPROVED
    assert actions["accept"]["params"] == {"pr": 12}
    _publish(env, _pr(card.id, head=HEAD2))  # a new head: the approval was of the old one
    actions = {a["id"]: a for a in api.dispatch(env.ctx, _person("owner"), {"op": "card.get", "card": card.id})["actions"]}
    assert actions["accept"]["reason"] == flow.PR_OPEN


# --- the decision binds the target, its merge effect and the green checks ---------------------------------

def test_payload_binds_target_effect_and_checks(env):
    from kd import pr_signature
    card = env.card(status="Review")
    payload = _publish(env, _pr(card.id, target="dev", effect="deploy to the acceptance stage",
                                checks=["unit", "static"]))["decision"]["payload"]
    assert (payload["target"], payload["effect"], payload["checks"]) == (
        "dev", "deploy to the acceptance stage", ["unit", "static"])
    assert pr_signature.payload_sha(payload) != pr_signature.payload_sha({**payload, "target": "main"})


@pytest.mark.parametrize("change", [{"target": "dev"}, {"effect": "deploy"}, {"checks": ["unit"]}, {"release": False}])
def test_a_new_target_on_the_same_head_is_a_new_decision(env, change):
    card = env.card(status="Review")
    first = _publish(env, _pr(card.id))["decision"]
    second = _publish(env, _pr(card.id, **change))["decision"]
    assert second["id"] != first["id"]
    assert env.call("root", {"op": "decision.get", "id": first["id"]})["decision"]["state"] == "expired"
    assert _publish(env, _pr(card.id, **change))["decision"]["id"] == second["id"]


def test_payload_of_a_stored_row_takes_the_passport_defaults(env):
    """The prs table keeps no passport (it lives in the decision): a payload built from a stored row is of main."""
    card = env.card(status="Review")
    row = _publish(env, _pr(card.id, target="dev", checks=["unit"]))["pr"]
    assert "target" not in row
    payload = prs.payload_of(row)
    assert (payload["target"], payload["effect"], payload["checks"]) == ("main", "", [])
    payload["checks"].append("x")
    assert prs.PASSPORT["checks"] == []


def test_publish_without_the_new_keys_still_lands(env):
    card = env.card(status="Review")
    payload = _publish(env, _pr(card.id))["decision"]["payload"]
    assert (payload["target"], payload["effect"], payload["checks"]) == ("main", "", [])


def test_a_decision_signed_before_the_passport_is_not_kept(env):
    """An open decision of the previous code has no target: kd-review refuses it, so a publish replaces it."""
    import json
    card = env.card(status="Review")
    first = _publish(env, _pr(card.id))["decision"]
    old = {k: v for k, v in first["payload"].items() if k not in ("target", "effect", "checks")}
    env.store._conn.execute("UPDATE decisions SET payload=? WHERE id=?", (json.dumps(old), first["id"]))
    again = _publish(env, _pr(card.id))["decision"]
    assert again["id"] != first["id"] and again["payload"]["target"] == "main"
