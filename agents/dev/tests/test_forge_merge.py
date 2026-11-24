# agents/dev/tests/test_forge_merge.py
import io
import json
import os
import urllib.error
import pytest
from dev_agent import forge, repos
from test_forge_read import REPO, Resp, make_opener, paths, write_env

HEAD = "c" * 40
MERGE = "d" * 40
MAIN = "a" * 40  # the target's live head
R = repos.Repo("digipaddock", REPO, "/srv/digipaddock-agents/dev/work/x", "digipaddock")  # passport defaults: main, current base
GOOD_RULE = {"rule_name": "main", "enable_push": True, "enable_push_whitelist": True, "push_whitelist_usernames": ["example-owner"],
             "push_whitelist_deploy_keys": False, "enable_merge_whitelist": True,
             "merge_whitelist_usernames": ["example-owner", "digipaddock-merge"], "block_on_rejected_reviews": True,
             # merge only after the owner's Approve on the current head
             "required_approvals": 1, "enable_approvals_whitelist": True, "approvals_whitelist_username": ["example-owner"],
             "dismiss_stale_approvals": True}
APPROVED = {"id": 3, "user": {"login": "example-owner"}, "state": "APPROVED", "commit_id": HEAD, "dismissed": False, "stale": False}


def protection(rule=None, permission="read"):
    return {("GET", f"/repos/{REPO}/branch_protections"): [rule or GOOD_RULE],
            ("GET", f"/repos/{REPO}/collaborators/digipaddock-dev/permission"): {"permission": permission}}


def merge_opener(reviews, *, pr=None, statuses=None, behind=0, files=None, rule=None, moved=None):
    """Forgejo for a merge: the protection is in order, the reviews are ``reviews``; after the POST the same GET of the
    PR returns merged. ``pr`` — PR fields over pr_payload(); ``statuses`` — context → state of the head's combined
    status; ``behind`` — how many commits of the target branch the head lacks (compare); ``files`` — the PR's files;
    ``moved`` — the PR's fields at the second read (retargeted during the checks)."""
    seen, posted, reads = [], {"done": False}, {"n": 0}
    def opener(req, timeout=None):
        method, url = req.get_method(), req.full_url
        seen.append((method, url, req.data, dict(req.header_items())))
        if method == "POST" and url.endswith("/pulls/12/merge"):
            posted["done"] = True
            return Resp(b"{}")
        if method == "GET" and "/pulls/12/reviews" in url:
            return Resp(json.dumps(reviews).encode())
        if method == "GET" and "/pulls/12/files" in url:
            return Resp(json.dumps((files or []) if "page=1" in url else []).encode())
        if method == "GET" and url.endswith("/branch_protections"):
            return Resp(json.dumps([rule or GOOD_RULE]).encode())
        if method == "GET" and url.endswith("/collaborators/digipaddock-dev/permission"):
            return Resp(json.dumps({"permission": "read"}).encode())
        if method == "GET" and "/branches/" in url:
            return Resp(json.dumps({"name": url.rsplit("/", 1)[1], "commit": {"id": MAIN}}).encode())
        if method == "GET" and f"/compare/{HEAD}...{MAIN}" in url:
            return Resp(json.dumps({"total_commits": behind, "commits": [{}] * behind, "files": []}).encode())
        if method == "GET" and f"/commits/{HEAD}/status" in url:
            found = [{"context": c, "status": s, "state": None} for c, s in (statuses or {}).items()]  # Forgejo: status, not state
            return Resp(json.dumps({"sha": HEAD, "state": "", "statuses": found, "total_count": len(found)}).encode())
        if method == "GET" and url.endswith("/pulls/12"):
            reads["n"] += 1
            later = (moved or {}) if reads["n"] > 1 else {}
            if posted["done"]:
                return Resp(json.dumps({**pr_payload(state="closed", merged=True, merge_sha=MERGE),
                                        **{k: v for k, v in (pr or {}).items() if k == "base"}, **later}).encode())
            return Resp(json.dumps({**pr_payload(), **(pr or {}), **later}).encode())
        raise AssertionError(url)
    opener.seen = seen
    return opener


def pr_payload(state="open", merged=False, mergeable=True, head=HEAD, author=forge.dev_login(), merge_sha=None,
               merged_by=None):
    return {"number": 12, "title": "KD-12: заголовок <x>", "state": state, "merged": merged, "mergeable": mergeable,
            "head": {"sha": head, "ref": "dev/agent/KD-12-x"}, "base": {"ref": "main", "sha": MAIN},
            "user": {"login": author}, "html_url": "u", "merge_commit_sha": merge_sha, "draft": False,
            **({"merged_by": {"login": merged_by}} if merged_by else {})}


def owner_merged():
    """A PR the owner merged in Forgejo himself: his word releases it."""
    return pr_payload(state="closed", merged=True, merge_sha=MERGE, merged_by="example-owner")


def test_merge_checks_author_sha_and_state():
    api = forge.Api("t" * 20, opener=make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr_payload(head="e" * 40)}))
    with pytest.raises(forge.ForgeError, match="изменился после карточки"):
        forge.merge_pr(api, api, R, 12, HEAD)
    api = forge.Api("t" * 20, opener=make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr_payload()}))
    with pytest.raises(forge.ForgeError, match="изменился после карточки"):
        forge.merge_pr(api, api, R, 12, HEAD[:10])  # a prefix is not accepted — the full sha only
    api = forge.Api("t" * 20, opener=make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr_payload(author="example-owner")}))
    with pytest.raises(forge.ForgeError, match=f"не от {forge.dev_login()}"):
        forge.merge_pr(api, api, R, 12, HEAD)
    api = forge.Api("t" * 20, opener=make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr_payload(state="closed")}))
    with pytest.raises(forge.ForgeError, match="закрыт без merge"):
        forge.merge_pr(api, api, R, 12, HEAD)
    api = forge.Api("t" * 20, opener=make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr_payload(mergeable=False)}))
    with pytest.raises(forge.ForgeError, match="не mergeable"):
        forge.merge_pr(api, api, R, 12, HEAD)


def test_merge_success_posts_card_sha_after_protection_and_approval_check():
    opener = merge_opener([APPROVED])
    seen = opener.seen
    read, merge = forge.Api("r" * 20, opener=opener), forge.Api("m" * 20, opener=opener)
    sha, title, state = forge.merge_pr(read, merge, R, 12, HEAD)
    assert (sha, state) == (MERGE, "merged") and title.startswith("KD-12")
    post = [c for c in seen if c[0] == "POST"][0]
    body = json.loads(post[2])
    assert body["Do"] == "merge" and body["head_commit_id"] == HEAD
    assert "KD-12" in body["MergeTitleField"] and "KD-12" in body["MergeMessageField"]
    assert post[3]["Authorization"] == "token " + "m" * 20  # the merge uses the merge account's token, reads use dev's
    reviews = [c for c in seen if "/pulls/12/reviews" in c[1]][0]
    assert reviews[3]["Authorization"] == "token " + "r" * 20  # dev reads the reviews (the Read right)
    ends = [c[1].split("?", 1)[0].rsplit("/", 1)[-1] for c in seen]
    assert ends.index("branch_protections") < ends.index("reviews") < ends.index("merge")  # the protection and the Approve come before the POST


@pytest.mark.parametrize("reviews", [
    [],                                                                    # nobody approved
    [{**APPROVED, "user": {"login": "digipaddock-merge"}}],                          # approved by someone other than the owner
    [{**APPROVED, "commit_id": "e" * 40}],                                 # an Approve of another commit
    [{**APPROVED, "state": "REQUEST_CHANGES"}],                            # the owner asked for changes
    [{**APPROVED, "state": "COMMENT"}],
    [{**APPROVED, "dismissed": True}],                                     # the approval is dismissed (dismiss stale)
], ids=["нет", "не владелец", "другой коммит", "request changes", "comment", "снято"])
def test_merge_refused_without_owner_approval_on_sha(reviews):
    opener = merge_opener(reviews)
    api = forge.Api("t" * 20, opener=opener)
    with pytest.raises(forge.ForgeError, match="не одобрен владельцем"):
        forge.merge_pr(api, api, R, 12, HEAD)
    assert not [c for c in opener.seen if c[0] == "POST"]  # without an Approve no POST is sent


