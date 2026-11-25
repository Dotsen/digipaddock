"""The state of a card's release, derived from the facts.

Nothing stores it: ``state`` reads the card's thread — the forge's notices (merged, released, degraded, refused)
and the health check's (``notice.release.healthy|degraded``) — and the operations the release asked the
factory for (keys ``release:<rev>…``). ``merged`` → ``code_switched`` (released, no health yet) → ``activating``
(healthy, operations still running) → ``healthy`` | ``degraded`` (a degraded notice, or an operation of the release
failed); ``refused`` when the forge refused the release.

A degraded release is one problem ``release_degraded`` on the card, held by its lead (the card's assignee): BOSS
is signalled, and again every ``NEXT_CHECK`` minutes by the sweep until a healthy notice of the same revision or
a later release resolves it (``on_event``).
"""

from typing import List, Mapping, Optional

from kd.cabinet import commands, problems, schema, users as people

FORGE = {"notice.forge.merged": "merged", "notice.forge.released": "code_switched",
         "notice.forge.release_degraded": "degraded", "notice.forge.release_refused": "refused"}
HEALTH = {"notice.release.healthy": "healthy", "notice.release.degraded": "degraded"}
KEYS = tuple(FORGE) + tuple(HEALTH)
DEGRADED_KEYS = ("notice.forge.release_degraded", "notice.release.degraded")
STATES = ("none", "merged", "code_switched", "activating", "healthy", "degraded", "refused")
KIND = "release_degraded"
NEXT_CHECK = 30  # minutes between BOSS's looks at a degraded release
BOSS = "boss"
OPEN = ("pending", "running")


def _notices(store, card_id: str) -> List[dict]:
    out = []
    for message in store.messages(card_id):
        payload = message.get("payload") or {}
        if message["kind"] == "notice" and payload.get("key") in KEYS:
            params = payload.get("params") or {}
            out.append({"key": payload["key"], "rev": str(params.get("rev") or ""),
                        "deploy_rev": str(params.get("deploy_rev") or ""),
                        "host": str(params.get("host") or ""), "why": str(params.get("why") or ""),
                        "failed": str(params.get("failed") or ""), "at": message["created"], "seq": message["seq"]})
    return out


def _operations(store, card_id: str, rev: str, deploy_rev: str = "") -> List[dict]:
    """The release's own operations: key ``release:<id>`` or ``release:<id>:<component>:<mode>``, id ``<rev>`` or
    the pair's ``<rev>+<deploy>`` — not another pair's with the same code rev (review M6)."""
    if not rev:
        return []
    key = f"release:{rev}+{deploy_rev}" if deploy_rev else f"release:{rev}"
    rows = store._conn.execute(
        "SELECT id, key, kind, agents, params, state, error FROM operations WHERE card=? AND (key=? OR key LIKE ?)"
        " ORDER BY created, rowid", (card_id, key, key + ":%")).fetchall()
    import json

    out = []
    for r in rows:
        params = json.loads(r["params"] or "{}")
        out.append({"id": r["id"], "key": r["key"], "kind": r["kind"], "agents": json.loads(r["agents"] or "[]"),
                    "component": params.get("component", ""), "mode": params.get("mode", ""), "state": r["state"],
                    "error": r["error"]})
    return out


def state(store, card_id: str) -> dict:
    """``{"state", "rev", "deploy_rev", "host", "why", "at", "seq", "key", "operations"}`` — ``none`` without a
    release notice; ``deploy_rev`` — the overlay's revision of a pair release, else empty."""
    notices = _notices(store, card_id)
    found = {"state": "none", "rev": "", "deploy_rev": "", "host": "", "why": "", "at": "", "seq": 0, "key": "",
             "operations": []}
    if not notices:
        return found
    # the newest notice decides: one naming a rev selects that release's notices; one without (merged, refused)
    # is the newest fact itself and shows the last known rev only for the eye (review F5)
    newest = notices[-1]
    rev = newest["rev"]
    if rev:
        current = [n for n in notices if n["rev"] == rev]
    else:
        rev = next((n["rev"] for n in reversed(notices) if n["rev"]), "")
        current = [newest]
    last = current[-1]
    health = [n for n in current if n["key"] in HEALTH]
    if last["key"] == "notice.forge.released" and health:
        # kd-merge posts «released» once the release's result is in, after the health check's own notice
        last = health[-1]
    found.update(rev=rev, host=next((n["host"] for n in reversed(current) if n["host"]), ""), at=last["at"],
                 seq=last["seq"], key=last["key"],
                 deploy_rev=next((n["deploy_rev"] for n in reversed(current) if n["deploy_rev"]), ""))
    if last["key"] in FORGE:
        found["state"] = FORGE[last["key"]]
        found["why"] = last["why"]
    else:
        found["state"] = HEALTH[last["key"]]
        found["why"] = last["why"] if last["key"] == "notice.release.degraded" else ""
    # a healthy notice of this rev after a degraded one of the forge: the health check is the later fact already
    ops = _operations(store, card_id, rev, found["deploy_rev"])
    found["operations"] = ops
    if found["state"] in ("healthy", "code_switched", "activating"):
        failed = [o for o in ops if o["state"] == "failed"]
        if failed:
            found["state"] = "degraded"
            found["why"] = "; ".join(f"{o['component'] or ', '.join(o['agents'])} {o['state']}: {o['error']}".strip()
                                     for o in failed)
        elif found["state"] == "healthy" and any(o["state"] in OPEN for o in ops):
            found["state"] = "activating"
    return found


