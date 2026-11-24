"""The registry of dev's projects: /etc/digipaddock/repos.json (root 0644; add_repo.sh writes it; kd-pr reads it as
the dev user, the executor as <base>-forge and kd-release as root). The secrets directory /etc/digipaddock/forge/ holds only *.env.
{"digipaddock": {"repo": "example/DigiPaddock", "dir": "work/DigiPaddock", "release": "digipaddock"}}
a release is its code repository (role code, the default) and optionally the deployment's overlay
(role overlay) — one of each per release; kd-release releases the pair.
the entry is the project's passport (schema 2) — every field below is optional, an entry without them is
today's: base and the only target main, no required checks, a PR merges on a current base.
{"alert": {"repo": "example/alert", "dir": "work/alert", "base": "master", "targets": ["master"],
           "checks": {"contour": "dev-a", "profiles": {"unit": {"required": true}}, "timeout_s": 1800},
           "merge_effects": {"master": "deploy to the acceptance stage"},
           "merge": {"require_up_to_date": true, "up_to_date_paths": ["migrations/*"]},
           "deploys": {"test": {"workflow": "deploy-test.yml", "branch": "master"},
                       "prod": {"workflow": "deploy-prod.yml", "branch": "master", "manual": true,
                                "confirm": "confirm"}}}}
``deploys`` — the project's environments in the order the cabinet's «Deploys» shows them: the Forgejo Actions
workflow file that deploys each, the branch it deploys and whether it runs only by hand (workflow_dispatch). A
manual one is started by the owner's button (kd-review's ``deploy``): ``confirm`` names the workflow's text input
that gets the typed word ``deploy``, ``skip_tests: true`` says the workflow takes the boolean input ``skip_tests``.
The schema lives in the entries only (no top-level key): an older release, rolled back to, reads the file as before."""
import fnmatch
import json
import posixpath
import re
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

REPOS = "/etc/digipaddock/repos.json"
AGENT_DIR = "/srv/digipaddock-agents/dev"
PROJECT_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,100}")
DIR_RE = re.compile(r"work/[A-Za-z0-9_.-]{1,64}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # and no "..", not ending ".lock" (git check-ref-format)
PROFILE_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")  # a profile and a contour
RELEASES = {"digipaddock"}  # what kd-release can release
ROLES = ("code", "overlay")
EFFECT_MAX = 300
TIMEOUT = (60, 7200)
PATH_MAX = 200
WORKFLOW_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\.ya?ml")
INPUT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]{0,63}")  # a workflow_dispatch input's name
DEPLOYS_MAX = 8


class RepoError(Exception):
    pass


class Deploy(NamedTuple):
    env: str
    workflow: str  # the file name under .forgejo/workflows — the run's workflow_id in the Actions API
    branch: str
    manual: bool = False
    confirm: str = ""  # a manual workflow's text input that gets the word "deploy"
    skip_tests: bool = False  # a manual workflow takes the boolean input skip_tests


class Repo(NamedTuple):
    project: str
    repo: str
    dir: str
    release: Optional[str]
    role: str = "code"
    base: str = "main"
    targets: Tuple[str, ...] = ("main",)
    profiles: Tuple[Tuple[str, bool], ...] = ()  # (name, required) in the passport's order
    contour: str = ""
    timeout_s: int = 1800
    effects: Tuple[Tuple[str, str], ...] = ()  # (target, text)
    require_up_to_date: bool = True
    up_to_date_paths: Tuple[str, ...] = ()
    deploys: Tuple[Deploy, ...] = ()


def is_branch(value) -> bool:
    return (isinstance(value, str) and bool(BRANCH_RE.fullmatch(value)) and ".." not in value
            and not value.endswith(".lock"))


def required(repo: Repo) -> List[str]:
    """The profiles whose devcheck/<profile> status a merge needs green."""
    return [name for name, need in repo.profiles if need]


def effect(repo: Repo, target: str) -> str:
    """What a merge into the target sets off — the owner sees it in the decision's block."""
    return dict(repo.effects).get(target, "")


def needs_up_to_date(repo: Repo, paths: Iterable[str]) -> bool:
    """The project asks for a current base, or the PR touches one of its up_to_date_paths."""
    if repo.require_up_to_date:
        return True
    return any(fnmatch.fnmatchcase(path, glob) for path in paths for glob in repo.up_to_date_paths)


def deploys(project: str, item: dict) -> Tuple[Deploy, ...]:
    """The passport's ``deploys``: env → {workflow, branch[, manual]}; one workflow file per environment."""
    value = item.get("deploys", {})
    if not isinstance(value, dict) or len(value) > DEPLOYS_MAX:
        raise RepoError(f"repos.json: {project}: deploys — объект окружение → выкат, не больше {DEPLOYS_MAX}")
    out, files = [], set()
    for env, spec in value.items():
        def bad(what: str, env=env):
            return RepoError(f"repos.json: {project}: deploys.{env}: {what}")

        if not isinstance(env, str) or not PROFILE_RE.fullmatch(env) or not isinstance(spec, dict):
            raise RepoError(f"repos.json: {project}: deploys — неверное окружение {env!r}")
        if not set(spec) <= {"workflow", "branch", "manual", "confirm", "skip_tests"}:
            raise bad("ключи — workflow, branch, manual, confirm, skip_tests")
        workflow, branch, manual = spec.get("workflow"), spec.get("branch"), spec.get("manual", False)
        confirm, skip_tests = spec.get("confirm", ""), spec.get("skip_tests", False)
        if not isinstance(workflow, str) or not WORKFLOW_RE.fullmatch(workflow) or workflow in files:
            raise bad("workflow — свой файл .yml в .forgejo/workflows")
        if not is_branch(branch):
            raise bad("branch — имя ветки")
        if not isinstance(manual, bool):
            raise bad("manual — true или false")
        if not isinstance(confirm, str) or (confirm and not INPUT_RE.fullmatch(confirm)):
            raise bad("confirm — имя текстового входа workflow")
        if not isinstance(skip_tests, bool) or (skip_tests and confirm == "skip_tests"):
            raise bad("skip_tests — true или false")
        if (confirm or skip_tests) and not manual:
            raise bad("confirm и skip_tests — только у ручного выката (manual: true)")
        files.add(workflow)
        out.append(Deploy(env, workflow, branch, manual, confirm, skip_tests))
    return tuple(out)


