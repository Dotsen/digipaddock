"""Pure attention rules for ``kd-board watch`` (BOSS spec sections 3.2 and 4.1, ADR 0003).

``evaluate`` turns one observation of the board and the trusted worker-activity journal into
normalized events and sanitary diagnostics. No LLM, no Telegram, no writes: the attention
automaton (B04) owns state, delivery and escalation bookkeeping. no panel rules — the
agents run in the cabinet's sessions, whose states are the cabinet's (spec §15).

Ages come only from evidence that a coordinator comment or a technical sync cannot move:
worker activity records (ADR 0003) for In Progress and Review, the card's ``created`` for Inbox.
Anything else is *unknown*: the rule stays silent for that card and a diagnostic says why, so a
missing journal never looks like 24 idle hours.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import re
from typing import Any, Collection, Iterable, Mapping, Optional, Tuple

from kd import activity, ids
from kd.specializations import LABEL_PREFIX

SCHEMA_VERSION = 1
STATUSES = ("Inbox", "To Do", "In Progress", "Review", "Done", "Blocked", "Cancelled")
TERMINAL = ("Done", "Cancelled")  # closed without the work is no live card either
COORDINATOR = "@boss"
OWNER = "@owner"
COORDINATORS = frozenset({COORDINATOR, OWNER})

TIMED_RULES = frozenset(
    {
        "in_progress_idle",
        "review_waiting",
        "inbox_waiting",
        "due_tomorrow",
        "due_today",
        "overdue",
    }
)
_RULE_FIELDS = {
    "blocked": {},
    "dependency_done": {},
    "in_progress_idle": {"afterHours": (1, 168), "escalateAfterHours": (1, 168)},
    "review_waiting": {"afterHours": (1, 168)},
    "inbox_waiting": {"afterHours": (1, 168)},
    "due_tomorrow": {},
    "due_today": {},
    "overdue": {},
    "route_mismatch": {"statuses": None},
    "coordinator_assigned": {},
    "spec_mismatch": {},
    "brief_ready": {},
    "inbox_unassigned": {},
}
RULES = tuple(_RULE_FIELDS)
_TOP = frozenset(
    {
        "schemaVersion",
        "utcOffsetMinutes",
        "clockSkewSeconds",
        "trustedWorkers",
        "rules",
    }
)
_CARD_FIELDS = frozenset(
    {
        "revision",
        "status",
        "assignees",
        "project",
        "dependencies",
        "created",
        "due",
        "labels",
    }
)
# a card put off until a time (``deferred``, the cabinet's UTC stamp) keeps the idle and waiting rules
# silent
# till then; ``woken`` — when its last defer woke it, from which in_progress_idle counts. Either may be absent ('').
_OPTIONAL_FIELDS = frozenset({"deferred", "woken"})
# the brief's council has not ended (True; absent otherwise) — brief_ready stays silent until it has
_FLAGS = frozenset({"in_council"})
QUIET_DEFERRED = frozenset({"blocked", "in_progress_idle", "review_waiting", "inbox_waiting"})
_CARD = ids.CARD
_ASSIGNEE = re.compile(r"@[a-z][a-z0-9_-]{0,31}")
_PROJECT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_CREATED = re.compile(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d):\d\dZ")  # the cabinet's UTC stamp
_DUE = re.compile(r"(\d{4}-\d\d-\d\d)(?: \d\d:\d\d)?")


class ConfigError(ValueError):
    """watch.json is not a valid rule set; the detector must not read state with it."""


@dataclass(frozen=True)
class Config:
    offset: timedelta
    skew: timedelta
    trusted_workers: frozenset
    rules: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class Event:
    """``(rule, subject, revision)`` is the automaton key; ``notify`` and ``level`` are hints.

    ``subject`` is a card ID; ``revision`` its change counter.
    """

    rule: str
    subject: str
    revision: str
    notify: str  # "boss" | "assignee" | "owner"
    level: str = "attention"  # "attention" | "escalate"


@dataclass(frozen=True)
class Diagnostic:
    """A sanitary data-quality note: a fixed kind and a validated card ID.

    ``rule`` names the one rule the note leaves unevaluated for the subject; empty means
    every rule of the subject (an invalid card) or, for ``clock``/``journal``, a class.
    """

    kind: str
    subject: str
    rule: str = ""


@dataclass(frozen=True)
class Report:
    events: Tuple[Event, ...]
    diagnostics: Tuple[Diagnostic, ...]


def load_config(data: Any) -> Config:
    """Validate watch.json strictly; any unknown key or out-of-range value is refused."""
    _exact(data, _TOP, "watch.json")
    if (
        data["schemaVersion"] != SCHEMA_VERSION
        or type(data["schemaVersion"]) is not int
    ):
        raise ConfigError("unsupported watch.json schema")
    offset = _int(data["utcOffsetMinutes"], -720, 840, "utcOffsetMinutes")
    skew = _int(data["clockSkewSeconds"], 0, 3600, "clockSkewSeconds")
    workers = data["trustedWorkers"]
    if not isinstance(workers, list) or not all(
        isinstance(w, str) and _ASSIGNEE.fullmatch(w) and w not in COORDINATORS
        for w in workers
    ):
        raise ConfigError("trustedWorkers must list worker assignees")
    _exact(data["rules"], frozenset(RULES), "rules")
    rules = {}
    for name, fields in _RULE_FIELDS.items():
        rule = data["rules"][name]
        _exact(rule, frozenset({"enabled", *fields}), f"rule {name}")
        if type(rule["enabled"]) is not bool:
            raise ConfigError(f"rule {name}: enabled must be true or false")
        for key, bounds in fields.items():
            if bounds is not None:
                _int(rule[key], *bounds, f"{name}.{key}")
        rules[name] = dict(rule)
    statuses = rules["route_mismatch"]["statuses"]
    if (
        not isinstance(statuses, list)
        or not statuses
        or any(s not in STATUSES or s == "Inbox" or s in TERMINAL for s in statuses)
    ):
        raise ConfigError("route_mismatch.statuses must be working statuses")
    return Config(
        timedelta(minutes=offset),
        timedelta(seconds=skew),
        frozenset(workers),
        rules,
    )


def evaluate(
    config: Config,
    cards: Mapping[str, Mapping[str, Any]],
    routes: Mapping[str, str],
    activity_records: Optional[Iterable[Any]],
    now: datetime,
    last_tick: Optional[datetime] = None,
    specs: Optional[Mapping[str, Collection[str]]] = None,
    rerouted: Optional[Mapping[str, str]] = None,
) -> Report:
    """One detector pass. ``activity_records`` is ``None`` when no trusted journal exists;
    ``specs`` maps ``@agent`` to its installed specializations (``None``: the spec rule is silent);
    ``rerouted`` maps a project to the moment its route last changed (the cabinet's stamp)."""
    _aware(now, "now")
    events, diagnostics = [], []
    timed = True
    if last_tick is not None:
        _aware(last_tick, "last_tick")
        if now < last_tick - config.skew:
            diagnostics.append(Diagnostic("time_jump", "clock"))
            timed = False
    records = _records(activity_records, diagnostics)
    valid = {}
    for card_id in sorted(cards, key=_card_order):
        card = _card(card_id, cards[card_id])
        if card is None:
            diagnostics.append(Diagnostic("invalid_card", _safe_card(card_id)))
        else:
            valid[card_id] = card
    ctx = _Context(config, valid, routes, records, now, events, diagnostics, specs, dict(rerouted or {}))
    for card_id, card in valid.items():
        for rule in _CARD_RULES:
            if card["deferred"] and rule in QUIET_DEFERRED:
                continue  # put off until a time: due_* and overdue still speak
            if config.rules[rule]["enabled"] and (timed or rule not in TIMED_RULES):
                _CARD_RULES[rule](ctx, card_id, card)
    return Report(tuple(events), tuple(diagnostics))


@dataclass
class _Context:
    config: Config
    cards: Mapping[str, Mapping[str, Any]]
    routes: Mapping[str, str]
    records: Optional[list]
    now: datetime
    events: list
    diagnostics: list
    specs: Optional[Mapping[str, Collection[str]]] = None
    rerouted: Mapping[str, str] = None

    def emit(self, rule, subject, revision, notify, level="attention"):
        self.events.append(Event(rule, subject, revision, notify, level))

    def note(self, kind, subject, rule):
        self.diagnostics.append(Diagnostic(kind, subject, rule))

    def hours(self, rule, key="afterHours"):
        return timedelta(hours=self.config.rules[rule][key])

    def today(self) -> date:
        return (self.now.astimezone(timezone.utc) + self.config.offset).date()


def _blocked(ctx, card_id, card):
    if card["status"] == "Blocked":
        ctx.emit("blocked", card_id, card["revision"], "boss")


def _dependency_done(ctx, card_id, card):
    deps = card["dependencies"]
    if card["status"] != "Blocked" or not deps:
        return
    if any(
        dep not in ctx.cards for dep in deps
    ):  # missing or invalid: not proof of "not Done"
        ctx.note("dependency_unknown", card_id, "dependency_done")
    elif any(ctx.cards[dep]["status"] == "Cancelled" for dep in deps):
        # a dependency closed without the work never gets done: BOSS reassigns it to the canonical card
        ctx.note("dependency_cancelled", card_id, "dependency_done")
    elif all(ctx.cards[dep]["status"] == "Done" for dep in deps):
        ctx.emit("dependency_done", card_id, card["revision"], "assignee")


def _in_progress_idle(ctx, card_id, card):
    if card["status"] != "In Progress":
        return
    last = _worker_time(ctx, card_id, card, "in_progress_idle", None)
    if last is None:
        return
    woke = _stamp(card["woken"])
    if woke is not None and woke <= ctx.now + ctx.config.skew:
        last = max(last, woke)  # a card woken from its defer idles from that moment
    age = ctx.now - last
    after = ctx.hours("in_progress_idle")
    if age >= after + ctx.hours("in_progress_idle", "escalateAfterHours"):
        ctx.emit("in_progress_idle", card_id, card["revision"], "owner", "escalate")
    elif age >= after:
        ctx.emit("in_progress_idle", card_id, card["revision"], "assignee")


def _review_waiting(ctx, card_id, card):
    if card["status"] != "Review":
        return
    last = _worker_time(ctx, card_id, card, "review_waiting", "review_submission")
    if last is not None and ctx.now - last >= ctx.hours("review_waiting"):
        ctx.emit("review_waiting", card_id, card["revision"], "owner")


def _inbox_waiting(ctx, card_id, card):
    if card["status"] != "Inbox":
        return
    match = _CREATED.fullmatch(card["created"])
    created = _parse(match and f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M")
    if created is None:
        ctx.note("invalid_date", card_id, "inbox_waiting")
    elif created > ctx.now + ctx.config.skew:
        ctx.note("future_date", card_id, "inbox_waiting")
    elif ctx.now - created >= ctx.hours("inbox_waiting"):
        ctx.emit("inbox_waiting", card_id, card["revision"], "owner")


def _due(rule, days_left, notify):
    def check(ctx, card_id, card):
        if card["status"] in TERMINAL or not card["due"]:
            return
        due = _due_day(card["due"])
        if due is None:
            ctx.note("invalid_date", card_id, rule)
            return
        left = (due - ctx.today()).days
        if (left < 0) if days_left is None else (left == days_left):
            ctx.emit(rule, card_id, card["revision"], notify)

    return check


def _route_mismatch(ctx, card_id, card):
    """A To Do card off its project's route. A card the owner holds is no routing mistake (an agent's
    task keeps the owner's part in its own project), unless the project itself is gone; nor is a card assigned
    before its project's route changed (a new route counts for new cards, an open one keeps its assignee)."""
    if card["status"] not in ctx.config.rules["route_mismatch"]["statuses"]:
        return
    expected = ctx.routes.get(card["project"])
    if expected is not None and card["assignees"] == (OWNER,):
        return
    since = (ctx.rerouted or {}).get(card["project"], "")
    if expected is not None and card["assignees"] and since and card["created"] < since:
        return
    if expected is None or card["assignees"] != (expected,):
        ctx.emit("route_mismatch", card_id, card["revision"], "boss")


def _coordinator_assigned(ctx, card_id, card):
    """A card on the coordinator is its work item (spec 4.4); sync never wakes it directly."""
    if card["status"] not in TERMINAL and COORDINATOR in card["assignees"]:
        ctx.emit("coordinator_assigned", card_id, card["revision"], "boss")


def _spec_mismatch(ctx, card_id, card):
    """A ``spec:`` label the assignee cannot follow (spec 5.8.5): not in its installed set,
    or two labels. Without a registry the rule says nothing rather than guessing."""
    if ctx.specs is None or card["status"] in TERMINAL or len(card["assignees"]) != 1:
        return
    names = [label[len(LABEL_PREFIX):] for label in card["labels"] if label.startswith(LABEL_PREFIX)]
    if not names:
        return
    allowed = ctx.specs.get(card["assignees"][0], ())
    if len(names) > 1 or names[0] not in allowed:
        ctx.emit("spec_mismatch", card_id, card["revision"], "boss")


BRIEF_READY = "brief-ready"  # the analyst's label: the brief is written, propose the start


def _brief_ready(ctx, card_id, card):
    """An Inbox card whose brief is done, on one worker: the coordinator proposes the start (spec 5.8.7)."""
    if card["status"] != "Inbox" or BRIEF_READY not in card["labels"] or card["in_council"]:
        return
    if len(card["assignees"]) != 1 or card["assignees"][0] in COORDINATORS:
        return
    ctx.emit("brief_ready", card_id, card["revision"], "boss")


def _inbox_unassigned(ctx, card_id, card):
    """An Inbox card nobody holds: the coordinator routes it (routing.md) — by the project's route or, when it
    cannot be started as written, to the analyst. ``inbox_waiting`` stays the owner's 24-hour reminder."""
    if card["status"] == "Inbox" and not card["assignees"]:
        ctx.emit("inbox_unassigned", card_id, card["revision"], "boss")


_CARD_RULES = {
    "blocked": _blocked,
    "dependency_done": _dependency_done,
    "in_progress_idle": _in_progress_idle,
    "review_waiting": _review_waiting,
    "inbox_waiting": _inbox_waiting,
    "due_tomorrow": _due("due_tomorrow", 1, "assignee"),
    "due_today": _due("due_today", 0, "assignee"),
    "overdue": _due("overdue", None, "owner"),
    "route_mismatch": _route_mismatch,
    "coordinator_assigned": _coordinator_assigned,
    "spec_mismatch": _spec_mismatch,
    "brief_ready": _brief_ready,
    "inbox_unassigned": _inbox_unassigned,
}


def _worker_time(ctx, card_id, card, rule, action):
    """Latest trusted action of the card's current single worker, or None (unknown + diagnostic for ``rule``)."""
    assignees = card["assignees"]
    if len(assignees) != 1 or assignees[0] in COORDINATORS:
        ctx.note("activity_unknown", card_id, rule)
        return None
    worker = assignees[0]
    if ctx.records is None or worker not in ctx.config.trusted_workers:
        ctx.note("activity_unknown", card_id, rule)
        return None
    latest = None
    for record in ctx.records:
        if (
            record["cardId"] == card_id
            and record["worker"] == worker
            and (action is None or record["action"] == action)
        ):
            latest = record["at"] if latest is None else max(latest, record["at"])
    if latest is None:
        ctx.note("activity_unknown", card_id, rule)
    elif latest > ctx.now + ctx.config.skew:
        ctx.note("future_date", card_id, rule)
        return None
    return latest


def _records(raw, diagnostics) -> Optional[list]:
    """All records valid, or None: one bad record makes the whole journal untrusted."""
    if raw is None:
        return None
    try:
        return [activity.parse_record(record) for record in raw]
    except (ValueError, TypeError):
        diagnostics.append(Diagnostic("activity_invalid", "journal"))
        return None


def _card(card_id, raw) -> Optional[dict]:
    if not isinstance(card_id, str) or not _CARD.fullmatch(card_id):
        return None
    known = _CARD_FIELDS | _OPTIONAL_FIELDS | _FLAGS
    if not isinstance(raw, Mapping) or not _CARD_FIELDS <= set(raw) or set(raw) - known:
        return None
    flags = {key: raw.get(key, False) for key in _FLAGS}
    if not all(type(v) is bool for v in flags.values()):
        return None
    timed = {key: raw.get(key, "") for key in _OPTIONAL_FIELDS}
    if not all(isinstance(v, str) and (v == "" or _CREATED.fullmatch(v)) for v in timed.values()):
        return None
    assignees, deps = raw["assignees"], raw["dependencies"]
    ok = (
        type(raw["revision"]) is int  # the cabinet's change counter of the card
        and raw["revision"] >= 1
        and raw["status"] in STATUSES
        and isinstance(raw["project"], str)
        and (raw["project"] == "" or _PROJECT.fullmatch(raw["project"]))
        and isinstance(assignees, (list, tuple))
        and all(isinstance(a, str) and _ASSIGNEE.fullmatch(a) for a in assignees)
        and isinstance(deps, (list, tuple))
        and all(isinstance(d, str) and _CARD.fullmatch(d) for d in deps)
        and isinstance(raw["created"], str)
        and isinstance(raw["due"], str)
        and isinstance(raw["labels"], (list, tuple))
        and all(isinstance(label, str) for label in raw["labels"])
    )
    if not ok:
        return None
    # The automaton's key stays a string: (rule, card, revision).
    return {**raw, **timed, **flags, "revision": str(raw["revision"]), "assignees": tuple(assignees),
            "dependencies": tuple(deps), "labels": tuple(raw["labels"])}


def _stamp(value: str) -> Optional[datetime]:
    """The cabinet's UTC stamp as a moment, or None for ''."""
    return _parse(value, "%Y-%m-%dT%H:%M:%SZ") if value else None


def _due_day(value: str) -> Optional[date]:
    match = _DUE.fullmatch(value)
    parsed = _parse(match and match.group(1), "%Y-%m-%d")
    return parsed.date() if parsed else None


def _parse(value, fmt) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _aware(moment, name):
    if not isinstance(moment, datetime) or moment.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")


def _exact(value, keys, label):
    if not isinstance(value, dict) or set(value) != keys:
        raise ConfigError(f"{label}: expected exactly {sorted(keys)}")


def _int(value, low, high, label) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ConfigError(f"{label} must be an integer in [{low}, {high}]")
    return value


def _card_order(card_id):
    return (len(card_id), card_id) if isinstance(card_id, str) else (0, "")


def _safe_card(card_id) -> str:
    return card_id if isinstance(card_id, str) and _CARD.fullmatch(card_id) else "card"

