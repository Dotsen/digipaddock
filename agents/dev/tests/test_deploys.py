"""The deploys reader (KD-9443): the projects' Forgejo Actions runs into the cabinet's «Deploys»."""

import json

import pytest

from dev_agent import deploys, forge
from kd.cabinet import deploys as cabinet_deploys

PROJECTS = {
    "schema_version": 1,
    "projects": {
        "digipaddock": {
            "name": "DIGIPADDOCK",
            "route": "@owner",
            "done_by": "",
            "memory_bank": "",
            "repo": {
                "repo": "o/digipaddock",
                "dir": "work/digipaddock",
                "release": "digipaddock",
            },
        },
        "alert": {
            "name": "Alert",
            "route": "@owner",
            "done_by": "",
            "memory_bank": "",
            "repo": {
                "repo": "o/alert",
                "dir": "work/alert",
                "base": "master",
                "targets": ["master"],
                "deploys": {
                    "test": {"workflow": "deploy-test.yml", "branch": "master"},
                    "prod": {
                        "workflow": "deploy-prod.yml",
                        "branch": "master",
                        "manual": True,
                        "confirm": "confirm",
                    },
                },
            },
        },
        "admin": {
            "name": "Servers",
            "route": "@owner",
            "done_by": "",
            "memory_bank": "",
        },
        "keeta": {
            "name": "Keeta",
            "route": "@owner",
            "done_by": "",
            "memory_bank": "",
            "repo": {
                "repo": "o/keeta",
                "dir": "work/keeta",
                "deploys": {"prod": {"workflow": "deploy.yml", "branch": "main"}},
            },
        },
    },
}


def _projects(tmp_path, data=PROJECTS):
    path = tmp_path / "projects.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _run(run, workflow="deploy-test.yml", status="success", **over):
    item = {
        "id": run,
        "index_in_repo": run,
        "workflow_id": workflow,
        "status": status,
        "event": "push",
        "commit_sha": "A" * 40,
        "prettyref": "master",
        "html_url": f"https://forge.example.test/o/alert/actions/runs/{run}",
        "trigger_user": {"login": "dev", "id": 5},
        "title": "KD-1: fix\nmore",
        "created": "2026-11-24T15:48:21+03:00",
        "started": "2026-11-24T15:48:22+03:00",
        "stopped": "0001-01-01T00:00:00Z",
        "event_payload": "{}",
    }
    item.update(over)
    return item


class Api:
    """Forgejo's answers by path; a branch it does not name is Forgejo's 404. ``totals``: (head, base) → the commits
    head has that base lacks (forge.commits_between) — their count, or (count, the listed items)."""

    def __init__(self, answers, totals=None):
        self.answers, self.calls, self.totals = answers, [], dict(totals or {})

    def get(self, path, params=None, max_bytes=forge.LIMIT):
        self.calls.append((path, params))
        self.max_bytes = max_bytes
        missing = "/branches/" in path or path.endswith("/status")  # Forgejo's own 404s
        default = forge.ForgeError("Forgejo 404: not found", 404) if missing else KeyError(path)
        answer = self.answers.get(path, default)
        if callable(answer):  # an answer per request (per workflow_id)
            answer = answer(params)
        if isinstance(answer, Exception):
            raise answer
        return answer

    def listing(self, path, params=None, max_bytes=forge.LIMIT):
        self.calls.append((path, params))
        answer = self.totals.get((params["sha"], params["not"]), forge.ForgeError("Forgejo 500", 500))
        if isinstance(answer, Exception):
            raise answer
        total, items = answer if isinstance(answer, tuple) else (answer, [])
        return items[:params["limit"]], total


class Client:
    def __init__(self, error=None, watch=()):
        self.requests, self.error, self.watch = [], error, list(watch)

    def call(self, request):
        if self.error:
            raise self.error
        if request["op"] == "deploy.watch":
            return {"watch": self.watch}
        self.requests.append(request)
        cabinet_deploys._sources(
            request["sources"]
        )  # what the cabinet takes, or it raises
        return {"changed": []}


