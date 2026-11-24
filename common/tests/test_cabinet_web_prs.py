"""dev's PR in the cabinet over HTTP — the view and the diff, a passkey-only challenge that binds the mode,
the approval through kd-review before the board records it, and remarks recorded first and posted best effort."""

import json
import re

import pytest
from softauth import SoftAuthenticator, b64url, unb64url
from web_env import WebEnv
from cabinet_env import totp_codes

from kd import pr_signature
from kd.cabinet import review_client

HEAD, HEAD2 = "a" * 40, "b" * 40


class FakeReview:
    """kd-review as the cabinet sees it: it recomputes the signed value from what it was sent."""

    def __init__(self):
        self.keys, self.calls, self.fail, self.during = [], [], {}, None
        self.state = "approved"
        self.live = {"state": "open", "head": HEAD, "target": "main"}  # the PR as Forgejo holds it now

    async def call(self, request, timeout=40.0):
        self.calls.append(request)
        failure = self.fail.get(request["op"])
        if failure is not None:
            raise failure
        if request["op"] == "keys":
            return {"credential_ids": list(self.keys)}
        if request["op"] == "live":
            return dict(self.live)
        if request["op"] == "approve":
            value = pr_signature.challenge(
                request["decision"], request["payload"], request["card_revision"], request["user"],
                request["expires"], request["nonce"], request["mode"],
            )
            client_data = json.loads(unb64url(request["assertion"]["response"]["clientDataJSON"]))
            assert unb64url(client_data["challenge"]) == value
            if self.during:
                self.during()
            return {"review": 501, "head": request["payload"]["head"], "state": self.state}
        if request["op"] == "carry":  # it found the new head a clean update of the signed one
            return {"review": 503, "head": request["head"], "state": "carried", "from": request["from"],
                    "mode": self.carried_mode, "target": "main", "decision": "1" * 32}
        return {"review": 502}

    carried_mode = "merge"


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.review = FakeReview()
    w.svc.review = w.review
    return w


def _pr(card, number=12, head=HEAD, **over):
    pr = {
        "repo": "owner/repo", "project": "digipaddock", "number": number, "card": card, "title": f"{card}: a change",
        "url": f"https://forge.example/owner/repo/pulls/{number}", "head": head, "state": "open",
        "files": [{"path": "common/kd/x.py", "status": "modified", "additions": 3, "deletions": 1}],
        "cage": ["common/kd/x.py"], "diff": "diff --git a/common/kd/x.py b/common/kd/x.py\n+x\n", "truncated": False,
        "release": True,
    }
    pr.update(over)
    return pr


def _publish(w, pr):
    return w.env.call("digi-forge", {"op": "pr.publish", "pr": pr})["decision"]


def _decision(w, **over):
    card = w.env.card(status="Review")
    return card, _publish(w, _pr(card.id, **over))


def _signer(w, stepup=None):
    auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    body = {}
    if stepup:  # the person signs already: a new key needs a step-up
        ch = w.client.post("/api/stepup/challenge", headers=w.headers(), json={"purpose": "passkey.add"}).json()
        body["stepup"] = {"challenge_id": ch["challenge_id"], "totp": stepup}
    opts = w.client.post("/api/me/passkeys/options", headers=w.headers(), json=body).json()
    r = w.client.post("/api/me/passkeys", headers=w.headers(),
                      json={"credential": auth.register(opts["options"]), "name": "Ноутбук", **body})
    assert r.status_code == 200, r.text
    w.review.keys = [b64url(auth.credential_id)]
    return auth


def _challenge(w, ids, mode="merge"):
    body = {"ids": ids} if mode is None else {"ids": ids, "mode": mode}
    return w.client.post("/api/decisions/challenge", headers=w.headers(), json=body)


