"""kd-admin-run --task KD-<n> --op <i> — one operation of the owner's grant for an admin card.

Runs as <prefix>-exec (wrapper kd-admin-run -> the sysadmin's exec channel), like ssh-exec. The agent names
only the card and the operation's number: server, unit, packages and the remote command all come from the stored
grant, never from arguments. Before SSH, in this order: syntax; the caller is the sysadmin's user (KD_CALLER_UID, set by
the channel — --agent and KD_AGENT only label the journal); the grant store and the card are read safely; the grant is
usable (active, not expired, same card scope, admin card of @sysadmin in To Do or In Progress); the operation is
re-validated against the current catalog and the enabled inventory; the hard denylist checks the formed command;
then the journal "start" (with task, grant and operation) — any failure up to here and SSH never starts. The run
itself — exec key, time and output limits, signals, journal "result", the notice in the card's thread — is
ssh-exec's own (execute._execute).
"""
import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))
from gate.agent_ro_gate import run_limited  # noqa: E402
from kd import admin_approval, admin_grant, admin_signed, admin_task, exec_envelope, naming  # noqa: E402
from kd.clock import utcnow  # noqa: E402
from kd.cabinet import client as cabinet_client  # noqa: E402 — the board
from kd import admin_operations as ops  # noqa: E402
from sysadmin import execute, inventory  # noqa: E402

OP = re.compile(r"[1-9][0-9]?")
AGENT_RE = execute.AGENT_RE