def test_the_sources_are_the_projects_with_deploys_in_the_files_order(tmp_path):
    assert deploys.sources(_projects(tmp_path)) == [
        {
            "project": "alert",
            "repo": "o/alert",
            "envs": [
                {
                    "env": "test",
                    "workflow": "deploy-test.yml",
                    "branch": "master",
                    "manual": False,
                    "confirm": "",
                    "skip_tests": False,
                },
                {
                    "env": "prod",
                    "workflow": "deploy-prod.yml",
                    "branch": "master",
                    "manual": True,
                    "confirm": "confirm",  # the owner's button types «deploy» into it (KD-9447)
                    "skip_tests": False,
                },
            ],
        },
        {
            "project": "keeta",
            "repo": "o/keeta",
            "envs": [
                {
                    "env": "prod",
                    "workflow": "deploy.yml",
                    "branch": "main",
                    "manual": False,
                    "confirm": "",
                    "skip_tests": False,
                }
            ],
        },
    ]
    assert deploys.sources(tmp_path / "missing.json") == []


def test_a_run_is_read_as_the_cabinet_keeps_it():
    envs = {
        "deploy-test.yml": ("test", "master"),
        "deploy-prod.yml": ("prod", "master"),
    }
    assert deploys.run_of(_run(7), envs) == {
        "run": 7,
        "number": 7,
        "env": "test",
        "status": "success",
        "event": "push",
        "sha": "a" * 40,
        "ref": "master",
        "url": "https://forge.example.test/o/alert/actions/runs/7",
        "actor": "dev",
        "inputs": {},
        "title": "KD-1: fix more",
        "created": "2026-11-24T12:48:21Z",
        "started": "2026-11-24T12:48:22Z",
        "stopped": "",
    }
    assert deploys.run_of(_run(8, workflow="ci.yml"), envs) is None  # not a deploy
    # one workflow that also runs on a branch deploying nothing (arm's deploy.yml on dev): not the env's run
    assert deploys.run_of(_run(11, prettyref="dev"), envs) is None
    assert (
        deploys.run_of(_run(12, prettyref="refs/heads/master"), envs)["env"] == "test"
    )
    assert deploys.run_of(_run(9, status="weird"), envs)["status"] == "unknown"
    assert (
        deploys.run_of(_run(10, html_url="http://x/1", commit_sha="HEAD"), envs)["url"]
        == ""
    )


def test_a_dispatched_runs_inputs_are_kept_as_text():
    """The journal shows what the owner's prod run asked for (``confirm``, ``skip_tests``)."""
    payload = json.dumps(
        {
            "inputs": {"skip_tests": True, "confirm": "deploy", "bad name": "x"},
            "ref": "main",
        }
    )
    run = deploys.run_of(
        _run(
            3,
            workflow="deploy-prod.yml",
            event="workflow_dispatch",
            event_payload=payload,
        ),
        {"deploy-prod.yml": ("prod", "master")},
    )
    assert run["inputs"] == {"skip_tests": "true", "confirm": "deploy"}
    push = deploys.run_of(
        _run(4, event_payload=payload), {"deploy-test.yml": ("test", "master")}
    )
    assert push["inputs"] == {}


