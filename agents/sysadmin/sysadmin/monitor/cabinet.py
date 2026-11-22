"""The monitor and the cabinet: servers, checks and incidents P-n live in the cabinet's database.

After a poll the tick queues what it saw in state["cabinet_queue"]: a sync (the servers polled, the last result of
each check, changes of check status) and the changes of problems (open, update, resolve) — and sends the queue in
order, in batches of up to ~1 MB (`monitor.push`, a call no longer than CALL_TIMEOUT_S). The cabinet is unreachable —
the queue waits for the next tick and the tick's other calls are skipped; every record has its own key, and the
cabinet does not apply it twice. The cabinet issues a new problem's number (the answer to open) — until then the
problem lives without one. The queue's bound is QUEUE_DAYS and QUEUE_MAX records: there is always one sync (a new one
absorbs the status changes of the previous), open and resolve are never dropped.

Every tick the monitor reads the acknowledgement and muting of open incidents (`incident.list active`): the cabinet
is the source of truth; without the cabinet — the last known. The first tick after the release carries state.json
over (a copy beside it — state.json.pre-kd9054) through `incident.import`: numbers, statuses, history; a repeat
doubles nothing.
"""
import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta
from pathlib import Path

from .registry import recent_key, split_key

COMMON = str(Path(__file__).resolve().parents[4] / "common")
if COMMON not in sys.path:
    sys.path.append(COMMON)
from kd import clock, monitor_settings  # noqa: E402
from kd.cabinet import client as cabinet_client  # noqa: E402

CALL_TIMEOUT_S = 10
BATCH_BYTES = 900 * 1024  # a request to the cabinet's socket is up to 1 MB
QUEUE_MAX = 5000
QUEUE_DAYS = 7
CHANGES_MAX = 2000  # status changes in one sync when the cabinet has been away for long
CHANGES_BYTES = 400 * 1024  # and no more than this: a sync with the snapshot of checks fits one batch
VALUE_MAX = 200
TEXT_MAX = 2000  # a problem's text
ROW_TEXT_MAX = 500  # the text of a check result in the snapshot
CHANGE_TEXT_MAX = 300  # the text of a status change
PRE_COPY = ".pre-kd9054"
KEPT = ("open", "resolve")  # the queue never drops these
CHANGE_OF = {"worse": "worse", "better": "better", "fixed": "fixed", "fix_failed": "fix_failed"}


def client():
    return cabinet_client.Client(timeout=CALL_TIMEOUT_S)


