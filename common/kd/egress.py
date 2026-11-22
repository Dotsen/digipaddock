"""Probe exits (the agent autonomy design, piece 2): a card's session of a project allowed to probe reaches the named
external hosts through its sandbox proxy, which forwards them to a loopback tinyproxy that sends exactly those names
into one VPN tunnel port and refuses everything else.

``host/egress.json`` of the release tree (overlay data; absent — no exits):

    {"schema_version": 1,
     "exits": {"<exit>": {"port": 3140, "upstream": "127.0.0.1:<tunnel port>", "domains": ["<name>", ...],
                          "agents": ["dev"], "max_clients": 8, "timeout": 60}},
     "projects": {"<project>": "<exit>"},
     "standing": "<exit>"}

``"domains": ["*"]`` — an exit of every host: the deployment's web access, still only through the tunnel port.
``standing`` (optional) — the exit every session of its agents holds, a card's or not: no card's consent
gives or ends it.
``countries`` (optional) — ``{"<ISO 3166-1 code>": "<exit>"}``: the exit of every host that leaves through
that country's tunnel port. A card labelled ``egress:<code>`` (lower case) gives its session that country's exit;
``KD_EGRESS_COUNTRY`` names the country of the exit a session holds.
"""

import json
import os
import re
import sys
from typing import Iterable, Mapping, Tuple
from urllib.parse import urlsplit

from kd.registry import DOMAIN_RE

# the release tree itself: /opt/digipaddock is the release switch's link, and kd.launch walks only root-owned
# directories to the file, never a link
PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "host",
    "egress.json",
)
EXIT = re.compile(r"[a-z][a-z0-9-]{0,31}")
AGENT = re.compile(r"[a-z][a-z0-9_-]{0,31}")
PROJECT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
UPSTREAM = re.compile(r"127\.0\.0\.1:([1-9][0-9]{0,4})")
FILTER_DIR = "/etc/digipaddock/egress"
# Never through an exit: Claude Code's own endpoints and loopback (the exit refuses them, and the session needs them).
NO_PROXY_HEAD = (
    "localhost",
    "127.0.0.1",
    "::1",
    "api.anthropic.com",
    "claude.ai",
    "platform.claude.com",
)
_TOP = {"schema_version", "exits", "projects"}
_OPTIONAL_TOP = {"standing", "countries"}
_EXIT_KEYS = {"port", "upstream", "domains", "agents", "max_clients", "timeout"}
ANY_HOST = "*"  # an exit's whole list: every host
COUNTRY = re.compile(r"[A-Z]{2}")
LABEL = "egress:"  # a card's country label: egress:<code>, the code in lower case


class EgressError(ValueError):
    pass


def _int(value, low: int, high: int, label: str) -> int:
    if type(value) is not int or not low <= value <= high:
        raise EgressError(f"{label} must be an integer {low}..{high}")
    return value


def _names(value, pattern, label: str) -> list:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(v, str) and pattern(v) for v in value)
        or len(set(value)) != len(value)
    ):
        raise EgressError(f"{label} must be a non-empty list of distinct names")
    return value


def _check_exit(name: str, entry) -> None:
    if not EXIT.fullmatch(name):
        raise EgressError(f"exit name {name!r} is not a plain name")
    if not isinstance(entry, dict) or set(entry) != _EXIT_KEYS:
        raise EgressError(f"exit {name}: keys must be {', '.join(sorted(_EXIT_KEYS))}")
    _int(entry["port"], 1024, 65535, f"exit {name}: port")
    upstream = entry["upstream"]
    match = UPSTREAM.fullmatch(upstream) if isinstance(upstream, str) else None
    if not match or not 1 <= int(match.group(1)) <= 65535:
        raise EgressError(f"exit {name}: upstream must be 127.0.0.1:<port>")
    if entry["domains"] != [ANY_HOST]:
        # a host name, never an address: DOMAIN_RE alone also matches a dotted IPv4
        _names(
            entry["domains"],
            lambda d: bool(DOMAIN_RE.match(d)) and not d.rsplit(".", 1)[-1].isdigit(),
            f"exit {name}: domains",
        )
    _names(entry["agents"], AGENT.fullmatch, f"exit {name}: agents")
    _int(entry["max_clients"], 1, 32, f"exit {name}: max_clients")
    _int(entry["timeout"], 5, 300, f"exit {name}: timeout")


def check(document) -> dict:
    """The checked document, or EgressError."""
    if not isinstance(document, dict) or set(document) - _OPTIONAL_TOP != _TOP:
        raise EgressError(f"egress.json: keys must be {', '.join(sorted(_TOP))}")
    if document["schema_version"] != 1 or type(document["schema_version"]) is not int:
        raise EgressError("egress.json: schema_version must be 1")
    exits, projects = document["exits"], document["projects"]
    if not isinstance(exits, dict) or not isinstance(projects, dict):
        raise EgressError("egress.json: exits and projects must be objects")
    for name, entry in exits.items():
        _check_exit(name, entry)
    ports = [entry["port"] for entry in exits.values()]
    if len(set(ports)) != len(ports):
        raise EgressError("egress.json: two exits on one port")
    for project, name in projects.items():
        if (
            not PROJECT.fullmatch(project)
            or not isinstance(name, str)
            or name not in exits
        ):
            raise EgressError(
                f"egress.json: project {project!r} must name an exit of this file"
            )
    standing = document.get("standing", "")
    if "standing" in document and (
        not isinstance(standing, str) or standing not in exits
    ):
        raise EgressError("egress.json: standing must name an exit of this file")
    countries = document.get("countries", {})
    if not isinstance(countries, dict):
        raise EgressError("egress.json: countries must be an object")
    for code, name in countries.items():
        if not COUNTRY.fullmatch(code):
            raise EgressError(
                f"egress.json: country {code!r} is not an ISO 3166-1 code"
            )
        if not isinstance(name, str) or name not in exits:
            raise EgressError(
                f"egress.json: country {code} must name an exit of this file"
            )
        if not any_host(exits[name]):
            raise EgressError(
                f"egress.json: country {code}: exit {name} must be of every host"
            )
    if len(set(countries.values())) != len(countries):
        raise EgressError("egress.json: two countries on one exit")
    return document


