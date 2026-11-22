"""Which agents run on the session host, with their slots and the idle time of a session.

``/etc/digipaddock/session-host.json`` (root, 0644) is read by the cabinet (whose commands go to the host),
by the host itself and by every wake path (``kd.wake``): an agent listed there gets its wakes as cabinet
commands; a wake of any other agent is ``absent`` (no digipaddock panel takes it). No file — nobody is hosted.
"""

from dataclasses import dataclass, field
import json
import os
import sys
import tempfile
from typing import Mapping, Optional, Tuple

from kd.agents import NAME_RE

CONFIG = "/etc/digipaddock/session-host.json"
IDLE_MINUTES = 10
MAX_SLOTS = 9


@dataclass(frozen=True)
class HostConfig:
    agents: Mapping[str, int] = field(default_factory=dict)
    idle_s: int = IDLE_MINUTES * 60

    def hosted(self, agent: str) -> bool:
        return agent in self.agents

    def slots(self, agent: str) -> int:
        return self.agents.get(agent, 0)


def _int(value, low: int, high: int, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"session-host: {what} must be {low}..{high}")
    return value


def parse(data) -> HostConfig:
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("session-host: schema_version must be 1")
    extra = set(data) - {"schema_version", "idle_minutes", "agents"}
    if extra:
        raise ValueError(f"session-host: unknown key {sorted(extra)[0]!r}")
    idle = _int(data.get("idle_minutes", IDLE_MINUTES), 1, 120, "idle_minutes")
    agents = data.get("agents", {})
    if not isinstance(agents, dict):
        raise ValueError("session-host: agents must be an object")
    slots = {}
    for name, item in agents.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise ValueError(f"session-host: bad agent name {name!r}")
        if not isinstance(item, dict) or set(item) - {"slots"}:
            raise ValueError(f"session-host: {name}: only slots may be set")
        slots[name] = _int(item.get("slots", 1), 1, MAX_SLOTS, f"{name}.slots")
    return HostConfig(slots, idle * 60)


def load(path: str = CONFIG) -> HostConfig:
    try:
        with open(path, encoding="utf-8") as f:
            text = f.read()
    except FileNotFoundError:
        return HostConfig()
    except OSError as e:
        raise ValueError(f"session-host: {path}: {e.strerror}") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(f"session-host: {path} is not JSON") from None
    return parse(data)


class Cached:
    """The config re-read when the file changes; a broken file after a good one keeps the good one."""

    def __init__(self, path: str = CONFIG):
        self.path = path
        self._stamp: Optional[Tuple[int, int]] = None
        self._config = HostConfig()

    def get(self) -> HostConfig:
        try:
            st = os.stat(self.path)
            stamp = (st.st_mtime_ns, st.st_size)
        except FileNotFoundError:
            self._stamp, self._config = None, HostConfig()
            return self._config
        except OSError:
            return self._config
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                self._config = load(self.path)
            except ValueError as e:
                print(f"{e}; keeping the last good config", file=sys.stderr)
        return self._config


USAGE = "usage: session_config init | add <agent> [<slots>] | remove <agent> | list"


def _save(path: str, data: dict) -> None:
    parse(data)  # never write a file the readers would refuse
    folder = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".session-host.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv, *, path: str = CONFIG, out=None, err=None) -> int:
    """The installer's edits (``session_host_setup.sh``): ``init`` makes the file with no agent when it is missing,
    ``add`` lists an agent with one slot or the given number (``add <agent> <slots>`` also changes a listed agent's
    slots; without the number a listed one stays as it is), ``remove`` unlists it, ``list`` prints
    ``<agent> <slots>``. A broken file is never edited. 0 — done, 1 — the file, 2 — usage."""
    out, err = out or sys.stdout, err or sys.stderr
    op, args = (argv[0], list(argv[1:])) if argv else ("", [])
    arity = {"init": (0,), "list": (0,), "add": (1, 2), "remove": (1,)}.get(op, ())
    slots = args[1] if op == "add" and len(args) == 2 else None
    if len(args) not in arity or (args and not NAME_RE.fullmatch(args[0])) or (
        slots is not None and not (slots.isdigit() and 1 <= int(slots) <= MAX_SLOTS)
    ):
        print(f"session-host: {USAGE}", file=err)
        return 2
    try:
        if op == "init":
            if not os.path.exists(path):
                _save(path, {"schema_version": 1, "agents": {}})
            load(path)
            return 0
        if not os.path.exists(path):
            raise ValueError(f"session-host: {path} is missing (run session_host_setup.sh install)")
        config = load(path)
        if op == "list":
            for agent, slots in config.agents.items():
                print(f"{agent} {slots}", file=out)
            return 0
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        agents = data.setdefault("agents", {})
        if op == "add" and (args[0] not in agents or slots is not None):
            wanted = {"slots": int(slots) if slots is not None else 1}
            if agents.get(args[0]) != wanted:
                agents[args[0]] = wanted
                _save(path, data)
        elif op == "remove" and args[0] in agents:
            del agents[args[0]]
            _save(path, data)
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=err)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
