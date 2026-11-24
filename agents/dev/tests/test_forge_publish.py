# agents/dev/tests/test_forge_publish.py
"""kd-forge publish brings dev's PR to the cabinet (files, the diff cut at a line, the cage, the release flag);
kd-merge marks it merged there."""
import io
import json
import urllib.error

from dev_agent import forge
from test_forge_merge import APPROVED, HEAD, merge_opener
from test_forge_read import REPO, Resp, paths, write_env

PR = {"number": 12, "title": "KD-12: заголовок", "state": "open", "merged": False, "mergeable": True,
      "head": {"sha": HEAD, "ref": "x"}, "base": {"ref": "main"}, "user": {"login": "digipaddock-dev"},
      "html_url": "https://forge.example/example/digipaddock/pulls/12"}
FILES = [{"filename": "common/kd/x.py", "status": "modified", "additions": 3, "deletions": 1, "changes": 4},
         {"filename": "docs/a.md", "status": "added", "additions": 10, "deletions": 0, "changes": 10}]
DIFF = "diff --git a/common/kd/x.py b/common/kd/x.py\n+x\n"


def opener(pr=None, files=None, diff=DIFF, statuses=None):
    """Forgejo for publish: the PR (a list — one answer per read, the last one stays), its files page by page (then an
    empty page) and the raw .diff (bytes as they are). the head's combined status (``statuses`` — context →
    state) and a title edit (PATCH, kept in ``patched`` with the token; the PR reads with the new title after it)."""
    seen, patched = [], []
    answers = list(pr) if isinstance(pr, list) else [pr or PR]

    def open_(req, timeout=None):
        url = req.full_url
        seen.append((req.get_method(), url))
        if req.get_method() == "PATCH" and url.endswith("/pulls/12"):
            body = json.loads(req.data)
            patched.append((body, dict(req.header_items())["Authorization"]))
            answers[:] = [{**a, **body} for a in answers]
            return Resp(json.dumps({**answers[-1]}).encode())
        if f"/commits/{HEAD}/status" in url:
            found = [{"context": c, "status": s, "state": None} for c, s in (statuses or {}).items()]  # Forgejo: status, not state
            return Resp(json.dumps({"sha": HEAD, "state": "", "statuses": found, "total_count": len(found)}).encode())
        if "/pulls/12/files" in url:
            body = (files if files is not None else FILES) if "page=1" in url else []
            return Resp(json.dumps(body).encode())
        if url.endswith("/pulls/12.diff"):
            return Resp(diff if isinstance(diff, bytes) else diff.encode("utf-8"))
        if url.endswith("/pulls/12"):
            return Resp(json.dumps(answers.pop(0) if len(answers) > 1 else answers[0]).encode())
        raise urllib.error.HTTPError(url, 404, "x", {}, Resp(b"{}"))
    open_.seen, open_.patched = seen, patched
    return open_


class Cabinet:
    def __init__(self, error=None, council=None, stage=False):
        self.seen, self.error, self.council, self.stage = [], error, council, stage

    def call(self, request):
        self.seen.append(request)
        if self.error:
            raise self.error
        if self.stage:  # a chain plan with open stages: the head is recorded, nothing opens
            return {"pr": {}, "decision": None, "council": None, "stage": True}
        held = self.council is not None and self.council["state"] in ("running", "needs_writer")
        return {"pr": {}, "decision": None if held else {"id": "1" * 32}, "council": self.council}


def run(tmp_path, op, cabinet=None, number="12"):
    folder = tmp_path / "forge"
    kw = paths(tmp_path, folder if folder.exists() else write_env(tmp_path))
    out, err = io.StringIO(), io.StringIO()
    rc = forge.main(["--agent", "dev", "publish", "digipaddock", number], opener=op, cabinet=cabinet, out=out, err=err, **kw)
    return rc, out.getvalue(), err.getvalue()


def test_publish_reads_files_and_diff_and_posts(tmp_path):
    cabinet = Cabinet()
    rc, out, err = run(tmp_path, opener(), cabinet)
    assert rc == 0, err
    assert cabinet.seen == [{"op": "pr.publish", "pr": {
        "repo": REPO, "project": "digipaddock", "number": 12, "card": "KD-12", "title": "KD-12: заголовок",
        "url": PR["html_url"], "head": HEAD, "state": "open",
        "files": [{"path": "common/kd/x.py", "status": "modified", "additions": 3, "deletions": 1},
                  {"path": "docs/a.md", "status": "added", "additions": 10, "deletions": 0}],
        "cage": ["common/kd/x.py"], "diff": DIFF, "truncated": False, "release": True,
        "target": "main", "effect": "", "checks": []}}]
    assert "PR #12 в кабинете: " in out and "KD-12" in out
    merged = Cabinet()
    rc, out, _ = run(tmp_path, opener(pr={**PR, "state": "closed", "merged": True}), merged)
    assert rc == 0 and merged.seen[0]["pr"]["state"] == "merged" and "PR #12: merged" in out


