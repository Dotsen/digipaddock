import io
import json
import os
import re
import subprocess
import sys
import tarfile
from importlib.machinery import SourceFileLoader
import importlib.util
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin"
KNOWN = Path(__file__).resolve().parents[1] / "install/forgejo_known_hosts"
CP = subprocess.CompletedProcess
HOST_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "host" / "host.json"


@pytest.fixture(scope="module")
def kd_release():
    loader = SourceFileLoader("kd_release", str(BIN / "kd-release"))
    spec = importlib.util.spec_from_loader("kd_release", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


SANDBOXED = []  # the argv of every sandboxed run of the current test, as the sandbox would see it


@pytest.fixture(autouse=True)
def _python(kd_release, monkeypatch):
    """The preflight runs the released code's scanner and composition with this interpreter; the sandbox
    (systemd-run, root only) is stood in for by a plain run with its paths mapped back to the work directory."""
    monkeypatch.setattr(kd_release, "PYTHON", sys.executable)
    SANDBOXED.clear()

    def sandboxed(argv, work, timeout):
        kd_release._sandbox_argv(argv, work, timeout)  # the real unit's argv is built for every run
        SANDBOXED.append(list(argv))
        real = [a.replace(kd_release.SANDBOX_IN, work) for a in argv]
        p = subprocess.run([sys.executable, "-I", *real], capture_output=True, timeout=timeout)
        out = p.stdout
        if argv[0] == kd_release.SANDBOX_SCAN:  # its paths, as printed inside the sandbox
            out = out.replace(str(work).encode(), kd_release.SANDBOX_IN.encode()).replace(os.sep.encode(), b"/")
        return p.returncode, out, p.stderr.decode("utf-8", "replace")

    monkeypatch.setattr(kd_release, "_sandboxed", sandboxed)


SHA = "e" * 40
PREV = "a" * 7
DEP = "d" * 40
DEP_PREV = "b" * 7
REQUEST = {"project": "digipaddock", "repo": "example/DigiPaddock", "sha": SHA, "pr": 12, "ts": "1.000"}
OVERLAY_ENTRY = {"repo": "example/digipaddock-deploy", "dir": "work/digipaddock-deploy", "release": "digipaddock",
                 "role": "overlay"}


def tar_of(files, modes=None):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), (modes or {}).get(name, 0o644)
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()


