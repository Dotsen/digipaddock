"""Owner-approved delegation of one goal.

The owner's click on the BOSS page (``kd.boss_promote``, proposal kind ``delegate``) writes a grant as the approval
service's user into ``GRANTS``; BOSS's executor (the board owner) only reads it and trusts a regular file of that
user alone. ``create_child`` runs under one lock beside the children journal: it checks the grant (active, not
expired, the project and its current route) and the quotas (every recorded child counts, an intent without a card
too; a child is active until Done), records the intent, creates the card in Inbox, records its ID, promotes it to
To Do (the cabinet's ``boss.promote`` with the grant id) and records the start, each board step through
``kd.boss_exec`` (routing, rate limits, audit). A repeat with
the same title finishes what is missing and never creates a second card; a card moved outside the grant meanwhile
is not started. A child belongs to its grant only by this journal: card text or labels prove nothing. Past
``COMPACT_AT`` the journal is rewritten with one line per child, so it never fills up with retries.

``announce`` (from ``kd-nudge deliver``: right after the owner's click through ``digipaddock-board-grants.path``, and at every
watch tick) tells the coordinator about a new active grant with one fixed line.
"""

import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, timezone
from typing import List, Mapping, Optional

from kd import atomic_file, boss_board, boss_exec, ids, naming

try:
    import fcntl
    import pwd
except ImportError:  # Windows: tests only
    fcntl = pwd = None

GRANTS = "/var/lib/digipaddock/boss-promotions/grants"
CHILDREN = "/var/log/digipaddock-board/delegation.jsonl"
ANNOUNCED = "/var/log/digipaddock-board/delegation-announced.json"
LIMITS = "/opt/digipaddock/common/coordination/delegation.json"
ACTIVE, REVOKED = "active", "revoked"
PHASES = ("intent", "created", "started", "dropped")
MAX_FILE = 16 * 1024
MAX_JOURNAL = 4 * 1024 * 1024
COMPACT_AT = 256 * 1024
_ID = re.compile(r"[0-9a-f]{32}")
_TASK = ids.CARD
_PROJECT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_ASSIGNEE = re.compile(r"@[a-z][a-z0-9_-]{0,31}")
_OPEN = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
Failure = boss_exec.Failure


class GrantError(Exception):
    """No such grant, or its file cannot be trusted or read."""


def owner_uid(getpwnam=None) -> int:
    """The cabinet's service uid, the only writer of grants; -1 (no grant is trusted) on a host without it."""
    lookup = getpwnam or (pwd.getpwnam if pwd is not None else None)
    try:
        return lookup(naming.service_user("cabinet")).pw_uid if lookup else -1
    except KeyError:
        return -1


@dataclass(frozen=True)
class Limits:
    max_children: int
    max_active: int
    days: int


