"""A card's brief as a versioned document (remediation spec R17).

The brief's scope is the card's description and the texts of its acceptance criteria — nothing else: ticking a
criterion, a note, a comment, the status or the title leave it as it is. A version is a frozen copy of the scope
with its hash: the analyst's handoff with ``brief-ready`` records the next one (``store.edit_card`` calls
``after_edit`` in the same transaction), a person may save the current text as one, and an approval marks
exactly one version — an approved version never changes. Once a version exists, a write that changes the scope
drops ``brief-ready`` (``adjust_labels``): the brief is stale until someone records the new text.

Nothing about the brief's progress is stored beside it: ``state`` reads it from the facts — who holds the card,
the analyst's open questions, its last command and session in the card's thread, the versions and the label.
A card that has the label from before the versions existed is a legacy brief: ready, never approved.
"""

import difflib
import hashlib
import json
from typing import List, Mapping, Optional

ANALYST = "@analyst"
BRIEF_SPEC = "spec:task-brief"
BRIEF_READY = "brief-ready"
SIZE = "size:"  # the analyst's size of the brief: size:S | size:M | size:L
STATES = ("not_requested", "queued", "in_progress", "waiting_user", "in_council", "ready", "stale", "failed")
READY = ("ready", "in_council")  # a version the owner may approve; ``in_council`` — its critics still read
ORIGINS = ("agent", "person", "legacy")
ENDED = "brief_ended"  # the analyst's turn ended with neither a handoff nor a question
NOT_STARTED = "brief_not_started"  # the card is the analyst's, but nothing woke it
_AGENT = ANALYST[1:]


class Stale(Exception):
    """The card's scope moved on from its last version: there is nothing current to approve."""


def scope(record) -> dict:
    """The description, the criteria and, only when there are any, the card's "Operations" and "Stages":
    a brief without them keeps the hash it always had."""
    return _scope_of(record.description, [a.text for a in record.acceptance], record.operations, record.stages)


def _scope_of(description: str, acceptance, operations=(), stages=()) -> dict:
    out = {"description": description, "acceptance": [a["text"] if isinstance(a, Mapping) else a for a in acceptance]}
    if operations:
        out["operations"] = [dict(op) for op in operations]
    if stages:
        out["stages"] = [dict(s) for s in stages]
    return out


def _json_list(value) -> str:
    return json.dumps(value, ensure_ascii=False) if value else ""