class Refused(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused(f"usage: kd-admin-run --task KD-<n> --op <number> ({message})")


def _uid(user):
    import pwd

    return pwd.getpwnam(user).pw_uid


def _envelope(grant, op, op_index, command, *, card, catalog, inventory_data, enabled, grants, owner_uid):
    """the step's envelope for the target's exec gate — the plan must be the one the owner signed;
    read by the grant's hash, so a newer proposal on the card does not strand a live grant's steps;
    from the cabinet's copy in the grant store — the exec user never reads the agent's proposals."""
    stored = admin_signed.load_plan(grants, grant.task_id, grant.proposal_sha256, owner_uid)
    plan = ops.validate_proposal(stored.data, catalog, inventory_data, enabled, card_servers=card.servers)
    if plan.sha256 != grant.proposal_sha256:
        raise Refused(f"{grant.task_id}: the stored proposal is not the approved one; the owner approved another plan")
    return exec_envelope.build_plan(grant, plan.as_dict(), op_index, op.server, command)


STEP_TIMEOUT = 20


def _check_previous(grant, envelope, op_index, runner, inventory_path, enabled_path) -> None:
    """A signed plan runs in order: step N only after step N-1 succeeded. A target's gate journals the steps it ran
    and keeps the order of its own; when step N-1 ran on another server, its gate answers whether it succeeded
    there (``exec_envelope.STEP_RUN``) — anything but its "ok" refuses (fail closed). An unsigned grant (the test
    zone's rule, a grant before the signature named its decision) keeps no journal there: nothing to ask."""
    if op_index == 1 or grant.approved_by == admin_grant.TEST_ZONE:
        return
    data = json.loads(envelope)
    previous = admin_grant.operation_at(grant, op_index - 1)["server"]
    key = exec_envelope.plan_key(data["decision"]["id"], data["decision"]["nonce"],
                                 data["payload"]["proposal_sha256"], op_index - 1)
    if previous == data["server"] or not key:
        return
    server = inventory.get(previous, inventory_path, enabled_path)
    ssh = inventory.ssh_argv(execute.EXEC_KEY, execute.KNOWN_HOSTS, server.exec_user, server.host,
                             f"{exec_envelope.STEP_RUN} {key}")
    try:
        result = runner(ssh, timeout=STEP_TIMEOUT, limit=4096, kill_on_limit=True)
        confirmed = not result.timed_out and result.returncode == 0 and result.output.split() == [b"ok"]
    except Exception:  # noqa: BLE001 — ssh did not start: nothing confirms the step
        confirmed = False
    if not confirmed:
        raise Refused(f"{grant.task_id}: operation {op_index - 1} on {previous} has not succeeded (its gate does not "
                      f"confirm it); run it first — a failed operation may be run again")


def main(argv=None, *, env=None, client=None, grants=admin_approval.GRANTS,
         inventory_path=None, enabled_path=None, catalog_path=None,
         log_path=execute.EXEC_LOG, runner=run_limited, notify=execute.post_notice, out=None, err=None, now=utcnow,
         agent_uid=None, approve_uid=-1, envelopes=None) -> int:
    """``envelopes`` — send the step to the target's exec gate as an envelope; None reads the host's
    switch (``exec_envelope.enabled``)."""
    out = out if out is not None else sys.stdout.buffer
    err = err if err is not None else sys.stderr
    env = os.environ if env is None else env
    enabled_path = enabled_path or inventory.ENABLED
    parser = _Parser(prog="kd-admin-run", allow_abbrev=False, add_help=False)
    parser.add_argument("--agent", action="append", default=[])  # set by the wrapper; a label only
    parser.add_argument("--task", required=True)
    parser.add_argument("--op", required=True)
    task_id = op_index = None
    try:
        args = parser.parse_args(argv)
        label = args.agent[0] if args.agent else "unknown"
        agent = label if AGENT_RE.fullmatch(label) else "unknown"
        task_id = admin_task.check_task_id(args.task)
        if not OP.fullmatch(args.op) or int(args.op) > ops.MAX_OPERATIONS:
            raise Refused(f"usage: --op is the operation's number in the approved plan, 1..{ops.MAX_OPERATIONS}")
        op_index = int(args.op)
    except (Refused, admin_task.CardError) as exc:
        print(f"kd-admin-run: {exc}", file=err)
        return 2

    try:
        expected = agent_uid if agent_uid is not None else _uid(naming.agent_user(admin_task.ASSIGNEE))
        # the exec channel's service sets KD_CALLER_UID from the peer credentials of the socket.
        if env.get("KD_CALLER_UID") != str(expected):
            raise Refused(f"run it as the sysadmin ({naming.agent_user(admin_task.ASSIGNEE)}) through its channel")
        owner = _uid(naming.service_user("cabinet")) if approve_uid == -1 else approve_uid  # the grant store's writer
        grant = admin_grant.Store(grants, owner).load(task_id)
        if grant is None:
            raise Refused(f"{task_id} has no owner's approval; it waits for his decision in the cabinet")
        card = cabinet_client.admin_card(client if client is not None else cabinet_client.Client(), task_id)
        admin_grant.check_usable(grant, card, now())
        catalog = ops.load_catalog(catalog_path)
        enabled = ops.load_enabled(Path(enabled_path))
        inventory_data = ops.load_inventory(Path(inventory_path or inventory.INVENTORY))
        op = ops.validate_operation(admin_grant.operation_at(grant, op_index), catalog, inventory_data, enabled)
        # The proposal refuses a script, a cron line or a file drop on the factory's own host at propose time; the
        # executor refuses it again here — a grant may hold one by another path (load_home fails closed: ValueError)
        if op.verb in ops.HOME_VERBS and op.server == ops.load_home(Path(inventory_path or inventory.INVENTORY)):
            raise Refused(ops.HOME_SCRIPT.format(home=op.server))
        if op.server not in card.servers:
            raise Refused(f"{op.server} is not a server of {task_id}")
        # an approval by the test-zone rule covers a test environment's steps and nothing else — read from
        # the catalog of this moment, so a mark taken off (or a grant that names the rule by another path) stops here
        if grant.approved_by == admin_grant.TEST_ZONE and ops.test_env_of(op, catalog) is None:
            raise Refused(f"{task_id}: operation {op_index} is no longer a test environment's step; propose the plan "
                          "again for the owner's signature")
        server = inventory.get(op.server, inventory_path, enabled_path)
        command = ops.remote_command(op, catalog)
        envelope = None
        if exec_envelope.enabled() if envelopes is None else envelopes:
            envelope = _envelope(grant, op, op_index, command, card=card, catalog=catalog,
                                 inventory_data=inventory_data, enabled=enabled, grants=grants, owner_uid=owner)
            _check_previous(grant, envelope, op_index, runner, inventory_path, enabled_path)
    except (Refused, admin_grant.GrantError, admin_task.CardError, ops.ProposalError, ops.CatalogError,
            inventory.InventoryError, ValueError, KeyError, OSError) as exc:
        reason = str(exc) or exc.__class__.__name__
        try:
            execute.journal(log_path, event="admin_refused", agent=agent, task_id=task_id, op_index=op_index,
                            reason=reason[:500])
        except OSError:
            pass
        print(f"kd-admin-run: refused, nothing ran: {reason}", file=err)
        return 2

    extra = {"kind": "admin", "task_id": task_id, "grant_id": grant.grant_id, "op_index": op_index,
             "op_count": len(grant.operations), "operation": op.as_dict()}
    if grant.approved_by == admin_grant.TEST_ZONE:
        extra["test_zone"] = True  # no owner's signature: its step hands nothing to the monitor's baseline
    out.write(f"=== {task_id} {op_index}/{len(grant.operations)}: {ops.describe(op)}\n".encode("utf-8"))
    try:
        execute._state["armed"] = True
        if execute._state["pending"]:
            raise execute.Interrupted(execute._state["pending"])
        return execute._execute(agent, server, command, log_path, runner, execute.reporter(notify, task_id), out,
                                err, extra=extra, envelope=envelope)
    except execute.Interrupted as exc:
        print(f"kd-admin-run: interrupted ({exc}) before the start; nothing ran on {server.alias}", file=err)
        return 130
    finally:
        execute._state["armed"] = False
