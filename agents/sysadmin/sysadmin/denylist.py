"""The single list of prohibitions for the commands the sysadmin runs through ssh-exec.

The guard (before the command starts) and ssh_exec (before running it) use it.
A prohibition holds even with an approval: such things the owner does by hand.

It catches mistakes and the obvious forms of dangerous commands, also in wrappers
(sudo, env, timeout, bash -c '…') and quotes. Deliberate obfuscation
(variables, base64) it does not catch: free commands run only in a
card whose plan the owner approved (ssh-exec --task), and what is
recognized as critical — only as the exact command, approved separately.
"""
import functools
import ipaddress
import json
import os
import posixpath
import re
import sys
from pathlib import Path
from typing import Callable, NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))  # …/DigiPaddock/common — as in execute.py
from kd import naming  # noqa: E402
from kd.shell import (Argv, Parsed, parse, command_argv, writes_into, _writes_into, _live, _cmd,  # noqa: E402,F401
                      _short_has, _option_values, _mv_sources, _strip_redirects, _encloses, _GLOB, _COPIERS,
                      _ATTR_CHANGERS)



class Rule(NamedTuple):
    name: str
    why: str
    match: Callable[[Parsed, Optional[str]], bool]


# --- rules -------------------------------------------------------------------

_IPTABLES = {"iptables", "ip6tables", "iptables-legacy", "ip6tables-legacy", "iptables-nft", "ip6tables-nft"}
_RESTORE = {t + "-restore" for t in _IPTABLES} | {"iptables-apply", "ip6tables-apply"}  # the whole rule set from a file


def _firewall_flush(p: Parsed, server: Optional[str]) -> bool:
    for argv in p.argvs:
        cmd, args = _cmd(argv), argv[1:]
        if cmd in _IPTABLES and (_short_has(args, "FXP") or {"--flush", "--delete-chain", "--policy"} & set(args)):
            return True
        if cmd in _RESTORE:
            return True
        if cmd == "nft" and (
            {"flush", "delete", "destroy", "-f", "--file"} & set(args) or any(a.startswith("--file=") for a in args)
        ):
            return True
        if cmd == "ufw" and {"disable", "reset", "enable"} & set(args):  # enable on a server without rules cuts everyone off
            return True
        if cmd == "netfilter-persistent" and "flush" in args:
            return True
    return False


_RSYNC_DELETE = re.compile(r"^--del(ete(-[a-z]+)*)?$")


def _rsync_delete(p: Parsed, server: Optional[str]) -> bool:
    return any(_cmd(a) == "rsync" and any(_RSYNC_DELETE.match(t) for t in a[1:]) for a in p.argvs)


_TOP_LEVEL = {"/", "/bin", "/boot", "/dev", "/home", "/lib", "/lib64", "/proc", "/root", "/run", "/sbin", "/srv",
              "/sys", "/usr", "/var"}
_SYSTEM_TREES = ("/bin", "/boot", "/dev", "/lib", "/lib64", "/proc", "/sbin", "/sys", "/usr")  # system trees as a whole
_PROTECTED_TREES = ("/var/www", "/backups", "/var/lib/mysql", "/etc", "/opt")
_ALL_ROOTS = tuple(sorted(_TOP_LEVEL)) + _PROTECTED_TREES


def _critical(path: str) -> bool:
    if not path.startswith("/"):
        return False
    glob = _GLOB.search(path)
    if glob:
        prefix = path[:glob.start()]
        if any(root.startswith(prefix) for root in _ALL_ROOTS):
            return True  # the glob covers a protected directory: /va* → /var, /var/w* → /var/www
        folder = prefix[:-1] if prefix.endswith("/") else posixpath.dirname(prefix)
        return _critical(folder or "/")
    p = posixpath.normpath(path)
    return p in _TOP_LEVEL or any(p == tree or p.startswith(tree + "/") for tree in _PROTECTED_TREES + _SYSTEM_TREES)


def _is_root_dir(path: str) -> bool:
    return path.startswith("/") and posixpath.normpath(path) in set(_ALL_ROOTS)


def _rm_critical(p: Parsed, server: Optional[str]) -> bool:
    for argv in p.argvs:
        cmd, args = _cmd(argv), argv[1:]
        if cmd == "rm" and (_short_has(args, "rR") or "--recursive" in args) and any(map(_critical, args)):
            return True
        if cmd == "mv":  # moving a root directory (mv /etc /etc.bak) is the same removal
            if any(map(_is_root_dir, _mv_sources(args))):
                return True
    return False


_FIND_ACTIONS = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprint0", "-fprintf", "-fls"}