def test_owner_approved_reads_reviews_with_limit():
    opener = merge_opener([{**APPROVED, "commit_id": "e" * 40}, APPROVED])
    api = forge.Api("t" * 20, opener=opener)
    assert forge.owner_approved(api, REPO, 12, HEAD, "main") is True       # at least one valid approval
    assert forge.owner_approved(api, REPO, 12, "f" * 40, "main") is False
    assert any("/pulls/12/reviews?limit=50" in c[1] for c in opener.seen)


def test_merge_refused_when_main_protection_is_wrong():
    bad = {**GOOD_RULE, "push_whitelist_usernames": ["example-owner", "digipaddock-merge"]}
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr_payload(), **protection(rule=bad)})
    api = forge.Api("t" * 20, opener=op)
    with pytest.raises(forge.ForgeError, match="защита main не настроена.*push whitelist"):
        forge.merge_pr(api, api, R, 12, HEAD)
    assert not [c for c in op.seen if c[0] == "POST"]
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr_payload(), **protection(permission="write")})
    api = forge.Api("t" * 20, opener=op)
    with pytest.raises(forge.ForgeError, match=forge.dev_login()):
        forge.merge_pr(api, api, R, 12, HEAD)


def test_protection_problems_direct_and_fallback():
    api = forge.Api("t" * 20, opener=make_opener(protection()))
    assert forge.protection_problems(api, api, REPO) == []
    no_main = forge.Api("t" * 20, opener=make_opener({**protection(), ("GET", f"/repos/{REPO}/branch_protections"): []}))
    assert "нет правила защиты main" in forge.protection_problems(no_main, no_main, REPO)
    # without the owner's required Approve the rule is no good
    no_approve = {**GOOD_RULE, "required_approvals": 0, "enable_approvals_whitelist": False,
                  "approvals_whitelist_username": [], "dismiss_stale_approvals": False}
    api = forge.Api("t" * 20, opener=make_opener(protection(rule=no_approve)))
    text = "; ".join(forge.protection_problems(api, api, REPO))
    for part in ("required approvals", "enable_approvals_whitelist", "approvals whitelist", "dismiss_stale_approvals"):
        assert part in text, part
    others = {**GOOD_RULE, "approvals_whitelist_username": ["example-owner", "digipaddock-merge"]}
    api = forge.Api("t" * 20, opener=make_opener(protection(rule=others)))
    assert any("approvals whitelist" in p for p in forge.protection_problems(api, api, REPO))
    # Forgejo gives branch_protections and other accounts' rights only to the repository's admins — the fallback path
    branch = {"protected": True, "effective_branch_protection_name": "main", "user_can_push": False, "user_can_merge": True,
              "required_approvals": 1}
    merge = forge.Api("m" * 20, opener=make_opener({("GET", f"/repos/{REPO}/branch_protections"): (403, {"message": "admin only"}),
                                                    ("GET", f"/repos/{REPO}/branches/main"): branch,
                                                    ("GET", f"/repos/{REPO}/collaborators/digipaddock-dev/permission"): (403, {"message": "own only"})}))
    read = forge.Api("r" * 20, opener=make_opener({("GET", f"/repos/{REPO}/collaborators/digipaddock-dev/permission"): {"permission": "read"}}))
    assert forge.protection_problems(merge, read, REPO) == []
    pushy = forge.Api("m" * 20, opener=make_opener({("GET", f"/repos/{REPO}/branch_protections"): (403, {}),
                                                    ("GET", f"/repos/{REPO}/branches/main"): {**branch, "user_can_push": True},
                                                    ("GET", f"/repos/{REPO}/collaborators/digipaddock-dev/permission"): {"permission": "read"}}))
    assert forge.merge_login() + " может push в main" in forge.protection_problems(pushy, pushy, REPO)
    lax = forge.Api("m" * 20, opener=make_opener({("GET", f"/repos/{REPO}/branch_protections"): (403, {}),
                                                  ("GET", f"/repos/{REPO}/branches/main"): {**branch, "required_approvals": 0},
                                                  ("GET", f"/repos/{REPO}/collaborators/digipaddock-dev/permission"): {"permission": "read"}}))
    assert any("required approvals" in p for p in forge.protection_problems(lax, lax, REPO))


def test_already_merged_returns_merge_sha():
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr_payload(state="closed", merged=True, merge_sha=MERGE)})
    api = forge.Api("t" * 20, opener=op)
    assert forge.merge_pr(api, api, R, 12, HEAD) == (MERGE, "KD-12: заголовок <x>", "already")


def test_request_and_wait(tmp_path):
    rel, res = tmp_path / "release", tmp_path / "result"
    rel.mkdir(); res.mkdir()
    ts = forge.request_release(str(rel), "digipaddock", REPO, MERGE, 12, clock=lambda: 1000.0, sleep=lambda s: None)
    req = json.loads((rel / "request.json").read_text(encoding="utf-8"))
    assert req == {"project": "digipaddock", "repo": REPO, "sha": MERGE, "pr": 12, "request_id": ts, "ts": ts}
    assert not [p for p in rel.iterdir() if p.name.endswith(".tmp")]
    now = {"t": 0.0}
    def clock():
        return now["t"]
    def sleep(s):
        now["t"] += s
        if now["t"] >= 4:
            (res / "result.json").write_text(json.dumps({"ts": ts, "ok": True, "health": "healthy", "revision": "d" * 7, "previous": "a" * 7}), encoding="utf-8")
    assert forge.wait_result(str(res), ts, deadline=10, clock=clock, sleep=sleep)["revision"] == "d" * 7
    (res / "result.json").write_text(json.dumps({"ts": "stale", "ok": True}), encoding="utf-8")
    now["t"] = 0.0
    assert forge.wait_result(str(res), ts, deadline=3, clock=clock, sleep=lambda s: now.__setitem__("t", now["t"] + s)) is None


def test_retries_reuse_queued_active_and_terminal_request(tmp_path):
    rel, res = tmp_path / "release", tmp_path / "result"
    rel.mkdir()
    res.mkdir()
    protocol = forge.release_requests

    def submit():
        return forge.request_release(str(rel), "digipaddock", REPO, MERGE, 12, result_dir=str(res))

    key = submit()
    queue = rel / "request.json"
    original = queue.read_bytes()
    assert submit() == key and queue.read_bytes() == original
    protocol.write(res, protocol.ACTIVE, {"request_id": key, "state": "checking"})
    queue.unlink()
    assert submit() == key and not queue.exists()
    terminal = {"request_id": key, "state": "healthy", "ok": True}
    protocol.write(res, protocol.result_name(key), terminal)
    (res / protocol.ACTIVE).unlink()
    protocol.write(res, protocol.LATEST, {"request_id": "other", "state": "healthy", "ok": True})
    assert submit() == key and not queue.exists()
    assert forge.wait_result(str(res), key, deadline=0) == terminal


def test_legacy_switch_is_not_a_result(tmp_path):
    protocol = forge.release_requests
    key = protocol.forge_id("digipaddock", REPO, 12, MERGE)
    switched = {"ts": key, "ok": True, "revision": MERGE[:7]}
    protocol.write(tmp_path, protocol.LATEST, switched)
    assert forge.wait_result(str(tmp_path), key, deadline=0) is None
    protocol.write(tmp_path, protocol.LATEST, {**switched, "health": "healthy"})
    assert forge.wait_result(str(tmp_path), key, deadline=0)["ok"]


