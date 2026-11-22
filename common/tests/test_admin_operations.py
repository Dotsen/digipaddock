"""The finite catalog of ordinary operations and the proposal format."""
import copy
import json
import shlex

import pytest

from kd import admin_operations as ops
from kd import exec_policy

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples
from admin_samples import CATALOG, ENABLED, INVENTORY, explain, proposal  # noqa: E402

def catalog(data=None):
    return ops.Catalog.from_dict(copy.deepcopy(data or CATALOG))


def validate(data, servers=("node-c",)):
    return ops.validate_proposal(data, catalog(), INVENTORY, ENABLED, card_servers=servers)


def test_valid_proposal_is_normalized():
    p = validate(proposal())
    assert p.task_id == "KD-123"
    assert [o.verb for o in p.operations] == ["package.refresh", "service.restart"]
    assert p.servers == ("node-c",)
    assert len(p.sha256) == 64


def test_hash_is_stable_and_covers_every_field():
    base = validate(proposal()).sha256
    assert validate(proposal()).sha256 == base
    assert validate(proposal(intent="другое")).sha256 != base
    assert validate(proposal(basis="другое")).sha256 != base
    for key, value in (("what", "x"), ("effect", "x"), ("rollback", "x"), ("risk", {"level": "high", "why": "x"})):
        changed = proposal()
        changed["operations"][0][key] = value
        assert validate(changed).sha256 != base, key
    changed = proposal()
    changed["operations"][1]["unit"] = "app-worker@alpha.service"
    assert validate(changed).sha256 != base
    swapped = proposal()
    swapped["operations"].reverse()
    assert validate(swapped).sha256 != base  # order is the run order the owner saw


@pytest.mark.parametrize("op,why", [
    ({"server": "node-c", "verb": "package.refresh", "command": "rm -rf /"}, "unexpected"),
    ({"server": "node-c", "verb": "service.restart"}, "unit"),
    ({"server": "node-c", "verb": "service.restart", "unit": "ssh.service"}, "unit"),
    ({"server": "node-c", "verb": "service.restart", "unit": "nginx"}, "unit"),
    ({"server": "node-c", "verb": "service.restart", "unit": "nginx.service; reboot"}, "unit"),
    ({"server": "node-c", "verb": "service.stop", "unit": "nginx.service"}, "verb"),
    ({"server": "node-c", "verb": "shell", "unit": "nginx.service"}, "shell steps are gone"),
    ({"server": "all", "verb": "package.refresh"}, "server"),
    ({"server": "node-c,node-e", "verb": "package.refresh"}, "server"),
    ({"server": "a*", "verb": "package.refresh"}, "server"),
    ({"server": "node-l", "verb": "package.refresh"}, "server"),          # in the inventory, not enabled
    ({"server": "example-prod", "verb": "package.refresh"}, "catalog"),  # enabled, but not in the catalog
    ({"server": "unknown", "verb": "package.refresh"}, "server"),
    ({"server": "node-e", "verb": "package.install", "packages": [{"name": "htop", "version": "3.3.0-4"}]},
     "install"),
    ({"server": "node-c", "verb": "package.install", "packages": []}, "packages"),
    ({"server": "node-c", "verb": "package.install", "packages": [{"name": "htop"}]}, "version"),
    ({"server": "node-c", "verb": "package.install", "packages": [{"name": "-o", "version": "1"}]}, "name"),
    ({"server": "node-c", "verb": "package.install", "packages": [{"name": "ht*p", "version": "1"}]}, "name"),
    ({"server": "node-c", "verb": "package.install", "packages": [{"name": "htop", "version": "1 --force"}]},
     "version"),
    ({"server": "node-c", "verb": "package.install",
      "packages": [{"name": "htop", "version": "1", "url": "http://x"}]}, "unexpected"),
    ({"server": "node-c", "verb": "package.install",
      "packages": [{"name": "htop", "version": "1"}, {"name": "htop", "version": "2"}]}, "twice"),
    ({"server": "node-c"}, "verb"),
    ("package.refresh node-c", "object"),
])
def test_invalid_operations_are_refused(op, why):
    with pytest.raises(ops.ProposalError, match=why):
        validate(proposal(operations=[op]), servers=("node-c", "node-l", "example-prod", "unknown", "node-e",
                                                      "all", "a*", "node-c,node-e"))