_XARGS_DELETERS = {"rm", "mv", "shred", "unlink"}


def _find_destructive(p: Parsed, server: Optional[str]) -> bool:
    if any(_cmd(a) == "find" and _FIND_ACTIONS & set(a[1:]) for a in p.argvs):
        return True
    for segment, argv in zip(p.segments, p.argvs):  # find … | xargs rm: the targets come through the pipeline
        wrappers = segment[:len(segment) - len(argv)]
        if _cmd(argv) in _XARGS_DELETERS and any(posixpath.basename(w) == "xargs" for w in wrappers):
            return True  # xargs appends the pipeline's targets to the explicit operands, so operands do not make it safe
    return False


_DISK_TOOLS = {"wipefs", "fdisk", "sfdisk", "parted", "sgdisk", "mke2fs", "blkdiscard", "lvremove", "vgremove",
               "pvremove"}
_CRYPT_DESTROY = {"luksformat", "erase", "lukserase", "plainopen", "create"}  # create is the old name of plainOpen


def _disk_destroy(p: Parsed, server: Optional[str]) -> bool:
    for argv in p.argvs:
        cmd, args = _cmd(argv), argv[1:]
        if cmd in _DISK_TOOLS or cmd == "mkfs" or cmd.startswith("mkfs."):
            return True
        if cmd == "dd" and any(t.startswith("of=/dev/") and t != "of=/dev/null" for t in args):
            return True
        if cmd == "mkswap" and any(a.startswith("/dev/") for a in args):  # mkswap /swapfile is an ordinary swap file
            return True
        if cmd == "cryptsetup":
            words = {a.lower() for a in args}
            if _CRYPT_DESTROY & words or ("open" in words and "plain" in _option_values(args, "M", ("type",))):
                return True  # open --type plain is the classic recipe to wipe a disk
    return False


_MYSQL_CLIENTS = {"mysql", "mariadb"}
_SQL_READ = {"select", "show", "explain", "describe", "desc"}
_SQL_WRITE = re.compile(
    r"\b(insert|update|delete|drop|truncate|alter|create|grant|revoke|rename|replace|load|kill|call|lock"
    r"|flush|handler|outfile|dumpfile|install|uninstall|shutdown|reset|purge)\b",
    re.I,
)


# repeats gate.agent_ro_gate.mysql_sql (the gateway is one stdlib file for py3.6 and cannot import from
# sysadmin): a change in one is repeated in the other
def _sql_of(args: Argv) -> Optional[str]:
    """The first word of the SQL of -e/--execute (the quotes are already stripped)."""
    for i, arg in enumerate(args):
        if arg in ("-e", "--execute"):
            return args[i + 1] if i + 1 < len(args) else ""
        if arg.startswith("--execute="):
            return arg.split("=", 1)[1]
        if arg.startswith("-e") and not arg.startswith("--") and len(arg) > 2:
            return arg[2:]
    return None


_MYSQLADMIN = {"mysqladmin", "mariadb-admin"}
_MYSQLIMPORT = {"mysqlimport", "mariadb-import"}
_MYSQLADMIN_READ = {"status", "extended-status", "processlist", "variables", "version", "ping"}
_MYSQLADMIN_VALUED = {"-u", "-h", "-P", "-S", "-i", "-c", "--user", "--host", "--port", "--socket", "--sleep",
                      "--count", "--protocol"}
_SQL_PAYLOAD = re.compile(r"""(?<!\S)(?:-[A-Za-z]*e|--execute=?)\s*("[^"]*"|'[^']*'|\S+)""")


def _verbs(args: Argv) -> Argv:
    """The words of mysqladmin without options and their values (-u root, -h host …)."""
    verbs, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a in _MYSQLADMIN_VALUED
        else:
            verbs.append(a)
    return verbs


def _multi_statement(raw: str) -> bool:
    """-e holds more than one statement or a client command (\\!, \\.): in quotes they cannot be told apart — refused."""
    for match in _SQL_PAYLOAD.finditer(raw):
        payload = match.group(1)
        if payload[0] in "\"'" and len(payload) > 1:
            payload = payload[1:-1]
        payload = re.sub(r";\s*$", "", payload)  # one `;` at the end is allowed
        if ";" in payload or "\\" in payload:
            return True
    return False


