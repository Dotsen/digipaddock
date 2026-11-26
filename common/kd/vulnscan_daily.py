"""kd-vulnscan — the daily scan of the projects' locked dependencies for known vulnerabilities (digipaddock-vulnscan.timer).

Root runs it (``digipaddock-vulnscan.service``) as the orchestrator of three steps, none of them with more than it needs:

1. the OSV database is downloaded in a child unit as the ci user with network, writing only the database's directory
   (``vuln_scan.py update``); a failure is in this unit's journal and makes the run fail (OnFailure tells people);
2. the base branch of every project of /etc/digipaddock/repos.json is fetched by root with the read key of the repo
   user (as dev.check fetches), and its lock files (vuln_scan.is_lock) are scanned in a child unit as the ci user with
   no network (``vuln_scan.py scan``);
3. every new finding — a project, a vulnerability and a package not seen before and not under a valid exception of the
   project's osv-scanner.toml — becomes one Inbox card of the project for ASSIGNEE, through the cabinet's root
   ``card.create``: the ci user never talks to the board. The seen findings are kept in STATE, so a run again makes no
   second card; a finding whose card could not be made is tried again the next day.

A database older than vuln_scan.MAX_AGE_S after the download gives one card that it is not updated (again only after
it was fresh in between). At most MAX_CARDS cards a run: the rest come the following days. Each child unit is the ci
user in a pid namespace of its own (``ci_unit``): a check's tests, of the same user, cannot reach the download's
directory through /proc.
"""

import datetime
import json
import os
import shutil
import subprocess
import sys
import time
from typing import Callable, Optional

from kd import ci_unit, devcheck_runner, naming, repo_sync
from kd import vuln_scan as vs

STATE = "/var/lib/digipaddock/vulnscan/state.json"  # root 0600: the findings seen and the open «not updated» card
WORK = "/var/lib/digipaddock/devcheck/vulnscan"  # root 0755: git/<project> (root's), scan/ (read by the ci user)
DEPLOYED = devcheck_runner.DEPLOYED
UPDATE_MAX_S = 1800
SCAN_MAX_S = 900
GIT_TIMEOUT = 600
MAX_CARDS = 20
ASSIGNEE = "@dev"  # the developer agent type: the findings are code work in any project
TITLE_MAX = 100
UNIT = "digipaddock-vulnscan-"  # + db | scan-<project>
TEXTS = {
    "ru": {
        "title": "Уязвимость {id} в {package} {version} ({project})",
        "body": (
            "Ежедневная проверка зависимостей нашла известную уязвимость в ветке `{base}` проекта `{project}`.\n\n"
            "- Уязвимость: `{id}`{aliases}\n- Пакет: `{package}` {version} ({ecosystem})\n"
            "- Исправлено в: {fixed}\n- Файл: `{path}`\n- Описание: {summary}\n"
            "- https://osv.dev/vulnerability/{id}\n\n"
            "Обновите пакет или, если уязвимость не касается проекта, добавьте исключение в `osv-scanner.toml` "
            "с `reason` и `ignoreUntil` (не дальше {days} дней)."
        ),
        "no_fix": "исправленной версии нет",
        "also": " (также {})",
        "none": "нет",
        "stale_title": "База уязвимостей OSV не обновляется",
        "stale_body": (
            "Ежедневная служба `digipaddock-vulnscan` не смогла обновить базу уязвимостей: {age}. "
            "Пока база старше {hours} ч, шаг `vulns` проверки `devcheck/unit` красный у каждого PR.\n\n"
            "Причина — в журнале службы `digipaddock-vulnscan.service`."
        ),
    },
    "en": {
        "title": "Vulnerability {id} in {package} {version} ({project})",
        "body": (
            "The daily dependency scan found a known vulnerability on the `{base}` branch of `{project}`.\n\n"
            "- Vulnerability: `{id}`{aliases}\n- Package: `{package}` {version} ({ecosystem})\n"
            "- Fixed in: {fixed}\n- File: `{path}`\n- Summary: {summary}\n"
            "- https://osv.dev/vulnerability/{id}\n\n"
            "Update the package or, if the vulnerability does not affect the project, add an exception to "
            "`osv-scanner.toml` with `reason` and `ignoreUntil` (at most {days} days ahead)."
        ),
        "no_fix": "no fixed version",
        "also": " (also {})",
        "none": "none",
        "stale_title": "The OSV vulnerability database is not updated",
        "stale_body": (
            "The daily service `digipaddock-vulnscan` could not update the vulnerability database: {age}. "
            "While it is older than {hours} h, the `vulns` step of `devcheck/unit` is red for every PR.\n\n"
            "The reason is in the journal of `digipaddock-vulnscan.service`."
        ),
    },
}