@pytest.mark.skipif(forge.fcntl is None, reason="flock is POSIX-only")
def test_concurrent_retries_write_one_request(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    rel, res = tmp_path / "release", tmp_path / "result"
    rel.mkdir()
    res.mkdir()
    writes = []
    write = forge.release_requests.write
    ready = Barrier(2, timeout=5)

    def record(*args, **kwargs):
        writes.append(args[1])
        return write(*args, **kwargs)

    def submit():
        ready.wait()
        return forge.request_release(str(rel), "digipaddock", REPO, MERGE, 12, result_dir=str(res))

    monkeypatch.setattr(forge.release_requests, "write", record)
    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = pool.submit(submit), pool.submit(submit)
        assert left.result(timeout=10) == right.result(timeout=10)
    assert writes == ["request.json"]


def test_request_waits_for_previous_request(tmp_path):
    rel = tmp_path / "release"; rel.mkdir()
    (rel / "request.json").write_text("{}", encoding="utf-8")  # kd-release has not taken the previous request yet
    now = {"t": 0.0}
    def sleep(s):
        now["t"] += s
        if now["t"] >= 6:
            (rel / "request.json").unlink()
    ts = forge.request_release(str(rel), "digipaddock", REPO, MERGE, 12, clock=lambda: now["t"], sleep=sleep)
    assert json.loads((rel / "request.json").read_text(encoding="utf-8"))["ts"] == ts and now["t"] >= 6
    with pytest.raises(forge.ForgeError, match="240"):  # the request hangs longer than RELEASE_WAIT — it is not overwritten
        forge.request_release(str(rel), "digipaddock", REPO, MERGE, 13, clock=lambda: now["t"],
                              sleep=lambda s: now.__setitem__("t", now["t"] + 100))


def told(sent):
    """A fake notify(level, key, params, card) that keeps every notice."""
    return lambda *notice: sent.append(notice)


def test_merged_notice_drops_the_card_prefix_of_the_title():
    expect = ("info", "notice.forge.merged", {"pr": 12, "title": "a <b> & c"})
    assert forge.notice_merged("a <b> & c", 12) == expect
    assert forge.notice_merged("KD-12: a <b> & c", 12) == expect  # the PR title already starts with KD-12:
    assert forge.notice_merged("OLD-12: a <b> & c", 12) == expect  # a PR opened before the card prefix moved


def test_released_notice_names_this_host(monkeypatch):
    monkeypatch.setattr(forge.socket, "gethostname", lambda: "host-a")
    assert forge.notice_released("3767f18") == ("info", "notice.forge.released", {"host": "host-a", "rev": "3767f18"})



def test_a_pair_release_names_both_revisions(monkeypatch):
    """code + overlay — the notice carries the overlay's revision and the release id, the line both."""
    monkeypatch.setattr(forge.socket, "gethostname", lambda: "host-a")
    assert forge.notice_released("3767f18", "1234567", "3767f18+1234567") == (
        "info", "notice.forge.released",
        {"host": "host-a", "rev": "3767f18", "deploy_rev": "1234567", "release_id": "3767f18+1234567"})
    result = {"revision": "3767f18", "previous": "aaaaaaa", "deploy_revision": "1234567", "deploy_previous": "bbbbbbb"}
    assert forge.released_revisions(result) == "REVISION aaaaaaa → 3767f18, overlay bbbbbbb → 1234567"
    assert forge.released_revisions({"revision": "3767f18", "previous": "aaaaaaa"}) == "REVISION aaaaaaa → 3767f18"


def test_refused_merge_is_a_warn_notice():
    assert forge.notice_refused("x <y>") == ("warn", "notice.forge.merge_refused", {"why": "x <y>"})
    assert forge.notice_refused("w" * 900)[2]["why"] == "w" * 500 + "…"
    assert forge.notice_release_refused("main <ушёл>") == ("warn", "notice.forge.release_refused",
                                                           {"why": "main <ушёл>"})


def test_release_degraded_is_warn(monkeypatch):
    monkeypatch.setattr(forge.socket, "gethostname", lambda: "host-a")
    assert forge.notice_release_degraded("ddddddd", "postflight") == (
        "warn", "notice.forge.release_degraded", {"host": "host-a", "rev": "ddddddd", "why": "postflight"})


def test_default_poster_posts_as_forge_and_only_on_a_card(monkeypatch):
    calls = []
    monkeypatch.setattr(forge.cabinet_client, "post_notice",
                        lambda *args, **kwargs: calls.append((args, kwargs)) or True)
    assert forge.post_notice("warn", "notice.forge.merge_refused", {"why": "x"}, "PR #5") is False and calls == []
    assert forge.post_notice("warn", "notice.forge.merge_refused", {"why": "x"}, "KD-5") is True
    assert calls == [(("forge", "warn", "notice.forge.merge_refused", {"why": "x"}), {"card": "KD-5"})]


def test_main_merge_flows(tmp_path):
    d = write_env(tmp_path)
    rel, res = tmp_path / "release", tmp_path / "result"
    rel.mkdir(); res.mkdir()
    kw = {**paths(tmp_path, d), "release_dir": str(rel), "result_dir": str(res)}
    j = tmp_path / "forge.jsonl"
    sent = []
    # 0) a short sha — refused at parsing, Forgejo is not called
    op = make_opener({})
    assert forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD[:10]], opener=op, notify=told(sent),
                      out=io.StringIO(), err=io.StringIO(), **kw) == 2
    assert not op.seen
    # 1) the sha did not match → 1, ⛔ in the card's thread, nothing is POSTed; the journal has the intent and the outcome
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr_payload(head="e" * 40)})
    err = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD], opener=op, notify=told(sent), out=io.StringIO(), err=err, **kw)
    assert rc == 1 and "изменился" in err.getvalue() and not [c for c in op.seen if c[0] == "POST"]
    assert sent[-1][:2] == ("warn", "notice.forge.merge_refused") and sent[-1][3] == "KD-12"
    phases = [json.loads(line).get("phase") for line in j.read_text(encoding="utf-8").splitlines()]
    assert phases[-2:] == ["intent", "result"]
    # 2) already merged without --release → 1 "already merged"
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): owner_merged()})
    err = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD], opener=op, notify=told(sent), out=io.StringIO(), err=err, **kw)
    assert rc == 1 and "уже смержен" in err.getvalue()
    # 3) already merged with --release → the release of merge_commit_sha; kd-release takes the request and writes
    # the result → 0, REVISION old → new
    requests = []
    def sleep(s):
        if (rel / "request.json").exists():
            requests.append(json.loads((rel / "request.json").read_text(encoding="utf-8")))
            (rel / "request.json").unlink()
            (res / "result.json").write_text(json.dumps({"ts": requests[-1]["ts"], "ok": True, "health": "healthy", "revision": MERGE[:7],
                                                         "previous": "a" * 7}), encoding="utf-8")
    out = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told(sent), out=out,
                    err=io.StringIO(), sleep=sleep, clock=lambda: 1.0, **kw)
    assert rc == 0 and f"REVISION {'a' * 7} → {MERGE[:7]}" in out.getvalue()
    assert sent[-1][1] == "notice.forge.released" and sent[-1][2]["rev"] == MERGE[:7] and sent[-1][3] == "KD-12"
    assert requests[-1]["sha"] == MERGE
    # 4) the release result did not come in time → 3, the merge happened, a hint for the owner
    (res / "result.json").unlink()
    t = {"v": 0.0}
    def clock():
        return t["v"]
    def slow(s):
        t["v"] += 100
    err = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told(sent),
                    out=io.StringIO(), err=err, sleep=slow, clock=clock, **kw)
    assert rc == 3 and "не дождал" in err.getvalue() and "journalctl -u digipaddock-release на example-stage" in err.getvalue()
    # 5) kd-release refused → 1 and exactly one ⛔ release notice (the notice service itself is not written to)
    (rel / "request.json").unlink()
    def refused(s):
        if (rel / "request.json").exists():
            req = json.loads((rel / "request.json").read_text(encoding="utf-8"))
            (rel / "request.json").unlink()
            (res / "result.json").write_text(json.dumps({"ts": req["ts"], "ok": False, "error": "main ушёл дальше"}), encoding="utf-8")
    before = len(sent)
    err = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told(sent),
                    out=io.StringIO(), err=err, sleep=refused, clock=lambda: 1.0, **kw)
    assert rc == 1 and "main ушёл дальше" in err.getvalue() and len(sent) == before + 1
    assert sent[-1][:3] == ("warn", "notice.forge.release_refused", {"why": "main ушёл дальше"})
    # 5a) the code is switched but postflight failed (degraded): neither "rejected" nor "released" — 3 and ⚠️ in
    # the thread
    (res / "result.json").unlink()
    def degraded(s):
        if (rel / "request.json").exists():
            req = json.loads((rel / "request.json").read_text(encoding="utf-8"))
            (rel / "request.json").unlink()
            (res / "result.json").write_text(json.dumps({"ts": req["ts"], "ok": False, "degraded": True, "health": "degraded", "revision": "d" * 7,
                                                         "previous": "a" * 7, "error": "код выкачен, но generated-агенты"}),
                                             encoding="utf-8")
    before = len(sent)
    err = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told(sent),
                    out=io.StringIO(), err=err, sleep=degraded, clock=lambda: 1.0, **kw)
    assert rc == 3 and "проверка после выкатки не прошла" in err.getvalue() and "отклонена" not in err.getvalue()
    assert len(sent) == before + 1 and sent[-1][:2] == ("warn", "notice.forge.release_degraded")
    assert sent[-1][2]["rev"] == "ddddddd"
    last = json.loads(j.read_text(encoding="utf-8").splitlines()[-1])
    assert last["cmd"] == "release" and last["ok"] is False and last["result"].startswith("degraded:")
    # 6) release without the release field in repos.json → 2
    (tmp_path / "repos.json").write_text(json.dumps({"api": {"repo": "k/Api", "dir": "work/Api"}}), encoding="utf-8")
    rc = forge.main(["--agent", "dev", "merge", "api", "12", HEAD, "--release"], opener=op, notify=told(sent),
                    out=io.StringIO(), err=io.StringIO(), **kw)
    assert rc == 2
    text = j.read_text(encoding="utf-8")
    assert "m" * 20 not in text and "merge0123456789abcdef" not in text and '"claimed_agent": "dev"' in text


