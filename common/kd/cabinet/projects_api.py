"""The owner's projects: the cabinet's projects table is the board's only source of projects, and these
operations are the only writers of it after its first seeding.

``project.create {name, route[, id]}`` — the id comes from the name (Latin letters, digits and dashes, up to 32,
Cyrillic transliterated) unless given; it is unique and never changes. ``project.update {id[, name][, route]
[, memory_bank][, done_by]}`` — a new route counts for the cards made from now on: an open card keeps its assignee; a
memory bank is the Hindsight bank the session host gives the sessions of the project's cards started from
now on (``""`` — the role bank); ``done_by`` is the agent that closes the project's cards Done itself
(``""`` — the owner's acceptance; ``kd.cabinet.outcomes.done_refusal``); ``model`` and ``effort`` are the
defaults of its cards' sessions — an enabled cabinet model or ``""`` (the subscription), an effort level or ``""``
(the agent's own); ``council`` is the default council of its cards (``kd.council``; ``{}`` — none).
``project.delete {id[, target]}``
— a project with cards needs another project to take them: every card moves there in one transaction with its
thread, files and history (they hang on the card), each gets a line in its thread, and its open decisions are
withdrawn (BOSS proposes again); the project's pull requests follow, the people's roles lose it, and the BOSS
grants stop naming it. A project bound to a repository (``/etc/digipaddock/repos.json``) is not
deleted. The web asks a step-up before a delete (``web.stepup``, purpose ``projects.delete``).

A repository: ``project.repo.bind {id, repo}`` and ``project.repo.unbind {id}`` only record the owner's
request (``owner/repo`` checked here, before any request) and touch the doorbell of the root unit kd-repo-attach. It
takes ``project.repo.queue`` (root), runs add_repo.sh for each and answers ``project.repo.result`` (root): connected,
no access for dev, no key, hooks refused, or an error. The release flag is never set from here: a project kd-merge
--release deploys is neither unbound nor moved to another repository (root's add_repo.sh only).

A route is ``@owner`` or a registered agent other than the coordinator. A person needs ``settings``; root and the
cabinet itself (its CLI) may too. The Projects section reads ``project.routes`` for its form and
``project.impact {id}`` for the delete dialog.
"""

import json
import re

from kd import agents, memory_bank, worker_board
from kd.cabinet import api, decisions, schema, store

ID = re.compile(r"[a-z][a-z0-9-]{0,31}")
ID_MAX = 32
NAME_MAX = 60
REPOS = "/etc/digipaddock/repos.json"
# owner/repo from the cabinet: add_repo.sh's own rule (dev_agent.repos.REPO_RE), each part from a letter or
# a digit; the root unit checks it again (dev_agent.repo_attach.REPO_RE — the same pattern)
REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
DOORBELL = "/var/lib/digipaddock/repo-attach/request"
DETAIL_MAX = 300
CREATE_KEYS = frozenset({"op", "name", "route", "id"})
UPDATE_KEYS = frozenset({"op", "id", "name", "route", "memory_bank", "done_by", "model", "effort", "council"})
DELETE_KEYS = frozenset({"op", "id", "target"})
MANAGERS = frozenset({"user", "root", "cabinet"})
_TRANSLIT = dict(zip(
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
    ["a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u", "f",
     "h", "ts", "ch", "sh", "sch", "", "y", "", "e", "yu", "ya"],
))


def slug(name: str) -> str:
    """The id a project's name gives: lower case, Cyrillic in Latin letters, anything else a dash, at most 32; ``""``
    when nothing usable is left."""
    text = "".join(_TRANSLIT.get(c, c) for c in name.lower())
    text = re.sub(r"[^a-z0-9]+", "-", text)
    text = re.sub(r"^[^a-z]+", "", text)[:ID_MAX]
    return text.strip("-")


def _manager(ctx, principal):
    api._require(ctx, principal, "settings")


def _keys(request, allowed):
    extra = set(request) - allowed
    if "new_id" in extra or "rename" in extra:
        raise api.ApiError("invalid", "a project's id never changes")
    if extra:
        raise api.ApiError("invalid", f"unknown field: {', '.join(sorted(extra))}")


def _name(value) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > NAME_MAX or any(
        ord(c) < 32 for c in value
    ):
        raise api.ApiError("invalid", f"a project's name is text of 1 to {NAME_MAX} characters")
    return value.strip()


def _routes(ctx) -> list:
    """What a project may route to: ``@owner`` first, then the registered agents but the coordinator (a card on it
    is its own attention event)."""
    try:
        names = set(agents.names(ctx.registry_path))
    except (OSError, ValueError) as exc:
        raise api.ApiError("unavailable", f"agent registry unreadable ({exc.__class__.__name__})")
    return [api.OWNER] + sorted(f"@{name}" for name in names if f"@{name}" != worker_board.COORDINATOR)


