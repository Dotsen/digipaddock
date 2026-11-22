"""Dev's pull requests on the board.

The cabinet has no network: the forge executor (its own principal, ``forge``) reads a PR, its files and its diff
from Forgejo and publishes them here (``pr.publish``); ``kd-merge`` reports the merge (``pr.state``), and so does the
cabinet when kd-review finds the PR merged or closed in Forgejo by hand. The board
keeps the latest of each PR and one ``pr_approve`` decision per head: the same head again is the same decision,
a new head supersedes the open one (as a new push dismisses an approval in Forgejo), a PR merged or closed ends
it. A head the card's council of the code reads (``council_rounds.on_pull_head``) has no decision until
that council is over: holding the decision back is all a council does to a PR — it approves nothing.
``signer.keys`` gives root the signing passkeys a person has, for kd-review's own registry of signers.
"""

import base64
import re
from typing import Mapping, Optional

from kd.cabinet import api, store

REPO = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
PROJECT = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
AGENT = re.compile(r"@[a-z][a-z0-9-]{0,31}")
HEAD = re.compile(r"[0-9a-f]{40}")
STATES = ("open", "merged", "closed")
ENDED = ("merged", "closed")
FILE_STATUS = re.compile(r"[a-z]{1,20}")
FILES_MAX = 1000
PATH_MAX = 500
TITLE_MAX = 300
URL_MAX = 500
DIFF_MAX = 384 * 1024  # bytes of UTF-8: with the files a publish stays well under the socket's 1 MiB line
COUNT_MAX = 10 ** 7
TYPE = "pr_approve"
# the PR's target branch (the passport's, as dev_agent.repos checks it), what a merge into it sets off and
# the
# required checks green on its head — optional for a publish of the previous code: main, nothing, none
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
PROFILE = re.compile(r"[a-z][a-z0-9-]{0,31}")
EFFECT_MAX = 300
CHECKS_MAX = 20
PASSPORT = {"target": "main", "effect": "", "checks": []}


def _invalid(message):
    return api.ApiError("invalid", message)


def _count(value, name) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= COUNT_MAX:
        raise _invalid(f"{name} must be a whole number")
    return value


def _files(value) -> list:
    if not isinstance(value, list) or len(value) > FILES_MAX:
        raise _invalid(f"files is a list of at most {FILES_MAX}")
    out = []
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"path", "status", "additions", "deletions"}:
            raise _invalid("a file is {path, status, additions, deletions}")
        path = item["path"]
        if not isinstance(path, str) or not path or len(path) > PATH_MAX or "\n" in path:
            raise _invalid(f"a file path is one line of 1 to {PATH_MAX} characters")
        if not isinstance(item["status"], str) or not FILE_STATUS.fullmatch(item["status"]):
            raise _invalid("a file status is a lowercase word")
        out.append({"path": path, "status": item["status"], "additions": _count(item["additions"], "additions"),
                    "deletions": _count(item["deletions"], "deletions")})
    return out


def _text(value, name, limit) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise _invalid(f"{name} must be text of at most {limit} characters")
    return value


def validate(pr) -> dict:
    """The PR as the board keeps it, or ``invalid``."""
    keys = {"repo", "project", "number", "card", "title", "url", "head", "state", "files", "cage", "diff",
            "truncated", "release"}
    if not isinstance(pr, Mapping) or not keys <= set(pr) <= keys | set(PASSPORT):
        raise _invalid(f"pr must have exactly {', '.join(sorted(keys))} (and optionally {', '.join(PASSPORT)})")
    pr = {**PASSPORT, **dict(pr)}
    target = pr["target"]
    if (not isinstance(target, str) or not BRANCH.fullmatch(target) or ".." in target
            or target.endswith(".lock")):
        raise _invalid("target must be a branch name")
    _text(pr["effect"], "effect", EFFECT_MAX)
    checks = pr["checks"]
    if (not isinstance(checks, list) or len(checks) > CHECKS_MAX or len(set(map(str, checks))) != len(checks)
            or not all(isinstance(c, str) and PROFILE.fullmatch(c) for c in checks)):
        raise _invalid(f"checks must be at most {CHECKS_MAX} different profile names")
    if not isinstance(pr["repo"], str) or not REPO.fullmatch(pr["repo"]):
        raise _invalid("repo must be owner/name")
    if not isinstance(pr["project"], str) or not PROJECT.fullmatch(pr["project"]):
        raise _invalid("project must be a project name")
    number = pr["number"]
    if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= COUNT_MAX:
        raise _invalid("number must be a PR number")
    if not isinstance(pr["card"], str) or not api.CARD.fullmatch(pr["card"]):
        raise _invalid("card must be KD-<n>")
    if not isinstance(pr["head"], str) or not HEAD.fullmatch(pr["head"]):
        raise _invalid("head must be 40 lowercase hex characters")
    if pr["state"] not in STATES:
        raise _invalid(f"state must be one of {', '.join(STATES)}")
    url = _text(pr["url"], "url", URL_MAX)
    if not url.startswith("https://") or any(c.isspace() for c in url):
        raise _invalid("url must be https")
    files = _files(pr["files"])
    paths = {f["path"] for f in files}
    cage = pr["cage"]
    if not isinstance(cage, list) or not all(isinstance(p, str) and p in paths for p in cage):
        raise _invalid("cage must list paths among the files")
    diff = _text(pr["diff"], "diff", DIFF_MAX)
    if len(diff.encode("utf-8", "surrogatepass")) > DIFF_MAX:
        raise _invalid(f"diff must be at most {DIFF_MAX} bytes")
    for name in ("truncated", "release"):
        if not isinstance(pr[name], bool):
            raise _invalid(f"{name} must be true or false")
    return {**pr, "title": _text(pr["title"], "title", TITLE_MAX), "url": url, "files": files,
            "cage": list(dict.fromkeys(cage)), "diff": diff, "checks": list(checks)}


