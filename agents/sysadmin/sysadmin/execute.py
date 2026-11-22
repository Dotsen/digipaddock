"""The run of one step on the servers (the exec key): shared by kd-admin-run and kd-admin-critical.

``_execute`` runs one server, ``_execute_batch`` a list in order up to the first error with one time budget and one
notice for the group. Each run: the hard denylist (refused before SSH, no approval hint) → journal «start» (with
the step's card and grant) → ssh with the exec key (timeout 10 min, output up to 1 MB) → journal «result» → the
notice in the card's thread → the output for Claude. The journal is not written — the command does not run.

``ssh-exec`` itself (the command line) only refuses: a run without ``--task`` names the plan, as before; ``--task``
— the old free command (any command on a server of a ``shell`` step for the grant's lifetime) — is gone since
the exact text is a ``script`` step of the plan, signed by the owner and run by ``kd-admin-run``.
"""
import argparse
import json
import os
import re
import shlex
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))  # …/DigiPaddock/common — as in denylist.py
from gate.agent_ro_gate import run_limited  # noqa: E402
from kd import exec_envelope, naming  # noqa: E402
from kd import admin_operations as ops  # noqa: E402
from kd.cabinet import client as cabinet_client  # noqa: E402 — the board
from sysadmin import denylist, inventory, notices, plan_window  # noqa: E402

EXEC_HOME = naming.home(naming.service_user("exec"))
EXEC_KEY = EXEC_HOME + "/.ssh/agent_exec_ed25519"  # the key file keeps its legacy name
KNOWN_HOSTS = EXEC_HOME + "/.ssh/known_hosts"
EXEC_LOG = Path("/var/log/digipaddock/exec.jsonl")
TIMEOUT = 600
MIN_SLOT = 60  # a group: with less of the shared budget left the next server is not started
LIMIT = 1024 * 1024
AGENT_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
CHECK_FAILED = "ошибка проверки"
NO_TASK = "нет одобренной задачи: предложи план kd-admin-propose"
SHELL_GONE = ops.SHELL_GONE  # --task runs nothing; the plan's script step does
# a step with its card runs inside a plan window — the monitor takes what it changed into its baseline
WINDOWS = plan_window.Windows()


class Interrupted(Exception):
    """SIGTERM/SIGHUP/SIGINT while running: the nightly restart of the session, a dropped connection, a unit stop."""


_SIGNALS = ("SIGTERM", "SIGHUP", "SIGINT")
_state = {"armed": False, "pending": None}


def _raise_interrupted(signum, frame):
    # the first signal wins: repeated ones (systemd sends SIGTERM to the whole cgroup, sudo forwards its own) are
    # ignored, otherwise they would break the write of the outcome; the safety net is systemd's SIGKILL
    name = signal.Signals(signum).name
    for sig_name in _SIGNALS:
        if hasattr(signal, sig_name):
            signal.signal(getattr(signal, sig_name), signal.SIG_IGN)
    if _state["armed"]:
        raise Interrupted(name)
    _state["pending"] = name  # the command has already ended: the outcome is written as usual, with a note


def install_signal_handlers() -> None:
    _state.update(armed=False, pending=None)  # the process starts: until main arms it a signal is only remembered
    for name in _SIGNALS:
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _raise_interrupted)


def journal(path, **record) -> None:
    record = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), **record}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o640)
    # backslashreplace: non-UTF-8 bytes of the command (surrogates) stay visible in the journal instead of failing the write
    with os.fdopen(fd, "a", encoding="utf-8", errors="backslashreplace") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def head(output: bytes, lines: int = 20, chars: int = 2000) -> str:
    return "\n".join(output.decode("utf-8", "replace").splitlines()[:lines])[:chars]


def remote_command(server: inventory.Server, command: str) -> str:
    """The legacy remote command (no envelope): sudo on a non-root exec user. With an envelope the remote word is
    the gate's and the gate sudo's itself."""
    return f"sudo -n bash -c {shlex.quote(command)}" if server.exec_sudo else command