def test_main_reports_every_project_and_one_failure_is_that_projects_error(
    tmp_path, capsys
):
    api = Api(
        {
            "/repos/o/alert/actions/runs": {
                "total_count": 3,
                "workflow_runs": [
                    _run(1),
                    _run(2, workflow="deploy-prod.yml", status="running"),
                    _run(3, workflow="lint.yml"),
                ],
            },
            "/repos/o/keeta/actions/runs": forge.ForgeError(
                "Forgejo 403: token does not have scope", 403
            ),
        }
    )
    client = Client()
    assert (
        deploys.main(
            api_factory=lambda: api, client=client, projects=_projects(tmp_path)
        )
        == 0
    )
    # one read per environment, of its own workflow: a busy workflow never pushes another env's runs out; page 1
    # named — without it Forgejo 15 ignores limit and sends the whole history
    assert api.calls[:2] == [
        ("/repos/o/alert/actions/runs", {"workflow_id": "deploy-test.yml", "limit": deploys.LIMIT, "page": 1}),
        ("/repos/o/alert/actions/runs", {"workflow_id": "deploy-prod.yml", "limit": deploys.LIMIT, "page": 1}),
    ]
    assert api.max_bytes == deploys.RUNS_MAX
    (request,) = client.requests
    alert, keeta = request["sources"]
    assert request["op"] == "deploy.report"
    assert [(r["run"], r["env"], r["status"]) for r in alert["runs"]] == [
        (1, "test", "success"),
        (2, "prod", "running"),
    ]
    assert (
        alert["error"] == ""
        and keeta["runs"] == []
        and keeta["error"].startswith("Forgejo 403")
    )
    assert "keeta: Forgejo 403" in capsys.readouterr().err


def test_without_a_token_every_project_reports_the_error(tmp_path):
    def no_token():
        raise forge.ForgeError("нет токена dev (FileNotFoundError)")

    client = Client()
    assert (
        deploys.main(api_factory=no_token, client=client, projects=_projects(tmp_path))
        == 0
    )
    assert {s["error"] for s in client.requests[0]["sources"]} == {
        "нет токена dev (FileNotFoundError)"
    }


def test_a_report_the_cabinet_refuses_fails_the_run(tmp_path, capsys):
    from kd.cabinet import client as cabinet_client

    client = Client(
        error=cabinet_client.CabinetError(
            "unavailable", "board unavailable (ConnectionRefusedError)"
        )
    )
    api = Api(
        {
            "/repos/o/alert/actions/runs": {"workflow_runs": []},
            "/repos/o/keeta/actions/runs": {},
        }
    )
    assert (
        deploys.main(
            api_factory=lambda: api, client=client, projects=_projects(tmp_path)
        )
        == 1
    )
    err = capsys.readouterr().err
    assert (
        "кабинет не принял отчёт" in err
        and "keeta: ответ Forgejo без workflow_runs" in err
    )


HEAD = "b" * 40


def _compare(total):
    return {"total_commits": total, "commits": []}


def test_the_watched_commit_is_looked_for_in_finished_runs_only(tmp_path):
    """A card waits for alert:test to deploy its PR head: the reader asks Forgejo whether each finished run's
    commit holds it, skipping runs already answered, running ones, the commit's own run and other environments."""
    api = Api(
        {
            "/repos/o/alert/actions/runs": {
                "workflow_runs": [
                    _run(1),  # commit a…: holds the head
                    _run(2, commit_sha="c" * 40),  # does not
                    _run(3, status="running"),
                    _run(4, commit_sha=HEAD),  # the head's own run: no question
                    _run(5, workflow="deploy-prod.yml"),  # another environment
                    _run(6, status="failure", commit_sha="d" * 40),  # answered before
                ]
            },
            "/repos/o/keeta/actions/runs": {"workflow_runs": []},
            f"/repos/o/alert/compare/{'a' * 40}...{HEAD}": _compare(0),
            f"/repos/o/alert/compare/{'c' * 40}...{HEAD}": _compare(2),
        }
    )
    client = Client(watch=[{"project": "alert", "env": "test", "sha": HEAD, "checked": [6]}])
    assert deploys.main(api_factory=lambda: api, client=client, projects=_projects(tmp_path)) == 0
    alert, keeta = client.requests[0]["sources"]
    assert alert["includes"] == [
        {"run": 1, "sha": HEAD, "included": True},
        {"run": 2, "sha": HEAD, "included": False},
    ]
    assert keeta["includes"] == []
    assert [p for p, _ in api.calls if "/compare/" in p] == [
        f"/repos/o/alert/compare/{'a' * 40}...{HEAD}",
        f"/repos/o/alert/compare/{'c' * 40}...{HEAD}",
    ]


