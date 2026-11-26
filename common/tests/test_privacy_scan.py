"""Public privacy scanner: addresses, forbidden domains and secret-like strings."""

import os
import subprocess
import sys
from pathlib import Path

from kd import privacy_scan
from kd.privacy_scan import Config, scan_text, scan_tree

COMMON = Path(__file__).resolve().parents[1]
# Samples are assembled at run time: this file is itself under the repository's privacy gate.
DOMAIN = "private-" + "example.co"
CFG = Config(forbidden_domains=(DOMAIN,), exclude=("excluded/**",))
PUBLIC = ".".join(["91", "0", "2", "3"])
PUBLIC2 = ".".join(["91", "0", "2", "4"])
KEY_HEADER = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"


def kinds(findings):
    return [f.kind for f in findings]


def test_public_ip_is_a_finding():
    assert kinds(scan_text("host " + PUBLIC, "a.py", CFG)) == ["ipv4"]


def test_test_net_and_private_ranges_pass():
    assert scan_text("203.0.113.10 10.0.0.1 192.168.1.1 127.0.0.1 0.0.0.0 172.16.0.1 169.254.1.1", "a", CFG) == []


def test_not_an_address_passes():
    assert scan_text("999.1.1.1 1.2.3 1.2.3.4.5", "a", CFG) == []


def test_version_like_passes():
    assert scan_text("python 3.12.1.0 and v1.2.3.4 version 5.6.7.8", "a", CFG) == []


def test_forbidden_domain():
    assert kinds(scan_text("https://board." + DOMAIN + "/x", "a", CFG)) == ["domain"]
    assert kinds(scan_text("see " + DOMAIN, "a", CFG)) == ["domain"]
    assert scan_text("not" + DOMAIN + "m digipaddock /opt/digipaddock", "a", CFG) == []


def test_secret_patterns():
    text = KEY_HEADER + "\n123456789:" + "A" * 35
    assert kinds(scan_text(text, "a", CFG)) == ["private-key", "telegram-token"]


def test_masked_key_header_passes():
    assert scan_text("-----BEGIN " + "PRIVATE KEY-----***-----END PRIVATE KEY-----", "a", CFG) == []


def test_more_secret_patterns():
    assert kinds(scan_text("sk-ant-" + "a" * 24, "a", CFG)) == ["anthropic-key"]
    assert kinds(scan_text("ghp_" + "A" * 36, "a", CFG)) == ["github-token"]
    assert kinds(scan_text("AKIA" + "A" * 16, "a", CFG)) == ["aws-key"]
    assert kinds(scan_text("ssh-ed25519 AAAA" + "B" * 30, "a", CFG)) == ["ssh-pubkey"]
    assert kinds(scan_text("FORGE_TOKEN=" + "a" * 40, "a", CFG)) == ["forgejo-token"]
    assert scan_text("commit " + "a" * 40, "a", CFG) == []
    # provider keys
    assert kinds(scan_text("sk-or-v1-" + "0" * 64, "a", CFG)) == ["openrouter-key"]
    assert kinds(scan_text("fw_" + "A" * 24, "a", CFG)) == ["fireworks-key"]
    assert scan_text("sk-or-v1-*** fw_*** fw_short", "a", CFG) == []


def test_findings_carry_path_and_line():
    (finding,) = scan_text("ok\nhost " + PUBLIC, "x/a.py", CFG)
    assert (finding.path, finding.line, finding.kind) == ("x/a.py", 2, "ipv4")
    assert PUBLIC in finding.text


def test_allow_marker_and_exclude(tmp_path):
    (tmp_path / "a.py").write_text("host " + PUBLIC + "  # privacy-scan: allow\n")
    (tmp_path / "excluded").mkdir()
    (tmp_path / "excluded" / "b.md").write_text(PUBLIC + "\n")
    (tmp_path / "c.md").write_text(PUBLIC2 + "\n")
    assert [(f.path, f.kind) for f in scan_tree(tmp_path, CFG)] == [("c.md", "ipv4")]


def test_binary_is_scanned_not_skipped(tmp_path):
    (tmp_path / "blob.bin").write_bytes(b"\0" + PUBLIC.encode())
    assert [(f.path, f.line, f.kind) for f in scan_tree(tmp_path, CFG)] == [("blob.bin", 1, "ipv4")]


def test_git_dir_skipped(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("url = https://board." + DOMAIN + "\n")
    assert scan_tree(tmp_path, CFG) == []


def test_tracked_only_reads_git_ls_files(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tracked.md").write_text(PUBLIC + "\n")
    (tmp_path / "untracked.md").write_text(PUBLIC2 + "\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "tracked.md"], check=True)
    assert [f.path for f in scan_tree(tmp_path, CFG, tracked_only=True)] == ["tracked.md"]


def test_load_config(tmp_path):
    path = tmp_path / "privacy-scan.json"
    path.write_text('{"forbidden_domains": ["example.org"], "exclude": ["tmp/**"]}')
    cfg = privacy_scan.load_config(path)
    assert cfg.forbidden_domains == ("example.org",) and cfg.exclude == ("tmp/**",)


def _cli(*args):
    env = {**os.environ, "PYTHONPATH": str(COMMON)}
    return subprocess.run([sys.executable, "-m", "kd.privacy_scan", *map(str, args)],
                          capture_output=True, text=True, env=env, cwd=str(COMMON))


def test_cli_exit_codes(tmp_path):
    config = tmp_path / "privacy-scan.json"
    config.write_text('{"forbidden_domains": ["%s"], "exclude": []}' % DOMAIN)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.md").write_text("clean\n")
    assert _cli(tree, "--config", config).returncode == 0
    (tree / "b.md").write_text(PUBLIC + "\n")
    result = _cli(tree, "--config", config)
    assert result.returncode == 1
    assert "b.md:1: ipv4:" in result.stdout
    assert _cli(tmp_path / "absent", "--config", config).returncode == 2


def test_repository_config_is_valid():
    """privacy-scan.json gates this tree. Before the split the repository also holds the public tree's
    config, privacy-scan.public.json: it names no deployment domain (the closed scanner covers those)."""
    cfg = privacy_scan.load_config(COMMON.parent / "privacy-scan.json")
    public = COMMON.parent / "privacy-scan.public.json"
    if public.is_file():
        assert DOMAIN in cfg.forbidden_domains
        cfg = privacy_scan.load_config(public)
    assert DOMAIN not in cfg.forbidden_domains and "tmp/**" in cfg.exclude
