# agents/dev/tests/test_review.py
"""kd-review posts the Approve in Forgejo only after checking the owner's passkey signature over the decision
it is shown — the term, the nonce, the registered key, the project's repository and the live PR's head."""
import json
import os
import socket
import sys
import threading
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common" / "tests"))
from softauth import SoftAuthenticator, b64url  # noqa: E402
from test_forge_read import REPO, Resp, make_opener  # noqa: E402

from dev_agent import forge, review  # noqa: E402
from kd import pr_signature  # noqa: E402
from kd.cabinet import passkeys  # noqa: E402

ORIGIN, RP = "https://cabinet.example", "cabinet.example"
NOW = 1_800_000_000
HEAD, HEAD2 = "c" * 40, "e" * 40
DECISION = "1" * 32
TOKEN = "tokABCDEF0123456789"
PAYLOAD = {"kind": "pr_approve", "repo": REPO, "project": "digipaddock", "number": 12, "head": HEAD, "url": "u",
           "title": "KD-12: x", "release": True, "target": "main", "effect": "", "checks": []}
PR = {"number": 12, "title": "KD-12: x", "state": "open", "merged": False, "head": {"sha": HEAD},
      "base": {"ref": "main"}, "user": {"login": "digipaddock-dev"}}
OURS = {"id": 77, "user": {"login": "digipaddock-review"}, "state": "APPROVED", "commit_id": HEAD, "dismissed": False,
        "stale": False, "body": "Одобрено владельцем в кабинете (одобрение и merge), решение 11111111, в ветку main"}


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def registry(tmp_path, *auths, user="owner", types=("pr_approve",)):
    folder = tmp_path / "signers"
    folder.mkdir(exist_ok=True)
    keys = [{"credential_id": b64url(a.credential_id), "public_key": b64url(a._cose_key()), "name": "Ноутбук",
             "types": list(types), "added": "2026-10-19T10:00:00Z", "added_by": "root"} for a in auths]
    (folder / f"{user}.json").write_text(json.dumps({"user": user, "keys": keys}), encoding="utf-8")
    return folder


def signed(auth, *, mode="merge", payload=PAYLOAD, revision=4, user="owner", expires=None, nonce="ab" * 16,
           sign_mode=None, sign_payload=None):
    """An approve request as the cabinet sends it, signed over ``sign_mode``/``sign_payload`` when given."""
    expires = expires or iso(NOW + 300)
    value = pr_signature.challenge(DECISION, sign_payload or payload, revision, user, expires, nonce, sign_mode or mode)
    options, _ = passkeys.authentication_options(review.Rp(ORIGIN, RP), challenge=value, allow=[auth.credential_id])
    return {"op": "approve", "decision": DECISION, "payload": payload, "card_revision": revision, "user": user,
            "expires": expires, "nonce": nonce, "mode": mode, "assertion": json.loads(auth.assert_(options))}


def forgejo(pr=None, reviews=(), posted=None, extra=None):
    routes = {("GET", f"/repos/{REPO}/pulls/12/reviews"): list(reviews), ("GET", f"/repos/{REPO}/pulls/12"): pr or PR,
              ("POST", f"/repos/{REPO}/pulls/12/reviews"): posted or {"id": 91},
              ("GET", f"/repos/{REPO}/pulls/12.diff"): "diff"}  # what kd-review records of the approved head
    routes.update(extra or {})
    return make_opener(routes)


def reviewer(tmp_path, opener, repos=None, nonces=None, clock=NOW, approvals=None):
    (tmp_path / "repos.json").write_text(json.dumps(repos or {
        "digipaddock": {"repo": REPO, "dir": "work/DigiPaddock", "release": "digipaddock"}}), encoding="utf-8")
    return review.Reviewer(api=forge.Api(TOKEN, opener=opener), repos_path=str(tmp_path / "repos.json"),
                           signers_dir=str(tmp_path / "signers"), origin=ORIGIN, clock=lambda: clock,
                           nonces_path=nonces, approvals_path=approvals)


def posts(opener):
    return [json.loads(c[2]) for c in opener.seen if c[0] == "POST"]


@pytest.fixture
def auth(tmp_path):
    a = SoftAuthenticator(RP, ORIGIN)
    registry(tmp_path, a)
    return a


