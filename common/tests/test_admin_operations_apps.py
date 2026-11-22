"""the catalog's apps — one entry per project environment — and the plan verbs app.tool, app.flag and
service.reload. An app step carries its resolved argv in the plan the owner signs."""

import copy
import sys
from pathlib import Path

import pytest

from kd import admin_operations as ops

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples
from admin_samples import CATALOG, ENABLED, INVENTORY, explain, proposal  # noqa: E402

APP = {
    "server": "node-c",
    "user": "www-data",
    "dir": "/var/www/app.example.test",
    "python": "./venv/bin/python",
    "tools": ["tools/partner_admin.py"],
    "flags": {"file": "config.ini", "sections": ["partner"]},
}
PREFIX = ["runuser", "-u", "www-data", "--", "env", "-C", "/var/www/app.example.test"]


def data(**app_over):
    out = copy.deepcopy(CATALOG)
    out["verbs"] = out["verbs"] + ["script", "service.reload", "app.tool", "app.flag"]
    out["apps"] = {"app-staging": {**copy.deepcopy(APP), **app_over}}
    return out


def catalog(**app_over):
    return ops.Catalog.from_dict(data(**app_over))


def tool_step(**over):
    step = {
        "server": "node-c",
        "verb": "app.tool",
        "app": "app-staging",
        "tool": "tools/partner_admin.py",
        "args": ["set-type", "--partner-id", "6", "--type", "sales", "--force"],
    }
    return explain({**step, **over}, what="Сменить тип учётки")


def flag_step(**over):
    step = {
        "server": "node-c",
        "verb": "app.flag",
        "app": "app-staging",
        "section": "partner",
        "key": "clients_enabled",
        "value": "1",
    }
    return explain({**step, **over}, what="Включить методы клиентов")


def check(step, cat=None):
    return ops.validate_operation(step, cat or catalog(), INVENTORY, ENABLED)


def test_catalog_reads_an_app_entry():
    entry = catalog().apps["app-staging"]
    assert (entry.server, entry.user, entry.dir, entry.python) == (
        "node-c",
        "www-data",
        "/var/www/app.example.test",
        "./venv/bin/python",
    )
    assert entry.tools == frozenset({"tools/partner_admin.py"})
    assert (entry.flag_file, entry.flag_sections) == (
        "config.ini",
        frozenset({"partner"}),
    )
    assert ops.Catalog.from_dict(copy.deepcopy(CATALOG)).apps == {}


def test_an_app_entry_may_have_no_flags():
    entry = dict(APP)
    del entry["flags"]
    raw = data()
    raw["apps"]["app-staging"] = entry
    seen = ops.Catalog.from_dict(raw).apps["app-staging"]
    assert (seen.flag_file, seen.flag_sections) == ("", frozenset())
    with pytest.raises(ops.ProposalError, match="no switches"):
        check(flag_step(), ops.Catalog.from_dict(raw))


@pytest.mark.parametrize(
    "over, why",
    [
        ({"user": "root"}, "user"),
        ({"user": "www data"}, "user"),
        ({"server": "node-x"}, "server"),
        ({"dir": "var/www/app"}, "dir"),
        ({"dir": "/var/www/../etc"}, "dir"),
        ({"dir": "/var/www/app; id"}, "dir"),
        ({"python": "../python"}, "python"),
        ({"python": "python -I"}, "python"),
        ({"tools": ["tools/../x.py"]}, "tools"),
        ({"tools": "tools/x.py"}, "tools"),
        ({"tools": ["-rf"]}, "tools"),
        ({"flags": {"file": "etc/config.ini", "sections": ["partner"]}}, "flags"),
        ({"flags": {"file": "config.ini", "sections": ["Partner"]}}, "flags"),
        ({"flags": {"file": "config.ini"}}, "flags"),
        ({"shell": True}, "keys"),
        # the final review: an entry may not reach beyond the application — no system interpreter by its path, no
        # tool that is not a script of the tree, no system or platform directory, no platform account
        ({"python": "/usr/bin/env"}, "python"),
        ({"python": "/var/www/app.example.test/venv/bin/python"}, "python"),
        ({"python": "env"}, "python"),
        ({"python": "bash"}, "python"),
        ({"tools": ["/usr/bin/id"]}, "tools"),
        ({"tools": ["tools/run.sh"]}, "tools"),
        ({"tools": ["bash"]}, "tools"),
        ({"dir": "/etc"}, "dir"),
        ({"dir": "/etc/nginx"}, "dir"),
        ({"dir": "/var/www"}, "dir"),
        ({"dir": "/home/someone/app"}, "dir"),
        ({"dir": "/srv/digipaddock-agents/dev"}, "dir"),
        ({"dir": "/opt/digipaddock"}, "dir"),
        ({"dir": "/opt/digipaddock-releases/current"}, "dir"),
        ({"user": "agent-ro"}, "user"),
    ],
)
def test_a_malformed_app_entry_is_refused(over, why):
    with pytest.raises(ops.CatalogError, match=why):
        catalog(**over)


