"""Plan windows in the monitor: what a step of the sysadmin's approved plan changed on a server goes into
the baseline instead of opening incidents.

The executor opens a window in the cabinet around the step and closes it with the objects the step changed. While a
window is open the tick does not compare that server's snapshot, listeners and peers (``mark``; the cabinet silent —
nothing is skipped). A closed window (``apply_closed``, under tick.lock) is taken object by object: a dangerous change
(``seccheck.dangerous``: keys, sudoers, groups, a uid 0 user, a weaker sshd, ld.so.preload, a firewall down) stays out
and opens its incident as usual, which the cabinet links to the step's card; an object whose baseline differed from
the step's "before" stays out too — the difference is not the step's. A gone listener or peer is no incident and
changes nothing. The problems of the taken objects close; the report goes to the cabinet, which tells the card.
"""

import json
import sys
from datetime import timedelta
from typing import List, Optional

from . import cabinet, seccheck
from .model import CheckResult
from .baseline import REMOVED, Baseline

ERRORS = (cabinet.cabinet_client.CabinetError, OSError, ValueError, KeyError, TypeError)
DONE_DAYS = 2  # a window's result is kept to repeat a lost report


def _kind(exc) -> str:
    return getattr(exc, "kind", type(exc).__name__)


def active(cab, cfg=None) -> Optional[List[str]]:
    """The servers with an open window; None — the cabinet did not answer. Windows switched off
    (``security.plan_windows``) leave no server waiting: a step's changes alert at once, as before."""
    if cfg is not None and not cfg["security"].get("plan_windows", True):
        return []
    try:
        return list(cab.call({"op": "monitor.window.active"})["servers"])
    except ERRORS as exc:
        print(
            f"kd-monitor: окна шагов плана не прочитаны ({_kind(exc)})", file=sys.stderr
        )
        return None


def mark(sec: dict, servers, now) -> None:
    """sec["plan_windows"] — the servers the tick leaves alone (runner._sec, _ports, _wgpeers)."""
    sec["plan_windows"] = {s: now.isoformat() for s in servers or ()}


HELD = (
    "ports",
    "wgpeers",
    "posture",
)  # with every sec:* — what a step's window keeps out of the tick


def late(cab, cfg, marked) -> set:
    """Servers whose step began while the tick polled (review 5): a window open now, or opened and closed already
    (still to be applied), that the tick's start did not see. Their sec, listeners and peers of this tick are the
    step's, not news."""
    if not cfg["security"].get("plan_windows", True):
        return set()
    servers = set(active(cab, cfg) or ())
    try:
        servers |= {
            w["server"] for w in cab.call({"op": "monitor.window.take"})["windows"]
        }
    except ERRORS as exc:
        print(
            f"kd-monitor: окна шагов плана не прочитаны ({_kind(exc)})", file=sys.stderr
        )
    return servers - set(marked)


def hold(results, servers) -> list:
    """Those servers' sec, ports, peers and posture of this tick as not run: their problems stay as they were."""
    return [
        CheckResult(r.server, r.check, False, [])
        if r.server in servers and (r.check.startswith("sec:") or r.check in HELD)
        else r
        for r in results
    ]


def _base_value(srv, section, item) -> Optional[str]:
    if section == "ports":
        return srv["ports"].get(item)
    if section == "wgpeers":
        return "" if item in srv["wgpeers"] else None
    return srv["snapshot"].get(f"{section}:{item}")


def _unit_settled(reg, srv, server, unit) -> bool:
    """A unit's open incident closes only when every object it names is now the baseline's: a file that drifted
    before the step keeps the incident the step's link would otherwise retire (review 2)."""
    p = reg.state["problems"].get(f"{server}:sec:units:{unit}")
    if p is None:
        return True
    try:
        objects = json.loads(p.get("value", ""))
    except ValueError:
        objects = None
    if not isinstance(
        objects, dict
    ):  # a split incident of before the change: the value is its unit file's
        objects = {unit: p.get("value")}
    return all(
        seccheck.same_value(
            "units",
            item,
            srv["snapshot"].get("units:" + item),
            None if new == REMOVED else new,
        )
        for item, new in objects.items()
    )


