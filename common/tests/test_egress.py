"""The probe exits of a host (host/egress.json): who gets one, and the tinyproxy config that allows only its names
through the VPN tunnel port."""

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kd import egress

FIXTURE = Path(__file__).parent / "fixtures" / "host" / "egress.json"
CONFIG = json.loads(FIXTURE.read_text(encoding="utf-8"))


def _write(tmp_path, document) -> Path:
    path = tmp_path / "egress.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def test_absent_file_is_no_exits(tmp_path):
    assert egress.load(tmp_path / "none.json") == {}
    assert egress.exit_for({}, "shop", "dev") == ""


def test_project_maps_to_its_exit_for_a_listed_agent():
    config = egress.load(FIXTURE)
    assert egress.exit_for(config, "shop", "dev") == "probe"
    assert egress.exit_for(config, "shop", "boss") == ""
    assert egress.exit_for(config, "other", "dev") == ""
    assert egress.exit_of(config, "probe")["port"] == 3140
    with pytest.raises(egress.EgressError):
        egress.exit_of(config, "missing")


def _broken(change):
    document = copy.deepcopy(CONFIG)
    change(document)
    return document


BROKEN = {
    "unknown top key": lambda d: d.update(extra=1),
    "low port": lambda d: d["exits"]["probe"].update(port=80),
    "upstream not loopback": lambda d: d["exits"]["probe"].update(
        upstream="10.0.0.1:1080"
    ),
    "domain with a port": lambda d: d["exits"]["probe"].update(
        domains=["example.test:443"]
    ),
    "domain as an IP": lambda d: d["exits"]["probe"].update(domains=["192.0.2.1"]),
    "wildcard domain": lambda d: d["exits"]["probe"].update(domains=["*.example.test"]),
    "no domains": lambda d: d["exits"]["probe"].update(domains=[]),
    "project names a missing exit": lambda d: d["projects"].update(shop="missing"),
    "bad exit name": lambda d: d["exits"].update({"Probe!": d["exits"]["probe"]}),
    "max_clients too high": lambda d: d["exits"]["probe"].update(max_clients=33),
    "timeout too short": lambda d: d["exits"]["probe"].update(timeout=4),
    "unknown exit key": lambda d: d["exits"]["probe"].update(direct=True),
    "schema": lambda d: d.update(schema_version=2),
    "agents not names": lambda d: d["exits"]["probe"].update(agents=["Dev Agent"]),
    # a wrong JSON type is a refusal (EgressError), never a TypeError that escapes the cabinet's catch
    "project names a list": lambda d: d["projects"].update(shop=["probe"]),
    "project names an object": lambda d: d["projects"].update(shop={"probe": 1}),
    "domains hold a list": lambda d: d["exits"]["probe"].update(
        domains=[["example.test"]]
    ),
    "agents hold an object": lambda d: d["exits"]["probe"].update(agents=[{"dev": 1}]),
    # every host is the exit's whole list, never one name among others
    "every host among names": lambda d: d["exits"]["probe"].update(
        domains=["*", "example.test"]
    ),
    "standing names a missing exit": lambda d: d.update(standing="missing"),
    "standing is not a name": lambda d: d.update(standing=["probe"]),
}


@pytest.mark.parametrize("case", sorted(BROKEN))
def test_shape_is_refused(tmp_path, case):
    with pytest.raises(egress.EgressError):
        egress.load(_write(tmp_path, _broken(BROKEN[case])))


def test_no_proxy_keeps_the_model_api_off_the_exit():
    value = egress.no_proxy(["git.example.test"], "https://llm.example.test/v1")
    assert value.startswith(
        "localhost,127.0.0.1,::1,api.anthropic.com,claude.ai,platform.claude.com,"
    )
    names = value.split(",")
    assert "git.example.test" in names and "llm.example.test" in names
    assert (
        egress.no_proxy([])
        == "localhost,127.0.0.1,::1,api.anthropic.com,claude.ai,platform.claude.com"
    )


def test_render_allows_only_its_names_through_the_tunnel():
    config = egress.load(FIXTURE)
    conf, filt = egress.render(
        "probe", egress.exit_of(config, "probe"), "/var/log/digipaddock-egress"
    )
    lines = conf.splitlines()
    for line in (
        "Listen 127.0.0.1",
        "Port 3140",
        "ConnectPort 443",
        "FilterDefaultDeny Yes",
        "FilterType ere",
        "MaxClients 8",
        "Timeout 60",
        'Filter "/etc/digipaddock/egress/probe.filter"',
        'LogFile "/var/log/digipaddock-egress/probe.log"',
        "User tinyproxy",
        "Group tinyproxy",
    ):
        assert line in lines, line
    upstream = [line for line in lines if line.startswith("upstream")]
    assert upstream == [
        'upstream socks5 127.0.0.1:1080 "api.example.test"',
        'upstream socks5 127.0.0.1:1080 "example.test"',
    ]
    assert filt.splitlines() == [r"^api\.example\.test$", r"^example\.test$"]


def _any_host(**top) -> dict:
    document = copy.deepcopy(CONFIG)
    document["exits"]["probe"]["domains"] = ["*"]
    document.update(top)
    return egress.check(document)


