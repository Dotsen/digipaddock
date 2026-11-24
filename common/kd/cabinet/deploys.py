"""Deploys: the projects' environments and their runs, for the «Deploys» page and the cards' ``deploy`` wait.

The cabinet has no network and holds no Forgejo token: the Forgejo executor's reader (``dev_agent.deploys``, a
timer under the forge user) reads the Actions runs of every environment the projects' passports name
(``repo.deploys`` of ``host/projects.json``) and reports them here at least once a minute.

- ``deploy.report {sources: [{project, repo, envs, runs, error}]}`` — the forge only: the environments of every
  project with deploys (a project the report leaves out is no longer shown), the runs it read (one row per run,
  updated while it runs) and what failed reading it. Returns the runs that changed (``changed``).
- ``deploy.list`` — the page's top: each environment's last run, ``stale`` when its project was last read
  successfully more than ``STALE_S`` seconds ago, and the platform's own last release (``platform``).
- ``deploy.runs {project?, env?, before?, limit?}`` — the journal: runs newest first, the platform's releases
  among them (project ``platform``, env ``release``).
- ``deploy.watch`` — the forge only: the commits the cards' open ``deploy`` waits name, with the runs whose
  ancestry the reader already reported; the reader asks Forgejo about the rest and reports ``includes``.
- ``deploy.alerts`` / ``deploy.alert.seen {project, run}`` — failed runs that ended a card's wait, in the inbox
  of people with ``deploys.read`` until one of them saw it.

- ``deploy.launchable {project, env}`` — the owner only (a person holding every permission): the manual environment
  the button «Deploy to prod» starts (KD-9447); ``deploy.launched {launch}`` — the cabinet's web records a launch
  kd-review dispatched or refused (a ``deploy.launched`` event, shown under its environment as ``launched``).

People read with ``deploys.read`` (the owner holds every permission); root and the cabinet's own CLI read too.

The ``deploy`` wait (``kd-work finish … --outcome wait_dependency --deploy <project>:<env>[@<sha>]``): its ref is
``<project>:<env>@<sha40>``, the commit by default the head of the card's last merged pull request in the project —
it merges with a merge commit, so every later commit of the branch holds it. The newest finished run of the
environment that holds the commit decides: ``success`` resolves the wait, ``failure`` ends it with the run's
number and log, posts a notice in the card's thread and an alert in the inbox; a cancelled or skipped run decides
nothing. A run holds the commit when it is its own commit or the reader found it among the run's commit's ancestors.
"""

import json
import re
from typing import Dict, List, Optional

from kd.cabinet import api, releases, users

STATUSES = (
    "unknown",
    "waiting",
    "running",
    "success",
    "failure",
    "cancelled",
    "skipped",
    "blocked",
)
FINISHED = ("success", "failure", "cancelled", "skipped")
STALE_S = 120
PLATFORM, RELEASE_ENV = "platform", "release"
SOURCES_MAX, ENVS_MAX, RUNS_MAX = 32, 8, 100
JOURNAL_DEFAULT, JOURNAL_MAX = 50, 200
INPUTS_MAX, INPUT_MAX = 8, 200
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
REPO = re.compile(r"[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,100}")
WORKFLOW = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.ya?ml")
BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
SHA = re.compile(r"[0-9a-f]{0,40}")
SHA40 = re.compile(r"[0-9a-f]{40}")
TIME = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")
# a wait's target as kd-work names it, and its recorded ref
TARGET = re.compile(r"([a-z][a-z0-9-]{0,31}):([a-z][a-z0-9-]{0,31})(?:@([0-9a-f]{40}))?")
REF = re.compile(r"([a-z][a-z0-9-]{0,31}):([a-z][a-z0-9-]{0,31})@([0-9a-f]{40})")
DECIDING = ("success", "failure")
INCLUDES_MAX = 100
ALERTS_MAX = 50
INPUT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
TEXT = {"event": 40, "ref": 200, "actor": 100, "title": 200, "error": 500}
URL_MAX = 500
# the platform's releases: the cards' release notices, by their own index (schema v47 repeats this condition)
RELEASE_KEYS = (
    "notice.forge.released",
    "notice.release.healthy",
    "notice.release.degraded",
    "notice.forge.release_degraded",
    "notice.forge.release_refused",
)
RELEASES = (
    "SELECT seq, ts, card, payload FROM events INDEXED BY events_release_notice WHERE kind = 'notice'"
    " AND json_extract(payload, '$.key') IN ('notice.forge.released', 'notice.release.healthy',"
    " 'notice.release.degraded', 'notice.forge.release_degraded', 'notice.forge.release_refused')"
    " ORDER BY seq DESC LIMIT ?"
)
RELEASE_SCAN = 1000  # notices read for the journal's releases: a release is 2–3 of them
RELEASE_STATUS = {
    "code_switched": "running",
    "activating": "running",
    "healthy": "success",
    "degraded": "failure",
    "refused": "failure",
}