def refused(r, request, kind="refused"):
    with pytest.raises(review.Refused) as exc:
        r.handle(request)
    assert exc.value.kind == kind, exc.value
    return exc.value


def test_approve_posts_one_approved_review_on_the_head(tmp_path, auth):
    opener = forgejo()
    out = reviewer(tmp_path, opener).handle(signed(auth))
    assert out == {"review": 91, "head": HEAD, "state": "approved"}
    assert posts(opener) == [{"event": "APPROVED", "commit_id": HEAD,
                              "body": "Одобрено владельцем в кабинете (одобрение и merge), решение 11111111, в ветку main"}]


def test_an_existing_review_on_the_head_is_reused(tmp_path, auth):
    opener = forgejo(reviews=[OURS])
    assert reviewer(tmp_path, opener).handle(signed(auth)) == {"review": 77, "head": HEAD, "state": "already"}
    assert posts(opener) == []
    for other in ({**OURS, "dismissed": True}, {**OURS, "commit_id": HEAD2}, {**OURS, "user": {"login": "example-owner"}},
                  {**OURS, "state": "COMMENT"},
                  {**OURS, "body": OURS["body"].replace("в ветку main", "в ветку dev")}):  # signed into another target
        opener = forgejo(reviews=[other])
        assert reviewer(tmp_path, opener).handle(signed(auth, nonce="cd" * 16))["state"] == "approved"
        assert len(posts(opener)) == 1


def test_a_release_signature_after_a_merge_one_posts_its_own_approve(tmp_path, auth):
    """kd-merge reads the mode from the body — a merge Approve on the head is not reused for a release."""
    opener = forgejo(reviews=[OURS])
    assert reviewer(tmp_path, opener).handle(signed(auth, mode="release"))["state"] == "approved"
    assert posts(opener)[0]["body"] == ("Одобрено владельцем в кабинете (одобрение, merge и выкатка), решение 11111111,"
                                        " в ветку main")
    assert forge.signed_mode(posts(opener)[0]["body"]) == "release"


def test_a_new_head_after_signing_is_refused(tmp_path, auth):
    for pr, kind in (({**PR, "head": {"sha": HEAD2}}, "stale"), ({**PR, "state": "closed"}, "stale"),
                     ({**PR, "merged": True}, "stale"), ({**PR, "user": {"login": "example-owner"}}, "refused"),
                     ({**PR, "base": {"ref": "dev"}}, "refused")):
        opener = forgejo(pr=pr)
        refused(reviewer(tmp_path, opener), signed(auth), kind)
        assert posts(opener) == []


def test_replay_foreign_key_and_old_term_are_refused(tmp_path, auth):
    r = reviewer(tmp_path, forgejo())
    request = signed(auth)
    r.handle(request)
    assert "used" in str(refused(r, request))
    stranger = SoftAuthenticator(RP, ORIGIN)
    refused(r, signed(stranger, nonce="01" * 16))
    for expires in (iso(NOW), iso(NOW - 60), iso(NOW + review.TERM_MAX + 1)):
        refused(r, signed(auth, expires=expires, nonce="02" * 16))
    refused(r, signed(auth, expires="soon", nonce="03" * 16), "invalid")
    refused(r, signed(auth, mode="release", sign_mode="approve", nonce="04" * 16))  # the mode it was not signed for
    refused(r, signed(auth, payload={**PAYLOAD, "number": 13}, sign_payload=PAYLOAD, nonce="05" * 16))
    registry(tmp_path, auth, types=())  # the key is registered, but not for PRs
    refused(r, signed(auth, nonce="06" * 16))
    refused(r, {**signed(auth, nonce="07" * 16), "nonce": "x"}, "invalid")
    refused(r, {**signed(auth, nonce="08" * 16), "payload": {**PAYLOAD, "kind": "admin_plan"}}, "invalid")


def test_mode_release_needs_a_release_payload(tmp_path, auth):
    opener = forgejo()
    refused(reviewer(tmp_path, opener), signed(auth, mode="release", payload={**PAYLOAD, "release": False}))
    assert opener.seen == []