@pytest.mark.parametrize("over,why", [
    ({"schema_version": 3}, "schema"),
    ({"task_id": "KD-0"}, "task"),
    ({"intent": ""}, "intent"),
    ({"intent": "x" * 2001}, "intent"),
    ({"basis": 5}, "basis"),
    ({"basis": ""}, "basis"),
    ({"operations": []}, "operations"),
    ({"operations": [{"server": "node-c", "verb": "package.refresh"}] * 31}, "30"),
    ({"extra": 1}, "unexpected"),
    ({"intent": "x\x00y"}, "intent"),
])
def test_invalid_proposals_are_refused(over, why):
    with pytest.raises(ops.ProposalError, match=why):
        validate(proposal(**over))


def test_servers_must_match_card_labels_exactly():
    with pytest.raises(ops.ProposalError, match="server:"):
        validate(proposal(), servers=())
    with pytest.raises(ops.ProposalError, match="server:"):
        validate(proposal(), servers=("node-c", "node-e"))  # a label without an operation widens the card


def test_more_than_eight_servers_are_refused():
    many = {f"n{i}" for i in range(9)}
    data = copy.deepcopy(CATALOG)
    for name in many:
        data["servers"][name] = {"packages": "apt", "install": False, "units": []}
    p = proposal(operations=[explain({"server": n, "verb": "package.refresh"}) for n in sorted(many)])
    with pytest.raises(ops.ProposalError, match="8"):
        ops.validate_proposal(p, catalog(data), INVENTORY | many, ENABLED | many, card_servers=tuple(many))


def test_remote_argv_is_fixed_per_verb_and_os():
    c = catalog()
    argv = lambda op: ops.remote_argv(ops.validate_operation(explain(op), c, INVENTORY, ENABLED), c)  # noqa: E731
    assert argv({"server": "node-c", "verb": "package.refresh"}) == ["apt-get", "-q", "update"]
    assert argv({"server": "node-g", "verb": "package.refresh"}) == ["dnf", "-q", "makecache"]
    assert argv({"server": "node-c", "verb": "service.restart", "unit": "nginx.service"}) == \
        ["systemctl", "restart", "nginx.service"]
    assert argv({"server": "node-c", "verb": "service.start", "unit": "app-worker@alpha.service"}) == \
        ["systemctl", "start", "app-worker@alpha.service"]
    assert argv({"server": "node-c", "verb": "package.install",
                 "packages": [{"name": "htop", "version": "3.3.0-4"}, {"name": "jq", "version": "1:1.7.1-3"}]}) == \
        ["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-q", "-y", "-o", "Dpkg::Options::=--force-confdef",
         "-o", "Dpkg::Options::=--force-confold", "--no-remove", "install", "htop=3.3.0-4", "jq=1:1.7.1-3"]
    assert argv({"server": "node-g", "verb": "package.install",
                 "packages": [{"name": "htop", "version": "3.3.0-4.el8"}]}) == \
        ["dnf", "-q", "-y", "install", "htop-3.3.0-4.el8"]


def upgrade_catalog():
    data = copy.deepcopy(CATALOG)
    data["verbs"].append("package.upgrade")
    return catalog(data)


def test_package_upgrade_runs_the_full_upgrade_in_its_own_unit():
    # the upgrade of the whole system, without a reboot, in a transient unit — a dropped session does not
    # kill it, and a second upgrade cannot start while the first runs (the unit name is taken)
    c = upgrade_catalog()
    argv = lambda op: ops.remote_argv(ops.validate_operation(explain(op), c, INVENTORY, ENABLED), c)  # noqa: E731
    assert argv({"server": "node-c", "verb": "package.upgrade"}) == [
        "systemd-run", "--unit=kd-package-upgrade", "--collect", "--setenv=NEEDRESTART_SUSPEND=1",
        "--setenv=DEBIAN_FRONTEND=noninteractive", "apt-get", "-q", "-y", "-o", "Dpkg::Options::=--force-confdef",
        "-o", "Dpkg::Options::=--force-confold", "full-upgrade"]
    assert argv({"server": "node-g", "verb": "package.upgrade"}) == [
        "systemd-run", "--unit=kd-package-upgrade", "--collect", "dnf", "-q", "-y", "upgrade"]
    op = ops.validate_operation(explain({"server": "node-e", "verb": "package.upgrade"}), c, INVENTORY, ENABLED)
    assert op.as_dict()["verb"] == "package.upgrade" and "packages" not in op.as_dict()
    assert "обновить все пакеты" in ops.mechanics(op)