def _db_write(p: Parsed, server: Optional[str]) -> bool:
    for argv in map(_live, p.argvs):
        if _cmd(argv) in _MYSQLIMPORT:
            return True
        if _cmd(argv) in _MYSQLADMIN and any(v.lower() not in _MYSQLADMIN_READ for v in _verbs(argv[1:])):
            return True
    clients = [_live(a) for a in p.argvs if _cmd(a) in _MYSQL_CLIENTS]
    if not clients:
        return False
    if _SQL_WRITE.search(p.flat) or _multi_statement(p.raw or p.flat):
        return True
    if any(t.startswith("--init-command") for a in clients for t in a):  # runs before -e
        return True
    return any((_sql_of(a[1:]) or "").lower() not in _SQL_READ for a in clients)


_ACCESS_FILES = ("sshd_config", "authorized_keys", "/.ssh", "/etc/passwd", "/etc/shadow")
_SSH_UNITS = {"ssh", "sshd", "ssh.service", "sshd.service", "ssh.socket"}
_SSH_PORTS = {"22", "ssh", "openssh"}
_INBOUND_CHAINS = {"INPUT", "PREROUTING"}  # -t raw|mangle PREROUTING … -j DROP cuts the inbound side as INPUT does


def _drops_ssh(args: Argv) -> bool:
    """iptables -A|-I INPUT … --dport 22|ssh … -j DROP|REJECT."""
    chains = _option_values(args, "AI", ("append", "insert"))
    ports = {p.lower() for v in _option_values(args, "", ("dport", "destination-port", "dports", "destination-ports"))
             for p in v.split(",")}
    targets = _option_values(args, "j", ("jump",))
    return bool(_INBOUND_CHAINS & set(chains)) and bool(_SSH_PORTS & ports) and bool({"DROP", "REJECT"} & set(targets))


def _ufw_denies_ssh(args: Argv) -> bool:
    """ufw deny|reject 22|ssh|OpenSSH, ufw default deny|reject [incoming]."""
    words = [a.lower() for a in args]
    if not {"deny", "reject"} & set(words) or "delete" in words:  # ufw delete deny 22 lifts a prohibition
        return False
    if "default" in words:
        return not {"outgoing", "routed"} & set(words)  # no direction means incoming
    return any(w.split("/")[0] in _SSH_PORTS for w in words)




def _home():
    """(alias, ip) of the factory's own host from the inventory; (None, None) when it cannot be read —
    then the stage rules apply to every server and every addressed source counts as covering the stage."""
    from sysadmin import inventory

    try:
        alias, host = inventory.home()
        return alias, ipaddress.ip_address(host)
    except (inventory.InventoryError, ValueError):
        return None, None


def _is_stage(server: Optional[str]) -> bool:
    alias, _ = _home()
    return alias is None or server == alias

_ANY_SOURCES = {"any", "0/0", "0.0.0.0", "::"}  # "everyone" as a word and the forms ip_network does not read as "everyone"


def _broad_source(value: str) -> bool:
    """The source is "everyone" or a network that includes the factory host (the inventory's home): a block would cut
    off the monitor and the sysadmin's session. A negation (! -s X, -s !X) is "everyone but X": both the owner and the
    factory host."""
    for source in value.lower().split(","):
        if source.startswith("!") or source in _ANY_SOURCES:
            return True
        try:
            net = ipaddress.ip_network(source, strict=False)
        except ValueError:
            continue  # a host name: a block by address
        stage_ip = _home()[1]
        if net.prefixlen == 0 or stage_ip is None or stage_ip in net:
            return True
    return False


def _covers_ssh(spec: str) -> bool:
    """The rule's ports (22, ssh, 80,443, 20:30, firewalld's 20-30, 22/tcp) include SSH; an unreadable range counts as including it."""
    for part in spec.lower().split(","):
        part = part.split("/")[0]
        if re.fullmatch(r"\d+-\d+", part):  # a firewalld range; a service name with a hyphen (my-app) is not a range
            part = part.replace("-", ":")
        lo, colon, hi = part.partition(":")
        if part in _SSH_PORTS:
            return True
        if colon:
            try:
                if int(lo or 0) <= 22 <= int(hi or 65535):
                    return True
            except ValueError:
                return True
    return False


_DPORT_OPTIONS = ("dport", "destination-port", "dports", "destination-ports", "ports")


def _negated(args: Argv, options) -> bool:
    """A "!" stands before the option (! -s X, ! --dport 8080) or the value starts with "!" (-s !X, --dport ! 8080)."""
    flags = {"--" + o for o in options} | {"-" + o for o in options if len(o) == 1}
    before = any(a == "!" and nxt in flags for a, nxt in zip(args, args[1:]))
    longs = tuple(o for o in options if len(o) > 1)
    shorts = "".join(o for o in options if len(o) == 1)
    return before or any(v.startswith("!") for v in _option_values(args, shorts, longs))


