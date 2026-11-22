"""Finite board operations of a worker agent (BOSS spec 5.8.8).

A pure model, the worker-side twin of ``kd.boss_board``: ``plan`` checks one request of the
``kd-task`` executor against the caller's identity (the channel's ``KD_AGENT``, never an argument)
and a card snapshot, and returns the one revision-checked edit (a dictionary of
``kd.cabinet.store.CHANGES``) or the one create in Inbox; the cabinet writes it. A card is the caller's only when the caller is its sole assignee. A status moves only
along ``TRANSITIONS``; nothing leaves Inbox (the owner's page does that), ``claim`` is the only way
from To Do, and Done is final. The executor signs notes and comments with the caller's name.
A ``spec:`` label names a specialization its holder has: ``specs`` maps
``@agent`` to the installed names, and no registry means no label can be set.
"""

import re
from typing import Collection, Mapping, Optional

from kd.boss_board import (ASSIGNEE, BLOCKED, CONTROL, Card, Create, DEPENDENCIES_MAX, DONE, Edit, INBOX, LABEL,
                           TASK_ID, TO_DO)

OPERATIONS = ("claim", "create", "edit", "handoff")
IN_PROGRESS = "In Progress"
REVIEW = "Review"
COORDINATOR = "@boss"
ANALYST = "@analyst"
BRIEF_READY = "brief-ready"
SIZE_PREFIX = "size:"  # the analyst's size of a brief: S starts by itself (kd.cabinet.autostart)
OWNER = "@owner"
TRANSITIONS = {
    IN_PROGRESS: frozenset({REVIEW, BLOCKED, DONE}),
    REVIEW: frozenset({IN_PROGRESS, BLOCKED, DONE}),
    BLOCKED: frozenset({IN_PROGRESS}),
}
TEXT_MAX = 4000
TITLE_MAX = 100  # the title names the exported md file
ITEMS_MAX = 8
SPEC_PREFIX = "spec:"
_SPEC = re.compile(r"[a-z][a-z0-9-]{0,31}")
_INDEX = re.compile(r"[1-9][0-9]{0,2}")
_TEXT_CONTROL = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")  # a note may span lines
# A note or comment is markdown in the card's md export and on the page: a heading would open a new
# section of the exported card (a planted "Definition of Done" would read as part of the scope), an HTML
# comment hides text from the page. Plain text has neither.
_SECTION_LIKE = re.compile(r"^ {0,3}(?:#{1,6}(?:[ \t]|$)|<!--)", re.MULTILINE)
_SIGNATURE = re.compile(r"\[([a-z][a-z0-9_-]{0,31})\] ")
# Request keys per operation; the CLI builds exactly these (lists for repeatable flags).
EDIT_KEYS = frozenset({
    "status", "append_notes", "check_ac", "uncheck_ac", "comment", "final_summary", "dep",
    "clear_deps", "add_label", "remove_label", "description", "ac", "remove_ac", "spec", "operations", "stages",
})
# an own Inbox card only; "Operations" come checked against the factory catalog by the cabinet, "Stages"
# by its stages module
DRAFT_KEYS = frozenset({"description", "ac", "remove_ac", "operations", "stages"})
# The card's scope: written only over the revision the agent read (``revision`` in the request)
SCOPE_KEYS = frozenset({"description", "ac", "remove_ac", "operations", "stages"})
CREATE_KEYS = frozenset({"title", "project", "to", "description", "ac", "label", "dep"})
HANDOFF_KEYS = frozenset({"to", "spec", "add_label", "comment"})
CLAIM_KEYS = frozenset({"note"})  # the executor's own specialization line, never a caller argument


class Refused(ValueError):
    """The request is outside the finite operations; nothing may be written."""


def signed(agent: str, text: str) -> str:
    """``[agent] text``, once: the caller's own signature is kept, any other is just text."""
    match = _SIGNATURE.match(text)
    if match and match.group(1) == agent:
        return text
    return f"[{agent}] {text}"


