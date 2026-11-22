"""The board grant of one agent user, taken from its compiled policy (BOSS spec 4.6, 5.1).

``common/install/add_user.sh`` creates an agent's Linux user and applies exactly the
grant that ``board_grant`` returns; the shell script decides nothing about rights. For an
agent described by a repository manifest the grant is the ``board-tasks`` entry of the
``acl`` artifact that ``kd.policy.compile_policy`` builds: ``board.read`` reads;
``board.write`` adds nothing, because its writes go through the kd-task executor,
and ``board.coordinate`` adds nothing, because its writes go through the kd-boss-board
executor. A user without a valid manifest is refused.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from kd.agents import DEFAULT_USER, USER_RE
from kd.manifest import ManifestError, validate_all
from kd.policy import PolicyError, compile_policy
from kd.registry import (
    RegistryError,
    load_registry,
    read_json_object,
    validate_registry,
)

BOARD_RESOURCE = "board-tasks"
# The cabinet's nightly snapshot of the board in Backlog.md files (kd-cabinet export) lives in this
# directory of the board root; the read grant goes on it, the root-owned board root itself is never granted.
BACKLOG_DIR = "backlog"
# The managed settings floor the policy is checked against, as tracked in the repository.
# The repository floor: naming tokens resolve when read_json_object reads it.
FLOOR = "common/claude/managed-settings.template.json"
GRANTED = frozenset({"read"})


class BoardGrantError(ValueError):
    """No board grant can be decided for this user; nothing may be installed."""


@dataclass(frozen=True)
class BoardGrant:
    """``access`` is none or read; ``path`` is the Backlog.md directory."""

    access: str
    path: str


def board_grant(user: str, root: str | os.PathLike[str]) -> BoardGrant:
    """Decide the board grant of ``user`` from the repository at ``root``; reads only.

    Raises BoardGrantError for a user that is not an agent user, registry data that does
    not validate, a user that no valid manifest runs as, or a policy that does not compile.
    """
    if not isinstance(user, str) or user == DEFAULT_USER or not USER_RE.fullmatch(user):
        raise BoardGrantError(f"not an agent user: {user!r}")
    root = Path(root)
    try:
        registry = load_registry(root)
        validate_registry(registry)
    except RegistryError as exc:
        raise BoardGrantError(f"registry: {exc}") from exc
    board = registry.resources.resources.get(BOARD_RESOURCE)
    if board is None or board.kind != "dir" or board.path is None:
        raise BoardGrantError(f"resource {BOARD_RESOURCE} must be a directory path")
    target = f"{board.path}/{BACKLOG_DIR}"
    try:
        manifests = validate_all(root)
    except ManifestError as exc:
        raise BoardGrantError(f"manifests: {exc}") from exc
    owned = [item for item in manifests.values() if item.user == user]
    if len(owned) != 1:
        raise BoardGrantError(f"no agent manifest runs as {user}")
    manifest = owned[0]
    try:
        floor = read_json_object(root / FLOOR)
        policy = compile_policy(manifest, registry, floor=floor, agents=manifests)
    except (RegistryError, PolicyError) as exc:
        raise BoardGrantError(f"policy of {manifest.name}: {exc}") from exc
    return grant_from_acl(policy["acl"], user, board.path, target)


def grant_from_acl(
    acl: Iterable[Mapping[str, Any]], user: str, board_path: str, target: str
) -> BoardGrant:
    """The board entry of a compiled ``acl`` artifact as a grant on ``target``.

    No board entry is no grant. More than one entry, or one for another user, another
    path or an unknown access, is refused rather than read generously.
    """
    entries = [item for item in acl if item.get("resource") == BOARD_RESOURCE]
    if not entries:
        return BoardGrant("none", target)
    if len(entries) != 1:
        raise BoardGrantError(f"{len(entries)} {BOARD_RESOURCE} entries for {user}")
    item = entries[0]
    if (
        item.get("user") != user
        or item.get("path") != board_path
        or item.get("access") not in GRANTED
    ):
        raise BoardGrantError(f"unexpected {BOARD_RESOURCE} entry for {user}")
    return BoardGrant(item["access"], target)


def main(argv: Sequence[str]) -> int:
    """``<repository root> <user>``: print ``<access> <path>`` for add_user.sh."""
    if len(argv) != 2:
        print("usage: board_grant <repository root> <user>", file=sys.stderr)
        return 2
    root, user = argv
    try:
        grant = board_grant(user, root)
    except BoardGrantError as exc:
        print(f"board grant refused: {exc}", file=sys.stderr)
        return 1
    print(f"{grant.access} {grant.path}")
    return 0
