"""kd-admin-critical --task KD-<n> --action <id> — an owner-approved exact critical command, at most once.

Runs as <prefix>-exec like ssh-exec. The command and the ordered servers come from the action grant only. Before SSH,
in this order: syntax; the caller is the sysadmin's user (KD_CALLER_UID, set by its channel); the action grant (active, not expired, its hash
matching its command and servers) and the card (admin card of @sysadmin, active, the scope the owner saw); every
server enabled and in the inventory; the hard denylist on every server; then the cabinet consumes the approval over
its socket (``action.consume``: only the exec user's peer uid may, and it marks the action used under the grant
store's lock). Only an "ok" starts SSH; a refused or failed consume runs nothing. A retry after any failure
needs a new decision: the command may have run.
"""
import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))
from gate.agent_ro_gate import run_limited  # noqa: E402
from kd import admin_action, admin_approval, admin_grant, admin_signed, admin_task, exec_envelope, naming  # noqa: E402
from kd import admin_operations as ops  # noqa: E402
from kd.clock import utcnow  # noqa: E402
from kd.cabinet import client as cabinet_client  # noqa: E402 — the board
from sysadmin import execute, inventory, notices  # noqa: E402

CONSUME_TIMEOUT = 15
GRANT_OWNER = "cabinet"  # the service user that writes the grant store (the approve user before)


