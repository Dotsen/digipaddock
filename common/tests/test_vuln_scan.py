"""vuln_scan.py — the vulns step of dev.check and the daily scan's parts, with no network and no osv-scanner: the
comparison with the target's commit, the database's age, the exceptions of osv-scanner.toml and the download."""

import datetime
import json
import os
import subprocess
import zipfile
from pathlib import Path

import pytest

from kd import devcheck_runner
from kd import vuln_scan as vs

PATH = Path(vs.__file__)
NOW = datetime.datetime(2026, 11, 23, 12, 0, tzinfo=datetime.timezone.utc).timestamp()
TODAY = datetime.date(2026, 11, 23)
HASH = "    --hash=sha256:" + "0" * 64 + "\n"


def test_the_script_is_stdlib_only():
    text = PATH.read_text(encoding="utf-8")
    assert (
        "from kd" not in text and "import kd" not in text
    )  # -I: nothing of the tree on the path
    assert (
        vs.VENV_LOCKS == devcheck_runner.VENV_LOCKS
    )  # the venv the check runs with is built from these


@pytest.mark.parametrize(
    "path,text,lock",
    [
        ("web/package-lock.json", "{}", True),
        ("composer.lock", "{}", True),
        ("a/requirements.txt", "x==1 \\\n" + HASH, True),
        ("requirements-dev.txt", "pytest>=8\n", False),
        ("requirements.in", "x\n", False),
        ("notes.txt", HASH, False),
    ],
)
def test_a_lock_is_a_known_name_or_a_hashed_requirements_file(path, text, lock):
    assert vs.is_lock(path, text) is lock


# --- the database ----------------------------------------------------------------------------------------------------


def _db(tmp_path, updated, ecosystems=vs.ECOSYSTEMS):
    osv = tmp_path / "osv"
    if osv.exists():  # a second check in one test
        return str(osv)
    db = osv / "db-1"
    for eco in ecosystems:
        (db / vs.DB_LAYOUT / eco).mkdir(parents=True)
        (db / vs.DB_LAYOUT / eco / "all.zip").write_bytes(b"zip")
    if updated is not None:
        (db / vs.STAMP).write_text(json.dumps({"updated": updated}), encoding="utf-8")
    os.symlink("db-1", osv / "current")
    return str(osv)


def test_the_database_age_comes_from_its_stamp(tmp_path):
    osv = _db(tmp_path, NOW - 5 * 3600)
    db, updated, age = vs.db_state(osv, NOW)
    assert db.endswith("db-1") and updated == NOW - 5 * 3600 and age == 5 * 3600
    assert (
        vs.describe_age(updated, age)
        == "the OSV database of 2026-11-23 07:00 UTC, 5 h old"
    )


@pytest.mark.parametrize(
    "make",
    [
        lambda p: str(p / "nothing"),  # never downloaded
        lambda p: _db(p, None),  # no stamp: a download cut short
        lambda p: _db(p, NOW, ecosystems=("PyPI",)),  # an ecosystem missing
    ],
)
def test_no_database_is_none(tmp_path, make):
    assert vs.db_state(make(tmp_path), NOW) == (None, None, None)
    assert "missing" in vs.describe_age(None, None)


@pytest.mark.parametrize("updated", [None, NOW - vs.MAX_AGE_S - 60])
def test_the_step_is_red_without_a_fresh_database_and_says_its_age(tmp_path, updated):
    osv = _db(tmp_path, updated) if updated else str(tmp_path / "none")
    lines = []
    assert (
        vs.check(
            str(tmp_path),
            "refs/kd/base",
            osv_dir=osv,
            scanner="/bin/true",
            now=NOW,
            out=lines.append,
        )
        == 1
    )
    assert "at most 72 h old" in lines[0] and (
        "72 h 1 min old" in lines[0] if updated else "missing" in lines[0]
    )


def _zip(target):
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr("GHSA-xxxx.json", "{}")


def test_update_switches_current_only_to_a_complete_download(tmp_path):
    osv = tmp_path / "osv"
    osv.mkdir()
    (osv / "db-1").mkdir()
    (osv / "db-2").mkdir()
    (osv / ".new-3").mkdir()  # a download a crash left
    fetched = []
    final = vs.update(
        str(osv),
        fetch=lambda url, target: (fetched.append(url), _zip(target)),
        now=NOW,
        log=lambda _: None,
    )
    assert fetched == [vs.DB_URL.format(eco) for eco in vs.ECOSYSTEMS]
    assert os.readlink(osv / "current") == os.path.basename(final)
    # the previous one stays for a check that resolved it just before the switch
    assert sorted(os.listdir(osv)) == sorted(
        ["current", "db-2", os.path.basename(final)]
    )
    db, updated, age = vs.db_state(str(osv), NOW)
    assert updated == int(NOW) and age == 0

    def broken(url, target):
        Path(target).write_bytes(b"<html>not a zip</html>")

    with pytest.raises(vs.ScanError, match="not a zip"):
        vs.update(str(osv), fetch=broken, now=NOW + 86400, log=lambda _: None)
    assert vs.db_state(str(osv), NOW)[1] == int(NOW)  # the old database stays current
    # no half download left
    assert sorted(os.listdir(osv)) == sorted(
        ["current", "db-2", os.path.basename(final)]
    )


