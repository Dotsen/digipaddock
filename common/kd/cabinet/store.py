"""The one store of cabinet.db: reads and compare-and-set writes, every write with its event.

A single connection in autocommit mode; each write opens ``BEGIN IMMEDIATE`` itself. The
service serializes calls, so no two writes interleave. Revisions are integers: the card's
revision the caller read must equal the stored one, or the write is a ``Conflict``.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import re
import sqlite3
import secrets
import sys
import unicodedata
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from kd import clock as _clock, council as _council, ids, llm_models
from kd.cabinet import card_edits, schema

CARD_ID = re.compile(r"([A-Z][A-Z0-9]{0,7})-([1-9][0-9]{0,5})")
# The one dictionary of card changes every writer speaks (the pure board models emit it).
CHANGES = frozenset(
    {
        "status",
        "assignee",
        "project",
        "priority",
        "description",
        "notes_append",
        "summary",
        "comment",
        "labels_add",
        "labels_remove",
        "dependencies",
        "servers",
        "ac_add",
        "ac_remove",
        "ac_check",
        "ac_uncheck",
        # a close without the work: written with status Cancelled only
        "outcome",
        "canonical",
        # the card's "Operations": typed factory steps, checked by the caller against the catalog
        "operations",
        # the stages of the brief: checked by the caller (``stages.parse``)
        "stages",
        # the model and effort the card's sessions run on: a cabinet model's slug or '', an effort level or
        # ''
        "model",
        "effort",
        # the card's own council: the composition ``kd.council`` holds, ``{}`` — none of its own
        "council",
        # whole-field writes of people in the web cabinet
        "title",
        "acceptance",
        "dod",
        "notes",
        "plan",
        "labels",
        "due",
        "parent",
    }
)
CREATE_KEYS = frozenset(
    {
        "title",
        "project",
        "status",
        "assignee",
        "description",
        "acceptance",
        "labels",
        "dependencies",
        "servers",
        "priority",
        "due",
        "created_by",
        "parent",  # a stage's child or BOSS's subtask
        "stages",
        # what the new card's form sets with the card, as an edit would: its sessions' model and effort,
        # its own council
        "model",
        "effort",
        "council",
    }
)
TITLE_MAX = 120
PUSH_ATTEMPTS = 5  # a notification the gateway refused this many times is given up
MERGED = "merged:"  # a command delivered in another one's turn: reason "merged:<that one's id>" (commands.MERGEABLE)
# such a command is in its lead's turn — running, not waiting: no query of what is "not run yet" counts it
JOINED_SQL = "(state='delivered' AND substr(reason, 1, 7)='merged:')"
# the wakes a queued lead carried the first time it was sent, under its id: a send again carries the same ones
# (the host answers a command id it has from its cache, whatever the text); gone when the lead leaves the queue
JOIN_KEY = "dispatch.join:"
# A command the cabinet queues itself whose key a row with other content holds already (one written before the op
# boundary reserved the cabinet's keys) goes under the key with this suffix — a character no caller's key may hold —
# instead of failing the write it belongs to (KD-9479)
KEY_TAKEN_SUFFIX = "!"
SERVER_LABEL = "server:"  # the old board's server label is a card_servers row here
SPACE_OF = {kind: space for space, kind in schema.AGENT_SPACES.items()}  # an agent thread's kind → its space
# The coordinator's "Monitor" is its work log (the board watch's attention ticks): no unread replies and no
# push — what people need of a tick it writes in its chat
WORK_LOGS = frozenset({("boss", "monitor")})
_AGENT_KINDS_SQL = "(" + ",".join(f"'{k}'" for k in schema.AGENT_THREAD_KINDS) + ")"


def _json_list(value) -> str:
    """A list column (operations, stages): JSON, or '' for none."""
    items = list(value or ())
    return json.dumps(items, ensure_ascii=False) if items else ""


def _split_servers(labels) -> Tuple[list, list]:
    """(labels, servers) from a label list: ``server:<alias>`` labels become the card's servers."""
    plain, servers = [], []
    for label in labels:
        if label.startswith(SERVER_LABEL):
            servers.append(label[len(SERVER_LABEL) :])
        else:
            plain.append(label)
    return plain, servers


class StoreError(Exception):
    pass


class NotFound(StoreError):
    pass


class Conflict(StoreError):
    pass


class Invalid(StoreError):
    pass


# a tool's masked output is kept this long; a write drops the older ones
OUTPUT_KEEP_DAYS = 30
# one turn's messages: the expression index messages_command (schema 8) serves it
TURN_MESSAGES = ("SELECT seq, kind, body, payload FROM messages WHERE thread=? AND session=?"
                 " AND json_extract(payload, '$.command')=? ORDER BY seq LIMIT ?")
# A turn's cost and its models, from the usage ledger. `command != ''` is what lets the partial index
# `usage_log_command` serve them: a bound parameter alone does not prove it.
TURN_COST = "SELECT SUM(cost) FROM usage_log WHERE command=? AND command != ''"
TURN_MODELS = ("SELECT model FROM usage_log WHERE command=? AND command != '' GROUP BY model"
               " ORDER BY SUM(cost) DESC, SUM(output) DESC, model")


def utcnow() -> str:
    return _clock.iso(_clock.utcnow())


SEQ_MAX = 2**63 - 1  # past any sequence number: "from the newest"
# the platform's alerts — warn and critical notices without a card; ``kind = 'notice' AND card = ''`` is the
# condition of the index events_platform_notice (schema v46), word for word. The index is named: left to itself
# SQLite takes events_card, which walks every event without a card
PLATFORM_NOTICES = ("SELECT seq, ts, actor, kind, card, payload FROM events INDEXED BY events_platform_notice"
                    " WHERE kind = 'notice' AND card = ''"
                    " AND seq < ? AND json_extract(payload, '$.level') IN ('warn', 'critical')"
                    " ORDER BY seq DESC LIMIT ?")
_SPACES = re.compile(r"\s+")


def norm(text) -> str:
    """Text as search compares it: NFKC, case-folded in any script, ё as е, spaces folded. SQLite's own
    LIKE folds ASCII only — "Голосовые" never matched "голос"."""
    if not isinstance(text, str):
        return ""
    folded = unicodedata.normalize("NFKC", text).casefold().replace("ё", "е")
    return _SPACES.sub(" ", folded).strip()


# a search runs one scan of every card per word and alternative under the board's one lock: a query is bounded
SEARCH_TEXT_MAX = 200  # characters, before and after normalization
SEARCH_WORDS_MAX = 10  # different words, each alternative of ``a|b`` counted (each is one more scan)


def search_words(text) -> List[List[str]]:
    """The words of a search, each the list of its alternatives (``a|b``), normalized (``norm``), with a repeated
    word or alternative dropped; ``Invalid`` for a text or a word count past the bounds."""
    if not isinstance(text, str) or len(text) > SEARCH_TEXT_MAX:
        raise Invalid(f"a search is at most {SEARCH_TEXT_MAX} characters")
    folded = norm(text)
    if len(folded) > SEARCH_TEXT_MAX:
        raise Invalid(f"a search is at most {SEARCH_TEXT_MAX} characters")
    words = {}
    for word in folded.split():
        alternatives = tuple(dict.fromkeys(alt for alt in word.split("|") if alt))
        if alternatives:
            words.setdefault(alternatives, None)
    if sum(len(word) for word in words) > SEARCH_WORDS_MAX:
        raise Invalid(f"a search is at most {SEARCH_WORDS_MAX} words")
    return [list(word) for word in words]


def actor_of(name: str) -> str:
    """The actor form of a board name: ``@owner`` -> ``user:owner``, ``@dev`` -> ``agent:dev``."""
    if name == "@owner":
        return "user:owner"
    if name.startswith("@"):
        return "agent:" + name[1:]
    return name


@dataclass(frozen=True)
class Acceptance:
    text: str
    checked: bool = False


@dataclass(frozen=True)
class CardRecord:
    id: str
    n: int
    project: str
    title: str
    status: str
    revision: int
    created_by: str
    created: str
    updated: str
    thread: int
    description: str = ""
    acceptance: Tuple[Acceptance, ...] = ()
    dod: str = ""
    plan: str = ""
    notes: str = ""
    summary: str = ""
    assignee: str = ""
    labels: Tuple[str, ...] = ()
    priority: str = ""
    due: str = ""
    parent: str = ""
    dependencies: Tuple[str, ...] = ()
    servers: Tuple[str, ...] = ()
    outcome: str = ""  # Cancelled: duplicate | cancelled
    canonical: str = ""  # a duplicate's canonical card
    operations: Tuple[dict, ...] = ()  # "Operations": factory steps of the brief or plan
    stages: Tuple[dict, ...] = ()  # the stages of the brief
    model: str = ""  # a cabinet model's slug; '' — the project's, else the subscription
    effort: str = ""  # an effort level; '' — the project's, else the agent's
    # the card's own council (``kd.council``): per kind the critics, the rounds, the least diff; a kind it
    # does not name is the project's
    council: dict = field(default_factory=dict)

    @property
    def terminal(self) -> bool:
        """Done or closed without the work: nothing runs on it any more."""
        return self.status in schema.TERMINAL

    def to_dict(self) -> dict:
        data = self.__dict__.copy()
        data["acceptance"] = [
            {"text": a.text, "checked": a.checked} for a in self.acceptance
        ]
        for key in ("labels", "dependencies", "servers", "operations", "stages"):
            data[key] = list(data[key])
        data["council"] = _council.load(_council.dump(self.council))  # its own copy
        return data

    @classmethod
    def from_dict(cls, data: Mapping) -> "CardRecord":
        fields = dict(data)
        fields["acceptance"] = tuple(
            Acceptance(a["text"], bool(a.get("checked")))
            for a in data.get("acceptance", [])
        )
        for key in ("labels", "dependencies", "servers", "operations", "stages"):
            fields[key] = tuple(data.get(key, ()))
        return cls(**fields)


def _command_row(row) -> dict:
    """A ``commands`` row with its basis as a mapping (``{}`` for a command queued without one)."""
    data = dict(row)
    data["basis"] = json.loads(data.get("basis") or "{}")
    return data


