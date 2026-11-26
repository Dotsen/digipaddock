"""kd-pr <project> KD-<n> --title … --body-file … [--target <branch>] — a PR through AGit. Runs as the agent's user
(<prefix>-dev), without sudo.
Checks: the project is attached, the clone's tree is clean, the branch is agent/KD-<n>-<slug> of the same card, the
body has ## Checks (what was run — for information) and ## Cage, the sizes. kd-pr computes the cage itself from git
diff (by inversion: everything except docs/, tasks/, tests and ordinary .md) and appends the line
"Cage (computed): …" to the body; the agent's "no" with a non-empty cage is a refusal.
The PR's branch is one of the targets of the project passport (--target; a single one is the default), the base of
the cage is its live head. kd-pr appends the passport's required profiles as the line "Checks (passport): …"; with
them the PR is a draft (WIP:) until the statuses are green, kd-forge publish verifies them.
The push is only HEAD:refs/for/<target>, never a force-push; a repeat of the same topic updates the PR.
Two dev sessions work on two cards in their own worktrees: a card's tree is the clone's worktree with its branch
(work/<Name>, beside the clone), and the clone itself when there is none."""
import argparse
import base64
import posixpath
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from dev_agent import repos

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))  # …/DigiPaddock/common
from kd import ids  # noqa: E402 — the host's card prefix

REPOS = repos.REPOS
# a branch made before the card prefix moved keeps its name (its AGit topic is the PR's): ids.canonical of group 1
BRANCH_RE = re.compile(r"agent/(" + ids.ANY.pattern + r")-[a-z0-9-]{1,40}")
CARD_RE = ids.CARD
URL_RE = re.compile(r"https?://\S+/pulls/[0-9]+")
WIP_RE = re.compile(r"^\s*(?:(?:wip:|\[wip\])\s*)+", re.I)  # the draft prefixes Forgejo knows (WIP:, [WIP]), all of them
# Texts the installers or the runtime put into an agent's or a worker's instructions — the cage whatever their
# extension: an agent's folder (its CLAUDE.md, playbooks, any .md its CLAUDE.md includes), the template shared by
# every agent (kd.agent_text), the specializations and skills of workers and persistent cards (kd.specializations)
INSTRUCTIONS = ("agents/", "common/agent-template/", "common/specializations/")
# Claude Code reads a repository's CLAUDE.md and .claude/ into the session that works in it
INSTRUCTION_NAME = re.compile(r"(?:.*/)?(?:CLAUDE\.md|\.claude/.*)")
SAYS_NO = re.compile(r"(нет|no)\b", re.I)
CONTROL = re.compile(r"[\x00-\x1f\x7f]")  # a push option is one line; \r and other control characters do not go into a PR title
BODY_MAX = 60 * 1024
TITLE_MAX = 200
# git sends each push option as one pkt-line: no longer than LARGE_PACKET_MAX (65520) − 4 bytes of the header, or
# it is "protocol error: impossibly long line". With base64 that is ~48 KB of body with the cage line (under BODY_MAX).
PUSH_OPTION_MAX = 65516
DESC_PREFIX = "description={base64}"
GIT_TIMEOUT = 120
SHA_RE = re.compile(r"[0-9a-f]{40}")
PUSH_TIMEOUT = 300
TREE_RE = re.compile(r"[A-Za-z0-9_.-]{1,80}")  # a card's worktree: work/<name>, next to the clones


class PrError(Exception):
    pass


