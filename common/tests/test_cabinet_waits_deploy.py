"""A turn ends waiting for a deploy (``--deploy <project>:<env>[@<sha>]``, KD-9443 stage ``wait``): the forge
reader's report resolves the wait when a finished run of the environment holds the card's commit — success wakes the
card, failure wakes it with the run and puts the run into the owner's inbox."""

import json
import sys
from pathlib import Path

import pytest
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import api, continuations, deploys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cabinet_outcomes import begin, end, finish  # noqa: E402

HEAD = "b" * 40  # the card's PR head; kd-merge merges with a merge commit
MERGE = "c" * 40
OLD = "a" * 40
ENVS = [
    {
        "env": "staging",
        "workflow": "deploy-staging.yml",
        "branch": "main",
        "manual": False,
    },
    {"env": "prod", "workflow": "deploy-prod.yml", "branch": "main", "manual": True},
]


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    env.ctx.host_config = lambda: session_config.HostConfig(
        {"boss": 1, "dev": 2, "analyst": 1}
    )
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce")
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
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    for command in env.store.commands(card="KD-50", states=["queued"]):
        for state in ("delivered", "started", "done"):
            env.store.command_state(command["id"], state)
    report(env)
    env.store._conn.execute(
        "INSERT INTO prs (repo, number, card, project, head, state, updated) VALUES (?,?,?,?,?,?,?)",
        ("o/api", 7, "KD-50", "api", HEAD, "merged", "2026-10-14T09:00:00Z"),
    )
    return env


def run(n, env="staging", status="success", sha=MERGE, created="2026-10-14T10:00:00Z"):
    return {
        "run": n,
        "number": n,
        "env": env,
        "status": status,
        "event": "push",
        "sha": sha,
        "ref": "main",
        "url": f"https://forge.example.test/o/api/actions/runs/{n}",
        "actor": "dev",
        "inputs": {},
        "title": "KD-50: fix",
        "created": created,
        "started": created,
        "stopped": "",
    }


def report(cab, runs=(), includes=(), envs=None):
    return cab.call(
        "digi-forge",
        {
            "op": "deploy.report",
            "sources": [
                {
                    "project": "api",
                    "repo": "o/api",
                    "envs": ENVS if envs is None else envs,
                    "runs": list(runs),
                    "includes": list(includes),
                    "error": "",
                }
            ],
        },
    )


def waiting(cab, *targets):
    record = cab.store.card("KD-50")
    command, session = begin(cab, record)
    finish(
        cab,
        record,
        outcome="wait_dependency",
        note="waits for the deploy",
        deploys=list(targets),
    )
    end(cab, command, session)
    return command


def waits_of(cab, command):
    return [
        dict(r)
        for r in cab.store._conn.execute(
            "SELECT * FROM waits WHERE command=?", (command,)
        )
    ]


def continues(cab):
    return [
        c["text"]
        for c in cab.store.commands(card="KD-50", states=["queued"])
        if c["kind"] == "continue"
    ]


def code(call):
    with pytest.raises(api.ApiError) as exc:
        call()
    return exc.value.kind


def test_the_wait_is_recorded_on_the_cards_merged_pr_head(cab):
    command = waiting(cab, "api:staging")
    [wait] = waits_of(cab, command)
    assert (wait["kind"], wait["ref"], wait["state"]) == (
        "deploy",
        f"api:staging@{HEAD}",
        "open",
    )
    explicit = waiting(cab, f"api:prod@{OLD}")
    assert waits_of(cab, explicit)[0]["ref"] == f"api:prod@{OLD}"


def test_an_unknown_project_or_environment_is_refused(cab):
    record = cab.store.card("KD-50")
    begin(cab, record)
    for target in ("alert:test", "api:stage", "api", "api:staging@abc"):
        assert (
            code(
                lambda: finish(
                    cab, record, outcome="wait_dependency", note="x", deploys=[target]
                )
            )
            == "invalid"
        )
    assert (
        code(
            lambda: finish(
                cab, record, outcome="continue", note="x", deploys=["api:staging"]
            )
        )
        == "invalid"
    )


def test_a_card_without_a_merged_pr_names_the_commit(cab):
    cab.store._conn.execute("UPDATE prs SET state='open'")
    record = cab.store.card("KD-50")
    begin(cab, record)
    assert (
        code(
            lambda: finish(
                cab,
                record,
                outcome="wait_dependency",
                note="x",
                deploys=["api:staging"],
            )
        )
        == "refused"
    )


def test_a_successful_run_of_the_commit_wakes_the_card(cab):
    command = waiting(cab, f"api:staging@{MERGE}")
    report(cab, [run(1, status="running")])
    assert waits_of(cab, command)[0]["state"] == "open" and continues(cab) == []
    assert report(cab, [run(1)])["woken"] == 1
    assert waits_of(cab, command)[0]["state"] == "resolved"
    assert continues(cab) == [continuations.RESOLVED.format(id="KD-50")]


def test_a_run_without_the_commit_does_not_wake_it(cab):
    command = waiting(cab, "api:staging")
    report(
        cab, [run(1, sha=OLD), run(2, env="prod", sha=HEAD)]
    )  # another commit; the commit on another env
    report(cab, [], includes=[{"run": 1, "sha": HEAD, "included": False}])
    assert waits_of(cab, command)[0]["state"] == "open" and continues(cab) == []