def test_checks_stop_at_the_budget_and_a_failed_one_waits_for_the_next_tick(tmp_path):
    source = {"project": "alert", "repo": "o/alert", "runs": [
        {"run": n, "env": "test", "status": "success", "sha": f"{n:x}" * 40} for n in (1, 2, 3)]}
    api = Api({
        f"/repos/o/alert/compare/{'1' * 40}...{HEAD}": forge.ForgeError("Forgejo 500", 500),
        f"/repos/o/alert/compare/{'2' * 40}...{HEAD}": _compare(0),
    })
    watch = [{"project": "alert", "env": "test", "sha": HEAD, "checked": []}]
    budget = [2]
    assert deploys.includes(api, source, watch, budget) == [{"run": 2, "sha": HEAD, "included": True}]
    assert budget == [0] and len(api.calls) == 2


def test_without_the_watch_list_the_runs_are_reported_all_the_same(tmp_path, capsys):
    from kd.cabinet import client as cabinet_client

    class Flaky(Client):
        def call(self, request):
            if request["op"] == "deploy.watch":
                raise cabinet_client.CabinetError("unavailable", "busy")
            return super().call(request)

    client = Flaky()
    api = Api({"/repos/o/alert/actions/runs": {"workflow_runs": [_run(1)]},
               "/repos/o/keeta/actions/runs": {"workflow_runs": []}})
    assert deploys.main(api_factory=lambda: api, client=client, projects=_projects(tmp_path)) == 0
    assert client.requests[0]["sources"][0]["includes"] == []
    assert "ожидания карточек не прочитаны" in capsys.readouterr().err


def _env(env, branch, manual=False):
    return {"env": env, "workflow": f"deploy-{env}.yml", "branch": branch, "manual": manual, "confirm": "",
            "skip_tests": False}


def test_an_answer_that_ignores_the_limit_is_cut_and_a_source_fits_the_cabinet():
    """Forgejo once sent a repository's whole history for limit=20 (arm: 411 runs): the cabinet refuses a source of
    more than 100 runs, and with it the whole report — the reader keeps each workflow's newest LIMIT runs and the
    source's newest 100."""
    envs = [_env(f"e{i}", "main") for i in range(6)]
    items = [
        _run(n * 100 + k, workflow=f"deploy-e{n}.yml", prettyref="main",
             created=f"2026-11-{10 + n:02d}T{k:02d}:00:00Z")
        for n in range(6) for k in range(24)
    ]
    api = Api({"/repos/o/many/actions/runs": lambda params: {"workflow_runs": [
        i for i in items if i["workflow_id"] == params["workflow_id"]]}})
    got = deploys.read(api, {"project": "many", "repo": "o/many", "envs": envs})
    assert got["error"] == "" and len(got["runs"]) == cabinet_deploys.RUNS_MAX
    # each workflow kept its first LIMIT items of the answer, and the source kept the newest of them
    assert {r["run"] % 100 for r in got["runs"]} <= set(range(deploys.LIMIT))
    assert min(r["created"] for r in got["runs"]) >= "2026-11-11"
    cabinet_deploys._sources([{**got, "includes": []}])  # the cabinet takes it


T, S, P, X, H = ("1" * 40, "2" * 40, "3" * 40, "4" * 40, "5" * 40)
ALERT = [_env("test", "master"), _env("stage", "dev"), _env("prod", "master", manual=True)]


def _r(run, env, status, sha, created):
    return {"run": run, "env": env, "status": status, "sha": sha, "created": created}


def _source(runs, envs=ALERT):
    return {"project": "alert", "repo": "o/alert", "envs": envs, "runs": runs, "error": ""}


MASTER = {"/repos/o/alert/branches/master": {"name": "master", "commit": {"id": H}}}