def _route(ctx, value) -> str:
    if value == api.OWNER:
        return value
    if not isinstance(value, str) or value not in _routes(ctx):
        raise api.ApiError("invalid", f"unknown route {value!r}: the owner or a registered agent")
    return value


def _existing(ctx, project_id) -> dict:
    if not isinstance(project_id, str) or not ID.fullmatch(project_id):
        raise api.ApiError("invalid", "invalid project id")
    found = ctx.store.project(project_id)
    if found is None:
        raise api.ApiError("not_found", f"no project {project_id}")
    return found


def registry(path=REPOS) -> dict:
    """``/etc/digipaddock/repos.json`` as it is: project -> ``{repo, dir[, release]}``; no file — nothing bound,
    a file that is there but unreadable refuses (a delete or a request never guesses)."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise api.ApiError("unavailable", f"the repository registry is unreadable ({exc.__class__.__name__})")
    if not isinstance(data, dict):
        raise api.ApiError("unavailable", "the repository registry is not a map of projects")
    return data


def bound_repos(path=REPOS) -> set:
    """The projects bound to a repository."""
    return set(registry(path))


def _entry(found_registry, project_id) -> dict:
    entry = found_registry.get(project_id)
    return entry if isinstance(entry, dict) else {}


def repo_view(row: dict, found_registry: dict) -> dict:
    """A project's repository as the screens show it: an open request as it waits; else the registry is the
    truth of what is bound (a project bound by hand with add_repo.sh shows ``connected`` too), and the last failed
    request is shown until the next one. ``repo_release`` — kd-merge --release deploys it (never set from here)."""
    out = dict(row)
    entry = _entry(found_registry, row["id"])
    out["repo_release"] = bool(entry.get("release"))
    if row["repo_state"] in schema.REPO_REQUESTS:
        return out
    if entry:
        out["repo"] = entry.get("repo", "")
        if row["repo"] != out["repo"] or not row["repo_state"]:
            out.update(repo_state="connected", repo_detail="")
    elif row["repo_state"] == "connected":  # taken out of the registry by hand
        out.update(repo="", repo_state="", repo_detail="")
    return out


def with_repos(ctx, rows) -> list:
    """``repo_view`` of each row; an unreadable registry shows the rows as the base holds them (a list never fails)."""
    try:
        found = registry(ctx.repos_path)
    except api.ApiError:
        found = None
    if found is None:
        return [{**row, "repo_release": False} for row in rows]
    return [repo_view(row, found) for row in rows]


def _repo_name(value) -> str:
    """``owner/repo`` as the cabinet takes it: what add_repo.sh takes, but each part starts with a letter or a digit
    (no clone in ``work/.claude``) and no ``..``."""
    if not isinstance(value, str) or not REPO.fullmatch(value) or ".." in value:
        raise api.ApiError(
            "invalid", "the repository is owner/repo: Latin letters, digits, '_', '.', '-', each part from a letter or digit"
        )
    return value


def ring(ctx) -> bool:
    """Touch the root unit's doorbell after the commit; a bell that fails leaves the request waiting in the base (the
    unit reads them all at its next start, and the cabinet rings again at its own start while any waits)."""
    if not ctx.repo_doorbell:
        return False
    try:
        with open(ctx.repo_doorbell, "w", encoding="utf-8") as handle:
            handle.write(ctx.now() + "\n")
        return True
    except OSError:
        return False


def _bind(ctx, principal, request):
    _manager(ctx, principal)
    _keys(request, frozenset({"op", "id", "repo"}))
    found = _existing(ctx, request.get("id"))
    repo = _repo_name(request.get("repo"))
    if found["repo_state"] in schema.REPO_REQUESTS:
        raise api.ApiError("refused", f"project {found['id']}: a repository request is waiting", reason="repo_pending")
    entry = _entry(registry(ctx.repos_path), found["id"])
    if entry.get("release") and entry.get("repo") != repo:
        raise api.ApiError(
            "refused", f"project {found['id']} is deployed by kd-merge --release from {entry.get('repo')}: "
            "only root changes its repository", reason="repo_release",
        )
    project = ctx.store.set_project_repo(found["id"], actor=api.card_actor(principal), repo=repo, state="pending")
    return {"project": repo_view(project, {}), "rung": ring(ctx)}


def _unbind(ctx, principal, request):
    _manager(ctx, principal)
    _keys(request, frozenset({"op", "id"}))
    found = _existing(ctx, request.get("id"))
    if found["repo_state"] in schema.REPO_REQUESTS:
        raise api.ApiError("refused", f"project {found['id']}: a repository request is waiting", reason="repo_pending")
    entry = _entry(registry(ctx.repos_path), found["id"])
    actor = api.card_actor(principal)
    if not entry:
        if not found["repo"]:
            raise api.ApiError("invalid", f"project {found['id']} has no repository")
        # a request that never bound (no access, hooks…): nothing in the registry to take out
        project = ctx.store.set_project_repo(found["id"], actor=actor, repo="", state="")
        return {"project": repo_view(project, {}), "rung": False}
    if entry.get("release"):
        raise api.ApiError(
            "refused", f"project {found['id']} is deployed by kd-merge --release: only root unbinds it",
            reason="repo_release",
        )
    project = ctx.store.set_project_repo(
        found["id"], actor=actor, repo=entry.get("repo", found["repo"]), state="unbinding"
    )
    return {"project": repo_view(project, {}), "rung": ring(ctx)}


def _queue(ctx, principal, request):
    """The requests the root unit takes: ``bind`` with the repository, or ``unbind``."""
    _keys(request, frozenset({"op"}))
    return {"items": [
        {"id": row["id"], "repo": row["repo"], "action": "bind" if row["repo_state"] == "pending" else "unbind"}
        for row in ctx.store.repo_requests()
    ]}


def _result(ctx, principal, request):
    """The root unit's answer to one request: ``bind`` — one of ``schema.REPO_RESULTS``; ``unbind`` — ``""`` (taken out
    of the registry) or ``error``. An answer to a request that is no longer the open one is refused (stale)."""
    _keys(request, frozenset({"op", "id", "action", "repo", "state", "detail"}))
    found = _existing(ctx, request.get("id"))
    action, repo, state = request.get("action"), request.get("repo"), request.get("state")
    detail = request.get("detail", "")
    if not isinstance(detail, str):
        raise api.ApiError("invalid", "detail is text")
    detail = detail.strip()[:DETAIL_MAX]
    want = {"bind": "pending", "unbind": "unbinding"}.get(action)
    if want is None:
        raise api.ApiError("invalid", "action is bind or unbind")
    if found["repo_state"] != want or found["repo"] != repo:
        raise api.ApiError("refused", f"project {found['id']}: no such open request", reason="stale")
    if action == "bind" and state not in schema.REPO_RESULTS:
        raise api.ApiError("invalid", f"a bind ends {', '.join(schema.REPO_RESULTS)}")
    if action == "unbind" and state not in ("", "error"):
        raise api.ApiError("invalid", "an unbind ends '' or error")
    if action == "unbind" and state == "":
        repo = ""
    project = ctx.store.set_project_repo(
        found["id"], actor=api.card_actor(principal), repo=repo, state=state, detail=detail
    )
    return {"project": project}


def _create(ctx, principal, request):
    _manager(ctx, principal)
    _keys(request, CREATE_KEYS)
    name = _name(request.get("name"))
    project_id = request.get("id", slug(name))
    if not isinstance(project_id, str) or not ID.fullmatch(project_id):
        raise api.ApiError(
            "invalid", "the id is 1 to 32 Latin letters, digits or dashes, a letter first; give one with the name"
        )
    route = _route(ctx, request.get("route"))
    try:
        project = ctx.store.create_project(project_id, name, route, actor=api.card_actor(principal))
    except store.Conflict:
        raise api.ApiError("invalid", f"project {project_id} exists")
    return {"project": project}


def _memory_bank(value) -> str:
    """A Hindsight bank id as the session host and agent-launch take it (``kd.memory_bank.BANK``), or ``""``."""
    if value != "" and not (isinstance(value, str) and memory_bank.BANK.fullmatch(value)):
        raise api.ApiError(
            "invalid", "a memory bank is 1 to 100 Latin letters, digits, '_', '.', ':', '-', not starting with '.', "
            "'-' or ':'; empty — the role bank"
        )
    return value


def _done_by(ctx, value) -> str:
    """An agent a project's route may name, without the ``@`` (the coordinator closes no card), or ``""``."""
    if value != "" and not (isinstance(value, str) and f"@{value}" in _routes(ctx)[1:]):
        raise api.ApiError("invalid", f"unknown done_by {value!r}: a registered agent or '' (the owner's acceptance)")
    return value


