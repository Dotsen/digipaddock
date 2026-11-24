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
    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def get(self, path, params=None, max_bytes=forge.LIMIT):
        self.calls.append((path, params))
        self.max_bytes = max_bytes
        answer = self.answers[path]
        if isinstance(answer, Exception):
            raise answer
        return answer


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
    # one read per environment, of its own workflow: a busy workflow never pushes another env's runs out
    assert api.calls[:2] == [
        ("/repos/o/alert/actions/runs", {"workflow_id": "deploy-test.yml", "limit": deploys.LIMIT}),
        ("/repos/o/alert/actions/runs", {"workflow_id": "deploy-prod.yml", "limit": deploys.LIMIT}),
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


@pytest.mark.parametrize(
    "value", ["", None, "yesterday", "0001-01-01T00:00:00Z", "2026-11-24T12:00:00"]
)
def test_times_without_a_zone_or_a_start_are_empty(value):
    assert deploys.utc(value) == ""