def _invalid(message):
    return api.ApiError("invalid", message)


def _text(value, name: str, limit: int) -> str:
    if (
        not isinstance(value, str)
        or len(value) > limit
        or any(ord(c) < 32 for c in value)
    ):
        raise _invalid(f"{name} is text of at most {limit} characters")
    return value


def _envs(project: str, value) -> List[dict]:
    if not isinstance(value, list) or len(value) > ENVS_MAX:
        raise _invalid(f"{project}: envs is a list of at most {ENVS_MAX}")
    out, names, files = [], set(), set()
    for item in value:
        # confirm and skip_tests: a manual environment's inputs to the owner's button (a reader of before KD-9447
        # sends neither)
        if not isinstance(item, dict) or not {"env", "workflow", "branch", "manual"} <= set(item) <= {
            "env",
            "workflow",
            "branch",
            "manual",
            "confirm",
            "skip_tests",
        }:
            raise _invalid(f"{project}: an env is {{env, workflow, branch, manual[, confirm, skip_tests]}}")
        env, workflow, branch, manual = (
            item["env"],
            item["workflow"],
            item["branch"],
            item["manual"],
        )
        confirm, skip_tests = item.get("confirm", ""), item.get("skip_tests", False)
        if (
            not isinstance(confirm, str)
            or (confirm and not INPUT.fullmatch(confirm))
            or not isinstance(skip_tests, bool)
        ):
            raise _invalid(f"{project}: {env!r}: confirm is an input name, skip_tests true or false")
        if not isinstance(env, str) or not NAME.fullmatch(env) or env in names:
            raise _invalid(f"{project}: env names are distinct [a-z][a-z0-9-]*")
        if (
            not isinstance(workflow, str)
            or not WORKFLOW.fullmatch(workflow)
            or workflow in files
        ):
            raise _invalid(f"{project}: {env}: workflow is a distinct .yml file name")
        if (
            not isinstance(branch, str)
            or not BRANCH.fullmatch(branch)
            or not isinstance(manual, bool)
        ):
            raise _invalid(
                f"{project}: {env}: branch is a branch name, manual true or false"
            )
        names.add(env)
        files.add(workflow)
        out.append(
            {"env": env, "workflow": workflow, "branch": branch, "manual": manual,
             "confirm": confirm if manual else "", "skip_tests": skip_tests and manual}
        )
    return out


def _time(value, name: str, empty: bool = False) -> str:
    if (empty and value == "") or (isinstance(value, str) and TIME.fullmatch(value)):
        return value
    raise _invalid(f"{name} is a UTC time YYYY-MM-DDTHH:MM:SSZ")


def _inputs(value) -> dict:
    if not isinstance(value, dict) or len(value) > INPUTS_MAX:
        raise _invalid(f"inputs is an object of at most {INPUTS_MAX} entries")
    out = {}
    for name, item in value.items():
        if not isinstance(name, str) or not INPUT.fullmatch(name):
            raise _invalid("an input name is a workflow input's name")
        out[name] = _text(item, f"input {name}", INPUT_MAX)
    return out