def test_an_app_name_is_a_plain_slug():
    raw = data()
    raw["apps"]["App Staging"] = raw["apps"].pop("app-staging")
    with pytest.raises(ops.CatalogError, match="app name"):
        ops.Catalog.from_dict(raw)


def test_app_tool_forms_the_argv_and_keeps_it_in_the_plan():
    cat = catalog()
    op = check(tool_step(), cat)
    argv = PREFIX + [
        "./venv/bin/python",
        "tools/partner_admin.py",
        "set-type",
        "--partner-id",
        "6",
        "--type",
        "sales",
        "--force",
    ]
    assert ops.remote_argv(op, cat) == argv
    assert ops.remote_command(op, cat) == (
        "runuser -u www-data -- env -C /var/www/app.example.test ./venv/bin/python "
        "tools/partner_admin.py set-type --partner-id 6 --type sales --force"
    )
    stored = op.as_dict()
    assert (
        stored["argv"] == argv
        and stored["app"] == "app-staging"
        and stored["args"][0] == "set-type"
    )
    assert check(stored, cat) == op  # the stored plan re-validates to the same step


def test_app_tool_may_have_no_arguments():
    op = check(tool_step(args=[]))
    assert list(op.argv) == PREFIX + ["./venv/bin/python", "tools/partner_admin.py"]


@pytest.mark.parametrize(
    "arg",
    [
        "a b",
        "x;y",
        "$(id)",
        "`id`",
        "a'b",
        'a"b',
        "",
        "-" * 201,
        "a\nb",
        "é",
        "a|b",
        "a>b",
        "a&b",
        "*",
        "~root",
        "a\\b",
    ],
)
def test_app_tool_refuses_an_argument_outside_the_charset(arg):
    with pytest.raises(ops.ProposalError, match="args"):
        check(tool_step(args=["show", arg]))


@pytest.mark.parametrize("args", ["show", [1], [["show"]], ["x"] * 41, None])
def test_app_tool_arguments_are_a_bounded_list_of_strings(args):
    with pytest.raises(ops.ProposalError, match="args"):
        check(tool_step(args=args))


def test_app_tool_refuses_a_tool_outside_the_entry_and_an_unknown_app():
    with pytest.raises(ops.ProposalError, match="tool"):
        check(tool_step(tool="tools/other.py"))
    with pytest.raises(ops.ProposalError, match="app-prod"):
        check(tool_step(app="app-prod"))


def test_an_app_step_runs_on_the_server_of_its_entry_only():
    with pytest.raises(ops.ProposalError, match="node-c"):
        check(tool_step(server="node-g"))
    with pytest.raises(ops.ProposalError, match="node-c"):
        check(flag_step(server="node-g"))


def test_a_stored_argv_that_no_longer_matches_the_catalog_is_refused():
    stored = check(tool_step()).as_dict()
    with pytest.raises(ops.ProposalError, match="catalog entry changed"):
        check(stored, catalog(dir="/var/www/other.example.test"))
    forged = dict(stored, argv=["runuser", "-u", "root", "--", "id"])
    with pytest.raises(ops.ProposalError, match="catalog entry changed"):
        check(forged)