def plan(
    op: str,
    request: Mapping,
    *,
    agent: str,
    card: Optional[Card] = None,
    projects=frozenset(),
    targets=frozenset(),
    specs: Optional[Mapping[str, Collection[str]]] = None,
):
    """The ``Edit`` or ``Create`` for one worker request, or ``Refused``."""
    if op not in OPERATIONS:
        raise Refused("unknown operation")
    if not isinstance(request, Mapping):
        raise Refused("request must be an object")
    me = f"@{agent}"
    if op == "create":
        return _create(request, me, projects, specs)
    if card is None:
        raise Refused("card snapshot is required")
    if op == "claim":
        return _claim(request, card, me)
    if op == "handoff":
        return _handoff(request, card, me, targets, specs)
    return _edit(request, card, me, specs)


def _allowed_spec(name, assignee: str, specs) -> str:
    """``name`` must be one of the assignee's installed specializations (spec 5.8.5): the
    label never names what its holder cannot do. No registry at all is a refusal too."""
    name = _spec(name)
    if specs is None:
        raise Refused("no specialization registry: the spec: label cannot be set")
    if name not in specs.get(assignee, ()):
        raise Refused(f"spec:{name} is not a specialization of {assignee}")
    return name


def claimable(card: Card, me: str) -> None:
    """Refuse a card ``me`` may not take: not in To Do, or someone else's. The executor asks
    this before it judges the card's ``spec:`` label, so the advice "hand the card to @boss"
    is only ever given about a card that is claimable."""
    if card.status != TO_DO:
        raise Refused(f"{card.id} is not in To Do: {card.status}")
    if card.assignees and card.assignees != (me,):
        raise Refused(f"{card.id} is already assigned to {', '.join(card.assignees)}")


def _claim(request: Mapping, card: Card, me: str) -> Edit:
    _keys(request, CLAIM_KEYS)
    claimable(card, me)
    changes = {"status": IN_PROGRESS, "assignee": me}
    if request.get("note") is not None:
        # The executor's own line (kd.specializations.note): which text the work follows.
        changes["notes_append"] = signed(me[1:], _text(request["note"]))
    return Edit(card.id, card.revision, changes)


def _edit(request: Mapping, card: Card, me: str, specs=None) -> Edit:
    _keys(request, EDIT_KEYS)
    agent = me[1:]
    changes = {}
    comment = request.get("comment")
    replanning = _replanning(request, card, me)
    if card.assignees != (me,) and not replanning:
        if set(k for k, v in request.items() if v) - {"comment"}:
            raise Refused(f"{card.id} is not your card: only a comment is allowed")
    status = request.get("status")
    if status is not None:
        if status == card.status:
            raise Refused(f"{card.id} is already {status}")
        if card.status == INBOX:
            raise Refused(f"{card.id} is in Inbox: only the owner's page moves it out")
        if card.status == TO_DO:
            raise Refused(f"{card.id} is in To Do: take it with kd-task claim")
        if status not in TRANSITIONS.get(card.status, ()):
            raise Refused(f"{card.id}: {card.status} -> {status} is not allowed")
        changes["status"] = status
    for key in sorted(DRAFT_KEYS & {k for k, v in request.items() if v}):
        if card.status != INBOX and not (replanning and key == "stages"):
            raise Refused(f"{card.id}: --{key.replace('_', '-')} only on your draft in Inbox")
    notes = [signed(agent, _text(t)) for t in _list(request, "append_notes")]
    if notes:
        changes["notes_append"] = "\n".join(notes)
    for key, change in (("check_ac", "ac_check"), ("uncheck_ac", "ac_uncheck"), ("remove_ac", "ac_remove")):
        indices = _indices(request, key)
        if indices:
            changes[change] = indices
    added = [_text(t) for t in _list(request, "ac")]
    if added:
        changes["ac_add"] = added
    if request.get("description") is not None:
        changes["description"] = _text(request["description"])
    if request.get("operations") is not None:  # [] takes the brief's "Operations" off
        changes["operations"] = list(request["operations"])
    if request.get("stages") is not None:  # [] takes the brief's "Stages" off
        changes["stages"] = list(request["stages"])
    if request.get("final_summary") is not None:
        changes["summary"] = signed(agent, _text(request["final_summary"]))
    dependencies = _dependencies(request, card)
    if dependencies is not None:
        changes["dependencies"] = dependencies
    add = [_label(v) for v in _list(request, "add_label")]
    remove = [_label(v) for v in _list(request, "remove_label")]
    if any(v.startswith(SPEC_PREFIX) for v in add + remove):
        raise Refused("the spec: label is set with --spec")
    spec = request.get("spec")
    if spec is not None:
        if card.status not in (INBOX, TO_DO, IN_PROGRESS, BLOCKED):
            raise Refused(f"{card.id}: instruction selection is only for a draft or active work")
        remove += [v for v in card.labels if v.startswith(SPEC_PREFIX)]
        add.append(SPEC_PREFIX + _allowed_spec(spec, me, specs))
    # a re-plan says brief-ready again on purpose: the write records the new version (briefs.after_edit)
    changes.update(_label_changes([v for v in add if v not in card.labels or (replanning and v == BRIEF_READY)],
                                  [v for v in remove if v in card.labels]))
    if comment is not None:
        changes["comment"] = {"text": signed(agent, _text(comment)), "author": me}
    if not changes:
        raise Refused("nothing to change")
    return Edit(card.id, card.revision, changes)