def _run(project: str, envs: Dict[str, dict], value) -> dict:
    if not isinstance(value, dict):
        raise _invalid(f"{project}: a run is an object")
    run, number, env = value.get("run"), value.get("number", 0), value.get("env")
    if isinstance(run, bool) or not isinstance(run, int) or run < 1:
        raise _invalid(f"{project}: run is the Actions run's id")
    if isinstance(number, bool) or not isinstance(number, int) or number < 0:
        raise _invalid(f"{project}: number is the run's number in the repository")
    if env not in envs:
        raise _invalid(f"{project}: run {run}: env is one of the reported envs")
    status = value.get("status")
    if status not in STATUSES:
        raise _invalid(f"{project}: run {run}: status is one of {', '.join(STATUSES)}")
    sha = value.get("sha", "")
    if not isinstance(sha, str) or not SHA.fullmatch(sha):
        raise _invalid(f"{project}: run {run}: sha is a commit hash")
    url = value.get("url", "")
    if (
        not isinstance(url, str)
        or len(url) > URL_MAX
        or (url and not url.startswith("https://"))
    ):
        raise _invalid(f"{project}: run {run}: url is an https address")
    out = {
        "run": run,
        "number": number,
        "env": env,
        "workflow": envs[env]["workflow"],
        "status": status,
        "sha": sha,
        "url": _text(url, "url", URL_MAX),
        "inputs": _inputs(value.get("inputs", {})),
        "created": _time(value.get("created"), "created"),
        "started": _time(value.get("started", ""), "started", empty=True),
        "stopped": _time(value.get("stopped", ""), "stopped", empty=True),
    }
    for name in ("event", "ref", "actor", "title"):
        out[name] = _text(value.get(name, ""), name, TEXT[name])
    return out


def _sources(value) -> List[dict]:
    if not isinstance(value, list) or len(value) > SOURCES_MAX:
        raise _invalid(f"sources is a list of at most {SOURCES_MAX}")
    out, seen = [], set()
    for item in value:
        if not isinstance(item, dict):
            raise _invalid("a source is {project, repo, envs, runs, error}")
        project, repo = item.get("project"), item.get("repo")
        if (
            not isinstance(project, str)
            or not NAME.fullmatch(project)
            or project in seen
            or project == PLATFORM
        ):
            raise _invalid("project is a distinct project id")
        if not isinstance(repo, str) or not REPO.fullmatch(repo):
            raise _invalid(f"{project}: repo is owner/name")
        envs = _envs(project, item.get("envs"))
        by_env = {e["env"]: e for e in envs}
        runs = item.get("runs", [])
        if not isinstance(runs, list) or len(runs) > RUNS_MAX:
            raise _invalid(f"{project}: runs is a list of at most {RUNS_MAX}")
        seen.add(project)
        out.append(
            {
                "project": project,
                "repo": repo,
                "envs": envs,
                "runs": [_run(project, by_env, r) for r in runs],
                "includes": _includes(project, item.get("includes", [])),
                "error": _text(item.get("error", ""), "error", TEXT["error"]),
            }
        )
    return out


def _includes(project: str, value) -> List[dict]:
    """What the reader found: whether run ``run``'s commit holds commit ``sha``."""
    if not isinstance(value, list) or len(value) > INCLUDES_MAX:
        raise _invalid(f"{project}: includes is a list of at most {INCLUDES_MAX}")
    out = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"run", "sha", "included"}:
            raise _invalid(f"{project}: an include is {{run, sha, included}}")
        run, sha, included = item["run"], item["sha"], item["included"]
        if isinstance(run, bool) or not isinstance(run, int) or run < 1:
            raise _invalid(f"{project}: include: run is the Actions run's id")
        if not isinstance(sha, str) or not SHA40.fullmatch(sha) or not isinstance(included, bool):
            raise _invalid(f"{project}: include: sha is a full commit hash, included true or false")
        out.append({"run": run, "sha": sha, "included": included})
    return out


