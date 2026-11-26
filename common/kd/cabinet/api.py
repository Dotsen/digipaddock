"""Typed operations of the cabinet socket and who may call them (owner cabinet spec sections 2.1, 9).

The caller is its uid: root (installer, root timers), the base user (the executors kd-task,
kd-boss-board, kd-work, kd-nudge — they have checked the agent's channel and name the agent),
the exec service user, the cabinet itself (its web), or a registered agent's own user (reads
only). The board rules are the pure models ``kd.worker_board`` and ``kd.boss_board``, evaluated
on the card as it is at the moment of writing: the service serializes calls, so a rule never
decides on a snapshot another writer has changed.

A person in the web cabinet is the principal ``user``: the HTTP layer resolved the session,
the permissions come from the person's roles over projects (``kd.cabinet.users``). A person sees
only the projects ``cards.read`` opens; a card of any other project does not exist for them.
"""

from dataclasses import dataclass, replace as _replace
from pathlib import Path
import re
from typing import Mapping, Optional

from kd import egress as egress_config
from kd import agents, boss_board, ids, naming, pipeline_config, pr_signature, session_config, specializations, worker_board
from kd.cabinet import briefs, decisions, i18n, perms, questions, schema, store, users as people, voice
from kd.manifest import ManifestError

try:
    import pwd
except ImportError:  # Windows: tests only
    pwd = None

# the approve user retired with its page; observe: the host reader of kd-host-read
KINDS = ("root", "executor", "exec", "forge", "cabinet", "push", "observe", "agent", "user")
# ``push``: the Web Push sender on the socket — the one cabinet process with network holds no owner authority
# (global review 15.10.2026); ``cabinet`` on the socket is the owner's root CLI (kd-admin-task) under the cabinet user;
# ``forge``: the Forgejo executor's user, which only posts its notices on cards
ERROR_CODES = {
    "invalid": 2,
    "refused": 2,
    "conflict": 3,
    "not_found": 2,
    "unavailable": 1,
}
CARD = ids.CARD
EVENT_ID = re.compile(r"[0-9a-f]{32}")
STATE_KEY = re.compile(r"[a-z][a-z0-9-]{0,63}")
# C03: what BOSS did about an attention event, and how soon the watch looks again (minutes).
ACK_ACTION = re.compile(r"(assist|decision|wait|job):[0-9a-f]{32}|card:" + ids.CARD.pattern + r"|directive:[0-9a-f]{16}"
                        r"|edit:[1-9][0-9]{0,8}|owner")
ACK_SOURCES = ("assist", "decision", "wait", "job", "card", "owner")  # the event's source: up to a day
ACK_CHECK = (5, 30, 1440)  # minutes: minimum, maximum, maximum for a source
ACK_REASON = 300
OWNER = "@owner"
ANALYST = briefs.ANALYST  # the brief writer: «write a brief» hands the card to it in Inbox
BRIEF_SPEC = briefs.BRIEF_SPEC
BRIEF_READY = briefs.BRIEF_READY
IN_COUNCIL = "in_council"  # a promote proposed while the brief's council has not ended
BRIEF_STALE = "brief_stale"  # a start or a promote of a brief whose text moved on from its last version
ROOT_ACTOR = "root"  # kd-cabinet create|edit on the host: its own actor, never a person's
EVENTS_MAX = 500
PRIORITIES = ("", "high", "medium", "low")
TEXT_MAX = 20000
NOTE_LINE_MAX = 2000
FILE_MAX = 20 * 1024 * 1024
# one message's files together, sent in one request with its text(nginx lets a little more through)
MESSAGE_FILES_MAX = 100 * 1024 * 1024
FILES_PER_MESSAGE = 20
# files share the volume with cabinet.db: one person, and everyone together, stop well before it is full
# (global review 15.10.2026)
FILES_USER_MAX = 1024 * 1024 * 1024
FILES_TOTAL_MAX = 4 * 1024 * 1024 * 1024
FILE_ID = re.compile(r"[0-9a-f]{32}")
SHA256 = re.compile(r"[0-9a-f]{64}")
MIME = re.compile(r"[a-z0-9.+-]{1,40}/[a-zA-Z0-9.+-]{1,80}")
OVERRIDE_MAX = 500  # a manual status change's reason
# The ready reasons of a manual status change by its target status, «other» last; their labels are the
# catalog keys flow.override.code.<code>, the page keeps the same list (lib/overrideReasons.ts).
OVERRIDE_OTHER = "other"
OVERRIDE_DUPLICATE = "duplicate"
OVERRIDE_REASONS = {
    "Inbox": ("rewrite_brief", "started_by_mistake", OVERRIDE_OTHER),
    "To Do": ("restart", "requeue", OVERRIDE_OTHER),
    "In Progress": ("continue", "rework", OVERRIDE_OTHER),
    "Review": ("ready_for_review", OVERRIDE_OTHER),
    "Blocked": ("waits_card", "waits_external", OVERRIDE_OTHER),
    "Done": ("done_outside", OVERRIDE_OTHER),
    "Cancelled": ("not_relevant", OVERRIDE_DUPLICATE, OVERRIDE_OTHER),
}
# What a person may write on a card besides its status (status needs cards.transition).
USER_FIELDS = frozenset(
    {
        "title",
        "description",
        "acceptance",
        "dod",
        "notes",
        "plan",
        "labels",
        "priority",
        "due",
        "assignee",
        "dependencies",
        "servers",
        "parent",
        # the model and effort of the card's sessions: ``settings`` — a decision about spend
        "model",
        "effort",
        # the card's own council: ``settings`` too
        "council",
    }
)
# What a person may set on a card they create besides its title, project, description and assignee:
# checked as the same fields of an edit are.
CREATE_FIELDS = ("labels", "acceptance", "parent", "dependencies", "model", "effort", "council")
# A card in a list: everything but the long texts (description, acceptance, notes, summary, plan, dod).
COMPACT = (
    "id",
    "n",
    "status",
    "assignee",
    "project",
    "title",
    "labels",
    "servers",
    "priority",
    "due",
    "parent",
    "dependencies",
    "revision",
    "created",
    "updated",
)


class ApiError(Exception):
    """``kind`` — the stable error class; ``reason`` — a machine code of a refused card action (``flow.reason.*``)."""

    def __init__(self, kind, message, reason=""):
        super().__init__(message)
        self.kind = kind
        self.reason = reason


@dataclass(frozen=True)
class Principal:
    kind: str
    uid: int
    user: str
    agent: str = ""


def principal_of(
    uid, *, getpwuid=None, registry=agents.REGISTRY, base=None, services=None
) -> Optional[Principal]:
    """The caller behind a peer uid; None for anyone the cabinet does not know."""
    getpwuid = getpwuid or pwd.getpwuid
    try:
        user = getpwuid(uid).pw_name
    except KeyError:
        return None
    base = base or naming.base_user()
    services = services or {
        role: naming.service_user(role) for role in ("exec", "forge", "cabinet", "push", "observe")
    }
    if uid == 0:
        return Principal("root", uid, user)
    if user == base:
        return Principal("executor", uid, user)
    for role, name in services.items():
        if user == name:
            return Principal(role, uid, user)
    try:
        agent = agents.agent_of_user(user, registry)
    except ValueError:
        return None
    return Principal("agent", uid, user, agent) if agent else None


class Context:
    def __init__(
        self,
        store_,
        registry_path,
        repo_root,
        *,
        require_root_owner=True,
        now=None,
        users=None,
        admin=None,
        promote=None,
        notifier=None,
    ):
        self.store, self.registry_path = store_, registry_path
        self.repo_root, self.require_root_owner, self.now = (
            repo_root,
            require_root_owner,
            now or store.utcnow,
        )
        self.users = users
        self.admin = admin  # admin_approval.Service: the effects of admin decisions
        self.promote = promote  # boss_promote.Settings: the effects of BOSS proposals
        self.notifier = notifier  # notify.Notifier: the push queue's scheduler
        # the session host's agents: () -> session_config.HostConfig; nobody until install_host
        self.host_config = session_config.HostConfig
        # the pipeline's mode and limits: () -> pipeline_config.Config; observe until serve wires the file
        self.pipeline = pipeline_config.Config
        # the projects bound to a repository: a bound project is never deleted; root writes the file (0644)
        self.repos_path = "/etc/digipaddock/repos.json"
        self.egress_path = egress_config.PATH  # the probe exits of the release tree (kd.egress)
        # the file a repository request touches for the root unit kd-repo-attach; None — nobody is rung
        self.repo_doorbell = None
        # where the web keeps the files' bytes: agents read and add files through the socket; None — off
        self.files_dir = None
        # the monitor's config.json, the defaults of its settings; None — the deployed tree's
        self.monitor_config = None
        # the release tree's contours; None — host/contours.json of the deployed tree
        self.contours_path = None
        # where the kinds' key files are (the cabinet reports a key's presence, never its content); None —
        # the host's kd.llm_models.ENV_DIR
        self.llm_env_dir = None
        # the host's Claude token file (a subscription model's «key» is this file in place); None —
        # kd.claude_token.PATH
        self.claude_env = None
        self._agents = self._specs = None

    def tree(self) -> Path:
        """The release tree this cabinet serves: the agents' manifests, the registries, the worker pools."""
        return Path(self.repo_root) if self.repo_root else specializations.REPO_ROOT

    def routing(self) -> boss_board.Routing:
        """The board's projects as the projects table holds them now (the only source, so a project the
        owner created or rerouted counts at once) over the registered agents. A route to an agent the registry no
        longer has keeps its project but is no target: that project's new cards are refused, every other project
        routes as before — one stale route never stops the board, nor makes a gone agent assignable."""
        if self._agents is None:
            try:
                self._agents = frozenset(agents.names(self.registry_path))
            except (OSError, ValueError) as exc:
                raise ApiError("unavailable", f"agent registry unreadable ({exc.__class__.__name__})")
        routes = self.store.projects()
        stale = {t[1:] for t in routes.values() if t.startswith("@") and t != OWNER} - self._agents
        try:
            found = boss_board.routing_from(routes, self._agents | stale)
        except boss_board.Refused as exc:
            raise ApiError("unavailable", f"the projects table is invalid ({exc})")
        return boss_board.Routing(found.projects, found.assignees - {"@" + name for name in stale})

    def targets(self) -> frozenset:
        return (frozenset(self.routing().assignees) - {OWNER}) | {
            worker_board.COORDINATOR
        }

    def specs(self):
        if self._specs is None:
            try:
                root = Path(self.repo_root)
                registry = specializations.load_registry(
                    root / "common" / "specializations",
                    require_root_owner=self.require_root_owner,
                )
                self._specs = specializations.agent_specs(registry, root)
            except (
                specializations.SpecError,
                ManifestError,
                OSError,
                TypeError,
            ) as exc:
                raise ApiError(
                    "unavailable",
                    f"specialization registry unreadable ({exc.__class__.__name__})",
                )
        return self._specs

    def stage_limit(self) -> int:
        """How many stages a brief may have: ``maxChildren`` of the delegation limits of the deployed tree
        (the same ceiling BOSS's subtasks have); the module's default without a tree or the file."""
        from kd import delegation
        from kd.cabinet import stages

        path = delegation.LIMITS if self.repo_root is None else str(
            Path(self.repo_root) / "common" / "coordination" / "delegation.json")
        try:
            return delegation.load_limits(path).max_children
        except (OSError, ValueError):
            return stages.MAX_DEFAULT

    def factory(self):
        """The factory catalog of the deployed tree, read each time: a reviewed change is a release."""
        from kd import factory_ops

        path = factory_ops.CATALOG_PATH if self.repo_root is None else str(
            Path(self.repo_root) / "common" / "coordination" / "factory.json")
        try:
            return factory_ops.load_catalog(path)
        except factory_ops.CatalogError as exc:
            raise ApiError("unavailable", str(exc))

    def contours(self) -> dict:
        """The contours of the deployed tree, read each time: a changed file is a release."""
        from kd import contours

        path = self.contours_path or (contours.path() if self.repo_root is None else str(
            Path(self.repo_root) / "host" / "contours.json"))
        try:
            return contours.load(path)
        except contours.ContourError as exc:
            raise ApiError("unavailable", str(exc))

    def agent_names(self) -> frozenset:
        """The generated agents of this host: the registry's names."""
        self.routing()
        return self._agents

    def refresh(self):
        self._agents = self._specs = None


# --- helpers ----------------------------------------------------------------------------------
def _card(ctx, card_id) -> store.CardRecord:
    if not isinstance(card_id, str) or not CARD.fullmatch(card_id):
        raise ApiError("invalid", "invalid card id")
    try:
        return ctx.store.card(card_id)
    except store.NotFound as exc:
        raise ApiError("not_found", str(exc))


def _no_release_steps_with_stages(ctx, card_id, body) -> None:
    """The PR of a staged brief names a stage's card, so a component step on revision
    ``release`` written on the parent would never be requested — it belongs to the stage's own factory plan."""
    from kd import factory_ops

    record = _card(ctx, card_id)
    stages_after = body["stages"] if "stages" in body else list(record.stages)
    steps = body["operations"] if "operations" in body else list(record.operations)
    on_release = [s for s in steps or () if isinstance(s, Mapping) and s.get("verb") == factory_ops.COMPONENT
                  and s.get("revision") == factory_ops.RELEASE_REVISION]
    if stages_after and on_release:
        raise ApiError("refused", f"{record.id}: a brief with stages keeps no component step on revision "
                                  f"'release' — the release of a stage's PR names the stage's card: put the step in "
                                  f"that stage's own plan (kd-factory plan KD-<stage>)")


def _snapshot(ctx, card_id) -> boss_board.Card:
    try:
        return boss_board.card_from(_card(ctx, card_id).to_dict())
    except boss_board.Refused as exc:
        raise ApiError("invalid", str(exc))


def _executor_named(ctx, principal, request) -> tuple:
    """(agent, its user) an executor speaks for: a registered agent's name."""
    if principal.kind != "executor":
        raise ApiError("refused", "only the board executors may do this")
    agent = request.get("agent")
    if not isinstance(agent, str) or not agents.NAME_RE.fullmatch(agent):
        raise ApiError("refused", "no agent named")
    try:
        if agent not in agents.names(ctx.registry_path):
            raise ApiError("refused", f"{agent} is not a registered agent")
        return agent, agents.user_of(agent, ctx.registry_path)
    except ValueError as exc:
        raise ApiError("refused", str(exc))


def _executor_agent(ctx, principal, request, *, coordinator: bool) -> str:
    """The agent an executor speaks for: registered, with its own user; the coordinator only where asked."""
    agent, user = _executor_named(ctx, principal, request)
    if (
        user == agents.DEFAULT_USER
        or (agent == worker_board.COORDINATOR[1:]) != coordinator
    ):
        raise ApiError("refused", f"{agent} may not do this")
    return agent


