"""The registry of problems: opened / worse / closed. Pure logic, no SSH."""
import json
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from .model import LEVELS, Event

CLOSE_AFTER = 3
REPEAT_WINDOW_DAYS = 7
HISTORY_MAX = 20  # history records per problem; the first (the opening) always stays
RECENT_DAYS = 14  # a closed problem is kept in recent for two weeks (its page is in the cabinet)
VALUE_MAX = 200


def split_key(p: dict):
    """(server, check, object) of a problem. The check comes from the source: it may contain ":" (gate:wg)."""
    key = p["key"]
    server = key.split(":", 1)[0]
    try:
        check = p["source"][1]
    except (KeyError, IndexError, TypeError):
        check = None
    if check and key.startswith(f"{server}:{check}:"):
        return server, check, key[len(server) + len(check) + 2:]
    server, check, subject = key.split(":", 2)
    return server, check, subject


def schedule_name(check: str, subject: str = "") -> str:
    """The check's name in every_min/last_run for a problem's source: the posture and the sec:* sections come from the
    snapshot "sec", a gateway refusal gate:X — from the same command X, "the check does not work" (health, the object is
    the check's name) — from that check itself."""
    if check == "health":
        check = subject
    if check.startswith("gate:"):
        check = check[5:]
    return "sec" if check == "posture" or check.startswith("sec:") else check


def is_sticky(check: str) -> bool:
    """sec:* and logins: the source reports the object as "bad" until it is accepted — only accept closes them."""
    return check == "logins" or check.startswith("sec:")


def _rechecked(p: dict, now: datetime) -> bool:
    """There is a "fixed" mark and the poll started no earlier than it. A poll that was running while the mark was made
    may have caught the state before the fix — it does not count. A mark without a readable time counts (not to keep
    it forever)."""
    fixed = p.get("fixed")
    if not isinstance(fixed, dict):
        return False
    try:
        return datetime.fromisoformat(fixed["at"]) <= now
    except (KeyError, TypeError, ValueError):
        return True


def _new_state(today: date) -> dict:
    return {
        "problems": {}, "closed": {}, "next_id": 1, "outbox": [], "wakes": [],
        "last_morning": today.isoformat(), "unreachable": {},
        "monitoring_ts": None, "last_run": None, "sec": {}, "recent": {},
    }


def recent_key(p: dict) -> str:
    """The key of a closed problem in recent: its number, and without a number (the cabinet has not issued it yet) —
    the key and the opening time; a number that comes later replaces it (cabinet.apply_numbers)."""
    return p.get("id") or f"~{p['key']}@{p['opened_at']}"