def test_merge_release_prints_operation_and_returns(tmp_path):
    """the release asked the factory to reinstall the roles; kd-merge does not wait for it."""
    d = write_env(tmp_path)
    rel, res = tmp_path / "release", tmp_path / "result"
    rel.mkdir(); res.mkdir()
    kw = {**paths(tmp_path, d), "release_dir": str(rel), "result_dir": str(res)}
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): owner_merged()})
    requests = []

    def released(reinstall):
        def sleep(s):
            if (rel / "request.json").exists():
                requests.append(json.loads((rel / "request.json").read_text(encoding="utf-8")))
                (rel / "request.json").unlink()
                (res / "result.json").write_text(json.dumps({"ts": requests[-1]["ts"], "ok": True, "health": "healthy", "revision": MERGE[:7],
                                                             "previous": "a" * 7, "reinstall": reinstall}),
                                                 encoding="utf-8")
        return sleep

    out = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told([]), out=out,
                    err=io.StringIO(), sleep=released({"state": "pending", "operation": "f" * 32,
                                                       "agents": ["boss", "dev"]}), clock=lambda: 1.0, **kw)
    text = out.getvalue()
    assert rc == 0 and requests[-1]["card"] == "KD-12"
    assert f"установка ролей boss,dev: операция {'f' * 32}" in text
    assert f"kd-work finish KD-12 --outcome wait_dependency --operation {'f' * 32}" in text
    assert "за владельцем" not in text
    (res / "result.json").unlink()
    out = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told([]), out=out,
                    err=io.StringIO(), sleep=released({"state": "uncovered", "agents": ["boss"], "why": "нет разрешения"}),
                    clock=lambda: 1.0, **kw)
    assert rc == 0 and "роли boss не переустановлены: нет разрешения" in out.getvalue()
    assert "kd-factory plan KD-12" in out.getvalue()
    (res / "result.json").unlink()
    out = io.StringIO()  # the request waits for the cabinet's update — the lead waits on its key
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told([]), out=out,
                    err=io.StringIO(), sleep=released({"state": "deferred", "key": f"release:{MERGE[:7]}",
                                                       "agents": ["dev"], "why": "unknown operation", "retry": True}),
                    clock=lambda: 1.0, **kw)
    text = out.getvalue()
    assert rc == 0 and f"kd-work finish KD-12 --outcome wait_dependency --operation release:{MERGE[:7]}" in text
    assert "установка ролей dev" in text and "не переустановлены" not in text


def test_roles_not_checked_are_said_to_the_lead():
    """Review I2: kd-release could not check the roles — the lead hears it, not the line about units."""
    line = forge._reinstall_line({"state": "unchecked", "agents": [], "why": "роли не проверены: TimeoutExpired"},
                                 "KD-12")
    assert "роли не проверены: TimeoutExpired" in line and "kd-alert" in line and "Юниты" not in line


def test_a_degraded_release_still_says_what_became_of_the_roles(tmp_path):
    """Review M7: the postflight failed and the roles were not covered — the lead still hears which and what next."""
    d = write_env(tmp_path)
    rel, res = tmp_path / "release", tmp_path / "result"
    rel.mkdir(); res.mkdir()
    kw = {**paths(tmp_path, d), "release_dir": str(rel), "result_dir": str(res)}
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): owner_merged()})

    def degraded(s):
        if (rel / "request.json").exists():
            req = json.loads((rel / "request.json").read_text(encoding="utf-8"))
            (rel / "request.json").unlink()
            (res / "result.json").write_text(json.dumps({
                "ts": req["ts"], "ok": False, "degraded": True, "health": "degraded", "revision": MERGE[:7], "previous": "a" * 7,
                "error": "код выкачен, но generated-агенты", "reinstall": {"state": "uncovered", "agents": ["boss"],
                                                                         "why": "нет разрешения"}}), encoding="utf-8")

    err = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told([]),
                    out=io.StringIO(), err=err, sleep=degraded, clock=lambda: 1.0, **kw)
    assert rc == 3 and "роли boss не переустановлены: нет разрешения" in err.getvalue()
    (res / "result.json").unlink()
    unchecked = {"state": "unchecked", "agents": [], "why": "роли не проверены: TimeoutExpired"}

    def degraded_unchecked(s):  # re-review M3: no role named, the lead still hears the check did not run
        if (rel / "request.json").exists():
            req = json.loads((rel / "request.json").read_text(encoding="utf-8"))
            (rel / "request.json").unlink()
            (res / "result.json").write_text(json.dumps({
                "ts": req["ts"], "ok": False, "degraded": True, "health": "degraded", "revision": MERGE[:7], "previous": "a" * 7,
                "error": "код выкачен, но generated-агенты", "reinstall": unchecked}), encoding="utf-8")

    err = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told([]),
                    out=io.StringIO(), err=err, sleep=degraded_unchecked, clock=lambda: 1.0, **kw)
    assert rc == 3 and "роли не проверены: TimeoutExpired" in err.getvalue()
    (res / "result.json").unlink()
    steps = {"state": "none", "components": [{"component": "repo-attach", "mode": "install", "state": "pending",
                                              "operation": "op-1"}]}

    def degraded_steps(s):  # re-review 3, M1: only a component step — the lead still hears to wait on it
        if (rel / "request.json").exists():
            req = json.loads((rel / "request.json").read_text(encoding="utf-8"))
            (rel / "request.json").unlink()
            (res / "result.json").write_text(json.dumps({
                "ts": req["ts"], "ok": False, "degraded": True, "health": "degraded", "revision": MERGE[:7], "previous": "a" * 7,
                "error": "nginx", "reinstall": steps}), encoding="utf-8")

    err = io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told([]),
                    out=io.StringIO(), err=err, sleep=degraded_steps, clock=lambda: 1.0, **kw)
    assert rc == 3 and "--operation op-1" in err.getvalue() and "Юниты" not in err.getvalue()


@pytest.mark.parametrize("exc", [OSError, RuntimeError])  # a non-OSError from notify does not drop the outcome either
def test_merge_result_survives_broken_journal_and_notice(tmp_path, exc):
    d = write_env(tmp_path)
    rel, res = tmp_path / "release", tmp_path / "result"
    rel.mkdir(); res.mkdir()
    kw = {**paths(tmp_path, d), "journal_path": str(tmp_path / "нет-каталога" / "forge.jsonl"), "release_dir": str(rel), "result_dir": str(res)}
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): owner_merged()})
    def broken_notice(*notice):
        raise exc("кабинет недоступен")
    def sleep(s):
        if (rel / "request.json").exists():
            req = json.loads((rel / "request.json").read_text(encoding="utf-8"))
            (rel / "request.json").unlink()
            (res / "result.json").write_text(json.dumps({"ts": req["ts"], "ok": True, "health": "healthy", "revision": MERGE[:7], "previous": "a" * 7}), encoding="utf-8")
    out, err = io.StringIO(), io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=broken_notice,
                    out=out, err=err, sleep=sleep, clock=lambda: 1.0, **kw)
    assert rc == 0 and "уже был смержен" in out.getvalue() and "REVISION" in out.getvalue()
    assert "журнал" in err.getvalue() and "уведомление" in err.getvalue()