def executor_capability(ctx, principal, request, capability: str) -> str:
    """The agent an executor reads for, when that agent's own manifest in the deployed tree names ``capability``
    . The executor checked its caller by the channel; the cabinet checks the right itself, so an executor
    naming another agent gets only what that agent holds."""
    agent, user = _executor_named(ctx, principal, request)
    if user == agents.DEFAULT_USER:
        raise ApiError("refused", f"{agent} may not do this")
    from kd.manifest import load_manifest

    root = Path(ctx.repo_root) if ctx.repo_root else specializations.REPO_ROOT
    try:
        manifest = load_manifest(root / "agents" / agent / "agent.json")
    except (ManifestError, OSError) as exc:
        raise ApiError("refused", f"{agent}: manifest unreadable ({exc.__class__.__name__})")
    if capability not in manifest.capabilities:
        raise ApiError("refused", f"{agent} has no {capability}")
    return agent


def _without_labels(plan, labels) -> "boss_board.Edit":
    changes = dict(plan.changes)
    if "labels" in changes:
        changes["labels"] = [v for v in changes["labels"] if v not in labels]
    else:
        changes["labels_add"] = [v for v in changes.get("labels_add", ()) if v not in labels]
        changes["labels_remove"] = list(changes.get("labels_remove", ())) + list(labels)
        if not changes["labels_add"]:
            del changes["labels_add"]
    return _replace(plan, changes=changes)


def _check_country(ctx, labels, before) -> None:
    """a card's ``egress:<code>`` label — one per card, of a country of the host's table. Only a label this
    write adds is checked: a card whose country left the file keeps its label through other edits."""
    marked = [v for v in labels if v.startswith(egress_config.LABEL)]
    if not [v for v in marked if v not in before]:
        return
    if len(marked) > 1:
        raise ApiError("refused", f"one {egress_config.LABEL} label per card, not {', '.join(marked)}:"
                                  " remove the old one in the same edit")
    try:
        known = egress_config.country_labels(egress_config.load(ctx.egress_path))
    except egress_config.EgressError:
        known = []
    if marked[0] not in known:
        raise ApiError("refused", f"unknown egress country {marked[0]}: known — {', '.join(known) or 'none'}")


def _apply(ctx, plan, actor, extra=None, *, promoting=False) -> store.CardRecord:
    try:
        # Reject an incompatible skill selection atomically with any reassignment/spec edit.
        # An old card without skills remains usable on minimal/test installations.
        inherited, existing = (), ()
        if isinstance(plan, boss_board.Create):
            selection = dict(plan.values)
        else:
            before = ctx.store.card(plan.task_id)
            selection = {'assignee': before.assignee, 'labels': list(before.labels)}
            selection.update(plan.changes)
            labels = list(selection['labels'])
            labels = [v for v in labels if v not in selection.get('labels_remove', ())]
            labels += [v for v in selection.get('labels_add', ()) if v not in labels]
            selection['labels'] = labels
            existing = [v for v in before.labels if v.startswith(specializations.SKILL_PREFIX)]
            if selection['assignee'] != before.assignee:
                inherited = [v for v in labels if v in before.labels and v.startswith(specializations.SKILL_PREFIX)]
        _check_country(ctx, selection.get('labels', ()),
                       () if isinstance(plan, boss_board.Create) else before.labels)
        if any(v.startswith(specializations.SKILL_PREFIX) for v in selection.get('labels', ())):
            from kd.persistent_skills import selection as checked_selection, spec_for
            assignee = selection.get('assignee', '')
            agent = assignee.startswith('@') and assignee != OWNER
            # a card off the agents keeps its skills for the next start: only one added now needs an agent
            new = [v for v in selection['labels'] if v.startswith(specializations.SKILL_PREFIX) and v not in existing]
            if not agent and new:
                raise specializations.SpecError('skills need a registered agent assignee')
            if agent:
                if inherited:
                    # a reassignment drops the skills its receiver cannot take, in the same write (as
                    # the foreign spec: label is); a skill named in this very edit is still checked strictly
                    spec = spec_for(ctx.repo_root, assignee[1:], selection['labels'],
                                    require_root_owner=ctx.require_root_owner)
                    dropped = [v for v in inherited
                               if spec is None or v[len(specializations.SKILL_PREFIX):] not in spec.skills]
                    if dropped:
                        plan = _without_labels(plan, dropped)
                        selection['labels'] = [v for v in selection['labels'] if v not in dropped]
                checked_selection(ctx.repo_root, assignee[1:], selection['labels'],
                                  require_root_owner=ctx.require_root_owner)
        if isinstance(plan, boss_board.Create):
            return ctx.store.create_card(plan.values, actor=actor)
        return ctx.store.edit_card(
            plan.task_id,
            plan.revision,
            plan.changes,
            actor=actor,
            guards=plan.guards,
            event_extra=extra,
            promoting=promoting,
        )
    except (specializations.SpecError, ManifestError) as exc:
        raise ApiError("refused", str(exc)) from exc
    except store.Conflict as exc:
        raise ApiError("conflict", str(exc))
    except (store.Invalid, store.NotFound) as exc:
        raise ApiError("invalid", str(exc))


def _written(record: store.CardRecord) -> dict:
    return {"card": record.to_dict(), "changed": True}


def compact(record: store.CardRecord) -> dict:
    data = record.to_dict()
    return {key: data[key] for key in COMPACT}


# --- people: visibility and permissions -------------------------------------------------------
def card_actor(principal: Principal) -> str:
    if principal.kind == "user":
        return f"user:{principal.user}"
    if principal.kind == "agent":
        return f"agent:{principal.agent}"
    return principal.user


def visible(ctx, principal: Principal, project: str) -> bool:
    """Everyone but a person sees every project; a person — those ``cards.read`` opens."""
    return principal.kind != "user" or ctx.users.can(
        principal.user, "cards.read", project
    )


def allowed(ctx, principal: Principal, perm: str, project=None) -> bool:
    return principal.kind != "user" or ctx.users.can(principal.user, perm, project)


def _require(ctx, principal, perm, project=None):
    if not allowed(ctx, principal, perm, project):
        raise ApiError("refused", f"no permission {perm}")


def reads_agent_thread(ctx, principal: Principal, space: str = "chat") -> bool:
    """An agent's own thread with people in ``space`` (its chat or «Monitor», outside cards) is open to the caller:
    a person needs ``perms.AGENT_THREAD_PERMS`` over every project; an agent or a service passes (an agent's own
    thread only — its callers check that)."""
    return all(allowed(ctx, principal, perm) for perm in perms.AGENT_THREAD_PERMS[space])


def _require_agent_thread(ctx, principal: Principal, space: str = "chat"):
    for perm in perms.AGENT_THREAD_PERMS[space]:
        _require(ctx, principal, perm)


def agent_spaces(ctx, principal: Principal) -> tuple:
    """The spaces of agents' own threads the caller may read, in ``schema.AGENT_SPACES`` order."""
    return tuple(space for space in schema.AGENT_SPACES if reads_agent_thread(ctx, principal, space))


def _visible_card(ctx, principal, card_id) -> store.CardRecord:
    record = _card(ctx, card_id)
    if not visible(ctx, principal, record.project):
        raise ApiError("not_found", f"{card_id}: card not found")
    return record