def tar_names(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as tar:
        return {m.name: (tar.extractfile(m).read() if m.isfile() else None) for m in tar.getmembers()}


KD = BIN.parent / "kd"
# the released code: its scanner and composition run in the preflight
CODE_FILES = {"privacy-scan.json": b'{"forbidden_domains": ["example.invalid"], "exclude": ["privacy-scan.json"]}',
              "README": b"digipaddock\n", "common/install/forgejo_known_hosts": b"# the overlay's\n",
              **{f"common/kd/{name}": (KD / name).read_bytes()
                 for name in ("__init__.py", "naming.py", "overlay.py", "privacy_scan.py", "install_spec.py",
                              "memory_bank.py", "egress.py", "registry.py")},
              "common/naming.default.json": (BIN.parent / "naming.default.json").read_bytes()}
OVERLAY_FILES = {
    "host/overlay.json": b'{"schema_version": 1}',
    "host/host.json": HOST_FIXTURE.read_bytes(),
    "agents/sysadmin/inventory.json": b'{"home": "example-stage", "servers": {}}',
    "agents/sysadmin/monitor/config.json": b"{}",
    "agents/sysadmin/operations.json": b'{"schema_version": 1, "verbs": [], "servers": {}}',
    "agents/sysadmin/playbooks/security.md": b"# security\n",
    "agents/sysadmin/servers.md": b"# servers\n",
    "common/install/forgejo_known_hosts": b"forge.example.test ssh-ed25519 AAAA\n",
    "host/private_scan.json": b'{"literals": ["secret-word"]}',
}


def make_env(tmp_path, sha_on_main=SHA, request=None, tree="100644 blob 1111\tREADME\n", prev_known=True, ancestor=True,
             overlay=..., overlay_main=DEP, code=None, dep_ancestor=True, overlay_modes=None):
    """overlay: the overlay repository's files (role overlay in repos.json; OVERLAY_FILES unless given);
    None — a registry without the overlay entry, which kd-release refuses."""
    overlay = OVERLAY_FILES if overlay is ... else overlay
    rel, res, mirror = tmp_path / "release", tmp_path / "result", tmp_path / "mirror"
    for d in (rel, res, mirror):
        d.mkdir(parents=True)
    repos = tmp_path / "repos.json"
    registry = {"digipaddock": {"repo": "example/DigiPaddock", "dir": "work/DigiPaddock", "release": "digipaddock"}}
    if overlay is not None:
        registry["digipaddock-deploy"] = OVERLAY_ENTRY
    repos.write_text(json.dumps(registry), encoding="utf-8")
    req = REQUEST if request is None else request
    (rel / "request.json").write_text(req if isinstance(req, str) else json.dumps(req), encoding="utf-8")
    code_tar = tar_of(CODE_FILES if code is None else code)
    overlay_tar = tar_of(overlay, overlay_modes) if overlay is not None else b""
    calls = []
    def run(cmd, **kw):
        calls.append(cmd)
        joined = " ".join(cmd)
        dep = "digipaddock-deploy" in joined
        if "rev-parse refs/heads/main" in joined:
            return CP(cmd, 0, (overlay_main if dep else sha_on_main) + "\n", "")
        if "rev-parse --verify" in joined:
            return CP(cmd, 0 if prev_known or dep else 128, "", "")
        if "merge-base --is-ancestor" in joined:
            return CP(cmd, 0 if (dep_ancestor if dep else ancestor) else 1, "", "")
        if "ls-tree" in joined:
            return CP(cmd, 0, "100644 blob 1\thost/host.json\n" if dep else tree, "")
        if "archive" in joined:
            return CP(cmd, 0, overlay_tar if dep else code_tar, b"")
        return CP(cmd, 0, "", "")
    switched = []
    def switch(rev, tar):
        switched.append((rev, tar))
        return 0, f"REVISION {rev} on stage → /opt/x"
    return {"rel": rel, "res": res, "mirror": mirror, "repos": repos, "run": run, "switch": switch, "calls": calls,
            "switched": switched, "code_tar": code_tar}


def go(kd_release, s, argv=(), **over):
    kw = dict(release_dir=str(s["rel"]), result_dir=str(s["res"]), mirror_dir=str(s["mirror"]), repos_path=str(s["repos"]),
              run=s["run"], switch=s["switch"], current=lambda link: PREV, current_deploy=lambda link: DEP_PREV,
              lock_path=None, clock=lambda: 0.0, postflight=lambda link: (0, ""), cabinet=lambda link: (0, ""),
              reinstall=lambda link, rev, card, release_id: {"state": "none"},
              health=lambda link, rev, card, postflight="": (0, ""), closed_scanner=lambda work: [],
              e2e=lambda link: (0, ""), render=lambda link: (0, ""),
              mirror=lambda: (0, ""), host=lambda link, release_id: {"state": "none"})
    kw.update(over)
    return kd_release.main(list(argv), **kw)


def result(s):
    return json.loads((s["res"] / "result.json").read_text(encoding="utf-8"))


def identified_request(kd_release, **changes):
    req = {**REQUEST, **changes}
    req["request_id"] = kd_release.release_requests.forge_id(req["project"], req["repo"], req["pr"], req["sha"])
    return req


def test_each_request_keeps_its_terminal_result(tmp_path, kd_release):
    first = identified_request(kd_release)
    second = identified_request(kd_release, pr=13)
    s = make_env(tmp_path, request=first)
    pending = s["rel"] / "request.json"
    identities = kd_release.release_requests

    def check(link, rev, card, why):
        active = identities.read(s["res"], identities.ACTIVE)
        assert active["state"] == "checking"
        assert not identities.terminal(s["res"], active["request_id"])
        if active["request_id"] == first["request_id"]:
            pending.write_text(json.dumps(second), encoding="utf-8")
        return 0, ""

    assert go(kd_release, s, health=check) == 0
    assert result(s)["request_id"] == second["request_id"]
    assert not (s["res"] / identities.ACTIVE).exists()
    first_result = identities.terminal(s["res"], first["request_id"])
    assert first_result["state"] == "healthy"
    assert identities.terminal(s["res"], second["request_id"])["state"] == "healthy"
    pending.write_text(json.dumps(first), encoding="utf-8")
    assert go(kd_release, s) == 0
    assert len(s["switched"]) == 2 and not pending.exists()
    assert identities.terminal(s["res"], first["request_id"]) == first_result
    assert result(s)["request_id"] == second["request_id"]


def test_health_notice_reads_the_terminal_release(tmp_path, kd_release, monkeypatch):
    req = identified_request(kd_release)
    s = make_env(tmp_path, request=req)
    notices = []

    def health(link, rev, card, why, *, announce):
        assert not announce
        assert not kd_release.release_requests.terminal(s["res"], req["request_id"])
        return 0, ""

    def notice(record, card):
        assert kd_release.release_requests.terminal(s["res"], req["request_id"]) == record
        notices.append(record["state"])

    monkeypatch.setattr(kd_release, "_health_default", health)
    monkeypatch.setattr(kd_release, "_notice_release", notice)
    assert go(kd_release, s, health=health) == 0
    assert notices == ["healthy"]


@pytest.mark.parametrize("phase, installed", [("accepted", False), ("switching", False), ("switching", True),
                                              ("checking", True)])
def test_restart_settles_active_request_without_switching_again(tmp_path, kd_release, phase, installed):
    req = identified_request(kd_release)
    s = make_env(tmp_path, request=req)
    release_id = f"{SHA[:7]}+{DEP[:7]}"
    kd_release._progress(s["res"], req["request_id"], req["ts"], phase, release="digipaddock",
                         revision=SHA[:7], deploy_revision=DEP[:7], release_id=release_id)
    assert go(kd_release, s, current=lambda link: SHA[:7] if installed else PREV,
              current_deploy=lambda link: DEP[:7]) == 0
    outcome = kd_release.release_requests.terminal(s["res"], req["request_id"])
    assert outcome["state"] == ("degraded" if installed else "refused")
    assert outcome["release_id"] == (release_id if installed else "")
    assert "interrupted" in outcome["error"]
    assert not s["switched"] and not (s["rel"] / "request.json").exists()


def test_release_happy_path(tmp_path, kd_release):
    s = make_env(tmp_path)
    assert go(kd_release, s) == 0
    res = result(s)
    assert res == {"ts": "1.000", "ok": True, "degraded": False, "revision": SHA[:7], "previous": PREV, "error": "",
                   "deploy_revision": DEP[:7], "release_id": f"{SHA[:7]}+{DEP[:7]}", "reinstall": {"state": "none"},
                   "deploy_previous": DEP_PREV, "health": "healthy"}
    (rev, tar), = s["switched"]  # REVISION — 7 characters; the pair composed by the code's kd.overlay
    assert rev == SHA[:7] and tar_names(tar)["DEPLOY_REVISION"] == f"{DEP[:7]}\n".encode()
    joined = [" ".join(c) for c in s["calls"]]
    assert any("clone --mirror" in c or "fetch" in c for c in joined)
    assert any(f"merge-base --is-ancestor {PREV} {SHA}" in c for c in joined)
    for part in ("forgejo_deploy", "IdentitiesOnly=yes", "StrictHostKeyChecking=yes",
                 "UserKnownHostsFile=/opt/digipaddock/common/install/forgejo_known_hosts"):
        assert part in kd_release.GIT_SSH
    assert not (s["rel"] / "request.json").exists()  # the request is taken off
    assert not [p for p in s["res"].iterdir() if p.name != "result.json"]  # no temporary files are left


def test_main_moved_on_is_refused_quietly(tmp_path, kd_release):
    s = make_env(tmp_path, sha_on_main="f" * 40)
    assert go(kd_release, s) == 0  # a handled refusal: the service's code is 0, OnFailure/kd-alert does not fire
    res = result(s)
    assert res["ok"] is False and "main moved on" in res["error"] and s["switched"] == []
    assert not hasattr(kd_release, "pult")  # only the executor writes to the owner's Telegram bot — one message per refusal


@pytest.mark.parametrize("over", [dict(ancestor=False), dict(prev_known=False)])
def test_stage_revision_not_from_main_is_refused(tmp_path, kd_release, over):
    s = make_env(tmp_path, **over)
    assert go(kd_release, s) == 0
    res = result(s)
    assert res["ok"] is False and "not from main" in res["error"] and PREV in res["error"] and s["switched"] == []


def test_unreadable_stage_revision_is_refused(tmp_path, kd_release):
    s = make_env(tmp_path)
    assert go(kd_release, s, current=lambda link: "") == 0
    assert "not from main" in result(s)["error"] and s["switched"] == []


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_symlinks_and_submodules_are_refused(tmp_path, kd_release, mode):
    s = make_env(tmp_path, tree=f"100644 blob 1\tREADME\n{mode} blob 2\tcommon/bin/x\n")
    assert go(kd_release, s) == 0
    res = result(s)
    assert res["ok"] is False and "a symlink or a submodule" in res["error"] and "common/bin/x" in res["error"]
    assert s["switched"] == []


@pytest.mark.parametrize("bad", [
    {**REQUEST, "repo": "example/Other"},   # a repo not from the registry
    {**REQUEST, "project": "x"},
    {**REQUEST, "sha": "zzz"},
    {**REQUEST, "sha": "e" * 7},               # only a full sha
    {**REQUEST, "pr": "a"},
    {**REQUEST, "pad": "x" * 5000},            # more than 4 KB
    "garbage",
])
def test_bad_requests_write_error_and_switch_nothing(tmp_path, kd_release, bad):
    s = make_env(tmp_path, request=bad)
    assert go(kd_release, s) == 0
    assert result(s)["ok"] is False and s["switched"] == []


@pytest.mark.parametrize("bad", [
    {**REQUEST, "pr": True},                   # a bool is not a PR number
    {**REQUEST, "pr": 0},
    {**REQUEST, "sha": int("1" * 40)},         # 40 digits, but not a string
    {**REQUEST, "repo": "DigiPaddock"},      # without an owner
    [REQUEST],
])
def test_odd_field_types_are_refused(tmp_path, kd_release, bad):
    s = make_env(tmp_path, request=bad)
    assert go(kd_release, s) == 0
    assert result(s)["ok"] is False and s["switched"] == []


@pytest.mark.parametrize("entry", [
    {"repo": "DigiPaddock", "dir": "work/DigiPaddock", "release": "digipaddock"},           # a repo without an owner
    {"repo": "example/DigiPaddock", "dir": "work/DigiPaddock", "release": ["digipaddock"]},  # release is not a string
])
def test_odd_registry_is_refused_not_crashed(tmp_path, kd_release, entry):
    s = make_env(tmp_path, request={**REQUEST, "repo": entry["repo"]})
    s["repos"].write_text(json.dumps({"digipaddock": entry}), encoding="utf-8")
    assert go(kd_release, s) == 0
    assert result(s)["ok"] is False and s["switched"] == []


def test_switched_but_result_not_written_is_a_failure_not_a_refusal(tmp_path, kd_release, monkeypatch, capsys):
    # the code is already switched: "the release is rejected" would be untrue — the service fails (kd-alert), the
    # executor waits until its timeout
    s = make_env(tmp_path)
    def no_result(*a, **kw):
        raise PermissionError("result")
    monkeypatch.setattr(kd_release, "_result", no_result)
    with pytest.raises(PermissionError):
        go(kd_release, s)
    assert len(s["switched"]) == 1
    err = capsys.readouterr()
    assert "kd-release: released, but the result is not written" in err.err and "отклон" not in err.err


@pytest.mark.skipif(os.name == "nt", reason="симлинки и O_NOFOLLOW — POSIX; прогон на example-stage")
@pytest.mark.parametrize("kind", ["symlink", "dir", "hardlink", "fifo"])
def test_request_symlink_or_dir_is_removed_and_refused(tmp_path, kd_release, kind):
    """The request's directory is the forge user's: a link, a directory, a FIFO or a hard link of another file in
    place of request.json is never read as a request (a hard link of a root-owned file would pass for root's own)."""
    s = make_env(tmp_path)
    req = s["rel"] / "request.json"
    target = tmp_path / "elsewhere.json"
    target.write_text(json.dumps(REQUEST), encoding="utf-8")
    req.unlink()
    if kind == "symlink":
        req.symlink_to(target)  # root does not follow a symlink of the forge agent
    elif kind == "hardlink":
        os.link(target, req)
    elif kind == "fifo":
        os.mkfifo(req)  # never a hang
    else:
        req.mkdir()
    assert go(kd_release, s) == 0
    assert result(s)["ok"] is False and s["switched"] == [] and not req.exists() and not req.is_symlink()
    assert target.exists() and json.loads(target.read_text(encoding="utf-8")) == REQUEST


def test_default_switch_gets_release_target(tmp_path, kd_release, monkeypatch):
    s = make_env(tmp_path)
    seen, real = [], subprocess.run
    def fake_run(cmd, **kw):
        if cmd[0] != "bash":  # the preflight's scanner and composition run for real
            return real(cmd, **kw)
        seen.append((cmd, kw))
        return CP(cmd, 0, b"REVISION eeeeeee", b"")
    monkeypatch.setattr(kd_release.subprocess, "run", fake_run)
    assert go(kd_release, s, switch=kd_release._switch_default) == 0
    (cmd, kw), = seen
    assert cmd == ["bash", kd_release.SWITCH, SHA[:7]] and kw["timeout"] == kd_release.T_SWITCH
    assert tar_names(kw["input"])["README"] == b"digipaddock\n"
    assert kw["env"]["KD_RELEASE_BASE"] == "/opt/digipaddock-releases" and kw["env"]["KD_RELEASE_LINK"] == "/opt/digipaddock"
    # per repository fetch, 4 local git calls and an archive; two scans and the composition; the switch,
    # the postflight, the factory's request twice, the units, the cabinet, the host's restart, the e2e
    # runtime and the health check
    unit = (BIN.parent / "systemd" / "digipaddock-release.service").read_text(encoding="utf-8")
    timeout = int(re.search(r"^TimeoutStartSec=(\d+)$", unit, re.M).group(1))
    # and the mirror's start; a second request in the same run only within LOOP_BUDGET of the first
    total = (2 * (kd_release.T_FETCH + 4 * kd_release.T_LOCAL + kd_release.T_ARCHIVE) + 3 * kd_release.T_SCAN
             + kd_release.T_SWITCH + kd_release.T_LOCAL + 3 * kd_release.T_REINSTALL + kd_release.T_RENDER
             + kd_release.T_CABINET + kd_release.T_E2E + kd_release.T_HEALTH + kd_release.T_LOCAL
             + kd_release.LOOP_BUDGET)
    assert total < timeout
    assert re.search(r"^# (\d+) s of the T_\* of kd-release", unit, re.M).group(1) == str(total)  # review M1


def test_refused_generated_agents_after_switch_fail_the_unit(tmp_path, kd_release, capsys):
    # the code is released, but a generated agent will not start until reinstalled: the outcome is degraded (not ok),
    # silence is not allowed → kd-alert
    s = make_env(tmp_path)
    seen = []
    def refused(link):
        seen.append(link)
        return 1, "generated runtime refused: boss: compiled policy: unsupported schema_version\n"
    with pytest.raises(RuntimeError):
        go(kd_release, s, postflight=refused)
    r = result(s)
    assert seen == ["/opt/digipaddock"] and len(s["switched"]) == 1
    assert r["ok"] is False and r["degraded"] is True and r["revision"] == SHA[:7] and r["previous"] == PREV
    assert "boss: compiled policy" in r["error"]
    err = capsys.readouterr().err
    assert "kd-release: released, but the generated agents will not start" in err and "boss: compiled policy" in err
    assert "new_agent.sh --install" in err


def test_success_is_published_only_after_the_postflight(tmp_path, kd_release):
    # forge reports and audits the first result it reads: no ok=true may exist while the postflight runs
    s = make_env(tmp_path)
    seen = []
    def postflight(link):
        seen.append((s["res"] / "result.json").exists())
        return 0, ""
    assert go(kd_release, s, postflight=postflight) == 0
    assert seen == [False] and result(s)["ok"] is True and result(s)["degraded"] is False


def test_refusal_before_switch_runs_no_postflight(tmp_path, kd_release):
    s = make_env(tmp_path, sha_on_main="f" * 40)
    seen = []
    assert go(kd_release, s, postflight=lambda link: seen.append(link) or (0, "")) == 0
    assert seen == [] and s["switched"] == []


def test_default_postflight_runs_the_new_code_isolated(kd_release, monkeypatch):
    seen = []
    def fake_run(cmd, **kw):
        seen.append((cmd, kw))
        return CP(cmd, 1, "", "generated runtime refused: boss: x\n")
    monkeypatch.setattr(kd_release.subprocess, "run", fake_run)
    assert kd_release._postflight_default("/opt/digipaddock") == (1, "generated runtime refused: boss: x\n")
    (cmd, kw), = seen
    assert cmd[:3] == ["/usr/bin/python3", "-I", "-c"] and cmd[4:] == ["/opt/digipaddock"]
    assert "report_generated_runtime" in cmd[3] and kw["timeout"] == kd_release.T_LOCAL


def test_the_cabinet_is_updated_before_the_result_is_published(tmp_path, kd_release):
    # a release brings new cabinet code and frontend: cabinet_setup.sh install restarts it on them
    s = make_env(tmp_path)
    seen = []
    def cabinet(link):
        seen.append((link, (s["res"] / "result.json").exists()))
        return 0, ""
    assert go(kd_release, s, cabinet=cabinet) == 0
    assert seen == [("/opt/digipaddock", False)]


def test_a_cabinet_not_updated_fails_the_unit_but_the_release_stands(tmp_path, kd_release, capsys):
    req = identified_request(kd_release)
    s = make_env(tmp_path, request=req)
    with pytest.raises(RuntimeError):
        go(kd_release, s, cabinet=lambda link: (1, "npm ci: network\nкабинет не собран\n"))
    final = kd_release.release_requests.terminal(s["res"], req["request_id"])
    assert final["state"] == "degraded" and final["ok"] is False and len(s["switched"]) == 1
    assert final["release_id"] == f"{SHA[:7]}+{DEP[:7]}" and "cabinet" in final["failed_steps"]
    err = capsys.readouterr().err
    assert "kd-release: cabinet not updated" in err and "кабинет не собран" in err


def test_refusal_before_switch_runs_no_cabinet_update(tmp_path, kd_release):
    s = make_env(tmp_path, sha_on_main="f" * 40)
    seen = []
    assert go(kd_release, s, cabinet=lambda link: seen.append(link) or (0, ""),
              render=lambda link: seen.append(("render", link)) or (0, "")) == 0
    assert seen == []


def test_the_units_are_rendered_before_the_cabinet_and_result(tmp_path, kd_release):
    """A change in common/systemd/ or the rules floor takes effect with the release, as after a rollback (P0 window:
    digipaddock-mirror.service stayed old until root ran render_host.sh by hand)."""
    s = make_env(tmp_path)
    order = []
    assert go(kd_release, s, render=lambda link: order.append(("render", link, (s["res"] / "result.json").exists())) or (0, ""),
              cabinet=lambda link: order.append("cabinet") or (0, ""),
              e2e=lambda link: order.append("e2e") or (0, "")) == 0
    assert order == [("render", "/opt/digipaddock", False), "cabinet", "e2e"]


def test_units_not_rendered_fail_the_unit_but_the_release_stands(tmp_path, kd_release, capsys):
    s = make_env(tmp_path)
    cabinet = []
    with pytest.raises(RuntimeError, match="render_host.sh"):
        go(kd_release, s, render=lambda link: (1, "systemctl daemon-reload: failed\n"),
           cabinet=lambda link: cabinet.append(link) or (0, ""))
    assert result(s)["ok"] is False and result(s)["degraded"] is True and len(s["switched"]) == 1 and cabinet == ["/opt/digipaddock"]
    err = capsys.readouterr().err
    assert "kd-release: units not rendered (render_host.sh): systemctl daemon-reload: failed" in err


def test_roles_not_checked_fail_the_unit_but_the_release_stands(tmp_path, kd_release, capsys):
    """Review I2: the factory's check did not run (a timeout) — no role is named for a notice, so the unit fails:
    kd-alert tells the owner the roles may still run the old instructions."""
    s = make_env(tmp_path)
    health = []
    with pytest.raises(RuntimeError, match="roles not checked"):
        go(kd_release, s, reinstall=lambda link, rev, card, release_id:
           {"state": "unchecked", "agents": [], "why": "roles not checked: TimeoutExpired"},
           health=lambda link, rev, card, postflight="": health.append(rev) or (0, ""))
    assert result(s)["ok"] is False and result(s)["degraded"] is True and len(s["switched"]) == 1 and health == [SHA[:7]]
    assert result(s)["reinstall"]["state"] == "unchecked"
    err = capsys.readouterr().err
    assert "kd-release: roles not checked after the release: roles not checked: TimeoutExpired" in err


def test_stdout_is_line_buffered_for_the_journal(tmp_path, kd_release, monkeypatch):
    """Re-review I1: under systemd stdout is a pipe — buffered, its «kd-release: REVISION …» would reach the journal
    after the stderr line kd-alert takes the reason from (the last «kd-release: » line)."""
    seen = []

    class Out(io.StringIO):
        def reconfigure(self, **kw):
            seen.append(kw)

    monkeypatch.setattr(kd_release.sys, "stdout", Out())
    go(kd_release, make_env(tmp_path))
    assert seen == [{"line_buffering": True}]


def test_default_render_runs_the_renderer_of_the_new_code(kd_release, monkeypatch):
    seen = []
    monkeypatch.setattr(kd_release.subprocess, "run",
                        lambda cmd, **kw: seen.append((cmd, kw["timeout"])) or CP(cmd, 0, "", ""))
    assert kd_release._render_default("/opt/digipaddock") == (0, "")
    assert seen == [(["bash", "/opt/digipaddock/common/install/render_host.sh"], kd_release.T_RENDER)]


def test_default_cabinet_update_runs_the_installer_of_the_new_code(kd_release, monkeypatch):
    seen = []
    def fake_run(cmd, **kw):
        seen.append((cmd, kw))
        return CP(cmd, 0, "", "")
    monkeypatch.setattr(kd_release.subprocess, "run", fake_run)
    assert kd_release._cabinet_default("/opt/digipaddock") == (0, "")
    (cmd, kw), = seen
    assert cmd == ["bash", "/opt/digipaddock/common/install/cabinet_setup.sh", "install"]
    assert kw["timeout"] == kd_release.T_CABINET


# --- the roles the release leaves outdated go to the factory ------------------------------
PENDING = {"state": "pending", "operation": "f" * 32, "agents": ["boss", "dev"]}


def test_release_requests_outdated_roles(tmp_path, kd_release):
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    seen = []

    def reinstall(link, rev, card, release_id):
        seen.append((link, rev, card, (s["res"] / "result.json").exists()))
        return PENDING

    assert go(kd_release, s, reinstall=reinstall) == 0
    assert seen == [("/opt/digipaddock", SHA[:7], "KD-12", False)]  # before the result is published
    assert result(s)["reinstall"] == PENDING and result(s)["ok"] is True


def test_a_request_of_an_earlier_card_prefix_is_the_cards(tmp_path, kd_release):
    # a request kd-merge wrote before the move names the card by its alias (the fixture's OLD)
    s = make_env(tmp_path, request={**REQUEST, "card": "OLD-12"})
    seen = []
    assert go(kd_release, s, reinstall=lambda link, rev, card, *a, **k: seen.append(card) or PENDING) == 0
    assert seen == ["KD-12"] and result(s)["ok"] is True


def test_release_postflight_failure_of_the_requested_roles_is_covered(tmp_path, kd_release):
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    refused = (1, "generated runtime refused: boss: compiled policy: unsupported schema_version\n"
                  "generated runtime refused: dev: compiled policy: unsupported schema_version\n")
    assert go(kd_release, s, postflight=lambda link: refused, reinstall=lambda *a: PENDING) == 0
    r = result(s)
    assert r["ok"] is True and r["degraded"] is False and r["reinstall"] == PENDING


@pytest.mark.parametrize("postflight, reinstall", [
    ((1, "generated runtime refused: sysadmin: compiled policy: x\n"), PENDING),  # not a role it reinstalls
    ((1, "generated runtime refused: registry: unreadable\n"), PENDING),  # not a role at all
    ((1, "generated runtime refused: boss: x\n"), {"state": "none"}),
    ((1, "generated runtime refused: boss: x\n"), {"state": "uncovered", "agents": ["boss"], "why": "no grant"}),
])
def test_release_never_reinstalls_on_unrelated_postflight_failure(tmp_path, kd_release, postflight, reinstall):
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    with pytest.raises(RuntimeError):
        go(kd_release, s, postflight=lambda link: postflight, reinstall=lambda *a: reinstall)
    r = result(s)
    assert r["ok"] is False and r["degraded"] is True and r["reinstall"] == reinstall


def test_release_card_must_be_a_card(tmp_path, kd_release):
    s = make_env(tmp_path, request={**REQUEST, "card": "../x"})
    assert go(kd_release, s) == 0  # a handled refusal: the result says it
    assert s["switched"] == [] and result(s)["ok"] is False and "card" in result(s)["error"]


def test_default_reinstall_runs_the_new_code_isolated(kd_release, monkeypatch):
    seen = []

    def fake_run(cmd, **kw):
        seen.append((cmd, kw))
        return CP(cmd, 0, json.dumps(PENDING) + "\n", "")

    monkeypatch.setattr(kd_release.subprocess, "run", fake_run)
    assert kd_release._reinstall_default("/opt/digipaddock", "abcdef1", "KD-12", "abcdef1+dddddd1") == PENDING
    (cmd, kw), = seen
    assert cmd[:3] == ["/usr/bin/python3", "-I", "-c"]
    assert cmd[4:] == ["/opt/digipaddock", "abcdef1", "KD-12", "--release-id", "abcdef1+dddddd1"]
    assert "release_reinstall" in cmd[3] and kw["timeout"] == kd_release.T_REINSTALL
    monkeypatch.setattr(kd_release.subprocess, "run", lambda cmd, **kw: CP(cmd, 1, "", "boom"))
    assert kd_release._reinstall_default("/opt/digipaddock", "abcdef1", "", "abcdef1") == {
        "state": "unchecked", "agents": [], "why": "roles not checked: boom"}  # review I2: not one role named


def test_no_request_is_noop(tmp_path, kd_release):
    s = make_env(tmp_path)
    (s["rel"] / "request.json").unlink()
    assert go(kd_release, s) == 0
    assert s["switched"] == [] and not (s["res"] / "result.json").exists()


def test_switch_failure_and_git_timeout_are_handled(tmp_path, kd_release):
    s = make_env(tmp_path)
    assert go(kd_release, s, switch=lambda rev, tar: (1, "tar: сбой")) == 0
    assert result(s)["ok"] is False and "сбой" in result(s)["error"]
    s2 = make_env(tmp_path / "t")
    def slow_run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
    assert go(kd_release, s2, run=slow_run) == 0
    assert result(s2)["ok"] is False


def test_unexpected_error_writes_result_and_fails_the_unit(tmp_path, kd_release):
    s = make_env(tmp_path)
    def broken(rev, tar):
        raise TypeError("баг")
    with pytest.raises(TypeError):
        go(kd_release, s, switch=broken)  # a non-zero service code → OnFailure → kd-alert
    res = result(s)
    assert res["ok"] is False and "journalctl -u digipaddock-release on example-stage" in res["error"]


def test_request_arriving_during_run_is_processed(tmp_path, kd_release):
    s = make_env(tmp_path)
    second = {**REQUEST, "ts": "2.000"}
    def switch(rev, tar):
        s["switched"].append((rev, tar))
        if len(s["switched"]) == 1:
            (s["rel"] / "request.json").write_text(json.dumps(second), encoding="utf-8")
        return 0, "ok"
    assert go(kd_release, s, switch=switch) == 0
    assert len(s["switched"]) == 2 and result(s)["ts"] == "2.000"
    # the budget is over — the second request is left to the next run (PathExists)
    s2 = make_env(tmp_path / "b")
    ticks = iter([0.0, 100.0])
    def switch2(rev, tar):
        s2["switched"].append(rev)
        (s2["rel"] / "request.json").write_text(json.dumps(second), encoding="utf-8")
        return 0, "ok"
    assert go(kd_release, s2, switch=switch2, clock=lambda: next(ticks)) == 0
    assert len(s2["switched"]) == 1 and (s2["rel"] / "request.json").exists()


@pytest.mark.parametrize("rel", ["common/bin/kd-release", "common/install/release_switch.sh"])
def test_scripts_are_lf_and_executable_in_git_index(rel):
    root = BIN.parents[1]
    assert b"\r" not in (root / rel).read_bytes()
    out = subprocess.run(["git", "ls-files", "-s", rel], cwd=root, capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip("файл ещё не в индексе git")
    assert out.stdout.split()[0] == "100755"


@pytest.mark.skipif(not KNOWN.exists(), reason="ключ хоста Forgejo кладётся в Task 11 Step 0")
def test_forgejo_known_hosts_is_pinned_ed25519():
    lines = [ln for ln in KNOWN.read_text(encoding="utf-8").splitlines() if ln.strip() and not ln.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Za-z0-9.-]+ ssh-ed25519 [A-Za-z0-9+/=]{60,}", ln) for ln in lines)


def test_release_asks_again_after_the_cabinet_update(tmp_path, kd_release):
    """the request the old cabinet could not take goes again to the cabinet on the new code."""
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    steps = []
    answers = [{"state": "uncovered", "agents": ["dev"], "why": "unknown operation", "retry": True}, PENDING]

    def reinstall(link, rev, card, release_id):
        steps.append("reinstall")
        return answers.pop(0)

    def cabinet(link):
        steps.append("cabinet")
        return 0, ""

    assert go(kd_release, s, reinstall=reinstall, cabinet=cabinet) == 0
    assert steps == ["reinstall", "cabinet", "reinstall"]


@pytest.mark.parametrize("first", [
    {"state": "uncovered", "agents": ["dev"], "why": "no grant"},
    {"state": "uncovered", "agents": ["dev"], "why": "cabinet down", "retry": True},
])
def test_release_asks_once_when_refused_or_the_cabinet_update_failed(tmp_path, kd_release, first):
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    asked = []
    cabinet_rc = 0 if "retry" not in first else 1
    try:
        go(kd_release, s, reinstall=lambda *a: asked.append(1) or first, cabinet=lambda link: (cabinet_rc, "x"))
    except RuntimeError:
        pass
    assert asked == [1]


# --- the request that waits for the cabinet's update is published by its key ----------------
DEFERRED = {"state": "deferred", "key": f"release:{SHA[:7]}", "agents": ["boss", "dev"], "why": "unknown operation",
            "retry": True}


def test_a_deferred_request_is_published_by_its_key_and_covers_the_postflight(tmp_path, kd_release):
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    refused = (1, "generated runtime refused: dev: compiled policy: unsupported schema_version\n")
    answers = [DEFERRED, PENDING]
    seen = []

    def cabinet(link):
        seen.append((s["res"] / "result.json").exists())
        return 0, ""

    assert go(kd_release, s, postflight=lambda link: refused, reinstall=lambda *a: answers.pop(0),
              cabinet=cabinet) == 0
    assert seen == [False]
    r = result(s)
    assert r["ok"] is True and r["degraded"] is False and r["reinstall"]["key"] == f"release:{SHA[:7]}"


def test_a_deferred_request_the_new_cabinet_refuses_fails_the_unit(tmp_path, kd_release):
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    answers = [DEFERRED, {"state": "uncovered", "agents": ["boss", "dev"], "why": "no grant"}]
    with pytest.raises(RuntimeError, match="reinstall"):
        go(kd_release, s, reinstall=lambda *a: answers.pop(0), cabinet=lambda link: (0, ""))


# --- the health check after the cabinet's update ---------------------------------------------
def test_health_runs_after_a_successful_cabinet_update_and_its_failure_fails_the_unit_only(tmp_path, kd_release):
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    steps = []

    def health(link, rev, card, postflight=""):
        steps.append(("health", rev, card))
        return 1, "cabinet_schema"

    with pytest.raises(RuntimeError, match="health"):
        go(kd_release, s, cabinet=lambda link: steps.append("cabinet") or (0, ""), health=health)
    assert steps == ["cabinet", ("health", SHA[:7], "KD-12")]
    # the release stands (switched, the notice on the card carries the health); its result is degraded, not ok
    final = result(s)
    assert final["revision"] == SHA[:7] and final["ok"] is False and final["degraded"] is True


def test_health_runs_even_when_the_cabinet_update_failed(tmp_path, kd_release):
    """Review F12: a failed cabinet update is what the cabinet_schema check is for."""
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    asked = []
    with pytest.raises(RuntimeError, match="cabinet"):
        go(kd_release, s, cabinet=lambda link: (1, "x"), health=lambda *a: asked.append(a) or (1, "cabinet_schema"))
    assert [a[1:] for a in asked] == [(SHA[:7], "KD-12", "")]
    assert kd_release.T_HEALTH >= 900


def test_health_hears_the_postflight_failure(tmp_path, kd_release):
    """Review F4: the postflight text reaches the health check as the generated_runtime check."""
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    asked = []
    with pytest.raises(RuntimeError, match="postflight"):
        go(kd_release, s, postflight=lambda link: (1, "dev: instructions differ\n"),
           health=lambda link, rev, card, postflight="": asked.append(postflight) or (1, "generated_runtime"))
    assert asked == ["dev: instructions differ"]


def test_health_ok_is_a_clean_release(tmp_path, kd_release):
    s = make_env(tmp_path)
    assert go(kd_release, s, health=lambda *a: (0, "")) == 0


def test_the_e2e_runtime_follows_the_release_after_the_cabinet(tmp_path, kd_release):
    """Review I5: kd-e2e tests the deployed code — its runtime is rebuilt after the cabinet, before the health check."""
    s = make_env(tmp_path)
    order = []
    assert go(kd_release, s, cabinet=lambda link: order.append("cabinet") or (0, ""),
              e2e=lambda link: order.append(("e2e", link)) or (0, ""),
              health=lambda link, rev, card, postflight="": order.append("health") or (0, "")) == 0
    assert order == ["cabinet", ("e2e", "/opt/digipaddock"), "health"]


def test_a_failed_e2e_refresh_is_said_and_left_to_the_health_check(tmp_path, kd_release, capsys):
    s = make_env(tmp_path)
    assert go(kd_release, s, e2e=lambda link: (1, "npm ci: network")) == 0
    assert "kd-release: e2e runtime not updated" in capsys.readouterr().err and result(s)["ok"] is True


def test_the_default_e2e_refresh_skips_a_host_without_the_runtime(kd_release, tmp_path, monkeypatch):
    monkeypatch.setattr(kd_release, "E2E_RUNTIME", str(tmp_path / "none"))
    called = []
    monkeypatch.setattr(kd_release.subprocess, "run", lambda *a, **kw: called.append(a))
    assert kd_release._e2e_default("/opt/digipaddock") == (0, "")
    assert called == []


# --- the GitHub mirror after a healthy release --------------------------------------------------------
def test_a_healthy_release_starts_the_mirror_once(tmp_path, kd_release):
    s = make_env(tmp_path)
    started = []
    assert go(kd_release, s, mirror=lambda: started.append(1) or (0, "")) == 0
    assert started == [1]


def test_an_unhealthy_release_does_not_start_the_mirror(tmp_path, kd_release):
    s = make_env(tmp_path)
    started = []
    with pytest.raises(RuntimeError, match="health"):
        go(kd_release, s, health=lambda *a: (1, "nginx"), mirror=lambda: started.append(1) or (0, ""))
    assert started == []


def test_a_mirror_that_does_not_start_leaves_the_release_standing(tmp_path, kd_release, capsys):
    s = make_env(tmp_path)

    def broken():
        raise OSError("no systemctl")

    assert go(kd_release, s, mirror=broken) == 0 and result(s)["ok"] is True
    assert go(kd_release, make_env(tmp_path / "b"), mirror=lambda: (5, "Unit digipaddock-mirror.service not found.")) == 0
    err = capsys.readouterr().err
    assert "the GitHub mirror is not started: OSError" in err and "not found" in err
    assert "kd-release: the GitHub mirror" not in err  # not a line kd-alert takes as the reason of a failure


def test_the_default_mirror_start_does_not_wait_for_the_scan(kd_release, monkeypatch):
    seen = []
    monkeypatch.setattr(kd_release.subprocess, "run",
                        lambda argv, **kw: seen.append(argv) or kd_release.subprocess.CompletedProcess(argv, 0, "", ""))
    assert kd_release._mirror_default() == (0, "")
    assert seen == [["systemctl", "start", "--no-block", "digipaddock-mirror.service"]]


# --- a release is the pair code + overlay ----------------------------------------------------------------
def test_pair_release_composes_and_records_both_revisions(tmp_path, kd_release):
    s = make_env(tmp_path, overlay=OVERLAY_FILES)
    assert go(kd_release, s) == 0
    res = result(s)
    assert res["ok"] is True and res["revision"] == SHA[:7] and res["deploy_revision"] == DEP[:7]
    assert res["deploy_previous"] == DEP_PREV
    assert res["release_id"] == f"{SHA[:7]}+{DEP[:7]}"
    (rev, tar), = s["switched"]
    files = tar_names(tar)
    assert rev == SHA[:7] and files["DEPLOY_REVISION"] == f"{DEP[:7]}\n".encode()
    assert files["host/host.json"] == OVERLAY_FILES["host/host.json"] and files["README"] == b"digipaddock\n"
    assert files["common/install/forgejo_known_hosts"] == OVERLAY_FILES["common/install/forgejo_known_hosts"]
    joined = [" ".join(c) for c in s["calls"]]
    assert any(f"merge-base --is-ancestor {DEP_PREV} {DEP}" in c and "digipaddock-deploy" in c for c in joined)


def test_a_release_without_an_overlay_is_refused(tmp_path, kd_release):
    """Review I2: no tree works without host/host.json — a registry without the overlay entry releases nothing."""
    s = make_env(tmp_path, overlay=None)
    assert go(kd_release, s) == 0
    assert result(s)["error"].startswith("release: overlay_incompatible: ") and s["switched"] == []


def test_overlay_request_releases_overlay_main_with_code_main(tmp_path, kd_release):
    request = {"project": "digipaddock-deploy", "repo": "example/digipaddock-deploy", "sha": DEP, "pr": 3, "ts": "2.000"}
    s = make_env(tmp_path, overlay=OVERLAY_FILES, request=request)
    assert go(kd_release, s) == 0
    assert result(s)["ok"] is True and result(s)["release_id"] == f"{SHA[:7]}+{DEP[:7]}"


def test_sha_not_on_main_of_its_repo_refused(tmp_path, kd_release):
    request = {"project": "digipaddock-deploy", "repo": "example/digipaddock-deploy", "sha": "c" * 40, "pr": 3, "ts": "2"}
    s = make_env(tmp_path, overlay=OVERLAY_FILES, request=request)
    assert go(kd_release, s) == 0
    assert "main moved on" in result(s)["error"] and s["switched"] == []


def test_privacy_finding_in_code_refuses(tmp_path, kd_release):
    address = ".".join(["91", "0", "2", "3"])
    s = make_env(tmp_path, overlay=OVERLAY_FILES, code={**CODE_FILES, "common/x.py": f"HOST = '{address}'\n".encode()})
    assert go(kd_release, s) == 0
    res = result(s)
    assert res["ok"] is False and res["error"] == "release: privacy: 1 findings, first: common/x.py:1: ipv4"
    assert s["switched"] == []


def test_closed_scanner_finding_refuses(tmp_path, kd_release):
    s = make_env(tmp_path, overlay=OVERLAY_FILES)
    seen = []

    def closed(work):
        seen.append((sorted(p.name for p in Path(work, "overlay").iterdir()),
                     sorted(p.name for p in Path(work, "code").iterdir())))
        return ["README:1: secret-word"]

    assert go(kd_release, s, closed_scanner=closed) == 0
    assert result(s)["error"].startswith("release: privacy: 1 findings, first: README:1: secret-word")
    assert seen == [(["agents", "common", "host"], ["README", "common", "privacy-scan.json"])]
    assert s["switched"] == []


def test_a_synthetic_private_literal_still_blocks_the_release(tmp_path, kd_release):
    """The installed scanner with the overlay's list finds the literal; the literal stays out."""
    s = make_env(tmp_path, code={**CODE_FILES, "common/x.md": b"see secret-word here\n"})
    assert go(kd_release, s, closed_scanner=kd_release._closed_default) == 0
    assert result(s)["error"] == "release: privacy: 1 findings, first: common/x.md:1: private literal"
    assert s["switched"] == []
    assert [kd_release.SANDBOX_SCAN, "--list", f"{kd_release.SANDBOX_IN}/overlay/host/private_scan.json",
            f"{kd_release.SANDBOX_IN}/code"] in SANDBOXED


def test_the_closed_scanner_is_the_installed_trees_never_the_overlays(tmp_path, kd_release):
    """an overlay still carrying the old scripts/private_scan.py (the pair before the switch-over) is
    checked without running it — it is not an allowed path, it never reaches a host either."""
    marker = tmp_path / "ran"
    script = f"open({str(marker)!r}, 'w').close()\n".encode()
    s = make_env(tmp_path / "env", overlay={**OVERLAY_FILES, "scripts/private_scan.py": script},
                 overlay_modes={"scripts/private_scan.py": 0o755})
    assert go(kd_release, s, closed_scanner=kd_release._closed_default) == 0
    assert result(s)["ok"] is True and not marker.exists()
    (_, tar), = s["switched"]
    assert "scripts/private_scan.py" not in tar_names(tar)


def test_no_literal_list_refuses_the_release(tmp_path, kd_release):
    s = make_env(tmp_path, overlay={k: v for k, v in OVERLAY_FILES.items() if k != "host/private_scan.json"})
    assert go(kd_release, s, closed_scanner=kd_release._closed_default) == 0  # the release does not go blind
    assert "overlay has no host/private_scan.json" in result(s)["error"] and s["switched"] == []


@pytest.mark.parametrize("name, mode, data", [
    ("host/run.sh", 0o644, b"echo\n"),  # a script under host/
    ("host/hook.json", 0o755, b"{}"),  # an executable bit
    ("agents/sysadmin/servers.md", 0o644, b"#!/bin/sh\necho\n"),  # a script under a data name
    ("host/blob.json", 0o644, b"\0\1"),
])
def test_an_overlay_with_an_executable_is_refused_before_anything_runs(tmp_path, kd_release, name, mode, data):
    """The installed tree refuses it before the scanners, the composition and the switch."""
    s = make_env(tmp_path, overlay={**OVERLAY_FILES, name: data}, overlay_modes={name: mode})
    assert go(kd_release, s, closed_scanner=kd_release._closed_default) == 0
    assert result(s)["error"].startswith(f"release: overlay_invalid: {name}: ") and s["switched"] == []
    assert SANDBOXED == []


def test_the_sandbox_has_no_network_secrets_or_writable_tree(kd_release):
    """The unit the preflight's steps run in (the stage proves the rest)."""
    argv = kd_release._sandbox_argv(["-c", "pass"], "/w/in", 60)
    props = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    for prop in ("DynamicUser=yes", "PrivateNetwork=yes", "PrivateTmp=yes", "ProtectSystem=strict",
                 "ProtectHome=yes", "NoNewPrivileges=yes", "CapabilityBoundingSet=", "RuntimeMaxSec=60"):
        assert prop in props
    binds = [p for p in props if p.startswith("BindReadOnlyPaths=")]
    assert binds == [f"BindReadOnlyPaths=/w/in:{kd_release.SANDBOX_IN}"]
    hidden = next(p for p in props if p.startswith("InaccessiblePaths=")).split("=", 1)[1].split()
    assert {"-/etc/digipaddock", "-/var/lib/digipaddock", "-/run/digipaddock-release"} <= set(hidden)
    assert not any(p.startswith(("ReadWritePaths=", "BindPaths=", "User=")) for p in props)
    assert argv[:5] == ["systemd-run", "--wait", "--pipe", "--collect", "--quiet"]
    assert argv[argv.index("--") + 1:] == [kd_release.PYTHON, "-I", "-c", "pass"]


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_the_sandbox_reads_its_input_and_says_when_systemd_could_not_set_it_up(tmp_path, kd_release, monkeypatch):
    """The input is readable by the dynamic user; systemd's own exit codes are no scanner's verdict (review)."""
    monkeypatch.undo()  # the autouse stand-in: the real _sandboxed, systemd-run stood in below
    real = kd_release._sandboxed
    work = tmp_path / "in"
    (work / "code").mkdir(parents=True)
    (work / "code" / "a.txt").write_text("x")
    (work / "code" / "a.txt").chmod(0o600)
    (work / "code").chmod(0o700)
    monkeypatch.setattr(kd_release.subprocess, "run", lambda argv, **kw: CP(argv, 226, b"", b"Failed to set up mount"))
    with pytest.raises(RuntimeError, match="release: sandbox failed: exit 226: Failed to set up mount"):
        real(["-c", "pass"], str(work), 5)
    assert (work / "code" / "a.txt").stat().st_mode & 0o777 == 0o644 and (work / "code").stat().st_mode & 0o777 == 0o755
    monkeypatch.setattr(kd_release.subprocess, "run", lambda argv, **kw: CP(argv, 1, b"x:1: ipv4\n", b""))
    assert real(["-c", "pass"], str(work), 5) == (1, b"x:1: ipv4\n", "")


def test_the_composition_runs_sandboxed_and_returns_the_tar_on_stdout(tmp_path, kd_release):
    s = make_env(tmp_path)
    assert go(kd_release, s) == 0 and result(s)["ok"] is True
    compose, = [a for a in SANDBOXED if a[:2] == ["-c", kd_release.COMPOSE]]
    assert compose[-1] == "-" and all(compose[i].startswith(kd_release.SANDBOX_IN) for i in (2, 4, 5))


def test_code_without_its_scanner_config_is_refused(tmp_path, kd_release):
    s = make_env(tmp_path, code={k: v for k, v in CODE_FILES.items() if k != "privacy-scan.json"})
    assert go(kd_release, s) == 0
    assert result(s)["error"].startswith("release: privacy: scanner failed") and s["switched"] == []


@pytest.mark.parametrize("scan", ["public", "closed"])
def test_a_scanner_that_crashes_refuses_instead_of_passing(tmp_path, kd_release, monkeypatch, scan):
    """Review M1: an uncaught exception exits 1 with no findings on stdout — a failure, not a clean tree."""
    (tmp_path / "overlay" / "host").mkdir(parents=True)
    (tmp_path / "overlay" / "host" / "private_scan.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(kd_release, "_sandboxed",
                        lambda argv, work, timeout: (1, b"", "Traceback (most recent call last):\nKeyError: 'x'\n"))
    with pytest.raises(RuntimeError, match="(?s)scanner failed: .*KeyError"):
        if scan == "public":
            kd_release._public_findings(str(tmp_path))
        else:
            kd_release._closed_default(str(tmp_path))


def test_overlay_private_values_do_not_trip_scanner(tmp_path, kd_release):
    address = ".".join(["91", "0", "2", "3"])
    inventory = json.dumps({"home": "example-stage", "servers": {"x": {"host": address}}}).encode()
    s = make_env(tmp_path, overlay={**OVERLAY_FILES, "agents/sysadmin/inventory.json": inventory})
    assert go(kd_release, s) == 0 and result(s)["ok"] is True


def test_overlay_invalid_schema_refuses(tmp_path, kd_release):
    s = make_env(tmp_path, overlay={**OVERLAY_FILES, "host/overlay.json": b'{"schema_version": 2}'})
    assert go(kd_release, s) == 0
    assert result(s)["error"].startswith("release: overlay_invalid: host/overlay.json") and s["switched"] == []


def test_missing_required_overlay_file_refuses(tmp_path, kd_release):
    files = {k: v for k, v in OVERLAY_FILES.items() if k != "agents/sysadmin/inventory.json"}
    s = make_env(tmp_path, overlay=files)
    assert go(kd_release, s) == 0
    res = result(s)
    assert res["error"].startswith("release: overlay_incompatible: missing: agents/sysadmin/inventory.json")
    assert s["switched"] == []


def test_foreign_overlay_path_not_installed(tmp_path, kd_release):
    s = make_env(tmp_path, overlay={**OVERLAY_FILES, "CLAUDE.md": b"internal\n"},
                 code={**CODE_FILES, "CLAUDE.md": b"public\n"})
    assert go(kd_release, s) == 0
    (_, tar), = s["switched"]
    assert tar_names(tar)["CLAUDE.md"] == b"public\n"


def test_bootstrap_skips_ancestry(tmp_path, kd_release, capsys):
    s = make_env(tmp_path, overlay=OVERLAY_FILES, ancestor=False, dep_ancestor=False)
    assert go(kd_release, s) == 0
    assert "not from main" in result(s)["error"] and s["switched"] == []
    s2 = make_env(tmp_path / "b", overlay=OVERLAY_FILES, ancestor=False, dep_ancestor=False)
    (s2["rel"] / "request.json").unlink()  # review I1: the window's first release has no request file
    assert go(kd_release, s2, argv=["--bootstrap", "digipaddock"], current=lambda link: "") == 0
    assert result(s2)["ok"] is True and len(s2["switched"]) == 1 and s2["switched"][0][0] == SHA[:7]
    assert "kd-release: bootstrap — ancestry checks skipped" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["--bootstrap"], ["--bootstrap", "nope"], ["--bootstrap", "Bad!"]])