# --- the scanner's report --------------------------------------------------------------------------------------------


def _pkg(
    name,
    version,
    eco,
    *groups,
    fixed="9.9.9",
    root="/s/head",
    path="a/requirements.txt",
):
    vulns = [
        {
            "id": i,
            "aliases": [f"CVE-{i}"] if i.startswith("GHSA") else [],
            "summary": f"{name} bug",
            "affected": [
                {
                    "package": {"name": name, "ecosystem": eco},
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [{"introduced": "0"}, {"fixed": fixed}],
                        },
                        {"type": "GIT", "events": [{"fixed": "ab" * 20}]},
                    ],
                }
            ],
        }
        for group in groups
        for i in group
    ]
    return {
        "source": {"path": f"{root}/{path}", "type": "lockfile"},
        "packages": [
            {
                "package": {"name": name, "version": version, "ecosystem": eco},
                "vulnerabilities": vulns,
                "groups": [{"ids": list(g)} for g in groups],
            }
        ],
    }


def test_findings_are_one_per_group_with_aliases_and_fixed_versions():
    report = {
        "results": [
            _pkg(
                "requests",
                "2.19.0",
                "PyPI",
                ["GHSA-a", "PYSEC-1"],
                ["PYSEC-2"],
                root="/s",
            )
        ]
    }
    found = vs.findings(report, "/s")
    assert [f["id"] for f in found] == ["CVE-GHSA-a", "PYSEC-2"]
    first = found[0]
    assert (
        first["ids"] == ["CVE-GHSA-a", "GHSA-a", "PYSEC-1"]
        and first["path"] == "a/requirements.txt"
    )
    assert first["fixed"] == ["9.9.9"]  # a GIT range's commit is no version
    assert (first["package"], first["version"], first["ecosystem"]) == (
        "requests",
        "2.19.0",
        "PyPI",
    )


def _f(vid, package="requests", path="a/requirements.txt", ids=None):
    return {
        "id": vid,
        "ids": ids or [vid],
        "package": package,
        "ecosystem": "PyPI",
        "version": "1",
        "fixed": [],
        "path": path,
        "summary": "",
    }


def test_only_a_pair_the_base_did_not_have_is_new():
    base = [_f("CVE-1", ids=["CVE-1", "GHSA-1"])]
    head = [
        _f(
            "GHSA-1", ids=["GHSA-1", "PYSEC-9"]
        ),  # the same vulnerability by an alias: known
        _f("CVE-1", package="urllib3"),  # the same id in another package: new
        _f("CVE-2"),
    ]
    new, known, ignored, expired = vs.classify(head, base, [], TODAY)
    assert [(f["id"], f["package"]) for f in new] == [
        ("CVE-1", "urllib3"),
        ("CVE-2", "requests"),
    ]
    assert [f["id"] for f in known] == ["GHSA-1"] and not ignored and not expired


def test_a_venv_finding_is_new_only_for_a_package_the_change_touched():
    head = [
        _f("CVE-1", package="pytest", path=vs.VENV),
        _f("CVE-2", package="Py_Yaml", path=vs.VENV),
    ]
    new, known, _, _ = vs.classify(
        head, [], [], TODAY, venv_changed=None
    )  # the venv's locks did not change
    assert not new and len(known) == 2
    new, known, _, _ = vs.classify(head, [], [], TODAY, venv_changed={"py-yaml"})
    assert [f["package"] for f in new] == ["Py_Yaml"] and [
        f["package"] for f in known
    ] == ["pytest"]


# --- exceptions ------------------------------------------------------------------------------------------------------


