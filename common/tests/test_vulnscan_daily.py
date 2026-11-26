"""kd-vulnscan — the daily scan: the children run as the ci user (the download with network, the scan without), one
card per new finding through the cabinet's card.create and never a second one, the exceptions, the «not updated»
card, the journal and the exit code."""

import datetime
import json
import os
from pathlib import Path

import pytest

from kd import ci_unit
from kd import vuln_scan as vs
from kd import vulnscan_daily as daily
from kd.cabinet.client import CabinetError

NOW = datetime.datetime(2026, 11, 23, 4, 30, tzinfo=datetime.timezone.utc).timestamp()
HASH = "    --hash=sha256:" + "0" * 64 + "\n"
HOST = {
    "forge_ssh_url": "ssh://git@forge.example.test/{repo}.git",
    "owner_language": "ru",
}
REPOS = {
    "digipaddock": ("example-owner/digipaddock", "main"),
    "shop": ("example-owner/shop", "master"),
}
TREES = {
    "digipaddock": {
        "common/x/requirements.txt": "requests==2.19.0 \\\n" + HASH,
        "requirements-dev.txt": "pytest>=8\n",
        "README.md": "x\n",
    },
    "shop": {"composer.lock": "{}", "osv-scanner.toml": ""},
}
FINDING = {
    "id": "CVE-2018-18074",
    "ids": ["CVE-2018-18074", "GHSA-x84v-xcm2-53pg"],
    "package": "requests",
    "ecosystem": "PyPI",
    "version": "2.19.0",
    "fixed": ["2.20.0"],
    "path": "common/x/requirements.txt",
    "summary": "Requests leaks the Authorization header",
}
SHOP = {
    "id": "CVE-2026-1",
    "ids": ["CVE-2026-1"],
    "package": "guzzlehttp/guzzle",
    "ecosystem": "Packagist",
    "version": "7.0.0",
    "fixed": [],
    "path": "composer.lock",
    "summary": "",
}


class Git:
    def __init__(self, trees):
        self.trees, self.calls = trees, []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] == "init":
            os.makedirs(os.path.join(argv[2], ".git"))
            return 0, ""
        path, command = argv[1], argv[2]
        tree = self.trees[os.path.basename(path)]
        if command == "fetch":
            return 0, ""
        if command == "ls-tree":
            return 0, "\0".join(tree) + "\0"
        name = argv[3].split(":", 1)[1]
        return (
            (0, tree[name])
            if name in tree
            else (128, f"fatal: path '{name}' does not exist")
        )


class Units:
    def __init__(self, findings, *, update_rc=0):
        self.findings, self.update_rc, self.calls = findings, update_rc, []

    def __call__(self, argv):
        self.calls.append(argv)
        command = ci_unit.command_of(argv)[3:]
        if command == ["update"]:
            return self.update_rc, "vulnscan: PyPI: 10 advisories, 1 MiB\n"
        project = os.path.basename(command[1])
        locks = sorted(
            os.path.relpath(os.path.join(top, n), command[1])
            for top, _, files in os.walk(command[1])
            for n in files
        )
        return 0, json.dumps(
            {
                "db": {"updated": NOW, "age_s": 0},
                "locks": locks,
                "findings": self.findings.get(project, []),
            }
        ) + "\n"


class Client:
    def __init__(self, fail=0):
        self.calls, self.fail = [], fail

    def call(self, request):
        self.calls.append(request)
        if self.fail:
            self.fail -= 1
            raise CabinetError("unavailable", "board unavailable")
        return {"card": {"id": f"KD-{900 + len(self.calls)}", "revision": 1}}


def _db(osv, updated):
    db = osv / "db-1"
    for eco in vs.ECOSYSTEMS:
        (db / vs.DB_LAYOUT / eco).mkdir(parents=True, exist_ok=True)
        (db / vs.DB_LAYOUT / eco / "all.zip").write_bytes(b"zip")
    (db / vs.STAMP).write_text(json.dumps({"updated": updated}), encoding="utf-8")
    if not os.path.lexists(osv / "current"):
        os.symlink("db-1", osv / "current")


@pytest.fixture
def env(tmp_path):
    osv = tmp_path / "osv"
    _db(osv, NOW - 3600)
    lines = []

    def make(findings=None, *, client=None, units=None, now=NOW, trees=TREES):
        return daily.Daily(
            units=units
            or Units(findings or {"digipaddock": [FINDING], "shop": [SHOP]}),
            git=Git(trees),
            client=client or Client(),
            repos=REPOS,
            host=HOST,
            ci_user="digi-ci",
            state_path=str(tmp_path / "state.json"),
            work=str(tmp_path / "work"),
            osv_dir=str(osv),
            deployed="/opt/digipaddock",
            now=lambda: now,
            log=lines.append,
        )

    make.lines, make.tmp, make.osv = lines, tmp_path, osv
    return make