def _drops_all(args: Argv) -> bool:
    """iptables -A|-I INPUT|PREROUTING … -j DROP|REJECT without -s/--source, with a negation or with the source
    "everyone"/the factory host — except rules for ports without 22 only (closing 8080 is no self-lockout; ! --dport
    8080 is all the others)."""
    chains = _option_values(args, "AI", ("append", "insert"))
    if not any(c.upper() in _INBOUND_CHAINS for c in chains):
        return False
    if not any(t.upper() in ("DROP", "REJECT") for t in _option_values(args, "j", ("jump",))):
        return False
    ports = _option_values(args, "", _DPORT_OPTIONS)
    if ports and not _negated(args, _DPORT_OPTIONS) and not any(map(_covers_ssh, ports)):
        return False
    sources = _option_values(args, "s", ("source", "src"))
    return not sources or _negated(args, ("s", "source", "src")) or any(map(_broad_source, sources))


_UFW_EXTENDED = {"in", "out", "on", "to", "proto", "from"}  # the extended form of a ufw rule


def _ufw_denies_all(args: Argv) -> bool:
    """ufw [insert N] deny|reject: the extended form without from (that is from any) or from any|0.0.0.0/0|::/0|a network
    with the factory host, except port/app without 22; any deny out (on the factory host it cuts off Telegram and ssh
    to the targets); the simple form — a range with 22;
    ufw default deny outgoing."""
    words = [a.lower() for a in args]
    if not {"deny", "reject"} & set(words) or "delete" in words:
        return False
    if "default" in words:
        return "outgoing" in words
    if "out" in words:
        return True
    if not _UFW_EXTENDED & set(words):  # ufw deny 8080, ufw deny Nginx, ufw deny 1:65535/tcp
        verb = next(i for i, w in enumerate(words) if w in ("deny", "reject"))
        rest = [w for w in words[verb + 1:] if w not in ("log", "log-all")]  # ufw deny log 1:65535/tcp
        return any(map(_covers_ssh, rest[:1]))
    for key in ("port", "app"):
        at = words.index(key) + 1 if key in words else len(words)
        if at < len(words) and not _covers_ssh(words[at]):
            return False
    if "from" not in words:
        return True
    at = words.index("from") + 1
    return at < len(words) and _broad_source(words[at])


_FIREWALLD_DROP_ZONES = {"drop", "block"}


_FIREWALLD_DROP_TARGETS = {"drop", "reject", "%%reject%%"}


def _firewalld_lockout(args: Argv) -> bool:
    """firewall-cmd --panic-on, --set-default-zone=drop|block, --remove-service=ssh, --remove-port=22/tcp|20-30/tcp,
    --zone=drop|block --change-interface|--add-interface|--change-zone (the whole interface into a refusing zone),
    --set-target=DROP|REJECT (the interface's zone drops everything not allowed explicitly)."""
    if "--panic-on" in args:
        return True
    zones = _option_values(args, "", ("set-default-zone",))
    removed = _option_values(args, "", ("remove-service", "remove-port"))
    if bool({z.lower() for z in zones} & _FIREWALLD_DROP_ZONES) or any(map(_covers_ssh, removed)):
        return True
    to_zone = {z.lower() for z in _option_values(args, "", ("zone",))}
    if to_zone & _FIREWALLD_DROP_ZONES and _option_values(args, "", ("change-interface", "add-interface", "change-zone")):
        return True
    return any(t.lower() in _FIREWALLD_DROP_TARGETS for t in _option_values(args, "", ("set-target",)))


def _access_lockout(p: Parsed, server: Optional[str]) -> bool:
    if any(f in p.flat for f in _ACCESS_FILES):
        return True
    for argv in p.argvs:
        cmd, args = _cmd(argv), argv[1:]
        if cmd in _IPTABLES and (_drops_ssh(args) or _drops_all(args)):
            return True
        if cmd == "ufw" and (_ufw_denies_ssh(args) or _ufw_denies_all(args)):
            return True
        if cmd == "firewall-cmd" and _firewalld_lockout(args):
            return True
        if cmd in ("passwd", "chpasswd", "userdel", "deluser"):
            return True
        if cmd == "usermod" and (
            _short_has(args, "Lpse") or {"--lock", "--password", "--shell", "--expiredate"} & set(args)
        ):
            return True
        if cmd == "chage" and (_short_has(args, "E") or any(a.startswith("--expiredate") for a in args)):
            return True
        if cmd in ("pkill", "killall") and {"ssh", "sshd"} & set(args):
            return True
        if cmd in ("rm", "mv", "rmdir", "ln") and any(
            a == "/etc/ssh" or a.startswith("/etc/ssh/") for a in map(posixpath.normpath, args) if a.startswith("/")
        ):
            return True
        if (
            cmd == "systemctl"
            and {"stop", "disable", "mask", "kill"} & set(args)
            and any(a.startswith("ssh") for a in args)
        ):
            return True
        if cmd == "service" and len(args) >= 2 and args[0] in _SSH_UNITS and args[1] == "stop":
            return True
        if cmd in ("ssh", "sshd") and "init.d" in argv[0] and "stop" in args:  # /etc/init.d/ssh stop
            return True
    return False