def test_an_exception_needs_a_reason_and_a_date_within_90_days():
    text = """
[[IgnoredVulns]]
id = "CVE-1"
ignoreUntil = 2026-12-31
reason = "the parser is never given user input"

[[IgnoredVulns]]
id = "CVE-2"
effectiveUntil = 2026-12-01T00:00:00Z
reason = "osv-scanner's own name of the date"

[[IgnoredVulns]]
id = "CVE-3"
ignoreUntil = 2026-12-31

[[IgnoredVulns]]
id = "CVE-4"
reason = "forever"

[[IgnoredVulns]]
id = "CVE-5"
ignoreUntil = 2027-03-01
reason = "too far"
"""
    entries, problems = vs.load_ignores(text, TODAY)
    assert [(e["id"], str(e["until"])) for e in entries] == [
        ("CVE-1", "2026-12-31"),
        ("CVE-2", "2026-12-01"),
    ]
    assert problems == [
        "osv-scanner.toml: entry 3 (CVE-3): no reason",
        "osv-scanner.toml: entry 4 (CVE-4): no ignoreUntil date",
        "osv-scanner.toml: entry 5 (CVE-5): ignoreUntil 2027-03-01 is more than 90 days ahead "
        "(at most 2027-02-21)",
    ]
    assert vs.load_ignores("", TODAY) == ([], [])
    assert vs.load_ignores("[[IgnoredVulns]\n", TODAY)[1][0].startswith(
        "osv-scanner.toml: not TOML"
    )
    assert vs.load_ignores('[[PackageOverrides]]\nname = "x"\nignore = true\n', TODAY)[
        1
    ] == ["osv-scanner.toml: only [[IgnoredVulns]] is applied, not PackageOverrides"]


def test_the_repositorys_own_exceptions_are_valid():
    text = (PATH.parents[2] / vs.CONFIG).read_text(encoding="utf-8")
    entries, problems = vs.load_ignores(text, datetime.date.today())
    assert problems == [] and entries  # a broken entry would make every dev.check red


def test_an_exception_takes_a_finding_out_until_its_date_then_it_blocks_base_or_not():
    entries = [{"id": "GHSA-1", "reason": "r", "until": TODAY}]
    head = [_f("CVE-1", ids=["CVE-1", "GHSA-1"])]
    new, known, ignored, expired = vs.classify(head, [], entries, TODAY)
    assert not new and [f["id"] for f, _ in ignored] == ["CVE-1"]
    later = TODAY + datetime.timedelta(days=1)
    new, known, ignored, expired = vs.classify(
        head, head, entries, later
    )  # on the base too
    assert not new and not known and [f["id"] for f, _ in expired] == ["CVE-1"]


# --- the step over a git checkout ------------------------------------------------------------------------------------


def _git(repo, *args):
    subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _commit(repo, files, message):
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(
        repo,
        "-c",
        "user.name=t",
        "-c",
        "user.email=t@example.test",
        "commit",
        "-q",
        "-m",
        message,
    )


class Scanner:
    """osv-scanner's stand-in: a finding for every ``name==version`` of the VULNERABLE table in the lock files."""

    VULNERABLE = {
        ("requests", "2.19.0"): ["CVE-2018-18074"],
        ("jinja2", "2.10"): ["CVE-2019-10906"],
    }

    def __init__(self):
        self.calls = []

    def __call__(self, argv, env):
        self.calls.append((argv, env))
        report = argv[argv.index("--output-file") + 1]
        locks = [
            a.split(":", 1)[-1] if a.startswith("requirements.txt:") else a
            for i, a in enumerate(argv)
            if i and argv[i - 1] == "-L"
        ]
        results = []
        for lock in locks:
            for line in Path(lock).read_text(encoding="utf-8").splitlines():
                name, _, version = line.strip(" \\").partition("==")
                for vid in self.VULNERABLE.get((name.lower(), version), ()):
                    results.append(
                        _pkg(
                            name,
                            version,
                            "PyPI",
                            [vid],
                            root=os.path.dirname(lock),
                            path=os.path.basename(lock),
                        )
                    )
        Path(report).write_text(json.dumps({"results": results}), encoding="utf-8")
        return (1 if results else 0), ""


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    _git(path, "init", "-q")
    _commit(path, {"common/x/requirements.txt": "jinja2==2.10 \\\n" + HASH}, "base")
    _git(path, "update-ref", "refs/kd/base", "HEAD")
    return path


def _check(repo, tmp_path, scanner, **kw):
    lines = []
    rc = vs.check(
        str(repo),
        "refs/kd/base",
        osv_dir=_db(tmp_path, NOW - 3600),
        scanner=str(tmp_path / "osv-bin"),
        now=NOW,
        runner=scanner,
        out=lines.append,
        **kw,
    )
    return rc, lines


