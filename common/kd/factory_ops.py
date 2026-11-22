"""The factory's operations: what the developer may ask the platform to do itself.

The catalog (``common/coordination/factory.json``) is finite and reviewed: ``agent.reinstall`` of generated agents
on the factory host — the host the cabinet runs on, never a server of the inventory — and ``component.install``
: one of the platform's own setup scripts in one of its fixed modes (``repo-attach`` install|check, …), each
mode a fixed argv of the deployed tree (a script of an ``install/`` folder, or ``systemctl restart`` of a ``digipaddock-``
unit), an optional check after it and whether every hosted role drains first (``drain: all`` — the session host's
restart). Nothing of a step is a path, an argument or a shell: a component and a mode are names the catalog knows.

A plan of such operations is a schema-2 proposal like the sysadmin's (intent, basis, steps with
what/effect/risk/rollback); the owner's approval writes the same grant through ``admin_grant.Store`` under the
factory policy. A ``component.install`` step names the deployed ``revision`` it was signed for — the tree deployed
at the signature (``at_revision``; the plan's own is the tree it was opened on): it covers that tree only (any number
of runs while the grant lives — the owner's choice), a release ends it. A request is
checked against that grant here: the card, its single assignee as the requester, the kind and the agents or the
component, mode and revision.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Iterable, Mapping, Tuple

from kd import admin_grant, admin_task
from kd.admin_operations import MAX_EXPLAIN, MAX_WHAT, SCHEMA_VERSION, ProposalError, _text, risk
from kd.admin_task import canonical

CATALOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(__file__))), "coordination",
                            "factory.json")
REINSTALL, COMPONENT = "agent.reinstall", "component.install"
RELEASE_REVISION = "release"  # a component step the release of the card's own PR requests
KINDS = (REINSTALL, COMPONENT)  # what the code knows how to run; the catalog enables a subset
TARGET = "factory"
MAX_OPERATIONS = 4
_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
REVISION = re.compile(r"[0-9a-f]{7,40}")
# a mode's argv: a setup script of the deployed tree with plain word arguments, or the restart of a digipaddock- unit
SCRIPT = re.compile(r"(?:common|agents/[a-z][a-z0-9_-]{0,31})/install/[a-z][a-z0-9_]{0,63}\.sh")
ARG = re.compile(r"[a-z][a-z0-9-]{0,31}")
UNIT = re.compile(r"digipaddock-[a-z0-9][a-z0-9-]{0,63}\.service")
DRAINS = ("", "all")
_STEP_KEYS = {REINSTALL: frozenset({"verb", "target", "agents", "what", "effect", "risk", "rollback"}),
              COMPONENT: frozenset({"verb", "target", "component", "mode", "revision", "what", "effect", "risk",
                                    "rollback"})}
_MODE_KEYS = frozenset({"run", "check", "drain"})
_PROPOSAL_KEYS = frozenset({"schema_version", "task_id", "intent", "basis", "operations"})

__all__ = ["Catalog", "CatalogError", "Mode", "Operation", "FactoryProposal", "ProposalError", "load_catalog",
           "operations_of", "proposal", "check_request", "revision_matches", "binds_tree", "RELEASE_REVISION"]


class CatalogError(ValueError):
    """The factory catalog is malformed or names what the code does not run."""


@dataclass(frozen=True)
class Mode:
    """One fixed mode of a component: its argv, the check after it (empty: none) and the roles that drain first."""
    run: Tuple[str, ...]
    check: Tuple[str, ...]
    drain: str


def _argv(value: object, label: str) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise CatalogError(f"{label}: an argv is a non-empty list of strings")
    if value[0] == "systemctl":
        if len(value) != 3 or value[1] != "restart" or not UNIT.fullmatch(value[2]):
            raise CatalogError(f"{label}: systemctl only as restart of one digipaddock- unit")
    elif not SCRIPT.fullmatch(value[0]) or not all(ARG.fullmatch(v) for v in value[1:]) or len(value) > 4:
        raise CatalogError(f"{label}: a setup script of an install/ folder with up to three word arguments")
    return tuple(value)


def _components(entry: dict) -> Mapping[str, Mapping[str, Mode]]:
    components = entry.get("components")
    if set(entry) != {"target", "components"} or not isinstance(components, dict) or not components:
        raise CatalogError(f"{COMPONENT}: target and a non-empty components object")
    out = {}
    for name, modes in components.items():
        if not _NAME.fullmatch(name) or not isinstance(modes, dict) or not modes:
            raise CatalogError(f"{COMPONENT}: component {name!r} needs a name and its modes")
        out[name] = {}
        for mode, spec in modes.items():
            label = f"{name} {mode}"
            if not _NAME.fullmatch(mode) or not isinstance(spec, dict) or "run" not in spec or set(spec) - _MODE_KEYS:
                raise CatalogError(f"{label}: a mode is a name with run, check?, drain?")
            if spec.get("drain", "") not in DRAINS:
                raise CatalogError(f"{label}: drain is one of {DRAINS}")
            check = _argv(spec["check"], f"{label} check") if "check" in spec else ()
            out[name][mode] = Mode(_argv(spec["run"], label), check, spec.get("drain", ""))
    return out


@dataclass(frozen=True)
class Catalog:
    projects: frozenset
    requesters: frozenset
    kinds: Mapping[str, dict]
    components: Mapping[str, Mapping[str, Mode]] = None

    def mode(self, component: str, mode: str) -> Mode:
        """The catalog's fixed mode; ProposalError for anything it does not name."""
        modes = (self.components or {}).get(component)
        if modes is None:
            raise ProposalError(f"unknown component {component!r}; known: {', '.join(sorted(self.components or ()))}")
        if mode not in modes:
            raise ProposalError(f"{component}: unknown mode {mode!r}; known: {', '.join(sorted(modes))}")
        return modes[mode]

    @classmethod
    def from_dict(cls, data: object) -> "Catalog":
        if not isinstance(data, dict) or set(data) != {"schema_version", "projects", "requesters", "operations"}:
            raise CatalogError("factory catalog keys must be schema_version, projects, requesters, operations")
        if data["schema_version"] != 1:
            raise CatalogError(f"unknown factory catalog schema_version {data['schema_version']!r}")
        projects, requesters, kinds = data["projects"], data["requesters"], data["operations"]
        for name, value in (("projects", projects), ("requesters", requesters)):
            if not isinstance(value, list) or not value or not all(isinstance(v, str) and _NAME.fullmatch(v)
                                                                    for v in value):
                raise CatalogError(f"{name} must be a non-empty list of names")
        if set(requesters) != {"dev"}:
            raise CatalogError("the factory takes requests of the developer only")
        if not isinstance(kinds, dict) or not kinds or set(kinds) - set(KINDS):
            raise CatalogError(f"operations must be some of {', '.join(KINDS)}")
        components = {}
        for kind, entry in kinds.items():
            if not isinstance(entry, dict) or entry.get("target") != TARGET:
                raise CatalogError(f"{kind}: the only target is {TARGET!r}")
            if kind == COMPONENT:
                components = _components(entry)
            elif entry != {"target": TARGET}:
                raise CatalogError(f"{kind}: the only key is target")
        return cls(frozenset(projects), frozenset(requesters), dict(kinds), components)

    @property
    def scope(self) -> admin_grant.Factory:
        return admin_grant.Factory(self.projects, self.requesters)