def test_app_flag_forms_the_helper_command():
    cat = catalog()
    op = check(flag_step(), cat)
    assert ops.remote_argv(op, cat) == PREFIX + [
        "python3",
        "-I",
        ops.FLAG_HELPER,
        "config.ini",
        "partner",
        "clients_enabled",
        "1",
    ]
    assert (
        ops.FLAG_HELPER == "/usr/local/libexec/agent-app-flag"
    )  # not under the policy-guarded agent-* files
    assert check(op.as_dict(), cat) == op
    assert check(flag_step(value="0"), cat).value == "0"


@pytest.mark.parametrize(
    "over, why",
    [
        ({"key": "webhook_sink_token"}, "key"),
        ({"key": "Clients_enabled"}, "key"),
        ({"key": "_enabled"}, "key"),
        ({"value": "2"}, "value"),
        ({"value": 1}, "value"),
        ({"value": "true"}, "value"),
        ({"section": "db"}, "section"),
        ({"args": []}, "unexpected keys"),
    ],
)
def test_app_flag_takes_only_switches_of_its_sections(over, why):
    with pytest.raises(ops.ProposalError, match=why):
        check(flag_step(**over))


def test_service_reload_is_a_catalog_unit_step():
    cat = catalog()
    op = check(
        explain(
            {"server": "node-c", "verb": "service.reload", "unit": "nginx.service"}
        ),
        cat,
    )
    assert ops.remote_argv(op, cat) == ["systemctl", "reload", "nginx.service"]
    assert op.as_dict()["unit"] == "nginx.service" and "argv" not in op.as_dict()
    with pytest.raises(ops.ProposalError, match="ordinary unit"):
        check(
            explain(
                {"server": "node-c", "verb": "service.reload", "unit": "mysql.service"}
            ),
            cat,
        )


def test_the_new_verbs_must_be_enabled_in_the_catalog():
    raw = data()
    raw["verbs"] = [v for v in raw["verbs"] if not v.startswith("app.")]
    with pytest.raises(ops.ProposalError, match="not enabled"):
        check(tool_step(), ops.Catalog.from_dict(raw))


def test_app_steps_pass_on_the_factorys_own_host_and_change_the_hash():
    plan = proposal(
        operations=[
            tool_step(),
            flag_step(),
            explain(
                {"server": "node-c", "verb": "service.reload", "unit": "nginx.service"}
            ),
        ]
    )
    seen = ops.validate_proposal(
        plan, catalog(), INVENTORY, ENABLED, card_servers=("node-c",), home="node-c"
    )
    assert [o.verb for o in seen.operations] == [
        "app.tool",
        "app.flag",
        "service.reload",
    ]
    other = ops.validate_proposal(
        proposal(operations=[tool_step(args=["show", "--partner-id", "6"])]),
        catalog(),
        INVENTORY,
        ENABLED,
        card_servers=("node-c",),
        home="node-c",
    )
    assert seen.sha256 != other.sha256
    again = ops.validate_proposal(
        seen.as_dict(), catalog(), INVENTORY, ENABLED, card_servers=("node-c",)
    )
    assert again.sha256 == seen.sha256


def test_mechanics_reads_in_russian_for_the_new_verbs():
    cat = catalog()
    assert ops.mechanics(check(tool_step(), cat)) == (
        "node-c: app-staging — tools/partner_admin.py set-type --partner-id 6 --type sales --force"
    )
    assert (
        ops.mechanics(check(flag_step(), cat))
        == "node-c: app-staging — выключатель [partner] clients_enabled = 1"
    )
    reload = check(
        explain(
            {"server": "node-c", "verb": "service.reload", "unit": "nginx.service"}
        ),
        cat,
    )
    assert ops.mechanics(reload) == "node-c: перечитать конфигурацию nginx.service"


