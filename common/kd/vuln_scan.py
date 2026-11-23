"""Known vulnerabilities of a repository's locked dependencies: osv-scanner over an offline copy of the OSV database.

    python3 -I common/kd/vuln_scan.py check <checkout> <base-ref> [--venv]
    python3 -I common/kd/vuln_scan.py scan <dir>
    python3 -I common/kd/vuln_scan.py update

``check`` is the step ``vulns`` of dev.check (devcheck_steps.py), in the child unit with no network: the lock files of
``HEAD`` and of ``<base-ref>`` (the target's commit, ``refs/kd/base``) are scanned against the database, and only a
finding the base did not have fails the step — a pair «vulnerability + package»; one already there is a line of the
output. ``--venv`` adds the packages installed in the check's own venv (the interpreter running this file): the
unpinned tools of requirements-dev.txt are scanned by the versions the check runs with. The repository's
``osv-scanner.toml`` takes findings out, each ``[[IgnoredVulns]]`` with an ``id``, a ``reason`` and an ``ignoreUntil``
no further than IGNORE_MAX_DAYS ahead: an entry without them fails the step, and past its date the finding blocks again,
whether the base had it or not. No database, or one older than MAX_AGE_S: the step fails with its age.

``scan`` is the daily service's (kd-vulnscan, as the ci user with no network): the findings of the lock files under
``<dir>`` as JSON on stdout. ``update`` is its download (the ci user, with network): the databases of ECOSYSTEMS into a
new directory, switched in as ``current`` only when all of them are complete.

A lock file is a known lock name (LOCK_NAMES) or a ``requirements*.txt`` pinned with ``--hash=``; the ranges of a plain
requirements file are no lock. Stdlib only and free of kd imports: ``-I`` puts nothing of the tree on the path.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import urllib.request
import zipfile

OSV_DIR = "/var/lib/digipaddock/devcheck/osv"  # the ci user's: current -> db-<stamp>/ (kd-vulnscan update)
SCANNER = "/usr/local/lib/digipaddock/osv-scanner"  # devcheck_setup.sh install: versions.json's version and sha256
ECOSYSTEMS = ("PyPI", "npm", "Packagist")
DB_URL = "https://osv-vulnerabilities.storage.googleapis.com/{}/all.zip"
DB_LAYOUT = "osv-scalibr"  # <db>/osv-scalibr/<ecosystem>/all.zip — where the pinned osv-scanner reads it offline
STAMP = "stamp.json"
MAX_AGE_S = 72 * 3600
IGNORE_MAX_DAYS = 90
CONFIG = "osv-scanner.toml"
LOCK_NAMES = (
    "composer.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
)
VENV = ".venv/requirements.txt"  # the check venv's packages, a path of the scan only
VENV_LOCKS = (
    "requirements-dev.txt",
    "common/cabinet/requirements-build.txt",
    "common/cabinet/requirements.txt",
)
LOCK_MAX = 8 << 20
DB_MAX = 2 << 30
SCAN_TIMEOUT = 600
DOWNLOAD_TIMEOUT = 120
_REQUIREMENTS = re.compile(r"requirements[^/]*\.txt")
_ADDED_NAME = re.compile(r"^\+\s*([A-Za-z0-9][A-Za-z0-9._-]*)", re.M)


class ScanError(Exception):
    pass


# --- lock files ------------------------------------------------------------------------------------------------------


def is_lock(path, text):
    name = path.rsplit("/", 1)[-1]
    if name in LOCK_NAMES:
        return True
    return bool(_REQUIREMENTS.fullmatch(name)) and "--hash=" in text


def _git(checkout, *args):
    proc = subprocess.run(
        ["git", "-C", checkout, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        raise ScanError(
            f"git {args[0]}: exit {proc.returncode}: {proc.stderr.decode('utf-8', 'replace').strip()[-300:]}"
        )
    return proc.stdout


def locks_at(checkout, ref):
    """``{path: text}`` of the lock files the commit holds (read from git, never from the work tree)."""
    names = (
        _git(checkout, "ls-tree", "-r", "-z", "--name-only", ref)
        .decode("utf-8", "replace")
        .split("\0")
    )
    out = {}
    for path in names:
        name = path.rsplit("/", 1)[-1]
        if not path or not (name in LOCK_NAMES or _REQUIREMENTS.fullmatch(name)):
            continue
        data = _git(checkout, "show", f"{ref}:{path}")
        if len(data) > LOCK_MAX:
            raise ScanError(f"{path}: larger than {LOCK_MAX} bytes")
        text = data.decode("utf-8", "replace")
        if is_lock(path, text):
            out[path] = text
    return out


def venv_requirements(prefix=None, base_prefix=None):
    """``name==version`` of every distribution installed in the running venv itself (not the system's site)."""
    from importlib import metadata

    prefix = os.path.realpath(prefix or sys.prefix)
    if prefix == os.path.realpath(base_prefix or sys.base_prefix):
        return ""
    lines = set()
    for dist in metadata.distributions():
        where = os.path.realpath(str(dist.locate_file("")))
        name, version = dist.metadata.get("Name"), dist.version
        if name and version and (where + os.sep).startswith(prefix + os.sep):
            lines.add(f"{name}=={version}")
    return "".join(line + "\n" for line in sorted(lines, key=str.lower))


def write_tree(root, files):
    for path, text in files.items():
        target = os.path.join(root, *path.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(text)


# --- the database ----------------------------------------------------------------------------------------------------


def db_state(osv_dir=OSV_DIR, now=None):
    """``(db directory or None, its UTC update time or None, age in seconds or None)``."""
    current = os.path.join(osv_dir, "current")
    try:
        db = os.path.realpath(current)
        with open(os.path.join(db, STAMP), encoding="utf-8") as handle:
            updated = float(json.load(handle)["updated"])
    except (OSError, ValueError, KeyError, TypeError):
        return None, None, None
    if not all(
        os.path.isfile(os.path.join(db, DB_LAYOUT, eco, "all.zip"))
        for eco in ECOSYSTEMS
    ):
        return None, None, None
    age = (time.time() if now is None else now) - updated
    return db, updated, age


def describe_age(updated, age):
    if updated is None:
        return "the OSV database is missing (kd-vulnscan has not downloaded it yet)"
    when = datetime.datetime.fromtimestamp(updated, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    hours, minutes = divmod(int(max(age, 0)) // 60, 60)
    return f"the OSV database of {when}, {hours} h{f' {minutes} min' if minutes else ''} old"


def update(osv_dir=OSV_DIR, *, fetch=None, now=None, log=print):
    """Every ecosystem's ``all.zip`` into ``.new-<stamp>``, checked as a zip, then ``db-<stamp>`` and ``current`` switched
    to it; the databases older than the previous one are removed. Raises ScanError on a failed or incomplete
    download."""
    fetch = fetch or _download
    stamp = int(time.time() if now is None else now)
    new = os.path.join(osv_dir, f".new-{stamp}")
    final = os.path.join(osv_dir, f"db-{stamp}")
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new, mode=0o755)
    try:
        for eco in ECOSYSTEMS:
            target = os.path.join(new, DB_LAYOUT, eco, "all.zip")
            os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
            fetch(DB_URL.format(eco), target)
            try:
                with zipfile.ZipFile(target) as archive:
                    count = len(archive.namelist())
            except (zipfile.BadZipFile, OSError) as exc:
                raise ScanError(
                    f"{eco}: not a zip archive ({exc.__class__.__name__})"
                ) from None
            if not count:
                raise ScanError(f"{eco}: an empty archive")
            log(
                f"vulnscan: {eco}: {count} advisories, {os.path.getsize(target) >> 20} MiB"
            )
        with open(os.path.join(new, STAMP), "w", encoding="utf-8") as handle:
            json.dump({"updated": stamp, "ecosystems": list(ECOSYSTEMS)}, handle)
        os.replace(new, final)
    except BaseException:
        shutil.rmtree(new, ignore_errors=True)
        raise
    link = os.path.join(osv_dir, f".current-{stamp}")
    if os.path.lexists(link):
        os.unlink(link)
    os.symlink(os.path.basename(final), link)
    os.replace(link, os.path.join(osv_dir, "current"))
    # the one before stays: a check that resolved it just before the switch scans to its end
    kept = sorted(
        (n for n in os.listdir(osv_dir) if n.startswith("db-") and n[3:].isdigit()),
        key=lambda n: int(n[3:]),
    )[-2:]
    for name in os.listdir(osv_dir):
        if (name.startswith("db-") or name.startswith(".new-")) and name not in kept:
            shutil.rmtree(os.path.join(osv_dir, name), ignore_errors=True)
    return final


def _download(url, target):
    try:
        with (
            urllib.request.urlopen(url, timeout=DOWNLOAD_TIMEOUT) as resp,
            open(target, "wb") as out,
        ):
            size = 0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                size += len(chunk)
                if size > DB_MAX:
                    raise ScanError(f"{url}: larger than {DB_MAX} bytes")
                out.write(chunk)
    except (OSError, ValueError) as exc:
        raise ScanError(f"{url}: {exc.__class__.__name__}: {exc}") from None


# --- the scanner -----------------------------------------------------------------------------------------------------


def run_scanner(root, paths, db, *, scanner=SCANNER, runner=None):
    """osv-scanner over the given lock files under ``root``, offline, with no configuration of the repository (the
    exceptions are applied here); its JSON report as a dict."""
    if not paths:
        return {"results": []}
    with tempfile.TemporaryDirectory() as tmp:
        empty, report = (
            os.path.join(tmp, "empty.toml"),
            os.path.join(tmp, "report.json"),
        )
        with open(empty, "w", encoding="utf-8"):
            pass
        argv = [
            scanner,
            "scan",
            "source",
            "--offline",
            "--format",
            "json",
            "--output-file",
            report,
            "--config",
            empty,
            "--verbosity",
            "error",
        ]
        for path in sorted(paths):
            full = os.path.join(root, *path.split("/"))
            name = path.rsplit("/", 1)[-1]
            argv += [
                "-L",
                f"requirements.txt:{full}" if _REQUIREMENTS.fullmatch(name) else full,
            ]
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": tmp,
            "OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY": db,
        }
        code, out = (runner or _run)(argv, env)
        if code not in (0, 1):  # 1: vulnerabilities found
            raise ScanError(f"osv-scanner: exit {code}: {out.strip()[-500:]}")
        try:
            with open(report, encoding="utf-8") as handle:
                return json.load(handle)
        except (OSError, ValueError) as exc:
            raise ScanError(
                f"osv-scanner: no report ({exc.__class__.__name__})"
            ) from None


def _run(argv, env):
    try:
        proc = subprocess.run(
            argv,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=SCAN_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {SCAN_TIMEOUT} s"
    except OSError as exc:
        return 127, f"{exc.__class__.__name__}: {exc}"
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def _rel(root, path):
    """The scanned file's path relative to the scan's root (the report has it absolute)."""
    real_root = os.path.realpath(root)
    full = os.path.realpath(path)
    return (
        os.path.relpath(full, real_root).replace(os.sep, "/")
        if full.startswith(real_root + os.sep)
        else path
    )


def _display(ids):
    for prefix in ("CVE-", "GHSA-"):
        found = sorted(i for i in ids if i.startswith(prefix))
        if found:
            return found[0]
    return sorted(ids)[0]


def _fixed(vulns, name):
    fixed = []
    for vuln in vulns:
        for affected in vuln.get("affected") or ():
            package = affected.get("package") or {}
            if str(package.get("name", "")).lower() != name.lower():
                continue
            for rng in affected.get("ranges") or ():
                if rng.get("type") not in (
                    "ECOSYSTEM",
                    "SEMVER",
                ):  # a GIT range's "fixed" is a commit
                    continue
                for event in rng.get("events") or ():
                    value = event.get("fixed")
                    if isinstance(value, str) and value not in fixed:
                        fixed.append(value)
    return fixed[:3]


def findings(report, root):
    """One finding per vulnerability group of a package: ``{id, ids, package, ecosystem, version, fixed, path,
    summary}``; ``ids`` are the group's ids and aliases."""
    out = []
    for result in report.get("results") or ():
        path = _rel(root, str((result.get("source") or {}).get("path", "")))
        for pkg in result.get("packages") or ():
            package = pkg.get("package") or {}
            name, version, eco = (
                str(package.get("name", "")),
                str(package.get("version", "")),
                str(package.get("ecosystem", "")),
            )
            vulns = {
                v.get("id"): v
                for v in pkg.get("vulnerabilities") or ()
                if isinstance(v, dict)
            }
            groups = pkg.get("groups") or [{"ids": [i]} for i in vulns]
            for group in groups:
                ids = {str(i) for i in (group.get("ids") or ()) if i}
                members = [vulns[i] for i in ids if i in vulns]
                for vuln in members:
                    ids.update(str(a) for a in vuln.get("aliases") or () if a)
                ids.update(str(a) for a in group.get("aliases") or () if a)
                if not ids:
                    continue
                summary = next(
                    (str(v.get("summary")) for v in members if v.get("summary")), ""
                )
                out.append(
                    {
                        "id": _display(ids),
                        "ids": sorted(ids),
                        "package": name,
                        "ecosystem": eco,
                        "version": version,
                        "fixed": _fixed(members, name),
                        "path": path,
                        "summary": " ".join(summary.split())[:200],
                    }
                )
    return sorted(out, key=lambda f: (f["path"], f["package"].lower(), f["id"]))


def same(a, b):
    """One vulnerability of one package: the ecosystem, the name and any shared id (aliases change over time)."""
    return (
        a["ecosystem"] == b["ecosystem"]
        and a["package"].lower() == b["package"].lower()
        and bool(set(a["ids"]) & set(b["ids"]))
    )


# --- exceptions ------------------------------------------------------------------------------------------------------


def _as_date(value):
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def load_ignores(text, today):
    """``(entries, problems)`` of an osv-scanner.toml: each entry ``{id, reason, until}``; an entry without a reason or
    a date, or with a date more than IGNORE_MAX_DAYS ahead, is a problem and takes nothing out."""
    if not text.strip():
        return [], []
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [], [f"{CONFIG}: not TOML ({exc})"]
    entries, problems = [], []
    others = sorted(k for k in data if k != "IgnoredVulns")
    if others:
        problems.append(
            f"{CONFIG}: only [[IgnoredVulns]] is applied, not {', '.join(others)}"
        )
    raw = data.get("IgnoredVulns", [])
    if not isinstance(raw, list):
        return [], problems + [
            f"{CONFIG}: IgnoredVulns must be a list of tables ([[IgnoredVulns]])"
        ]
    limit = today + datetime.timedelta(days=IGNORE_MAX_DAYS)
    for number, item in enumerate(raw, 1):
        vid = item.get("id") if isinstance(item, dict) else None
        label = f"{CONFIG}: entry {number}" + (
            f" ({vid})" if isinstance(vid, str) else ""
        )
        if not isinstance(vid, str) or not vid.strip():
            problems.append(f"{label}: no id")
            continue
        reason = item.get("reason")
        until = _as_date(item.get("ignoreUntil", item.get("effectiveUntil")))
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{label}: no reason")
        elif until is None:
            problems.append(f"{label}: no ignoreUntil date")
        elif until > limit:
            problems.append(
                f"{label}: ignoreUntil {until} is more than {IGNORE_MAX_DAYS} days ahead (at most {limit})"
            )
        else:
            entries.append(
                {
                    "id": vid.strip(),
                    "reason": " ".join(reason.split())[:200],
                    "until": until,
                }
            )
    return entries, problems


def ignore_of(finding, entries):
    """The entry naming the finding, the latest date first; None — none does."""
    matching = [e for e in entries if e["id"] in finding["ids"]]
    return max(matching, key=lambda e: e["until"]) if matching else None


# --- the step --------------------------------------------------------------------------------------------------------


def _line(kind, f, extra=""):
    fixed = f"fix {', '.join(f['fixed'])}" if f["fixed"] else "no fixed version"
    return f"{kind} {f['id']} {f['package']} {f['version']} ({f['ecosystem']}) · {fixed} · {f['path']}{extra}"


def _venv_changed(checkout, base):
    """The names a PR adds or changes in the venv's lock files, or None — they did not change."""
    diff = _git(
        checkout, "diff", "--no-color", "-U0", base, "HEAD", "--", *VENV_LOCKS
    ).decode("utf-8", "replace")
    if not diff.strip():
        return None
    return {m.lower().replace("_", "-") for m in _ADDED_NAME.findall(diff)}


def classify(head, base, entries, today, venv_changed=None):
    """``(new, known, ignored, expired)``: new — not on the base and not taken out; expired — named by an exception
    whose date has passed (blocks, base or not). A finding of the venv is new only when the PR changed that package's
    line in the venv's locks (``venv_changed``: the names, None — no change)."""
    new, known, ignored, expired = [], [], [], []
    for f in head:
        entry = ignore_of(f, entries)
        if entry is not None and entry["until"] >= today:
            ignored.append((f, entry))
        elif entry is not None:
            expired.append((f, entry))
        elif any(same(f, b) for b in base):
            known.append(f)
        elif f["path"] == VENV and (
            venv_changed is None
            or f["package"].lower().replace("_", "-") not in venv_changed
        ):
            known.append(f)
        else:
            new.append(f)
    return new, known, ignored, expired


def check(
    checkout,
    base_ref,
    *,
    venv=False,
    osv_dir=OSV_DIR,
    scanner=SCANNER,
    now=None,
    runner=None,
    out=print,
):
    """The step: 0 — nothing new, 1 — a new finding, a broken exception or no fresh database."""
    db, updated, age = db_state(osv_dir, now)
    if db is None or age > MAX_AGE_S:
        out(
            f"vulns: {describe_age(updated, age)}; the check needs one at most {MAX_AGE_S // 3600} h old "
            "(the daily kd-vulnscan updates it)"
        )
        return 1
    if not os.path.isfile(scanner):
        out(f"vulns: no osv-scanner at {scanner} (devcheck_setup.sh install)")
        return 1
    out(f"vulns: {describe_age(updated, age)}")
    today = datetime.datetime.fromtimestamp(
        time.time() if now is None else now, datetime.timezone.utc
    ).date()
    try:
        config = _git(checkout, "show", f"HEAD:{CONFIG}").decode("utf-8", "replace")
    except ScanError:
        config = ""
    entries, problems = load_ignores(config, today)
    head_files, base_files = locks_at(checkout, "HEAD"), locks_at(checkout, base_ref)
    if venv:
        head_files[VENV] = venv_requirements()
    both = {
        **{f"head/{p}": t for p, t in head_files.items()},
        **{f"base/{p}": t for p, t in base_files.items()},
    }
    with (
        tempfile.TemporaryDirectory() as tmp
    ):  # one scan of both sides: the database loads once
        write_tree(tmp, both)
        found = findings(
            run_scanner(tmp, list(both), db, scanner=scanner, runner=runner), tmp
        )
    head, base = [], []
    for f in found:
        side, _, f["path"] = f["path"].partition("/")
        (head if side == "head" else base).append(f)
    head = [
        f
        for f in head
        if f["path"] != VENV or not any(same(f, g) for g in head if g["path"] != VENV)
    ]
    new, known, ignored, expired = classify(
        head, base, entries, today, _venv_changed(checkout, base_ref) if venv else None
    )
    out(
        f"vulns: {len(head_files)} lock files: {', '.join(sorted(head_files)) or 'none'}"
    )
    for f in known:
        out(_line("known", f, " · not brought by this change"))
    for f, entry in ignored:
        out(_line("ignored", f, f" · until {entry['until']}: {entry['reason']}"))
    for problem in problems:
        out(f"vulns: {problem}")
    for f, entry in expired:
        out(_line("EXPIRED", f, f" · the exception ended {entry['until']}"))
    for f in new:
        out(_line("NEW", f))
    out(
        f"vulns: {len(new)} new, {len(expired)} past their exception, {len(known)} known, {len(ignored)} ignored"
    )
    return 1 if new or expired or problems else 0


def scan(root, *, osv_dir=OSV_DIR, scanner=SCANNER, now=None, runner=None):
    """The daily scan of one project's lock files under ``root``: ``{db: {updated, age_s}, findings}``."""
    db, updated, age = db_state(osv_dir, now)
    if db is None:
        raise ScanError(describe_age(None, None))
    paths = []
    for top, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            rel = os.path.relpath(os.path.join(top, name), root).replace(os.sep, "/")
            with open(
                os.path.join(top, name), encoding="utf-8", errors="replace"
            ) as handle:
                if is_lock(rel, handle.read(LOCK_MAX)):
                    paths.append(rel)
    report = run_scanner(root, paths, db, scanner=scanner, runner=runner)
    return {
        "db": {"updated": updated, "age_s": age},
        "locks": paths,
        "findings": findings(report, root),
    }


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    try:
        if len(args) in (3, 4) and args[0] == "check" and args[3:] in ([], ["--venv"]):
            return check(args[1], args[2], venv=args[3:] == ["--venv"])
        if len(args) == 2 and args[0] == "scan":
            print(json.dumps(scan(args[1]), sort_keys=True))
            return 0
        if args == ["update"]:
            print(f"vulnscan: the OSV database is {update()}")
            return 0
    except ScanError as exc:
        print(f"vulns: {exc}", file=sys.stdout if args[:1] == ["check"] else sys.stderr)
        return 1
    print(
        "usage: vuln_scan.py check <checkout> <base-ref> [--venv] | scan <dir> | update",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