def load_catalog(path: str = CATALOG_PATH) -> Catalog:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        raise CatalogError(f"factory catalog {path} cannot be read: {exc.__class__.__name__}") from exc
    return Catalog.from_dict(data)


@dataclass(frozen=True)
class Operation:
    verb: str
    target: str
    agents: Tuple[str, ...]
    what: str
    effect: str
    risk_level: str
    risk_why: str
    rollback: str
    component: str = ""
    mode: str = ""
    revision: str = ""

    def as_dict(self) -> dict:
        names = ({"component": self.component, "mode": self.mode, "revision": self.revision}
                 if self.verb == COMPONENT else {"agents": list(self.agents)})
        return {"verb": self.verb, "target": self.target, **names, "what": self.what,
                "effect": self.effect, "risk": {"level": self.risk_level, "why": self.risk_why},
                "rollback": self.rollback}


@dataclass(frozen=True)
class FactoryProposal:
    task_id: str
    intent: str
    basis: str
    operations: Tuple[Operation, ...]

    def as_dict(self) -> dict:
        return {"schema_version": SCHEMA_VERSION, "task_id": self.task_id, "intent": self.intent,
                "basis": self.basis, "operations": [op.as_dict() for op in self.operations]}

    @property
    def sha256(self) -> str:
        return hashlib.sha256(canonical(self.as_dict())).hexdigest()


