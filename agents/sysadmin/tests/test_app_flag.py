"""agent-app-flag — the helper an ``app.flag`` plan step runs as the application's own account in the
application's directory. It sets one ``*_enabled`` switch of one section of an ini file and touches nothing else."""

import io
import os

import pytest

from gate import agent_app_flag as flag

INI = b"""[db]
host = localhost
clients_enabled = 0

[partner]
; clients_enabled = 1
api_enabled = 1
webhook_url = https://hooks.example.test/in

[internal_api]
token_ttl = 60
"""


def run(tmp_path, *argv, today="2026-11-16"):
    out, err = io.StringIO(), io.StringIO()
    rc = flag.main(list(argv), cwd=str(tmp_path), today=today, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def ini(tmp_path, content=INI, name="config.ini"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_sets_a_missing_key_in_its_section_only(tmp_path):
    path = ini(tmp_path)
    rc, out, err = run(tmp_path, "config.ini", "partner", "clients_enabled", "1")
    assert (rc, err) == (0, "")
    assert out == "[partner] clients_enabled: нет -> 1\n"
    assert path.read_bytes() == INI.replace(
        b"webhook_url = https://hooks.example.test/in\n",
        b"webhook_url = https://hooks.example.test/in\nclients_enabled = 1\n",
    )


def test_sets_a_key_in_the_last_section_of_a_file_without_a_final_newline(tmp_path):
    path = ini(tmp_path, INI.rstrip(b"\n"))
    rc, out, _ = run(tmp_path, "config.ini", "internal_api", "probe_enabled", "1")
    assert rc == 0
    assert path.read_bytes() == INI.rstrip(b"\n") + b"\nprobe_enabled = 1\n"


def test_replaces_the_line_of_that_section_and_not_of_another(tmp_path):
    path = ini(tmp_path, INI.replace(b"api_enabled = 1", b"api_enabled=yes  "))
    rc, out, _ = run(tmp_path, "config.ini", "partner", "api_enabled", "0")
    assert (rc, out) == (0, "[partner] api_enabled: yes -> 0\n")
    assert path.read_bytes() == INI.replace(b"api_enabled = 1", b"api_enabled = 0")
    # the same key of another section keeps its line
    rc, out, _ = run(tmp_path, "config.ini", "db", "clients_enabled", "1")
    assert (rc, out) == (0, "[db] clients_enabled: 0 -> 1\n")
    assert b"; clients_enabled = 1\napi_enabled = 0" in path.read_bytes()


def test_a_duplicate_key_is_refused(tmp_path):
    twice = INI.replace(b"api_enabled = 1\n", b"api_enabled = 1\napi_enabled = 0\n")
    path = ini(tmp_path, twice)
    rc, out, err = run(tmp_path, "config.ini", "partner", "api_enabled", "1")
    assert (rc, out) == (2, "") and "дважды" in err
    assert path.read_bytes() == twice and os.listdir(str(tmp_path)) == ["config.ini"]


def test_a_missing_or_repeated_section_is_refused(tmp_path):
    path = ini(tmp_path)
    rc, _, err = run(tmp_path, "config.ini", "billing", "api_enabled", "1")
    assert rc == 2 and "billing" in err
    path.write_bytes(INI + b"[partner]\nx = 1\n")
    rc, _, err = run(tmp_path, "config.ini", "partner", "api_enabled", "0")
    assert rc == 2 and "дважды" in err
    assert os.listdir(str(tmp_path)) == ["config.ini"]


def test_keeps_the_first_backup_of_the_day(tmp_path):
    path = ini(tmp_path)
    assert run(tmp_path, "config.ini", "partner", "api_enabled", "0")[0] == 0
    backup = tmp_path / "config.ini.bak-2026-11-16"
    assert backup.read_bytes() == INI
    assert run(tmp_path, "config.ini", "partner", "clients_enabled", "1")[0] == 0
    assert (
        backup.read_bytes() == INI
    )  # the day's first state, not the state before the second step
    assert sorted(os.listdir(str(tmp_path))) == [
        "config.ini",
        "config.ini.bak-2026-11-16",
    ]
    assert (
        run(tmp_path, "config.ini", "partner", "api_enabled", "1", today="2026-11-17")[
            0
        ]
        == 0
    )
    assert (
        tmp_path / "config.ini.bak-2026-11-17"
    ).read_bytes() == path.read_bytes().replace(b"api_enabled = 1", b"api_enabled = 0")


@pytest.mark.parametrize(
    "argv",
    [
        ["../config.ini", "partner", "api_enabled", "1"],
        ["sub/config.ini", "partner", "api_enabled", "1"],
        ["..", "partner", "api_enabled", "1"],
        ["missing.ini", "partner", "api_enabled", "1"],
        ["config.ini", "partner", "webhook_url", "1"],
        ["config.ini", "partner", "Api_enabled", "1"],
        ["config.ini", "partner", "_enabled", "1"],
        ["config.ini", "Partner", "api_enabled", "1"],
        ["config.ini", "partner]", "api_enabled", "1"],
        ["config.ini", "partner", "api_enabled", "2"],
        ["config.ini", "partner", "api_enabled", "true"],
        ["config.ini", "partner", "api_enabled"],
        ["config.ini", "partner", "api_enabled", "1", "extra"],
        [],
    ],
)
def test_refuses_a_path_a_key_that_is_not_a_switch_and_a_value_that_is_not_a_bit(
    tmp_path, argv
):
    path = ini(tmp_path)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "config.ini").write_bytes(INI)
    rc, out, err = run(tmp_path, *argv)
    assert (rc, out) == (2, "") and err.startswith("agent-app-flag: ")
    assert (
        path.read_bytes() == INI
        and (tmp_path / "sub" / "config.ini").read_bytes() == INI
    )
    assert sorted(os.listdir(str(tmp_path))) == ["config.ini", "sub"]


def test_refuses_a_symlink_and_a_directory(tmp_path):
    target = ini(tmp_path, name="real.ini")
    (tmp_path / "dir.ini").mkdir()
    rc, _, err = run(tmp_path, "dir.ini", "partner", "api_enabled", "0")
    assert rc == 2 and "обычный файл" in err
    try:
        os.symlink(str(target), str(tmp_path / "config.ini"))
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks for this account")
    rc, _, err = run(tmp_path, "config.ini", "partner", "api_enabled", "0")
    assert rc == 2 and "обычный файл" in err
    assert target.read_bytes() == INI


def test_a_leftover_new_file_stops_the_write(tmp_path):
    path = ini(tmp_path)
    (tmp_path / "config.ini.new").write_bytes(b"someone else's draft")
    rc, _, err = run(tmp_path, "config.ini", "partner", "api_enabled", "0")
    assert rc == 2 and "config.ini.new" in err
    assert (
        path.read_bytes() == INI
        and (tmp_path / "config.ini.new").read_bytes() == b"someone else's draft"
    )
    assert sorted(os.listdir(str(tmp_path))) == [
        "config.ini",
        "config.ini.new",
    ]  # no backup of a step that did not run


def test_same_value_writes_nothing(tmp_path):
    path = ini(tmp_path)
    before = path.stat().st_mtime_ns
    rc, out, err = run(tmp_path, "config.ini", "partner", "api_enabled", "1")
    assert (rc, err) == (0, "")
    assert out == "[partner] api_enabled: 1 -> 1, без изменений\n"
    assert path.stat().st_mtime_ns == before and os.listdir(str(tmp_path)) == [
        "config.ini"
    ]


def test_crlf_file_keeps_its_line_endings(tmp_path):
    crlf = INI.replace(b"\n", b"\r\n")
    path = ini(tmp_path, crlf)
    assert run(tmp_path, "config.ini", "partner", "api_enabled", "0")[0] == 0
    assert run(tmp_path, "config.ini", "partner", "clients_enabled", "1")[0] == 0
    assert path.read_bytes() == crlf.replace(
        b"api_enabled = 1", b"api_enabled = 0"
    ).replace(b"/in\r\n", b"/in\r\nclients_enabled = 1\r\n")


@pytest.mark.parametrize(
    "body",
    [
        # configparser reads an indented line as a key or as the value above it, by the indent around it:
        # the helper does not guess — a second line of the key would stop the application at its next start
        INI.replace(b"api_enabled = 1\n", b"note = see\n  api_enabled = 1\n"),
        INI.replace(b"api_enabled = 1\n", b"    api_enabled = 1\n"),
        INI.replace(b"api_enabled = 1\n", b"\tApi_Enabled: 1\n"),
    ],
)
def test_an_indented_line_of_the_key_is_refused(tmp_path, body):
    path = ini(tmp_path, body)
    rc, out, err = run(tmp_path, "config.ini", "partner", "api_enabled", "0")
    assert (rc, out) == (2, "") and "с отступом" in err
    assert path.read_bytes() == body and os.listdir(str(tmp_path)) == ["config.ini"]


def test_a_key_is_found_whatever_its_case_as_configparser_reads_it(tmp_path):
    body = INI.replace(b"api_enabled = 1", b"Api_Enabled = 1")
    path = ini(tmp_path, body)
    rc, out, _ = run(tmp_path, "config.ini", "partner", "api_enabled", "0")
    assert (rc, out) == (0, "[partner] api_enabled: 1 -> 0\n")
    assert path.read_bytes() == INI.replace(
        b"api_enabled = 1", b"api_enabled = 0"
    )  # one line, not two
    twice = INI.replace(b"api_enabled = 1\n", b"api_enabled = 1\nAPI_ENABLED = 0\n")
    path.write_bytes(twice)
    rc, _, err = run(
        tmp_path, "config.ini", "partner", "api_enabled", "1", today="2026-11-18"
    )
    assert rc == 2 and "дважды" in err and path.read_bytes() == twice


def test_a_section_name_is_exact_as_configparser_reads_it(tmp_path):
    # configparser names «[ partner ]» with its spaces: it is another section, not [partner]
    body = INI.replace(b"[partner]", b"[ partner ]")
    path = ini(tmp_path, body)
    rc, _, err = run(tmp_path, "config.ini", "partner", "api_enabled", "0")
    assert rc == 2 and "нет секции" in err and path.read_bytes() == body


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_the_new_file_and_the_backup_keep_the_mode_of_the_original(tmp_path):
    path = ini(tmp_path)
    path.chmod(0o640)
    assert run(tmp_path, "config.ini", "partner", "api_enabled", "0")[0] == 0
    assert path.stat().st_mode & 0o777 == 0o640
    assert (tmp_path / "config.ini.bak-2026-11-16").stat().st_mode & 0o777 == 0o640


def test_runs_on_python_36_syntax():
    import ast

    source = open(flag.__file__, encoding="utf-8").read()
    ast.parse(source, feature_version=(3, 6))
    assert source.startswith("#!")


# --- the env mode — agent-app-flag --env <file> <key> <value> ---------------------------------------------
ENV = b"""#--------------------------------------------------------------------
# ENVIRONMENT
#--------------------------------------------------------------------
CI_ENVIRONMENT = production

app.baseURL = 'https://shop.example.test/'
# appmode.mode = legacy
encryption.key = hex2bin:0123456789abcdef
"""


def env(tmp_path, content=ENV, name=".env"):
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_env_replaces_the_line_of_the_key_and_no_other_byte(tmp_path):
    path = env(tmp_path)
    rc, out, err = run(tmp_path, "--env", ".env", "CI_ENVIRONMENT", "development")
    assert (rc, err) == (0, "")
    assert out == ".env CI_ENVIRONMENT: production -> development\n"
    assert path.read_bytes() == ENV.replace(b"CI_ENVIRONMENT = production", b"CI_ENVIRONMENT = development")
    assert (tmp_path / ".env.bak-2026-11-16").read_bytes() == ENV
    assert not (tmp_path / ".env.new").exists()


def test_env_keeps_crlf_line_ends(tmp_path):
    content = ENV.replace(b"\n", b"\r\n")
    path = env(tmp_path, content)
    rc, _, _ = run(tmp_path, "--env", ".env", "CI_ENVIRONMENT", "development")
    assert rc == 0
    assert path.read_bytes() == content.replace(b"CI_ENVIRONMENT = production", b"CI_ENVIRONMENT = development")


def test_env_appends_a_missing_key_after_a_file_without_a_final_newline(tmp_path):
    path = env(tmp_path, ENV.rstrip(b"\n"))
    rc, out, _ = run(tmp_path, "--env", ".env", "appmode.mode", "whitelabel")
    assert rc == 0 and out == ".env appmode.mode: нет -> whitelabel\n"
    assert path.read_bytes() == ENV + b"appmode.mode = whitelabel\n"


def test_env_appends_with_the_line_end_of_the_file(tmp_path):
    content = ENV.replace(b"\n", b"\r\n")
    path = env(tmp_path, content)
    assert run(tmp_path, "--env", ".env", "appmode.mode", "whitelabel")[0] == 0
    assert path.read_bytes() == content + b"appmode.mode = whitelabel\r\n"


def test_env_sets_a_key_in_an_empty_file(tmp_path):
    path = env(tmp_path, b"")
    assert run(tmp_path, "--env", ".env", "CI_ENVIRONMENT", "development")[0] == 0
    assert path.read_bytes() == b"CI_ENVIRONMENT = development\n"


def test_env_a_commented_line_is_not_the_key(tmp_path):
    path = env(tmp_path)
    rc, out, _ = run(tmp_path, "--env", ".env", "appmode.mode", "whitelabel")
    assert rc == 0 and "нет -> whitelabel" in out
    assert b"# appmode.mode = legacy\n" in path.read_bytes()
    assert path.read_bytes().endswith(b"appmode.mode = whitelabel\n")


@pytest.mark.parametrize(
    "line",
    [b"export CI_ENVIRONMENT=production", b'CI_ENVIRONMENT = "production"', b"  CI_ENVIRONMENT='production'  ",
     b"CI_ENVIRONMENT=production"],
)
def test_env_finds_the_key_in_the_forms_a_dotenv_reader_takes(tmp_path, line):
    path = env(tmp_path, b"A = 1\n" + line + b"\nB = 2\n")
    rc, out, _ = run(tmp_path, "--env", ".env", "CI_ENVIRONMENT", "development")
    assert rc == 0 and out == ".env CI_ENVIRONMENT: production -> development\n"
    assert path.read_bytes() == b"A = 1\nCI_ENVIRONMENT = development\nB = 2\n"


def test_env_a_key_is_exact_not_a_prefix_or_another_case(tmp_path):
    path = env(tmp_path, b"CI_ENVIRONMENT_OLD = x\nci_environment = y\n")
    rc, out, _ = run(tmp_path, "--env", ".env", "CI_ENVIRONMENT", "development")
    assert rc == 0 and "нет -> development" in out
    assert path.read_bytes() == b"CI_ENVIRONMENT_OLD = x\nci_environment = y\nCI_ENVIRONMENT = development\n"


def test_env_a_key_written_twice_is_refused(tmp_path):
    content = b"CI_ENVIRONMENT = production\nCI_ENVIRONMENT = development\n"
    path = env(tmp_path, content)
    rc, _, err = run(tmp_path, "--env", ".env", "CI_ENVIRONMENT", "development")
    assert rc == 2 and "дважды" in err
    assert path.read_bytes() == content


def test_env_never_prints_an_old_value_that_is_not_a_plain_word(tmp_path):
    env(tmp_path)
    rc, out, _ = run(tmp_path, "--env", ".env", "encryption.key", "off")
    assert rc == 0 and out == ".env encryption.key: другое -> off\n"
    assert "0123456789abcdef" not in out


def test_env_same_value_writes_nothing(tmp_path):
    path = env(tmp_path)
    rc, out, _ = run(tmp_path, "--env", ".env", "CI_ENVIRONMENT", "production")
    assert rc == 0 and "без изменений" in out
    assert path.read_bytes() == ENV
    assert not (tmp_path / ".env.bak-2026-11-16").exists()


@pytest.mark.parametrize(
    "argv, why",
    [
        (["--env", "../.env", "A", "1"], "не путь"),
        (["--env", "..", "A", "1"], "не путь"),
        (["--env", "sub/.env", "A", "1"], "не путь"),
        (["--env", ".env", "A B", "1"], "ключ"),
        (["--env", ".env", "9A", "1"], "ключ"),
        (["--env", ".env", "A", "a b"], "значение"),
        (["--env", ".env", "A", "a;b"], "значение"),
        (["--env", ".env", "A", ""], "значение"),
        (["--env", ".env", "A"], "нужны"),
        (["--env", ".env", "A", "1", "2"], "нужны"),
    ],
)
def test_env_refuses_a_path_an_odd_key_and_a_value_that_is_not_a_plain_word(tmp_path, argv, why):
    path = env(tmp_path)
    rc, _, err = run(tmp_path, *argv)
    assert rc == 2 and why in err
    assert path.read_bytes() == ENV


def test_env_refuses_a_missing_file_and_a_symlink(tmp_path):
    rc, _, err = run(tmp_path, "--env", ".env", "A", "1")
    assert rc == 2 and "нет в каталоге" in err
    target = env(tmp_path, name="real.env")
    try:
        os.symlink(str(target), str(tmp_path / ".env"))
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks for this account")
    rc, _, err = run(tmp_path, "--env", ".env", "A", "1")
    assert rc == 2 and "обычный файл" in err
    assert target.read_bytes() == ENV


def test_the_ini_form_does_not_take_a_dot_file(tmp_path):
    env(tmp_path, INI)
    rc, _, err = run(tmp_path, ".env", "partner", "api_enabled", "0")
    assert rc == 2 and "не путь" in err