def _key(project: str, finding: dict, vid: str) -> str:
    return f"{project}|{finding['ecosystem']}|{finding['package'].lower()}|{vid}"


def seen(state: dict, project: str, finding: dict) -> bool:
    keys = state.get("seen", {})
    return any(_key(project, finding, vid) in keys for vid in finding["ids"])


def mark(state: dict, project: str, finding: dict, card: str, at: str) -> None:
    for vid in finding["ids"]:
        state.setdefault("seen", {})[_key(project, finding, vid)] = {
            "card": card,
            "at": at,
        }


def load_state(path: str = STATE) -> dict:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {"seen": {}}
    if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
        raise ValueError(f"{path}: not a state of kd-vulnscan")
    return data


def save_state(state: dict, path: str = STATE) -> None:
    tmp = f"{path}.new"
    fd = os.open(
        tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600
    )
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(state, handle, sort_keys=True, indent=1)
    os.replace(tmp, path)


def card_text(lang: str, project: str, base: str, f: dict) -> tuple:
    t = TEXTS[lang]
    others = [i for i in f["ids"] if i != f["id"]]
    values = {
        "id": f["id"],
        "package": f["package"],
        "version": f["version"],
        "project": project,
        "base": base,
        "ecosystem": f["ecosystem"],
        "path": f["path"],
        "days": vs.IGNORE_MAX_DAYS,
        "aliases": t["also"].format(", ".join(f"`{i}`" for i in others[:6]))
        if others
        else "",
        "fixed": ", ".join(f["fixed"]) if f["fixed"] else t["no_fix"],
        "summary": f["summary"] or t["none"],
    }
    return t["title"].format(**values)[:TITLE_MAX], t["body"].format(**values)