def test_the_example_catalog_shows_an_app_entry():
    cat = ops.load_catalog(
        Path(ops.__file__).resolve().parents[2]
        / "agents"
        / "sysadmin"
        / "operations.example.json"
    )
    assert {"service.reload", "app.tool", "app.flag"} <= cat.verbs
    assert cat.apps and all(entry.server in cat.servers for entry in cat.apps.values())


def test_an_app_entry_never_runs_as_an_account_of_the_platform():
    from kd import naming

    for user in (
        naming.base_user(),
        naming.service_user("cabinet"),
        naming.service_user("exec"),
        naming.agent_user("sysadmin"),
    ):
        with pytest.raises(ops.CatalogError, match="user"):
            catalog(user=user)


@pytest.mark.parametrize(
    "python", ["python3", "python3.12", "venv/bin/python", "./venv/bin/python3"]
)
def test_an_app_interpreter_is_of_the_tree_or_the_systems_python3(python):
    assert catalog(python=python).apps["app-staging"].python == python


@pytest.mark.parametrize(
    "where", ["/var/www/app.example.test", "/opt/app", "/srv/app/current"]
)
def test_an_app_tree_lives_under_the_application_roots(where):
    assert catalog(dir=where).apps["app-staging"].dir == where


def test_option_like_arguments_stay_behind_the_tool():
    op = check(tool_step(args=["--", "-u", "root", "-C", "/etc"]))
    assert list(op.argv)[:9] == PREFIX + ["./venv/bin/python", "tools/partner_admin.py"]
    assert list(op.argv)[9:] == ["--", "-u", "root", "-C", "/etc"]


# --- spark commands of a PHP application and enumerated .env switches -----------------------------------
PHP_APP = {
    "server": "node-c",
    "user": "www-data",
    "dir": "/var/www/shop.example.test",
    "php": "php",
    "spark": ["key:generate", "migrate", "migrate:status"],
    "flags": {
        "file": ".env",
        "format": "env",
        "keys": {"CI_ENVIRONMENT": ["development", "production"]},
    },
}
PHP_PREFIX = ["runuser", "-u", "www-data", "--", "env", "-C", "/var/www/shop.example.test"]


def php_data(**over):
    out = data()
    entry = {**copy.deepcopy(PHP_APP), **over}
    out["apps"]["shop-test"] = {k: v for k, v in entry.items() if v is not None}
    return out


def php_catalog(**over):
    return ops.Catalog.from_dict(php_data(**over))


def spark_step(**over):
    step = {
        "server": "node-c",
        "verb": "app.tool",
        "app": "shop-test",
        "tool": "spark",
        "args": ["key:generate", "--force"],
    }
    return explain({**step, **over}, what="Сделать ключ шифрования")


def env_step(**over):
    step = {
        "server": "node-c",
        "verb": "app.flag",
        "app": "shop-test",
        "key": "CI_ENVIRONMENT",
        "value": "development",
    }
    return explain({**step, **over}, what="Режим разработки")


def test_an_app_entry_may_be_a_php_application_alone():
    entry = php_catalog().apps["shop-test"]
    assert (entry.php, entry.spark) == ("php", frozenset({"key:generate", "migrate", "migrate:status"}))
    assert (entry.python, entry.tools) == ("", frozenset())
    assert (entry.flag_file, entry.flag_format) == (".env", "env")
    assert entry.flag_values("CI_ENVIRONMENT") == frozenset({"development", "production"})
    assert entry.flag_values("app.baseURL") is None
    assert catalog().apps["app-staging"].flag_format == "ini"


def test_an_app_entry_may_hold_python_tools_and_spark_commands():
    entry = php_catalog(python="./venv/bin/python", tools=["tools/seed.py"]).apps["shop-test"]
    assert (entry.python, entry.php) == ("./venv/bin/python", "php")