_NETWORKD_VERBS = {"restart", "stop", "try-restart", "reload-or-restart", "kill"}


def _networkd_restart(p: Parsed, server: Optional[str]) -> bool:
    for argv in p.argvs:
        cmd, args = _cmd(argv), argv[1:]
        if cmd == "systemctl" and _NETWORKD_VERBS & set(argv) and any(t.startswith("systemd-networkd") for t in argv):
            return True
        if cmd == "service" and len(args) >= 2 and args[0].startswith("systemd-networkd") and args[1] in _NETWORKD_VERBS:
            return True
        if cmd == "netplan" and "apply" in args:  # re-applies the config through networkd — ip rules are lost
            return True
        positional = [a for a in args if not a.startswith("-")]
        if cmd == "ip" and positional[:1] == ["rule"] and positional[1:2] and positional[1] in ("flush", "del", "delete"):
            return True
    return False


# --- the deployment's own rules (review 04.11.2026) ---------------------------------------------------------------
# What only one deployment has — its applications' code trees, the hosts its servers reach only through a SOCKS
# proxy, its tools that run only with --dry-run — is no part of this public list: host/sysadmin_rules.json of the
# overlay, next to host.json, names them. No file — none of them; a file that does not parse refuses every command.
RULES_NAME = "sysadmin_rules.json"
_TREE_RE = re.compile(r"/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*")
_LABEL_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_WORD_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}")


class Deployment(NamedTuple):
    code_trees: tuple
    socks_only: Optional["re.Pattern"]
    dry_run_only: tuple  # (program, subcommand)
    problem: str = ""


def rules_path() -> str:
    return os.path.join(os.path.dirname(naming.host_json_path()), RULES_NAME)


@functools.lru_cache(maxsize=4)
def deployment(path: Optional[str] = None) -> Deployment:
    path = path or rules_path()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return Deployment((), None, ())
    except (OSError, ValueError) as exc:
        return Deployment((), None, (), f"{path}: unreadable ({exc.__class__.__name__})")
    trees, hosts, dry = (data.get(k, []) if isinstance(data, dict) else None
                         for k in ("code_trees", "socks_only_hosts", "dry_run_only"))
    if (
        not isinstance(data, dict) or data.get("schema_version") != 1
        or set(data) - {"schema_version", "code_trees", "socks_only_hosts", "dry_run_only"}
        or not isinstance(trees, list) or not all(isinstance(t, str) and _TREE_RE.fullmatch(t) for t in trees)
        or not isinstance(hosts, list) or not all(isinstance(h, str) and _LABEL_RE.fullmatch(h) for h in hosts)
        or not isinstance(dry, list) or not all(
            isinstance(d, dict) and set(d) == {"program", "subcommand"}
            and all(isinstance(d[k], str) and _WORD_RE.fullmatch(d[k]) for k in d) for d in dry)
    ):
        return Deployment((), None, (), f"{path}: expected {{schema_version: 1, code_trees, socks_only_hosts, "
                                        f"dry_run_only}}")
    socks = (re.compile(r"(?:^|[/.@])(" + "|".join(map(re.escape, hosts)) + r")[a-z0-9-]*\.[a-z]", re.I)
             if hosts else None)
    return Deployment(tuple(trees), socks, tuple((d["program"], d["subcommand"]) for d in dry))


def _rules_unreadable(p: Parsed, server: Optional[str]) -> bool:
    return bool(deployment().problem)


def _dry_run_only(p: Parsed, server: Optional[str]) -> bool:
    return any(
        any(posixpath.basename(t).startswith(program) for t in a) and subcommand in a
        and "--dry-run" not in _live(a)
        for program, subcommand in deployment().dry_run_only
        for a in p.argvs
    )


_PUBLIC_CODE_TREES = ("/var/www", "/var/lib/mysql")  # code (git and deploys only) and database files; the deployment's trees are above
_BACKUP_TREES = ("/backups",)  # new copies may be added; erasing and overwriting may not


def _code_write(p: Parsed, server: Optional[str]) -> bool:
    return _writes_into(p, _PUBLIC_CODE_TREES + deployment().code_trees, _COPIERS)