def _text(value, name, *, limit=TEXT_MAX, empty=False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ApiError("invalid", f"{name} must be text of 1 to {limit} characters")
    return value


def _ids(value, name) -> list:
    if not isinstance(value, list) or not all(
        isinstance(v, str) and CARD.fullmatch(v) for v in value
    ):
        raise ApiError("invalid", f"{name} must be a list of card ids")
    return sorted(set(value))


def _user_changes(ctx, changes, record=None, project="") -> dict:
    """The changes a person sent, checked and in the store's dictionary; ``record`` — the card they change (its
    council's critics stay its critics), or ``project`` — the one of the card they create."""
    if not isinstance(changes, Mapping) or not changes:
        raise ApiError("invalid", "changes must be a non-empty object")
    extra = set(changes) - USER_FIELDS - {"status"}
    if extra:
        raise ApiError("invalid", f"unknown field: {', '.join(sorted(extra))}")
    out = {}
    for key in ("description", "dod", "notes", "plan"):
        if key in changes:
            out[key] = _text(changes[key], key, empty=True)
    if "title" in changes:
        out["title"] = _text(changes["title"], "title", limit=store.TITLE_MAX)
    if "status" in changes:
        if changes["status"] not in schema.STATUSES:
            raise ApiError("invalid", "unknown status")
        out["status"] = changes["status"]
    if "acceptance" in changes:
        items = changes["acceptance"]
        if not isinstance(items, list) or not all(
            isinstance(a, Mapping)
            and isinstance(a.get("text"), str)
            and a["text"].strip()
            and isinstance(a.get("checked", False), bool)
            for a in items
        ):
            raise ApiError("invalid", "acceptance is a list of {text, checked}")
        out["acceptance"] = [
            {"text": a["text"].strip(), "checked": a.get("checked", False)} for a in items
        ]
    if "labels" in changes:
        labels = changes["labels"]
        if not isinstance(labels, list) or not all(
            isinstance(v, str) and v.strip() and not v.startswith(store.SERVER_LABEL)
            for v in labels
        ):
            raise ApiError("invalid", "labels are a list of text (servers have their own field)")
        normalized = [v.strip() for v in labels]
        skills = [v for v in normalized if v.startswith(specializations.SKILL_PREFIX)]
        if len(skills) != len(set(skills)):
            raise ApiError("invalid", "duplicate skill")
        out["labels"] = list(dict.fromkeys(normalized))
    if "servers" in changes:
        servers = changes["servers"]
        if not isinstance(servers, list) or not all(
            isinstance(v, str) and re.fullmatch(r"[a-z0-9][a-z0-9_.-]{0,63}", v)
            for v in servers
        ):
            raise ApiError("invalid", "servers are a list of server aliases")
        out["servers"] = sorted(set(servers))
    if "dependencies" in changes:
        out["dependencies"] = _ids(changes["dependencies"], "dependencies")
    if "parent" in changes:
        parent = changes["parent"]
        if parent != "" and not (isinstance(parent, str) and CARD.fullmatch(parent)):
            raise ApiError("invalid", "parent is a card id or empty")
        out["parent"] = parent
    if "priority" in changes:
        if changes["priority"] not in PRIORITIES:
            raise ApiError("invalid", "priority is high, medium, low or empty")
        out["priority"] = changes["priority"]
    if "due" in changes:
        due = changes["due"]
        if due != "" and not (
            isinstance(due, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due)
        ):
            raise ApiError("invalid", "due is YYYY-MM-DD or empty")
        out["due"] = due
    if "assignee" in changes:
        out["assignee"] = _assignee(ctx, changes["assignee"])
    if "model" in changes:
        out["model"] = llm_api.check_model(ctx.store, changes["model"])
    if "effort" in changes:
        out["effort"] = llm_api.check_effort(changes["effort"])
    if "council" in changes:
        out["council"] = councils.check(
            ctx.store, changes["council"], stored=record.council if record else None, ctx=ctx,
            project=record.project if record else project)
    return out


def _assignee(ctx, value) -> str:
    allowed_ = ctx.routing().assignees | {worker_board.COORDINATOR, ""}
    if value not in allowed_:
        raise ApiError("invalid", f"unknown assignee {value!r}")
    return value


def _user_card_create(ctx, principal, request):
    project = request.get("project")
    if not isinstance(project, str) or project not in ctx.routing().projects:
        raise ApiError("invalid", "unknown project")
    _require(ctx, principal, "cards.create", project)
    title = _text(request.get("title"), "title", limit=store.TITLE_MAX)
    assignee = _assignee(ctx, request.get("assignee", ctx.routing().projects[project]))
    if assignee not in _card_agents(ctx, project) | {""} and not _steers(ctx, card_actor(principal), project):
        raise ApiError("refused", f"{assignee} is not {project}'s agent: assigning another is agents.control")
    values = {
        "title": title.strip(),
        "project": project,
        "status": "Inbox",
        "assignee": assignee,
        "description": _text(request.get("description", ""), "description", empty=True),
        "created_by": f"@{principal.user}",
    }
    # the new card's form sets what an edit could, by an edit's rules and in the card's one write
    given = {key: request[key] for key in CREATE_FIELDS if key in request}
    if given:
        if {"model", "effort", "council"} & set(given):
            # decisions about spend, as on an edit — asked before the values are looked at
            _require(ctx, principal, "settings", project)
        more = _user_changes(ctx, given, project=project)
        _guard_links(ctx, principal, more)
        values.update(more)
    return _written(_apply(ctx, boss_board.Create(values), card_actor(principal)))


def _user_card_edit(ctx, principal, request):
    record = _visible_card(ctx, principal, request.get("card"))
    revision = request.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ApiError("invalid", "revision must be the card's revision")
    changes = _user_changes(ctx, request.get("changes"), record)
    actor, extra, outcome = card_actor(principal), None, {}
    # what the card's sessions run on and who criticises it are decisions about spend
    if {"model", "effort", "council"} & set(changes):
        _require(ctx, principal, "settings", record.project)
    if changes.get("status") == record.status:
        del changes["status"]  # the page sends the card back as it is: no move
    if "status" in changes:
        _require(ctx, principal, "cards.transition", record.project)
        # a move is one of the card's actions (flow); a plain status edit is a manual override with a reason
        code, why, canonical = _override_reason(ctx, principal, record, changes["status"], request)
        changes["comment"] = {
            "text": f"[{principal.user}] вручную: {record.status} → {changes['status']}: {why}",
            "author": actor,
        }
        extra = {"override": True, "reason_code": code}
        if changes["status"] == "Cancelled":
            # The person's explicit override cancels work; a duplicate names its live original, the
            # coordinator's closure with the wishes carried over stays a separate typed operation.
            outcome = {"outcome": "duplicate", "canonical": canonical} if canonical else {"outcome": "cancelled"}
    if set(changes) - {"status", "comment"}:
        _require(ctx, principal, "cards.edit", record.project)
        if record.created_by != f"@{principal.user}" and not allowed(
            ctx, principal, "cards.transition", record.project
        ):
            raise ApiError("refused", "only the card's author edits it")
    _guard_reach(ctx, principal, record, changes)
    if changes.get("assignee", record.assignee) != record.assignee and changes["assignee"] in ctx.specs():
        # a new agent never keeps a specialization it does not hold (as kd-task handoff, spec 3.2); a card taken
        # off the agents (no assignee, the owner) keeps the one it will be started with
        labels = changes.get("labels", list(record.labels))
        foreign = flow.foreign_specs(ctx, changes["assignee"], labels)
        if foreign:
            changes["labels"] = [v for v in labels if v not in foreign]
    if not changes:  # the page replaces the card with the answer: the buttons come with it
        return {**_written(record), **for_people(ctx, principal, record)}
    changes.update(outcome)  # the move's own outcome: no cards.edit of its own
    record = _apply(ctx, boss_board.Edit(record.id, revision, changes), actor, extra)
    return {**_written(record), **for_people(ctx, principal, record)}


def _override_reason(ctx, principal, record, status, request) -> tuple:
    """A manual status change's ``(code, the thread's reason, canonical)``: a ready reason of the target
    status with an optional explanation, or «other» with the person's own text; a page from before the codes sends
    only the text, taken as «other». A duplicate may name its original: a live card the person sees, not this one."""
    code = request.get("override_reason_code")
    text = request.get("override_reason")
    canonical = request.get("override_canonical")
    if code is None:
        if text is None:
            raise ApiError(
                "refused",
                f"{record.id}: move it with the card's actions; a manual status change needs a reason",
                reason=flow.USE_ACTION,
            )
        code = OVERRIDE_OTHER
    if code not in OVERRIDE_REASONS[status]:
        raise ApiError("invalid", f"override_reason_code {code!r} is not a reason for {status}")
    text = _text("" if text is None else text, "override_reason", limit=OVERRIDE_MAX,
                 empty=code != OVERRIDE_OTHER).strip()
    if canonical not in (None, "") and code != OVERRIDE_DUPLICATE:
        raise ApiError("invalid", "override_canonical goes only with the reason duplicate")
    original = None
    if canonical not in (None, ""):
        if not isinstance(canonical, str):
            raise ApiError("invalid", "override_canonical must be a card id")
        card_id = ids.canonical(canonical.strip().upper())
        if not CARD.fullmatch(card_id):
            raise ApiError("invalid", f"{canonical.strip()} is not a card id")
        if card_id == record.id:
            raise ApiError("invalid", f"{card_id} is this card: a duplicate names another one")
        original = _visible_card(ctx, principal, card_id)
        if original.status == "Cancelled":
            raise ApiError("refused", f"{card_id} is cancelled itself: name the live card")
    if code == OVERRIDE_OTHER:
        return code, text, ""
    why = i18n.t("ru", f"flow.override.code.{code}") + (f" ({original.id})" if original else "")
    return code, why + (f" — {text}" if text else ""), original.id if original else ""


def _revision(request) -> int:
    revision = request.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ApiError("invalid", "revision must be the card's revision")
    return revision


def _root_card_create(ctx, principal, request):
    """``kd-cabinet create`` as root: a card in Inbox for the project's route (or ``assignee``)."""
    project = request.get("project")
    if not isinstance(project, str) or project not in ctx.routing().projects:
        raise ApiError("invalid", "unknown project")
    values = {
        "title": _text(request.get("title"), "title", limit=store.TITLE_MAX).strip(),
        "project": project,
        "status": "Inbox",
        "assignee": _assignee(ctx, request.get("assignee", ctx.routing().projects[project])),
        "description": _text(request.get("description", ""), "description", empty=True),
        "created_by": ROOT_ACTOR,
    }
    return _written(_apply(ctx, boss_board.Create(values), ROOT_ACTOR))


def _root_card_edit(ctx, principal, request):
    """``kd-cabinet edit`` as root: a person's field changes and an optional comment, signed ``root``."""
    record = _card(ctx, request.get("card"))
    revision = _revision(request)
    changes = _user_changes(ctx, request["changes"], record) if request.get("changes") else {}
    canonical = request.get("canonical")
    if changes.get("status") == "Cancelled":
        # a close without the work says why: ``canonical`` makes it a duplicate of a card the board
        # has, anything else is a plain cancel — as a person's manual move in the web
        if canonical is not None:
            if canonical == record.id:
                raise ApiError("invalid", "a card is no duplicate of itself")
            changes.update(outcome="duplicate", canonical=_card(ctx, canonical).id)
        else:
            changes["outcome"] = "cancelled"
    elif canonical is not None:
        raise ApiError("invalid", "a canonical card is written only with the move into Cancelled")
    if request.get("comment") is not None:
        changes["comment"] = {"text": _text(request["comment"], "comment"), "author": ROOT_ACTOR}
    if request.get("notes_append") is not None:
        # the session host's provenance line: appended, so the notes' own size never refuses it
        changes["notes_append"] = _text(request["notes_append"], "notes_append", limit=NOTE_LINE_MAX)
    if not changes:
        raise ApiError("invalid", "nothing to change")
    return _written(_apply(ctx, boss_board.Edit(record.id, revision, changes), ROOT_ACTOR))


CLIENT_KEY = re.compile(r"[A-Za-z0-9-]{8,64}")


def _guard_reach(ctx, principal, record, changes) -> None:
    """What a person's edit points at stays in their reach (review 04.11.2026): another agent than the project's or
    the card's only with ``agents.control``, servers only with ``servers.read``, a parent or a dependency only among
    the cards they see."""
    new = changes.get("assignee", record.assignee)
    if new != record.assignee and new not in _card_agents(ctx, record.project, record.assignee) | {""} \
            and not _steers(ctx, card_actor(principal), record.project):
        raise ApiError("refused", f"{new} is not {record.project}'s agent: assigning another is agents.control")
    if "servers" in changes and set(changes["servers"]) != set(record.servers):
        _require(ctx, principal, "servers.read", record.project)
    _guard_links(ctx, principal, changes)


def _guard_links(ctx, principal, changes) -> None:
    """A parent and the dependencies a person names are among the cards they see."""
    for card_id in [changes.get("parent") or None, *changes.get("dependencies", ())]:
        if not card_id:
            continue
        try:
            linked = ctx.store.card(card_id)
        except store.NotFound:
            continue  # an id the board does not have is the store's to judge, as before
        if not visible(ctx, principal, linked.project):
            raise ApiError("not_found", f"{card_id}: card not found")


def _user_comment(ctx, principal, request):
    """A person's message in a card's thread: text, files, or both. Text wakes whom it is for
    (``_addressees``); files alone wake nobody. With audio to transcribe it wakes nobody yet: the queue
    wakes them once its audio have their results (``voice.settle``)."""
    record = _visible_card(ctx, principal, request.get("card"))
    _require(ctx, principal, "cards.comment", record.project)
    author = card_actor(principal)
    files = message_files(ctx, author, request.get("files", []))
    text = message_text(request.get("text"), files)
    key = request.get("client_key", "")
    if key != "" and not (isinstance(key, str) and CLIENT_KEY.fullmatch(key)):
        raise ApiError("invalid", "client_key must be 8 to 64 letters, digits or dashes")
    voiced = voice.audio_ids(ctx, author, files)
    with ctx.store.transaction():
        message = ctx.store.add_message(
            record.id,
            author=author,
            **message_fields(text, files, voiced),
            mentions=_addressees(ctx, record, text, author) if text and not voiced else (),
            client_key=key,
            attachments=files,
        )
        if voiced:
            voice.enqueue(ctx, message, record.thread, author)
    return {"message": voice.placed(ctx.store, [message])[0], "changed": True}


def message_text(value, files) -> str:
    """A message's text: required without files, may be empty with them (then ``""``)."""
    text = _text("" if value is None and files else value, "text", empty=bool(files))
    return text if text.strip() else ""


def message_fields(text: str, files: list, voiced=(), extra=None) -> dict:
    """``kind``, ``body`` and ``payload`` of a person's message: with text it is a ``text`` message (its files
    in ``payload.files``), without — a ``file`` message whose body names its files. A file in ``voiced`` waits
    for its transcription (``voice: {state: queued}``); ``extra`` joins the payload."""
    if not files:
        return {"kind": "text", "body": text}
    payload = {"files": [{**{k: f[k] for k in ("id", "name", "mime", "size")},
                          **({"voice": {"state": "queued"}} if f["id"] in voiced else {})} for f in files],
               **(extra or {})}
    if text:
        return {"kind": "text", "body": text, "payload": payload}
    return {"kind": "file", "body": ", ".join(f["name"] for f in files), "payload": payload}


def message_files(ctx, author: str, value) -> list:
    """The files of one message as the web stored them: each valid, together within the message's and the
    author's limits — the whole message is refused, never a part of it."""
    if not isinstance(value, list) or len(value) > FILES_PER_MESSAGE:
        raise ApiError("invalid", f"a message carries at most {FILES_PER_MESSAGE} files")
    files = [_file(item) for item in value]
    if len({f["id"] for f in files}) != len(files):
        raise ApiError("invalid", "a file is in the message twice")
    total = sum(f["size"] for f in files)
    if total > MESSAGE_FILES_MAX:
        raise ApiError("invalid", f"a message's files are at most {MESSAGE_FILES_MAX >> 20} MB together")
    if files and ctx.store.files_size(author) + total > FILES_USER_MAX:
        raise ApiError(
            "refused", f"your files already hold {FILES_USER_MAX >> 20} MB; remove some first", reason="files_quota"
        )
    if files and ctx.store.files_size() + total > FILES_TOTAL_MAX:
        raise ApiError(
            "refused", "the cabinet's files are full; the owner must remove some first", reason="files_full"
        )
    return files


def _file(item) -> dict:
    if not isinstance(item, Mapping):
        raise ApiError("invalid", "a file is an object")
    name, mime, size = item.get("name"), item.get("mime"), item.get("size")
    if not isinstance(item.get("id"), str) or not FILE_ID.fullmatch(item["id"]):
        raise ApiError("invalid", "invalid file id")
    if (
        not isinstance(name, str)
        or not 0 < len(name) <= 200
        or any(c in name for c in "/\\") or any(ord(c) < 32 for c in name)
    ):
        raise ApiError("invalid", "invalid file name")
    if not isinstance(mime, str) or not MIME.fullmatch(mime):
        raise ApiError("invalid", "invalid file type")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= FILE_MAX:
        raise ApiError("invalid", f"a file is at most {FILE_MAX >> 20} MB")
    if not isinstance(item.get("sha256"), str) or not SHA256.fullmatch(item["sha256"]):
        raise ApiError("invalid", "invalid file digest")
    return {"id": item["id"], "name": name, "mime": mime, "size": size, "sha256": item["sha256"]}


def _agents(ctx) -> frozenset:
    return frozenset((ctx.routing().assignees | {worker_board.COORDINATOR}) - {OWNER})


def _mentions(ctx, text: str) -> list:
    """The agents a person's message names (``@dev``, ``@boss``): whole words among the board's targets."""
    found = set(re.findall(r"(?<![\w@])@([a-z][a-z0-9-]*)", text))
    return sorted(name for name in _agents(ctx) if name[1:] in found)


def _steers(ctx, author: str, project: str) -> bool:
    """Whether ``author`` may wake, assign or point at any agent on ``project``'s cards: agents and the host may, a
    person with ``agents.control`` there (the owner) may; anyone else reaches the project's own agent and the card's
    only (review 04.11.2026: a partner had the sysadmin read servers for them, which their role does not give)."""
    return not author.startswith("user:") or ctx.users.can(author[len("user:"):], "agents.control", project)


def _card_agents(ctx, project: str, assignee: str = "") -> set:
    """The project's route and the card's assignee: whom a person without ``agents.control`` reaches there."""
    return {a for a in (ctx.routing().projects.get(project, ""), assignee) if a}


def _addressees(ctx, record, text: str, author: str = "") -> list:
    """Whom a person's thread message is for: the agents it names, else the card's agent while the card is
    live — a plain message on the agent's card is for it; nobody on a Done or own card. A person who
    does not steer the project's agents (``_steers``) names only the project's and the card's."""
    named = _mentions(ctx, text)
    if named and author and not _steers(ctx, author, record.project):
        named = [n for n in named if n in _card_agents(ctx, record.project, record.assignee)]
    if named:
        return named
    if not record.terminal and record.assignee in _agents(ctx):
        return [record.assignee]
    return []


def _readable_file(ctx, principal, file_id) -> dict:
    """An attachment of a thread the caller may read: a card's (its project in sight) or an agent's chat or «Monitor»
    (people who may read that thread, ``reads_agent_thread``; an agent its own); anything else is «no such file»."""
    if not isinstance(file_id, str) or not FILE_ID.fullmatch(file_id):
        raise ApiError("not_found", "no such file")
    try:
        row = ctx.store.attachment(file_id)
    except store.NotFound:
        raise ApiError("not_found", "no such file")
    if row["thread_kind"] in schema.AGENT_THREAD_KINDS:  # its chat or "Monitor"
        mine = principal.kind != "agent" or principal.agent == row["agent"]
        space = store.SPACE_OF[row["thread_kind"]]
        if not (mine and reads_agent_thread(ctx, principal, space)):
            raise ApiError("not_found", "no such file")
    else:
        _visible_card(ctx, principal, row["card"])
    return row


def _user_file_delete(ctx, principal, request):
    """A person deletes a file of a message: its author or the owner. The row goes (the quota is
    free again), the message keeps the file's name marked deleted; the web then removes the bytes."""
    row = _readable_file(ctx, principal, request.get("id"))
    if row["author"] != card_actor(principal) and not ctx.users.can(principal.user, perms.ALL):
        raise ApiError("refused", "only the file's author or the owner deletes it")
    message = ctx.store.delete_file(row["id"], actor=card_actor(principal))
    return {"message": message, "changed": True}


# --- people: accounts and roles ---------------------------------------------------------------
def _manager(ctx, principal):
    if principal.kind == "user":
        _require(ctx, principal, "users.manage")


def _roles_arg(value):
    if not isinstance(value, list):
        raise ApiError("invalid", "roles are a list of [role, projects]")
    return [tuple(item) if isinstance(item, list) else item for item in value]


# A person who manages people gives no more than they hold (review 04.11.2026: users.manage alone assigned itself
# the owner's role, through user.update past role.set's guard): their own roles are another manager's to change, a
# holder of every permission (the owner) is changed, blocked or given a sign-in code only by one, and a role
# assigned or rewritten carries only permissions the person holds over the projects it covers. Root and the
# cabinet's own CLI are not people: they keep the host's word.
def _holds_all(ctx, user_id: str) -> bool:
    return perms.ALL in ctx.users.permissions(user_id).get("*", set())


def _guard_target(ctx, principal, user_id: str) -> None:
    if principal.kind == "user" and _holds_all(ctx, user_id) and not _holds_all(ctx, principal.user):
        raise ApiError("refused", "only a holder of every permission changes one")


def _guard_grant(ctx, principal, role_perms, projects) -> None:
    """``role_perms`` over ``projects`` ('*' or a list) are all the person's own."""
    if principal.kind != "user":
        return
    mine = ctx.users.permissions(principal.user)
    for project in ["*"] if projects == "*" else list(projects):
        held = set(mine.get("*", set())) | (set() if project == "*" else set(mine.get(project, set())))
        missing = sorted(p for p in role_perms if not perms.allows(held, p))
        if missing:
            raise ApiError("refused", f"one gives only what one holds: {', '.join(missing)}"
                                      + ("" if project == "*" else f" on {project}"))


def _guard_roles(ctx, principal, roles) -> None:
    known = ctx.users.roles()
    for item in roles:
        try:
            role, projects = item
        except (TypeError, ValueError):
            raise ApiError("invalid", "a role is (role, projects)") from None
        if role in known and (projects == "*" or isinstance(projects, (list, tuple))):
            _guard_grant(ctx, principal, known[role], projects)


def _user_create(ctx, principal, request):
    _manager(ctx, principal)
    user_id = request.get("id")
    try:  # a person and an agent never share a board name (@dev is the agent)
        taken = user_id == "boss" or user_id in agents.names(ctx.registry_path)
    except ValueError as exc:
        raise ApiError("unavailable", str(exc))
    if taken:
        raise ApiError("invalid", f"{user_id} is an agent's name")
    roles = _roles_arg(request.get("roles"))
    _guard_roles(ctx, principal, roles)
    try:
        user = ctx.users.create(
            user_id,
            request.get("name"),
            roles=roles,
            language=request.get("language"),
            timezone=request.get("timezone"),
            actor=card_actor(principal),
        )
        now = ctx.now()
        code = ctx.users.issue_code(user_id, now, actor=card_actor(principal))
    except ValueError as exc:
        raise ApiError("invalid", str(exc))
    return {"user": user, "code": code, "expires": people.shift(now, seconds=people.CODE_TTL)}


def _user_id(ctx, request) -> str:
    user_id = request.get("id")
    if not isinstance(user_id, str) or ctx.users.get(user_id) is None:
        raise ApiError("not_found", "no such user")
    return user_id


def _user_signing(ctx, principal, request):
    """root turns one of a person's passkeys into a signing key for the targets' exec gate, or back;
    ``credential`` is a prefix of the key's base64url id (exactly one key must match)."""
    user_id = _user_id(ctx, request)
    prefix, enabled = request.get("credential"), request.get("enabled")
    if not isinstance(prefix, str) or not isinstance(enabled, bool):
        raise ApiError("invalid", "credential is a prefix of the key's id, enabled is true or false")
    try:
        return {"credential": ctx.users.set_signing(user_id, prefix, enabled)}
    except ValueError as exc:
        raise ApiError("not_found", str(exc))


def _user_code(ctx, principal, request):
    """A sign-in code for ``id``: root and who manages people give one to anybody, a person takes their own
    (the web asks a step-up first). ``expires`` is when it stops working."""
    if not (principal.kind == "user" and request.get("id") == principal.user):
        _manager(ctx, principal)
        _guard_target(ctx, principal, _user_id(ctx, request))
    user_id = _user_id(ctx, request)
    now = ctx.now()
    code = ctx.users.issue_code(user_id, now, actor=card_actor(principal))
    return {"user": user_id, "code": code, "expires": people.shift(now, seconds=people.CODE_TTL)}


def _user_update(ctx, principal, request):
    user_id = _user_id(ctx, request)
    profile = {k: request.get(k) for k in ("name", "language", "timezone")}
    admin = {k for k in ("status", "roles") if k in request}
    if principal.kind == "user" and (admin or user_id != principal.user):
        _require(ctx, principal, "users.manage")
    if principal.kind == "user" and user_id == principal.user and request.get("status") == "blocked":
        raise ApiError("refused", "one does not block oneself")
    if admin:
        _guard_target(ctx, principal, user_id)
    if "roles" in request and principal.kind == "user":
        if user_id == principal.user:
            raise ApiError("refused", "one's own roles are another manager's to change")
        _guard_roles(ctx, principal, _roles_arg(request["roles"]))
    actor = card_actor(principal)
    try:
        ctx.users.update(user_id, **profile, actor=actor)
        if "roles" in request:
            ctx.users.assign(user_id, _roles_arg(request["roles"]), actor=actor)
        if "status" in request:
            ctx.users.set_status(user_id, request["status"], actor=actor)
    except ValueError as exc:
        raise ApiError("invalid", str(exc))
    return {"user": ctx.users.get(user_id)}


def _user_block(ctx, principal, request):
    return _user_update(ctx, principal, {"id": request.get("id"), "status": "blocked"})


def _user_list(ctx, principal, request):
    _manager(ctx, principal)
    return {
        "users": [
            {**u, "roles": [[r, p] for r, p in ctx.users.assignments(u["id"])]}
            for u in ctx.users.list()
        ],
        "roles": ctx.users.roles(),
    }


def _role_set(ctx, principal, request):
    role_id = request.get("id")
    if principal.kind == "user":
        # people are users.manage; a role the caller holds, or the owner's, is never rewritten from the web
        # (global review 15.10.2026: settings alone could widen its own role to everything)
        _require(ctx, principal, "users.manage")
        held = {role for role, _ in ctx.users.assignments(principal.user)}
        if role_id == "owner" or role_id in held:
            raise ApiError("refused", "a role you hold, or the owner's, is not yours to change")
    role_perms = request.get("perms")
    if not isinstance(role_perms, list):
        raise ApiError("invalid", "perms must be a list")
    if principal.kind == "user":  # a role may be held over every project: what it carries is the person's own there
        _guard_grant(ctx, principal, [p for p in role_perms if isinstance(p, str)], "*")
    try:
        ctx.users.set_role(request.get("id"), role_perms, actor=card_actor(principal))
    except ValueError as exc:
        raise ApiError("invalid", str(exc))
    return {"roles": ctx.users.roles()}


def _me(ctx, principal, request):
    table = ctx.users.permissions(principal.user)
    return {
        "user": ctx.users.get(principal.user),
        "permissions": {k: sorted(v) for k, v in sorted(table.items())},
        "projects": sorted(
            ctx.users.visible_projects(principal.user, ctx.routing().projects)
        ),
        "second_factor": ctx.users.has_second_factor(principal.user),
    }


# --- board writes -----------------------------------------------------------------------------
# an agent's board writes per hour and its new cards per day (the coordinator's limits, boss_exec): a hostile
# or looping agent cannot flood the owner's feed or churn the attention automaton (global review 15.10.2026)
AGENT_WRITES_PER_HOUR = 60
AGENT_CREATES_PER_DAY = 20


def _agent_rate_limit(ctx, agent: str, op: str) -> None:
    actor, now = f"agent:{agent}", ctx.now()
    if ctx.store.count_events(actor, people.shift(now, hours=-1)) >= AGENT_WRITES_PER_HOUR:
        raise ApiError("refused", f"rate limit: {AGENT_WRITES_PER_HOUR} board changes per hour")
    if op == "create" and (
        ctx.store.count_events(actor, people.shift(now, days=-1), "card.created") >= AGENT_CREATES_PER_DAY
    ):
        raise ApiError("refused", f"rate limit: {AGENT_CREATES_PER_DAY} new cards per day")


def _worker(op):
    def handler(ctx, principal, request):
        agent = _executor_agent(ctx, principal, request, coordinator=False)
        _agent_rate_limit(ctx, agent, op)
        body = {
            k: v
            for k, v in request.items()
            if k not in ("op", "agent", "card", "revision")
        }
        if "operations" in body:
            body["operations"] = _operations(ctx, body["operations"])
        if "stages" in body and op != "create":
            from kd.cabinet import stages

            body["stages"] = stages.normalized(ctx, _snapshot(ctx, request.get("card")), body["stages"])
        if op != "create" and ("stages" in body or "operations" in body):
            _no_release_steps_with_stages(ctx, request.get("card"), body)
        labels = body.get("label") if isinstance(body.get("label"), list) else []
        involved = (
            any(str(v).startswith(worker_board.SPEC_PREFIX) for v in labels)
            or body.get("spec") is not None
        )
        try:
            if op == "create":
                projects = frozenset(ctx.routing().projects)
                plan = worker_board.plan(
                    op,
                    body,
                    agent=agent,
                    projects=projects,
                    specs=ctx.specs() if involved else None,
                )
                return _written(_apply(ctx, plan, f"agent:{agent}"))
            card = _snapshot(ctx, request.get("card"))
            if op == "claim" and request.get("revision") != card.revision:
                raise ApiError("conflict", f"{card.id}: the card changed; re-read it")
            if op == "claim":  # work waits for what it depends on (spec 3.2)
                waiting = flow.deps_error(ctx, ctx.store.card(card.id))
                if waiting is not None:
                    raise waiting
            if op in ("edit", "handoff"):
                _read_revision(card, request, scope=op == "edit" and any(body.get(k) for k in worker_board.SCOPE_KEYS))
            involved = involved or (
                op == "handoff"
                and any(v.startswith(worker_board.SPEC_PREFIX) for v in card.labels)
            )
            plan = worker_board.plan(
                op,
                body,
                agent=agent,
                card=card,
                targets=ctx.targets(),
                specs=ctx.specs() if involved else None,
            )
        except (worker_board.Refused, boss_board.Refused) as exc:
            raise ApiError("refused", str(exc))
        if op in ("edit", "handoff") and BRIEF_READY in plan.changes.get("labels_add", ()):
            from kd.cabinet import council_rounds  # at call time: it loads the jobs, which load this module

            # a brief is not handed in past its council's findings
            council_rounds.gate(ctx, ctx.store.card(card.id), agent, "handin", plan.changes)
        if body.get("status") in (outcomes.REVIEW, outcomes.DONE):
            # the same guards as kd-work finish --outcome review|done: the council's findings, open help, an
            # aggregate's stages, the closing policy
            outcomes.hand_in(ctx, ctx.store.card(card.id), agent, body["status"])
        activity = None
        if op == "edit":
            labels = [v for v in card.labels if v not in plan.changes.get("labels_remove", ())]
            labels += [v for v in plan.changes.get("labels_add", ()) if v not in labels]
            if ({v for v in card.labels if v.startswith(("spec:", "skill:"))}
                    != {v for v in labels if v.startswith(("spec:", "skill:"))}):
                from kd.persistent_skills import selection as checked_selection

                try:
                    spec, skills = checked_selection(ctx.repo_root, agent, labels,
                                                    require_root_owner=ctx.require_root_owner)
                except (specializations.SpecError, ManifestError) as exc:
                    raise ApiError("refused", str(exc)) from exc
                if spec is not None:
                    activity = {"type": "instructions", "state": "selected", **specializations.provenance(spec, skills)}
        with ctx.store.transaction():
            record = _apply(ctx, plan, f"agent:{agent}")
            if activity:
                ctx.store.add_thread_message(record.thread, author="system", kind="status", body="",
                    payload={"key": "sys.session.instructions_selected", "params": {"agent": "@" + agent},
                             "activity": activity}, via="card.changed")
        return _written(record)

    return handler


def _operations(ctx, value) -> list:
    """The "Operations" an agent writes into its draft: the factory catalog's typed steps, normalized; an empty
    list takes them off."""
    from kd import factory_ops

    if value == []:
        return []
    try:
        steps = factory_ops.operations_of(pin_revisions(ctx, value), catalog=ctx.factory(),
                                          agents_known=ctx.agent_names())
    except factory_ops.ProposalError as exc:
        raise ApiError("invalid", f"operations: {exc}")
    return [step.as_dict() for step in steps]


def pin_revisions(ctx, steps):
    """a ``component.install`` step is signed for the deployed tree — one without ``revision`` gets it, one
    naming another tree is refused (an older or a future tree is never what the owner approves)."""
    from kd import factory_ops
    from kd.cabinet import operations

    if not isinstance(steps, list):
        return steps
    out, current = [], None
    for step in steps:
        if isinstance(step, dict) and step.get("verb") == factory_ops.COMPONENT:
            given = step.get("revision")
            if given != factory_ops.RELEASE_REVISION:
                current = current or operations.deployed(ctx)
            if given == factory_ops.RELEASE_REVISION:
                pass  # signed for the card's own release, no tree to pin
            elif given is None:
                step = {**step, "revision": current}
            elif isinstance(given, str) and not factory_ops.names_tree(given, current):
                raise ApiError("refused", f"{step.get('component')} {step.get('mode')}: revision "
                                          f"{factory_ops.short(given)} is not the deployed tree "
                                          f"{factory_ops.short(current)}")
        out.append(step)
    return out


def _read_revision(card, request, *, scope: bool) -> None:
    """An agent's edit or hand-over is compare-and-set against the revision it read: a newer
    card is a conflict, never its text overwritten by an older draft; a scope change must name that revision."""
    if "revision" not in request:
        if scope:
            raise ApiError(
                "invalid",
                f"{card.id}: a description or AC change needs --revision <n> — the revision you read with kd-task view",
            )
        return
    revision = request["revision"]
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise ApiError("invalid", "revision must be the card's revision")
    if revision != card.revision:
        raise ApiError(
            "conflict",
            f"{card.id} changed since revision {revision} (now {card.revision}): "
            f"read it again with kd-task view {card.id} (its text: kd-task scope {card.id}), merge, retry",
        )


def _boss(op):
    def handler(ctx, principal, request):
        if principal.kind != "cabinet":  # the owner's decision posts only its comment as @boss (OPS)
            _executor_agent(ctx, principal, request, coordinator=True)
        body = {k: v for k, v in request.items() if k != "agent"}
        extra = None
        if op == "promote":
            # Only a subtask the owner allowed: kd-boss-board create-child checked the grant file; the
            # event keeps the grant id. The owner's own promote is card.promote, from a decision.
            grant = body.pop("grant", None)
            if not isinstance(grant, str) or not EVENT_ID.fullmatch(grant):
                raise ApiError("refused", "a promote needs the owner's grant")
            extra = {"grant": grant}
        body["op"], body["actor"] = op, boss_board.ACTOR
        card = deps = None
        if op not in ("create-inbox", "create-child"):
            card = _snapshot(ctx, body.get("task"))
            if op == "unblock":
                deps = {
                    d: _snapshot(ctx, d) for d in card.dependencies if CARD.fullmatch(d)
                }
        specs = ctx.specs() if boss_board.needs_specs(body, card) else None
        try:
            plan = boss_board.plan(body, ctx.routing(), card, deps, specs=specs)
        except boss_board.StaleRevision as exc:
            raise ApiError("conflict", f"{body.get('task')}: {exc}; re-read it")
        except boss_board.Refused as exc:
            raise ApiError("refused", str(exc))
        return _written(_apply(ctx, plan, "agent:boss", extra))

    return handler


def _promote(ctx, principal, request):
    """A person's ``Inbox -> To Do`` (a decision in the cabinet), checked by the ``promote`` rule.

    ``by`` (``user:<id>``, default the owner) is the person who decided: the edit's actor and the
    comment's author. ``quiet`` — the move is part of an approval that wakes the agent itself.
    ``proposed_by`` (only ``@boss``) — the approval of BOSS's proposal: the reason is BOSS's, and the comment says
    so, or it reads as the person's own words.
    ``rule`` (only the test zone's) — the start is no person's: the comment is the system's, no brief is
    approved and nothing is granted; a card that carries a brief is refused — a person starts it."""
    from kd import admin_grant
    from kd.cabinet import effects

    by = request.get("by", "user:owner")
    if not (isinstance(by, str) and by.startswith("user:") and people.USER_ID.fullmatch(by[5:])):
        raise ApiError("invalid", "by must be user:<id>")
    quiet = request.get("quiet", False)
    if not isinstance(quiet, bool):
        raise ApiError("invalid", "quiet must be true or false")
    proposer = request.get("proposed_by")
    if proposer is not None and proposer != boss_board.AUTHOR:
        raise ApiError("invalid", f"proposed_by must be {boss_board.AUTHOR}")
    rule = request.get("rule")
    if rule is not None and (rule != admin_grant.TEST_ZONE or not quiet or "by" in request or proposer is not None):
        raise ApiError("invalid", "rule: only the test-environment rule, quietly and with no person named")
    card = _snapshot(ctx, request.get("card"))
    body = {
        "op": "promote",
        "actor": boss_board.ACTOR,
        "task": card.id,
        "revision": request.get("revision"),
        "reason": request.get("reason"),
    }
    try:
        plan = boss_board.plan(body, ctx.routing(), card)
    except boss_board.StaleRevision as exc:
        raise ApiError("conflict", f"{card.id}: {exc}")
    except boss_board.Refused as exc:
        raise ApiError("refused", str(exc))
    changes = dict(plan.changes)
    reason = body["reason"].strip()
    if proposer is not None:
        reason = f"одобрено предложение {proposer}, исполнитель {card.assignees[0]}. Основание {proposer}: {reason}"
    if rule is not None:
        changes["comment"] = {"text": f"{boss_board.TO_DO}: {reason}", "author": "system"}
        with ctx.store.transaction():
            record = _card(ctx, card.id)
            if briefs.state(ctx.store, record)["state"] != "not_requested" or record.operations or record.stages:
                raise ApiError("refused", f"{card.id}: a card with a brief is started by a person, not by a rule")
            return _written(_apply(ctx, boss_board.Edit(card.id, plan.revision, changes), "system",
                                   {"wake": False}, promoting=True))
    changes["comment"] = {"text": f"[{by[5:]}] {boss_board.TO_DO}: {reason}", "author": by}
    try:
        with ctx.store.transaction():
            record = _card(ctx, card.id)
            from kd.cabinet import council_rounds  # at call time: it loads the jobs, which load this module

            council_rounds.approved_early(ctx, record, by)  # the owner's word ends the brief's council
            approved = briefs.approve(ctx.store, record, by=by)  # the start approves the brief's version (R17)
            if approved is not None and approved.get("stages"):  # a brief with stages: the aggregate
                from kd.cabinet import flow

                written = flow.approve_with_stages(ctx, record, approved, by=by, user=by[5:], revision=plan.revision,
                                                   promoting=True)  # the approval records the promote after it
            else:
                written = _apply(ctx, boss_board.Edit(card.id, plan.revision, changes), by,
                                 {"wake": False} if quiet else None,
                                 promoting=True)  # the approval records the promote after this edit: keep it open
            effects.brief_grant(ctx, written, by=by)  # the "Operations" of the approved brief
            return _written(written)
    except briefs.Stale as exc:
        raise ApiError("refused", str(exc), reason=BRIEF_STALE)
    except effects.EffectError as exc:
        raise ApiError(exc.kind, str(exc)) from exc


def _comment(ctx, principal, request):
    agent = _executor_agent(
        ctx, principal, request, coordinator=request.get("author") == boss_board.AUTHOR
    )
    author, text = request.get("author"), request.get("text")
    if author not in (boss_board.AUTHOR, f"@{agent}"):
        raise ApiError("refused", "author must be the caller")
    try:
        text = worker_board.signed(author[1:], worker_board._text(text))
    except worker_board.Refused as exc:
        raise ApiError("refused", str(exc))
    card = _card(ctx, request.get("card"))
    changes = {"comment": {"text": text, "author": author}}
    return _written(
        _apply(
            ctx,
            boss_board.Edit(card.id, card.revision, changes),
            store.actor_of(author),
        )
    )


# --- reads ------------------------------------------------------------------------------------
def for_people(ctx, principal, record) -> dict:
    """What the page shows beside a card: the person's actions (the backend decides, the page shows),
    the card's pipeline facts — its outcome, waits and problems — and its next step, the one the agents'
    snapshot names."""
    pipeline = outcomes.card_facts(ctx, record, principal)
    seen = snapshot.step_facts(ctx, record, lambda project: visible(ctx, principal, project), pipeline)
    return {"actions": flow.actions(ctx, principal, record), "pipeline": pipeline, "next": snapshot.next_step(seen)}


def _get(ctx, principal, request):
    record = _visible_card(ctx, principal, request.get("card"))
    out = {"card": record.to_dict()}
    if principal.kind == "user":
        out.update(for_people(ctx, principal, record))
    return out


def _list(ctx, principal, request):
    filters = {}
    for key in ("status", "assignee", "project", "label"):
        value = request.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ApiError("invalid", f"{key} must be text")
            filters[key] = value
    # a goal's children and a card's dependencies: the lists a snapshot only counts past its cap
    if request.get("parent") is not None:
        filters["parent"] = _visible_card(ctx, principal, request["parent"]).id
    if request.get("deps_of") is not None:
        filters["ids"] = list(_visible_card(ctx, principal, request["deps_of"]).dependencies)
    deferred, woken = defers.active_map(ctx.store), defers.woken_map(ctx.store)
    gathered = councils.open_map(ctx.store)  # named only on a card whose council has not ended
    return {
        "cards": [
            {**compact(r), "defer": defers.view(deferred.get(r.id)), "woken": woken.get(r.id, ""),
             **({"councils": [c["kind"] for c in gathered[r.id]]} if r.id in gathered else {})}
            for r in ctx.store.cards(**filters)
            if visible(ctx, principal, r.project)
        ]
    }


def _search(ctx, principal, request):
    text = request.get("text")
    if not isinstance(text, str) or not text.strip():
        raise ApiError("invalid", "search text is empty")
    try:
        found = ctx.store.search(text.strip())
    except store.Invalid as exc:
        raise ApiError("invalid", str(exc))
    deferred = defers.active_map(ctx.store)  # the board's tile says until when
    return {
        "cards": [
            {**compact(r), "defer": defers.view(deferred.get(r.id))}
            for r in found
            if visible(ctx, principal, r.project)
        ]
    }


PAGE_MAX = 500  # the most messages or events one page carries


def _page_params(request, default: int):
    """``(before, limit)`` of a paged read, or ``(None, None)`` for a whole one (neither given)."""
    if "before" not in request and "limit" not in request:
        return None, None
    before, limit = request.get("before"), request.get("limit", default)
    if before is not None and (not isinstance(before, int) or isinstance(before, bool) or before < 1):
        raise ApiError("invalid", "before must be a seq")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= PAGE_MAX:
        raise ApiError("invalid", f"limit is 1 to {PAGE_MAX}")
    return before, limit


def thread_page(store_, thread: int, request) -> dict:
    """``{messages, more}`` of a thread read past ``after`` — a page (``before``/``limit``: the newest ``limit``
    below ``before``, ``more`` when older ones are left) or the whole rest."""
    after = request.get("after", 0)
    if not isinstance(after, int) or isinstance(after, bool) or after < 0:
        raise ApiError("invalid", "after must be a message seq")
    before, limit = _page_params(request, 200)
    if limit is None:
        return {"messages": voice.placed(store_, store_.thread_messages(thread, after)), "more": False}
    found = store_.thread_messages(thread, after, before=before, limit=limit + 1)
    return {"messages": voice.placed(store_, found[-limit:]), "more": len(found) > limit}


def _messages(ctx, principal, request):
    """The thread (past ``after``, or a page of it) and the journal's last seq read with it, under the same
    lock: a page that asks the hub to replay from that watermark misses nothing written after its snapshot
    ."""
    if "agent" in request:
        return sessions_api.chat_messages(ctx, principal, request)
    card = _visible_card(ctx, principal, request.get("card"))
    found = thread_page(ctx.store, card.thread, request)
    return {**found, "watermark": ctx.store.last_seq()}


def _card_events(ctx, principal, request):
    """The card's own history (the History tab): whole and oldest first, or a page (``before``/
    ``limit``) newest first with ``next`` — the ``before`` of the next older page, None at the first event."""
    card = _visible_card(ctx, principal, request.get("card"))
    before, limit = _page_params(request, 100)
    if limit is None:
        return {"events": ctx.store.card_events(card.id)}
    found = ctx.store.card_events_page(card.id, before, limit + 1)
    page = found[:limit]
    return {"events": page, "next": page[-1]["seq"] if len(found) > limit else None}


def _file_get(ctx, principal, request):
    """A file of a thread the caller may read: its name, type, size, digest, where it is and who sent it (the
    bytes are the web's)."""
    row = _readable_file(ctx, principal, request.get("id"))
    keys = ("id", "name", "mime", "size", "sha256", "card", "agent", "seq", "author", "created")
    return {"file": {k: row[k] for k in keys}}


def _assignees(ctx, principal, request):
    """Whom a card can be given to (the card page's chooser): the routing targets and the coordinator."""
    return {"assignees": sorted(ctx.routing().assignees | {worker_board.COORDINATOR})}


def _projects(ctx, principal, request):
    """The projects the caller sees: ``projects`` (id -> route) and ``items`` (``{id, name, route, repo, …}``; the
    repository as the registry and the open request show it)."""
    items = projects_api.with_repos(
        ctx, [p for p in ctx.store.project_rows() if visible(ctx, principal, p["id"])]
    )
    return {"projects": {p["id"]: p["route"] for p in items}, "items": items}


# --- attention, state, events, import ---------------------------------------------------------
def _coordinator_only(ctx, principal, request):
    if principal.kind == "executor":
        _executor_agent(ctx, principal, request, coordinator=True)


def _ack(ctx, principal, request):
    _coordinator_only(ctx, principal, request)
    event = request.get("event")
    if not isinstance(event, str) or not EVENT_ID.fullmatch(event):
        raise ApiError("invalid", "invalid event id")
    action = request.get("action")
    if not isinstance(action, str) or not ACK_ACTION.fullmatch(action):
        raise ApiError("invalid", "action is assist|decision|wait|job:<id>, card:KD-<n>, directive:<id>, "
                                  "edit:<revision> or owner")
    kind, _, ref = action.partition(":")
    minutes, low, high, source_high = request.get("next_check"), *ACK_CHECK
    top = source_high if kind in ACK_SOURCES else high
    if type(minutes) is not int or not low <= minutes <= top:
        raise ApiError("invalid", f"next_check is {low}–{top} minutes for {kind}")
    reason = request.get("reason")
    if (not isinstance(reason, str) or not reason.strip() or len(reason) > ACK_REASON
            or any(ord(c) < 32 or ord(c) == 127 for c in reason)):
        raise ApiError("invalid", f"reason is one line of up to {ACK_REASON} characters")
    published = {e.get("id"): e for e in ctx.store.get_state("attention-open") or []}
    if event not in published:
        raise ApiError("refused", "unknown or resolved event")
    if kind in ("assist", "decision", "wait", "job", "card") and not ctx.store.ack_target_exists(kind, ref):
        raise ApiError("refused", f"{kind} {ref} does not exist")
    since = published[event].get("created") if isinstance(published[event].get("created"), str) else ""
    if kind == "edit" and not ctx.store.boss_edit_at(str(published[event].get("subject", "")), int(ref), since):
        raise ApiError("refused", f"no change by @boss on {published[event].get('subject')} to revision {ref} "
                                  "since the event")
    return {"already": not ctx.store.add_ack(event, action, minutes, " ".join(reason.split()))}


def _open_get(ctx, principal, request):
    """The open events the root watch published; one acknowledged since its last tick shows as such."""
    _coordinator_only(ctx, principal, request)
    acked = set(ctx.store.pending_acks())
    events = ctx.store.get_state("attention-open") or []
    return {
        "events": [
            {**e, "state": "acknowledged"} if e.get("id") in acked else e
            for e in events
        ]
    }


def _open_set(ctx, principal, request):
    events = request.get("events")
    if not isinstance(events, list) or not all(isinstance(e, Mapping) for e in events):
        raise ApiError("invalid", "events must be a list of objects")
    ctx.store.set_state("attention-open", events)
    return {}


def _acks_take(ctx, principal, request):
    return {"events": ctx.store.take_acks()}


def _state_key(request) -> str:
    key = request.get("key")
    if not isinstance(key, str) or not STATE_KEY.fullmatch(key):
        raise ApiError("invalid", "invalid state key")
    return key


def _state_get(ctx, principal, request):
    return {"value": ctx.store.get_state(_state_key(request))}


def _state_set(ctx, principal, request):
    if "value" not in request:
        raise ApiError("invalid", "no value")
    ctx.store.set_state(_state_key(request), request["value"])
    return {}


def _events(ctx, principal, request):
    """Events after a sequence number (``seq`` or ``after``), or newest first before ``before``; a person
    gets those of visible projects and their own account's; ``last`` is the last sequence scanned, for the
    next page."""
    backwards = "before" in request
    seq = request["before"] if backwards else request.get("after", request.get("seq", 0))
    limit = request.get("limit", EVENTS_MAX)
    for value in (seq, limit):
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ApiError("invalid", "seq and limit are counts")
    if backwards:
        events = ctx.store.events_before(seq, max(1, min(limit, EVENTS_MAX)))
    else:
        events = ctx.store.events_since(seq, max(1, min(limit, EVENTS_MAX)))
    last = events[-1]["seq"] if events else seq
    if principal.kind == "user":
        events = event_filter(ctx, principal)(events)
    return {"events": events, "last": last}


# a project's settings no partner hears the value of
SETTINGS_ONLY = frozenset({"memory_bank", "done_by", "model", "effort"})


def event_filter(ctx, principal):
    """A filter of events for one reader: a person sees card events of visible projects and account
    events about themselves (all of them with users.manage), a project's memory bank only with settings;
    everyone else sees everything."""
    if principal.kind != "user":
        return lambda events: list(events)
    manager = allowed(ctx, principal, "users.manage")
    projects = {}

    def project_of(card_id):
        if card_id not in projects:
            try:
                projects[card_id] = ctx.store.card(card_id).project
            except store.NotFound:
                projects[card_id] = None
        return projects[card_id]

    spaces = agent_spaces(ctx, principal)
    settings = allowed(ctx, principal, "settings")
    threads = {}

    def space_of(event) -> str:
        """The space of the agent's own thread an event is in — its chat or «Monitor» — by the event's thread (a
        command's or a session's event names no space)."""
        thread = event.get("thread")
        if thread is None:
            return (event.get("payload") or {}).get("space") or "chat"
        if thread not in threads:
            threads[thread] = ctx.store.agent_space(thread) or "chat"
        return threads[thread]

    def shown(event):
        """Which memory a project has and who closes its cards are the settings' — a partner
        hears the change, not the value."""
        payload = event.get("payload") or {}
        if settings or event.get("kind") != "project.changed" or not SETTINGS_ONLY & set(payload):
            return event
        rest = {k: v for k, v in payload.items() if k not in SETTINGS_ONLY}
        return {**event, "payload": rest}

    def keep(event):
        if sessions_api.chat_event(event):
            return space_of(event) in spaces  # an agent's chat or «Monitor» and its runs there
        if event.get("card"):
            project = project_of(event["card"])
            return project is not None and visible(ctx, principal, project)
        if event.get("kind") == "notice":
            return allowed(ctx, principal, "settings")  # the platform's notices
        kind = event.get("kind", "")
        if kind.startswith(("incident.", "server.", "monitor_trust.")) or kind in ("monitor.settings", "monitor.test"):
            # the monitor's incidents and checks, its settings, its trust by signature
            # , the owner's test alert
            return allowed(ctx, principal, "servers.read")
        payload = event.get("payload") or {}
        if event.get("kind", "").startswith("project."):  # a name or route the reader's screens show
            return manager or any(
                visible(ctx, principal, p) for p in (payload.get("project"), payload.get("target")) if p
            )
        return manager or payload.get("user") == principal.user or event.get(
            "actor"
        ) == f"user:{principal.user}"

    return lambda events: [shown(e) for e in events if keep(e)]


# --- decisions ---------------------------------------------------------------------------
OPERATION_ID = re.compile(r"[0-9a-f]{32}")  # a factory operation
# what a turn may wait on: an operation's id or the key of its request (a release's before it is asked)
OPERATION_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._+-]{0,95}")  # "+": release:<rev>+<deploy>
DECISION_ID = re.compile(r"[0-9a-f]{32}")
ACTION_ID = re.compile(r"[0-9a-f]{16}")
SHA256 = re.compile(r"[0-9a-f]{64}")
PLAN_KEYS = frozenset({"task_id", "proposal_sha256", "card_scope_sha256"})
ACTION_KEYS = PLAN_KEYS | {"proposal_id"}
BOSS_KEYS = frozenset({"proposal_id", "proposal_sha256"})
BATCH_MAX = 50


