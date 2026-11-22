"""A card session's probe exit (the agent autonomy design, piece 2), granted by the owner's consent on the card.

The consent of «in progress» lives while the card is the agent's and in work, and — when the card has a brief — while
its latest version is approved and the card's scope is still that version's. The exit itself is the card's project's
in ``host/egress.json`` (``kd.egress``), and only for an agent that exit lists. The dispatcher asks ``grant`` at
every delivery (the session host restarts a process whose exit changed) and, on its sweep, for every live process
that holds one (``ended``: the host's ``revoke`` cuts that process, and the session goes on without the exit).
"""

from typing import List, Mapping, Tuple

from pathlib import Path

from kd import egress as config
from kd import specializations
from kd.cabinet import briefs
from kd.cabinet import schema
from kd.cabinet import store as store_mod
from kd.manifest import ManifestError, load_manifest

IN_WORK = ("To Do", "In Progress", "Blocked")


def grant(ctx, record, agent: str) -> str:
    """The exit ``agent``'s session of ``record`` runs with, or ``""``. A missing or broken host file grants none."""
    return choose(ctx, record, agent)[0]


def choose(ctx, record, agent: str) -> Tuple[str, str]:
    """``(exit, gone)``: the exit of ``grant``, and the country of the card's ``egress:`` label when the host has no
    exit of it for the agent (``""`` otherwise) — the delivery then goes on as without the label, and the cabinet
    says so in the thread. a country label on an open card of the agent's gives that country's exit of
    every host, the standing exit's kind, so no brief's approval is asked; it beats the project's exit."""
    if record.assignee != "@" + agent:
        return "", ""
    try:
        document = config.load(ctx.egress_path)
    except config.EgressError:
        return "", ""
    country = config.label_country(record.labels)
    if country and record.status not in schema.TERMINAL:
        name = config.country_exit(document, country)
        if name and agent in config.exit_of(document, name)["agents"] and _holds(ctx, agent):
            return name, ""
        return _project_exit(ctx, record, agent, document), country
    return _project_exit(ctx, record, agent, document), ""


def _project_exit(ctx, record, agent: str, document: Mapping) -> str:
    if record.status not in IN_WORK:
        return ""
    name = config.exit_for(document, record.project, agent)
    if not name or not _holds(ctx, agent):
        return ""
    last = briefs.latest(ctx.store, record.id)
    if last is not None and (
        not last["approved_by"]
        or briefs.scope_sha(briefs.scope(record)) != last["scope_sha"]
    ):
        return ""
    return name


def _holds(ctx, agent: str) -> bool:
    """The agent's manifest in the deployed tree names net.egress: kd.launch refuses an exit to a policy without it,
    so a grant would fail every launch of the project's cards."""
    root = Path(ctx.repo_root) if ctx.repo_root else specializations.REPO_ROOT
    try:
        return (
            "net.egress"
            in load_manifest(root / "agents" / agent / "agent.json").capabilities
        )
    except (ManifestError, OSError):
        return False


def _country(ctx, exit_name: str) -> str:
    try:
        return config.country_of(config.load(ctx.egress_path), exit_name)
    except config.EgressError:
        return ""


def configured(ctx) -> bool:
    """Whether the host has any probe exit: without one no process can hold an exit, and the sweep asks nothing."""
    try:
        return bool(config.load(ctx.egress_path).get("exits"))
    except config.EgressError:
        return True  # a broken file grants nothing, but a process from before the breakage may still hold an exit


def ended(ctx, held: List[Mapping]) -> List[dict]:
    """The revokes for the sessions of ``held`` (the host's status: session, agent, card, egress — a live process's
    exit, or the one a waiting session would start with) whose card no longer grants that exit. A revoke, not a stop command: the cabinet reads a stop as the owner's (no
    continuation, no problem raised), while here the session goes on without the exit. Runs under the service lock."""
    store, out = ctx.store, []
    for item in held:
        card_id = item.get("card") or ""
        try:
            record = store.card(card_id) if card_id else None
        except store_mod.NotFound:
            record = None
        granted = grant(ctx, record, item["agent"]) if record is not None else ""
        if granted == item["egress"] or (
            granted and _country(ctx, granted) and _country(ctx, item["egress"])
        ):
            # a country label changed under a turn — both exits are of every host, and the turn ends
            # as it is; the next delivery restarts the process with the new country
            continue
        out.append(
            {"op": "revoke", "session": item["session"], "egress": item["egress"]}
        )
    return out