@pytest.mark.parametrize("op,why", [
    ({"server": "node-c", "verb": "package.upgrade", "packages": []}, "unexpected"),
    ({"server": "example-prod", "verb": "package.upgrade"}, "catalog"),
])
def test_package_upgrade_takes_no_parameters_and_needs_the_catalog(op, why):
    with pytest.raises(ops.ProposalError, match=why):
        ops.validate_operation(explain(op), upgrade_catalog(), INVENTORY, ENABLED)
    with pytest.raises(ops.ProposalError, match="not enabled"):
        ops.validate_operation(explain({"server": "node-c", "verb": "package.upgrade"}), catalog(), INVENTORY,
                               ENABLED)


def test_package_upgrade_passes_the_policy():
    # neither rule 1 nor rule 2 of the gate's policy holds the formed command: the owner's signature on the plan
    # is enough, no critical action
    c = upgrade_catalog()
    for server in ("node-c", "node-g"):
        op = ops.validate_operation(explain({"server": server, "verb": "package.upgrade"}), c, INVENTORY, ENABLED)
        command = ops.remote_command(op, c)
        assert exec_policy.protect(command) is None and exec_policy.plan_step(1, command, ("*",)) is None


def test_remote_command_quotes_every_word():
    c = catalog()
    op = ops.validate_operation(explain({"server": "node-c", "verb": "service.restart", "unit": "app-worker@alpha.service"}),
                                c, INVENTORY, ENABLED)
    command = ops.remote_command(op, c)
    assert shlex.split(command) == ["systemctl", "restart", "app-worker@alpha.service"]


@pytest.mark.parametrize("unit", ["ssh.service", "sshd.service", "mysql.service", "postgresql@18-main.service",
                                  "digipaddock-board-web.service", "digipaddock-cabinet.service",
                                  "systemd-networkd.service", "docker.service", "wg-quick@wg0.service",
                                  "danted@wg10.service", "hindsight-api.service", "forgejo.service",
                                  "fail2ban.service", "networking.service", "NetworkManager.service"])
def test_catalog_refuses_protected_units(unit):
    data = copy.deepcopy(CATALOG)
    data["servers"]["node-c"]["units"].append(unit)
    with pytest.raises(ops.CatalogError, match="protected"):
        catalog(data)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update(schema_version=2),
    lambda d: d.update(verbs=["command.run"]),  # script is a verb since the change; free commands are not
    lambda d: d["servers"]["node-c"].update(packages="pacman"),
    lambda d: d["servers"]["node-c"].update(extra=1),
    lambda d: d["servers"].update({"bad name": {"packages": "apt", "install": False, "units": []}}),
    lambda d: d["servers"]["node-c"].update(units=["nginx"]),
    lambda d: d.update(extra=1),
    lambda d: d.update(critical_units="app-*"),
    lambda d: d.update(critical_units=["app-* ; reboot"]),
])
def test_catalog_shape_is_checked(mutate):
    data = copy.deepcopy(CATALOG)
    mutate(data)
    with pytest.raises(ops.CatalogError):
        catalog(data)


def test_catalog_critical_units_are_optional_patterns():
    data = copy.deepcopy(CATALOG)
    data.pop("critical_units", None)
    assert catalog(data).critical_units == ()
    data["critical_units"] = ["app-*", "*.example.test*"]
    assert catalog(data).critical_units == ("app-*", "*.example.test*")