def test_a_run_whose_commit_holds_the_head_wakes_it(cab):
    command = waiting(cab, "api:staging")
    report(cab, [run(1)])  # the merge commit: the reader has not asked Forgejo yet
    assert waits_of(cab, command)[0]["state"] == "open"
    report(cab, [], includes=[{"run": 1, "sha": HEAD, "included": True}])
    assert waits_of(cab, command)[0]["state"] == "resolved" and len(continues(cab)) == 1


def test_a_run_finished_before_the_turn_ended_resolves_on_activate(cab):
    report(cab, [run(1, sha=HEAD)])
    command = waiting(cab, "api:staging")
    assert waits_of(cab, command)[0]["state"] == "resolved" and len(continues(cab)) == 1


def test_a_cancelled_run_decides_nothing(cab):
    command = waiting(cab, "api:staging")
    report(cab, [run(1, sha=HEAD, status="cancelled")])
    assert waits_of(cab, command)[0]["state"] == "open"


def test_a_failed_run_wakes_the_card_with_its_log_and_alerts_the_owner(cab):
    command = waiting(cab, "api:staging")
    report(cab, [run(4, sha=HEAD, status="failure")])
    [wait] = waits_of(cab, command)
    url = "https://forge.example.test/o/api/actions/runs/4"
    assert (
        wait["state"] == "rejected"
        and wait["reason"] == f"deploy api:staging failed: run #4 {url}"
    )
    assert continues(cab) == [
        continuations.ENDED.format(id="KD-50", state=wait["reason"])
    ]
    notice = cab.store._conn.execute(
        "SELECT payload FROM messages WHERE thread=? AND kind='notice'",
        (cab.store.card("KD-50").thread,),
    ).fetchall()
    assert [json.loads(n[0])["key"] for n in notice] == ["notice.deploy.failed"]
    owner = api.Principal("user", -1, "owner")
    [alert] = api.dispatch(cab.ctx, owner, {"op": "deploy.alerts"})["items"]
    assert (
        alert["project"],
        alert["env"],
        alert["run"],
        alert["url"],
        alert["cards"],
    ) == ("api", "staging", 4, url, ["KD-50"])
    assert api.inbox_count(cab.ctx, owner) >= 1
    before = api.inbox_count(cab.ctx, owner)
    assert (
        api.dispatch(
            cab.ctx, owner, {"op": "deploy.alert.seen", "project": "api", "run": 4}
        )["seen"]
        is True
    )
    assert api.dispatch(cab.ctx, owner, {"op": "deploy.alerts"})["items"] == []
    assert api.inbox_count(cab.ctx, owner) == before - 1


def test_alerts_are_for_readers_of_deploys(cab):
    vera = api.Principal("user", -1, "vera")
    assert (
        code(lambda: api.dispatch(cab.ctx, vera, {"op": "deploy.alerts"})) == "refused"
    )
    assert (
        code(
            lambda: api.dispatch(
                cab.ctx, vera, {"op": "deploy.alert.seen", "project": "api", "run": 1}
            )
        )
        == "refused"
    )


def test_the_page_lists_the_cards_waiting_for_an_environment(cab):
    waiting(cab, "api:staging")
    owner = api.Principal("user", -1, "owner")
    rows = {
        e["env"]: e for e in api.dispatch(cab.ctx, owner, {"op": "deploy.list"})["envs"]
    }
    assert (
        rows["staging"]["waiting"] == [{"card": "KD-50", "sha": HEAD}]
        and rows["prod"]["waiting"] == []
    )
    report(cab, [run(1, sha=HEAD)])
    rows = {
        e["env"]: e for e in api.dispatch(cab.ctx, owner, {"op": "deploy.list"})["envs"]
    }
    assert rows["staging"]["waiting"] == []


def test_the_reader_is_told_what_to_check(cab):
    waiting(cab, "api:staging")
    assert cab.call("digi-forge", {"op": "deploy.watch"})["watch"] == [
        {"project": "api", "env": "staging", "sha": HEAD, "checked": []}
    ]
    report(cab, [run(1)], includes=[{"run": 1, "sha": HEAD, "included": False}])
    assert cab.call("digi-forge", {"op": "deploy.watch"})["watch"][0]["checked"] == [1]
    assert code(lambda: cab.call("digi-dev", {"op": "deploy.watch"})) == "refused"


def test_an_environment_the_report_drops_needs_routing(cab):
    command = waiting(cab, "api:prod")
    report(cab, envs=ENVS[:1])
    assert waits_of(cab, command)[0]["state"] == "needs_routing"


def test_includes_are_checked(cab):
    for bad in (
        {"run": 0, "sha": HEAD, "included": True},
        {"run": 1, "sha": "b" * 7, "included": True},
        {"run": 1, "sha": HEAD, "included": "yes"},
        {"run": 1, "sha": HEAD},
    ):
        assert code(lambda: report(cab, includes=[bad])) == "invalid"


def test_the_wait_ref_is_parsed_once(cab):
    assert deploys.wait_state(cab.store, "api:staging") == "needs_routing"
    assert deploys.wait_state(cab.store, f"api:staging@{HEAD}") == "open"
