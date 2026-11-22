"""The release's request to the factory: kd-release runs this with the code it has
just switched to, as root, before it publishes its result.

The roles are the installed generated agents whose install differs from what this code would install
(``install_apply.outdated_agents`` — a checked set, never "everyone because the postflight failed"). They are asked
for as root (``operation.request``, key ``release:<rev>``), on the card of the released PR when it names one: the
owner signed the release, and its roles run its code only once reinstalled, so no factory grant of the card's is
asked for (a card without one left them ``uncovered`` and failed every such release). A cabinet that refuses
the request leaves them ``uncovered``, named, for the owner to reinstall. The request goes before the
cabinet itself is updated: an answer of the old code (unavailable, an op it does not serve) is ``deferred`` with the
request's key and ``retry`` — kd-release asks once more after the cabinet's update, and the lead waits on the key
meanwhile (the operation's id cannot reach it in the result it already read).

    python3 -I -c '…release_reinstall.main…' <tree> <revision> [<card>] [--release-id <id>]
prints the outcome as one JSON line. a release of code + overlay keys its requests by its release id
``<rev>+<deploy>`` (``release:<id>``, ``release:<id>:<component>:<mode>``); the revision asked for stays the code's.
``--by-root`` (root's release by hand) is taken and changes nothing since the change. Roles left uncovered are a ``warn``
notice on the card (or in the feed), not only a line of result.json: the release stands, the roles run the old text
until reinstalled.

The long-running session host runs the code of the release it started from and pins it (``pin_host``:
``<base>/HOST``, so release_switch.sh keeps that directory). ``host_main <tree> <release id>`` — after the
cabinet's update — asks the factory, as root, to restart the host (``component.install session-host restart``, key
``release:<id>:host-restart``) when a file the host runs (``host_paths``) differs from the switched tree. The key is
never a card's release step's (``release:<id>:session-host:restart``): the same key with another payload is
refused; under its own key the request joins that step's running operation.
"""

import ast
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Callable, List, Optional

from kd.cabinet.client import CabinetError

KIND = "agent.reinstall"
COMPONENT = "component.install"
SOURCE, UNCOVERED = "release", "notice.release.reinstall_uncovered"
WHY_MAX = 300
UNKNOWN_OP = "unknown operation"  # api.dispatch's answer to an op the running cabinet does not serve
RELEASE_ID = re.compile(r"[0-9a-f]{7,40}(?:\+[0-9a-f]{7,40})?")
STAMP = re.compile(r"[0-9]{1,12}(?:\.[0-9]{1,6})?")  # a rollback's time, its restart's own key
HOST_PIN = "HOST"  # in the release base: the release directory the running session host loaded
# What the host runs from its release directory: its command and package, every ``kd`` module they import, at any
# depth (``host_paths``), and the files those read beside the code; a change in any of them restarts it
HOST_ROOTS = ("common/bin/kd-session-host", "common/kd/session_host")
# the cabinet's own modules, imported by its client for their constants: the cabinet restarts on them, the host
# only talks to it
HOST_STOP = frozenset({"kd.cabinet.api", "kd.cabinet.server"})
HOST_DATA = ("common/naming.default.json", "common/worker", "common/specializations",
             "agents/sysadmin/gate/agent_ro_gate.py", "host/host.json")


def reinstall(repo: Path, revision: str, card: str, *, outdated: Callable[[Path], List[str]], client,
              release_id: Optional[str] = None) -> dict:
    """``{"state": "none"}`` | ``{"state": "pending"|<the operation's state>, "operation", "agents"}`` |
    ``{"state": "uncovered", "agents", "why"}`` | ``{"state": "deferred", "key", "agents", "why", "retry": True}``;
    with a card, ``"components"`` : the component steps approved on revision ``release``, each asked
    for on the released revision — ``{"component", "mode", "operation", "state"}`` or
    ``{"component", "mode", "state": "uncovered", "why"}``."""
    names = sorted(outdated(repo))
    key = f"release:{release_id or revision}"
    roles = _roles(revision, key, card, names, client) if names else {"state": "none"}
    if not card or roles.get("retry"):
        return {**roles, "components": []}
    steps = _release_steps(card, client)
    if steps is None:  # the old cabinet does not serve the op: kd-release asks again after the update
        if roles["state"] == "none":
            return {"state": "deferred", "key": key, "agents": [], "components": [],
                    "why": "the cabinet does not serve operation.release_steps yet", "retry": True}
        return {**roles, "components": []}
    return {**roles, "components": [_component(revision, key, card, step, client) for step in steps]}


