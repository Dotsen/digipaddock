"""The session host's socket protocol: one JSON line in, one JSON line out, from the cabinet only.

``deliver`` hands the host one command for one session (the host is idempotent by command id; ``urgent`` true —
it starts at once over the agent's slots; ``space`` ``monitor`` names the agent's "Monitor" thread
for the launch's context; ``memory`` — the Hindsight bank of a card's project; ``model`` and ``effort`` —
the cabinet model's spec and the effort level a card's session runs on; ``context_limit`` — the role's session
context limit in tokens, ``kd.context_limit``), ``cancel`` takes a
delivered command that has not started out of the host's queues, ``stop`` stops a session, ``revoke`` cuts the process holding
a probe exit its card no longer grants (the session goes on without it), ``status`` tells the
sessions and slots. ``job`` hands it a worker job (its definition goes to the runner's stdin), and
``job_cancel`` takes a waiting job back or stops a running one. ``operation`` asks for a factory
operation: the host pauses and drains the named roles and runs the root runner's unit (``start`` false only asks
about one it should already hold); ``resume`` lifts a role's pause (the cabinet's, or root's on the host). ``probe``
asks for one short run on a cabinet model's spec. Every field is checked here: the text reaches the agent only
as a stream-json user message, and nothing of a request reaches a command line but checked ids.
"""

import json
import re

from kd import context_limit, llm_models, memory_bank, worker_inputs, worker_jobs
from kd.egress import EXIT as EGRESS

HEX32 = re.compile(r"[0-9a-f]{32}")
AGENT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
CARD = re.compile(r"[A-Z][A-Z0-9]{0,7}-[1-9][0-9]{0,5}")
SPACES = ("monitor",)  # an agent's thread other than its chat (absent — the chat, or the card's thread)
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")
TEXT_MAX = 2000
# one request line: a job's definition and the inputs it may carry (kd.worker_inputs.MAX on the wire) —
# every other request is far under it
REQUEST_MAX = 1 << 19
DEFINITION_MAX = 48 * 1024  # the definition without its inputs
JOB_TIMEOUT = (60, 14400)
REINSTALL, COMPONENT = "agent.reinstall", "component.install"
OPERATION_KINDS = (REINSTALL, COMPONENT)
NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")  # a component or a mode: a name of the catalog, never a path
DRAINS = ("", "all")
# Claude refused the session's credentials — a turn's and its session's error; no turn of that process can
# run
AUTH_FAILED = "auth_failed"
OPERATION_TIMEOUT = (60, 7200)
OPERATION_AGENTS_MAX = 16
REVISION = re.compile(r"[0-9a-f]{7,40}")
REASON = re.compile(r"[a-z][a-z_]{0,39}")  # why the cabinet cancels an operation
# a dev.check: what it checks, as the cabinet keeps it in the operation's params
CHECK_TIMEOUT = (60, 4200)
CHECK_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")  # a project, a profile, a contour (kd.contours.NAME_RE)
SHA = re.compile(r"[0-9a-f]{40}")
REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
CHECK_PARAMS = {"card", "project", "repo", "pr", "head", "base_sha", "target", "profiles", "contour", "timeout_s",
                "set"}
CANCELS = ("card_left", "superseded", "root")
FIELDS = {
    "deliver": {"op", "command", "session", "agent", "thread", "card", "text", "resume", "urgent", "space", "memory",
                "model", "effort", "egress", "context_limit"},
    "stop": {"op", "session", "command"},
    "revoke": {"op", "session", "egress"},
    "cancel": {"op", "session", "command"},
    "status": {"op"},
    "job": {"op", "job", "agent", "definition"},
    "job_cancel": {"op", "job"},
    "operation": {"op", "operation", "kind", "agents", "revision", "timeout", "start", "component", "mode", "drain"},
    "resume": {"op", "agent"},
    "operation_cancel": {"op", "operation", "reason"},
    "devcheck": {"op", "check", "start", "timeout", "params"},
    "devcheck_cancel": {"op", "check", "reason"},
    "probe": {"op", "probe", "model"},  # one short run on a cabinet model (kd.session_host.probe)
}