def _ssh(server: inventory.Server, command: str, envelope) -> tuple:
    """(ssh argv, runner kwargs) of one step: the gate word fed the envelope, or the command itself."""
    if envelope is None:
        return inventory.ssh_argv(EXEC_KEY, KNOWN_HOSTS, server.exec_user, server.host,
                                  remote_command(server, command)), {}
    return inventory.ssh_argv(EXEC_KEY, KNOWN_HOSTS, server.exec_user, server.host,
                              exec_envelope.GATE_RUN), {"feed": [envelope]}


def post_notice(level: str, key: str, params: dict, card) -> bool:
    """The executor's report in the card's thread as the exec principal; without a card nothing is posted.
    Never raises: a refused notice never fails the run it reports."""
    return bool(card) and cabinet_client.post_notice("exec", level, key, params, card=card)


def reporter(notify, card):
    """notify(level, key, params) of one run: ``notify`` with the card the run belongs to."""
    return lambda level, key, params: notify(level, key, params, card)


def main(argv=None, *, inventory_path=None, enabled_path=None, log_path=EXEC_LOG,
         runner=run_limited, notify=post_notice, out=None, err=None, clock=time.monotonic, env=None) -> int:
    """The ``ssh-exec`` command line: it only refuses (see the module docstring); ``runner``, ``notify``, ``clock``
    serve the step runs below, which kd-admin-run and kd-admin-critical call directly."""
    del runner, notify, clock, env
    out = out if out is not None else sys.stdout.buffer
    err = err if err is not None else sys.stderr
    parser = argparse.ArgumentParser(prog="ssh-exec", description="a plan step runs with kd-admin-run (KD-9238)")
    parser.add_argument("--agent", action="append", default=[])  # the wrapper puts the first one; the rest do not count
    parser.add_argument("--task")  # the free command of an approved card — refused now
    parser.add_argument("server")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    label = args.agent[0] if args.agent else "unknown"
    agent = label if AGENT_RE.fullmatch(label) else "unknown"
    group = "," in args.server or args.server == "all"  # exec_targets rejects all: the card names each server

    try:
        if group:
            servers = inventory.exec_targets(args.server, inventory_path, enabled_path or inventory.ENABLED)
        else:
            servers = [inventory.get(args.server, inventory_path, enabled_path or inventory.ENABLED)]
        command = inventory.join_command(args.command)  # an empty command is an InventoryError too
    except inventory.InventoryError as exc:
        print(f"ssh-exec: {exc}", file=err)
        return 2

    reason = SHELL_GONE if args.task is not None else NO_TASK
    try:
        journal(log_path, event="task_refused", agent=agent, task=(args.task or "")[:40],
                servers=[server.alias for server in servers], command=command, reason=reason[:500])
    except OSError:
        pass
    print(f"ssh-exec: отказ, ничего не выполнено: {reason}", file=err)
    return 2


def run_step(agent, servers, command, *, log_path, runner, notify, out, err, clock=time.monotonic,
             extra=None, envelopes=None) -> int:
    """One step on ``servers`` (one, or a group in order): what kd-admin-run and kd-admin-critical run after their
    checks. Arms the signal handling, runs ``_execute`` or ``_execute_batch``, disarms; a signal before the «start»
    record ran nothing and says so (exit 130). ``envelopes(server)`` — the step's envelope for the target's exec
    gate; None — the command itself."""
    report = reporter(notify, (extra or {}).get("task_id"))
    try:
        # pending is not reset: a signal that came while the arguments were parsed is already there, and the handler
        # has set SIG_IGN — forgetting it would mean running (for a group — starting new servers) after a request to stop
        _state["armed"] = True
        if _state["pending"]:
            raise Interrupted(_state["pending"])
        if len(servers) > 1:
            return _execute_batch(agent, servers, command, log_path, runner, report, out, err, clock, extra=extra,
                                  envelopes=envelopes)
        return _execute(agent, servers[0], command, log_path, runner, report, out, err, extra=extra,
                        envelope=envelopes(servers[0]) if envelopes is not None else None)
    except Interrupted as exc:  # a signal before the start record: nothing ran
        where = ", ".join(server.alias for server in servers)
        print(f"ssh-exec: прервано ({exc}) до запуска команды; на {where} ничего не выполнено", file=err)
        return 130
    finally:
        _state["armed"] = False