def _council(state, round_no=2):
    return {"id": "c" * 32, "round": round_no, "rounds": 3, "state": state}


def test_publish_prints_the_council_and_the_finish_line(tmp_path):
    # the head is read by the card's council of the code — the turn ends waiting for it
    rc, out, err = run(tmp_path, opener(), Cabinet(council=_council("running")))
    assert rc == 0, err
    assert out.splitlines()[1:] == [
        "совет по коду: круг 2 из 3 — читают критики",
        f'kd-work finish KD-12 --outcome wait_dependency --council {"c" * 32} --note "PR #12: ждёт совета"',
    ]
    assert out.startswith("PR #12 в кабинете: ")


def test_publish_names_a_council_that_waits_for_its_writer(tmp_path):
    rc, out, err = run(tmp_path, opener(), Cabinet(council=_council("needs_writer", 1)))
    assert rc == 0, err
    assert out.splitlines()[1:] == ["совет по коду: круг 1 из 3 — ждёт твоих ответов: kd-work council KD-12"]


def test_publish_without_a_council_prints_as_before(tmp_path):
    for council in (None, _council("clean"), _council("failed")):  # none, or one that is over: the decision is open
        rc, out, err = run(tmp_path, opener(), Cabinet(council=council))
        assert rc == 0, err
        assert len(out.splitlines()) == 1 and out.startswith("PR #12 в кабинете: ") and "совет" not in out
    rc, out, _ = run(tmp_path, opener(pr={**PR, "state": "closed", "merged": True}), Cabinet())
    assert out == "PR #12: merged\n"


def test_publish_of_a_stage_says_the_decision_waits_for_the_last_stage(tmp_path):
    rc, out, err = run(tmp_path, opener(), Cabinet(stage=True))
    assert rc == 0, err
    assert out.splitlines()[1:] == [
        f"этап: PR плана обновлён (голова {HEAD[:12]}); совет и решение владельца — после последнего этапа",
    ]
    assert out.startswith("PR #12 в кабинете: ")


def test_a_title_with_an_earlier_card_prefix_is_the_card(tmp_path):
    # a PR opened before the host renamed its cards (OLD-12, the fixture's card_aliases) reaches KD-12
    cabinet = Cabinet()
    rc, _, err = run(tmp_path, opener(pr={**PR, "title": "OLD-12: заголовок"}), cabinet)
    assert rc == 0, err
    assert cabinet.seen[0]["pr"]["card"] == "KD-12" and cabinet.seen[0]["pr"]["title"] == "OLD-12: заголовок"


