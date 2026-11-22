import json
import os

import pytest
from kd import session_config


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_missing_file_hosts_nobody(tmp_path):
    config = session_config.load(str(tmp_path / "none.json"))
    assert config.agents == {} and config.idle_s == 600
    assert not config.hosted("analyst") and config.slots("analyst") == 0


def test_reads_agents_and_idle(tmp_path):
    path = _write(
        tmp_path / "c.json",
        {"schema_version": 1, "idle_minutes": 5, "agents": {"analyst": {"slots": 1}, "dev": {"slots": 2}}},
    )
    config = session_config.load(path)
    assert config.idle_s == 300 and config.hosted("dev") and config.slots("dev") == 2
    assert not config.hosted("boss")


@pytest.mark.parametrize(
    "data",
    [
        {"schema_version": 2, "agents": {}},
        {"schema_version": 1, "agents": {"Bad Name": {"slots": 1}}},
        {"schema_version": 1, "agents": {"dev": {"slots": 0}}},
        {"schema_version": 1, "agents": {"dev": {"slots": 10}}},
        {"schema_version": 1, "agents": {"dev": {"slots": True}}},
        {"schema_version": 1, "agents": {"dev": {"slots": 1, "x": 1}}},
        {"schema_version": 1, "idle_minutes": 0, "agents": {}},
        {"schema_version": 1, "idle_minutes": 121, "agents": {}},
        {"schema_version": 1, "agents": {}, "extra": 1},
        {"schema_version": 1, "agents": []},
        [],
    ],
)
def test_rejects_bad_name_slots_idle_and_extra_keys(tmp_path, data):
    with pytest.raises(ValueError):
        session_config.load(_write(tmp_path / "c.json", data))


def test_rejects_invalid_json(tmp_path):
    path = tmp_path / "c.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(ValueError):
        session_config.load(str(path))


def test_cached_rereads_on_mtime_and_keeps_last_good(tmp_path, capsys):
    path = tmp_path / "c.json"
    _write(path, {"schema_version": 1, "agents": {"analyst": {"slots": 1}}})
    cached = session_config.Cached(str(path))
    assert cached.get().hosted("analyst")
    _write(path, {"schema_version": 1, "agents": {"dev": {"slots": 1}}})
    os.utime(path, (1, 2_000_000_000))
    assert cached.get().hosted("dev") and not cached.get().hosted("analyst")
    path.write_text("{", encoding="utf-8")
    os.utime(path, (1, 2_000_000_100))
    assert cached.get().hosted("dev")  # the last good one stays
    assert "session-host" in capsys.readouterr().err
    os.remove(path)
    assert cached.get().agents == {}  # no file: nobody is hosted


def test_cli_init_add_remove_keeps_the_file_valid(tmp_path, capsys):
    """session_host_setup.sh edits the config only through this: never a broken file, never a lost key."""
    import json

    path = tmp_path / "session-host.json"
    assert session_config.main(["init"], path=str(path)) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == {"schema_version": 1, "agents": {}}
    path.write_text('{"schema_version": 1, "idle_minutes": 5, "agents": {"dev": {"slots": 2}}}', encoding="utf-8")
    assert session_config.main(["init"], path=str(path)) == 0  # an existing file stays as it is
    assert session_config.main(["add", "analyst"], path=str(path)) == 0
    assert session_config.main(["add", "analyst"], path=str(path)) == 0  # already listed: unchanged
    config = session_config.load(str(path))
    assert config.agents == {"dev": 2, "analyst": 1} and config.idle_s == 300
    assert session_config.main(["remove", "dev"], path=str(path)) == 0
    assert session_config.load(str(path)).agents == {"analyst": 1}
    assert session_config.main(["list"], path=str(path)) == 0 and capsys.readouterr().out == "analyst 1\n"
    if os.name == "posix":
        assert oct(path.stat().st_mode & 0o777) == "0o644"


def test_cli_refuses_bad_names_and_broken_files(tmp_path, capsys):
    path = tmp_path / "session-host.json"
    assert session_config.main(["add", "../x"], path=str(path)) == 2
    assert session_config.main(["add", "analyst"], path=str(path)) == 1  # no file: install first
    path.write_text("{", encoding="utf-8")
    assert session_config.main(["add", "analyst"], path=str(path)) == 1
    assert path.read_text(encoding="utf-8") == "{"
    assert session_config.main(["bogus"], path=str(path)) == 2
    assert "session-host" in capsys.readouterr().err


def test_add_with_slots_sets_and_updates(tmp_path, capsys):
    """dev on two slots — `add <agent> <slots>` lists it with them or changes a listed agent's."""
    path = tmp_path / "session-host.json"
    assert session_config.main(["init"], path=str(path)) == 0
    assert session_config.main(["add", "dev", "2"], path=str(path)) == 0
    assert session_config.load(str(path)).agents == {"dev": 2}
    assert session_config.main(["add", "dev", "1"], path=str(path)) == 0
    assert session_config.load(str(path)).agents == {"dev": 1}
    assert session_config.main(["add", "dev"], path=str(path)) == 0  # no number: a listed agent keeps its slots
    assert session_config.main(["add", "dev", "3"], path=str(path)) == 0
    assert session_config.main(["add", "dev"], path=str(path)) == 0
    assert session_config.load(str(path)).agents == {"dev": 3}
    for bad in ("0", "10", "x", "-1"):
        assert session_config.main(["add", "dev", bad], path=str(path)) == 2
    assert session_config.load(str(path)).agents == {"dev": 3}
