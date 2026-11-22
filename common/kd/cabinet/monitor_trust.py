"""The monitor's trust by the owner's signature: ``monitor_trust`` decisions without a card.

What becomes trusted — an incident's object into the baseline (``accept`` P-n), a server's new baseline (``baseline``
<alias>|all), an address on the shared trusted list (``trust`` <ip|cidr> with a label) — is only proposed here; it
takes effect when the owner signs the decision and the monitor applies it. ``revoke`` P-n is the other way
to close a login by a key: the access is gone — the monitor checks that the key is no longer allowed on the server and
closes the P-n without trusting it, so the key's next login opens a new one:

- ``monitor_trust.propose {kind, incident|server|cidr, label?, text?}`` — the sysadmin (``kd-monitor accept|baseline|
  trust`` runs as its user) or a person with ``decide.monitor_trust`` (the page's button; it signs right after). The
  same open proposal answers the existing decision. An accept needs its P-n open and a check that has a baseline.
- the decision lives in the sysadmin's "Monitor" thread, readable with ``servers.read``; the signature binds the P-n's
  status and its ``updated`` instead of a card revision (``basis``). A P-n that closes before the signature expires
  its open accepts (``expire_for``).
- ``monitor_trust.take`` (the monitor) — the signed decisions not applied yet and the queued removals;
  ``monitor_trust.report {id|removal, ok, detail}`` — applied, or the error and why (``apply_state``). A report on what
  is already reported changes nothing, so the monitor may repeat it after a lost answer.
- ``monitor_trust.remove {cidr}`` — taking an address off the trusted list needs no signature (the owner's word,
  22.10.2026): a queued row the monitor applies on its tick, every step in the journal.

Every step is a ``monitor_trust.*`` event (proposed, signed, rejected, expired, applied, failed, removal_requested,
removed, removal_failed); an accept's events carry its incident, so the P-n's timeline shows them.
"""

import ipaddress
import hashlib
import json
import secrets

from kd.cabinet import api, decisions, incidents

TYPE = decisions.TRUST
KINDS = ("accept", "baseline", "trust", "revoke")
# the kinds whose object is an incident: one signed of them closes it, a second would fail "P-n is closed"
CLOSING = ("accept", "revoke")
# checks whose object has a baseline (the monitor's Baseline.accept); the rest close by ack, never by accept
ACCEPT_CHECKS = ("logins", "ports", "wgpeers")
WEAK = ("@password", "@key ?")  # a login without a key: no key to take away (the monitor's baseline.WEAK)
MIN_PREFIX = {4: 16, 6: 112}  # no wider than /16 (v6 /112): 0.0.0.0/0 would silence every login
LABEL_MAX = 200
TEXT_MAX = 2000
DETAIL_MAX = 500
ALL = "all"


def _monitor(principal) -> bool:
    return principal.kind == "agent" and principal.agent == incidents.MONITOR_AGENT


def _proposer(ctx, principal) -> str:
    """Who may propose (and take an address off): the sysadmin, a person who decides monitor trust, root."""
    if principal.kind == "root" or _monitor(principal):
        return api.card_actor(principal)
    if principal.kind == "user" and api.allowed(ctx, principal, f"decide.{TYPE}"):
        return api.card_actor(principal)
    raise api.ApiError("refused", f"no permission decide.{TYPE}")


def acceptable(check: str, subject: str) -> str:
    """Why an incident's object cannot go into the baseline, or "" — it can (the monitor's own rules)."""
    if check not in ACCEPT_CHECKS and not check.startswith("sec:"):
        return f"accept не применим к проверке {check}: это закрывает подтверждение (ack)"
    if check == "logins" and subject == "gap":
        return "logins gap — переполнение журнала входов, а не личность: это закрывает подтверждение (ack)"
    return ""