def test_each_environment_says_what_waits_and_how_far_it_is_ahead_of_prod():
    """prod (the only manual environment) deployed P; its newer run failed, so P is still what runs there. master's
    head H is 3 commits past P; test deployed H itself (nothing waits, no question asked), 3 commits ahead of prod;
    stage deploys dev — a branch Forgejo does not know: its head is unknown, its lead over prod is still counted."""
    runs = [
        _r(1, "test", "success", H, "2026-11-25T10:00:00Z"),
        _r(2, "stage", "success", S, "2026-11-25T09:00:00Z"),
        _r(3, "prod", "success", P, "2026-11-25T08:00:00Z"),
        _r(4, "prod", "failure", X, "2026-11-25T11:00:00Z"),
        _r(5, "test", "running", T, "2026-11-25T12:00:00Z"),
    ]
    api = Api(MASTER, totals={(H, P): 3, (S, P): 2})
    budget = [20]
    measured = deploys.measure(api, _source(runs), budget)
    assert [(e["env"], e["head"], e["pending"], e["ahead"], e["ahead_of"]) for e in measured] == [
        ("test", H, 0, 3, "prod"),
        ("stage", "", None, 2, "prod"),
        ("prod", H, 3, None, ""),
    ]
    assert measured[2]["manual"] is True  # the passport's fields stay
    assert [p for p, _ in api.calls if "/branches/" in p] == [
        "/repos/o/alert/branches/master", "/repos/o/alert/branches/dev"]
    # test against prod and prod's own pending are one range (H past P): asked once
    assert budget == [18]


def test_a_count_past_the_budget_or_refused_is_unknown():
    runs = [_r(1, "test", "success", T, "2026-11-25T10:00:00Z"), _r(2, "prod", "success", P, "2026-11-25T08:00:00Z")]
    api = Api(MASTER, totals={(H, T): forge.ForgeError("Forgejo 500", 500), (T, P): 4, (H, P): 5})
    measured = deploys.measure(api, _source(runs, ALERT[:1] + ALERT[2:]), [2])
    assert [(e["env"], e["pending"], e["ahead"]) for e in measured] == [("test", None, 4), ("prod", None, None)]


def _fc(sha, message, login="dev", name="Dev", date="2026-11-24T21:10:59+03:00", url=None):
    """A commit as Forgejo's commits list has it."""
    return {"sha": sha, "html_url": f"https://forge.example.test/o/alert/commit/{sha}" if url is None else url,
            "commit": {"message": message, "author": {"name": name, "date": "2026-11-20T10:00:00Z"},
                       "committer": {"name": "Forgejo", "date": date}},
            "author": {"login": login} if login else None, "files": [], "stats": None}


def test_each_count_brings_its_commits():
    """What waits for prod is a list the owner reads: each commit's title and description, its author, when it landed
    on the branch (the committer's time, in UTC) and its page in Forgejo — newest first, as Forgejo lists them."""
    runs = [_r(1, "test", "success", H, "2026-11-25T10:00:00Z"), _r(2, "prod", "success", P, "2026-11-25T08:00:00Z")]
    listed = [_fc(H, "fix(status): a silent run still writes the status\n\nA loud skip notifies."),
              _fc(X, "fix: one line", login="", name="Ada")]
    api = Api(MASTER, totals={(H, P): (3, listed)})
    test, prod = deploys.measure(api, _source(runs, ALERT[:1] + ALERT[2:]), [20])
    assert prod["pending"] == 3 and prod["pending_commits"] == [
        {"sha": H, "title": "fix(status): a silent run still writes the status", "body": "A loud skip notifies.",
         "author": "dev", "date": "2026-11-24T18:10:59Z", "url": f"https://forge.example.test/o/alert/commit/{H}"},
        {"sha": X, "title": "fix: one line", "body": "", "author": "Ada", "date": "2026-11-24T18:10:59Z",
         "url": f"https://forge.example.test/o/alert/commit/{X}"},
    ]
    assert test["ahead"] == 3 and test["ahead_commits"] == prod["pending_commits"]
    assert test["pending"] == 0 and test["pending_commits"] == [] and prod["ahead_commits"] == []
    ranges = [p for p, params in api.calls if p.endswith("/commits")]
    assert ranges == ["/repos/o/alert/commits"]  # one range, asked once
    assert api.calls[-1][1]["limit"] == deploys.COMMITS_SHOWN


