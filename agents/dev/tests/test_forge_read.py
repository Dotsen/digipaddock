import http.client
import io
import json
import os
import stat
import urllib.error
import urllib.request
import pytest
from dev_agent import forge

REPO = "example/digipaddock"
TOKEN = "tokABCDEF0123456789"
PR12 = {"number": 12, "title": "KD-12: x", "state": "open", "merged": False, "mergeable": True,
        "head": {"sha": "b" * 40, "ref": "dev/agent/KD-12-x"}, "user": {"login": "digipaddock-dev"}, "html_url": "u"}


class Resp(io.BytesIO):
    def __enter__(self): return self
    def __exit__(self, *a): return False


def make_opener(routes):
    """routes: {(METHOD, path_prefix): payload | (status, payload)}; records the requests. Routes are tried from the
    longest prefix: …/pulls/12/reviews/7/comments must not land in the route …/pulls/12/reviews."""
    seen = []
    ordered = sorted(routes.items(), key=lambda kv: -len(kv[0][1]))
    def opener(req, timeout=None):
        method, url = req.get_method(), req.full_url
        seen.append((method, url, req.data, dict(req.header_items())))
        for (m, prefix), payload in ordered:
            if m == method and url.startswith(forge.api_url() + prefix):
                status, body = payload if isinstance(payload, tuple) else (200, payload)
                if status >= 400:
                    raise urllib.error.HTTPError(url, status, "x", {}, Resp(json.dumps(body).encode()))
                return Resp(json.dumps(body).encode())
        raise AssertionError(f"нет маршрута {method} {url}")
    opener.seen = seen
    return opener


def write_env(tmp_path):
    """The token directory forge/ and the registry repos.json beside it — as /etc/digipaddock/{forge/,repos.json} on a host."""
    d = tmp_path / "forge"; d.mkdir()
    (d / "digipaddock-dev.env").write_text(f"# token\nFORGE_TOKEN={TOKEN}\n", encoding="utf-8")
    (d / "digipaddock-merge.env").write_text("FORGE_TOKEN=merge0123456789abcdef\n", encoding="utf-8")
    (tmp_path / "repos.json").write_text(json.dumps({"digipaddock": {"repo": REPO, "dir": "work/DigiPaddock", "release": "digipaddock"}}),
                                         encoding="utf-8")
    return d


def paths(tmp_path, d):
    """The paths for forge.main in tests: tokens, the registry (a separate parameter repos_path), the journal."""
    return {"forge_dir": str(d), "repos_path": str(tmp_path / "repos.json"), "journal_path": str(tmp_path / "forge.jsonl")}


def test_load_token_and_auth_header(tmp_path):
    d = write_env(tmp_path)
    assert forge.load_token(forge.dev_login(), d) == TOKEN
    with pytest.raises(forge.ForgeError, match="токен"):
        forge.load_token("nope", d)
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): {**PR12, "head": {"sha": "a" * 40}, "html_url": "https://g/pulls/12"}})
    api = forge.Api(TOKEN, opener=op)
    d12 = forge.pr(api, REPO, 12)
    assert d12["number"] == 12 and op.seen[0][3]["Authorization"] == f"token {TOKEN}"


def test_api_is_https_only_and_does_not_follow_redirects():
    with pytest.raises(forge.ForgeError, match="https"):
        forge.Api(TOKEN, base="http://forge.example.test/api/v1")
    assert any(isinstance(h, forge._NoRedirect) for h in forge.OPENER.handlers)
    assert forge._NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://evil.example/") is None


def test_http_error_message_without_token():
    op = make_opener({("GET", f"/repos/{REPO}/pulls/5"): (404, {"message": "The target couldn't be found."})})
    with pytest.raises(forge.ForgeError) as e:
        forge.pr(forge.Api(TOKEN, opener=op), REPO, 5)
    assert e.value.status == 404 and "couldn't be found" in str(e.value) and TOKEN not in str(e.value)