def _git(repo: repos.Repo, run, *args: str) -> subprocess.CompletedProcess:
    """git in the project's clone; a timeout or a missing git is a refused check, not a traceback."""
    try:
        # --no-replace-objects: a replaced object (git replace) does not change the cage's diff
        return run(["git", "-C", repo.dir, "--no-replace-objects", *args], capture_output=True, encoding="utf-8",
                   errors="replace", timeout=GIT_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PrError(f"git {args[0]} в {repo.dir}: {exc.__class__.__name__}") from exc


def tree_of(repo: repos.Repo, card: str, run=subprocess.run) -> str:
    """The card's work tree: the clone's worktree on its branch agent/<card>-…, otherwise the clone itself. A tree
    outside work/<name> and the card's branches in two trees are refused: the push would leave from the wrong tree."""
    listed = _git(repo, run, "worktree", "list", "--porcelain")
    if listed.returncode != 0:
        raise PrError(f"git worktree list в {repo.dir}: {listed.stderr.strip()[:200]}")
    found, path = [], None
    for line in listed.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line.startswith("branch refs/heads/") and path is not None:
            m = BRANCH_RE.fullmatch(line[len("branch refs/heads/"):])
            if m and ids.canonical(m.group(1)) == card:
                found.append(path)
    if not found:
        return repo.dir
    if len(found) > 1:
        raise PrError(f"ветки {card} в нескольких деревьях: {', '.join(found)} — оставь одно (git worktree remove)")
    tree = found[0]
    work = posixpath.join(repos.AGENT_DIR, "work")
    name = posixpath.basename(tree)
    if tree != repo.dir and (posixpath.normpath(tree) != tree or posixpath.dirname(tree) != work
                             or not TREE_RE.fullmatch(name) or name in (".", "..")):
        raise PrError(f"дерево карточки {tree} не в {work}/<имя> — создай worktree рядом с клоном")
    return tree


def cage_files(names: List[str]) -> List[str]:
    """The agents' cage, by inversion: everything except docs/**, tasks/**, **/tests/** and the *.md that are no
    instructions (``INSTRUCTIONS``, ``INSTRUCTION_NAME``). A new code directory falls into the cage by itself — the
    list never goes stale. A name counts as git or Forgejo gives it, never trimmed; one git quoted (a control
    character in it — the cabinet quotes such a name the same way) is the cage wherever it sits."""
    out = []
    for name in names:
        if not name:
            continue
        if not name.startswith('"') and (name.startswith(("docs/", "tasks/")) or "/tests/" in "/" + name):
            continue
        if name.endswith(".md") and not name.startswith(INSTRUCTIONS) and not INSTRUCTION_NAME.fullmatch(name):
            continue
        out.append(name)
    return out


def cage_line(cage: List[str]) -> str:
    return f"Cage (computed): да — {', '.join(cage)}" if cage else "Cage (computed): нет"


def checks_line(required: List[str]) -> str:
    if not required:
        return "Checks (passport): нет обязательных"
    names = ", ".join(f"devcheck/{name}" for name in required)
    return f"Checks (passport): обязательны {names} — PR черновик до зелёных статусов"


def draft_title(title: str, required: List[str]) -> str:
    """One WIP: prefix when the passport has required checks, none otherwise — whatever the agent wrote."""
    bare = WIP_RE.sub("", title, count=1)
    return f"WIP: {bare}" if required else bare


def target_of(repo: repos.Repo, given: Optional[str]) -> str:
    """The PR's branch: one of the passport's targets — named, or the only one."""
    if given is None:
        if len(repo.targets) != 1:
            raise PrError(f"у проекта {repo.project} несколько целевых веток: {', '.join(repo.targets)} — укажи --target")
        return repo.targets[0]
    if given not in repo.targets:
        raise PrError(f"ветка {given} не в targets проекта: {', '.join(repo.targets)}")
    return given


def check(repo: repos.Repo, card: str, title: str, body: str, run=subprocess.run,
          target: Optional[str] = None) -> Tuple[str, List[str]]:
    target = target_of(repo, target)
    if not CARD_RE.fullmatch(card):
        raise PrError(f"карточка — KD-<n>: {card}")
    if not title.strip() or len(title) > TITLE_MAX or CONTROL.search(title):
        raise PrError(f"заголовок: одна строка до {TITLE_MAX} символов")
    if len(body.encode("utf-8")) > BODY_MAX:
        raise PrError("тело PR больше 60 КБ")
    if "## Checks" not in body:
        raise PrError("в теле PR нет раздела ## Checks")
    if "## Cage" not in body:
        raise PrError("в теле PR нет раздела ## Cage (трогает ли PR клетку агентов)")
    status = _git(repo, run, "status", "--porcelain", "--untracked-files=no")
    if status.returncode != 0:
        raise PrError(f"git status в {repo.dir}: {status.stderr.strip()[:200]}")
    if status.stdout.strip():
        raise PrError("рабочее дерево клона не чисто — сначала коммит")
    head = _git(repo, run, "rev-parse", "--abbrev-ref", "HEAD")
    branch = head.stdout.strip()
    m = BRANCH_RE.fullmatch(branch)
    if head.returncode != 0 or not m or ids.canonical(m.group(1)) != card:
        raise PrError(f"текущая ветка {branch!r} — нужна agent/{card}-<slug>")
    # The base of the cage is the live head of the target branch in Forgejo, not the clone's origin/<branch>: the
    # agent could move that ref (update-ref, a branch of that name, a fetch refspec), and the line "Cage (computed)"
    # the owner releases by would turn false. The sha itself must already be in the clone — the agent runs
    # git fetch origin.
    ref = f"refs/heads/{target}"
    remote = _git(repo, run, "ls-remote", "--exit-code", "origin", ref)
    fields = remote.stdout.split()
    if remote.returncode != 0 or len(fields) < 2 or not SHA_RE.fullmatch(fields[0]) or fields[1] != ref:
        raise PrError(f"origin {ref} недоступен: {remote.stderr.strip()[:200]}")
    base = fields[0]
    # --no-renames: otherwise a cage file moved out (common/kd/x.py → docs/x.py) shows only by its new path, and the
    # cage reads "no"
    diff = _git(repo, run, "diff", "--name-only", f"{base}...HEAD", "--no-renames")
    if diff.returncode != 0:
        raise PrError(f"git diff {base[:7]}...HEAD: {diff.stderr.strip()[:200]} (сначала git fetch origin)")
    cage = cage_files(diff.stdout.splitlines())
    said = body.split("## Cage", 1)[1].split("\n## ", 1)[0].strip()
    if cage and SAYS_NO.match(said):
        raise PrError(f"в ## Cage написано «нет», а PR трогает клетку: {', '.join(cage)}")
    return branch, cage


def push_command(branch: str, title: str, body: str, target: str = "main") -> List[str]:
    desc = base64.b64encode(body.encode("utf-8")).decode("ascii")
    return ["git", "push", "origin", f"HEAD:refs/for/{target}", "-o", f"topic={branch}", "-o", f"title={title}",
            "-o", DESC_PREFIX + desc]


def pr_url(output: str) -> Optional[str]:
    m = URL_RE.search(output)
    return m.group(0) if m else None


def main(argv=None, run=subprocess.run, out=None, err=None) -> int:
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = argparse.ArgumentParser(prog="kd-pr", add_help=True)
    parser.add_argument("project"); parser.add_argument("card")
    parser.add_argument("--title", required=True); parser.add_argument("--body-file", required=True)
    parser.add_argument("--target")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else 2  # --help — 0, an argument error — 2
    if not CARD_RE.fullmatch(args.card):
        print("карточка — KD-<n>", file=err)
        return 2
    try:
        repo = repos.get(args.project, REPOS)
        target = target_of(repo, args.target)  # before any git call
        repo = repo._replace(dir=tree_of(repo, args.card, run))
        with open(args.body_file, encoding="utf-8") as f:
            body = f.read()
        branch, cage = check(repo, args.card, args.title, body, run, target=target)
    except (repos.RepoError, PrError, OSError, UnicodeDecodeError) as exc:
        print(f"kd-pr: {exc}", file=err)
        return 1
    line, required = cage_line(cage), repos.required(repo)
    # the computed cage and the passport's checks outrank the agent's ## Cage and ## Checks sections
    body = body.rstrip("\n") + "\n\n" + line + "\n" + checks_line(required) + "\n"
    cmd = ["git", "-C", repo.dir] + push_command(branch, draft_title(args.title, required), body, target)[1:]
    if any(len(a.encode("utf-8")) > PUSH_OPTION_MAX for a in cmd):
        print("kd-pr: тело PR вместе со строкой клетки длиннее, чем git передаёт в push-опции (~48 КБ) — сократи тело",
              file=err)
        return 1
    try:
        proc = run(cmd, capture_output=True, encoding="utf-8", errors="replace", timeout=PUSH_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"kd-pr: push не завершился ({exc.__class__.__name__}) — проверь kd-forge prs {args.project}", file=err)
        return 3
    text = (proc.stdout or "") + (proc.stderr or "")
    if proc.returncode != 0:
        print(f"kd-pr: push отклонён:\n{text.strip()[-1500:]}", file=err)
        return 3
    url = pr_url(text)
    print(f"PR: {url}" if url else f"PR отправлен (тема {branch}); ссылку покажет kd-forge prs {args.project}", file=out)
    print(f"{line} — перенеси эту строку в сообщение владельцу", file=out)
    number = re.search(r"/pulls/([0-9]+)", url or "")
    publish = f"kd-forge publish {args.project} {number.group(1) if number else '<N>'}"
    if required:
        print(f"PR — черновик в {target}: обязательные проверки ({', '.join(required)}) ещё не зелёные; после зелёных — "
              f"{publish}, PR уйдёт владельцу в кабинет", file=out)
    else:
        print(f"Дальше: {publish} — PR уйдёт владельцу в кабинет", file=out)
    return 0