COLUMNS = (
    "run",
    "env",
    "number",
    "workflow",
    "status",
    "event",
    "sha",
    "ref",
    "url",
    "actor",
    "inputs",
    "title",
    "created",
    "started",
    "stopped",
)


def apply_report(s, sources: List[dict], now: str) -> List[dict]:
    """The reader's report, in one write: the sources replaced, the runs upserted. Returns the runs whose status
    changed (a new run counts) — ``{project, env, run, sha, status, before}``, ``before`` '' for a new one."""
    changed = []
    with s.transaction():
        conn = s._conn
        known = [r[0] for r in conn.execute("SELECT project FROM deploy_sources")]
        listed = {src["project"] for src in sources}
        for project in known:
            if project not in listed:
                conn.execute("DELETE FROM deploy_sources WHERE project=?", (project,))
        for src in sources:
            project = src["project"]
            old = conn.execute(
                "SELECT ok_at FROM deploy_sources WHERE project=?", (project,)
            ).fetchone()
            ok_at = now if not src["error"] else (old["ok_at"] if old else "")
            conn.execute(
                "INSERT INTO deploy_sources (project, repo, envs, read_at, ok_at, error) VALUES (?,?,?,?,?,?)"
                " ON CONFLICT(project) DO UPDATE SET repo=excluded.repo, envs=excluded.envs,"
                " read_at=excluded.read_at, ok_at=excluded.ok_at, error=excluded.error",
                (
                    project,
                    src["repo"],
                    json.dumps(src["envs"], ensure_ascii=False),
                    now,
                    ok_at,
                    src["error"],
                ),
            )
            for run in src["runs"]:
                row = conn.execute(
                    "SELECT status FROM deploy_runs WHERE project=? AND run=?",
                    (project, run["run"]),
                ).fetchone()
                values = [
                    json.dumps(run[c], ensure_ascii=False, sort_keys=True)
                    if c == "inputs"
                    else run[c]
                    for c in COLUMNS
                ]
                conn.execute(
                    f"INSERT INTO deploy_runs (project, {', '.join(COLUMNS)}, seen) VALUES (?, "
                    + ", ".join("?" for _ in COLUMNS)
                    + ", ?) ON CONFLICT(project, run) DO UPDATE SET "
                    + ", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "run")
                    + ", seen=excluded.seen",
                    [project, *values, now],
                )
                before = row["status"] if row else ""
                if before != run["status"]:
                    changed.append(
                        {
                            "project": project,
                            "env": run["env"],
                            "run": run["run"],
                            "sha": run["sha"],
                            "status": run["status"],
                            "before": before,
                        }
                    )
            for found in src.get("includes", []):
                conn.execute(
                    "INSERT INTO deploy_includes (project, run, sha, included, checked) VALUES (?,?,?,?,?)"
                    " ON CONFLICT(project, run, sha) DO UPDATE SET included=excluded.included,"
                    " checked=excluded.checked",
                    (project, found["run"], found["sha"], int(found["included"]), now),
                )
    return changed


def _row(r) -> dict:
    out = {c: r[c] for c in COLUMNS}
    out["project"] = r["project"]
    out["inputs"] = json.loads(r["inputs"] or "{}")
    out["card"] = ""
    return out


def runs(
    s, project: str = "", env: str = "", before: str = "", limit: int = JOURNAL_DEFAULT
) -> List[dict]:
    """The Actions runs, newest first (by when they were created); ``before`` — created strictly earlier."""
    where, args = [], []
    for column, value in (("project", project), ("env", env)):
        if value:
            where.append(f"{column}=?")
            args.append(value)
    if before:
        where.append("created<?")
        args.append(before)
    sql = (
        "SELECT * FROM deploy_runs"
        + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY created DESC, run DESC LIMIT ?"
    )
    return [_row(r) for r in s._conn.execute(sql, (*args, limit))]