def revocable(check: str, subject: str) -> str:
    """Why an incident cannot close as "access revoked", or "" — it can: a login by a key, which the monitor
    then checks is no longer allowed on the server and closes without putting it into the baseline."""
    if check != "logins" or subject == "gap":
        return "«доступ снят» — только для входа по ключу"
    if subject.endswith(WEAK):
        return "вход без ключа: снимать нечего — примите событие входа и отключите вход по паролю"
    return ""


def _key(net) -> str:
    return str(net.network_address) if net.num_addresses == 1 else str(net)


def address(value) -> str:
    """An address or subnet as the baseline keys it; ApiError when it is none or too wide."""
    try:
        net = ipaddress.ip_network(str(value or "").strip(), strict=False)
    except ValueError:
        raise api.ApiError("invalid", "cidr is an IP address or a subnet")
    if net.prefixlen < MIN_PREFIX[net.version]:
        raise api.ApiError("invalid", f"слишком широкая подсеть {net}: не шире /{MIN_PREFIX[net.version]}")
    return _key(net)


def _label(value, required: bool) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str) or len(value) > LABEL_MAX or (required and not value.strip()):
        raise api.ApiError("invalid", f"label is text of {1 if required else 0} to {LABEL_MAX} characters")
    return value.strip()


def _payload(ctx, request) -> dict:
    kind = request.get("kind")
    if kind not in KINDS:
        raise api.ApiError("invalid", f"kind is one of {', '.join(KINDS)}")
    s = ctx.store
    if kind in CLOSING:
        found = incidents._get(s, incidents._number(request))
        if found is None:
            raise api.ApiError("not_found", "no such incident")
        if found["status"] == "resolved":
            raise api.ApiError("refused", f"P-{found['n']} закрыта")
        why = (acceptable if kind == "accept" else revocable)(found["check"], found["subject"])
        if why:
            raise api.ApiError("refused", why)
        signed = accepted(s, found["n"])
        if signed:  # a second signature would only fail "P-n is closed" on the next tick
            raise api.ApiError("refused", f"P-{found['n']} уже " + (
                "закрывается как «доступ снят»" if signed["kind"] == "revoke" else "принята в эталон")
                + ": подписано, ждёт применения монитором")
        out = {"kind": kind, "incident": found["n"], "server": found["server"], "check": found["check"],
               "subject": found["subject"], "value": found["value"]}
        if found["value_hash"]:
            out["value_hash"] = found["value_hash"]
        label = _label(request.get("label"), required=False) if kind == "accept" else ""
        if label:
            out["label"] = label
        return out
    if kind == "baseline":
        alias = request.get("server")
        if alias != ALL:
            row = s._conn.execute("SELECT enabled FROM servers WHERE alias=?", (alias,)).fetchone() if isinstance(
                alias, str) else None
            if row is None or not row["enabled"]:
                raise api.ApiError("invalid", f"{alias} is not a server the monitor checks")
        return {"kind": kind, "server": alias}
    return {"kind": kind, "cidr": address(request.get("cidr")), "label": _label(request.get("label"), required=True)}


# one open accept per P-n whatever its label: two signed would close it twice — the second "error: P-n is closed"
OBJECT = {"accept": ("incident",), "baseline": ("server",), "trust": ("cidr", "label"), "revoke": ("incident",)}


def _same(decision, payload) -> bool:
    """The same proposal: its kind and object (the P-n, the server, the address with its label)."""
    p = decision["payload"]
    return p.get("kind") == payload["kind"] and all(p.get(k) == payload.get(k) for k in OBJECT[payload["kind"]])


def trust_decisions(store_, state=None) -> list:
    """The monitor_trust decisions without a card, oldest first (one state, or all)."""
    where, args = "WHERE d.type=? AND d.card=''", (TYPE,)
    if state is not None:
        where, args = where + " AND d.state=?", args + (state,)
    return decisions.Decisions(store_)._rows(where, args)


def thread_of(ctx) -> int:
    return ctx.store.agent_thread(incidents.MONITOR_AGENT, "monitor")