def payload_of(pr: Mapping) -> dict:
    """What the owner signs (``kd.pr_signature``): the PR at its head into its target, and what the merge sets off.
    A stored row keeps no passport (it lives in the decision) and gets the defaults."""
    return {"kind": TYPE, "repo": pr["repo"], "project": pr["project"], "number": pr["number"], "head": pr["head"],
            "url": pr["url"], "title": pr["title"], "release": pr["release"], "target": pr.get("target", "main"),
            "effect": pr.get("effect", ""), "checks": list(pr.get("checks", ()))}


def pull_view(db, payload: Mapping) -> dict:
    """The PR under ``payload`` — what a decision of its head carries — as a reader sees it: the passport, and the
    files, the cage and the truncation only while the PR is on that head; ``stale`` — it moved on or ended."""
    row = db.pr(payload["repo"], payload["number"], diff=False)
    same = row is not None and row["head"] == payload["head"]
    return {
        **{k: payload[k] for k in ("repo", "number", "title", "url", "head", "release")},
        # the branch, what a merge into it sets off, the checks green on the head (none before the
        # passport)
        "target": payload.get("target", ""), "effect": payload.get("effect", ""), "checks": payload.get("checks", []),
        "state": row["state"] if row else "",
        "files": row["files"] if same else [],
        "cage": row["cage"] if same else [],
        "truncated": row["truncated"] if same else False,
        "stale": not same or row["state"] != "open",
    }


def plan_pulls(db, card_id: str) -> int:
    """How many pull requests a chain plan's card has open — the repositories its end still merges; 0 for a card
    that is no chain plan. With more than one, a release signed on any of them is one rollout after the last merge."""
    from kd.cabinet import stages  # at call time: it loads the API

    return len(db.prs(card=card_id, state="open")) if stages.is_chain(db, card_id) else 0


def _same(payload: Mapping, pr: Mapping) -> bool:
    """The decision is of this head into this target, with this effect, these checks and this release flag (a decision
    of the previous code has no target: it is not kept)."""
    return all(payload.get(k) == pr[k] for k in ("head", "release", *PASSPORT))


def decisions_of(ctx, card: str, repo: str, number: int, state: Optional[str] = None) -> list:
    """The ``pr_approve`` decisions of one PR on its card."""
    return [d for d in api._ledger(ctx).list(state=state, card=card)
            if d["type"] == TYPE and d["payload"].get("repo") == repo and d["payload"].get("number") == number]


def _expire(ctx, found: list) -> None:
    ledger = api._ledger(ctx)
    for d in found:
        api._decision_call(lambda: ledger.expire_one(d["id"], now=ctx.now()))


def _kept(mine: list, payload: Mapping) -> list:
    """The decisions of this very head that stand: open or approved."""
    return [d for d in mine if d["state"] in ("open", "approved") and _same(d["payload"], payload)]


def _open(ctx, card_id: str, mine: list, held: Mapping) -> dict:
    """The decision of ``held`` (``{payload, requested_by}``): the one that stands on this head, else a new one —
    the open ones of other heads expire."""
    kept = _kept(mine, held["payload"])
    if kept:
        return kept[-1]
    _expire(ctx, [d for d in mine if d["state"] == "open"])
    return api._decision_call(lambda: api._ledger(ctx).open(
        TYPE, card=card_id, payload=dict(held["payload"]), requested_by=held["requested_by"], now=ctx.now()))


def _council_brief(found: Optional[Mapping]) -> Optional[dict]:
    """The council of the PR's head as a publish answers with it: which, its round and its state."""
    if found is None:
        return None
    return {"id": found["id"], "round": found["round"], "rounds": found["rounds_max"], "state": found["state"]}


