"""the read-only gate reads a SQLite file itself — one SELECT, read-only by SQLite's own permissions."""

import io
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from gate import agent_ro_gate as gate

DB = "/var/www/shop.example.test/writable/shop.sqlite"


def _address_space():
    try:
        import resource
    except ImportError:
        return None
    return resource.getrlimit(resource.RLIMIT_AS)


@pytest.fixture(autouse=True)
def no_cap_on_the_test_process(monkeypatch):
    """the gate's cap is its process's for good — in a pytest worker it stays for every later test, and past
    the worker's address space no thread starts (the CI contour hung at its limit). Only a child takes the real one."""
    before = _address_space()
    monkeypatch.setattr(gate, "_cap_memory", lambda: None)
    yield
    assert _address_space() == before, "a test capped the pytest process's address space"


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "shop.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(
        "CREATE TABLE settings (name TEXT, value TEXT, raw BLOB);"
        "INSERT INTO settings VALUES ('mode', 'whitelabel', NULL);"
        "INSERT INTO settings VALUES ('logo', 'a\tb\nc', x'00ff10');"
        "CREATE TABLE numbers (n INTEGER);"
    )
    conn.executemany("INSERT INTO numbers VALUES (?)", [(i,) for i in range(2000)])
    conn.commit()
    conn.close()
    return path


def text(result):
    return result.output.decode("utf-8")


# --- the command's form -------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT name, value FROM settings WHERE name = 'mode'",
        "select count(*) from numbers where n > 5 and n < 9",
        "EXPLAIN QUERY PLAN SELECT * FROM settings",
        "WITH t AS (SELECT 1 AS x) SELECT x FROM t",
        "SELECT name, sql FROM sqlite_master",
    ],
)
def test_one_reading_statement_over_a_file_of_the_read_roots_passes(sql):
    plan = gate.check('sqlite3 %s "%s"' % (DB, sql))
    assert plan == gate.Plan(["sqlite3", DB, sql], False)


@pytest.mark.parametrize(
    "raw, why",
    [
        ('sqlite3 %s "INSERT INTO settings VALUES (1, 2, 3)"' % DB, "только SELECT"),
        ('sqlite3 %s "PRAGMA journal_mode=wal"' % DB, "только SELECT"),
        ("sqlite3 %s \"ATTACH DATABASE '/tmp/x' AS x\"" % DB, "только SELECT"),
        ('sqlite3 %s ".dump"' % DB, "только SELECT"),
        ('sqlite3 %s "SELECT 1; DROP TABLE settings"' % DB, "метасимволы"),
        ("sqlite3 %s" % DB, "файл и один запрос"),
        ('sqlite3 %s "SELECT 1" "SELECT 2"' % DB, "файл и один запрос"),
        ('sqlite3 -readonly %s "SELECT 1"' % DB, "файл и один запрос"),
        ('sqlite3 -readonly "SELECT 1"', "абсолютный путь"),
        ('sqlite3 /tmp/shop.sqlite "SELECT 1"', "вне разрешённых"),
        ('sqlite3 /var/www/../etc/x.sqlite "SELECT 1"', "'..'"),
        ('sqlite3 /var/www/shop.example.test/.env "SELECT 1"', "путь запрещён"),
        ('sqlite3 /backups/shop.sqlite "SELECT 1"', "только листинг"),
    ],
)
def test_anything_else_is_refused_before_the_file_is_opened(raw, why):
    with pytest.raises(gate.Deny, match=why):
        gate.check(raw)


def test_the_data_key_does_not_read_sqlite():
    with pytest.raises(gate.Deny, match="только к mysql"):
        gate.check_data('sqlite3 %s "SELECT 1"' % DB)


# --- the read itself ----------------------------------------------------------------------------------------------
def test_a_select_prints_a_header_and_tab_separated_rows(db):
    result = gate.run_sqlite(
        str(db), "SELECT name, value FROM settings WHERE name = 'mode'"
    )
    assert (result.returncode, result.truncated, result.timed_out) == (0, False, False)
    assert text(result) == "name\tvalue\nmode\twhitelabel\n"


def test_null_blob_and_control_characters_stay_one_cell_each(db):
    result = gate.run_sqlite(str(db), "SELECT value, raw FROM settings ORDER BY name")
    assert text(result) == "value\traw\na\\tb\\nc\tx'00ff10'\nwhitelabel\tNULL\n"


def test_the_tables_are_listed_by_an_ordinary_query(db):
    result = gate.run_sqlite(
        str(db), "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    )
    assert text(result) == "name\nnumbers\nsettings\n"


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO settings VALUES ('x', 'y', NULL)",
        "UPDATE settings SET value = 'x'",
        "DELETE FROM numbers",
        "DROP TABLE numbers",
        "CREATE TABLE t (x)",
        "ATTACH DATABASE ':memory:' AS other",
        "PRAGMA journal_mode = wal",
        "WITH t AS (SELECT 1) INSERT INTO numbers SELECT * FROM t",
        "SELECT load_extension('x')",
        "SELECT 1; DELETE FROM numbers",
        "VACUUM",
    ],
)
def test_sqlite_itself_refuses_everything_but_a_read(db, sql):
    before = db.read_bytes()
    result = gate.run_sqlite(str(db), sql)
    assert result.returncode == 1 and text(result).startswith("sqlite3: ")
    assert db.read_bytes() == before


