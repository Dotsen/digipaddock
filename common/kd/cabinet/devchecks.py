"""dev.check in the cabinet: the lead's check of its PR's head in the project's contour.

A check is an ``operations`` row of kind ``dev.check`` with no roles and no grant: the developer asks through
``kd-forge check`` (the forge executor, which read the PR), on the card the PR's title names, while it leads that
card in work. ``params`` keep what is checked — the PR, its head and base, the profiles, the contour and the other
components' SHAs (``set``). The key is what is checked (``key_of``): the same check asked again joins the active or
passed one; one that ended red may be asked again, up to ``ATTEMPTS`` times. A newer head of the same PR supersedes
an older check: one the host has not got ends at once, one it has is flagged and the host stops it.

The factory's lane never sees these rows (``operations.in_flight/next_due``); the dispatcher keeps a lane per contour
(``in_flight``, ``next_due``, ``capacity`` of the contour). The host's report (``host.report`` item ``devcheck``) moves
a check running → succeeded (class ``passed``) | failed (``error`` = its class), one event each, and its end is one
notice in the card's thread. A report that says the contour needs maintenance marks it (``contours``): the next
requests for it are refused until root releases it. A card that leaves work takes its checks with it.
"""

import hashlib
import json
import re
import secrets
from typing import List, Mapping, Optional

from kd import contours as contour_file
from kd import worker_board
from kd.cabinet import api, notices, operations, prs_api

KIND = "dev.check"
CLASSES = ("passed", "checks_failed", "environment_not_ready", "dependency_update_required", "external_unavailable",
           "superseded", "cancelled", "host_lost")
ATTEMPTS = 5  # checks of the same key that may end red before it is refused
GRACE_S = 1200  # the host's limit over the check's own: the checkout, the dependencies (a cold build), the statuses
TIMEOUT_S = (60, 3600)
PROFILES_MAX = 8
SET_MAX = 8
SOURCE = "devcheck"
HOST = operations.HOST
NAME = contour_file.NAME_RE
SHA = re.compile(r"[0-9a-f]{40}")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
LOG = re.compile(r"devcheck/[0-9a-f]{32}\.log")
PROFILE_STATES = ("success", "failure", "error")
CANCELS = ("card_left", "superseded", "root")
REQUEST_FIELDS = frozenset({"op", "card", "agent", "project", "repo", "pr", "head", "base_sha", "target", "profiles",
                            "contour", "timeout_s", "set"})
ACTIVE_SQL = "(state='running' OR (state='pending' AND phase='delivered'))"
# a check that ended with nobody to act on it says so quietly; a contour in maintenance is the owner's at once
_QUIET = ("passed", "superseded", "cancelled")


def _invalid(message):
    return api.ApiError("invalid", message)


def _refused(message):
    return api.ApiError("refused", message)


# --- a request --------------------------------------------------------------------------------
def key_of(project: str, head: str, profiles, base_sha: str, set_: Mapping) -> str:
    """What is checked, as one key: the project and its head, the profiles in any order, the base, the set."""
    what = json.dumps({"profiles": sorted(profiles), "base": base_sha, "set": dict(sorted(set_.items()))},
                      sort_keys=True, separators=(",", ":"))
    return f"devcheck:{project}:{head[:12]}:{hashlib.sha256(what.encode('utf-8')).hexdigest()[:24]}"


