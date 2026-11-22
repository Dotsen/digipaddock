"""the pipeline's mode and limits — repo defaults, the root's file, its cached reader and its writer."""

import json

import pytest

from kd import pipeline_config


@pytest.fixture
def defaults(tmp_path):
    path = tmp_path / "pipeline.json"
    path.write_text(json.dumps({"continuations": {"windowMinutes": 60, "perWindow": 6, "streak": 3}}), encoding="utf-8")
    return str(path)


def test_pipeline_config_defaults_to_observe(tmp_path, defaults):
    config = pipeline_config.load(str(tmp_path / "none.json"), defaults)
    assert (config.mode, config.since, config.enforcing) == ("observe", "", False)
    assert (config.window_s, config.per_window, config.streak) == (3600, 6, 3)


def test_who_closes_done_is_no_longer_the_codes(tmp_path):
    """the project's ``done_by`` in the cabinet says it; a defaults file that still names it is refused."""
    old = tmp_path / "pipeline.json"
    old.write_text(json.dumps({"continuations": {}, "doneBy": {"dev": ["digipaddock"]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="continuations only"):
        pipeline_config.load(str(tmp_path / "none.json"), str(old))
    assert not hasattr(pipeline_config.Config(), "done_by")


def test_the_repo_defaults_load():
    config = pipeline_config.load("/nonexistent/pipeline.json")
    assert (config.mode, config.per_window, config.streak, config.window_s) == ("observe", 6, 3, 3600)


def test_chain_plans_are_on_unless_the_mode_file_switches_them_off(tmp_path, defaults):
    etc = tmp_path / "etc.json"
    assert pipeline_config.load(str(etc), defaults).chain is True
    etc.write_text(json.dumps({"mode": "enforce", "since": ""}), encoding="utf-8")
    assert pipeline_config.load(str(etc), defaults).chain is True
    etc.write_text(json.dumps({"mode": "enforce", "since": "", "chain": False}), encoding="utf-8")
    assert pipeline_config.load(str(etc), defaults).chain is False
    etc.write_text(json.dumps({"mode": "enforce", "since": "", "chain": "no"}), encoding="utf-8")
    with pytest.raises(ValueError, match="chain"):
        pipeline_config.load(str(etc), defaults)


def test_a_mode_switch_keeps_the_chain_switch(tmp_path, defaults):
    etc = tmp_path / "etc.json"
    etc.write_text(json.dumps({"mode": "observe", "since": "", "chain": False}), encoding="utf-8")
    pipeline_config.write("enforce", now="2026-11-19T10:00:00Z", etc=str(etc))
    config = pipeline_config.load(str(etc), defaults)
    assert (config.mode, config.chain) == ("enforce", False)


def test_pipeline_config_enforce_from_etc(tmp_path, defaults):
    etc = tmp_path / "etc.json"
    etc.write_text(json.dumps({"mode": "enforce", "since": "2026-10-20T12:00:00Z"}), encoding="utf-8")
    config = pipeline_config.load(str(etc), defaults)
    assert config.enforcing and config.since == "2026-10-20T12:00:00Z"
    etc.write_text(json.dumps({"mode": "turbo", "since": ""}), encoding="utf-8")
    with pytest.raises(ValueError):
        pipeline_config.load(str(etc), defaults)


def test_cached_keeps_the_last_good_config(tmp_path, defaults):
    etc = tmp_path / "etc.json"
    etc.write_text(json.dumps({"mode": "enforce", "since": "2026-10-20T12:00:00Z"}), encoding="utf-8")
    cached = pipeline_config.Cached(str(etc), defaults)
    assert cached.get().enforcing
    etc.write_text("{broken", encoding="utf-8")
    assert cached.get().enforcing
    etc.unlink()
    assert cached.get().mode == "observe"


def test_pipeline_config_write_is_atomic(tmp_path, defaults):
    etc = tmp_path / "etc.json"
    pipeline_config.write("enforce", now="2026-10-20T12:00:00Z", etc=str(etc))
    assert json.loads(etc.read_text(encoding="utf-8")) == {"mode": "enforce", "since": "2026-10-20T12:00:00Z"}
    assert pipeline_config.load(str(etc), defaults).enforcing
    with pytest.raises(ValueError):
        pipeline_config.write("turbo", now="2026-10-20T12:00:00Z", etc=str(etc))
    assert pipeline_config.load(str(etc), defaults).enforcing
