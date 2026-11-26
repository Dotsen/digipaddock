"""Decisions: what an agent asks a person to approve, answer or refuse.

A decision is a row bound to a card and its thread: its type, the payload the agent opened it with
and that payload's hash. Opening it and every transition write a ``decision`` message to the thread
and a ``decision.<state>`` event in the same transaction. ``open`` → ``approved`` / ``rejected`` /
``expired`` / ``revoked``; «ask» leaves it open with the question for the agent.

A type with step-up (``STEP_UP``) is approved only with a passkey assertion over ``challenge``: the
digest binds the decision id, its payload hash, the card's revision, the person and the challenge's
expiry, so a signature over an older card or for another person does not fit. Several decisions are
signed at once over the sorted digests. The effect itself (a grant file, a promote) is not here:
``effects`` runs it through the existing grant writers before ``approve`` records the result.

A ``monitor_trust`` decision has no card: it lives in the sysadmin's "Monitor" thread, and its digest binds
the state of its P-n (status and updated; empty for a server or an address) where a card's revision would stand —
``basis_key`` names that slot. Each of its steps writes a ``monitor_trust.<step>`` event too: the P-n's timeline and
the journal show them.
"""

import hashlib
import json
import re
import secrets
from typing import Iterable, List, Mapping, Optional

from kd.cabinet import users, view

TYPES = (
    "admin_plan",
    "admin_critical",
    "delegate",
    "monitor_trust",
    "pr_approve",
    "promote",
    "question",
    "permission",
)
STEP_UP = {
    "admin_plan": True,
    "admin_critical": True,
    "delegate": True,
    "monitor_trust": True,
    "pr_approve": True,
    "promote": False,
    "question": False,
    "permission": False,
}
TTL_SECONDS = {
    "admin_plan": 72 * 3600,
    "admin_critical": 1800,
    "delegate": 72 * 3600,
    "promote": 72 * 3600,
    "question": 0,
}  # 0 or missing: does not expire
STATES = ("open", "approved", "rejected", "expired", "revoked")
TEXT_MAX = 4000
SIGNATURE_MAX = 16 * 1024
SYSTEM = "system"
TRUST = "monitor_trust"
# a monitor_trust decision's state -> its own journal event; applied and failed come from the monitor's
# report
TRUST_EVENTS = {"open": "proposed", "approved": "signed", "rejected": "rejected", "expired": "expired",
                "revoked": "revoked"}


def deployed_key(decision: Mapping) -> str:
    """The slot of ``revisions`` that binds the deployed tree a factory plan's component step is signed for."""
    return f"deployed:{decision['card']}"


def basis_key(decision: Mapping) -> str:
    """The slot of ``revisions`` that binds the decision: its card, or for a decision without one its own id."""
    return decision["card"] or f"decision:{decision['id']}"


def trust_event(decision: Mapping, step: str, **extra) -> tuple:
    """(kind, payload) of a monitor_trust decision's event: the P-n's timeline finds it by ``incident``."""
    p = decision["payload"]
    payload = {"decision": decision["id"], "kind": p.get("kind", ""), **extra}
    if p.get("incident"):
        payload.update(incident=p["incident"], id=f"P-{p['incident']}")
    for key in ("server", "cidr", "label"):
        if p.get(key):
            payload[key] = p[key]
    return f"{TRUST}.{step}", payload