def test_bootstrap_names_a_project_of_the_registry(tmp_path, kd_release, argv, capsys):
    s = make_env(tmp_path, overlay=OVERLAY_FILES)
    (s["rel"] / "request.json").unlink()
    rc = go(kd_release, s, argv=argv)
    assert s["switched"] == []
    if len(argv) == 1:
        assert rc == 2 and "usage" in capsys.readouterr().err
    else:
        # bootstrap.sh reads the code — a refused first tree is no success
        assert rc == 1 and ("not in the registry" in result(s)["error"] or "bad request fields" in result(s)["error"])


def test_bootstrap_leaves_a_pending_request_to_the_path_unit(tmp_path, kd_release):
    s = make_env(tmp_path, overlay=OVERLAY_FILES)
    assert go(kd_release, s, argv=["--bootstrap", "digipaddock"]) == 0
    assert (s["rel"] / "request.json").exists() and len(s["switched"]) == 1


def test_overlay_not_from_main_is_refused(tmp_path, kd_release):
    s = make_env(tmp_path, overlay=OVERLAY_FILES, dep_ancestor=False)
    assert go(kd_release, s) == 0
    assert "overlay" in result(s)["error"] and DEP_PREV in result(s)["error"] and s["switched"] == []


def test_first_overlay_release_needs_no_ancestry(tmp_path, kd_release):
    s = make_env(tmp_path, overlay=OVERLAY_FILES, dep_ancestor=False)
    assert go(kd_release, s, current_deploy=lambda link: "") == 0 and result(s)["ok"] is True