def test_project_must_name_the_repo(tmp_path, auth):
    opener = forgejo()
    refused(reviewer(tmp_path, opener), signed(auth, payload={**PAYLOAD, "repo": "other/repo"}))
    refused(reviewer(tmp_path, opener, repos={"web": {"repo": REPO, "dir": "work/Ops"}}), signed(auth, nonce="09" * 16))
    assert opener.seen == []


def test_comment_posts_line_comments(tmp_path):
    opener = forgejo(posted={"id": 93})
    out = reviewer(tmp_path, opener).handle(
        {"op": "comment", "project": "digipaddock", "repo": REPO, "number": 12, "head": HEAD, "body": "Поправь",
         "comments": [{"path": "a.py", "line": 3, "side": "new", "body": "тут"},
                      {"path": "b.py", "line": 1, "side": "old", "body": "и тут"}]})
    assert out == {"review": 93}
    assert posts(opener) == [{"event": "COMMENT", "commit_id": HEAD, "body": "Поправь",
                              "comments": [{"path": "a.py", "body": "тут", "new_position": 3},
                                           {"path": "b.py", "body": "и тут", "old_position": 1}]}]
    opener = forgejo(pr={**PR, "head": {"sha": HEAD2}})
    refused(reviewer(tmp_path, opener), {"op": "comment", "project": "digipaddock", "repo": REPO, "number": 12, "head": HEAD,
                                         "body": "x", "comments": []}, "stale")


def test_keys_lists_the_signer_ids(tmp_path):
    a, b = SoftAuthenticator(RP, ORIGIN), SoftAuthenticator(RP, ORIGIN)
    registry(tmp_path, a)
    r = reviewer(tmp_path, forgejo())
    assert r.handle({"op": "keys", "user": "owner"}) == {"credential_ids": [b64url(a.credential_id)]}
    registry(tmp_path, b, user="anna", types=())
    assert r.handle({"op": "keys", "user": "anna"}) == {"credential_ids": []}
    assert r.handle({"op": "keys", "user": "nobody"}) == {"credential_ids": []}
    refused(r, {"op": "keys", "user": "../owner"}, "invalid")
    refused(r, {"op": "drop"}, "invalid")


class FakeCabinet:
    def __init__(self, keys):
        self.keys, self.seen = keys, []

    def call(self, request):
        self.seen.append(request)
        return {"keys": self.keys}


def _cabinet_key(auth, name, created="2026-10-15T09:00:00Z"):
    return {"credential_id": b64url(auth.credential_id), "public_key": b64url(auth._cose_key()), "name": name,
            "created": created}


def test_signers_add_shows_the_keys_then_adds_one_chosen_key(tmp_path, capsys, monkeypatch):
    # final review: root chooses each key by its id — a key the cabinet's base gained is never taken unseen
    a, b = SoftAuthenticator(RP, ORIGIN), SoftAuthenticator(RP, ORIGIN)
    cabinet = FakeCabinet([_cabinet_key(a, "Ноутбук"), _cabinet_key(b, "Телефон", "2026-10-18T08:00:00Z")])
    folder = tmp_path / "signers"
    owned = []
    monkeypatch.setattr(os, "chown", lambda path, uid, gid: owned.append((uid, gid)), raising=False)

    def run(*args, clock=NOW):
        return review.main(["signers", *args], cabinet=cabinet, signers_dir=str(folder), owner=(1001, 1002),
                           clock=lambda: clock)

    assert run("add", "owner") == 0  # without --credential: the cabinet's keys, nothing written
    shown = capsys.readouterr().out
    assert b64url(a.credential_id)[:16] in shown and "Ноутбук" in shown and "2026-10-18T08:00:00Z" in shown
    assert "--credential" in shown and not (folder / "owner.json").exists()
    assert run("add", "owner", "--credential", b64url(a.credential_id)[:16]) == 0
    assert "Ноутбук" in capsys.readouterr().out
    assert run("add", "owner", "--credential", b64url(b.credential_id), clock=NOW + 60) == 0
    data = json.loads((folder / "owner.json").read_text(encoding="utf-8"))
    assert [(k["name"], k["added"]) for k in data["keys"]] == [("Ноутбук", iso(NOW)), ("Телефон", iso(NOW + 60))]
    assert data["keys"][0] == {"credential_id": b64url(a.credential_id), "public_key": b64url(a._cose_key()),
                               "name": "Ноутбук", "types": ["pr_approve"], "added": iso(NOW), "added_by": "root"}
    assert set(owned) == {(1001, 1002)}  # the service user's uid and its group, not the uid twice
    if os.name == "posix":
        assert (folder / "owner.json").stat().st_mode & 0o777 == 0o600
    for bad in (b64url(a.credential_id), "zzzzzzzzzzzz", b64url(a.credential_id)[:5]):  # again, none, too short
        assert run("add", "owner", "--credential", bad) == 2
    assert review.main(["signers", "add", "anna", "--credential", b64url(a.credential_id)], cabinet=FakeCabinet([]),
                       signers_dir=str(folder), owner=None) == 2
    assert not (folder / "anna.json").exists()
    assert run("remove", "owner", "--credential", "zzzzzzzzzzzz") == 2
    assert review.main(["signers", "list", "owner"], signers_dir=str(folder)) == 0
    assert "Телефон" in capsys.readouterr().out
    assert run("remove", "owner", "--credential", b64url(a.credential_id)[:16]) == 0
    data = json.loads((folder / "owner.json").read_text(encoding="utf-8"))
    assert [k["name"] for k in data["keys"]] == ["Телефон"]
    assert review.main(["signers", "remove", "owner"], signers_dir=str(folder)) == 0
    assert not (folder / "owner.json").exists()