class ProtocolError(Exception):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def _bad(message):
    return ProtocolError("invalid", message)


def parse(line: bytes) -> dict:
    if len(line) > REQUEST_MAX:
        raise _bad("request too large")
    try:
        request = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise _bad("one JSON object per line is expected") from None
    if not isinstance(request, dict) or request.get("op") not in FIELDS:
        raise _bad("unknown operation")
    op = request["op"]
    if set(request) - FIELDS[op]:
        raise _bad(f"unknown field for {op}")
    if op == "status":
        return request
    if op in ("job", "job_cancel"):
        return _job(request)
    if op == "operation":
        return _operation(request)
    if op in ("devcheck", "devcheck_cancel"):
        return _devcheck(request)
    if op == "probe":
        return _probe(request)
    if op == "operation_cancel":
        if set(request) != FIELDS[op]:
            raise _bad("operation_cancel needs operation and reason")
        if not isinstance(request["operation"], str) or not HEX32.fullmatch(request["operation"]):
            raise _bad("invalid operation id")
        if not isinstance(request["reason"], str) or not REASON.fullmatch(request["reason"]):
            raise _bad("invalid reason")
        return request
    if op == "resume":
        if not isinstance(request.get("agent"), str) or not AGENT.fullmatch(request["agent"]):
            raise _bad("invalid agent")
        return request
    if not isinstance(request.get("session"), str) or not HEX32.fullmatch(request["session"]):
        raise _bad("invalid session id")
    if op == "stop":
        if "command" in request and not (isinstance(request["command"], str) and HEX32.fullmatch(request["command"])):
            raise _bad("invalid command id")
        return request
    if op == "revoke":
        if not (isinstance(request.get("egress"), str) and EGRESS.fullmatch(request["egress"])):
            raise _bad("revoke needs an exit name")
        return request
    if op == "cancel":
        if not (isinstance(request.get("command"), str) and HEX32.fullmatch(request["command"])):
            raise _bad("cancel needs a command id")
        return request
    optional = {"urgent", "space", "memory", "model", "effort", "egress", "context_limit"}
    if set(request) - optional != FIELDS["deliver"] - optional:
        raise _bad("deliver needs command, session, agent, thread, card, text and resume")
    # the model and effort of a card's session — the cabinet model's spec and a level, present only when
    # set
    if "model" in request:
        if request["card"] == "":
            raise _bad("model is only with a card")
        try:
            request["model"] = llm_models.check_spec(request["model"])
        except ValueError as exc:
            raise _bad(f"model: {exc}") from None
    if "effort" in request:
        try:
            level = llm_models.check_effort(request["effort"])
        except ValueError as exc:
            raise _bad(f"effort: {exc}") from None
        if not level or request["card"] == "":
            raise _bad("effort is a level, only with a card")
    # the role's session context limit in tokens, present only when the role has one (any session: a card's, the chat)
    if "context_limit" in request:
        try:
            context_limit.check(request["context_limit"])
        except ValueError as exc:
            raise _bad(f"context_limit: {exc}") from None
    # the autonomy design, piece 2: a card's probe exit, present only when the cabinet granted it
    egress = request.get("egress")
    if "egress" in request and not (isinstance(egress, str) and EGRESS.fullmatch(egress) and request["card"] != ""):
        raise _bad("egress is an exit name, only with a card")
    if "urgent" in request and request["urgent"] is not True:  # present only to start at once
        raise _bad("urgent must be true when given")
    if "space" in request and (request["space"] not in SPACES or request["card"] != ""):
        raise _bad(f"space is one of {', '.join(SPACES)}, only without a card")
    memory = request.get("memory")  # present only for a card of a project with a bank
    if "memory" in request and not (isinstance(memory, str) and memory_bank.BANK.fullmatch(memory)
                                    and request["card"] != ""):
        raise _bad("memory is a bank id, only with a card")
    if not isinstance(request["command"], str) or not HEX32.fullmatch(request["command"]):
        raise _bad("invalid command id")
    if not isinstance(request["agent"], str) or not AGENT.fullmatch(request["agent"]):
        raise _bad("invalid agent")
    thread = request["thread"]
    if not isinstance(thread, int) or isinstance(thread, bool) or thread <= 0:
        raise _bad("invalid thread")
    card = request["card"]
    if card != "" and not (isinstance(card, str) and CARD.fullmatch(card)):
        raise _bad("invalid card")
    text = request["text"]
    if not isinstance(text, str) or not text.strip() or len(text) > TEXT_MAX or _CTRL.search(text):
        raise _bad(f"text must be one line of 1 to {TEXT_MAX} characters")
    resume = request["resume"]
    if resume != "" and not (isinstance(resume, str) and UUID.fullmatch(resume)):
        raise _bad("invalid resume id")
    return request


