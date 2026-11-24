"""The deploys reader (KD-9443): digipaddock-deploys.service under <prefix>-forge, started by its timer at least
once a minute. It reads the Forgejo Actions runs of every environment the projects' passports name
(``repo.deploys`` of the release tree's ``host/projects.json``) with dev's read token and reports them to the
cabinet (``deploy.report``) — the cabinet has no network and holds no Forgejo token. For the commits the cards'
``deploy`` waits name (``deploy.watch``) it asks Forgejo whether a finished run's commit holds them and reports that
too (``includes``).

One project's failure (Forgejo refused, a broken answer) is that project's ``error`` in the report, its runs kept
as the cabinet has them; the others are read as usual. Exit codes: 0 reported; 1 the cabinet did not take the
report, or the passports are unreadable."""

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from dev_agent import forge, repos

TREE = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(TREE / "common"))
from kd import install_spec  # noqa: E402
from kd.cabinet import client as cabinet_client  # noqa: E402

PROJECTS = TREE / "host" / "projects.json"
LIMIT = 30  # runs read per project and tick: the table keeps the history, the journal pages through it
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
SHA_RE = re.compile(r"[0-9a-f]{40}")
DECIDING = ("success", "failure")  # the runs that decide a card's deploy wait
CHECKS_MAX = 20  # Forgejo compares per tick for the cards' deploy waits
INPUT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")
INPUTS_MAX, INPUT_MAX, TEXT_MAX, URL_MAX, ERROR_MAX = 8, 200, 200, 500, 500


def sources(path=PROJECTS) -> List[dict]:
    """``{project, repo, envs}`` of every project whose passport names deploys, in the file's order; no file —
    none. A broken file raises ``install_spec.InstallSpecError`` (the overlay check refuses it before a release)."""
    if not Path(path).exists():
        return []
    out = []
    for pid, project in install_spec.load_projects(str(path)).items():
        if not project.repo:
            continue
        found = repos.deploys(pid, project.repo)
        if found:
            out.append(
                {
                    "project": pid,
                    "repo": project.repo["repo"],
                    "envs": [
                        {
                            "env": d.env,
                            "workflow": d.workflow,
                            "branch": d.branch,
                            "manual": d.manual,
                            "confirm": d.confirm,
                            "skip_tests": d.skip_tests,
                        }
                        for d in found
                    ],
                }
            )
    return out


def _clean(value, limit: int = TEXT_MAX) -> str:
    text = value if isinstance(value, str) else ""
    return "".join(c if ord(c) >= 32 else " " for c in text)[:limit]


def utc(value) -> str:
    """A Forgejo time as ``YYYY-MM-DDTHH:MM:SSZ``; Go's zero time (a run not started yet) and nonsense — ''."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.tzinfo is None or moment.year < 2000:
        return ""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _inputs(run: dict) -> Dict[str, str]:
    """A dispatched run's inputs (``confirm``, ``skip_tests``) from its event payload, as text."""
    if run.get("event") != "workflow_dispatch":
        return {}
    try:
        payload = json.loads(run.get("event_payload") or "{}")
    except (TypeError, ValueError):
        return {}
    inputs = payload.get("inputs") if isinstance(payload, dict) else None
    if not isinstance(inputs, dict):
        return {}
    out = {}
    for name, value in inputs.items():
        if (
            len(out) == INPUTS_MAX
            or not isinstance(name, str)
            or not INPUT_RE.fullmatch(name)
        ):
            continue
        text = (
            json.dumps(value)
            if isinstance(value, bool)
            else value
            if isinstance(value, str)
            else str(value)
        )
        out[name] = _clean(text, INPUT_MAX)
    return out


def run_of(item: dict, envs: Dict[str, Tuple[str, str]]) -> Optional[dict]:
    """One run of the Actions API as the cabinet keeps it; None for a run of another workflow, of another branch
    (one workflow may also run on a branch that deploys nothing) or without an id. ``envs``: workflow → (env,
    branch)."""
    env, branch = envs.get(item.get("workflow_id")) or (None, "")
    ref = item.get("prettyref")
    run, created = item.get("id"), utc(item.get("created"))
    if (
        env is None
        or (
            isinstance(ref, str) and ref and ref not in (branch, "refs/heads/" + branch)
        )
        or isinstance(run, bool)
        or not isinstance(run, int)
        or run < 1
        or not created
    ):
        return None
    number = item.get("index_in_repo")
    sha = str(item.get("commit_sha") or "").lower()
    url = item.get("html_url") if isinstance(item.get("html_url"), str) else ""
    user = item.get("trigger_user")
    status = item.get("status")
    return {
        "run": run,
        "number": number
        if isinstance(number, int) and not isinstance(number, bool) and number > 0
        else 0,
        "env": env,
        "status": status if status in STATUSES else "unknown",
        "event": _clean(item.get("event"), 40),
        "sha": sha if SHA_RE.fullmatch(sha) else "",
        "ref": _clean(item.get("prettyref")),
        "url": url if url.startswith("https://") and len(url) <= URL_MAX else "",
        "actor": _clean(user.get("login") if isinstance(user, dict) else "", 100),
        "inputs": _inputs(item),
        "title": _clean(item.get("title")),
        "created": created,
        "started": utc(item.get("started")),
        "stopped": utc(item.get("stopped")),
    }


