"""The monitor's settings in the cabinet over config.json.

The owner edits the catalog's keys (``kd.monitor_settings``) without a commit and a release; config.json of the
deployed tree stays the defaults (the cabinet reads it each time). The edits live in ``monitor_settings``; each save
of the set moves ``monitor_settings_revision``, and a save names the revision it was made against — a stale one is
refused, so two pages never overwrite each other unseen.

- ``monitor.settings.get`` — the edits, the revision, the catalog, the defaults of config.json and the known servers;
  for the monitor (the sysadmin agent's user), root and the cabinet, and people with ``servers.read``.
- ``monitor.settings.save {revision, changes: [{key, server?, value} | {key, server?, reset: true}], signed}`` — people
  with ``settings`` only. Every change is validated (type, range, the server and the checks it names — a check is
  switched off only where it runs —, crit stricter than warn on the merged set); one bad change saves nothing. The cabinet itself classifies each change against the
  value in force: a save with any weakening needs ``signed`` — the web passes it only after the owner's step-up
  (purpose ``monitor.settings``), one signature for the whole save — else ``stepup_required`` and nothing changes.
  Each save writes the event ``monitor.settings`` (who, before and after by key, signed), which people read with
  ``servers.read``.
"""

import json
from pathlib import Path

from kd import clock
from kd import monitor_settings as ms
from kd.cabinet import api, incidents

DEFAULT_CONFIG = "/opt/digipaddock/agents/sysadmin/monitor/config.json"
REVISION = "monitor_settings_revision"
EVENT = "monitor.settings"
CHANGES_MAX = 200


def config_path(ctx) -> str:
    if ctx.monitor_config:
        return ctx.monitor_config
    if ctx.repo_root:
        return str(Path(ctx.repo_root) / "agents" / "sysadmin" / "monitor" / "config.json")
    return DEFAULT_CONFIG