class DecisionError(Exception):
    """Nothing changed; ``kind`` is the API error kind (refused, not_found, invalid, conflict)."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def canonical(value) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def payload_sha(payload: Mapping) -> str:
    return hashlib.sha256(canonical(payload)).hexdigest()


def _text(value, name: str, limit: int = TEXT_MAX) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise DecisionError(
            "invalid", f"{name} must be text of 1 to {limit} characters"
        )
    return value.strip()


class Decisions:
    def __init__(self, store):
        self.store = store
        self._conn = store._conn

    # --- reads ---------------------------------------------------------------------------------
    def _rows(self, where: str = "", args: tuple = ()) -> List[dict]:
        rows = self._conn.execute(
            "SELECT d.*, c.project AS project FROM decisions d LEFT JOIN cards c ON c.id = d.card "
            f"{where} ORDER BY d.created, d.rowid",
            args,
        ).fetchall()
        return [self._dict(r) for r in rows]

    @staticmethod
    def _dict(row) -> dict:
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        data["step_up"] = bool(data["step_up"])
        data["signature"] = json.loads(data["signature"]) if data["signature"] else None
        data["answer"] = json.loads(data["answer"]) if data["answer"] else None
        return data

    def get(self, decision_id) -> Optional[dict]:
        if not isinstance(decision_id, str):
            return None
        found = self._rows("WHERE d.id = ?", (decision_id,))
        return found[0] if found else None

    def list(self, *, state=None, card=None, projects=None) -> List[dict]:
        """Decisions by state and card; ``projects`` (a set, ``"*"`` for all) limits them to visible cards."""
        clauses, args = [], []
        if state is not None:
            clauses.append("d.state = ?")
            args.append(state)
        if card is not None:
            clauses.append("d.card = ?")
            args.append(card)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        found = self._rows(where, tuple(args))
        if projects is None or "*" in projects:
            return found
        return [d for d in found if d["project"] in projects]

    def asked_since(self, requested_by: str, since: str) -> int:
        """How many questions ``requested_by`` opened at or after ``since``, on any card and in any state now."""
        row = self._conn.execute(
            "SELECT COUNT(*) FROM decisions WHERE type='question' AND requested_by=? AND created>=?",
            (requested_by, since),
        ).fetchone()
        return int(row[0])

    # --- the challenge -------------------------------------------------------------------------
    @staticmethod
    def digest(
        decision: Mapping, *, user: str, expires: str, card_revision: int
    ) -> bytes:
        text = f"{decision['id']}|{decision['payload_sha']}|{card_revision}|{user}|{expires}"
        return hashlib.sha256(text.encode("utf-8")).digest()

    def digests(
        self, ids: Iterable[str], *, user: str, expires: str, revisions: Mapping
    ) -> List[str]:
        """The hex digest of each decision, in ``ids`` order — what a target recomputes from an envelope;
        ``challenge`` is built from them."""
        digests = []
        for decision_id in ids:
            decision = self.get(decision_id)
            if decision is None:
                raise DecisionError("not_found", f"no decision {decision_id}")
            key = basis_key(decision)
            if key not in revisions:
                raise DecisionError("invalid", f"no revision of {key} given")
            basis = revisions[key]
            tree = revisions.get(deployed_key(decision)) if decision["card"] else None
            if tree:  # a component step's tree is in the signed value, not only the server's check
                basis = f"{basis}|tree:{tree}"
            digests.append(
                self.digest(
                    decision,
                    user=user,
                    expires=expires,
                    card_revision=basis,
                ).hex()
            )
        if not digests:
            raise DecisionError("invalid", "no decisions")
        return digests

    @staticmethod
    def bound(digests: List[str]) -> bytes:
        """One decision: its digest; several: sha256 over their sorted hex digests (one signature)."""
        if len(digests) == 1:
            return bytes.fromhex(digests[0])
        return hashlib.sha256("".join(sorted(digests)).encode("ascii")).digest()

    def challenge(
        self, ids: Iterable[str], *, user: str, expires: str, revisions: Mapping
    ) -> bytes:
        return self.bound(
            self.digests(ids, user=user, expires=expires, revisions=revisions)
        )

    # --- writes --------------------------------------------------------------------------------
    def open(self, type, *, card, payload, requested_by, now, expires=None, thread=None) -> dict:
        """``thread`` — where a decision without a card lives; a card's decision is in its thread."""
        if type not in TYPES:
            raise DecisionError("invalid", f"unknown decision type {type!r}")
        if not isinstance(payload, Mapping):
            raise DecisionError("invalid", "payload must be an object")
        if not card and thread is None:
            raise DecisionError("invalid", "a decision without a card names its thread")
        payload = json.loads(canonical(payload))
        ttl = TTL_SECONDS.get(type, 0)
        if expires is None:
            expires = users.shift(now, seconds=ttl) if ttl else ""
        decision_id = secrets.token_hex(16)
        body = payload.get("text", "") if type == "question" else ""
        with self.store._tx():
            thread = self.store._thread_id(card) if card else thread
            seq = self.store._add_message(
                thread,
                requested_by,
                "decision",
                body,
                {
                    "decision": decision_id,
                    "type": type,
                    "state": "open",
                    "key": "sys.decision.opened",
                    "params": {"type": type},
                },
                "",
                via="decision",
                extra={"decision": decision_id},
            )
            self._conn.execute(
                "INSERT INTO decisions (id, type, card, thread, message, payload, payload_sha, requested_by, "
                "decide_permission, step_up, state, created, expires) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    type,
                    card,
                    thread,
                    seq,
                    canonical(payload).decode("utf-8"),
                    payload_sha(payload),
                    requested_by,
                    f"decide.{type}",
                    int(STEP_UP[type]),
                    "open",
                    now,
                    expires,
                ),
            )
            self.store._event(
                requested_by,
                "decision.opened",
                card,
                {"decision": decision_id, "type": type},
                thread,
            )
            if type == TRUST:
                kind, data = trust_event({"id": decision_id, "payload": payload}, TRUST_EVENTS["open"])
                self.store._event(requested_by, kind, card, data, thread)
        return self.get(decision_id)

    def record_rule(self, type, *, card, payload, requested_by, rule, grant_id, now, params) -> dict:
        """a decision a rule took instead of the owner, written as already approved — the card keeps the
        plan, who approved it and the owner's Revoke, but nothing is opened: no question, no push, no wake. One
        ``decision`` line in the thread (``sys.decision.rule_approved`` with ``params``) and one event,
        ``admin.plan.rule_approved``."""
        if type not in TYPES:
            raise DecisionError("invalid", f"unknown decision type {type!r}")
        payload = json.loads(canonical(payload))
        decision_id = secrets.token_hex(16)
        with self.store._tx():
            thread = self.store._thread_id(card)
            seq = self.store._add_message(
                thread,
                "system",
                "decision",
                "",
                {
                    "decision": decision_id,
                    "type": type,
                    "state": "approved",
                    "key": "sys.decision.rule_approved",
                    "params": dict(params),
                },
                "",
                via="decision",
                extra={"decision": decision_id},
            )
            self._conn.execute(
                "INSERT INTO decisions (id, type, card, thread, message, payload, payload_sha, requested_by, "
                "decide_permission, step_up, state, created, expires, decided_by, decided_at, grant_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    decision_id,
                    type,
                    card,
                    thread,
                    seq,
                    canonical(payload).decode("utf-8"),
                    payload_sha(payload),
                    requested_by,
                    f"decide.{type}",
                    int(STEP_UP[type]),
                    "approved",
                    now,
                    "",
                    rule,
                    now,
                    grant_id,
                ),
            )
            self.store._event(
                "system",
                "admin.plan.rule_approved",
                card,
                {"decision": decision_id, "type": type, "requested_by": requested_by, "rule": rule, **dict(params)},
                thread,
            )
        return self.get(decision_id)

    def carry(self, type, *, card, payload, requested_by, now, origin: Mapping, review: int) -> dict:
        """The owner's approval of ``origin`` carried to a new head (KD-9442): written approved, as the person who
        signed ``origin`` with the mode he chose, kd-review's ``review`` on the new head its grant. One ``decision``
        line in the thread (``sys.decision.carried``: from which head, after an update from which branch) and one
        ``decision.approved`` event naming ``carried_from`` and no mode — nobody is woken: the agent that published
        the head reads the carry in the publish's answer."""
        payload = json.loads(canonical(payload))
        source = origin["payload"]
        answer = {**origin["answer"], "review": review,
                  "carried_from": {"decision": origin["id"], "head": source["head"]}}
        decision_id = secrets.token_hex(16)
        params = {"from": source["head"][:7], "target": payload.get("target", ""), "mode": answer["mode"]}
        with self.store._tx():
            thread = self.store._thread_id(card)
            seq = self.store._add_message(
                thread, "system", "decision", "",
                {"decision": decision_id, "type": type, "state": "approved", "key": "sys.decision.carried",
                 "params": params},
                "", via="decision", extra={"decision": decision_id},
            )
            self._conn.execute(
                "INSERT INTO decisions (id, type, card, thread, message, payload, payload_sha, requested_by, "
                "decide_permission, step_up, state, created, expires, decided_by, decided_at, grant_id, answer) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (decision_id, type, card, thread, seq, canonical(payload).decode("utf-8"), payload_sha(payload),
                 requested_by, f"decide.{type}", int(STEP_UP[type]), "approved", now, "", origin["decided_by"], now,
                 str(review), canonical(answer).decode("utf-8")),
            )
            self.store._event(
                origin["decided_by"], "decision.approved", card,
                {"decision": decision_id, "type": type, "requested_by": requested_by, "pr": payload.get("number"),
                 "head": payload.get("head"), "carried_from": source["head"]},
                thread,
            )
        return self.get(decision_id)

    def _transition(
        self, decision_id, *, to, actor, now, text="", event=None, key=None, expected=None, event_extra=None,
        attachments=(), message_extra=None, **columns
    ) -> dict:
        """Move one decision and say so in its card's thread: a ``decision`` message whose payload carries the
        catalog key and parameters; the body is only the person's own text."""
        with self.store._tx():
            decision = self.get(decision_id)
            if decision is None:
                raise DecisionError("not_found", f"no decision {decision_id}")
            expected = expected or ("approved" if to == "revoked" else "open")
            if decision["state"] != expected:
                raise DecisionError("refused", f"the decision is {decision['state']}")
            if (
                to == "revoked" and expected == "approved"
            ):  # who approved stays; the event and the thread say who revoked
                columns["state"] = to
            elif to != "open":
                columns.update(state=to, decided_by=actor, decided_at=now)
            if columns:
                names = ", ".join(f"{k}=?" for k in columns)
                self._conn.execute(
                    f"UPDATE decisions SET {names} WHERE id=?",
                    (*columns.values(), decision_id),
                )
            payload = {
                "decision": decision_id,
                "type": decision["type"],
                "state": to,
                "key": key or f"sys.decision.{to}",
                "params": {"by": view.who(actor)},
            }
            if text:
                payload["text"] = text
            payload.update(message_extra or {})
            seq = self.store._add_message(
                decision["thread"],
                actor,
                "decision",
                text,
                payload,
                "",
                via="decision",
                extra={"decision": decision_id},
            )
            self.store._attach(decision["thread"], seq, attachments)
            self.store._event(
                actor,
                event or f"decision.{to}",
                decision["card"],
                {"decision": decision_id, "type": decision["type"], "requested_by": decision["requested_by"],
                 **(event_extra or {})},
                decision["thread"],
            )
            if decision["type"] == TRUST and event is None and to in TRUST_EVENTS:
                kind, data = trust_event(decision, TRUST_EVENTS[to])
                self.store._event(actor, kind, decision["card"], data, decision["thread"])
        return self.get(decision_id)

    def approve(
        self,
        decision_id,
        *,
        user,
        now,
        signature=None,
        credential=None,
        batch_id="",
        grant_id="",
        answer=None,
        event_extra=None,
    ) -> dict:
        """``answer`` — what the person chose with the approval (a PR's mode and the review); ``event_extra``
        joins the event, for the wake it makes. A monitor_trust decision now waits for the monitor."""
        signed = canonical(signature).decode("utf-8") if signature else ""
        if len(signed) > SIGNATURE_MAX:
            raise DecisionError("invalid", "the signature is too large")
        columns = {"answer": canonical(answer).decode("utf-8")} if answer is not None else {}
        found = self.get(decision_id)
        if found is not None and found["type"] == TRUST:
            columns["apply_state"] = "pending"
        return self._transition(
            decision_id,
            to="approved",
            actor=f"user:{user}",
            now=now,
            event_extra=event_extra,
            signature=signed,
            credential=credential,
            batch_id=batch_id,
            grant_id=grant_id,
            **columns,
        )

    def reject(self, decision_id, *, user, text, now, event_extra=None) -> dict:
        """``reply`` keeps the reason for the agent, as it keeps a question's answer."""
        text = _text(text, "text")
        return self._transition(
            decision_id,
            to="rejected",
            actor=f"user:{user}",
            now=now,
            text=text,
            event_extra=event_extra,
            reply=text,
        )

    def ask(self, decision_id, *, user, text, now) -> dict:
        text = _text(text, "text")
        return self._transition(
            decision_id,
            to="open",
            actor=f"user:{user}",
            now=now,
            text=text,
            event="decision.asked",
            key="sys.decision.asked",
            question=text,
        )

    def reply(self, decision_id, *, user, text, now, answer=None, attachments=(), client_key="") -> dict:
        """The answer to an agent's question: the text, and for a form the structure ``answer`` too,
        whose readable text ``text`` is. ``attachments`` are the answer's files: their rows join the
        same write, ``answer.files`` names them; with files the text may be empty. The same person sending the
        same answer again (a retry after a timeout: the same ``client_key``, or the same text and structure) gets
        the answered decision, nothing new; any other answer to it is refused."""
        from kd.cabinet import questions

        decision = self.get(decision_id)
        if decision is not None and decision["type"] != "question":
            raise DecisionError("refused", "only a question takes a reply")
        if (
            client_key
            and decision is not None
            and decision["state"] == "approved"
            and decision["decided_by"] == f"user:{user}"
            and self._client_of(decision) == client_key
        ):
            return decision
        limit = questions.READABLE_MAX if answer is not None else TEXT_MAX
        text = "" if attachments and isinstance(text, str) and not text.strip() else _text(text, "text", limit)
        columns = {"reply": text}
        if answer is not None:
            answer = json.loads(canonical(answer))
            columns["answer"] = canonical(answer).decode("utf-8")
        if (
            not attachments
            and decision is not None
            and decision["state"] == "approved"
            and decision["decided_by"] == f"user:{user}"
            and decision["reply"] == text
            and decision["answer"] == answer
        ):
            return decision
        return self._transition(
            decision_id,
            to="approved",
            actor=f"user:{user}",
            now=now,
            text=text,
            key="sys.decision.replied",
            attachments=attachments,
            # the answer's files: attached here, or a recording of the thread whose text this is
            message_extra={**({"files": answer["files"]} if answer and answer.get("files") else {}),
                           **({"client": client_key} if client_key else {})},
            **columns,
        )

    def _client_of(self, decision) -> str:
        """The page's send id the decision was answered with, from its thread message."""
        row = self._conn.execute(
            "SELECT json_extract(payload, '$.client') FROM messages WHERE thread=? AND kind='decision'"
            " AND json_extract(payload, '$.decision')=? AND json_extract(payload, '$.state')='approved'"
            " ORDER BY seq DESC LIMIT 1",
            (decision["thread"], decision["id"]),
        ).fetchone()
        return (row[0] or "") if row is not None else ""

    def revoke(self, decision_id, *, user, now) -> dict:
        return self._transition(
            decision_id, to="revoked", actor=f"user:{user}", now=now
        )

    def withdraw(self, decision_id, *, user, reply, now) -> dict:
        """Close an open decision the person made moot (its card moved out of a deleted project): revoked,
        ``reply`` tells the agent why."""
        return self._transition(
            decision_id,
            to="revoked",
            actor=f"user:{user}",
            now=now,
            expected="open",
            key="sys.decision.withdrawn",
            reply=reply,
        )

    def close_promotes(self, card, *, status, actor, now, fields=()) -> List[dict]:
        """Close the card's open BOSS promotes after any edit of it: its approval binds the card's revision, so it
        can never be given. Out of Inbox by any road — a start from the card, a status a person set, an
        agent's or root's edit — nothing is left to promote; in Inbox (``fields`` changed) BOSS proposes it
        again. Revoked by whoever changed the card; ``reply`` tells BOSS why."""
        who = view.who(actor)
        if status != "Inbox":
            key, reply = "sys.decision.withdrawn", f"the card left Inbox: {status} by {who}"
        else:
            changed = ", ".join(sorted({re.sub(r"_(add|remove)$", "", f) for f in fields}))
            key, reply = "sys.decision.superseded", f"the card changed after the proposal ({changed}) by {who}: propose it again"
        return [
            self._transition(d["id"], to="revoked", actor=actor, now=now, expected="open", key=key, reply=reply)
            for d in self.list(state="open", card=card)
            if d["type"] == "promote"
        ]

    def asked_open(self, decision) -> bool:
        """A person's question on the decision that the agent who asked for it has not answered in the
        thread yet: no text of that agent after the newest question."""
        if not decision.get("question"):
            return False
        asked = self._conn.execute(
            "SELECT MAX(seq) FROM messages WHERE thread=? AND kind='decision' AND json_extract(payload, '$.decision')=?"
            " AND json_extract(payload, '$.key')='sys.decision.asked'",
            (decision["thread"], decision["id"]),
        ).fetchone()[0]
        if asked is None:
            return False
        answered = self._conn.execute(
            "SELECT 1 FROM messages WHERE thread=? AND seq>? AND author=? AND kind='text' LIMIT 1",
            (decision["thread"], asked, decision["requested_by"]),
        ).fetchone()
        return answered is None

    def close_with_card(self, card, *, status, actor, now) -> List[dict]:
        """Close every open decision of a card that reached Done or Cancelled: a question nobody will
        answer, a plan or proposal nobody will approve — withdrawn by whoever closed the card, in its write."""
        return [
            self._transition(
                d["id"],
                to="revoked",
                actor=actor,
                now=now,
                expected="open",
                key="sys.decision.card_closed",
                event_extra={"status": status},
                message_extra={"params": {"by": view.who(actor), "status": status}},
                reply=f"the card is closed: {status} by {view.who(actor)}",
            )
            for d in self.list(state="open", card=card)
        ]

    def sweep_closed(self, now) -> int:
        """Open decisions on cards already Done or Cancelled (closed before the change, or past the hook): withdrawn
        by the system; how many."""
        rows = self._conn.execute(
            "SELECT d.card, c.status FROM decisions d JOIN cards c ON c.id = d.card"
            " WHERE d.state='open' AND c.status IN ('Done', 'Cancelled') GROUP BY d.card, c.status ORDER BY d.created"
        ).fetchall()
        return sum(len(self.close_with_card(card, status=status, actor=SYSTEM, now=now)) for card, status in rows)

    def expire_one(self, decision_id, *, now) -> dict:
        """Close one open decision as expired (a newer plan of the same card supersedes it)."""
        return self._transition(decision_id, to="expired", actor=SYSTEM, now=now)

    def expire(self, now) -> List[dict]:
        """Close every open decision whose term has passed; the closed ones."""
        due = [
            r[0]
            for r in self._conn.execute(
                "SELECT id FROM decisions WHERE state='open' AND expires != '' AND expires <= ? ORDER BY created",
                (now,),
            ).fetchall()
        ]
        return [self._transition(d, to="expired", actor=SYSTEM, now=now) for d in due]

    def totp_failure(self, decision_id) -> int:
        """One more failed TOTP attempt on the decision; the new count."""
        with self.store._tx():
            self._conn.execute(
                "UPDATE decisions SET totp_failures = totp_failures + 1 WHERE id=?",
                (decision_id,),
            )
        row = self._conn.execute(
            "SELECT totp_failures FROM decisions WHERE id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise DecisionError("not_found", f"no decision {decision_id}")
        return row[0]