def _fields(req: Mapping) -> dict:
    extra = set(req) - REQUEST_FIELDS
    if extra:
        raise _invalid(f"unknown field: {', '.join(sorted(extra))}")
    card, project, repo = req.get("card"), req.get("project"), req.get("repo")
    if not isinstance(card, str) or not api.CARD.fullmatch(card):
        raise _invalid("card is KD-<n>")
    if not isinstance(project, str) or not NAME.fullmatch(project):
        raise _invalid("project is a project name")
    if not isinstance(repo, str) or not prs_api.REPO.fullmatch(repo) or ".." in repo:
        raise _invalid("repo is owner/name")
    pr = req.get("pr")
    if isinstance(pr, bool) or not isinstance(pr, int) or not 0 < pr < 10 ** 9:
        raise _invalid("pr is the PR's number")
    head, base, target = req.get("head"), req.get("base_sha"), req.get("target")
    if not isinstance(head, str) or not SHA.fullmatch(head) or not isinstance(base, str) or not SHA.fullmatch(base):
        raise _invalid("head and base_sha are full commit SHAs")
    if not isinstance(target, str) or not BRANCH.fullmatch(target) or ".." in target:
        raise _invalid("target is the PR's base branch")
    profiles = req.get("profiles")
    if (not isinstance(profiles, list) or not 0 < len(profiles) <= PROFILES_MAX
            or not all(isinstance(p, str) and NAME.fullmatch(p) for p in profiles) or len(set(profiles)) != len(profiles)):
        raise _invalid(f"profiles are 1 to {PROFILES_MAX} distinct profile names")
    timeout = req.get("timeout_s")
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not TIMEOUT_S[0] <= timeout <= TIMEOUT_S[1]:
        raise _invalid(f"timeout_s is {TIMEOUT_S[0]} to {TIMEOUT_S[1]} seconds")
    set_ = req.get("set") or {}
    if (not isinstance(set_, Mapping) or len(set_) > SET_MAX or project in set_
            or not all(isinstance(k, str) and NAME.fullmatch(k) and isinstance(v, str) and SHA.fullmatch(v)
                       for k, v in set_.items())):
        raise _invalid("set is the other components' project -> full SHA")
    contour = req.get("contour")
    if not isinstance(contour, str) or not NAME.fullmatch(contour):
        raise _invalid("contour is a contour name")
    agent = req.get("agent")
    if not isinstance(agent, str) or not NAME.fullmatch(agent):
        raise _invalid("agent is the lead's name")
    return {"card": card, "agent": agent, "project": project, "repo": repo, "pr": pr, "head": head, "base_sha": base,
            "target": target, "profiles": list(profiles), "contour": contour, "timeout_s": timeout,
            "set": dict(sorted(set_.items()))}


def contour_row(s, name: str) -> dict:
    found = s._conn.execute("SELECT * FROM contours WHERE name=?", (name,)).fetchone()
    if found is None:
        return {"name": name, "state": "idle", "deployed": {}, "since": "", "reason": "", "updated": ""}
    out = dict(found)
    out["deployed"] = json.loads(out["deployed"] or "{}")
    return out


def _contour_set(s, name: str, **fields) -> None:
    current = contour_row(s, name)
    current.update(fields, updated=s._clock())
    s._conn.execute(
        "INSERT INTO contours (name, state, deployed, since, reason, updated) VALUES (?,?,?,?,?,?)"
        " ON CONFLICT(name) DO UPDATE SET state=excluded.state, deployed=excluded.deployed, since=excluded.since,"
        " reason=excluded.reason, updated=excluded.updated",
        (name, current["state"], json.dumps(current["deployed"], sort_keys=True), current["since"],
         current["reason"], current["updated"]))


def _checks(s, where: str, args=()) -> List[dict]:
    rows = s._conn.execute(f"SELECT id FROM operations WHERE kind=? AND {where} ORDER BY created, rowid",
                           (KIND, *args)).fetchall()
    return [operations.row(s, r[0]) for r in rows]


def of_card(s, card: str) -> List[dict]:
    """The card's checks, newest first (the snapshot's ``checks``)."""
    return _checks(s, "card=?", (card,))[::-1]


def _link(s, op: Mapping, card: str, requester: str) -> None:
    """A join from another card: that card may wait on the check too (``operations.linked``)."""
    if card != op["card"]:
        s._conn.execute("INSERT OR IGNORE INTO operation_requests (key, operation, card, requester, payload_sha,"
                        " created) VALUES (?,?,?,?,?,?)",
                        (f"{op['key']}@{card}", op["id"], card, requester, op["payload_sha"], s._clock()))


