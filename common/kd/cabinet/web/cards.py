"""Cards, their threads and history, projects and the inbox over HTTP.

Every route acts as the signed-in person through the board API (``api.dispatch`` under the service's
lock): what a person may not read answers 404, what they may not do answers 403.
"""

from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from kd.cabinet import api, defers, deploys, incidents, stage_gate
from kd.cabinet.web import deps, stepup
from kd.cabinet.web.errors import Failure

router = APIRouter()


async def call(request: Request, user: deps.User, op: dict) -> dict:
    return await deps.svc(request).call(deps.principal(user), op)


def author_name(service, author: str) -> str:
    """``user:owner`` -> the person's name, ``agent:dev`` -> ``@dev``, anything else as it is."""
    if author.startswith("user:"):
        person = service.users.get(author[5:])
        return person["name"] if person else author[5:]
    if author.startswith("agent:"):
        return "@" + author[6:]
    return author


def with_names(service, messages):
    return [{**m, "author_name": author_name(service, m["author"])} for m in messages]


@router.get("/api/projects")
async def projects(request: Request, user: deps.User = Depends(deps.current_user)):
    found = (await call(request, user, {"op": "project.list"}))["items"]
    full = deps.svc(request).users.can(user.id, "settings")
    return [_project(p, full) for p in found]


REPO_FIELDS = ("repo_state", "repo_detail", "repo_updated", "repo_release")
SETTINGS_FIELDS = REPO_FIELDS + ("memory_bank", "done_by", "model", "effort")


def _project(found: dict, full: bool = True) -> dict:
    """A project for the screens; with ``settings`` also its repository's request or result, its memory
    bank (a partner never sees which memory a project has), who closes its cards Done and the
    model and effort its cards' sessions run on by default. The default council of its cards
    is everyone's to read: the card shows who criticises it."""
    out = {k: found[k] for k in ("id", "name", "route", "repo")}
    out["council"] = found.get("council") or {}
    if full:
        out.update({k: found.get(k, False if k == "repo_release" else "") for k in SETTINGS_FIELDS})
    return out


def _settings(request: Request, user: deps.User):
    """Who may not manage projects hears so before any step-up is spent."""
    if not deps.svc(request).users.can(user.id, "settings"):
        raise Failure(403, "refused", "settings is needed")


@router.get("/api/projects/routes")
async def project_routes(request: Request, user: deps.User = Depends(deps.current_user)):
    """The routes the Projects section offers: ``@owner`` and the registered agents but the coordinator."""
    _settings(request, user)
    return (await call(request, user, {"op": "project.routes"}))["routes"]


@router.get("/api/projects/{project_id}/impact")
async def project_impact(project_id: str, request: Request, user: deps.User = Depends(deps.current_user)):
    """The delete dialog's facts: ``{id, cards, users: [{id, name}], bound}``."""
    _settings(request, user)
    found = await call(request, user, {"op": "project.impact", "id": project_id})
    service = deps.svc(request)
    return {
        "id": found["id"],
        "cards": found["cards"],
        "users": [{"id": u, "name": author_name(service, f"user:{u}")} for u in found["users"]],
        "bound": found["bound"],
    }


@router.post("/api/projects")
async def project_create(request: Request, user: deps.User = Depends(deps.writer)):
    """The owner's new project: ``{name, route[, id]}``; the id comes from the name unless given."""
    body = await deps.json_body(request)
    _settings(request, user)
    op = {"op": "project.create", **{k: body[k] for k in ("name", "route", "id") if k in body}}
    return _project((await call(request, user, op))["project"])