def _ledger(ctx) -> decisions.Decisions:
    return decisions.Decisions(ctx.store)


def _decision_call(call):
    from kd.cabinet import effects

    try:
        return call()
    except (decisions.DecisionError, effects.EffectError) as exc:
        raise ApiError(exc.kind, str(exc), reason=getattr(exc, "reason", ""))


def _hashes(payload, keys) -> dict:
    if (
        not isinstance(payload, Mapping)
        or set(payload) != keys
        or not all(isinstance(payload[k], str) for k in keys)
    ):
        raise ApiError("invalid", f"payload keys must be {sorted(keys)}")
    for key in keys & {"proposal_sha256", "card_scope_sha256"}:
        if not SHA256.fullmatch(payload[key]):
            raise ApiError("invalid", f"{key} must be sha256 hex")
    return dict(payload)


def _admin_payload(ctx, kind, card, payload) -> dict:
    """The sysadmin's plan or action exactly as stored now, for its own card (``conflict`` if it moved on)."""
    payload = _hashes(payload, PLAN_KEYS if kind == "admin_plan" else ACTION_KEYS)
    if payload["task_id"] != card.id:
        raise ApiError("invalid", "the payload names another card")
    if ctx.admin is None:
        raise ApiError("unavailable", "admin approvals are not served here")
    now = people.parse_time(ctx.now())
    if kind == "admin_plan":
        view = ctx.admin.view(card.id, now)
        shown = view.proposal
    else:
        if not ACTION_ID.fullmatch(payload["proposal_id"]):
            raise ApiError("invalid", "proposal_id must be 16 hex characters")
        view = ctx.admin.view_action(card.id, payload["proposal_id"], now)
        shown = view.action
    if shown is None or view.card is None:
        raise ApiError("refused", view.problem or "nothing to approve")
    if (payload["proposal_sha256"], payload["card_scope_sha256"]) != (
        shown.sha256,
        view.card.scope_sha256,
    ):
        raise ApiError("conflict", f"{card.id}: the proposal or the card changed; propose again")
    return payload


