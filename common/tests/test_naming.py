"""Host naming: every Linux user name derives from one host prefix."""

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kd import naming
from kd.naming import NamingError

ROOT = Path(__file__).resolve().parents[2]


def _config(tmp_path, data):
    path = tmp_path / "naming.json"
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, "utf-8")
    return str(path)


def test_default_prefix_is_digi_and_the_repo_default_says_so():
    assert naming.load_prefix("/nonexistent/naming.json") == "digi"
    assert json.loads(Path(naming.DEFAULT_CONFIG).read_text("utf-8")) == {
        "schema_version": 1,
        "prefix": "digi",
        "commands": "kd",
        "cards": "KD",
    }


def test_host_file_wins_over_the_default(tmp_path):
    path = _config(tmp_path, {"schema_version": 1, "prefix": "acme"})
    assert naming.load_prefix(path) == "acme"


@pytest.mark.parametrize(
    "data",
    [
        {"schema_version": 1, "prefix": "Kit"},
        {"schema_version": 1, "prefix": "k"},
        {"schema_version": 1, "prefix": "digi-x"},
        {"schema_version": 1, "prefix": "toolongpx"},
        {"schema_version": 2, "prefix": "digi"},
        {"schema_version": True, "prefix": "digi"},
        {"schema_version": 1, "prefix": "digi", "extra": 1},
        {"prefix": "digi"},
        ["digi"],
        "{broken",
    ],
)
def test_a_bad_host_file_is_refused_not_defaulted(tmp_path, data):
    with pytest.raises(NamingError):
        naming.load_prefix(_config(tmp_path, data))


def test_names_of_every_role():
    assert naming.base_user("digi") == "digi"
    assert naming.agent_user("dev", "digi") == "digi-dev"
    assert naming.service_user("exec", "digi") == "digi-exec"
    assert naming.service_users("acme") == frozenset({"acme-exec", "acme-forge", "acme-repo", "acme-cabinet", "acme-push", "acme-review", "acme-transcribe", "acme-observe", "acme-data", "acme-ci"})
    assert naming.service_user("data", "digi") == "digi-data"  # the one holder of the data key
    assert naming.render("User=@data@", "digi") == "User=digi-data"
    for typo in ("@datax@", "@Data@"):
        with pytest.raises(NamingError):
            naming.render(typo, "digi")
    assert naming.service_user("transcribe", "digi") == "digi-transcribe"  # the voice transcription
    assert naming.render("User=@transcribe@", "digi") == "User=digi-transcribe"
    for typo in ("@transcribex@", "@Transcribe@", "@transcribe/"):
        with pytest.raises(NamingError):
            naming.render(typo, "digi")
    assert naming.service_user("observe", "digi") == "digi-observe"  # the host reader of kd-host-read
    assert naming.render("User=@observe@", "digi") == "User=digi-observe"
    for typo in ("@observex@", "@Observe@"):
        with pytest.raises(NamingError):
            naming.render(typo, "digi")
    assert naming.service_user("review", "digi") == "digi-review"  # the approver of dev's PRs
    assert naming.render("User=@review@", "digi") == "User=digi-review"
    for typo in ("@reviewx@", "@Review@", "@review/"):
        with pytest.raises(NamingError):
            naming.render(typo, "digi")
    assert naming.service_user("push", "digi") == "digi-push"  # the Web Push sender of the cabinet (global review 15.10.2026)
    assert naming.service_user("repo", "digi") == "digi-repo"  # the checkout refresher of repo.read
    assert naming.home("digi-dev") == "/home/digi-dev"
    assert naming.agent_user("sysadmin") == f"{naming.PREFIX}-sysadmin"


@pytest.mark.parametrize(
    "name",
    ["exec", "forge", "repo", "ro", "worker", "Dev", "a" * 24, "", "dev_x", "-dev", None],
)
def test_reserved_or_malformed_agent_names_get_no_user(name):
    with pytest.raises(NamingError):
        naming.agent_user(name, "digi")