def _update(ctx, principal, request):
    _manager(ctx, principal)
    _keys(request, UPDATE_KEYS)
    found = _existing(ctx, request.get("id"))
    name = _name(request["name"]) if "name" in request else None
    route = _route(ctx, request["route"]) if "route" in request else None
    bank = _memory_bank(request["memory_bank"]) if "memory_bank" in request else None
    closer = _done_by(ctx, request["done_by"]) if "done_by" in request else None
    # the defaults of the project's cards' sessions: an enabled model or '' (the subscription), an effort
    # level or '' (the agent's)
    model = _model(ctx, request["model"]) if "model" in request else None
    effort = _effort(request["effort"]) if "effort" in request else None
    # the default council of the project's cards: a kind without critics is simply not there
    council = _council(ctx, request["council"], found) if "council" in request else None
    if all(v is None for v in (name, route, bank, closer, model, effort, council)):
        raise api.ApiError("invalid",
                           "nothing to change: name, route, memory_bank, done_by, model, effort or council")
    project = ctx.store.update_project(found["id"], actor=api.card_actor(principal), name=name, route=route,
                                       memory_bank=bank, done_by=closer, model=model, effort=effort, council=council)
    return {"project": project}


def _council(ctx, value, found) -> dict:
    from kd.cabinet import councils  # lazily, as the models: it imports the API

    return councils.check(ctx.store, value, keep_empty=False, stored=found["council"], ctx=ctx, project=found["id"])