def test_a_used_signature_stays_used_after_a_restart(tmp_path, auth):
    # final review: the nonces a signature spent survive kd-review's restart within the signature's term
    used = tmp_path / "state" / "used-nonces.json"
    first = reviewer(tmp_path, forgejo(), nonces=used)
    first.handle(signed(auth))
    opener = forgejo()
    assert "used" in str(refused(reviewer(tmp_path, opener, nonces=used), signed(auth)))
    assert posts(opener) == []
    later = reviewer(tmp_path, forgejo(), nonces=used, clock=NOW + 400)  # the term is over: the file forgets it
    later.handle(signed(auth, nonce="cd" * 16, expires=iso(NOW + 600)))
    assert set(json.loads(used.read_text(encoding="utf-8"))) == {"cd" * 16}
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    opener = forgejo()
    refused(reviewer(tmp_path, opener, nonces=blocked), signed(auth, nonce="ef" * 16), "unavailable")
    assert posts(opener) == []  # a nonce it cannot keep approves nothing


def test_live_answers_the_prs_state_and_an_ended_pr_says_so(tmp_path, auth):
    r = reviewer(tmp_path, forgejo())
    ask = {"op": "live", "project": "digipaddock", "repo": REPO, "number": 12}
    assert r.handle(ask) == {"state": "open", "head": HEAD, "target": "main"}
    for pr, state in (({**PR, "merged": True, "state": "closed"}, "merged"), ({**PR, "state": "closed"}, "closed")):
        assert reviewer(tmp_path, forgejo(pr=pr)).handle(ask) == {"state": state, "head": HEAD, "target": "main"}
        error = refused(reviewer(tmp_path, forgejo(pr=pr)), signed(auth, nonce="9" * 32), "stale")
        assert error.extra == {"live": state}
        line = json.loads(review.answer(reviewer(tmp_path, forgejo(pr=pr)), json.dumps(signed(auth, nonce="8" * 32))
                                        .encode()))
        assert line["error"] == "stale" and line["live"] == state
    refused(reviewer(tmp_path, forgejo(pr={**PR, "user": {"login": "example-owner"}})), ask)
    refused(r, {**ask, "repo": "other/repo"})


def test_the_token_never_reaches_an_answer(tmp_path, auth, capsys):
    opener = forgejo(extra={("POST", f"/repos/{REPO}/pulls/12/reviews"): (500, {"message": f"bad {TOKEN}"})})
    r = reviewer(tmp_path, opener)
    line = review.answer(r, json.dumps(signed(auth)).encode())
    answer = json.loads(line)
    assert answer["ok"] is False and answer["error"] == "unavailable"
    logged = capsys.readouterr().err
    request = signed(auth, nonce="0a" * 16)
    for text in (line.decode(), logged):
        assert TOKEN not in text and request["assertion"]["response"]["signature"] not in text
    assert json.loads(logged.strip().splitlines()[-1])["outcome"] == "unavailable"
    assert json.loads(review.answer(r, b"not json"))["error"] == "invalid"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name != "posix", reason="SO_PEERCRED is Linux only")