class Registry:
    def __init__(self, state: dict, close_after: int = CLOSE_AFTER,
                 repeat_window_days: int = REPEAT_WINDOW_DAYS, every_min=None, local_ids: bool = True):
        self.state = state
        self.close_after = close_after
        self.repeat_window_days = repeat_window_days
        self.every_min = every_min or {}
        # The cabinet issues the number P-n — the tick and the CLI create the registry with local_ids=False, and a new
        # problem lives without a number ("" in id) until the cabinet issues it. The own counter next_id is only for
        # tests and old snapshots.
        self.local_ids = local_ids
        # this run's history records — what the monitor will send to the cabinet (cabinet.items_from)
        self.journal = []

    def _closes_after(self, check: str) -> int:
        """The "ok" runs in a row needed to close: close_after is ~15 minutes in ticks, while a check on a schedule
        (hourly or daily) covers more with a single run already. health — by ticks."""
        return 1 if check != "health" and schedule_name(check) in self.every_min else self.close_after

    @classmethod
    def load(cls, path, today: date, **kw) -> "Registry":
        path = Path(path)
        fresh = _new_state(today)
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return cls(fresh, **kw)
        except OSError:
            raw = None
        state = None
        if raw is not None:
            try:
                state = json.loads(raw)
            except ValueError:
                state = None
        if not isinstance(state, dict) or not isinstance(state.get("problems"), dict):
            stamp = datetime.now().strftime("%Y%m%d%H%M%S")
            try:
                os.replace(path, path.with_name(path.name + ".bad-" + stamp))
            except OSError:
                pass
            print(f"kd-monitor: реестр повреждён, начат заново: {path}", file=sys.stderr)
            return cls(fresh, **kw)
        for k, v in fresh.items():
            state.setdefault(k, v)
        if not isinstance(state["recent"], dict):
            state["recent"] = {}
        return cls(state, **kw)

    def save(self, path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)  # untrusted addresses, security snapshots
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(str(tmp), 0o600)
        os.replace(tmp, path)

    def _purge_closed(self, now: datetime) -> None:
        edge = now - timedelta(days=self.repeat_window_days)
        closed = self.state["closed"]
        for key in list(closed):
            keep = [t for t in closed[key] if datetime.fromisoformat(t) > edge]
            if keep:
                closed[key] = keep
            else:
                del closed[key]

    def _purge_recent(self, now: datetime) -> None:
        edge = now - timedelta(days=RECENT_DAYS)
        recent = self.state["recent"]
        for pid in list(recent):
            try:
                keep = datetime.fromisoformat(recent[pid]["closed_at"]) > edge
            except (KeyError, TypeError, ValueError):
                keep = False
            if not keep:
                del recent[pid]

    def _note(self, p: dict, at: str, kind: str, level: str, value) -> None:
        """A record of a problem's history: open | worse | better | close | accept | disabled | fixed | fix_failed;
        over HISTORY_MAX — the earliest after the opening drop out. The same record goes to journal (for the cabinet)."""
        h = p.setdefault("history", [])
        entry = {"at": at, "kind": kind, "level": level, "value": str(value)[:VALUE_MAX]}
        h.append(entry)
        if len(h) > HISTORY_MAX:
            del h[1:len(h) - HISTORY_MAX + 1]
        self.journal.append((dict(entry), p))

    def _retire(self, key: str, at: str, kind: str, level: str, value) -> dict:
        """The problem leaves problems: a history record, the time — into closed (repeats), a snapshot with closed_at — into recent."""
        p = self.state["problems"].pop(key)
        p.pop("fixed", None)  # a closed one is not "being checked"; the mark's card stays in the history
        self._note(p, at, kind, level, value)
        self.state["closed"].setdefault(key, []).append(at)
        self.state["recent"][recent_key(p)] = dict(p, closed_at=at)
        return p

    def observe(self, results, now: datetime) -> list:
        st = self.state
        problems = st["problems"]
        at = now.isoformat()
        events = []
        self._purge_closed(now)
        self._purge_recent(now)
        for res in results:
            if not res.ran:
                continue
            source = [res.server, res.check]
            bad = {}
            for o in res.observations:
                if o.level not in ("warn", "crit"):
                    continue
                prev = bad.get(o.key)
                if prev is None or LEVELS[o.level] > LEVELS[prev.level]:
                    bad[o.key] = o
            for key, o in bad.items():
                p = problems.get(key)
                if p is None:
                    repeats = len(st["closed"].get(key, []))
                    p = {
                        "id": f"P-{st['next_id']}" if self.local_ids else "", "key": key, "source": source,
                        "level": o.level, "max_level": o.level, "value": o.value,
                        "text": o.text, "opened_at": at, "last_seen": at,
                        "ok_streak": 0, "acked_until": None, "repeats": repeats,
                        "confirmation_count": 1, "confirmation_started_at": at,
                    }
                    self._note(p, at, "open", o.level, o.value)
                    if self.local_ids:
                        st["next_id"] += 1
                    problems[key] = p
                    events.append(self._event("open", p, o, at))
                    continue
                old = p["level"]
                if now > datetime.fromisoformat(p["last_seen"]):
                    p["confirmation_count"] = p.get("confirmation_count", 0) + 1
                    p.setdefault("confirmation_started_at", at)
                p.update(ok_streak=0, level=o.level, value=o.value, text=o.text, last_seen=at)
                if _rechecked(p, now):  # the recheck after "fixed" still sees the problem
                    del p["fixed"]
                    self._note(p, at, "fix_failed", o.level, o.value)
                    events.append(self._event("fix_failed", p, o, at))
                if o.level != old:
                    self._note(p, at, "worse" if LEVELS[o.level] > LEVELS.get(old, 0) else "better", o.level, o.value)
                if LEVELS[o.level] > LEVELS[p["max_level"]]:
                    p["max_level"] = o.level
                    events.append(self._event("worse", p, o, at))
            need = self._closes_after(res.check)
            for key in [k for k, p in problems.items() if p["source"] == source and k not in bad]:
                p = problems[key]
                p["ok_streak"] += 1
                if p["ok_streak"] >= need or _rechecked(p, now):  # after "fixed" one is enough
                    events.append(self._event("close", p, None, at))
                    good = next((o for o in res.observations if o.key == key and o.level == "ok"), None)
                    self._retire(key, at, "close", "ok", good.value if good else "")
        for e in events:
            st["outbox"].append(e._asdict())
        return events

    @staticmethod
    def _event(kind, p, o, at) -> Event:
        server, check, subject = split_key(p)
        return Event(kind, p["id"], p["key"], p["level"], server, check, subject,
                     p["value"], p["text"], p["opened_at"], at, p["repeats"])

    def ack(self, problem_id: str, until) -> bool:
        for p in self.state["problems"].values():
            if problem_id and p["id"] == problem_id:
                p["acked_until"] = "close" if until is None else until.isoformat()
                return True
        return False

    def fix(self, problem_id: str, card: str, at: str):
        """The mark "fixed, awaiting the monitor's check": the next tick rechecks the source off schedule, one "ok"
        closes it. -> the problem or None (no such open one). Sticky ones (sec:*, logins) and oom — ValueError."""
        p = next((p for p in self.state["problems"].values() if problem_id and p["id"] == problem_id), None)
        if p is None:
            return None
        check = split_key(p)[1]
        if is_sticky(check):
            raise ValueError(f"{problem_id} ({check}) закрывается только через kd-monitor accept по слову владельца — "
                             "отметка «исправлено» к ней не применяется")
        if check == "oom":  # the window is a day: the next tick after a correct fix still sees the OOM
            raise ValueError(f"{problem_id} (oom): OOM считается за 24 ч — закроется сама; "
                             "отметка «исправлено» к ней не применяется")
        p["fixed"] = {"at": at, "card": card}
        self._note(p, at, "fixed", p["level"], card)
        return p

    def close_now(self, problem_id: str, at: str, how: str = "accept"):
        """Close without an event (the owner accepted the object into the baseline; how="revoke" — access revoked): from
        problems into closed and recent, its events out of outbox."""
        p = next((p for p in self.state["problems"].values() if problem_id and p["id"] == problem_id), None)
        return None if p is None else self.close_key(p["key"], at, how)

    def close_key(self, key: str, at: str, how: str = "accept"):
        """close_now by key: the problem may not have a number yet."""
        p = self.state["problems"].get(key)
        if p is None:
            return None
        self._retire(key, at, how, p["level"], p["value"])
        self.state["outbox"] = [e for e in self.state["outbox"] if e.get("key") != key]
        return p

    def close_disabled(self, off, at: str) -> list:
        """Close the problems of checks the owner disabled: off(the source's server, check, object) -> whether it is
        disabled. No event for the sysadmin (the owner disabled it himself); in the history and the cabinet —
        "disabled". -> the keys."""
        gone = []
        for key, p in list(self.state["problems"].items()):
            server, check, subject = split_key(p)
            source = p.get("source")
            if isinstance(source, list) and source:
                server = source[0]  # the nodes' heartbeat is read on the database server: it is disabled where the check runs
            if off(server, check, subject):
                self._retire(key, at, "disabled", p["level"], p["value"])
                gone.append(key)
        if gone:
            self.state["outbox"] = [e for e in self.state["outbox"] if e.get("key") not in gone]
        return gone

    def open_problems(self) -> list:
        return sorted(self.state["problems"].values(), key=id_order)


def id_order(p: dict):
    """Ordered by number; problems without a number go last, by opening time."""
    pid = p.get("id") or ""
    tail = pid[2:] if pid.startswith("P-") else ""
    return (0, int(tail), "") if tail.isdigit() else (1, 0, p.get("opened_at") or "")


def format_list(problems) -> str:
    if not problems:
        return "открытых проблем нет"
    rows = [["ID", "уровень", "сервер", "проверка", "объект", "значение", "с", "принято", "статус"]]
    for p in problems:
        server, check, subject = split_key(p)
        fixed = p.get("fixed")
        status = f"проверяется {fixed.get('card', '?')}" if isinstance(fixed, dict) else "-"
        rows.append([p["id"] or "без номера", p["level"], server, check, subject, str(p["value"]),
                     p["opened_at"][:16].replace("T", " "), p["acked_until"] or "-", status])
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in rows)