def test_root_request_without_sha_releases_main(tmp_path, kd_release):
    s = make_env(tmp_path, overlay=OVERLAY_FILES,
                 request={"project": "digipaddock", "repo": "example/DigiPaddock", "ts": "3", "requester": "root"})
    asked = []
    assert go(kd_release, s, reinstall=lambda link, rev, card, release_id:
              asked.append(card) or {"state": "none"}) == 0
    assert result(s)["ok"] is True and s["switched"][0][0] == SHA[:7]
    assert asked == [""]  # root's own release: no card; the roles are asked for as root


@pytest.mark.parametrize("bad", [
    {"project": "digipaddock", "repo": "example/DigiPaddock", "ts": "3", "requester": "dev"},  # not root: sha and pr
    {"project": "digipaddock", "repo": "example/DigiPaddock", "ts": "3", "requester": "root", "sha": "zz"},
])
def test_only_root_may_omit_sha(tmp_path, kd_release, bad):
    s = make_env(tmp_path, request=bad)
    assert go(kd_release, s) == 0
    assert result(s)["ok"] is False and s["switched"] == []


def test_a_root_request_root_did_not_write_is_refused(tmp_path, kd_release, monkeypatch):
    """Review I3: "requester": "root" in a file another user wrote (the forge executor's directory) releases
    nothing and asks the factory nothing — only the file's owner makes it root's."""
    with pytest.raises(ValueError):
        kd_release._fields({"project": "digipaddock", "repo": "example/DigiPaddock", "requester": "root"}, ours=False)
    s = make_env(tmp_path, overlay=OVERLAY_FILES,
                 request={"project": "digipaddock", "repo": "example/DigiPaddock", "ts": "3", "requester": "root"})
    read = kd_release._read_request
    monkeypatch.setattr(kd_release, "_read_request", lambda path: (lambda f: f and (f[0], False))(read(path)))
    asked = []
    assert go(kd_release, s, reinstall=lambda *a: asked.append(a) or {"state": "none"}) == 0
    assert result(s)["ok"] is False and s["switched"] == [] and asked == []