FACTORY_POLICY = "factory"


def factory_plan(ctx, record, data):
    """(card, proposal) of a factory plan for ``record`` now — the card as the grant store judges it, the plan
    normalized against the catalog; ApiError when either does not fit."""
    from kd import admin_task, factory_ops

    try:
        card = admin_task.from_record(record.to_dict())
        proposal = factory_ops.proposal(data, catalog=ctx.factory(), agents_known=ctx.agent_names())
    except (admin_task.CardError, factory_ops.ProposalError) as exc:
        raise ApiError("invalid", f"the factory plan: {exc}")
    if proposal.task_id != record.id:
        raise ApiError("invalid", "the plan names another card")
    return card, proposal


def _factory_payload(ctx, agent, record, payload) -> dict:
    """The developer's plan of factory operations on its own card of a factory project: the proposal
    itself travels in the decision, with the hashes the approval checks again."""
    if set(payload) != {"policy", "proposal"}:
        raise ApiError("invalid", "a factory plan payload is {policy, proposal}")
    catalog = ctx.factory()
    if agent not in catalog.requesters or record.project not in catalog.projects:
        raise ApiError("refused", f"@{agent} on {record.project} asks the factory for nothing")
    if record.assignee != f"@{agent}":
        raise ApiError("refused", f"{record.id} is not a card of @{agent}")
    data = payload["proposal"]
    if isinstance(data, dict) and "operations" in data:
        data = {**data, "operations": pin_revisions(ctx, data["operations"])}
    card, proposal = factory_plan(ctx, record, data)
    return {"policy": FACTORY_POLICY, "task_id": record.id, "proposal": proposal.as_dict(),
            "proposal_sha256": proposal.sha256, "card_scope_sha256": card.scope_sha256}