def _passport(project: str, item: dict, release: Optional[str]) -> dict:
    def bad(what: str):
        return RepoError(f"repos.json: {project}: {what}")

    base = item.get("base", "main")
    if not is_branch(base):
        raise bad("base — имя ветки")
    targets = item.get("targets", [base])
    if (not isinstance(targets, list) or not targets or not all(is_branch(t) for t in targets)
            or len(set(targets)) != len(targets)):
        raise bad("targets — непустой список разных веток")
    if release is not None and (base != "main" or targets != ["main"]):
        raise bad("release выкатывает main: base и targets — только main")
    checks = item.get("checks", {})
    if not isinstance(checks, dict):
        raise bad("checks — объект")
    profiles = checks.get("profiles", {})
    if not isinstance(profiles, dict):
        raise bad("checks.profiles — объект профиль → {required}")
    found = []
    for name, spec in profiles.items():
        if not isinstance(name, str) or not PROFILE_RE.fullmatch(name) or not isinstance(spec, dict):
            raise bad(f"неверный профиль {name!r}")
        need = spec.get("required", False)
        if not isinstance(need, bool):
            raise bad(f"профиль {name}: required — true или false")
        found.append((name, need))
    contour = checks.get("contour", "")
    if not isinstance(contour, str) or (contour and not PROFILE_RE.fullmatch(contour)):
        raise bad("checks.contour — имя контура")
    timeout = checks.get("timeout_s", 1800)
    if isinstance(timeout, bool) or not isinstance(timeout, int) or not TIMEOUT[0] <= timeout <= TIMEOUT[1]:
        raise bad(f"checks.timeout_s — {TIMEOUT[0]}…{TIMEOUT[1]}")
    effects = item.get("merge_effects", {})
    if not isinstance(effects, dict) or not all(
            k in targets and isinstance(v, str) and len(v) <= EFFECT_MAX for k, v in effects.items()):
        raise bad(f"merge_effects — ветка из targets → текст до {EFFECT_MAX} символов")
    merge = item.get("merge", {})
    if not isinstance(merge, dict):
        raise bad("merge — объект")
    current = merge.get("require_up_to_date", True)
    if not isinstance(current, bool):
        raise bad("merge.require_up_to_date — true или false")
    paths = merge.get("up_to_date_paths", [])
    if not isinstance(paths, list) or not all(
            isinstance(p, str) and p and len(p) <= PATH_MAX and not p.startswith("/") and ".." not in p.split("/")
            for p in paths):
        raise bad("merge.up_to_date_paths — относительные пути репозитория")
    return {"base": base, "targets": tuple(targets), "profiles": tuple(found), "contour": contour,
            "timeout_s": timeout, "effects": tuple(effects.items()), "require_up_to_date": current,
            "up_to_date_paths": tuple(paths), "deploys": deploys(project, item)}


def load(path=REPOS) -> Dict[str, Repo]:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as exc:
        raise RepoError(f"repos.json не читается: {exc.__class__.__name__}") from exc
    if not isinstance(data, dict):
        raise RepoError("repos.json: ожидается объект проект → описание")
    out: Dict[str, Repo] = {}
    taken = {}
    for project, item in data.items():
        if not isinstance(project, str) or not PROJECT_RE.fullmatch(project) or not isinstance(item, dict):
            raise RepoError(f"repos.json: неверный проект {project!r}")
        repo, folder, release = item.get("repo"), item.get("dir"), item.get("release")
        if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
            raise RepoError(f"repos.json: {project}: неверный repo")
        # work/. and work/.. are work/ itself and the agent's folder, not a clone
        if not isinstance(folder, str) or not DIR_RE.fullmatch(folder) or ".." in folder or folder == "work/.":
            raise RepoError(f"repos.json: {project}: dir — только work/<Имя>")
        if release is not None and (not isinstance(release, str) or release not in RELEASES):
            raise RepoError(f"repos.json: {project}: неизвестный release {release!r}")
        role = item.get("role", "code")
        if role not in ROLES or (role == "overlay" and release is None):
            raise RepoError(f"repos.json: {project}: role — code или overlay (overlay — только с release)")
        if release is not None:
            other = taken.setdefault((release, role), project)
            if other != project:
                raise RepoError(f"repos.json: release {release}: role {role} у {other} и у {project}")
        out[project] = Repo(project, repo, posixpath.join(AGENT_DIR, folder), release, role,
                            **_passport(project, item, release))
    return out


def get(project: str, path=REPOS) -> Repo:
    if not isinstance(project, str) or not PROJECT_RE.fullmatch(project):
        raise RepoError(f"неверное имя проекта: {project!r}")
    repo = load(path).get(project)
    if repo is None:
        raise RepoError(f"проект не подключён: {project} (add_repo.sh)")
    return repo