def test_a_vulnerable_version_added_by_the_change_fails_the_step_with_its_id_and_fix(
    repo, tmp_path
):
    (tmp_path / "osv-bin").write_text("", encoding="utf-8")
    _commit(
        repo,
        {
            "common/x/requirements.txt": "jinja2==2.10 \\\n"
            + HASH
            + "requests==2.19.0 \\\n"
            + HASH
        },
        "add",
    )
    scanner = Scanner()
    rc, lines = _check(repo, tmp_path, scanner)
    assert rc == 1
    assert lines[0] == "vulns: the OSV database of 2026-11-23 11:00 UTC, 1 h old"
    assert (
        "known CVE-2019-10906 jinja2 2.10 (PyPI) · fix 9.9.9 · common/x/requirements.txt · not brought by this "
        "change"
    ) in lines
    # the new one last: the notice of the check carries the output's tail
    assert (
        lines[-2]
        == "NEW CVE-2018-18074 requests 2.19.0 (PyPI) · fix 9.9.9 · common/x/requirements.txt"
    )
    assert lines[-1] == "vulns: 1 new, 0 past their exception, 1 known, 0 ignored"
    ((argv, env),) = scanner.calls  # one scan of both sides: the database loads once
    assert argv[:2] == [str(tmp_path / "osv-bin"), "scan"] and "--offline" in argv
    config = argv[argv.index("--config") + 1]
    assert config.endswith(
        "empty.toml"
    )  # the repository's osv-scanner.toml is never osv-scanner's to apply
    assert env["OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY"].endswith("db-1") and set(
        env
    ) == {"PATH", "HOME", "OSV_SCANNER_LOCAL_DB_CACHE_DIRECTORY"}


def test_a_change_without_new_dependencies_passes_with_the_base_vulnerability_as_a_line(
    repo, tmp_path
):
    (tmp_path / "osv-bin").write_text("", encoding="utf-8")
    _commit(repo, {"README.md": "x\n"}, "docs")
    rc, lines = _check(repo, tmp_path, Scanner())
    assert rc == 0 and any(
        line.startswith("known CVE-2019-10906 jinja2") for line in lines
    )


def test_an_exception_without_a_reason_fails_and_a_valid_one_passes(repo, tmp_path):
    (tmp_path / "osv-bin").write_text("", encoding="utf-8")
    added = {
        "common/x/requirements.txt": "jinja2==2.10 \\\n"
        + HASH
        + "requests==2.19.0 \\\n"
        + HASH
    }
    _commit(
        repo,
        {
            **added,
            "osv-scanner.toml": '[[IgnoredVulns]]\nid = "CVE-2018-18074"\nignoreUntil = 2026-12-01\n',
        },
        "no reason",
    )
    rc, lines = _check(repo, tmp_path, Scanner())
    assert (
        rc == 1
        and "vulns: osv-scanner.toml: entry 1 (CVE-2018-18074): no reason" in lines
    )
    _commit(
        repo,
        {
            "osv-scanner.toml": '[[IgnoredVulns]]\nid = "CVE-2018-18074"\nignoreUntil = 2026-12-01\n'
            'reason = "only the tests use it"\n'
        },
        "reason",
    )
    rc, lines = _check(repo, tmp_path, Scanner())
    assert rc == 0 and any(
        line.startswith("ignored CVE-2018-18074")
        and line.endswith("until 2026-12-01: only the tests use it")
        for line in lines
    )


def test_the_venv_is_scanned_by_the_versions_installed(repo, tmp_path, monkeypatch):
    (tmp_path / "osv-bin").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        vs, "venv_requirements", lambda: "requests==2.19.0\njinja2==2.10\n"
    )
    scanner = Scanner()
    rc, lines = _check(repo, tmp_path, scanner, venv=True)
    # the venv's locks did not change: its requests is no finding of this change; jinja2 is the lock file's line only
    assert rc == 0
    assert [line.split(" · ")[-2] for line in lines if line.startswith("known")] == [
        vs.VENV,
        "common/x/requirements.txt",
    ]
    _commit(repo, {"requirements-dev.txt": "requests==2.19.0\n"}, "a dev tool")
    rc, lines = _check(repo, tmp_path, Scanner(), venv=True)
    assert (
        rc == 1
        and lines[-2].startswith("NEW CVE-2018-18074 requests 2.19.0 (PyPI)")
        and vs.VENV in lines[-2]
    )


def test_venv_requirements_lists_only_the_venvs_own_packages(tmp_path):
    lines = vs.venv_requirements(
        prefix=os.path.dirname(os.path.dirname(pytest.__file__)), base_prefix="/nowhere"
    )
    assert any(line.lower().startswith("pytest==") for line in lines.splitlines())
    assert (
        vs.venv_requirements(prefix="/same", base_prefix="/same") == ""
    )  # not a venv: nothing of the system's


def test_main_refuses_unknown_arguments(capsys):
    assert vs.main(["check", "."]) == 2
    assert vs.main(["update", "x"]) == 2
    assert vs.main(["scan"]) == 2
