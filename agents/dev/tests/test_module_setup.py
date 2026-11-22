"""Module setup scripts of forge.review and git.develop (M01): the steps lifted from agent_setup.sh."""

import os
import re
import io
import shutil
import subprocess
from pathlib import Path

import pytest
from types import SimpleNamespace

INSTALL = Path(__file__).resolve().parents[1] / "install"
COMMON = Path(__file__).resolve().parents[3] / "common"
FORGE = (INSTALL / "forge_setup.sh").read_text(encoding="utf-8")
DEVELOP = (INSTALL / "develop_setup.sh").read_text(encoding="utf-8")
MIRROR = (INSTALL / "mirror_read_setup.sh").read_text(encoding="utf-8")
BASH = next(
    (
        c
        for c in (r"C:\Program Files\Git\bin\bash.exe", shutil.which("bash"))
        if c and Path(c).exists()
    ),
    None,
)


def _function(text: str, name: str) -> str:
    return re.search(rf"^{name}\(\) \{{\n.*?^\}}\n", text, re.M | re.S).group(0)


def _mode(text: str, mode: str) -> str:
    cases = text[text.rindex("\ncase $mode in") :]
    return re.search(rf"^  {mode}\)(.*?);;", cases, re.M | re.S).group(1)


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("script", ["forge_setup.sh", "develop_setup.sh", "mirror_read_setup.sh"])
@pytest.mark.parametrize(
    "args",
    [
        [],
        ["bogus", "dev", "digi-dev"],
        ["install", "../x", "digi-dev"],
        ["check", "dev", "Agent Dev"],
    ],
)
def test_usage_is_refused_before_anything(script: str, args: list[str]) -> None:
    proc = subprocess.run(
        [BASH, str(INSTALL / script), *args], capture_output=True, text=True
    )
    assert proc.returncode == 2 and "usage:" in proc.stderr


@pytest.mark.parametrize("text", [FORGE, DEVELOP, MIRROR], ids=["forge", "develop", "mirror"])
def test_install_verifies_with_its_own_check(text: str) -> None:
    install = [line.strip() for line in _mode(text, "install").splitlines()]
    assert "check" in install
    assert _mode(text, "check").strip() == "check"
    assert "\ncd /\n" in text and text.index("\ncd /\n") < text.index("runuser -u")


# --- forge.review -------------------------------------------------------------------------


def test_forge_dirs_acl_and_units() -> None:
    assert (
        'install -d -o root -g "$forge" -m 0750 "$SECRETS"' in FORGE
        and "SECRETS=/etc/digipaddock/forge" in FORGE
    )
    # the release service is the host's (stage_setup.sh); the module only hands the requests to its user
    assert (
        'install -d -o "$forge" -g root -m 0750 /var/lib/digipaddock/release'
        in FORGE
    )
    # the preflight only looks whether a mirror is there; making mirrors is the host's
    for moved in ("release-result", 'install -d -o root -g root -m 0700 "$MIRRORS"', "digipaddock-release.path",
                  "digipaddock-release.service", "RELEASE_LINK"):
        assert moved not in FORGE, moved
    assert FORGE.count("$MIRRORS") == 1
    # The forge user is the executor user new_agent.sh creates; its name comes from naming.sh and the
    # script
    # never makes or removes users.
    assert '. "$REPO/common/install/lib/naming.sh"\n' in FORGE and '\nforge=$(kd_user forge)\n' in FORGE
    assert "useradd" not in FORGE and "userdel" not in FORGE


def test_forge_setup_grants_the_cabinet_group_and_no_pult_acl() -> None:
    """merge and release reports go to the card's thread through the cabinet; pult.env is root's."""
    install, check = _function(FORGE, "install_all"), _function(FORGE, "check")
    assert '"$PULT"' not in install
    assert 'bash "$CABINET" grant "$forge"' in install and "CABINET=$REPO/common/install/cabinet_setup.sh" in FORGE
    # the board's socket group is the product's, not a command name
    assert ('[[ " $(id -nG "$forge") " == *" digipaddock-cabinet "* ]] || '
            'problems+=("$forge is not in group digipaddock-cabinet")') in check
    assert '! runuser -u "$forge" -- test -r "$PULT" || problems+=("$forge reads $PULT")' in check