@pytest.mark.parametrize(
    "over, why",
    [
        ({"php": None, "spark": None}, "python and tools, or php and spark"),
        ({"spark": None}, "php and spark"),
        ({"php": None}, "php and spark"),
        ({"php": "/usr/bin/php"}, "php must be"),
        ({"php": "php8.3"}, None),
        ({"php": "bash"}, "php must be"),
        ({"spark": ["Key:Generate"]}, "spark must be"),
        ({"spark": ["migrate --force"]}, "spark must be"),
        ({"spark": "migrate"}, "spark must be"),
        ({"flags": {"file": ".env", "format": "env", "keys": {"CI_ENVIRONMENT": ["a b"]}}}, "flags must be"),
        ({"flags": {"file": ".env", "format": "env", "keys": {"CI ENVIRONMENT": ["a"]}}}, "flags must be"),
        ({"flags": {"file": ".env", "format": "env", "keys": {"CI_ENVIRONMENT": []}}}, "flags must be"),
        ({"flags": {"file": "..", "format": "env", "keys": {"A": ["1"]}}}, "flags must be"),
        ({"flags": {"file": "sub/.env", "format": "env", "keys": {"A": ["1"]}}}, "flags must be"),
        ({"flags": {"file": ".env", "format": "yaml", "keys": {"A": ["1"]}}}, "flags must be"),
        ({"flags": {"file": ".env", "format": "env", "sections": ["partner"]}}, "flags must be"),
    ],
)
def test_a_php_entry_is_checked_whole(over, why):
    if why is None:
        assert php_catalog(**over).apps["shop-test"].php == over["php"]
        return
    with pytest.raises(ops.CatalogError, match=why):
        php_catalog(**over)


def test_an_ini_entry_keeps_its_file_name_rule():
    with pytest.raises(ops.CatalogError, match="flags must be"):
        catalog(flags={"file": ".env", "sections": ["partner"]})  # a dot file is an env file only


def test_a_spark_step_runs_php_spark_with_a_listed_command():
    op = check(spark_step(), php_catalog())
    assert list(op.argv) == PHP_PREFIX + ["php", "spark", "key:generate", "--force"]
    assert op.as_dict()["argv"] == list(op.argv)
    assert ops.mechanics(op) == "node-c: shop-test — spark key:generate --force"


@pytest.mark.parametrize("args", [["db:wipe"], [], ["--force", "migrate"], ["migrate;id"]])
def test_a_spark_step_names_a_command_of_the_entry_first(args):
    with pytest.raises(ops.ProposalError):
        check(spark_step(args=args), php_catalog())


def test_spark_is_no_tool_of_an_entry_without_php():
    with pytest.raises(ops.ProposalError, match="not a tool"):
        check(tool_step(tool="spark", args=["migrate"]))
    with pytest.raises(ops.ProposalError, match="not a tool"):
        check(spark_step(tool="tools/partner_admin.py"), php_catalog())


def test_an_env_switch_forms_the_helper_command_without_a_section():
    op = check(env_step(), php_catalog())
    assert list(op.argv) == PHP_PREFIX + [
        "python3", "-I", ops.FLAG_HELPER, "--env", ".env", "CI_ENVIRONMENT", "development"]
    stored = op.as_dict()
    assert "section" not in stored and stored["key"] == "CI_ENVIRONMENT"
    assert check(stored, php_catalog()) == op  # the stored step reads back as itself
    assert ops.mechanics(op) == "node-c: shop-test — переключатель .env CI_ENVIRONMENT = development"


@pytest.mark.parametrize(
    "over, why",
    [
        ({"key": "encryption.key"}, "not a switch of this environment"),
        ({"value": "staging"}, "not a value"),
        ({"value": 1}, "not a value"),
        ({"section": "partner"}, "no section"),
    ],
)
def test_an_env_switch_takes_only_listed_keys_and_values(over, why):
    with pytest.raises(ops.ProposalError, match=why):
        check(env_step(**over), php_catalog())


def test_an_ini_switch_still_needs_its_section():
    step = flag_step()
    del step["section"]
    with pytest.raises(ops.ProposalError, match="section"):
        check(step)