def _safe_journal(err, log_path, **record) -> bool:
    try:
        journal(log_path, **record)
        return True
    except OSError as exc:
        print(f"ssh-exec: ВНИМАНИЕ: команда выполнена, журнал не записан ({exc.__class__.__name__})", file=err)
        return False


def _check(command, alias):
    """(rule, why) or (None, None)."""
    try:
        rule = denylist.check(command, alias)
        return (rule.name, rule.why) if rule else (None, None)
    except Interrupted:
        raise
    except Exception:  # noqa: BLE001 — a failure of the prohibition check is a refusal (fail closed)
        return CHECK_FAILED, "проверка запретов завершилась ошибкой"


def _execute(agent, server, command, log_path, runner, notify, out, err, extra=None, envelope=None) -> int:
    """``extra`` — the task's fields (card, permission; for kd-admin-run the operation too) in the refusal and start
    records; ``notify(level, key, params)`` — a notice in the card's thread (``reporter``); ``envelope`` — the step's envelope
    for the target's exec gate: the remote word is then the gate's and the envelope goes on stdin."""
    extra = extra or {}
    rule_name, why = _check(command, server.alias)
    if rule_name:
        journal(log_path, event="denied", agent=agent, server=server.alias, command=command, rule=rule_name, **extra)
        notify(*notices.denied(agent, server.alias, command, rule_name))
        print(f"ssh-exec: ⛔ запрещено правилом {rule_name}: {why}. Такое делает владелец руками.", file=err)
        return 126

    run_id = uuid.uuid4().hex[:8]
    opened = WINDOWS.begin(server, extra, runner, notify, err=err)  # interrupted here: nothing ran, main says so
    try:
        journal(log_path, event="start", id=run_id, agent=agent, server=server.alias, command=command,
                envelope=envelope is not None, **extra)
    except BaseException:
        WINDOWS.end(opened, runner, failed="шаг не начат", err=err)
        raise
    started = time.monotonic()
    try:
        ssh, feeding = _ssh(server, command, envelope)
        result = runner(ssh, timeout=TIMEOUT, limit=LIMIT, kill_on_limit=False, **feeding)
    except Interrupted as exc:  # run_limited has already stopped ssh
        _state["armed"] = False
        duration = round(time.monotonic() - started, 1)
        _safe_journal(err, log_path, event="result", id=run_id, rc=None, duration_s=duration, timed_out=False,
                      truncated=False, interrupted=str(exc), head="")
        WINDOWS.end(opened, runner, failed="прервано", err=err)
        notify(*notices.interrupted(agent, server.alias, command, duration, str(exc)))
        print(f"ssh-exec: прервано ({exc}); команда на {server.alias} могла не завершиться — проверьте через ssh-ro",
              file=err)
        return 130
    _state["armed"] = False  # from here a signal is only remembered — the outcome must be written
    duration = round(time.monotonic() - started, 1)
    rc = 124 if result.timed_out else result.returncode
    logged = _safe_journal(err, log_path, event="result", id=run_id, rc=rc, duration_s=duration,
                           timed_out=result.timed_out, truncated=result.truncated,
                           interrupted=_state["pending"], head=head(result.output))
    notify(*notices.result(agent, server.alias, command, rc, duration, result.timed_out))
    WINDOWS.end(opened, runner, err=err, notify=notify)

    out.write(result.output)
    if result.truncated:
        out.write(f"\n[ssh-exec: вывод обрезан до {LIMIT // 1024} КБ]\n".encode("utf-8"))
    if result.timed_out:
        out.write(f"\n[ssh-exec: прервано по таймауту {TIMEOUT} с]\n".encode("utf-8"))
    out.flush()
    if _state["pending"]:
        return 130
    return rc if (logged or rc) else 1


# --- group ssh-exec: a,b,c — in turn, up to the first error, one notice per group ---------------------