def test_the_children_run_as_the_ci_user_the_download_alone_with_network(env):
    run = env()
    assert run.run() == 0
    update, *scans = run.units.calls
    assert update[:4] == ["systemd-run", "--unit", "digipaddock-vulnscan-db", "--wait"]
    for argv in (update, *scans):
        props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
        # the ci user in a pid namespace of its own (KD-9473), never User= in the host's
        assert (
            not any(p.startswith("User=") for p in props)
            and "ProtectSystem=strict" in props
            and "NoNewPrivileges=yes" in props
        )
        assert "--reuid=digi-ci" in argv and argv[argv.index("--") + 1] == ci_unit.UNSHARE
        assert any(
            p.startswith("InaccessiblePaths=") and "-/etc/digipaddock" in p
            for p in props
        )
        assert ci_unit.command_of(argv)[:3] == [
            "/usr/bin/python3",
            "-I",
            "/opt/digipaddock/common/kd/vuln_scan.py",
        ]
    props = [update[i + 1] for i, a in enumerate(update) if a == "-p"]
    assert "PrivateNetwork=yes" not in props and f"ReadWritePaths={env.osv}" in props
    for argv in scans:
        props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
        assert "PrivateNetwork=yes" in props and not any(
            p.startswith("ReadWritePaths=") for p in props
        )
    assert [a[2] for a in scans] == [
        "digipaddock-vulnscan-scan-digipaddock",
        "digipaddock-vulnscan-scan-shop",
    ]


def test_root_fetches_the_base_branch_and_hands_only_its_lock_files_to_the_scan(env):
    run = env()
    run.run()
    fetches = [a for a in run.git.calls if a[2:3] == ["fetch"]]
    assert fetches[0][3:] == [
        "-q",
        "--no-tags",
        "ssh://git@forge.example.test/example-owner/digipaddock.git",
        "refs/heads/main",
    ]
    assert (
        fetches[1][-1] == "refs/heads/master"
    )  # the project's base branch, whatever its name
    scan = Path(env.tmp / "work" / "scan" / "digipaddock")
    assert sorted(p.name for p in scan.rglob("*") if p.is_file()) == [
        "requirements.txt"
    ]  # not the dev ranges, no docs
    assert oct(scan.stat().st_mode & 0o777) == "0o755"
    assert oct((env.tmp / "work" / "git").stat().st_mode & 0o777) == "0o700"


def test_a_new_finding_is_one_card_in_the_projects_inbox_and_a_run_again_makes_none(
    env,
):
    client = Client()
    assert env(client=client).run() == 0
    first, second = client.calls
    # the developer's in every project, not the project's route (a project of the owner's routes to him)
    assert first["op"] == "card.create" and first["project"] == "digipaddock"
    assert {c["assignee"] for c in client.calls} == {"@dev"}
    assert first["title"] == "Уязвимость CVE-2018-18074 в requests 2.19.0 (digipaddock)"
    for needle in (
        "`CVE-2018-18074` (также `GHSA-x84v-xcm2-53pg`)",
        "`requests` 2.19.0 (PyPI)",
        "Исправлено в: 2.20.0",
        "`common/x/requirements.txt`",
        "ветке `main`",
        "https://osv.dev/vulnerability/CVE-2018-18074",
        "Requests leaks the Authorization header",
    ):
        assert needle in first["description"], needle
    assert (
        second["project"] == "shop"
        and "исправленной версии нет" in second["description"]
    )
    assert "ветке `master`" in second["description"]
    state = json.loads((env.tmp / "state.json").read_text(encoding="utf-8"))
    assert (
        state["seen"]["digipaddock|PyPI|requests|GHSA-x84v-xcm2-53pg"]["card"]
        == "KD-901"
    )
    again = Client()
    alias = dict(
        FINDING, id="GHSA-x84v-xcm2-53pg", ids=["GHSA-x84v-xcm2-53pg", "PYSEC-2018-28"]
    )  # a new alias
    assert env({"digipaddock": [alias], "shop": [SHOP]}, client=again).run() == 0
    assert again.calls == []
    assert "kd-vulnscan: digipaddock: 1 lock files, 1 findings" in env.lines


def test_a_finding_under_a_valid_exception_makes_no_card_and_one_past_its_date_does(
    env,
):
    toml = '[[IgnoredVulns]]\nid = "CVE-2026-1"\nignoreUntil = 2026-12-01\nreason = "not reachable"\n'
    trees = {**TREES, "shop": {"composer.lock": "{}", "osv-scanner.toml": toml}}
    client = Client()
    env(client=client, trees=trees).run()
    assert [c["project"] for c in client.calls] == ["digipaddock"]
    later = Client()
    _db(env.osv, NOW + 9 * 86400)
    env(client=later, trees=trees, now=NOW + 9 * 86400).run()
    assert [c["project"] for c in later.calls] == ["shop"]
    no_reason = {
        **TREES,
        "shop": {
            "composer.lock": "{}",
            "osv-scanner.toml": toml.replace('reason = "not reachable"\n', ""),
        },
    }
    (env.tmp / "state.json").unlink()
    third = Client()
    env(client=third, trees=no_reason).run()
    assert [c["project"] for c in third.calls] == ["digipaddock", "shop"]
    assert (
        "kd-vulnscan: shop: osv-scanner.toml: entry 1 (CVE-2026-1): no reason"
        in env.lines
    )


