"""A cabinet with a council to gather: the real specialization registry, the analyst's worker pool, two
critics' models, and the moves a council's tests repeat — the analyst's hand-in, a critic's job ending."""

import secrets
from pathlib import Path

from cabinet_env import CabinetEnv

from kd import session_config
from kd.cabinet import api, council_rounds, councils, jobs, users

REPO = Path(__file__).resolve().parents[2]
HOSTED = session_config.HostConfig({"analyst": 1, "dev": 2, "boss": 1}, 600)
OR = {
    "id": "or-gpt5",
    "provider": "openrouter",
    "base_url": "",
    "upstream": "openai/gpt-5",
    "title": "GPT-5",
    "price_in": 2,
    "price_out": 10,
    "price_cache_read": 0.2,
    "price_cache_write": 2.5,
    "context": 200000,
}
TWO = {"brief": {"critics": ["claude:opus", "or-gpt5"], "rounds": 3}}
CODE = {"code": {"critics": ["claude:opus", "or-gpt5"], "rounds": 3, "min_lines": 100}}
USED = {
    "models": {
        "claude-opus-5-5": {
            "in": 5,
            "out": 900,
            "cache_read": 1000,
            "cache_write": 200,
            "cost": 0.12,
        }
    },
    "requests": 4,
}


def make(tmp_path, hosted=HOSTED):
    env = CabinetEnv(tmp_path, repo_root=REPO)
    api.install_host(env.ctx, lambda: hosted)
    users.Users(env.store).create(
        "owner", "Owner", roles=[("owner", "*")], actor="root"
    )
    env.llm_dir = tmp_path / "llm"
    env.llm_dir.mkdir(exist_ok=True)
    (env.llm_dir / "openrouter.env").write_text(
        "ANTHROPIC_AUTH_TOKEN=sk-or-test\n", encoding="utf-8"
    )
    env.ctx.llm_env_dir = str(env.llm_dir)
    env.store.create_llm_model(OR, actor="user:owner")
    return env


def draft(env, council=TWO, **over):
    """A card the analyst writes the brief of, with its own council."""
    card = env.card(
        **{
            "status": "Inbox",
            "assignee": "@analyst",
            "labels": ["spec:task-brief"],
            **over,
        }
    )
    if council is not None:
        env.store.edit_card(card.id, None, {"council": council}, actor="user:owner")
    return env.store.card(card.id)


def handin(
    env, card_id, description="Goal: a report.", ac=("There is a report",), **more
):
    """The analyst's hand-in as ``kd-task handoff`` writes it: the brief, ``brief-ready``, the card to @dev."""
    changes = {
        "description": description,
        "acceptance": [{"text": text, "checked": False} for text in ac],
        "assignee": "@dev",
        "labels_remove": ["spec:task-brief"],
        "labels_add": ["spec:implementer", "brief-ready"],
        **more,
    }
    return env.store.edit_card(card_id, None, changes, actor="agent:analyst")


def coding(env, council=CODE, **over):
    """A card the developer works on, with its own council of the code."""
    card = env.card(
        **{
            "status": "In Progress",
            "assignee": "@dev",
            "labels": ["spec:implementer"],
            "project": "web",
            **over,
        }
    )
    if council is not None:
        env.store.edit_card(card.id, None, {"council": council}, actor="user:owner")
    return env.store.card(card.id)


def pull(card_id, head="a" * 40, lines=150, **over):
    """A pull request as the forge executor publishes it: ``lines`` changed lines of code, and tests beside."""
    pr = {
        "repo": "acme/web",
        "project": "web",
        "number": 7,
        "card": card_id,
        "title": f"{card_id}: a change",
        "url": "https://forge.example.test/acme/web/pulls/7",
        "head": head,
        "state": "open",
        "files": [
            {
                "path": "src/app.py",
                "status": "modified",
                "additions": lines,
                "deletions": 0,
            },
            {
                "path": "tests/test_app.py",
                "status": "added",
                "additions": 400,
                "deletions": 0,
            },
        ],
        "cage": [],
        "diff": "",
        "truncated": False,
        "release": False,
        "target": "main",
        "effect": "",
        "checks": [],
    }
    pr.update(over)
    return pr


def head(env, pr):
    """The council's part of a publish: the pull request's head reaches the card's council of the code."""
    return council_rounds.on_pull_head(env.ctx, env.store.card(pr["card"]), pr)


def publish(env, pr):
    """``kd-forge publish``: the forge executor puts the pull request on the board."""
    return env.call("digi-forge", {"op": "pr.publish", "pr": pr})


def pr_decisions(env, card_id, state="open"):
    found = env.call("root", {"op": "decision.list", "card": card_id, "state": state})[
        "decisions"
    ]
    return [d for d in found if d["type"] == "pr_approve"]


def council_of(env, card_id, kind="brief"):
    return councils.latest(env.store, card_id, kind)


