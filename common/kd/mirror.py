"""GitHub mirror of the public code repository: each new main goes out as a snapshot, through both privacy scanners.

Every run (digipaddock-mirror.timer, five minutes; kd-release after a healthy release) fetches its own bare mirror of
the forge. A main it has neither published nor blocked goes to GitHub as one commit: the tree of that main on top of
GitHub's main as the run reads it, by ``mirror_author`` (host.json), with the message ``Update to upstream <sha>``.
The forge's history never goes out — its commits, messages, authors and removed lines stay on the forge — so what is
scanned is what is published: the tree, its file names and the snapshot's message and identity, by the public scanner
and the running release's closed scanner. A finding blocks that SHA — one red run (OnFailure → kd-alert), then again
once a day (kd-alert's hourly cooldown may have swallowed the first) until main moves; clean — the snapshot goes out
as a fast-forward of GitHub's main (no force: a push to GitHub in between rejects it, and the next run builds on that).
A tag on the published main goes out as a plain tag of its snapshot (no forge tag message); with the running tree's
host/allowed_signers only a tag signed by one of them under its own name (kd-release's rule for a tag). A tag put on
that main later goes out on a later run, through both scanners, onto GitHub's main when that still has the tree of
this main; state.json keeps the tags of the published main already decided, so each is decided once and a run with no
new tag does not reach GitHub. A tag on an older commit stays on the forge. The mirror follows the forge's main, not the released revision: what reaches main
has the owner's Approve, released or not yet.
"""

import argparse
import calendar
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from kd import naming, privacy_scan
from kd.history_scan import history, history_text, public_history_findings

SHA_RE = re.compile(r"[0-9a-f]{40}")
MAIN = "refs/heads/main"
GITHUB_MAIN = "refs/kd-mirror/github"  # GitHub's main as this run read it (a fetch --prune of the forge drops it)
T_GIT, T_PUSH, T_SCAN = 120, 120, 60
REALERT = 24 * 3600  # a blocked main fails again once a day
_HIT = re.compile(r"(.+?):(\d+):")  # a closed scanner line: <path>:<line>:<literal>
_AUTHOR = re.compile(r"(.+?) <([^<>]+)>\Z")

RELEASE = "digipaddock"  # the release whose code repository is the public one
STATE_DIR = "/var/lib/digipaddock/github-mirror"
REPOS = "/etc/digipaddock/repos.json"
TREE = "/opt/digipaddock"  # the running release: the overlay's closed scanner and both known_hosts files
PYTHON = "/usr/bin/python3"
FORGE_KEY = "/root/.ssh/forgejo_deploy"
GITHUB_KEY = "/etc/digipaddock/mirror/github_deploy"
DISABLED = "/etc/digipaddock/mirror/disabled"  # the kill switch: honoured by the timer and by kd-release's start


class MirrorError(Exception):
    """A step that could not run (git, a scanner): nothing is pushed or recorded; exit 2."""


