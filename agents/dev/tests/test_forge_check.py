"""kd-forge check asks the cabinet for a dev.check of the PR's live head — the
passport's contour and profiles, the target's head, the other components' heads; refused before the cabinet when the
check would mean nothing."""
import io
import json
import urllib.error
from pathlib import Path

import pytest

from dev_agent import forge
from test_forge_read import REPO, Resp, write_env

HEAD, BASE, DEPLOY = "b" * 40, "c" * 40, "d" * 40
DEPLOY_REPO = "example/digipaddock-deploy"
PR = {"number": 12, "title": "KD-12: заголовок", "state": "open", "merged": False, "mergeable": True,
      "head": {"sha": HEAD, "ref": "x"}, "base": {"ref": "main"}, "user": {"login": "digipaddock-dev"}, "html_url": "u"}
CHECKS = {"contour": "app-ci", "profiles": {"unit": {"required": True}, "e2e": {}}, "timeout_s": 1800}
ID = "e" * 32


def opener(pr=None, contains=0):
    seen = []

    def open_(req, timeout=None):
        url = req.full_url
        seen.append((req.get_method(), url))
        if url.endswith(f"/repos/{REPO}/pulls/12"):
            return Resp(json.dumps(pr or PR).encode())
        if url.endswith(f"/repos/{REPO}/branches/main"):
            return Resp(json.dumps({"name": "main", "commit": {"id": BASE}}).encode())
        if url.endswith(f"/repos/{DEPLOY_REPO}/branches/main"):
            return Resp(json.dumps({"name": "main", "commit": {"id": DEPLOY}}).encode())
        if f"/repos/{REPO}/compare/{HEAD}...{BASE}" in url:
            return Resp(json.dumps({"total_commits": contains}).encode())
        raise urllib.error.HTTPError(url, 404, "x", {}, Resp(b"{}"))
    open_.seen = seen
    return open_


class Cabinet:
    def __init__(self, answer=None, error=None):
        self.seen, self.answer, self.error = [], answer, error

    def call(self, request):
        self.seen.append(request)
        if self.error:
            raise self.error
        return self.answer or {"operation": {"id": ID, "state": "pending"}, "created": True}


def prepare(tmp_path, checks=CHECKS, components=("digipaddock", "digipaddock-deploy")):
    folder = tmp_path / "forge" if (tmp_path / "forge").exists() else write_env(tmp_path)
    (tmp_path / "repos.json").write_text(json.dumps({
        "digipaddock": {"repo": REPO, "dir": "work/DigiPaddock", "release": "digipaddock",
                  **({"checks": checks} if checks is not None else {})},
        "digipaddock-deploy": {"repo": DEPLOY_REPO, "dir": "work/digipaddock-deploy", "release": "digipaddock",
                         "role": "overlay"}}), encoding="utf-8")
    (tmp_path / "contours.json").write_text(json.dumps({"app-ci": {"host": "local", "components": list(components)}}),
                                            encoding="utf-8")
    (tmp_path / "agents.json").write_text(json.dumps({"dev": {"user": "digi-dev"}}), encoding="utf-8")
    return {"forge_dir": str(folder), "repos_path": str(tmp_path / "repos.json"),
            "journal_path": str(tmp_path / "forge.jsonl"), "contours_path": str(tmp_path / "contours.json"),
            "agents_path": str(tmp_path / "agents.json")}


CHANNEL = {"KD_AGENT": "dev", "KD_CALLER_USER": "digi-dev"}


def run(tmp_path, *args, op=None, cabinet=None, environ=CHANNEL, checks=CHECKS, components=("digipaddock", "digipaddock-deploy")):
    kw = prepare(tmp_path, checks, components)
    out, err = io.StringIO(), io.StringIO()
    rc = forge.main(["--agent", "dev", "check", "digipaddock", "12", *args], opener=op or opener(), cabinet=cabinet,
                    environ=environ, out=out, err=err, **kw)
    return rc, out.getvalue(), err.getvalue()


def test_check_sends_the_live_head_and_the_targets_head(tmp_path):
    cabinet = Cabinet()
    rc, out, err = run(tmp_path, cabinet=cabinet)
    assert rc == 0, err
    assert cabinet.seen == [{"op": "devcheck.request", "card": "KD-12", "agent": "dev", "project": "digipaddock",
                             "repo": REPO, "pr": 12, "head": HEAD, "base_sha": BASE, "target": "main",
                             "profiles": ["unit"], "contour": "app-ci", "timeout_s": 1800,
                             "set": {"digipaddock-deploy": DEPLOY}}]
    assert f"проверка {ID[:12]} в очереди контура app-ci: профили unit" in out
    assert f"kd-work finish KD-12 --outcome wait_dependency --operation {ID}" in out


def test_default_profiles_are_the_required_ones(tmp_path):
    cabinet = Cabinet()
    checks = {**CHECKS, "profiles": {"unit": {"required": True}, "e2e": {"required": True}, "lint": {}}}
    assert run(tmp_path, cabinet=cabinet, checks=checks)[0] == 0
    assert cabinet.seen[0]["profiles"] == ["unit", "e2e"]
    cabinet = Cabinet()
    rc, out, err = run(tmp_path, cabinet=cabinet, checks={**CHECKS, "profiles": {"unit": {}}})
    assert rc == 1 and "обязательных" in err and cabinet.seen == []