class Store:
    def __init__(self, conn: sqlite3.Connection, prefix: str, clock):
        self._conn, self.prefix, self._clock = conn, prefix, clock or utcnow
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function("kd_norm", 1, norm, deterministic=True)  # search beyond ASCII
        self._depth = 0
        # the commands the open write queued (or found queued already), and those of the last committed one
        self._tx_commands: List[dict] = []
        self.last_commands: List[dict] = []
        # Called inside every write right after its event row, in the same transaction (the
        # commands an event asks for land with it or not at all); an exception rolls the write back.
        self.on_event: Optional[Callable[["Store", dict], None]] = None

    @classmethod
    def open(cls, path: str, *, prefix: str = ids.PREFIX, aliases: Tuple[str, ...] = ids.ALIASES,
             clock=None) -> "Store":
        # One connection, used by one call at a time: the service serializes every call under its lock.
        conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(schema.DDL)
        row = conn.execute(
            "SELECT value FROM meta WHERE key='schema_version'"
        ).fetchone()
        # a new base: v1 now, the migrations below bring it to the current version
        if row is None:
            conn.execute("INSERT INTO meta VALUES ('schema_version', '1')")
            conn.execute("INSERT INTO meta VALUES ('next_card', '1')")
            current = 1
        else:
            current = int(row[0])
        if current > schema.SCHEMA_VERSION:
            conn.close()
            raise StoreError(
                f"cabinet.db schema {current} is newer than this code ({schema.SCHEMA_VERSION})"
            )
        if current < schema.SCHEMA_VERSION:
            _migrate(conn, current)
        _card_prefix(conn, prefix, aliases)
        _default_roles(conn)
        return cls(conn, prefix, clock)

    def close(self):
        self._conn.close()

    def schema_version(self) -> int:
        return int(
            self._conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()[0]
        )

    # --- reads ---------------------------------------------------------------------------------
    def card(self, card_id: str) -> CardRecord:
        row = self._conn.execute(
            "SELECT * FROM cards WHERE id=?", (card_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"{card_id}: card not found")
        return self._record(row)

    def cards(
        self,
        *,
        status=None,
        assignee=None,
        project=None,
        label=None,
        ids=None,
        parent=None,
    ) -> List[CardRecord]:
        where, args = [], []
        for column, value in (
            ("status", status),
            ("assignee", assignee),
            ("project", project),
            ("parent", parent),
        ):
            if value is not None:
                where.append(f"{column}=?")
                args.append(value)
        if ids is not None:
            where.append("id IN (%s)" % ",".join("?" * len(ids)))
            args.extend(ids)
        sql = (
            "SELECT * FROM cards"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY n"
        )
        rows = [self._record(r) for r in self._conn.execute(sql, args).fetchall()]
        return [r for r in rows if label is None or label in r.labels]

    def search(self, text: str, limit: int = 50) -> List[CardRecord]:
        """Cards whose title, description or notes hold every word of ``text`` (Unicode-normalized —
        NFKC, any case of any script, ё as е, spaces folded; a word matches inside a longer one, "голос" finds
        "Голосовые"; ``a|b`` — either). Title matches come first, then the newest. A card id (any case), a bare
        number or ``#number`` — that card of this prefix first. ``Invalid`` past the bounds of ``search_words``."""
        words = search_words(text)
        card_id = ids.canonical(text.strip().upper())
        if re.fullmatch(r"#?[1-9][0-9]{0,5}", card_id):
            card_id = f"{self.prefix}-{card_id.lstrip('#')}"
        match = CARD_ID.fullmatch(card_id)
        exact = None
        if match and match.group(1) == self.prefix:
            exact = self._conn.execute(
                "SELECT * FROM cards WHERE id=?", (card_id,)
            ).fetchone()
        if not words:
            return [self._record(exact)] if exact else []
        where, args, rank = [], [], []
        for word in words:
            where.append("(" + " OR ".join("instr(kd_norm(title || ' ' || description || ' ' || notes), ?) > 0"
                                           for _ in word) + ")")
            args.extend(word)
            rank.append("(" + " OR ".join("instr(kd_norm(title), ?) > 0" for _ in word) + ")")
        rank_args = [alt for word in words for alt in word]
        rows = self._conn.execute(
            "SELECT * FROM cards WHERE " + " AND ".join(where) + " AND id<>? ORDER BY ("
            + " + ".join(rank) + ") DESC, n DESC LIMIT ?",
            args + [card_id if exact else ""] + rank_args + [max(limit - (1 if exact else 0), 0)],
        ).fetchall()
        return [self._record(r) for r in ([exact] if exact else []) + rows]

    def similar(self, text: str, limit: int = 20) -> List[Tuple[CardRecord, int]]:
        """Cards that share words with ``text`` (BOSS's look before it creates), with a score: each word
        of 3+ letters counts by its stem (its first 6 letters — "голосовые" and "голосовых" meet), twice in the
        title. Any status: a Done card may be a regression's origin. The best first, then the newest."""
        stems = sorted({w[:6] for w in re.findall(r"\w+", norm(text)) if len(w) >= 3 and not w.isdigit()})
        if not stems:
            return []
        title = " + ".join("(instr(kd_norm(title), ?) > 0) * 2" for _ in stems)
        body = " + ".join("(instr(kd_norm(description || ' ' || notes), ?) > 0)" for _ in stems)
        rows = self._conn.execute(
            f"SELECT *, ({title} + {body}) AS score FROM cards WHERE score > 0 ORDER BY score DESC, n DESC LIMIT ?",
            stems + stems + [limit],
        ).fetchall()
        return [(self._record(r), r["score"]) for r in rows]

    def messages(
        self, card_id: str, after: int = 0, *, before: Optional[int] = None, limit: Optional[int] = None
    ) -> List[dict]:
        """The card's thread in order; ``after``/``before``/``limit`` as ``thread_messages``."""
        return self.thread_messages(self._thread_id(card_id), after, before=before, limit=limit)

    def events_since(self, seq: int, limit: int = 500) -> List[dict]:
        """The journal after ``seq``; ``thread`` — the thread an event is in, None for none (who may see an agent
        thread's event depends on which of its threads it is: ``api.event_filter``)."""
        rows = self._conn.execute(
            "SELECT seq, ts, actor, kind, card, thread, payload FROM events WHERE seq>? ORDER BY seq LIMIT ?",
            (seq, limit),
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def events_before(self, seq: int, limit: int = 500) -> List[dict]:
        """The journal backwards from ``seq`` (exclusive), newest first."""
        rows = self._conn.execute(
            "SELECT seq, ts, actor, kind, card, thread, payload FROM events WHERE seq<? ORDER BY seq DESC LIMIT ?",
            (seq, limit),
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def platform_notices(self, before: Optional[int] = None, limit: int = 20) -> List[dict]:
        """The platform's own alerts, newest first, before ``before`` (exclusive): notices without a card of
        level warn or critical — a failed unit, the monitor's fallback, a release or a factory operation."""
        rows = self._conn.execute(PLATFORM_NOTICES, (SEQ_MAX if before is None else before, limit)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def card_events(self, card_id: str, limit: int = 1000) -> List[dict]:
        rows = self._conn.execute(
            "SELECT seq, ts, actor, kind, card, payload FROM events WHERE card=? ORDER BY seq LIMIT ?",
            (card_id, limit),
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def card_events_page(
        self, card_id: str, before: Optional[int] = None, limit: int = 100
    ) -> List[dict]:
        """The card's history backwards from ``before`` (exclusive; None — from the newest), newest first."""
        rows = self._conn.execute(
            "SELECT seq, ts, actor, kind, card, payload FROM events WHERE card=? AND seq<? "
            "ORDER BY seq DESC LIMIT ?",
            (card_id, before if before is not None else 2**62, limit),
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def recent_messages_of(
        self, author: str, kind: str, limit: int = 200
    ) -> List[dict]:
        """``author``'s latest messages of ``kind`` in card threads, newest first: ``{card, seq, body, created}``."""
        rows = self._conn.execute(
            "SELECT t.card, m.seq, m.body, m.created FROM messages m JOIN threads t ON t.id = m.thread "
            "WHERE m.author=? AND m.kind=? AND t.card IS NOT NULL ORDER BY m.created DESC, m.id DESC LIMIT ?",
            (author, kind, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark(self, user: str, name: str, value: int) -> int:
        """A person's mark (``schema.MARKS``) moved up to ``value``; it never goes back. The mark is the reader's
        own place on a screen, not a fact of the board: no event. Returns the mark as it stands."""
        if name not in schema.MARKS:
            raise Invalid(f"unknown mark {name!r}")
        with self._tx():
            self._conn.execute(
                "INSERT INTO user_marks VALUES (?,?,?,?) ON CONFLICT(user, name) DO UPDATE SET "
                "value=MAX(value, excluded.value), updated=excluded.updated",
                (user, name, value, self._clock()),
            )
            return self.marks(user)[name]

    def marks(self, user: str) -> Dict[str, int]:
        rows = self._conn.execute("SELECT name, value FROM user_marks WHERE user=?", (user,)).fetchall()
        return {r["name"]: r["value"] for r in rows}

    # --- read cursors of agent chats ----------------------------------------------------
    # A reply is the agent's text to people: its text messages in its own chat, those of one turn (the host marks
    # each with the turn's command) one reply, a ``kd-task chat --post`` one of its own. Tool steps, status lines
    # and people's own messages are not replies.
    _REPLIES = (
        "FROM messages m JOIN threads t ON t.id=m.thread WHERE m.thread=? AND m.seq>? AND m.kind='text'"
        f" AND t.kind IN {_AGENT_KINDS_SQL} AND m.author='agent:'||t.agent"
    )
    REPLY_KEY = "COALESCE(json_extract(m.payload, '$.command'), 'seq:'||m.seq)"

    def chat_cursor(self, user: str, thread: int) -> int:
        row = self._conn.execute("SELECT seq FROM chat_reads WHERE user=? AND thread=?", (user, thread)).fetchone()
        return row[0] if row is not None else 0

    def chat_state(self, user: str, thread: int) -> dict:
        """``{read, unread, first, last}``: the person's cursor, the replies past it, the first unread message
        and the thread's last message seq."""
        read = self.chat_cursor(user, thread)
        unread, first = self._conn.execute(
            f"SELECT COUNT(DISTINCT {self.REPLY_KEY}), MIN(m.seq) " + self._REPLIES, (thread, read)
        ).fetchone()
        last = self._conn.execute("SELECT COALESCE(MAX(seq), 0) FROM messages WHERE thread=?", (thread,)).fetchone()[0]
        return {"read": read, "unread": unread, "first": first, "last": last}

    def chat_read(self, user: str, thread: int, seq: int) -> dict:
        """The person was shown the chat up to ``seq``: the cursor moves up to it (never back, never past the
        thread's last message). A move is a ``chat.read`` event for the person's other pages; a repeat is none."""
        with self._tx():
            state = self.chat_state(user, thread)
            target = min(seq, state["last"])
            if target <= state["read"]:
                return state
            self._conn.execute(
                "INSERT INTO chat_reads VALUES (?,?,?,?) ON CONFLICT(user, thread) DO UPDATE SET "
                "seq=MAX(seq, excluded.seq), updated=excluded.updated",
                (user, thread, target, self._clock()),
            )
            info = self.thread_info(thread)
            event = {"user": user, "agent": info["agent"], "seq": target}
            if SPACE_OF.get(info["kind"], "chat") != "chat":
                event["space"] = SPACE_OF[info["kind"]]
            self._event(f"user:{user}", "chat.read", "", event, thread)
            return self.chat_state(user, thread)

    def chat_reads_start(self, user: str) -> None:
        """A new person starts at the end of every agent thread (inside the write that adds them)."""
        self._conn.execute(
            "INSERT OR IGNORE INTO chat_reads (user, thread, seq, updated) SELECT ?, t.id, "
            "COALESCE((SELECT MAX(m.seq) FROM messages m WHERE m.thread=t.id), 0), ? "
            f"FROM threads t WHERE t.kind IN {_AGENT_KINDS_SQL}",
            (user, self._clock()),
        )

    def agent_spaces(self) -> Dict[Tuple[str, str], int]:
        """Every agent thread with people that exists: (agent, space) → thread."""
        rows = self._conn.execute(
            f"SELECT agent, kind, id FROM threads WHERE kind IN {_AGENT_KINDS_SQL} ORDER BY agent, kind"
        ).fetchall()
        return {(r["agent"], SPACE_OF[r["kind"]]): r["id"] for r in rows}

    def last_seq(self) -> int:
        return self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM events"
        ).fetchone()[0]

    def projects(self) -> Dict[str, str]:
        """project -> route (``@agent`` or ``@owner``): what the board routes by, read at every call."""
        return {
            r["id"]: r["route"]
            for r in self._conn.execute("SELECT id, route FROM projects ORDER BY id")
        }

    _PROJECT_COLUMNS = ("id, name, route, repo, routed, repo_state, repo_detail, repo_updated, memory_bank, done_by,"
                        " model, effort, council")

    def project_rows(self) -> List[dict]:
        """Every project as ``{id, name, route, repo, routed, repo_state, repo_detail, repo_updated, memory_bank,
        done_by, model, effort}`` (``routed`` — when its route last changed; ``repo_*`` — its repository's request or
        result; ``memory_bank`` — the Hindsight bank of its cards' sessions, ``""`` the role bank;
        ``done_by`` — the agent that closes its cards Done itself, ``""`` the owner's acceptance; ``model``,
        ``effort`` — the defaults of its cards' sessions, ``""`` the subscription and the agent's effort;
        ``council`` — the default council of its cards as ``kd.council`` holds it, ``{}`` none), by id."""
        return [
            self._project_row(r)
            for r in self._conn.execute(f"SELECT {self._PROJECT_COLUMNS} FROM projects ORDER BY id")
        ]

    @staticmethod
    def _project_row(row) -> dict:
        data = dict(row)
        data["council"] = _council.load(data["council"])  # the default council of its cards, parsed
        return data

    def project(self, project_id) -> Optional[dict]:
        row = self._conn.execute(
            f"SELECT {self._PROJECT_COLUMNS} FROM projects WHERE id=?", (project_id,)
        ).fetchone()
        return None if row is None else self._project_row(row)

    def set_project_repo(self, project_id, *, actor, repo, state, detail="") -> dict:
        """A project's repository request or the root unit's answer, one transaction with its event
        ``project.repo``; ``state`` — one of ``schema.REPO_REQUESTS``, ``schema.REPO_RESULTS`` or ``""`` (unbound)."""
        if state not in ("",) + schema.REPO_REQUESTS + schema.REPO_RESULTS:
            raise Invalid(f"unknown repository state {state!r}")
        with self._tx():
            before = self.project(project_id)
            if before is None:
                raise NotFound(f"no project {project_id}")
            self._conn.execute(
                "UPDATE projects SET repo=?, repo_state=?, repo_detail=?, repo_updated=? WHERE id=?",
                (repo, state, detail, self._clock(), project_id),
            )
            payload = {"project": project_id, "repo": repo, "state": state}
            if before["repo"] != repo:
                payload["from"] = before["repo"]
            if detail:
                payload["detail"] = detail
            self._event(actor, "project.repo", "", payload)
            return self.project(project_id)

    def repo_requests(self) -> List[dict]:
        """The repository requests the root unit has not answered yet, oldest first."""
        marks = ",".join("?" * len(schema.REPO_REQUESTS))
        return [
            self._project_row(r) for r in self._conn.execute(
                f"SELECT {self._PROJECT_COLUMNS} FROM projects WHERE repo_state IN ({marks}) ORDER BY repo_updated, id",
                schema.REPO_REQUESTS,
            )
        ]

    def project_cards(self, project_id) -> List[str]:
        """The ids of every card of the project, Done ones too, oldest first."""
        return [r[0] for r in self._conn.execute("SELECT id FROM cards WHERE project=? ORDER BY n", (project_id,))]

    def _project_roles(self, project_id) -> List[tuple]:
        """``(user, role, projects)`` of every role that names the project (a role over all projects does not)."""
        found = []
        for row in self._conn.execute("SELECT user, role, projects FROM user_roles WHERE projects != '\"*\"'"):
            held = json.loads(row["projects"])
            if isinstance(held, list) and project_id in held:
                found.append((row["user"], row["role"], held))
        return found

    def project_people(self, project_id) -> List[str]:
        """The people whose roles name the project — whom its delete touches."""
        return sorted({user for user, _, _ in self._project_roles(project_id)})

    # --- the owner's projects: each change one transaction with its event -----------------------------
    def create_project(self, project_id, name, route, *, actor) -> dict:
        with self._tx():
            if self.project(project_id) is not None:
                raise Conflict(f"project {project_id} exists")
            now = self._clock()
            self._conn.execute(
                "INSERT INTO projects (id, name, route, routed) VALUES (?,?,?,?)", (project_id, name, route, now)
            )
            self._event(actor, "project.created", "", {"project": project_id, "name": name, "route": route})
            return self.project(project_id)

    def update_project(self, project_id, *, actor, name=None, route=None, memory_bank=None, done_by=None, model=None,
                       effort=None, council=None) -> dict:
        """A new name, route, memory bank, closer, model, effort or council; a new route counts for the cards made
        from now on (``routed``), a new bank, model or effort for the sessions started from now on, a new closer for
        the next Done, a new council (a kind without critics is dropped) for the councils gathered from now
        on."""
        with self._tx():
            before = self.project(project_id)
            if before is None:
                raise NotFound(f"no project {project_id}")
            changes = {}
            if name is not None and name != before["name"]:
                changes["name"] = name
            if route is not None and route != before["route"]:
                changes.update(route=route, routed=self._clock())
            if memory_bank is not None and memory_bank != before["memory_bank"]:
                changes["memory_bank"] = memory_bank
            if done_by is not None and done_by != before["done_by"]:
                changes["done_by"] = done_by
            if model is not None and self._model_ref(model) != before["model"]:
                changes["model"] = model
            if effort is not None and self._effort(effort) != before["effort"]:
                changes["effort"] = effort
            if council is not None:
                text = self._council_text(council, before["council"], keep_empty=False)
                if text != _council.dump(before["council"]):
                    changes["council"] = text
            if changes:
                sets = ", ".join(f"{k}=?" for k in changes)
                self._conn.execute(f"UPDATE projects SET {sets} WHERE id=?", (*changes.values(), project_id))
                payload = {"project": project_id}
                for key in ("name", "route", "memory_bank", "done_by", "model", "effort"):
                    if key in changes:
                        payload[key] = {"from": before[key], "to": changes[key]}
                if "council" in changes:
                    payload["council"] = {"from": before["council"], "to": _council.load(changes["council"])}
                self._event(actor, "project.changed", "", payload)
            return self.project(project_id)

    # --- the models of other providers: each change one transaction with its event -------------------
    _LLM_COLUMNS = ("id, provider, base_url, upstream, title, price_in, price_out, price_cache_read, price_cache_write,"
                    " context, enabled, probe, created, updated")
    _LLM_CREATE = frozenset({"id", "provider", "base_url", "upstream", "title", "price_in", "price_out",
                             "price_cache_read", "price_cache_write", "context"})
    _LLM_UPDATE = frozenset({"title", "upstream", "base_url", "price_in", "price_out", "price_cache_read",
                             "price_cache_write", "context", "enabled", "probe"})
    _LLM_PRICES = ("price_in", "price_out", "price_cache_read", "price_cache_write")
    LLM_PRICE_MAX = 10_000.0  # USD per million tokens
    LLM_CONTEXT_MAX = 100_000_000

    @staticmethod
    def _llm_row(row) -> dict:
        data = dict(row)
        data["probe"] = json.loads(data["probe"] or "{}")
        return data

    def llm_models(self, enabled_only=False) -> List[dict]:
        """Every cabinet model (or the enabled ones) as its row with ``probe`` parsed, by id."""
        sql = f"SELECT {self._LLM_COLUMNS} FROM llm_models" + (" WHERE enabled=1" if enabled_only else "")
        return [self._llm_row(r) for r in self._conn.execute(sql + " ORDER BY id")]

    def llm_model(self, model_id) -> Optional[dict]:
        row = self._conn.execute(f"SELECT {self._LLM_COLUMNS} FROM llm_models WHERE id=?", (model_id,)).fetchone()
        return None if row is None else self._llm_row(row)

    def llm_model_refs(self, model_id) -> dict:
        """``{cards: [ids], projects: [ids]}`` that name the model — as what their sessions run on or as a critic
        of their council: what keeps it from a delete."""
        def named(table, order):
            rows = self._conn.execute(
                f"SELECT id, model, council FROM {table} WHERE model=? OR instr(council, ?) > 0 ORDER BY {order}",
                (model_id, model_id))
            return [r["id"] for r in rows
                    if r["model"] == model_id or model_id in _council.critics(_council.load(r["council"]))]

        return {"cards": named("cards", "n"), "projects": named("projects", "id")}

    @classmethod
    def _llm_values(cls, values: Mapping, spec: Mapping) -> dict:
        """The checked column values among ``values`` (a create's whole set or an update's part); ``spec`` — the
        model's id, provider, upstream and base_url as they stand after the change (``kd.llm_models.check_spec``)."""
        try:
            checked = llm_models.check_spec(spec)
        except ValueError as exc:
            raise Invalid(str(exc)) from None
        out = {k: checked[k] for k in llm_models.SPEC_FIELDS if k in values}
        if "title" in values:
            title = values["title"].strip() if isinstance(values["title"], str) else ""
            if not title or len(title) > TITLE_MAX:
                raise Invalid("a model's title is empty or too long")
            out["title"] = title
        for key in cls._LLM_PRICES:
            if key in values:
                price = values[key]
                if isinstance(price, bool) or not isinstance(price, (int, float)) or not 0 <= price <= cls.LLM_PRICE_MAX:
                    raise Invalid(f"{key} is USD per million tokens, from 0 to {cls.LLM_PRICE_MAX:.0f}")
                out[key] = float(price)
        if "context" in values:
            context = values["context"]
            if isinstance(context, bool) or not isinstance(context, int) or not 1 <= context <= cls.LLM_CONTEXT_MAX:
                raise Invalid("context is a number of tokens, 1 or more")
            out["context"] = context
        if "enabled" in values:
            if values["enabled"] not in (0, 1):
                raise Invalid("enabled is 0 or 1")
            out["enabled"] = int(values["enabled"])
        if "probe" in values:
            if not isinstance(values["probe"], Mapping):
                raise Invalid("probe is an object")
            out["probe"] = json.dumps(values["probe"], ensure_ascii=False, sort_keys=True)
        return out

    def create_llm_model(self, values: Mapping, *, actor) -> dict:
        """A new cabinet model, enabled and unprobed: ``values`` are exactly id, provider, base_url, upstream, title,
        the four prices and context."""
        if set(values) != self._LLM_CREATE:
            raise Invalid("a model has exactly the fields " + ", ".join(sorted(self._LLM_CREATE)))
        columns = self._llm_values(values, {k: values[k] for k in llm_models.SPEC_FIELDS})
        with self._tx():
            if self.llm_model(columns["id"]) is not None:
                raise Conflict(f"model {columns['id']} exists")
            now = self._clock()
            columns.update(created=now, updated=now)
            marks = ", ".join("?" * len(columns))
            self._conn.execute(f"INSERT INTO llm_models ({', '.join(columns)}) VALUES ({marks})",
                               tuple(columns.values()))
            self._event(actor, "llm_model.created", "",
                        {"model": columns["id"], "provider": columns["provider"], "upstream": columns["upstream"]})
            return self.llm_model(columns["id"])

    def update_llm_model(self, model_id, changes: Mapping, *, actor) -> dict:
        """Any of title, upstream, base_url, the prices, context, enabled, probe; the id and the kind never change
        (the usage history lies under the id). The event names what changed as ``{from, to}``."""
        extra = set(changes) - self._LLM_UPDATE
        if extra or not changes:
            raise Invalid("a model's update is any of " + ", ".join(sorted(self._LLM_UPDATE)))
        with self._tx():
            before = self.llm_model(model_id)
            if before is None:
                raise NotFound(f"no model {model_id}")
            spec = {k: changes.get(k, before[k]) for k in llm_models.SPEC_FIELDS}
            columns = self._llm_values(changes, spec)
            after = {**before, **columns, "probe": changes.get("probe", before["probe"])}
            changed = {k for k in columns if after[k] != before[k]}
            if not changed:
                return before
            columns = {k: v for k, v in columns.items() if k in changed}
            columns["updated"] = self._clock()
            sets = ", ".join(f"{k}=?" for k in columns)
            self._conn.execute(f"UPDATE llm_models SET {sets} WHERE id=?", (*columns.values(), model_id))
            after = self.llm_model(model_id)
            payload = {"model": model_id, **{k: {"from": before[k], "to": after[k]} for k in sorted(changed)}}
            self._event(actor, "llm_model.changed", "", payload)
            return after

    def delete_llm_model(self, model_id, *, actor) -> None:
        """The model's row goes — unless a card or a project names it (then it is disabled instead, by the caller)."""
        with self._tx():
            before = self.llm_model(model_id)
            if before is None:
                raise NotFound(f"no model {model_id}")
            refs = self.llm_model_refs(model_id)
            if refs["cards"] or refs["projects"]:
                raise Invalid(f"model {model_id} is set on cards or projects; disable it instead")
            self._conn.execute("DELETE FROM llm_models WHERE id=?", (model_id,))
            self._event(actor, "llm_model.deleted", "",
                        {"model": model_id, "provider": before["provider"], "upstream": before["upstream"]})

    def delete_project(self, project_id, *, actor, target="", cards=0) -> List[str]:
        """The project's row goes, its pull requests follow ``target`` and the people's roles lose it (a role left with
        no project goes too); the users whose roles changed. Its cards are moved by the caller first, in the same
        transaction (``kd.cabinet.projects_api``)."""
        with self._tx():
            if self.project(project_id) is None:
                raise NotFound(f"no project {project_id}")
            if self.project_cards(project_id):
                raise Invalid(f"project {project_id} still has cards")
            if target:
                self._conn.execute("UPDATE prs SET project=? WHERE project=?", (target, project_id))
            people = set()
            for user, role, held in self._project_roles(project_id):
                people.add(user)
                rest = [p for p in held if p != project_id]
                if rest:
                    self._conn.execute(
                        "UPDATE user_roles SET projects=? WHERE user=? AND role=?", (json.dumps(rest), user, role)
                    )
                else:
                    self._conn.execute("DELETE FROM user_roles WHERE user=? AND role=?", (user, role))
            self._conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
            self._event(
                actor, "project.deleted", "",
                {"project": project_id, "target": target, "cards": cards, "users": sorted(people)},
            )
            return sorted(people)

    def path(self) -> str:
        """The base's file (for its backups)."""
        return self._conn.execute("PRAGMA database_list").fetchone()[2]

    def get_state(self, key: str):
        row = self._conn.execute(
            "SELECT value FROM state WHERE key=?", (key,)
        ).fetchone()
        return None if row is None else json.loads(row[0])

    # --- writes --------------------------------------------------------------------------------
    def transaction(self):
        """One write; a block inside another joins it (see ``_Tx``)."""
        return _Tx(self)

    _tx = transaction

    def count_events(self, actor: str, since: str, kind: Optional[str] = None) -> int:
        """Events of ``actor`` stamped at or after ``since`` (of one kind, or any)."""
        # a thread message another write already stands for (``via``) is not a write of its own, nor are the
        # commands, runs and sessions of the session host the agent's writes start (``commands.OWN_EVENTS``), nor
        # the brief version or approval a card's write records (``briefs.after_edit``)
        sql = (
            "SELECT COUNT(*) FROM events WHERE actor=? AND ts>=?"
            " AND NOT (kind='message.added' AND json_extract(payload, '$.via') IS NOT NULL)"
            " AND kind NOT LIKE 'command.%' AND kind NOT LIKE 'run.%' AND kind NOT LIKE 'session.%'"
            " AND kind NOT LIKE 'brief.%'"
        )
        if kind is None:
            row = self._conn.execute(sql, (actor, since)).fetchone()
        else:
            row = self._conn.execute(sql + " AND kind=?", (actor, since, kind)).fetchone()
        return int(row[0])

    def _event(self, actor, kind, card, payload, thread=None) -> int:
        ts = self._clock()
        cursor = self._conn.execute(
            "INSERT INTO events (ts, actor, kind, card, thread, payload) VALUES (?,?,?,?,?,?)",
            (
                ts,
                actor,
                kind,
                card,
                thread,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
            ),
        )
        seq = cursor.lastrowid
        if self.on_event is not None:
            self.on_event(
                self,
                {
                    "seq": seq,
                    "ts": ts,
                    "actor": actor,
                    "kind": kind,
                    "card": card,
                    "thread": thread,
                    "payload": payload,
                },
            )
        return seq

    def wake_card(self, card_id: str, *, actor: str, agent: str) -> CardRecord:
        """A ``card.wake`` event without a card change: ``actor`` asks ``agent`` (``@dev``) to look at the card
        now; kd-board sync turns it into the agent's wake on the session host (the owner's «take it into work» on a To Do
        card, or the brief request to the analyst in Inbox)."""
        with self._tx():
            card = self.card(card_id)
            self._event(
                actor, "card.wake", card.id, {"agent": agent, "title": card.title}, card.thread
            )
            return card

    def create_card(self, values: Mapping, *, actor: str) -> CardRecord:
        extra = set(values) - CREATE_KEYS
        if extra:
            raise Invalid(f"unknown field: {', '.join(sorted(extra))}")
        title = str(values.get("title", "")).strip()
        if not title or len(title) > TITLE_MAX:
            raise Invalid("title is empty or too long")
        status = values.get("status", "Inbox")
        if status not in schema.STATUSES or status == "Cancelled":  # closed without the work: only by a close
            raise Invalid(f"unknown status {status!r}")
        if not values.get("project") or not values.get("created_by"):
            raise Invalid("a card needs a project and an author")
        now = self._clock()
        with self._tx():
            spend = {
                "model": self._model_ref(values.get("model", "")),
                "effort": self._effort(values.get("effort", "")),
                "council": self._council_text(values.get("council", {}), {}),
            }
            n = int(
                self._conn.execute(
                    "SELECT value FROM meta WHERE key='next_card'"
                ).fetchone()[0]
            )
            card_id = f"{self.prefix}-{n}"
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='next_card'", (str(n + 1),)
            )
            self._insert_card(
                card_id, n, values, title, status, revision=1, created=now, updated=now, **spend
            )
            self._event(
                actor,
                "card.created",
                card_id,
                {
                    "title": title,
                    "status": status,
                    "assignee": values.get("assignee", ""),
                    "project": values["project"],
                },
                self._thread_id(card_id),
            )
            return self.card(card_id)

    def _insert_card(
        self, card_id, n, values, title, status, *, revision, created, updated, model="", effort="", council=""
    ):
        acceptance = [
            a if isinstance(a, Mapping) else {"text": a, "checked": False}
            for a in values.get("acceptance", [])
        ]
        labels, labelled = _split_servers(values.get("labels", []))
        servers = sorted(set(values.get("servers", [])) | set(labelled))
        self._conn.execute(
            "INSERT INTO cards (id, n, project, title, description, acceptance, dod, plan, notes, summary, status, assignee,"
            " labels, priority, due, parent, created_by, revision, created, updated, stages,"
            " model, effort, council)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                card_id,
                n,
                values["project"],
                title,
                values.get("description", ""),
                json.dumps(acceptance, ensure_ascii=False),
                values.get("dod", ""),
                values.get("plan", ""),
                values.get("notes", ""),
                values.get("summary", ""),
                status,
                values.get("assignee", ""),
                json.dumps(labels, ensure_ascii=False),
                values.get("priority", ""),
                values.get("due", ""),
                values.get("parent", ""),
                values["created_by"],
                revision,
                created,
                updated,
                _json_list(values.get("stages")),
                model,
                effort,
                council,
            ),
        )
        self._conn.executemany(
            "INSERT INTO card_deps VALUES (?,?)",
            [(card_id, d) for d in values.get("dependencies", [])],
        )
        self._conn.executemany(
            "INSERT INTO card_servers VALUES (?,?)", [(card_id, s) for s in servers]
        )
        self._conn.execute(
            "INSERT INTO threads (kind, card) VALUES ('card', ?)", (card_id,)
        )

    def edit_card(
        self,
        card_id,
        revision,
        changes: Mapping,
        *,
        actor: str,
        guards: Mapping[str, int] = None,
        event_extra: Mapping = None,
        promoting: bool = False,
    ) -> CardRecord:
        """One compare-and-set write: ``revision`` (None only from the service itself) and every guard must be current.

        ``event_extra`` adds fields to the event's payload (the owner's grant of a BOSS promote). What the edit sets
        off is ``kd.cabinet.card_edits``; ``promoting`` — the edit is a BOSS promote's approval, which
        records the decision after it: the promote stays open for it."""
        extra = set(changes) - CHANGES
        if extra:
            raise Invalid(f"unknown change: {', '.join(sorted(extra))}")
        with self._tx():
            before = self.card(card_id)
            if revision is not None and revision != before.revision:
                raise Conflict(
                    f"{card_id}: revision {revision} is not current ({before.revision})"
                )
            for dep_id, dep_revision in (guards or {}).items():
                if self.card(dep_id).revision != dep_revision:
                    raise Conflict(f"{dep_id}: revision changed")
            columns = self._apply(before, changes)
            card_edits.before_write(self, before, changes, columns)  # what follows from an edit: kd.cabinet.card_edits
            columns["revision"] = before.revision + 1
            columns["updated"] = self._clock()
            sets = ", ".join(f"{k}=?" for k in columns)
            self._conn.execute(
                f"UPDATE cards SET {sets} WHERE id=?", (*columns.values(), card_id)
            )
            if "dependencies" in changes:
                self._conn.execute("DELETE FROM card_deps WHERE card=?", (card_id,))
                self._conn.executemany(
                    "INSERT INTO card_deps VALUES (?,?)",
                    [(card_id, d) for d in changes["dependencies"]],
                )
            servers = self._servers_after(before, changes)
            if servers is not None:
                self._conn.execute("DELETE FROM card_servers WHERE card=?", (card_id,))
                self._conn.executemany(
                    "INSERT INTO card_servers VALUES (?,?)",
                    [(card_id, s) for s in servers],
                )
            comment = changes.get("comment")
            noted = 0
            if comment:
                noted = self._add_message(
                    before.thread,
                    actor_of(comment["author"]),
                    "text",
                    comment["text"],
                    {},
                    "",
                    via="card.changed",
                )
            after = self.card(card_id)
            self._event(
                actor,
                "card.changed",
                card_id,
                {
                    **(event_extra or {}),
                    "fields": sorted(changes),
                    "revision": after.revision,  # an ack's edit:<revision> finds BOSS's own write
                    "title": after.title,
                    "comment": bool(comment),
                    **({"comment_seq": noted} if noted else {}),  # the comment's message: the card's result reads it
                    "before": {"status": before.status, "assignee": before.assignee, "labels": list(before.labels)},
                    "after": {"status": after.status, "assignee": after.assignee, "labels": list(after.labels)},
                },
                before.thread,
            )
            card_edits.in_write(self, before, after, changes, actor, promoting)
            return after

    @staticmethod
    def _servers_after(card: CardRecord, changes: Mapping):
        """The card's servers after ``servers`` and any ``server:`` labels added or removed; None if untouched."""
        _, added = _split_servers(changes.get("labels_add", []))
        _, removed = _split_servers(changes.get("labels_remove", []))
        if "servers" not in changes and not added and not removed:
            return None
        servers = set(changes["servers"] if "servers" in changes else card.servers)
        return sorted((servers - set(removed)) | set(added))

    def _apply(self, card: CardRecord, changes: Mapping) -> dict:
        """Column values after ``changes``; Invalid for a value the board cannot hold."""
        columns = {}
        for key in (
            "assignee",
            "project",
            "priority",
            "description",
            "summary",
            "dod",
            "notes",
            "plan",
            "due",
            "parent",
        ):
            if key in changes:
                columns[key] = str(changes[key])
        if "title" in changes:
            title = str(changes["title"]).strip()
            if not title or len(title) > TITLE_MAX:
                raise Invalid("title is empty or too long")
            columns["title"] = title
        if "model" in changes:
            columns["model"] = self._model_ref(changes["model"])
        if "effort" in changes:
            columns["effort"] = self._effort(changes["effort"])
        if "council" in changes:
            columns["council"] = self._council_text(changes["council"], card.council)
        if "operations" in changes:
            columns["operations"] = _json_list(changes["operations"])
        if "stages" in changes:
            columns["stages"] = _json_list(changes["stages"])
        if "labels" in changes:
            plain, _ = _split_servers(changes["labels"])
            columns["labels"] = json.dumps(plain, ensure_ascii=False)
        if "status" in changes:
            if changes["status"] not in schema.STATUSES:
                raise Invalid(f"unknown status {changes['status']!r}")
            columns["status"] = changes["status"]
        columns.update(self._closing(card, changes))
        if "notes_append" in changes:
            columns["notes"] = (card.notes + "\n" if card.notes else "") + str(
                changes["notes_append"]
            )
        if "labels_add" in changes or "labels_remove" in changes:
            add, _ = _split_servers(changes.get("labels_add", []))
            remove, _ = _split_servers(changes.get("labels_remove", []))
            labels = [label for label in card.labels if label not in remove]
            labels += [label for label in add if label not in labels]
            columns["labels"] = json.dumps(labels, ensure_ascii=False)
        acceptance = [{"text": a.text, "checked": a.checked} for a in card.acceptance]
        if "acceptance" in changes:
            acceptance = [
                {"text": str(a["text"]), "checked": bool(a.get("checked"))}
                for a in changes["acceptance"]
            ]
            columns["acceptance"] = json.dumps(acceptance, ensure_ascii=False)
        for index in changes.get("ac_remove", []):
            self._index(acceptance, index)
        for index in sorted(set(changes.get("ac_remove", [])), reverse=True):
            del acceptance[index - 1]
        acceptance += [
            {"text": str(t), "checked": False} for t in changes.get("ac_add", [])
        ]
        for key, flag in (("ac_check", True), ("ac_uncheck", False)):
            for index in changes.get(key, []):
                acceptance[self._index(acceptance, index) - 1]["checked"] = flag
        if any(k in changes for k in ("ac_remove", "ac_add", "ac_check", "ac_uncheck")):
            columns["acceptance"] = json.dumps(acceptance, ensure_ascii=False)
        return columns

    @staticmethod
    def _closing(card: CardRecord, changes: Mapping) -> dict:
        """Cancelled always says why (``outcome``), a duplicate names its ``canonical`` card; the two are
        written only with a move into Cancelled, and leaving it clears them (the event history keeps them)."""
        status = changes.get("status", card.status)
        if "outcome" in changes or "canonical" in changes:
            if status != "Cancelled" or "status" not in changes:
                raise Invalid("an outcome is written only with the move into Cancelled")
        if status != "Cancelled":
            return {"outcome": "", "canonical": ""} if card.outcome or card.canonical else {}
        if "status" not in changes:
            return {}
        outcome = changes.get("outcome")
        if outcome not in schema.CLOSE_OUTCOMES:
            raise Invalid("Cancelled needs an outcome: duplicate or cancelled")
        canonical = str(changes.get("canonical") or "")
        if (outcome == "duplicate") != bool(canonical):
            raise Invalid("a duplicate names its canonical card; a cancelled card names none")
        if canonical == card.id:
            raise Invalid("a card is no duplicate of itself")
        return {"outcome": outcome, "canonical": canonical}

    def _model_ref(self, value) -> str:
        """A cabinet model's slug, a subscription model's reference (``claude:<alias>`` of the catalog), or
        '' for the agent's own model; Invalid for one neither the table nor the catalog holds."""
        if value == "":
            return ""
        if llm_models.is_ref(value):
            if value not in llm_models.claude_models():
                raise Invalid(f"unknown model {value[:64]!r}")
            return value
        if not isinstance(value, str) or self.llm_model(value) is None:
            raise Invalid(f"unknown model {str(value)[:64]!r}")
        return value

    def _council_text(self, value, stored: Mapping, keep_empty: bool = True) -> str:
        """A council's composition as its column holds it (``kd.council``: its one form, every critic a model the
        table or the catalog holds — or one ``stored``, the composition it replaces, names already), ``""`` for
        none; Invalid otherwise."""
        kept = _council.critics(stored)

        def critic(name):
            if name not in kept:
                self._model_ref(name)

        try:
            return _council.dump(_council.normalize(value, critic, keep_empty))
        except ValueError as exc:
            raise Invalid(f"council: {exc}") from None

    @staticmethod
    def _effort(value) -> str:
        """An effort level or '' (the project's, else the agent's); Invalid otherwise."""
        try:
            return llm_models.check_effort(value)
        except ValueError as exc:
            raise Invalid(str(exc)) from None

    @staticmethod
    def _index(items, index) -> int:
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 1 <= index <= len(items)
        ):
            raise Invalid(f"acceptance item {index} does not exist")
        return index

    def _add_message(
        self, thread, author, kind, body, payload, session, *, via="", extra=None, notify=True
    ):
        """One thread message and, unless ``notify`` is off (an import), its ``message.added`` event in the
        caller's transaction: every open page of the card hears of every message. ``via`` names the
        write the message is part of (``card.changed``, ``decision``): that write has its own event, so the
        feed shows it once and the agents' write limit counts it once."""
        if kind not in schema.MESSAGE_KINDS:
            raise Invalid(f"unknown message kind {kind!r}")
        seq = self._conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE thread=?", (thread,)
        ).fetchone()[0]
        self._conn.execute(
            "INSERT INTO messages (thread, seq, author, kind, body, payload, session, created) VALUES (?,?,?,?,?,?,?,?)",
            (
                thread,
                seq,
                author,
                kind,
                body,
                json.dumps(payload or {}, ensure_ascii=False),
                session,
                self._clock(),
            ),
        )
        from kd.cabinet import incident_reports

        report_incidents = incident_reports.link_message(self, thread, seq, author, kind, body)
        if notify:
            row = self._conn.execute(
                "SELECT t.kind, t.agent, c.id, c.title FROM threads t LEFT JOIN cards c ON c.id = t.card WHERE t.id=?",
                (thread,),
            ).fetchone()
            event = {"seq": seq, "kind": kind, "title": (row["title"] or "") if row else "", **(extra or {})}
            if report_incidents:
                event["incidents"] = report_incidents
            if via:
                event["via"] = via
            if row is not None and row["kind"] in SPACE_OF:
                event["agent"] = row["agent"]  # who may see it: the agent chat has no card (event_filter)
                if SPACE_OF[row["kind"]] != "chat":
                    event["space"] = SPACE_OF[row["kind"]]  # which of the agent's threads
            self._event(author, "message.added", (row["id"] or "") if row else "", event, thread)
        return seq

    def add_message(
        self, card_id, *, author, kind, body, payload=None, session="", mentions=(), client_key="", attachments=()
    ) -> dict:
        """``mentions`` are the agents the message is for (``@dev``): kd-board sync wakes them (a person's
        message to an agent in the thread). ``client_key`` is the page's id of one send: the same author
        sending it again (a retry after a timeout) gets the message already written, nothing new."""
        with self._tx():
            card = self.card(card_id)
            return self.add_thread_message(
                card.thread,
                author=author,
                kind=kind,
                body=body,
                payload=payload,
                session=session,
                mentions=mentions,
                client_key=client_key,
                attachments=attachments,
            )

    def add_thread_message(
        self,
        thread: int,
        *,
        author,
        kind,
        body,
        payload=None,
        session="",
        mentions=(),
        client_key="",
        extra=None,
        via="",
        attachments=(),
    ) -> dict:
        """``add_message`` by thread: a card's thread or an agent's chat; ``via`` as ``_add_message``'s
        (``session``: a line of an agent's session, written by the session host, not one of the agent's writes).
        ``attachments`` (id, name, mime, size, sha256) are the message's files: their rows join the same write."""
        with self._tx():
            self.thread_info(thread)
            if client_key:
                row = self._conn.execute(
                    "SELECT seq FROM messages WHERE thread=? AND author=? AND json_extract(payload, '$.client')=?",
                    (thread, author, client_key),
                ).fetchone()
                if row is not None:
                    return self._message(thread, row[0])
                payload = {**(payload or {}), "client": client_key}
            event_extra = dict(extra or {})
            if mentions:
                event_extra["mentions"] = list(mentions)
            seq = self._add_message(
                thread, author, kind, body, payload, session, via=via, extra=event_extra or None
            )
            self._attach(thread, seq, attachments)
            return self._message(thread, seq)

    def _attach(self, thread: int, seq: int, attachments) -> None:
        """The rows of a message's files (id, name, mime, size, sha256), in the caller's transaction."""
        now = self._clock()
        for f in attachments:
            self._conn.execute(
                "INSERT INTO attachments (id, thread, seq, name, mime, size, sha256, created)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (f["id"], thread, seq, f["name"], f["mime"], f["size"], f["sha256"], now),
            )

    def _message(self, thread: int, seq: int) -> dict:
        return self.thread_messages(thread, seq - 1)[0]

    def thread_messages(
        self, thread: int, after: int = 0, *, before: Optional[int] = None, limit: Optional[int] = None
    ) -> List[dict]:
        """A thread in order (a card's or an agent chat's): the messages past ``after`` and below ``before``;
        with ``limit`` — the newest ``limit`` of them (a page of a long thread), still in order."""
        sql = "SELECT seq, author, kind, body, payload, created FROM messages WHERE thread=? AND seq>?"
        args = [thread, after]
        if before is not None:
            sql, args = sql + " AND seq<?", args + [before]
        if limit is None:
            rows = self._conn.execute(sql + " ORDER BY seq", args).fetchall()
        else:
            rows = self._conn.execute(sql + " ORDER BY seq DESC LIMIT ?", args + [limit]).fetchall()[::-1]
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    # --- a tool's output: masked by the host, apart from the thread, read by whoever controls the agent
    # ---
    def tool_output_put(self, thread: int, seq: int, *, session: str, command: str, output: Mapping) -> None:
        now = self._clock()
        edge = (datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ") - timedelta(days=OUTPUT_KEEP_DAYS)).strftime(
            "%Y-%m-%dT%H:%M:%SZ")
        with self._tx():
            self._conn.execute("DELETE FROM tool_outputs WHERE created < ?", (edge,))
            self._conn.execute(
                "INSERT OR REPLACE INTO tool_outputs (thread, seq, session, command, text, size, truncated, created)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (thread, seq, session, command, output["text"], output["size"], int(bool(output["truncated"])),
                 now),
            )

    def tool_output(self, thread: int, seq: int, *, session: Optional[str] = None) -> Optional[dict]:
        sql, args = "SELECT text, size, truncated FROM tool_outputs WHERE thread=? AND seq=?", [thread, seq]
        if session is not None:
            sql, args = sql + " AND session=?", args + [session]
        row = self._conn.execute(sql, args).fetchone()
        return None if row is None else {"text": row[0], "size": row[1], "truncated": bool(row[2])}

    # --- dev's pull requests: the latest of each as the forge executor published it ---------------------
    _PR_FIELDS = ("repo", "number", "card", "project", "url", "title", "head", "state", "files", "cage", "diff",
                  "truncated", "release")

    @staticmethod
    def _pr_row(row, diff=True) -> dict:
        data = dict(row)
        data["files"], data["cage"] = json.loads(data["files"]), json.loads(data["cage"])
        data["truncated"], data["release"] = bool(data["truncated"]), bool(data["release"])
        if not diff:
            data.pop("diff", None)
        return data

    def pr_put(self, row: Mapping, *, actor: str) -> dict:
        """Insert or replace one PR's row and say so (``pr.published``) on its card, in one write."""
        values = [row[k] for k in self._PR_FIELDS]
        values[8], values[9] = json.dumps(row["files"], ensure_ascii=False), json.dumps(row["cage"], ensure_ascii=False)
        values[11], values[12] = int(bool(row["truncated"])), int(bool(row["release"]))
        with self._tx():
            card = self.card(row["card"])
            self._conn.execute(
                f"INSERT OR REPLACE INTO prs ({', '.join(self._PR_FIELDS)}, updated) VALUES "
                f"({', '.join('?' * len(self._PR_FIELDS))}, ?)",
                (*values, self._clock()),
            )
            self._event(actor, "pr.published", card.id,
                        {"repo": row["repo"], "number": row["number"], "head": row["head"], "state": row["state"]},
                        card.thread)
        return self.pr(row["repo"], row["number"], diff=False)

    def pr(self, repo: str, number: int, *, diff: bool = True) -> Optional[dict]:
        found = self._conn.execute("SELECT * FROM prs WHERE repo=? AND number=?", (repo, number)).fetchone()
        return None if found is None else self._pr_row(found, diff)

    def prs(self, *, card: Optional[str] = None, state: Optional[str] = None) -> List[dict]:
        """PRs (without their diffs) of a card or in a state, oldest first."""
        clauses, args = [], []
        for name, value in (("card", card), ("state", state)):
            if value is not None:
                clauses.append(f"{name}=?")
                args.append(value)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(f"SELECT * FROM prs{where} ORDER BY updated, repo, number", args).fetchall()
        return [self._pr_row(r, diff=False) for r in rows]

    def turn_messages(self, thread: int, session: str, command: str, limit: int) -> List[dict]:
        """The session's messages of one turn (``payload.command``), in order: the first ``limit``."""
        rows = self._conn.execute(TURN_MESSAGES, (thread, session, command, limit)).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def turn_cost(self, command: str) -> float:
        row = self._conn.execute(TURN_COST, (command,)).fetchone()
        return round(row[0] or 0, 6)

    def turn_models(self, command: str) -> List[str]:
        """The models that served the turn, as its ledger rows name them: the costliest first, then by
        output, then by name; none for a turn that reported no usage."""
        return [row[0] for row in self._conn.execute(TURN_MODELS, (command,)).fetchall()]

    def thread_info(self, thread: int) -> dict:
        row = self._conn.execute(
            "SELECT id, kind, card, agent FROM threads WHERE id=?", (thread,)
        ).fetchone()
        if row is None:
            raise NotFound(f"thread {thread} not found")
        return {"id": row["id"], "kind": row["kind"], "card": row["card"] or "", "agent": row["agent"]}

    def agent_thread(self, agent: str, space: str = "chat") -> int:
        """The agent's own thread with people in ``space``: its chat, or "Monitor" (one each per agent,
        made on first use)."""
        kind = schema.AGENT_SPACES[space]
        with self._tx():
            row = self._conn.execute(
                "SELECT id FROM threads WHERE kind=? AND agent=?", (kind, agent)
            ).fetchone()
            if row is not None:
                return row[0]
            return self._conn.execute(
                "INSERT INTO threads (kind, card, agent) VALUES (?, NULL, ?)", (kind, agent)
            ).lastrowid

    def agent_space(self, thread: int) -> str:
        """The space of an agent's thread (``chat``, ``monitor``), '' for a card's or no thread."""
        row = self._conn.execute("SELECT kind FROM threads WHERE id=?", (thread,)).fetchone()
        return SPACE_OF.get(row[0], "") if row is not None else ""

    # --- commands and sessions: the outbox to agents and their runs on the session host -----
    _COMMAND_MOVES = {
        "queued": {"delivered", "cancelled", "failed", "queued"},
        "delivered": {"started", "queued", "failed", "cancelled", "done"},
        "started": {"done", "failed"},
    }
    _SESSION_FIELDS = frozenset(
        {"state", "slot", "runtime_session_id", "usage", "usage_total", "error", "started", "ended", "heartbeat",
         "model", "effort",  # model, effort: what the host started the process on
         "queue_position", "queued_since"}  # its place in the agent's queue for a slot, since when
    )

    def enqueue(self, spec: Mapping, *, strict: bool = False) -> dict:
        """One command for an agent: the same ``key`` again, or the same text for the same agent and thread
        while the first is not yet running, is the command already written (nothing new). The key in use with other
        content is a ``Conflict`` for a caller's own key (``strict``, the op boundary); the cabinet's own command
        goes under ``KEY_TAKEN_SUFFIX`` then, so the write it belongs to is never rolled back by it."""
        with self._tx():
            return self._enqueue_in_tx(spec, strict=strict)

    def _enqueue_in_tx(self, spec: Mapping, *, strict: bool = False) -> dict:
        command = self._enqueue_row(spec, strict)
        if all(c["id"] != command["id"] for c in self._tx_commands):
            self._tx_commands.append(command)
        return command

    def _enqueue_row(self, spec: Mapping, strict: bool) -> dict:
        if spec.get("kind") not in schema.COMMAND_KINDS:
            raise Invalid(f"unknown command kind {spec.get('kind')!r}")
        if not spec.get("key") or not spec.get("agent") or not spec.get("text"):
            raise Invalid("a command needs a key, an agent and a text")
        thread = self.thread_info(spec["thread"])
        card = spec.get("card") or thread["card"]
        row = self._conn.execute("SELECT id FROM commands WHERE key=?", (spec["key"],)).fetchone()
        if row is not None:
            known = self.command(row[0])
            if (known["agent"], known["thread"], known["card"], known["kind"], known["text"]) != (
                spec["agent"], thread["id"], card, spec["kind"], spec["text"]
            ):
                if strict or spec["key"].endswith(KEY_TAKEN_SUFFIX):
                    raise Conflict(f"command key {spec['key']} is in use with other content")
                print(f"cabinet: command key {spec['key']} is in use with other content (command {known['id']},"
                      f" @{known['agent']}): queued under {spec['key']}{KEY_TAKEN_SUFFIX}", file=sys.stderr)
                return self._enqueue_row({**spec, "key": spec["key"] + KEY_TAKEN_SUFFIX}, True)
            return known
        row = self._conn.execute(
            f"SELECT id FROM commands WHERE agent=? AND thread=? AND text=? AND state IN ('queued','delivered')"
            f" AND NOT {JOINED_SQL}",
            (spec["agent"], thread["id"], spec["text"]),
        ).fetchone()
        if row is not None:
            return self.command(row[0])
        now = self._clock()
        command_id = secrets.token_hex(16)
        next_try = spec.get("next_try") or now
        # the owner's or BOSS's defer of the card holds the agent's answers and continuations until its
        # time
        held = self._conn.execute(
            "SELECT until FROM defers WHERE card=? AND state='active' AND source!='agent'", (card,)
        ).fetchone() if card and spec["kind"] in ("answer", "continue") else None
        if held is not None and held[0] > next_try:
            next_try = held[0]
        self._conn.execute(
            "INSERT INTO commands (id, key, agent, thread, card, kind, text, initiator, event, state, next_try,"
            " created, updated, basis) VALUES (?,?,?,?,?,?,?,?,?,'queued',?,?,?,?)",
            (
                command_id,
                spec["key"],
                spec["agent"],
                thread["id"],
                card,
                spec["kind"],
                spec["text"],
                spec.get("initiator", ""),
                spec.get("event"),
                next_try,
                now,
                now,
                json.dumps(dict(spec.get("basis") or {}), ensure_ascii=False, sort_keys=True),
            ),
        )
        command = self.command(command_id)
        self._command_event(command, "command.queued", spec.get("initiator") or "system")
        return command

    def _command_event(self, command: Mapping, kind: str, actor: str, **extra):
        payload = {"command": command["id"], "agent": command["agent"], "kind": command["kind"]}
        if command["session"]:
            payload["session"] = command["session"]
        payload.update({k: v for k, v in extra.items() if v})
        self._event(actor, kind, command["card"], payload, command["thread"])

    def command(self, command_id: str) -> dict:
        row = self._conn.execute("SELECT * FROM commands WHERE id=?", (command_id,)).fetchone()
        if row is None:
            raise NotFound(f"command {command_id} not found")
        return _command_row(row)

    def commands(self, *, card=None, thread=None, agent=None, session=None, states=None, limit=100) -> List[dict]:
        """Commands newest first."""
        where, args = [], []
        for column, value in (("card", card), ("thread", thread), ("agent", agent), ("session", session)):
            if value is not None:
                where.append(f"{column}=?")
                args.append(value)
        if states:
            where.append("state IN (%s)" % ",".join("?" * len(states)))
            args.extend(states)
        sql = (
            "SELECT * FROM commands"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY created DESC, rowid DESC LIMIT ?"
        )
        return [_command_row(r) for r in self._conn.execute(sql, (*args, limit)).fetchall()]

    def meta(self, key: str) -> Optional[str]:
        """A value of the base's own ``meta`` (``pipeline_since``: when turns began to owe an outcome)."""
        row = self._conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return None if row is None else row[0]

    def lease_commands(self, now: str, until: str, limit: int, held: tuple = ()) -> List[dict]:
        """Queued commands that are due and not leased, oldest first, leased to the caller until ``until``; one of
        the ``held`` kinds waits while its agent has a command of the same thread at the host (delivered or
        started): it joins the next turn there (``commands.MERGEABLE``)."""
        hold = ""
        if held:
            hold = (f" AND NOT (c.kind IN ({','.join('?' * len(held))}) AND EXISTS (SELECT 1 FROM commands l"
                    " WHERE l.thread=c.thread AND l.agent=c.agent AND l.state IN ('delivered','started')))")
        with self._tx():
            ids = [
                r[0]
                for r in self._conn.execute(
                    "SELECT c.id FROM commands c WHERE c.state='queued' AND c.next_try<=? AND c.lease_until<?"
                    + hold + " ORDER BY c.created, c.rowid LIMIT ?",
                    (now, now, *held, limit),
                ).fetchall()
            ]
            for command_id in ids:
                self._conn.execute(
                    "UPDATE commands SET lease_until=?, attempts=attempts+1 WHERE id=?", (until, command_id)
                )
            return [self.command(i) for i in ids]

    def command_state(
        self,
        command_id: str,
        state: str,
        *,
        session=None,
        error="",
        reason="",
        next_try=None,
        actor="system",
    ) -> dict:
        """One move of a command with its event; ``queued`` again is a retry (no event unless it was delivered)."""
        with self._tx():
            before = self.command(command_id)
            if state not in self._COMMAND_MOVES.get(before["state"], ()):
                raise Invalid(f"command {command_id}: {before['state']} -> {state} is not a move")
            if (before["state"] == "delivered" and state == "done" and before["kind"] != "stop"
                    and not before["reason"].startswith(MERGED)):
                raise Invalid(f"command {command_id}: only a stop is done without a run")
            columns = {"state": state, "updated": self._clock(), "error": error, "reason": reason}
            if session is not None:
                columns["session"] = session
            if state == "queued":
                columns["lease_until"] = ""
                columns["next_try"] = next_try or self._clock()
            sets = ", ".join(f"{k}=?" for k in columns)
            self._conn.execute(f"UPDATE commands SET {sets} WHERE id=?", (*columns.values(), command_id))
            if before["state"] == "queued" and state != "queued":
                self._conn.execute("DELETE FROM state WHERE key=?", (JOIN_KEY + command_id,))
            after = self.command(command_id)
            kind = _COMMAND_EVENTS.get((before["state"], state))
            if kind:
                self._command_event(after, kind, actor, error=error, reason=reason)
            if before["state"] in ("delivered", "started") and state != "started":
                self._merged_follow(after, actor)
            return after

    def _merged_follow(self, command: Mapping, actor: str) -> None:
        """The commands delivered in ``command``'s turn (reason ``MERGED<id>``) follow it: done with its done turn,
        back to the queue otherwise — the turn never ran, or not to its end — so none is lost and none runs twice."""
        mark = MERGED + command["id"]
        rows = self._conn.execute(
            "SELECT id FROM commands WHERE thread=? AND agent=? AND state='delivered' AND reason=?",
            (command["thread"], command["agent"], mark),
        ).fetchall()
        for (merged_id,) in rows:
            if command["state"] == "done":
                self.command_state(merged_id, "done", reason=mark, actor=actor)
            else:
                self.command_state(merged_id, "queued", actor=actor)

    def session_for(self, thread: int, agent: str) -> dict:
        """The agent's session in the thread, made ``new`` on first use."""
        with self._tx():
            row = self._conn.execute(
                "SELECT id FROM sessions WHERE thread=? AND agent=?", (thread, agent)
            ).fetchone()
            if row is not None:
                return self.session(row[0])
            self.thread_info(thread)
            now = self._clock()
            session_id = secrets.token_hex(16)
            self._conn.execute(
                "INSERT INTO sessions (id, thread, agent, created, updated) VALUES (?,?,?,?,?)",
                (session_id, thread, agent, now, now),
            )
            return self.session(session_id)

    def session(self, session_id: str) -> dict:
        row = self._conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise NotFound(f"session {session_id} not found")
        return {**dict(row), "usage": json.loads(row["usage"]), "usage_total": json.loads(row["usage_total"])}

    def sessions(self, *, thread=None, agent=None, states=None) -> List[dict]:
        where, args = [], []
        for column, value in (("thread", thread), ("agent", agent)):
            if value is not None:
                where.append(f"{column}=?")
                args.append(value)
        if states:
            where.append("state IN (%s)" % ",".join("?" * len(states)))
            args.extend(states)
        sql = (
            "SELECT id FROM sessions"
            + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY updated DESC, rowid DESC"
        )
        return [self.session(r[0]) for r in self._conn.execute(sql, args).fetchall()]

    def session_update(self, session_id: str, *, actor="system", **fields) -> dict:
        """The host's view of a session; an event only when its state, error or place in the queue changes (not per
        heartbeat)."""
        extra = set(fields) - self._SESSION_FIELDS
        if extra:
            raise Invalid(f"unknown session field: {', '.join(sorted(extra))}")
        if "state" in fields and fields["state"] not in schema.SESSION_STATES:
            raise Invalid(f"unknown session state {fields['state']!r}")
        if fields.get("state", "queued") != "queued":  # a session out of the slot queue keeps no place in it
            fields = {"queue_position": 0, "queued_since": "", **fields}
        with self._tx():
            before = self.session(session_id)
            columns = dict(fields)
            for name in ("usage", "usage_total"):
                if name in columns:
                    columns[name] = json.dumps(columns[name], ensure_ascii=False, sort_keys=True)
            columns["updated"] = self._clock()
            sets = ", ".join(f"{k}=?" for k in columns)
            self._conn.execute(f"UPDATE sessions SET {sets} WHERE id=?", (*columns.values(), session_id))
            after = self.session(session_id)
            if (after["state"] != before["state"] or after["error"] != before["error"]
                    or after["queue_position"] != before["queue_position"]):
                payload = {
                    "session": session_id,
                    "agent": after["agent"],
                    "state": after["state"],
                    "was": before["state"],
                }
                if after["error"]:
                    payload["error"] = after["error"]
                if after["queue_position"]:
                    payload["position"] = after["queue_position"]
                card = self.thread_info(after["thread"])["card"]
                self._event(actor, "session.changed", card, payload, after["thread"])
            return after

    def files_size(self, author=None) -> int:
        """Bytes of every attachment, or of one author's (the message behind the attachment names them)."""
        if author is None:
            row = self._conn.execute("SELECT COALESCE(SUM(size), 0) FROM attachments").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(a.size), 0) FROM attachments a"
                " JOIN messages m ON m.thread = a.thread AND m.seq = a.seq WHERE m.author = ?",
                (author,),
            ).fetchone()
        return int(row[0])

    def add_file(self, card_id, *, author, file: Mapping) -> dict:
        """A ``file`` message with one attachment (id, name, mime, size, sha256) in one write."""
        payload = {"files": [{k: file[k] for k in ("id", "name", "mime", "size")}]}
        return self.add_message(
            card_id, author=author, kind="file", body=file["name"], payload=payload, attachments=[file]
        )

    def attachment(self, attachment_id) -> dict:
        """A file's row with its thread (``card`` — "" in an agent's chat, ``agent``, ``thread_kind``) and the
        ``author`` of its message."""
        row = self._conn.execute(
            "SELECT a.*, COALESCE(t.card, '') AS card, t.agent, t.kind AS thread_kind, m.author"
            " FROM attachments a JOIN threads t ON t.id = a.thread"
            " JOIN messages m ON m.thread = a.thread AND m.seq = a.seq WHERE a.id=?",
            (attachment_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"{attachment_id}: no such file")
        return dict(row)

    def delete_file(self, attachment_id, *, actor) -> dict:
        """The file's row goes (its bytes no longer count); its message keeps the name, marked ``deleted``, and
        ``message.changed`` tells the open pages to read it again. A message from before the change (the file's
        fields in the payload itself) takes the ``files`` form here."""
        with self._tx():
            row = self.attachment(attachment_id)
            message = self._message(row["thread"], row["seq"])
            payload = dict(message["payload"])
            files = payload.pop("files", None)
            if files is None:
                files = [{k: payload.pop(k) for k in ("id", "name", "mime", "size") if k in payload}]
            payload["files"] = [
                {"id": f["id"], "name": f.get("name", ""), "deleted": True} if f.get("id") == attachment_id else f
                for f in files
            ]
            self._conn.execute("DELETE FROM attachments WHERE id=?", (attachment_id,))
            self._conn.execute(
                "UPDATE messages SET payload=? WHERE thread=? AND seq=?",
                (json.dumps(payload, ensure_ascii=False), row["thread"], row["seq"]),
            )
            event = {"seq": row["seq"], "file": attachment_id}
            if row["thread_kind"] in SPACE_OF:
                event["agent"] = row["agent"]  # who may see it: the agent chat has no card (event_filter)
                if SPACE_OF[row["thread_kind"]] != "chat":
                    event["space"] = SPACE_OF[row["thread_kind"]]
            self._event(actor, "message.changed", row["card"], event, row["thread"])
            return self._message(row["thread"], row["seq"])

    def import_card(
        self, record: Mapping, messages: Iterable[Mapping], *, actor="system:import"
    ) -> CardRecord:
        """A card of the old board with its id, number, revision and dates; its comments open the thread."""
        match = CARD_ID.fullmatch(str(record.get("id", "")))
        if (
            not match
            or match.group(1) != self.prefix
            or int(match.group(2)) != record.get("n")
        ):
            raise Invalid(f"import: bad card id {record.get('id')!r}")
        if record.get("status") not in schema.STATUSES:
            raise Invalid(f"import: unknown status {record.get('status')!r}")
        with self._tx():
            if self._conn.execute(
                "SELECT 1 FROM cards WHERE id=?", (record["id"],)
            ).fetchone():
                raise Conflict(f"{record['id']}: already imported")
            self._insert_card(
                record["id"],
                record["n"],
                record,
                record["title"],
                record["status"],
                revision=max(1, int(record.get("revision", 1))),
                created=record["created"],
                updated=record["updated"],
            )
            thread = self._thread_id(record["id"])
            for message in messages:
                seq = self._add_message(
                    thread, actor_of(message["author"]), "text", message["body"], {}, "", notify=False
                )
                self._conn.execute(
                    "UPDATE messages SET created=? WHERE thread=? AND seq=?",
                    (message["created"], thread, seq),
                )
            nxt = int(
                self._conn.execute(
                    "SELECT value FROM meta WHERE key='next_card'"
                ).fetchone()[0]
            )
            self._conn.execute(
                "UPDATE meta SET value=? WHERE key='next_card'",
                (str(max(nxt, record["n"] + 1)),),
            )
            self._event(
                actor,
                "card.imported",
                record["id"],
                {
                    "title": record["title"],
                    "status": record["status"],
                    "assignee": record.get("assignee", ""),
                    "project": record["project"],
                },
                thread,
            )
            return self.card(record["id"])

    def seed_projects(self, rows: Iterable[Mapping[str, str]]):
        """A new base's projects (``{id, name, route}``; the dev cabinet, tests): one already there stays as it is.
        The projects table is the board's only source — nothing mirrors a file into it."""
        with self._tx():
            self._conn.executemany(
                "INSERT OR IGNORE INTO projects (id, name, route) VALUES (?,?,?)",
                [(r["id"], r["name"], r["route"]) for r in rows],
            )

    def set_state(self, key, value):
        with self._tx():
            self._put_state(key, value)

    def _put_state(self, key, value):
        """``set_state`` inside the caller's transaction."""
        self._conn.execute(
            "INSERT INTO state VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
            " updated=excluded.updated",
            (
                key,
                json.dumps(value, ensure_ascii=False, sort_keys=True),
                self._clock(),
            ),
        )

    # --- push: subscriptions and the notification queue; no events, the queue is not the board ---
    def notification(self, notification_id) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT * FROM notifications WHERE id=?", (notification_id,)
        ).fetchone()
        return None if row is None else dict(row)

    def notifications(self, user) -> List[dict]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT * FROM notifications WHERE user=? ORDER BY id", (user,)
            ).fetchall()
        ]

    def subscriptions(self, user) -> List[dict]:
        return [
            dict(r)
            for r in self._conn.execute(
                "SELECT id, endpoint, device, created, last_ok FROM push_subscriptions WHERE user=? ORDER BY id",
                (user,),
            ).fetchall()
        ]

    def subscribe(self, user, endpoint, p256dh, auth, device, *, actor):
        """A browser's push subscription; the same endpoint again (another sign-in) is replaced."""
        with self._tx():
            self._drop_deliveries(endpoint)
            self._conn.execute(
                "DELETE FROM push_subscriptions WHERE endpoint=?", (endpoint,)
            )
            self._conn.execute(
                "INSERT INTO push_subscriptions (user, endpoint, p256dh, auth, device, created) VALUES (?,?,?,?,?,?)",
                (user, endpoint, p256dh, auth, device, self._clock()),
            )
            self._event(actor, "push.subscribed", "", {"user": user, "device": device})

    def _drop_deliveries(self, endpoint: str):
        """A subscription that goes takes its delivery records along."""
        self._conn.execute(
            "DELETE FROM push_deliveries WHERE subscription IN (SELECT id FROM push_subscriptions WHERE endpoint=?)",
            (endpoint,))

    def unsubscribe(self, user, endpoint, *, actor) -> bool:
        with self._tx():
            self._conn.execute(
                "DELETE FROM push_deliveries WHERE subscription IN "
                "(SELECT id FROM push_subscriptions WHERE user=? AND endpoint=?)", (user, endpoint))
            cursor = self._conn.execute(
                "DELETE FROM push_subscriptions WHERE user=? AND endpoint=?",
                (user, endpoint),
            )
            if cursor.rowcount:
                self._event(actor, "push.unsubscribed", "", {"user": user})
        return cursor.rowcount == 1

    def take_notifications(self, now: str, retry_after: str, limit: int) -> List[dict]:
        """Due rows for each subscription of their person still owed them (per device; a subscription
        made after a row was due never gets it); a device's failed delivery waits until ``retry_after``. An agent's
        chat reply the person has read meanwhile, or whose chat they have open now, is settled unsent here."""
        with self._tx():
            rows = self._conn.execute(
                "SELECT n.id, n.kind, n.title, n.body, n.tag, n.url, n.user, n.reply, n.thread, n.seq, "
                "s.endpoint, s.p256dh, s.auth "
                "FROM notifications n JOIN push_subscriptions s ON s.user = n.user "
                "LEFT JOIN push_deliveries d ON d.notification = n.id AND d.subscription = s.id "
                "WHERE n.sent = '' AND n.due <= ? AND n.due >= s.created AND COALESCE(d.sent, '') = '' "
                "AND COALESCE(d.attempts, 0) < ? AND (COALESCE(d.tried, '') = '' OR d.tried <= ?) "
                "ORDER BY n.due, n.id, s.id LIMIT ?",
                (now, PUSH_ATTEMPTS, retry_after, limit),
            ).fetchall()
            out, settled = [], {}
            for row in map(dict, rows):
                item = {k: v for k, v in row.items() if k not in ("reply", "thread", "seq")}
                if row["reply"]:
                    if row["id"] not in settled:
                        settled[row["id"]] = self._chat_reply_settled(row, now)
                    if settled[row["id"]]:
                        continue
                out.append(item)
            return out

    def _chat_reply_settled(self, row: Mapping, now: str) -> bool:
        """A chat reply that needs no push any more: read, or its chat open in front of the person."""
        if self.chat_cursor(row["user"], row["thread"]) >= row["seq"]:
            reason = "read"
        elif self.chat_present(row["user"], row["thread"], now):
            reason = "present"
        else:
            return False
        self._conn.execute("UPDATE notifications SET sent=?, result=? WHERE id=?", (now, reason, row["id"]))
        return True

    def notifications_sent(self, results: Iterable[Mapping]):
        """The push gateway's answers per device: sent, a dead subscription dropped (404/410), or one more failed
        attempt of that device. A row is sent once no device it is owed to waits for it."""
        now = self._clock()
        with self._tx():
            for r in results:
                sub = self._conn.execute(
                    "SELECT id FROM push_subscriptions WHERE endpoint=?", (r["endpoint"],)
                ).fetchone()
                if sub is not None:
                    self._conn.execute(
                        "INSERT INTO push_deliveries (notification, subscription, sent, result, attempts, tried) "
                        "VALUES (?,?,?,?,?,?) ON CONFLICT(notification, subscription) DO UPDATE SET "
                        "sent=excluded.sent, result=excluded.result, attempts=attempts + excluded.attempts, "
                        "tried=excluded.tried",
                        (r["id"], sub["id"], now if r["ok"] else "", str(r["status"]), 0 if r["ok"] else 1, now),
                    )
                if r["ok"]:
                    self._conn.execute(
                        "UPDATE push_subscriptions SET last_ok=? WHERE endpoint=?",
                        (now, r["endpoint"]),
                    )
                else:
                    if r["status"] in (404, 410):
                        self._drop_deliveries(r["endpoint"])
                        self._conn.execute(
                            "DELETE FROM push_subscriptions WHERE endpoint=?",
                            (r["endpoint"],),
                        )
                    self._conn.execute(
                        "UPDATE notifications SET attempts = attempts + 1, result=?, tried=? WHERE id=? AND sent=''",
                        (str(r["status"]), now, r["id"]),
                    )
                self._settle(r["id"], now, str(r["status"]) if r["ok"] else "")

    def _settle(self, notification: int, now: str, ok_status: str):
        """The row is sent once every subscription it is owed to has it or gave up; ``ok_status`` — this answer's."""
        if ok_status:
            self._conn.execute("UPDATE notifications SET result=? WHERE id=? AND sent=''", (ok_status, notification))
        owed = self._conn.execute(
            "SELECT COUNT(*) FROM notifications n JOIN push_subscriptions s ON s.user = n.user "
            "LEFT JOIN push_deliveries d ON d.notification = n.id AND d.subscription = s.id "
            "WHERE n.id=? AND n.due >= s.created AND COALESCE(d.sent, '') = '' AND COALESCE(d.attempts, 0) < ?",
            (notification, PUSH_ATTEMPTS),
        ).fetchone()[0]
        delivered = self._conn.execute(
            "SELECT COUNT(*) FROM push_deliveries WHERE notification=? AND sent != ''", (notification,)
        ).fetchone()[0]
        if not owed and delivered:
            self._conn.execute("UPDATE notifications SET sent=? WHERE id=? AND sent=''", (now, notification))

    def push_last(self, user: str, endpoint: str) -> Optional[dict]:
        """This device's last delivery attempt: ``{result, at, sent}`` (the settings' "last attempt")."""
        row = self._conn.execute(
            "SELECT d.result, d.tried, d.sent FROM push_deliveries d JOIN push_subscriptions s ON s.id = d.subscription "
            "WHERE s.user=? AND s.endpoint=? ORDER BY d.tried DESC, d.notification DESC LIMIT 1",
            (user, endpoint),
        ).fetchone()
        return None if row is None else {"result": row["result"], "at": row["tried"], "sent": bool(row["sent"])}

    # --- presence in an agent chat: a visible, focused chat page holds a short lease -------------
    def chat_presence(self, user: str, thread: int, device: str, until: Optional[str]) -> None:
        """The page ``device`` shows the chat until ``until`` (renewed while it does); None — it stopped."""
        with self._tx():
            if until is None:
                self._conn.execute(
                    "DELETE FROM chat_presence WHERE user=? AND thread=? AND device=?", (user, thread, device))
            else:
                self._conn.execute(
                    "INSERT INTO chat_presence VALUES (?,?,?,?) ON CONFLICT(user, thread, device) DO UPDATE SET "
                    "until=excluded.until", (user, thread, device, until))

    def chat_present(self, user: str, thread: int, now: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM chat_presence WHERE user=? AND thread=? AND until > ? LIMIT 1", (user, thread, now)
        ).fetchone()
        return row is not None

    def chat_presence_clear(self, user: str) -> None:
        """Sign-out: the person is in front of none of their chats."""
        with self._tx():
            self._conn.execute("DELETE FROM chat_presence WHERE user=?", (user,))

    def add_ack(self, event_id: str, action: str, next_check: int, reason: str) -> bool:
        """True when the ack is new; a replayed one changes nothing. ``action`` is what BOSS did about the event,
        ``next_check`` the minutes until the watch looks again (C03)."""
        with self._tx():
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO attention_acks (event, at, action, next_check, reason) VALUES (?,?,?,?,?)",
                (event_id, self._clock(), action, next_check, reason),
            )
            return cursor.rowcount == 1

    def pending_acks(self) -> List[str]:
        return [
            r[0]
            for r in self._conn.execute(
                "SELECT event FROM attention_acks ORDER BY at, event"
            ).fetchall()
        ]

    def take_acks(self) -> List[dict]:
        """The acks since the last take, oldest first, as ``{event, action, next_check, reason}``; then none."""
        with self._tx():
            acks = [
                {"event": r[0], "action": r[1] or "", "next_check": r[2], "reason": r[3] or ""}
                for r in self._conn.execute(
                    "SELECT event, action, next_check, reason FROM attention_acks ORDER BY at, event"
                ).fetchall()
            ]
            self._conn.execute("DELETE FROM attention_acks")
        return acks

    _ACK_TABLES = {"assist": "assists", "decision": "decisions", "wait": "waits", "job": "jobs", "card": "cards"}

    def ack_target_exists(self, kind: str, ref: str) -> bool:
        """An ack's reference (``assist|decision|wait|job|card``) names a row of the cabinet."""
        table = self._ACK_TABLES[kind]
        return self._conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (ref,)).fetchone() is not None

    def boss_edit_at(self, card: str, revision: int, since: str = "") -> bool:
        """BOSS itself changed ``card`` to exactly ``revision``, at ``since`` or later (an ack's ``edit:<revision>``:
        a write about the event, not any earlier one)."""
        return self._conn.execute(
            "SELECT 1 FROM events WHERE kind='card.changed' AND card=? AND actor='agent:boss'"
            " AND CAST(json_extract(payload, '$.revision') AS INTEGER) = ? AND ts >= ? LIMIT 1",
            (card, revision, since),
        ).fetchone() is not None

    # --- helpers -------------------------------------------------------------------------------
    def _thread_id(self, card_id: str) -> int:
        row = self._conn.execute(
            "SELECT id FROM threads WHERE card=?", (card_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"{card_id}: card not found")
        return row[0]

    def _record(self, row: sqlite3.Row) -> CardRecord:
        deps = tuple(
            r[0]
            for r in self._conn.execute(
                "SELECT depends_on FROM card_deps WHERE card=? ORDER BY depends_on",
                (row["id"],),
            ).fetchall()
        )
        servers = tuple(
            r[0]
            for r in self._conn.execute(
                "SELECT server FROM card_servers WHERE card=? ORDER BY server",
                (row["id"],),
            ).fetchall()
        )
        thread = self._conn.execute(
            "SELECT id FROM threads WHERE card=?", (row["id"],)
        ).fetchone()[0]
        return CardRecord(
            id=row["id"],
            n=row["n"],
            project=row["project"],
            title=row["title"],
            status=row["status"],
            revision=row["revision"],
            created_by=row["created_by"],
            created=row["created"],
            updated=row["updated"],
            thread=thread,
            description=row["description"],
            acceptance=tuple(
                Acceptance(a["text"], bool(a["checked"]))
                for a in json.loads(row["acceptance"])
            ),
            dod=row["dod"],
            plan=row["plan"],
            notes=row["notes"],
            summary=row["summary"],
            assignee=row["assignee"],
            labels=tuple(json.loads(row["labels"])),
            priority=row["priority"],
            due=row["due"],
            parent=row["parent"],
            dependencies=deps,
            servers=servers,
            outcome=row["outcome"],
            canonical=row["canonical"],
            operations=tuple(json.loads(row["operations"])) if row["operations"] else (),
            stages=tuple(json.loads(row["stages"])) if row["stages"] else (),
            model=row["model"],
            effort=row["effort"],
            council=_council.load(row["council"]),
        )


# The event of each move of a command; a retry of a queued command writes none (only its next try changes).
_COMMAND_EVENTS = {
    ("queued", "delivered"): "command.delivered",
    ("queued", "cancelled"): "command.cancelled",
    ("queued", "failed"): "command.failed",
    ("delivered", "queued"): "command.queued",
    ("delivered", "started"): "run.started",
    ("delivered", "failed"): "command.failed",
    ("delivered", "cancelled"): "command.cancelled",
    ("delivered", "done"): "command.done",
    ("started", "done"): "run.finished",
    ("started", "failed"): "run.failed",
}


def _migrate(conn, current: int):
    """Every migration after ``current`` and the new version, in one transaction (executescript would
    commit a transaction opened with execute, so the script carries its own BEGIN and COMMIT)."""
    steps = "".join(
        schema.MIGRATIONS[v] for v in range(current + 1, schema.SCHEMA_VERSION + 1)
    )
    try:
        conn.executescript(
            "BEGIN IMMEDIATE;\n"
            + steps
            + f"\nUPDATE meta SET value='{schema.SCHEMA_VERSION}' WHERE key='schema_version';\nCOMMIT;"
        )
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise


def _card_prefix(conn, prefix: str, aliases: Tuple[str, ...]):
    """The cards' prefix of the base (meta ``card_prefix``; a base before it has its cards' or this host's). A base
    whose cards carry an alias of this host (naming.json card_aliases) takes the host's prefix in every text value
    of every table, the number kept, in one transaction (an earlier prefix <n> -> KD-<n>); another one is refused."""
    row = conn.execute("SELECT value FROM meta WHERE key='card_prefix'").fetchone()
    if row is not None:
        stored = row[0]
    else:
        first = conn.execute("SELECT id FROM cards ORDER BY n LIMIT 1").fetchone()
        stored = first[0].rsplit("-", 1)[0] if first else prefix
    if stored == prefix:
        if row is None:
            conn.execute("INSERT INTO meta VALUES ('card_prefix', ?)", (prefix,))
        return
    if stored not in aliases:
        conn.close()
        raise StoreError(f"cabinet.db cards are {stored}-<n>, this host's {prefix}-<n>: name {stored} in card_aliases"
                         " of naming.json to move them, or keep cards " + stored)
    old = re.compile(r"(?<![A-Za-z0-9])" + re.escape(stored) + r"(?=-[1-9][0-9]{0,5}(?![0-9]))")
    conn.create_function("kd_card_prefix", 1, lambda v: old.sub(prefix, v) if isinstance(v, str) else v,
                         deterministic=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("PRAGMA defer_foreign_keys=ON")  # cards.id and the rows that reference it move together
        # a brief version's hash is of its own text: one that matched its text matches the moved text too
        consistent = {rowid for rowid, (text_sha, sha) in _brief_hashes(conn).items() if text_sha == sha}
        for table in tables:
            for column in [c[1] for c in conn.execute(f'PRAGMA table_info("{table}")')]:
                conn.execute(f'UPDATE "{table}" SET "{column}"=kd_card_prefix("{column}") '
                             f'WHERE typeof("{column}")=\'text\' AND instr("{column}", ?)>0', (stored + "-",))
        for rowid, (text_sha, sha) in _brief_hashes(conn).items():
            if rowid in consistent and text_sha != sha:
                conn.execute("UPDATE brief_versions SET scope_sha=? WHERE rowid=?", (text_sha, rowid))
        conn.execute("INSERT OR REPLACE INTO meta VALUES ('card_prefix', ?)", (prefix,))
        conn.execute("COMMIT")
    except sqlite3.Error:
        if conn.in_transaction:
            conn.execute("ROLLBACK")
        conn.close()
        raise


def _brief_hashes(conn) -> Dict[int, Tuple[str, str]]:
    """Every brief version: the hash of its text now and the one it carries."""
    from kd.cabinet import briefs

    out = {}
    for row in conn.execute("SELECT rowid, description, acceptance, operations, stages, scope_sha FROM brief_versions"):
        rowid, description, acceptance, operations, stages, sha = tuple(row)
        text = briefs._scope_of(description, json.loads(acceptance), json.loads(operations) if operations else (),
                                json.loads(stages) if stages else ())
        out[rowid] = (briefs.scope_sha(text), sha)
    return out


def _default_roles(conn):
    """The starting roles; a role the owner changed keeps its set."""
    from kd.cabinet import perms

    conn.executemany(
        "INSERT OR IGNORE INTO roles (id, perms) VALUES (?,?)",
        [(role, json.dumps(list(p))) for role, p in perms.DEFAULT_ROLES.items()],
    )


class _Tx:
    """BEGIN IMMEDIATE ... COMMIT, or ROLLBACK when the block raises: rows and event land together.

    A block inside another joins it as a savepoint: the outer block commits once, and an inner block that
    raises undoes only its own writes (the outer one may catch the error and go on)."""

    def __init__(self, store):
        self._store = store
        self._name = ""
        self._mark = 0

    def __enter__(self):
        store = self._store
        if store._depth:
            self._name = f"kd_tx{store._depth}"
            self._mark = len(store._tx_commands)
            store._conn.execute(f"SAVEPOINT {self._name}")
        else:
            store._tx_commands = []
            store._conn.execute("BEGIN IMMEDIATE")
        store._depth += 1

    def __exit__(self, exc_type, exc, tb):
        store = self._store
        store._depth -= 1
        if self._name:
            if exc_type:
                store._conn.execute(f"ROLLBACK TO {self._name}")
                del store._tx_commands[self._mark :]
            store._conn.execute(f"RELEASE {self._name}")
        else:
            store._conn.execute("ROLLBACK" if exc_type else "COMMIT")
            store.last_commands = [] if exc_type else store._tx_commands
            store._tx_commands = []
