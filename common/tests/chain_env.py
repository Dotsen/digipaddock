"""A cabinet with chain plans for the tests: one role, one repository, the pipeline in enforce."""

import json

import council_env as ce
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import api, stage_gate, stages

A = {
    "key": "a",
    "title": "Модель",
    "role": "@dev",
    "spec": "implementer",
    "description": "Схема.",
    "acceptance": ["Тест проходит"],
    "depends": [],
    "project": "",
}
B = {**A, "key": "b", "title": "Веб"}
C = {**A, "key": "c", "title": "Отчёт", "depends": ["a"]}
OPS = {**A, "key": "ops", "title": "Выкладка", "role": "@sysadmin", "spec": ""}
SPECS = {
    "@dev": frozenset({"implementer", "docs"}),
    "@analyst": frozenset({"task-brief"}),
    "@sysadmin": frozenset(),
    "@boss": frozenset(),
}
HOSTED = session_config.HostConfig(
    {"analyst": 1, "dev": 2, "boss": 1, "sysadmin": 1}, 600
)
REPO = "example/digipaddock"
# a second repository the plan's stages commit into: its pull request of the plan's card
WEB = {"repo": "example/web", "project": "web", "number": 3}


def cabinet(tmp_path, *, repo=True, mode="enforce", chain=True) -> CabinetEnv:
    env = ce.make(
        tmp_path, hosted=HOSTED
    )  # the repository's own registries: a reviewer's job is built from them
    env.ctx._specs = SPECS
    if repo:
        bind(env)
    pipeline(env, mode=mode, chain=chain)
    return env


def bind(env, registry=None) -> None:
    """The repository registry (root's file) — the truth of which project has a repository, whether the cabinet
    bound it or root did by hand (then the project's own row says nothing)."""
    found = {
        "digipaddock": {"repo": REPO, "dir": "work/digipaddock"},
        "web": {"repo": WEB["repo"], "dir": "work/web"},
    }
    with open(env.ctx.repos_path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(found) if registry is None else registry)


def pipeline(env, *, mode="enforce", chain=True) -> None:
    config = pipeline_config.Config(
        mode=mode, since="2000-01-01T00:00:00Z", chain=chain
    )
    env.ctx.pipeline = lambda: config
    env.store._conn.execute(
        "UPDATE meta SET value='2000-01-01T00:00:00Z' WHERE key='pipeline_since'"
    )


def owner():
    return api.Principal("user", -1, "owner")


def planned(env, stage_list=(A, B)):
    """A card the analyst handed to @dev in Inbox with a ready brief of these stages (v1)."""
    card = env.card(status="Inbox", assignee="@analyst", labels=["spec:task-brief"])
    return env.store.edit_card(
        card.id,
        None,
        {
            "description": "Цель.",
            "acceptance": [{"text": "Всё вместе работает", "checked": False}],
            "stages": list(stage_list),
            "assignee": "@dev",
            "labels_remove": ["spec:task-brief"],
            "labels_add": ["spec:implementer", "brief-ready"],
        },
        actor="agent:analyst",
    )


def act(env, card_id, action, **extra):
    revision = env.store.card(card_id).revision
    return api.dispatch(
        env.ctx,
        owner(),
        {
            "op": "user.card.act",
            "card": card_id,
            "action": action,
            "revision": revision,
            **extra,
        },
    )


def children(env, parent_id):
    return {
        r["key"]: env.store.card(r["child"])
        for r in stages.rows(env.store, parent_id)
        if r["child"]
    }


def started(env, stage_list=(A, B)):
    """A plan whose brief the owner approved: ``(parent, children by key)``."""
    card = planned(env, stage_list)
    act(env, card.id, "start", brief_version=1)
    return env.store.card(card.id), children(env, card.id)


def queued(env, agent):
    return [c["card"] for c in env.store.commands(agent=agent, states=["queued"])]


CRITICS = {"code": {"critics": ["claude:opus", "or-gpt5"], "rounds": 3, "min_lines": 0}}


def plan_pull(parent_id, head, **over):
    """A pull request of the plan's parent card, as the forge executor publishes it: the one in the plan's own
    repository, or — ``**WEB`` — in another repository its stages commit into."""
    return ce.pull(parent_id, head=head, **{"repo": REPO, "project": "digipaddock", **over})


def publish(env, parent_id, head, **over):
    return ce.publish(env, plan_pull(parent_id, head, **over))


def close(env, card):
    """The card Done as a person accepts it."""
    return env.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")


def later(env, minutes=0, seconds=0):
    from datetime import datetime, timezone

    stamp = env.when().timestamp() + minutes * 60 + seconds
    env.now = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def handin(env, stage, head=None):
    """The stage's agent publishes ``head`` into the plan's pull request and hands the stage in (Review); the
    stage's hand-in as the cabinet recorded it."""
    if head is not None:
        publish(env, stages.parent_of(env.store, stage.id), head)
    if env.store.card(stage.id).status != "In Progress":
        env.store.edit_card(
            stage.id, None, {"status": "In Progress"}, actor="agent:dev"
        )
    env.store.edit_card(stage.id, None, {"status": "Review"}, actor="agent:dev")
    return stage_gate.latest(env.store, stage.id)


def return_to_work(env, stage):
    return env.store.edit_card(
        stage.id, None, {"status": "In Progress"}, actor="user:owner"
    )


def boss_signals(env):
    found = env.store.commands(agent="boss", states=["queued"])
    return [c["text"] for c in reversed(found) if c["text"].startswith("[stage]")]


def notices_of(env, card_id):
    return [m["payload"] for m in env.store.messages(card_id) if m["kind"] == "notice"]


# --- the gate: the review of a hand-in and the coordinator's decision
READY = {"verdict": "ready", "findings": []}


def latest(cab, card):
    return stage_gate.latest(cab.store, card.id)


def call(cab, op, stage, **fields):
    request = {
        "op": op,
        "agent": "boss",
        "task": stage.id,
        "revision": cab.store.card(stage.id).revision,
        **fields,
    }
    return cab.call("digi", request)


def done_work(cab, stage, tick=True):
    """The stage's agent finished its turn: the criteria ticked, a report in the thread; the report's seq."""
    if tick:
        ticked = [
            {"text": a.text, "checked": True}
            for a in cab.store.card(stage.id).acceptance
        ]
        cab.store.edit_card(stage.id, None, {"acceptance": ticked}, actor="agent:dev")
    cab.store._conn.execute(
        "UPDATE commands SET state='done' WHERE card=? AND agent='dev'", (stage.id,)
    )
    return cab.store.add_message(
        stage.id, author="agent:dev", kind="text", body="сделано, тесты прошли"
    )["seq"]


def reviewed(cab, stage, head, block=READY, tick=True):
    """The stage handed in on ``head`` and its reviewer's block in: ``(hand-in, the report's seq)``."""
    seq = done_work(cab, stage, tick)
    row = handin(cab, stage, head)
    ce.end(cab, row["job"], block)
    return latest(cab, stage), seq


def passing(cab, stage, seq):
    return call(
        cab,
        "boss.stage-pass",
        stage,
        reason="критерии закрыты",
        source={"card": stage.id, "seq": seq},
    )