def test_best_effort_swallows_any_exception_as_one_line():
    # the journal and the notice never cost the outcome, whatever the error (a replaced notify, a json failure)
    def boom(*a, **kw):
        raise ValueError("сломалось")
    err = io.StringIO()
    assert forge._best_effort(err, "уведомление в треде", boom, "x") is None
    lines = err.getvalue().splitlines()
    assert len(lines) == 1 and "уведомление в треде" in lines[0] and "ValueError" in lines[0]


def test_selfcheck_subcommand(tmp_path):
    d = write_env(tmp_path)
    out = io.StringIO()
    assert forge.main(["--agent", "setup", "selfcheck", "digipaddock"], opener=make_opener(protection()), out=out, err=io.StringIO(),
                      **paths(tmp_path, d)) == 0
    assert "в порядке" in out.getvalue()
    bad = {**GOOD_RULE, "push_whitelist_usernames": ["example-owner", "digipaddock-merge"]}
    err = io.StringIO()
    assert forge.main(["--agent", "setup", "selfcheck", "digipaddock"], opener=make_opener(protection(rule=bad)), out=io.StringIO(), err=err,
                      **paths(tmp_path, d)) == 1
    assert "защита main не настроена" in err.getvalue() and "push whitelist" in err.getvalue()


# --- Beyond the brief: edges found during the implementation ---

def paged_reviews_opener(pages):
    """Reviews page by page: pages is a function page number → list (as Forgejo with ?limit=&page=)."""
    seen = []
    def opener(req, timeout=None):
        url = req.full_url
        seen.append(url)
        assert "/pulls/12/reviews?" in url, url
        query = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))
        return Resp(json.dumps(pages(int(query.get("page", "1")))).encode())
    opener.seen = seen
    return opener


def _noise(start, count):
    return [{**APPROVED, "id": i, "user": {"login": "digipaddock-merge"}, "state": "COMMENT"} for i in range(start, start + count)]


def test_owner_approved_pages_through_reviews():
    """More than one page of reviews: the owner's Approve on page 2 is not lost (Forgejo returns ~30 by default)."""
    op = paged_reviews_opener(lambda p: {1: _noise(1, 50), 2: [{**APPROVED, "id": 51}]}.get(p, []))
    assert forge.owner_approved(forge.Api("t" * 20, opener=op), REPO, 12, HEAD, "main") is True
    assert any("page=2" in u for u in op.seen)
    # the server cut limit down to 30 (MAX_RESPONSE_ITEMS): a short page is not the end yet
    op = paged_reviews_opener(lambda p: {1: _noise(1, 30), 2: _noise(31, 30), 3: [{**APPROVED, "id": 61}]}.get(p, []))
    assert forge.owner_approved(forge.Api("t" * 20, opener=op), REPO, 12, HEAD, "main") is True
    # the server does not understand page and repeats the same page — no endless loop
    op = paged_reviews_opener(lambda p: _noise(1, 50))
    assert forge.owner_approved(forge.Api("t" * 20, opener=op), REPO, 12, HEAD, "main") is False
    assert len(op.seen) == 2
    # endlessly new pages without an Approve — the page limit, not an eternal loop
    op = paged_reviews_opener(lambda p: _noise(p * 100, 50))
    assert forge.owner_approved(forge.Api("t" * 20, opener=op), REPO, 12, HEAD, "main") is False
    assert len(op.seen) == forge.REVIEW_PAGES


@pytest.mark.parametrize("review", [
    {**APPROVED, "stale": True},                       # stale (a new commit without dismiss) — no good
    {**APPROVED, "commit_id": HEAD.upper()[:39]},       # an incomplete sha
    {**APPROVED, "commit_id": None},
    {**APPROVED, "user": "example-owner"},                 # an unexpected shape — neither an approval nor a traceback
    "APPROVED",
])
def test_owner_approved_rejects_odd_reviews(review):
    api = forge.Api("t" * 20, opener=merge_opener([review]))
    assert forge.owner_approved(api, REPO, 12, HEAD, "main") is False


def test_merge_malformed_pr_is_refusal_not_traceback():
    for payload in ({**pr_payload(), "head": "c" * 40}, {**pr_payload(), "head": {"sha": None}}, {**pr_payload(), "user": None}):
        api = forge.Api("t" * 20, opener=make_opener({("GET", f"/repos/{REPO}/pulls/12"): payload}))
        with pytest.raises(forge.ForgeError):
            forge.merge_pr(api, api, R, 12, HEAD)


def test_merge_posts_merged_to_the_card(tmp_path):
    d = write_env(tmp_path)
    kw = paths(tmp_path, d)
    j = tmp_path / "forge.jsonl"
    inner = merge_opener([APPROVED])
    at_post = []
    def opener(req, timeout=None):
        if req.get_method() == "POST":
            at_post.append(j.read_text(encoding="utf-8").splitlines()[-1])
        return inner(req, timeout)
    sent, out = [], io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD], opener=opener, notify=told(sent), out=out,
                    err=io.StringIO(), **kw)
    assert rc == 0 and MERGE in out.getvalue() and "смержен" in out.getvalue()
    assert json.loads(at_post[0])["phase"] == "intent" and json.loads(at_post[0])["sha"] == HEAD
    last = json.loads(j.read_text(encoding="utf-8").splitlines()[-1])
    assert last["phase"] == "result" and last["ok"] is True and last["merge_sha"] == MERGE
    assert sent == [("info", "notice.forge.merged", {"pr": 12, "title": "заголовок <x>"}, "KD-12")]


def test_main_merge_refusal_names_card_and_hides_tokens(tmp_path):
    """Forgejo rejects the POST (for instance without an Approve — the boundary Forgejo itself holds); its text with
    the token reaches neither the agent, nor the journal, nor the thread; the refusal goes to the thread of the card
    named in the PR's title."""
    d = write_env(tmp_path)
    kw = paths(tmp_path, d)
    merge_token = "merge0123456789abcdef"
    inner = merge_opener([APPROVED])
    def opener(req, timeout=None):
        if req.get_method() == "POST":
            inner.seen.append(("POST", req.full_url, req.data, {}))
            raise urllib.error.HTTPError(req.full_url, 405, "x", {},
                                         Resp(json.dumps({"message": f"token {merge_token}: not enough approvals"}).encode()))
        return inner(req, timeout)
    sent, out, err = [], io.StringIO(), io.StringIO()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD], opener=opener, notify=told(sent), out=out, err=err, **kw)
    assert rc == 1 and "405" in err.getvalue() and "not enough approvals" in err.getvalue()
    assert sent and sent[-1][1] == "notice.forge.merge_refused" and sent[-1][3] == "KD-12"
    for text in (out.getvalue(), err.getvalue(), (tmp_path / "forge.jsonl").read_text(encoding="utf-8"),
                 *(n[2]["why"] for n in sent)):
        assert merge_token not in text and "tokABCDEF0123456789" not in text


def test_merge_post_ok_but_result_unreadable_says_check_forgejo():
    opener = merge_opener([APPROVED])
    calls = {"n": 0}
    def flaky(req, timeout=None):
        if req.get_method() == "GET" and req.full_url.endswith("/pulls/12"):
            calls["n"] += 1
            if calls["n"] == 3:  # the GET after the POST (the second one is the repeated check before it)
                raise urllib.error.URLError("timeout")
        return opener(req, timeout)
    api = forge.Api("t" * 20, opener=flaky)
    with pytest.raises(forge.ForgeError, match="merge отправлен.*проверь в Forgejo"):
        forge.merge_pr(api, api, R, 12, HEAD)