def test_the_hash_of_an_ini_plan_did_not_change():
    plan = ops.validate_proposal(
        proposal(operations=[flag_step(), tool_step()]), catalog(), INVENTORY, ENABLED, card_servers=["node-c"])
    assert plan.sha256 == "8854ea74d1ed3b49b339ca653ff64b8c16f218a2d25fdada67ebff046b124afc"


# --- the test zone: an entry marked test, its reload units, the zone's predicates -------------------------
def zone_catalog(protected=(), **over):
    out = php_data(test=True, **over)
    out["apps"]["app-staging"].update(test=True, reload=["nginx.service"])
    return ops.Catalog.from_dict(out, protected)


def reload_step(unit="nginx.service", verb="service.reload"):
    return explain({"server": "node-c", "verb": verb, "unit": unit})


def test_an_entry_may_be_marked_test_and_name_units_to_reload():
    cat = zone_catalog()
    assert cat.apps["shop-test"].test and cat.apps["shop-test"].reload == frozenset()
    assert cat.apps["app-staging"].test and cat.apps["app-staging"].reload == {"nginx.service"}
    plain = catalog().apps["app-staging"]
    assert plain.test is False and plain.reload == frozenset()


@pytest.mark.parametrize(
    "over, why",
    [
        ({"test": "yes"}, "test"),
        ({"test": 1}, "test"),
        ({"test": True, "reload": ["mysql.service"]}, "ordinary unit"),
        ({"test": True, "reload": "nginx.service"}, "reload"),
        ({"test": True, "reload": ["nginx.service; id"]}, "reload"),
        ({"reload": ["nginx.service"]}, "test"),
        ({"test": False, "reload": ["nginx.service"]}, "test"),
    ],
)
def test_a_malformed_test_mark_is_refused(over, why):
    with pytest.raises(ops.CatalogError, match=why):
        catalog(**over)


def test_a_test_environment_never_lives_on_a_protected_server():
    with pytest.raises(ops.CatalogError, match="node-c.*production"):
        zone_catalog(protected=("node-c",))
    assert zone_catalog(protected=("example-prod",)).apps["shop-test"].test
    # an entry without the mark is no business of the protection
    assert ops.Catalog.from_dict(data(), ("node-c",)).apps["app-staging"].server == "node-c"


def _catalog_file(tmp_path, content):
    path = tmp_path / "operations.json"
    path.write_text(__import__("json").dumps(content), encoding="utf-8")
    return path


def test_load_catalog_protects_the_hosts_production_server(tmp_path, monkeypatch):
    content = php_data(test=True)
    monkeypatch.setattr(ops.naming, "load_host", lambda: {"prod_alias": "node-c"})
    with pytest.raises(ops.CatalogError, match="production"):
        ops.load_catalog(_catalog_file(tmp_path, content))
    monkeypatch.setattr(ops.naming, "load_host", lambda: {"prod_alias": "example-prod"})
    assert ops.load_catalog(_catalog_file(tmp_path, content)).apps["shop-test"].test


def test_load_catalog_with_a_test_entry_needs_to_know_production(tmp_path, monkeypatch):
    def unknown():
        raise ops.naming.NamingError("host.json: unreadable")

    monkeypatch.setattr(ops.naming, "load_host", unknown)
    with pytest.raises(ops.CatalogError, match="production"):
        ops.load_catalog(_catalog_file(tmp_path, php_data(test=True)))
    # a catalog without test environments never asks
    assert "shop-test" in ops.load_catalog(_catalog_file(tmp_path, php_data())).apps


def test_test_env_of_names_the_environment_of_a_step():
    cat = zone_catalog()
    assert ops.test_env_of(check(spark_step(), cat), cat) == "shop-test"
    assert ops.test_env_of(check(env_step(), cat), cat) == "shop-test"
    assert ops.test_env_of(check(tool_step(), cat), cat) == "app-staging"
    assert ops.test_env_of(check(flag_step(), cat), cat) == "app-staging"
    assert ops.test_env_of(check(reload_step(), cat), cat) == "app-staging"