def last_run(s, project: str, env: str) -> Optional[dict]:
    found = runs(s, project, env, limit=1)
    return found[0] if found else None


def platform_releases(s, limit: int = JOURNAL_DEFAULT) -> List[dict]:
    """The platform's releases as runs, newest first: one per revision its notices name — when it switched, the
    card it came from, its latest state (switched → running, healthy → success, degraded or refused → failure)."""
    by_rev: Dict[str, dict] = {}
    order: List[str] = []
    for r in s._conn.execute(RELEASES, (RELEASE_SCAN,)):
        payload = json.loads(r["payload"])
        params = payload.get("params") or {}
        key = payload.get("key")
        rev = (
            str(params.get("rev") or "") or f"seq:{r['seq']}"
        )  # a refusal names no revision: a row of its own
        state = releases.FORGE.get(key) or releases.HEALTH.get(key) or ""
        status = RELEASE_STATUS.get(state, "unknown")
        if rev not in by_rev:
            if len(order) == limit:
                break
            # newest first: the first notice of a revision is its state
            order.append(rev)
            by_rev[rev] = {
                "project": PLATFORM,
                "env": RELEASE_ENV,
                "run": r["seq"],
                "number": 0,
                "workflow": "",
                "status": status,
                "event": "release",
                "sha": "" if rev.startswith("seq:") else rev,
                "ref": "main",
                "url": "",
                "actor": "",
                "inputs": {},
                "title": "",
                "created": r["ts"],
                "started": r["ts"],
                "stopped": r["ts"] if status in FINISHED else "",
                "card": r["card"] or "",
            }
        else:  # an older notice of the revision moves its start back to the switch
            by_rev[rev]["created"] = by_rev[rev]["started"] = r["ts"]
            by_rev[rev]["card"] = by_rev[rev]["card"] or r["card"] or ""
    return [by_rev[rev] for rev in order[:limit]]


# --- the ``deploy`` wait -----------------------------------------------------------------------------------


def env_of(s, project: str, env: str) -> Optional[dict]:
    """The environment as the reader last reported it, or None."""
    row = s._conn.execute("SELECT envs FROM deploy_sources WHERE project=?", (project,)).fetchone()
    found = [e for e in json.loads(row["envs"] or "[]") if e["env"] == env] if row else []
    return found[0] if found else None


def wait_ref(s, record, target) -> str:
    """``<project>:<env>@<sha40>`` of ``kd-work finish --deploy <project>:<env>[@<sha>]`` on ``record``."""
    found = TARGET.fullmatch(target) if isinstance(target, str) else None
    if found is None:
        raise _invalid("--deploy is <project>:<env> or <project>:<env>@<the full 40-character commit>")
    project, env, sha = found.groups()
    if env_of(s, project, env) is None:
        raise _invalid(f"no environment {project}:{env} on the «Deploys» page: name one of its environments")
    if not sha:
        row = s._conn.execute(
            "SELECT head FROM prs WHERE card=? AND project=? AND state='merged' ORDER BY updated DESC, number DESC"
            " LIMIT 1",
            (record.id, project),
        ).fetchone()
        if row is None or not SHA40.fullmatch(row["head"] or ""):
            raise api.ApiError(
                "refused", f"{record.id} has no merged pull request in {project}: name the commit, --deploy "
                f"{project}:{env}@<sha>")
        sha = row["head"]
    return f"{project}:{env}@{sha}"


def _deciding(s, ref: str) -> Optional[dict]:
    """The newest finished run of the wait's environment that holds its commit, or None."""
    project, env, sha = REF.fullmatch(ref).groups()
    row = s._conn.execute(
        "SELECT * FROM deploy_runs r WHERE project=? AND env=? AND status IN ('success', 'failure') AND (sha=? OR"
        " EXISTS (SELECT 1 FROM deploy_includes i WHERE i.project=r.project AND i.run=r.run AND i.sha=?"
        " AND i.included=1)) ORDER BY created DESC, run DESC LIMIT 1",
        (project, env, sha, sha),
    ).fetchone()
    return _row(row) if row else None


