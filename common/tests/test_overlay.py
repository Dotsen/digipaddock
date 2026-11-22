"""Deployment overlay: composition of code + overlay by allowlist, and its validators."""

import io
import json
import os
import tarfile
from pathlib import Path

import pytest

from kd import overlay

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "host"
HOST = (FIXTURES / "host.json").read_bytes()
INVENTORY = b'{"servers": {"example-stage": {"host": "203.0.113.10"}}}'


def _tar(files, symlinks=()):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        for name, target in symlinks:
            info = tarfile.TarInfo(name)
            info.type = tarfile.SYMTYPE
            info.linkname = target
            tar.addfile(info)
    return buf.getvalue()


OVERLAY_FILES = {
    "host/overlay.json": b'{"schema_version": 1}',
    "host/host.json": HOST,
    "agents/sysadmin/inventory.json": INVENTORY,
    "CLAUDE.md": b"internal instructions",
    "docs/internal/x.md": b"internal",
}


@pytest.fixture
def code_tar():
    return _tar({
        "common/kd/naming.py": b"# code",
        "agents/sysadmin/inventory.json": b'{"servers": {}}',
        "CLAUDE.md": b"public instructions",
    })


@pytest.fixture
def overlay_tar():
    return _tar(OVERLAY_FILES)


@pytest.fixture
def overlay_tar_with_symlink():
    return _tar({"host/overlay.json": b'{"schema_version": 1}'}, symlinks=[("host/host.json", "/etc/passwd")])


def _members(data):
    out = tarfile.open(fileobj=io.BytesIO(data))
    return out, {m.name: m for m in out.getmembers()}


def test_compose_overrides_and_adds_deploy_revision(code_tar, overlay_tar):
    out, members = _members(overlay.compose(code_tar, overlay_tar, "def5678"))
    assert out.extractfile("agents/sysadmin/inventory.json").read() == INVENTORY
    assert out.extractfile("host/host.json").read() == HOST
    assert out.extractfile("DEPLOY_REVISION").read() == b"def5678\n"
    # foreign overlay paths are dropped: the code's CLAUDE.md stays, docs/internal never lands
    assert out.extractfile("CLAUDE.md").read() == b"public instructions"
    assert not [name for name in members if name.startswith("docs/internal")]
    assert list(members).count("agents/sysadmin/inventory.json") == 1


def test_compose_refuses_symlink_in_overlay(code_tar, overlay_tar_with_symlink):
    with pytest.raises(overlay.OverlayError):
        overlay.compose(code_tar, overlay_tar_with_symlink, "def5678")


def test_compose_refuses_dotdot_in_overlay(code_tar):
    with pytest.raises(overlay.OverlayError):
        overlay.compose(code_tar, _tar({"host/../../etc/x": b"x"}), "def5678")


def test_compose_refuses_bad_deploy_revision(code_tar, overlay_tar):
    with pytest.raises(overlay.OverlayError):
        overlay.compose(code_tar, overlay_tar, "nothex!")


def _write(root, files):
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def test_check_overlay_accepts_valid(tmp_path):
    assert overlay.check_overlay(_write(tmp_path, OVERLAY_FILES)) == []


def test_check_overlay_reports_schema_and_secret(tmp_path):
    files = dict(OVERLAY_FILES)
    files["host/overlay.json"] = b'{"schema_version": 2}'
    files["agents/sysadmin/servers.md"] = b"-----BEGIN OPENSSH PRIVATE KEY-----\n"  # privacy-scan: allow (fake sample)
    errors = overlay.check_overlay(_write(tmp_path, files))
    assert any("schema_version" in e for e in errors)
    assert any("agents/sysadmin/servers.md" in e and "private-key" in e for e in errors)


def test_check_overlay_accepts_the_forges_host_key(tmp_path):
    """A public host key is the overlay's data (known_hosts), not a secret; the code tree forbids it, not here."""
    files = dict(OVERLAY_FILES)
    files["common/install/forgejo_known_hosts"] = b"[forge.example]:2222 ssh-ed25519 AAAA" + b"C" * 64 + b"\n"
    assert overlay.check_overlay(_write(tmp_path, files)) == []


def test_check_overlay_does_not_refuse_over_files_that_never_reach_a_host(tmp_path):
    """Review M8: an internal doc quoting a token-like line must not refuse every release."""
    files = dict(OVERLAY_FILES)
    files["docs/internal/notes.md"] = b"token " + b"0123456789abcdef" * 2 + b"01234567\n"
    files["docs/internal/key.md"] = b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----\n"
    assert overlay.check_overlay(_write(tmp_path, files)) == []