def _keys(request: Mapping, allowed: frozenset) -> None:
    extra = sorted(str(k) for k in set(request) - allowed)
    if extra:
        raise Refused(f"field not allowed: {', '.join(extra)}")


def _list(request: Mapping, key: str) -> list:
    value = request.get(key) or []
    if not isinstance(value, list) or len(value) > ITEMS_MAX:
        raise Refused(f"{key}: at most {ITEMS_MAX} values")
    return value


def _replanning(request: Mapping, card: Card, me: str) -> bool:
    """The analyst re-plans an aggregate in work — new stages and ``brief-ready`` on a card whose plan
    was approved (it has stages), nothing else; the owner's "Approve the stages" materializes them."""
    asked = {k for k, v in request.items() if v}
    return (me == ANALYST and bool(card.stages) and card.status in (TO_DO, IN_PROGRESS)
            and "stages" in asked and asked <= {"stages", "add_label", "comment"}
            and set(request.get("add_label") or ()) <= {BRIEF_READY})


def _text(value) -> str:
    if not isinstance(value, str) or _TEXT_CONTROL.search(value) or _SECTION_LIKE.search(value):
        raise Refused("text must be plain text: no control characters, headings or HTML comments")
    value = value.strip()
    if not value or len(value) > TEXT_MAX:
        raise Refused(f"text is empty or longer than {TEXT_MAX}")
    if value.startswith("-"):
        raise Refused("text may not start with -")
    return value


def _indices(request: Mapping, key: str) -> list:
    """1-based acceptance criteria indexes, as the CLI gives them (strings) or as JSON numbers."""
    out = []
    for value in _list(request, key):
        if isinstance(value, int) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str) or not _INDEX.fullmatch(value):
            raise Refused(f"--{key.replace('_', '-')}: index must be 1..999")
        out.append(int(value))
    return out


def _label_changes(add, remove) -> dict:
    changes = {}
    if add:
        changes["labels_add"] = list(add)
    if remove:
        changes["labels_remove"] = list(remove)
    return changes


def _label(value) -> str:
    if not isinstance(value, str) or not LABEL.fullmatch(value):
        raise Refused("invalid label")
    return value


def _spec(value) -> str:
    if not isinstance(value, str) or not _SPEC.fullmatch(value):
        raise Refused("invalid spec name")
    return value


def _task_id(value) -> str:
    if not isinstance(value, str) or not TASK_ID.fullmatch(value):
        raise Refused("invalid task id")
    return value