def _text(request) -> str:
    text = request.get("text") or ""
    if not isinstance(text, str) or len(text) > TEXT_MAX:
        raise api.ApiError("invalid", f"text is at most {TEXT_MAX} characters")
    return text.strip()


def _open_one(ctx, ledger, payload, actor, text, bundle="") -> tuple:
    """(decision, repeat) inside the caller's transaction: the same open proposal answers again; the other way to
    close the same P-n (accept ↔ revoke) is replaced by this one."""
    for open_ in trust_decisions(ctx.store, "open"):
        other = open_["payload"].get("kind")
        if (payload["kind"] in CLOSING and other in CLOSING and other != payload["kind"]
                and open_["payload"].get("incident") == payload["incident"]):
            ledger.expire_one(open_["id"], now=ctx.now())
            continue
        if _same(open_, payload):
            if payload["kind"] in CLOSING and (open_["payload"].get("value"), open_["payload"].get("value_hash")) != (payload.get("value"), payload.get("value_hash")):
                ledger.expire_one(open_["id"], now=ctx.now())
                continue
            return open_, True  # proposed again: the same open decision
    full = {**payload, "proposed_by": actor, **({"text": text} if text else {}), **({"bundle": bundle} if bundle else {})}
    return ledger.open(TYPE, card="", payload=full, requested_by=actor, now=ctx.now(), thread=thread_of(ctx)), False


def _propose(ctx, principal, request):
    actor = _proposer(ctx, principal)
    payload = _payload(ctx, request)
    text = _text(request)
    with ctx.store.transaction():
        opened, repeat = _open_one(ctx, decisions.Decisions(ctx.store), payload, actor, text)
    return {"decision": opened, "repeat": repeat}


def _propose_many(ctx, principal, request):
    """Accepts (or "access revoked", ``kind`` revoke) of many incidents (a group of one tick) as one
    bundle the owner signs once; each that cannot be is refused with its reason, the rest are proposed."""
    actor = _proposer(ctx, principal)
    items = request.get("incidents")
    if not isinstance(items, list) or not 1 <= len(items) <= api.BATCH_MAX:
        raise api.ApiError("invalid", f"incidents is a list of 1 to {api.BATCH_MAX} P-n")
    kind = request.get("kind", "accept")
    if kind not in CLOSING:
        raise api.ApiError("invalid", f"kind is one of {', '.join(CLOSING)}")
    text, bundle = _text(request), secrets.token_hex(8) if len(items) > 1 else ""
    ledger, out, refused = decisions.Decisions(ctx.store), [], []
    with ctx.store.transaction():
        for item in items:
            try:
                payload = _payload(ctx, {"kind": kind, "incident": item})
            except api.ApiError as exc:
                name = item if isinstance(item, str) else f"P-{item}"
                refused.append({"incident": str(name)[:20], "error": str(exc)})
                continue
            opened, _ = _open_one(ctx, ledger, payload, actor, text, bundle)
            if all(d["id"] != opened["id"] for d in out):
                out.append(opened)
    return {"decisions": out, "refused": refused}


def basis(store_, decision) -> str:
    """Bind the object's value and status, independently of an unchanged observation's polling clock."""
    n = decision["payload"].get("incident")
    if not n:
        return ""
    row = incidents._get(store_, n)
    if row is None:
        return "gone"
    value = [row[k] for k in ("status", "check", "subject", "value", "value_hash")]
    return f"{row['status']}|" + hashlib.sha256(json.dumps(value, ensure_ascii=False).encode()).hexdigest()


def expire_moved(store_, moved, now: str) -> None:
    """the objects of these P-n moved on — ``moved`` is (n, value, value_hash) as the incident holds them now.
    An open accept of an older value is moot: the owner would sign what is no longer there and the monitor refuse it
    ("the object changed after the signature"); expired, it is proposed again over the new value."""
    now_of = {n: (value, value_hash or "") for n, value, value_hash in moved}
    if not now_of:
        return
    ledger = decisions.Decisions(store_)
    for d in trust_decisions(store_, "open"):
        p = d["payload"]
        if p.get("kind") in CLOSING and p.get("incident") in now_of and (
                (p.get("value"), p.get("value_hash") or "") != now_of[p["incident"]]):
            ledger.expire_one(d["id"], now=now)