def _release_steps(card: str, client) -> Optional[list]:
    try:
        return list(client.call({"op": "operation.release_steps", "card": card})["steps"])
    except CabinetError as exc:
        if exc.kind == "unavailable" or (exc.kind == "invalid" and str(exc) == UNKNOWN_OP):
            return None
        return []
    except (OSError, KeyError, TypeError):
        return None


def _component(revision: str, key: str, card: str, step: dict, client) -> dict:
    component, mode = str(step.get("component") or ""), str(step.get("mode") or "")
    out = {"component": component, "mode": mode}
    try:
        found = client.call({"op": "operation.request", "requester": "release", "card": card, "kind": COMPONENT,
                             "component": component, "mode": mode,
                             "key": f"{key}:{component}:{mode}", "revision": revision})["operation"]
    except CabinetError as exc:
        return {**out, "state": "uncovered", "why": str(exc)[:WHY_MAX]}
    except (OSError, KeyError, TypeError) as exc:
        return {**out, "state": "uncovered", "why": f"the cabinet is unavailable ({exc.__class__.__name__})"}
    return {**out, "operation": found["id"], "state": found["state"]}


def _roles(revision: str, key: str, card: str, names: List[str], client) -> dict:
    request = {"op": "operation.request", "requester": "root", **({"card": card} if card else {})}
    try:
        found = client.call({**request, "kind": KIND, "agents": names, "key": key,
                             "revision": revision})["operation"]
    except CabinetError as exc:
        if exc.kind == "unavailable" or (exc.kind == "invalid" and str(exc) == UNKNOWN_OP):
            # the cabinet still runs the old code: kd-release asks again after its update
            return {"state": "deferred", "key": key, "agents": names, "why": str(exc)[:WHY_MAX], "retry": True}
        return {"state": "uncovered", "agents": names, "why": str(exc)[:WHY_MAX]}
    except (OSError, KeyError, TypeError) as exc:
        return {"state": "deferred", "key": key, "agents": names,
                "why": f"the cabinet is unavailable ({exc.__class__.__name__})", "retry": True}
    return {"state": found["state"], "operation": found["id"], "agents": names}


def pin_host(release: Path) -> bool:
    """The running host's release: ``<base>/HOST`` names the release directory the host loaded its code
    from, so release_switch.sh never prunes it under the host; only a release (a directory with ``REVISION``)."""
    release = Path(release)
    if not (release / "REVISION").is_file():
        return False
    from kd import atomic_file

    atomic_file.replace(str(release.parent / HOST_PIN), f"{release}\n".encode("utf-8"), 0o644)
    return True


def pinned_release(target: Path) -> Optional[Path]:
    """The release directory the running host pinned, beside ``target`` (the release switched to), or None."""
    try:
        text = (Path(target).parent / HOST_PIN).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return Path(text) if text else None


def _digest(root: Path, rel: str) -> dict:
    path = root / rel
    if path.is_dir():
        files = sorted(p for p in path.rglob("*") if p.is_file() and "__pycache__" not in p.parts)
    else:
        files = [path] if path.is_file() else []
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def _imports(path: Path, late: bool) -> set:
    """The ``kd`` names a file imports when it loads (``late``: in its functions too); ``from a import b`` gives
    ``a`` and ``a.b``, whichever of them is a module."""
    try:
        tree = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return set()
    names, nodes = set(), list(ast.walk(tree)) if late else list(tree.body)
    while nodes:
        node = nodes.pop()
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif not late and isinstance(node, (ast.If, ast.Try)):
            nodes += node.body + node.orelse + getattr(node, "finalbody", [])
            nodes += [n for handler in getattr(node, "handlers", []) for n in handler.body]
    return {name for name in names if name == "kd" or name.startswith("kd.")}


def _module(root: Path, name: str) -> Optional[Path]:
    base = root.joinpath("common", *name.split("."))
    for path in (base / "__init__.py", base.with_suffix(".py")):
        if path.is_file():
            return path
    return None


def host_paths(root: Path) -> List[str]:
    """The files of ``root`` the host runs or reads: ``HOST_ROOTS`` with every ``kd`` module they import, the
    modules those load with them, short of ``HOST_STOP`` (a deeper function's late import is code the host never
    calls: the notifications users.py loads for the cabinet), and ``HOST_DATA``."""
    root = Path(root)
    todo = []
    for rel in HOST_ROOTS:
        path = root / rel
        todo += sorted(path.rglob("*.py")) if path.is_dir() else [path] if path.is_file() else []
    roots = set(todo)
    seen = set(todo)
    while todo:
        path = todo.pop()
        for name in sorted(_imports(path, late=path in roots)):
            found = None if name in HOST_STOP else _module(root, name)
            if found is not None and found not in seen:
                seen.add(found)
                todo.append(found)
    return sorted({p.relative_to(root).as_posix() for p in seen} | set(HOST_DATA))