def scope_sha(value: Mapping) -> str:
    data = json.dumps(dict(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _row(row) -> dict:
    data = dict(row)
    data["acceptance"] = json.loads(data["acceptance"])
    data["operations"] = json.loads(data["operations"]) if data.get("operations") else []
    data["stages"] = json.loads(data["stages"]) if data.get("stages") else []
    return data


def versions(store, card_id: str) -> List[dict]:
    rows = store._conn.execute(
        "SELECT * FROM brief_versions WHERE card=? ORDER BY version", (card_id,)
    ).fetchall()
    return [_row(r) for r in rows]


def latest(store, card_id: str) -> Optional[dict]:
    row = store._conn.execute(
        "SELECT * FROM brief_versions WHERE card=? ORDER BY version DESC LIMIT 1", (card_id,)
    ).fetchone()
    return _row(row) if row is not None else None


def add_version(store, record, *, origin: str, author: str, size: str = "") -> Optional[dict]:
    """The card's current scope as its next version; None when it is the last version's text already. ``size`` —
    the ``size:`` label the same write set, carried in the event for the S start."""
    if origin not in ORIGINS:
        raise ValueError(f"unknown brief origin {origin!r}")
    current = scope(record)
    sha = scope_sha(current)
    with store._tx():
        last = latest(store, record.id)
        if last is not None and last["scope_sha"] == sha:
            return None
        number = (last["version"] if last else 0) + 1
        store._conn.execute(
            "INSERT INTO brief_versions (card, version, origin, author, description, acceptance, scope_sha, created,"
            " operations, stages) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (record.id, number, origin, author, current["description"],
             json.dumps(current["acceptance"], ensure_ascii=False), sha, store._clock(),
             _json_list(current.get("operations")), _json_list(current.get("stages"))),
        )
        payload = {"version": number, "origin": origin, **({"size": size} if size else {})}
        store._event(author, "brief.version", record.id, payload, record.thread)
        return latest(store, record.id)


def approve(store, record, *, by: str) -> Optional[dict]:
    """Approve the brief the person sees: the ready version (once), a legacy brief as its first version; a stale
    brief is refused (``Stale``); no brief at all is None."""
    with store._tx():
        seen = state(store, record)
        if seen["state"] == "stale":
            raise Stale(f"{record.id}: the brief changed after v{seen['version']}")
        if seen["legacy"]:
            add_version(store, record, origin="legacy", author=by)
        elif seen["state"] not in READY:
            return None
        last = latest(store, record.id)
        if last is None or last["approved_by"]:
            return last
        store._conn.execute(
            "UPDATE brief_versions SET approved_by=?, approved_at=? WHERE card=? AND version=?",
            (by, store._clock(), record.id, last["version"]),
        )
        store._event(by, "brief.approved", record.id, {"version": last["version"]}, record.thread)
        return latest(store, record.id)


def _preparing(store, record) -> Optional[dict]:
    """The state while the analyst holds the card with its brief to write, else None."""
    if record.assignee != ANALYST or BRIEF_SPEC not in record.labels:
        return None
    asked = store._conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE card=? AND type='question' AND state='open' AND requested_by=?",
        (record.id, f"agent:{_AGENT}"),
    ).fetchone()[0]
    if asked:
        return {"state": "waiting_user", "questions": int(asked), "error": ""}
    last = store.commands(thread=record.thread, agent=_AGENT, limit=1)
    command = last[0]["state"] if last else ""
    sessions = store.sessions(thread=record.thread, agent=_AGENT)
    session = sessions[0]["state"] if sessions else ""
    if command in ("queued", "delivered"):
        return {"state": "queued", "questions": 0, "error": ""}
    if command == "started" or session in ("starting", "working"):
        return {"state": "in_progress", "questions": 0, "error": ""}
    if command == "failed":
        return {"state": "failed", "questions": 0, "error": last[0]["error"] or last[0]["reason"]}
    if session == "failed":
        return {"state": "failed", "questions": 0, "error": sessions[0]["error"]}
    if session == "queued":
        return {"state": "queued", "questions": 0, "error": ""}
    # nothing runs and nothing is asked: the turn ended without a brief, or the analyst was never woken —
    # either way a retry is the next step, not a wait
    return {"state": "failed", "questions": 0, "error": ENDED if command else NOT_STARTED}


def state(store, record) -> dict:
    """What the brief of ``record`` is now (``STATES``), the number of its last version and whether it is
    approved, whether it is a legacy label, the analyst's open questions and the reason of a failure. A card whose
    brief had a council carries it (``council``: ``councils.facts``), and a ready version nobody approved is
    ``in_council`` until that council ends."""
    from kd.cabinet import councils  # at call time: it loads the API, which loads this module

    last = latest(store, record.id)
    out = {
        "state": "not_requested", "version": last["version"] if last else 0,
        "approved": bool(last and last["approved_by"]), "legacy": False, "questions": 0, "error": "",
    }
    preparing = _preparing(store, record)
    if preparing is not None:
        out.update(preparing)
    elif last is not None:
        out["state"] = "ready" if last["scope_sha"] == scope_sha(scope(record)) else "stale"
    elif BRIEF_READY in record.labels:
        out.update(state="ready", legacy=True)
    gathered = councils.facts(store, record.id, "brief")
    if gathered is not None:
        out["council"] = gathered
        if out["state"] == "ready" and not out["approved"] and gathered["state"] in councils.OPEN:
            out["state"] = "in_council"
    return out


# --- the hooks of store.edit_card (one transaction with the card's write) ------------------------------
def _added(changes: Mapping) -> bool:
    return BRIEF_READY in (changes.get("labels_add") or ()) or BRIEF_READY in (changes.get("labels") or ())