def _backup_write(p: Parsed, server: Optional[str]) -> bool:
    return _writes_into(p, _BACKUP_TREES, set())


# the agents' cage: the agents' home and code, sudoers, wrappers, units, the execution journal, the gateway and the
# home of agent-ro on targets. Frozen: only the users were renamed since — the names come from kd.naming, the
# contents and the order are the same.
_BASE = naming.base_user()
_EXEC = naming.service_user("exec")
_SYSADMIN = naming.agent_user("sysadmin")
_CAGE_TREES = (
    "/etc/sudoers", "/etc/sudoers.d", "/etc/claude-code", "/srv/digipaddock-agents", "/opt/digipaddock",
    "/opt/digipaddock-releases", naming.home(_EXEC), "/etc/" + _EXEC, "/etc/digipaddock", naming.home(_BASE),
    naming.home(_SYSADMIN),  # the sysadmin's home
    # in the home of agent-ro — the hidden files (.ssh, .my.cnf) and the directory itself: as a write target
    # (cp … /home/agent-ro/) and through the parent (_encloses: chmod, mv, rm -r); other files there are not the
    # cage: the safe twin is "rm -rf /home/agent-ro/tmp-x"
    "/home/agent-ro/.*", "/home/agent-ro/",
    "/usr/local/bin/ssh-ro", "/usr/local/bin/ssh-exec", "/usr/local/bin/agent-launch",
    "/usr/local/bin/agent-ro-gate",
    "/var/log/digipaddock",
    # units and commands of the monitor, the usage accounting and the alert (kd-monitor …; commands carry the host's prefix)
    "/etc/systemd/system/digipaddock-*", "/usr/local/bin/" + naming.command("kd-*"),
    # symlinks of the cage's enabled units: rm …/timers.target.wants/digipaddock-monitor.timer would switch the timer off
    "/etc/systemd/system/*.wants/digipaddock-*",
    # the security snapshot (root through sudo on targets) and the monitor's state (the baseline, trusted addresses)
    "/usr/local/bin/agent-sec-snapshot", "/var/lib/digipaddock",
)
_ADMIN_GROUPS = {"sudo", "admin", "wheel"}
_CAGE_USERS = {_BASE, _EXEC, naming.TARGET_USER, _SYSADMIN}
# the cage's units and processes on the factory host: the session host, agent sessions, the cabinet, the monitor
_CAGE_UNIT_PREFIXES = ("digipaddock-", naming.command("kd-"))
_UNIT_STOP = {"stop", "disable", "mask", "kill", "restart", "try-restart", "reload-or-restart",
              "try-reload-or-restart", "freeze"}


def _cage_unit(name: str) -> bool:
    """The name of a unit or process of the cage; a glob (digipaddock-*, *) — when it can cover one."""
    base = posixpath.basename(name)
    glob = _GLOB.search(base)
    if glob:
        return any(prefix.startswith(base[:glob.start()]) for prefix in _CAGE_UNIT_PREFIXES)
    return base.startswith(_CAGE_UNIT_PREFIXES)


def _stops_stage_cage(cmd: str, args: Argv) -> bool:
    """On the factory host: stopping the cage's units and processes (systemctl, service, pkill/killall, -u <base user>)."""
    operands = [a for a in args if not a.startswith("-")]
    if cmd == "systemctl" and _UNIT_STOP & set(args):
        return any(_cage_unit(a) for a in operands if a not in _UNIT_STOP)
    if cmd == "service":
        return len(operands) >= 2 and _cage_unit(operands[0]) and operands[1] in _UNIT_STOP
    if cmd in ("pkill", "killall"):
        users = {u for v in _option_values(args, "uU", ("user", "euid", "uid")) for u in v.split(",")}
        return bool(users & _CAGE_USERS) or any(map(_cage_unit, operands))
    return False


def _cage_write(p: Parsed, server: Optional[str]) -> bool:
    if writes_into(p, _CAGE_TREES):
        return True
    for argv in (_strip_redirects(_live(a)) for a in p.argvs):
        cmd, args = _cmd(argv), argv[1:]
        if _is_stage(server) and _stops_stage_cage(cmd, args):
            return True
        operands = [a for a in args if not a.startswith("-")]
        recursive_rm = cmd == "rm" and (_short_has(args, "rR") or "--recursive" in args)
        if (cmd in _ATTR_CHANGERS or recursive_rm) and any(_encloses(a, _CAGE_TREES) for a in operands):
            return True
        if cmd == "mv" and any(_encloses(a, _CAGE_TREES) for a in _mv_sources(args)):
            return True
        if cmd in ("usermod", "useradd"):  # -G/-aG/--groups, -g/--gid
            groups = {g for v in _option_values(args, "Gg", ("groups", "gid")) for g in v.split(",")}
            if groups & _ADMIN_GROUPS:
                return True
        if cmd in ("gpasswd", "adduser", "addgroup") and {a.split("=")[-1] for a in args} & _ADMIN_GROUPS:
            return True
        if cmd == "passwd" and set(operands) & _CAGE_USERS:
            return True
        if cmd == "visudo" and not (_short_has(args, "c") or "--check" in args):
            return True
    return False


