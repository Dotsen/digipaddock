"""Who is told what and when (owner cabinet spec section 7.2): the notification queue behind Web Push.

Events of the journal become rows of ``notifications``, one per person, in that person's language:
a decision waiting for those who may take it, an agent's question, a card in Review, a card that
waits for a person's own card; ``Done`` and other routine events wait for the next half hour and go
as one batch per person; progress and tool calls stay in the feed. Each person's preferences
(``notification_prefs``) override the defaults; quiet hours hold everything but a critical command
until they end. ``tick`` adds reminders of open decisions (4 h and 24 h), the 09:00 digest in each
person's time zone, and drops rows older than 30 days. The push worker (``push_worker``) sends the
rows through ``push.take`` / ``push.sent``.

The queue is not board state: its rows carry no event of their own. Progress is kept in ``state``:
``notify-cursor`` (the last journal event handled), ``notify-reminders`` and ``notify-digests``.
"""

import json
import re
import sqlite3
import sys
import traceback
from datetime import datetime, time, timedelta, timezone
from typing import Dict, List, Mapping, Optional, Tuple
from zoneinfo import ZoneInfo

from kd.cabinet import config, i18n, perms, schema, store, users, view

KINDS = (
    "decision.waiting",
    "question",
    "incident.critical",
    "session.failed",
    "agent.message",
    "card.review",
    "card.blocked_on_me",
    "card.done",
    "monitor.warning",
    "security.routine",
    "progress",
    "tool",
    "status.agent",
    "login.known",
    "notice.critical",
    "notice.warn",
    "notice.info",
    "voice.failed",
)
MODES = ("now", "batch", "feed")
DEFAULT = {
    "now": {
        "decision.waiting",
        "question",
        "incident.critical",
        "session.failed",
        "agent.message",
        "card.review",
        "card.blocked_on_me",
        "notice.critical",
        "notice.warn",
        "voice.failed",
    },
    "batch": {"card.done", "monitor.warning", "security.routine"},
    "feed": {"progress", "tool", "status.agent", "login.known", "notice.info"},
}
QUIET = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d-(?:[01]\d|2[0-3]):[0-5]\d")
LOUD = {"incident.critical", "notice.critical"}  # and a waiting critical command: they ignore quiet hours
BATCH_MINUTES = 30
REMIND_HOURS = (4, 24)
DIGEST_HOUR = 9
KEEP_DAYS = 30
USAGE = "/var/lib/digipaddock/usage.jsonl"
HINDSIGHT = "hindsight"  # kd-usage's category of the Hindsight server's own calls, not an agent
CURSOR, REMINDERS, DIGESTS = "notify-cursor", "notify-reminders", "notify-digests"
BODY_MAX = 200
CHAT_DELAY = 2  # seconds a chat reply waits for the person's open chat to read it before it is pushed
# where a push leads when its event has no page of its own — never the feed, which is the journal: the platform's
# own alerts are listed on the incidents page, a batch of several cards is the board, the digest the dashboard
ALERTS_URL = "/incidents"
BOARD_URL = "/board"
DASHBOARD_URL = "/"
SKIP_LIMIT = 3  # events in a row the notifier may skip on its own failure before it stops


DIGEST_CRIT = 5  # open crit incidents the digest names
INCIDENTS_KEY = "push.digest.body.incidents"


def incident_url(pid: str) -> str:
    """Where an incident's push leads: its page in the cabinet."""
    return f"/incidents/{pid}"


def default_mode(kind: str) -> str:
    return next((mode for mode in MODES if kind in DEFAULT[mode]), "feed")


def check_prefs(prefs) -> Dict[str, str]:
    """The preferences a person sent, or ValueError: ``kind -> now|batch|feed`` and ``quiet`` (``HH:MM-HH:MM``
    in the person's time zone, empty to switch off)."""
    if not isinstance(prefs, Mapping) or not prefs:
        raise ValueError("prefs must be a non-empty object")
    out = {}
    for key, value in prefs.items():
        if key == "quiet":
            if not isinstance(value, str) or (value and not QUIET.fullmatch(value)):
                raise ValueError("quiet is HH:MM-HH:MM or empty")
        elif key not in KINDS:
            raise ValueError(f"unknown notification kind {key!r}")
        elif value not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        out[key] = value
    return out


def full_prefs(stored: Mapping) -> Dict[str, str]:
    """Every kind with its mode (the person's or the default) and ``quiet``."""
    return {
        **{kind: stored.get(kind, default_mode(kind)) for kind in KINDS},
        "quiet": stored.get("quiet", ""),
    }


def _parse(text: str) -> datetime:
    return users.parse_time(text)


def _fmt(moment: datetime) -> str:
    return users.format_time(moment)