def test_rollback_from_main_takes_the_injected_steps(kd_release, monkeypatch):
    """Review M2: main(["--rollback", …]) hands its own result directory and steps to rollback."""
    seen = {}
    monkeypatch.setattr(kd_release, "rollback", lambda target, **kw: seen.update(kw, target=target) or 0)
    steps = {name: (lambda *a, **kw: (0, "")) for name in ("render", "cabinet", "e2e", "postflight", "health")}
    assert kd_release.main(["--rollback", "/x"], result_dir="/r", lock_path=None, current=lambda link: PREV,
                           **steps) == 0
    assert seen["target"] == "/x" and seen["result_dir"] == "/r" and seen["current"](None) == PREV
    assert all(seen[name] is steps[name] for name in steps)


def test_reinstall_gets_release_id(tmp_path, kd_release):
    s = make_env(tmp_path, overlay=OVERLAY_FILES, request={**REQUEST, "card": "KD-12"})
    seen = []
    assert go(kd_release, s, reinstall=lambda link, rev, card, release_id: seen.append((rev, release_id)) or PENDING) == 0
    assert seen == [(SHA[:7], f"{SHA[:7]}+{DEP[:7]}")]


@pytest.mark.parametrize("registry", [
    {"digipaddock": {"repo": "example/DigiPaddock", "dir": "w", "release": "digipaddock"},
     "two": {"repo": "example/Other", "dir": "o", "release": "digipaddock", "role": "code"}},
    {"digipaddock": {"repo": "example/DigiPaddock", "dir": "w", "release": "digipaddock", "role": "deploy"}},
])
def test_registry_roles_are_checked(tmp_path, kd_release, registry):
    s = make_env(tmp_path)
    s["repos"].write_text(json.dumps(registry), encoding="utf-8")
    assert go(kd_release, s) == 0
    assert result(s)["ok"] is False and "role" in result(s)["error"] and s["switched"] == []


