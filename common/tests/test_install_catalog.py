"""The install catalogs: pinned versions and the host's secrets."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kd import install_catalog, naming

REPO = Path(__file__).resolve().parents[2]
HOST = naming.load_host(str(REPO / "host" / "host.example.json"))


def test_versions_has_exactly_the_pinned_keys(tmp_path):
    v = install_catalog.versions()
    assert v["claude_code"] == "2.1.289"
    assert all(len(v[f"claude_code_sha256_{arch}"]) == 64 for arch in ("amd64", "arm64"))
    assert v["node_major"] == "22"
    assert v["hindsight_api"] == "1.1.0"
    assert v["torch"] == "2.14.1"
    assert v["postgresql"] == "18"
    assert v["pgvector_min"] == "0.8.6"
    assert v["hindsight_plugin"] == "0.8.0"
    assert v["osv_scanner"] == "2.7.1"
    assert all(len(v[f"osv_scanner_sha256_{arch}"]) == 64 for arch in ("amd64", "arm64"))
    extra = dict(json.loads((REPO / "common" / "install" / "versions.json").read_text(encoding="utf-8")), docker="27")
    (tmp_path / "v.json").write_text(json.dumps(extra), encoding="utf-8")
    with pytest.raises(install_catalog.CatalogError):
        install_catalog.versions(str(tmp_path / "v.json"))
    short = dict(extra)
    del short["docker"], short["torch"]
    (tmp_path / "v.json").write_text(json.dumps(short), encoding="utf-8")
    with pytest.raises(install_catalog.CatalogError):
        install_catalog.versions(str(tmp_path / "v.json"))


def test_secrets_render_host_accounts():
    by = {s.name: s for s in install_catalog.secrets(host=HOST, prefix="digi")}
    assert set(by) == {"forge-dev", "forge-merge", "review", "devcheck", "claude-token", "pult",
                      "llm-openrouter", "llm-fireworks", "llm-deepseek", "llm-custom", "llm-anthropic", "llm-openai"}  # not generated ones
    assert by["forge-dev"].path == "/etc/digipaddock/forge/digipaddock-dev.env"
    assert by["forge-merge"].path == "/etc/digipaddock/forge/digipaddock-merge.env"
    assert (by["forge-dev"].owner, by["forge-dev"].group, by["forge-dev"].mode) == ("root", "digi-forge", 0o640)
    assert by["review"].path == "/etc/digipaddock/review/digipaddock-review.env"
    assert (by["review"].owner, by["review"].group, by["review"].mode) == ("digi-review", "digi-review", 0o600)
    assert (by["devcheck"].owner, by["devcheck"].mode) == ("root", 0o600)
    assert by["pult"].wanted_by == ()  # optional: the Telegram copy of critical alerts
    assert by["forge-dev"].wanted_by == ("dev",)
    assert by["forge-dev"].format == "FORGE_TOKEN=<token>"
    # The units that read the file at start, restarted by kd-secret put; the others read it per run
    assert by["claude-token"].restart == ("hindsight-api.service", "hindsight-worker.service")
    assert all(s.restart == () for name, s in by.items() if name != "claude-token")
    only = install_catalog.secrets(host=HOST, prefix="digi", names=["review"])
    assert [s.name for s in only] == ["review"]
    with pytest.raises(install_catalog.CatalogError):
        install_catalog.secrets(host=HOST, prefix="digi", names=["ghost"])


def test_secrets_say_where_the_value_comes_from():
    """How and format are for the host administrator — English, the forge account by its name, not
    by the host.json key; check — the arguments the installer's check takes (forge_setup.sh: the agent and its user)."""
    by = {s.name: s for s in install_catalog.secrets(host=HOST, prefix="digi")}
    assert by["forge-dev"].how.startswith("Forgejo → account {} → ".format(HOST["forge_dev_user"]))
    assert HOST["forge_merge_user"] in by["forge-merge"].how and HOST["forge_review_user"] in by["review"].how
    for s in by.values():
        assert "forge_" not in s.how and "@" not in s.how, s.name
        assert not any("\u0400" <= c <= "\u04ff" for c in s.how + s.format), s.name
        assert s.format.split("=", 1)[-1].startswith("<"), s.name  # the keys stay, a placeholder goes after them
    assert by["forge-dev"].check == by["forge-merge"].check == "check dev digi-dev"
    assert by["review"].check == by["devcheck"].check == "check" and by["pult"].check == ""


def test_secret_modes_match_their_installers():
    for s in install_catalog.secrets(host=HOST, prefix="digi"):
        text = (REPO / s.needed_by).read_text(encoding="utf-8")
        assert format(s.mode, "o") in text, (s.name, s.needed_by)
        assert s.path.rsplit("/", 1)[0] in text or s.name.startswith("forge-") or s.name == "review", s.name


def test_hindsight_client_reads_the_plugin_version_from_the_catalog():
    text = (REPO / "common" / "install" / "lib" / "hindsight_client.sh").read_text(encoding="utf-8")
    assert "HINDSIGHT_PLUGIN_VERSION=0.8.0" not in text
    assert "install_catalog.py\" version hindsight_plugin" in text


def test_version_cli_prints_the_value():
    out = subprocess.run(
        [sys.executable, "-I", str(REPO / "common" / "kd" / "install_catalog.py"), "version", "claude_code"],
        capture_output=True, text=True, check=True,
    )
    assert out.stdout == "2.1.289\n"
    bad = subprocess.run(
        [sys.executable, "-I", str(REPO / "common" / "kd" / "install_catalog.py"), "version", "docker"],
        capture_output=True, text=True,
    )
    assert bad.returncode == 2 and bad.stdout == ""