def _clock(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def quiet_until(now: datetime, quiet: str, tz) -> Optional[datetime]:
    """When the quiet hours around ``now`` end (UTC), or None when ``now`` is outside them."""
    if not quiet:
        return None
    start, end = (_clock(part) for part in quiet.split("-"))
    if start == end:
        return None
    local = now.astimezone(tz)
    moment = local.time().replace(second=0, microsecond=0, tzinfo=None)
    inside = start <= moment < end if start < end else (moment >= start or moment < end)
    if not inside:
        return None
    day = local.date() if moment < end else local.date() + timedelta(days=1)
    return datetime.combine(day, end, tzinfo=tz).astimezone(timezone.utc)


def next_batch(now: datetime) -> datetime:
    """The next half-hour boundary strictly after ``now``."""
    floor = now.replace(
        minute=now.minute - now.minute % BATCH_MINUTES, second=0, microsecond=0
    )
    return floor + timedelta(minutes=BATCH_MINUTES)


class Notifier:
    def __init__(self, store_, people, cfg, *, usage_path: str = USAGE):
        self.store, self.users, self.cfg, self.usage_path = (
            store_,
            people,
            cfg,
            usage_path,
        )

    @property
    def _conn(self):
        return self.store._conn

    # --- people ---------------------------------------------------------------------------------
    def _person(self, user_id) -> dict:
        person = self.users.get(user_id) or {}
        lang = person.get("language") or (self.cfg.language if self.cfg else config.owner_language())
        zone = person.get("timezone") or (self.cfg.timezone if self.cfg else "UTC")
        return {
            "id": user_id,
            "lang": lang,
            "tz": ZoneInfo(zone),
            "prefs": self.users.prefs(user_id),
        }

    def _active(self) -> List[str]:
        return [p["id"] for p in self.users.list() if p["status"] == "active"]

    def _who_can(self, perm: str, project: str, actor: str = "") -> List[dict]:
        return [
            self._person(u)
            for u in self._active()
            if self.users.can(u, perm, project) and actor != f"user:{u}"
        ]

    def _who_reads(self, space: str, actor: str = "") -> List[dict]:
        """Who may read an agent's own thread in ``space`` (its chat; «Monitor» needs the servers too)."""
        need = perms.AGENT_THREAD_PERMS[space]
        return [p for p in self._who_can(need[0], None, actor) if all(self.users.can(p["id"], n) for n in need[1:])]

    # --- the queue ------------------------------------------------------------------------------
    def _due(self, person: dict, mode: str, now: datetime, loud: bool) -> datetime:
        due = next_batch(now) if mode == "batch" else now
        if not loud:
            held = quiet_until(due, person["prefs"].get("quiet", ""), person["tz"])
            if held is not None:
                due = held
        return due

    def _queue(
        self, person, kind, *, event, tag, title, body, url, now, loud=False, mode=None
    ) -> Optional[int]:
        """One row for ``person`` (inside the caller's transaction); a batch kind joins the person's open batch."""
        mode = mode or person["prefs"].get(kind, default_mode(kind))
        if mode == "feed":
            return None
        due = _fmt(self._due(person, mode, now, loud))
        if mode == "batch":
            # an incident's page or their queue is a line of the monitor's batch; the platform's own alert,
            # which the incidents page lists too, is a line of the plain one
            monitoring = url.startswith("/incidents") and not kind.startswith("notice.")
            bucket = "monitor:batch" if monitoring else "batch"
            row = self._conn.execute(
                "SELECT id, body, url FROM notifications WHERE user=? AND kind='batch' AND tag=? AND due=? AND sent='' "
                "ORDER BY id LIMIT 1",
                (person["id"], bucket, due),
            ).fetchone()
            line = title + (" · " + body.replace("\n", " ") if monitoring and body else "")
            if row is not None:
                lines = row["body"].split("\n")
                pid = re.match(r"(P-[0-9]+) ·", line) if monitoring else None
                if pid:
                    lines = [old for old in lines if not old.startswith(pid[0])]
                lines.append(line)
                self._conn.execute(
                    "UPDATE notifications SET title=?, body=?,url=? WHERE id=?",
                    (
                        i18n.t(person["lang"], "push.monitor.batch" if monitoring else "push.batch.title", count=len(lines)),
                        "\n".join(lines),
                        row["url"] if row["url"] == url else "/incidents" if monitoring else BOARD_URL,
                        row["id"],
                    ),
                )
                return row["id"]
            kind, tag, title, body, url = (
                "batch",
                bucket,
                i18n.t(person["lang"], "push.monitor.batch" if monitoring else "push.batch.title", count=1),
                line,
                url,  # one line leads where it would alone; lines of several places — see the join above
            )
        cursor = self._conn.execute(
            "INSERT INTO notifications (user, event, kind, tag, title, body, url, due) VALUES (?,?,?,?,?,?,?,?)",
            (person["id"], event, kind, tag, title, body[: BODY_MAX * 5], url, due),
        )
        return cursor.lastrowid

    def test(self, user_id: str, now: str, *, incident=None) -> int:
        """A push to the person's own devices right away (the settings page's check), whatever the prefs."""
        person = self._person(user_id)
        with self.store._tx():
            cursor = self._conn.execute(
                "INSERT INTO notifications (user, event, kind, tag, title, body, url, due) VALUES (?,?,?,?,?,?,?,?)",
                (
                    user_id,
                    0,
                    "test",
                    f"test:{incident['id']}" if incident else "test",
                    i18n.t(person["lang"], "push.test.incident", id=incident["id"], server=incident["server"])
                    if incident else i18n.t(person["lang"], "push.test.title"),
                    i18n.t(person["lang"], "push.test.incident_body") if incident else i18n.t(person["lang"], "push.test.body"),
                    incident_url(incident["id"]) if incident else "/settings",
                    now,
                ),
            )
        return cursor.lastrowid

    # --- events ---------------------------------------------------------------------------------
    def on_event(self, event: Mapping, decisions=None) -> List[int]:
        """The rows one journal event adds (or joins); an event at or before the cursor was handled already."""
        cursor = self.store.get_state(CURSOR)
        if cursor is not None and event["seq"] <= cursor:
            return []
        with self.store._tx():
            ids = self._handle(event, decisions)
            self.store._put_state(CURSOR, event["seq"])
        return ids

    def catch_up(self, decisions=None) -> int:
        """Handle every event after the cursor; the first call starts at the end of the journal (no history)."""
        cursor = self.store.get_state(CURSOR)
        if cursor is None:
            self.store.set_state(CURSOR, self.store.last_seq())
            return 0
        handled = skipped = 0
        while True:
            events = self.store.events_since(cursor, 500)
            if not events:
                return handled
            for event in events:
                try:
                    with self.store._tx():
                        self._handle(event, decisions)
                        self.store._put_state(CURSOR, event["seq"])
                except sqlite3.OperationalError:
                    raise  # the database, not the event (locked, disk, a missing table): the next call tries again
                except Exception:  # noqa: BLE001 — one event's failure must not stop every later push
                    skipped += 1
                    if skipped > SKIP_LIMIT:
                        raise  # failing on event after event is the notifier's fault: stop where it is, loudly
                    sys.stderr.write(
                        f"kd-cabinet notify: event seq {event['seq']} ({event['kind']}"
                        f"{', ' + event['card'] if event.get('card') else ''}) skipped\n")
                    traceback.print_exc(file=sys.stderr)
                    self.store.set_state(CURSOR, event["seq"])
                else:
                    skipped, handled = 0, handled + 1
                cursor = event["seq"]

    def _handle(self, event: Mapping, decisions) -> List[int]:
        now = _parse(event["ts"])
        payload = event.get("payload") or {}
        if event["kind"] == "decision.opened":
            return self._decision(event, payload, decisions, now)
        if event["kind"] == "card.changed":
            return self._card(event, payload, now)
        if event["kind"] == "session.changed":
            return self._session(event, payload, now)
        if event["kind"] == "message.added":
            return self._chat(event, payload, now)
        if event["kind"] == "run.finished" and payload.get("agent") == "sysadmin":
            return self._monitor_finished(event, payload, now)
        if event["kind"] == "notice":
            return self._notice(event, payload, now)
        if event["kind"].startswith("incident."):
            return self._incident(event, payload, now)
        if event["kind"] == "voice.failed":
            return self._voice(event, payload, now)
        return []

    def _card_of(self, card_id):
        try:
            return self.store.card(card_id)
        except store.NotFound:
            return None

    def _decision(self, event, payload, decisions, now) -> List[int]:
        from kd.cabinet import decisions as ledger

        decision = (decisions or ledger.Decisions(self.store)).get(
            payload.get("decision", "")
        )
        if decision is not None and decision["type"] == ledger.TRUST and not event["card"]:
            return self._trust(event, decision, now)
        card = self._card_of(event["card"])
        if decision is None or card is None or decision["state"] != "open":
            return []
        ids = []
        for person in self._who_can(f"decide.{decision['type']}", card.project):
            kind, title, body = self._decision_text(person["lang"], decision, card)
            ids.append(
                self._queue(
                    person,
                    kind,
                    event=event["seq"],
                    tag=card.id,
                    title=title,
                    body=body,
                    url=config.decision_url("", decision["id"]),
                    now=now,
                    loud=decision["type"] == "admin_critical",
                )
            )
        return [i for i in ids if i is not None]

    def _trust(self, event, decision, now) -> List[int]:
        """The sysadmin proposes the monitor's trust: who decides it hears so; the owner's own proposal from
        a page is signed there at once — no push."""
        if decision["state"] != "open" or not decision["requested_by"].startswith("agent:"):
            return []
        p = decision["payload"]
        kind = p.get("kind", "")
        tag = f"P-{p['incident']}" if p.get("incident") else (p.get("server") or p.get("cidr") or "")
        ids = []
        for person in self._who_can(f"decide.{decision['type']}", None):
            lang = person["lang"]
            server = i18n.t(lang, "trust.all") if p.get("server") == "all" else p.get("server", "")
            what = i18n.t(lang, f"trust.what.{kind}", id=tag, server=server, cidr=p.get("cidr", ""),
                          label=p.get("label", ""))
            title = i18n.t(lang, "push.decision.trust", what=what)
            ids.append(self._queue(person, "decision.waiting", event=event["seq"], tag=f"monitor_trust:{tag}",
                                   title=title, body=str(p.get("text", ""))[:BODY_MAX],
                                   url=config.decision_url("", decision["id"]), now=now))
        return [i for i in ids if i is not None]

    @staticmethod
    def _decision_text(lang, decision, card):
        if decision["type"] == "question":
            agent = view.who(decision["requested_by"])
            text = str(decision["payload"].get("text", ""))[:BODY_MAX]
            return (
                "question",
                i18n.t(lang, "push.question", agent=agent, card=card.id),
                text,
            )
        title = i18n.t(lang, "push.decision.waiting", card=card.id, title=card.title)
        return (
            "decision.waiting",
            title,
            i18n.t(lang, f"decision.type.{decision['type']}"),
        )

    def _card(self, event, payload, now) -> List[int]:
        before, after = payload.get("before") or {}, payload.get("after") or {}
        status = after.get("status")
        if status == before.get("status"):
            return []
        card = self._card_of(event["card"])
        if card is None:
            return []
        ids = []
        from kd.cabinet import stage_gate  # at call time: it loads the API

        # a chain plan's stage in Review is its coordinator's to decide on; held, its notice tells the owner.
        # A stage the coordinator passed is Done by the gate, not work the owner accepted: no «done» either
        staged = stage_gate.is_stage(self.store, card.id) and (
            status == "Review" or (status == "Done" and stage_gate.passed(self.store, card.id))
        )
        if status in ("Review", "Done") and not staged:
            kind = "card.review" if status == "Review" else "card.done"
            for person in self._who_can("cards.read", card.project, event["actor"]):
                title = i18n.t(
                    person["lang"], f"push.{kind}", card=card.id, title=card.title
                )
                ids.append(
                    self._queue(
                        person,
                        kind,
                        event=event["seq"],
                        tag=card.id,
                        title=title,
                        body="",
                        url=config.card_url("", card.id),
                        now=now,
                    )
                )
        if status == "Blocked":
            for person in self._waiting_on(card, event["actor"]):
                title = i18n.t(
                    person["lang"],
                    "push.card.blocked_on_me",
                    card=card.id,
                    title=card.title,
                )
                ids.append(
                    self._queue(
                        person,
                        "card.blocked_on_me",
                        event=event["seq"],
                        tag=card.id,
                        title=title,
                        body="",
                        url=config.card_url("", card.id),
                        now=now,
                    )
                )
        return [i for i in ids if i is not None]

    def _session(self, event, payload, now) -> List[int]:
        """An agent's session that has just failed: who may control it — on a card, ``agents.control``
        on its project; in the agent's chat or «Monitor», everyone who may read that thread."""
        if payload.get("state") != "failed" or payload.get("was") == "failed":
            return []
        agent = payload.get("agent", "")
        card = self._card_of(event["card"]) if event["card"] else None
        if event["card"] and card is None:
            return []
        if card is not None:
            people = self._who_can("agents.control", card.project, event["actor"])
        else:
            thread = event.get("thread")
            space = (self.store.agent_space(thread) if thread is not None else "") or "chat"
            people = self._who_reads(space, event["actor"])
        url = config.card_url("", card.id) if card is not None else f"/agents/{agent}"
        ids = [
            self._queue(
                person,
                "session.failed",
                event=event["seq"],
                tag=f"session:{agent}",
                title=(
                    i18n.t(person["lang"], "push.session.failed", agent=f"@{agent}", card=card.id)
                    if card is not None
                    else i18n.t(person["lang"], "push.session.failed.chat", agent=f"@{agent}")
                ),
                body=i18n.reason(person["lang"], payload.get("error", ""))[:BODY_MAX],
                url=url,
                now=now,
            )
            for person in people
        ]
        return [i for i in ids if i is not None]

    def _chat(self, event, payload, now) -> List[int]:
        """An agent's reply in its chat with people; a card thread's texts stay in the feed.

        One row per person and reply (the turn's texts are one reply, ``Store.REPLY_KEY``): a later text of the same
        turn only refreshes the row's body while it is unsent. It is due ``CHAT_DELAY`` later and checked right
        before it goes (``Store.take_notifications``): read by then, or the chat open in front of the person — no
        push. Its tag is the reply's own, so two replies are two notifications on a device."""
        agent = payload.get("agent")
        if not agent or event["actor"] != f"agent:{agent}" or payload.get("kind") != "text":
            return []
        space = payload.get("space", "chat")  # "Monitor" is the agent's second thread, as its chat
        kind = schema.AGENT_SPACES.get(space)
        if kind is None or (agent, space) in store.WORK_LOGS:  # a work log pushes nothing
            return []
        row = self._conn.execute(
            "SELECT m.id,m.thread, m.seq, m.body, json_extract(m.payload, '$.command') AS command FROM messages m"
            " JOIN threads t ON t.id = m.thread WHERE t.kind=? AND t.agent=? AND m.seq=?",
            (kind, agent, payload.get("seq")),
        ).fetchone()
        if row is None:
            return []
        key = row["command"] or f"seq:{row['seq']}"
        reply = f"chat:{row['thread']}:{key}"
        monitor = space == "monitor"
        tag = f"monitor:{agent}" if monitor else f"chat:{agent}"
        url = f"/agents/{agent}?tab=monitor" if monitor else f"/agents/{agent}"
        references = self._conn.execute(
            "SELECT i.n,i.server FROM incident_reports r JOIN incidents i ON i.n=r.incident"
            " WHERE r.message=? ORDER BY i.n", (row["id"],)
        ).fetchall() if monitor and agent == "sysadmin" else []
        if references and row["command"]:
            command = self._conn.execute("SELECT state FROM commands WHERE id=?", (row["command"],)).fetchone()
            if command and command[0] != "done":
                return []  # a streamed intermediate text is not the finished investigation
            references = self._conn.execute(
                "SELECT DISTINCT i.n,i.server FROM incident_reports r JOIN incidents i ON i.n=r.incident"
                " JOIN messages m ON m.id=r.message WHERE m.thread=? AND json_extract(m.payload,'$.command')=?"
                " ORDER BY i.n", (row["thread"], row["command"])
            ).fetchall()
        if references:
            url = incident_url(f"P-{references[0]['n']}") if len(references) == 1 else "/incidents"
            tag = f"incident:P-{references[0]['n']}" if len(references) == 1 else "incidents:reports"
        ids = []
        # an incident's report goes to who reads the servers (its page is theirs); a reply to who may read the thread
        if references:
            people = self._who_can("servers.read", None, event["actor"])
        else:
            people = self._who_reads(space, event["actor"])
        for person in people:
            mode = person["prefs"].get("agent.message", default_mode("agent.message"))
            unread = self.store.chat_state(person["id"], row["thread"])["unread"]
            title = i18n.t(person["lang"], "push.agent.monitor" if monitor else "push.agent.reply", agent=agent.upper(),
                           count=unread)
            body = row["body"][:BODY_MAX]
            if references:
                from kd.cabinet import incident_reports

                body = incident_reports.summary(row["body"])
                title = i18n.t(person["lang"], "push.incident.report", id=f"P-{references[0]['n']}",
                               server=references[0]["server"]) if len(references) == 1 else i18n.t(
                                   person["lang"], "push.monitor.batch", count=len(references))
                if mode != "feed":
                    self._clear_monitor_fallback(person, references, now)
            if mode != "now":
                found = self._queue(person, "agent.message", event=event["seq"], tag=tag, title=title,
                                    body=body, url=url, now=now, mode=mode)
                ids.append(found)
                continue
            due = _fmt(self._due(person, "now", now + timedelta(seconds=CHAT_DELAY), False))
            self._conn.execute(
                "INSERT INTO notifications (user, event, kind, tag, title, body, url, due, reply, thread, seq)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(user, reply) WHERE reply != '' DO UPDATE SET"
                " title=excluded.title, body=excluded.body, seq=excluded.seq,url=excluded.url,tag=excluded.tag WHERE sent=''",
                (person["id"], event["seq"], "agent.message", f"{tag}:{key}", title, body,
                 url, due, reply, row["thread"], row["seq"]),
            )
            # the row of this reply (new or refreshed); one already sent is not this event's
            found = self._conn.execute(
                "SELECT id FROM notifications WHERE user=? AND reply=? AND sent=''", (person["id"], reply)
            ).fetchone()
            ids.append(found["id"] if found else None)
        return [i for i in ids if i]

    def _monitor_finished(self, event, payload, now):
        row = self._conn.execute(
            "SELECT m.seq FROM messages m JOIN threads t ON t.id=m.thread JOIN commands c ON c.thread=m.thread"
            " WHERE c.id=? AND c.state='done' AND t.kind='agent_monitor' AND t.agent='sysadmin'"
            " AND m.author='agent:sysadmin' AND m.kind='text' AND json_extract(m.payload,'$.command')=c.id"
            " AND EXISTS(SELECT 1 FROM incident_reports r WHERE r.message=m.id) ORDER BY m.seq DESC LIMIT 1",
            (payload.get("command"),)
        ).fetchone()
        if not row:
            return []
        return self._chat({**event, "actor":"agent:sysadmin"},
                          {"agent":"sysadmin", "space":"monitor", "kind":"text", "seq":row[0]}, now)

    def _clear_monitor_fallback(self, person, references, now):
        """The ready investigation replaces its still-unsent raw warning, without affecting other incidents."""
        prefixes = tuple(f"P-{r['n']} ·" for r in references)
        for row in self._conn.execute(
            "SELECT id,body FROM notifications WHERE user=? AND tag='monitor:batch' AND sent=''", (person["id"],)
        ).fetchall():
            lines = [line for line in row["body"].split("\n") if not line.startswith(prefixes)]
            if not lines:
                self._conn.execute("UPDATE notifications SET sent=?,result='superseded by investigation' WHERE id=?",
                                   (_fmt(now), row["id"]))
            elif "\n".join(lines) != row["body"]:
                single = re.match(r"(P-[0-9]+) ·", lines[0]) if len(lines) == 1 else None
                self._conn.execute("UPDATE notifications SET body=?,title=?,url=? WHERE id=?",
                                   ("\n".join(lines), i18n.t(person["lang"], "push.monitor.batch", count=len(lines)),
                                    incident_url(single[1]) if single else "/incidents", row["id"]))

    def _notice(self, event, payload, now) -> List[int]:
        """A root service's or an executor's notice: on a card, who may control its project's agents; the
        platform's own, who may see the settings. ``critical`` ignores quiet hours."""
        level = payload.get("level")
        if level not in ("info", "warn", "critical"):
            return []
        card = self._card_of(event["card"]) if event["card"] else None
        if event["card"] and card is None:
            return []
        people = (
            self._who_can("agents.control", card.project) if card is not None else self._who_can("settings", None)
        )
        kind = f"notice.{level}"
        ids = [
            self._queue(
                person,
                kind,
                event=event["seq"],
                tag=f"notice:{payload.get('source', '')}",
                title=i18n.t(person["lang"], f"push.{kind}", card=f" · {card.id}" if card is not None else ""),
                body=(i18n.render(person["lang"], payload) or "")[:BODY_MAX],
                url=config.card_url("", card.id) if card is not None else ALERTS_URL,
                now=now,
                loud=kind in LOUD,
            )
            for person in people
        ]
        return [i for i in ids if i is not None]

    def _voice(self, event, payload, now) -> List[int]:
        """An audio of a person's message not transcribed: its author alone is told, right away — "the audio in
        KD-n / the chat of @agent is not transcribed"."""
        author = str(payload.get("author", ""))
        if not author.startswith("user:") or self.users.get(author[len("user:"):]) is None:
            return []
        person = self._person(author[len("user:"):])
        agent = payload.get("agent") or ""
        where = event["card"] or f"@{agent}"
        key = "push.voice.failed.card" if event["card"] else "push.voice.failed.chat"
        found = self._queue(
            person,
            "voice.failed",
            event=event["seq"],
            tag=f"voice:{payload.get('file', '')}",
            title=i18n.t(person["lang"], key, where=where),
            body=i18n.t(person["lang"], f"voice.status.{payload.get('status', 'error')}"),
            url=(config.card_url("", event["card"]) if event["card"]
                 else f"/agents/{agent}?tab=monitor" if payload.get("space") == "monitor" else f"/agents/{agent}"),
            now=now,
        )
        return [found] if found is not None else []

    def _incident(self, event, payload, now) -> List[int]:
        """The monitor's incident P-n: a new crit, warn -> crit and a failed fix push ``incident.critical``
        right away, quiet hours or not; a new warn is a line of the half-hour batch (``monitor.warning``). The rest
        stays in the feed: better, fixed, a rise within warn, the closing, an incident the monitor's import brought.
        Acknowledged or muted is silent but for crit and a failed fix; resolved by now (the monitor's queue sent late)
        is silent. Told: active people with ``servers.read``."""
        if payload.get("imported"):
            return []
        row = self._conn.execute(
            "SELECT * FROM incidents WHERE n=?", (payload.get("incident"),)
        ).fetchone()
        if row is None or row["status"] == "resolved":
            return []
        change, level = event["kind"][len("incident."):], payload.get("level")
        silent = row["status"] == "acknowledged" or row["muted_until"] > event["ts"]
        if change == "opened":
            if silent or level not in ("warn", "crit"):
                return []
            kind = "incident.critical" if level == "crit" else "monitor.warning"
        elif change == "fix_failed" or (
            change == "worse" and level == "crit" and self._level_before(row["n"], event["seq"]) != "crit"
        ):
            kind = "incident.critical"
        else:
            return []
        pid = f"P-{row['n']}"
        check = row["check"] + (f" {row['subject']}" if row["subject"] not in ("", "-") else "")
        facts = {"id": pid, "server": row["server"], "check": check, "value": payload.get("value", row["value"])}
        ids = []
        for person in self._who_can("servers.read", None, event["actor"]):
            line = i18n.t(person["lang"], "push.incident.line", **facts)
            if kind == "incident.critical":
                title = i18n.t(person["lang"], "push.incident.critical", **facts)
                body = i18n.t(person["lang"], f"push.incident.{change}")
            else:
                title, body = line, ""
            ids.append(
                self._queue(person, kind, event=event["seq"], tag=pid, title=title, body=body,
                            url=incident_url(pid), now=now, loud=kind in LOUD)
            )
        return [i for i in ids if i is not None]

    def _level_before(self, n: int, seq: int) -> str:
        """The incident's level as its timeline stood before event ``seq``."""
        row = self._conn.execute(
            "SELECT json_extract(payload, '$.level') FROM events WHERE kind IN ('incident.opened', 'incident.worse',"
            " 'incident.better', 'incident.fixed', 'incident.fix_failed') AND json_extract(payload, '$.incident')=?"
            " AND seq<? ORDER BY seq DESC LIMIT 1",
            (n, seq),
        ).fetchone()
        return row[0] if row is not None else ""

    def _waiting_on(self, card, actor) -> List[dict]:
        """People whose own cards the blocked card depends on (and who may read it)."""
        known, out = set(self._active()), {}
        for dep_id in card.dependencies:
            dep = self._card_of(dep_id)
            handle = (
                dep.assignee[1:]
                if dep is not None and dep.assignee.startswith("@")
                else ""
            )
            if (
                handle in known
                and actor != f"user:{handle}"
                and self.users.can(handle, "cards.read", card.project)
            ):
                out[handle] = self._person(handle)
        return list(out.values())

    # --- the scheduler --------------------------------------------------------------------------
    def tick(self, now: str) -> dict:
        moment = _parse(now)
        return {
            "events": self.catch_up(),
            "reminders": self._remind(moment),
            "digests": self._digest(moment),
            "purged": self._purge(moment),
        }

    def _remind(self, now: datetime) -> int:
        from kd.cabinet import decisions as ledger

        done = self.store.get_state(REMINDERS) or {}
        open_ = ledger.Decisions(self.store).list(state="open")
        count, kept = 0, {}
        with self.store._tx():
            for decision in open_:
                marks = list(done.get(decision["id"], []))
                age = now - _parse(decision["created"])
                due = [
                    h
                    for h in REMIND_HOURS
                    if age >= timedelta(hours=h) and h not in marks
                ]
                if due:
                    count += self._remind_one(decision, max(due), now)
                    marks = sorted(set(marks) | set(due))
                kept[decision["id"]] = marks
            self.store._put_state(REMINDERS, kept)
        return count

    def _remind_one(self, decision, hours, now) -> int:
        card = self._card_of(decision["card"])
        if card is None:
            return 0
        count = 0
        kind = "question" if decision["type"] == "question" else "decision.waiting"
        for person in self._who_can(f"decide.{decision['type']}", card.project):
            title = i18n.t(person["lang"], "push.reminder", hours=hours)
            _, body, _ = self._decision_text(person["lang"], decision, card)
            mode = person["prefs"].get(kind, default_mode(kind))
            row = self._queue(
                person,
                "reminder",
                event=0,
                tag=card.id,
                title=title,
                body=body,
                url=config.decision_url("", decision["id"]),
                now=now,
                loud=decision["type"] == "admin_critical",
                mode=mode,
            )
            count += row is not None
        return count

    def _digest(self, now: datetime) -> int:
        sent = self.store.get_state(DIGESTS) or {}
        count = 0
        with self.store._tx():
            for user in self._active():
                person = self._person(user)
                local = now.astimezone(person["tz"])
                day = local.date().isoformat()
                if local.hour != DIGEST_HOUR or sent.get(user) == day:
                    continue
                # the spend is money: only a person with settings is told it
                key, facts = self._facts(
                    user, now, (local.date() - timedelta(days=1)).isoformat()
                )
                body = i18n.t(person["lang"], key, **facts)
                record = {"user": user, "key": key, "params": facts}
                # the servers' part only to who reads the servers
                if self.users.can(user, "servers.read"):
                    record["incidents"] = self._incident_facts(now)
                    body += "\n" + i18n.t(person["lang"], INCIDENTS_KEY, **record["incidents"])
                self._queue(
                    person,
                    "digest",
                    event=0,
                    tag="digest",
                    url=DASHBOARD_URL,
                    now=now,
                    mode="now",
                    title=i18n.t(person["lang"], "push.digest.title"),
                    body=body,
                )
                # the feed shows the digest too: a key and its facts, rendered in the reader's language
                self.store._event("system", "digest", "", record)
                sent[user], count = day, count + 1
            self.store._put_state(DIGESTS, sent)
        return count

    def _facts(self, user, now: datetime, yesterday: str) -> Tuple[str, dict]:
        """The digest's key and facts: the team's counts, and the spend to a person with settings."""
        from kd.cabinet import decisions as ledger

        open_ = [
            d
            for d in ledger.Decisions(self.store).list(state="open")
            if self.users.can(user, f"decide.{d['type']}", d.get("project") or None)
        ]
        since = _fmt(now - timedelta(days=1))
        done = 0
        for event in self._conn.execute(
            "SELECT card, payload FROM events WHERE kind='card.changed' AND ts>? AND ts<=?",
            (since, _fmt(now)),
        ):
            change = json.loads(event["payload"])
            if (
                (change.get("after") or {}).get("status")
                == "Done"
                != (change.get("before") or {}).get("status")
            ):
                card = self._card_of(event["card"])
                done += card is not None and self.users.can(
                    user, "cards.read", card.project
                )
        facts = {"decisions": len(open_), "done": done}
        if not self.users.can(user, "settings"):
            return "push.digest.body.team", facts
        cost, hindsight, coverage = self._tokens(yesterday)
        facts["cost"] = cost
        if coverage == "none":
            return "push.digest.body.spend_nodata", facts
        facts["hindsight"] = hindsight
        if coverage == "partial":
            return "push.digest.body.spend_partial", facts
        return "push.digest.body.spend", facts

    def _incident_facts(self, now: datetime) -> dict:
        """The digest's servers part: open crit and warn by their level now, opened and closed in 24 h, the newest
        open crit P-n."""
        since, until = _fmt(now - timedelta(days=1)), _fmt(now)
        def one(sql, *args):
            return self._conn.execute(sql, args).fetchone()[0]

        crit = [
            f"P-{r[0]}"
            for r in self._conn.execute(
                "SELECT n FROM incidents WHERE status!='resolved' AND level='crit' ORDER BY opened_at DESC, n DESC"
            )
        ]
        return {
            "crit": len(crit),
            "warn": one("SELECT COUNT(*) FROM incidents WHERE status!='resolved' AND level='warn'"),
            "opened": one("SELECT COUNT(*) FROM incidents WHERE opened_at>? AND opened_at<=?", since, until),
            "closed": one("SELECT COUNT(*) FROM incidents WHERE resolved_at>? AND resolved_at<=?", since, until),
            "list": " · " + ", ".join(crit[:DIGEST_CRIT]) if crit else "",
        }

    def _tokens(self, day: str) -> Tuple[str, str, str]:
        """Yesterday's tokens (input, output, cache writes) from kd-usage's ledger, ``N k``: the agents' (with their
        cost), Hindsight's summed over its models, and Hindsight's coverage — ``none`` without its rows (before its
        counting started, or its source was not read), ``partial`` if any row says so."""
        agents, dollars, hindsight, coverage = 0, 0.0, 0, set()
        try:
            with open(self.usage_path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(row, dict) or row.get("day") != day:
                        continue
                    tokens = sum(
                        int(row.get(k) or 0) for k in ("input", "output", "cache_write")
                    )
                    if row.get("category") == HINDSIGHT:
                        hindsight += tokens
                        coverage.add(row.get("coverage"))
                    else:
                        agents += tokens
                        dollars += float(row.get("cost_usd") or 0.0)
        except OSError:
            pass
        text = f"{round(agents / 1000)} k"
        state = (
            "none"
            if not coverage or coverage - {"full", "partial"}
            else "partial" if "partial" in coverage else "full"
        )
        return (
            f"{text} · ${dollars:.2f}" if dollars else text,
            f"{round(hindsight / 1000)} k",
            state,
        )

    def _purge(self, now: datetime) -> int:
        edge = _fmt(now - timedelta(days=KEEP_DAYS))
        with self.store._tx():
            cursor = self._conn.execute(
                "DELETE FROM notifications WHERE (sent != '' AND sent < ?) OR (sent = '' AND due < ?)",
                (edge, edge),
            )
            purged = cursor.rowcount
            # the deliveries of rows gone, and chat presences whose lease ran out (a tab that crashed)
            self._conn.execute(
                "DELETE FROM push_deliveries WHERE notification NOT IN (SELECT id FROM notifications)"
                " OR subscription NOT IN (SELECT id FROM push_subscriptions)")
            self._conn.execute("DELETE FROM chat_presence WHERE until < ?", (_fmt(now),))
        return purged
