"""bootstrap.sh: the factory host from an empty Ubuntu, the installers in order.

``bootstrap.sh <phase>|all [--plan] [--overlay <dir> --code <dir>]`` as root. Phases, in order: preflight (the OS
packages, the overlay, root's key to the forge), tree (the first release tree, by kd-release --bootstrap), base
(stage_setup.sh, the session host), hindsight, agents (new_agent.sh per role, then the board's projects — their
routes name registered agents), components (what their installers make), secrets (their directories come with the
installers above), keys (the public keys and where they go), logins (Claude Code per user, each role on the session
host with its slots, then what needs a login: the worker slots, kd-review's signer), check (every check again, the
release's health; nothing installed). ``all`` runs them in order and stops at the first that does not end ok.
``starter`` is not one of ``all``: run alone, once the cabinet is up, it puts a minimal start on the board — a project
of the owner's with a few cards to read and tick (``kd.starter``, ``common/install/starter.json``).

A step is a check and, when the check fails, its install commands, then the check again. A step without install
commands is the owner's (a key in Forgejo, a secret, a Claude login): its line says what to do, then the same phase
again; an installer that exits 3 stopped at such a step itself and said which. What to install comes from the
overlay: host/install.json (roles and slots, components, the pipeline's mode, Hindsight) and host/projects.json (the
board's projects and their repositories); before the first tree from --overlay, after it from the release tree.
Exit 0 all ok, 3 the owner's step, 1 an installer failed, 2 usage.
"""

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, NamedTuple, Tuple

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from kd import install_catalog, install_spec, naming  # noqa: E402

OPT = "/opt/digipaddock"
PY = "/usr/bin/python3"
TREE = Path(__file__).resolve().parents[2]
DEPLOY_KEY = "/root/.ssh/forgejo_deploy"
PHASES = ("preflight", "tree", "base", "hindsight", "agents", "components", "secrets", "keys", "logins", "check")
OPTIONAL = ("starter",)  # run by name only, never by all
USAGE = ("usage: bootstrap.sh <phase>|all [--plan] [--overlay <dir> --code <dir>]; phases: "
         + ", ".join(PHASES) + "; optional, on its own: " + ", ".join(OPTIONAL))
# component -> (installer, its check, its install) — the factory's own checks (factory.json uses the same scripts);
# review's token and signer are the owner's (logins), so the components phase checks only what its install makes
COMPONENTS = {
    "review": ("agents/dev/install/review_setup.sh", "check installed", "install"),
    "repo-attach": ("agents/dev/install/repo_attach_setup.sh", "check", "install"),
    "transcribe": ("common/install/transcribe_setup.sh", "check", "install"),
    "e2e-runtime": ("common/install/e2e_runtime_setup.sh", "check", "install"),
    "devcheck": ("common/install/devcheck_setup.sh", "check", "install"),
    "mirror": ("common/install/mirror_setup.sh", "check", "install"),
    "worker-slots": ("common/install/worker_slots.sh", "--check", "--install"),
    "egress": ("common/install/egress_setup.sh", "check", "install"),
}


class Step(NamedTuple):
    phase: str
    name: str
    check: Tuple[str, ...]  # () — nothing to check: the step only says something
    apply: Tuple[Tuple[str, ...], ...] = ()  # the install commands in order; () — the owner's step
    manual: str = ""  # what the owner does when the check fails (or what an info step says)
    show: Tuple[str, ...] = ()  # files printed with the manual line (public keys)


class Where(NamedTuple):
    code: str  # the code tree preflight and tree run from (the release tree once there is one)
    overlay: str  # the overlay before the first tree; "" after it (the release tree is the pair)
    scratch: str  # this run's directory: the registry kd-release --repos takes


def _capabilities(role) -> set:
    try:
        with open(TREE / "agents" / role / "agent.json", encoding="utf-8") as f:
            return set(json.load(f).get("capabilities") or ())
    except (OSError, ValueError, AttributeError):
        return set()


