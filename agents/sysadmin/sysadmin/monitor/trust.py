"""The monitor's trust by the owner's signature: monitor_trust decisions.

`kd-monitor accept|baseline|trust` only propose — a monitor_trust decision in the cabinet (`propose`), baseline.json
does not change; `kd-monitor revoke` proposes to close a login by key as "access revoked" — without the baseline.
What the owner signed the monitor itself applies: a tick — accept, revoke and trust and the removal of addresses
from the trusted list (`apply_signed`, under tick.lock), `kd-monitor check-now` — baseline (`apply_baselines`: a
capture takes up to ~230 s, a tick has no budget for it). Each one's outcome is `monitor_trust.report` ("applied" or
"error: reason").

state["trust_applied"] remembers what was applied (id → outcome, TRUST_DAYS): the cabinet's answer to a report got
lost — the next run only repeats the same report and does not apply it a second time (accept has closed the P-n — a
second time would be "P-n is closed").
"""
import sys
import hashlib
from datetime import datetime, timedelta

from . import cabinet, seccheck
from .baseline import Baseline, is_weak
from .registry import split_key

TRUST_DAYS = 8
DETAIL_MAX = 480
SNAPSHOT_MAX_AGE = timedelta(hours=3)  # the security snapshot is taken hourly; three leaves room for a failure


def _call(cab, request):
    """The cabinet's answer or None (unreachable — what was signed waits for the next run)."""
    try:
        return cab.call(request)
    except (cabinet.cabinet_client.CabinetError, OSError, ValueError) as exc:
        print(f"kd-monitor: доверие по подписи: кабинет не ответил ({getattr(exc, 'kind', type(exc).__name__)})",
              file=sys.stderr)
        return None


def propose(cab, **request) -> dict:
    """A proposal to the cabinet; CabinetError goes out (the CLI prints the reason)."""
    return cab.call({"op": "monitor_trust.propose", **request})


def propose_many(cab, incidents, text="", kind="accept") -> dict:
    """accepts (or revokes) of many P-n as one bundle the owner signs once; CabinetError goes out."""
    return cab.call({"op": "monitor_trust.propose_many", "incidents": list(incidents), "text": text, "kind": kind})


def remove(cab, cidr: str) -> dict:
    return cab.call({"op": "monitor_trust.remove", "cidr": cidr})


def take(cab, kinds):
    """(the signed decisions, the removals of addresses) or None."""
    out = _call(cab, {"op": "monitor_trust.take", "kinds": list(kinds)})
    if out is None:
        return None
    return out.get("decisions") or [], out.get("removals") or []


def applied(state: dict) -> dict:
    book = state.get("trust_applied")
    if not isinstance(book, dict):
        book = state["trust_applied"] = {}
    return book


def _remember(state, key, ok, detail, now) -> None:
    book = applied(state)
    book[key] = {"ok": ok, "detail": detail[:DETAIL_MAX], "at": now.isoformat()}
    edge = (now - timedelta(days=TRUST_DAYS)).isoformat()
    for k in [k for k, v in book.items() if not isinstance(v, dict) or str(v.get("at", "")) < edge]:
        del book[k]


def report(cab, key: str, result: dict) -> bool:
    """A report to the cabinet: key — "d:<id>" of a decision or "r:<id>" of a removal."""
    kind, ident = key.split(":", 1)
    request = {"op": "monitor_trust.report", "ok": bool(result["ok"]), "detail": result.get("detail", "")}
    request.update({"id": ident} if kind == "d" else {"removal": ident})
    return _call(cab, request) is not None


def _signed_problem(reg, payload) -> tuple:
    """(P-n, the problem, "") of a signed decision about it, or (P-n, None, why not): closed or changed."""
    pid = f"P-{payload.get('incident')}"
    p = next((p for p in reg.state["problems"].values() if p.get("id") == pid), None)
    if p is None:
        return pid, None, f"{pid} закрыта"
    signed_hash = payload.get("value_hash")
    if (signed_hash and signed_hash != hashlib.sha256(str(p["value"]).encode()).hexdigest()) or (
            not signed_hash and "value" in payload and payload["value"] != p["value"]):
        return pid, None, f"{pid}: объект изменился после подписи; требуется новое решение"
    return pid, p, ""