def test_disabled_verb_is_refused():
    data = copy.deepcopy(CATALOG)
    data["verbs"] = ["package.refresh"]
    with pytest.raises(ops.ProposalError, match="verb"):
        ops.validate_proposal(proposal(), catalog(data), INVENTORY, ENABLED, card_servers=("node-c",))


def test_repository_catalog_loads_and_keeps_production_out():
    c = ops.load_catalog()
    assert "example-prod" not in c.servers
    for name, server in c.servers.items():
        for unit in server.units:
            assert not ops.protected_unit(unit), (name, unit)


def test_describe_is_human_readable():
    c = catalog()
    p = ops.validate_proposal(proposal(), c, INVENTORY, ENABLED, card_servers=("node-c",))
    assert ops.describe(p.operations[1]) == "Перезапустить веб-сервер (node-c: перезапустить nginx.service)"


def test_proposal_round_trips_through_canonical_json():
    p = validate(proposal())
    again = validate(json.loads(json.dumps(p.as_dict())))
    assert again.sha256 == p.sha256


def test_repository_inventory_and_enabled_list_load(tmp_path):
    assert {"node-c", "example-prod", "node-g"} <= ops.load_inventory()
    enabled = tmp_path / "servers.enabled"
    enabled.write_text("node-c\n\n node-g \n", encoding="utf-8")
    assert ops.load_enabled(enabled) == {"node-c", "node-g"}
    assert ops.load_enabled(tmp_path / "missing") == set()


def test_broken_inventory_is_refused(tmp_path):
    broken = tmp_path / "inventory.json"
    broken.write_text('{"servers": []}', encoding="utf-8")
    with pytest.raises(ValueError, match="inventory"):
        ops.load_inventory(broken)


# --- the script step of an approved plan (exact text the owner signs; the shell step is gone) ---

SCRIPT_TEXT = "set -e\ncat > /etc/app/x.conf <<'EOF'\nkey = 1\nEOF\nsystemctl reload app-worker@alpha.service\n"


def script_step(**over):
    return explain({"server": "node-c", "verb": "script", "script": SCRIPT_TEXT}, **{"what": "правка конфига", **over})


def script_catalog():
    data = copy.deepcopy(CATALOG)
    data["verbs"] = data["verbs"] + ["script"]
    return catalog(data)


def test_script_step_is_validated_and_sent_verbatim():
    c = script_catalog()
    op = ops.validate_operation(script_step(), c, INVENTORY, ENABLED)
    assert op.verb == "script" and op.script == SCRIPT_TEXT
    assert ops.remote_command(op, c) == SCRIPT_TEXT
    assert ops.mechanics(op) == "node-c: скрипт, 5 строк"
    assert op.as_dict() == script_step()


def test_shell_step_is_refused_with_the_script_hint():
    data = copy.deepcopy(CATALOG)
    data["verbs"] = data["verbs"] + ["shell"]
    with pytest.raises(ops.ProposalError, match="shell steps are gone"):
        ops.validate_operation(explain({"server": "node-c", "verb": "shell"}), catalog(data), INVENTORY, ENABLED)


def test_catalog_with_legacy_shell_verb_loads_without_it():
    data = copy.deepcopy(CATALOG)
    data["verbs"] = data["verbs"] + ["shell"]
    assert "shell" not in catalog(data).verbs


def test_script_needs_no_catalog_entry_for_its_server():
    # like the shell step did: production carries scripts while its typed operations stay off
    op = ops.validate_operation(script_step(server="example-prod"), script_catalog(), INVENTORY, ENABLED)
    assert op.server == "example-prod"
    with pytest.raises(ops.ProposalError, match="no ordinary operations"):
        ops.validate_operation(explain({"server": "example-prod", "verb": "service.restart", "unit": "nginx.service"}),
                               script_catalog(), INVENTORY, ENABLED)


def test_script_needs_the_verb_in_the_catalog_and_an_enabled_server():
    with pytest.raises(ops.ProposalError, match="not enabled in the catalog"):
        ops.validate_operation(script_step(), catalog(), INVENTORY, ENABLED)
    with pytest.raises(ops.ProposalError, match="not enabled"):
        ops.validate_operation(script_step(), script_catalog(), INVENTORY, set())