def _model(ctx, value) -> str:
    from kd.cabinet import llm_api  # lazily: it imports the API, which imports this module at its end

    try:
        return llm_api.check_model(ctx.store, value)
    except api.ApiError as exc:
        raise api.ApiError(exc.kind, f"model: {exc}") from None


def _effort(value) -> str:
    from kd.cabinet import llm_api

    try:
        return llm_api.check_effort(value)
    except api.ApiError as exc:
        raise api.ApiError(exc.kind, f"effort: {exc}") from None


def _delete(ctx, principal, request):
    _manager(ctx, principal)
    _keys(request, DELETE_KEYS)
    found = _existing(ctx, request.get("id"))
    if found["id"] in bound_repos(ctx.repos_path) or found["repo_state"] in schema.REPO_REQUESTS:
        raise api.ApiError(
            "refused", f"project {found['id']} is bound to a repository; unbind it first", reason="project_bound"
        )
    cards = ctx.store.project_cards(found["id"])
    target = request.get("target", "")
    if cards and not target:
        raise api.ApiError("invalid", f"project {found['id']} has {len(cards)} cards: name the project to take them")
    if target:
        if target == found["id"]:
            raise api.ApiError("invalid", "the cards cannot move into the project being deleted")
        target = _existing(ctx, target)["id"]
    actor, now = api.card_actor(principal), ctx.now()
    ledger = decisions.Decisions(ctx.store)
    withdrawn = 0
    with ctx.store.transaction():  # all of it or nothing: a failure in the middle keeps every card where it was
        for card_id in cards:
            mine = ledger.list(state="open", card=card_id)
            for decision in mine:
                ledger.withdraw(decision["id"], user=principal.user, now=now,
                                reply=f"проект {found['id']} удалён, карточка перенесена в {target}")
            withdrawn += len(mine)
            note = f"[{principal.user}] проект {found['id']} удалён — карточка перенесена в {target}"
            if mine:
                note += f"; открытые решения отменены: {len(mine)}"
            ctx.store.edit_card(
                card_id, None, {"project": target, "comment": {"text": note, "author": actor}}, actor=actor
            )
        people = ctx.store.delete_project(found["id"], actor=actor, target=target, cards=len(cards))
    from kd import delegation  # lazily: it reaches the cabinet through its client, which imports the API

    out = {"deleted": found["id"], "target": target, "moved": len(cards), "withdrawn": withdrawn,
           "users": people, "grants": []}
    if ctx.promote is not None:  # after the commit: the grants are files, not rows
        try:
            moment = api.people.parse_time(now).timestamp()
            out["grants"] = delegation.drop_project(ctx.promote.grants, found["id"], actor, moment)
        except (OSError, delegation.GrantError) as exc:
            # the project is gone either way (a subtask there is refused as an unknown project); say what is left
            out["grants_error"] = f"the BOSS grants were not updated ({exc.__class__.__name__})"
    return out


def _impact(ctx, principal, request):
    """What a delete would touch, for its dialog: the cards that move, the people whose roles lose the
    project, and whether a repository holds it (then the delete is refused)."""
    _manager(ctx, principal)
    _keys(request, frozenset({"op", "id"}))
    found = _existing(ctx, request.get("id"))
    return {
        "id": found["id"],
        "cards": len(ctx.store.project_cards(found["id"])),
        "users": ctx.store.project_people(found["id"]),
        "bound": found["id"] in bound_repos(ctx.repos_path) or found["repo_state"] in schema.REPO_REQUESTS,
    }


def _route_list(ctx, principal, request):
    """The routes the Projects section offers."""
    _manager(ctx, principal)
    return {"routes": _routes(ctx)}


OPS = {
    "project.impact": (MANAGERS, _impact),
    "project.routes": (MANAGERS, _route_list),
    "project.create": (MANAGERS, _create),
    "project.update": (MANAGERS, _update),
    "project.delete": (MANAGERS, _delete),
    "project.repo.bind": (MANAGERS, _bind),
    "project.repo.unbind": (MANAGERS, _unbind),
    "project.repo.queue": (frozenset({"root"}), _queue),
    "project.repo.result": (frozenset({"root"}), _result),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