def _flag(s, op: Mapping, why: str) -> None:
    """A check the host has: it is to stop (``cancelling``); the host's report ends it."""
    if not op["params"].get("cancel"):
        params = {**op["params"], "cancel": why}
        s._conn.execute("UPDATE operations SET params=?, updated=? WHERE id=?",
                        (json.dumps(params, sort_keys=True), s._clock(), op["id"]))
        operations._event(s, HOST, "operation.cancelling", op, reason=why)


def _stop(ctx, op: Mapping, why: str) -> None:
    """Not reached the host yet — it ends here; else it is flagged for the host to stop."""
    if op["state"] == "pending" and not op["phase"]:
        finish(ctx, op, "superseded" if why == "superseded" else "cancelled", {"reason": why})
    else:
        _flag(ctx.store, op, why)


def request(ctx, req: Mapping) -> dict:
    """One check (see the module): ``{"operation": view, "created": bool}``."""
    fields = _fields(req)
    agent = fields["agent"]
    if agent not in ctx.agent_names() or f"@{agent}" == worker_board.COORDINATOR:
        raise _refused(f"@{agent} is not an agent that checks its PRs")
    found = ctx.contours()
    contour = found.get(fields["contour"])
    if contour is None or fields["project"] not in contour.components:
        raise _refused(f"{fields['project']} is not in the contour {fields['contour']}")
    outside = sorted(set(fields["set"]) - set(contour.components))
    if outside:
        raise _refused(f"{', '.join(outside)} not in the contour {contour.name}")
    s, requester = ctx.store, f"agent:{agent}"
    revision = operations.deployed(ctx)
    with s.transaction():
        record = api._card(ctx, fields["card"])
        if record.status != "In Progress":
            raise _refused(f"{record.id} is {record.status}: a check runs for a card in work")
        if record.assignee != f"@{agent}":
            raise _refused(f"{record.id} is led by {record.assignee or 'nobody'}, not @{agent}")
        health = contour_row(s, contour.name)
        if health["state"] == "maintenance":
            raise _refused(f"environment_not_ready: the contour {contour.name} is in maintenance since "
                           f"{health['since']}: {health['reason']}")
        base = key_of(fields["project"], fields["head"], fields["profiles"], fields["base_sha"], fields["set"])
        family = _checks(s, "(key=? OR key LIKE ?)", (base, base + ":%"))
        joined = [op for op in family if op["state"] in operations.ACTIVE or op["state"] == "succeeded"]
        if joined:
            _link(s, joined[-1], record.id, requester)
            return {"operation": operations.view(joined[-1]), "created": False}
        if len(family) >= ATTEMPTS:
            raise _refused(f"{len(family)} attempts of this check ended red: fix the code and push a new head")
        key = base if not family else f"{base}:{len(family) + 1}"
        for older in _checks(s, "state IN ('pending','running') AND json_extract(params,'$.repo')=?"
                                " AND json_extract(params,'$.pr')=? AND json_extract(params,'$.head')!=?",
                             (fields["repo"], fields["pr"], fields["head"])):
            _stop(ctx, older, "superseded")
        params = {k: v for k, v in fields.items() if k != "agent"}  # the card: the runner's status links to it
        sha = operations.payload_sha(KIND, record.id, [], revision, params)
        now, operation_id = s._clock(), secrets.token_hex(16)
        s._conn.execute(
            "INSERT INTO operations (id, key, kind, card, requested_by, agents, params, revision, grant_id,"
            " payload_sha, created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (operation_id, key, KIND, record.id, requester, "[]", json.dumps(params, sort_keys=True), revision, "",
             sha, now, now))
        operations._remember(s, key, operation_id, record.id, requester, sha)
        op = operations.row(s, operation_id)
        operations._event(s, requester, "operation.queued", op, key=key, contour=contour.name, pr=fields["pr"],
                          head=fields["head"])
        return {"operation": operations.view(op), "created": True}


# --- the dispatcher's part --------------------------------------------------------------------
def in_flight(s, contour: str) -> List[dict]:
    """The contour's checks the host has now (delivered or running)."""
    return _checks(s, f"{ACTIVE_SQL} AND json_extract(params,'$.contour')=?", (contour,))