def test_a_query_that_never_ends_is_stopped(db):
    endless = "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM c) SELECT count(*) FROM c"
    result = gate.run_sqlite(str(db), endless, timeout=0.2)
    assert result.timed_out and result.returncode == 1


def test_a_long_result_is_cut_at_the_limit_on_a_whole_row(db):
    result = gate.run_sqlite(str(db), "SELECT n FROM numbers ORDER BY n", limit=1024)
    assert result.truncated and len(result.output) <= 1024
    assert text(result).endswith("\n") and text(result).startswith("n\n0\n1\n")


def test_a_file_that_is_not_a_database_or_is_missing_names_the_reason(tmp_path):
    (tmp_path / "notes.sqlite").write_bytes(
        b"just text, long enough to not look like an empty database file\n" * 4
    )
    result = gate.run_sqlite(str(tmp_path / "notes.sqlite"), "SELECT 1 FROM x")
    assert result.returncode == 1 and text(result).startswith("sqlite3: ")
    missing = gate.run_sqlite(str(tmp_path / "none.sqlite"), "SELECT 1")
    assert missing.returncode == 1 and "нет" in text(missing)
    assert not (tmp_path / "none.sqlite").exists()  # a read never creates the file


# --- through the gate's main ---------------------------------------------------------------------------------------
def run_main(monkeypatch, db, sql):
    monkeypatch.setattr(
        gate, "check_path", lambda path, need_content: None
    )  # tmp_path is outside the read roots
    out, err = io.BytesIO(), io.BytesIO()
    command = 'sqlite3 %s "%s"' % (db.as_posix(), sql)
    code = gate.main([], {"SSH_ORIGINAL_COMMAND": command}, out, err)
    return code, out.getvalue().decode("utf-8"), err.getvalue().decode("utf-8")


def test_main_reads_without_an_sqlite3_binary(monkeypatch, db):
    monkeypatch.setattr(gate.shutil, "which", lambda *a, **k: None)
    code, out, err = run_main(
        monkeypatch, db, "SELECT name FROM settings ORDER BY name"
    )
    assert (code, out, err) == (0, "name\nlogo\nmode\n", "")


def test_main_masks_a_secret_in_a_cell(monkeypatch, db):
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO settings VALUES ('api', 'password=hunter2hunter2', NULL)")
    conn.commit()
    conn.close()
    code, out, _ = run_main(
        monkeypatch, db, "SELECT value FROM settings WHERE name = 'api'"
    )
    assert code == 0 and "hunter2" not in out and "скрыто секретов" in out


def test_main_reports_a_refused_statement_with_its_code(monkeypatch, db):
    monkeypatch.setitem(
        gate.VALIDATORS, "sqlite3", list
    )  # past the text check: SQLite's own refusal
    code, out, _ = run_main(monkeypatch, db, "DELETE FROM numbers")
    assert code == 1 and out.startswith("sqlite3: ")


# --- a result too large for the gate --------------------------------------------------------------
def test_a_row_past_the_limit_is_cut_before_it_is_formatted(db, monkeypatch):
    conn = sqlite3.connect(str(db))
    conn.execute("INSERT INTO settings VALUES ('big', 'x', zeroblob(5000))")
    conn.commit()
    conn.close()
    cell = gate._sqlite_cell

    def small_only(value):
        assert not isinstance(value, (bytes, str)) or len(value) <= 1000, (
            "a cell past the limit was formatted"
        )
        return cell(value)

    monkeypatch.setattr(gate, "_sqlite_cell", small_only)
    result = gate.run_sqlite(
        str(db), "SELECT name, raw FROM settings ORDER BY name = 'big'", limit=1000
    )
    assert result.truncated and result.returncode == 0
    assert text(result) == "name\traw\nmode\tNULL\nlogo\tx'00ff10'\n"


def test_memory_running_out_is_a_refusal_not_a_crash(db, monkeypatch):
    def exhausted(value):
        raise MemoryError()

    monkeypatch.setattr(gate, "_sqlite_cell", exhausted)
    result = gate.run_sqlite(str(db), "SELECT name FROM settings")
    assert (
        result.returncode == 1
        and text(result) == "sqlite3: результат слишком велик для шлюза\n"
    )


def test_main_caps_the_memory_of_a_sqlite_read(monkeypatch, db):
    capped = []
    monkeypatch.setattr(gate, "_cap_memory", lambda: capped.append(True))
    code, out, _ = run_main(monkeypatch, db, "SELECT count(*) AS n FROM numbers")
    assert (code, out) == (0, "n\n2000\n") and capped == [True]


@pytest.mark.skipif(sys.platform == "win32", reason="an address-space limit is POSIX")
def test_the_memory_cap_stops_one_huge_value(db):
    # in a child: the limit is the process's for good
    script = (
        "import sys; sys.path.insert(0, %r)\n"
        "from gate import agent_ro_gate as gate\n"
        "gate._cap_memory()\n"
        "r = gate.run_sqlite(%r, 'SELECT hex(zeroblob(900000000))')\n"
        "sys.stdout.write(r.output.decode('utf-8')); sys.exit(r.returncode)\n"
    ) % (str(Path(gate.__file__).resolve().parents[1]), str(db))
    done = subprocess.run(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    assert done.returncode == 1 and done.stdout.decode("utf-8").startswith(
        "sqlite3: "
    ), done.stderr