def test_script_takes_no_other_keys():
    with pytest.raises(ops.ProposalError, match="unexpected keys"):
        ops.validate_operation(script_step(command="rm -rf /"), script_catalog(), INVENTORY, ENABLED)


@pytest.mark.parametrize("text", ["", " \n", "x" * (16 * 1024 + 1), None, 5])
def test_script_text_limits(text):
    with pytest.raises(ops.ProposalError, match="script"):
        ops.validate_operation(script_step(script=text), script_catalog(), INVENTORY, ENABLED)


def test_script_refuses_carriage_return_with_a_hint():
    # the plan saved with CRLF names the cause and the cure, not only the character
    for text in ("echo a\r\necho b\n", "cat > /etc/x <<'EOF'\r\na\r\nEOF\r\n"):
        with pytest.raises(ops.ProposalError, match="U\\+000D") as exc:
            ops.validate_operation(script_step(script=text), script_catalog(), INVENTORY, ENABLED)
        assert "CRLF" in str(exc.value) and "heredoc" in str(exc.value) and "LF line endings" in str(exc.value)


WRAPPED_B64 = "\n".join(["H4sIAAAAAAAAA3NMSU0tUlRITc7PTUlVSEnNz1coLi1QSM7PS0nMy0n"] * 3) + "\n"


@pytest.mark.parametrize("text", [
    "echo " + "H4sIAAAAAAAAA" * 12 + " | base64 -d | gunzip > /root/x.sh",
    "base64 --decode /tmp/blob > /usr/local/bin/x",
    "openssl enc -d -base64 -in /tmp/b",
    "echo x | xxd -r -p",
    "uudecode /tmp/blob",
    # the decoders and unpackers the first pattern missed
    "base64 -di /tmp/blob > /usr/local/bin/x",
    "base64 -id /tmp/blob > /usr/local/bin/x",
    "base64 /tmp/blob -d > /usr/local/bin/x",
    "base32 -d /tmp/blob > /root/x",
    "openssl base64 -d -in /tmp/b -out /root/x",
    "openssl base64 -A -d < /tmp/b",
    "python3 -m base64 -d /tmp/b > /root/x",
    "python3 -c 'import base64,sys; sys.stdout.buffer.write(base64.b64decode(sys.argv[1]))' QUJD",
    "python3 -c 'import base64; print(base64.urlsafe_b64decode(\"QUJD\"))'",
    "python3 -c 'import zlib,sys; sys.stdout.buffer.write(zlib.decompress(open(sys.argv[1], \"rb\").read()))' /tmp/b",
    "perl -MMIME::Base64 -e 'print decode_base64(<STDIN>)' < /tmp/b",
    "zcat /tmp/payload.gz > /usr/local/bin/x",
    "gunzip -c /tmp/payload.gz > /root/x",
    "gzip -dc /tmp/payload.gz > /root/x",
    "xz --decompress /tmp/payload.xz",
    "cat > /root/blob <<'EOF'\n" + WRAPPED_B64 + "EOF\n",
    "printf '\\x23\\x21/bin/sh\\n' > /root/x",
    "echo -e '\\x69\\x64' | bash",
    "printf '\\151\\144' | bash",
    "bash -c $'\\x69\\x64'",
])
def test_script_refuses_packed_payload(text):
    with pytest.raises(ops.ProposalError, match="readable"):
        ops.validate_operation(script_step(script=text), script_catalog(), INVENTORY, ENABLED)


@pytest.mark.parametrize("text", [
    "set -e\ncp /etc/nginx/nginx.conf /root/nginx.conf.bak\nsed -i 's/768/1024/' /etc/nginx/nginx.conf\nnginx -t\n",
    "printf '%s\\n' 'line one' > /etc/motd\n",
    "base64 /etc/hostname\n",  # encoding is readable input; only decoding hides what runs
    "gzip -9 /var/log/app/old.log\n",
    "openssl enc -aes-256-cbc -md sha256 -salt -pbkdf2 -in /root/a -out /root/a.enc -pass file:/root/p\n",
    "# the app decompresses its own cache\nsystemctl restart app.service\n",
    "cat > /etc/logrotate.d/app <<'EOF'\n/var/www/site/storage/logs/laravel-worker.log\n"
    "/var/www/site/storage/logs/laravel-scheduler.log\n/var/www/site/storage/logs/laravel-queue-high.log\n"
    "{\n  daily\n}\nEOF\n",
    "umask 077; mysqldump app | gzip > /tmp/kd-app.sql.gz && cp -n /tmp/kd-app.sql.gz /backups/app-1.sql.gz\n",
])
def test_script_keeps_readable_scripts(text):
    assert ops.validate_operation(script_step(script=text), script_catalog(), INVENTORY, ENABLED).script == text


