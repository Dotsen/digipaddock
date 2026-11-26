"""kd-factory — the developer's factory operations.

    kd-factory plan KD-<n> --operations '<JSON>' --intent T --basis T   ask the owner to approve factory steps
    kd-factory reinstall KD-<n> --agents a[,b] --key <key>               request an approved agent.reinstall
    kd-factory install KD-<n> --component <c> --mode <m> --key <key>     request an approved component.install
    kd-factory status <operation-id>                                     its state, phase and per-agent result

A board executor of the cabinet under the base user, reached through the agent's base capability channel like
kd-diag: the channel names the caller (``KD_AGENT``, ``KD_CALLER_USER``), nothing the caller passes does. The steps
are the factory catalog's (``common/coordination/factory.json``): ``agent.reinstall`` of generated agents on the
factory host and ``component.install`` of a catalog component in one of its fixed modes (the plan's step
is signed for the deployed revision), nothing else — no command, path, server or service travels with a request. ``plan`` opens the
schema-2 plan decision on the caller's own card (the owner signs it in the cabinet); ``reinstall`` asks for what the
card's grant covers, idempotently by ``--key`` (the same key and payload answers the same operation), and prints the
operation id with the line that ends the turn waiting for it. The session host drains the role, installs, checks
and resumes it; the result comes back through the pipeline's wait, not through this call.

Exit codes: 0 done, 1 error (the cabinet), 2 refused or invalid, 3 conflict.
"""

import argparse
import json
import re
import sys

from kd import agents, ids
from kd.cabinet.client import CabinetError

CARD = ids.CARD
OPERATION_ID = re.compile(r"[0-9a-f]{32}")
KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,95}")
TEXT_MAX = 2000
EXIT_ERROR, EXIT_REFUSED, EXIT_CONFLICT = 1, 2, 3
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
FINISH = ('end the turn waiting for it: kd-work finish {card} --outcome wait_dependency --operation {id} '
          '--note "<what waits for the {what}>"')


