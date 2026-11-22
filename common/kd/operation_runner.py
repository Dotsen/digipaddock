"""kd-operation-run — the root runner of the factory's ``agent.reinstall``.

The session host starts it as a transient unit (``digipaddock-operation-<id12>-<n>``) for each batch of the operation's
roles drained by now, with the operation id and the batch number only; the request — and the batch's roles
in it — is the host's own root file ``<id>.request.json``. The runner checks the
deployed tree is still the requested revision, waits for a release under way (``digipaddock-release.service``) to end, checks
the revision again (that release may have moved the tree: an older revision is never installed), asks the cabinet
whether the operation's grant still allows these roles (``operation.authorize``: refused or unanswered — nothing is
installed), and for each role
in order: ``new_agent.sh --verify`` — a role that matches is ``unchanged`` and is not installed —
else ``--install``, ``--check`` and ``--verify`` again; the role's slots on the session host must stay as they were.
The first role that fails stops the rest (``skipped``); its result keeps the failed stage, the batch and the
masked tail of that stage's output (every stage's output is captured, only its masked form reaches the
unit's journal — no masks, a placeholder). In the end ``session_host_setup.sh check``. The batch's result goes
atomically to ``<id>.batch<n>.json``: the host adopts it, lifts the pause of the roles that are fine and keeps a failed one paused, every one on a failed host
check.

No path, command or argument of the run comes from a caller: the agents are names the host checked, the scripts
are this tree's.

``component.install``: the request names a component and a mode; the runner maps them through the deployed
tree's own catalog (``common/coordination/factory.json``) to its fixed argv — a setup script of this tree or the
restart of a ``digipaddock-`` unit — after the same revision, release and authorization checks. It runs that argv, then the
mode's check when the script succeeded, and checks the revision again after them (a tree switched under the run is
``revision_changed``, never a success). Their exit codes and the tail of their output, masked with the gate's rules
(no masks — no output), are the batch result's ``component``; the full masked output goes to the unit's journal.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from kd.session_host import operation, protocol

DEPLOYED = Path("/opt/digipaddock")  # the link a release switches, never the tree it points at now
RELEASE_UNIT = "digipaddock-release.service"
RELEASE_WAIT = 900
POLL = 5.0
ERROR_MAX = 300
TAIL_MAX = 1500  # characters of masked output a component's result (or a failed role's) keeps
OUTPUT_MAX = 1 << 20  # bytes of a script's output read at all (masked whole, then cut)
UNMASKED = "(вывод скрыт: маски шлюза недоступны)"


class RunnerError(Exception):
    """The operation cannot be run at all (no request): nothing is written."""


def _matches(revision: str, deployed: str) -> bool:
    return bool(revision and deployed) and (deployed.startswith(revision) or revision.startswith(deployed))


def _agent(state: str, stage: str = "", error: str = "") -> dict:
    return {"state": state, "stage": stage, "error": error[:ERROR_MAX]}


def _failed(stage: str, error: str, tail: str, batch: int) -> dict:
    """A role's failure: its stage, the masked tail of that stage's output and the batch it ran in."""
    item = {**_agent("failed", stage, error), "tail": tail}
    item["batch"] = batch
    return item


def run(operation_id: str, *, state_dir: str, repo: Path, run_cmd: Optional[Callable[[list], int]],
        host_slots: Callable[[], Dict[str, int]], release_active: Callable[[], bool],
        sleep: Callable[[float], None] = time.sleep, new_agent: Optional[str] = None,
        host_setup: Optional[str] = None, batch: int,
        authorize: Optional[Callable[[str, list], str]] = None,
        capture: Optional[Callable[[list], Tuple[int, str]]] = None,
        mask: Optional[Callable[[str], Optional[str]]] = None) -> dict:
    if not protocol.HEX32.fullmatch(operation_id or ""):
        raise RunnerError("bad operation id")
    ops_dir = os.path.join(state_dir, operation.OPERATIONS)
    request = operation.read_json(os.path.join(ops_dir, f"{operation_id}.request.json"))
    if not isinstance(request, dict) or request.get("operation") != operation_id:
        raise RunnerError(f"no request for operation {operation_id}")
    # the batch the host named in the request right before it started this unit
    named = request.get("batch")
    if not isinstance(named, dict) or named.get("n") != batch:
        raise RunnerError(f"operation {operation_id} has no batch {batch} under way")
    roles, out_name = named.get("agents", ()), f"{operation_id}.batch{batch}.json"
    names = [a for a in roles if isinstance(a, str) and protocol.AGENT.fullmatch(a)]
    new_agent = new_agent or str(repo / "common" / "install" / "new_agent.sh")
    host_setup = host_setup or str(repo / "common" / "install" / "session_host_setup.sh")
    agents = {name: _agent("skipped") for name in names}
    result = {"operation": operation_id, "state": "failed", "reason": "", "agents": agents, "phase": "installing",
              "batch": batch}

    def done(state: str, reason: str = "") -> dict:
        result.update(state=state, reason=reason)
        operation.write_json(os.path.join(ops_dir, out_name), result)
        return result

    def moved() -> bool:
        try:
            deployed = (repo / "REVISION").read_text(encoding="utf-8").strip()
        except OSError:
            deployed = ""
        if _matches(str(request.get("revision", "")), deployed):
            return False
        for name in names:
            agents[name] = _agent("skipped", "", "revision_moved")
        return True

    if moved():
        return done("failed", "revision_moved")
    waited = 0.0
    while release_active():
        if waited >= RELEASE_WAIT:
            return done("failed", "release_busy")
        sleep(POLL)
        waited += POLL
    if moved():
        return done("failed", "revision_moved")
    # the host asked the cabinet before it started this unit, but a release wait may have taken minutes since:
    # the grant is asked once more right before the first install
    refused = authorize(operation_id, names) if authorize is not None else ""
    if refused:
        for name in names:
            agents[name] = _agent("skipped", "", refused)
        return done("failed", refused)
    if request.get("kind") == protocol.COMPONENT:
        result["agents"] = {}  # a component installs no role: the batch's drained roles are only paused
        state, reason, ran = _component(request, repo, capture or _capture, mask or _mask, moved)
        result["component"] = ran
        return done(state, reason)
    mask = mask or _mask

    def step(argv: list) -> Tuple[int, str]:
        # a stage's output is captured and goes to the unit's journal masked only (``_capture``)
        return capture(argv) if capture is not None else (run_cmd(argv), "")

    before = host_slots()
    for name in names:
        if step([new_agent, "--verify", name])[0] == 0:
            agents[name] = _agent("unchanged", "verify")
            continue
        for stage in ("install", "check", "verify"):
            code, out = step([new_agent, f"--{stage}", name])
            if code != 0:
                agents[name] = _failed(stage, f"new_agent.sh --{stage}: exit {code}", _tail(out, mask), batch)
                return done("failed", "install_failed")
        if host_slots().get(name) != before.get(name):
            agents[name] = _failed("slots", "the role's session host slots changed", "", batch)
            return done("failed", "install_failed")
        agents[name] = _agent("ok", "verify")
    if step([host_setup, "check"])[0] != 0:
        return done("failed", operation.HOST_CHECK_FAILED)
    return done("succeeded")


def _resolve(argv, repo: Path) -> list:
    """A catalog argv as run: a script of the deployed tree by its full path (a regular file inside the tree, never
    reached through a link of its own), or ``systemctl`` as it is."""
    if argv[0] == "systemctl":
        return list(argv)
    script = repo / argv[0]
    root = os.path.realpath(str(repo))
    real = os.path.realpath(str(script))
    if not real.startswith(root + os.sep) or os.path.islink(str(script)) or not os.path.isfile(real):
        raise ValueError(f"{argv[0]} is not a script of the deployed tree")
    return [str(script), *argv[1:]]


def _tail(text: str, mask: Callable[[str], Optional[str]]) -> str:
    masked = mask(text)
    if masked is None:
        return UNMASKED if text.strip() else ""
    return masked[-TAIL_MAX:]


def _component(request: dict, repo: Path, capture: Callable[[list], Tuple[int, str]],
               mask: Callable[[str], Optional[str]], moved: Callable[[], bool]) -> Tuple[str, str, dict]:
    """(state, reason, component) of a component's run: its argv from the deployed catalog, then its check."""
    from kd import factory_ops

    ran = {"code": None, "tail": "", "check": None, "check_tail": ""}
    try:
        catalog = factory_ops.load_catalog(str(repo / "common" / "coordination" / "factory.json"))
        mode = catalog.mode(str(request.get("component") or ""), str(request.get("mode") or ""))
        run_argv = _resolve(mode.run, repo)
        check_argv = _resolve(mode.check, repo) if mode.check else None
    except (factory_ops.CatalogError, factory_ops.ProposalError, ValueError) as exc:
        ran["tail"] = str(exc)[:ERROR_MAX]
        return "failed", "not_in_catalog", ran
    try:
        code, out = capture(run_argv)
    except OSError as exc:  # it never started: a known effect, not a lost runner
        ran["tail"] = f"{run_argv[0]}: {exc.__class__.__name__}"
        return "failed", "launch_failed", ran
    ran.update(code=code, tail=_tail(out, mask))
    if code != 0:
        return "failed", "install_failed", ran
    if check_argv is not None:
        try:
            code, out = capture(check_argv)
        except OSError as exc:
            ran["check_tail"] = f"{check_argv[0]}: {exc.__class__.__name__}"
            return "failed", "check_failed", ran
        ran.update(check=code, check_tail=_tail(out, mask))
    if moved():
        return "failed", "revision_changed", ran
    if ran["check"] not in (None, 0):
        return "failed", "check_failed", ran
    return "succeeded", "", ran