# --- the problem ---------------------------------------------------------------------------------------------
def _raise(ctx, record, rev: str, why: str) -> None:
    """One open problem per revision: a degraded release raised again while open is the same problem; one that
    degrades anew after a healthy check is a new row (the key counts them)."""
    s, now = ctx.store, ctx.now()
    if any(p["kind"] == KIND and p["detail"].get("rev") == rev for p in problems.open_for(s, record.id)):
        return
    base = f"release:{record.id}:{rev}"
    used = s._conn.execute("SELECT COUNT(*) FROM problems WHERE key=? OR key LIKE ?", (base, base + ":%")).fetchone()[0]
    key = base if not used else f"{base}:{used + 1}"
    problems.raise_problem(ctx, key=key, record=record, kind=KIND,
                           agent=record.assignee[1:] if record.assignee.startswith("@") else "",
                           detail={"rev": rev, "why": why[:500], "next_check": people.shift(now, minutes=NEXT_CHECK),
                                   "checks": 0})


def on_event(ctx, event: Mapping) -> None:
    """A release fact arrived (inside its write): a degraded notice or a failed operation of a release raises the
    problem; a healthy notice, or a release of another revision, resolves it."""
    kind, payload = str(event.get("kind", "")), event.get("payload") or {}
    card_id = event.get("card")
    if not card_id:
        return
    s = ctx.store
    if kind == "notice":
        key = payload.get("key")
        if key not in KEYS:
            return
        params = payload.get("params") or {}
        rev = str(params.get("rev") or "")
        if key in DEGRADED_KEYS:
            why = str(params.get("why") or "")
            if params.get("failed"):
                why = f"{params['failed']}: {why}" if why else str(params["failed"])
            _raise(ctx, s.card(card_id), rev, why)
        elif key == "notice.release.healthy":
            problems.resolve(s, card_id, [KIND], by="healthy")
        elif key == "notice.forge.released":
            # a later release: the degraded one is history; the new one is checked on its own
            open_ = [p for p in problems.open_for(s, card_id) if p["kind"] == KIND and p["detail"].get("rev") != rev]
            if open_:
                problems.resolve(s, card_id, [KIND], by=f"release:{rev}")
        return
    if kind == "operation.failed" and payload.get("operation"):
        from kd.cabinet import operations

        op = operations.row(s, payload["operation"])
        if op is None or not str(op["key"]).startswith("release:"):
            return
        # release:<rev>[+<deploy>][:<component>:<mode>] — the problem names the code's rev
        rev = str(op["key"]).split(":")[1].split("+")[0] if ":" in op["key"] else ""
        params, agents = op.get("params") or {}, op.get("agents") or []  # ``operations.row`` parsed them already
        what = params.get("component") or ", ".join(agents)
        _raise(ctx, s.card(card_id), rev, f"{what} failed: {payload.get('reason') or op['error'] or '?'}")


def sweep(ctx) -> int:
    """Open ``release_degraded`` problems whose ``next_check`` passed: BOSS is signalled once more (the card's
    problem text), the check counted and the next one scheduled. Returns how many were re-signalled."""
    import json

    s, now, count = ctx.store, ctx.now(), 0
    rows = s._conn.execute("SELECT * FROM problems WHERE state='open' AND kind=?", (KIND,)).fetchall()
    with s.transaction():
        for row in rows:
            detail = json.loads(row["detail"] or "{}")
            record = s.card(row["card"])
            if record.terminal:  # the card is closed: nobody holds the problem any more (review F3)
                problems.resolve(s, record.id, [KIND], by="card_closed")
                continue
            if state(s, record.id)["state"] != "degraded":  # the facts moved on without the event
                problems.resolve(s, record.id, [KIND], by="state")
                continue
            if not ctx.pipeline().enforcing or not detail.get("next_check") or detail["next_check"] > now:
                continue
            checks = int(detail.get("checks") or 0) + 1
            detail.update(checks=checks, next_check=people.shift(now, minutes=NEXT_CHECK))
            s._conn.execute("UPDATE problems SET detail=?, updated=? WHERE id=?",
                            (json.dumps(detail, ensure_ascii=False, sort_keys=True), s._clock(), row["id"]))
            if ctx.host_config().hosted(BOSS) and record.assignee != "@" + BOSS:
                # a queued signal with the same text is merged with the earlier one: the repeat names its count
                text = problems.PROBLEM_WAKE[KIND].format(id=record.id, agent=row["agent"] or "?")
                commands.enqueue_signal(s, agent=BOSS, card=record.id, initiator="system",
                                        key=f"problem:{row['id']}:{checks}",
                                        text=f"{text} (still degraded, check {checks}: rev {detail.get('rev', '?')})")
            count += 1
    return count


assert set(STATES) >= set(FORGE.values()) | set(HEALTH.values()) and KIND in schema.PROBLEM_KINDS