def wait_state(s, ref: str) -> str:
    """What the wait's environment says now: ``open``, ``resolved``, ``rejected`` or ``needs_routing`` (gone)."""
    found = REF.fullmatch(ref)
    if found is None or env_of(s, found.group(1), found.group(2)) is None:
        return "needs_routing"
    run = _deciding(s, ref)
    if run is None:
        return "open"
    return "resolved" if run["status"] == "success" else "rejected"


def failed(s, wait) -> str:
    """A failed run ended the card's wait: a notice in its thread, the run in the inbox; returns the reason."""
    from kd.cabinet import notices  # at call time: it loads the API this module registers on

    run = _deciding(s, wait["ref"])
    if run is None:
        return ""
    project, env = run["project"], run["env"]
    reason = f"deploy {project}:{env} failed: run #{run['number'] or run['run']} {run['url']}".strip()
    row = s._conn.execute("SELECT cards FROM deploy_alerts WHERE project=? AND run=?", (project, run["run"])).fetchone()
    cards = json.loads(row["cards"]) if row else []
    if wait["card"] not in cards:
        cards.append(wait["card"])
    s._conn.execute(
        "INSERT INTO deploy_alerts (project, run, cards, created) VALUES (?,?,?,?) ON CONFLICT(project, run)"
        " DO UPDATE SET cards=excluded.cards",
        (project, run["run"], json.dumps(cards), s._clock()),
    )
    notices.post_in_tx(s, source="forge", level="warn", key="notice.deploy.failed", card=wait["card"], params={
        "project": project, "env": env, "number": run["number"] or run["run"], "sha": run["sha"][:7],
        "url": run["url"]})
    return reason


def waiting(s, project: str, env: str) -> List[dict]:
    """The cards whose open waits name this environment: ``{card, sha}``, oldest first."""
    rows = s._conn.execute(
        "SELECT card, ref FROM waits WHERE kind='deploy' AND state='open' AND ref LIKE ? ORDER BY created, card",
        (f"{project}:{env}@%",),
    ).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["card"] not in seen:
            seen.add(r["card"])
            out.append({"card": r["card"], "sha": r["ref"].rsplit("@", 1)[1]})
    return out


def settle_waits(ctx) -> int:
    """After a report: every open ``deploy`` wait as the runs say now (a project the report left out — routing)."""
    from kd.cabinet import waits  # at call time: it loads the API this module registers on

    s, moved, sets = ctx.store, 0, []
    with s.transaction():
        for wait in [dict(r) for r in s._conn.execute("SELECT * FROM waits WHERE kind='deploy' AND state='open'")]:
            state = waits.source_state(ctx, wait)
            if state != "open":
                waits._move(s, wait, state)
                moved += 1
                if wait["command"] not in sets:
                    sets.append(wait["command"])
        for command in sets:
            waits.settle(ctx, command)
    return moved


def watch(s) -> List[dict]:
    """``{project, env, sha, checked}`` of every commit an open wait names; ``checked`` — the runs already asked."""
    out = []
    for (ref,) in s._conn.execute("SELECT DISTINCT ref FROM waits WHERE kind='deploy' AND state='open' ORDER BY ref"):
        found = REF.fullmatch(ref)
        if found is None:
            continue
        project, env, sha = found.groups()
        checked = [r[0] for r in s._conn.execute(
            "SELECT run FROM deploy_includes WHERE project=? AND sha=? ORDER BY run", (project, sha))]
        out.append({"project": project, "env": env, "sha": sha, "checked": checked})
    return out