def test_a_commit_is_cut_to_what_the_cabinet_takes():
    long = "x" * (deploys.BODY_MAX + 50)
    got = deploys.commit(_fc(H, "  title\twith\x07bell  \r\n\r\n" + long + "\r\nend", url="http://plain/x"))
    assert got["title"] == "title with bell" and got["url"] == ""
    assert len(got["body"]) == deploys.BODY_MAX and got["body"].startswith("xxx") and "\r" not in got["body"]
    assert deploys.commit(_fc("not-a-sha", "x")) is None
    assert deploys.commit({"sha": H})["title"] == ""  # a broken item is still a commit, with nothing to say


def test_a_range_nobody_answered_has_no_commits():
    runs = [_r(1, "test", "success", T, "2026-11-25T10:00:00Z"), _r(2, "prod", "success", P, "2026-11-25T08:00:00Z")]
    api = Api(MASTER, totals={(T, P): forge.TooLarge("ответ Forgejo больше 1 МБ")})
    test, prod = deploys.measure(api, _source(runs, ALERT[:1] + ALERT[2:]), [20])
    assert (test["ahead"], test["ahead_commits"]) == (None, [])
    assert (prod["pending"], prod["pending_commits"]) == (None, [])


def _status(*checks):
    """Forgejo's combined status of a commit: (context, state) pairs, each with its job's log."""
    return {"state": "x", "total_count": len(checks), "statuses": [
        {"context": c, "status": st, "target_url": f"https://forge.example.test/o/alert/actions/runs/{n}/jobs/0",
         "description": "Failing after 1m"} for n, (c, st) in enumerate(checks)]}


def _listed(api, runs, budget=20):
    """The environments as collect reports them: measured, each listed commit with its checks."""
    envs = deploys.measure(api, _source(runs, ALERT[:1] + ALERT[2:]), [20])
    return deploys.with_checks(api, "o/alert", envs, [budget])


def test_each_listed_commit_carries_its_checks():
    """A commit that failed its tests says so before anyone deploys it: its checks' worst state, and the checks that
    did not pass with their logs — an error is a failure; the passed ones are the state alone."""
    runs = [_r(1, "test", "success", H, "2026-11-25T10:00:00Z"), _r(2, "prod", "success", P, "2026-11-25T08:00:00Z")]
    api = Api({**MASTER,
               f"/repos/o/alert/commits/{H}/status": _status(("CI / Deploy (push)", "success"),
                                                             ("CI / Run tests (push)", "failure"),
                                                             ("CI / lint (push)", "error")),
               f"/repos/o/alert/commits/{X}/status": _status(("CI / Run tests (push)", "success")),
               f"/repos/o/alert/commits/{T}/status": _status(("CI / Run tests (push)", "pending"))},
              totals={(H, P): (3, [_fc(H, "a"), _fc(X, "b"), _fc(T, "c"), _fc(S, "d")])})
    test, prod = _listed(api, runs)
    h, x, t, s = prod["pending_commits"]
    assert (h["status"], h["checks"]) == ("failure", [
        {"context": "CI / Run tests (push)", "state": "failure",
         "url": "https://forge.example.test/o/alert/actions/runs/1/jobs/0"},
        {"context": "CI / lint (push)", "state": "failure",
         "url": "https://forge.example.test/o/alert/actions/runs/2/jobs/0"}])
    assert (x["status"], x["checks"]) == ("success", [])
    assert (t["status"], [c["state"] for c in t["checks"]]) == ("pending", ["pending"])
    assert (s["status"], s["checks"]) == ("", [])  # Forgejo has none
    assert test["ahead_commits"][0]["status"] == "failure"  # the same commit in two lists
    asked = [p for p, _ in api.calls if p.endswith("/status")]
    assert len(asked) == len(set(asked)) == 4  # each commit asked once