def load(path=PATH) -> dict:
    """The host's checked exits; ``{}`` when the file is absent."""
    try:
        with open(path, encoding="utf-8") as f:
            document = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise EgressError(f"egress.json: {exc}") from None
    return check(document)


def exit_of(config: Mapping, name: str) -> dict:
    entry = (config.get("exits") or {}).get(name)
    if entry is None:
        raise EgressError(f"no probe exit {name!r} on this host")
    return entry


def exit_for(config: Mapping, project: str, agent: str) -> str:
    """The exit a card's session of ``agent`` in ``project`` gets, or ``""``."""
    name = (config.get("projects") or {}).get(project, "")
    if not name:
        return ""
    return name if agent in exit_of(config, name)["agents"] else ""


def standing_for(config: Mapping, agent: str) -> str:
    """The exit every session of ``agent`` holds, a card's or not, or ``""``."""
    name = config.get("standing", "")
    if not name:
        return ""
    return name if agent in exit_of(config, name)["agents"] else ""


def any_host(entry: Mapping) -> bool:
    """Whether the exit lets every host through (``"domains": ["*"]``)."""
    return entry["domains"] == [ANY_HOST]


def label_country(labels: Iterable[str]) -> str:
    """The country a card's ``egress:<code>`` label names, upper case, or ``""``."""
    for label in labels:
        if label.startswith(LABEL):
            return label[len(LABEL) :].upper()
    return ""


def country_exit(config: Mapping, country: str) -> str:
    """The exit of ``country`` in the host's table, or ``""``."""
    return (config.get("countries") or {}).get(country, "")


def country_of(config: Mapping, exit_name: str) -> str:
    """The country whose exit ``exit_name`` is, or ``""``."""
    for code, name in (config.get("countries") or {}).items():
        if name == exit_name:
            return code
    return ""


def country_labels(config: Mapping) -> list:
    """The labels a card may carry: ``egress:<code>`` of every country of the host's table."""
    return sorted(LABEL + code.lower() for code in config.get("countries") or {})


def no_proxy(allowed: Iterable[str], base_url: str = "") -> str:
    """``NO_PROXY`` of a session with an exit: the fixed head, then the agent's other names and the provider's host."""
    rest = set(allowed)
    host = urlsplit(base_url).hostname if base_url else None
    if host:
        rest.add(host)
    rest -= set(NO_PROXY_HEAD)
    return ",".join([*NO_PROXY_HEAD, *sorted(rest)])


def render(name: str, entry: Mapping, log_dir: str) -> Tuple[str, str]:
    """The tinyproxy config of one exit and its filter: only CONNECT to 443 of its names, each through the tunnel
    port; anything else is refused by the filter, so no name ever leaves directly. An exit of every host has no
    filter (its file is empty) and one upstream for everything: still nothing leaves but through the tunnel port."""
    head = [
        "User tinyproxy",
        "Group tinyproxy",
        f"Port {entry['port']}",
        "Listen 127.0.0.1",
        "Allow 127.0.0.1",
        f"Timeout {entry['timeout']}",
        f"MaxClients {entry['max_clients']}",
        "LogLevel Connect",
        f'LogFile "{log_dir}/{name}.log"',
        "ConnectPort 443",
        "DisableViaHeader Yes",
    ]
    if any_host(entry):
        return "\n".join([*head, f"upstream socks5 {entry['upstream']}"]) + "\n", ""
    lines = [
        *head,
        f'Filter "{FILTER_DIR}/{name}.filter"',
        "FilterType ere",
        "FilterDefaultDeny Yes",
        *(
            f'upstream socks5 {entry["upstream"]} "{domain}"'
            for domain in entry["domains"]
        ),
    ]
    filt = [
        "^" + re.escape(domain).replace(r"\-", "-") + "$" for domain in entry["domains"]
    ]
    return "\n".join(lines) + "\n", "\n".join(filt) + "\n"


def main(argv) -> int:
    """``render --out DIR --log-dir DIR [--config PATH]`` — one ``<exit>.conf`` and ``<exit>.filter`` per exit;
    ``list [--config PATH]`` — ``<exit> <port>`` per line (the installer starts one unit per line)."""
    import argparse

    parser = argparse.ArgumentParser(prog="kd.egress")
    sub = parser.add_subparsers(dest="op", required=True)
    render_p = sub.add_parser("render")
    render_p.add_argument("--out", required=True)
    render_p.add_argument("--log-dir", required=True)
    render_p.add_argument("--config", default=PATH)
    list_p = sub.add_parser("list")
    list_p.add_argument("--config", default=PATH)
    args = parser.parse_args(argv)
    try:
        config = load(args.config)
    except EgressError as exc:
        print(f"kd.egress: {exc}", file=sys.stderr)
        return 1
    exits = config.get("exits") or {}
    if args.op == "list":
        for name in sorted(exits):
            print(f"{name} {exits[name]['port']}")
        return 0
    os.makedirs(args.out, exist_ok=True)
    for name in sorted(exits):
        conf, filt = render(name, exits[name], args.log_dir)
        for suffix, text in ((".conf", conf), (".filter", filt)):
            with open(
                os.path.join(args.out, name + suffix), "w", encoding="utf-8"
            ) as f:
                f.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