def runs_of(env, card_id, round_no=None, kind="brief"):
    found = council_of(env, card_id, kind)
    return councils.runs(env.store, found["id"], round_no)


def finding(severity="major", n=1):
    return {
        "severity": severity,
        "location": f"AC {n}",
        "evidence": "the brief names no timeout",
        "scenario": "a request hangs and the owner waits forever",
        "recommendation": "name the timeout",
    }


def block(verdict="ready", findings=(), questions=(), **more):
    return {
        "status": "done",
        "verdict": verdict,
        "findings": list(findings),
        "questions": list(questions),
        "missing_inputs": [],
        **more,
    }


def thorough(n=30):
    """A thorough critic's block in Russian — ``n`` findings, each read at a glance: about 40 KiB at 30."""
    one = {
        "severity": "major",
        "location": "Критерий приёмки 3, второй абзац раздела «Этапы»",
        "evidence": "В ТЗ не назван таймаут запроса к внешнему сервису: "
        + "текст карточки и код клиента расходятся. " * 4,
        "scenario": "Запрос зависает, владелец ждёт ответа и не видит причины: "
        + "очередь заданий растёт без предела. " * 4,
        "recommendation": "Назвать таймаут и поведение при его истечении: "
        + "повтор, отказ или сообщение владельцу. " * 3,
    }
    return block("needs_changes", [dict(one) for _ in range(n)])


def report(env, *items):
    return env.call("root", {"op": "host.report", "items": list(items)})


def start(env, job_id, slot=None):
    slot = slot or f"{jobs.row(env.store, job_id)['agent']}/worker1"
    report(env, {"type": "job", "job": job_id, "state": "starting", "slot": slot})


def end(env, job_id, result=None, state="done", **more):
    """The host's report of a critic's job: started, then ended with ``result`` (the final block)."""
    if jobs.row(env.store, job_id)["state"] in ("queued", "delivered"):
        start(env, job_id)
    item = {"type": "job", "job": job_id, "state": state, "usage": USED, **more}
    if result is not None:
        item["result"] = result
    report(env, item)


def play(env, card_id, *results, kind="brief"):
    """End the open round's runs in their order, each with its block (``None`` — the job ends without one)."""
    found = council_of(env, card_id, kind)
    for run, result in zip(runs_of(env, card_id, found["round"], kind), results):
        if result is None:
            end(env, run["job"], state="no-result", reason="no final block")
        else:
            end(env, run["job"], result)
    return council_of(env, card_id, kind)


def reviews(env, card_id, kind="review"):
    thread = env.store.card(card_id).thread
    return [m for m in env.store.thread_messages(thread, 0) if m["kind"] == kind]


def wakes(env, agent, states=("queued", "delivered", "started")):
    return [
        c
        for c in env.store.commands(agent=agent, limit=200)
        if c["kind"] == "signal" and c["state"] in states
    ]


def started(env, card_id, agent="analyst", kind="signal"):
    """A turn of ``agent`` running in the card's thread: a command the host delivered and started."""
    record = env.store.card(card_id)
    session = env.store.session_for(record.thread, agent)["id"]
    command = env.store.enqueue(
        {
            "key": f"turn:{record.thread}:{agent}:{env.store._clock()}:{kind}",
            "agent": agent,
            "thread": record.thread,
            "card": record.id,
            "kind": kind,
            "text": f"[test] {record.id} {kind} {secrets.token_hex(4)}",
        }
    )["id"]
    for state in ("delivered", "started"):
        env.store.command_state(command, state, session=session)
    return command


def reply(env, card_id, answers, note="", agent="analyst", **more):
    """``kd-work council-reply``: ``answers`` — ``{finding id: answer}`` or ``(answer, card)`` for «later»."""
    items = []
    for finding, answer in answers.items():
        item = {"finding": finding, "answer": answer}
        if isinstance(answer, tuple):
            item["answer"], item["card"] = answer
        items.append(item)
    request = {
        "op": "council.reply",
        "agent": agent,
        "card": card_id,
        "answers": items,
        "note": note,
        **more,
    }
    return env.call("digi", request)


def view(env, card_id, user="digi", **more):
    return env.call(
        user, {"op": "council.view", "agent": "analyst", "card": card_id, **more}
    )


def handoff(env, card_id, agent="analyst", to="@dev", **more):
    """``kd-task handoff … --add-label brief-ready`` through the worker's API (the council's gate is there)."""
    record = env.store.card(card_id)
    request = {
        "op": "worker.handoff",
        "agent": agent,
        "card": card_id,
        "revision": record.revision,
        "to": to,
        "add_label": ["brief-ready"],
        "spec": "implementer",
        **more,
    }
    return env.call("digi", request)


def edit(env, card_id, agent="analyst", **changes):
    record = env.store.card(card_id)
    request = {
        "op": "worker.edit",
        "agent": agent,
        "card": card_id,
        "revision": record.revision,
        **changes,
    }
    return env.call("digi", request)