def test_a_cancelled_job_is_no_failure():
    """Forgejo posts a cancelled job — a newer push of the branch stopped its run — as a failure described «Has been
    cancelled»: the commit says cancelled, not failed; a real failure beside it still decides."""
    runs = [_r(1, "test", "success", H, "2026-11-25T10:00:00Z"), _r(2, "prod", "success", P, "2026-11-25T08:00:00Z")]
    stopped = _status(("CI / Run tests (push)", "failure"), ("CI / Deploy (push)", "failure"),
                      ("CI / lint (push)", "success"))
    for entry in stopped["statuses"][:2]:
        entry["description"] = "Has been cancelled"
    mixed = _status(("CI / Run tests (push)", "failure"), ("CI / Deploy (push)", "failure"))
    mixed["statuses"][1]["description"] = "Has been cancelled"
    api = Api({**MASTER, f"/repos/o/alert/commits/{H}/status": stopped, f"/repos/o/alert/commits/{X}/status": mixed},
              totals={(H, P): (2, [_fc(H, "a"), _fc(X, "b")])})
    _, prod = _listed(api, runs)
    h, x = prod["pending_commits"]
    assert (h["status"], [(c["context"], c["state"]) for c in h["checks"]]) == (
        "cancelled", [("CI / Run tests (push)", "cancelled"), ("CI / Deploy (push)", "cancelled")])
    assert (x["status"], [c["state"] for c in x["checks"]]) == ("failure", ["failure", "cancelled"])


def test_checks_past_the_budget_are_unknown():
    runs = [_r(1, "test", "success", H, "2026-11-25T10:00:00Z"), _r(2, "prod", "success", P, "2026-11-25T08:00:00Z")]
    api = Api({**MASTER, f"/repos/o/alert/commits/{H}/status": _status(("CI", "failure")),
               f"/repos/o/alert/commits/{X}/status": _status(("CI", "failure"))},
              totals={(H, P): (2, [_fc(H, "a"), _fc(X, "b")])})
    _, prod = _listed(api, runs, budget=1)
    assert [c["status"] for c in prod["pending_commits"]] == ["failure", ""]


@pytest.mark.parametrize("envs, expected", [
    (ALERT, "prod"),
    ([_env("staging", "main"), _env("live", "main", manual=True)], "live"),
    ([_env("a", "main", manual=True), _env("prod", "main", manual=True)], "prod"),
    ([_env("prod", "main")], "prod"),
    ([_env("test", "main"), _env("stage", "dev")], ""),
])
def test_the_reference_is_the_only_manual_environment_or_prod(envs, expected):
    assert deploys.reference(envs) == expected


def test_the_measured_environments_reach_the_cabinet(tmp_path):
    api = Api({
        "/repos/o/alert/actions/runs": {"workflow_runs": [
            _run(1, commit_sha=H), _run(2, workflow="deploy-prod.yml", commit_sha=P)]},
        "/repos/o/keeta/actions/runs": {"workflow_runs": []},
        **MASTER,
        f"/repos/o/alert/commits/{H}/status": _status(("CI / tests", "failure")),
    }, totals={(H, P): (3, [_fc(H, "feat: a\n\nwhy")])})
    client = Client()
    assert deploys.main(api_factory=lambda: api, client=client, projects=_projects(tmp_path)) == 0
    alert, keeta = client.requests[0]["sources"]
    assert [(e["env"], e["pending"], e["ahead"], e["ahead_of"]) for e in alert["envs"]] == [
        ("test", 0, 3, "prod"), ("prod", 3, None, "")]
    assert [c["title"] for c in alert["envs"][1]["pending_commits"]] == ["feat: a"]
    assert alert["envs"][1]["pending_commits"][0]["checks"] == [
        {"context": "CI / tests", "state": "failure", "url": "https://forge.example.test/o/alert/actions/runs/0/jobs/0"}]
    cabinet_deploys._sources([alert, keeta])  # the cabinet takes the commits too
    assert [(e["env"], e["head"], e["pending"]) for e in keeta["envs"]] == [("prod", "", None)]


@pytest.mark.parametrize(
    "value", ["", None, "yesterday", "0001-01-01T00:00:00Z", "2026-11-24T12:00:00"]
)
def test_times_without_a_zone_or_a_start_are_empty(value):
    assert deploys.utc(value) == ""