def alerts(s) -> List[dict]:
    """The failed runs nobody saw yet, newest first, each with its run and the cards it woke."""
    out = []
    for a in s._conn.execute(
            "SELECT * FROM deploy_alerts WHERE seen='' ORDER BY created DESC, run DESC LIMIT ?", (ALERTS_MAX,)):
        row = s._conn.execute("SELECT * FROM deploy_runs WHERE project=? AND run=?", (a["project"], a["run"])).fetchone()
        if row is not None:
            out.append({**_row(row), "cards": json.loads(a["cards"]), "alerted": a["created"]})
    return out


def _stale(now: str, ok_at: str) -> bool:
    if not ok_at:
        return True
    return (users.parse_time(now) - users.parse_time(ok_at)).total_seconds() > STALE_S


def _reader(ctx, principal):
    api._require(ctx, principal, "deploys.read")


def can_launch(ctx, principal) -> bool:
    """Who presses «Deploy to prod»: the owner — a person holding every permission over every project. Root and the
    cabinet's CLI are not people: a launch is signed with a person's passkey."""
    return principal.kind == "user" and api._holds_all(ctx, principal.user)


def launchable(s, project: str, env: str) -> Optional[dict]:
    """The manual environment the button starts, as the reader last reported it (with its repository); None for an
    environment the reader does not report or one that deploys by itself."""
    row = s._conn.execute("SELECT repo, envs FROM deploy_sources WHERE project=?", (project,)).fetchone()
    found = next((e for e in json.loads(row["envs"] or "[]") if e["env"] == env), None) if row else None
    if found is None or not found["manual"]:
        return None
    return {"project": project, "repo": row["repo"], **found}


def _launchable(ctx, principal, request):
    """The environment the owner's button would start: ``deploy.launchable {project, env}`` → ``{env}``."""
    if not can_launch(ctx, principal):
        raise api.ApiError("refused", "only the owner starts a prod deploy")
    project, env = request.get("project"), request.get("env")
    if not isinstance(project, str) or not NAME.fullmatch(project) or not isinstance(env, str) \
            or not NAME.fullmatch(env):
        raise _invalid("project and env are ids")
    found = launchable(ctx.store, project, env)
    if found is None:
        raise api.ApiError("not_found", f"{project}:{env}: no environment started by hand")
    return {"env": found}


def _launched(ctx, principal, request):
    """The cabinet records a launch kd-review dispatched (or refused): one ``deploy.launched`` event — the journal of
    who started what, on which commit, with ``skip_tests``."""
    record = request.get("launch")
    if not isinstance(record, dict):
        raise _invalid("launch is an object")
    fields = {k: record.get(k) for k in ("id", "user", "project", "env", "sha", "skip_tests", "outcome", "message")}
    for name in ("id", "user", "project", "env", "outcome"):
        _text(fields[name], name, 100)
    _text(fields["message"] or "", "message", 500)
    if not isinstance(fields["sha"], str) or not SHA.fullmatch(fields["sha"]) or not isinstance(
            fields["skip_tests"], bool):
        raise _invalid("sha is a commit, skip_tests true or false")
    s = ctx.store
    with s.transaction():
        s._event(f"user:{fields['user']}", "deploy.launched", "", fields)
    return {"recorded": True}


def launches(s, limit: int = JOURNAL_DEFAULT) -> List[dict]:
    """The newest launches from the cabinet, newest first."""
    rows = s._conn.execute(
        "SELECT ts, payload FROM events WHERE kind='deploy.launched' ORDER BY seq DESC LIMIT ?", (limit,))
    return [{"at": r["ts"], **json.loads(r["payload"])} for r in rows]


def _report(ctx, principal, request):
    sources = _sources(request.get("sources"))
    changed = apply_report(ctx.store, sources, ctx.now())
    return {"changed": changed, "woken": settle_waits(ctx)}


def _watch(ctx, principal, request):
    return {"watch": watch(ctx.store)}


def _alerts(ctx, principal, request):
    _reader(ctx, principal)
    return {"items": alerts(ctx.store)}