def load_limits(path: str = LIMITS) -> Limits:
    with open(path, encoding="utf-8") as handle:
        raw = json.load(handle)
    if not isinstance(raw, dict) or set(raw) != {"maxChildren", "maxActive", "days"}:
        raise ValueError("delegation limits need exactly maxChildren, maxActive and days")
    limits = Limits(raw["maxChildren"], raw["maxActive"], raw["days"])
    for value, top in ((limits.max_children, 20), (limits.max_active, limits.max_children), (limits.days, 30)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
            raise ValueError("delegation limits are out of range")
    return limits


@dataclass(frozen=True)
class Grant:
    id: str
    goal: str
    projects: Mapping[str, str]
    max_children: int
    max_active: int
    created: float
    expires: float
    state: str
    approved_by: str
    revoked: float = 0.0
    revoked_by: str = ""

    def active(self, now: float) -> bool:
        return self.state == ACTIVE and now < self.expires


def write_grant(root: str, grant: Grant) -> None:
    """Only the approval service writes grants (its directory); 0640 lets the board owner read them by group."""
    if not _ID.fullmatch(grant.id):
        raise GrantError("invalid grant id")
    data = json.dumps(asdict(grant), ensure_ascii=False, sort_keys=True).encode("utf-8")
    atomic_file.replace(os.path.join(root, f"{grant.id}.json"), data, 0o640)


def load_grant(root: str, grant_id: str, owner_uid: Optional[int] = None) -> Grant:
    if not isinstance(grant_id, str) or not _ID.fullmatch(grant_id):
        raise GrantError("no such grant")
    try:
        fd = os.open(os.path.join(root, f"{grant_id}.json"), _OPEN)
    except FileNotFoundError as exc:
        raise GrantError("no such grant") from exc
    except OSError as exc:
        raise GrantError("grant is not readable") from exc
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or (owner_uid is not None and info.st_uid != owner_uid):
            raise GrantError("grant file is not trusted")
        data = handle.read(MAX_FILE + 1)
    try:
        grant = _parse(json.loads(data.decode("utf-8")))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise GrantError("grant is not readable") from exc
    if grant.id != grant_id or len(data) > MAX_FILE:
        raise GrantError("grant is not readable")
    return grant


def _parse(raw) -> Grant:
    if not isinstance(raw, dict) or set(raw) != {f.name for f in fields(Grant)}:
        raise ValueError("keys")
    for name in ("id", "goal", "state", "approved_by", "revoked_by"):
        if not isinstance(raw[name], str):
            raise ValueError(name)
    for name in ("max_children", "max_active"):
        if isinstance(raw[name], bool) or not isinstance(raw[name], int) or raw[name] < 1:
            raise ValueError(name)
    for name in ("created", "expires", "revoked"):
        if isinstance(raw[name], bool) or not isinstance(raw[name], (int, float)):
            raise ValueError(name)
        raw[name] = float(raw[name])
    projects = raw["projects"]
    if not isinstance(projects, dict) or not projects or not all(
        isinstance(p, str) and _PROJECT.fullmatch(p) and isinstance(a, str) and _ASSIGNEE.fullmatch(a)
        for p, a in projects.items()
    ):
        raise ValueError("projects")
    if not _TASK.fullmatch(raw["goal"]) or raw["state"] not in (ACTIVE, REVOKED):
        raise ValueError("goal or state")
    return Grant(**raw)


def grants(root: str = GRANTS, owner_uid: Optional[int] = None) -> List[Grant]:
    """Every readable, trusted grant; an untrusted or broken file is skipped."""
    try:
        names = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise GrantError("grants are not readable") from exc
    out = []
    for entry in names:
        name, ext = os.path.splitext(entry)
        if ext == ".json" and _ID.fullmatch(name):
            try:
                out.append(load_grant(root, name, owner_uid))
            except GrantError:
                continue
    return out


def announcement(grant: Grant) -> str:
    """The fixed line that tells the coordinator about the owner's grant; every part comes from the trusted file."""
    until = datetime.fromtimestamp(grant.expires, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    projects = ", ".join(f"{p} {a}" for p, a in sorted(grant.projects.items()))
    return (f"[grant] {grant.goal}: the owner allowed subtasks until {until} (up to {grant.max_children}, "
            f"{grant.max_active} at a time; {projects}); run kd-boss-board create-child --goal {grant.goal}")


def announce(root: str, path: str, now: float, owner_uid: Optional[int], wake) -> int:
    """Tell the coordinator once about each active grant (``wake(line) == "sent"``); a busy coordinator hears it on a
    later call. The record keeps only grants still active, so it stays as small as they are."""
    with atomic_file.locked(path, 0o640):
        raw = atomic_file.read(path, MAX_FILE)
        try:
            before = sorted(set(json.loads(raw.decode("utf-8") or "[]")))
        except (UnicodeDecodeError, ValueError, TypeError):
            before = []
        active = [g for g in grants(root, owner_uid) if g.active(now)]
        told, sent = set(before), 0
        for grant in active:
            if grant.id not in told and wake(announcement(grant)) == "sent":
                told.add(grant.id)
                sent += 1
        after = sorted(told & {g.id for g in active})
        if after != before:
            atomic_file.replace(path, json.dumps(after).encode("utf-8"), 0o640)
    return sent


def active_grant_for(root: str, goal: str, now: float, owner_uid: Optional[int] = None) -> Optional[Grant]:
    found = [g for g in grants(root, owner_uid) if g.goal == goal and g.active(now)]
    return max(found, key=lambda g: (g.created, g.id)) if found else None


@contextmanager
def held(root: str, grant_id: str, *, shared: bool):
    """A lock on the grant file itself, the inode its path names now: ``create_child`` holds it shared from its check
    to the start of the subtask, ``revoke`` exclusively until the revoked file replaced it. Both users can open the
    file (the board owner reads it by group), neither can create a lock file beside the other's."""
    if fcntl is None:  # Windows: tests only; an open file there would also block the revoke's replace
        yield
        return
    path = os.path.join(root, f"{grant_id}.json")
    while True:
        try:
            fd = os.open(path, _OPEN)
        except FileNotFoundError as exc:
            raise GrantError("no such grant") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            current = os.path.samestat(os.fstat(fd), os.stat(path))
        except FileNotFoundError:
            current = False
        except BaseException:
            os.close(fd)
            raise
        if current:
            break
        os.close(fd)  # replaced while we waited: lock the file that is there now
    try:
        yield
    finally:
        os.close(fd)


def revoke(root: str, grant_id: str, by: str, now: float) -> Grant:
    """The owner's "Withdraw": final; a second revoke keeps the first one's time and name. It waits for a running
    ``create_child`` of this grant, so no subtask starts after it."""
    if not isinstance(grant_id, str) or not _ID.fullmatch(grant_id):
        raise GrantError("no such grant")
    with atomic_file.locked(os.path.join(root, grant_id), 0o600), held(root, grant_id, shared=False):
        grant = load_grant(root, grant_id)
        if grant.state == REVOKED:
            return grant
        revoked = replace(grant, state=REVOKED, revoked=float(now), revoked_by=by)
        write_grant(root, revoked)
        return revoked


def drop_project(root: str, project: str, by: str, now: float) -> List[str]:
    """The owner deleted a board project: no grant lets BOSS start a subtask in it any more. A grant whose
    only project it was is revoked, any other loses it; the ids of the grants changed."""
    changed = []
    for grant in grants(root):
        if grant.state != ACTIVE or project not in grant.projects:
            continue
        with atomic_file.locked(os.path.join(root, grant.id), 0o600), held(root, grant.id, shared=False):
            current = load_grant(root, grant.id)
            if current.state != ACTIVE or project not in current.projects:
                continue
            rest = {p: a for p, a in current.projects.items() if p != project}
            if rest:
                write_grant(root, replace(current, projects=rest))
            else:
                write_grant(root, replace(current, state=REVOKED, revoked=float(now), revoked_by=by))
            changed.append(grant.id)
    return changed


# --- the children journal ------------------------------------------------------------------------------------


def _records(path: str) -> list:
    try:
        data = atomic_file.read(path, MAX_JOURNAL)
    except OSError as exc:
        raise Failure(boss_exec.EXIT_ERROR, "children journal is not readable") from exc
    if len(data) > MAX_JOURNAL:
        raise Failure(boss_exec.EXIT_ERROR, "children journal is too large")
    out = []
    for line in data.decode("utf-8", "replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(record, dict)
            and isinstance(record.get("grant"), str)
            and isinstance(record.get("title"), str)
            and record.get("phase") in PHASES
        ):
            out.append(record)
    return out


def children(path: str = CHILDREN, grant_id: Optional[str] = None) -> list:
    """One entry per (grant, title) in creation order: goal, project, assignee, card ("" before created), state."""
    found = {}
    for r in _records(path):
        if grant_id is not None and r["grant"] != grant_id:
            continue
        key = (r["grant"], r["title"])
        if r["phase"] == "dropped":
            found.pop(key, None)
            continue
        if r["phase"] == "intent" or key not in found:  # a compacted line carries the whole child
            found[key] = {
                "grant": r["grant"], "goal": r.get("goal", ""), "title": r["title"], "project": r.get("project", ""),
                "assignee": r.get("assignee", ""), "card": "", "state": "intent", "at": r.get("at", 0),
            }
        if r["phase"] != "intent" and isinstance(r.get("card"), str) and r["card"]:
            found[key].update(card=r["card"], state=r["phase"])
    return list(found.values())


def _compact(path: str) -> None:
    """One line per child, the same answers from ``children`` and ``grant_of_child``; under the create lock."""
    lines = "".join(
        json.dumps({"grant": c["grant"], "goal": c["goal"], "title": c["title"], "project": c["project"],
                    "assignee": c["assignee"], "phase": c["state"], "card": c["card"], "at": c["at"]},
                   ensure_ascii=False, sort_keys=True) + "\n"
        for c in children(path)
    )
    try:
        atomic_file.replace(path, lines.encode("utf-8"), 0o640)
    except OSError:
        pass  # the long journal stays readable; the next create tries again


def grant_of_child(path: str, card: str) -> Optional[str]:
    """The grant a card was created under, by the children journal alone."""
    for r in _records(path):
        if r["phase"] in ("created", "started") and r.get("card") == card:
            return r["grant"]
    return None


def _append(path: str, record: dict) -> None:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    data = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    try:
        fd = os.open(path, flags, 0o640)
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise Failure(boss_exec.EXIT_ERROR, "children journal is unavailable") from exc


def _done(client, card_id: str) -> bool:
    if not card_id:
        return False
    try:
        return boss_exec.load_card(client, card_id).status == boss_board.DONE
    except Failure:
        return False  # unreadable: counted as active, conservatively


# --- create-child ----------------------------------------------------------------------------------------------


def create_child(
    *,
    goal: str,
    title: str,
    project: str,
    reason: str,
    now: float,
    grants_root: str = GRANTS,
    children_path: str = CHILDREN,
    client,
    registry_path: str,
    audit_path: str,
    caller: str,
    owner_uid: Optional[int] = None,
):
    """``(card id, "started" | "already")``; ``boss_exec.Failure`` on a refusal or an error."""
    if not isinstance(goal, str) or not _TASK.fullmatch(goal):
        raise Failure(boss_exec.EXIT_REFUSED, "invalid goal card")
    try:
        with atomic_file.locked(children_path, 0o640):
            return _create_child(
                goal, title, project, reason, now, grants_root, children_path, owner_uid,
                dict(client=client, registry_path=registry_path, audit_path=audit_path,
                     caller=caller, now=lambda: now),
            )
    except OSError as exc:
        raise Failure(boss_exec.EXIT_ERROR, "children journal is unavailable; nothing was changed") from exc


def _create_child(goal, title, project, reason, now, grants_root, children_path, owner_uid, common):
    try:
        grant = active_grant_for(grants_root, goal, now, owner_uid)
    except GrantError as exc:
        raise Failure(boss_exec.EXIT_ERROR, f"{goal}: owner grants are not readable ({exc})") from exc
    if grant is None:
        raise Failure(boss_exec.EXIT_REFUSED, f"{goal}: no active owner grant; propose one with propose-delegate")
    try:
        with held(grants_root, grant.id, shared=True):  # a revoke waits until this subtask is started or refused
            grant = load_grant(grants_root, grant.id, owner_uid)  # again: a revoke just before the hold is seen
            if not grant.active(now):
                raise Failure(boss_exec.EXIT_REFUSED, f"{goal}: the owner's grant {grant.id[:8]} was revoked or expired")
            return _start_child(grant, goal, title, project, reason, now, children_path, common)
    except GrantError as exc:
        raise Failure(boss_exec.EXIT_ERROR, f"{goal}: owner grants are not readable ({exc})") from exc


def _start_child(grant, goal, title, project, reason, now, children_path, common):
    client = common["client"]
    gid8 = grant.id[:8]
    if project not in grant.projects:
        raise Failure(boss_exec.EXIT_REFUSED, f"project {project} is not in the owner's grant {gid8}")
    request = {
        "op": "create-child", "actor": boss_board.ACTOR, "reason": reason, "goal": goal, "grant": grant.id,
        "title": title, "project": project, "assignee": grant.projects[project],
    }
    routing = boss_exec.load_routing(client, common["registry_path"])
    try:
        boss_board.plan(request, routing)
    except boss_board.Refused as exc:
        raise Failure(boss_exec.EXIT_REFUSED, str(exc)) from exc
    title = title.strip()
    common = {**common, "audit_extra": {"grant": grant.id, "goal": goal}}
    mine = children(children_path, grant.id)
    same = next((c for c in mine if c["title"] == title), None)
    record = {"grant": grant.id, "goal": goal, "title": title}
    if same is not None:
        if same["state"] == "started":
            return same["card"], "already"
        if same["state"] == "intent":
            raise Failure(
                boss_exec.EXIT_ERROR,
                f"uncertain: an earlier create of this title left no card ID; look for a card naming grant {gid8} "
                "on the board and tell the owner",
            )
        card_id = same["card"]
    else:
        if len(mine) >= grant.max_children:
            raise Failure(boss_exec.EXIT_REFUSED, f"the grant allows {grant.max_children} subtasks; all are used")
        if sum(1 for c in mine if not _done(client, c["card"])) >= grant.max_active:
            raise Failure(
                boss_exec.EXIT_REFUSED,
                f"the grant allows {grant.max_active} active subtasks at a time; wait until one is Done",
            )
        audit = boss_exec.Audit(common["audit_path"])
        try:  # a create the rate limit refuses leaves no intent behind
            boss_exec.rate_limit(audit, "create-child", now)
        finally:
            audit.close()
        _append(children_path, {**record, "project": project, "assignee": request["assignee"],
                                "caller": common["caller"], "phase": "intent", "at": now})
        try:
            card_id, _ = boss_exec.execute(request, **common)
        except Failure as exc:
            if exc.code in (boss_exec.EXIT_RATE, boss_exec.EXIT_REFUSED) and not exc.applied:
                _append(children_path, {**record, "phase": "dropped", "at": now})  # refused before any create
            raise
        _append(children_path, {**record, "phase": "created", "card": card_id, "at": now})
    card = boss_exec.load_card(client, card_id)
    if card.status == boss_board.INBOX:
        if len(card.assignees) != 1 or grant.projects.get(card.project) != card.assignees[0]:
            raise Failure(boss_exec.EXIT_REFUSED, f"{card_id} was moved outside the grant {gid8}; ask the owner")
        boss_exec.execute(
            {"op": "promote", "actor": boss_board.ACTOR, "task": card_id, "revision": card.revision,
             "reason": f"подзадача цели {goal} по разрешению владельца {gid8}"},
            **common, grant=grant.id,
        )
    _append(children_path, {**record, "phase": "started", "card": card_id, "at": now})
    try:
        large = os.path.getsize(children_path) > COMPACT_AT
    except OSError:
        large = False
    if large:
        _compact(children_path)
    return card_id, "started"
