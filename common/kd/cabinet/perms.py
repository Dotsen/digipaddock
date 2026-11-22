"""Permissions of people in the cabinet: a finite set in code, roles are data (owner cabinet spec section 6).

A role is a set of permissions; a person holds roles, each over a set of projects or over all of
them (``"*"``). ``"*"`` as a permission is every permission (the owner's role).
"""

from typing import Iterable

PERMISSIONS = (
    "cards.read",
    "cards.create",
    "cards.comment",
    "cards.edit",
    "cards.transition",
    "decide.admin_plan",
    "decide.admin_critical",
    "decide.delegate",
    "decide.monitor_trust",
    "decide.pr_approve",
    "decide.promote",
    "decide.question",
    "decide.permission",
    "agents.chat",
    "agents.control",
    "servers.read",
    "incidents.ack",
    "users.manage",
    "settings",
)
ALL = "*"
DEFAULT_ROLES = {
    "owner": (ALL,),
    "partner": (
        "cards.read",
        "cards.create",
        "cards.comment",
        "cards.edit",
        "decide.promote",
        "decide.question",
        "agents.chat",
    ),
    "viewer": ("cards.read",),
}


def check(perm: str) -> str:
    """The permission itself, or ValueError for one the code does not know."""
    if perm != ALL and perm not in PERMISSIONS:
        raise ValueError(f"unknown permission {perm!r}")
    return perm


def allows(role_perms: Iterable[str], perm: str) -> bool:
    perms = set(role_perms)
    return ALL in perms or perm in perms