def test_a_card_the_cabinet_refused_is_tried_again_next_run(env):
    client = Client(fail=1)
    env(client=client).run()
    assert len(client.calls) == 2 and any(
        "not made (unavailable" in line for line in env.lines
    )
    again = Client()
    env(client=again).run()
    assert [c["project"] for c in again.calls] == ["digipaddock"]


def test_at_most_max_cards_a_run(env, monkeypatch):
    monkeypatch.setattr(daily, "MAX_CARDS", 1)
    client = Client()
    env(client=client).run()
    assert len(client.calls) == 1
    assert "kd-vulnscan: 1 findings wait for their cards (at most 1 a run)" in env.lines


def test_a_failed_download_is_in_the_journal_and_fails_the_run(env):
    run = env(units=Units({}, update_rc=1))
    assert run.run() == 1
    assert "kd-vulnscan: the OSV database update failed (exit 1)" in env.lines
    assert run.client.calls == []  # the database is still fresh: no «not updated» card


def test_a_stale_database_gives_one_card_until_it_is_fresh_again(env):
    client = Client()
    old = NOW + vs.MAX_AGE_S  # the last download was an hour before NOW
    assert env({}, client=client, units=Units({}, update_rc=1), now=old).run() == 1
    (card,) = client.calls
    assert (
        card["project"] == "digipaddock"
        and card["title"] == "База уязвимостей OSV не обновляется"
    )
    assert "73 h old" in card["description"] and "72 ч" in card["description"]
    again = Client()
    env({}, client=again, units=Units({}, update_rc=1), now=old + 86400).run()
    assert again.calls == []  # one card while it stays stale
    _db(env.osv, old + 86400)
    env({}, client=Client(), now=old + 86400).run()
    assert "stale" not in json.loads(
        (env.tmp / "state.json").read_text(encoding="utf-8")
    )
    later = Client()
    env({}, client=later, units=Units({}, update_rc=1), now=old + 5 * 86400).run()
    assert len(later.calls) == 1  # stale again later: a new card


def test_a_project_that_fails_is_reported_and_the_others_still_run(env):
    trees = {**TREES, "digipaddock": None}

    class Broken(Git):
        def __call__(self, argv):
            if "digipaddock" in " ".join(argv) and argv[2:3] == ["fetch"]:
                return 128, "fatal: could not read from remote repository"
            return super().__call__(argv)

    run = env(trees=trees)
    run.git = Broken(trees)
    assert run.run() == 1
    assert any(
        line.startswith(
            "kd-vulnscan: digipaddock: not scanned (ScanError: git fetch: exit 128"
        )
        for line in env.lines
    )
    assert [c["project"] for c in run.client.calls] == ["shop"]


def test_english_texts_for_an_english_deployment(env):
    run = env()
    run.lang = "en"
    run.run()
    assert (
        run.client.calls[0]["title"]
        == "Vulnerability CVE-2018-18074 in requests 2.19.0 (digipaddock)"
    )


def test_main_needs_run(capsys):
    assert daily.main([]) == 2 and daily.main(["scan"]) == 2


def test_main_refuses_to_run_without_the_isolation_tools(monkeypatch, capsys):
    monkeypatch.setattr(daily.ci_unit, "missing", lambda: [ci_unit.UNSHARE])
    assert daily.main(["run"]) == 1 and ci_unit.UNSHARE in capsys.readouterr().err


def test_the_units_and_the_installer_agree():
    root = Path(__file__).resolve().parents[2]
    service = (root / "common/systemd/digipaddock-vulnscan.service").read_text(
        encoding="utf-8"
    )
    timer = (root / "common/systemd/digipaddock-vulnscan.timer").read_text(
        encoding="utf-8"
    )
    assert (
        "ExecStart=/usr/bin/python3 -I /opt/digipaddock/common/bin/kd-vulnscan run"
        in service
    )
    assert "OnFailure=digipaddock-alert@digipaddock-vulnscan.service" in service
    assert (
        "OnCalendar=*-*-* 04:20:00 @host:timezone@" in timer
        and "Persistent=true" in timer
    )
    setup = (root / "common/install/devcheck_setup.sh").read_text(encoding="utf-8")
    assert f"\nSTATE_DIR={os.path.dirname(daily.STATE)}\n" in setup
    assert (
        "\nSCAN_WORK=$WORK/vulnscan\n" in setup
        and daily.WORK == "/var/lib/digipaddock/devcheck/vulnscan"
    )
    assert (
        "\nOSV_DIR=$WORK/osv\n" in setup
        and vs.OSV_DIR == "/var/lib/digipaddock/devcheck/osv"
    )
    assert f"\nOSV_BIN={vs.SCANNER}\n" in setup