def test_check_overlay_reports_missing_overlay_json_and_bad_host(tmp_path):
    files = {k: v for k, v in OVERLAY_FILES.items() if k != "host/overlay.json"}
    files["host/host.json"] = b'{"schema_version": 1}'
    errors = overlay.check_overlay(_write(tmp_path, files))
    assert any("host/overlay.json" in e for e in errors)
    assert any("host/host.json" in e for e in errors)


def test_check_overlay_reports_invalid_json(tmp_path):
    files = dict(OVERLAY_FILES)
    files["agents/sysadmin/inventory.json"] = b"{not json"
    errors = overlay.check_overlay(_write(tmp_path, files))
    assert any("agents/sysadmin/inventory.json" in e for e in errors)


def _full_tree(root):
    files = {path: b"{}" if path.endswith(".json") else b"x" for path in overlay.ALLOWED if not path.endswith("/")}
    files["host/host.json"] = HOST
    return _write(root, files)


def test_check_tree_accepts_full_tree(tmp_path):
    assert overlay.check_tree(_full_tree(tmp_path)) == []


def test_check_tree_missing_required(tmp_path):
    root = _full_tree(tmp_path)
    (root / "agents/sysadmin/inventory.json").unlink()
    assert overlay.check_tree(root) == ["missing: agents/sysadmin/inventory.json"]


def test_check_tree_bad_host_json(tmp_path):
    root = _full_tree(tmp_path)
    (root / "host/host.json").write_text(json.dumps({"schema_version": 1}))
    assert [e for e in overlay.check_tree(root) if e.startswith("host/host.json")]


def test_foreign_lists_paths_outside_allowlist(tmp_path):
    assert overlay.foreign(_write(tmp_path, OVERLAY_FILES)) == ["CLAUDE.md", "docs/internal/x.md"]


def test_allowed():
    assert overlay.allowed("host/contours.json")
    assert overlay.allowed("agents/sysadmin/inventory.json")
    assert not overlay.allowed("agents/sysadmin/inventory.json.bak")
    assert not overlay.allowed("CLAUDE.md")
    assert not overlay.allowed("host")


# --- the release's composition step, run by kd-release with the code being released -----------------------------
def test_unpack_writes_regular_files_only(tmp_path):
    overlay.unpack(_tar({"a/b.txt": b"1", "c": b"2"}), tmp_path / "t")
    assert (tmp_path / "t/a/b.txt").read_bytes() == b"1" and (tmp_path / "t/c").read_bytes() == b"2"
    with pytest.raises(overlay.OverlayError):
        overlay.unpack(_tar({"../x": b"1"}), tmp_path / "u")
    with pytest.raises(overlay.OverlayError):
        overlay.unpack(_tar({}, symlinks=[("l", "/etc/passwd")]), tmp_path / "v")


def _full_overlay():
    files = {path: b"{}" if path.endswith(".json") else b"x" for path in overlay.ALLOWED if not path.endswith("/")}
    files.update({"host/host.json": HOST, "host/overlay.json": b'{"schema_version": 1}'})
    return files


def _release(tmp_path, code, files):
    paths = {name: tmp_path / name for name in ("code.tar", "overlay.tar", "out.tar")}
    paths["code.tar"].write_bytes(code)
    paths["overlay.tar"].write_bytes(_tar(files))
    rc = overlay.main(["release", str(paths["code.tar"]), str(paths["overlay.tar"]), "abcdef1", str(paths["out.tar"])])
    return rc, paths["out.tar"]


def test_release_main_composes(tmp_path, code_tar, capsys):
    rc, out = _release(tmp_path, code_tar, _full_overlay())
    assert rc == 0
    with tarfile.open(out) as tar:
        assert tar.extractfile("DEPLOY_REVISION").read() == b"abcdef1\n"


def test_release_main_invalid_overlay(tmp_path, code_tar, capsys):
    rc, out = _release(tmp_path, code_tar, {**_full_overlay(), "host/overlay.json": b'{"schema_version": 9}'})
    assert rc == 3 and capsys.readouterr().out.startswith("overlay_invalid: host/overlay.json") and not out.exists()


def test_release_main_writes_the_tar_to_stdout_for_the_sandbox(tmp_path, code_tar, capsysbinary):
    """kd-release's sandbox writes nowhere — ``-`` gives the tar back on stdout."""
    (tmp_path / "code.tar").write_bytes(code_tar)
    (tmp_path / "overlay.tar").write_bytes(_tar(_full_overlay()))
    assert overlay.main(["release", str(tmp_path / "code.tar"), str(tmp_path / "overlay.tar"), "abcdef1", "-"]) == 0
    with tarfile.open(fileobj=io.BytesIO(capsysbinary.readouterr().out)) as tar:
        assert tar.extractfile("DEPLOY_REVISION").read() == b"abcdef1\n"