class Daily:
    def __init__(
        self,
        *,
        units: Callable,
        git: Callable,
        client,
        repos: dict,
        host: dict,
        ci_user: str,
        state_path: str = STATE,
        work: str = WORK,
        osv_dir: str = vs.OSV_DIR,
        deployed: str = DEPLOYED,
        now: Callable = time.time,
        log: Callable = print,
        remove=shutil.rmtree,
    ):
        self.units, self.git, self.client, self.repos, self.host = (
            units,
            git,
            client,
            repos,
            host,
        )
        self.ci_user, self.state_path, self.work, self.osv_dir, self.deployed = (
            ci_user,
            state_path,
            work,
            osv_dir,
            deployed,
        )
        self.now, self.log, self.remove = now, log, remove
        self.lang = (
            host.get("owner_language") if host.get("owner_language") in TEXTS else "en"
        )
        self.made = 0

    # --- the children ----------------------------------------------------------------------------------------------
    def _unit(
        self, name: str, command: list, *, network: bool, writable: str, limit: int
    ) -> list:
        """A child unit: the ci user in a pid namespace of its own (``ci_unit``), the network only for the download."""
        props = [
            *(() if network else ("PrivateNetwork=yes",)),
            "PrivateTmp=yes",
            "ProtectSystem=strict",
            "ProtectHome=yes",
            *((f"ReadWritePaths={writable}",) if writable else ()),
            "MemoryMax=2G",
            f"RuntimeMaxSec={limit}",
            "InaccessiblePaths="
            + " ".join("-" + p for p in devcheck_runner.INACCESSIBLE),
        ]
        script = os.path.join(self.deployed, "common", "kd", "vuln_scan.py")
        return ci_unit.argv(
            UNIT + name,
            props,
            ["/usr/bin/python3", "-I", script, *command],
            user=self.ci_user,
            env=["HOME=/tmp"],
        )

    def update(self) -> bool:
        rc, out = self.units(
            self._unit(
                "db",
                ["update"],
                network=True,
                writable=self.osv_dir,
                limit=UPDATE_MAX_S,
            )
        )
        for line in out.strip().splitlines()[-20:]:
            self.log(line)
        if rc != 0:
            self.log(f"kd-vulnscan: the OSV database update failed (exit {rc})")
        return rc == 0

    # --- the projects ----------------------------------------------------------------------------------------------
    def _git(self, argv: list) -> str:
        rc, out = self.git(argv)
        if rc != 0:
            raise vs.ScanError(
                f"git {argv[2] if argv[:1] == ['-C'] else argv[0]}: exit {rc}: {out.strip()[-300:]}"
            )
        return out

    def fetch(self, project: str) -> dict:
        """``{path: text}`` of the base branch's lock files and its osv-scanner.toml ("" if none)."""
        repo, base = self.repos[project]
        path = os.path.join(self.work, "git", project)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        os.chmod(
            os.path.dirname(path), 0o700
        )  # the projects' code is root's; only scan/ is the ci user's to read
        if not os.path.isdir(os.path.join(path, ".git")):
            self._git(["init", "-q", path])
        url = self.host["forge_ssh_url"].replace("{repo}", repo)
        self._git(["-C", path, "fetch", "-q", "--no-tags", url, f"refs/heads/{base}"])
        names = self._git(
            ["-C", path, "ls-tree", "-r", "-z", "--name-only", "FETCH_HEAD"]
        ).split("\0")
        files = {}
        for name in names:
            leaf = name.rsplit("/", 1)[-1]
            if name and (leaf in vs.LOCK_NAMES or vs._REQUIREMENTS.fullmatch(leaf)):
                text = self._git(["-C", path, "show", f"FETCH_HEAD:{name}"])
                if vs.is_lock(name, text):
                    files[name] = text
        rc, config = self.git(["-C", path, "show", f"FETCH_HEAD:{vs.CONFIG}"])
        return {"locks": files, "config": config if rc == 0 else ""}

    def scan(self, project: str, locks: dict) -> dict:
        root = os.path.join(self.work, "scan", project)
        if os.path.lexists(root):
            self.remove(root)
        os.makedirs(root, mode=0o755)
        vs.write_tree(root, locks)
        for top, dirs, files in os.walk(
            root
        ):  # root's umask: the ci user reads the copy
            for name in dirs:
                os.chmod(os.path.join(top, name), 0o755)
            for name in files:
                os.chmod(os.path.join(top, name), 0o644)
        rc, out = self.units(
            self._unit(
                f"scan-{project}",
                ["scan", root],
                network=False,
                writable="",
                limit=SCAN_MAX_S,
            )
        )
        if rc != 0:
            raise vs.ScanError(f"scan exit {rc}: {out.strip()[-500:]}")
        try:
            return json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            raise vs.ScanError(f"scan answered no JSON: {out.strip()[-300:]}") from None

    def card(self, project: str, title: str, body: str) -> str:
        from kd.cabinet.client import CabinetError

        try:
            made = self.client.call(
                {
                    "op": "card.create",
                    "project": project,
                    # the developer's, whoever the project's route names (a project of the owner's routes to him)
                    "assignee": ASSIGNEE,
                    "title": title,
                    "description": body,
                }
            )
        except (CabinetError, OSError, ValueError) as exc:
            self.log(
                f"kd-vulnscan: {project}: card «{title}» not made ({getattr(exc, 'kind', '')}: {exc})"
            )
            return ""
        self.made += 1
        return made["card"]["id"]

    def project(self, state: dict, project: str) -> int:
        """The new findings of one project as cards; the number of findings still without a card."""
        got = self.fetch(project)
        if not got["locks"]:
            self.log(f"kd-vulnscan: {project}: no lock files")
            return 0
        result = self.scan(project, got["locks"])
        today = datetime.datetime.fromtimestamp(
            self.now(), datetime.timezone.utc
        ).date()
        entries, problems = vs.load_ignores(got["config"], today)
        for problem in problems:
            self.log(f"kd-vulnscan: {project}: {problem}")
        left = 0
        base = self.repos[project][1]
        found = result.get("findings", [])
        self.log(
            f"kd-vulnscan: {project}: {len(got['locks'])} lock files, {len(found)} findings"
        )
        for f in found:
            entry = vs.ignore_of(f, entries)
            if (entry is not None and entry["until"] >= today) or seen(
                state, project, f
            ):
                continue
            if self.made >= MAX_CARDS:
                left += 1
                continue
            title, body = card_text(self.lang, project, base, f)
            card = self.card(project, title, body)
            if card:
                mark(
                    state,
                    project,
                    f,
                    card,
                    datetime.datetime.fromtimestamp(
                        self.now(), datetime.timezone.utc
                    ).isoformat(timespec="seconds"),
                )
                self.log(
                    f"kd-vulnscan: {project}: {f['id']} {f['package']} {f['version']} -> {card}"
                )
                save_state(state, self.state_path)
            else:
                left += 1
        return left

    def stale(self, state: dict) -> None:
        """One card while the database is missing or older than its limit; forgotten once it is fresh again."""
        _db, updated, age = vs.db_state(self.osv_dir, self.now())
        if updated is not None and age <= vs.MAX_AGE_S:
            if state.pop("stale", None):
                save_state(state, self.state_path)
            return
        self.log(f"kd-vulnscan: {vs.describe_age(updated, age)}")
        if state.get("stale"):
            return
        project = (
            "digipaddock"
            if "digipaddock" in self.repos
            else next(iter(sorted(self.repos)), "")
        )
        if not project:
            return
        t = TEXTS[self.lang]
        card = self.card(
            project,
            t["stale_title"],
            t["stale_body"].format(
                age=vs.describe_age(updated, age), hours=vs.MAX_AGE_S // 3600
            ),
        )
        if card:
            state["stale"] = card
            save_state(state, self.state_path)

    def run(self) -> int:
        state = load_state(self.state_path)
        ok = self.update()
        self.stale(state)
        failed = left = 0
        for project in sorted(self.repos):
            try:
                left += self.project(state, project)
            except (vs.ScanError, OSError, KeyError) as exc:
                failed += 1
                self.log(
                    f"kd-vulnscan: {project}: not scanned ({exc.__class__.__name__}: {exc})"
                )
        if left:
            self.log(
                f"kd-vulnscan: {left} findings wait for their cards (at most {MAX_CARDS} a run)"
            )
        return 0 if ok and not failed else 1