def _capture(argv: list, mask: Optional[Callable[[str], Optional[str]]] = None) -> Tuple[int, str]:
    """A fixed argv, never a shell: its exit code and its output (stdout and stderr together, read up to
    ``OUTPUT_MAX``); only the masked output goes to the unit's journal (no masks — the placeholder)."""
    proc = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          check=False)
    out = proc.stdout[-OUTPUT_MAX:].decode("utf-8", "replace")
    masked = (mask or _mask)(out)
    print(masked if masked is not None else UNMASKED, flush=True)
    return proc.returncode, out


def _mask(text: str) -> Optional[str]:
    from kd import redact

    return redact.text(text)


def _release_active() -> bool:
    out = subprocess.run(["systemctl", "show", "-p", "ActiveState", "--value", "--", RELEASE_UNIT],
                         stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False).stdout.strip()
    return out in ("active", "activating", "deactivating", "reloading")


def _authorize(operation_id: str, agents: list) -> str:
    """The cabinet's ``operation.authorize`` for this batch: "" when allowed, else why (a reason word); no answer is
    a refusal too — nothing is installed on a grant nobody could check."""
    from kd.cabinet import client as cabinet_client

    try:
        answer = cabinet_client.Client().call({"op": "operation.authorize", "operation": operation_id,
                                               "agents": list(agents)})
    except (cabinet_client.CabinetError, OSError, ValueError):
        return "authorize_unavailable"
    if not isinstance(answer, dict) or not isinstance(answer.get("allowed"), bool):
        return "authorize_unavailable"
    if answer["allowed"]:
        return ""
    reason = answer.get("reason")
    return reason if isinstance(reason, str) and protocol.REASON.fullmatch(reason) else "refused"


def _host_slots() -> Dict[str, int]:
    from kd import session_config

    try:
        return dict(session_config.load().agents)
    except (OSError, ValueError):
        return {}


def main(argv=None, *, repo: Optional[Path] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 2 or not args[1].isdigit():
        print("usage: kd-operation-run <operation-id> <batch>", file=sys.stderr)
        return 2
    repo = repo or DEPLOYED
    try:
        result = run(args[0], state_dir=operation.STATE, repo=repo, run_cmd=None, host_slots=_host_slots,
                     release_active=_release_active, batch=int(args[1]),
                     authorize=_authorize, capture=_capture)
    except (RunnerError, OSError) as exc:
        print(f"kd-operation-run: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({k: result[k] for k in ("operation", "state", "reason")}))
    return 0
