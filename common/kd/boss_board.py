"""Finite BOSS board operations (BOSS spec sections 3.5 and 4.6, ADR 0001).

A pure model: ``plan`` checks one request against a card snapshot and the routing table and
returns the exact revision-checked edit (a dictionary of ``kd.cabinet.store.CHANGES``) or Inbox
create (``kd.cabinet.store.CREATE_KEYS``), which the cabinet service writes. There
is no generic edit: every operation names the only fields it may touch, and the only status
values it can emit are fixed (``Blocked -> To Do`` for ``unblock``, ``Inbox -> To Do`` for
``promote``, ``Inbox`` for ``create-inbox`` and ``create-child``). Title, description, acceptance criteria and every
other status stay out of reach. Nothing here writes to the board; the cabinet runs the plans
under its write lock. ``promote`` is the owner's move: BOSS only proposes it and the
owner's approval page runs it (``kd.boss_promote``); the plan only checks the card
is ready to be taken. ``create-child`` is a subtask of a goal the owner delegated: only
``kd.delegation`` plans it, after checking the owner's grant, and then promotes it under the same grant.

Revision discipline: a card's revision is the cabinet's integer counter, the request carries the
revision BOSS based its decision on, and the plan sends *that* revision. A card that changed
after BOSS read it is refused by the cabinet in the write transaction instead of being overwritten;
``unblock`` also pins every dependency's revision, so a dependency reopened in between refuses
the edit too.
"""

from dataclasses import dataclass, field
import re
from typing import Collection, Mapping, Optional, Tuple

from kd import ids


ACTOR = "boss"
AUTHOR = "@boss"
OWNER = "@owner"
SIGNATURE = "[boss]"
OPERATIONS = (
    "comment",
    "assign",
    "set-project",
    "set-priority",
    "set-labels",
    "set-dependency",
    "create-inbox",
    "create-child",
    "unblock",
    "promote",
)
PRIORITIES = ("high", "medium", "low")
INBOX = "Inbox"
BLOCKED = "Blocked"
TO_DO = "To Do"
DONE = "Done"
REASON_MAX = 500
TITLE_MAX = 120
LABELS_MAX = 8
DEPENDENCIES_MAX = 8  # every dependency is pinned by revision in one write

_TASK_ID = ids.CARD
_ASSIGNEE = re.compile(r"@[a-z][a-z0-9_-]{0,31}")
_PROJECT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
# Label and dependency values never contain a comma (the md export lists them comma-separated). ``P-<n>`` links a card
# to the monitor's incident (the cabinet writes it on "Link"; BOSS and workers may set it too).
_LABEL = re.compile(r"[a-z0-9][a-z0-9_.-]{0,31}(?::[A-Za-z0-9_.-]{1,32})?|P-[1-9][0-9]{0,8}")
SPEC_PREFIX = "spec:"  # the specialization label
SERVER_PREFIX = "server:"  # a card server, stored as card_servers; the rules see it as a label
_SPEC = re.compile(r"[a-z][a-z0-9-]{0,31}")
_DUPLICATE = re.compile(r"duplicate-of:(.*)")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f  ]")
# Shared with kd.worker_board: one spelling of a card id, label, assignee and project.
TASK_ID, LABEL, ASSIGNEE, PROJECT, CONTROL = _TASK_ID, _LABEL, _ASSIGNEE, _PROJECT, _CONTROL

_COMMON = frozenset({"op", "actor", "task", "revision", "reason"})
_FIELDS = {
    "comment": frozenset(),
    "assign": frozenset({"assignee"}),
    "set-project": frozenset({"project"}),
    "set-priority": frozenset({"priority"}),
    "set-labels": frozenset({"add", "remove"}),
    "set-dependency": frozenset({"add", "remove"}),
    "unblock": frozenset(),
    "promote": frozenset(),
}
_OPTIONAL = {"assign": frozenset({"spec"})}  # fields an operation may carry
# a create carries the look before it — prepare-create's token, the candidates read, why none is this card
_CREATE = frozenset({"op", "actor", "reason", "title", "project", "token", "reviewed", "difference"})
_CHILD = frozenset({"op", "actor", "reason", "goal", "grant", "title", "project", "assignee"})
_GRANT = re.compile(r"[0-9a-f]{32}")
_TOKEN = re.compile(r"[0-9a-f]{32}")  # prepare-create's
REVIEWED_MAX = 20