@router.patch("/api/projects/{project_id}")
async def project_update(project_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """A new name, route, memory bank, closer, default model, effort or council; the id never changes (an ``id`` in
    the body other than the path's is refused)."""
    body = await deps.json_body(request)
    _settings(request, user)
    if "id" in body and body["id"] != project_id:
        raise Failure(400, "invalid", "a project's id never changes")
    fields = ("name", "route", "memory_bank", "done_by", "model", "effort", "council")
    op = {"op": "project.update", "id": project_id, **{k: body[k] for k in fields if k in body}}
    return _project((await call(request, user, op))["project"])


@router.delete("/api/projects/{project_id}")
async def project_delete(project_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """Delete a project, its cards moving to ``target`` (``{target, stepup}``): confirmed with a passkey or TOTP."""
    body = await deps.json_body(request) if await request.body() else {}
    _settings(request, user)
    await stepup.require(request, user, body, "projects.delete")
    op = {"op": "project.delete", "id": project_id}
    if body.get("target"):
        op["target"] = body["target"]
    return await call(request, user, op)


@router.post("/api/projects/{project_id}/repo")
async def project_repo_bind(project_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """Ask the root unit to bind ``{repo: "owner/repo"}``; a bad name is refused here, before any request."""
    body = await deps.json_body(request)
    _settings(request, user)
    found = await call(request, user, {"op": "project.repo.bind", "id": project_id, "repo": body.get("repo")})
    return _project(found["project"])


@router.delete("/api/projects/{project_id}/repo")
async def project_repo_unbind(project_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """Ask the root unit to take the project out of the repository registry; the clones stay."""
    _settings(request, user)
    found = await call(request, user, {"op": "project.repo.unbind", "id": project_id})
    return _project(found["project"])


@router.get("/api/assignees")
async def assignees(request: Request, user: deps.User = Depends(deps.current_user)):
    return (await call(request, user, {"op": "assignee.list"}))["assignees"]


@router.get("/api/cards")
async def cards(
    request: Request,
    user: deps.User = Depends(deps.current_user),
    status: Optional[str] = None,
    project: Optional[str] = None,
    assignee: Optional[str] = None,
    label: Optional[str] = None,
    q: Optional[str] = None,
):
    given = {
        "status": status,
        "project": project,
        "assignee": assignee,
        "label": label,
    }
    filters = {k: v for k, v in given.items() if v}
    if not q:
        return (await call(request, user, {"op": "card.list", **filters}))["cards"]
    found = (await call(request, user, {"op": "card.search", "text": q}))["cards"]

    def keep(card):
        for key, value in filters.items():
            if key == "label" and value not in card["labels"] + [
                f"server:{s}" for s in card["servers"]
            ]:
                return False
            if key != "label" and card.get(key) != value:
                return False
        return True

    return [c for c in found if keep(c)]


@router.post("/api/cards")
async def create(request: Request, user: deps.User = Depends(deps.writer)):
    body = await deps.json_body(request)
    op = {
        "op": "user.card.create",
        # the new card's form — the assignee and what an edit could set on the card
        **{k: body[k] for k in ("title", "project", "description", "assignee", *api.CREATE_FIELDS) if k in body},
    }
    card = (await call(request, user, op))["card"]
    return JSONResponse(card, status_code=201)


def with_actions(found: dict) -> dict:
    """A card for the page with what the person may do next (the backend decides, the page shows) and
    its pipeline facts and next step."""
    return {**found["card"], "actions": found.get("actions", []), "pipeline": found.get("pipeline"),
            "next": found.get("next")}


@router.get("/api/cards/{card_id}")
async def card(
    card_id: str, request: Request, user: deps.User = Depends(deps.current_user)
):
    return with_actions(await call(request, user, {"op": "card.get", "card": card_id}))


@router.patch("/api/cards/{card_id}")
async def edit(card_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """A card's fields; a status only as a manual override with a reason — ``override_reason_code`` and/or
    ``override_reason``, a duplicate's ``override_canonical`` (else ``use_action``)."""
    body = await deps.json_body(request)
    op = {
        "op": "user.card.edit",
        "card": card_id,
        "revision": body.get("revision"),
        "changes": body.get("changes"),
    }
    for key in ("override_reason", "override_reason_code", "override_canonical"):
        if key in body:
            op[key] = body[key]
    return with_actions(await call(request, user, op))


async def _act(card_id: str, request: Request, user: deps.User, action) -> dict:
    body = await deps.json_body(request)
    op = {
        "op": "user.card.act",
        "card": card_id,
        "action": action if action is not None else body.get("action"),
        "revision": body.get("revision"),
    }
    for key in ("comment", "brief_version"):
        if key in body:
            op[key] = body[key]
    return await call(request, user, op)


@router.post("/api/cards/{card_id}/act")
async def act(card_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """One of the card's actions: ``{card, commands}`` — the card as it is now and what its agents were asked."""
    done = await _act(card_id, request, user, None)
    return {"card": with_actions(done), "commands": done["commands"]}


def local_until(value, zone: str) -> str:
    """The page's ``YYYY-MM-DDTHH:MM`` in the person's time zone as a moment with its offset; anything
    else goes on as it came, for the board to refuse."""
    if not isinstance(value, str):
        return value
    try:
        moment = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError:
        return value
    try:
        tz = ZoneInfo(zone)
    except (ValueError, ZoneInfoNotFoundError):
        return value  # no zone the board could read: refused as a moment without one
    return moment.replace(tzinfo=tz).isoformat()


@router.post("/api/cards/{card_id}/defer")
async def defer(card_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The "Put off until…" action: ``{until, reason?}`` — ``until`` the page's local moment in the person's time zone;
    the card as it is now."""
    body = await deps.json_body(request)
    op = {"op": "user.card.defer", "card": card_id, "until": local_until(body.get("until"), user.timezone)}
    if body.get("reason"):
        op["reason"] = body["reason"]
    await call(request, user, op)
    return with_actions(await call(request, user, {"op": "card.get", "card": card_id}))


@router.post("/api/cards/{card_id}/undefer")
async def undefer(card_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The "Wake now" action: the defer ends, the held wakes go at once; the card as it is now."""
    await call(request, user, {"op": "user.card.undefer", "card": card_id})
    return with_actions(await call(request, user, {"op": "card.get", "card": card_id}))


@router.post("/api/cards/{card_id}/start")
async def start(card_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The ``start`` action under its older address: the card."""
    return with_actions(await _act(card_id, request, user, "start"))


@router.post("/api/cards/{card_id}/brief")
async def brief(card_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The ``brief`` action under its older address: the card."""
    return with_actions(await _act(card_id, request, user, "brief"))


@router.get("/api/cards/{card_id}/brief")
async def brief_view(card_id: str, request: Request, user: deps.User = Depends(deps.current_user)):
    """The card's brief: its state, versions with author, date, approval and changes, the text now."""
    found = await call(request, user, {"op": "card.brief", "card": card_id})
    return {key: found[key] for key in ("brief", "versions", "current", "changes")}


@router.get("/api/cards/{card_id}/councils")
async def card_councils(card_id: str, request: Request, user: deps.User = Depends(deps.current_user)):
    """The card's councils, the newest first: each round's critics, their verdicts and findings with the
    writer's answers; ``can_manage`` — this person may cancel an open one."""
    found = await call(request, user, {"op": "card.councils", "card": card_id})
    return {key: found[key] for key in ("councils", "can_manage")}


@router.post("/api/cards/{card_id}/councils/{council_id}/cancel")
async def council_cancel(card_id: str, council_id: str, request: Request, user: deps.User = Depends(deps.writer)):
    """The "Cancel the council" action: the open council ends where it is; the card's councils as they are now."""
    await call(request, user, {"op": "council.cancel", "card": card_id, "council": council_id})
    found = await call(request, user, {"op": "card.councils", "card": card_id})
    return {key: found[key] for key in ("councils", "can_manage")}


@router.get("/api/cards/{card_id}/councils/{council_id}/diff")
async def council_diff(card_id: str, council_id: str, request: Request, user: deps.User = Depends(deps.current_user)):
    """The diff of the pull request a council of the code stands on: the owner reads it in the council's
    block while its decision is held back; 409 ``stale`` once the pull request moved on from that head."""
    found = await call(request, user, {"op": "card.councils", "card": card_id})
    one = next((c for c in found["councils"] if c["id"] == council_id and c["kind"] == "code"), None)
    if one is None:
        raise Failure(404, "not_found", f"{card_id}: no such council of the code")
    basis, service = one["basis"], deps.svc(request)
    row = await service.locked(lambda: service.ctx.store.pr(basis["repo"], basis["number"]))
    if row is None or row["head"] != basis["head"]:
        raise Failure(409, "stale", f"{card_id}: the PR moved on from {str(basis['head'])[:7]}")
    return {"diff": row["diff"], "truncated": row["truncated"], "head": row["head"]}


def page_op(op: dict, before: Optional[int], limit: Optional[int]) -> dict:
    """A read with the page's ``before``/``limit`` when the request gave them (the API checks them)."""
    if before is not None:
        op["before"] = before
    if limit is not None:
        op["limit"] = limit
    return op


def thread_reply(request: Request, found: dict) -> dict:
    return {
        "messages": with_names(deps.svc(request), found["messages"]),
        "watermark": found["watermark"],
        "more": found["more"],
    }


@router.get("/api/cards/{card_id}/result")
async def card_result(card_id: str, request: Request, user: deps.User = Depends(deps.current_user)):
    """The card's result: summary, plan, notes, AC, DoD, links, its Review iterations with who sent and decided."""
    service = deps.svc(request)
    found = (await call(request, user, {"op": "card.result", "card": card_id}))["result"]
    found["reviews"] = [
        {
            **r,
            "by_name": author_name(service, r["by"]),
            "decided_by_name": author_name(service, r["decided_by"]) if r["decided_by"] else "",
        }
        for r in found["reviews"]
    ]
    return found


@router.get("/api/cards/{card_id}/messages")
async def messages(
    card_id: str,
    request: Request,
    user: deps.User = Depends(deps.current_user),
    after: int = 0,
    before: Optional[int] = None,
    limit: Optional[int] = None,
):
    """``{messages, watermark, more}``: the page replays the journal from ``watermark`` after this snapshot;
    ``limit`` (and ``before``) read the newest window of a long thread and its older pages."""
    op = {"op": "thread.messages", "card": card_id, "after": max(0, after)}
    found = await call(request, user, page_op(op, before, limit))
    return thread_reply(request, found)


@router.post("/api/cards/{card_id}/messages")
async def comment(
    card_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    body = await deps.json_body(request)
    message = (
        await call(
            request,
            user,
            {
                "op": "user.card.comment",
                "card": card_id,
                "text": body.get("body"),
                "client_key": body.get("client_key", ""),
            },
        )
    )["message"]
    return with_names(deps.svc(request), [message])[0]


@router.get("/api/cards/{card_id}/events")
async def events(
    card_id: str,
    request: Request,
    user: deps.User = Depends(deps.current_user),
    before: Optional[int] = None,
    limit: Optional[int] = None,
):
    """The card's history: with ``limit``/``before`` a page ``{events, next}`` newest first, else the whole list."""
    op = page_op({"op": "card.events", "card": card_id}, before, limit)
    found = await call(request, user, op)
    return found if "next" in found else found["events"]


@router.get("/api/cards/{card_id}/decisions")
async def card_decisions(
    card_id: str, request: Request, user: deps.User = Depends(deps.current_user)
):
    # 404 for a card the person may not read
    await call(request, user, {"op": "card.get", "card": card_id})
    found = await call(request, user, {"op": "decision.list", "card": card_id})
    return found["decisions"]


@router.get("/api/cards/{card_id}/sessions")
async def card_sessions(
    card_id: str, request: Request, user: deps.User = Depends(deps.current_user)
):
    """The agents' sessions in the card's thread, newest first."""
    return (await call(request, user, {"op": "session.list", "card": card_id}))["sessions"]


@router.get("/api/cards/{card_id}/jobs")
async def card_jobs(
    card_id: str, request: Request, user: deps.User = Depends(deps.current_user)
):
    """The card's worker jobs, newest first: what the cabinet shows of each, never its definition."""
    return (await call(request, user, {"op": "job.list", "card": card_id}))["jobs"]


@router.get("/api/cards/{card_id}/commands")
async def card_commands(
    card_id: str, request: Request, user: deps.User = Depends(deps.current_user)
):
    """What was asked of the agents on the card and how far each request got."""
    return (await call(request, user, {"op": "command.list", "card": card_id}))["commands"]


@router.post("/api/sessions/{session_id}/continue")
async def session_continue(
    session_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    op = {"op": "user.session.continue", "session": session_id}
    return (await call(request, user, op))["command"]


@router.post("/api/sessions/{session_id}/stop")
async def session_stop(
    session_id: str, request: Request, user: deps.User = Depends(deps.writer)
):
    op = {"op": "user.session.stop", "session": session_id}
    return (await call(request, user, op))["command"]


@router.get("/api/sessions/{session_id}/turns")
async def session_turns(session_id: str, request: Request, user: deps.User = Depends(deps.current_user)):
    """the session's turns with their steps — for whoever controls the agent."""
    return (await call(request, user, {"op": "session.turns", "session": session_id}))["turns"]


@router.get("/api/sessions/{session_id}/output/{seq}")
async def session_output(session_id: str, seq: int, request: Request, user: deps.User = Depends(deps.current_user)):
    """A tool's masked output in the session's thread."""
    return (await call(request, user, {"op": "session.output", "session": session_id, "seq": seq}))["output"]


@router.get("/api/inbox")
async def inbox(request: Request, user: deps.User = Depends(deps.current_user)):
    """What waits for the person: decisions they may take, agents' questions, cards in Review, crit incidents and
    failed deploys (with ``deploys.read``)."""
    service = deps.svc(request)
    open_ = (await call(request, user, {"op": "decision.list", "state": "open"}))[
        "decisions"
    ]
    review = (await call(request, user, {"op": "card.list", "status": "Review"}))[
        "cards"
    ]
    # a card put off until a time keeps its questions, decisions and Review out of the inbox till then
    deferred = await service.locked(lambda: defers.active_map(service.ctx.store))
    mine = [
        d
        for d in open_
        if service.users.can(user.id, f"decide.{d['type']}", d["project"] or None) and d.get("card") not in deferred
    ]
    # a chain plan's stage its coordinator still decides on is not the person's review yet
    hidden = await service.locked(lambda: stage_gate.hidden_ids(service.ctx.store))
    review = [c for c in review if c["id"] not in deferred and c["id"] not in hidden]
    alarms = await service.locked(
        lambda: incidents.collapse(incidents.alarms(service.ctx, deps.principal(user)))
    )
    failed = (
        await service.locked(lambda: deploys.alerts(service.ctx.store))
        if service.users.can(user.id, "deploys.read")
        else []
    )
    return {
        "decisions": [d for d in mine if d["type"] != "question"],
        "questions": [d for d in mine if d["type"] == "question"],
        "review": review,
        "incidents": alarms,  # open crit nobody acknowledged or muted
        "deploys": failed,  # failed runs that ended cards' deploy waits, nobody saw yet
    }