def council_ended(ctx, found: Mapping) -> Optional[dict]:
    """The code council of a PR is over (``council_rounds._end``): the decision it held back opens — on the head
    the council stood on, with the passport of that publish, while the PR is still open on that head and on that
    card. None when there is nothing to open: the PR moved on, ended or went to another card — its next publish
    decides."""
    basis = found["basis"]
    held = basis.get("held") or {}
    row = ctx.store.pr(basis["repo"], basis["number"], diff=False)
    if (not held or row is None or row["state"] != "open" or row["head"] != basis["head"]
            or row["card"] != found["card"]):
        return None
    return _open(ctx, found["card"], decisions_of(ctx, found["card"], basis["repo"], basis["number"]), held)


def _publish(ctx, principal, request):
    from kd.cabinet import (  # at call time: they load the jobs, which load the API
        council_rounds,
        councils,
        stage_gate,
        stages,
    )

    pr = validate(request.get("pr"))
    try:
        card = ctx.store.card(pr["card"])
    except store.NotFound:
        raise api.ApiError("not_found", f"{pr['card']} not found")
    agent = card.assignee[1:] if AGENT.fullmatch(card.assignee or "") and card.assignee != api.OWNER else ""
    if not agent and pr["state"] not in ENDED:  # a PR that ended is recorded whoever holds the card now
        raise api.ApiError("refused", f"{card.id} has no agent assignee to ask for the approval")
    before = ctx.store.pr(pr["repo"], pr["number"], diff=False)
    with ctx.store.transaction():
        row = ctx.store.pr_put(pr, actor="forge")
        if before is not None and before["card"] != card.id:  # the title names another card now
            _expire(ctx, decisions_of(ctx, before["card"], pr["repo"], pr["number"], state="open"))
            council_rounds.on_pull_ended(ctx, before["card"], pr["repo"], pr["number"])
        mine = decisions_of(ctx, card.id, pr["repo"], pr["number"])
        if pr["state"] in ENDED:
            _expire(ctx, [d for d in mine if d["state"] == "open"])
            council_rounds.on_pull_ended(ctx, card.id, pr["repo"], pr["number"])
            return {"pr": row, "decision": None, "council": None}
        if stages.is_chain(ctx.store, card.id) and stages.open_children(ctx.store, card.id):
            # a chain plan's pull request grows stage by stage: its head is recorded for the stage's reviewer,
            # and the council and the owner's decision wait for the publish after the last stage
            _expire(ctx, [d for d in mine if d["state"] == "open"])
            stage_gate.published(ctx.store, pr)
            return {"pr": row, "decision": None, "council": None, "stage": True}
        held = {"payload": payload_of(pr), "requested_by": f"agent:{agent}"}
        kept = _kept(mine, held["payload"])
        if kept:  # this head is the owner's already: a council gathers from the next head on
            read = councils.for_head(ctx.store, card.id, pr["repo"], pr["number"], pr["head"])
            return {"pr": row, "decision": kept[-1], "council": _council_brief(read)}
        found = council_rounds.on_pull_head(ctx, ctx.store.card(card.id), pr, held)
        if found is not None and found["state"] in councils.OPEN:
            # the critics read this head: its decision opens when they are done (``council_ended``)
            _expire(ctx, [d for d in mine if d["state"] == "open"])
            return {"pr": row, "decision": None, "council": _council_brief(found)}
        opened = _open(ctx, card.id, decisions_of(ctx, card.id, pr["repo"], pr["number"]), held)
    return {"pr": row, "decision": opened, "council": _council_brief(found)}


def _state(ctx, principal, request):
    repo, number, state = request.get("repo"), request.get("number"), request.get("state")
    if not isinstance(repo, str) or not REPO.fullmatch(repo) or isinstance(number, bool) or not isinstance(number, int):
        raise _invalid("repo and number name the PR")
    if state not in ENDED:
        raise _invalid(f"state must be one of {', '.join(ENDED)}")
    found = ctx.store.pr(repo, number)
    if found is None:
        raise api.ApiError("not_found", f"no PR {repo}#{number} on the board")
    from kd.cabinet import council_rounds  # at call time: it loads the jobs, which load the API

    with ctx.store.transaction():
        row = ctx.store.pr_put({**found, "state": state}, actor="forge")
        _expire(ctx, decisions_of(ctx, found["card"], repo, number, state="open"))
        council_rounds.on_pull_ended(ctx, found["card"], repo, number)  # its council has nothing left to read
    return {"pr": row}


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")


def _signer_keys(ctx, principal, request):
    user = request.get("user")
    if ctx.users is None:
        raise api.ApiError("unavailable", "people are not served here")
    if not isinstance(user, str) or ctx.users.get(user) is None:
        raise api.ApiError("not_found", "no such user")
    return {"keys": [{"credential_id": b64url(k["credential_id"]), "public_key": b64url(k["public_key"]),
                      "name": k["name"], "created": k["created"]} for k in ctx.users.signing_keys(user)]}


OPS = {
    "pr.publish": (frozenset({"forge"}), _publish),
    "pr.state": (frozenset({"forge", "cabinet"}), _state),  # cabinet: kd-review saw it end in Forgejo
    "signer.keys": (frozenset({"root"}), _signer_keys),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)