def test_explicit_profiles_must_be_in_the_passport(tmp_path):
    cabinet = Cabinet()
    assert run(tmp_path, "--profiles", "e2e,unit", cabinet=cabinet)[0] == 0
    assert cabinet.seen[0]["profiles"] == ["e2e", "unit"]
    cabinet = Cabinet()
    rc, out, err = run(tmp_path, "--profiles", "unit,smoke", cabinet=cabinet)
    assert rc == 1 and "smoke" in err and cabinet.seen == []
    rc, out, err = run(tmp_path, "--profiles", "unit,unit", cabinet=Cabinet())
    assert rc == 2


def test_base_stale_refused_before_the_cabinet(tmp_path):
    cabinet = Cabinet()
    rc, out, err = run(tmp_path, op=opener(contains=2), cabinet=cabinet)
    assert rc == 1 and "база main ушла вперёд" in err and cabinet.seen == []


def test_untrusted_author_refused(tmp_path):
    cabinet = Cabinet()
    rc, out, err = run(tmp_path, op=opener({**PR, "user": {"login": "stranger"}}), cabinet=cabinet)
    assert rc == 1 and cabinet.seen == []


def test_closed_pr_refused(tmp_path):
    for pr in ({**PR, "state": "closed"}, {**PR, "merged": True, "state": "closed"}):
        cabinet = Cabinet()
        rc, out, err = run(tmp_path, op=opener(pr), cabinet=cabinet)
        assert rc == 1 and "не открыт" in err and cabinet.seen == []


def test_no_card_in_the_title_refused(tmp_path):
    rc, out, err = run(tmp_path, op=opener({**PR, "title": "без карточки"}), cabinet=Cabinet())
    assert rc == 1 and "KD-<n>" in err


def test_target_outside_the_passport_refused(tmp_path):
    rc, out, err = run(tmp_path, op=opener({**PR, "base": {"ref": "release"}}), cabinet=Cabinet())
    assert rc == 1 and "targets" in err


def test_no_contour_refused(tmp_path):
    cabinet = Cabinet()
    rc, out, err = run(tmp_path, cabinet=cabinet, checks={"profiles": {"unit": {"required": True}}})
    assert rc == 1 and "контур" in err and cabinet.seen == []
    rc, out, err = run(tmp_path, cabinet=cabinet, components=("other",))
    assert rc == 1 and "app-ci" in err and cabinet.seen == []


def test_set_has_every_other_component_of_the_contour(tmp_path):
    cabinet = Cabinet()
    assert run(tmp_path, cabinet=cabinet, components=("digipaddock",))[0] == 0
    assert cabinet.seen[0]["set"] == {}
    cabinet = Cabinet()
    assert run(tmp_path, cabinet=cabinet)[0] == 0
    assert cabinet.seen[0]["set"] == {"digipaddock-deploy": DEPLOY}


def test_caller_must_be_the_channels_agent(tmp_path):
    for environ in ({}, {"KD_AGENT": "dev"}, {"KD_AGENT": "dev", "KD_CALLER_USER": "digi-boss"},
                    {"KD_AGENT": "boss", "KD_CALLER_USER": "digi-dev"}):
        cabinet = Cabinet()
        rc, out, err = run(tmp_path, cabinet=cabinet, environ=environ)
        assert rc == 1 and cabinet.seen == [], environ


def test_timeout_is_the_cabinets_at_most(tmp_path):
    from kd.cabinet import devchecks

    assert forge.CHECK_TIMEOUT_MAX == devchecks.TIMEOUT_S[1]
    cabinet = Cabinet()
    assert run(tmp_path, cabinet=cabinet, checks={**CHECKS, "timeout_s": 7200})[0] == 0
    assert cabinet.seen[0]["timeout_s"] == forge.CHECK_TIMEOUT_MAX


def test_a_joined_check_says_so(tmp_path):
    rc, out, err = run(tmp_path, cabinet=Cabinet({"operation": {"id": ID, "state": "running"}, "created": False}))
    assert rc == 0 and f"проверка {ID[:12]} уже идёт" in out and "--operation" in out
    rc, out, err = run(tmp_path, cabinet=Cabinet({"operation": {"id": ID, "state": "succeeded"}, "created": False}))
    assert rc == 0 and f"проверка {ID[:12]} уже пройдена" in out and "--operation" not in out


def test_a_cabinet_refusal_is_exit_1(tmp_path):
    from kd.cabinet.client import CabinetError

    rc, out, err = run(tmp_path, cabinet=Cabinet(error=CabinetError("refused", "KD-12 is To Do")))
    assert rc == 1 and "KD-12 is To Do" in err


def test_profiles_only_with_check(tmp_path):
    kw = prepare(tmp_path)
    kw.pop("contours_path"), kw.pop("agents_path")
    err = io.StringIO()
    assert forge.main(["--agent", "dev", "pr", "digipaddock", "12", "--profiles", "unit"], opener=opener(),
                      out=io.StringIO(), err=err, **kw) == 2


def test_capability_lists_check():
    from dev_agent import guard

    root = Path(__file__).resolve().parents[3]
    cap = json.loads((root / "common/agent-capabilities/forge.review.json").read_text(encoding="utf-8"))
    forms = next(w["subcommands"] for w in cap["wrappers"] if w["command"] == "kd-forge")
    assert "check" in forms and "check" in guard.FORGE_SUBCOMMANDS
    wrapper = (root / "agents/dev/bin/kd-forge").read_text(encoding="utf-8")
    assert "prs|pr|comments|publish|check|status)" in wrapper and "check <project> <N> [--profiles" in wrapper
    assert guard.decide("kd-forge check digipaddock 12 --profiles unit,e2e") is None