# --- an overlay is data only ------------------------------------------------------------------------------
def _tar_modes(files, modes):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), modes.get(name, 0o644)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_the_scanner_is_no_overlay_path_any_more():
    assert not overlay.allowed("scripts/private_scan.py")


def test_data_errors_accepts_the_data_of_an_overlay():
    files = {**_full_overlay(), "host/allowed_signers": b"owner ssh-ed25519 AAAA\n", "CLAUDE.md": b"internal",
             "scripts/split_snapshot.py": b"#!/usr/bin/env python3\n"}  # foreign paths never reach a host
    assert overlay.data_errors(_tar_modes(files, {"scripts/split_snapshot.py": 0o755})) == []


@pytest.mark.parametrize("name, mode, data, why", [
    ("host/hook.json", 0o755, b"{}", "executable"),
    ("host/run.sh", 0o644, b"echo\n", "not a data file"),
    ("host/run.py", 0o644, b"print()\n", "not a data file"),
    ("agents/sysadmin/servers.md", 0o644, b"#!/bin/sh\n", "not text data"),
    ("common/install/forgejo_known_hosts", 0o644, b"\0ELF", "not text data"),
])
def test_data_errors_refuses_code_in_an_allowed_path(name, mode, data, why):
    errors = overlay.data_errors(_tar_modes({**_full_overlay(), name: data}, {name: mode}))
    assert len(errors) == 1 and errors[0].startswith("{}: {}".format(name, why))


def test_data_errors_wants_the_overlay_json_shape_not_its_range():
    files = _full_overlay()
    assert overlay.data_errors(_tar_modes({**files, "host/overlay.json": b'{"schema_version": 9}'}, {})) == []
    del files["host/overlay.json"]
    assert overlay.data_errors(_tar_modes(files, {})) == [
        "host/overlay.json: missing or without an integer schema_version"]


def test_release_refuses_an_executable_overlay_file(tmp_path, code_tar, capsys):
    paths = {name: tmp_path / name for name in ("code.tar", "overlay.tar", "out.tar")}
    paths["code.tar"].write_bytes(code_tar)
    paths["overlay.tar"].write_bytes(_tar_modes(_full_overlay(), {"agents/sysadmin/inventory.json": 0o755}))
    rc = overlay.main(["release", str(paths["code.tar"]), str(paths["overlay.tar"]), "abcdef1", str(paths["out.tar"])])
    assert rc == 3 and "inventory.json: executable" in capsys.readouterr().out and not paths["out.tar"].exists()


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_check_overlay_refuses_an_executable_file_of_a_checkout(tmp_path):
    root = _write(tmp_path, OVERLAY_FILES)
    (root / "agents/sysadmin/inventory.json").chmod(0o755)
    assert "agents/sysadmin/inventory.json: executable — an overlay carries data only" in overlay.check_overlay(root)


def test_release_main_incompatible_tree(tmp_path, code_tar, capsys):
    files = {k: v for k, v in _full_overlay().items() if k != "agents/sysadmin/servers.md"}
    rc, out = _release(tmp_path, code_tar, files)
    assert rc == 4 and capsys.readouterr().out.strip() == "overlay_incompatible: missing: agents/sysadmin/servers.md"
    assert not out.exists()


EGRESS = json.loads((FIXTURES / "egress.json").read_text("utf-8"))


def _with_egress(tmp_path, document):
    files = dict(OVERLAY_FILES)
    files["host/egress.json"] = json.dumps(document).encode()
    return overlay.check_overlay(_write(tmp_path, files))


def test_check_overlay_accepts_a_valid_egress_file(tmp_path):
    assert _with_egress(tmp_path, EGRESS) == []  # dev of this code tree holds net.egress


def test_check_overlay_refuses_a_broken_egress_file(tmp_path):
    # the release refuses it, not the cabinet's sweep after it: a broken file grants nothing and stops every exit
    errors = _with_egress(tmp_path, {**EGRESS, "projects": {"shop": ["probe"]}})
    assert any(e.startswith("host/egress.json:") for e in errors)


def test_check_overlay_refuses_an_exit_agent_without_the_capability(tmp_path):
    # every agent of this tree holds it; an agent the tree has no manifest of holds nothing
    probe = {**EGRESS["exits"]["probe"], "agents": ["dev", "ghost"]}
    errors = _with_egress(tmp_path, {**EGRESS, "exits": {"probe": probe}})
    assert errors == ["host/egress.json: exit probe lists ghost, whose manifest has no net.egress"]