def test_publish_cuts_a_big_diff_at_a_line(tmp_path):
    line = "+" + "ж" * 60 + "\n"  # two bytes a letter: the cut never splits one
    big = "diff --git a/x b/x\n" + line * (forge.DIFF_MAX // len(line.encode("utf-8")) + 50)
    cabinet = Cabinet()
    rc, _, err = run(tmp_path, opener(diff=big), cabinet)
    assert rc == 0, err
    sent = cabinet.seen[0]["pr"]
    assert sent["truncated"] is True and sent["diff"].endswith("\n") and big.startswith(sent["diff"])
    assert len(sent["diff"].encode("utf-8")) <= forge.DIFF_MAX
    assert len(sent["diff"].encode("utf-8")) > forge.DIFF_MAX - len(line.encode("utf-8"))


def test_publish_refuses_a_head_that_moved_while_the_diff_was_read(tmp_path):
    # final review: the files and the diff must be of the head the decision names
    cabinet = Cabinet()
    rc, _, err = run(tmp_path, opener(pr=[PR, {**PR, "head": {"sha": "f" * 40, "ref": "x"}}]), cabinet)
    assert rc == 1 and "publish" in err and cabinet.seen == []


def test_publish_fits_the_cabinets_socket_line(tmp_path):
    # final review: JSON escapes grow a diff of control bytes past the socket's 1 MiB line
    from kd.cabinet import client as cabinet_client, server
    big = "diff --git a/x b/x\n" + "+\x01\x01\x01\n" * (forge.DIFF_MAX // 5 - 10)
    assert len(big.encode("utf-8")) <= forge.DIFF_MAX
    cabinet = Cabinet()
    rc, _, err = run(tmp_path, opener(diff=big), cabinet)
    assert rc == 0, err
    request = cabinet.seen[0]
    assert len(cabinet_client.encode(request)) <= server.MAX_REQUEST
    sent = request["pr"]
    assert sent["truncated"] is True and big.startswith(sent["diff"]) and sent["diff"].endswith("\n")
    assert len(cabinet_client.encode(request)) > server.MAX_REQUEST - 64 * 1024  # cut to fit, not emptied
    files = [{"filename": f"agents/dev/{'x' * 480}{i}.py", "status": "modified", "additions": 1, "deletions": 0}
             for i in range(1000)]  # a list of files that does not fit even without the diff
    rc, _, err = run(tmp_path, opener(files=files, diff=big), Cabinet())
    assert rc == 1 and "кабинет" in err


def test_publish_marks_bytes_that_are_not_utf8(tmp_path):
    cabinet = Cabinet()
    rc, _, err = run(tmp_path, opener(diff=b"diff --git a/x b/x\n+caf\xe9\n"), cabinet)
    assert rc == 0, err
    assert cabinet.seen[0]["pr"]["diff"] == "diff --git a/x b/x\n+caf�\n"


def test_publish_refuses_foreign_or_cardless_prs(tmp_path):
    for pr, needle in (({**PR, "user": {"login": "example-owner"}}, f"не от {forge.dev_login()}"), ({**PR, "title": "без карточки"}, "KD-")):
        cabinet = Cabinet()
        rc, _, err = run(tmp_path, opener(pr=pr), cabinet)
        assert rc == 1 and needle in err and cabinet.seen == []
    from kd.cabinet import client as cabinet_client
    rc, _, err = run(tmp_path, opener(), Cabinet(cabinet_client.CabinetError("refused", "KD-12 has no agent")))
    assert rc == 1 and "KD-12 has no agent" in err


def test_merge_marks_the_pr_merged(tmp_path):
    d = write_env(tmp_path)
    marked = []
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD], opener=merge_opener([APPROVED]),
                    notify=lambda *a: True, mark=lambda repo, n: marked.append((repo, n)), out=io.StringIO(),
                    err=io.StringIO(), **paths(tmp_path, d))
    assert rc == 0 and marked == [(REPO, 12)]
    marked.clear()
    rc = forge.main(["--agent", "dev", "merge", "digipaddock", "12", HEAD], opener=merge_opener([]),
                    notify=lambda *a: True, mark=lambda repo, n: marked.append((repo, n)), out=io.StringIO(),
                    err=io.StringIO(), **paths(tmp_path, d))
    assert rc == 1 and marked == []


def _registry(tmp_path, **entry):
    write_env(tmp_path)
    (tmp_path / "repos.json").write_text(json.dumps({"digipaddock": {"repo": REPO, "dir": "work/DigiPaddock", **entry}}),
                                         encoding="utf-8")


def test_publish_refuses_a_pr_into_a_branch_outside_the_passport(tmp_path):
    cabinet = Cabinet()
    rc, _, err = run(tmp_path, opener(pr={**PR, "base": {"ref": "dev"}}), cabinet)
    assert rc == 1 and "PR в ветку dev" in err and cabinet.seen == []


def test_publish_takes_an_imported_pr(tmp_path):
    cabinet = Cabinet()
    rc, _, err = run(tmp_path, opener(pr={**PR, "user": {"login": "digipaddock-import"}}), cabinet)
    assert rc == 0, err
    assert cabinet.seen[0]["pr"]["card"] == "KD-12"


def test_publish_carries_the_target_and_its_effect(tmp_path):
    _registry(tmp_path, release="digipaddock", merge_effects={"main": "kd-release на хост фабрики"})
    cabinet = Cabinet()
    rc, _, err = run(tmp_path, opener(), cabinet)
    assert rc == 0, err
    sent = cabinet.seen[0]["pr"]
    assert (sent["target"], sent["effect"], sent["checks"]) == ("main", "kd-release на хост фабрики", [])


def test_publish_refuses_without_green_required_checks(tmp_path):
    _registry(tmp_path, checks={"profiles": {"unit": {"required": True}, "static": {"required": True}}})
    cabinet, op = Cabinet(), opener(pr={**PR, "title": "WIP: KD-12: x"},
                                    statuses={"devcheck/unit": "pending", "ci/x": "success"})
    rc, _, err = run(tmp_path, op, cabinet)
    assert rc == 1 and "devcheck/unit (pending)" in err and "devcheck/static (нет)" in err
    assert cabinet.seen == [] and op.patched == []


def test_publish_takes_the_draft_off_when_checks_are_green(tmp_path):
    _registry(tmp_path, checks={"profiles": {"unit": {"required": True}, "smoke": {}}})
    cabinet, op = Cabinet(), opener(pr={**PR, "title": "WIP: KD-12: x"}, statuses={"devcheck/unit": "success"})
    rc, _, err = run(tmp_path, op, cabinet)
    assert rc == 0, err
    assert op.patched == [({"title": "KD-12: x"}, "token merge0123456789abcdef")]  # the title by kd-merge's token
    sent = cabinet.seen[0]["pr"]
    assert sent["title"] == "KD-12: x" and sent["checks"] == ["unit"]