def _boss_payload(ctx, kind, card, payload) -> dict:
    from kd import boss_promote

    payload = _hashes(payload, BOSS_KEYS)
    if ctx.promote is None:
        raise ApiError("unavailable", "BOSS proposals are not served here")
    try:
        proposal = boss_promote.load(payload["proposal_id"], ctx.promote.proposals)
    except boss_promote.Missing as exc:
        raise ApiError("not_found", str(exc))
    if proposal.kind != kind or proposal.card != card.id:
        raise ApiError("invalid", "the proposal is of another kind or card")
    if proposal.sha256 != payload["proposal_sha256"]:
        raise ApiError("conflict", "the proposal changed")
    return payload


def _plan_by_rule(ctx, agent, card, payload):
    """the sysadmin's plan whose every step belongs to a test environment of the catalog is approved here,
    by rule — the answer carries ``approved`` and the decision recorded as already approved; None for any other
    plan, which waits for the owner as before. Asked again while the approval lives — the same answer."""
    from kd import admin_approval, admin_grant
    from kd.cabinet import effects

    now = ctx.now()
    try:
        zone = ctx.admin.approve_test_zone(card.id, card_sha256=payload["card_scope_sha256"],
                                           proposal_sha256=payload["proposal_sha256"], now=effects._moment(now))
    except admin_approval.Conflict as exc:
        raise ApiError("conflict", str(exc))
    except admin_approval.Refused as exc:
        raise ApiError("refused", str(exc))
    except (admin_approval.Unavailable, admin_grant.GrantError, OSError) as exc:
        raise ApiError("unavailable", str(exc))
    if zone is None:
        return None
    approved = {"grant": zone.grant.grant_id, "expires": admin_grant.fmt(zone.grant.expires_at),
                "envs": list(zone.envs)}
    ledger = _ledger(ctx)
    for stale in ledger.list(state="open", card=card.id):  # the owner's question about an older plan of the card
        if stale["type"] == "admin_plan":
            _decision_call(lambda: ledger.expire_one(stale["id"], now=now))
    for known in ledger.list(state="approved", card=card.id):
        if known["type"] == "admin_plan" and known["grant_id"] == zone.grant.grant_id:
            return {"decision": known, "approved": approved}
    recorded = _decision_call(lambda: ledger.record_rule(
        "admin_plan", card=card.id, payload=payload, requested_by=f"agent:{agent}", rule=admin_grant.TEST_ZONE,
        grant_id=zone.grant.grant_id, now=now, params={"envs": ", ".join(zone.envs), "steps": zone.steps}))
    return {"decision": recorded, "approved": approved}


def _question_limits(ctx, card, agent: str, open_now: list) -> None:
    """Every question is a push to the people: a looping or hostile agent may not flood them. So many open on one
    card, so many in a window across the agent's cards (``pipeline_config``: ``questions``)."""
    config = ctx.pipeline()
    held = sum(1 for d in open_now if d["type"] == "question")
    if held >= config.questions_open:
        raise ApiError("refused", f"rate limit: {held} open questions on {card.id}; wait for their answers")
    minutes = config.questions_window_s // 60
    since = people.shift(ctx.now(), seconds=-config.questions_window_s)
    if _ledger(ctx).asked_since(f"agent:{agent}", since) >= config.questions_per_window:
        raise ApiError("refused", f"rate limit: {config.questions_per_window} questions in {minutes} minutes")


def _decision_open(ctx, principal, request):
    """An agent asks: the sysadmin its plan or critical action, BOSS a promote or delegation, any agent a
    question on its own card (through the base executor)."""
    kind = request.get("type")
    if kind not in decisions.TYPES:
        raise ApiError("invalid", f"unknown decision type {kind!r}")
    card = _card(ctx, request.get("card"))
    payload = request.get("payload")
    if principal.kind == "agent":
        agent = principal.agent
        if kind not in ("admin_plan", "admin_critical"):
            raise ApiError("refused", f"{agent} opens only its plans and critical actions here")
        if card.assignee != f"@{agent}":
            raise ApiError("refused", f"{card.id} is not a card of @{agent}")
        payload = _admin_payload(ctx, kind, card, payload)
        if kind == "admin_plan":
            ruled = _plan_by_rule(ctx, agent, card, payload)
            if ruled is not None:
                return ruled
    else:
        agent = _executor_agent(
            ctx, principal, request, coordinator=request.get("agent") == worker_board.COORDINATOR[1:]
        )
        if kind in ("promote", "delegate") and agent == worker_board.COORDINATOR[1:]:
            reading = councils.open_for(ctx.store, card.id, "brief") if kind == "promote" else None
            if reading is not None:  # the critics still read the brief: nothing to propose yet
                raise ApiError(
                    "refused",
                    f"{card.id}: the brief's council has not ended"
                    f" (round {reading['round']} of {reading['rounds_max']})",
                    reason=IN_COUNCIL,
                )
            payload = _boss_payload(ctx, kind, card, payload)
        elif kind == "admin_plan" and isinstance(payload, Mapping) and payload.get("policy") == FACTORY_POLICY:
            payload = _factory_payload(ctx, agent, card, payload)
        elif kind == "question":
            if card.assignee != f"@{agent}":
                raise ApiError("refused", f"{card.id} is not a card of @{agent}")
            if not isinstance(payload, Mapping):
                raise ApiError("invalid", "a question payload is {text} or a form v1")
            if set(payload) in ({"text"}, {"text", "expect"}):
                expect = payload.get("expect", "text")
                if expect not in questions.EXPECTS:
                    raise ApiError("invalid", f"expect is one of {', '.join(questions.EXPECTS)}")
                payload = {"text": _text(payload["text"], "text", limit=decisions.TEXT_MAX).strip(),
                           **({"expect": "file"} if expect == "file" else {})}  # text is the default: never written
            else:  # a form: checked whole before anything is written
                try:
                    payload = questions.validate_form(payload)
                except questions.FormError as exc:
                    raise ApiError("invalid", f"the question form: {exc}")
        else:
            raise ApiError("refused", f"{agent} may not open a {kind} decision")
    ledger = _ledger(ctx)
    sha = decisions.payload_sha(payload)
    open_now = ledger.list(state="open", card=card.id)
    for open_ in open_now:
        if open_["type"] == kind and open_["payload_sha"] == sha:
            return {"decision": open_}  # asked again: the same open decision
    if kind == "question":
        _question_limits(ctx, card, agent, open_now)
    if kind in ("admin_plan", "promote", "delegate"):  # one per card and kind: a new one supersedes the open ones
        for stale in ledger.list(state="open", card=card.id):
            if stale["type"] == kind:
                _decision_call(lambda: ledger.expire_one(stale["id"], now=ctx.now()))
    opened = _decision_call(
        lambda: ledger.open(kind, card=card.id, payload=payload, requested_by=f"agent:{agent}", now=ctx.now())
    )
    return {"decision": opened}


def decision_visible(ctx, principal, decision) -> bool:
    """A card's decision — with its card's project; the monitor's trust without a card — with servers.read."""
    if decision["type"] == decisions.TRUST and not decision["card"]:
        return allowed(ctx, principal, "servers.read")
    return visible(ctx, principal, decision["project"] or "")


def deployed_basis(ctx, decision):
    """a factory plan with a component step is signed for the tree deployed at the signature — the challenge
    binds that revision too (key ``deployed:<card>``): a release between the page and the click is a conflict, the
    page reads the new tree again. None for any other decision."""
    from kd import factory_ops
    from kd.cabinet import operations

    payload = decision.get("payload") or {}
    if decision.get("type") != "admin_plan" or payload.get("policy") != FACTORY_POLICY:
        return None
    steps = (payload.get("proposal") or {}).get("operations") or ()
    if not factory_ops.binds_tree(steps):  # a step on `release` names no tree
        return None
    return operations.deployed(ctx)


def deployed_key(decision) -> str:
    return decisions.deployed_key(decision)


def decision_basis(ctx, decision):
    """What the challenge binds besides the decision itself: its card's revision, or for monitor trust without a card
    the state of its P-n."""
    if not decision["card"]:
        from kd.cabinet import monitor_trust

        return monitor_trust.basis(ctx.store, decision)
    return _card(ctx, decision["card"]).revision