def test_serve_refuses_another_uid(tmp_path):
    r = reviewer(tmp_path, forgejo())
    for allowed, ok in ((os.getuid() + 1, False), (os.getuid(), True)):
        server, client = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        worker = threading.Thread(target=review.serve_one, args=(server, r, allowed))
        worker.start()
        client.sendall(b'{"op": "keys", "user": "owner"}\n')
        answer = json.loads(client.makefile("rb").readline())
        worker.join(5)
        client.close()
        assert answer["ok"] is ok and (ok or answer["error"] == "refused")


# --- the signature binds the target; the passport's required checks; imported PRs -------------------------

TWO = {"digipaddock": {"repo": REPO, "dir": "work/DigiPaddock", "targets": ["main", "dev"]}}


def test_approve_refuses_a_pr_retargeted_after_signing(tmp_path, auth):
    opener = forgejo(pr={**PR, "base": {"ref": "dev"}})
    error = refused(reviewer(tmp_path, opener, repos=TWO), signed(auth), "stale")
    assert "dev" in str(error) and "main" in str(error) and posts(opener) == []


def test_approve_refuses_red_required_checks(tmp_path, auth):
    repos_ = {"digipaddock": {"repo": REPO, "dir": "work/DigiPaddock", "checks": {"profiles": {"unit": {"required": True}}}}}
    status = {("GET", f"/repos/{REPO}/commits/{HEAD}/status"): {
        "sha": HEAD, "state": "failure", "statuses": [{"context": "devcheck/unit", "status": "failure", "state": None}]}}
    opener = forgejo(extra=status)
    error = refused(reviewer(tmp_path, opener, repos=repos_), signed(auth))
    assert "devcheck/unit (failure)" in str(error) and posts(opener) == []
    green = {("GET", f"/repos/{REPO}/commits/{HEAD}/status"): {
        "sha": HEAD, "state": "success", "statuses": [{"context": "devcheck/unit", "status": "success", "state": None}]}}
    opener = forgejo(extra=green)
    assert reviewer(tmp_path, opener, repos=repos_).handle(signed(auth, nonce="cd" * 16))["state"] == "approved"


def test_an_unreachable_forgejo_keeps_the_signature_usable(tmp_path, auth):
    """the nonce is spent only once the live PR and its checks passed — a Forgejo that did not answer, a
    stale head or red checks leave the owner's signature for the retry (within its term, once)."""
    used = tmp_path / "state" / "used-nonces.json"
    down = forgejo(pr=(502, {"message": "bad gateway"}))
    with pytest.raises(forge.ForgeError):
        reviewer(tmp_path, down, nonces=used).handle(signed(auth))
    refused(reviewer(tmp_path, forgejo(pr={**PR, "head": {"sha": HEAD2}}), nonces=used), signed(auth), "stale")
    opener = forgejo()
    assert reviewer(tmp_path, opener, nonces=used).handle(signed(auth))["state"] == "approved"
    assert len(posts(opener)) == 1
    assert "used" in str(refused(reviewer(tmp_path, forgejo(), nonces=used), signed(auth)))


def test_reviews_that_did_not_answer_keep_the_signature_usable(tmp_path, auth):
    """Review M9: the reviews lookup is one more call to Forgejo before the Approve — its failure spends nothing."""
    used = tmp_path / "state" / "used-nonces.json"
    down = forgejo(extra={("GET", f"/repos/{REPO}/pulls/12/reviews"): (502, {"message": "bad gateway"})})
    with pytest.raises(forge.ForgeError):
        reviewer(tmp_path, down, nonces=used).handle(signed(auth))
    assert reviewer(tmp_path, forgejo(), nonces=used).handle(signed(auth))["state"] == "approved"


def test_approve_without_required_profiles_reads_no_status(tmp_path, auth):
    opener = forgejo()
    assert reviewer(tmp_path, opener).handle(signed(auth))["state"] == "approved"
    assert not any("/status" in c[1] for c in opener.seen)