def _names(aliases) -> str:
    return ", ".join(aliases) or "—"


def _block(out, alias: str, output: bytes = b"", rc: int = 0, notes=()) -> None:
    """The output of one server of a group: "=== server", its output, ssh-exec's notes and "(code N)" when not 0."""
    out.write(f"=== {alias}\n".encode("utf-8"))
    out.write(output)
    if output and not output.endswith(b"\n"):
        out.write(b"\n")
    for note in notes:
        out.write(f"[ssh-exec: {note}]\n".encode("utf-8"))
    if rc:
        out.write(f"(код {rc})\n".encode("utf-8"))


def _execute_batch(agent, servers, command, log_path, runner, notify, out, err, clock, extra=None,
                   envelopes=None) -> int:
    """``envelopes(server)`` — each server's envelope for its exec gate, built right before its start."""
    extra = extra or {}  # the task's fields (ssh-exec --task, kd-admin-critical) in the refusal and start records
    aliases = [server.alias for server in servers]
    batch = uuid.uuid4().hex[:8]
    for server in servers:  # the prohibitions — for every server before the first one starts: any refusal means nothing runs
        rule_name, why = _check(command, server.alias)
        if rule_name:
            journal(log_path, event="denied", agent=agent, server=server.alias, servers=aliases, batch=batch,
                    command=command, rule=rule_name, **extra)
            notify(*notices.batch_denied(agent, command, aliases, server.alias, rule_name))
            print(f"ssh-exec: ⛔ запрещено правилом {rule_name} на {server.alias}: {why}. Такое делает владелец "
                  "руками. Ни на одном сервере ничего не выполнено.", file=err)
            return 126

    deadline = clock() + TIMEOUT  # one budget for the whole group: the agent's Bash waits 11 minutes
    ok = []
    # failed/why — the server it stopped at; reason — a stop between servers or a failure of the executor
    failed = why = reason = None
    code, logged = 0, True
    for index, server in enumerate(servers, 1):
        fields = {"batch": batch, "batch_index": index, "batch_size": len(servers)}
        begun, opened = False, None
        try:
            # arm first, check after: a signal before the arming is already in pending, one after it raises Interrupted
            _state["armed"] = True
            remaining = deadline - clock()
            if _state["pending"] or remaining < MIN_SLOT:  # under a minute — not started: the timeout would cut it halfway
                _state["armed"] = False
                reason, code = (f"прервано, {_state['pending']}", 130) if _state["pending"] else ("время вышло", 124)
                break
            timeout = min(TIMEOUT, remaining)
            if remaining >= MIN_SLOT + plan_window.WINDOW_COST:
                opened = WINDOWS.begin(server, extra, runner, notify, run=batch, err=err)
                remaining = deadline - clock()  # what opening took, opened or not (review 8)
                if remaining < MIN_SLOT:
                    _state["armed"] = False
                    WINDOWS.end(opened, runner, failed="время вышло", err=err)
                    reason, code = "время вышло", 124
                    break
                # the snapshot after the step comes out of the group's budget too
                timeout = min(TIMEOUT, remaining - (plan_window.SNAPSHOT_TIMEOUT if opened is not None else 0))
            else:  # short of it: the step runs without its window, and the card hears why
                WINDOWS.skipped(server, extra, notify, "в группе осталось мало времени", err)
            envelope = envelopes(server) if envelopes is not None else None
            run_id = uuid.uuid4().hex[:8]
            started = clock()
            begun = True  # from here an interruption means "may not have completed", not "did not run"
            try:
                journal(log_path, event="start", id=run_id, agent=agent, server=server.alias, command=command,
                        envelope=envelope is not None, **fields, **extra)
            except OSError as exc:  # the journal cannot be written — the command does not run
                begun = False
                _state["armed"] = False
                WINDOWS.end(opened, runner, failed="шаг не начат", err=err)
                print(f"ssh-exec: журнал не записан ({exc.__class__.__name__}): {server.alias} и следующие "
                      "не выполнялись", file=err)
                reason, code = "журнал не пишется", 1
                break
            ssh, feeding = _ssh(server, command, envelope)
            result = runner(ssh, timeout=timeout, limit=LIMIT, kill_on_limit=False, **feeding)
            _state["armed"] = False  # from here a signal is only remembered — the outcome must be written
        except Interrupted as exc:  # run_limited has already stopped ssh
            _state["armed"] = False
            if not begun:
                reason, code = f"прервано, {exc}", 130
                break
            _safe_journal(err, log_path, event="result", id=run_id, rc=None, duration_s=round(clock() - started, 1),
                          timed_out=False, truncated=False, interrupted=str(exc), head="", **fields)
            WINDOWS.end(opened, runner, failed="прервано", err=err)
            note = f"прервано ({exc}); команда могла не завершиться — проверьте через ssh-ro"
            print(f"ssh-exec: прервано ({exc}); команда на {server.alias} могла не завершиться — проверьте через "
                  "ssh-ro", file=err)
            _block(out, server.alias, rc=130, notes=[note])
            failed, why, code = server.alias, f"прервано, {exc}", 130
            break
        except Exception as exc:  # noqa: BLE001 — a failure of the executor (ssh did not start …): the group's outcome and the notice are still due
            _state["armed"] = False
            kind, text = exc.__class__.__name__, f"{exc.__class__.__name__}: {exc}"
            reason, code = f"ошибка исполнителя: {kind}", 1
            WINDOWS.end(opened, runner, failed=reason, err=err)
            if not begun:
                print(f"ssh-exec: ошибка исполнителя ({text}): {server.alias} и следующие не выполнялись", file=err)
                break
            _safe_journal(err, log_path, event="result", id=run_id, rc=None, duration_s=round(clock() - started, 1),
                          timed_out=False, truncated=False, interrupted=None, head="", error=text[:2000], **fields)
            print(f"ssh-exec: ошибка исполнителя на {server.alias}: {text}; команда могла не выполниться или не "
                  "завершиться — проверьте через ssh-ro", file=err)
            _block(out, server.alias, rc=1, notes=[f"ошибка исполнителя ({kind})"])
            failed, why = server.alias, reason
            break
        rc = 124 if result.timed_out else result.returncode
        logged = _safe_journal(err, log_path, event="result", id=run_id, rc=rc,
                               duration_s=round(clock() - started, 1), timed_out=result.timed_out,
                               truncated=result.truncated, interrupted=_state["pending"], head=head(result.output),
                               **fields) and logged
        notes = []
        if result.truncated:
            notes.append(f"вывод обрезан до {LIMIT // 1024} КБ")
        if result.timed_out:
            notes.append(f"прервано по таймауту {round(timeout)} с")
        _block(out, server.alias, result.output, rc, notes)
        WINDOWS.end(opened, runner, err=err, notify=notify)
        if rc != 0:
            failed, why, code = server.alias, "таймаут" if result.timed_out else f"код {rc}", rc
            break
        ok.append(server.alias)
    _state["armed"] = False

    not_run = aliases[len(ok) + (1 if failed else 0):]
    try:
        journal(log_path, event="batch_result", batch=batch, ok=ok, failed=failed, not_run=not_run, reason=reason)
    except OSError as exc:
        logged = False
        print(f"ssh-exec: ВНИМАНИЕ: итог группы в журнал не записан ({exc.__class__.__name__})", file=err)
    if failed:
        notify(*notices.batch_stopped(agent, command, ok, not_run, failed, why))
        line = f"итог: остановлено на {failed} (код {code}); выполнено: {_names(ok)}; не выполнялось: {_names(not_run)}"
    elif reason:
        notify(*notices.batch_stopped(agent, command, ok, not_run, None, reason))
        line = f"итог: остановлено; выполнено: {_names(ok)}; не выполнялось ({reason}): {_names(not_run)}"
    else:
        notify(*notices.batch_ok(agent, command, aliases))
        line = f"итог: успешно {len(ok)} из {len(aliases)}"
    out.write(f"{line}\n".encode("utf-8"))
    out.flush()
    if _state["pending"]:
        return 130
    return code or (0 if logged else 1)
