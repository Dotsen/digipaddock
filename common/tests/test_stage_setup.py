"""stage_setup.sh: the pinned Claude Code, a read-only --check, origin and NAT from the overlay."""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "common" / "install"
POSIX = os.name == "posix" and Path("/usr/bin/python3").exists() and shutil.which("bash") is not None


def _text(name="stage_setup.sh"):
    return (INSTALL / name).read_text(encoding="utf-8")


def test_stage_setup_pins_claude_code_from_the_catalog():
    text = _text()
    assert 'claude_version=$(/usr/bin/python3 -I "$REPO/common/kd/install_catalog.py" version claude_code)' in text
    assert "install.sh | bash -s '$claude_version'" in text
    assert "install.sh | bash'" not in text  # never «the latest»


def test_stage_setup_check_changes_nothing():
    text = _text()
    start = text.index("stage_check() {")
    end = text.index('if [ -n "$check" ]; then')
    # the one git call of the check reads a mirror's config
    block = text[start:end].replace('git -C "$mirror" config --get remote.origin.mirror', "")
    for change in ("install ", "useradd", "apt-get", "ln -s", "systemctl enable", "systemctl start", "git -C",
                   "printf '{", "chmod", "chown", "> "):
        assert change not in block.replace("2>/dev/null", "").replace(">/dev/null", ""), change
    assert 'bash "$REPO/common/install/cabinet_setup.sh" check' in block
    # the check runs before anything the install writes, and outside the monitor's expectation window
    run = text.index('if [ -n "$check" ]; then')
    assert run < text.index("kd_expect_run") and run < text.index("install -d -o root -g root -m 0755 /srv/digipaddock-agents")
    assert '[ -z "$check" ] || [ -z "$prefix$commands$cards$origin" ] || usage' in text


def test_another_claude_code_version_is_a_warning_of_the_check():
    """The full run never changes an installed version, so a failing check would run it
    (and restart the cabinet) on every bootstrap during the window of a new version's probes. No claude fails."""
    text = _text()
    block = text[text.index("stage_check() {"):text.index('if [ -n "$check" ]; then')]
    assert '[ -n "$have" ] || problems+=("no Claude Code for $base' in block
    assert re.search(r'\[ "\$have" = "\$claude_version" \] \|\| \[ -z "\$have" \] \\\n\s+\|\| echo "warning: '
                     r'Claude Code for \$base', block)
    assert 'problems+=("Claude Code for $base' not in block


def test_origin_defaults_to_host_json():
    text = _text()
    assert "origin=${origin:-$(kd_host cabinet_origin)}" in text
    assert text.index("origin=${origin:-$(kd_host cabinet_origin)}") < text.index('cabinet_setup.sh" install')


def test_both_installers_ask_the_one_factory_host_check():
    for name in ("stage_setup.sh", "board_setup.sh"):
        text = _text(name)
        assert "kd_factory_host || exit 1" in text, name
        assert "hostname -I" not in text, name
    lib = _text("lib/naming.sh")
    assert "kd_factory_host() {" in lib and 'getent ahosts "$(kd_host cabinet_host)"' in lib


def _factory_tree(tmp_path, behind_nat):
    tree = tmp_path / "tree"
    (tree / "common" / "kd").mkdir(parents=True)
    (tree / "common" / "install" / "lib").mkdir(parents=True)
    (tree / "host").mkdir()
    shutil.copy(REPO / "common" / "kd" / "naming.py", tree / "common" / "kd" / "naming.py")
    shutil.copy(REPO / "common" / "naming.default.json", tree / "common" / "naming.default.json")
    shutil.copy(INSTALL / "lib" / "naming.sh", tree / "common" / "install" / "lib" / "naming.sh")
    shutil.copy(REPO / "host" / "host.example.json", tree / "host" / "host.json")
    if behind_nat is not None:
        install = json.loads((REPO / "host" / "install.example.json").read_text(encoding="utf-8"))
        install["behind_nat"] = behind_nat
        (tree / "host" / "install.json").write_text(json.dumps(install), encoding="utf-8")
    return tree


def _factory(tree, interfaces, resolved):
    script = ('hostname() { echo "$KD_IFACES"; }\ngetent() { printf "%s STREAM x\\n" $KD_RESOLVED; }\n'
              '. "$1/common/install/lib/naming.sh"\nkd_factory_host\n')
    env = {k: v for k, v in os.environ.items() if k not in ("KD_HOST_JSON", "KD_NAMING_JSON")}
    env.update(KD_IFACES=interfaces, KD_RESOLVED=resolved)
    return subprocess.run(["bash", "-c", script, "_", str(tree)], capture_output=True, text=True, env=env, timeout=30)


@pytest.mark.skipif(not POSIX, reason="/usr/bin/python3 and bash: Linux")
@pytest.mark.parametrize("behind_nat,interfaces,resolved,ok", [
    (None, "203.0.113.10 10.0.0.2", "", True),       # the address on an interface: as before
    (None, "10.0.0.2", "203.0.113.10", False),       # DNS alone is not enough without behind_nat
    (False, "10.0.0.2", "203.0.113.10", False),
    (True, "10.0.0.2", "203.0.113.10", True),        # behind NAT: the cabinet's name resolves to stage_ip
    (True, "10.0.0.2", "198.51.100.7", False),
])
def test_behind_nat_checks_dns_instead_of_interfaces(tmp_path, behind_nat, interfaces, resolved, ok):
    proc = _factory(_factory_tree(tmp_path, behind_nat), interfaces, resolved)
    assert (proc.returncode == 0) is ok, proc.stderr
    if not ok:
        assert "this is not the factory host (203.0.113.10)" in proc.stderr


def test_the_host_check_reads_install_json_of_its_own_tree():
    lib = _text("lib/naming.sh")
    assert re.search(r'"\$\{KD_NAMING_PY%/common/kd/naming\.py\}/host/install\.json"', lib)


def test_new_naming_json_is_checked_before_it_is_installed():
    """10.3: a busy command prefix (--commands git) or users named like commands never reach /etc."""
    text = _text()
    write = text.index("printf '{\"schema_version\": 1, \"prefix\"")
    check = text.index('KD_NAMING_JSON="$tmp" kd_names >/dev/null')
    assert write < check < text.index('install -o root -g root -m 0644 "$tmp" "$NAMING"')


@pytest.mark.parametrize("names", [{"prefix": "acme", "commands": "git"}, {"prefix": "kd"}])
def test_naming_names_refuses_what_load_refuses(tmp_path, names):
    config = tmp_path / "naming.json"
    config.write_text(json.dumps({"schema_version": 1, **names}), encoding="utf-8")
    env = {**os.environ, "KD_NAMING_JSON": str(config)}
    proc = subprocess.run([os.sys.executable, "-I", str(REPO / "common/kd/naming.py"), "names"], env=env,
                          capture_output=True, text=True)
    assert proc.returncode != 0 and not proc.stdout