def test_live_accepts_any_passport_target(tmp_path):
    ask = {"op": "live", "project": "digipaddock", "repo": REPO, "number": 12}
    r = reviewer(tmp_path, forgejo(pr={**PR, "base": {"ref": "dev"}}), repos=TWO)
    assert r.handle(ask) == {"state": "open", "head": HEAD, "target": "dev"}
    refused(reviewer(tmp_path, forgejo(pr={**PR, "base": {"ref": "dev"}})), ask)  # dev is not a target here


def test_an_imported_pr_is_approvable(tmp_path, auth):
    opener = forgejo(pr={**PR, "user": {"login": "digipaddock-import"}})
    assert reviewer(tmp_path, opener).handle(signed(auth))["state"] == "approved"


@pytest.mark.parametrize("payload", [
    {k: v for k, v in PAYLOAD.items() if k != "target"}, {**PAYLOAD, "target": ""}, {**PAYLOAD, "target": 1}])
def test_payload_without_target_is_invalid(tmp_path, auth, payload):
    opener = forgejo()
    error = refused(reviewer(tmp_path, opener), signed(auth, payload=payload), "invalid")
    assert "kd-forge publish" in str(error) and posts(opener) == []


def test_kd_signers_sync_registry_approves_with_the_new_key_and_refuses_the_removed_one(tmp_path, auth):
    """after the owner replaced a passkey in the cabinet, ``kd-signers sync --apply`` writes kd-review's
    registry: the new key's signature approves the PR, the removed key's is refused."""
    import io

    from kd import signers

    new = SoftAuthenticator(RP, ORIGIN)
    (tmp_path / "model").mkdir()

    class Cabinet:
        def call(self, request):
            if request["op"] == "user.list":
                return {"users": [{"id": "owner", "status": "active", "roles": [["owner", "*"]]}]}
            return {"keys": [_cabinet_key(new, "Новый ноутбук")]}

    me = (os.getuid(), os.getgid())
    env = signers.Env(client=Cabinet(), servers=lambda: [], read=None, origin=lambda: ORIGIN,
                      registries=(signers.Review("kd-review", str(tmp_path / "signers"), lambda: me),
                                  signers.ModelKeys("kd-model-keys", str(tmp_path / "model"), lambda: me)))
    out = io.StringIO()
    assert signers.main(["sync", "--apply"], env=env, euid=0, out=out, err=out) == 0, out.getvalue()
    opener = forgejo()
    assert reviewer(tmp_path, opener).handle(signed(new))["state"] == "approved"
    error = refused(reviewer(tmp_path, forgejo()), signed(auth, nonce="cd" * 16))
    assert "not one of owner's signers" in str(error)


# --- carry: the signature of a head on that head brought up to date with the target (KD-9442) ------------------

MAIN = "a" * 40
H3, OWN, M1, M2, STRAY = "f" * 40, "9" * 40, "1" * 40, "2" * 40, "3" * 40
DIFF = ("diff --git a/x.py b/x.py\nindex 1111111..2222222 100644\n--- a/x.py\n+++ b/x.py\n"
        "@@ -10,3 +10,4 @@ def f():\n a\n b\n+c\n d\n")
MOVED = DIFF.replace("1111111..2222222", "3333333..4444444").replace("@@ -10,3 +10,4 @@ def f():", "@@ -14,3 +14,4 @@")
EVIL = MOVED.replace("+c\n", "+c\n+evil()\n")  # a line changed inside the merge