def expire_for(store_, n: int, now: str) -> None:
    """The P-n closed: its open accepts are moot (before the signature — expired, the owner sees why)."""
    ledger = decisions.Decisions(store_)
    for d in trust_decisions(store_, "open"):
        if d["payload"].get("incident") == n:
            ledger.expire_one(d["id"], now=now)


def readable(ctx, principal) -> bool:
    return principal.kind != "user" or api.allowed(ctx, principal, "servers.read")


def _writer(principal) -> None:
    if principal.kind != "root" and not _monitor(principal):
        raise api.ApiError("refused", "only the monitor (the sysadmin agent) and root apply monitor trust")


def _rows(s, sql, args=()) -> list:
    return [dict(r) for r in s._conn.execute(sql, args).fetchall()]


def _take(ctx, principal, request):
    _writer(principal)
    kinds = request.get("kinds", list(KINDS))
    if not isinstance(kinds, list) or not set(kinds) <= set(KINDS):
        raise api.ApiError("invalid", f"kinds is a list of {', '.join(KINDS)}")
    signed = [d for d in trust_decisions(ctx.store, "approved") if d["apply_state"] == "pending"
              and d["payload"].get("kind") in kinds]
    removals = _rows(ctx.store, "SELECT * FROM trust_removals WHERE state='queued' ORDER BY created, rowid") if (
        "trust" in kinds) else []
    return {"decisions": [{"id": d["id"], "payload": d["payload"], "decided_by": d["decided_by"],
                           "decided_at": d["decided_at"]} for d in signed],
            "removals": [{"id": r["id"], "cidr": r["item"], "requested_by": r["requested_by"]} for r in removals]}


def _detail(request) -> tuple:
    ok = request.get("ok")
    if not isinstance(ok, bool):
        raise api.ApiError("invalid", "ok is true or false")
    detail = request.get("detail") or ""
    return ok, (detail if isinstance(detail, str) else str(detail))[:DETAIL_MAX]


def _report(ctx, principal, request):
    _writer(principal)
    ok, detail = _detail(request)
    if "removal" in request:
        return _report_removal(ctx, principal, request["removal"], ok, detail)
    s, now = ctx.store, ctx.now()
    ledger = decisions.Decisions(s)
    with s.transaction():
        found = ledger.get(request.get("id"))
        if found is None or found["type"] != TYPE:
            raise api.ApiError("not_found", "no such monitor_trust decision")
        if found["state"] != "approved" or found["apply_state"] != "pending":
            return {"decision": found, "repeat": True}  # reported before (a lost answer): nothing changes
        state = "applied" if ok else "error"
        s._conn.execute("UPDATE decisions SET apply_state=?, apply_detail=?, applied_at=? WHERE id=?",
                        (state, detail, now, found["id"]))
        actor = incidents.MONITOR_ACTOR
        kind, data = decisions.trust_event(found, "applied" if ok else "failed", detail=detail)
        s._event(actor, kind, "", data, found["thread"])
        s._add_message(found["thread"], actor, "decision", "", {
            "decision": found["id"], "type": TYPE, "state": "approved",
            "key": "sys.monitor_trust.applied" if ok else "sys.monitor_trust.failed", "params": {"detail": detail}},
            "", via="decision", extra={"decision": found["id"]})
    return {"decision": ledger.get(found["id"]), "repeat": False}