def _capture(
    argv: list,
    timeout: Optional[int] = None,
    env=None,
    limit: int = devcheck_runner.OUTPUT_MAX,
) -> tuple:
    """(exit code, output); an output past ``limit`` bytes is an error, never a cut file."""
    try:
        proc = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 124, f"{argv[0]}: timed out after {timeout} s"
    except OSError as exc:
        return 127, f"{argv[0]}: {exc.__class__.__name__}"
    if len(proc.stdout) > limit:
        return 125, f"{argv[0]}: more than {limit} bytes of output"
    out = proc.stdout.decode("utf-8", "replace")
    return proc.returncode, out if proc.returncode == 0 else out + proc.stderr.decode(
        "utf-8", "replace"
    )[-2000:]


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["run"]:
        print("usage: kd-vulnscan run", file=sys.stderr)
        return 2
    absent = ci_unit.missing()
    if absent:
        print(
            f"kd-vulnscan: no {', '.join(absent)} (util-linux): the ci units cannot be isolated",
            file=sys.stderr,
        )
        return 1
    import pwd

    from kd.cabinet import client as cabinet_client

    try:
        host, repos = naming.load_host(), repo_sync.load_repos()
        key = os.path.join(
            pwd.getpwnam(naming.service_user("repo")).pw_dir,
            ".ssh",
            "repo_sync_ed25519",
        )
        ci_user = naming.service_user("ci")
    except (naming.NamingError, repo_sync.SyncError, KeyError, ValueError) as exc:
        print(f"kd-vulnscan: {exc}", file=sys.stderr)
        return 1
    env = repo_sync.git_env(os.environ, key=key)
    daily = Daily(
        units=lambda a: _capture(a, timeout=UPDATE_MAX_S + 60),
        git=lambda a: _capture(
            ["git", *a], timeout=GIT_TIMEOUT, env=env, limit=vs.LOCK_MAX
        ),
        client=cabinet_client.Client(),
        repos=repos,
        host=host,
        ci_user=ci_user,
        log=lambda line: print(line, flush=True),
    )
    try:
        return daily.run()
    except (OSError, ValueError) as exc:
        print(f"kd-vulnscan: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