def _job(request: dict) -> dict:
    if not isinstance(request.get("job"), str) or not HEX32.fullmatch(request["job"]):
        raise _bad("invalid job id")
    if request["op"] == "job_cancel":
        return request
    if set(request) != FIELDS["job"]:
        raise _bad("job needs job, agent and definition")
    if not isinstance(request["agent"], str) or not AGENT.fullmatch(request["agent"]):
        raise _bad("invalid agent")
    definition = request["definition"]
    own = {k: v for k, v in definition.items() if k != "inputs"} if isinstance(definition, dict) else None
    if own is None or len(json.dumps(own, ensure_ascii=False)) > DEFINITION_MAX:
        raise _bad(f"a job definition is an object of at most {DEFINITION_MAX} bytes")
    if "inputs" in definition:  # what a job of the cabinet reads instead of the board
        try:
            worker_inputs.check(definition["inputs"])
        except ValueError as exc:
            raise _bad(f"job definition: {exc}") from None
    if definition.get("id") != request["job"] or definition.get("agent") != request["agent"]:
        raise _bad("the definition is of another job or agent")
    timeout = definition.get("timeout")
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not JOB_TIMEOUT[0] <= timeout <= JOB_TIMEOUT[1]:
        raise _bad("invalid job timeout")
    try:  # a job on a cabinet model carries the model's spec; an effort comes with or without one
        if "model_spec" in definition:
            llm_models.check_spec(definition["model_spec"])
        if "effort" in definition:
            llm_models.check_effort(definition["effort"])
        if "pull" in definition:  # the runner puts the job's copy on a pull request's head
            worker_jobs.check_pull(definition["pull"])
        if "context_limit" in definition:  # the role's session context limit, as the cabinet sent the job
            context_limit.check(definition["context_limit"])
    except ValueError as exc:
        raise _bad(f"job definition: {exc}") from None
    return request


_COMPONENT_FIELDS = {"component", "mode", "drain"}


def _operation(request: dict) -> dict:
    component = request.get("kind") == COMPONENT
    needed = FIELDS["operation"] if component else FIELDS["operation"] - _COMPONENT_FIELDS
    if set(request) != needed:
        raise _bad("operation needs operation, kind, agents, revision, timeout and start"
                   + (", component, mode and drain" if component else ""))
    if not isinstance(request["operation"], str) or not HEX32.fullmatch(request["operation"]):
        raise _bad("invalid operation id")
    if request["kind"] not in OPERATION_KINDS:
        raise _bad("unknown operation kind")
    agents = request["agents"]
    if component:  # no roles of its own — the host drains all of them for ``drain: all`` itself
        if agents != []:
            raise _bad("a component operation names no agents")
        if not all(isinstance(request[k], str) and NAME.fullmatch(request[k]) for k in ("component", "mode")):
            raise _bad("component and mode are names")
        if request["drain"] not in DRAINS:
            raise _bad("drain is empty or all")
    elif (not isinstance(agents, list) or not 0 < len(agents) <= OPERATION_AGENTS_MAX
            or not all(isinstance(a, str) and AGENT.fullmatch(a) for a in agents) or len(set(agents)) != len(agents)):
        raise _bad(f"agents is a list of 1 to {OPERATION_AGENTS_MAX} distinct agent names")
    if not isinstance(request["revision"], str) or not REVISION.fullmatch(request["revision"]):
        raise _bad("invalid revision")
    timeout = request["timeout"]
    if (not isinstance(timeout, int) or isinstance(timeout, bool)
            or not OPERATION_TIMEOUT[0] <= timeout <= OPERATION_TIMEOUT[1]):
        raise _bad("invalid operation timeout")
    if not isinstance(request["start"], bool):
        raise _bad("start is true or false")
    return request