def test_script_text_changes_the_hash():
    a = ops.Proposal("KD-1", "i", "b", (ops.Operation("node-c", "script", script="echo a\n"),))
    b = ops.Proposal("KD-1", "i", "b", (ops.Operation("node-c", "script", script="echo b\n"),))
    assert a.sha256 != b.sha256


def test_script_has_no_argv():
    with pytest.raises(ops.ProposalError, match="script"):
        ops.remote_argv(ops.Operation("node-c", "script", script="echo a\n"), script_catalog())


def test_proposal_runs_deny_and_critical_on_scripts():
    data = proposal(operations=[script_step(), explain({"server": "node-e", "verb": "package.refresh"})])
    servers = ("node-c", "node-e")
    seen = []

    def deny(command, server):
        seen.append((command, server))
        return ("rm-root", "recursive removal")

    with pytest.raises(ops.ProposalError, match="denied by rule rm-root on node-c: recursive removal"):
        ops.validate_proposal(data, script_catalog(), INVENTORY, ENABLED, card_servers=servers, deny=deny)
    assert seen == [(SCRIPT_TEXT, "node-c")]  # typed steps are not free text: the denylist does not read them
    with pytest.raises(ops.ProposalError, match="node-c: шаг 1 критичный \\(сетевой доступ\\): владелец подписывает "
                                                "его отдельно, точной командой \\(kd-admin-propose-action\\)"):
        ops.validate_proposal(data, script_catalog(), INVENTORY, ENABLED, card_servers=servers,
                              critical=lambda command: "сетевой доступ")
    ok = ops.validate_proposal(data, script_catalog(), INVENTORY, ENABLED, card_servers=servers,
                               deny=lambda c, s: None, critical=lambda c: None)
    assert [o.verb for o in ok.operations] == ["script", "package.refresh"]


def test_typed_steps_are_classified_by_their_formed_command():
    # rule 2 reads the command a typed step forms, not only script text
    data = proposal(operations=[script_step(), explain({"server": "node-e", "verb": "package.refresh"})])
    seen = []

    def critical(command):
        seen.append(command)
        return "перезагрузка или остановка" if command.startswith("apt-get") else None

    with pytest.raises(ops.ProposalError, match="node-e: шаг 2 критичный"):
        ops.validate_proposal(data, script_catalog(), INVENTORY, ENABLED, card_servers=("node-c", "node-e"),
                              critical=critical)
    assert seen == [SCRIPT_TEXT, "apt-get -q update"]


@pytest.mark.parametrize("script", [
    "echo 'x ALL=(ALL) NOPASSWD: ALL' > /etc/sudoers.d/x\n",
    "useradd -m bob\n",
    "cp /tmp/k /root/.ssh/authorized_keys\n",
    "perl -pi -e 's/a/b/' /etc/nginx/nginx.conf\n",
])
def test_rule1_refuses_a_script_with_the_gates_reason_before_the_denylist(script):
    data = proposal(operations=[script_step(script=script)])
    with pytest.raises(ops.ProposalError) as caught:
        ops.validate_proposal(data, script_catalog(), INVENTORY, ENABLED, card_servers=("node-c",),
                              deny=lambda c, s: ("other", "the denylist's reason"), critical=lambda c: None)
    assert str(caught.value) == "node-c: " + exec_policy.protect(script)


def test_a_packed_script_is_refused_with_the_gates_reason_and_the_hint():
    with pytest.raises(ops.ProposalError) as caught:
        ops.validate_operation(script_step(script="base64 -d /tmp/x | sh\n"), script_catalog(), INVENTORY, ENABLED)
    assert str(caught.value).startswith(exec_policy.PACKED_FOUND + ": " + exec_policy.GUARD + "; ")
    assert ops.PACKED_HINT in str(caught.value)