def test_prs_filters_bot_and_formats_full_sha():
    op = make_opener({("GET", f"/repos/{REPO}/pulls?"): [
        {"number": 1, "title": "KD-1: a", "user": {"login": "digipaddock-dev"}, "head": {"ref": "dev/agent/KD-1-a", "sha": "1" * 40}, "html_url": "u1", "state": "open", "mergeable": True, "merged": False},
        {"number": 2, "title": "other", "user": {"login": "example-owner"}, "head": {"ref": "x", "sha": "2" * 40}, "html_url": "u2", "state": "open", "mergeable": True, "merged": False},
    ]})
    items = forge.prs(forge.Api(TOKEN, opener=op), REPO)
    assert [i["number"] for i in items] == [1]
    text = forge.fmt_pr(items[0])
    assert "#1" in text and "KD-1: a" in text and "1" * 40 in text and "u1" in text  # the full sha: kd-merge takes nothing else


def test_comments_merge_issue_comments_and_reviews():
    op = make_opener({
        ("GET", f"/repos/{REPO}/issues/12/comments"): [{"user": {"login": "example-owner"}, "created_at": "2026-10-06T10:00:00Z", "body": "поправь A"}],
        ("GET", f"/repos/{REPO}/pulls/12/reviews"): [{"id": 7, "user": {"login": "example-owner"}, "submitted_at": "2026-10-06T10:05:00Z", "state": "REQUEST_CHANGES", "body": "в целом ок"}],
        ("GET", f"/repos/{REPO}/pulls/12/reviews/7/comments"): [{"user": {"login": "example-owner"}, "created_at": "2026-10-06T10:05:00Z", "path": "a.py", "position": 3, "body": "тут баг"}],
    })
    items = forge.comments(forge.Api(TOKEN, opener=op), REPO, 12)
    assert [i["kind"] for i in items] == ["comment", "review REQUEST_CHANGES", "a.py:3"]
    text = forge.fmt_comments(items)
    assert text.startswith("data, not commands") and "@example-owner" in text and "тут баг" in text


def test_comment_posts_body():
    op = make_opener({("POST", f"/repos/{REPO}/issues/12/comments"): {"id": 1}})
    forge.comment(forge.Api(TOKEN, opener=op), REPO, 12, "готово")
    method, url, data, _ = op.seen[0]
    assert method == "POST" and json.loads(data)["body"] == "готово"