def read(api, source: dict) -> dict:
    """The source with its runs, or with its ``error`` and no runs."""
    envs = {e["workflow"]: (e["env"], e["branch"]) for e in source["envs"]}
    try:
        data = api.get(f"/repos/{source['repo']}/actions/runs", {"limit": LIMIT})
    except forge.ForgeError as exc:
        return {
            **source,
            "runs": [],
            "error": _clean(str(exc), ERROR_MAX) or "Forgejo: ошибка",
        }
    items = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {**source, "runs": [], "error": "ответ Forgejo без workflow_runs"}
    runs = [
        r
        for r in (run_of(i, envs) for i in items if isinstance(i, dict))
        if r is not None
    ]
    return {**source, "runs": runs, "error": ""}


def includes(api, source: dict, watch: List[dict], budget: List[int]) -> List[dict]:
    """``{run, sha, included}``: whether each finished run of a watched environment holds the commit a card's
    ``deploy`` wait names — Forgejo's compare of the two; runs the cabinet has the answer for and a run of that very
    commit are not asked. ``budget`` — the checks left in this tick; an unanswered check is asked next tick."""
    out = []
    for item in watch:
        if item.get("project") != source["project"]:
            continue
        sha, checked = item.get("sha"), set(item.get("checked") or ())
        if not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            continue
        for run in source["runs"]:
            if (run["env"] != item.get("env") or run["status"] not in DECIDING or not run["sha"]
                    or run["sha"] == sha or run["run"] in checked):
                continue
            if budget[0] <= 0:
                return out
            budget[0] -= 1
            try:
                held = forge.contains(api, source["repo"], run["sha"], sha)
            except forge.ForgeError:
                continue
            out.append({"run": run["run"], "sha": sha, "included": held})
    return out


def collect(found: List[dict], api_factory=None, watch=()) -> List[dict]:
    """Every source read, with what the watched commits' checks found; without a token every one carries that
    error."""
    if not found:
        return []
    try:
        api = (
            api_factory or (lambda: forge.Api(forge.load_token(forge.dev_login())))
        )()
    except forge.ForgeError as exc:
        return [{**s, "runs": [], "error": _clean(str(exc), ERROR_MAX)} for s in found]
    budget = [CHECKS_MAX]
    out = []
    for source in found:
        got = read(api, source)
        got["includes"] = includes(api, got, list(watch), budget) if not got["error"] else []
        out.append(got)
    return out


def main(argv=None, *, api_factory=None, client=None, projects=PROJECTS) -> int:
    try:
        found = sources(projects)
    except install_spec.InstallSpecError as exc:
        print(f"kd-deploys: host/projects.json не прочитан: {exc}", file=sys.stderr)
        return 1
    client = client or cabinet_client.Client()
    try:  # the commits the cards wait for; without them the runs are reported all the same
        watch = client.call({"op": "deploy.watch"}).get("watch") or []
    except cabinet_client.CabinetError as exc:
        print(f"kd-deploys: ожидания карточек не прочитаны ({exc.kind}: {exc})", file=sys.stderr)
        watch = []
    read_ = collect(found, api_factory, watch)
    for source in read_:
        if source["error"]:
            print(
                f"kd-deploys: {source['project']}: {source['error']}", file=sys.stderr
            )
    try:
        result = client.call(
            {"op": "deploy.report", "sources": read_}
        )
    except cabinet_client.CabinetError as exc:
        print(
            f"kd-deploys: кабинет не принял отчёт ({exc.kind}: {exc})", file=sys.stderr
        )
        return 1
    print(
        f"kd-deploys: проектов {len(read_)}, изменились прогоны: {len(result.get('changed', []))}"
    )
    return 0