_CRON_TREES = ("/etc/cron.d", "/etc/crontab", "/var/spool/cron")
_CRON_UNITS = {"cron", "crond", "cron.service", "crond.service"}


def _cron_wipe(p: Parsed, server: Optional[str]) -> bool:
    """crontab -r; removing, moving and overwriting system and user crontabs; stopping cron."""
    if _writes_into(p, _CRON_TREES, set(), exempt=False):  # a new file may be put there with cp
        return True
    for argv in map(_live, p.argvs):
        cmd, args = _cmd(argv), argv[1:]
        if cmd == "crontab" and _short_has(args, "r"):
            return True
        if cmd == "systemctl" and {"stop", "disable", "mask"} & set(args) and _CRON_UNITS & set(args):
            return True
        if cmd == "service" and len(args) >= 2 and args[0] in _CRON_UNITS and args[1] == "stop":
            return True
    return False


_FORCE = {"--force", "--force-with-lease", "--force-if-includes", "--delete", "--mirror"}  # + deleting a branch


def _git_force_push(p: Parsed, server: Optional[str]) -> bool:
    for argv in p.argvs:
        if _cmd(argv) == "git" and "push" in argv:
            rest = argv[argv.index("push") + 1:]
            if any(
                t in _FORCE or t.startswith(("--force-with-lease=", "+", ":")) or _short_has([t], "fd")
                for t in rest
            ):
                return True
    return False


_HTTP_CLIENTS = {"curl", "wget", "http", "https", "xh"}


def _socks_only_direct(p: Parsed, server: Optional[str]) -> bool:
    hosts = deployment().socks_only
    if hosts is None:
        return False
    for segment, argv in zip(p.segments, p.argvs):
        if (
            _cmd(argv) in _HTTP_CLIENTS
            and any(hosts.search(t) for t in argv)
            and not any("socks" in t.lower() for t in _live(segment))
        ):
            return True
    return False


_KILL_VALUED = {"-s", "-n", "--signal"}  # kill -s SIGNAL, kill -n NUMBER


def _kill_targets(args: Argv) -> Argv:
    """The pids of kill: without the signal (-9, -TERM, -s KILL). A first "-1" before a pid is the signal HUP
    (kill -1 123), while without a pid and after "--" it is pid -1, that is all processes."""
    targets, i = [], 0
    while i < len(args):
        a = args[i]
        if a == "--":
            return targets + args[i + 1:]
        if a in _KILL_VALUED:
            i += 2
            continue
        if a.startswith("-") and (i == 0 and len(args) > 1 or not a[1:].isdigit()):
            i += 1  # a signal as the first word (-9, -KILL, -1 before a pid) or an option (-l, -q); a later -N is a pid
            continue
        targets.append(a)
        i += 1
    return targets


def _kill_all(p: Parsed, server: Optional[str]) -> bool:
    for argv in map(_live, p.argvs):
        cmd, args = _cmd(argv), argv[1:]
        if cmd == "killall5":  # SysV: a signal to all processes
            return True
        if cmd == "kill" and {"1", "-1"} & set(_kill_targets(args)):
            return True
        if cmd in ("pkill", "killall"):
            users = {u for v in _option_values(args, "uU", ("user", "euid", "uid")) for u in v.split(",")}
            if users & {"root", "0"}:
                return True
    return False


_POWER = {"reboot", "shutdown", "poweroff", "halt"}
_POWER_TARGET = re.compile(r"^(reboot|poweroff|halt|kexec|soft-reboot|rescue|emergency)")


def _stage_power(p: Parsed, server: Optional[str]) -> bool:
    if not _is_stage(server):
        return False
    if "/proc/sysrq-trigger" in p.flat:  # echo b > /proc/sysrq-trigger
        return True
    for argv in p.argvs:
        cmd, args = _cmd(argv), set(argv[1:])
        if cmd in _POWER:
            return True
        if cmd == "systemctl" and any(_POWER_TARGET.match(posixpath.basename(a)) for a in args if not a.startswith("-")):
            return True  # reboot, start reboot.target, isolate poweroff.target, rescue …
        if cmd == "kexec" and (_short_has(argv[1:], "e") or "--exec" in args):
            return True
        if cmd in ("init", "telinit") and {"0", "6"} & args:
            return True
    return False


