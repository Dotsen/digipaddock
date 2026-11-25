"""deploys — the projects' environments and their Actions runs, as the forge reader reports them (KD-9443): the
«Deploys» page's statuses, the journal and what the ``deploy`` wait reads."""

import pytest
from cabinet_env import CabinetEnv

from kd.cabinet import api, deploys, notices


@pytest.fixture
def env(tmp_path):
    env = CabinetEnv(tmp_path)
    env.users.create(
        "owner",
        "Владелец",
        roles=[("owner", "*")],
        actor="root",
        language="ru",
        timezone="Etc/GMT-3",
    )
    env.users.create(
        "vera",
        "Вера",
        roles=[("viewer", ["digipaddock"])],
        actor="root",
        language="ru",
        timezone="Etc/GMT-3",
    )
    env.users.set_role("ops", ["deploys.read"], actor="user:owner")
    env.users.create(
        "olga",
        "Ольга",
        roles=[("ops", "*")],
        actor="root",
        language="ru",
        timezone="Etc/GMT-3",
    )
    return env


ENVS = [
    {"env": "test", "workflow": "deploy-test.yml", "branch": "master", "manual": False},
    {"env": "prod", "workflow": "deploy-prod.yml", "branch": "master", "manual": True},
]


def _run(run, env="test", status="success", created="2026-10-14T09:58:00Z", **over):
    item = {
        "run": run,
        "number": run,
        "env": env,
        "status": status,
        "event": "push",
        "sha": "a" * 40,
        "ref": "master",
        "url": f"https://forge.example.test/o/alert/actions/runs/{run}",
        "actor": "dev",
        "inputs": {},
        "title": "KD-1: fix",
        "created": created,
        "started": created,
        "stopped": "",
    }
    item.update(over)
    return item


def _source(project="alert", runs=(), error="", envs=None):
    return {
        "project": project,
        "repo": f"o/{project}",
        "envs": ENVS if envs is None else envs,
        "runs": list(runs),
        "error": error,
    }


def _report(env, *sources):
    return env.call("digi-forge", {"op": "deploy.report", "sources": list(sources)})


def _person(env, user, request):
    return api.dispatch(env.ctx, api.Principal("user", -1, user), request)


def _code(call):
    with pytest.raises(api.ApiError) as exc:
        call()
    return exc.value.kind


def test_only_the_forge_executor_reports_runs(env):
    assert (
        _code(lambda: env.call("digi-dev", {"op": "deploy.report", "sources": []}))
        == "refused"
    )
    assert (
        _code(lambda: _person(env, "owner", {"op": "deploy.report", "sources": []}))
        == "refused"
    )
    assert _report(env) == {"ok": True, "changed": [], "woken": 0}


def test_the_page_shows_each_environments_last_run_and_who_started_it(env):
    _report(
        env,
        _source(
            runs=[
                _run(1, created="2026-10-14T09:50:00Z", status="failure"),
                _run(2, created="2026-10-14T09:55:00Z"),
                _run(
                    3,
                    env="prod",
                    status="running",
                    actor="owner",
                    inputs={"confirm": "deploy"},
                ),
            ]
        ),
    )
    found = _person(env, "owner", {"op": "deploy.list"})
    rows = {e["env"]: e for e in found["envs"]}
    assert list(rows) == ["test", "prod"]  # the passport's order
    assert (
        rows["test"]["run"]["run"] == 2 and rows["test"]["run"]["status"] == "success"
    )
    assert rows["test"]["run"]["sha"] == "a" * 40 and rows["test"]["run"][
        "url"
    ].endswith("/runs/2")
    assert rows["prod"]["run"]["actor"] == "owner" and rows["prod"]["manual"] is True
    assert rows["prod"]["run"]["inputs"] == {"confirm": "deploy"}
    assert rows["test"]["stale"] is False and found["stale_after"] == 120