def test_preflight_finds_its_own_tree_through_the_usr_local_link(tmp_path):
    """/usr/local/bin/kd-release is a symlink to the release tree: kd.overlay comes from the tree it points to."""
    link = tmp_path / "bin" / "kd-release"
    link.parent.mkdir()
    try:
        link.symlink_to(BIN / "kd-release")
    except OSError:
        pytest.skip("no symlinks here")
    loader = SourceFileLoader("kd_release_link", str(link))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("kd_release_link", loader))
    loader.exec_module(module)
    assert Path(module.COMMON) == BIN.parent.resolve()


# --- rollback to a kept release (root, by hand) --------------------------------------------------------
def rollback_env(tmp_path, **over):
    steps = []
    res = tmp_path / "result"
    res.mkdir()
    base = tmp_path / "base"
    (base / "aaaaaaa-1").mkdir(parents=True)
    (base / "PREVIOUS").write_text(str(base / "aaaaaaa-1") + "\n", encoding="utf-8")
    kw = dict(result_dir=str(res), base=str(base), link="/opt/digipaddock",
              db_version=lambda: 30, target_schema=lambda target: steps.append(("schema", target)) or 30,
              switch_rollback=lambda target: steps.append(("switch", target)) or (0, "REVISION aaaaaaa (rollback)"),
              render=lambda link: steps.append("render") or (0, ""),
              cabinet=lambda link: steps.append("cabinet") or (0, ""),
              e2e=lambda link: steps.append("e2e") or (0, ""),
              postflight=lambda link: steps.append("postflight") or (0, ""),
              health=lambda link, rev, card, postflight="": steps.append(("health", rev, card)) or (0, ""),
              current=lambda link: "aaaaaaa")
    kw.update(over)
    return steps, res, base, kw