def _reader(ctx, principal, request):
    """Who may read which decision: a person — of visible projects; an agent or executor — its own."""
    if principal.kind == "user":
        return lambda d: decision_visible(ctx, principal, d)
    if principal.kind == "agent":
        return lambda d: d["requested_by"] == f"agent:{principal.agent}"
    if principal.kind == "executor":
        agent = _executor_agent(
            ctx, principal, request, coordinator=request.get("agent") == worker_board.COORDINATOR[1:]
        )
        return lambda d: d["requested_by"] == f"agent:{agent}"
    return lambda d: True


def _decision_get(ctx, principal, request):
    found = _ledger(ctx).get(request.get("id"))
    if found is None or not _reader(ctx, principal, request)(found):
        raise ApiError("not_found", "no such decision")
    return {"decision": found}


def _decision_list(ctx, principal, request):
    state, card = request.get("state"), request.get("card")
    if state is not None and state not in decisions.STATES:
        raise ApiError("invalid", f"state must be one of {', '.join(decisions.STATES)}")
    if card is not None and (not isinstance(card, str) or not CARD.fullmatch(card)):
        raise ApiError("invalid", "invalid card id")
    keep = _reader(ctx, principal, request)
    return {"decisions": [d for d in _ledger(ctx).list(state=state, card=card) if keep(d)]}


def _decider(ctx, principal, request) -> Principal:
    """The person deciding: the caller, or the one the HTTP layer names (``cabinet``); active."""
    if principal.kind == "user":
        return principal
    user = request.get("user")
    if not isinstance(user, str) or not people.USER_ID.fullmatch(user):
        raise ApiError("invalid", "no user named")
    person = ctx.users.get(user) if ctx.users is not None else None
    if person is None or person["status"] != "active":
        raise ApiError("refused", "unknown or blocked user")
    return Principal("user", -1, user)


def _deciding(ctx, principal, request) -> tuple:
    """The decision and the person, who must see its card and hold ``decide.<type>`` on its project."""
    person = _decider(ctx, principal, request)
    found = _ledger(ctx).get(request.get("id"))
    if found is None or not decision_visible(ctx, person, found):
        raise ApiError("not_found", "no such decision")
    _require(ctx, person, found["decide_permission"], found["project"])
    return found, person.user


def _decision_reject(ctx, principal, request):
    from kd.cabinet import effects

    found, user = _deciding(ctx, principal, request)
    text = _text(request.get("text"), "text", limit=decisions.TEXT_MAX)
    if found["state"] != "open":
        raise ApiError("refused", f"the decision is {found['state']}")
    _decision_call(lambda: effects.refuse(ctx, found, user=user, now=ctx.now()))
    # a PR's remarks wake the agent that asked with the PR's number
    extra = {"pr": found["payload"].get("number")} if found["type"] == "pr_approve" else None
    return {
        "decision": _decision_call(
            lambda: _ledger(ctx).reject(found["id"], user=user, text=text, now=ctx.now(), event_extra=extra)
        )
    }


def _decision_ask(ctx, principal, request):
    found, user = _deciding(ctx, principal, request)
    if not found["card"] or not found["requested_by"].startswith("agent:"):
        # the question wakes the agent that asked for the decision, on its card: here nobody reads it
        raise ApiError("refused", "no agent reads a question on this decision")
    return {
        "decision": _decision_call(
            lambda: _ledger(ctx).ask(found["id"], user=user, text=request.get("text"), now=ctx.now())
        )
    }


def _decision_reply(ctx, principal, request):
    """A person's answer: text, or for a question with a form (the agent's or one its text reads as)
    the chosen options and own words over the form the page showed (``form_sha256``)."""
    found, user = _deciding(ctx, principal, request)
    ledger = _ledger(ctx)
    form, origin = questions.form_of(found["payload"]) if found["type"] == "question" else (None, "")
    files, key = _reply_files(ctx, found, user, request)
    if "answers" not in request or found["type"] != "question":  # not a question: the ledger refuses it
        if origin == "agent":
            raise ApiError("invalid", "answer the form: answers, comment and form_sha256")
        spoken = _voice_answer(ctx, found, user, request.get("text"), files, key)
        if spoken is not None:
            return spoken
        text = _reply_text(found, request.get("text"), files)
        answer = {"files": _shown_files(files)} if files else None
        return {
            "decision": _decision_call(
                lambda: ledger.reply(found["id"], user=user, text=text, now=ctx.now(), answer=answer,
                                     attachments=files, client_key=key)
            )
        }
    if form is None:
        raise ApiError("invalid", "this question has no form; answer it with text")
    sha = questions.form_sha(form)
    if request.get("form_sha256") != sha:
        raise ApiError("conflict", "the question changed; read it again")
    try:
        comment = questions.comment_of(request.get("comment"))
        answers = questions.validate_answer(form, request["answers"], comment)
    except questions.AnswerError as exc:
        raise ApiError("invalid", str(exc))
    answer = {
        "schema_version": questions.SCHEMA_VERSION, "origin": origin, "form_sha256": sha,
        "answers": answers, "comment": comment,
    }
    if files:
        answer["files"] = _shown_files(files)
    text = questions.readable(form, answers, comment)
    return {
        "decision": _decision_call(
            lambda: ledger.reply(found["id"], user=user, text=text, now=ctx.now(), answer=answer,
                                 attachments=files, client_key=key)
        )
    }


def _shown_files(files: list) -> list:
    return [{k: f[k] for k in ("id", "name", "mime", "size")} for f in files]


def _reply_files(ctx, found, user, request):
    """The files and the page's send id of a reply: only a question takes files, within the same
    limits as a thread message."""
    key = request.get("client_key", "")
    if key != "" and not (isinstance(key, str) and CLIENT_KEY.fullmatch(key)):
        raise ApiError("invalid", "client_key must be 8 to 64 letters, digits or dashes")
    value = request.get("files", [])
    if value and found["type"] != "question":
        raise ApiError("invalid", "only a question takes files")
    return message_files(ctx, f"user:{user}", value), key


def _voice_answer(ctx, found, user, value, files, key) -> Optional[dict]:
    """A text question answered by recordings alone: they go to its thread as the person's message that
    answers it (``answers``) and wait for their transcription; the question stays open until the text answers it
    (``voice.settle``). None — not such an answer (no queue, a file question, text or a file that is no audio)."""
    author = f"user:{user}"
    text = value.strip() if isinstance(value, str) else ""
    sent = ctx.store._conn.execute(  # the same send again after a lost answer: what it wrote, whatever came since
        "SELECT seq FROM messages WHERE thread=? AND author=? AND json_extract(payload, '$.client')=?"
        " AND json_extract(payload, '$.answers')=?", (found["thread"], author, key, found["id"])).fetchone() if key else None
    if sent is not None and files and not text:
        message = ctx.store._message(found["thread"], sent[0])
        return {"decision": found, "message": voice.placed(ctx.store, [message])[0]}
    if (found["type"] != "question" or found["state"] != "open" or text or not files
            or questions.expect_of(found["payload"]) == "file"
            or len(voice.audio_ids(ctx, author, files)) != len(files)):  # off: no audio is transcribed
        return None
    with ctx.store.transaction():
        message = ctx.store.add_thread_message(
            found["thread"], author=author,
            **message_fields("", files, [f["id"] for f in files], extra={"answers": found["id"]}),
            client_key=key, attachments=files)
        voice.enqueue(ctx, message, found["thread"], author)
    return {"decision": _ledger(ctx).get(found["id"]), "message": voice.placed(ctx.store, [message])[0],
            "changed": True}


def _reply_text(found, value, files) -> str:
    """The reply's text by what the question expects: a file question is answered by a file, its text
    optional; a text question by text — a file, a recording among them, never stands for it (no transcription)."""
    if found["type"] != "question":
        return value
    text = value.strip() if isinstance(value, str) else ""
    if questions.expect_of(found["payload"]) == "file":
        if not files:
            raise ApiError("invalid", "this question asks for a file: attach one", reason="file_needed")
        return text
    if not text:
        raise ApiError("invalid", "this question needs a text answer; a recording or file is not one",
                       reason="text_needed")
    return value


def _decision_revoke(ctx, principal, request):
    from kd.cabinet import effects

    found, user = _deciding(ctx, principal, request)
    if found["state"] != "approved":
        raise ApiError("refused", f"the decision is {found['state']}")
    _decision_call(lambda: effects.revoke(ctx, found, user=user, now=ctx.now()))
    return {"decision": _decision_call(lambda: _ledger(ctx).revoke(found["id"], user=user, now=ctx.now()))}