def _apply(reg, baseline, w, cfg, now) -> dict:
    server = w["server"]
    if not cfg["security"].get("plan_windows", True):
        return {"ok": False, "accepted": [], "dangerous": [], "detail": "выключено"}
    if baseline.broken:
        return {
            "ok": False,
            "accepted": [],
            "dangerous": [],
            "detail": "эталон повреждён (baseline.json не читается)",
        }
    if not baseline.has(server):
        return {
            "ok": True,
            "accepted": [],
            "dangerous": [],
            "detail": "у сервера нет эталона",
        }
    srv, at = baseline.server(server), now.isoformat()
    accepted, dangerous, drifted = [], [], 0
    for key, (old, new) in sorted(w["changes"].items()):
        section, _, item = key.partition(":")
        listing = section in ("ports", "wgpeers")
        check = section if listing else "sec:" + section
        if listing and new is None:
            continue
        # key fingerprints follow their keys file, which the owner accepts (review 1); «-» and a missing line
        # of a file are one (review 6)
        if section in seccheck.INFO_SECTIONS or (
            not listing and seccheck.same_value(section, item, old, new)
        ):
            continue
        if not listing and seccheck.dangerous(section, item, old, new):
            dangerous.append([check, item])
            continue
        base = _base_value(srv, section, item)
        if base != old and (
            listing or not seccheck.same_value(section, item, base, old)
        ):
            drifted += 1
            continue
        baseline.accept(
            check, server, item, new if listing else (REMOVED if new is None else new)
        )
        subject = seccheck.unit_of(item) if section == "units" else item
        if [check, subject] not in accepted:
            accepted.append([check, subject])
        if (
            subject != item or section != "units"
        ):  # a unit's own key closes below, once all of it is in
            reg.close_key(f"{server}:{check}:{item}", at)
    for check, subject in accepted:
        if check == "sec:units" and _unit_settled(reg, srv, server, subject):
            reg.close_key(f"{server}:{check}:{subject}", at)
    if accepted:
        seccheck.note_accepted(
            seccheck.sec_state(reg.state),
            now,
            f"{server}: шаг плана {w['card']} — в эталон {len(accepted)}",
        )
    detail = (
        f"{drifted} отличались от эталона ещё до шага — проверятся как обычно"
        if drifted
        else ""
    )
    return {"ok": True, "accepted": accepted, "dangerous": dangerous, "detail": detail}


def _done(state, now) -> dict:
    book = state.get("plan_windows_done")
    if not isinstance(book, dict):
        book = state["plan_windows_done"] = {}
    edge = (now - timedelta(days=DONE_DAYS)).isoformat()
    for k in [
        k
        for k, v in book.items()
        if not isinstance(v, dict) or str(v.get("at", "")) < edge
    ]:
        del book[k]
    return book


def apply_closed(cab, state_dir, now, cfg, reg_load, state_lock, save_reg) -> list:
    """The closed windows → baseline, registry, the cabinet's queue → reports. → lines for the journal. A window
    applied before (its report was lost) is reported again with the same result."""
    try:
        got = cab.call({"op": "monitor.window.take"})["windows"]
    except ERRORS as exc:
        print(
            f"kd-monitor: окна шагов плана не прочитаны ({_kind(exc)})", file=sys.stderr
        )
        return []
    if not got:
        return []
    results = {}
    with state_lock():
        reg = reg_load()
        book = _done(reg.state, now)
        baseline = Baseline.load(state_dir / "baseline.json")
        for w in got:
            if w["id"] not in book:
                book[w["id"]] = dict(
                    _apply(reg, baseline, w, cfg, now),
                    server=w["server"],
                    at=now.isoformat(),
                )
            results[w["id"]] = book[w["id"]]
        if baseline.dirty:
            baseline.save(state_dir / "baseline.json")
        cabinet.enqueue(reg.state, cabinet.items_from(reg.journal), now)
        save_reg(reg)
    lines = []
    for wid, r in results.items():
        try:
            cab.call(
                {
                    "op": "monitor.window.report",
                    "id": wid,
                    "ok": r["ok"],
                    "accepted": r["accepted"],
                    "dangerous": r["dangerous"],
                    "detail": r["detail"],
                }
            )
        except ERRORS as exc:
            lines.append(
                f"kd-monitor: окно шага плана {wid[:8]}: отчёт не записан ({_kind(exc)}) — повторю"
            )
            continue
        what = (
            f"в эталон {len(r['accepted'])}, опасное {len(r['dangerous'])}"
            if r["ok"]
            else "не применено: " + r["detail"]
        )
        lines.append(f"kd-monitor: окно шага плана {wid[:8]} ({r['server']}): {what}")
    return lines