class Refused(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Refused(EXIT_REFUSED, message)


def _card(value):
    if not CARD.fullmatch(value):
        raise argparse.ArgumentTypeError("a card is KD-<n>")
    return value


def _text(value):
    if not value.strip() or len(value) > TEXT_MAX or "\x00" in value:
        raise argparse.ArgumentTypeError(f"text of 1 to {TEXT_MAX} characters")
    return value.strip()


def _agents(value):
    names = [v for v in value.split(",") if v]
    if not names or not all(agents.NAME_RE.fullmatch(v) for v in names):
        raise argparse.ArgumentTypeError("--agents is a comma list of agent names")
    return names


def _key(value):
    if not KEY.fullmatch(value):
        raise argparse.ArgumentTypeError("--key is 1 to 96 letters, digits or :._-")
    return value


def _name(value):
    if not NAME.fullmatch(value):
        raise argparse.ArgumentTypeError("a component or a mode is a name of the factory catalog")
    return value


def _operation(value):
    if not OPERATION_ID.fullmatch(value):
        raise argparse.ArgumentTypeError("an operation id is 32 hex characters")
    return value


def parse(argv):
    parser = Parser(prog="kd-factory", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    plan = commands.add_parser("plan")
    plan.add_argument("card", type=_card)
    plan.add_argument("--operations", required=True)
    plan.add_argument("--intent", required=True, type=_text)
    plan.add_argument("--basis", required=True, type=_text)
    reinstall = commands.add_parser("reinstall")
    reinstall.add_argument("card", type=_card)
    reinstall.add_argument("--agents", required=True, type=_agents)
    reinstall.add_argument("--key", required=True, type=_key)
    install = commands.add_parser("install")
    install.add_argument("card", type=_card)
    install.add_argument("--component", required=True, type=_name)
    install.add_argument("--mode", required=True, type=_name)
    install.add_argument("--key", required=True, type=_key)
    commands.add_parser("status").add_argument("id", type=_operation)
    return parser.parse_args(argv)


def caller(environ, registry=agents.REGISTRY) -> str:
    """The channel's agent, when the peer is that agent's own user."""
    agent, user = environ.get("KD_AGENT", ""), environ.get("KD_CALLER_USER", "")
    if not agents.NAME_RE.fullmatch(agent) or not user:
        raise Refused(EXIT_REFUSED, "no capability channel names the caller")
    try:
        own = agents.user_of(agent, registry)
    except ValueError as exc:
        raise Refused(EXIT_REFUSED, str(exc))
    if own == agents.DEFAULT_USER or own != user:
        raise Refused(EXIT_REFUSED, "the caller is not the agent's own user")
    return agent


def _call(client, request):
    try:
        return client.call(request)
    except CabinetError as exc:
        if exc.kind == "conflict":
            raise Refused(EXIT_CONFLICT, str(exc)) from exc
        if exc.kind in ("refused", "invalid", "not_found"):
            raise Refused(EXIT_REFUSED, str(exc)) from exc
        raise Refused(EXIT_ERROR, f"the cabinet is unavailable ({exc})") from exc
    except OSError as exc:
        raise Refused(EXIT_ERROR, f"the cabinet is unavailable ({exc.__class__.__name__})") from exc


def _short(revision: str) -> str:
    """A release tree as printed: each commit (the code's, then the overlay's) cut to 12 characters."""
    return "+".join(part[:12] for part in str(revision).split("+"))


def _show(op, out):
    params = op.get("params") or {}
    what = (f"component {params.get('component', '')} {params.get('mode', '')}" if op.get("kind") == "component.install"
            else f"agents {','.join(op['agents'])}")
    out.write(f"operation {op['id']} {op['state']} phase {op['phase'] or '-'} {what} "
              f"revision {_short(op['revision'])}{' error ' + op['error'] if op['error'] else ''}\n")
    for name, item in sorted((op.get("result") or {}).get("agents", {}).items()):
        out.write(f"  {name}: {item['state']} {item['stage'] or '-'}{' ' + item['error'] if item['error'] else ''}\n")
    ran = (op.get("result") or {}).get("component")
    if ran:
        for label, code, tail in (("run", ran.get("code"), ran.get("tail")),
                                  ("check", ran.get("check"), ran.get("check_tail"))):
            out.write(f"  {label}: exit {'-' if code is None else code}\n")
            for line in (tail or "").splitlines()[-20:]:
                out.write(f"    {line}\n")


def main(argv=None, *, environ, client, registry=agents.REGISTRY, out=sys.stdout, err=sys.stderr) -> int:
    try:
        args = parse(list(sys.argv[1:] if argv is None else argv))
        agent = caller(environ, registry)
        if args.command == "plan":
            try:
                steps = json.loads(args.operations)
            except ValueError:
                raise Refused(EXIT_REFUSED, "--operations must be a JSON list of steps")
            proposal = {"schema_version": 2, "task_id": args.card, "intent": args.intent, "basis": args.basis,
                        "operations": steps}
            found = _call(client, {"op": "decision.open", "agent": agent, "type": "admin_plan", "card": args.card,
                                   "payload": {"policy": "factory", "proposal": proposal}})["decision"]
            out.write(f"plan {found['id']} {found['state']} — the owner approves it in the cabinet; end the turn "
                      f"waiting for it: kd-work finish {args.card} --outcome wait_user --note \"…\"\n")
            return 0
        if args.command == "reinstall":
            found = _call(client, {"op": "operation.request", "agent": agent, "card": args.card,
                                   "kind": "agent.reinstall", "agents": args.agents, "key": args.key})["operation"]
            out.write(f"operation {found['id']} {found['state']}\n")
            if found["state"] in ("pending", "running"):
                out.write(FINISH.format(card=args.card, id=found["id"], what="reinstall") + "\n")
            return 0
        if args.command == "install":
            found = _call(client, {"op": "operation.request", "agent": agent, "card": args.card,
                                   "kind": "component.install", "component": args.component, "mode": args.mode,
                                   "key": args.key})["operation"]
            out.write(f"operation {found['id']} {found['state']} {args.component} {args.mode} "
                      f"revision {_short(found['revision'])}\n")
            if found["state"] in ("pending", "running"):
                out.write(FINISH.format(card=args.card, id=found["id"], what="install") + "\n")
            return 0
        answer = _call(client, {"op": "operation.get", "agent": agent, "id": args.id})
        _show(answer["operation"], out)
        if answer.get("current"):
            out.write(f"superseded by {answer['current']['id']}:\n")
            _show(answer["current"], out)
        return 0
    except Refused as exc:
        err.write(f"kd-factory: {exc}\n")
        return exc.code