def _accept(reg, baseline, payload, decided_by, now) -> tuple:
    pid, p, why = _signed_problem(reg, payload)
    if p is None:
        return False, why
    server, check, subject = split_key(p)
    try:
        text = baseline.accept(check, server, subject, p["value"], payload.get("label") or "")
    except ValueError as exc:
        return False, str(exc)
    sec = seccheck.sec_state(reg.state)
    if check == "logins":
        sec["pending"].get(server, {}).pop(subject, None)
    if check == "sec:keys":  # the baseline's key fingerprints — of the same file version that was accepted
        seccheck.accept_keyprints(baseline, server, subject, p["value"], sec["current"].get(server))
    reg.close_now(pid, now.isoformat())
    seccheck.note_accepted(sec, now, f"{pid}: {text} (подписал {decided_by or 'владелец'})")
    return True, text


def _revoke(reg, payload, decided_by, now) -> tuple:
    """The close as "access revoked" — a login by key is closed without the baseline when, by the server's latest snapshot, the key
    no longer gets in through any authorized_keys. The identity leaves the pending list and is not enrolled in the
    baseline: the next login with this key opens a new problem."""
    pid, p, why = _signed_problem(reg, payload)
    if p is None:
        return False, why
    server, check, subject = split_key(p)
    if check != "logins" or subject == "gap" or is_weak(subject):
        return False, f"{pid}: «доступ снят» — только для входа по ключу"
    if "-CERT " in subject:  # a certificate's fingerprint is in no authorized_keys: the snapshot cannot prove it gone
        return False, f"{pid}: вход по сертификату — снимок ключей не покажет, что доступ снят"
    fingerprint = subject.rsplit(" ", 1)[-1]
    sec = seccheck.sec_state(reg.state)
    cur = sec["current"].get(server)
    paths, unknown = seccheck.allowed_paths(cur, fingerprint)
    if paths is None:
        return False, f"{pid}: {server}: {unknown} — не проверить, что ключ снят"
    stale = _stale(cur, sec["pending"].get(server, {}).get(subject, {}).get("last") or p.get("opened_at"), now)
    if stale:
        return False, f"{pid}: {stale}; повторите после проверки сервера"
    taken = str(cur.get("at", ""))[:16].replace("T", " ")
    if paths:
        return False, f"{pid}: ключ ещё разрешён на {server}: {', '.join(paths[:3])} (снимок {taken})"
    sec["pending"].get(server, {}).pop(subject, None)
    reg.close_now(pid, now.isoformat(), how="revoke")
    text = f"доступ {subject} на {server} снят (снимок {taken}), в эталон не внесён"
    seccheck.note_accepted(sec, now, f"{pid}: {text} (подписал {decided_by or 'владелец'})")
    return True, text