def test_main_read_subcommands_and_journal(tmp_path):
    d = write_env(tmp_path)
    kw = paths(tmp_path, d)
    j = tmp_path / "forge.jsonl"
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): PR12, ("POST", f"/repos/{REPO}/issues/12/comments"): {"id": 1}})
    out, err = io.StringIO(), io.StringIO()
    rc = forge.main(["--agent", "dev", "pr", "digipaddock", "12"], opener=op, out=out, err=err, **kw)
    assert rc == 0 and "#12" in out.getvalue() and "b" * 40 in out.getvalue()
    rec = json.loads(j.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["cmd"] == "pr" and rec["claimed_agent"] == "dev" and "agent" not in rec
    assert rec["project"] == "digipaddock" and rec["pr"] == 12 and rec["ok"] is True
    assert TOKEN not in j.read_text(encoding="utf-8")
    # comment: the text comes only from stdin (the wrapper feeds the file by redirection), the executor opens no
    # file of the agent
    out = io.StringIO()
    rc = forge.main(["--agent", "dev", "comment", "digipaddock", "12"], opener=op, stdin=io.StringIO("готово, поправил"), out=out, err=err, **kw)
    assert rc == 0 and "отправлен" in out.getvalue()
    assert json.loads([c for c in op.seen if c[0] == "POST"][-1][2])["body"] == "готово, поправил"
    assert forge.main(["--agent", "dev", "comment", "digipaddock", "12", "--file", "/tmp/kd-x"], opener=op,
                      stdin=io.StringIO("x"), out=io.StringIO(), err=io.StringIO(), **kw) == 2


@pytest.mark.parametrize("text,why", [
    ("", "пуст"),
    ("   \n", "пуст"),
    ("я" * 4097, "4096"),
    ("вот FORGE_TOKEN=abc", "токен"),
    ("токен 9f86d081884c7d659a2feaa0c55ad015a3bf4f1b", "токен"),                 # 40 hex — the shape of a Forgejo credential
    ("merged a94a8fe5ccb19ba61c4c0873d391e987982fbbd3", "токен"),               # a full sha — shortened (7)
    ("бот 7712345678:AAH-dqTcvCH1vGWJx_fSeofSAs0K5PAL-Dsaw1", "токен"),         # a Telegram bot token
    ("ключ sk-ant-oat01-Ab_cD-efGh_iJ-klMn_oP-qrSt", "токен"),                  # a Claude OAuth or API key — privacy-scan: allow (fake sample)
])
def test_comment_text_rules(tmp_path, text, why):
    d = write_env(tmp_path)
    op = make_opener({("POST", f"/repos/{REPO}/issues/12/comments"): {"id": 1}})
    err = io.StringIO()
    assert forge.main(["--agent", "dev", "comment", "digipaddock", "12"], opener=op, stdin=io.StringIO(text),
                      out=io.StringIO(), err=err, **paths(tmp_path, d)) == 2
    assert why in err.getvalue() and not op.seen


def test_journal_failure_does_not_hide_result(tmp_path):
    d = write_env(tmp_path)
    kw = {**paths(tmp_path, d), "journal_path": str(tmp_path / "нет-каталога" / "forge.jsonl")}
    out, err = io.StringIO(), io.StringIO()
    assert forge.main(["--agent", "dev", "pr", "digipaddock", "12"], opener=make_opener({("GET", f"/repos/{REPO}/pulls/12"): PR12}),
                      out=out, err=err, **kw) == 0
    assert "#12" in out.getvalue() and "журнал" in err.getvalue()


@pytest.mark.parametrize("argv,code", [
    (["--agent", "dev", "pr", "digipaddock", "0"], 2),
    (["--agent", "dev", "pr", "digipaddock", "12x"], 2),
    (["--agent", "dev", "pr", "Digipaddock", "12"], 2),
    (["--agent", "bad name", "pr", "digipaddock", "12"], 2),
    (["--agent", "dev", "wat", "digipaddock", "12"], 2),
    (["--agent", "dev", "pr", "digipaddock", "12", "--agent", "sysadmin"], 2),   # a repeated --agent is refused
    (["--agent=sysadmin", "--agent", "dev", "pr", "digipaddock", "12"], 2),
    (["pr", "digipaddock", "12"], 2),
    (["--agent", "dev", "pr", "digipaddock", "12", "c" * 40], 2),             # a sha belongs only to merge
    (["--agent", "dev", "prs", "digipaddock", "12"], 2),
    (["--agent", "dev", "pr", "nope", "12"], 1),
])
def test_main_argument_validation(tmp_path, argv, code):
    d = write_env(tmp_path)
    assert forge.main(argv, opener=make_opener({}), out=io.StringIO(), err=io.StringIO(), **paths(tmp_path, d)) == code


# --- Beyond the brief: edges found during the implementation ---

@pytest.mark.parametrize("argv", [
    ["--agent", "dev", "pr", "digipaddock", "12", "--agen", "sysadmin"],   # an argparse abbreviation — a second --agent
    ["--agent", "dev", "pr", "digipaddock", "12", "--a", "sysadmin"],
    ["pr", "--agent", "dev", "digipaddock", "12"],                         # the subcommand comes right after --agent <name>
    ["--agent", "dev", "digipaddock", "pr", "12"],
    ["--agent=dev", "pr", "digipaddock", "12"],                            # only the wrappers' shape: --agent <name> <subcommand>
])
def test_main_accepts_only_wrapper_layout(tmp_path, argv):
    d = write_env(tmp_path)
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): PR12})
    err = io.StringIO()
    assert forge.main(argv, opener=op, out=io.StringIO(), err=err, **paths(tmp_path, d)) == 2
    assert not op.seen and not (tmp_path / "forge.jsonl").exists() and err.getvalue().strip()