class Refused(ValueError):
    """The request is outside the finite operations; nothing may be written."""


class StaleRevision(Refused):
    """The card changed after BOSS read it."""


@dataclass(frozen=True)
class Card:
    """One card as the cabinet stores it; ``revision`` is its compare-and-set counter."""

    id: str
    revision: int
    status: str
    assignees: Tuple[str, ...]
    project: str
    priority: str
    labels: Tuple[str, ...]
    dependencies: Tuple[str, ...]
    title: str = ""  # shown to the owner on the promote page; no operation edits it
    stages: Tuple[Mapping, ...] = ()  # the brief's stages: an aggregate the analyst may re-plan


@dataclass(frozen=True)
class Routing:
    """``projects`` is the cabinet's projects table (project -> assignee); ``assignees`` the cards' allowed
    targets."""

    projects: Mapping[str, str]
    assignees: frozenset


@dataclass(frozen=True)
class Edit:
    """A revision-checked card write: refused by the cabinet if any pinned revision moved.

    ``changes`` holds only keys of ``kd.cabinet.store.CHANGES``; ``guards`` pins other cards."""

    task_id: str
    revision: int
    changes: Mapping
    guards: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class Create:
    """A new card that always lands in Inbox; the cabinet assigns the ID in the write."""

    values: Mapping


def routing_from(routes: Mapping, agents: Collection[str]) -> Routing:
    """Routing from the projects (project -> route) and the registered agent names (without ``@``).

    BOSS may assign to any registered agent or the owner, never to itself: a card on the
    coordinator becomes its own attention event. A board with no project yet routes nothing.
    """
    if not isinstance(routes, Mapping):
        raise Refused("routes must map projects to assignees")
    targets = {OWNER} | {"@" + name for name in agents if isinstance(name, str)}
    targets.discard(AUTHOR)
    for project, target in routes.items():
        if not isinstance(project, str) or not _PROJECT.fullmatch(project):
            raise Refused("invalid project in routes")
        if target not in targets:
            raise Refused(f"route for {project} names an unknown assignee")
    for target in targets:
        if not _ASSIGNEE.fullmatch(target):
            raise Refused("invalid agent name")
    return Routing(dict(routes), frozenset(targets))


def card_from(data: Mapping) -> Card:
    """A card as the cabinet returns it (``CardRecord.to_dict``): the rules see exactly the stored row.

    The card's servers come back as ``server:<alias>`` labels: the rules judge labels as they always did.
    """
    if not isinstance(data, Mapping) or not _TASK_ID.fullmatch(str(data.get("id", ""))):
        raise Refused("invalid card record")
    revision = data.get("revision")
    if not _revision(revision):
        raise Refused("invalid card revision")
    assignee = _scalar(data.get("assignee"))
    labels = _items(data.get("labels"))
    labels += [SERVER_PREFIX + s for s in _items(data.get("servers")) if SERVER_PREFIX + s not in labels]
    return Card(
        id=data["id"],
        revision=revision,
        status=_scalar(data.get("status")),
        assignees=(assignee,) if assignee else (),
        project=_scalar(data.get("project")),
        priority=_scalar(data.get("priority")),
        labels=tuple(labels),
        dependencies=tuple(d.upper() for d in _items(data.get("dependencies"))),
        title=_scalar(data.get("title")),
        stages=tuple(s for s in (data.get("stages") or ()) if isinstance(s, Mapping)),
    )