def _key(*parts) -> str:
    return hashlib.sha256("|".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def _utc_to_local(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(clock.zone()).isoformat()
    except ValueError:
        return None


# ---------- what to send ----------

def items_from(journal) -> list:
    """The problem history records of this run (Registry.journal) → queue records."""
    out = []
    for entry, p in journal:
        kind, at = entry["kind"], entry["at"]
        base = {"problem_key": p["key"], "opened_at": p["opened_at"], "at": at}
        if kind == "open":
            server, check, subject = split_key(p)
            out.append({"key": _key("open", p["key"], p["opened_at"]), "kind": "open", "problem": {
                "key": p["key"], "server": server, "check": check, "subject": subject, "level": entry["level"],
                "max_level": entry["level"], "value": str(p.get("value", ""))[:VALUE_MAX],
                "value_hash": hashlib.sha256(str(p.get("value", "")).encode()).hexdigest(),
                "text": str(p.get("text", ""))[:TEXT_MAX], "opened_at": p["opened_at"],
                "repeats": int(p.get("repeats") or 0),
                "confirmation_count": p.get("confirmation_count", 0),
                "confirmation_started_at": p.get("confirmation_started_at", ""),
                "confirmation_at": p.get("last_seen", p["opened_at"])}})
        elif kind in ("close", "accept", "disabled", "revoke"):
            out.append({"key": _key("resolve", p["key"], p["opened_at"], at), "kind": "resolve", "how": kind,
                        "value": entry["value"], **base})
        elif kind in CHANGE_OF:
            item = {"key": _key(kind, p["key"], p["opened_at"], at, entry["level"], entry["value"]),
                    "kind": "update", "change": CHANGE_OF[kind], "level": entry["level"], **base}
            if kind == "fixed":
                item["card"] = entry["value"]
            else:
                item["value"] = entry["value"]
                item["value_hash"] = hashlib.sha256(str(p.get("value", "")).encode()).hexdigest()
                item["text"] = str(p.get("text", ""))[:TEXT_MAX]
            out.append(item)
    return out


def trust_of(baseline, servers):
    """The baseline's trusted lists for the cabinet: the shared trusted addresses and, for the servers polled, the
    identities, ports, WG peers and the time the baseline was taken. A damaged baseline — None (the cabinet keeps the
    former lists)."""
    if baseline is None or baseline.broken:
        return None
    data = baseline.data
    listed = {}
    for alias in sorted(servers):
        if baseline.has(alias):
            srv = baseline.server(alias)
            listed[alias] = {"identities": dict(srv.get("identities") or {}), "ports": dict(srv.get("ports") or {}),
                             "wgpeers": list(srv.get("wgpeers") or []), "snapshot": dict(srv.get("snapshot") or {})}
            if srv.get("taken_at"):
                listed[alias]["taken_at"] = srv["taken_at"]
    return {"trusted": dict(data.get("trusted") or {}), "servers": listed}


def sync_item(results, state, servers, hosts, now, partial=False, trust=None) -> dict:
    """A poll → a sync: the servers polled (the unreachability counter), the last result of each check that ran and the
    changes of its status against the previous run (state["check_levels"]; a first observation is not a change).
    partial — a tick over a part of the servers: the list holds only them, the cabinet does not switch the others off.
    trust — the baseline's lists (trust_of)."""
    at = now.isoformat()
    levels = state.setdefault("check_levels", {})
    unreachable = state.get("unreachable")
    unreachable = {k: v for k, v in unreachable.items() if isinstance(v, int) and not isinstance(v, bool)} if (
        isinstance(unreachable, dict)) else {}
    checks, changes = [], []
    sources = {}
    for res in results:
        if res.ran:
            sources.setdefault((res.server, res.check), []).extend(res.observations)
    for (server, check), observations in sorted(sources.items()):
        rows = {}
        for o in observations:
            prev = rows.get(o.subject)
            if prev is None or _rank(o.level) > _rank(prev.level):
                rows[o.subject] = o
        out = [{"subject": o.subject, "level": o.level, "value": str(o.value)[:VALUE_MAX],
                "value_hash": hashlib.sha256(str(o.value).encode()).hexdigest(),
                "text": str(o.text)[:ROW_TEXT_MAX]} for _, o in sorted(rows.items())]
        if not out:
            out = [{"subject": "", "level": "ok", "value": "", "text": ""}]
        checks.append({"server": server, "check": check, "rows": out, "at": at})
        prefix = f"{server}:{check}:"
        seen = set()
        for row in out:
            name = prefix + row["subject"]
            seen.add(name)
            prev = levels.get(name)
            if prev is not None and prev != row["level"]:
                changes.append({"server": server, "check": check, "subject": row["subject"], "level": row["level"],
                                "prev": prev, "value": row["value"], "text": row["text"][:CHANGE_TEXT_MAX], "at": at})
            levels[name] = row["level"]
        for name in [n for n in levels if n.startswith(prefix) and n not in seen]:
            if levels[name] != "ok":  # the object is gone from a check that ran — it is fine now
                changes.append({"server": server, "check": check, "subject": name[len(prefix):], "level": "ok",
                                "prev": levels[name], "value": "", "text": "", "at": at})
            del levels[name]
    names = set(servers) if partial else set(servers) | set(unreachable)
    last_replies = state.setdefault("server_last_reply", {})
    for result in results:
        if result.check == "reach" and result.ran and all(o.level == "ok" for o in result.observations):
            last_replies[result.server] = at
    listed = [{"alias": s, "host": hosts.get(s, ""), "unreachable_count": max(0, unreachable.get(s, 0)), "at": at,
               "last_reply_at": last_replies.get(s, "")}
              for s in sorted(names) if isinstance(s, str)]
    item = {"key": _key("sync", at), "kind": "sync", "at": at, "servers": listed, "checks": checks,
            "changes": changes}
    # Cumulative source counts survive offline queue compaction and retries. Legacy history is not guessed.
    item["confirmations"] = [{"problem_key": p["key"], "opened_at": p["opened_at"],
                              "count": p["confirmation_count"], "started_at": p["confirmation_started_at"],
                              "at": p["last_seen"]}
                             for p in list(state.get("problems", {}).values()) + list(state.get("recent", {}).values())
                             if p.get("confirmation_count") and p.get("confirmation_started_at")]
    item["runs"] = [{"server": r.server, "check": r.check, "at": at,
                     "state": "success" if r.ran else "failed" if (
                         (state.get("sec") or {}).get("fails", {}).get(r.server, {}).get(r.check)) else "not_due"}
                    for r in results]
    item["baseline_state"] = "available" if trust is not None else "unavailable"
    item["diagnostics"] = {"queued_alerts": len(state.get("outbox") or []),
                           "queued_changes": len(state.get("cabinet_queue") or [])}
    if partial:
        item["partial"] = True
    if trust is not None:
        item["trust"] = trust
    return item


def _rank(level) -> int:
    return {"ok": 0, "warn": 1, "crit": 2}.get(level, 0)


# ---------- the queue ----------

def enqueue(state: dict, items, now) -> None:
    """The records go to the end of the queue; there is one sync: a new one absorbs the status changes of the previous.
    The bound is QUEUE_DAYS and QUEUE_MAX: an old update goes, open and resolve stay."""
    queue = state.setdefault("cabinet_queue", [])
    stamp = now.isoformat()
    for item in items:
        if item["kind"] == "sync":
            old = [q for q in queue if q["kind"] == "sync"]
            if old:
                item = _merge_sync(old, item)
                queue[:] = [q for q in queue if q["kind"] != "sync"]
        queue.append(dict(item, queued=stamp))
    edge = (now - timedelta(days=QUEUE_DAYS)).isoformat()
    queue[:] = [q for q in queue if q["kind"] in KEPT or q.get("queued", stamp) >= edge]
    if len(queue) > QUEUE_MAX:
        spare = len(queue) - QUEUE_MAX
        kept = []
        for q in queue:
            if spare and q["kind"] not in KEPT and q["kind"] != "sync":
                spare -= 1
                continue
            kept.append(q)
        queue[:] = kept


def _merge_sync(old, item) -> dict:
    """Unsent syncs + the new one → one: everyone's status changes, the last result of each (server, check), the
    baseline's lists by server. A new partial one does not lose the servers of a previous full one: the list is the
    previous one with its servers on top, and the whole sync is partial only when all of them are."""
    merged = [c for q in old for c in q.get("changes", [])] + item.get("changes", [])
    checks = {}
    for q in old + [item]:
        for c in q.get("checks", []):
            checks[(c["server"], c["check"])] = c
    out = dict(item, changes=_newest(merged), checks=[checks[k] for k in sorted(checks)])
    confirmations = {}
    for q in old + [item]:
        for c in q.get("confirmations", []):
            key = (c["problem_key"], c["opened_at"])
            if key not in confirmations or c["count"] > confirmations[key]["count"]:
                confirmations[key] = c
    out["confirmations"] = list(confirmations.values())
    runs = {(r["server"], r["check"]): r for q in old + [item] for r in q.get("runs", [])}
    out["runs"] = list(runs.values())
    if item.get("partial"):
        servers = {}
        for q in old + [item]:
            if not q.get("partial"):
                servers = {}  # a full sync — the whole list as of that moment
            servers.update({s["alias"]: s for s in q.get("servers", [])})
        out["servers"] = [servers[a] for a in sorted(servers)]
        if not all(q.get("partial") for q in old):
            out.pop("partial")
    trusts = [q["trust"] for q in old + [item] if q.get("trust") is not None]
    if trusts:
        listed = {}
        for t in trusts:
            listed.update(t.get("servers") or {})
        out["trust"] = {"trusted": trusts[-1].get("trusted") or {}, "servers": listed}
    return out


def _newest(changes) -> list:
    """The latest status changes — no more than CHANGES_MAX and CHANGES_BYTES: a sync merged over an outage does not
    outgrow the socket's request (1 MB), otherwise it would never leave."""
    out, size = [], 0
    for c in reversed(changes[-CHANGES_MAX:]):
        size += len(json.dumps(c, ensure_ascii=False).encode("utf-8")) + 1
        if size > CHANGES_BYTES:
            break
        out.append(c)
    return out[::-1]


def _batches(items):
    batch, size = [], 0
    for item in items:
        n = len(json.dumps(item, ensure_ascii=False).encode("utf-8")) + 1
        if batch and size + n > BATCH_BYTES:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += n
    if batch:
        yield batch


def _wire(item) -> dict:
    return {k: v for k, v in item.items() if k != "queued"}


def flush(items, cab, clock, deadline) -> tuple:
    """Send the queue in order. -> (answers {key: answer}, an error or None). The first failure stops it: the rest waits."""
    answers = {}
    for batch in _batches([_wire(i) for i in items]):
        if deadline - clock() < CALL_TIMEOUT_S:
            return answers, "бюджет тика"
        try:
            out = cab.call({"op": "monitor.push", "items": batch})
        except (cabinet_client.CabinetError, OSError, ValueError) as exc:
            return answers, f"{getattr(exc, 'kind', type(exc).__name__)}: {exc}"
        for r in out.get("results", []):
            if isinstance(r, dict) and r.get("key"):
                answers[r["key"]] = r
    return answers, None


def settle(state: dict, answers: dict) -> dict:
    """The cabinet's answers go into the state: what was sent leaves the queue, the numbers go to problems, recent and
    outbox. -> {(problem key, opened_at): "P-n"} — for a wake composed before the write."""
    numbers, dropped = {}, []
    queue = state.get("cabinet_queue") or []
    for item in queue:
        r = answers.get(item["key"])
        if r is None:
            continue
        if r.get("error"):
            dropped.append(f"{item['kind']} {item.get('problem_key') or item.get('problem', {}).get('key', '')}: "
                           f"{r['error']}")
        elif item["kind"] == "open" and r.get("incident"):
            p = item["problem"]
            numbers[(p["key"], p["opened_at"])] = f"P-{r['incident']}"
    state["cabinet_queue"] = [q for q in queue if q["key"] not in answers]
    for line in dropped:
        print(f"kd-monitor: кабинет не принял запись ({line})", file=sys.stderr)
    apply_numbers(state, numbers)
    return numbers


def apply_numbers(state: dict, numbers: dict) -> None:
    if not numbers:
        return
    for p in state["problems"].values():
        n = numbers.get((p["key"], p["opened_at"]))
        if n and not p.get("id"):
            p["id"] = n
    recent = state.get("recent") or {}
    for rk in [k for k in recent if k.startswith("~")]:
        p = recent[rk]
        n = numbers.get((p.get("key"), p.get("opened_at")))
        if n:
            recent[n] = dict(recent.pop(rk), id=n)
    for e in state.get("outbox") or []:
        n = numbers.get((e.get("key"), e.get("opened_at")))
        if n and not e.get("problem_id"):
            e["problem_id"] = n


# ---------- acknowledgement and muting ----------

def read_acks(cab):
    """{"P-n": acked_until (close | a time in the host's zone | None)} of open incidents; None — the cabinet did not answer."""
    try:
        items, before = [], None
        while True:
            request = {"op": "incident.list", "status": "active"}
            if before is not None:
                request["before"] = before
            page = cab.call(request)["items"]
            items.extend(page)
            if len(page) < 500:
                break
            following = min(i["n"] for i in page)
            if before is not None and following >= before:
                raise ValueError("incident pagination did not advance")
            before = following
    except (cabinet_client.CabinetError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"kd-monitor: кабинет не ответил ({getattr(exc, 'kind', type(exc).__name__)}): "
              "подтверждения — последние известные", file=sys.stderr)
        return None
    out = {}
    for i in items:
        if i.get("status") == "acknowledged":
            out[i["id"]] = "close"
        elif i.get("muted_until"):
            out[i["id"]] = _utc_to_local(i["muted_until"])
        else:
            out[i["id"]] = None
    return out


def apply_acks(state: dict, acks) -> None:
    if acks is None:
        return
    for p in state["problems"].values():
        if p.get("id") in acks:
            p["acked_until"] = acks[p["id"]]


def ack(cab, problem_id: str, until) -> None:
    """kd-monitor ack — through the cabinet: until closing — incident.ack, until a time — incident.mute. CabinetError goes out."""
    if until is None:
        cab.call({"op": "incident.ack", "incident": problem_id})
    else:
        cab.call({"op": "incident.mute", "incident": problem_id, "until": until.isoformat()})


# ---------- the owner's settings over config.json ----------

def read_settings(cab):
    """{"revision", "items"} — the owner's edits from the cabinet (`monitor.settings.get`); None — the cabinet did not answer."""
    try:
        out = cab.call({"op": "monitor.settings.get"})
        revision, items = out["revision"], out["items"]
    except (cabinet_client.CabinetError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"kd-monitor: настройки из кабинета не получены ({getattr(exc, 'kind', type(exc).__name__)}): "
              "тик — по последнему набору", file=sys.stderr)
        return None
    if not isinstance(items, list):
        return None
    return {"revision": revision,
            "items": [{"key": i.get("key"), "server": i.get("server") or "", "value": i.get("value")}
                      for i in items if isinstance(i, dict)]}


def apply_settings(cfg: dict, saved) -> dict:
    """config.json with the edits of the set (the last one received from the cabinet; none — config.json as it is). An
    edit the monitor will not take (an unknown key, a bad value) is skipped; a set that breaks the thresholds (crit no
    stricter than warn) is not applied at all — the tick runs on config.json."""
    if not isinstance(saved, dict) or not isinstance(saved.get("items"), list):
        return cfg
    good = []
    for item in saved["items"]:
        try:
            if not isinstance(item, dict) or monitor_settings.stale(cfg, item.get("key"), item.get("server") or ""):
                continue
            value = monitor_settings.validate(item["key"], item.get("server") or "", item.get("value"), None)
        except (monitor_settings.Invalid, KeyError, TypeError):
            print(f"kd-monitor: правка {str(item.get('key'))[:80]} не применена", file=sys.stderr)
            continue
        good.append(dict(item, value=value))
    merged = monitor_settings.merge(cfg, good)
    try:
        monitor_settings.check_config(merged)
    except monitor_settings.Invalid as exc:
        print(f"kd-monitor: настройки кабинета не применены ({exc}): тик — по config.json", file=sys.stderr)
        return cfg
    return merged


# ---------- carrying state.json over ----------

def _import_items(state: dict) -> list:
    out = []
    for source, closed in ((state.get("problems") or {}).values(), False), ((state.get("recent") or {}).values(), True):
        for p in source:
            pid = p.get("id") if isinstance(p, dict) else None
            if not (isinstance(pid, str) and pid.startswith("P-") and pid[2:].isdigit()):
                continue
            try:
                server, check, subject = split_key(p)
            except (KeyError, ValueError):
                continue
            fixed = p.get("fixed")
            card = fixed.get("card", "") if isinstance(fixed, dict) else ""
            if not card:
                card = next((h.get("value", "") for h in reversed(p.get("history") or [])
                             if h.get("kind") == "fixed"), "")
            item = {"number": int(pid[2:]), "key": p["key"], "server": server, "check": check, "subject": subject,
                    "level": p.get("level", "warn"), "max_level": p.get("max_level", p.get("level", "warn")),
                    "value": str(p.get("value", ""))[:VALUE_MAX], "text": str(p.get("text", ""))[:TEXT_MAX],
                    "value_hash": hashlib.sha256(str(p.get("value", "")).encode()).hexdigest(),
                    "opened_at": p["opened_at"], "last_seen": p.get("last_seen") or p["opened_at"],
                    "repeats": int(p.get("repeats") or 0), "fixed_card": card,
                    "history": [h for h in (p.get("history") or []) if isinstance(h, dict)]}
            if closed:
                item["closed_at"] = p.get("closed_at") or p["opened_at"]
            elif p.get("acked_until"):
                item["acked_until"] = p["acked_until"]
            out.append(item)
    return out


def import_needed(state: dict) -> bool:
    return not state.get("cabinet_imported")


def keep_copy(state_path: Path, state: dict) -> None:
    """state.json as it was before the carry-over — beside it, state.json.pre-kd9054: once, before the new code's first write."""
    copy = state_path.with_name(state_path.name + PRE_COPY)
    if import_needed(state) and not copy.exists() and state_path.exists():
        shutil.copy2(str(state_path), str(copy))


def import_state(state_path: Path, state: dict, cab, clock, deadline):
    """The carry-over into the cabinet. -> the reconciliation line ("" — there was nothing to carry) or None (it failed —
    the next tick repeats; a repeat doubles nothing)."""
    copy = state_path.with_name(state_path.name + PRE_COPY)
    items = _import_items(state)
    next_id = max([int(state.get("next_id") or 1)] + [i["number"] + 1 for i in items])
    added = kept = 0
    errors = []
    batches = list(_batches(items)) or [[]]
    for batch in batches:
        if deadline - clock() < CALL_TIMEOUT_S:
            return None
        try:
            out = cab.call({"op": "incident.import", "next_id": next_id, "items": batch})
        except (cabinet_client.CabinetError, OSError, ValueError) as exc:
            print(f"kd-monitor: перенос в кабинет отложен ({getattr(exc, 'kind', type(exc).__name__)}: {exc})",
                  file=sys.stderr)
            return None
        added += len(out.get("added", []))
        kept += len(out.get("kept", []))
        errors += out.get("errors", [])
    opened = sum(1 for p in (state.get("problems") or {}).values() if p.get("id"))
    recent = len(state.get("recent") or {})
    if not items and not errors:
        return ""  # there was nothing to carry: nothing to reconcile, no journal line is needed
    line = (f"kd-monitor: перенос в кабинет: открытых {opened}, недавних {recent}, перенесено {added}, уже были {kept}, "
            f"не приняты {len(errors)}, счётчик с P-{next_id}; копия {copy.name}")
    for e in errors:
        print(f"kd-monitor: перенос: P-{e.get('number')} не принята: {e.get('error')}", file=sys.stderr)
    return line