def test_script_is_described_with_its_what():
    assert ops.describe(ops.Operation("node-c", "script", script="a\nb", what="правка nginx")) == \
        "правка nginx (node-c: скрипт, 2 строк)"


# --- the owner's explanations ---------------------------------------------------

@pytest.mark.parametrize("mutate,why", [
    (lambda d: d.pop("basis"), "basis"),
    (lambda d: d["operations"][0].pop("what"), "what"),
    (lambda d: d["operations"][0].pop("effect"), "effect"),
    (lambda d: d["operations"][0].pop("rollback"), "rollback"),
    (lambda d: d["operations"][0].pop("risk"), "risk"),
    (lambda d: d["operations"][0]["risk"].pop("why"), "risk"),
    (lambda d: d["operations"][0]["risk"].update(level="urgent"), "risk"),
    (lambda d: d["operations"][0]["risk"].update(extra=1), "risk"),
    (lambda d: d["operations"][0].update(risk="low"), "risk"),
    (lambda d: d["operations"][0].update(effect="x" * 501), "effect"),
    (lambda d: d.update(rollback="the v1 plan-wide rollback"), "unexpected"),
])
def test_schema_2_requires_the_owner_explanations(mutate, why):
    data = proposal()
    mutate(data)
    with pytest.raises(ops.ProposalError, match=why):
        validate(data)


def test_a_full_schema_2_plan_passes_and_is_described_by_what():
    p = validate(proposal())
    first = p.operations[0]
    assert (first.what, first.risk_level, first.rollback) == ("Обновить индексы пакетов", "low", "Не нужен")
    assert ops.describe(first).startswith("Обновить индексы пакетов")
    assert p.as_dict()["schema_version"] == 2 and p.basis == "ssh-ro: nginx отдаёт 502"
    assert p.as_dict()["operations"][0]["risk"] == {"level": "low", "why": "Конфигурация не меняется"}


def test_schema_1_is_refused_with_a_hint():
    old = {"schema_version": 1, "task_id": "KD-123", "intent": "x", "rollback": "y",
           "operations": [{"server": "node-c", "verb": "package.refresh"}]}
    with pytest.raises(ops.ProposalError, match="schema_version 2: add basis and what/effect/risk/rollback"):
        validate(old)
    with pytest.raises(ops.ProposalError, match="schema_version 2"):
        ops.validate_operation({"server": "node-c", "verb": "script", "script": "x", "purpose": "x"}, script_catalog(),
                               INVENTORY, ENABLED)


def test_repository_catalog_enables_the_script_step():
    assert "script" in ops.load_catalog().verbs and "shell" not in ops.load_catalog().verbs


def test_a_plan_has_no_script_step_on_the_factorys_own_host():
    # review 04.11.2026: on the home server a free-form command reaches the cage; its catalog operations stay
    script = proposal(operations=[script_step(what="почистить /tmp")])
    with pytest.raises(ops.ProposalError, match="node-c is the factory's own host"):
        ops.validate_proposal(script, script_catalog(), INVENTORY, ENABLED, card_servers=("node-c",), home="node-c")
    assert ops.validate_proposal(script, script_catalog(), INVENTORY, ENABLED, card_servers=("node-c",), home="other")
    typed = ops.validate_proposal(proposal(), catalog(), INVENTORY, ENABLED, card_servers=("node-c",), home="node-c")
    assert [o.verb for o in typed.operations] == ["package.refresh", "service.restart"]


def test_load_home_reads_the_inventorys_home_and_fails_closed(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps({"home": "a", "servers": {"a": {"host": "203.0.113.1"}}}), "utf-8")
    assert ops.load_home(path) == "a"
    for data in ({"servers": {"a": {}}}, {"home": "b", "servers": {"a": {}}}, {"home": 5, "servers": {}}):
        path.write_text(json.dumps(data), "utf-8")
        with pytest.raises(ValueError, match="no home server"):
            ops.load_home(path)
