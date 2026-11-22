"""The command lines around the approval.

``kd-admin-propose`` runs as the sysadmin's own user: the agent is the Unix user, never an argument
or ``KD_AGENT``; it reads the card through the cabinet socket as that user and, once the plan
is stored, opens the owner's decision there: the owner approves it in the cabinet, signed.
``kd-admin-task`` runs as root on the stage only, drops to the cabinet's service user (the owner of
the grant store) and is the owner's fallback when the cabinet is down; it is never in any agent's
sudoers.
"""

import argparse
import os
import sys
from datetime import datetime, timedelta
from typing import Callable, Iterable, List, Optional, TextIO

from kd import admin_approval, admin_grant, admin_proposal, admin_task, clock, naming
from kd import admin_operations as ops
from kd.cabinet import client as cabinet_client
from kd.cabinet import config as cabinet_config

EXIT_ERROR, EXIT_REFUSED = 1, 2
GRANT_OWNER = "cabinet"  # the service user that writes the grant store (the approve user before)


class _Usage(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise _Usage(message)


def _parser(prog: str) -> "_Parser":
    """--task and exactly one source: --file <path> or --json '<text>' (one argument, so the agent needs no file)."""
    parser = _Parser(prog=prog, allow_abbrev=False, add_help=False)
    parser.add_argument("--task", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--file")
    source.add_argument("--json")
    return parser


def _read_source(args, prog: str, err: TextIO) -> Optional[bytes]:
    if args.json is not None:
        return os.fsencode(args.json)  # the argument's own bytes; strict UTF-8 JSON is checked on submit
    try:
        with open(args.file, "rb") as handle:
            return handle.read(admin_proposal.MAX_BYTES + 1)
    except OSError as exc:
        print(f"{prog}: cannot read {args.file} ({exc.__class__.__name__})", file=err)
        return None


def _ask_owner(client, kind: str, task_id: str, payload: dict, cabinet_json: str, prog: str, out: TextIO,
               err: TextIO) -> int:
    """Open the owner's decision in the cabinet (asking again returns the same open decision)."""
    try:
        answer = client.call({"op": "decision.open", "type": kind, "card": task_id, "payload": payload})
        decision = answer["decision"]
    except cabinet_client.CabinetError as exc:
        print(f"{prog}: saved, but the owner was not asked ({exc}); run the same command again", file=err)
        return EXIT_ERROR
    approved = answer.get("approved")
    if approved:  # every step is of a test environment — approved by rule, nothing waits for the owner
        print(f"approved by the test-environment rule until {approved['expires']} ({', '.join(approved['envs'])}): "
              f"run the steps now with kd-admin-run --task {task_id} --op <n>", file=out)
        return 0
    origin = cabinet_config.origin_of(cabinet_json)
    where = cabinet_config.card_url(origin, task_id) if origin else f"card {task_id}"
    print(f"the owner decides in the cabinet: {where} (decision {decision['id']})", file=out)
    return 0


def _home() -> Optional[str]:
    """The factory's own host for the plan's early check; None when the inventory names none — the target's gate,
    the boundary, runs only what the owner signed anyway (review 04.11.2026)."""
    try:
        return ops.load_home()
    except ValueError:
        return None


def propose_main(argv: List[str], *, user: str, client=None,
                 root: str = admin_proposal.ROOT, catalog: Callable[[], ops.Catalog] = ops.load_catalog,
                 inventory: Callable[[], Iterable[str]] = ops.load_inventory,
                 home: Callable[[], Optional[str]] = _home,
                 enabled: Callable[[], Iterable[str]] = ops.load_enabled,
                 now: Callable[[], datetime] = clock.utcnow, out: TextIO = sys.stdout, err: TextIO = sys.stderr,
                 cabinet_json: str = cabinet_config.JSON) -> int:
    parser = _parser("kd-admin-propose")
    try:
        args = parser.parse_args(argv)
    except _Usage as exc:
        print(f"usage: kd-admin-propose --task KD-<n> --file <plan.json> | --json '<plan>' ({exc})", file=err)
        return EXIT_REFUSED
    if user != naming.agent_user(admin_task.ASSIGNEE):
        print(f"kd-admin-propose: only {naming.agent_user(admin_task.ASSIGNEE)} may propose, not {user}", file=err)
        return EXIT_REFUSED
    raw = _read_source(args, "kd-admin-propose", err)
    if raw is None:
        return EXIT_REFUSED
    client = client if client is not None else cabinet_client.Client()
    try:
        card = cabinet_client.admin_card(client, args.task)
        proposal = admin_proposal.submit(root, raw, agent=admin_task.ASSIGNEE, card=card, catalog=catalog(),
                                         inventory=inventory(), enabled=enabled(), now=now(), home=home(),
                                         deny=sysadmin_deny(), critical=sysadmin_critical())
    except (admin_task.CardError, ops.ProposalError, ops.CatalogError) as exc:
        print(f"kd-admin-propose: refused: {exc}", file=err)
        return EXIT_REFUSED
    except (admin_proposal.ProposalStoreError, ValueError, OSError) as exc:
        print(f"kd-admin-propose: not saved: {exc}", file=err)
        return EXIT_ERROR
    print(f"saved the plan of {card.task_id}: {len(proposal.operations)} operation(s) on "
          f"{', '.join(proposal.servers)}; proposal sha256 {proposal.sha256}", file=out)
    payload = {"task_id": card.task_id, "proposal_sha256": proposal.sha256, "card_scope_sha256": card.scope_sha256}
    return _ask_owner(client, "admin_plan", card.task_id, payload, cabinet_json, "kd-admin-propose", out, err)


def propose_action_main(argv: List[str], *, user: str, deny, client=None,
                        root: str = admin_proposal.ROOT, inventory: Callable[[], Iterable[str]] = ops.load_inventory,
                        enabled: Callable[[], Iterable[str]] = ops.load_enabled, now: Callable[[], datetime] = clock.utcnow,
                        out: TextIO = sys.stdout, err: TextIO = sys.stderr,
                        cabinet_json: str = cabinet_config.JSON) -> int:
    """kd-admin-propose-action --task KD-<n> --file|--json: one exact critical command for the owner's page."""
    parser = _parser("kd-admin-propose-action")
    try:
        args = parser.parse_args(argv)
    except _Usage as exc:
        print(f"usage: kd-admin-propose-action --task KD-<n> --file <action.json> | --json '<action>' ({exc})",
              file=err)
        return EXIT_REFUSED
    if user != naming.agent_user(admin_task.ASSIGNEE):
        print(f"kd-admin-propose-action: only {naming.agent_user(admin_task.ASSIGNEE)} may propose, not {user}",
              file=err)
        return EXIT_REFUSED
    raw = _read_source(args, "kd-admin-propose-action", err)
    if raw is None:
        return EXIT_REFUSED
    client = client if client is not None else cabinet_client.Client()
    try:
        card = cabinet_client.admin_card(client, args.task)
        action = admin_proposal.submit_action(root, raw, agent=admin_task.ASSIGNEE, card=card, inventory=inventory(),
                                              enabled=enabled(), now=now(), deny=deny)
    except (admin_task.CardError, ops.ProposalError) as exc:
        print(f"kd-admin-propose-action: refused: {exc}", file=err)
        return EXIT_REFUSED
    except (admin_proposal.ProposalStoreError, ValueError, OSError) as exc:
        print(f"kd-admin-propose-action: not saved: {exc}", file=err)
        return EXIT_ERROR
    print(f"saved critical action {action.proposal_id} of {card.task_id} on {' -> '.join(action.servers)}", file=out)
    payload = {"task_id": card.task_id, "proposal_id": action.proposal_id, "proposal_sha256": action.sha256,
               "card_scope_sha256": card.scope_sha256}
    code = _ask_owner(client, "admin_critical", card.task_id, payload, cabinet_json, "kd-admin-propose-action", out,
                      err)
    if code == 0:
        print(f"after the approval run: kd-admin-critical --task {card.task_id} --action {action.proposal_id}",
              file=out)
    return code


def sysadmin_deny():
    """The sysadmin's hard denylist as (command, server) -> (rule, why) | None; a failing check denies."""
    from pathlib import Path

    agents = str(Path(__file__).resolve().parents[2] / "agents" / "sysadmin")
    if agents not in sys.path:
        sys.path.insert(0, agents)
    from sysadmin import denylist

    def deny(command, server):
        try:
            rule = denylist.check(command, server)
        except Exception:  # noqa: BLE001 - fail closed
            return ("check-failed", "the denylist check failed")
        return (rule.name, rule.why) if rule else None

    return deny


def sysadmin_critical():
    """The sysadmin's recognizer of critical commands as (command) -> group | None; a failing check is a group
    of its own, so the proposal is refused (fail closed)."""
    from pathlib import Path

    agents = str(Path(__file__).resolve().parents[2] / "agents" / "sysadmin")
    if agents not in sys.path:
        sys.path.insert(0, agents)
    from sysadmin import critical

    def classify(command):
        try:
            return critical.classify(command)
        except Exception:  # noqa: BLE001 - fail closed
            return "проверка критичного завершилась ошибкой"

    return classify


def task_main(argv: List[str], *, service: admin_approval.Service, now: Callable[[], datetime] = clock.utcnow,
              out: TextIO = sys.stdout, err: TextIO = sys.stderr) -> int:
    parser = _Parser(prog="kd-admin-task", allow_abbrev=False, add_help=False)
    sub = parser.add_subparsers(dest="action", required=True, parser_class=_Parser)
    sub.add_parser("show", allow_abbrev=False, add_help=False).add_argument("task")
    approve = sub.add_parser("approve", allow_abbrev=False, add_help=False)
    approve.add_argument("task")
    approve.add_argument("--proposal-sha256", required=True)
    approve.add_argument("--card-sha256")
    approve.add_argument("--ttl-hours", type=int, default=int(admin_grant.DEFAULT_TTL.total_seconds() // 3600))
    sub.add_parser("revoke", allow_abbrev=False, add_help=False).add_argument("task")
    try:
        args = parser.parse_args(argv)
        admin_task.check_task_id(args.task)
    except (_Usage, admin_task.CardError) as exc:
        print(f"usage: kd-admin-task show|approve|revoke KD-<n> [--proposal-sha256 <hex>] ({exc})", file=err)
        return EXIT_REFUSED
    moment = now()
    try:
        if args.action == "show":
            _show(service.view(args.task, moment), out)
            return 0
        if args.action == "approve":
            card_sha = args.card_sha256
            if card_sha is None:
                view = service.view(args.task, moment)
                if view.card is None:
                    raise admin_approval.Refused(view.problem or "card cannot be read")
                card_sha = view.card.scope_sha256
            grant = service.approve(args.task, card_sha256=card_sha, proposal_sha256=args.proposal_sha256,
                                    ttl=timedelta(hours=args.ttl_hours), via="cli", now=moment)
            print(f"{args.task}: approved, grant {grant.grant_id} until {admin_grant.fmt(grant.expires_at)}", file=out)
            return 0
        grant = service.revoke(args.task, grant_id=None, via="cli", now=moment)
        print(f"{args.task}: grant {grant.grant_id} revoked", file=out)
        return 0
    except (admin_approval.Refused, admin_approval.Conflict) as exc:
        print(f"kd-admin-task: refused: {exc}", file=err)
        return EXIT_REFUSED
    except admin_approval.Unavailable as exc:
        print(f"kd-admin-task: {exc}", file=err)
        return EXIT_ERROR


def _show(view: admin_approval.View, out: TextIO) -> None:
    card, proposal, grant = view.card, view.proposal, view.grant
    print(f"task: {view.task_id}", file=out)
    if card is not None:
        print(f"title: {card.title}\nstatus: {card.status}\nservers: {', '.join(card.servers) or '-'}", file=out)
    if view.stored is not None:
        print(f"proposed_by: {view.stored.proposed_by} at {admin_grant.fmt(view.stored.proposed_at)}", file=out)
    if proposal is not None:
        print(f"intent: {proposal.intent}\nbasis: {proposal.basis}", file=out)
        for i, op in enumerate(proposal.operations, 1):
            print(f"  {i}. {ops.describe(op)}\n     effect: {op.effect}\n     risk: {op.risk_level} — {op.risk_why}\n"
                  f"     rollback: {op.rollback}", file=out)
    if grant is None:
        print("grant: none", file=out)
    else:
        state = "usable" if view.usable else f"{grant.state}, not usable: {view.grant_problem}"
        print(f"grant: {grant.grant_id} ({state}), expires {admin_grant.fmt(grant.expires_at)}", file=out)
    if view.problem:
        print(f"problem: {view.problem}", file=out)
    if card is not None:
        print(f"card_sha256: {card.scope_sha256}", file=out)
    if proposal is not None:
        print(f"proposal_sha256: {proposal.sha256}", file=out)


def default_service(client=None) -> admin_approval.Service:
    """The stage's service: the real paths and the uids every stored file must belong to."""
    import pwd

    return admin_approval.Service(
        admin_approval.Paths(), catalog=ops.load_catalog, inventory=ops.load_inventory, enabled=ops.load_enabled,
        agent_uid=pwd.getpwnam(naming.agent_user(admin_task.ASSIGNEE)).pw_uid,
        approve_uid=pwd.getpwnam(naming.service_user(GRANT_OWNER)).pw_uid, client=client, deny=sysadmin_deny())


def drop_to_service_user() -> None:
    """Root becomes the grant store's service user (with its groups) before touching any store."""
    import pwd

    entry = pwd.getpwnam(naming.service_user(GRANT_OWNER))
    os.initgroups(entry.pw_name, entry.pw_gid)
    os.setgid(entry.pw_gid)
    os.setuid(entry.pw_uid)
    if os.getuid() == 0 or os.geteuid() == 0:
        raise SystemExit("kd-admin-task: could not leave root")
    os.environ.clear()
    os.environ.update({"HOME": "/nonexistent", "PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"})
    os.umask(0o027)