def test_selfcheck_api_failure_is_code_1_without_token(tmp_path):
    d = write_env(tmp_path)
    op = make_opener({("GET", f"/repos/{REPO}/branch_protections"): (500, {"message": "boom merge0123456789abcdef"})})
    err = io.StringIO()
    assert forge.main(["--agent", "setup", "selfcheck", "digipaddock"], opener=op, out=io.StringIO(), err=err, **paths(tmp_path, d)) == 1
    assert "selfcheck" in err.getvalue() and "merge0123456789abcdef" not in err.getvalue()
    assert forge.main(["--agent", "setup", "selfcheck", "digipaddock", "12"], opener=op, out=io.StringIO(), err=io.StringIO(),
                      **paths(tmp_path, d)) == 2


def test_request_release_lock_wait_is_bounded_fake_fcntl(tmp_path, monkeypatch):
    """The lock wait logic on any OS: busy — wait 2 s at a time within wait; free — write the request."""
    class FakeFcntl:
        LOCK_EX, LOCK_NB = 2, 4
        busy_until = 6.0
        calls = []
        def flock(self, f, op):
            self.calls.append(op)
            if now["t"] < self.busy_until:
                raise BlockingIOError(11, "busy")
    fake = FakeFcntl()
    monkeypatch.setattr(forge, "fcntl", fake)
    rel = tmp_path / "release"; rel.mkdir()
    now = {"t": 0.0}
    def sleep(s):
        now["t"] += s
    ts = forge.request_release(str(rel), "digipaddock", REPO, MERGE, 12, clock=lambda: now["t"], sleep=sleep)
    assert now["t"] == 6.0 and json.loads((rel / "request.json").read_text(encoding="utf-8"))["ts"] == ts
    assert set(fake.calls) == {fake.LOCK_EX | fake.LOCK_NB}  # no blocking flock
    (rel / "request.json").unlink()
    fake.busy_until = 10 ** 9
    with pytest.raises(forge.ForgeError, match="замок выкатки занят дольше 240"):
        forge.request_release(str(rel), "digipaddock", REPO, MERGE, 12, clock=lambda: now["t"],
                              sleep=lambda s: now.__setitem__("t", now["t"] + 100))
    assert not (rel / "request.json").exists()


@pytest.mark.skipif(os.name == "nt" or forge.fcntl is None, reason="замок flock — только POSIX")
def test_request_release_lock_wait_is_bounded(tmp_path):
    """A second kd-merge --release waits for the lock release/.lock no longer than wait, not forever."""
    rel = tmp_path / "release"; rel.mkdir()
    holder = open(rel / ".lock", "a")
    forge.fcntl.flock(holder, forge.fcntl.LOCK_EX)
    try:
        now = {"t": 0.0}
        with pytest.raises(forge.ForgeError, match="240"):
            forge.request_release(str(rel), "digipaddock", REPO, MERGE, 12, clock=lambda: now["t"],
                                  sleep=lambda s: now.__setitem__("t", now["t"] + 100))
        assert not (rel / "request.json").exists()
    finally:
        holder.close()
    # the lock is free — the request is written
    assert forge.request_release(str(rel), "digipaddock", REPO, MERGE, 12, clock=lambda: 0.0, sleep=lambda s: None)
    assert (rel / "request.json").exists()


def test_branch_problems_rule_name_hidden_from_non_admin():
    # Forgejo 15 hides the rule's name from a non-admin: an empty string is not a problem
    real = {"name": "main", "protected": True, "required_approvals": 1, "enable_status_check": False,
            "status_check_contexts": None, "user_can_push": False, "user_can_merge": True,
            "effective_branch_protection_name": ""}
    assert forge._branch_problems(real) == []
    assert forge._branch_problems({k: v for k, v in real.items() if k != "effective_branch_protection_name"}) == []
    assert forge._branch_problems({**real, "protected": False}) == ["main не защищена правилом main"]
    assert forge._branch_problems({**real, "effective_branch_protection_name": "release"}) == ["main не защищена правилом main"]




OURS = {**APPROVED, "user": {"login": forge.review_login()},
        "body": "Одобрено владельцем в кабинете (одобрение и merge), решение 11111111, в ветку main"}


def test_kd_review_approval_counts():
    """the owner's Approve from the cabinet is kd-review's, on this very head — into the branch its
    body names (the owner signed that target)."""
    api = forge.Api("t" * 20, opener=merge_opener([OURS]))
    assert forge.owner_approved(api, REPO, 12, HEAD, "main")
    assert not forge.owner_approved(api, REPO, 12, HEAD, "dev")
    for other in ({**OURS, "stale": True}, {**OURS, "body": "Одобрено владельцем в кабинете"},
                  {**OURS, "body": OURS["body"] + "x"}):
        api = forge.Api("t" * 20, opener=merge_opener([other]))
        assert not forge.owner_approved(api, REPO, 12, HEAD, "main")
    api = forge.Api("t" * 20, opener=merge_opener([APPROVED]))  # his own Approve in Forgejo — on any target
    assert forge.owner_approved(api, REPO, 12, HEAD, "dev")


def test_an_approval_signed_into_another_target_does_not_merge():
    """Final review: a PR retargeted between two of the passport's branches after the owner signed keeps kd-review's
    Approve in Forgejo — it does not count for the new branch."""
    two = R._replace(release=None, targets=("main", "dev"))
    op = merge_opener([{**OURS, "body": OURS["body"].replace("в ветку main", "в ветку dev")}])
    with pytest.raises(forge.ForgeError, match="не одобрен владельцем"):
        _merge(two, op)
    assert _posted(op) == []
    op = merge_opener([OURS])
    assert _merge(two, op)[2] == "merged"


def test_an_approval_of_the_old_form_says_so():
    """Round-2 review: kd-review's Approve from before the change names no branch — the refusal says what to do."""
    op = merge_opener([{**OURS, "body": "Одобрено владельцем в кабинете (одобрение и merge), решение 11111111"}])
    with pytest.raises(forge.ForgeError, match=r"старого вида.*kd-forge publish"):
        _merge(R, op)
    assert _posted(op) == []


RELEASE_SIGNED = {**OURS, "body": "Одобрено владельцем в кабинете (одобрение, merge и выкатка), решение 11111111, в ветку main"}
APPROVE_ONLY = {**OURS, "body": "Одобрено владельцем в кабинете (только одобрение), решение 11111111, в ветку main"}


def test_the_body_contract_names_the_signed_mode():
    """kd-review's body is the contract kd-merge reads the signed mode from."""
    for mode, text in forge.MODE_TEXT.items():
        assert forge.signed_mode(forge.SIGNED.format(text) + "11111111" + forge.INTO.format("main")) == mode
    assert forge.signed_mode(OURS["body"]) == "merge" and forge.signed_mode(RELEASE_SIGNED["body"]) == "release"
    assert forge.signed_mode("Одобрено владельцем в кабинете") == ""
    assert forge.signed_mode("x" + RELEASE_SIGNED["body"]) == ""


@pytest.mark.parametrize("reviews, release, ok", [
    ([OURS], False, True),                   # merge by a merge signature
    ([RELEASE_SIGNED], False, True),         # merge by a release signature
    ([RELEASE_SIGNED], True, True),          # release by a release signature
    ([OURS], True, False),                   # release by a merge signature — refused
    ([APPROVE_ONLY], False, False),          # «only approve» merges nothing
    ([APPROVED], True, True),                # the owner's own Approve in Forgejo: the admin's word, a release too
    ([OURS, RELEASE_SIGNED], True, True),    # he signed again with the release
], ids=["merge/merge", "merge/release", "release/release", "release/merge", "merge/approve", "release/forgejo",
        "release/both"])
def test_the_signed_mode_decides_the_release(reviews, release, ok):
    api = forge.Api("t" * 20, opener=merge_opener(reviews))
    assert forge.owner_approved(api, REPO, 12, HEAD, "main", release=release) is ok


def test_a_release_by_a_merge_signature_is_refused_and_says_so():
    op = merge_opener([OURS])
    api = forge.Api("t" * 20, opener=op)
    with pytest.raises(forge.ForgeError, match=r"выкатка не подписана.*«одобрение и merge», не «одобрение, merge и "
                                               r"выкатка».*новая подпись владельца в кабинете с выкаткой"):
        forge.merge_pr(api, api, R, 12, HEAD, release=True)
    assert _posted(op) == []
    op = merge_opener([RELEASE_SIGNED])
    api = forge.Api("t" * 20, opener=op)
    assert forge.merge_pr(api, api, R, 12, HEAD, release=True)[2] == "merged"
    op = merge_opener([APPROVED])
    api = forge.Api("t" * 20, opener=op)
    assert forge.merge_pr(api, api, R, 12, HEAD, release=True)[2] == "merged"


