"""Plan windows: a step of the sysadmin's approved plan changes a server on purpose; the executor takes a
security snapshot of the server before and after the step and hands the difference to the cabinet, where the monitor
takes it into its baseline instead of opening incidents (dangerous objects still open theirs).

One ssh call over the exec key takes the three parts the monitor compares — the security snapshot, the listeners and
the WireGuard peers — each followed by a marker line with its exit code. A window that cannot open (no card, the
cabinet down, no snapshot) never stops the step: its changes are then checked as usual, and the card hears why.
"""

import secrets
import sys
import uuid
from typing import Callable, Dict, List, NamedTuple, Optional, Set, Tuple

from kd import exec_envelope
from kd.cabinet import client as cabinet_client
from sysadmin import inventory
from sysadmin.monitor import seccheck

SNAPSHOT_TIMEOUT = 20  # a snapshot takes seconds; two of them keep a 600 s step inside the agent's 11 minutes
WINDOW_COST = 2 * SNAPSHOT_TIMEOUT
LIMIT = 4 * 1024 * 1024
CHANGES_MAX = 5000
SNAPSHOT = "/usr/local/bin/agent-sec-snapshot"
PARTS = (
    ("snapshot", SNAPSHOT),
    ("ports", "ss -tlnpu"),
    ("wgpeers", "if command -v wg >/dev/null 2>&1; then wg show all allowed-ips; fi"),
)
CABINET_ERRORS = (cabinet_client.CabinetError, OSError, ValueError, KeyError, TypeError)


def _script(nonce: str) -> str:
    return "; ".join(
        f"{command}; printf '\\n@@kd-window-{nonce} {part} rc=%s\\n' \"$?\""
        for part, command in PARTS
    )


def _parts(output: str, nonce: str) -> Dict[str, Tuple[int, str]]:
    marker, parts, buf = f"@@kd-window-{nonce} ", {}, []
    for line in output.splitlines():
        if line.startswith(marker):
            part, _, rc = line[len(marker) :].partition(" rc=")
            parts[part] = (int(rc) if rc.isdigit() else -1, "\n".join(buf))
            buf = []
        else:
            buf.append(line)
    return parts


def capture(
    server, runner, timeout=SNAPSHOT_TIMEOUT
) -> Tuple[Optional[Dict[str, str]], Set[str], str]:
    """→ (``section:item`` → value of the snapshot, ``ports:<listener>`` → process, ``wgpeers:<peer>`` → "";
    the sections not compared; why there is no snapshot at all, or "")."""
    from sysadmin import execute

    nonce = secrets.token_hex(8)
    # a host that sends envelopes lets the target's exec gate take the snapshot (its second word, the
    # same parts and markers, sudo inside the gate); otherwise the script goes over the exec key as before
    if exec_envelope.enabled():
        remote = f"{exec_envelope.SNAPSHOT_RUN} {nonce}"
    else:
        remote = execute.remote_command(server, _script(nonce))
    ssh = inventory.ssh_argv(
        execute.EXEC_KEY,
        execute.KNOWN_HOSTS,
        server.exec_user,
        server.host,
        remote,
    )
    try:
        result = runner(ssh, timeout=timeout, limit=LIMIT, kill_on_limit=True)
    except execute.Interrupted:
        raise
    except Exception as exc:  # noqa: BLE001 — ssh did not start: no window, the step still runs
        return None, set(), f"ssh: {exc.__class__.__name__}"
    if result.timed_out:
        return None, set(), f"снимок не снят за {timeout} с"
    if result.truncated:
        return None, set(), "снимок больше предела"
    parts = _parts(result.output.decode("utf-8", "replace"), nonce)
    rc, text = parts.get("snapshot", (-1, ""))
    parsed = seccheck.parse_snapshot(text) if rc == 0 else None
    if parsed is None:
        return None, set(), f"снимок не снят (код {rc})"
    snapshot, failed = parsed
    items, errors = dict(snapshot), set(failed)
    rc, text = parts.get("ports", (-1, ""))
    if rc == 0:
        items.update(
            {"ports:" + k: v for k, v in seccheck.parse_listeners(text).items()}
        )
    else:
        errors.add("ports")
    rc, text = parts.get("wgpeers", (-1, ""))
    if rc == 0:
        items.update({"wgpeers:" + p: "" for p in seccheck.parse_wgpeers(text)})
    else:
        errors.add("wgpeers")
    return items, errors, ""


def diff(before, before_errors, after, after_errors) -> Dict[str, List[Optional[str]]]:
    """The objects whose value differs, [old, new] with None for a missing side; a section with an error in either
    snapshot is not compared."""
    skip = set(before_errors) | set(after_errors)
    out = {}
    for key in sorted(set(before) | set(after)):
        old, new = before.get(key), after.get(key)
        if old != new and key.split(":", 1)[0] not in skip:
            out[key] = [old, new]
    return out