def _check_params(params) -> None:
    if not isinstance(params, dict) or not CHECK_PARAMS <= set(params) <= CHECK_PARAMS | {"cancel"}:
        raise _bad("params are what the check checks")
    if not isinstance(params["card"], str) or not CARD.fullmatch(params["card"]):
        raise _bad("card is the check's card")
    if not all(isinstance(params[k], str) and CHECK_NAME.fullmatch(params[k]) for k in ("project", "contour")):
        raise _bad("project and contour are names")
    if not isinstance(params["repo"], str) or not REPO.fullmatch(params["repo"]) or ".." in params["repo"]:
        raise _bad("repo is owner/name")
    if not isinstance(params["target"], str) or not BRANCH.fullmatch(params["target"]) or ".." in params["target"]:
        raise _bad("target is a branch")
    if not all(isinstance(params[k], str) and SHA.fullmatch(params[k]) for k in ("head", "base_sha")):
        raise _bad("head and base_sha are full SHAs")
    for key in ("pr", "timeout_s"):
        if not isinstance(params[key], int) or isinstance(params[key], bool) or params[key] <= 0:
            raise _bad(f"{key} is a positive number")
    profiles = params["profiles"]
    if (not isinstance(profiles, list) or not 0 < len(profiles) <= 8 or len(set(profiles)) != len(profiles)
            or not all(isinstance(p, str) and CHECK_NAME.fullmatch(p) for p in profiles)):
        raise _bad("profiles are 1 to 8 distinct names")
    components = params["set"]
    if not isinstance(components, dict) or len(components) > 8 or not all(
            isinstance(k, str) and CHECK_NAME.fullmatch(k) and isinstance(v, str) and SHA.fullmatch(v)
            for k, v in components.items()):
        raise _bad("set is project -> full SHA")
    if "cancel" in params and params["cancel"] not in CANCELS:
        raise _bad("cancel is " + ", ".join(CANCELS))


def _devcheck(request: dict) -> dict:
    if set(request) != FIELDS[request["op"]]:
        raise _bad(f"{request['op']} needs " + ", ".join(sorted(FIELDS[request["op"]] - {"op"})))
    if not isinstance(request["check"], str) or not HEX32.fullmatch(request["check"]):
        raise _bad("invalid check id")
    if request["op"] == "devcheck_cancel":
        if request["reason"] not in CANCELS:
            raise _bad("reason is " + ", ".join(CANCELS))
        return request
    if not isinstance(request["start"], bool):
        raise _bad("start is true or false")
    timeout = request["timeout"]
    if not isinstance(timeout, int) or isinstance(timeout, bool) or not CHECK_TIMEOUT[0] <= timeout <= CHECK_TIMEOUT[1]:
        raise _bad("invalid check timeout")
    _check_params(request["params"])
    return request


def _probe(request: dict) -> dict:
    if set(request) != FIELDS["probe"]:
        raise _bad("probe needs probe and model")
    if not isinstance(request["probe"], str) or not HEX32.fullmatch(request["probe"]):
        raise _bad("invalid probe id")
    if request["model"] is None:  # fixed subscription credential, never a caller-supplied path
        return request
    try:
        request["model"] = llm_models.check_spec(request["model"])
    except ValueError as exc:
        raise _bad(f"model: {exc}") from None
    return request


def ok(**fields) -> bytes:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False).encode("utf-8") + b"\n"


def error(kind: str, message: str) -> bytes:
    return json.dumps({"ok": False, "error": kind, "message": message}, ensure_ascii=False).encode("utf-8") + b"\n"