@pytest.mark.parametrize("merged_by, reviews, ok", [
    ("example-owner", [], True),                     # the owner merged it himself in Forgejo
    ("digipaddock-merge", [RELEASE_SIGNED], True),   # kd-merge merged it under a release signature (a retry)
    ("digipaddock-merge", [APPROVED], True),         # the owner's own Approve
    ("digipaddock-merge", [OURS], False),            # merged by a merge signature: a second call does not release
    ("digipaddock-merge", [], False),
], ids=["owner", "release-signed", "forgejo-approve", "merge-signed", "unsigned"])
def test_a_merged_pr_releases_only_by_a_release_word(merged_by, reviews, ok):
    pr = pr_payload(state="closed", merged=True, merge_sha=MERGE, merged_by=merged_by)
    api = forge.Api("t" * 20, opener=make_opener({("GET", f"/repos/{REPO}/pulls/12"): pr,
                                                   ("GET", f"/repos/{REPO}/pulls/12/reviews"): reviews}))
    if ok:
        assert forge.merge_pr(api, api, R, 12, HEAD, release=True)[2] == "already"
    else:
        with pytest.raises(forge.ForgeError, match="выкатка не подписана.*PR уже смержен"):
            forge.merge_pr(api, api, R, 12, HEAD, release=True)
    assert forge.merge_pr(api, api, R, 12, HEAD)[2] == "already"  # without the release nothing is asked


def test_cli_release_by_a_merge_signature_tells_the_card_and_merges_nothing(tmp_path):
    d = write_env(tmp_path)
    rel, res = tmp_path / "release", tmp_path / "result"
    rel.mkdir()
    res.mkdir()
    sent, err = [], io.StringIO()
    op = merge_opener([OURS])
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD, "--release"], opener=op, notify=told(sent),
                    out=io.StringIO(), err=err, **{**paths(tmp_path, d), "release_dir": str(rel), "result_dir": str(res)})
    assert rc == 1 and "выкатка не подписана" in err.getvalue() and _posted(op) == []
    assert sent[-1][:2] == ("warn", "notice.forge.merge_refused") and sent[-1][3] == "KD-12"
    assert "одобрение, merge и выкатка" in sent[-1][2]["why"] and not (rel / "request.json").exists()


def test_whitelist_with_kd_review_is_fine():
    for names in (["example-owner"], ["example-owner", "digipaddock-review"], ["digipaddock-review", "example-owner"]):
        assert forge._rule_problems([{**GOOD_RULE, "approvals_whitelist_username": names}]) == []
    for names in (["digipaddock-review"], ["example-owner", "someone"], ["example-owner", "digipaddock-review", "someone"], None):
        assert forge._rule_problems([{**GOOD_RULE, "approvals_whitelist_username": names}])


def test_another_reviewers_approval_does_not_count():
    other = {**APPROVED, "user": {"login": "someone"}}
    opener = merge_opener([other])
    api = forge.Api("t" * 20, opener=opener)
    with pytest.raises(forge.ForgeError, match=r"не одобрен владельцем \(в кабинете или Approve в Forgejo\)"):
        forge.merge_pr(api, api, R, 12, HEAD)


def test_reinstall_line_names_every_operation():
    """The roles' operation and each release step's — the turn waits for all of them."""
    line = forge._reinstall_line({"state": "pending", "operation": "a" * 32, "agents": ["dev"],
                                  "components": [{"component": "repo-attach", "mode": "check", "operation": "b" * 32,
                                                  "state": "pending"}]}, "KD-12")
    assert f"--operation {'a' * 32} --operation {'b' * 32}" in line and "repo-attach check" in line
    line = forge._reinstall_line({"state": "none", "components": [
        {"component": "repo-attach", "mode": "check", "operation": "b" * 32, "state": "pending"}]}, "KD-12")
    assert f"kd-work finish KD-12 --outcome wait_dependency --operation {'b' * 32}" in line and "за владельцем" not in line
    line = forge._reinstall_line({"state": "none", "components": [
        {"component": "repo-attach", "mode": "check", "state": "uncovered", "why": "revision moved"}]}, "KD-12")
    assert "repo-attach check" in line and "revision moved" in line


# --- the passport's targets, required statuses, the current base, trusted authors ----------------------------

UNIT = R._replace(profiles=(("unit", True), ("smoke", False)))
CHECKED_RULE = {**GOOD_RULE, "enable_status_check": True, "status_check_contexts": ["devcheck/unit"]}  # M13
DEV = R._replace(release=None, base="dev", targets=("dev",))
LAX = R._replace(require_up_to_date=False, up_to_date_paths=("common/kd/cabinet/schema.py",))


def _merge(repo, op):
    api = forge.Api("t" * 20, opener=op)
    return forge.merge_pr(api, api, repo, 12, HEAD)


def _posted(op):
    return [c for c in op.seen if c[0] == "POST"]


def test_merge_takes_an_imported_pr():
    assert forge.authors() == {forge.dev_login(), "digipaddock-import"}
    op = merge_opener([APPROVED], pr={"user": {"login": "digipaddock-import"}})
    assert _merge(R, op)[2] == "merged"


def test_merge_refuses_a_target_outside_the_passport():
    op = merge_opener([APPROVED], pr={"base": {"ref": "dev", "sha": MAIN}})
    with pytest.raises(forge.ForgeError, match="PR в ветку dev.*targets проекта: main"):
        _merge(R, op)
    assert not _posted(op)


@pytest.mark.parametrize("pr", [{"draft": True}, {"title": "WIP: KD-12: x"}, {"title": "[wip] KD-12: x"}])
def test_merge_refuses_a_draft(pr):
    op = merge_opener([APPROVED], pr=pr)
    with pytest.raises(forge.ForgeError, match="черновик"):
        _merge(R, op)
    assert not _posted(op)


@pytest.mark.parametrize("statuses, named", [
    ({}, "devcheck/unit (нет)"),
    ({"devcheck/unit": "pending"}, "devcheck/unit (pending)"),
    ({"devcheck/unit": "failure"}, "devcheck/unit (failure)"),
    ({"devcheck/unit": "error"}, "devcheck/unit (error)"),
    ({"ci/other": "success", "devcheck/smoke": "success"}, "devcheck/unit (нет)"),
])
def test_merge_refuses_red_or_missing_required_checks(statuses, named):
    op = merge_opener([APPROVED], statuses=statuses, rule=CHECKED_RULE)
    with pytest.raises(forge.ForgeError, match=r"обязательные проверки не зелёные: " + named.replace("(", r"\(")
                       .replace(")", r"\)")):
        _merge(UNIT, op)
    assert not _posted(op)


def test_merge_with_green_required_checks():
    op = merge_opener([APPROVED], statuses={"devcheck/unit": "success", "devcheck/smoke": "failure"}, rule=CHECKED_RULE)
    assert _merge(UNIT, op)[2] == "merged"  # smoke is not required


def test_a_required_check_is_the_rules_too():
    """M13: a required profile's status is required by Forgejo itself, not only by kd-merge."""
    assert forge._rule_problems([GOOD_RULE]) == []  # nothing required — nothing expected
    off = forge._rule_problems([GOOD_RULE], "main", ["devcheck/unit"])
    assert len(off) == 1 and "enable_status_check" in off[0] and "devcheck/unit" in off[0]
    assert forge._rule_problems([CHECKED_RULE], "main", ["devcheck/unit"]) == []
    other = forge._rule_problems([{**CHECKED_RULE, "status_check_contexts": ["ci/build"]}], "main", ["devcheck/unit"])
    assert len(other) == 1 and "devcheck/unit" in other[0]
    none = forge._rule_problems([{**CHECKED_RULE, "status_check_contexts": None}], "main", ["devcheck/unit"])
    assert len(none) == 1 and "devcheck/unit" in none[0]