RULES = [
    Rule("deployment-rules", "правила внедрения host/sysadmin_rules.json не читаются — ни одна команда не идёт, пока "
         "их не починят", _rules_unreadable),
    Rule("firewall-flush", "сброс или подмена правил фаервола (iptables -F/-X/-P, iptables-restore|apply, "
         "nft flush|delete|destroy|-f, ufw disable|reset, netfilter-persistent flush; ufw enable — на сервере без "
         "правил отрежет всех) — риск потерять доступ", _firewall_flush),
    Rule("rsync-delete", "rsync --delete стирает данные (например, в /var/www)", _rsync_delete),
    Rule("rm-critical", "рекурсивное удаление или перенос /, системных каталогов, /var/www, /backups, /var/lib/mysql, "
         "/etc, /opt (в том числе масками)", _rm_critical),
    Rule("find-destructive", "find с -delete/-exec/-ok/-fprint — удаляет или выполняет что угодно; удалять — явным "
         "rm по списку от ssh-ro", _find_destructive),
    Rule("disk-destroy", "разметка, форматирование и затирание дисков (mkfs, mke2fs, mkswap, dd of=/dev/…, wipefs, "
         "blkdiscard, fdisk, parted, cryptsetup luksFormat|erase|open --type plain, lvremove|vgremove|pvremove)",
         _disk_destroy),
    Rule("db-write", "запись в БД через агента запрещена: только mysql -e \"SELECT…\" и mysqladmin status|processlist|"
         "variables; изменения выполняет владелец", _db_write),
    Rule("access-lockout", "sshd_config, authorized_keys, passwd, блокировка пользователей, остановка ssh, "
         "DROP/REJECT порта 22 (iptables, ufw deny ssh, ufw default deny), DROP/REJECT INPUT|PREROUTING без "
         "источника, с отрицанием или для всех/stage, ufw deny без from или from any/stage, ufw deny out, "
         "firewall-cmd --panic-on|--set-default-zone=drop|--remove-service=ssh, интерфейс в зону drop/block, "
         "--set-target=DROP|REJECT — риск потерять доступ", _access_lockout),
    Rule("networkd-restart", "рестарт systemd-networkd сносит ip rule от wg-quick", _networkd_restart),
    Rule("dry-run-only", "эта команда приложения — только с --dry-run (правила внедрения, host/sysadmin_rules.json)",
         _dry_run_only),
    Rule("code-write", "код в /var/www и в деревьях приложений внедрения меняется только через git и деплой; файлы "
         "/var/lib/mysql не трогать", _code_write),
    Rule("backup-write", "бэкапы в /backups не удалять и не перезаписывать (новые копии класть можно)", _backup_write),
    Rule("cage-write", "клетку агентов не трогать: sudoers, /etc/claude-code, /srv/digipaddock-agents, /opt/digipaddock, "
         f"дома {_BASE}/{_EXEC}/agent-ro, /etc/digipaddock (выключатели), обёртки в /usr/local/bin, юниты "
         "digipaddock-* и их симлинки в *.wants, журнал /var/log/digipaddock, снимок agent-sec-snapshot, /var/lib/digipaddock; на stage — "
         "остановка юнитов и процессов digipaddock-* и команд kd-*; группы sudo/admin/wheel, passwd агентов, visudo "
         "(кроме -c)", _cage_write),
    Rule("cron-wipe", "crontab -r, удаление, перенос и перезапись /etc/cron.d, /etc/crontab, /var/spool/cron, "
         "остановка cron — кроны приложений пропали бы молча", _cron_wipe),
    Rule("git-force-push", "git push --force", _git_force_push),
    Rule("socks-only", "к этим хостам — только через SOCKS-прокси (curl --socks5-hostname …; правила внедрения)",
         _socks_only_direct),
    Rule("kill-all", "kill/pkill/killall по pid 1, -1 или всем процессам root (pkill -u root, killall5) — "
         "остановит всё на сервере", _kill_all),
    Rule("stage-power", "хост фабрики (сайты, демо, дом агентов) перезагружается только руками в окно",
         _stage_power),
]


def check(command: str, server: Optional[str] = None) -> Optional[Rule]:
    """The first rule that fired, or None."""
    parsed = parse(command)
    for rule in RULES:
        if rule.match(parsed, server):
            return rule
    return None