def _decision_approve(ctx, principal, request):
    """The HTTP layer, after it checked the passkey assertion over the challenge: every decision must still
    be open, in term, the person's to decide and over the card revision the challenge bound; then each
    effect runs and its decision is recorded."""
    from kd.cabinet import effects

    person = _decider(ctx, principal, request)
    ids = request.get("ids") if "ids" in request else [request.get("id")]
    if (
        not isinstance(ids, list)
        or not 1 <= len(ids) <= BATCH_MAX
        or not all(isinstance(i, str) and DECISION_ID.fullmatch(i) for i in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ApiError("invalid", f"ids must be 1 to {BATCH_MAX} decision ids")
    revisions = request.get("revisions")
    if not isinstance(revisions, Mapping):
        raise ApiError("invalid", "revisions must map cards to the revisions the challenge bound")
    signature = request.get("signature")
    if signature is not None and not isinstance(signature, Mapping):
        raise ApiError("invalid", "signature must be the assertion object")
    now, ledger = ctx.now(), _ledger(ctx)
    batch = []
    for decision_id in ids:
        found = ledger.get(decision_id)
        if found is None or not decision_visible(ctx, person, found):
            raise ApiError("not_found", f"no decision {decision_id}")
        if found["state"] != "open":
            raise ApiError("refused", f"{decision_id}: the decision is {found['state']}")
        if found["type"] == "question":  # an answer is decision.reply; "approved" with no text would end it empty
            raise ApiError("refused", f"{decision_id}: a question is answered, not approved")
        if found["expires"] and found["expires"] <= now:
            raise ApiError("refused", f"{decision_id}: the decision expired")
        _require(ctx, person, found["decide_permission"], found["project"])
        if decision_basis(ctx, found) != revisions.get(decisions.basis_key(found)):
            raise ApiError("conflict", f"{found['card'] or decision_id}: changed since the challenge")
        tree = deployed_basis(ctx, found)
        if tree is not None and tree != revisions.get(deployed_key(found)):
            raise ApiError("conflict", f"{found['card']}: the deployed tree changed since the challenge")
        if found["step_up"] and not signature:
            raise ApiError("refused", f"{decision_id}: needs a passkey signature")
        batch.append(found)
    mode = _pr_mode(batch, signature)
    if request.get("check") is True:  # the web layer asks before kd-review posts the Approve
        return {"decisions": [], "checked": True}
    review, answer, extra = None, None, None
    if mode is not None:
        payload = batch[0]["payload"]
        review = _pr_review(request.get("review"), payload)
        answer = {"mode": mode, "review": review["id"]}
        extra = {"pr": payload["number"], "head": payload["head"], "mode": mode}
        if mode == "release":
            from kd.cabinet import prs_api  # at call time: it imports this module

            if prs_api.plan_pulls(ctx.store, batch[0]["card"]) > 1:
                extra["plan"] = True  # a chain plan's other pull requests merge first: one rollout, at the end
    signer = str(signature.get("id", ""))[:512] if signature else ""
    batch_id = request.get("batch_id") or ""
    if not isinstance(batch_id, str) or (batch_id and not DECISION_ID.fullmatch(batch_id)):
        raise ApiError("invalid", "batch_id must be 32 hex characters")
    results = []
    for found in batch:
        try:
            grant_id = _decision_call(
                lambda: effects.apply(
                    ctx, found, user=person.user, now=now, ttl_hours=request.get("ttl_hours"),
                    signer=signer, signature=_grant_signature(signature, found["id"]), review=review,
                )
            )
        except ApiError as exc:
            if results:  # the earlier ones of the batch are approved and recorded
                raise ApiError(exc.kind, f"{found['id']}: {exc} (approved before it: {len(results)})")
            raise
        results.append(
            _decision_call(
                lambda: ledger.approve(
                    found["id"], user=person.user, now=now, signature=signature, credential=request.get("credential"),
                    batch_id=batch_id, grant_id=grant_id, answer=answer, event_extra=extra or _approval_extra(found, revisions),
                )
            )
        )
    return {"decisions": results}


def _grant_signature(signature: Optional[Mapping], decision_id: str) -> str:
    """What the approval's effect keeps of the owner's signature: the assertion, the challenge's binding and the id
    of the decision it approves. A target's exec gate digests that id to recompute the signed value, and a grant's
    own ``decision_id`` is its journal's, not the cabinet's; one signature over a batch names each
    decision in its own grant."""
    if not signature:
        return ""
    return decisions.canonical({**signature, "decision": decision_id}).decode("utf-8")


def _approval_extra(found, revisions=None) -> Optional[dict]:
    """What the wake of an approved critical command names: the proposal ``kd-admin-critical`` runs."""
    if found["type"] == "admin_critical":
        return {"action": found["payload"]["proposal_id"]}
    if found["type"] == "admin_plan" and found["payload"].get("policy") == FACTORY_POLICY:
        extra = {"policy": FACTORY_POLICY}  # the wake names the factory's path, not the sysadmin's
        tree = (revisions or {}).get(deployed_key(found)) if found["card"] else None
        if tree:  # the tree the signature covered, for the history
            extra["revision"] = tree
        return extra
    return None


def _pr_mode(batch, signature) -> Optional[str]:
    """A pull request's approval is one decision and its signature binds the mode the person chose; the
    mode, or None for a batch of other types."""
    if not any(found["type"] == "pr_approve" for found in batch):
        return None
    if len(batch) != 1:
        raise ApiError("invalid", "a pull request is approved alone")
    binding = signature.get("binding") if isinstance(signature, Mapping) else None
    mode = binding.get("mode") if isinstance(binding, Mapping) else None
    if mode not in pr_signature.MODES:
        raise ApiError("invalid", f"the signature binds no mode ({', '.join(pr_signature.MODES)})")
    if mode == "release" and batch[0]["payload"].get("release") is not True:
        raise ApiError("invalid", "this project is not released from here")
    return mode


def _pr_review(review, payload) -> dict:
    """The Approve kd-review posted for this head: ``{id, head}``."""
    if (
        not isinstance(review, Mapping)
        or isinstance(review.get("id"), bool)
        or not isinstance(review.get("id"), int)
        or review["id"] < 1
        or review.get("head") != payload.get("head")
    ):
        raise ApiError("invalid", "review must be the Approve kd-review posted on this head: {id, head}")
    return {"id": review["id"], "head": review["head"]}


def _decision_expire(ctx, principal, request):
    return {"decisions": _decision_call(lambda: _ledger(ctx).expire(ctx.now()))}


def _action_consume(ctx, principal, request):
    """kd-admin-critical (the exec user) marks the approved action used before SSH (from the old page's socket)."""
    from kd import admin_grant, admin_task

    keys = ("task_id", "proposal_id", "action_sha256")
    if not all(isinstance(request.get(k), str) for k in keys):
        raise ApiError("invalid", f"request needs {', '.join(keys)}")
    if ctx.admin is None:
        raise ApiError("unavailable", "admin approvals are not served here")
    try:
        grant = ctx.admin.consume(
            request["task_id"], request["proposal_id"], request["action_sha256"], people.parse_time(ctx.now())
        )
    except (admin_grant.GrantError, admin_task.CardError, ValueError) as exc:
        raise ApiError("refused", str(exc)[:300] or exc.__class__.__name__)
    except OSError as exc:
        raise ApiError("unavailable", exc.__class__.__name__)
    return {"action_id": grant.action_id}


# --- push and notification preferences ----------------------------------------
ENDPOINT_MAX, KEY_MAX, PUSH_TAKE_MAX = 1000, 200, 200
PUSH_RETRY = 300  # seconds before a notification the gateway refused is tried again
B64URL = re.compile(r"[A-Za-z0-9_-]+=*")


def _prefs_get(ctx, principal, request):
    from kd.cabinet import notify

    return {"prefs": notify.full_prefs(ctx.users.prefs(principal.user))}


def _prefs_set(ctx, principal, request):
    from kd.cabinet import notify

    try:
        stored = ctx.users.set_prefs(principal.user, request.get("prefs"), actor=card_actor(principal))
    except ValueError as exc:
        raise ApiError("invalid", str(exc))
    return {"prefs": notify.full_prefs(stored)}


def _endpoint(value) -> str:
    if not isinstance(value, str) or not value.startswith("https://") or len(value) > ENDPOINT_MAX:
        raise ApiError("invalid", "endpoint must be an https URL")
    return value


def _push_subscribe(ctx, principal, request):
    from kd.cabinet import push_worker

    endpoint = _endpoint(request.get("endpoint"))
    if not push_worker.gateway_ok(endpoint):
        raise ApiError("invalid", "endpoint must be a browser push service")
    keys = request.get("keys")
    if not isinstance(keys, Mapping) or not all(
        isinstance(keys.get(k), str) and 0 < len(keys[k]) <= KEY_MAX and B64URL.fullmatch(keys[k])
        for k in ("p256dh", "auth")
    ):
        raise ApiError("invalid", "keys must hold p256dh and auth")
    device = _text(request.get("device", ""), "device", limit=people.DEVICE_MAX, empty=True)
    ctx.store.subscribe(
        principal.user, endpoint, keys["p256dh"], keys["auth"], device, actor=card_actor(principal)
    )
    return {"subscriptions": ctx.store.subscriptions(principal.user)}


def _push_unsubscribe(ctx, principal, request):
    ctx.store.unsubscribe(principal.user, _endpoint(request.get("endpoint")), actor=card_actor(principal))
    return {"subscriptions": ctx.store.subscriptions(principal.user)}


def _push_mine(ctx, principal, request):
    """Whether the browser's subscription is this person's: the device may still hold someone else's
    (global review 15.10.2026: the settings page said "on" for a subscription of the previous person)."""
    endpoint = _endpoint(request.get("endpoint"))
    mine = any(s["endpoint"] == endpoint for s in ctx.store.subscriptions(principal.user))
    # this device's last delivery as the gateway answered it — accepted is not shown by the system
    return {"mine": mine, "last": ctx.store.push_last(principal.user, endpoint) if mine else None}


def _push_take(ctx, principal, request):
    limit = request.get("limit", 50)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 0 < limit <= PUSH_TAKE_MAX:
        raise ApiError("invalid", f"limit is 1 to {PUSH_TAKE_MAX}")
    now = ctx.now()
    rows = ctx.store.take_notifications(now, people.shift(now, seconds=-PUSH_RETRY), limit)
    inbox = {}
    for row in rows:  # a batch and the digest are not urgent; everything else is
        row["urgency"] = "normal" if row.pop("kind") in ("batch", "digest") else "high"
        user = row.pop("user")
        if user not in inbox:
            inbox[user] = inbox_count(ctx, Principal("user", -1, user))
        row["badge"] = inbox[user]  # the installed app's badge is the person's inbox, as of now
    return {"items": rows}


def inbox_count(ctx, principal) -> int:
    """Decisions the person may take, agents' questions to them, cards in Review they see and the open crit incidents
    nobody acknowledged or muted: the inbox's badge, the tab's title and the installed app's icon."""
    try:
        open_ = dispatch(ctx, principal, {"op": "decision.list", "state": "open"})
        review = dispatch(ctx, principal, {"op": "card.list", "status": "Review"})
    except ApiError:
        return 0
    deferred = defers.active_map(ctx.store)  # a card put off until a time is out of the inbox till then
    mine = [d for d in open_["decisions"] if ctx.users.can(principal.user, d["decide_permission"], d["project"] or None)
            and d.get("card") not in deferred]
    rows = {(d.get("payload") or {}).get("bundle") or d["id"] for d in mine}  # a bundle is one inbox row
    hidden = stage_gate.hidden_ids(ctx.store)  # a chain plan's stage the coordinator still decides on
    review = [c for c in review["cards"] if c["id"] not in deferred and c["id"] not in hidden]
    failed = len(deploys.alerts(ctx.store)) if allowed(ctx, principal, "deploys.read") else 0  # deploys nobody saw
    return len(rows) + len(review) + len(incidents.collapse(incidents.alarms(ctx, principal))) + failed


def _push_sent(ctx, principal, request):
    results = request.get("results")
    if not isinstance(results, list) or len(results) > PUSH_TAKE_MAX or not all(
        isinstance(r, Mapping)
        and isinstance(r.get("id"), int)
        and isinstance(r.get("endpoint"), str)
        and isinstance(r.get("ok"), bool)
        and isinstance(r.get("status"), int)
        for r in results
    ):
        raise ApiError("invalid", "results are [{id, endpoint, ok, status}]")
    ctx.store.notifications_sent(results)
    return {"count": len(results)}


def _notify_tick(ctx, principal, request):
    if ctx.notifier is None:
        raise ApiError("unavailable", "notifications are not served here")
    return {"tick": ctx.notifier.tick(ctx.now())}


# the Forgejo executor reads nothing of the board; the host reader only asks whose operation's journal it
# is
READERS = frozenset(KINDS) - {"forge", "observe"}
ROOTISH = frozenset({"root", "cabinet"})
OPS = {
    "card.get": (READERS, _get),
    "card.list": (READERS, _list),
    "card.search": (READERS, _search),
    "thread.messages": (READERS, _messages),
    "card.events": (READERS, _card_events),
    "file.get": (READERS, _file_get),
    "project.list": (READERS, _projects),
    "assignee.list": (READERS, _assignees),
    "card.comment": (frozenset({"executor"}), _comment),
    "card.promote": (frozenset({"root", "cabinet"}), _promote),
    "attention.ack": (frozenset({"executor"}), _ack),
    "attention.open.get": (frozenset({"executor", "root", "cabinet"}), _open_get),
    "attention.open.set": (ROOTISH, _open_set),
    "attention.acks.take": (ROOTISH, _acks_take),
    "state.get": (ROOTISH, _state_get),
    "state.set": (ROOTISH, _state_set),
    "events.since": (ROOTISH | {"user"}, _events),
    "user.card.create": (frozenset({"user"}), _user_card_create),
    "user.card.edit": (frozenset({"user"}), _user_card_edit),
    "user.card.comment": (frozenset({"user"}), _user_comment),
    "card.create": (frozenset({"root"}), _root_card_create),
    "card.edit": (frozenset({"root"}), _root_card_edit),
    "user.file.delete": (frozenset({"user"}), _user_file_delete),
    "user.create": (frozenset({"root", "user"}), _user_create),
    "user.code": (frozenset({"root", "user"}), _user_code),
    "user.signing": (frozenset({"root"}), _user_signing),
    "user.update": (frozenset({"root", "user"}), _user_update),
    "user.block": (frozenset({"root", "user"}), _user_block),
    "user.list": (frozenset({"root", "user"}), _user_list),
    "role.set": (frozenset({"root", "user"}), _role_set),
    "user.me": (frozenset({"user"}), _me),
    "decision.open": (frozenset({"agent", "executor"}), _decision_open),
    "decision.get": (frozenset({"user", "root", "cabinet", "executor", "agent"}), _decision_get),
    "decision.list": (frozenset({"user", "root", "cabinet", "executor", "agent"}), _decision_list),
    "decision.reject": (frozenset({"user", "cabinet"}), _decision_reject),
    "decision.ask": (frozenset({"user", "cabinet"}), _decision_ask),
    "decision.reply": (frozenset({"user", "cabinet"}), _decision_reply),
    "decision.revoke": (frozenset({"user", "cabinet"}), _decision_revoke),
    "decision.approve": (frozenset({"cabinet"}), _decision_approve),
    "decision.expire": (ROOTISH, _decision_expire),
    "action.consume": (frozenset({"exec"}), _action_consume),
    "prefs.get": (frozenset({"user"}), _prefs_get),
    "prefs.set": (frozenset({"user"}), _prefs_set),
    "push.subscribe": (frozenset({"user"}), _push_subscribe),
    "push.unsubscribe": (frozenset({"user"}), _push_unsubscribe),
    "push.mine": (frozenset({"user"}), _push_mine),
    "push.take": (ROOTISH | {"push"}, _push_take),
    "push.sent": (ROOTISH | {"push"}, _push_sent),
    "notify.tick": (ROOTISH, _notify_tick),
}
OPS.update(
    {
        f"worker.{op}": (frozenset({"executor"}), _worker(op))
        for op in worker_board.OPERATIONS
    }
)
OPS.update(
    {f"boss.{op}": (frozenset({"executor"}), _boss(op)) for op in boss_board.OPERATIONS}
)
# The session host's operations: its handlers use this module's helpers, and the module adds its OPS
# here itself, as its last line — so either module may be the one a process imports first.
from kd.cabinet import sessions_api  # noqa: E402
from kd.cabinet import flow  # noqa: E402 — the card's actions, the same way
from kd.cabinet import result as _result_api  # noqa: E402,F401 — registers card result operations
from kd.cabinet import overview  # noqa: E402,F401 — the overview and a person's marks, the same way
from kd.cabinet import jobs  # noqa: E402,F401 — worker jobs, the same way
from kd.cabinet import diag_api  # noqa: E402,F401 — diagnostics on a card (R31), the same way
from kd.cabinet import notices  # noqa: E402,F401 — notices of root services and executors, the same way
from kd.cabinet import prs_api  # noqa: E402,F401 — dev's pull requests, the same way
from kd.cabinet import chats  # noqa: E402,F401 — read cursors of agent chats, the same way
from kd.cabinet import closing  # noqa: E402,F401 — closing a card without the work, the same way
from kd.cabinet import creating  # noqa: E402,F401 — BOSS's look before a create, its brief requests
from kd.cabinet import boss_review  # noqa: E402,F401 — BOSS hands checked work to the owner's Review
from kd.cabinet import stage_gate  # noqa: E402,F401 — BOSS's decision on a stage of a chain plan
from kd.cabinet import projects_api  # noqa: E402 — the owner's projects, the same way
from kd.cabinet import llm_api  # noqa: E402 — the models of other providers, the same way
from kd.cabinet import context_limits  # noqa: E402,F401 — each role's session context limit, the same way
from kd.cabinet import councils  # noqa: E402 — the council's composition, the same way
from kd.cabinet import council_api  # noqa: E402,F401 — kd-work council and council-reply
from kd.cabinet import snapshot  # noqa: E402 — the card's snapshot, the same way
from kd.cabinet import pages  # noqa: E402,F401 — pages of the scope, result, a message and the thread
from kd.cabinet import outcomes  # noqa: E402 — a turn's outcome, kd-work finish, the same way
from kd.cabinet import files_api  # noqa: E402,F401 — agents' kd-task file get|add, the same way
from kd.cabinet import operations as _operations_api  # noqa: E402,F401 — registers factory operations
from kd.cabinet import devchecks as _devchecks_api  # noqa: E402,F401 — registers dev.check and contours
from kd.cabinet import incidents  # noqa: E402 — the monitor's servers, checks and incidents, the same way
from kd.cabinet import monitoring  # noqa: E402,F401 — registers incident investigations and read models
from kd.cabinet import assists  # noqa: E402,F401 — help inside an approved task, the same way
from kd.cabinet import monitor_config  # noqa: E402,F401 — the monitor's settings over config.json, the same way
from kd.cabinet import monitor_trust  # noqa: E402,F401 — the monitor's trust by signature, the same way
from kd.cabinet import brief_reviews  # noqa: E402,F401 — registers independent version-bound brief reviews
from kd.cabinet import monitor_test  # noqa: E402,F401 — the owner's test critical alert, the same way
from kd.cabinet import monitor_windows  # noqa: E402,F401 — the sysadmin's plan windows, the same way
from kd.cabinet import defers  # noqa: E402 — a card put off until a time, the same way
from kd.cabinet import deploys  # noqa: E402 — the projects' deploys and their runs, the same way

OPS.update(voice.OPS)  # "Retry" of a transcription

def install_host(ctx, config) -> None:
    sessions_api.install(ctx, config)


OPS["boss.comment"] = (
    frozenset({"executor", "cabinet"}),
    _boss("comment"),
)  # the approval's note on a delegated goal


def dispatch(ctx: Context, principal: Optional[Principal], request: Mapping) -> dict:
    """``{"ok": True, ...}`` for one request, or ``ApiError``; a card write carries ``"changed": True``."""
    if principal is None:
        raise ApiError("refused", "unknown caller")
    if not isinstance(request, Mapping) or request.get("op") not in OPS:
        raise ApiError("invalid", "unknown operation")
    request = ids.canonical_all(dict(request))  # a card typed with an earlier prefix of the host
    kinds, handler = OPS[request["op"]]
    if principal.kind not in kinds:
        raise ApiError("refused", f"{request['op']} is not for {principal.kind}")
    if principal.kind == "user" or request["op"].startswith(("user.", "role.")):
        if ctx.users is None:
            raise ApiError("unavailable", "people are not served here")
    if principal.kind == "user":
        person = ctx.users.get(principal.user)
        if person is None or person["status"] != "active":
            raise ApiError("refused", "unknown or blocked user")
    result = handler(ctx, principal, request)
    return {"ok": True, **result}