def _dependencies(request: Mapping, card: Optional[Card]) -> Optional[list]:
    """The card's new dependency list, or None when the request leaves it alone."""
    deps = [_task_id(v) for v in _list(request, "dep")]
    if request.get("clear_deps"):
        if deps:
            raise Refused("--clear-deps cannot be combined with --dep")
        return [] if card is None or card.dependencies else None
    if not deps:
        return None
    if card is not None and card.id in deps:
        raise Refused("a card cannot depend on itself")
    if len(set(deps)) != len(deps) or len(deps) > DEPENDENCIES_MAX:
        raise Refused("repeated dependency or too many")
    return deps


def _title(value) -> str:
    if not isinstance(value, str) or CONTROL.search(value):
        raise Refused("invalid title")
    value = value.strip()
    if not value or len(value) > TITLE_MAX or value.startswith("-"):
        raise Refused(f"invalid title: 1..{TITLE_MAX} characters, not starting with -")
    return value


def _create(request: Mapping, me: str, projects, specs=None) -> Create:
    _keys(request, CREATE_KEYS)
    title = _title(request.get("title"))
    project = request.get("project")
    if not isinstance(project, str) or project not in projects:
        raise Refused("unknown project")
    to = request.get("to") or me
    if to not in (me, COORDINATOR):
        raise Refused("a new card goes to only yourself or @boss")
    values = {"title": title, "status": INBOX, "project": project, "assignee": to, "created_by": me}
    if request.get("description") is not None:
        values["description"] = signed(me[1:], _text(request["description"]))
    acceptance = [_text(t) for t in _list(request, "ac")]
    if acceptance:
        values["acceptance"] = acceptance
    labels = [_label(v) for v in _list(request, "label")]
    named = [v for v in labels if v.startswith(SPEC_PREFIX)]
    if len(named) > 1:
        raise Refused("one spec: label per card")
    if named:  # the label never names what its holder cannot do (spec 5.8.5)
        _allowed_spec(named[0][len(SPEC_PREFIX):], to, specs)
    if labels:
        values["labels"] = labels
    dependencies = _dependencies(request, None)  # --clear-deps is not a create key
    if dependencies:
        values["dependencies"] = dependencies
    return Create(values)


def _handoff(request: Mapping, card: Card, me: str, targets, specs=None) -> Edit:
    _keys(request, HANDOFF_KEYS)
    if card.assignees != (me,):
        raise Refused(f"{card.id} is not your card")
    to = request.get("to")
    if to == me:
        raise Refused("handoff to yourself changes nothing")
    if not isinstance(to, str) or not ASSIGNEE.fullmatch(to) or to not in targets or to == OWNER:
        raise Refused("unknown target: a registered agent or @boss")
    add, remove = [], []
    summary = f"handoff -> {to}"
    spec = request.get("spec")
    labelled = [v for v in card.labels if v.startswith(SPEC_PREFIX)]
    if spec is not None:
        name = SPEC_PREFIX + _allowed_spec(spec, to, specs)
        summary += f" ({name})"
        if name not in card.labels:
            add.append(name)
        remove += [v for v in labelled if v != name]
    elif labelled:
        # The receiver never sees a specialization it does not hold (spec 5.8.5): one write.
        if specs is None:
            raise Refused("no specialization registry: the spec: label cannot be checked")
        held = specs.get(to, ())
        remove += [v for v in labelled if v[len(SPEC_PREFIX):] not in held]
    # Plain labels travel in the same write (the analyst's brief-ready): no intermediate revision.
    for value in _list(request, "add_label"):
        label = _label(value)
        if label.startswith(SPEC_PREFIX):
            raise Refused("a spec: label is set with spec, not add_label")
        if label.startswith(SIZE_PREFIX):  # the brief's size: this handoff's, replacing an earlier one
            if any(v.startswith(SIZE_PREFIX) for v in add):
                raise Refused("one size: label per handoff")
            remove += [v for v in card.labels if v.startswith(SIZE_PREFIX) and v != label]
            add.append(label)  # even when the card has it: the version this write records carries it
        elif label not in card.labels and label not in add:
            add.append(label)
    comment = request.get("comment")
    text = f"{summary}: {_text(comment)}" if comment is not None else summary
    return Edit(card.id, card.revision, {"assignee": to, **_label_changes(add, remove),
                                         "comment": {"text": signed(me[1:], text), "author": me}})