def test_forge_journal_is_readable_by_the_agents_group_and_fixed_without_following_links() -> (
    None
):
    journal = _function(FORGE, "install_journal")
    assert 'install -d -o "$forge" -g "$user" -m 2750 "$JOURNAL"' in journal
    assert (
        'install -o "$forge" -g "$user" -m 0640 /dev/null "$JOURNAL/forge.jsonl"'
        in journal
    )
    snippet = journal.split("<<'JOURNAL'\n", 1)[1].split("\nJOURNAL\n", 1)[0]
    assert (
        "O_NOFOLLOW" in snippet
        and "S_ISREG" in snippet
        and "os.fchown" in snippet
        and "os.fchmod(fd, 0o640)" in snippet
    )
    assert not re.search(r"^\s*(chown|chmod|chgrp)\b.*JOURNAL", FORGE, re.M)
    assert (
        "su $forge $user" in journal and "create 0640 $forge $user" in journal
    )
    assert "/var/log/digipaddock/" not in FORGE and "digi-exec" not in FORGE


def test_forge_deploy_key_preflight_comes_before_the_mirror_clone() -> None:
    assert "/root/.ssh/forgejo_deploy" in FORGE and '[ -s "$KNOWN" ]' in FORGE
    assert (
        "git ls-remote" in FORGE
        and "refs/heads/main" in FORGE
        and "forgejo_deploy.pub" in FORGE
    )
    assert FORGE.index("git ls-remote") < FORGE.index("own_requests\n  if")


def test_forge_mirror_is_a_real_mirror_fetched_with_kd_release_ssh_options() -> None:
    line = re.search(r'^ROOT_SSH="(.*)"$', FORGE, re.M).group(1)
    release = (COMMON / "bin/kd-release").read_text(encoding="utf-8")
    block = release.split("def _git_ssh(known_hosts):", 1)[1].split("\n\n", 1)[0]
    assert "GIT_SSH = _git_ssh(KNOWN_HOSTS)" in release

    def opts(s: str) -> set[str]:
        return {o.strip("\"')") for o in re.findall(r"-o (\S+)", s)}

    pinned = {"UserKnownHostsFile={known_hosts}": "UserKnownHostsFile=$KNOWN"}
    assert {pinned.get(o, o) for o in opts(block)} == opts(line)
    assert line.startswith("ssh -i /root/.ssh/forgejo_deploy ")
    # kd-release clones each release repository's mirror itself; the host's check verifies an existing one
    # (stage_setup.sh, with the release service)
    stage = (COMMON / "install/stage_setup.sh").read_text(encoding="utf-8")
    assert 'git -C "$mirror" config --get remote.origin.mirror' in stage
    assert '"git", "clone", "--mirror"' in release and "timeout 300 git clone" not in FORGE
    assert 'ls-remote "$url" refs/heads/main' in FORGE and "kd_host forge_ssh_url" in FORGE


def test_forge_tokens_are_hints_and_selfcheck_a_warning() -> None:
    # Token files follow the Forgejo accounts, which keep their names after the rename.
    assert 'dev_token="$(kd_host forge_dev_user).env"' in FORGE and 'merge_token="$(kd_host forge_merge_user).env"' in FORGE
    assert 'for token in "$dev_token" "$merge_token"; do' in FORGE and "put_secret.sh" in FORGE
    assert (
        'runuser -u "$forge" -- "$DEV/bin/forge_exec.py" --agent setup selfcheck digipaddock'
        in FORGE
    )
    # missing tokens do not fail the check: the owner puts them after the install
    assert ".env" not in _function(FORGE, "check")


def test_forge_disable_hands_the_requests_back_to_root() -> None:
    """disabling dev stops its release requests (kd-merge --release); root's releases go on."""
    block = _mode(FORGE, "disable")
    assert "chown root:root /var/lib/digipaddock/release" in block
    assert "systemctl" not in block
    assert not re.search(r"\b(rm|userdel|setfacl)\b", block)


# --- git.develop --------------------------------------------------------------------------


def test_develop_key_and_ssh_config_are_made_by_the_agents_user_with_the_pinned_host_key() -> (
    None
):
    key = _function(DEVELOP, "install_key")
    assert (
        key.lstrip().startswith("install_key() {")
        and 'runuser -u "$user" -- bash -euc' in key
    )
    assert '_ "$agent" "/home/$user" "$FORGEJO_HOST" "$KNOWN"' in key
    assert (
        'ssh-keygen -q -t ed25519 -N ""' in key
        and "IdentitiesOnly yes" in key
        and "StrictHostKeyChecking yes" in key
    )
    assert "UserKnownHostsFile %s" in key and "mv -f" in key
    assert "ssh-keyscan" not in DEVELOP and "/home/digi/" not in DEVELOP