def test_each_environment_says_what_runs_there_and_what_waits(env):
    """The reader's measures (its branch's head, commits not deployed yet, the lead over prod) come back per
    environment; ``deployed`` is the newest successful run — what runs there even when the last run failed."""
    head, prod_sha = "b" * 40, "c" * 40
    envs = [
        {**ENVS[0], "head": head, "pending": 0, "ahead": 3, "ahead_of": "prod"},
        {**ENVS[1], "head": head, "pending": 3, "ahead": None, "ahead_of": ""},
    ]
    _report(env, _source(envs=envs, runs=[
        _run(1, env="prod", sha=prod_sha, created="2026-10-14T09:00:00Z"),
        _run(2, env="prod", status="failure", sha="d" * 40, created="2026-10-14T09:30:00Z"),
        _run(3, sha=head),
    ]))
    rows = {e["env"]: e for e in _person(env, "owner", {"op": "deploy.list"})["envs"]}
    assert (rows["test"]["head"], rows["test"]["pending"], rows["test"]["ahead"], rows["test"]["ahead_of"]) == (
        head, 0, 3, "prod")
    assert (rows["prod"]["pending"], rows["prod"]["ahead"], rows["prod"]["ahead_of"]) == (3, None, "")
    assert rows["prod"]["run"]["status"] == "failure"
    assert (rows["prod"]["deployed"]["sha"], rows["prod"]["deployed"]["run"]) == (prod_sha, 1)
    assert rows["test"]["deployed"]["sha"] == head


def test_a_reader_without_measures_is_taken_and_listed_unknown(env):
    _report(env, _source(runs=[_run(1, status="failure")]))
    row = _person(env, "owner", {"op": "deploy.list"})["envs"][0]
    assert (row["head"], row["pending"], row["ahead"], row["ahead_of"], row["deployed"]) == ("", None, None, "", None)


@pytest.mark.parametrize("extra", [
    {"head": "HEAD"},
    {"pending": -1},
    {"pending": True},
    {"ahead": "3"},
    {"ahead_of": "nowhere"},
    {"ahead_of": "test"},  # never the environment itself
])
def test_broken_measures_are_refused(env, extra):
    envs = [{**ENVS[0], **extra}, ENVS[1]]
    assert _code(lambda: _report(env, _source(envs=envs))) == "invalid"


def test_an_environment_without_runs_is_listed_empty(env):
    _report(env, _source())
    rows = _person(env, "owner", {"op": "deploy.list"})["envs"]
    assert [(r["env"], r["run"]) for r in rows] == [("test", None), ("prod", None)]


def test_data_older_than_two_minutes_is_stale_and_a_failed_read_keeps_the_last_good_time(
    env,
):
    _report(env, _source(runs=[_run(1)]))
    env.now = "2026-10-14T10:01:59Z"
    _report(env, _source(error="Forgejo недоступен (URLError)"))
    row = _person(env, "owner", {"op": "deploy.list"})["envs"][0]
    assert row["ok_at"] == "2026-10-14T10:00:00Z" and row["error"].startswith("Forgejo")
    assert (
        row["stale"] is False and row["run"]["run"] == 1
    )  # a failed read keeps the runs it had
    env.now = "2026-10-14T10:02:01Z"
    assert _person(env, "owner", {"op": "deploy.list"})["envs"][0]["stale"] is True


def test_a_project_the_report_leaves_out_is_no_longer_shown(env):
    _report(env, _source(), _source("api"))
    _report(env, _source("api"))
    assert {
        r["project"] for r in _person(env, "owner", {"op": "deploy.list"})["envs"]
    } == {"api"}


def test_a_run_is_updated_while_it_runs_and_its_changes_are_returned(env):
    """The interface of the ``deploy`` wait: what changed in a report, inside its write."""
    assert _report(env, _source(runs=[_run(5, status="running")]))["changed"] == [
        {
            "project": "alert",
            "env": "test",
            "run": 5,
            "sha": "a" * 40,
            "status": "running",
            "before": "",
        }
    ]
    assert _report(env, _source(runs=[_run(5, status="running")]))["changed"] == []
    done = _report(
        env, _source(runs=[_run(5, status="success", stopped="2026-10-14T09:59:00Z")])
    )["changed"]
    assert done == [
        {
            "project": "alert",
            "env": "test",
            "run": 5,
            "sha": "a" * 40,
            "status": "success",
            "before": "running",
        }
    ]
    assert (
        deploys.last_run(env.store, "alert", "test")["stopped"]
        == "2026-10-14T09:59:00Z"
    )
    assert (
        env.store._conn.execute("SELECT COUNT(run) FROM deploy_runs").fetchone()[0] == 1
    )


