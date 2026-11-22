"""the bank config a card's session launches with — the role config with the project's bank."""

import json
import os
import stat

import pytest

from kd import memory_bank

ROLE = {
    "serverMode": "self-hosted", "apiUrl": "http://127.0.0.1:9077", "apiToken": "t0ken", "optInOnly": True,
    "optInPaths": ["/srv/agents"], "mapPathToBank": {"/srv/agents/dev": "coding-agent::dev"},
    "bankIdTemplate": "x::{gitProject}", "dynamicBankId": True,
    "banks": {"coding-agent::dev": {"gitIngest": "message"}},
}


def _role(home, data=ROLE):
    folder = home / ".hindsight"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "coding-agent.json").write_text(json.dumps(data), "utf-8")


def test_the_bank_config_is_the_role_config_pinned_to_the_bank(tmp_path):
    _role(tmp_path)
    path = memory_bank.bank_config(tmp_path, "coding-agent::_Shop.v2")
    assert path == tmp_path / ".hindsight" / "bank-coding-agent%3A%3A_Shop.v2.json"  # one file per bank, any FS
    data = json.loads(path.read_text("utf-8"))
    # the plugin prefers mapPathToBank to bankId: a mapped folder would win over the project's bank
    assert data["bankId"] == "coding-agent::_Shop.v2"
    assert not {"mapPathToBank", "bankIdTemplate", "dynamicBankId"} & set(data)
    assert {k: v for k, v in data.items() if k != "bankId"} == {
        k: v for k, v in ROLE.items() if k not in ("mapPathToBank", "bankIdTemplate", "dynamicBankId")
    }
    if os.name != "nt":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_a_second_launch_rewrites_the_config(tmp_path):
    _role(tmp_path)
    memory_bank.bank_config(tmp_path, "b1")
    _role(tmp_path, {**ROLE, "apiToken": "new"})
    path = memory_bank.bank_config(tmp_path, "b1")
    assert json.loads(path.read_text("utf-8"))["apiToken"] == "new"
    assert sorted(p.name for p in path.parent.iterdir()) == ["bank-b1.json", "coding-agent.json"]  # no temp left


def test_no_role_config_no_bank_config(tmp_path):
    assert memory_bank.bank_config(tmp_path, "b1") is None
    assert not (tmp_path / ".hindsight").exists()


@pytest.mark.parametrize("bank", ["", "a/b", ".x", "-x", ":x", "a b", "x" * 101])
def test_a_bad_bank_is_refused(tmp_path, bank):
    _role(tmp_path)
    with pytest.raises(ValueError):
        memory_bank.bank_config(tmp_path, bank)


@pytest.mark.parametrize("text", ["not json", "[1]"])
def test_an_unreadable_role_config_is_an_error(tmp_path, text):
    (tmp_path / ".hindsight").mkdir()
    (tmp_path / ".hindsight" / "coding-agent.json").write_text(text, "utf-8")
    with pytest.raises(ValueError):
        memory_bank.bank_config(tmp_path, "b1")


def test_the_launcher_checks_the_same_bank_rule():
    from pathlib import Path

    launcher = (Path(__file__).resolve().parents[1] / "bin" / "agent-launch").read_text("utf-8")
    assert f"[[ $memory =~ ^{memory_bank.BANK.pattern}$ ]]" in launcher


def test_main_prints_the_path_or_nothing(tmp_path, capsys):
    home = str(tmp_path)
    assert memory_bank.main(["b1", home]) == 0 and capsys.readouterr().out == ""
    _role(tmp_path)
    assert memory_bank.main(["b1", home]) == 0
    assert capsys.readouterr().out.strip() == (tmp_path / ".hindsight" / "bank-b1.json").as_posix()
    for bad in (["a/b", home], ["b1"], ["b1", "relative"], []):
        assert memory_bank.main(bad) == 2
    (tmp_path / ".hindsight" / "coding-agent.json").write_text("[", "utf-8")
    assert memory_bank.main(["b1", home]) == 1
    assert "memory_bank" in capsys.readouterr().err
