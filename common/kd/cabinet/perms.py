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
    "deploys.read",
    "users.manage",
    "settings",
)
ALL = "*"
# A partner works with agents in the threads of their projects' cards only: an agent's own threads are not theirs
# (KD-9476; schema v48 takes agents.chat from a partner role stored before).
DEFAULT_ROLES = {
    "owner": (ALL,),
    "partner": (
        "cards.read",
        "cards.create",
        "cards.comment",
        "cards.edit",
        "decide.promote",
        "decide.question",
    ),
    "viewer": ("cards.read",),
}
# An agent's own threads with people, outside cards, by space: its chat, and its «Monitor» — the monitor's reports on
# the servers — which also needs the servers. Both are agent-wide, so only a grant over every project counts.
AGENT_THREAD_PERMS = {"chat": ("agents.chat",), "monitor": ("agents.chat", "servers.read")}


def check(perm: str) -> str:
    """The permission itself, or ValueError for one the code does not know."""
    if perm != ALL and perm not in PERMISSIONS:
        raise ValueError(f"unknown permission {perm!r}")
    return perm


def allows(role_perms: Iterable[str], perm: str) -> bool:
    perms = set(role_perms)
    return ALL in perms or perm in perms