def test_an_exit_of_every_host_sends_everything_through_the_tunnel():
    entry = egress.exit_of(_any_host(), "probe")
    assert egress.any_host(entry)
    assert not egress.any_host(egress.exit_of(egress.load(FIXTURE), "probe"))
    conf, filt = egress.render("probe", entry, "/var/log/digipaddock-egress")
    lines = conf.splitlines()
    # no name list to filter by: one default upstream, so nothing leaves the host directly
    assert [line for line in lines if line.startswith("upstream")] == [
        "upstream socks5 127.0.0.1:1080"
    ]
    assert not [line for line in lines if line.startswith("Filter")]
    assert "ConnectPort 443" in lines and "Listen 127.0.0.1" in lines
    assert filt == ""


def test_a_standing_exit_is_every_session_of_its_agents():
    config = _any_host(standing="probe")
    assert egress.standing_for(config, "dev") == "probe"
    assert egress.standing_for(config, "boss") == ""  # the exit does not list it
    assert egress.standing_for(egress.load(FIXTURE), "dev") == ""  # no standing exit
    assert egress.standing_for({}, "dev") == ""


# --- an exit per country, chosen by a card's egress:<code> label ---------------------------------------

def _countries(**countries) -> dict:
    document = copy.deepcopy(CONFIG)
    web = {**document["exits"]["probe"], "domains": ["*"]}
    document["exits"].update(
        web={**web, "port": 3141}, **{"web-ae": {**web, "port": 3142, "upstream": "127.0.0.1:1151"}}
    )
    document["countries"] = countries or {"GB": "web", "AE": "web-ae"}
    return document


def test_countries_map_a_code_to_an_exit_of_every_host():
    config = egress.check(_countries())
    assert egress.country_exit(config, "AE") == "web-ae"
    assert egress.country_exit(config, "FR") == ""
    assert egress.country_of(config, "web") == "GB"
    assert egress.country_of(config, "probe") == ""
    assert egress.country_labels(config) == ["egress:ae", "egress:gb"]
    assert egress.country_labels(CONFIG) == []  # countries are optional


@pytest.mark.parametrize("countries", [
    {"AE": "missing"},  # an exit the file does not have
    {"AE": "probe"},  # an exit of named hosts, not of every host
    {"ae": "web-ae"},  # not an ISO 3166-1 code
    {"UAE": "web-ae"},
    {"AE": ["web-ae"]},
    {"GB": "web", "AE": "web"},  # one exit is one country's tunnel
])
def test_a_country_must_name_an_exit_of_every_host(countries):
    with pytest.raises(egress.EgressError):
        egress.check(_countries(**countries))


def test_countries_must_be_an_object():
    with pytest.raises(egress.EgressError, match="countries"):
        egress.check({**_countries(), "countries": ["AE"]})


def test_label_country():
    assert egress.label_country(["spec:implementer", "egress:ae"]) == "AE"
    assert egress.label_country(["spec:implementer"]) == ""


def test_render_cli_writes_conf_and_filter(tmp_path):
    out, logs = tmp_path / "etc", tmp_path / "log"
    rc = egress.main(
        ["render", "--out", str(out), "--log-dir", str(logs), "--config", str(FIXTURE)]
    )
    assert rc == 0
    assert sorted(p.name for p in out.iterdir()) == ["probe.conf", "probe.filter"]
    assert "Port 3140" in (out / "probe.conf").read_text(encoding="utf-8").splitlines()


def test_list_cli_names_the_exits_and_their_ports(tmp_path, capsys):
    assert egress.main(["list", "--config", str(FIXTURE)]) == 0
    assert capsys.readouterr().out.splitlines() == ["probe 3140"]
    assert egress.main(["list", "--config", str(tmp_path / "none.json")]) == 0
    assert capsys.readouterr().out == ""


@pytest.mark.skipif(os.name != "posix", reason="symlinks: POSIX only")
def test_the_path_is_the_release_tree_behind_the_switch_link(tmp_path):
    # the release switch makes /opt/digipaddock a symbolic link; kd.launch requires root-owned directories all the
    # way to the file (O_NOFOLLOW), so the path names the release tree itself, never the link
    common = Path(egress.__file__).resolve().parents[1]
    link = tmp_path / "current"
    link.symlink_to(common.parent, target_is_directory=True)
    out = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); "
            "from kd import egress; print(egress.PATH)",
            str(link / "common"),
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert out == str(common.parent / "host" / "egress.json")


SETUP = Path(__file__).resolve().parents[1] / "install" / "egress_setup.sh"
UNIT = Path(__file__).resolve().parents[1] / "systemd" / "digipaddock-egress@.service"


def test_the_exit_log_is_readable_by_kd_host_read():
    # tinyproxy creates its log 0600 (umask 0177): the setup makes it first, tinyproxy:<observe> 0640, in a root
    # directory (tinyproxy can plant no link that root's chown would follow), and check verifies both
    text = SETUP.read_text("utf-8")
    assert 'install -d -o root -g root -m 0755 "$LOGS"' in text
    assert 'install -o tinyproxy -g "$observe" -m 0640 /dev/null "$log"' in text
    assert '"tinyproxy:$observe 640"' in text and '"root:root 755"' in text


def test_the_exit_runs_as_tinyproxy_without_capabilities():
    text = UNIT.read_text("utf-8")
    assert "\nUser=tinyproxy\n" in text and "\nGroup=tinyproxy\n" in text
    assert "\nCapabilityBoundingSet=\n" in text