class Live:
    """Forgejo whose PR moves: ``head``, the PR's diff per head, the commits' parents, what the target holds."""

    def __init__(self, head=HEAD, base="main"):
        self.head, self.base, self.reviews, self.seen = head, base, [], []
        self.diffs = {HEAD: DIFF}
        self.parents = {}
        self.in_target = {MAIN, M1, M2}
        self.statuses = {}

    def __call__(self, req, timeout=None):
        method, url = req.get_method(), req.full_url
        self.seen.append((method, url, req.data))
        path = url[len(forge.api_url()):].split("?")[0]
        pulls = f"/repos/{REPO}/pulls/12"
        if method == "POST" and path == pulls + "/reviews":
            body = json.loads(req.data)
            self.reviews.append({"id": 90 + len(self.reviews), "user": {"login": "digipaddock-review"},
                                 "state": "APPROVED", "commit_id": body["commit_id"], "body": body["body"]})
            return Resp(json.dumps({"id": self.reviews[-1]["id"]}).encode())
        if path == pulls + ".diff":
            diff = self.diffs.get(self.head)
            if diff is None:
                raise urllib.error.HTTPError(url, 500, "x", {}, Resp(b"{}"))
            return Resp(diff.encode())
        if path == pulls + "/reviews":
            return Resp(json.dumps(self.reviews if "page=1" in url else []).encode())
        if path == pulls:
            return Resp(json.dumps({**PR, "head": {"sha": self.head}, "base": {"ref": self.base}}).encode())
        if path == f"/repos/{REPO}/branches/{self.base}":
            return Resp(json.dumps({"commit": {"id": MAIN}}).encode())
        if path.startswith(f"/repos/{REPO}/git/commits/"):
            sha = path.rsplit("/", 1)[1]
            return Resp(json.dumps({"sha": sha, "parents": [{"sha": p} for p in self.parents.get(sha, [])]}).encode())
        if path.startswith(f"/repos/{REPO}/compare/{MAIN}..."):
            return Resp(json.dumps({"total_commits": 0 if path.rsplit("...", 1)[1] in self.in_target else 3}).encode())
        if path.startswith(f"/repos/{REPO}/commits/") and path.endswith("/status"):
            found = [{"context": c, "status": s} for c, s in self.statuses.items()]
            return Resp(json.dumps({"statuses": found}).encode())
        raise AssertionError(f"no route {method} {url}")


def carry(head=HEAD2, old=HEAD):
    return {"op": "carry", "project": "digipaddock", "repo": REPO, "number": 12, "from": old, "head": head}


def approved_then_updated(tmp_path, auth, live, mode="merge", **kw):
    """The owner signed HEAD; then the branch got main merged in: HEAD2 = merge(HEAD, M1)."""
    r = reviewer(tmp_path, live, **kw)
    assert r.handle(signed(auth, mode=mode))["state"] == "approved"
    live.head, live.diffs[HEAD2], live.parents[HEAD2] = HEAD2, MOVED, [HEAD, M1]
    return r


def test_a_clean_update_carries_the_signature_and_its_mode(tmp_path, auth):
    live = Live()
    r = approved_then_updated(tmp_path, auth, live, mode="release")
    out = r.handle(carry())
    assert out == {"review": 91, "head": HEAD2, "state": "carried", "from": HEAD, "mode": "release", "target": "main",
                   "decision": DECISION}
    body = live.reviews[-1]["body"]
    assert live.reviews[-1]["commit_id"] == HEAD2
    assert body == ("Одобрено владельцем в кабинете (одобрение, merge и выкатка), решение 11111111 (подпись "
                    f"перенесена с {HEAD[:7]} после обновления от main), в ветку main")
    # kd-merge reads it as the owner's release signature on exactly this head
    assert forge.signed_mode(body) == "release" and forge._owner_approval(live.reviews[-1], HEAD2, "main", ("release",))
    assert r.handle(carry())["state"] == "already" and len(live.reviews) == 2


def test_a_carry_chains_and_survives_a_restart(tmp_path, auth):
    """HEAD → HEAD2 → H3, each a merge of main; the record lives in approvals.json."""
    store = tmp_path / "state" / "approvals.json"
    live = Live()
    approved_then_updated(tmp_path, auth, live, approvals=store)
    assert reviewer(tmp_path, live, approvals=store).handle(carry())["state"] == "carried"
    live.head, live.diffs[H3], live.parents[H3] = H3, MOVED, [HEAD2, M2]
    assert reviewer(tmp_path, live, approvals=store).handle(carry(H3, HEAD2))["state"] == "carried"
    assert "решение 11111111" in live.reviews[-1]["body"]