def host_changed(pinned: Path, target: Path) -> bool:
    """Any file the host runs (``host_paths`` of either tree) differs between the host's release and ``target``
    (a gone release has changed)."""
    pinned, target = Path(pinned), Path(target)
    if not pinned.is_dir():
        return True
    paths = set(host_paths(pinned)) | set(host_paths(target))
    return any(_digest(pinned, rel) != _digest(target, rel) for rel in sorted(paths))


def host_restart(pinned: Optional[Path], target: Path, release_id: str, client, rollback: str = "") -> dict:
    """``{"state": "none"}`` (no pin, the host's own release, or its code unchanged) | ``{"state", "operation"}`` |
    ``{"state": "uncovered", "why"}``: the factory restarts the host, asked as root (no grant), once the roles'
    turns end; the key is the release's, so a repeat joins the same operation. ``rollback`` — the rollback's stamp:
    the release rolled back to used its own key long ago (a known key answers with its ended operation)."""
    target = Path(target)
    if pinned is None or Path(pinned) == target or not host_changed(Path(pinned), target):
        return {"state": "none"}
    key = f"rollback:{release_id}:{rollback}" if rollback else f"release:{release_id}"
    # never ``…:session-host:restart`` — a card's release step of the same release is keyed so
    try:
        revision = (target / "REVISION").read_text(encoding="utf-8").strip()
        found = client.call({"op": "operation.request", "requester": "root", "kind": COMPONENT,
                             "component": "session-host", "mode": "restart",
                             "key": f"{key}:host-restart", "revision": revision})["operation"]
    except CabinetError as exc:
        return {"state": "uncovered", "why": str(exc)[:WHY_MAX]}
    except (OSError, KeyError, TypeError) as exc:
        return {"state": "uncovered", "why": f"the cabinet is unavailable ({exc.__class__.__name__})"}
    return {"state": found["state"], "operation": found["id"]}


def host_main(argv: Optional[list] = None) -> int:
    """``<tree> <release id> [--rollback <stamp>]``: the host's restart for the release just switched to (or rolled
    back to), as one JSON line."""
    args = list(sys.argv[1:] if argv is None else argv)
    rollback = ""
    if len(args) == 4 and args[2] == "--rollback" and STAMP.fullmatch(args[3]):
        rollback, args = args[3], args[:2]
    if len(args) != 2 or not RELEASE_ID.fullmatch(args[1]):
        print("usage: release_reinstall host <tree> <rev>[+<deploy>] [--rollback <stamp>]", file=sys.stderr)
        return 2
    from kd.cabinet import client as cabinet_client

    target = Path(os.path.realpath(args[0]))
    print(json.dumps(host_restart(pinned_release(target), target, args[1], cabinet_client.Client(),
                                  rollback=rollback)))
    return 0


def uncovered_notice(result: dict, release: str, card: str, *, post) -> bool:
    """Roles this release left on the old text, said on ``card`` (or in the feed); anything else posts nothing."""
    if result.get("state") != "uncovered" or not result.get("agents"):
        return False
    agents = list(result["agents"])
    return post(SOURCE, "warn", UNCOVERED, {"rev": release, "agents": ", ".join(agents),
                                            "agents_csv": ",".join(agents),
                                            "why": str(result.get("why") or "")[:WHY_MAX]}, card)


def main(argv: Optional[list] = None, *, post=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--by-root" in args:  # an older kd-release's flag: the roles are asked for as root either way
        args.remove("--by-root")
    release_id = None
    if len(args) >= 2 and args[-2] == "--release-id":
        release_id, args = args[-1], args[:-2]
    if len(args) not in (2, 3) or (release_id is not None and not RELEASE_ID.fullmatch(release_id)):
        print("usage: release_reinstall <tree> <revision> [<card>] [--by-root] [--release-id <rev>[+<deploy>]]",
              file=sys.stderr)
        return 2
    from kd import install_apply
    from kd.cabinet import client as cabinet_client

    repo, card = Path(args[0]), args[2] if len(args) == 3 else ""
    result = reinstall(repo, args[1], card, outdated=lambda tree: install_apply.outdated_agents(tree),
                       client=cabinet_client.Client(), release_id=release_id)
    uncovered_notice(result, release_id or args[1], card, post=post or cabinet_client.post_notice)
    print(json.dumps(result, ensure_ascii=False))
    return 0
