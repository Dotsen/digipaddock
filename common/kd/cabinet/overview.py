"""The overview (remediation spec §9.1, R22): the cabinet's first screen, one aggregate for one person.

``overview.get {project?, period?}`` answers, in this order:

1. ``needs`` — what waits for this person: the open decisions they may take (an agent's question included)
   and the cards in Review they may accept, each with who waits, since when and a link to the very place;
2. ``work`` — cards under way: In Progress, To Do with a request on its way, and Inbox cards whose brief the
   analyst is writing — with the assignee, the stage, how far the run got and the assignee's last real step;
3. ``problems`` — the open crit incidents nobody acknowledged or muted (for who reads the servers), then
   failures (a request that failed with nothing after it) apart from normal waiting (a start that is late, a
   session waiting for a slot, a card blocked by open dependencies); ``servers`` — the "Servers" tile's counts
   (``None`` without ``servers.read``);
4. ``changes`` — what happened in the period: new cards, sent to Review, done, blocked, handed to another
   agent, a brief version — never progress lines or tool calls; ``new`` is past the person's own mark;
5. ``team`` — each agent's state and numbers (``kd.cabinet.team``).

Everything is counted from the cards the person may read (and the project filter) before any number is made
(AC22); spend is not here. ``watermark`` is the journal's seq the answer is current to: the page marks it seen
(``user.mark``), which only moves the person's own mark — it closes no question and no approval.
"""

from datetime import timedelta
import json
from typing import Dict, List, Mapping, Optional

from kd import agents as registry
from kd.cabinet import api, briefs, councils, flow, incidents, problems, stage_gate, store, team, users

PERIODS = {"1d": 1, "7d": 7, "30d": 30}
DELAYED = 120  # seconds a handed-over request may wait for its session before the start counts as late
CHANGES = 50
QUIET = team.QUIET
BRIEF_WORK = ("queued", "in_progress", "waiting_user", "in_council")  # in_council: its critics read
SEEN = "overview_seen"


def _iso(moment) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _age(now: str, then: str) -> float:
    return (users.parse_time(now) - users.parse_time(then)).total_seconds()


def _handle(actor: str) -> str:
    return "@" + actor.split(":", 1)[1] if ":" in actor else actor


def get(ctx, principal, request) -> dict:
    project = request.get("project") or ""
    period = request.get("period") or "1d"
    if period not in PERIODS:
        raise api.ApiError("invalid", f"period is one of {', '.join(PERIODS)}")
    if not isinstance(project, str):
        raise api.ApiError("invalid", "project must be a project id")
    now = ctx.now()
    records = [
        r
        for r in ctx.store.cards()
        if api.visible(ctx, principal, r.project) and (not project or r.project == project)
    ]
    by_id = {r.id: r for r in records}
    cards = [api.compact(r) for r in records]
    runs = runs_of(ctx.store, [r.id for r in records if not r.terminal])

    needs = _needs(ctx, principal, by_id)
    work = _work(ctx.store, records, runs, now)
    problems = ([] if project else _alarms(ctx, principal)) + _problems(ctx, records, runs, now)
    seen = ctx.store.marks(principal.user).get(SEEN, 0)
    since = _iso(users.parse_time(now) - timedelta(days=PERIODS[period]))
    changes = _changes(ctx.store, by_id, since, seen)
    spaces = api.agent_spaces(ctx, principal)
    config = ctx.host_config()
    crew = []
    for name in registry.names(ctx.registry_path):
        found = team.facts(ctx.store, name, cards=cards, spaces=spaces, hosted=config.hosted(name), now=now)
        crew.append({"name": name, **{k: found[k] for k in ("state", "working", "waiting_you", "queued", "failed")}})
    return {
        "needs": needs,
        "work": work,
        "problems": problems,
        "changes": changes,
        "team": crew,
        "servers": incidents.tile(ctx, principal),
        "counts": {
            "needs": len(needs),
            "work": len(work),
            "problems": len(problems),
            "changes_new": sum(c["new"] for c in changes),
        },
        "seen": seen,
        "watermark": ctx.store.last_seq(),
    }


