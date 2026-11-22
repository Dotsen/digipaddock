"""The owner's test critical alert: one button checks the whole path of a critical incident.

The monitor's next scheduled tick takes the request and raises a test incident (``monitor_test.SERVER`` · ``CHECK``,
its value says it is a test): the cabinet opens P-n, pushes ``incident.critical``, kd-alert's watch sends it to
Telegram; the monitor wakes nobody for it and touches no server, and the incident closes the usual way after
``CLOSE_AFTER`` good ticks. One test at a time, kept in ``meta`` (no table):

- ``monitor.test.request`` — people with ``settings`` and root: a new request, or the waiting or running one
  (``repeat``);
- ``monitor.test.get`` — the last test, with ``servers.read``;
- ``monitor.test.take`` — the monitor's tick: the queued test, now running (``None`` — nothing to do);
- ``monitor.test.done {id, ok, detail?}`` — the monitor: its test incident closed (or could not be raised).

A test nobody took in ``QUEUED_TTL_MIN`` (the monitor is off) or nobody finished in ``RUNNING_TTL_MIN`` is failed, so
the button never stays locked. Every step is a ``monitor.test`` event, read with ``servers.read``.
"""

import json
import uuid

from kd.cabinet import api, incidents

KEY = "monitor_test"
EVENT = "monitor.test"
SERVER = "test"  # the test incident's «server» and check: no real server is named, none is touched
CHECK = "alert"
ACTIVE = ("queued", "running")
QUEUED_TTL_MIN = 15  # three scheduled ticks
RUNNING_TTL_MIN = 60  # raised, then CLOSE_AFTER good ticks: ~20 minutes when the monitor is well
DETAIL_MAX = 200


def _read(s):
    row = s._conn.execute("SELECT value FROM meta WHERE key=?", (KEY,)).fetchone()
    if row is None:
        return None
    try:
        found = json.loads(row[0])
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


def _write(s, actor, test: dict) -> dict:
    s._conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (KEY, json.dumps(test, ensure_ascii=False)))
    s._event(actor, EVENT, "", dict(test))
    return test


def _expire(s, now: str):
    """The stored test; an active one past its time is failed first (written, with its event)."""
    test = _read(s)
    if test is None or test.get("state") not in ACTIVE:
        return test
    since, ttl = (test.get("created"), QUEUED_TTL_MIN) if test["state"] == "queued" else (
        test.get("started"), RUNNING_TTL_MIN)
    if isinstance(since, str) and since < incidents._shift_min(now, ttl):
        return _write(s, incidents.MONITOR_ACTOR, dict(test, state="failed", detail="timeout", finished=now))
    return test


def _request(ctx, principal, request):
    if principal.kind == "user":
        api._require(ctx, principal, "settings")
    s, now = ctx.store, ctx.now()
    with s.transaction():
        test = _expire(s, now)
        if test is not None and test.get("state") in ACTIVE:
            return {"test": test, "repeat": True}
        actor = api.card_actor(principal)
        test = _write(s, actor, {"id": uuid.uuid4().hex[:16], "state": "queued", "requested_by": actor,
                                 "created": now, "started": "", "finished": "", "detail": ""})
    return {"test": test, "repeat": False}


def _get(ctx, principal, request):
    incidents._reader(ctx, principal)
    s = ctx.store
    with s.transaction():
        return {"test": _expire(s, ctx.now())}


def _take(ctx, principal, request):
    incidents._writer(principal)
    s, now = ctx.store, ctx.now()
    with s.transaction():
        test = _expire(s, now)
        if test is None or test.get("state") != "queued":
            return {"test": None}
        return {"test": _write(s, incidents.MONITOR_ACTOR, dict(test, state="running", started=now))}


def _done(ctx, principal, request):
    incidents._writer(principal)
    ok = request.get("ok")
    if not isinstance(ok, bool):
        raise api.ApiError("invalid", "ok is true or false")
    s, now = ctx.store, ctx.now()
    with s.transaction():
        test = _expire(s, now)
        if test is None or test.get("id") != request.get("id"):
            raise api.ApiError("not_found", "no such test")
        if test.get("state") not in ACTIVE:  # timed out meanwhile: the late report changes nothing
            return {"test": test}
        return {"test": _write(s, incidents.MONITOR_ACTOR, dict(
            test, state="done" if ok else "failed", finished=now,
            detail=incidents._free(request.get("detail"), DETAIL_MAX)))}


OPS = {
    "monitor.test.request": (frozenset({"root", "user"}), _request),
    "monitor.test.get": (incidents.READERS, _get),
    "monitor.test.take": (incidents.WRITERS, _take),
    "monitor.test.done": (incidents.WRITERS, _done),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