def _stale(cur: dict, last_login, now) -> str:
    """Why the snapshot is no proof of "the key is gone", or "": it must be taken after the last login (before it the
    key may have been added and removed later — or not be in the snapshot yet) and be no older than SNAPSHOT_MAX_AGE."""
    try:
        taken = datetime.fromisoformat(str(cur.get("at", "")))
        login = datetime.fromisoformat(str(last_login))
        if taken.tzinfo is None or login.tzinfo is None:
            raise ValueError("naive")
    except ValueError:
        return "время снимка или входа не разобрать"
    if taken <= login:
        return "снимок ключей старше последнего входа"
    if now - taken > SNAPSHOT_MAX_AGE:
        return "снимок ключей старше " + str(int(SNAPSHOT_MAX_AGE.total_seconds() // 3600)) + " ч"
    return ""


def _trust(reg, baseline, payload, decided_by, now) -> tuple:
    try:
        key = baseline.trust(payload.get("cidr", ""), payload.get("label") or "")
    except ValueError as exc:
        return False, str(exc)
    text = f"доверенный адрес {key} («{payload.get('label')}»)"
    seccheck.note_accepted(seccheck.sec_state(reg.state), now, f"{text} (подписал {decided_by or 'владелец'})")
    return True, text


def _untrust(reg, baseline, cidr, now) -> tuple:
    try:
        key = baseline.untrust(cidr)
    except ValueError as exc:
        return False, str(exc)
    if key is None:
        return False, f"{cidr} не в списке доверенных"
    text = f"доверенный адрес {key} убран"
    seccheck.note_accepted(seccheck.sec_state(reg.state), now, text)
    return True, text


def apply_signed(cab, state_dir, now, reg_load, state_lock, save_reg) -> list:
    """A tick (under tick.lock): the signed accepts and trusts and the removals of addresses → the baseline, the
    registry, the cabinet's queue → the reports. → lines for the journal; None — the cabinet did not answer (what was
    signed waits for the next tick). reg_load/state_lock/save_reg — the monitor's registry (deliver)."""
    got = take(cab, ("accept", "trust", "revoke"))
    if got is None:
        return None
    signed, removals = got
    if not signed and not removals:
        return []
    results, lines = {}, []
    with state_lock():
        reg = reg_load()
        book = applied(reg.state)
        baseline = Baseline.load(state_dir / "baseline.json")
        for d in signed:
            key = "d:" + d["id"]
            if key in book:
                results[key] = book[key]
                continue
            payload = d.get("payload") or {}
            if baseline.broken:
                ok, detail = False, "эталон повреждён (baseline.json не читается)"
            elif payload.get("kind") == "accept":
                ok, detail = _accept(reg, baseline, payload, d.get("decided_by", ""), now)
            elif payload.get("kind") == "revoke":
                ok, detail = _revoke(reg, payload, d.get("decided_by", ""), now)
            else:
                ok, detail = _trust(reg, baseline, payload, d.get("decided_by", ""), now)
            _remember(reg.state, key, ok, detail, now)
            results[key] = book[key]
        for r in removals:
            key = "r:" + r["id"]
            if key not in book:
                ok, detail = (False, "эталон повреждён (baseline.json не читается)") if baseline.broken else (
                    _untrust(reg, baseline, r.get("cidr", ""), now))
                _remember(reg.state, key, ok, detail, now)
            results[key] = book[key]
        if baseline.dirty:
            baseline.save(state_dir / "baseline.json")
        cabinet.enqueue(reg.state, cabinet.items_from(reg.journal), now)
        save_reg(reg)
    for key, result in results.items():
        if report(cab, key, result):
            lines.append(f"kd-monitor: доверие по подписи {key[2:10]}: "
                         + ("применено" if result["ok"] else "ошибка: " + result["detail"]))
    return lines


def apply_baselines(cab, state_dir, now, servers, capture_fn, tick_lock, reg_load, state_lock, save_reg) -> list:
    """kd-monitor check-now: the signed baselines → a capture under tick.lock (it waits for a running tick) → a report.
    servers — the attached servers (None — the inventory was not read: wait for the next run); capture_fn(baseline,
    targets) → (lines, {server: baseline}). One decision is captured per run: the wait for the lock (up to 290 s) and
    a capture (up to a tick's budget) together fit the unit's TimeoutStartSec=620, a second one no longer does; the
    timer (15 s) takes the next. A repeated report of an earlier capture needs no capture — any number of them fits.
    → the journal's lines."""
    if servers is None:
        return []
    got = take(cab, ("baseline",))
    if not got or not got[0]:
        return []
    lines, captured_once = [], False
    for d in got[0]:
        key = "d:" + d["id"]
        with state_lock():
            done = applied(reg_load().state).get(key)
        if done is None:
            alias = (d.get("payload") or {}).get("server")
            targets = list(servers) if alias == "all" else [alias]
            if not targets or any(s not in servers for s in targets):
                result = (False, f"сервер {alias} не подключён к монитору")
            elif captured_once:
                continue  # one capture per run — the next run takes the next
            else:
                captured_once = True
                with tick_lock() as got_lock:
                    if not got_lock:
                        lines.append("kd-monitor: эталон по подписи ждёт: tick.lock занят")
                        continue  # the next check-now run repeats it
                    result = _baseline(state_dir, now, targets, capture_fn, d, reg_load, state_lock, save_reg, lines)
            with state_lock():
                reg = reg_load()
                _remember(reg.state, key, result[0], result[1], now)
                save_reg(reg)
                done = applied(reg.state)[key]
        if report(cab, key, done):
            lines.append(f"kd-monitor: эталон по подписи {d['id'][:8]}: "
                         + ("применено" if done["ok"] else "ошибка: " + done["detail"]))
    return lines


def _baseline(state_dir, now, targets, capture_fn, d, reg_load, state_lock, save_reg, lines):
    baseline = Baseline.load(state_dir / "baseline.json")
    if baseline.broken:
        return False, "эталон повреждён (baseline.json не читается)"
    printed, captured = capture_fn(baseline, targets)
    lines += printed
    missed = [s for s in targets if s not in captured]
    if not captured:
        why = next((ln for ln in printed if "НЕ снят" in ln), "")
        return False, "сервер недоступен: " + (why or ", ".join(missed))
    with state_lock():
        for sv, data in captured.items():
            baseline.merge_server(sv, data)
        baseline.save(state_dir / "baseline.json")
        reg = reg_load()
        seccheck.note_accepted(seccheck.sec_state(reg.state), now, "эталон снят: " + ", ".join(sorted(captured))
                               + f" (подписал {d.get('decided_by') or 'владелец'})")
        save_reg(reg)
    detail = "эталон снят: " + ", ".join(sorted(captured))
    if missed:
        detail += "; не снят: " + ", ".join(missed)
    return True, detail