def test_root_never_writes_inside_the_agents_home_or_work_by_name() -> None:
    for line in DEVELOP.splitlines():
        if re.search(
            r"/home/\$user|\$TEXTS|\$AGENTS", line
        ) and not line.lstrip().startswith("#"):
            assert (
                'runuser -u "$user"' in line
                or line.startswith(("TEXTS=", "AGENTS="))
                or "problems+=" in line
                or line.strip().startswith(("' _ ", "_ "))
            ), line


def test_develop_disable_changes_nothing() -> None:
    assert '[ "$mode" != disable ] || exit 0' in DEVELOP
    assert DEVELOP.index('[ "$mode" != disable ] || exit 0') < DEVELOP.index(
        "install_key()"
    )


# --- git.mirror ------------------------------------------------------------------


def test_mirror_key_is_the_forge_users_own_never_the_agents() -> None:
    # review 04.11.2026: the developer's sandbox holds no key for the mirror and reaches no mirror host
    block = _function(MIRROR, "install_key")
    assert "KEY=$FORGE_DIR/mirror_ed25519\n" in MIRROR and "FORGE_DIR=/etc/digipaddock/forge\n" in MIRROR
    assert 'chown -h "$forge:$forge" "$KEY" "$KEY.pub"' in block and 'chmod 0600 "$KEY"' in block
    assert "ssh-keyscan" not in MIRROR and "/home/$user/.ssh" not in MIRROR
    assert (COMMON / "install" / "github_known_hosts").is_file()


def test_mirror_copy_is_the_forge_users_bare_repository() -> None:
    block = _function(MIRROR, "install_copy")
    assert "COPY=$MIRROR_ROOT/code.git\n" in MIRROR and "MIRROR_ROOT=/srv/digipaddock-mirror\n" in MIRROR
    assert 'install -d -o root -g root -m 0755 "$MIRROR_ROOT"' in block
    assert 'runuser -u "$forge" -- "${FORGE_GIT[@]}" init --quiet --bare "$COPY"' in block
    assert "GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null" in MIRROR and "core.hooksPath=/dev/null" in MIRROR


def test_mirror_remote_is_the_copy_fetch_only_and_written_by_the_agents_user() -> None:
    block = _function(MIRROR, "install_remote")
    assert 'runuser -u "$user" -- bash -euc' in block and '_ "$DIR" "$REMOTE" "$COPY"' in block
    assert '"+refs/heads/*:refs/remotes/$name/*"' in block and '"remote.$name.tagOpt" --no-tags' in block
    assert 'git config --global --add safe.directory "$url"' in block
    assert "REMOTE=github\n" in MIRROR and "push" not in block
    assert 'it.get("release") == "digipaddock" and it.get("role", "code") == "code"' in MIRROR
    assert "DIR=${CLONE:+$AGENTS/$agent/$CLONE}\n" in MIRROR


def test_mirror_root_never_writes_inside_the_agents_work_by_name() -> None:
    for line in MIRROR.splitlines():
        if re.search(r"\$DIR|\$AGENTS", line) and not line.lstrip().startswith("#"):
            assert (
                'runuser -u "$user"' in line
                or line.startswith(("DIR=", "AGENTS="))
                or "problems+=" in line
                or line.strip().startswith(("' _ ", "_ ", 'if [ -n "$DIR" ]'))
            ), line


def test_mirror_disable_changes_nothing() -> None:
    assert MIRROR.index('[ "$mode" != disable ] || exit 0') < MIRROR.index("install_key()")


