"""The pipeline's read-only inventory: every open card with something to look at, classed.

``collect`` reads the base and nothing else — no transaction, no event, no command: a card is listed once with
every class that fits it, oldest first, so the owner (``kd-cabinet pipeline inventory``) or BOSS sees in one page
what the sweep and the problems would otherwise surface one by one.

Classes: ``no_next_step`` (a hosted holder owes an outcome and nobody moves the card), ``problem_open`` (its open
problems' kinds), ``wait_stale`` (an open wait whose source is no longer open — the sweep will move it),
``blocked_unexplained`` (Blocked with no open dependency), ``stages_unmaterialized`` (the approved brief names a
stage without its child), ``aggregate`` (open children of its stages), ``release_degraded`` (the release's state).
"""

from typing import List

from kd.cabinet import briefs, outcomes, problems, releases, stages, waits

CLASSES = ("no_next_step", "problem_open", "wait_stale", "blocked_unexplained", "stages_unmaterialized", "aggregate",
           "release_degraded")
BLOCKED = "Blocked"
EMPTY = "nothing to look at"


def _stale_waits(ctx, card_id: str) -> List[dict]:
    rows = ctx.store._conn.execute("SELECT * FROM waits WHERE card=? AND state='open' ORDER BY created, id",
                                   (card_id,)).fetchall()
    out = []
    for row in rows:
        wait = dict(row)
        state = waits.source_state(ctx, wait)
        if state != "open":
            out.append({"id": wait["id"], "kind": wait["kind"], "ref": wait["ref"], "source": state})
    return out


def _stages_missing(store, card_id: str) -> List[str]:
    version = briefs.latest(store, card_id)
    if version is None or not version.get("approved_by") or not version.get("stages"):
        return []
    made = {r["key"] for r in stages.rows(store, card_id) if r["child"]}  # a finished child is a child too
    return [st["key"] for st in version["stages"] if st["key"] not in made]


def _item(ctx, record) -> dict:
    s, classes, detail = ctx.store, [], {}
    agent = record.assignee[1:] if record.assignee.startswith("@") else ""
    if agent and ctx.host_config().hosted(agent) and outcomes.owes_outcome(record, agent) \
            and outcomes.next_state(ctx, record) == "missing":
        classes.append("no_next_step")
    open_problems = problems.open_for(s, record.id)
    if open_problems:
        classes.append("problem_open")
        detail["problems"] = [p["kind"] for p in open_problems]
    stale = _stale_waits(ctx, record.id)
    if stale:
        classes.append("wait_stale")
        detail["waits"] = stale
    if record.status == BLOCKED and not any(outcomes._not_done(s, d) for d in record.dependencies):
        classes.append("blocked_unexplained")
    missing = _stages_missing(s, record.id)
    if missing:
        classes.append("stages_unmaterialized")
        detail["stages_missing"] = missing
    children = stages.open_children(s, record.id)
    if children:
        classes.append("aggregate")
        detail["open_children"] = len(children)
    release = releases.state(s, record.id)
    if release["state"] == "degraded":
        classes.append("release_degraded")
        detail["release"] = {"rev": release["rev"], "why": release["why"], "at": release["at"]}
    return {"card": record.id, "status": record.status, "assignee": record.assignee, "project": record.project,
            "title": record.title, "classes": classes, "detail": detail}


def collect(ctx) -> List[dict]:
    """One item per open card with at least one class, oldest card first. Reads only."""
    s = ctx.store
    rows = s._conn.execute("SELECT id FROM cards WHERE status NOT IN ('Done', 'Cancelled') ORDER BY n").fetchall()
    out = []
    for (card_id,) in rows:
        record = s.card(card_id)
        if record.terminal:
            continue
        item = _item(ctx, record)
        if item["classes"]:
            out.append(item)
    return out


def _detail(item: dict) -> str:
    parts = []
    detail = item["detail"]
    if "problems" in detail:
        parts.append("problems " + ", ".join(detail["problems"]))
    if "waits" in detail:
        parts.append("waits " + ", ".join(f"{w['kind']} {w['ref']} ({w['source']})" for w in detail["waits"]))
    if "stages_missing" in detail:
        parts.append("stages without a child " + ", ".join(detail["stages_missing"]))
    if "open_children" in detail:
        parts.append(f"open children {detail['open_children']}")
    if "release" in detail:
        parts.append(f"release {detail['release']['rev']} degraded: {detail['release']['why']}".rstrip(": "))
    return "; ".join(parts)


def render(items: List[dict]) -> str:
    """One line per card: id, status, holder, classes, then what the classes rest on."""
    if not items:
        return EMPTY
    lines = []
    for item in items:
        line = f"{item['card']} · {item['status']} · {item['assignee']} · {', '.join(item['classes'])}"
        tail = _detail(item)
        lines.append(f"{line} — {tail}" if tail else line)
    return "\n".join(lines)