@pytest.mark.parametrize("change, why", [
    (lambda live: live.parents.update({HEAD2: [HEAD]}), "не merge"),                     # its own commit after HEAD
    (lambda live: live.parents.update({HEAD2: [OWN, M1], OWN: [HEAD]}), "не merge"),     # own commit, then main
    (lambda live: live.diffs.update({HEAD2: EVIL}), "own diff changed"),                 # a resolved conflict
    (lambda live: live.parents.update({HEAD2: [HEAD, STRAY]}), "нет в main"),            # not from the target
    (lambda live: live.diffs.update({HEAD2: MOVED + "+x\n" * (forge.LIMIT // 3)}), "cut"),  # a cut diff
    (lambda live: live.diffs.pop(HEAD2), "cut or unreadable"),                            # an unreadable diff
    (lambda live: setattr(live, "base", "dev"), "targets dev"),                           # retargeted
    (lambda live: live.parents.update({HEAD2: [HEAD, M1, M2]}), "не merge"),             # an octopus
])
def test_anything_but_a_clean_update_needs_a_new_signature(tmp_path, auth, change, why):
    live = Live()
    r = approved_then_updated(tmp_path, auth, live, repos=TWO)
    change(live)
    error = refused(r, carry(), "stale" if live.base == "dev" else "refused")
    assert why in str(error)
    assert len(live.reviews) == 1  # only the owner's own Approve on HEAD


BINARY = "diff --git a/logo.png b/logo.png\nindex 5555555..6666666 100644\nBinary files a/logo.png and b/logo.png differ\n"


@pytest.mark.parametrize("opaque", [
    BINARY,
    "diff --git a/logo.png b/logo.png\nindex 5555555..6666666 100644\nGIT binary patch\nliteral 4\nLcmZQzWMT#Y01f~L\n\n",
])
def test_a_change_the_diffs_text_does_not_show_is_never_carried(tmp_path, auth, opaque):
    """KD-9478: a merge from the target whose tree also swaps the binary's bytes (an evil merge) keeps the diff's
    text: the binary is one line without its bytes, and the blob ids of its index line are not hashed."""
    live = Live()
    live.diffs[HEAD] = DIFF + opaque
    r = approved_then_updated(tmp_path, auth, live)
    live.diffs[HEAD2] = MOVED + opaque.replace("5555555..6666666", "7777777..8888888")
    error = refused(r, carry())
    assert "its text does not show" in str(error) and ("Binary files a/logo.png" in str(error)
                                                       or "GIT binary patch" in str(error))
    assert len(live.reviews) == 1


def test_an_evil_merge_into_a_file_the_pr_never_touched_changes_its_diff(tmp_path, auth):
    """The PR's diff is against the target: a merge that also changes another file adds that file to it."""
    live = Live()
    r = approved_then_updated(tmp_path, auth, live)
    live.diffs[HEAD2] = MOVED + ("diff --git a/common/kd/other.py b/common/kd/other.py\nindex 1234567..89abcde 100644\n"
                                 "--- a/common/kd/other.py\n+++ b/common/kd/other.py\n@@ -1 +1 @@\n-a\n+evil()\n")
    assert "own diff changed" in str(refused(r, carry()))
    assert len(live.reviews) == 1


def test_no_record_nothing_to_carry(tmp_path, auth):
    live = Live(head=HEAD2)
    live.parents[HEAD2] = [HEAD, M1]
    assert "no Approve of #12" in str(refused(reviewer(tmp_path, live), carry()))
    # a record older than its term, or one whose diff was not read at the approval
    r = approved_then_updated(tmp_path, auth, Live())
    r.clock = lambda: NOW + review.RECORD_SECONDS + 1
    assert "no Approve of #12" in str(refused(r, carry()))
    live = Live()
    live.diffs.pop(HEAD)
    r = approved_then_updated(tmp_path, auth, live)
    assert "no diff to compare" in str(refused(r, carry()))


def test_a_carry_needs_the_required_checks_green(tmp_path, auth):
    repos_ = {"digipaddock": {"repo": REPO, "dir": "work/DigiPaddock", "checks": {"profiles": {"unit": {"required": True}}}}}
    live = Live()
    live.statuses = {"devcheck/unit": "success"}
    r = approved_then_updated(tmp_path, auth, live, repos=repos_)
    live.statuses = {"devcheck/unit": "pending"}
    assert "devcheck/unit (pending)" in str(refused(r, carry()))
    live.statuses = {"devcheck/unit": "success"}
    assert r.handle(carry())["state"] == "carried"