def defaults(ctx) -> dict:
    """config.json of the deployed tree, read each time (a release changes the defaults)."""
    try:
        found = json.loads(Path(config_path(ctx)).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise api.ApiError("unavailable", f"the monitor's config.json is unreadable ({exc.__class__.__name__})")
    if not isinstance(found, dict):
        raise api.ApiError("unavailable", "the monitor's config.json is not an object")
    return found


def revision(s) -> int:
    row = s._conn.execute("SELECT value FROM meta WHERE key=?", (REVISION,)).fetchone()
    return int(row[0]) if row else 0


def edits(s) -> list:
    rows = s._conn.execute("SELECT * FROM monitor_settings ORDER BY key, server").fetchall()
    return [{"key": r["key"], "server": r["server"], "value": json.loads(r["value"]), "by": r["author"],
             "at": r["at"]} for r in rows]


def known_servers(s, found: dict) -> set:
    """The servers the monitor reported to the cabinet and those its config.json names."""
    return {r[0] for r in s._conn.execute("SELECT alias FROM servers").fetchall()} | ms.servers_of(found)


def _shown(value):
    return None if value is ms.MISSING else value


# --- reading ---------------------------------------------------------------------------------------------------
def _get(ctx, principal, request):
    incidents._reader(ctx, principal)
    s = ctx.store
    found = edits(s)
    try:
        base = defaults(ctx)
    except api.ApiError:
        if principal.kind == "user":
            raise
        base = None  # the monitor merges over its own config.json: the edits are enough for it
    out = {"revision": revision(s), "items": found, "timezone": str(clock.zone())}  # quiet hours are the host's
    if base is not None:
        servers = sorted(known_servers(s, base))
        for item in found:
            item["stale"] = ms.stale(base, item["key"], item["server"])
        out["defaults"] = {e["key"]: base_value for e in ms.describe() if e["scope"] == "global"
                           for base_value in [ms.lookup(base, e["key"])] if base_value is not ms.MISSING}
        out["server_defaults"] = {
            server: {k: v for k in ms.MEM_MB for v in [ms.lookup(base, k, server)] if v is not ms.MISSING}
            for server in servers}
        out["servers"] = servers
        out["checks"] = {server: sorted(ms.planned(base, server)) for server in servers}  # what may be switched off
        out["catalog"] = ms.describe()
    return out


# --- saving ----------------------------------------------------------------------------------------------------
def _changes(request) -> list:
    changes = request.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= CHANGES_MAX:
        raise api.ApiError("invalid", f"changes is a list of 1 to {CHANGES_MAX} changes")
    return changes


def _plan(base, current: dict, changes, servers) -> tuple:
    """-> (the new set {(key, server): value}, the touched (key, server) in order); Invalid for a bad change."""
    new, touched = dict(current), []
    for change in changes:
        if not isinstance(change, dict):
            raise ms.Invalid("a change is an object")
        key, server = change.get("key"), change.get("server", "")
        if not isinstance(key, str) or not isinstance(server, str):
            raise ms.Invalid("a change names its key and server")
        if (key, server) in touched:
            raise ms.Invalid(f"{key} is changed twice in one save")
        ms.scope(key)
        if change.get("reset") is True:
            new.pop((key, server), None)
        else:
            if ms.stale(base, key, server):
                raise ms.Invalid(f"{key} is not in config.json of this release")
            new[(key, server)] = ms.validate(key, server, change.get("value"), servers)
            if key == ms.OFF:  # a check switched off where it does not run would be a signed no-op
                idle = set(new[(key, server)]) - set(current.get((key, server)) or []) - ms.planned(base, server)
                if idle:
                    raise ms.Invalid(f"{sorted(idle)[0]} does not run on {server}")
        touched.append((key, server))
    return new, touched


def _merged(base, values: dict) -> dict:
    return ms.merge(base, [{"key": k, "server": sv, "value": v} for (k, sv), v in values.items()])


def _save(ctx, principal, request):
    api._require(ctx, principal, "settings")
    against = request.get("revision")
    if isinstance(against, bool) or not isinstance(against, int) or against < 0:
        raise api.ApiError("invalid", "revision is the settings' revision the page read")
    changes, signed = _changes(request), request.get("signed") is True
    base, s = defaults(ctx), ctx.store
    with s.transaction():
        if against != revision(s):
            raise api.ApiError("conflict", "the monitor's settings changed meanwhile: read them again")
        current = {(e["key"], e["server"]): e["value"] for e in edits(s)}
        try:
            new, touched = _plan(base, current, changes, known_servers(s, base))
            before, after = _merged(base, current), _merged(base, new)
            ms.check_config(after)
        except ms.Invalid as exc:
            raise api.ApiError("invalid", str(exc))
        diff = []
        for key, server in touched:
            old, now = ms.lookup(before, key, server), ms.lookup(after, key, server)
            if old == now and (((key, server) in current) == ((key, server) in new)):
                continue
            diff.append({"key": key, "server": server, "before": _shown(old), "after": _shown(now),
                         "reset": (key, server) not in new, "weaker": ms.weakens(key, old, now)})
        if not diff:
            return {"revision": against, "changes": [], "signed": False}
        if any(d["weaker"] for d in diff) and not signed:
            raise api.ApiError("stepup_required", "a weakening of the monitor is confirmed with a passkey or TOTP")
        actor, stamp = api.card_actor(principal), ctx.now()
        for d in diff:
            key, server = d["key"], d["server"]
            if d["reset"]:
                s._conn.execute("DELETE FROM monitor_settings WHERE key=? AND server=?", (key, server))
            else:
                s._conn.execute(
                    "INSERT INTO monitor_settings (key, server, value, author, at) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(key, server) DO UPDATE SET value=excluded.value, author=excluded.author,"
                    " at=excluded.at", (key, server, json.dumps(new[(key, server)], ensure_ascii=False), actor, stamp))
        moved = against + 1
        s._conn.execute("INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET"
                        " value=excluded.value", (REVISION, str(moved)))
        s._event(actor, EVENT, "", {"changes": diff, "signed": signed, "revision": moved})
    return {"revision": moved, "changes": diff, "signed": signed}


OPS = {
    "monitor.settings.get": (incidents.READERS, _get),
    "monitor.settings.save": (frozenset({"user"}), _save),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