def _operation(op: object, catalog: Catalog, known: frozenset) -> Operation:
    if not isinstance(op, dict):
        raise ProposalError("an operation must be an object")
    verb = op.get("verb")
    if verb not in catalog.kinds:
        raise ProposalError(f"unknown factory kind {verb!r}; known: {', '.join(sorted(catalog.kinds))}")
    extra = set(op) - _STEP_KEYS[verb]
    if extra:
        what = "a component, a mode and a revision" if verb == COMPONENT else "agents"
        raise ProposalError(f"unexpected keys {sorted(extra)}: a {verb} step names a target and {what}")
    if op.get("target") != catalog.kinds[verb]["target"]:
        raise ProposalError(f"{verb}: the target is {catalog.kinds[verb]['target']!r}, not {op.get('target')!r}")
    if verb == COMPONENT:
        component, mode, revision = op.get("component"), op.get("mode"), op.get("revision")
        if not isinstance(component, str) or not isinstance(mode, str):
            raise ProposalError("a component step names its component and mode")
        catalog.mode(component, mode)
        if not isinstance(revision, str) or not (REVISION.fullmatch(revision) or revision == RELEASE_REVISION):
            raise ProposalError(f"revision is the deployed tree's commit (7 to 40 hex) or {RELEASE_REVISION!r}")
        level, why = risk(op.get("risk"))
        return Operation(verb, op["target"], (), _text(op, "what", MAX_WHAT, allowed=""),
                         _text(op, "effect", MAX_EXPLAIN), level, why, _text(op, "rollback", MAX_EXPLAIN),
                         component=component, mode=mode, revision=revision)
    agents = op.get("agents")
    if not isinstance(agents, list) or not agents:
        raise ProposalError("agents must be a non-empty list of generated agents")
    if len(set(agents)) != len(agents):
        raise ProposalError("an agent is listed twice")
    for name in agents:
        if not isinstance(name, str) or name not in known:
            raise ProposalError(f"{name!r} is not a generated agent of this host")
    level, why = risk(op.get("risk"))
    return Operation(verb, op["target"], tuple(sorted(agents)), _text(op, "what", MAX_WHAT, allowed=""),
                     _text(op, "effect", MAX_EXPLAIN), level, why, _text(op, "rollback", MAX_EXPLAIN))


def operations_of(value: object, *, catalog: Catalog, agents_known: Iterable[str]) -> Tuple[Operation, ...]:
    """The steps of a plan or of a brief's "Operations" section, validated against the catalog."""
    if not isinstance(value, list) or not value:
        raise ProposalError("operations must be a non-empty list")
    if len(value) > MAX_OPERATIONS:
        raise ProposalError(f"at most {MAX_OPERATIONS} operations")
    known = frozenset(agents_known)
    return tuple(_operation(op, catalog, known) for op in value)


def proposal(data: object, *, catalog: Catalog, agents_known: Iterable[str]) -> FactoryProposal:
    """The normalized schema-2 factory plan, or ProposalError."""
    if not isinstance(data, dict) or set(data) != _PROPOSAL_KEYS:
        raise ProposalError(f"a plan has exactly the keys {sorted(_PROPOSAL_KEYS)}")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ProposalError(f"unknown plan schema_version {data['schema_version']!r}")
    task_id = data["task_id"]
    if not isinstance(task_id, str) or not admin_task.TASK_ID.fullmatch(task_id):
        raise ProposalError(f"invalid task id {task_id!r}")
    return FactoryProposal(task_id, _text(data, "intent"), _text(data, "basis"),
                           operations_of(data["operations"], catalog=catalog, agents_known=agents_known))