def _sign(w, auth, decision, mode="merge"):
    ch = _challenge(w, [decision["id"]], mode)
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    return w.client.post("/api/decisions/approve", headers=w.headers(),
                         json={"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])})


def _state(w, decision):
    return w.env.call("root", {"op": "decision.get", "id": decision["id"]})["decision"]


def test_pr_view_and_diff(w):
    w.login()
    card, d = _decision(w)
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    assert view["card"]["id"] == card.id and view["repo"] == "owner/repo" and view["number"] == 12
    assert view["head"] == HEAD and view["state"] == "open" and view["url"].endswith("/pulls/12")
    assert view["files"][0]["path"] == "common/kd/x.py" and view["cage"] == ["common/kd/x.py"]
    assert view["release"] is True and view["truncated"] is False and view["stale"] is False
    assert (view["target"], view["effect"], view["checks"]) == ("main", "", [])
    r = w.client.get(f"/api/decisions/{d['id']}/diff")
    assert r.status_code == 200, r.text
    assert r.json() == {"diff": "diff --git a/common/kd/x.py b/common/kd/x.py\n+x\n", "truncated": False, "head": HEAD}
    _publish(w, _pr(card.id, head=HEAD2))  # a new head: the old decision is over and its diff is gone
    assert w.client.get(f"/api/decisions/{d['id']}").json()["view"]["stale"] is True
    r = w.client.get(f"/api/decisions/{d['id']}/diff")
    assert r.status_code == 409 and r.json()["error"] == "stale"
    other = w.env.card()
    question = w.env.call("digi", {"op": "decision.open", "type": "question", "agent": "dev", "card": other.id,
                                  "payload": {"text": "Платить?"}})["decision"]
    assert w.client.get(f"/api/decisions/{question['id']}/diff").status_code == 404


def test_pr_view_shows_target_effect_and_checks(w):
    """the block of the decision names the branch, what a merge into it sets off and the green checks."""
    w.login()
    _, d = _decision(w, target="dev", effect="Forgejo Actions: deploy to prod", checks=["unit"])
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    assert (view["target"], view["effect"], view["checks"]) == ("dev", "Forgejo Actions: deploy to prod", ["unit"])


def test_challenge_is_single_passkey_only_and_binds_the_mode(w):
    w.login()
    b32 = w.client.post("/api/me/totp/setup", headers=w.headers()).json()["secret"]
    code = totp_codes(b32)
    assert w.client.post("/api/me/totp/confirm", headers=w.headers(), json={"code": code[0]}).status_code == 200
    auth = _signer(w, stepup=code[1])  # a TOTP code too: a PR is still signed with a passkey only
    _, d = _decision(w)
    _, plain = _decision(w, number=13, release=False)
    for ids, mode in (([d["id"]], None), ([d["id"]], "bogus"), ([d["id"], plain["id"]], "merge"),
                      ([plain["id"]], "release")):
        r = _challenge(w, ids, mode)
        assert r.status_code == 400 and r.json()["error"] == "invalid", (ids, mode, r.text)
    ch = _challenge(w, [d["id"]], "approve").json()
    assert ch["totp"] is False and ch["step_up"] is True
    assert [c["id"] for c in ch["options"]["allowCredentials"]] == [b64url(auth.credential_id)]
    r = w.client.post("/api/decisions/approve", headers=w.headers(),
                      json={"challenge_id": ch["challenge_id"], "totp": "123456"})
    assert r.status_code == 403 and r.json()["error"] == "stepup_required"
    r = _sign(w, auth, d, "approve")
    assert r.status_code == 200, r.text
    sent = [c for c in w.review.calls if c["op"] == "approve"][-1]
    assert sent["mode"] == "approve" and re.fullmatch(r"[0-9a-f]{32}", sent["nonce"])
    assert _state(w, d)["answer"]["mode"] == "approve"


def test_approve_merge_goes_through_kd_review_then_the_board(w):
    w.login()
    auth = _signer(w)
    card, d = _decision(w)
    ch = _challenge(w, [d["id"]]).json()
    w.env.store.edit_card(card.id, None, {"description": "иначе"}, actor="user:owner")  # the board refuses first
    r = w.client.post("/api/decisions/approve", headers=w.headers(),
                      json={"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])})
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert not [c for c in w.review.calls if c["op"] == "approve"]
    r = _sign(w, auth, d)
    assert r.status_code == 200, r.text
    out = r.json()["decisions"][0]
    assert out["state"] == "approved" and out["decided_by"] == "user:owner"
    assert out["answer"] == {"mode": "merge", "review": 501}
    sent = [c for c in w.review.calls if c["op"] == "approve"]
    assert len(sent) == 1
    assert sent[0]["decision"] == d["id"] and sent[0]["payload"] == d["payload"] and sent[0]["user"] == "owner"
    assert sent[0]["card_revision"] == w.env.store.card(card.id).revision
    signed = _state(w, d)["signature"]
    assert signed["binding"]["mode"] == "merge" and signed["binding"]["nonce"] == sent[0]["nonce"]


def test_record_refused_after_forgejo_says_so(w):
    w.login()
    auth = _signer(w)
    card, d = _decision(w)
    w.review.during = lambda: w.env.store.edit_card(card.id, None, {"description": "иначе"}, actor="user:owner")
    r = _sign(w, auth, d)
    assert r.status_code == 409 and r.json()["error"] == "approved_in_forge"
    assert "Forgejo" in r.json()["message"]
    assert _state(w, d)["state"] == "open"
    w.review.during, w.review.state = None, "already"  # pressing again: kd-review finds its Approve on the head
    r = _sign(w, auth, d)
    assert r.status_code == 200, r.text
    assert r.json()["decisions"][0]["answer"] == {"mode": "merge", "review": 501}


def test_review_unavailable_or_refused_changes_nothing(w):
    w.login()
    auth = _signer(w)
    _, d = _decision(w)
    for failure, status, kind in ((review_client.ReviewUnavailable("no socket"), 503, "unavailable"),
                                  (review_client.ReviewTimeout("no answer in time"), 503, "review_timeout"),
                                  (review_client.ReviewRefused("stale", "a new head"), 409, "stale"),
                                  (review_client.ReviewRefused("refused", "not a signer"), 403, "refused")):
        w.review.fail = {"approve": failure}
        r = _sign(w, auth, d)
        assert (r.status_code, r.json()["error"]) == (status, kind), r.text
        assert _state(w, d)["state"] == "open"
    w.review.fail = {"keys": review_client.ReviewUnavailable("no socket")}
    r = _challenge(w, [d["id"]])
    assert r.status_code == 503 and r.json()["error"] == "unavailable"


def test_a_pr_that_ended_in_forgejo_ends_its_decision_before_any_signature(w):
    # final review: merged or closed in Forgejo without kd-merge — the challenge asks kd-review and records it
    w.login()
    auth = _signer(w)
    card, d = _decision(w)
    w.review.live = {"state": "merged", "head": HEAD}
    r = _challenge(w, [d["id"]])
    assert r.status_code == 409 and r.json()["error"] == "stale", r.text
    assert _state(w, d)["state"] == "expired" and w.env.store.pr("owner/repo", 12)["state"] == "merged"
    _, moved = _decision(w, number=13)  # a new head in Forgejo, not published yet: no prompt, the decision waits
    w.review.live = {"state": "open", "head": HEAD2}
    r = _challenge(w, [moved["id"]])
    assert r.status_code == 409 and r.json()["error"] == "stale" and _state(w, moved)["state"] == "open"
    w.review.live = {"state": "open", "head": HEAD}
    _, closed = _decision(w, number=14)  # closed between the prompt and kd-review's own look
    w.review.fail = {"approve": review_client.ReviewRefused("stale", "PR #14 is closed", live="closed")}
    r = _sign(w, auth, closed)
    assert r.status_code == 409 and r.json()["error"] == "stale"
    assert _state(w, closed)["state"] == "expired" and w.env.store.pr("owner/repo", 14)["state"] == "closed"
    w.review.fail = {"live": review_client.ReviewUnavailable("no socket")}
    _, other = _decision(w, number=15)
    r = _challenge(w, [other["id"]])
    assert r.status_code == 503 and r.json()["error"] == "unavailable"
    assert card.id  # the first card keeps its other history


def test_a_pr_retargeted_in_forgejo_is_not_offered_for_signature(w):
    """Final review: kd-review answers the live target; another branch than the decision's — no passkey prompt."""
    w.login()
    _signer(w)
    _, d = _decision(w)
    w.review.live = {"state": "open", "head": HEAD, "target": "dev"}
    r = _challenge(w, [d["id"]])
    assert r.status_code == 409 and r.json()["error"] == "stale" and "dev" in r.text
    assert _state(w, d)["state"] == "open"
    w.review.live = {"state": "open", "head": HEAD, "target": "main"}
    assert _challenge(w, [d["id"]]).status_code == 200
    w.review.live = {"state": "open", "head": HEAD}  # a kd-review from before the release: its own look decides
    assert _challenge(w, [d["id"]]).status_code == 200


def test_remarks_record_first_and_post_best_effort(w):
    w.login()
    card, d = _decision(w)
    comments = [{"path": "common/kd/x.py", "line": 3, "side": "new", "body": "тут"},
                {"path": "docs/x.md", "line": 1, "side": "old", "body": "и тут"}]
    for bad in ([{**comments[0], "side": "mid"}], [{**comments[0], "body": "x" * 1001}], comments[:1] * 31,
                [{**comments[0], "line": 0}]):
        r = w.client.post(f"/api/decisions/{d['id']}/reject", headers=w.headers(),
                          json={"text": "Поправь", "comments": bad})
        assert r.status_code == 400 and r.json()["error"] == "invalid"
    r = w.client.post(f"/api/decisions/{d['id']}/reject", headers=w.headers(),
                      json={"text": "Поправь", "comments": comments})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["forge"] == "posted" and out["decision"]["state"] == "rejected"
    assert out["decision"]["reply"] == "Поправь\n\n- `common/kd/x.py:3` — тут\n- `docs/x.md:1` — и тут"
    sent = w.review.calls[-1]
    assert sent == {"op": "comment", "project": "digipaddock", "repo": "owner/repo", "number": 12, "head": HEAD,
                    "body": "Поправь", "comments": comments}
    _, again = _decision(w, number=13)
    w.review.fail = {"comment": review_client.ReviewUnavailable("no socket")}
    r = w.client.post(f"/api/decisions/{again['id']}/reject", headers=w.headers(), json={"text": "Нет"})
    assert r.status_code == 200, r.text
    assert r.json()["forge"] == "failed" and _state(w, again)["state"] == "rejected"


def test_no_signer_key_is_setup_required(w):
    w.login()
    _signer(w)
    w.review.keys = []  # the cabinet's key is not in kd-review's registry yet
    _, d = _decision(w)
    r = _challenge(w, [d["id"]])
    assert r.status_code == 403 and r.json()["error"] == "setup_required"
    message = r.json()["message"]
    assert "kd-signers sync" in message and "of owner" in message and "--replace-signers" in message


def test_review_client_without_a_socket_is_unavailable(tmp_path):
    import asyncio

    with pytest.raises(review_client.ReviewUnavailable):
        asyncio.run(review_client.ReviewClient(str(tmp_path / "none.sock")).call({"op": "keys", "user": "owner"}))


UNIX = pytest.mark.skipif(not hasattr(__import__("asyncio"), "start_unix_server") or __import__("os").name != "posix",
                          reason="unix sockets")


def _served(tmp_path, handle, request, timeout=5.0):
    import asyncio

    async def main():
        path = str(tmp_path / "r.sock")
        server = await asyncio.start_unix_server(handle, path=path)
        async with server:
            return await review_client.ReviewClient(path).call(request, timeout=timeout)

    return asyncio.run(main())


@UNIX
def test_review_client_carries_the_end_of_a_pr_and_a_timeout(tmp_path):
    async def ended(reader, writer):
        await reader.readline()
        writer.write(b'{"ok": false, "error": "stale", "message": "PR #12 is merged", "live": "merged"}\n')
        await writer.drain()
        writer.close()

    with pytest.raises(review_client.ReviewRefused) as exc:
        _served(tmp_path, ended, {"op": "live"})
    assert exc.value.kind == "stale" and exc.value.live == "merged"

    async def silent(reader, writer):
        await reader.read()  # until the client gives up and closes
        writer.close()

    with pytest.raises(review_client.ReviewTimeout):
        _served(tmp_path, silent, {"op": "approve"}, timeout=0.2)
    assert issubclass(review_client.ReviewTimeout, review_client.ReviewUnavailable)


# --- the owner's signature carried to a head brought up to date with the target (KD-9442) ----------------------

def _local(w, pr):
    """A publish over the cabinet's local socket, as the forge executor sends it."""
    import asyncio

    return asyncio.run(w.svc.call_local(w.env.principal("digi-forge"), {"op": "pr.publish", "pr": pr}))


def _signed(w, mode="merge", **over):
    w.login()
    auth = _signer(w)
    card, d = _decision(w, **over)
    assert _sign(w, auth, d, mode).status_code == 200
    return card, d


def _pr_decisions(w, card, state=None):
    return [d for d in w.env.call("root", {"op": "decision.list", "card": card.id})["decisions"]
            if d["type"] == "pr_approve" and (state is None or d["state"] == state)]


def test_a_clean_update_keeps_the_signature_without_a_council_or_a_new_decision(w, monkeypatch):
    from kd.cabinet import council_rounds

    card, d = _signed(w, mode="release")
    w.review.carried_mode = "release"
    monkeypatch.setattr(council_rounds, "on_pull_head", lambda *a, **k: pytest.fail("no council for a carry"))
    out = _local(w, _pr(card.id, head=HEAD2))
    sent = [c for c in w.review.calls if c["op"] == "carry"]
    assert sent == [{"op": "carry", "project": "digipaddock", "repo": "owner/repo", "number": 12, "from": HEAD,
                     "head": HEAD2}]
    assert out["carried"] == {"from": HEAD, "mode": "release", "target": "main"} and "carry" not in out
    new = out["decision"]
    assert new["state"] == "approved" and new["payload"]["head"] == HEAD2 and new["decided_by"] == "user:owner"
    assert new["answer"] == {"mode": "release", "review": 503, "carried_from": {"decision": d["id"], "head": HEAD}}
    assert _pr_decisions(w, card, "open") == []
    line =w.env.store._conn.execute(
        "SELECT payload FROM messages WHERE json_extract(payload, '$.decision') = ?", (new["id"],)).fetchone()
    assert json.loads(line["payload"])["key"] == "sys.decision.carried"
    assert json.loads(line["payload"])["params"] == {"from": HEAD[:7], "target": "main", "mode": "release"}
    # the card's PR block names the head the signature was given on, in the signed mode
    view = w.client.get(f"/api/decisions/{new['id']}").json()["view"]
    assert view["carried"] == {"decision": d["id"], "head": HEAD, "mode": "release"} and view["stale"] is False
    # the same head again is the same approval: kd-review is not asked twice
    again = _local(w, _pr(card.id, head=HEAD2))
    assert again["decision"]["id"] == new["id"] and len([c for c in w.review.calls if c["op"] == "carry"]) == 1


def test_the_carried_approval_wakes_nobody(w):
    from kd import board

    card, d = _signed(w)
    before = w.env.store.last_seq()
    _local(w, _pr(card.id, head=HEAD2))
    events = [e for e in w.env.store.events_since(before, 1000) if e["kind"] == "decision.approved"]
    assert len(events) == 1 and events[0]["payload"]["carried_from"] == HEAD and "mode" not in events[0]["payload"]
    assert board._pr_change(events[0], events[0]["payload"], "user:owner") is None


def test_a_refused_carry_asks_the_owner_to_sign_again(w):
    card, d = _signed(w)
    w.review.fail["carry"] = review_client.ReviewRefused("refused", f"the PR's own diff changed from {HEAD[:7]}")
    out = _local(w, _pr(card.id, head=HEAD2))
    assert "own diff changed" in out["uncarried"] and "carried" not in out
    assert out["decision"]["state"] == "open" and out["decision"]["payload"]["head"] == HEAD2
    assert [x["payload"]["head"] for x in _pr_decisions(w, card, "open")] == [HEAD2]
    w.review.fail["carry"] = review_client.ReviewUnavailable("kd-review unavailable (OSError)")
    out = _local(w, _pr(card.id, head="c" * 40))
    assert "unavailable" in out["uncarried"] and out["decision"]["state"] == "open"


@pytest.mark.parametrize("over", [{"release": False}, {"target": "dev"}, {"checks": ["unit"]}])
def test_another_target_release_or_passport_is_not_carried(w, over):
    card, d = _signed(w)
    out = _local(w, _pr(card.id, head=HEAD2, **over))
    assert not [c for c in w.review.calls if c["op"] == "carry"]
    assert out["decision"]["state"] == "open" and "carried" not in out and "uncarried" not in out


def test_only_an_approved_signature_carries(w):
    w.login()
    card, d = _decision(w)  # open, never signed
    out = _local(w, _pr(card.id, head=HEAD2))
    assert out["decision"]["state"] == "open" and not [c for c in w.review.calls if c["op"] == "carry"]
    card, d = _signed(w, number=13)  # revoked by the owner since
    w.env.store._conn.execute("UPDATE decisions SET state='revoked' WHERE id=?", (d["id"],))
    out = _local(w, _pr(card.id, number=13, head=HEAD2))
    assert out["decision"]["state"] == "open" and not [c for c in w.review.calls if c["op"] == "carry"]


def test_only_the_cabinet_records_a_carry(w):
    card, d = _signed(w)
    held = {"payload": {**d["payload"], "head": HEAD2}, "requested_by": "agent:dev"}
    for op in ("pr.carried", "pr.uncarried"):
        with pytest.raises(Exception) as exc:
            w.env.call("digi-forge", {"op": op, "repo": "owner/repo", "number": 12, "held": held,
                                      "decision": d["id"], "review": {"id": 1, "head": HEAD2, "from": HEAD,
                                                                      "mode": "merge"}})
        assert getattr(exc.value, "kind", "") in ("refused", "forbidden"), exc.value