def _revision(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def plan(
    request: Mapping,
    routing: Routing,
    card: Optional[Card] = None,
    dependencies: Optional[Mapping[str, Card]] = None,
    specs: Optional[Mapping[str, Collection[str]]] = None,
):
    """Return the ``Edit`` or ``Create`` for one BOSS request, or raise ``Refused``.

    ``specs`` maps ``@agent`` to its installed specializations; ``assign`` with ``spec`` and ``set-labels`` that adds
    a ``spec:`` label need it.
    """
    if not isinstance(request, Mapping):
        raise Refused("request must be an object")
    op = request.get("op")
    if op not in OPERATIONS:
        raise Refused("unknown operation")
    if request.get("actor") != ACTOR:
        raise Refused("actor must be boss")
    reason = _reason(request.get("reason"))
    if op == "create-inbox":
        _keys(request, _CREATE)
        return _create(request, routing, reason)
    if op == "create-child":
        _keys(request, _CHILD)
        return _create_child(request, routing, reason)
    optional = _OPTIONAL.get(op, frozenset()) & set(request)
    _keys(request, _COMMON | _FIELDS[op] | optional)
    if card is None:
        raise Refused("card snapshot is required")
    if request.get("task") != card.id:
        raise Refused("request names another card")
    revision = request.get("revision")
    if not _revision(revision):
        raise Refused("invalid revision")
    if revision != card.revision:
        raise StaleRevision("card changed since it was read")
    if op == "unblock":
        return _unblock(card, revision, dependencies, reason)
    if op == "promote":
        return _promote(card, revision, routing, reason)
    if op == "assign":
        summary, changes = _assign(request, card, routing, specs)
    elif op == "set-labels":
        summary, changes = _set_labels(request, card, routing, specs)
    else:
        summary, changes = _OPS[op](request, card, routing)
    return Edit(card.id, revision, {**changes, **_comment(summary, reason)})


def needs_specs(request: Mapping, card: Optional[Card]) -> bool:
    """Whether ``plan`` judges this request against the specialization registry: an ``assign`` with ``spec`` or of a
    card that carries a ``spec:`` label, a ``set-labels`` that adds one. The registry is read only then."""
    op = request.get("op")
    if op == "assign":
        return bool(request.get("spec")) or (card is not None and any(
            v.startswith(SPEC_PREFIX) for v in card.labels))
    added = request.get("add") if op == "set-labels" else None
    return isinstance(added, list) and any(isinstance(v, str) and v.startswith(SPEC_PREFIX) for v in added)


def _assign(request, card: Card, routing: Routing, specs=None):
    """The assignee, and the ``spec:`` label too, in one write (spec 5.8.5): the receiver never
    sees a card with a new assignee and a specialization it does not hold — with ``spec`` the
    label is set, without it a label the target does not hold is removed."""
    target = request["assignee"]
    if not isinstance(target, str) or target not in routing.assignees:
        raise Refused("unknown assignee")
    add, remove = [], []
    summary = f"assign {target}"
    spec = request.get("spec")
    labelled = [v for v in card.labels if v.startswith(SPEC_PREFIX)]
    if spec is not None:
        if not isinstance(spec, str) or not _SPEC.fullmatch(spec):
            raise Refused("invalid spec name")
        if specs is None:
            raise Refused("no specialization registry: the spec: label cannot be set")
        if spec not in specs.get(target, ()):
            raise Refused(f"spec:{spec} is not a specialization of {target}")
        label = SPEC_PREFIX + spec
        summary += f" ({label})"
        if label not in card.labels:
            add.append(label)
        remove += [v for v in labelled if v != label]
    elif labelled:
        if specs is None:
            raise Refused("no specialization registry: the spec: label cannot be checked")
        held = specs.get(target, ())
        remove += [v for v in labelled if v[len(SPEC_PREFIX):] not in held]
    if card.assignees == (target,) and not add and not remove:
        raise Refused("nothing to change")
    return summary, {"assignee": target, **_label_changes(add, remove)}


def _set_project(request, card: Card, routing: Routing):
    """Only an Inbox card: one the owner started was approved in its project — its repository, its route, who sees
    it — and moving it on would carry that approval to another one."""
    project = request["project"]
    if not isinstance(project, str) or project not in routing.projects:
        raise Refused("unknown project")
    if card.status != INBOX:
        raise Refused(f"the project is set only in Inbox; {card.id} is {card.status}")
    if card.project == project:
        raise Refused("nothing to change")
    return f"project {project}", {"project": project}


def _set_priority(request, card: Card, routing: Routing):
    priority = request["priority"]
    if not isinstance(priority, str) or priority not in PRIORITIES:
        raise Refused("unknown priority")
    if card.priority == priority:
        raise Refused("nothing to change")
    return f"priority {priority}", {"priority": priority}


def _set_labels(request, card: Card, routing: Routing, specs=None):
    add, remove = _changes(request, _label)
    for label in add:
        duplicate = _DUPLICATE.fullmatch(label)
        if duplicate and duplicate.group(1) == card.id:
            raise Refused("a card cannot duplicate itself")
    add = [label for label in add if label not in card.labels]
    remove = [label for label in remove if label in card.labels]
    if not add and not remove:
        raise Refused("nothing to change")
    _spec_labels(card, add, remove, specs)
    return f"labels {_delta(add, remove)}", _label_changes(add, remove)


def _spec_labels(card: Card, add, remove, specs) -> None:
    """A ``spec:`` label set by hand names a specialization of the registry (spec 5.8.5): one the card's assignee
    holds, or — a card nobody holds yet — one some agent holds; and a card keeps one. Removing one needs nothing: a
    label that fits nobody is dropped this way."""
    added = [label[len(SPEC_PREFIX):] for label in add if label.startswith(SPEC_PREFIX)]
    if not added:
        return
    if specs is None:
        raise Refused("no specialization registry: the spec: label cannot be set")
    kept = [v for v in card.labels if v.startswith(SPEC_PREFIX) and v not in remove]
    if len(added) + len(kept) > 1:
        raise Refused("one spec: label per card: remove the other in the same set-labels, or assign --spec")
    name = added[0]
    if not _SPEC.fullmatch(name):
        raise Refused("invalid spec name")
    if card.assignees:
        if name not in specs.get(card.assignees[0], ()):
            raise Refused(f"spec:{name} is not a specialization of {card.assignees[0]}")
    elif not any(name in held for held in specs.values()):
        raise Refused(f"spec:{name} is no agent's specialization")


def _set_dependency(request, card: Card, routing: Routing):
    add, remove = _changes(request, _task_id)
    _card_dependencies(card)
    if not add and not remove:
        raise Refused("nothing to change")
    if card.id in add:
        raise Refused("a card cannot depend on itself")
    if any(dep in card.dependencies for dep in add) or any(
        dep not in card.dependencies for dep in remove
    ):
        raise Refused("dependency already set or not present")
    result = [dep for dep in card.dependencies if dep not in remove] + add
    if len(result) > DEPENDENCIES_MAX:
        raise Refused("too many dependencies")
    return f"dependencies {_delta(add, remove)}", {"dependencies": result}


def _unblock(card: Card, revision: int, dependencies, reason: str) -> Edit:
    """``Blocked -> To Do`` only when every structured dependency is Done, pinned by revision."""
    if card.status != BLOCKED:
        raise Refused("card is not Blocked")
    if not _card_dependencies(card):
        raise Refused("card has no structured dependency")
    if not isinstance(dependencies, Mapping) or set(dependencies) != set(
        card.dependencies
    ):
        raise Refused("dependency snapshots do not match the card")
    guards = {}
    for dep_id in card.dependencies:
        dep = dependencies[dep_id]
        if not isinstance(dep, Card) or dep.id != dep_id:
            raise Refused("dependency snapshots do not match the card")
        if dep.status != DONE:
            raise Refused(f"dependency {dep_id} is not Done")
        guards[dep_id] = dep.revision
    summary = f"unblock: {', '.join(card.dependencies)} Done"
    return Edit(card.id, revision, {"status": TO_DO, **_comment(summary, reason)}, guards)


def _promote(card: Card, revision: int, routing: Routing, reason: str) -> Edit:
    """``Inbox -> To Do`` for a card that is ready to be taken: routed, one known assignee."""
    if card.status != INBOX:
        raise Refused("card is not in Inbox")
    if card.project not in routing.projects:
        raise Refused("card project is not in routes")
    if len(card.assignees) != 1 or card.assignees[0] not in routing.assignees:
        raise Refused("card needs exactly one known assignee")
    return Edit(card.id, revision, {"status": TO_DO, **_comment(TO_DO, reason)})


def _title(title) -> str:
    if (
        not isinstance(title, str)
        or not title.strip()
        or len(title) > TITLE_MAX
        or _CONTROL.search(title)
        or title.lstrip().startswith("-")
    ):
        raise Refused("invalid title")
    return title.strip()


def _create(request, routing: Routing, reason: str) -> Create:
    title = _title(request["title"])
    project = request["project"]
    if not isinstance(project, str) or project not in routing.projects:
        raise Refused("unknown project")
    if not isinstance(request["token"], str) or not _TOKEN.fullmatch(request["token"]):
        raise Refused("create-inbox needs the token of prepare-create")
    reviewed = request["reviewed"]
    if not isinstance(reviewed, list) or len(reviewed) > REVIEWED_MAX or not all(
            isinstance(c, str) and TASK_ID.fullmatch(c) for c in reviewed):
        raise Refused("reviewed lists card ids")
    difference = request["difference"]
    if not isinstance(difference, str) or len(difference) > REASON_MAX or CONTROL.search(difference):
        raise Refused("difference is one line")
    # the look's basis (what was compared, how it differs) goes to the audit journal, not into the description:
    # the owner reads the card, not BOSS's duplicate check
    return Create({"title": title, "status": INBOX, "project": project,
                   "description": f"{SIGNATURE} {reason}", "created_by": AUTHOR})


def _create_child(request, routing: Routing, reason: str) -> Create:
    """A goal's subtask in Inbox with the project's routed agent; the description names the goal and the grant."""
    goal = _task_id(request["goal"])
    grant = request["grant"]
    if not isinstance(grant, str) or not _GRANT.fullmatch(grant):
        raise Refused("invalid grant")
    title = _title(request["title"])
    project = request["project"]
    if not isinstance(project, str) or project not in routing.projects:
        raise Refused("unknown project")
    assignee = request["assignee"]
    if assignee != routing.projects[project] or assignee not in routing.assignees or assignee == OWNER:
        # a grant's snapshot of the route that the owner changed since: propose the delegation again
        raise Refused("route_mismatch: assignee is not the project's agent route")
    description = f"{SIGNATURE} Подзадача цели {goal} (разрешение владельца {grant[:8]}): {reason}"
    return Create({"title": title, "status": INBOX, "project": project, "assignee": assignee,
                   "description": description, "created_by": AUTHOR, "parent": goal})


_OPS = {
    "comment": lambda request, card, routing: ("", {}),
    "assign": _assign,
    "set-project": _set_project,
    "set-priority": _set_priority,
    "set-labels": _set_labels,
    "set-dependency": _set_dependency,
}


def _card_dependencies(card: Card) -> Tuple[str, ...]:
    """The card's own dependency list, refused if the file holds anything we could not pin."""
    deps = tuple(_task_id(dep) for dep in card.dependencies)
    if len(set(deps)) != len(deps) or len(deps) > DEPENDENCIES_MAX:
        raise Refused("card dependencies cannot be pinned")
    return deps


def _keys(request: Mapping, allowed: frozenset) -> None:
    extra = sorted(str(key) for key in set(request) - allowed)
    if extra:
        raise Refused(f"field not allowed: {', '.join(extra)}")
    missing = sorted(allowed - set(request))
    if missing:
        raise Refused(f"missing field: {', '.join(missing)}")


def _reason(value) -> str:
    if not isinstance(value, str) or _CONTROL.search(value):
        raise Refused("reason must be one line of text")
    value = value.strip()
    if not value or len(value) > REASON_MAX:
        raise Refused("reason is empty or too long")
    return value


def _comment(summary: str, reason: str) -> dict:
    text = f"{SIGNATURE} {summary}: {reason}" if summary else f"{SIGNATURE} {reason}"
    return {"comment": {"text": text, "author": AUTHOR}}


def _label_changes(add, remove) -> dict:
    """``labels_add`` / ``labels_remove``, each only when it changes something."""
    changes = {}
    if add:
        changes["labels_add"] = list(add)
    if remove:
        changes["labels_remove"] = list(remove)
    return changes


def _changes(request: Mapping, check) -> Tuple[list, list]:
    add, remove = request["add"], request["remove"]
    if not isinstance(add, list) or not isinstance(remove, list):
        raise Refused("add and remove must be lists")
    if len(add) + len(remove) > LABELS_MAX:
        raise Refused("too many changes")
    add, remove = [check(v) for v in add], [check(v) for v in remove]
    if (
        len(set(add)) != len(add)
        or len(set(remove)) != len(remove)
        or set(add) & set(remove)
    ):
        raise Refused("repeated value")
    return add, remove


def _label(value) -> str:
    if not isinstance(value, str) or not _LABEL.fullmatch(value):
        raise Refused("invalid label")
    duplicate = _DUPLICATE.fullmatch(value)
    if duplicate:
        _task_id(duplicate.group(1))
    return value


def _task_id(value) -> str:
    if not isinstance(value, str) or not _TASK_ID.fullmatch(value):
        raise Refused("invalid task id")
    return value


def _delta(add, remove) -> str:
    return " ".join([f"+{v}" for v in add] + [f"-{v}" for v in remove])


def _scalar(value) -> str:
    return value if isinstance(value, str) else ""


def _items(value) -> list:
    if isinstance(value, str):
        value = [value]
    return [str(v).strip() for v in (value or []) if str(v).strip()]
