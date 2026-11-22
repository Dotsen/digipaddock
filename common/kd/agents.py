"""The agents' users: /etc/digipaddock/agents.json (root 0644, written by the agent installer).
{"dev": {"user": "<base>-dev", "runtime": "generated"}}; an agent without an entry runs as the base user.
User names come from kd.naming (the host prefix). Dev spec 5.1a (option G): an agent with arbitrary
code runs as its own user <base>-<role>, and each such user has exactly one agent (the installer
stops an agent by signalling every process of its user)."""
import json
import re
from typing import List, Optional

from kd import naming

REGISTRY = "/etc/digipaddock/agents.json"
DEFAULT_USER = naming.base_user()
NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
USER_RE = naming.user_re()  # only agent users: the base one and <base>-<name>, use with fullmatch
# How agent-launch starts an agent (BOSS spec 5.2a-5.2b): only an explicit entry selects the generated runtime.
LEGACY_RUNTIME = "legacy"
GENERATED_RUNTIME = "generated"


def _load(path) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError(f"agents.json не читается: {exc.__class__.__name__}") from exc
    if not isinstance(data, dict):
        raise ValueError("agents.json: ожидается объект агент → описание")
    owners = {}
    for name, item in data.items():
        if not isinstance(name, str) or not NAME_RE.fullmatch(name) or not isinstance(item, dict):
            raise ValueError(f"agents.json: неверная запись {name!r}")
        user = item.get("user", DEFAULT_USER)
        if not isinstance(user, str) or not USER_RE.fullmatch(user):
            raise ValueError(f"agents.json: {name}: пользователь только {DEFAULT_USER} или {DEFAULT_USER}-<роль>")
        if user != DEFAULT_USER:
            if user in owners:
                raise ValueError(f"agents.json: пользователь {user} у двух агентов ({owners[user]}, {name})")
            owners[user] = name
    return data


def user_of(name: str, path=REGISTRY) -> str:
    """The agent's Linux user; the agent is not in the registry or there is no file — agent."""
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(f"неверное имя агента: {name!r}")
    return _load(path).get(name, {}).get("user", DEFAULT_USER)


def agent_of_user(user: str, path=REGISTRY) -> Optional[str]:
    """The agent that runs under its own user `user`; None for the base user (several agents) and unknown users."""
    if user == DEFAULT_USER:
        return None
    for name, item in _load(path).items():
        if item.get("user") == user:
            return name
    return None


def users(path=REGISTRY) -> List[str]:
    """All agent users, agent included, without duplicates, alphabetically."""
    found = {DEFAULT_USER} | {item.get("user", DEFAULT_USER) for item in _load(path).values()}
    return sorted(found)


def names(path=REGISTRY) -> List[str]:
    """The registered agent names, sorted (without ``@``)."""
    return sorted(_load(path))


def runtime_of(name: str, path=REGISTRY) -> str:
    """LEGACY_RUNTIME or GENERATED_RUNTIME for agent-launch.

    Generated only for an entry with "runtime": "generated"; no file, no entry or no field
    is legacy. Any other value raises ValueError, so a typo never quietly picks a launch.
    """
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ValueError(f"неверное имя агента: {name!r}")
    entry = _load(path).get(name, {})
    if "runtime" not in entry:
        return LEGACY_RUNTIME
    if entry["runtime"] == GENERATED_RUNTIME:
        return GENERATED_RUNTIME
    raise ValueError(f"agents.json: {name}: неизвестный runtime")