def next_due(s, contour: str, now: str) -> Optional[dict]:
    found = _checks(s, "state='pending' AND phase='' AND next_try<=? AND json_extract(params,'$.contour')=?",
                    (now, contour))
    return found[0] if found else None


def orphans(s, names) -> List[dict]:
    """Checks not yet with the host whose contour the release tree no longer has: nothing would ever take them."""
    return [op for op in _checks(s, "state='pending' AND phase=''") if op["params"].get("contour") not in names]


def cancelling(s) -> List[dict]:
    """Checks the host has that are to stop: the card left work, a newer head, root."""
    return _checks(s, f"{ACTIVE_SQL} AND json_extract(params,'$.cancel') IS NOT NULL")


def host_request(op: Mapping, start: bool) -> dict:
    """The host's ``devcheck``: ``start`` for a pending one; a poll asks only (``unknown`` — the host lost it)."""
    return {"op": "devcheck", "check": op["id"], "start": start,
            "timeout": int(op["params"]["timeout_s"]) + GRACE_S, "params": op["params"]}


def host_cancel(op: Mapping) -> dict:
    return {"op": "devcheck_cancel", "check": op["id"], "reason": op["params"].get("cancel") or "root"}


def _own(s, operation_id: str) -> Optional[dict]:
    op = operations.row(s, operation_id)
    return op if op is not None and op["kind"] == KIND else None


def delivered(ctx, operation_id: str, answer: Optional[Mapping] = None) -> None:
    op = _own(ctx.store, operation_id)
    if op is not None and op["state"] == "pending" and not op["phase"]:
        operations._set(ctx.store, operation_id, phase="delivered", attempts=op["attempts"] + 1)
    if answer:
        answered(ctx, operation_id, answer)


def answered(ctx, operation_id: str, answer: Mapping) -> None:
    """The host's answer: its report, or ``unknown`` — it holds no such check and has no result: ``host_lost``, or
    ``cancelled`` when the check was to stop anyway."""
    if answer.get("state") == "unknown":
        op = _own(ctx.store, operation_id)
        if op is not None and (op["state"] == "running" or (op["state"] == "pending" and op["phase"] == "delivered")):
            why = op["params"].get("cancel")
            finish(ctx, op, "cancelled" if why else "host_lost", {"reason": why} if why else {})
        return
    item = answer.get("report")
    if isinstance(item, Mapping):
        report_item(ctx, {**item, "check": operation_id})


def retry(ctx, operation_id: str, next_try: str) -> None:
    op = _own(ctx.store, operation_id)
    if op is not None and op["state"] == "pending" and not op["phase"]:
        operations._set(ctx.store, operation_id, next_try=next_try, attempts=op["attempts"] + 1)


def refused(ctx, operation_id: str, message: str) -> None:
    """The host would not take it: nothing ran — the environment is not ready for it."""
    op = _own(ctx.store, operation_id)
    if op is not None and op["state"] == "pending":
        finish(ctx, op, "environment_not_ready", {"error_short": f"{operations.HOST_REFUSED}: {message}"})


# --- the host's reports -----------------------------------------------------------------------
def _number(value, what: str):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value < 10 ** 9:
        raise _invalid(f"{what} is a non-negative number")
    return value


