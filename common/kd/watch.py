"""One tick of ``kd-board watch`` (BOSS spec 4.1, 4.4; B05). Root, no LLM.

the cabinet's card snapshot + trusted activity journal
  -> ``attention_rules.evaluate`` -> ``attention.observe`` (the automaton)
  -> acknowledgements the coordinator gave through the cabinet (``attention.acks.take``)
  -> delivery: pending events wake the coordinator with one fixed signal; without a
     coordinator one sanitary notice in the cabinet (``notice.watch.jams``); escalations
     always reach the owner once (``notice.watch.owner``).
  -> the coordinator's directives: ``deliver`` (``kd-nudge deliver`` as the base user)
     retries pending ones, expires old ones and wakes the coordinator about unseen answers.

An event is ``dispatched`` only after a successful wake or a published notice; a busy
coordinator leaves it pending for the next tick, a failed wake counts an attempt. Card text
never leaves this module: a notice line carries the card ID and the rule. After the tick the
open events (no card text) are published to the cabinet for ``kd-boss-board attention-list``
. no Pult and no panels — the agents run in the cabinet's sessions.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any, Callable, Dict, List, Mapping, Optional

from kd import activity_journal, agents, attention, attention_rules, board, clock, ids, specializations
from kd.manifest import ManifestError

COORDINATOR = "boss"
# Fixed text: no card data reaches the coordinator's session through the dispatcher.
SIGNAL = "[attention] new board events: run kd-boss-board attention-list"
DIGEST_SIGNAL = "[digest] 09:00: send the owner the morning board report"
NOTICE_ITEMS = 30  # lines of one notice; the rest is counted
NOTICE_CHARS = 1500  # the notice's ``lines`` parameter (the cabinet takes up to 2000 per value)
PUBLISHED = ("id", "state", "level", "rule", "subject", "revision", "created")  # what attention-list shows
RECOVERED = ("warn", "notice.watch.recovered", {})
_TIME = "%Y-%m-%dT%H:%M:%SZ"
RULE_TEXT = {
    "blocked": "заблокирована",
    "dependency_done": "зависимость закрыта — можно продолжать",
    "in_progress_idle": "в работе без движения",
    "review_waiting": "ждёт вашего ревью",
    "inbox_waiting": "ждёт решения в Inbox",
    "due_tomorrow": "срок завтра",
    "due_today": "срок сегодня",
    "overdue": "срок прошёл",
    "route_mismatch": "исполнитель не по маршруту",
    "coordinator_assigned": "назначена координатору",
    "spec_mismatch": "метка spec: не из реестра исполнителя",
    "brief_ready": "ТЗ готово: предложить старт",
    "inbox_unassigned": "в Inbox без исполнителя",
}


@dataclass(frozen=True)
class Paths:
    config: str = "/opt/digipaddock/common/coordination/watch.json"
    registry: str = agents.REGISTRY
    attention: str = attention.STATE
    state: str = "/var/lib/digipaddock/board/watch-state.json"
    activity: str = activity_journal.JOURNAL
    repo: str = str(specializations.REPO_ROOT)  # the deployed tree: manifests and the specialization registry
    require_root_owner: bool = True


def tick(
    *,
    now: datetime,
    paths: Paths,
    wake: Callable[..., str],
    notify: Callable[[str, str, dict], bool],
    activity_owner_uid: Optional[int],
    cards: Callable[[], Optional[Dict[str, Any]]],
    projects: Callable[[], Optional[List[dict]]],
    acks: Callable[[], List[str]],
    publish: Callable[[List[dict]], bool],
    err=None,
    deliver: Optional[Callable[[], int]] = None,
) -> int:
    """0 — the tick ran and every due notice was published; 1 — config, board or a notice failed.

    ``notify(level, key, params)`` posts a notice in the cabinet (True — published), ``cards`` is the cabinet's snapshot (``card_snapshot``; None — the cabinet did not answer),
    ``projects`` the cabinet's projects (``{id, route, routed, …}``; None — the cabinet did not answer),
    ``acks`` the coordinator's acknowledgements since the last tick (``{event, action, next_check, reason}``), ``publish`` hands
    the open events to the cabinet. ``activity_owner_uid`` is the base user: the activity
    journal is trusted only as its regular file.
    """
    err = err or sys.stderr
    try:
        with open(paths.config, encoding="utf-8") as handle:
            config = attention_rules.load_config(json.load(handle))
    except (OSError, ValueError) as exc:
        print(
            f"kd-board watch: конфигурация не прочитана ({exc.__class__.__name__})",
            file=err,
        )
        return 1
    specs, degraded = None, False
    try:
        registry = specializations.load_registry(os.path.join(paths.repo, "common", "specializations"),
                                                 require_root_owner=paths.require_root_owner)
        specs = specializations.agent_specs(registry, paths.repo)
    except (specializations.SpecError, ManifestError, OSError) as exc:
        # The other rules keep watching; spec_mismatch says nothing without a registry.
        print(f"kd-board watch: реестр специализаций не прочитан ({exc.__class__.__name__})", file=err)
        degraded = True
    cards, projects = cards(), projects()
    if cards is None or projects is None:
        print("kd-board watch: доска не прочитана — тик пропущен", file=err)
        return 1
    routes = {p["id"]: p["route"] for p in projects}
    rerouted = {p["id"]: p.get("routed", "") for p in projects}
    previous = _load_state(paths.state)
    last_tick = _parse(previous.get("tick"))
    report = attention_rules.evaluate(
        config,
        cards,
        routes,
        activity_journal.read(paths.activity, activity_owner_uid),
        now,
        last_tick,
        specs=specs,
        rerouted=rerouted,
    )
    notes = []
    try:
        attention.observe(paths.attention, report, now)
    except attention.CorruptState:
        attention.recover(paths.attention, report, now)
        notes.append(RECOVERED)
    attention.apply_acks(paths.attention, acks(), now)
    attention.recheck(paths.attention, now)  # C03: a due check whose condition holds goes back to BOSS now
    ok = dispatch(paths, wake, notify, notes, now, err)
    if not publish([{k: e.get(k) for k in PUBLISHED} for e in attention.listing(paths.attention)]):
        print("kd-board watch: открытые события не переданы в кабинет — повтор на следующем тике", file=err)
        ok = False
    _save_state(paths.state, {"schemaVersion": 1, "tick": clock.iso(now)})
    if deliver is not None and deliver() != 0:
        # The board events went out; a directive stays pending for the next tick.
        print("kd-board watch: поручения BOSS не доставлены — повтор на следующем тике", file=err)
    return 0 if ok and not degraded else 1


def digest_signal(paths: Paths, wake, err=None) -> int:
    """09:00: ask the coordinator for its morning report with one fixed line (spec 3.4).

    Always 0: the cabinet's 09:00 digest does not depend on this wake or on what the
    coordinator reports back.
    """
    err = err or sys.stderr
    if _has_coordinator(paths.registry):
        result = wake(COORDINATOR, DIGEST_SIGNAL)
        if result != "sent":
            print(
                f"kd-board digest-signal: координатор не разбужен ({result})", file=err
            )
    return 0


def dispatch(paths: Paths, wake, notify, notes: List[tuple], now: datetime, err) -> bool:
    """Pending events wake the coordinator, or without one go out as one notice; ``notes`` are
    notices (level, key, params) of the tick itself; escalations reach the owner once. A coordinator
    off the session host (disabled, released: the wake answers ``absent``) counts as none."""
    pending = [e for e in attention.listing(paths.attention) if e["state"] == "pending"]
    to_notice = pending
    if _has_coordinator(paths.registry):
        to_notice = []
        ids = [e["id"] for e in pending]
        if ids:
            # a tick is the coordinator's work log, its "Monitor"; the owner's chat gets what it writes there
            result = wake(COORDINATOR, SIGNAL, space="monitor")
            if result == "sent":
                attention.mark_dispatched(paths.attention, ids, now)
            elif result == "error":
                attention.mark_failed(paths.attention, ids, now)
            elif result == "absent":
                to_notice = pending

    def post(level: str, key: str, params: dict) -> bool:
        if notify is not None and notify(level, key, params):
            return True
        print(f"kd-board watch: уведомление не опубликовано в кабинете: {key}", file=err)
        return False

    ok = all([post(*note) for note in notes])
    if to_notice:
        ids = [e["id"] for e in to_notice]
        if post("warn", "notice.watch.jams", {"lines": _lines(to_notice)}):
            attention.mark_dispatched(paths.attention, ids, now)
        else:
            attention.mark_failed(paths.attention, ids, now)
            ok = False
    owner = attention.owner_due(paths.attention)
    if owner:
        if post("warn", "notice.watch.owner", {"lines": _lines(owner)}):
            attention.mark_owner_notified(
                paths.attention, [e["id"] for e in owner], now
            )
        else:
            ok = False
    return ok


def card_snapshot(records: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """{card ID: raw card for attention_rules} from the cabinet's card records (``card.list``).

    The cabinet keeps one record per ID and one assignee per card; ``attention_rules`` still
    validates every field, so a record it cannot read stays an *unknown* card, never a vanished one.
    """
    return {r["id"]: _raw_card(r) for r in records}


def _raw_card(record: Mapping[str, Any]) -> dict:
    assignee = record.get("assignee")
    return {
        "revision": record.get("revision"),
        "status": record.get("status"),
        "assignees": [assignee] if assignee else [],
        "project": record.get("project"),
        "dependencies": list(record.get("dependencies") or []),
        "created": record.get("created"),
        "due": record.get("due"),
        "labels": list(record.get("labels") or []),
        # put off until (UTC, '' — not put off) and when its last defer woke it ('' — never)
        "deferred": (record.get("defer") or {}).get("until", ""),
        "woken": record.get("woken") or "",
        # the brief's council has not ended — brief_ready waits for it; said only of such a card
        **({"in_council": True} if "brief" in (record.get("councils") or ()) else {}),
    }


def _has_coordinator(registry: str) -> bool:
    try:
        return COORDINATOR in agents._load(registry)
    except ValueError:
        return False


def _lines(events: List[dict]) -> str:
    """A line "• KD-n — rule" per event, up to NOTICE_ITEMS and NOTICE_CHARS; the rest is "… and N more"."""
    room = NOTICE_CHARS - len(f"\n… и ещё {len(events)}")  # the tail's count never exceeds len(events)
    out: List[str] = []
    for e in events[:NOTICE_ITEMS]:
        line = f"• {_subject(e['subject'])} — {RULE_TEXT.get(e['rule'], 'затор')}"
        if len("\n".join(out + [line])) > room:
            break
        out.append(line)
    if len(events) > len(out):
        out.append(f"… и ещё {len(events) - len(out)}")
    return "\n".join(out)


def _subject(subject: str) -> str:
    """A card ID as is; anything else plain and cut (the cabinet shows the notice as text)."""
    if ids.CARD.fullmatch(subject):
        return subject
    return board._plain(subject)[:32]


def _load_state(path: str) -> dict:
    try:
        state = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return state if isinstance(state, dict) and state.get("schemaVersion") == 1 else {}


def _save_state(path: str, state: dict) -> None:
    """The watch's own state (the last tick), replaced atomically."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".state-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, ensure_ascii=False, sort_keys=True)
        os.chmod(tmp, 0o640)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _parse(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, _TIME).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