def runs_of(store_, cards: List[str]) -> Dict[str, dict]:
    """Each of ``cards``' newest request (not a stop) with its session's state and heartbeat."""
    rows = store_._conn.execute(
        "SELECT * FROM ("
        "SELECT c.card, c.id AS command, c.kind AS kind, c.state, c.reason, c.error, c.agent, c.updated, s.state AS session, "
        "s.heartbeat AS heartbeat, s.updated AS beat, "
        "ROW_NUMBER() OVER (PARTITION BY c.card ORDER BY c.created DESC, c.rowid DESC) AS n "
        "FROM commands c LEFT JOIN sessions s ON s.id = c.session "
        f"WHERE c.card IN {team.SCOPE} AND c.kind != 'stop') WHERE n = 1",
        (json.dumps(cards),),
    ).fetchall()
    return {row["card"]: dict(row) for row in rows}


def run_state(run: Optional[dict], now: str) -> str:
    """How far a card's newest request got: working (its session beats), stale (started or live, with no fresh
    heartbeat: nothing confirms it works), queued, delayed, failed or none."""
    if run is None:
        return "none"
    live = run["session"] in ("starting", "working")
    if live and _age(now, run["heartbeat"] or run["beat"]) <= team.FRESH:
        return "working"
    if run["state"] == "failed":
        return "failed"
    if run["state"] == "started" or live:
        return "stale"
    if run["state"] == "queued" or run["session"] == "queued":
        return "queued"
    if run["state"] == "delivered":
        return "delayed" if _age(now, run["updated"]) > DELAYED else "queued"
    return "none"


def _needs(ctx, principal, by_id: Mapping) -> List[dict]:
    out = []
    for row in ctx.store._conn.execute(
        "SELECT id, type, card, created, requested_by, decide_permission FROM decisions WHERE state='open' "
        "ORDER BY created"
    ).fetchall():
        record = by_id.get(row["card"])
        if record is None or not api.allowed(ctx, principal, row["decide_permission"], record.project):
            continue
        out.append(
            {
                "kind": row["type"],
                "card": record.id,
                "title": record.title,
                "project": record.project,
                "decision": row["id"],
                "waiting": _handle(row["requested_by"]),
                "since": row["created"],
                "link": f"/cards/{record.id}?decision={row['id']}",
            }
        )
    hidden = stage_gate.hidden_ids(ctx.store)  # a chain plan's stage the coordinator still decides on
    for record in by_id.values():
        if record.status != "Review" or not api.allowed(ctx, principal, "cards.transition", record.project):
            continue
        if record.id in hidden:
            continue
        entered = ctx.store._conn.execute(
            "SELECT MAX(ts) FROM events WHERE card=? AND kind='card.changed' "
            "AND json_extract(payload, '$.after.status')='Review' AND json_extract(payload, '$.before.status')!='Review'",
            (record.id,),
        ).fetchone()[0]
        out.append(
            {
                "kind": "review",
                "card": record.id,
                "title": record.title,
                "project": record.project,
                "decision": "",
                "waiting": record.assignee,
                "since": entered or record.updated,
                "link": f"/cards/{record.id}?tab=result",
            }
        )
    return out


def _last_step(store_, record: store.CardRecord) -> Optional[dict]:
    """The assignee's newest message on the card that is not progress or a tool call."""
    if not record.assignee.startswith("@"):
        return None
    kinds = ",".join("?" * len(QUIET))
    row = store_._conn.execute(
        f"SELECT created, body FROM messages WHERE thread=? AND author=? AND kind NOT IN ({kinds}) "
        "ORDER BY seq DESC LIMIT 1",
        (record.thread, f"agent:{record.assignee[1:]}", *QUIET),
    ).fetchone()
    return {"at": row["created"], "text": row["body"][:140]} if row else None


def _work(store_, records, runs: Mapping, now: str) -> List[dict]:
    out = []
    gathered = councils.open_map(store_)  # a card under its council says so
    for record in records:
        run = runs.get(record.id)
        stage = record.status
        if record.status == "Inbox":
            state = briefs.state(store_, record)["state"]
            if state not in BRIEF_WORK:
                continue
            stage = state
        elif record.status == "To Do":
            if run is None or run["state"] not in ("queued", "delivered", "started"):
                continue
        elif record.status != "In Progress":
            continue
        out.append(
            {
                "card": record.id,
                "title": record.title,
                "project": record.project,
                "assignee": record.assignee,
                "status": record.status,
                "stage": stage,
                "run": run_state(run, now),
                "last_step": _last_step(store_, record),
                **({"council": gathered[record.id][0]} if record.id in gathered else {}),
            }
        )
    return out