def _profiles(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > PROFILES_MAX:
        raise _invalid(f"profiles is a list of at most {PROFILES_MAX} results")
    out = []
    for item in value:
        if (not isinstance(item, Mapping) or not isinstance(item.get("profile"), str)
                or not NAME.fullmatch(item["profile"]) or item.get("status") not in PROFILE_STATES):
            raise _invalid("a profile's result is {profile, status: success|failure|error, counts, duration_s}")
        counts = item.get("counts") or {}
        if not isinstance(counts, Mapping) or len(counts) > 8 or not all(
                isinstance(k, str) and NAME.fullmatch(k) for k in counts):
            raise _invalid("counts is an object of at most 8 named numbers")
        out.append({"profile": item["profile"], "status": item["status"],
                    "counts": {k: int(_number(v, f"counts.{k}")) for k, v in counts.items()},
                    "duration_s": _number(item.get("duration_s") or 0, "duration_s")})
    return out


def _deployed(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping) or len(value) > SET_MAX or not all(
            isinstance(k, str) and NAME.fullmatch(k) and isinstance(v, str) and SHA.fullmatch(v)
            for k, v in value.items()):
        raise _invalid("deployed is component -> full SHA")
    return dict(sorted(value.items()))


def report_item(ctx, item: Mapping) -> None:
    """``{"type": "devcheck", "check", "state": running|succeeded|failed, "class"?, "profiles"?, "deployed"?, "log"?,
    "error_short"?, "maintenance"?}``. A final state is never moved again; a repeated report changes nothing."""
    operation_id = item.get("check")
    if not isinstance(operation_id, str) or not operations.OPERATION_ID.fullmatch(operation_id):
        raise _invalid("invalid check id")
    op = _own(ctx.store, operation_id)
    if op is None:
        raise api.ApiError("not_found", "unknown check")
    state, cls = item.get("state"), item.get("class")
    if state not in ("running", "succeeded", "failed"):
        raise _invalid("state is running, succeeded or failed")
    if state != "running" and (cls not in CLASSES or (state == "succeeded") != (cls == "passed")):
        raise _invalid("a final report has its class: passed for succeeded, another for failed")
    log, tail = item.get("log") or "", item.get("error_short") or ""
    if not isinstance(log, str) or (log and not LOG.fullmatch(log)):
        raise _invalid("log is devcheck/<id>.log")
    if not isinstance(tail, str):
        raise _invalid("error_short is text")
    result = {"profiles": _profiles(item.get("profiles")), "deployed": _deployed(item.get("deployed")), "log": log,
              "error_short": tail[-operations.TAIL_MAX:]}
    maintenance = operations._reason(item.get("maintenance"))
    if maintenance:
        result["maintenance"] = maintenance
    if op["state"] in operations.FINAL:
        return
    if state == "running":
        if op["state"] != "running":
            operations._event(ctx.store, HOST, "operation.running",
                              operations._set(ctx.store, operation_id, state="running", phase="checking"),
                              phase="checking")
        return
    finish(ctx, op, cls, result)


def _fence(text: str) -> str:
    return text.rstrip() or "—"


def finish(ctx, op: Mapping, cls: str, result: Mapping) -> dict:
    """The check's end: its row, the event (the waits on it end), the contour's health, one notice — one write."""
    s = ctx.store
    params = op["params"]
    state = "succeeded" if cls == "passed" else "failed"
    with s.transaction():
        done = operations._set(s, op["id"], state=state, phase="", error="" if cls == "passed" else cls,
                               result={"class": cls, **result})
        operations._event(s, HOST, f"operation.{state}", done, reason=cls)
        maintenance = result.get("maintenance") or ""
        if maintenance:
            _contour_set(s, params["contour"], state="maintenance", reason=maintenance, since=s._clock())
            s._event(HOST, "contour.maintenance", "", {"contour": params["contour"], "reason": maintenance,
                                                       "operation": op["id"]})
        elif result.get("deployed"):
            _contour_set(s, params["contour"], deployed=result["deployed"])
        profiles = result.get("profiles") or []
        # the host's report of a stopped check carries no reason: the cancel flag holds it
        reason = maintenance or result.get("reason") or (
            params.get("cancel") if cls in ("cancelled", "superseded") else "") or "-"
        level ="critical" if maintenance else "info" if cls in _QUIET else "warn"
        notices.post_in_tx(s, source=SOURCE, level=level, key=f"notice.devcheck.{cls}", card=done["card"], params={
            "pr": str(params["pr"]), "repo": params["repo"], "project": params["project"],
            "head": params["head"][:7], "profiles": ",".join(params["profiles"]), "contour": params["contour"],
            "failed": ",".join(p["profile"] for p in profiles if p["status"] != "success") or "-",
            "duration": f"{round(sum(p['duration_s'] for p in profiles))}s", "log": result.get("log") or "-",
            "operation": done["id"][:12], "tail": _fence(result.get("error_short") or ""),
            "reason": reason})
        return done


def on_card_changed(ctx, event: Mapping) -> None:
    """The card left work or changed its lead (``card.changed``, inside its write): its checks go with it."""
    payload = event.get("payload") or {}
    before, after = payload.get("before") or {}, payload.get("after") or {}
    if before.get("status") != "In Progress" or not event.get("card"):
        return
    if after.get("status") == "In Progress" and after.get("assignee") == before.get("assignee"):
        return
    for op in _checks(ctx.store, "card=? AND state IN ('pending','running')", (event["card"],)):
        _stop(ctx, op, "card_left")


# --- ops --------------------------------------------------------------------------------------
def _request_op(ctx, principal, req):
    return request(ctx, req)


def _name(ctx, req) -> str:
    name = req.get("name")
    if not isinstance(name, str) or name not in ctx.contours():
        raise api.ApiError("not_found", f"no contour {name!r} in the release tree")
    return name


def _contour_view(s, contour) -> dict:
    health, running = contour_row(s, contour.name), in_flight(s, contour.name)
    state = health["state"] if health["state"] == "maintenance" else "busy" if running else "idle"
    return {"name": contour.name, "host": contour.host, "components": list(contour.components),
            "exclusive": contour.exclusive, "capacity": contour_file.capacity(contour), "state": state,
            "running": [{"operation": op["id"], "card": op["card"], "project": op["params"]["project"],
                         "pr": op["params"]["pr"], "head": op["params"]["head"]} for op in running],
            "deployed": health["deployed"], "reason": health["reason"], "since": health["since"]}


def _list_contours_op(ctx, principal, req):
    api._require(ctx, principal, "servers.read")
    return {"contours": [_contour_view(ctx.store, c) for _, c in sorted(ctx.contours().items())]}


def _release_op(ctx, principal, req):
    name, s = _name(ctx, req), ctx.store
    with s.transaction():
        _contour_set(s, name, state="idle", reason="", since=s._clock())
        s._event("root", "contour.released", "", {"contour": name})
    return {"contour": _contour_view(s, ctx.contours()[name])}


def _maintenance_op(ctx, principal, req):
    name, s = _name(ctx, req), ctx.store
    reason = operations._reason(req.get("reason"))
    if not reason:
        raise _invalid("a contour goes into maintenance for a reason")
    with s.transaction():
        _contour_set(s, name, state="maintenance", reason=reason, since=s._clock())
        s._event("root", "contour.maintenance", "", {"contour": name, "reason": reason})
    return {"contour": _contour_view(s, ctx.contours()[name])}


def _cancel_op(ctx, principal, req):
    operation_id = req.get("id")
    if not isinstance(operation_id, str) or not operations.OPERATION_ID.fullmatch(operation_id):
        raise _invalid("invalid check id")
    s = ctx.store
    with s.transaction():
        op = _own(s, operation_id)
        if op is None:
            raise api.ApiError("not_found", "unknown check")
        if op["state"] in operations.ACTIVE:
            _stop(ctx, op, "root")
        return {"operation": operations.view(_own(s, operation_id))}


def _list_op(ctx, principal, req):
    if principal.kind == "user" or req.get("card"):
        card = api._visible_card(ctx, principal, req.get("card"))
        found = of_card(ctx.store, card.id)
    else:
        found = _checks(ctx.store, "1=1")[::-1]
    return {"checks": [operations.view(op) for op in found[:50]]}


OPS = {
    "devcheck.request": (frozenset({"forge"}), _request_op),
    "devcheck.cancel": (frozenset({"root"}), _cancel_op),
    "devcheck.list": (frozenset({"root", "user"}), _list_op),
    "contour.list": (frozenset({"root", "user"}), _list_contours_op),
    "contour.release": (frozenset({"root"}), _release_op),
    "contour.maintenance": (frozenset({"root"}), _maintenance_op),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