def test_unknown_service_role_is_refused():
    with pytest.raises(NamingError):
        naming.service_user("root", "digi")


def test_user_re_accepts_only_this_prefix():
    pattern = naming.user_re("digi")
    assert all(pattern.fullmatch(u) for u in ("digi", "digi-dev", "digi-exec", "digi-a-b"))
    assert not any(
        pattern.fullmatch(u)
        for u in (
            "agent",
            "agent-dev",
            "digi-",
            "kitx",
            "digi-Dev",
            "root",
            "digi-" + "a" * 24,
        )
    )


def test_render_replaces_every_token():
    text = "User=@base@\nexec=/home/@exec@/x forge=@forge@ dev=//home/@agent:dev@/.ssh"
    assert naming.render(text, "digi") == (
        "User=digi\nexec=/home/digi-exec/x forge=digi-forge dev=//home/digi-dev/.ssh"
    )


@pytest.mark.parametrize(
    "text",
    [
        "@agnet:dev@",
        "@agent:Dev@",
        "@agent:exec@",
        "@agent:@",
        "@EXEC@",
        "@BASE@",
        "@Forge@",
        "@forgee@",
        "@repos@",
        "@repo",
        "@agent@",
        # unclosed: a floor rule with one of these would silently stop denying
        "@exec",
        "//home/@base/.ssh",
        '"//home/@agent:dev/.ssh"',
        "@forge.",
        "@Exec x",
    ],
)
def test_render_refuses_a_broken_token(text):
    with pytest.raises(NamingError):
        naming.render("x " + text, "digi")


def test_render_leaves_plain_at_signs_alone():
    text = (
        "@example_dev_bot old-dev@example.test app-agent@%i.service @dev @dev@ a@b"
        " git@forge.example ops@base.example @baseline"
    )
    assert naming.render(text, "digi") == text


def test_cli_prints_users_and_renders(tmp_path):
    src = tmp_path / "unit.service"
    src.write_text("User=@base@\n", "utf-8")
    script = str(ROOT / "common" / "kd" / "naming.py")

    def run(*args):
        return subprocess.run(
            [sys.executable, "-I", script, *args], capture_output=True, text=True
        )

    assert run("user", "agent", "dev").stdout == f"{naming.PREFIX}-dev\n"
    assert run("user", "forge").stdout == f"{naming.PREFIX}-forge\n"
    assert run("user", "base").stdout == f"{naming.PREFIX}\n"
    assert run("render", str(src)).stdout == f"User={naming.PREFIX}\n"
    assert run("user", "agent", "exec").returncode != 0
    assert run("user", "root").returncode != 0