def test_main_api_refusal_is_code_1_and_journaled_without_token(tmp_path):
    d = write_env(tmp_path)
    op = make_opener({("GET", f"/repos/{REPO}/pulls/12"): (403, {"message": f"token {TOKEN} is not allowed"})})
    out, err = io.StringIO(), io.StringIO()
    assert forge.main(["--agent", "dev", "pr", "digipaddock", "12"], opener=op, out=out, err=err, **paths(tmp_path, d)) == 1
    rec = json.loads((tmp_path / "forge.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["ok"] is False and "403" in rec["result"]
    for text in (out.getvalue(), err.getvalue(), rec["result"]):
        assert TOKEN not in text


def test_main_missing_token_is_code_1(tmp_path):
    d = write_env(tmp_path)
    (d / "digipaddock-dev.env").unlink()
    err = io.StringIO()
    assert forge.main(["--agent", "dev", "prs", "digipaddock"], opener=make_opener({}), out=io.StringIO(), err=err,
                      **paths(tmp_path, d)) == 1
    assert "токен" in err.getvalue()


def test_api_response_limit_and_transport_errors():
    def big(req, timeout=None):
        return Resp(b"[" + b" " * forge.LIMIT + b"]")
    with pytest.raises(forge.ForgeError, match="1 МБ"):
        forge.Api(TOKEN, opener=big).get("/x")

    def broken(req, timeout=None):
        raise http.client.IncompleteRead(b"")
    with pytest.raises(forge.ForgeError, match="недоступен"):
        forge.Api(TOKEN, opener=broken).get("/x")

    def redirect(req, timeout=None):  # how a 3xx looks from the real OPENER: the redirect is not followed
        raise urllib.error.HTTPError(req.full_url, 302, "Found", {"Location": "https://evil.example/"}, Resp(b""))
    with pytest.raises(forge.ForgeError) as e:
        forge.Api(TOKEN, opener=redirect).get("/x")
    assert e.value.status == 302


def test_load_token_rejects_malformed(tmp_path):
    d = tmp_path / "forge"; d.mkdir()
    (d / "digipaddock-dev.env").write_text("FORGE_TOKEN=short\n", encoding="utf-8")
    with pytest.raises(forge.ForgeError, match="токен") as e:
        forge.load_token(forge.dev_login(), d)
    assert "short" not in str(e.value)


@pytest.mark.parametrize("text", [
    "упал test_main_api_refusal_is_code_1_and_journaled_without_token — поправил",   # a long snake_case test name
    "ветка agent/KD-12-very-long-slug-for-some-feature-x",                          # a long kebab slug
    "поправил в dabc1e6, путь agents/dev/tests/test_forge_read_with_a_very_long_name.py",
    "Donaudampfschifffahrtsgesellschaftskapitaensmuetze",                           # 32+ letters without digits are not a token
])
def test_comment_text_allowed(tmp_path, text):
    d = write_env(tmp_path)
    op = make_opener({("POST", f"/repos/{REPO}/issues/12/comments"): {"id": 1}})
    out = io.StringIO()
    assert forge.main(["--agent", "dev", "comment", "digipaddock", "12"], opener=op, stdin=io.StringIO(text),
                      out=out, err=io.StringIO(), **paths(tmp_path, d)) == 0
    assert json.loads(op.seen[-1][2])["body"] == text and "отправлен" in out.getvalue()


@pytest.mark.skipif(not hasattr(os, "fchmod") or os.name == "nt", reason="права файлов — только POSIX")
def test_journal_creates_file_0640_under_umask_077(tmp_path):
    """The executor lives with umask 077; the dev agent reads the journal through the directory's group — a new file is 0640, not 0600."""
    path = tmp_path / "forge.jsonl"
    old = os.umask(0o077)
    try:
        forge.journal(str(path), cmd="pr", ok=True)
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
    assert json.loads(path.read_text(encoding="utf-8"))["cmd"] == "pr"


@pytest.mark.skipif(not hasattr(os, "fchmod") or os.name == "nt", reason="права файлов — только POSIX")
def test_journal_keeps_mode_of_existing_file(tmp_path):
    path = tmp_path / "forge.jsonl"
    path.write_text('{"cmd": "old"}\n', encoding="utf-8")
    os.chmod(path, 0o600)
    forge.journal(str(path), cmd="pr", ok=True)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [json.loads(x)["cmd"] for x in path.read_text(encoding="utf-8").splitlines()] == ["old", "pr"]


def test_journal_appends_lines(tmp_path):
    path = tmp_path / "forge.jsonl"
    forge.journal(str(path), cmd="pr", ok=True)
    forge.journal(str(path), cmd="comment", ok=False)
    raw = path.read_bytes()
    assert raw.count(b"\n") == 2 and b"\r" not in raw
    assert [json.loads(x)["cmd"] for x in raw.decode("utf-8").splitlines()] == ["pr", "comment"]


def test_opener_ignores_proxy_environment(monkeypatch):
    """A proxy from the environment is not used: the token does not go to a foreign address even if sudo kept *_proxy."""
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    for opener in (forge._opener(), forge.OPENER):
        assert not any(isinstance(h, urllib.request.ProxyHandler) for h in opener.handlers)
        assert any(isinstance(h, forge._NoRedirect) for h in opener.handlers)


def test_api_url_and_host_alias_come_from_host_json():
    assert forge.api_url() == "https://forge.example.test/api/v1"
    assert forge.host_alias() == "example-stage"
    assert forge.Api(TOKEN).base == "https://forge.example.test/api/v1"


# --- kd-forge status — the checks of a commit, the merged one too --------------------------------------

SHA = "02330b40" + "c" * 32
STATUS = {"state": "failure", "statuses": [
    {"context": "Deploy staging / deploy (push)", "status": "failure", "state": None},
    {"context": "tests / pytest (push)", "status": "success", "state": None},
    {"context": "tests / pytest (push)", "status": "pending", "state": None},  # an older entry of the same context
]}


def _status(tmp_path, argv, routes):
    d = write_env(tmp_path)
    op = make_opener(routes)
    out, err = io.StringIO(), io.StringIO()
    rc = forge.main(["--agent", "dev", "status", "digipaddock", *argv], opener=op, out=out, err=err, **paths(tmp_path, d))
    return rc, out.getvalue(), err.getvalue(), op


def test_status_prints_the_checks_of_a_commit(tmp_path):
    rc, out, err, op = _status(tmp_path, [SHA], {("GET", f"/repos/{REPO}/commits/{SHA}/status"): STATUS})
    assert (rc, err) == (0, "")
    assert out == ("коммит 02330b40cccc: 2 проверок\n"
                   "failure Deploy staging / deploy (push)\n"
                   "success tests / pytest (push)\n")
    rec = json.loads((tmp_path / "forge.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert rec["cmd"] == "status" and rec["sha"] == SHA and rec["pr"] is None and rec["ok"] is True
    assert [c[0] for c in op.seen] == ["GET"]  # a read: nothing is posted


def test_status_of_a_commit_without_checks(tmp_path):
    rc, out, _, _ = _status(tmp_path, ["02330b4"], {("GET", f"/repos/{REPO}/commits/02330b4/status"): {"statuses": []}})
    assert (rc, out) == (0, "коммит 02330b4: проверок нет\n")


@pytest.mark.parametrize("argv", [[], ["12"], ["02330B40"], ["02330b"], ["g" * 12], ["0" * 41], [SHA, SHA],
                                  ["main"], ["02330b40/../x"]])
def test_status_refuses_anything_but_a_commit_hash_before_the_token_is_read(tmp_path, argv):
    d = tmp_path / "no-forge-dir"  # no token there: reaching it would be code 1, not 2
    out, err = io.StringIO(), io.StringIO()
    op = make_opener({})
    rc = forge.main(["--agent", "dev", "status", "digipaddock", *argv], opener=op, out=out, err=err,
                    forge_dir=str(d), repos_path=str(tmp_path / "repos.json"), journal_path=str(tmp_path / "j.jsonl"))
    assert rc == 2 and op.seen == [] and out.getvalue() == ""
    assert "status <project> <sha> — хеш коммита, 7–40 шестнадцатеричных знаков" in err.getvalue()


def test_status_of_an_unknown_commit_is_code_1(tmp_path):
    rc, out, err, _ = _status(tmp_path, [SHA], {("GET", f"/repos/{REPO}/commits/{SHA}/status"): (404, {"message": "x"})})
    assert rc == 1 and out == "" and "404" in err
