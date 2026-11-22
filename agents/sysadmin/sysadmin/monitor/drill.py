"""The owner's test critical alert: a button in the cabinet checks the whole path of a critical incident.

A scheduled tick takes the request (`monitor.test.take`) and raises a crit observation on the stub server
`test` · `alert` (the value says it is a test): the registry opens a problem, the cabinet — a P-n, a push and the
Telegram duplicate (kd-alert --watch). No server is polled or touched; the test problem's event is removed from
outbox — the sysadmin is not woken and there is no kd-alert fallback. The next ticks see "ok", and the problem
closes the ordinary way (CLOSE_AFTER runs); the closing is `monitor.test.done`. A partial tick ("check now") leaves
the drill alone; the weekly digest and `kd-monitor list` do not show the test problem.
"""
import sys

from .model import CheckResult, Observation

SERVER = "test"  # the same names as the cabinet's (kd.cabinet.monitor_test)
CHECK = "alert"
KEY = f"{SERVER}:{CHECK}:"
STATE = "drill"
VALUE_CRIT = "ТЕСТ: проверка критичного алерта, серверы не затронуты"
VALUE_OK = "ТЕСТ завершён"
TEXT = "Учебная тревога по кнопке владельца: проверка кабинета, push и Telegram. Действий не нужно."


def take(cab):
    """A request from the cabinet ({id, …}) or None: there is none or the cabinet did not answer (the request waits for the next tick)."""
    try:
        return cab.call({"op": "monitor.test.take"})["test"]
    except Exception as exc:  # noqa: BLE001 — a drill never crashes the tick
        print(f"kd-monitor: заявка тестового алерта не прочитана ({getattr(exc, 'kind', type(exc).__name__)})",
              file=sys.stderr)
        return None


def result(state: dict, test, now):
    """The drill's observation for this tick, or None. A new request starts the drill anew (the cabinet has already
    closed the former one by its deadline): the first tick is crit, the next ones "ok" until the registry closes the problem."""
    if isinstance(test, dict) and test.get("id"):
        state[STATE] = {"id": test["id"], "phase": "crit", "at": now.isoformat()}
    drill = state.get(STATE)
    if not isinstance(drill, dict):
        return None
    level, value = ("crit", VALUE_CRIT) if drill.get("phase") == "crit" else ("ok", VALUE_OK)
    drill["phase"] = "ok"
    return CheckResult(SERVER, CHECK, True, [Observation(SERVER, CHECK, "", level, value, TEXT)])


def quiet(state: dict) -> None:
    """The test problem's event is not for the sysadmin: out of outbox (no wake and no kd-alert fallback)."""
    state["outbox"] = [e for e in state.get("outbox", []) if e.get("key") != KEY]


def finished(state: dict):
    """The id of the drill whose problem has closed, or None. The drill stays in the state until its result is written
    (forget): not written — the next tick repeats (its "ok" for a closed problem changes nothing)."""
    drill = state.get(STATE)
    if not isinstance(drill, dict) or drill.get("phase") != "ok" or KEY in state.get("problems", {}):
        return None
    return drill.get("id")


def forget(state: dict, test_id) -> bool:
    drill = state.get(STATE)
    if isinstance(drill, dict) and drill.get("id") == test_id:
        del state[STATE]
        return True
    return False


def report(cab, test_id) -> bool:
    """The result to the cabinet. True — written, or there is nothing to write (the cabinet does not know the request); False — repeat."""
    try:
        cab.call({"op": "monitor.test.done", "id": test_id, "ok": True})
        return True
    except Exception as exc:  # noqa: BLE001 — not written: repeated on the next tick, the deadline at the worst
        if getattr(exc, "kind", "") == "not_found":
            return True
        print(f"kd-monitor: итог тестового алерта не записан ({getattr(exc, 'kind', type(exc).__name__)})",
              file=sys.stderr)
        return False