def test_the_executor_fetches_one_branch_with_its_own_key_and_no_hook() -> None:
    from dev_agent import forge

    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout="a" * 40 + "\n", stderr="")

    out, err = io.StringIO(), io.StringIO()
    rc = forge.mirror_fetch("dev", "claude/x-1", run=run, url="git@github.example.test:o/r.git",
                            journal_path=os.devnull, out=out, err=err)
    assert rc == 0, err.getvalue()
    (fetch, kw), _ = calls
    assert fetch[:6] == ["git", "--git-dir", forge.MIRROR_COPY, "-c", "core.hooksPath=/dev/null", "fetch"]
    assert fetch[-2:] == ["git@github.example.test:o/r.git", "+refs/heads/claude/x-1:refs/heads/claude/x-1"]
    assert "--no-tags" in fetch and "--" in fetch
    env = kw["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert f"-i {forge.MIRROR_KEY} " in env["GIT_SSH_COMMAND"] and "StrictHostKeyChecking=yes" in env["GIT_SSH_COMMAND"]
    assert "git fetch github claude/x-1" in out.getvalue()
    for bad in ("-upload-pack=x", "--upload-pack=x", "a..b", "x.lock", "a//b", "a/.b", "refs@{0}", "", "a b", "*"):
        assert forge.mirror_fetch("dev", bad, run=run, url="u", journal_path=os.devnull, out=out, err=err) == 2, bad


def test_venv_matches_agent_launch() -> None:
    assert "VENV=/opt/digipaddock-venv/$agent\n" in DEVELOP
    assert "venv=/opt/digipaddock-venv/$name" in (
        COMMON / "bin/agent-launch"
    ).read_text(encoding="utf-8")
    check = _function(DEVELOP, "check")
    assert 'runuser -u "$user" -- find "$VENV" -writable -print -quit' in check
    assert 'runuser -u "$user" -- "$VENV/bin/python" -I -c' in check


HARNESS = r"""set -euo pipefail
LOG=$1 VENV=$2 REPO=$3 STEP=${4:-build_venv}
STAMP=$VENV/.kd-requirements.sha256
install() { echo "install $*" >> "$LOG"; }
chown() { echo "chown $*" >> "$LOG"; }
chmod() { echo "chmod $*" >> "$LOG"; }
python3() {
  echo "python3 $*" >> "$LOG"
  [ "${KD_VENV_RC:-0}" = 0 ] || return 1
  mkdir -p "$VENV/bin"
  printf '#!/bin/sh\necho "pip $*" >> "%s"\nexit ${KD_PIP_RC:-0}\n' "$LOG" > "$VENV/bin/pip"
  printf '#!/bin/sh\nexit ${KD_IMPORT_RC:-0}\n' > "$VENV/bin/python"
  command chmod 0755 "$VENV/bin/pip" "$VENV/bin/python"
}
"""


def _run_build_venv(tmp_path: Path, step: str = "build_venv", **env: str):
    log, venv = tmp_path / "log.txt", tmp_path / "venv-root" / "dev"
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    if not (repo / "requirements-dev.txt").exists():
        (repo / "requirements-dev.txt").write_text(
            "pytest>=8\nvermin\n", encoding="utf-8"
        )
    lock = repo / "common" / "cabinet"  # the web service's hashed lock, installed next to the tools
    lock.mkdir(parents=True, exist_ok=True)
    for name in ("requirements-build.txt", "requirements.txt"):
        if not (lock / name).exists():
            (lock / name).write_text("x==1 --hash=sha256:0\n", encoding="utf-8")
    script = tmp_path / "step.sh"
    script.write_text(
        HARNESS
        + _function(DEVELOP, "build_venv")
        + _function(DEVELOP, "venv_current")
        + 'eval "$STEP"\necho done >> "$LOG"\n',
        encoding="utf-8",
        newline="\n",
    )
    proc = subprocess.run(
        [BASH, str(script), log.as_posix(), venv.as_posix(), repo.as_posix(), step],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        env={**os.environ, **env},
    )
    return (
        proc,
        (log.read_text(encoding="utf-8") if log.exists() else ""),
        venv.as_posix(),
    )


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_build_venv_reruns_in_place_then_locks(tmp_path: Path) -> None:
    for _ in range(2):
        proc, log, venv = _run_build_venv(tmp_path)
        assert proc.returncode == 0, proc.stderr
    lines = log.splitlines()
    assert lines.count(f"python3 -m venv --system-site-packages {venv}") == 2
    pip = [ln for ln in lines if ln.startswith("pip ")]
    repo = (tmp_path / "repo").as_posix()
    # tools unpinned and upgraded; the web lock hash-checked, wheels only, http-ece built from its source
    assert [ln.rsplit(" -r ", 1)[1] for ln in pip] == [
        f"{repo}/requirements-dev.txt",
        f"{repo}/common/cabinet/requirements-build.txt",
        f"{repo}/common/cabinet/requirements.txt",
    ] * 2
    assert "--upgrade" in pip[0] and "--require-hashes" not in pip[0]
    assert all("--require-hashes --only-binary=:all:" in ln for ln in pip[1:3])
    assert "--no-binary=http-ece --no-build-isolation" in pip[2]
    first = lines[: lines.index("done")]
    order = [
        next(i for i, ln in enumerate(first) if ln.startswith(p))
        for p in (
            "install -d",
            "python3 -m venv",
            "pip install",
            "chown -hR",
            "chmod -R go-w",
        )
    ]
    assert order == sorted(order), first


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("env", [{"KD_VENV_RC": "1"}, {"KD_PIP_RC": "1"}])
def test_build_venv_fails_loud_without_touching_rights(
    tmp_path: Path, env: dict
) -> None:
    proc, log, _ = _run_build_venv(tmp_path, **env)
    assert (
        proc.returncode == 1
        and "is not built" in proc.stderr
        and "Traceback" not in proc.stderr
    ), proc.stderr
    assert "done" not in log.splitlines() and "chown" not in log


def test_texts_live_in_work_tmp() -> None:
    assert "TEXTS=$AGENTS/$agent/work/tmp" in DEVELOP
    assert 'runuser -u "$user" -- mkdir -p -m 0755 "$TEXTS"' in DEVELOP


def test_reinstall_needs_no_network_when_the_state_is_already_there() -> None:
    # A Forgejo outage must not take a working agent and kd-release down on a routine reinstall.
    install = _function(FORGE, "install_all")
    assert install.index('[ ! -d "$MIRRORS/${repo##*/}.git" ] || continue') < install.index("git ls-remote")
    assert "venv_current || build_venv" in DEVELOP.split("  install)", 1)[1]


@pytest.mark.skipif(BASH is None, reason="no working bash")
def test_reinstall_keeps_a_current_venv_and_rebuilds_on_new_requirements(
    tmp_path: Path,
) -> None:
    # No PyPI round trip while the stamp matches and the tools import; a changed requirements-dev.txt rebuilds.
    def builds() -> int:
        log = tmp_path / "log.txt"
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        return text.count("python3 -m venv")

    for _ in range(2):
        proc, _, _ = _run_build_venv(tmp_path, "venv_current || build_venv")
        assert proc.returncode == 0, proc.stderr
    assert builds() == 1
    (tmp_path / "repo" / "requirements-dev.txt").write_text(
        "pytest>=9\nvermin\n", encoding="utf-8"
    )
    _run_build_venv(tmp_path, "venv_current || build_venv")
    assert builds() == 2
    _run_build_venv(tmp_path, "venv_current || build_venv", KD_IMPORT_RC="1")
    assert builds() == 3  # broken tools rebuild too
    (tmp_path / "repo" / "common" / "cabinet" / "requirements.txt").write_text(
        "x==2 --hash=sha256:1\n", encoding="utf-8"
    )
    _run_build_venv(tmp_path, "venv_current || build_venv")
    assert builds() == 4  # a changed web lock rebuilds too


@pytest.mark.skipif(BASH is None, reason="no working bash")
@pytest.mark.parametrize("mode,ok", [("640", True), ("650", True), ("660", False), ("644", False)])
def test_the_forge_journal_takes_the_x_of_a_read_grants_acl_mask(tmp_path: Path, mode: str, ok: bool) -> None:
    """as the exec journal — a rotated journal under the agent's read grant shows 650 after setfacl."""
    lines = [ln for ln in _function(FORGE, "check").splitlines() if "forge.jsonl" in ln]
    harness = tmp_path / "journal.sh"
    harness.write_text(
        f"set -euo pipefail\nproblems=()\nJOURNAL={tmp_path.as_posix()}\nforge=x user=y\n"
        f"stat() {{ echo 'x:y {mode}'; }}\n" + re.search(r"^want\(\) \{.*?^\}\n", FORGE, re.M | re.S).group(0)
        + "\n".join(lines)
        + '\n[ ${#problems[@]} -eq 0 ] || printf "%s\n" "${problems[@]}"\n',
        encoding="utf-8", newline="\n",
    )
    (tmp_path / "forge.jsonl").write_text("", encoding="utf-8")
    proc = subprocess.run([BASH, str(harness)], capture_output=True, encoding="utf-8", errors="replace", timeout=60)
    assert proc.returncode == 0, proc.stderr
    assert (proc.stdout.strip() == "") is ok