def _release_repos(projects) -> List[str]:
    return [p.repo["repo"] for p in projects.values() if p.repo and p.repo.get("release")]


def steps(install, projects, where, *, host, opt=OPT) -> Dict[str, List[Step]]:
    """Every phase's steps for this install: what install.json chose and nothing else."""
    sh = lambda path, *args: ("bash", f"{opt}/{path}", *args)  # noqa: E731
    probe = lambda *args: (PY, "-I", f"{opt}/common/kd/bootstrap.py", "--probe", *args)  # noqa: E731
    prefix, roles = install.naming["prefix"], sorted(install.roles)
    known = (f"{where.overlay}/common/install/forgejo_known_hosts" if where.overlay
             else f"{opt}/common/install/forgejo_known_hosts")
    ssh = (f"ssh -i {DEPLOY_KEY} -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile={known} "
           "-o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o UpdateHostKeys=no")
    hs = ("--hindsight",) if install.hindsight else ()
    prereq = f"{where.code}/common/install/prereq.sh"
    code_repos = _release_repos(projects)
    out = {phase: [] for phase in PHASES + OPTIONAL}

    out["preflight"].append(Step(
        "preflight", "OS packages", ("bash", prereq, "check", *hs), (("bash", prereq, "install", *hs),),
        "Node.js and PostgreSQL come from their vendors' repositories — the prereq commands above (root does not add "
        "a third-party apt repository itself)"))
    if where.overlay:
        out["preflight"].append(Step(
            "preflight", "overlay", (PY, "-I", f"{where.code}/common/kd/overlay.py", "check", where.overlay), (),
            "overlay is not valid — the lines above: fix the overlay and run again"))
    out["preflight"].append(Step(
        "preflight", "root's key for Forgejo", ("test", "-f", DEPLOY_KEY),
        (("install", "-d", "-m", "0700", "/root/.ssh"),
         ("ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "digipaddock-release", "-f", DEPLOY_KEY))))
    for repo in code_repos:
        out["preflight"].append(Step(
            "preflight", f"read {repo}",
            ("env", f"GIT_SSH_COMMAND={ssh}", "git", "ls-remote", "--exit-code",
             host["forge_ssh_url"].replace("{repo}", repo), "refs/heads/main"), (),
            f"Forgejo → {repo} → Settings → Deploy Keys → Add Deploy Key, without write access, key:",
            (DEPLOY_KEY + ".pub",)))

    release = install_spec.release_project(projects) or ""
    out["tree"].append(Step(
        "tree", "first tree", ("test", "-f", f"{opt}/REVISION"),
        (("env", f"KD_HOST_JSON={where.overlay}/host/host.json", PY, "-I", f"{where.code}/common/bin/kd-release",
          "--bootstrap", release, "--repos", f"{where.scratch}/repos.json", "--known-hosts", known),)
        if where.overlay else (),
        "there is no tree — bootstrap.sh tree --overlay <dir> --code <dir>"))

    stage = sh("common/install/stage_setup.sh")
    out["base"].append(Step(
        "base", "foundation", stage + ("--check",),
        (stage + ("--prefix", prefix, "--commands", install.naming["commands"], "--cards", install.naming["cards"]),)))
    out["base"].append(Step("base", "session host", sh("common/install/session_host_setup.sh", "check"),
                            (sh("common/install/session_host_setup.sh", "install"),)))

    if install.hindsight:
        out["hindsight"].append(Step("hindsight", "Hindsight server", sh("common/install/hindsight_setup.sh", "check"),
                                     (sh("common/install/hindsight_setup.sh", "install"),)))
        # the base user's client comes with stage_setup.sh once the server's env is there (the agents' with
        # add_user.sh); rerun only without it: stage_setup.sh installs the cabinet again
        base = naming.base_user(prefix)
        out["hindsight"].append(Step("hindsight", f"Hindsight client {base}",
                                     ("test", "-f", f"{naming.home(base)}/.hindsight/coding-agent.json"), (stage,)))

    # --verify: the installed role is what this code installs (--check only says it could be installed)
    for role in roles:
        out["agents"].append(Step("agents", f"agent {role}", sh("common/install/new_agent.sh", "--verify", role),
                                  (sh("common/install/new_agent.sh", "--install", role),)))
    board = f"{opt}/host/projects.json"
    out["agents"].append(Step("agents", "board projects", probe("projects", "check", board),
                              (probe("projects", "apply", board),)))

    def component(name, phase):
        script, check, apply = COMPONENTS[name]
        return Step(phase, f"component {name}", sh(script, *check.split()), (sh(script, apply),))

    # the worker slots are enabled only with their agent's Claude login: they come with the logins
    out["components"].extend(component(c, "components") for c in install_spec.COMPONENTS
                             if c in install.components and c != "worker-slots")
    out["components"].append(Step(
        "components", f"pipeline {install.pipeline}", probe("pipeline", install.pipeline),
        (sh("common/install/pipeline_setup.sh", "enable" if install.pipeline == "enforce" else "disable"),)))

    chosen = set(install.roles) | set(install.components)
    secret = naming.command("kd-secret", install.naming["commands"])
    for s in install_catalog.secrets(host=host, prefix=prefix):
        if chosen & set(s.wanted_by):
            out["secrets"].append(Step("secrets", f"secret {s.name}", probe("secret", s.name), (),
                                       f"{secret} put {s.name} — {s.how}"))

    forge = " and ".join(code_repos) or "the release repositories"
    out["keys"].append(Step("keys", "root's key", (), (),
                            f"Forgejo → {forge} → Deploy Keys, read-only:", (DEPLOY_KEY + ".pub",)))
    if any("repo.read" in _capabilities(r) for r in roles):
        user = naming.service_user("repo", prefix)
        out["keys"].append(Step("keys", "repository copies' key", (), (),
                                "Forgejo → every project with repo in projects.json → Deploy Keys, read-only:",
                                (f"{naming.home(user)}/.ssh/repo_sync_ed25519.pub",)))
    for role in roles:
        if "git.develop" in _capabilities(role):
            user = naming.agent_user(role, prefix)
            out["keys"].append(Step("keys", f"{role}'s key", (), (),
                                    f"Forgejo → account {host['forge_dev_user']} → Settings → SSH Keys:",
                                    (f"{naming.home(user)}/.ssh/{role}_ed25519.pub",)))
    # the mirror's key is not made here: its installer asks for the private part with write access (exit 3)

    # the agents' Claude login is the host's token (the secret claude-token of the phase before), not a
    # /login per user that dies in about four weeks
    for role in roles:
        slots = str(install.roles[role])
        out["logins"].append(Step(
            "logins", f"{role} in the session host, {slots} slots", probe("hosted", role, slots),
            (sh("common/install/session_host_setup.sh", "enable", role, "--slots", slots),)))
    if "worker-slots" in install.components:
        out["logins"].append(component("worker-slots", "logins"))
    if "review" in install.components:
        cabinet, review = (naming.command(c, install.naming["commands"]) for c in ("kd-cabinet", "kd-review"))
        out["logins"].append(Step(
            "logins", "kd-review signer", sh("agents/dev/install/review_setup.sh", "check"), (),
            f"the cabinet's first owner — {cabinet} user add <id> --name <name> --role owner, the login code — {cabinet} "
            f"user code <id>; sign in to the cabinet and add a passkey in Settings; then {review} signers add <id> "
            f"(lists the keys) and {review} signers add <id> --credential <key>"))

    out["starter"].append(Step("starter", "starter cards", probe("starter", "check", f"{opt}/host/install.json"),
                               (probe("starter", "apply", f"{opt}/host/install.json"),)))

    # check: the same checks from the release tree, then what only a whole host can show; nothing installed
    again = steps(install, projects, Where(opt, "", where.scratch), host=host, opt=opt) if where.code != opt else out
    for phase in PHASES:
        if phase not in ("tree", "keys", "check"):
            out["check"].extend(s._replace(phase="check", apply=()) for s in again[phase])
    out["check"].append(Step("check", "release tree overlay", (PY, "-I", f"{opt}/common/kd/overlay.py", "check", opt)))
    out["check"].append(Step("check", "release health", (PY, "-I", f"{opt}/common/bin/kd-cabinet", "health")))
    return out


_SHOW_LIMIT = 8192  # a public key is a line; a file larger than this is not one


def _read_plain(path) -> str:
    """The text of a regular file, opened without following a link: root prints files from a user's home, and that
    user could point a link at any file of root's. ``OSError`` — not there; ``ValueError`` — not a plain key file."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if os.path.islink(path):  # O_NOFOLLOW refuses with ELOOP: a link is not "not there yet"
            raise ValueError("a symlink") from exc
        raise
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):  # before fdopen: it refuses a directory itself, as an OSError
            raise ValueError("not a regular file")
        handle = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        if info.st_size > _SHOW_LIMIT:
            raise ValueError("too large for a key")
        return handle.read(_SHOW_LIMIT + 1).decode("utf-8")


def _show(paths, out) -> None:
    for path in paths:
        try:
            out.write(_read_plain(path).rstrip("\n") + "\n")
        except OSError:
            out.write(f"    {path} — not there yet, the install makes it\n")
        except ValueError as exc:  # also an undecodable file
            out.write(f"    {path} — not shown: {exc}, look at it by hand\n")


def _again(phase) -> str:
    return f"bootstrap.sh {phase}"


def run_phase(phase, phase_steps, *, run, out, err, plan=False, again=_again, checked=True) -> int:
    """One phase. A failing check: plan — said; else its install, then the check again. check — every check runs.
    ``again(phase)`` — the command that runs the phase again, said with a stop. ``checked=False`` — a plan before
    the first tree: the phase's checks would run from a tree that is not there, so every step is said unchecked."""
    out.write(f"== {phase}\n")
    if not checked and phase == "check":
        out.write("  after the install: every phase's checks again and the release health\n")
        return 0
    failed = 0
    for step in phase_steps:
        if not step.check:
            out.write(f"  {step.name}: {step.manual}\n")
            _show(step.show, out)
            continue
        if checked and run(step.check, quiet=True) == 0:
            out.write(f"  ok          {step.name}\n")
            continue
        if phase == "check":
            err.write(f"  failed      {step.name}\n")
            failed = 1
            continue
        if plan:
            out.write(f"  to install  {step.name}\n" if step.apply else f"  manual      {step.name}: {step.manual}\n")
            continue
        if step.apply:
            out.write(f"  installing  {step.name}\n")
            for cmd in step.apply:
                rc = run(cmd)
                if rc == 3:  # the installer stopped at the owner's step itself and said which
                    err.write(f"bootstrap: owner's step — {step.name}: the installer's lines above\n"
                              f"then again: {again(phase)}\n")
                    return 3
                if rc != 0:
                    err.write(f"bootstrap: failure — {step.name}: the installer's lines above; once fixed — "
                              f"{again(phase)}\n")
                    return 1
            if run(step.check, quiet=True) == 0:
                out.write(f"  installed   {step.name}\n")
                continue
        err.write(f"bootstrap: owner's step — {step.name}: {step.manual or 'the check failed, the lines above'}\n")
        _show(step.show, err)
        err.write(f"then again: {again(phase)}\n")
        return 3
    return 0 if plan else failed  # a plan only says what fails


def run_phases(phases, by_phase, *, run, out, err, plan=False, again=_again, unchecked=()) -> int:
    for phase in phases:
        rc = run_phase(phase, by_phase.get(phase, ()), run=run, out=out, err=err, plan=plan, again=again,
                       checked=phase not in unchecked)
        if rc != 0:
            return rc
    return 0


def projects(action, path, *, client=None, err=None) -> int:
    """The board's projects of projects.json in the cabinet (root): check — 1 when one is missing or differs;
    apply — the missing created, the differing updated. A project only the cabinet has stays as it is."""
    from kd.cabinet import client as cabinet_client  # noqa: PLC0415

    err = err or sys.stderr
    wanted = install_spec.load_projects(path)
    client = client or cabinet_client.Client()
    try:
        have = {p["id"]: p for p in cabinet_client.projects(client)}
        todo = []
        for p in wanted.values():
            fields = {"name": p.name, "route": p.route, "memory_bank": p.memory_bank, "done_by": p.done_by}
            row = have.get(p.id)
            diff = fields if row is None else {k: v for k, v in fields.items() if row.get(k, "") != v}
            if diff:
                todo.append((p, row is None, diff))
        if action == "check":
            for p, missing, diff in todo:
                err.write(f"project {p.id}: " + ("not in the cabinet\n" if missing else
                                               "differs: " + ", ".join(sorted(diff)) + "\n"))
            return 1 if todo else 0
        for p, missing, diff in todo:
            if missing:
                client.call({"op": "project.create", "id": p.id, "name": p.name, "route": p.route})
                diff = {k: v for k, v in diff.items() if k in ("memory_bank", "done_by") and v}
            if diff:
                client.call({"op": "project.update", "id": p.id, **diff})
    except cabinet_client.CabinetError as exc:
        err.write(f"cabinet: {exc}\n")
        return 1
    return 0


def probe(argv, *, out, err) -> int:
    """The checks a command cannot say by its exit code alone, run by the steps themselves on the host."""
    if argv[:1] == ["secret"] and len(argv) == 2:
        from kd import host_secrets  # noqa: PLC0415

        row = host_secrets.status(install_catalog.secrets(names=[argv[1]]))[0]
        if row["state"] == "ok":
            return 0
        err.write(f"{row['path']}: {row['detail']}\n")
        return 1
    if argv[:1] == ["hosted"] and len(argv) == 3:
        from kd import session_config  # noqa: PLC0415

        return 0 if session_config.load().slots(argv[1]) == int(argv[2]) else 1
    if argv[:1] == ["pipeline"] and len(argv) == 2:
        from kd import pipeline_config  # noqa: PLC0415

        return 0 if pipeline_config.load().mode == argv[1] else 1
    if argv[:1] == ["projects"] and len(argv) == 3 and argv[1] in ("check", "apply"):
        return projects(argv[1], argv[2], err=err)
    if argv[:1] == ["starter"] and len(argv) == 3 and argv[1] in ("check", "apply"):
        from kd import starter  # noqa: PLC0415

        return starter.main(argv[1], argv[2], out=out, err=err)
    err.write("usage: bootstrap.py --probe secret <name> | hosted <role> <slots> | pipeline <mode> | "
              "projects check|apply <projects.json> | starter check|apply <install.json>\n")
    return 2


def _run(cmd, quiet=False) -> int:
    """A command from / with the terminal; a passing check stays silent."""
    if not quiet:
        return subprocess.run(list(cmd), cwd="/").returncode
    p = subprocess.run(list(cmd), cwd="/", capture_output=True, text=True)
    if p.returncode != 0:
        sys.stderr.write(p.stdout + p.stderr)
    return p.returncode


def _names_of_another_host(wanted) -> str:
    """'' when the host has no names yet (its first install writes them) or has install.json's; else what differs.
    The names are set once: stage_setup.sh would keep the host's and every user of the steps would be wrong."""
    path = naming.config_path()
    if not os.path.lexists(path):
        return ""
    have = naming.load(path)
    if all(have[k] == wanted[k] for k in ("prefix", "commands", "cards")):
        return ""
    said = lambda n: ", ".join(f"{k} {n[k]}" for k in ("prefix", "commands", "cards"))  # noqa: E731
    return (f"host/install.json — naming {said(wanted)}, but the host ({path}) has {said(have)}: the first install "
            "sets the names; put the host's names in install.json")


def _parse(argv):
    if not argv or argv[0] not in PHASES + OPTIONAL + ("all",):
        return None
    opts, rest = {"--plan": False, "--overlay": "", "--code": ""}, argv[1:]
    while rest:
        if rest[0] == "--plan" and not opts["--plan"]:
            opts["--plan"], rest = True, rest[1:]
        elif rest[0] in ("--overlay", "--code") and len(rest) >= 2 and not opts[rest[0]] and rest[1]:
            opts[rest[0]], rest = os.path.abspath(rest[1]), rest[2:]
        else:
            return None
    if bool(opts["--overlay"]) != bool(opts["--code"]):
        return None
    return argv[0], opts["--plan"], opts["--overlay"], opts["--code"]


def main(argv=None, *, run=None, out=None, err=None, opt=OPT, is_root=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--probe"]:
        return probe(argv[1:], out=out, err=err)
    parsed = _parse(argv)
    if parsed is None:
        err.write(USAGE + "\n")
        return 2
    phase, plan, overlay, code = parsed
    if not (is_root or (lambda: os.geteuid() == 0))():
        err.write("root is required\n")
        return 1
    first = not os.path.exists(os.path.join(opt, "REVISION"))
    phases = PHASES if phase == "all" else (phase,)
    if first and not overlay and phase in ("preflight", "tree", "all"):
        err.write(USAGE + "\nthere is no release tree yet: the first tree needs --overlay <overlay dir> and --code <code dir>\n")
        return 2
    if first and phase not in ("preflight", "tree", "all"):
        err.write(f"there is no release tree yet ({opt}/REVISION) — first bootstrap.sh tree --overlay … --code …\n")
        return 3
    if not first and overlay:
        err.write(f"--overlay and --code are not needed: the tree is there ({opt}), everything comes from it\n")
        overlay = code = ""
    root = overlay or opt
    try:
        install = install_spec.load_install(os.path.join(root, "host", "install.json"),
                                            known_roles=install_spec.known_roles(code or TREE))
        board = install_spec.load_projects(os.path.join(root, "host", "projects.json"), roles=install.roles)
        host = naming.load_host(os.path.join(root, "host", "host.json"))
        mismatch = _names_of_another_host(install.naming)
    except (install_spec.InstallSpecError, naming.NamingError, OSError, ValueError) as exc:
        err.write(f"overlay is not valid: {exc}\n")
        return 3
    if mismatch:
        err.write(f"overlay is not valid: {mismatch}\n")
        return 3
    if not install_spec.release_project(board):
        err.write("overlay is not valid: host/projects.json has no project with release (the factory's code)\n")
        return 3

    def again(p):  # before the first tree preflight and tree need the overlay and the code again
        flags = f" --overlay {overlay} --code {code}" if first and overlay and p in ("preflight", "tree") else ""
        return f"bootstrap.sh {p}{flags}"

    with tempfile.TemporaryDirectory(prefix="kd-bootstrap.") as scratch:
        if first:
            with open(os.path.join(scratch, "repos.json"), "w", encoding="utf-8") as f:
                json.dump(install_spec.repos_json(board), f, ensure_ascii=False, indent=2)
        where = Where(code or opt, overlay, scratch)
        by_phase = steps(install, board, where, host=host, opt=opt)
        # a plan before the first tree: what comes after tree runs from it — said, not checked
        unchecked = PHASES[PHASES.index("tree") + 1:] if plan and first else ()
        return run_phases(phases, by_phase, run=run or _run, out=out, err=err, plan=plan, again=again,
                          unchecked=unchecked)


if __name__ == "__main__":
    sys.exit(main())