@pytest.mark.skipif(
    os.name == "nt" or not os.path.exists("/usr/bin/python3"), reason="POSIX shell"
)
def test_shell_helpers(tmp_path):
    lib = ROOT / "common" / "install" / "lib" / "naming.sh"
    out = subprocess.run(
        ["bash", "-c", f'. "{lib}"; kd_user base; kd_user agent dev; kd_user exec'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert out == [naming.PREFIX, f"{naming.PREFIX}-dev", f"{naming.PREFIX}-exec"]
    bad = subprocess.run(
        ["bash", "-c", f'. "{lib}"; kd_user agent forge'],
        capture_output=True,
        text=True,
    )
    assert bad.returncode != 0
    src = tmp_path / "u.service"
    src.write_text("User=@base@\n", "utf-8")
    rendered = subprocess.run(
        ["bash", "-c", f'. "{lib}"; kd_render "{src}"'],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert rendered == f"User={naming.PREFIX}\n"


def test_repository_json_resolves_naming_tokens(tmp_path):
    from kd.registry import RegistryError, read_json_file

    good = tmp_path / "a.json"
    good.write_text('{"run_as": "@forge@", "path": "/home/@base@/x"}', "utf-8")
    assert read_json_file(good) == {
        "run_as": f"{naming.PREFIX}-forge",
        "path": f"/home/{naming.PREFIX}/x",
    }
    bad = tmp_path / "b.json"
    bad.write_text('{"run_as": "@forgee@"}', "utf-8")
    with pytest.raises(RegistryError, match="naming token"):
        read_json_file(bad)


def test_no_manifest_names_its_user():
    for path in sorted((ROOT / "agents").glob("*/agent.json")):
        data = json.loads(path.read_text("utf-8"))
        assert "user" not in data and data["schema_version"] == 2, path


def test_repo_token_renders_and_loose_forms_are_refused():
    assert naming.render("User=@repo@\nGroup=@repo@\n", "digi") == "User=digi-repo\nGroup=digi-repo\n"
    for text in ("@Repo@", "@repo", "@repos@"):
        with pytest.raises(NamingError):
            naming.render(text, "digi")


def test_cabinet_service_role(monkeypatch):
    assert naming.service_user("cabinet", "digi") == "digi-cabinet"
    assert "cabinet" in naming.RESERVED_AGENTS
    assert naming.render("User=@cabinet@\n", "digi") == "User=digi-cabinet\n"
    with pytest.raises(naming.NamingError):
        naming.agent_user("cabinet", "digi")


def test_host_token_renders_from_host_json():
    assert naming.render("ssh @host:stage_alias@ @base@") == "ssh example-stage digi"


def test_unknown_host_key_fails_closed():
    with pytest.raises(naming.NamingError):
        naming.render("@host:nope@")


def test_load_host_missing_key_fails(tmp_path):
    bad = tmp_path / "host.json"
    bad.write_text('{"schema_version": 1, "stage_alias": "x"}')
    with pytest.raises(naming.NamingError):
        naming.load_host(str(bad))


def test_load_host_wrong_type_or_schema_fails(tmp_path):
    good = json.loads((ROOT / "common/tests/fixtures/host/host.json").read_text("utf-8"))
    for broken in ({**good, "stage_ip": 1}, {**good, "schema_version": 2}, [good]):
        path = tmp_path / "host.json"
        path.write_text(json.dumps(broken))
        with pytest.raises(naming.NamingError):
            naming.load_host(str(path))
    with pytest.raises(naming.NamingError):
        naming.load_host(str(tmp_path / "absent.json"))


@pytest.mark.parametrize("key, value", [
    ("stage_alias", "stage\nExecStart=/bin/sh"), ("stage_alias", "a b"), ("prod_alias", "-oProxyCommand=x"),
    ("stage_ip", "203.0.113"), ("forge_host", "forge.example.test/x"), ("cabinet_host", "cab;rm"),
    ("forge_ssh_url", "ssh://git@forge.example.test/x.git"), ("forge_ssh_url", "https://forge/{repo}.git"),
    ("forge_api", "http://forge.example.test/api/v1"), ("cabinet_origin", "https://cabinet.example.test/x y"),
    ("docs_dir", "relative/docs"), ("docs_dir", "/srv/../etc"), ("docs_dir", "/srv/do cs"),
    ("mirror_remote", "git@github.example.test:x/y.git\n"), ("forge_owner", "a/b"), ("dev_email", "no-at-sign"),
])
def test_load_host_refuses_a_value_of_the_wrong_shape(tmp_path, key, value):
    """Review M7: host values go into units, nginx, shell and texts — each key has its shape, one line."""
    good = json.loads((ROOT / "common/tests/fixtures/host/host.json").read_text("utf-8"))
    path = tmp_path / "host.json"
    path.write_text(json.dumps({**good, key: value}))
    with pytest.raises(naming.NamingError, match=key):
        naming.load_host(str(path))


def test_load_host_takes_the_example_and_the_fixture():
    for path in (ROOT / "host/host.example.json", ROOT / "common/tests/fixtures/host/host.json"):
        assert naming.load_host(str(path))["schema_version"] == 1


def test_host_json_follows_env(monkeypatch, tmp_path):
    data = json.loads((ROOT / "common/tests/fixtures/host/host.json").read_text("utf-8"))
    data["stage_alias"] = "other-stage"
    path = tmp_path / "host.json"
    path.write_text(json.dumps(data))
    monkeypatch.setenv("KD_HOST_JSON", str(path))
    assert naming.render("@host:stage_alias@") == "other-stage"


@pytest.mark.parametrize("remote, host", [
    ("git@github.example.test:example/digipaddock.git", "github.example.test"),
    ("ssh://git@mirror.example.test:2222/example/digipaddock.git", "mirror.example.test"),
    ("ssh://git@mirror.example.test/example/digipaddock.git", "mirror.example.test"),
])
def test_mirror_host_derives_from_mirror_remote(tmp_path, remote, host):
    """the sandbox domain of git.mirror follows mirror_remote; a mirror_host in host.json is ignored."""
    good = json.loads((ROOT / "common/tests/fixtures/host/host.json").read_text("utf-8"))
    path = tmp_path / "host.json"
    path.write_text(json.dumps({**good, "mirror_remote": remote, "mirror_host": "elsewhere.example.test"}))
    loaded = naming.load_host(str(path))
    assert loaded["mirror_host"] == host
    assert naming.render("@host:mirror_host@", host=loaded) == host
    assert naming.render("@host:mirror_host@", host={"mirror_remote": remote}) == host


def test_mirror_host_without_its_source_fails_closed():
    with pytest.raises(naming.NamingError):
        naming.render("@host:mirror_host@", host={"mirror_remote": "git@github.example.test:x/y.git\n"})
    with pytest.raises(naming.NamingError):
        naming.render("@host:mirror_host@", host={})


def test_explicit_host_mapping_wins():
    assert naming.render("@host:cabinet_host@", host={"cabinet_host": "x.example.test"}) == "x.example.test"


def test_host_cli_prints_value():
    out = subprocess.run([sys.executable, "-I", str(ROOT / "common/kd/naming.py"), "host", "stage_ip"],
                         capture_output=True, text=True,
                         env={**os.environ, "KD_HOST_JSON": str(ROOT / "common/tests/fixtures/host/host.json")})
    assert (out.returncode, out.stdout) == (0, "203.0.113.10\n")


# --- the host's command and card names, its zone and banks -------------------------------------------


def test_commands_and_cards_default_to_the_repositorys_spelling(tmp_path):
    names = naming.load(_config(tmp_path, {"schema_version": 1, "prefix": "acme"}))
    assert names == {"prefix": "acme", "commands": "kd", "cards": "KD", "card_aliases": ()}


def test_the_host_names_its_commands_and_cards(tmp_path):
    data = {"schema_version": 1, "prefix": "acme", "commands": "ac", "cards": "AC", "card_aliases": ["KD", "OLD"]}
    names = naming.load(_config(tmp_path, data))
    assert (names["commands"], names["cards"], names["card_aliases"]) == ("ac", "AC", ("KD", "OLD"))


@pytest.mark.parametrize("extra", [
    {"commands": "k"}, {"commands": "Kd"}, {"commands": "kd-"}, {"commands": "abcdefghi"}, {"commands": 1},
    {"cards": "kd"}, {"cards": "K"}, {"cards": "ABCDEFG"}, {"cards": "K-D"},
    {"card_aliases": "OLD"}, {"card_aliases": ["OLD", "OLD"]}, {"card_aliases": ["KD"]}, {"card_aliases": ["old"]},
    {"color": "red"},
])
def test_bad_command_or_card_names_are_refused(tmp_path, extra):
    with pytest.raises(NamingError):
        naming.load(_config(tmp_path, {"schema_version": 1, "prefix": "acme", **extra}))


def test_a_command_has_the_hosts_name_and_back():
    assert naming.command("kd-task", "acme") == "acme-task"
    assert naming.command("ssh-ro", "acme") == "ssh-ro"
    assert naming.canonical_command("acme-task", "acme") == "kd-task"
    assert naming.canonical_command("acme-", "acme") == "acme-"
    assert naming.canonical_command("ssh-ro", "acme") == "ssh-ro"
    assert naming.command("kd-task") == naming.canonical_command("kd-task") == "kd-task"  # the fixture keeps kd


def test_rewrite_gives_the_hosts_names_and_keeps_the_trees_files():
    # a card's form (KD-<n>, KD-n, KD-m) is the host's; a numbered card is the platform's own history, not the host's
    text = ("run kd-task view KD-<n>; /usr/local/bin/kd-cap base; common/bin/kd-task, agents/dev/bin/kd-pr and"
            " /opt/digipaddock/common/bin/kd-work are files; (KD-3) KD-n, KD-m skd-x mykd-task XKD-<n> kd-Task KD-name")
    assert naming.rewrite(text, "acme", "AC") == (
        "run acme-task view AC-<n>; /usr/local/bin/acme-cap base; common/bin/kd-task, agents/dev/bin/kd-pr and"
        " /opt/digipaddock/common/bin/kd-work are files; (KD-3) AC-n, AC-m skd-x mykd-task XKD-<n> kd-Task KD-name")
    assert naming.rewrite(text) == text  # a host with the repository's names


def test_a_text_is_measured_as_the_repository_spells_it():
    # a limit on a text is its author's: the host's longer names do not push it over
    source = "run kd-task view KD-<n> by common/bin/kd-task"
    text = naming.rewrite(source, "acme", "ACME")
    assert len(text) == len(source) + 4
    assert naming.repository_length(text, "acme", "ACME") == len(source)
    assert naming.repository_length(source) == len(source)


def test_cli_cmd_prints_the_hosts_name_and_render_rewrites(tmp_path):
    names = _config(tmp_path, {"schema_version": 1, "prefix": "acme", "commands": "ac", "cards": "AC"})
    src = tmp_path / "unit.service"
    src.write_text("ExecStart=/usr/local/bin/kd-alert %i\nUser=@base@\n# KD-1, KD-<n>\n", "utf-8")

    def run(*args):
        return subprocess.run([sys.executable, "-I", str(ROOT / "common/kd/naming.py"), *args], capture_output=True,
                              text=True, env={**os.environ, "KD_NAMING_JSON": names})

    assert run("cmd", "kd-task").stdout == "ac-task\n"
    assert run("names").stdout == "acme ac AC\n"
    assert run("cmd", "ssh-ro").returncode == 1 and run("cmd", "kd-").returncode == 1
    assert run("render", str(src)).stdout == "ExecStart=/usr/local/bin/ac-alert %i\nUser=acme\n# KD-1, AC-<n>\n"


def test_timezone_is_the_hosts_or_utc(tmp_path):
    good = json.loads((ROOT / "common/tests/fixtures/host/host.json").read_text("utf-8"))
    good.pop("timezone", None)
    path = tmp_path / "host.json"
    path.write_text(json.dumps(good))
    assert naming.load_host(str(path))["timezone"] == "UTC"
    assert naming.render("@host:timezone@", host=naming.load_host(str(path))) == "UTC"
    for zone in ("Etc/GMT-3", "America/Argentina/Buenos_Aires", "Etc/GMT+3"):
        path.write_text(json.dumps({**good, "timezone": zone}))
        assert naming.load_host(str(path))["timezone"] == zone
    for zone in ("utc", "Europe", "Etc/GMT-3\nX=1", "../etc/passwd", 3):
        path.write_text(json.dumps({**good, "timezone": zone}))
        with pytest.raises(NamingError, match="timezone"):
            naming.load_host(str(path))


def test_backup_dir_is_the_hosts_or_var_backups(tmp_path):
    """Where the host's own dumps go (the Hindsight database) — a deployment's backup mirror path."""
    good = json.loads((ROOT / "common/tests/fixtures/host/host.json").read_text("utf-8"))
    good.pop("backup_dir", None)
    path = tmp_path / "host.json"
    path.write_text(json.dumps(good))
    assert naming.load_host(str(path))["backup_dir"] == "/var/backups/digipaddock"
    path.write_text(json.dumps({**good, "backup_dir": "/srv/backups/db"}))
    assert naming.render("@host:backup_dir@/hindsight", host=naming.load_host(str(path))) == "/srv/backups/db/hindsight"
    for bad in ("backups", "/srv/../etc", "/srv/b x", "/srv/b\nX=1", 3):
        path.write_text(json.dumps({**good, "backup_dir": bad}))
        with pytest.raises(NamingError, match="backup_dir"):
            naming.load_host(str(path))


def test_memory_banks_are_checked(tmp_path):
    good = json.loads((ROOT / "common/tests/fixtures/host/host.json").read_text("utf-8"))
    path = tmp_path / "host.json"
    path.write_text(json.dumps({**good, "memory_banks": {"dev": "coding-agent::old-dev"}}))
    assert naming.load_host(str(path))["memory_banks"] == {"dev": "coding-agent::old-dev"}
    for banks in ({"dev": "a bank"}, {"Dev": "coding-agent::x"}, ["dev"], {"dev": 1}):
        path.write_text(json.dumps({**good, "memory_banks": banks}))
        with pytest.raises(NamingError, match="memory_banks"):
            naming.load_host(str(path))


def test_token_files_are_the_forge_accounts(monkeypatch, tmp_path):
    host = naming.load_host()
    rx = re.compile(naming.token_files_re())
    for key in naming.FORGE_ACCOUNTS:
        assert rx.fullmatch(host[key] + ".env"), key
    assert not rx.search("other.env") and not rx.search(host["forge_dev_user"] + "xenv")
    monkeypatch.setenv("KD_HOST_JSON", str(tmp_path / "absent.json"))
    assert re.search(naming.token_files_re(), "anything.env")  # a broken host: every token file, closed


# --- small fixes left from the rename ---


@pytest.mark.parametrize("names", [{"prefix": "acme", "commands": "acme"}, {"prefix": "kd", "commands": "acme"}])
def test_load_refuses_commands_equal_to_the_user_prefix(tmp_path, names):
    """Users kd with commands acme: rewrite would turn the homes' deny of kd-dev into acme-dev (10.2)."""
    with pytest.raises(NamingError):
        naming.load(_config(tmp_path, {"schema_version": 1, **names}))


@pytest.mark.parametrize("busy", ["apt", "git", "npm", "pip", "ssh", "systemd", "dpkg"])
def test_load_refuses_a_busy_command_prefix(tmp_path, busy):
    with pytest.raises(NamingError):
        naming.load(_config(tmp_path, {"schema_version": 1, "prefix": "acme", "commands": busy}))


def test_canonical_command_leaves_foreign_tools():
    """Only the repository's own commands come back to kd-<word> (10.3): acme-get is no command of ours."""
    assert naming.canonical_command("acme-task", "acme") == "kd-task"
    assert naming.canonical_command("acme-admin-run", "acme") == "kd-admin-run"  # agents/*/bin too
    assert naming.canonical_command("acme-get", "acme") == "acme-get"
    assert naming.canonical_command("acme-merge-base", "acme") == "acme-merge-base"


def test_rewrite_turns_the_word_placeholders_of_a_card():
    text = "kd-task scope KD-target; KD-review, KD-id: KD-N; KD-name KD-3"
    assert naming.rewrite(text, "acme", "AC") == "acme-task scope AC-target; AC-review, AC-id: AC-N; KD-name KD-3"


def test_rendered_agent_texts_keep_no_repository_card_placeholder():
    """10.1: what an agent reads on a host with its own cards names no KD- placeholder (a numbered card is history)."""
    texts = [*ROOT.glob("agents/*/CLAUDE.md"), *ROOT.glob("agents/*/playbooks/**/*.md"),
             *ROOT.glob("common/agent-template/*.md"), *ROOT.glob("common/specializations/**/*.md")]
    assert texts
    for path in texts:
        rewritten = naming.rewrite(path.read_text(encoding="utf-8"), "acme", "ACME")
        left = re.findall(r"(?<![A-Za-z0-9_])KD-(?![0-9])\S*", rewritten)
        assert not left, (path, left)