class Refused(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused(f"usage: kd-admin-critical --task KD-<n> --action <id> ({message})")


def _uid(user):
    import pwd

    return pwd.getpwnam(user).pw_uid


def socket_consume(task_id: str, proposal_id: str, action_sha256: str, client=None) -> dict:
    """Ask the cabinet to mark the action used: ``{"ok": True, "action_id"}`` or ``{"ok": False, "error"}``."""
    client = client if client is not None else cabinet_client.Client(timeout=CONSUME_TIMEOUT)
    try:
        reply = client.call({"op": "action.consume", "task_id": task_id, "proposal_id": proposal_id,
                             "action_sha256": action_sha256})
    except cabinet_client.CabinetError as exc:
        return {"ok": False, "error": str(exc)}
    return {"ok": True, "action_id": reply.get("action_id")}


def _envelopes(grant, card, *, inventory_path, enabled_path, grants, owner_uid) -> dict:
    """one envelope per server of the signed action, all built before the approval is consumed;
    from the cabinet's copy in the grant store — the exec user never reads the agent's proposals."""
    stored = admin_signed.load_action(grants, grant.task_id, grant.proposal_id, owner_uid)
    action = admin_action.validate_action(stored.data, ops.load_inventory(Path(inventory_path or inventory.INVENTORY)),
                                          ops.load_enabled(Path(enabled_path)), card_servers=card.servers)
    if admin_action.binding_sha(action.task_id, action.servers, action.command) != grant.action_sha256:
        raise Refused(f"{grant.task_id}: the kept action {grant.proposal_id} is not the approved command and servers")
    return {alias: exec_envelope.build_action(grant, action.as_dict(), alias, grant.command) for alias in grant.servers}


def main(argv=None, *, env=None, client=None, grants=admin_approval.GRANTS,
         inventory_path=None, enabled_path=None, log_path=execute.EXEC_LOG, runner=run_limited,
         notify=execute.post_notice, out=None, err=None, now=utcnow, agent_uid=None, approve_uid=-1,
         consume=socket_consume, clock=time.monotonic, envelopes=None) -> int:
    """``envelopes`` — send the command to each target's exec gate as an envelope; None reads the host's
    switch."""
    out = out if out is not None else sys.stdout.buffer
    err = err if err is not None else sys.stderr
    env = os.environ if env is None else env
    enabled_path = enabled_path or inventory.ENABLED
    parser = _Parser(prog="kd-admin-critical", allow_abbrev=False, add_help=False)
    parser.add_argument("--agent", action="append", default=[])
    parser.add_argument("--task", required=True)
    parser.add_argument("--action", required=True)
    agent, task_id, proposal_id = "unknown", None, None
    try:
        args = parser.parse_args(argv)
        label = args.agent[0] if args.agent else "unknown"
        agent = label if execute.AGENT_RE.fullmatch(label) else "unknown"
        task_id = admin_task.check_task_id(args.task)
        proposal_id = admin_action.check_proposal_id(args.action)
    except (Refused, admin_task.CardError, admin_grant.GrantError) as exc:
        print(f"kd-admin-critical: {exc}", file=err)
        return 2

    try:
        expected = agent_uid if agent_uid is not None else _uid(naming.agent_user(admin_task.ASSIGNEE))
        # the exec channel's service sets KD_CALLER_UID from the peer credentials of the socket.
        if env.get("KD_CALLER_UID") != str(expected):
            raise Refused(f"run it as the sysadmin ({naming.agent_user(admin_task.ASSIGNEE)}) through its channel")
        owner = _uid(naming.service_user(GRANT_OWNER)) if approve_uid == -1 else approve_uid
        grant = admin_action.ActionStore(grants, owner).load(task_id, proposal_id)
        if grant is None:
            raise Refused(f"{task_id}: action {proposal_id} has no owner's approval; it waits for his decision in the cabinet")
        if grant.state != "active":
            raise Refused(f"{task_id}: action {proposal_id} is {grant.state}; a new approval is needed")
        if now() >= grant.expires_at:
            raise Refused(f"{task_id}: the approval of action {proposal_id} expired at "
                          f"{admin_grant.fmt(grant.expires_at)}; a new approval is needed")
        card = cabinet_client.admin_card(client if client is not None else cabinet_client.Client(), task_id)
        admin_task.check_admin(card)
        admin_task.check_active(card)
        if card.scope_sha256 != grant.card_scope_sha256:
            raise Refused(f"{task_id}: the card's scope changed since the owner's approval")
        servers = [inventory.get(alias, inventory_path, enabled_path) for alias in grant.servers]
        missing = [alias for alias in grant.servers if alias not in card.servers]
        if missing:
            raise Refused(f"{', '.join(missing)} not a server of {task_id}")
        built = None
        if exec_envelope.enabled() if envelopes is None else envelopes:
            built = _envelopes(grant, card, inventory_path=inventory_path, enabled_path=enabled_path,
                               grants=grants, owner_uid=owner)
    except (Refused, admin_grant.GrantError, admin_task.CardError, inventory.InventoryError, ValueError, KeyError,
            OSError) as exc:
        reason = str(exc) or exc.__class__.__name__
        try:
            execute.journal(log_path, event="admin_refused", kind="critical", agent=agent, task_id=task_id,
                            proposal_id=proposal_id, reason=reason[:500])
        except OSError:
            pass
        print(f"kd-admin-critical: refused, nothing ran: {reason}", file=err)
        return 2

    extra = {"kind": "critical", "task_id": task_id, "proposal_id": proposal_id, "action_id": grant.action_id}
    report = execute.reporter(notify, task_id)  # the grant's card
    for server in servers:  # the denylist on every server before the approval is used
        rule_name, why = execute._check(grant.command, server.alias)
        if rule_name:
            execute.journal(log_path, event="denied", agent=agent, server=server.alias, command=grant.command,
                            rule=rule_name, **extra)
            report(*notices.denied(agent, server.alias, grant.command, rule_name))
            print(f"kd-admin-critical: ⛔ denied by rule {rule_name} on {server.alias}: {why}. The owner does this "
                  "by hand. Nothing ran; the approval is unused.", file=err)
            return 126

    try:
        reply = consume(task_id, proposal_id, grant.action_sha256)
    except (OSError, ValueError) as exc:
        reply = {"ok": False, "error": f"approval service unreachable ({exc.__class__.__name__})"}
    if not reply.get("ok") or reply.get("action_id") != grant.action_id:
        reason = str(reply.get("error") or "the approval service did not confirm this action")[:300]
        try:
            execute.journal(log_path, event="admin_refused", kind="critical", agent=agent, task_id=task_id,
                            proposal_id=proposal_id, reason=f"consume: {reason}")
        except OSError:
            pass
        print(f"kd-admin-critical: refused, nothing ran: {reason}", file=err)
        return 2

    out.write(f"=== {task_id} critical action {proposal_id} on {' -> '.join(grant.servers)} (approval used)\n"
              .encode("utf-8"))
    try:
        execute._state["armed"] = True
        if execute._state["pending"]:
            raise execute.Interrupted(execute._state["pending"])
        if len(servers) == 1:
            return execute._execute(agent, servers[0], grant.command, log_path, runner, report, out, err, extra=extra,
                                    envelope=built[servers[0].alias] if built else None)
        return execute._execute_batch(agent, servers, grant.command, log_path, runner, report, out, err, clock,
                                      extra=extra, envelopes=(lambda server: built[server.alias]) if built else None)
    except execute.Interrupted as exc:
        print(f"kd-admin-critical: interrupted ({exc}) before the start; nothing ran, but the approval is used",
              file=err)
        return 130
    finally:
        execute._state["armed"] = False