class Opened(NamedTuple):
    id: str
    server: object
    before: Dict[str, str]
    errors: Set[str]


def _step(extra) -> str:
    if extra.get("kind") == "admin":
        return f"операция {extra.get('op_index')}/{extra.get('op_count')}"
    return "критичная команда" if extra.get("kind") == "critical" else "шаг"


class Windows:
    def __init__(self, client=None):
        self._client = client

    def _call(self, request) -> dict:
        return (self._client or cabinet_client.Client()).call(request)

    @staticmethod
    def _say(text, err) -> None:
        print(f"ssh-exec: {text}", file=err or sys.stderr)

    def _failed(self, notify: Callable, server, reason: str, err) -> None:
        self._say(
            f"окно шага плана на {server.alias} не открыто ({reason}) — изменения шага проверятся как обычно",
            err,
        )
        notify(
            "info",
            "notice.exec.window_failed",
            {"server": server.alias, "reason": reason[:200]},
        )

    def _unclosed(self, notify: Optional[Callable], server, reason: str, err) -> None:
        """The window closed without the step's difference: the card hears it, as a window that did not open."""
        self._say(
            f"окно шага плана на {server.alias} не закрыто ({reason}) — изменения шага проверятся как обычно",
            err,
        )
        if notify is not None:
            notify(
                "info",
                "notice.exec.window_unclosed",
                {"server": server.alias, "reason": reason[:200]},
            )

    def skipped(self, server, extra, notify, reason: str, err=None) -> None:
        """No window for this step (a group short of time): the card hears why, as for one that did not open."""
        if extra.get("task_id"):
            self._failed(notify, server, reason, err)

    def _close(self, wid, err, changes=None, errors=(), failed="") -> None:
        request = {
            "op": "monitor.window.close",
            "id": wid,
            "changes": changes or {},
            "errors": list(errors),
        }
        if failed:
            request["failed"] = failed
        try:
            self._call(request)
        except CABINET_ERRORS as exc:
            self._say(
                f"окно {wid} не закрыто ({getattr(exc, 'kind', exc.__class__.__name__)}) — истечёт через "
                "30 минут, изменения проверятся как обычно",
                err,
            )

    def begin(
        self, server, extra, runner, notify, run="", err=None
    ) -> Optional[Opened]:
        """The window of one server's step, or None — no card, or it could not open (then the card hears why)."""
        card = extra.get("task_id")
        if not card:
            return None
        wid = uuid.uuid4().hex[
            :16
        ]  # named here: a signal inside the open call still closes it (review 9)
        try:
            wid = self._call(
                {
                    "op": "monitor.window.open",
                    "server": server.alias,
                    "card": card,
                    "step": _step(extra),
                    "run": run or uuid.uuid4().hex[:16],
                    "id": wid,
                }
            ).get("id", wid)
        except BaseException as exc:
            if not isinstance(
                exc, CABINET_ERRORS
            ):  # interrupted: the window may already be there
                self._close(wid, err, failed="прервано до шага")
                raise
            self._failed(
                notify,
                server,
                f"кабинет: {getattr(exc, 'kind', exc.__class__.__name__)}",
                err,
            )
            return None
        try:
            before, errors, reason = capture(server, runner)
        except BaseException:  # interrupted before the step: nothing ran
            self._close(wid, err, failed="прервано до шага")
            raise
        if reason:
            self._close(wid, err, failed=reason)
            self._failed(notify, server, reason, err)
            return None
        return Opened(wid, server, before, errors)

    def end(
        self, opened: Optional[Opened], runner, failed: str = "", err=None, notify=None
    ) -> None:
        """After the step: the snapshot after it and the difference; ``failed`` — the step was cut, no snapshot (the
        step's own result says so). A difference that could not be taken is told to the card (review 4)."""
        if opened is None:
            return
        if failed:
            self._close(opened.id, err, failed=failed)
            return
        after, errors, reason = capture(opened.server, runner)
        if not reason:
            changes = diff(opened.before, opened.errors, after, errors)
            if len(changes) > CHANGES_MAX:
                reason = f"изменений больше {CHANGES_MAX}"
        if reason:
            self._close(opened.id, err, failed=reason)
            self._unclosed(notify, opened.server, reason, err)
            return
        self._close(
            opened.id, err, changes=changes, errors=sorted(opened.errors | errors)
        )


class Off:
    """No windows (the tests of the run itself, which expect exactly the step's ssh)."""

    def begin(self, server, extra, runner, notify, run="", err=None) -> None:
        return None

    def skipped(self, server, extra, notify, reason, err=None) -> None:
        return None

    def end(self, opened, runner, failed="", err=None, notify=None) -> None:
        return None
