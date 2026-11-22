"""Contours of the release tree: where a project's dev.check runs."""

import configparser
import json
from pathlib import Path

import pytest

from kd import contours

ROOT = Path(__file__).resolve().parents[2]


def _write(tmp_path, data) -> str:
    path = tmp_path / "contours.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_missing_file_is_no_contours(tmp_path):
    assert contours.load(str(tmp_path / "nope.json")) == {}


def test_example_loads():
    found = contours.load(str(ROOT / "host" / "contours.example.json"))
    ci = found["example-ci"]
    assert ci.host == contours.LOCAL
    assert ci.exclusive is False and ci.parallel == 2
    assert len(ci.components) == 2


def test_capacity(tmp_path):
    found = contours.load(_write(tmp_path, {
        "one": {"host": "local", "components": ["a"], "exclusive": True, "parallel": 3},
        "many": {"host": "local", "components": ["b"], "exclusive": False, "parallel": 3},
        "plain": {"host": "local", "components": ["c"]},
    }))
    assert contours.capacity(found["one"]) == 1
    assert contours.capacity(found["many"]) == 3
    assert found["plain"].exclusive is False and contours.capacity(found["plain"]) == 2




def test_env_names_the_file(tmp_path, monkeypatch):
    monkeypatch.setenv("KD_CONTOURS_JSON", _write(tmp_path, {"ci": {"host": "local", "components": ["app"]}}))
    assert list(contours.load()) == ["ci"]


@pytest.mark.parametrize("data", [
    {"ci": {"host": "", "components": ["a"]}},
    {"ci": {"host": "local", "components": []}},
    {"ci": {"host": "local", "components": ["Bad"]}},
    {"ci": {"host": "local", "components": ["a", "a"]}},
    {"one": {"host": "local", "components": ["a"]}, "two": {"host": "local", "components": ["a"]}},
    {"ci": {"host": "local", "components": ["a"], "parallel": 0}},
    {"ci": {"host": "local", "components": ["a"], "parallel": 5}},
    {"ci": {"host": "local", "components": ["a"], "parallel": True}},
    {"ci": {"host": "local", "components": ["a"], "exclusive": "yes"}},
    {"ci": {"host": "local", "components": ["a"], "cmd": "make"}},
    {"X": {"host": "local", "components": ["a"]}},
    {"ci": "local"},
    ["ci"],
])
def test_invalid_rejected(tmp_path, data):
    with pytest.raises(contours.ContourError):
        contours.load(_write(tmp_path, data))


def test_unreadable_file_rejected(tmp_path):
    path = tmp_path / "contours.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(contours.ContourError):
        contours.load(str(path))


def test_pytest_registers_the_host_marker():
    config = configparser.ConfigParser()
    config.read(ROOT / "pytest.ini", encoding="utf-8")
    markers = config["pytest"].get("markers", "")
    assert any(line.strip().startswith("host:") for line in markers.splitlines())