def _seen(ctx, principal, request):
    _reader(ctx, principal)
    project, run = request.get("project"), request.get("run")
    if not isinstance(project, str) or not NAME.fullmatch(project) or isinstance(run, bool) \
            or not isinstance(run, int) or run < 1:
        raise _invalid("project is a project id, run the Actions run's id")
    s = ctx.store
    with s.transaction():
        found = s._conn.execute(
            "UPDATE deploy_alerts SET seen=?, seen_by=? WHERE project=? AND run=? AND seen=''",
            (ctx.now(), f"{principal.kind}:{principal.user}", project, run)).rowcount
        if found:
            s._event(f"{principal.kind}:{principal.user}", "deploy.alert_seen", "", {"project": project, "run": run})
    return {"seen": bool(found)}


def _list(ctx, principal, request):
    _reader(ctx, principal)
    s, now = ctx.store, ctx.now()
    names = {p["id"]: p["name"] for p in s.project_rows()}
    owner = can_launch(ctx, principal)
    last_launch = {}
    for item in reversed(launches(s)):
        last_launch[(item.get("project"), item.get("env"))] = item
    envs = []
    for src in s._conn.execute("SELECT * FROM deploy_sources ORDER BY project"):
        stale = _stale(now, src["ok_at"])
        for env in json.loads(src["envs"] or "[]"):
            envs.append(
                {
                    "project": src["project"],
                    "name": names.get(src["project"], src["project"]),
                    "repo": src["repo"],
                    **env,
                    "read_at": src["read_at"],
                    "ok_at": src["ok_at"],
                    "error": src["error"],
                    "stale": stale,
                    "run": last_run(s, src["project"], env["env"]),
                    "waiting": waiting(s, src["project"], env["env"]),
                    # the button «Deploy to prod»: a manual environment, for the owner only
                    "launch": owner and env["manual"],
                    "launched": last_launch.get((src["project"], env["env"])),
                }
            )
    found = platform_releases(s, 1)
    return {
        "envs": envs,
        "platform": found[0] if found else None,
        "now": now,
        "stale_after": STALE_S,
    }


def _runs(ctx, principal, request):
    _reader(ctx, principal)
    project, env = request.get("project", ""), request.get("env", "")
    for name, value in (("project", project), ("env", env)):
        if not isinstance(value, str) or (value and not NAME.fullmatch(value)):
            raise _invalid(f"{name} is a project or env id")
    before = request.get("before", "")
    if before:
        _time(before, "before")
    limit = request.get("limit", JOURNAL_DEFAULT)
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= JOURNAL_MAX
    ):
        raise _invalid(f"limit is a number from 1 to {JOURNAL_MAX}")
    s = ctx.store
    items = [] if project == PLATFORM else runs(s, project, env, before, limit + 1)
    if project in ("", PLATFORM) and env in ("", RELEASE_ENV):
        # the releases page by the same moment as the runs (a release in a page is never a run's twin)
        items += [
            r
            for r in platform_releases(s, JOURNAL_MAX + limit)
            if not before or r["created"] < before
        ]
    items.sort(key=lambda r: (r["created"], r["run"]), reverse=True)
    more = len(items) > limit
    items = items[:limit]
    return {"items": items, "next": items[-1]["created"] if more and items else None}


OPS = {
    "deploy.report": (frozenset({"forge"}), _report),
    "deploy.list": (api.ROOTISH | {"user", "agent"}, _list),
    "deploy.runs": (api.ROOTISH | {"user", "agent"}, _runs),
    "deploy.watch": (frozenset({"forge"}), _watch),
    "deploy.alerts": (api.ROOTISH | {"user", "agent"}, _alerts),
    "deploy.alert.seen": (api.ROOTISH | {"user"}, _seen),
    "deploy.launchable": (frozenset({"user"}), _launchable),
    "deploy.launched": (frozenset({"cabinet"}), _launched),
}
api.OPS.update(OPS)  # the board API serves it (api imports this module at its end)