def test_a_required_check_seen_from_the_branch():
    real = {"name": "main", "protected": True, "required_approvals": 1, "enable_status_check": False,
            "status_check_contexts": None, "user_can_push": False, "user_can_merge": True,
            "effective_branch_protection_name": ""}
    assert forge._branch_problems(real) == []
    assert any("enable_status_check" in p for p in forge._branch_problems(real, "main", ["devcheck/unit"]))
    on = {**real, "enable_status_check": True, "status_check_contexts": ["devcheck/unit"]}
    assert forge._branch_problems(on, "main", ["devcheck/unit"]) == []


def test_merge_refused_when_the_rule_does_not_require_the_check():
    op = merge_opener([APPROVED], statuses={"devcheck/unit": "success"})
    with pytest.raises(forge.ForgeError, match="защита main не настроена: enable_status_check"):
        _merge(UNIT, op)
    assert not _posted(op)


def test_selfcheck_expects_the_required_checks(tmp_path):
    d = write_env(tmp_path)
    _registry(tmp_path, checks={"contour": "example-ci", "profiles": {"unit": {"required": True}}, "timeout_s": 1800})
    err = io.StringIO()
    assert forge.main(["--agent", "setup", "selfcheck", "digipaddock"], opener=make_opener(protection()), out=io.StringIO(),
                      err=err, **paths(tmp_path, d)) == 1
    assert "enable_status_check" in err.getvalue() and "devcheck/unit" in err.getvalue()
    out = io.StringIO()
    assert forge.main(["--agent", "setup", "selfcheck", "digipaddock"], opener=make_opener(protection(rule=CHECKED_RULE)),
                      out=out, err=io.StringIO(), **paths(tmp_path, d)) == 0


def test_merge_needs_the_current_base():
    op = merge_opener([APPROVED], behind=2)
    with pytest.raises(forge.ForgeError, match="base_moved: целевая ветка main ушла вперёд.*перебазируй"):
        _merge(R, op)
    assert not _posted(op)
    asked = [c[1] for c in op.seen]
    assert any(u.endswith("/branches/main") for u in asked) and any(f"/compare/{HEAD}...{MAIN}" in u for u in asked)


def test_merge_up_to_date_by_path_incl_rename():
    files = [{"filename": "common/kd/cabinet/schema_v2.py", "previous_filename": "common/kd/cabinet/schema.py",
              "status": "renamed", "additions": 0, "deletions": 0}]
    op = merge_opener([APPROVED], behind=1, files=files)
    with pytest.raises(forge.ForgeError, match="base_moved"):
        _merge(LAX, op)
    assert not _posted(op)


MIGRATIONS = R._replace(require_up_to_date=False, up_to_date_paths=("data/db/migrations/*",))


def test_a_behind_pr_merges_unless_it_touches_a_migration():
    """KD-9442: the passport of api wants a current base only for its migrations."""
    op = merge_opener([APPROVED], behind=2, files=[{"filename": "lib/api.py", "status": "modified"}])
    assert _merge(MIGRATIONS, op)[2] == "merged"
    op = merge_opener([APPROVED], behind=2, files=[{"filename": "lib/api.py", "status": "modified"},
                                                   {"filename": "data/db/migrations/041_x.sql", "status": "added"}])
    with pytest.raises(forge.ForgeError, match="base_moved"):
        _merge(MIGRATIONS, op)
    assert not _posted(op)


def test_merge_without_up_to_date_asks_no_compare():
    op = merge_opener([APPROVED], behind=3, files=[{"filename": "a.py", "status": "modified"}])
    assert _merge(LAX, op)[2] == "merged"
    assert not any("/compare/" in c[1] for c in op.seen)


def test_protection_is_checked_on_the_target_branch():
    op = merge_opener([APPROVED], pr={"base": {"ref": "dev", "sha": MAIN}}, rule={**GOOD_RULE, "rule_name": "dev"})
    assert _merge(DEV, op)[2] == "merged"
    assert any(c[1].endswith("/branches/dev") for c in op.seen)
    op = merge_opener([APPROVED], pr={"base": {"ref": "dev", "sha": MAIN}})
    with pytest.raises(forge.ForgeError, match="защита dev не настроена: нет правила защиты dev"):
        _merge(DEV, op)


def test_branch_problems_name_the_target():
    branch = {"protected": True, "effective_branch_protection_name": "main", "user_can_push": True, "user_can_merge": True,
              "required_approvals": 1}
    assert forge._branch_problems(branch, "dev") == ["dev не защищена правилом dev", forge.merge_login() + " может push в dev"]


def test_prs_lists_trusted_authors_only():
    items = [{"number": 1, "user": {"login": "digipaddock-dev"}}, {"number": 2, "user": {"login": "digipaddock-import"}},
             {"number": 3, "user": {"login": "example-owner"}}]
    api = forge.Api("t" * 20, opener=make_opener({("GET", f"/repos/{REPO}/pulls"): items}))
    assert [i["number"] for i in forge.prs(api, REPO)] == [1, 2]


def _registry(tmp_path, **entry):
    (tmp_path / "repos.json").write_text(json.dumps({"digipaddock": {"repo": REPO, "dir": "work/DigiPaddock", **entry}}),
                                         encoding="utf-8")


def test_selfcheck_checks_every_target(tmp_path):
    d = write_env(tmp_path)
    _registry(tmp_path, targets=["main", "dev"])
    err = io.StringIO()
    assert forge.main(["--agent", "setup", "selfcheck", "digipaddock"], opener=make_opener(protection()), out=io.StringIO(),
                      err=err, **paths(tmp_path, d)) == 1
    assert "нет правила защиты dev" in err.getvalue() and "нет правила защиты main" not in err.getvalue()
    both = {**protection(), ("GET", f"/repos/{REPO}/branch_protections"): [GOOD_RULE, {**GOOD_RULE, "rule_name": "dev"}]}
    out = io.StringIO()
    assert forge.main(["--agent", "setup", "selfcheck", "digipaddock"], opener=make_opener(both), out=out, err=io.StringIO(),
                      **paths(tmp_path, d)) == 0
    assert "main, dev" in out.getvalue()


def test_selfcheck_names_the_trusted_authors(tmp_path):
    d = write_env(tmp_path)
    out = io.StringIO()
    assert forge.main(["--agent", "setup", "selfcheck", "digipaddock"], opener=make_opener(protection()), out=out,
                      err=io.StringIO(), **paths(tmp_path, d)) == 0
    assert f"доверенные авторы PR: {forge.dev_login()}, digipaddock-import" in out.getvalue()


def test_a_retarget_while_the_checks_ran_merges_nothing():
    """Review 04.11.2026 (L3): the PR is read again right before the merge — a new target or head merges nothing;
    a retarget that slipped in after that read is reported, not passed off as the checked merge."""
    op = merge_opener([APPROVED], moved={"base": {"ref": "dev", "sha": MAIN}})
    with pytest.raises(forge.ForgeError, match="сдвинулся во время проверок"):
        forge.merge_pr(forge.Api("t" * 20, opener=op), forge.Api("t" * 20, opener=op), R, 12, HEAD)
    assert not any(c[0] == "POST" for c in op.seen)
    op = merge_opener([APPROVED], moved={"head": {"sha": "e" * 40, "ref": "dev/agent/KD-12-x"}})
    with pytest.raises(forge.ForgeError, match="сдвинулся во время проверок"):
        forge.merge_pr(forge.Api("t" * 20, opener=op), forge.Api("t" * 20, opener=op), R, 12, HEAD)
    calls = {"n": 0}
    base = merge_opener([APPROVED])

    def late(req, timeout=None):
        resp = base(req, timeout)
        if req.get_method() == "GET" and req.full_url.endswith("/pulls/12"):
            calls["n"] += 1
            if calls["n"] == 3:  # the result after the POST
                return Resp(json.dumps({**pr_payload(state="closed", merged=True, merge_sha=MERGE),
                                        "base": {"ref": "dev", "sha": MAIN}}).encode())
        return resp

    with pytest.raises(forge.ForgeError, match="смержен в dev, а проверялся для main"):
        forge.merge_pr(forge.Api("t" * 20, opener=late), forge.Api("t" * 20, opener=late), R, 12, HEAD)