def _load(state_dir):
    try:
        data = json.loads((state_dir / "state.json").read_text("utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as exc:
        raise MirrorError(f"state.json unreadable ({exc.__class__.__name__})") from None
    if not isinstance(data, dict):
        raise MirrorError("state.json is not an object")
    state = {key: str(data.get(key) or "") for key in ("pushed", "blocked", "blocked_at")}
    tags = data.get("tags")  # the tags of the published main already decided (out, unsigned or blocked)
    state["tags"] = [str(tag) for tag in tags] if isinstance(tags, list) else []
    return state


def _save(state_dir, state):
    fd, tmp = tempfile.mkstemp(dir=str(state_dir), prefix=".state.json.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, str(state_dir / "state.json"))
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _git(git, args, env, timeout=T_GIT, binary=False):
    rest = args[2:] if args[0] == "--git-dir" else args
    while rest[:1] == ["-c"]:
        rest = rest[2:]
    step = next(arg for arg in rest if not arg.startswith("-") and "=" not in arg)
    try:
        p = git(["git", *args], capture_output=True, text=not binary, env=env, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MirrorError(f"git {step}: {exc.__class__.__name__}") from None
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace") if isinstance(p.stderr, bytes) else (p.stderr or "")
        raise MirrorError(f"git {step}: {err.strip()[-300:]}")
    return p.stdout


def _unpack(data, dest):
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or ".." in Path(name).parts or not (member.isfile() or member.isdir()):
                continue  # a tree kd-release accepts has no links; never follow them here either
            target = dest / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(tar.extractfile(member).read())


def _due(blocked_at, now):
    """A day since the last red run (an unreadable stamp is due)."""
    try:
        then, current = (calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")) for stamp in (blocked_at, now))
    except (TypeError, ValueError):
        return True
    return current - then >= REALERT


def run(*, state_dir, mirror_dir, remote, forge_url, scanners, author, git=subprocess.run,
        now=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), forge_env=None, github_env=None,
        disabled=DISABLED, signers=None) -> int:
    """0 — published, nothing new or disabled; 1 — a scanner found something in a new main, or main is still blocked
    a day after the last red run; 2 — an error. ``scanners(tree, records)`` returns findings; MirrorError from it
    is an error, not a block. ``author`` is the snapshot's author and committer, ``Name <email>``."""
    state_dir, mirror_dir = Path(state_dir), Path(mirror_dir)
    if Path(disabled).exists():
        print("kd-mirror: disabled")
        return 0
    try:
        name, email = _identity(author)
        state = _load(state_dir)
        bare = ["--git-dir", str(mirror_dir)]
        if mirror_dir.is_dir():
            _git(git, [*bare, "fetch", "--prune", "--quiet", "origin"], forge_env)
        else:
            _git(git, ["clone", "--mirror", "--quiet", forge_url, str(mirror_dir)], forge_env)
        head = _rev(git, bare, MAIN)
        on_head = _on_head(git, bare, head)
        if head == state["pushed"]:
            new = [ref for ref in on_head if ref not in state["tags"]]
            if not new:
                return 0
            return _late_tags(git=git, bare=bare, head=head, new=new, state=state, state_dir=state_dir,
                              scanners=scanners, author=author, remote=remote, github_env=github_env,
                              signers=signers)
        if head == state["blocked"]:
            since, stamp = state["blocked_at"], now()
            if not _due(since, stamp):
                print(f"kd-mirror: still blocked at {head[:12]}")
                return 0
            state.update(blocked_at=stamp)
            _save(state_dir, state)
            print(f"kd-mirror: privacy: still blocked at {head[:12]} since {since or '?'}; main не отправлен на GitHub",
                  file=sys.stderr)
            return 1
        refs = _tags(git, bare, head, signers, on_head)
        message = _message(head)
        findings = _scan(git, bare, head, author, refs, scanners)
        if findings:
            state.update(blocked=head, blocked_at=now())
            _save(state_dir, state)
            print(f"kd-mirror: privacy: {findings[0]}; main не отправлен на GitHub", file=sys.stderr)
            return 1
        tree_id = _rev(git, bare, head + "^{tree}")
        base = _github_main(git, bare, remote, github_env)
        if base and _rev(git, bare, base + "^{tree}") == tree_id:
            snapshot = base  # the same tree is out already: a tag, if any, goes on it
        else:
            ident = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
                     "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
            snapshot = (_git(git, [*bare, "-c", "commit.gpgsign=false", "commit-tree", tree_id,
                                   *(["-p", base] if base else []), "-m", message], ident) or "").strip()
            if not SHA_RE.fullmatch(snapshot):
                raise MirrorError("git commit-tree: no commit made")
        if snapshot != base or refs:
            # atomic: a rejected tag rejects main too; no force — GitHub's main moved meanwhile rejects the push
            _git(git, [*bare, "push", "--atomic", "--quiet", remote, f"{snapshot}:{MAIN}",
                       *(f"{snapshot}:{ref}" for ref in refs)], github_env, timeout=T_PUSH)
        state.update(pushed=head, blocked="", blocked_at="", tags=on_head)
        _save(state_dir, state)
    except MirrorError as exc:
        print(f"kd-mirror: {exc}", file=sys.stderr)
        return 2
    if snapshot == base and not refs:
        print(f"kd-mirror: main {head[:12]} has the tree GitHub has; nothing to publish")
    else:
        print(f"kd-mirror: main {head[:12]} → GitHub {snapshot[:12]}" + (f", {len(refs)} tags" if refs else ""))
    return 0


def _message(head):
    return f"Update to upstream {head[:12]}"


def _scan(git, bare, head, author, refs, scanners):
    """Both scanners over what a publish of ``head`` with ``refs`` shows: the tree, its paths, the snapshot's
    message and identity and the tag names."""
    published = (f"commit snapshot\nauthor {author}\ncommitter {author}\n{_message(head)}\n"
                 + "".join(f"\0tag {ref}\n" for ref in refs))
    paths = _git(git, [*bare, "ls-tree", "-r", "--name-only", "-z", head], None) or ""
    records = history(published, "", paths)
    with tempfile.TemporaryDirectory(prefix="kd-mirror.") as tmp:
        tree = Path(tmp) / "tree"
        tree.mkdir()
        _unpack(_git(git, [*bare, "archive", "--format=tar", head], None, binary=True), tree)
        return scanners(tree, records)


def _late_tags(*, git, bare, head, new, state, state_dir, scanners, author, remote, github_env, signers):
    """Tags put on a main already published. Each new one is decided once — out on GitHub's main while that has the
    tree of this main, else left on the forge with the reason (exit 1, one alert); a step that cannot run (exit 2)
    decides nothing, and the next run tries again."""
    refs = _outgoing(git, bare, new, signers)
    code, base = 0, ""
    if refs:
        findings = _scan(git, bare, head, author, refs, scanners)
        if findings:
            print(f"kd-mirror: privacy: {findings[0]}; теги не отправлены на GitHub", file=sys.stderr)
            code, refs = 1, []
        else:
            base = _github_main(git, bare, remote, github_env)
            if base and _rev(git, bare, base + "^{tree}") == _rev(git, bare, head + "^{tree}"):
                _git(git, [*bare, "push", "--atomic", "--quiet", remote, *(f"{base}:{ref}" for ref in refs)],
                     github_env, timeout=T_PUSH)
            else:
                for ref in refs:
                    print(f"kd-mirror: tag {ref[len('refs/tags/'):][:80]} not pushed — GitHub's main is not the tree "
                          "of the published main", file=sys.stderr)
                code, refs = 1, []
    state.update(tags=sorted(set(state["tags"]) | set(new)))
    _save(state_dir, state)
    if refs:
        print(f"kd-mirror: main {head[:12]} → GitHub {base[:12]}, {len(refs)} tags")
    return code


def _identity(author):
    match = _AUTHOR.match(author or "")
    if not match:
        raise MirrorError(f"mirror_author is not «Name <email>»: {str(author)[:80]!r}")
    return match.group(1), match.group(2)


def _rev(git, bare, rev):
    sha = (_git(git, [*bare, "rev-parse", "--verify", "--quiet", rev], None) or "").strip()
    if not SHA_RE.fullmatch(sha):
        raise MirrorError(f"git rev-parse: {rev[:60]} not read")
    return sha


def _github_main(git, bare, remote, env):
    """GitHub's main, fetched into the bare mirror; "" for a repository without one (a new, empty mirror)."""
    if not (_git(git, [*bare, "ls-remote", remote, MAIN], env) or "").strip():
        return ""
    _git(git, [*bare, "fetch", "--quiet", "--no-tags", remote, f"+{MAIN}:{GITHUB_MAIN}"], env)
    return _rev(git, bare, GITHUB_MAIN)


def _on_head(git, bare, head):
    """The tags pointing at ``head``, sorted."""
    return sorted(set((_git(git, [*bare, "for-each-ref", f"--points-at={head}", "--format=%(refname)", "refs/tags"],
                            None) or "").split()))


def _tags(git, bare, head, signers, on_head):
    """The tags on ``head`` that go out (signed ones only when ``signers`` is a file); a tag on an older commit of
    main stays on the forge and is said so."""
    merged = (_git(git, [*bare, "for-each-ref", f"--merged={head}", "--format=%(refname)", "refs/tags"], None)
              or "").split()
    for ref in merged:
        if ref not in on_head:
            print(f"kd-mirror: tag {ref[len('refs/tags/'):][:80]} not pushed — not on the main being published",
                  file=sys.stderr)
    return _outgoing(git, bare, [ref for ref in merged if ref in on_head], signers)


def _outgoing(git, bare, refs, signers):
    """``refs`` that may go out: all of them, or with ``signers`` a file only the signed ones."""
    if not (signers and Path(signers).is_file()):
        return list(refs)
    return [ref for ref in refs if _signed(git, bare, ref, signers)]


def _signed(git, bare, ref, signers):
    """The tag is signed by one of ``signers`` and the signed object names it (an old signed tag under a new name
    carries the old one); else it is left out and said so."""
    name = ref[len("refs/tags/"):]
    try:
        p = git(["git", *bare, "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={signers}", "verify-tag",
                 "--format=%(tag)", ref], capture_output=True, text=True, timeout=T_GIT)
    except (OSError, subprocess.SubprocessError) as exc:
        raise MirrorError(f"git verify-tag: {exc.__class__.__name__}") from None
    if p.returncode == 0 and (p.stdout or "").strip() == name:
        return True
    print(f"kd-mirror: tag {name[:80]} not pushed — no signature from {signers} under its name", file=sys.stderr)
    return False


def _ssh(key, known_hosts):
    # the host key only from the release tree: neither TOFU nor the system's known_hosts
    return {**os.environ, "GIT_SSH_COMMAND": (
        f"ssh -i {key} -o IdentitiesOnly=yes -o BatchMode=yes -o UserKnownHostsFile={known_hosts} "
        "-o GlobalKnownHostsFile=/dev/null -o StrictHostKeyChecking=yes -o UpdateHostKeys=no")}


def default_scanners(release_tree=TREE, python=PYTHON):
    """The public scanner (the tree's own privacy-scan.json) over the tree and the snapshot's message, then the
    running release's closed scanner (its code, the overlay's literal list) over both; a scanner that cannot run is
    an error."""
    release_tree = Path(release_tree)
    script = release_tree / "common" / "kd" / "private_scan.py"  # code of the release tree, never the overlay's
    listing = release_tree / "host" / "private_scan.json"

    def closed(paths, labels):
        if not (script.is_file() and listing.is_file()):
            raise MirrorError(f"closed scanner: no {script} with {listing}")
        try:
            p = subprocess.run([python, "-I", str(script), "--list", str(listing), *map(str, paths)],
                               capture_output=True, text=True, timeout=T_SCAN)
        except (OSError, subprocess.SubprocessError) as exc:
            raise MirrorError(f"closed scanner: {exc.__class__.__name__}") from None
        if p.returncode == 0:
            return []
        if p.returncode != 1:
            raise MirrorError(f"closed scanner failed: {(p.stderr or p.stdout or '').strip()[-200:]}")
        found = []
        for line in (p.stdout or "").splitlines():
            match = _HIT.match(line)
            if not match:
                continue
            where = match.group(1)
            for prefix, label in labels:
                if where.startswith(prefix):
                    where = label + where[len(prefix):]
                    break
            found.append(f"{where}:{match.group(2)}: private literal")  # the literal itself stays out
        return found or ["<closed scanner>:0: private literal"]  # exit 1 is a finding even when unparsed

    def scan(tree, records):
        try:
            cfg = privacy_scan.load_config(tree / "privacy-scan.json")
        except (OSError, ValueError) as exc:
            raise MirrorError(f"public scanner: privacy-scan.json of main: {exc}") from None
        found = [f"{f.path}:{f.line}: {f.kind}" for f in privacy_scan.scan_tree(tree, cfg)]
        found += [f.split(" (allow ")[0] for f in public_history_findings(records, cfg)]
        text = tree.parent / "message.txt"
        text.write_text(history_text(records), "utf-8")
        found += closed([tree, text], [(str(tree) + os.sep, ""), (str(tree) + "/", ""), (str(text), "<message>")])
        return found

    return scan


def _code_repo(repos_path):
    try:
        with open(repos_path, encoding="utf-8") as f:
            registry = json.load(f)
        repos = [entry["repo"] for entry in registry.values()
                 if isinstance(entry, dict) and entry.get("release") == RELEASE and entry.get("role", "code") == "code"]
    except (OSError, ValueError, AttributeError, KeyError, TypeError) as exc:
        raise MirrorError(f"{repos_path}: unreadable ({exc.__class__.__name__})") from None
    if len(repos) != 1 or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}/[A-Za-z0-9_.-]{1,100}", str(repos[0])):
        raise MirrorError(f"{repos_path}: no single code repository of release {RELEASE}")
    return repos[0]


def main(argv=None) -> int:
    argparse.ArgumentParser(prog="kd-mirror", description=__doc__.split("\n")[0]).parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if Path(DISABLED).exists():
        print("kd-mirror: disabled")
        return 0
    try:
        host = naming.load_host()
        repo = _code_repo(REPOS)
    except (naming.NamingError, MirrorError) as exc:
        print(f"kd-mirror: {exc}", file=sys.stderr)
        return 2
    state_dir = Path(STATE_DIR)
    install = Path(TREE) / "common" / "install"
    return run(state_dir=state_dir, mirror_dir=state_dir / (repo.split("/", 1)[1] + ".git"),
               remote=host["mirror_remote"], forge_url=host["forge_ssh_url"].replace("{repo}", repo),
               scanners=default_scanners(), author=host["mirror_author"],
               forge_env=_ssh(FORGE_KEY, install / "forgejo_known_hosts"),
               github_env=_ssh(GITHUB_KEY, install / "github_known_hosts"),
               signers=Path(TREE) / "host" / "allowed_signers")