def test_the_journal_lists_runs_newest_first_with_filters_and_pages(env):
    many = [
        _run(
            n,
            env="test" if n % 2 else "prod",
            created=f"2026-10-14T0{n // 60}:{n % 60:02d}:00Z",
        )
        for n in range(1, 61)
    ]
    _report(
        env,
        _source(runs=many),
        _source("api", runs=[_run(1, created="2026-10-14T09:00:30Z")]),
    )
    page = _person(env, "owner", {"op": "deploy.runs"})
    assert len(page["items"]) == 50 and page["items"][0]["project"] == "api"
    assert [i["created"] for i in page["items"]] == sorted(
        (i["created"] for i in page["items"]), reverse=True
    )
    rest = _person(env, "owner", {"op": "deploy.runs", "before": page["next"]})
    assert len(rest["items"]) == 11 and rest["next"] is None
    only = _person(
        env,
        "owner",
        {"op": "deploy.runs", "project": "alert", "env": "prod", "limit": 200},
    )
    assert len(only["items"]) == 30 and {
        (i["project"], i["env"]) for i in only["items"]
    } == {("alert", "prod")}


def test_the_platforms_releases_come_from_the_cards_release_notices(env):
    """As a release really goes: the health check's notice first, the forge's «released» two seconds later — the
    health decides, the later «released» does not turn a healthy release back into a running one."""
    card = env.card()
    for key, rev in (
        ("notice.release.healthy", "abc1234"),
        ("notice.forge.released", "abc1234"),
    ):
        env.now = (
            "2026-10-14T10:00:02Z"
            if key.endswith("released")
            else "2026-10-14T10:00:00Z"
        )
        notices.post_in_tx(
            env.store,
            source="forge",
            level="info",
            key=key,
            params={"host": "stage", "rev": rev},
            card=card.id,
        )
    env.now = "2026-10-14T11:00:00Z"
    notices.post_in_tx(
        env.store,
        source="forge",
        level="info",
        key="notice.forge.released",
        params={"host": "stage", "rev": "def5678"},
        card=card.id,
    )
    found = _person(env, "owner", {"op": "deploy.list"})["platform"]
    assert (found["sha"], found["status"], found["card"], found["created"]) == (
        "def5678",
        "running",
        card.id,
        "2026-10-14T11:00:00Z",
    )
    journal = _person(env, "owner", {"op": "deploy.runs", "project": "platform"})[
        "items"
    ]
    assert [(r["sha"], r["status"], r["created"], r["stopped"]) for r in journal] == [
        ("def5678", "running", "2026-10-14T11:00:00Z", ""),
        ("abc1234", "success", "2026-10-14T10:00:00Z", "2026-10-14T10:00:00Z"),
    ]
    _report(env, _source(runs=[_run(1, created="2026-10-14T10:30:00Z")]))
    mixed = _person(env, "owner", {"op": "deploy.runs"})["items"]
    assert [r["project"] for r in mixed] == ["platform", "alert", "platform"]
    assert (
        _person(env, "owner", {"op": "deploy.runs", "project": "alert"})["items"][0][
            "project"
        ]
        == "alert"
    )


def test_reading_needs_deploys_read(env):
    _report(env, _source(runs=[_run(1)]))
    assert _code(lambda: _person(env, "vera", {"op": "deploy.list"})) == "refused"
    assert _code(lambda: _person(env, "vera", {"op": "deploy.runs"})) == "refused"
    assert len(_person(env, "olga", {"op": "deploy.list"})["envs"]) == 2
    assert len(env.call("root", {"op": "deploy.runs"})["items"]) == 1


@pytest.mark.parametrize(
    "bad",
    [
        _source(project="platform"),
        _source(
            envs=[
                {
                    "env": "test",
                    "workflow": "deploy.sh",
                    "branch": "master",
                    "manual": False,
                }
            ]
        ),
        _source(
            envs=ENVS
            + [{"env": "test", "workflow": "x.yml", "branch": "main", "manual": False}]
        ),
        _source(runs=[_run(1, env="stage")]),
        _source(runs=[_run(1, status="done")]),
        _source(runs=[_run(1, url="http://forge.example.test/run/1")]),
        _source(runs=[_run(1, sha="HEAD")]),
        _source(runs=[_run(1, created="2026-10-14 09:00")]),
        _source(runs=[_run(1, inputs={"confirm": ["deploy"]})]),
        _source(runs=[_run(0)]),
    ],
)
def test_a_malformed_report_is_refused_whole(env, bad):
    assert _code(lambda: _report(env, bad)) == "invalid"
    assert (
        env.store._conn.execute("SELECT COUNT(project) FROM deploy_sources").fetchone()[
            0
        ]
        == 0
    )