def test_steps_outside_a_test_environment_have_none():
    cat = zone_catalog()
    assert ops.test_env_of(check(reload_step(verb="service.restart"), cat), cat) is None
    assert ops.test_env_of(check(reload_step("app-worker@alpha.service"), cat), cat) is None  # not in any reload
    assert ops.test_env_of(check(explain({"server": "node-c", "verb": "package.refresh"}), cat), cat) is None
    unmarked = php_catalog()
    assert ops.test_env_of(check(spark_step(), unmarked), unmarked) is None
    assert ops.test_env_of(check(reload_step(), unmarked), unmarked) is None
    # the mark was taken off after the step was validated: the step is no longer of the zone
    assert ops.test_env_of(check(spark_step(), cat), unmarked) is None


def _proposal(cat, *steps):
    return ops.validate_proposal(
        proposal(operations=list(steps)), cat, INVENTORY, ENABLED, card_servers=["node-c"]
    )


def test_the_zone_is_a_plan_whose_every_step_is_of_a_test_environment():
    cat = zone_catalog()
    assert ops.test_zone(_proposal(cat, spark_step(), reload_step(), tool_step()), cat) == ("app-staging", "shop-test")
    assert ops.test_zone(_proposal(cat, env_step()), cat) == ("shop-test",)
    assert ops.test_zone(_proposal(cat, spark_step(), reload_step(verb="service.restart")), cat) == ()
    assert ops.test_zone(_proposal(cat, explain({"server": "node-c", "verb": "package.refresh"})), cat) == ()


def test_test_envs_is_the_list_a_server_keeps():
    cat = zone_catalog()
    assert ops.test_envs(cat, "node-c") == {
        "schema_version": 1,
        "envs": {
            "app-staging": {
                "user": "www-data",
                "dir": "/var/www/app.example.test",
                "python": "./venv/bin/python",
                "tools": ["tools/partner_admin.py"],
                "flags": {"file": "config.ini", "format": "ini", "sections": ["partner"]},
                "reload": ["nginx.service"],
            },
            "shop-test": {
                "user": "www-data",
                "dir": "/var/www/shop.example.test",
                "php": "php",
                "spark": ["key:generate", "migrate", "migrate:status"],
                "flags": {"file": ".env", "format": "env", "keys": {"CI_ENVIRONMENT": ["development", "production"]}},
            },
        },
    }
    assert ops.test_envs(cat, "node-g") == {"schema_version": 1, "envs": {}}
    assert ops.test_envs(php_catalog(), "node-c") == {"schema_version": 1, "envs": {}}


def test_the_mark_does_not_change_a_plans_hash():
    step = spark_step()
    assert _proposal(zone_catalog(), step).sha256 == _proposal(php_catalog(), step).sha256


def test_a_test_entry_may_name_directories_the_agents_read():
    """``read`` — the test environment's directories opened to the read-only gate's account whole."""
    out = php_data(test=True)
    out["apps"]["app-staging"].update(test=True, read=["logs/api_journal", "logs"])
    cat = ops.Catalog.from_dict(out)
    assert cat.apps["app-staging"].read == {"logs/api_journal", "logs"}
    assert cat.apps["shop-test"].read == frozenset()
    assert ops.test_envs(cat, "node-c")["envs"]["app-staging"]["read"] == ["logs", "logs/api_journal"]
    assert "read" not in ops.test_envs(cat, "node-c")["envs"]["shop-test"]


@pytest.mark.parametrize(
    "over, why",
    [
        ({"read": ["logs"]}, "not marked test"),
        ({"test": True, "read": "logs"}, "read"),
        ({"test": True, "read": ["/var/log"]}, "read"),
        ({"test": True, "read": ["../etc"]}, "read"),
        ({"test": True, "read": ["logs/.."]}, "read"),
        ({"test": True, "read": [".git"]}, "read"),
        ({"test": True, "read": ["d%d" % i for i in range(11)]}, "read"),
    ],
)
def test_a_malformed_read_list_is_refused(over, why):
    with pytest.raises(ops.CatalogError, match=why):
        catalog(**over)