def test_rollback_runs_switch_render_cabinet_health_in_order(tmp_path, kd_release):
    """The e2e runtime follows the code rolled back to, as after a release: the health check finds a stale one."""
    steps, res, base, kw = rollback_env(tmp_path)
    assert kd_release.rollback(None, **kw) == 0
    target = str(base / "aaaaaaa-1")
    assert steps == [("schema", target), ("switch", target), "render", "cabinet", "e2e", "postflight",
                     ("health", "aaaaaaa", "")]
    found = json.loads((res / "result.json").read_text(encoding="utf-8"))
    assert found["ok"] is True and found["rollback"] is True and found["revision"] == "aaaaaaa"


def test_a_rollback_restarts_the_host_onto_the_code_rolled_back_to(tmp_path, kd_release):
    """Review M3: the host pinned the newer release; the rollback asks the factory for its restart, and the health
    check knows the operation."""
    asked, healths = [], []
    steps, res, base, kw = rollback_env(
        tmp_path, host=lambda link, release_id, rollback="": asked.append((link, release_id, rollback)) or
        {"state": "pending", "operation": "op9"},
        health=lambda link, rev, card, postflight="", restart="": healths.append(restart) or (0, ""))
    assert kd_release.rollback(None, **kw) == 0
    # a key of its own: the release's key (rolled back to) names that release's finished restart
    assert [a[:2] for a in asked] == [("/opt/digipaddock", "aaaaaaa")] and asked[0][2] and healths == ["op9"]


def test_main_hands_the_host_step_to_a_rollback(kd_release, monkeypatch):
    seen = {}
    monkeypatch.setattr(kd_release, "rollback", lambda target, **kw: seen.update(kw) or 0)
    host = lambda link, release_id: {"state": "none"}  # noqa: E731
    assert kd_release.main(["--rollback", "/x"], result_dir="/r", lock_path=None, host=host) == 0
    assert seen["host"] is host


def test_rollback_refuses_newer_db_schema(tmp_path, kd_release):
    steps, res, base, kw = rollback_env(tmp_path, db_version=lambda: 31)
    assert kd_release.rollback(str(base / "aaaaaaa-1"), **kw) == 1
    assert [s for s in steps if s[0] == "switch"] == []
    found = json.loads((res / "result.json").read_text(encoding="utf-8"))
    assert found["ok"] is False and found["rollback"] is True
    assert found["error"] == "rollback refused: cabinet schema 31 > 30; fix forward"


@pytest.mark.parametrize("where", ["outside", "nested", "base"])
def test_rollback_outside_the_base_is_refused_before_its_code_is_run(tmp_path, kd_release, where):
    """Review M5: the target's schema.py is code run as root — only a release directory under the base gets that."""
    steps, res, base, kw = rollback_env(tmp_path)
    target = {"outside": tmp_path / "elsewhere", "nested": base / "aaaaaaa-1" / "inner", "base": base}[where]
    target.mkdir(exist_ok=True)
    assert kd_release.rollback(str(target), **kw) == 1 and steps == []
    assert "not a release under" in json.loads((res / "result.json").read_text(encoding="utf-8"))["error"]


def test_rollback_without_previous_is_refused(tmp_path, kd_release):
    steps, res, base, kw = rollback_env(tmp_path)
    (base / "PREVIOUS").unlink()
    assert kd_release.rollback(None, **kw) == 1 and steps == []


def test_rollback_after_the_switch_fails_loudly(tmp_path, kd_release, capsys):
    steps, res, base, kw = rollback_env(tmp_path, cabinet=lambda link: (1, "npm: down"))
    assert kd_release.rollback(None, **kw) == 3
    assert "kd-release: cabinet not updated" in capsys.readouterr().err
    assert json.loads((res / "result.json").read_text(encoding="utf-8"))["degraded"] is True


def test_main_takes_rollback(tmp_path, kd_release, monkeypatch):
    seen = []
    monkeypatch.setattr(kd_release, "rollback", lambda target, **kw: seen.append(target) or 0)
    assert kd_release.main(["--rollback"], lock_path=None) == 0
    assert kd_release.main(["--rollback", "/opt/digipaddock-releases/x"], lock_path=None) == 0
    assert seen == [None, "/opt/digipaddock-releases/x"]
    assert kd_release.main(["--rollback", "a", "b"], lock_path=None) == 2


PASSPORT = {"base": "main", "targets": ["main"], "checks": {"profiles": {"unit": {"required": False}}},
            "merge_effects": {"main": "deploy to stage"}, "merge": {"require_up_to_date": False}}


def test_a_schema_2_registry_releases_as_before(tmp_path, kd_release):
    """the passport's keys do not change what is released — main of both repositories."""
    s = make_env(tmp_path)
    registry = json.loads(s["repos"].read_text(encoding="utf-8"))
    for entry in registry.values():
        entry.update(PASSPORT)
    s["repos"].write_text(json.dumps(registry), encoding="utf-8")
    assert go(kd_release, s) == 0 and result(s)["ok"] is True
    asked = [" ".join(c) for c in s["calls"] if "rev-parse" in c and "refs/heads/main" in c]
    assert len(asked) == 2


@pytest.mark.parametrize("project", ["digipaddock", "digipaddock-deploy"])
@pytest.mark.parametrize("base", ["dev", "trunk", "", "a..b", 7])
def test_a_release_entry_on_a_base_other_than_main_is_refused(tmp_path, kd_release, project, base):
    """Final review: a release releases main (the registry's rule, dev_agent.repos) — kd-release reads the raw file,
    so a hand edit of root's registry does not put another branch on the host."""
    s = make_env(tmp_path)
    registry = json.loads(s["repos"].read_text(encoding="utf-8"))
    registry[project]["base"] = base
    s["repos"].write_text(json.dumps(registry), encoding="utf-8")
    assert go(kd_release, s) == 0
    error = result(s)["error"]
    assert f"project {project}" in error and "main" in error and s["switched"] == []
    assert not any("rev-parse" in c for c in s["calls"])


# --- the session host follows the release ----------------------------------------------------------
def test_the_host_restart_is_asked_after_the_cabinet_and_reaches_the_health_check(tmp_path, kd_release, capsys):
    s = make_env(tmp_path, request={**REQUEST, "card": "KD-12"})
    order = []

    def health(link, rev, card, postflight="", restart=""):
        order.append(("health", restart))
        return 0, ""

    assert go(kd_release, s, cabinet=lambda link: order.append("cabinet") or (0, ""),
              host=lambda link, release_id: order.append(("host", release_id)) or
              {"state": "pending", "operation": "op1"}, health=health) == 0
    assert order[0] == "cabinet" and order[1][0] == "host" and order[1][1].startswith(SHA[:7])
    assert order[2:] == [("health", "op1")]
    assert "session host restart — operation op1" in capsys.readouterr().out


def test_a_host_restart_not_asked_is_said_and_left_to_the_health_check(tmp_path, kd_release, capsys):
    s = make_env(tmp_path)
    asked = []
    assert go(kd_release, s, host=lambda link, release_id: {"state": "uncovered", "why": "revision moved"},
              health=lambda *a: asked.append(a) or (0, "")) == 0
    assert "session host restart not requested: revision moved" in capsys.readouterr().err and len(asked) == 1


