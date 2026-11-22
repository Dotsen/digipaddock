"""Contours of the release tree: which projects check together, and where.

host/contours.json of the release tree (KD_CONTOURS_JSON overrides the path) names each contour, the host its
dev.check runs on, the projects (components) it covers and how many checks it takes at once. A missing file
is no contours: dev.check is then refused for every project. A broken file fails closed.
"""

import json
import os
import re
from typing import Dict, NamedTuple, Optional, Tuple

# The release tree is three levels up: <tree>/common/kd/contours.py.
CONTOURS_JSON = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "host", "contours.json"
)
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
LOCAL = "local"
DEFAULT_PARALLEL = 2
MAX_PARALLEL = 4
_KEYS = {"host", "components", "exclusive", "parallel"}


class ContourError(ValueError):
    pass


class Contour(NamedTuple):
    name: str
    host: str
    components: Tuple[str, ...]
    exclusive: bool
    parallel: int


def path() -> str:
    return os.environ.get("KD_CONTOURS_JSON") or CONTOURS_JSON


def _contour(source: str, name, spec) -> Contour:
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise ContourError(f"{source}: contour name {name!r} must match {NAME_RE.pattern}")
    if not isinstance(spec, dict) or not set(spec) <= _KEYS:
        raise ContourError(f"{source}: {name}: expected an object of {sorted(_KEYS)}")
    host = spec.get("host")
    if not isinstance(host, str) or not host:
        raise ContourError(f"{source}: {name}: host must be a non-empty string")
    components = spec.get("components")
    if (
        not isinstance(components, list)
        or not components
        or not all(isinstance(c, str) and NAME_RE.fullmatch(c) for c in components)
        or len(set(components)) != len(components)
    ):
        raise ContourError(f"{source}: {name}: components must be distinct project names")
    exclusive = spec.get("exclusive", False)
    if type(exclusive) is not bool:
        raise ContourError(f"{source}: {name}: exclusive must be true or false")
    parallel = spec.get("parallel", DEFAULT_PARALLEL)
    if type(parallel) is not int or not 1 <= parallel <= MAX_PARALLEL:
        raise ContourError(f"{source}: {name}: parallel must be 1..{MAX_PARALLEL}")
    return Contour(name, host, tuple(components), exclusive, parallel)


def load(source: Optional[str] = None) -> Dict[str, Contour]:
    source = source or path()
    try:
        with open(source, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ContourError(f"{source}: unreadable ({exc.__class__.__name__})")
    if not isinstance(data, dict):
        raise ContourError(f"{source}: expected an object of contours")
    found = {name: _contour(source, name, spec) for name, spec in data.items()}
    seen: Dict[str, str] = {}
    for contour in found.values():
        for project in contour.components:
            if project in seen:
                raise ContourError(f"{source}: project {project} is in {seen[project]} and {contour.name}")
            seen[project] = contour.name
    return found


def capacity(contour: Contour) -> int:
    """How many checks the contour runs at once."""
    return 1 if contour.exclusive else contour.parallel