def _alarms(ctx, principal) -> List[dict]:
    """The open crit incidents nobody acknowledged or muted, first among the problems."""
    return [{"kind": "incident", "incident": i["id"], "server": i["server"], "check": i["check"],
             "subject": i["subject"], "value": i["value"], "since": i["opened_at"],
             **({"members": i["members"], "servers": i["servers"], "group": i["group"]} if "members" in i else {})}
            for i in incidents.collapse(incidents.alarms(ctx, principal))]  # one row per group


def _problems(ctx, records, runs: Mapping, now: str) -> List[dict]:
    out = []
    for record in records:
        if record.terminal:
            continue
        run = runs.get(record.id)
        row = {"card": record.id, "title": record.title, "project": record.project, "agent": record.assignee}
        state = run_state(run, now)
        if state == "failed":
            out.append({**row, "kind": "error", "reason": run["reason"] or run["error"], "since": run["updated"],
                        "agent": "@" + run["agent"]})
        elif state == "delayed":
            out.append({**row, "kind": "waiting", "reason": "delayed", "since": run["updated"]})
        elif run is not None and run["session"] == "queued":
            out.append({**row, "kind": "waiting", "reason": "slot", "since": run["updated"]})
        if record.status == "Blocked":
            deps = flow.open_dependencies(ctx, record)
            if deps:
                out.append({**row, "kind": "waiting", "reason": "deps", "since": record.updated, "deps": deps})
    by_id = {r.id: r for r in records if not r.terminal}
    for problem in problems.open_rows(ctx.store, list(by_id)):  # work that stopped with nobody on it
        record = by_id[problem["card"]]
        out.append({"card": record.id, "title": record.title, "project": record.project,
                    "agent": "@" + problem["agent"] if problem["agent"] else record.assignee, "kind": "stuck",
                    "reason": problem["kind"], "since": problem["created"]})
    return out


def _change(event: Mapping, payload: Mapping) -> Optional[str]:
    if event["kind"] == "card.created":
        return "created"
    if event["kind"] == "brief.version":
        return "brief"
    before, after = payload.get("before") or {}, payload.get("after") or {}
    if before.get("status") != after.get("status"):
        moved = {"Review": "review", "Done": "done", "Blocked": "blocked", "Cancelled": "cancelled"}.get(
            after.get("status"))
        if moved:
            return moved
    if before.get("assignee") != after.get("assignee") and event["actor"].startswith("agent:"):
        return "handoff"
    return None


def _changes(store_, by_id: Mapping, since: str, seen: int) -> List[dict]:
    """The period's changes, newest first: the journal read back from its end, until the period's start."""
    out = []
    for event in store_._conn.execute(
        "SELECT seq, ts, actor, kind, card, payload FROM events WHERE card != '' "
        "AND kind IN ('card.created', 'card.changed', 'brief.version') ORDER BY seq DESC"
    ):
        if event["ts"] < since:
            break
        record = by_id.get(event["card"])
        if record is None:
            continue
        payload = json.loads(event["payload"])
        kind = _change(event, payload)
        if kind is None:
            continue
        item = {
            "seq": event["seq"],
            "kind": kind,
            "card": record.id,
            "title": record.title,
            "actor": event["actor"],
            "at": event["ts"],
            "new": event["seq"] > seen,
        }
        if kind == "handoff":
            item["to"] = (payload.get("after") or {}).get("assignee", "")
        if kind == "brief":
            item["version"] = payload.get("version")
        out.append(item)
        if len(out) == CHANGES:
            break
    return out


def _mark(ctx, principal, request) -> dict:
    """``user.mark {name, value}`` → ``{marks}``: the person's own mark moves up (never back)."""
    name, value = request.get("name"), request.get("value")
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= ctx.store.last_seq():
        raise api.ApiError("invalid", "value must be a journal seq")
    try:
        ctx.store.mark(principal.user, name, value)
    except store.Invalid as exc:
        raise api.ApiError("invalid", str(exc))
    return {"marks": ctx.store.marks(principal.user)}


OPS = {
    "overview.get": (frozenset({"user"}), get),
    "user.mark": (frozenset({"user"}), _mark),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