def _report_removal(ctx, principal, removal_id, ok, detail):
    s, now = ctx.store, ctx.now()
    with s.transaction():
        row = s._conn.execute("SELECT * FROM trust_removals WHERE id=?", (removal_id,)).fetchone() if isinstance(
            removal_id, str) else None
        if row is None:
            raise api.ApiError("not_found", "no such removal")
        if row["state"] != "queued":
            return {"removal": dict(row), "repeat": True}
        state = "applied" if ok else "error"
        s._conn.execute("UPDATE trust_removals SET state=?, detail=?, updated=? WHERE id=?",
                        (state, detail, now, row["id"]))
        s._event(incidents.MONITOR_ACTOR, f"{TYPE}.{'removed' if ok else 'removal_failed'}", "",
                 {"removal": row["id"], "kind": "trust", "cidr": row["item"], "requested_by": row["requested_by"],
                  "detail": detail})
    return {"removal": dict(s._conn.execute("SELECT * FROM trust_removals WHERE id=?", (row["id"],)).fetchone()),
            "repeat": False}


def _remove(ctx, principal, request):
    actor = _proposer(ctx, principal)
    item = address(request.get("cidr"))
    s, now = ctx.store, ctx.now()
    with s.transaction():
        queued = s._conn.execute("SELECT * FROM trust_removals WHERE item=? AND state='queued'", (item,)).fetchone()
        if queued is not None:
            return {"removal": dict(queued), "repeat": True}
        row = {"id": secrets.token_hex(8), "item": item, "requested_by": actor, "state": "queued", "detail": "",
               "created": now, "updated": now}
        s._conn.execute("INSERT INTO trust_removals (id, item, requested_by, state, detail, created, updated)"
                        " VALUES (:id, :item, :requested_by, :state, :detail, :created, :updated)", row)
        s._event(actor, f"{TYPE}.removal_requested", "", {"removal": row["id"], "kind": "trust", "cidr": item})
    return {"removal": row, "repeat": False}


# --- what the pages show -------------------------------------------------------------------------------------
def shown(decision) -> dict:
    keep = ("id", "type", "state", "payload", "requested_by", "created", "expires", "decided_by", "decided_at",
            "reply", "apply_state", "apply_detail", "applied_at")
    return {k: decision.get(k) for k in keep}


def accepted(store_, n: int):
    """the P-n's signed accept (or revoke) the monitor has not failed — {decision, apply_state,
    decided_at, kind}, or None. Until its tick closes the P-n the incident stays open; it is no longer the owner's."""
    row = store_._conn.execute(
        "SELECT id, apply_state, decided_at, json_extract(payload, '$.kind') AS kind FROM decisions"
        " WHERE type=? AND card='' AND state='approved' AND apply_state IN ('pending','applied')"
        " AND json_extract(payload, '$.kind') IN ('accept','revoke')"
        " AND json_extract(payload, '$.incident')=? ORDER BY decided_at DESC, rowid DESC LIMIT 1", (TYPE, n)).fetchone()
    return {"decision": row["id"], "apply_state": row["apply_state"], "decided_at": row["decided_at"],
            "kind": row["kind"]} if row else None


def for_incident(store_, n: int) -> list:
    return [shown(d) for d in trust_decisions(store_) if d["payload"].get("incident") == n]


def for_server(store_, alias: str) -> dict:
    """A server page's proposals: its baselines (and those of all servers) and the shared list's addresses."""
    found = [shown(d) for d in trust_decisions(store_)
             if (d["payload"].get("kind") == "baseline" and d["payload"].get("server") in (alias, ALL))
             or d["payload"].get("kind") == "trust"]
    removals = _rows(store_, "SELECT * FROM trust_removals ORDER BY created DESC, rowid DESC LIMIT 50")
    return {"decisions": found[-50:], "removals": removals}


OPS = {
    "monitor_trust.propose": (frozenset({"root", "agent", "user"}), _propose),
    "monitor_trust.propose_many": (frozenset({"root", "agent", "user"}), _propose_many),
    "monitor_trust.remove": (frozenset({"root", "agent", "user"}), _remove),
    "monitor_trust.take": (frozenset({"root", "agent"}), _take),
    "monitor_trust.report": (frozenset({"root", "agent"}), _report),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
