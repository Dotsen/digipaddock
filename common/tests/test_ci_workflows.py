"""GitHub workflows of the public mirror: advisory CI and a signed release archive."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO / ".github" / "workflows"


def _text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def test_ci_installs_packages_and_runs_the_checks_in_one_command():
    """the checks are scripts/check.py --profile ci, the one command that repeats this job locally."""
    ci = _text("ci.yml")
    for piece in ("--require-hashes", "npm ci --prefix common/cabinet/web", "runs-on: ubuntu-24.04",
                  'python-version: "3.12"', 'node-version: "22"'):
        assert piece in ci, piece
    lines = [line.strip() for line in ci.splitlines() if line.strip() and not line.strip().startswith("#")]
    checks = [line for line in lines if "scripts/check.py" in line]
    # the scratch under /tmp: under the checkout, pytest's socket paths outgrow AF_UNIX's 108 bytes
    assert checks == ['python scripts/check.py --profile ci --base "$base" --workers 4 --scratch-root /tmp']
    # no check of its own besides that command: every other line installs packages or picks the base
    for line in lines:
        assert not re.search(r"privacy_scan|pytest|overlay|npm (?:run|test)|check_changed|KD_HOST_JSON", line), line


def test_the_ci_profile_plans_what_the_job_used_to_run():
    check = subprocess.run([sys.executable, "scripts/check.py", "--profile", "ci", "--base", "origin/main",
                            "--workers", "4", "--dry-run"], cwd=REPO, capture_output=True, text=True, timeout=60)
    assert check.returncode == 0, check.stderr
    commands = json.loads(check.stdout)["commands"]
    argv = [" ".join(c["argv"][1:]) for c in commands]
    assert argv[0] == "-I common/kd/privacy_scan.py --tracked . --config privacy-scan.json"
    assert argv[1] == "scripts/check_changed.py --base origin/main"
    assert argv[2].startswith("-m pytest -n 4") and "-m not host" in argv[2]
    assert commands[2]["env"] == {"KD_HOST_JSON": "host/host.example.json"}
    assert argv[3:6] == ["-I common/kd/overlay.py init {scratch}/overlay",
                         "-I scripts/overlay_stand_ins.py {scratch}/overlay",
                         "-I common/kd/overlay.py check {scratch}/overlay"]
    assert argv[6:] == ["--prefix common/cabinet/web run check", "--prefix common/cabinet/web test",
                        "--prefix common/cabinet/web run build"]


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_workflows_pin_actions_by_sha(name):
    uses = re.findall(r"^\s*-? *uses: *(\S+)", _text(name), re.M)
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action


def test_ci_has_no_secrets():
    ci = _text("ci.yml")
    assert "secrets." not in ci and "github.token" not in ci
    assert re.search(r"^permissions:\n  contents: read\n", ci, re.M)
    assert "persist-credentials: false" in ci


@pytest.mark.parametrize("name", ["ci.yml", "release.yml"])
def test_untrusted_values_reach_scripts_through_env(name):
    """A ${{ }} expression only as a whole mapping value, never spliced into a script (script injection)."""
    for line in _text(name).splitlines():
        if "${{" in line:
            assert re.fullmatch(r"\s*[\w-]+: \$\{\{ [\w.]+ \}\}", line), line


def test_release_signs_a_draft():
    release = _text("release.yml")
    assert re.search(r"^on:\n  push:\n    tags: \['v\*'\]\n", release, re.M)
    assert re.search(r"^permissions:\n  contents: read\n", release, re.M)
    assert re.search(r"^    permissions:\n      contents: write\n      id-token: write\n", release, re.M)
    for piece in ('git archive --format=tar --prefix="$name/"', "common/cabinet/web/dist", 'sha256sum "$name.tar.gz" > SHA256SUMS',
                  "cosign sign-blob --yes", "--draft", "--verify-tag", "cosign-release: v2.5.0"):
        assert piece in release, piece
    assert "[[ $TAG =~ ^v[0-9A-Za-z._-]+$ ]]" in release  # the tag's shape before it names a file


def test_release_builds_the_web_where_nothing_can_be_signed():
    """npm's dependencies run in a job without id-token; the signing job runs no npm."""
    jobs = _text("release.yml").split("\njobs:\n", 1)[1]
    web, sign = jobs.split("\n  release:\n", 1)
    assert web.startswith("  web:\n") and "npm ci" in web and "npm run build" in web
    assert "id-token" not in web and "permissions" not in web and "upload-artifact" in web
    assert "needs: web" in sign and "download-artifact" in sign and "npm" not in sign and "setup-node" not in sign


def test_ci_static_gate_takes_every_commit_of_a_push():
    """A push of several commits is checked from the commit before it, not only its last one."""
    assert "          BEFORE: ${{ github.event.before }}\n" in _text("ci.yml")
    gate = _step("Checks")
    assert 'git cat-file -e "$BEFORE^{commit}"' in gate and 'base="$BEFORE"' in gate
    assert "base=HEAD~1" in gate  # a new branch (before is zeros) or a force-push whose before is gone


@pytest.mark.skipif(os.name != "posix" or not shutil.which("git"), reason="the runner's shell")
@pytest.mark.parametrize("event,before,want", [("push", "known", "first"), ("push", "0" * 40, "HEAD~1"),
                                                ("pull_request", "", "origin/main")])
def test_the_static_gate_picks_its_base(tmp_path, event, before, want):
    repo = tmp_path / "r"
    git = ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.test"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for message in ("first", "second", "third"):
        subprocess.run(git + ["commit", "-q", "--allow-empty", "-m", message], check=True)
    first = subprocess.run(git + ["rev-parse", "HEAD~2"], capture_output=True, text=True, check=True).stdout.strip()
    script = _step("Checks").replace("python scripts/check.py --profile ci", "echo")
    env = {**os.environ, "EVENT": event, "BASE_REF": "main", "BEFORE": first if before == "known" else before}
    out = subprocess.run(["bash", "-e", "-c", script], cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["--base", first if want == "first" else want, "--workers", "4",
                                  "--scratch-root", "/tmp"]


def _step(name):
    """The run: block of a ci.yml step, unindented."""
    block = _text("ci.yml").split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    return "\n".join(line[10:] for line in block.split("        run: |\n", 1)[1].splitlines() if line.strip())


def test_the_overlay_steps_pass_on_the_examples(tmp_path):
    """The ci profile's overlay commands as it runs them: the first run on GitHub failed — check names the files a
    release needs, and init made only host/."""
    plan = json.loads(subprocess.run([sys.executable, "scripts/check.py", "--profile", "ci", "--dry-run"], cwd=REPO,
                                     capture_output=True, text=True, timeout=60, check=True).stdout)["commands"]
    overlay = [c["argv"] for c in plan if any("overlay" in arg for arg in c["argv"])]
    assert len(overlay) == 3
    for argv in overlay:
        proc = subprocess.run([arg.replace("{scratch}", str(tmp_path)) for arg in argv], cwd=REPO,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "overlay: ok" in proc.stdout
    literals = json.loads((tmp_path / "overlay/host/private_scan.json").read_text())["literals"]
    example = (REPO / "host/private_scan.example.json")
    if example.is_file():  # the stand-in must not repeat the example's literals
        assert not set(literals) & set(json.loads(example.read_text()).get("literals", []))


def test_testing_doc_says_ci_only_advises():
    doc = (REPO / "docs" / "testing.md").read_text(encoding="utf-8")
    assert "## GitHub CI" in doc and "devcheck/unit" in doc and "advisory" in doc
    assert '-m "not host"' in doc