def adjust_labels(store, before, changes: Mapping, columns: dict) -> None:
    """A write that changes the scope of a ready brief takes ``brief-ready`` off in the same write."""
    if not {"description", "acceptance", "operations", "stages"} & set(columns):
        return
    labels = json.loads(columns["labels"]) if "labels" in columns else list(before.labels)
    if BRIEF_READY not in labels or _added(changes):
        return
    last = latest(store, before.id)
    if last is None:
        return  # a legacy label: nothing to compare with
    acceptance = json.loads(columns["acceptance"]) if "acceptance" in columns else [a.text for a in before.acceptance]
    operations = json.loads(columns["operations"] or "[]") if "operations" in columns else before.operations
    stages = json.loads(columns["stages"] or "[]") if "stages" in columns else before.stages
    after = _scope_of(columns.get("description", before.description), acceptance, operations, stages)
    if scope_sha(after) != last["scope_sha"]:
        columns["labels"] = json.dumps([label for label in labels if label != BRIEF_READY], ensure_ascii=False)


def after_edit(store, before, after, changes: Mapping, actor: str) -> None:
    """``brief-ready`` set by this write records the scope as the next version."""
    if BRIEF_READY not in after.labels or (BRIEF_READY in before.labels and not _added(changes)):
        return
    added = [v for v in changes.get("labels_add") or () if str(v).startswith(SIZE)]
    size = added[0][len(SIZE):] if len(added) == 1 else ""  # only the size this write gives, never a stale one
    add_version(store, after, origin="agent" if actor.startswith("agent:") else "person", author=actor, size=size)


# --- what a reader sees -------------------------------------------------------------------------------------
def render(value: Mapping) -> str:
    """The scope as one text: the description, an empty line, a ``- `` line per criterion, the "Operations" when
    the brief has them — a step's kind, target, roles and what on one line, then its effect, risk with why and
    rollback (everything the approval grants is compared) — and the "Stages": key, role,
    specialization, title and what each waits for, then its criteria."""
    text = value.get("description", "")
    criteria = [f"- {a}" for a in value.get("acceptance", [])]
    text += "\n\n" + "\n".join(criteria) if criteria else ""
    steps = []
    for op in value.get("operations") or ():
        risk = op.get("risk") or {}
        steps += [f"- {op.get('verb')} · {op.get('target', '')} · {', '.join(op.get('agents') or ())} · "
                  f"{op.get('what', '')}",
                  f"  эффект: {op.get('effect', '')}",
                  f"  риск: {risk.get('level', '')} — {risk.get('why', '')}",
                  f"  откат: {op.get('rollback', '')}"]
    text += "\n\nОперации:\n" + "\n".join(steps) if steps else ""
    parts = []
    for stage in value.get("stages") or ():
        after = f" (после: {', '.join(stage.get('depends') or ())})" if stage.get("depends") else ""
        parts.append(f"- {stage.get('key')} · {stage.get('role', '')} · {stage.get('spec') or '—'} · "
                     f"{stage.get('title', '')}{after}")
        parts += [f"  {stage['description']}"] if stage.get("description") else []
        parts += [f"  - {a}" for a in stage.get("acceptance") or ()]
    return text + ("\n\nЭтапы:\n" + "\n".join(parts) if parts else "")


def diff(old: Mapping, new: Mapping, context: int = 2) -> List[dict]:
    """Line changes from ``old`` to ``new``: ``=`` kept, ``-`` removed, ``+`` added; a run of kept lines farther
    than ``context`` from a change is one ``…`` item whose text is the number of lines it stands for."""
    a, b = render(old).split("\n"), render(new).split("\n")
    out: List[dict] = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag != "equal":
            out += [{"op": "-", "text": line} for line in a[i1:i2]]
            out += [{"op": "+", "text": line} for line in b[j1:j2]]
            continue
        run = a[i1:i2]
        head = 0 if i1 == 0 else context
        tail = 0 if i2 == len(a) else context
        if len(run) <= head + tail + 1:  # hiding one line saves nothing
            out += [{"op": "=", "text": line} for line in run]
            continue
        out += [{"op": "=", "text": line} for line in run[:head]]
        out.append({"op": "…", "text": str(len(run) - head - tail)})
        out += [{"op": "=", "text": line} for line in run[len(run) - tail:]]
    return out
