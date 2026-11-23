"""devcheck_setup.sh — the ci user the checks run as, its directories, the token's
place, the switch; and the factory component that runs it."""

import re
from pathlib import Path

from kd import devcheck_runner, factory_ops, naming

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "common" / "install" / "devcheck_setup.sh"
TEXT = SCRIPT.read_text(encoding="utf-8") if SCRIPT.exists() else ""


def _mode(mode: str) -> str:
    cases = TEXT[TEXT.rindex("\ncase $mode in"):]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


def _function(name: str) -> str:
    return TEXT.split(f"\n{name}() {{", 1)[1].split("\n}", 1)[0]


def test_the_ci_role_is_a_service_user():
    assert "ci" in naming.SERVICE_ROLES
    assert naming.service_user("ci", "digi") == "digi-ci"
    assert naming.render("User=@ci@", "digi") == "User=digi-ci"
    assert "ci" in naming.RESERVED_AGENTS  # no agent takes the name


def test_the_script_knows_its_modes_and_runs_as_root():
    assert TEXT.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in TEXT
    assert 'USAGE="usage: devcheck_setup.sh install|check|disable"' in TEXT
    assert "  install|check|disable) ;;" in TEXT
    assert '[ "$(id -u)" = 0 ] || { echo "root is required" >&2; exit 1; }' in TEXT
    assert "ci=$(kd_user ci)" in TEXT and "observe=$(kd_user observe)" in TEXT


def test_the_paths_are_the_runners():
    for name, value in (("WORK", devcheck_runner.WORK), ("LOGS", devcheck_runner.LOG_DIR),
                        ("TOKEN", devcheck_runner.TOKEN)):
        assert f"\n{name}={value}\n" in TEXT, name
    assert "\nETC=$(dirname \"$TOKEN\")\n" in TEXT and "\nHOME_DIR=$WORK/home\n" in TEXT


def test_the_ci_user_has_no_login_and_no_groups():
    install = _function("install_all")
    useradd = next(line for line in install.splitlines() if "useradd" in line)
    assert useradd.strip() == ('useradd --system --no-create-home --home-dir "$HOME_DIR" --user-group '
                               '--shell /usr/sbin/nologin "$ci"')
    assert not re.search(r"usermod|gpasswd|-G\b|--groups", install)  # never digipaddock-cabinet, systemd-journal or any other group
    assert "/etc/digipaddock/cap" not in TEXT  # no capability channel of its own


def test_install_makes_the_directories_with_their_owners_and_modes():
    install = _function("install_all")
    for line in ('install -d -o root -g root -m 0755 "$WORK"', 'install -d -o root -g root -m 0755 "$WORK/cache"',
                 'install -d -o root -g "$observe" -m 0750 "$LOGS"', 'install -d -o root -g root -m 0700 "$ETC"'):
        assert line in install, line
    # the observe user comes with host.read; without it the log has no reader
    assert 'id "$observe" >/dev/null 2>&1 || {' in install
    assert "TOKEN" not in install  # the token is root's to put there (put_secret.sh), never written here


def test_check_names_every_problem_and_only_warns_of_the_token():
    check = _function("check")
    for needle in ('"no user $ci"', '"$ci: shell is not nologin"', '"$ci: home directory is not $HOME_DIR"',
                   '"$ci: extra groups:', '"$WORK: must be root:root 755"', '"$WORK/cache: must be root:root 755"',
                   '"$LOGS: must be root:$observe 750"', '"$ETC: must be root:root 700"', '"no npm"',
                   '"no python3 -m venv"'):
        assert needle in check, needle
    assert 'id -nG "$ci"' in check
    token = check.split('if [ ! -e "$TOKEN" ]', 1)[1].split("fi\n", 1)[0]
    assert "warning" in token and "problems+=" not in token.split("else", 1)[0]
    assert '"$TOKEN: must be root:root 600"' in check  # present — then a secret's mode
    assert 'printf \'devcheck: %s\\n\' "${problems[@]}" >&2' in check and "exit 1" in check
    assert 'echo "devcheck: ok"' in check


def test_install_ends_with_check_and_disable_takes_the_contours_out():
    assert _mode("install").split() == ["install_all", "check"]
    assert _mode("check").split() == ["check"]
    disable = _function("disable_all")
    # the list first, as an assignment: with the cabinet down disable fails instead of disabling nothing
    assert "names=$(kd-cabinet contour list | cut -d' ' -f1)" in disable and 'for name in $names; do' in disable
    assert 'kd-cabinet contour maintenance "$name" --reason "devcheck_setup.sh disable"' in disable
    for gone in ("rm ", "userdel", "TOKEN"):
        assert gone not in disable, gone  # data, the user and the token stay
    assert _mode("disable").split() == ["disable_all"]


def test_install_takes_the_pinned_osv_scanner_and_turns_the_daily_scan_on():
    install = _function("install_all")
    assert install.index("install_osv") < install.index("systemctl enable --now digipaddock-vulnscan.timer")
    for line in ('install -d -o "$ci" -g "$ci" -m 0755 "$OSV_DIR"', 'install -d -o root -g root -m 0755 "$SCAN_WORK"',
                 'install -d -o root -g root -m 0700 "$STATE_DIR"',
                 'kd_install_rendered "$REPO/common/systemd/$unit" "$SYSTEMD/$unit" 0644',
                 "systemctl start --no-block digipaddock-vulnscan.service"):
        assert line in install, line
    assert '\nVULNSCAN_UNITS="digipaddock-vulnscan.service digipaddock-vulnscan.timer"\n' in TEXT
    for unit in ("digipaddock-vulnscan.service", "digipaddock-vulnscan.timer"):
        assert (ROOT / "common" / "systemd" / unit).exists()
    osv = _function("install_osv")
    # the version and the sha256 of versions.json; a download that differs is never installed
    assert 'version=$(catalog osv_scanner)' in osv and 'want=$(catalog "osv_scanner_sha256_$arch")' in osv
    assert ('"https://github.com/google/osv-scanner/releases/download/v$version/osv-scanner_linux_$arch"' in osv
            and "--proto '=https'" in osv)
    assert osv.index('!= "$want" ]') < osv.index('install -o root -g root -m 0755 "$tmp" "$OSV_BIN"')
    check = _function("check")
    for needle in ('"$OSV_BIN: not osv-scanner $(catalog osv_scanner) of versions.json (sha256)"',
                   '"$OSV_DIR: must be $ci:$ci 755"', '"$STATE_DIR: must be root:root 700"',
                   '"digipaddock-vulnscan.timer is not enabled"'):
        assert needle in check, needle
    assert "no OSV database yet" in check and "warning" in check  # the first download may still run


def test_the_factory_installs_and_checks_it():
    cat = factory_ops.load_catalog()
    assert set(cat.components["devcheck"]) == {"install", "check"}
    assert cat.mode("devcheck", "install").run == ("common/install/devcheck_setup.sh", "install")
    assert cat.mode("devcheck", "install").check == ("common/install/devcheck_setup.sh", "check")
    assert cat.mode("devcheck", "check").run == ("common/install/devcheck_setup.sh", "check")