def at_revision(prop: FactoryProposal, revision: str) -> FactoryProposal:
    """The plan with every component step on ``revision``: the signature covers the
    tree deployed when the owner signs, not the one of the hour the plan was opened. Other steps stay as they are."""
    if not REVISION.fullmatch(revision or ""):
        raise ProposalError(f"the deployed revision {revision[:40]!r} is not a commit")
    steps = tuple(replace(op, revision=revision) if op.verb == COMPONENT and op.revision != RELEASE_REVISION
                  else op for op in prop.operations)
    return replace(prop, operations=steps)


def binds_tree(steps) -> bool:
    """A plan with a component step signed for a tree (not one on ``release``) binds the deployed revision."""
    for op in steps:
        verb = op.verb if isinstance(op, Operation) else (op.get("verb") if isinstance(op, dict) else None)
        rev = op.revision if isinstance(op, Operation) else (op.get("revision") if isinstance(op, dict) else None)
        if verb == COMPONENT and rev != RELEASE_REVISION:
            return True
    return False


def revision_matches(signed: str, deployed: str) -> bool:
    """A revision named short or in full is the same commit."""
    return bool(signed and deployed) and (deployed.startswith(signed) or signed.startswith(deployed))


def check_request(grant: admin_grant.Grant, *, card: admin_task.Card, requester: str, kind: str,
                  agents: Iterable[str], catalog: Catalog, now: datetime, component: str = "", mode: str = "",
                  revision: str = "", release: bool = False) -> dict:
    """The approved step that covers this request; GrantError (or CardError) when none does. A component step
    covers its component and mode on the revision it was signed for only; a step on ``release`` covers
    only the request of the card's own release (``release=True``), and a pinned step never does."""
    admin_grant.check_usable(grant, card, now, policy=admin_grant.FACTORY, factory=catalog.scope)
    if requester not in catalog.requesters or card.assignees != (requester,):
        raise admin_grant.GrantError(f"{card.task_id}: requester @{requester} is not the card's assignee")
    if kind not in catalog.kinds:
        raise admin_grant.GrantError(f"{card.task_id}: kind {kind!r} is not in the factory catalog")
    if kind == COMPONENT:
        try:
            catalog.mode(component, mode)
        except ProposalError as exc:
            raise admin_grant.GrantError(f"{card.task_id}: {exc}")
        steps = [op for op in grant.operations if op.get("verb") == kind and op.get("component") == component
                 and op.get("mode") == mode]
        on_release = [op for op in steps if op.get("revision") == RELEASE_REVISION]
        steps = [op for op in steps if op.get("revision") != RELEASE_REVISION]
        if release:
            if on_release:
                return dict(on_release[0])
            raise admin_grant.GrantError(f"{card.task_id}: {component} {mode} is not approved for the release of "
                                         f"this card (a step on revision {RELEASE_REVISION!r})")
        for op in steps:
            if revision_matches(str(op.get("revision") or ""), revision):
                return dict(op)
        if on_release and not steps:
            raise admin_grant.GrantError(f"{card.task_id}: {component} {mode} is approved for the release of this "
                                         f"card — the release requests it")
        if steps:
            raise admin_grant.GrantError(
                f"{card.task_id}: {component} {mode} was approved for revision "
                f"{', '.join(sorted(str(op.get('revision'))[:12] for op in steps))}, the deployed tree is "
                f"{revision[:12]} — a new plan for this revision")
        raise admin_grant.GrantError(f"{card.task_id}: {component} {mode} not covered by the approved operations")
    wanted = set(agents)
    for op in grant.operations:
        if op.get("verb") == kind and wanted <= set(op.get("agents") or ()):
            return dict(op)
    covered = sorted({a for op in grant.operations if op.get("verb") == kind for a in op.get("agents") or ()})
    outside = sorted(wanted - set(covered))
    raise admin_grant.GrantError(f"{card.task_id}: {', '.join(outside) or kind} not covered by the approved "
                                 f"operations ({', '.join(covered) or 'none'})")