def test_a_refused_publish_keeps_the_draft(tmp_path):
    """Final review: WIP: comes off only once the cabinet took the PR — a head that moved or a refusal keeps it."""
    from kd.cabinet import client as cabinet_client
    _registry(tmp_path, checks={"profiles": {"unit": {"required": True}}})
    draft = {**PR, "title": "WIP: KD-12: x"}
    moved = opener(pr=[draft, {**draft, "head": {"sha": "f" * 40, "ref": "x"}}], statuses={"devcheck/unit": "success"})
    rc, _, err = run(tmp_path, moved, Cabinet())
    assert rc == 1 and "publish" in err and moved.patched == []
    refused = opener(pr=draft, statuses={"devcheck/unit": "success"})
    rc, _, err = run(tmp_path, refused, Cabinet(error=cabinet_client.CabinetError("refused", "KD-12 has no agent")))
    assert rc == 1 and "кабинет не принял" in err and refused.patched == []


def test_publish_takes_the_draft_off_with_its_whole_title(tmp_path):
    long = "KD-12: " + "д" * (forge.TITLE_MAX + 20)
    cabinet, op = Cabinet(), opener(pr={**PR, "title": "WIP: " + long})
    rc, _, err = run(tmp_path, op, cabinet)
    assert rc == 0, err
    assert op.patched == [({"title": long}, "token merge0123456789abcdef")]
    assert cabinet.seen[0]["pr"]["title"] == long[:forge.TITLE_MAX]


def test_publish_without_required_checks_reads_no_status(tmp_path):
    cabinet, op = Cabinet(), opener(pr={**PR, "title": "[WIP] KD-12: заголовок"})
    rc, _, err = run(tmp_path, op, cabinet)
    assert rc == 0, err
    assert not any("/status" in url for _, url in op.seen)
    assert op.patched == [({"title": "KD-12: заголовок"}, "token merge0123456789abcdef")]
    assert cabinet.seen[0]["pr"]["title"] == "KD-12: заголовок"


def test_publish_of_a_merged_pr_edits_no_title(tmp_path):
    cabinet, op = Cabinet(), opener(pr={**PR, "title": "WIP: KD-12: x", "state": "closed", "merged": True})
    rc, _, err = run(tmp_path, op, cabinet)
    assert rc == 0, err
    assert op.patched == [] and cabinet.seen[0]["pr"]["title"] == "KD-12: x"


def test_the_limits_are_the_cabinets():
    from kd.cabinet import prs_api
    assert (forge.DIFF_MAX, forge.FILES_MAX, forge.TITLE_MAX, forge.URL_MAX, forge.PATH_MAX) == (
        prs_api.DIFF_MAX, prs_api.FILES_MAX, prs_api.TITLE_MAX, prs_api.URL_MAX, prs_api.PATH_MAX)


def test_publish_says_whether_the_signature_carried():
    """KD-9442: a carried merge signature lets the merge go on; approve-only and a refusal do not."""
    row = {"number": 12, "head": HEAD}
    lines = forge.carry_lines("digipaddock", row, {"carried": {"from": "e" * 40, "mode": "release", "target": "main"}})
    assert "перенесена с eeeeeee на ccccccc" in lines[0] and "от main" in lines[0]
    assert lines[1].endswith(f"digipaddock 12 {HEAD} --release")
    lines = forge.carry_lines("digipaddock", row, {"carried": {"from": "e" * 40, "mode": "approve", "target": "main"}})
    assert lines[1] == "merge — только по слову владельца"
    lines = forge.carry_lines("digipaddock", row, {"uncarried": "the PR's own diff changed"})
    assert lines == ["подпись не перенесена (the PR's own diff changed) — на ccccccc нужна новая подпись владельца"]
    assert forge.carry_lines("digipaddock", row, {"decision": {}}) == []


def test_patch_id_ignores_where_the_change_sits_but_not_what_it_is():
    diff = "diff --git a/x b/x\nindex 1..2 100644\n--- a/x\n+++ b/x\n@@ -1,2 +1,3 @@ f\n a\n+b\n c\n"
    moved = diff.replace("index 1..2", "index 7..8").replace("@@ -1,2 +1,3 @@ f", "@@ -40,2 +41,3 @@ g")
    assert forge.patch_id(diff) == forge.patch_id(moved)
    assert forge.patch_id(diff) != forge.patch_id(moved.replace("+b", "+B"))
    assert forge.patch_id(diff) != forge.patch_id(moved.replace(" c\n", " C\n"))  # context counts too