def test_the_default_host_restart_runs_the_new_code(kd_release, monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"state": "none"}\n', "")

    monkeypatch.setattr(kd_release.subprocess, "run", run)
    assert kd_release._host_restart_default("/opt/digipaddock", "abc1234") == {"state": "none"}
    assert seen[0][:3] == ["/usr/bin/python3", "-I", "-c"] and seen[0][-2:] == ["/opt/digipaddock", "abc1234"]
    kd_release._host_restart_default("/opt/digipaddock", "abc1234", rollback="1730000000.123")
    assert seen[1][-4:] == ["/opt/digipaddock", "abc1234", "--rollback", "1730000000.123"]
    assert "host_main" in seen[0][3]


# --- result.json ends with the release's final state -----------------------------------------------
@pytest.mark.parametrize("health_rc, state", [(0, "healthy"), (1, "degraded"), (2, "unchecked")])
def test_the_terminal_result_is_published_only_after_health(tmp_path, kd_release, health_rc, state):
    s = make_env(tmp_path)
    first = []

    def health(*a, **kw):
        first.append((s["res"] / "result.json").exists())
        return health_rc, "" if health_rc == 0 else "nginx"

    if health_rc:
        with pytest.raises(RuntimeError, match="health"):
            go(kd_release, s, health=health)
    else:
        assert go(kd_release, s, health=health) == 0
    assert first == [False]
    final = result(s)
    assert final["health"] == state and final["degraded"] is (health_rc != 0)
    # review I2: a degraded release is never ok, and says why (as every degraded result did before)
    assert final["ok"] is (health_rc == 0)
    assert ("nginx" in final["error"]) if health_rc else final["error"] == ""
    assert final["release_id"] == f"{SHA[:7]}+{DEP[:7]}"


# --- the first tree of a new host; a root release of a rev or a signed tag ---

def _given_registry(tmp_path, **change):
    data = json.loads((tmp_path / "repos.json").read_text(encoding="utf-8"))
    data["digipaddock"].update(change)
    given = tmp_path / "given-repos.json"
    given.write_text(json.dumps(data), encoding="utf-8")
    return given


def test_bootstrap_writes_repos_once(tmp_path, kd_release, capsys):
    s = make_env(tmp_path, overlay=OVERLAY_FILES)
    (s["rel"] / "request.json").unlink()
    given = _given_registry(tmp_path)
    s["repos"].unlink()
    assert go(kd_release, s, argv=["--bootstrap", "digipaddock", "--repos", str(given)]) == 0
    assert json.loads(s["repos"].read_text(encoding="utf-8")) == json.loads(given.read_text(encoding="utf-8"))
    assert result(s)["ok"] is True and len(s["switched"]) == 1
    assert go(kd_release, s, argv=["--bootstrap", "digipaddock", "--repos", str(given)]) == 0  # the same: fine
    assert len(s["switched"]) == 2
    other = _given_registry(tmp_path, dir="work/Other")
    before = s["repos"].read_text(encoding="utf-8")
    assert go(kd_release, s, argv=["--bootstrap", "digipaddock", "--repos", str(other)]) == 1
    assert "repos.json is already there and differs" in capsys.readouterr().err
    assert s["repos"].read_text(encoding="utf-8") == before and len(s["switched"]) == 2


def test_bootstrap_refuses_a_registry_add_repo_would_refuse(tmp_path, kd_release, capsys):
    s = make_env(tmp_path, overlay=OVERLAY_FILES)
    (s["rel"] / "request.json").unlink()
    bad = _given_registry(tmp_path, dir="../escape")
    s["repos"].unlink()
    assert go(kd_release, s, argv=["--bootstrap", "digipaddock", "--repos", str(bad)]) == 1
    assert "dir" in capsys.readouterr().err and not s["repos"].exists() and s["switched"] == []


def test_bootstrap_known_hosts_only_without_a_tree(tmp_path, kd_release, capsys):
    s = make_env(tmp_path, overlay=OVERLAY_FILES)
    (s["rel"] / "request.json").unlink()
    seen = []
    inner = s["run"]

    def run(cmd, **kw):
        if "env" in kw:
            seen.append(kw["env"]["GIT_SSH_COMMAND"])
        return inner(cmd, **kw)

    known = tmp_path / "overlay_known_hosts"
    known.write_text("forge.example.test ssh-ed25519 AAAA\n", encoding="utf-8")
    argv = ["--bootstrap", "digipaddock", "--known-hosts", str(known)]
    assert go(kd_release, s, argv=argv, run=run, current=lambda link: "") == 0
    assert seen and all(f"UserKnownHostsFile={known}" in c for c in seen)
    seen.clear()
    assert go(kd_release, s, argv=argv, run=run) == 0  # a tree is there: its pinned file, the flag said ignored
    assert seen and all("UserKnownHostsFile=/opt/digipaddock/common/install/forgejo_known_hosts" in c for c in seen)
    assert "--known-hosts is not needed" in capsys.readouterr().err


def test_the_first_tree_stops_after_the_switch(tmp_path, kd_release, capsys):
    """A host with no tree yet: the switch and the result only — stage_setup.sh installs the rest."""
    s = make_env(tmp_path, overlay=OVERLAY_FILES)
    (s["rel"] / "request.json").unlink()
    after = []
    hooks = {name: (lambda name: lambda *a, **k: after.append(name) or (0, ""))(name)
             for name in ("postflight", "cabinet", "render", "e2e", "health", "mirror")}
    assert go(kd_release, s, argv=["--bootstrap", "digipaddock"], current=lambda link: "",
              reinstall=lambda *a, **k: after.append("reinstall") or {"state": "none"},
              host=lambda *a, **k: after.append("host") or {"state": "none"}, **hooks) == 0
    assert after == [] and len(s["switched"]) == 1
    res = result(s)
    assert res["ok"] is True and res["revision"] == SHA[:7] and res["previous"] == ""
    assert "the first tree" in capsys.readouterr().out


TAG_SHA = "c" * 40


def _rev_env(tmp_path, rev, *, on_main=True, descends=True, signed=True, known=True, named=None):
    s = make_env(tmp_path, overlay=OVERLAY_FILES,
                 request={"project": "digipaddock", "repo": "example/DigiPaddock", "ts": "5", "requester": "root",
                          "rev": rev})
    inner = s["run"]

    def run(cmd, **kw):
        joined = " ".join(cmd)
        if "deploy" not in joined:
            if "rev-parse --verify --quiet" in joined and ("refs/tags/" in joined or TAG_SHA in joined):
                return CP(cmd, 0 if known else 128, TAG_SHA + "\n" if known else "", "")
            if f"merge-base --is-ancestor {TAG_SHA} refs/heads/main" in joined:
                return CP(cmd, 0 if on_main else 1, "", "")
            if f"merge-base --is-ancestor {PREV} {TAG_SHA}" in joined:
                return CP(cmd, 0 if descends else 1, "", "")
            if "verify-tag" in joined:  # --format=%(tag): the name the signed tag object carries
                s["calls"].append(list(cmd))
                return CP(cmd, 0 if signed else 1, (named or rev) + "\n" if signed else "",
                          "" if signed else "error: no signature found\n")
        return inner(cmd, **kw)

    s["run"] = run
    return s


def test_a_root_release_of_a_tag(tmp_path, kd_release):
    s = _rev_env(tmp_path, "v1.2.0")
    assert go(kd_release, s, allowed_signers=str(tmp_path / "none")) == 0
    assert result(s)["ok"] is True and s["switched"][0][0] == TAG_SHA[:7]
    assert not any("verify-tag" in " ".join(c) for c in s["calls"])  # no signers file: the tag is root's word


@pytest.mark.parametrize("rev,change,why", [
    ("c" * 40, {"on_main": False}, "rev_not_on_main"),
    ("v1.2.0", {"on_main": False}, "rev_not_on_main"),
    ("v1.2.0", {"descends": False}, "not from main"),
    ("v1.2.0", {"signed": False}, "tag_unsigned"),
    # a signed tag object of v1.0.0 under refs/tags/v1.2.0 — the signature is v1.0.0's, not this name's
    ("v1.2.0", {"named": "v1.0.0"}, "tag_unsigned: v1.2.0 — the signed tag is v1.0.0"),
    ("v9.9.9", {"known": False}, "rev_unknown"),
])
def test_rev_refusals(tmp_path, kd_release, rev, change, why):
    s = _rev_env(tmp_path, rev, **change)
    signers = tmp_path / "allowed_signers"
    signers.write_text("owner@example.test ssh-ed25519 AAAA\n", encoding="utf-8")
    assert go(kd_release, s, allowed_signers=str(signers)) == 0
    res = result(s)
    assert res["ok"] is False and why in res["error"] and s["switched"] == []


def test_a_signed_tag_is_verified_against_the_trees_signers(tmp_path, kd_release):
    s = _rev_env(tmp_path, "v1.2.0")
    signers = tmp_path / "allowed_signers"
    signers.write_text("owner@example.test ssh-ed25519 AAAA\n", encoding="utf-8")
    assert go(kd_release, s, allowed_signers=str(signers)) == 0
    assert result(s)["ok"] is True
    verify = [c for c in s["calls"] if "verify-tag" in c]
    assert verify == [["git", "-C", verify[0][2], "-c", "gpg.format=ssh", "-c",
                       f"gpg.ssh.allowedSignersFile={signers}", "verify-tag", "--format=%(tag)",
                       "refs/tags/v1.2.0"]]  # the tag ref itself, as rev-parse took the commit from it


@pytest.mark.parametrize("rev", ["main", "v1..2", "-v1", "v" + "1" * 70, "abc", "refs/heads/main"])
def test_a_rev_of_another_shape_is_refused(tmp_path, kd_release, rev):
    s = _rev_env(tmp_path, rev)
    assert go(kd_release, s, allowed_signers=str(tmp_path / "none")) == 0
    assert result(s)["ok"] is False and s["switched"] == []


def test_only_root_names_a_rev(tmp_path, kd_release):
    s = make_env(tmp_path, overlay=OVERLAY_FILES, request={**REQUEST, "rev": "v1.2.0"})
    assert go(kd_release, s) == 0
    assert result(s)["ok"] is False and s["switched"] == []
