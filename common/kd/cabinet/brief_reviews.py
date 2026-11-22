"""Version-bound independent brief reviews, on a separate card/session."""

import json
import re

from kd.cabinet import api, briefs

SHA = re.compile(r"[0-9a-f]{40}")


def review_scope(target):
    """Review applicability includes routing and dependencies, unlike brief approval."""
    return {
        **briefs.scope(target),
        "dependencies": sorted(target.dependencies),
        "selection": {
            "assignee": target.assignee,
            "labels": sorted(
                label
                for label in target.labels
                if label.startswith(("spec:", "skill:"))
            ),
        },
    }


def handle(ctx, principal, request):
    agent = api._executor_agent(ctx, principal, request, coordinator=False)
    review = api._card(ctx, request.get("card"))
    if (
        agent != "analyst"
        or review.assignee != "@analyst"
        or "spec:brief-review" not in review.labels
        or review.status not in ("In Progress", "Review")
    ):
        raise api.ApiError(
            "refused", "brief review needs a separate claimed analyst brief-review card"
        )
    key = "brief-review:" + review.id
    value = ctx.store.get_state(key) or {"rounds": []}
    action = request.get("action")
    if action == "prepare":
        target = api._card(ctx, request.get("target"))
        if target.id == review.id or target.thread == review.thread:
            raise api.ApiError(
                "refused", "review must use a separate card and session from its author"
            )
        commit = request.get("commit")
        if not isinstance(commit, str) or not SHA.fullmatch(commit):
            raise api.ApiError(
                "invalid", "observed repository commit must be a full lowercase SHA"
            )
        if value.get("target") not in (None, target.id):
            raise api.ApiError("refused", "a review card cannot switch its target")
        scope = review_scope(target)
        basis = dict(
            card=target.id,
            revision=target.revision,
            scope_sha=briefs.scope_sha(scope),
            commit=commit,
        )
        latest = briefs.latest(ctx.store, target.id)
        author = latest["author"] if latest else None
        packet = dict(
            basis=basis,
            title=target.title,
            scope=scope,
            dependencies=list(target.dependencies),
            # one analyst writes and reviews briefs (review I4): the round says when it is not independent
            author=author,
            same_role=author == "agent:" + agent,
        )
        value.update(target=target.id, packet=packet)
        ctx.store.set_state(key, value)
        return dict(packet=packet)
    if action == "record":
        if not value.get("packet"):
            raise api.ApiError("refused", "prepare a review basis first")
        report = request.get("report")
        if not isinstance(report, dict) or set(report) != {
            "verdict",
            "basis",
            "findings",
            "missing_inputs",
        }:
            raise api.ApiError(
                "invalid",
                "report must contain verdict, basis, findings, missing_inputs",
            )
        if report["basis"] != value["packet"]["basis"]:
            raise api.ApiError(
                "conflict", "report basis differs from the prepared input"
            )
        target = api._card(ctx, value["target"])
        if briefs.scope_sha(review_scope(target)) != report["basis"]["scope_sha"]:
            raise api.ApiError(
                "conflict", "target scope changed; prepare and review the new version"
            )
        findings, missing, verdict = (
            report["findings"],
            report["missing_inputs"],
            report["verdict"],
        )
        if (
            not isinstance(findings, list)
            or not isinstance(missing, list)
            or any(not isinstance(x, str) or not x.strip() for x in missing)
        ):
            raise api.ApiError("invalid", "findings and missing_inputs must be lists")
        for finding in findings:
            if (
                not isinstance(finding, dict)
                or set(finding)
                != {"severity", "location", "evidence", "scenario", "recommendation"}
                or finding["severity"] not in ("critical", "major", "minor")
                or any(
                    not isinstance(v, str) or not v.strip() for v in finding.values()
                )
            ):
                raise api.ApiError(
                    "invalid",
                    "each finding needs severity, location, evidence, scenario, recommendation",
                )
        # a style remark (minor) never blocks a ready brief, nor alone makes it need changes (review M1)
        blocking = [f for f in findings if f["severity"] != "minor"]
        if (
            verdict not in ("ready", "needs_changes", "blocked")
            or (verdict == "ready" and (blocking or missing))
            or (verdict == "needs_changes" and not blocking)
            or (verdict == "blocked" and not missing)
        ):
            raise api.ApiError(
                "invalid", "verdict is inconsistent with findings or missing inputs"
            )
        value["rounds"].append(report)
        value.setdefault("same_role", {})[str(len(value["rounds"]) - 1)] = value["packet"].get("same_role", False)
        with ctx.store.transaction():
            ctx.store.set_state(key, value)
            ctx.store.edit_card(
                review.id,
                review.revision,
                {
                    "notes_append": "[brief-review] "
                    + json.dumps({**report, "same_role": value["packet"].get("same_role", False)},
                                 ensure_ascii=False)
                },
                actor="agent:analyst",
            )
    elif action != "status":
        raise api.ApiError("invalid", "action must be prepare, record or status")
    target = api._card(ctx, value["target"]) if value.get("target") else None
    current = briefs.scope_sha(review_scope(target)) if target is not None else None
    commit = value.get("packet", {}).get("basis", {}).get("commit")
    return {
        "rounds": [
            dict(
                report=r,
                current=r["basis"]["scope_sha"] == current
                and r["basis"]["commit"] == commit,
                same_role=value.get("same_role", {}).get(str(n), False),
            )
            for n, r in enumerate(value["rounds"])
        ],
        "packet": value.get("packet"),
    }


api.OPS["brief-review"] = (frozenset({"executor"}), handle)
