"""The exec gate's policy: one module for both sides — ``kd-admin-propose`` / ``kd-admin-propose-action``
on the factory host and ``agent-exec-gate`` on a target, where it is installed next to the gate as
``/usr/local/bin/agent-exec-policy``. Python 3.6+ and only stdlib (AlmaLinux 8 targets).

It is a policy, not a signature check: it holds in both gate modes (observe and enforce) and on every way in (a plan
step, a critical action, a legacy command without an envelope).

- Rule 1, self-protection (``protect``): a command that touches the gate, its state, ssh, sudo or accounts, or
  carries an inline interpreter, a decoder or an archive into a system root, is refused — the gate and access to the
  server change only from the owner's machine (deploy_target.sh).
- Rule 2, the content class matches the signature kind (``plan_step``): a plan step whose command ``classify``
  recognizes as critical is refused — the owner signs it separately, as an exact command (kd-admin-propose-action).

What it does not close (docs/security-model.md): persistence (a unit or a cron job that later runs something as root —
legitimate plans install units too) and indirect writes through variables with an empty literal prefix.

The shell parse below is the guards' and the denylist's (``kd.shell`` re-exports it; moved from
agents/sysadmin/sysadmin/denylist.py 06.10.2026); the critical classifier is ``sysadmin/critical.py``'s,
which now wraps it.

Parsing of a shell command for the guards and the denylist (the behaviour did not change).
"""

import fnmatch
import posixpath
import re
from typing import List, NamedTuple, Optional, Tuple

Argv = List[str]


class Parsed(NamedTuple):
    flat: str  # the command without quotes and backslashes
    segments: List[Argv]  # simple commands by the boundaries of shell operators
    argvs: List[Argv]  # the same without wrappers (sudo, env, bash -c …)
    raw: str = (
        ""  # the command as it is (after $'…' → '…'): for parsing what is inside quotes
    )


_QUOTES = re.compile(r"[\"'\\]")
_SLASHES = re.compile(r"/{2,}")
_OPERATORS = re.compile(r"\$\(|`|&&|\|\||[;&|()\n]")
_BRACES = re.compile(
    r"(?<!\S)[{}](?!\S)"
)  # { and } as separate words (not the {} of find/xargs and not ${x})
_QUOTED = re.compile(r"""'[^']*'|"(?:[^"\\]|\\.)*"|\\.""", re.S)
_ANSI_QUOTE = re.compile(r"\$(['\"])")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DURATION = re.compile(r"^\d+(\.\d+)?[smhd]?$")

# a wrapper → its options that take a value as a separate word
_WRAPPERS = {
    "sudo": {
        "-u",
        "-g",
        "-U",
        "-h",
        "-p",
        "-C",
        "-D",
        "-r",
        "-t",
        "-T",
        "--user",
        "--group",
        "--host",
        "--chdir",
        "--prompt",
        "--role",
        "--type",
    },
    "doas": {"-u", "-C"},
    "su": {"-s", "-g", "-G", "--shell", "--group", "--supp-group"},
    "runuser": {"-u", "-g", "-G", "-s", "--user", "--group", "--supp-group", "--shell"},
    "env": {"-u", "-C", "-S"},
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "-k"},
    "xargs": {"-I", "-n", "-P", "-L", "-d", "-E", "-s", "-a"},
    "systemd-run": {
        "-u",
        "--unit",
        "-p",
        "--property",
        "-E",
        "--setenv",
        "--uid",
        "--gid",
        "--description",
        "--slice",
        "-M",
        "--machine",
        "--working-directory",
        "-H",
        "--host",
    },
    "nohup": set(),
    "time": set(),
    "command": set(),
    "exec": set(),
    "setsid": set(),
    "sh": {"-o", "-O"},
    "bash": {"-o", "-O"},
    "dash": {"-o", "-O"},
    "zsh": {"-o", "-O"},
    "chroot": {"--userspec", "--groups"},
    "flock": {"-w", "-E", "--wait", "--timeout", "--conflict-exit-code"},
    "nsenter": {"-t", "-S", "-G", "--target", "--setuid", "--setgid"},
    "unshare": {
        "-S",
        "-G",
        "-R",
        "-w",
        "--setuid",
        "--setgid",
        "--root",
        "--wd",
        "--propagation",
        "--map-user",
        "--map-group",
        "--map-users",
        "--map-groups",
        "--setgroups",
    },
    "taskset": set(),
    "watch": {"-n", "-q", "--interval", "--equexit"},
    "strace": {
        "-a",
        "-b",
        "-e",
        "-E",
        "-I",
        "-o",
        "-O",
        "-p",
        "-P",
        "-s",
        "-S",
        "-u",
        "-U",
        "-X",
        "--output",
        "--attach",
        "--user",
        "--env",
    },
    "script": {
        "-E",
        "-I",
        "-O",
        "-B",
        "-T",
        "-m",
        "-o",
        "--echo",
        "--log-in",
        "--log-out",
        "--log-io",
        "--log-timing",
        "--logging-format",
        "--output-limit",
    },
    "chrt": {
        "-T",
        "-P",
        "-D",
        "--sched-runtime",
        "--sched-period",
        "--sched-deadline",
    },  # the priority is a number
    "xtables-legacy-multi": set(),
    "xtables-nft-multi": set(),  # xtables-nft-multi iptables -F
    # the keywords of compound commands: the real command follows them
    "!": set(),
    "if": set(),
    "while": set(),
    "until": set(),
    "then": set(),
    "else": set(),
    "elif": set(),
    "do": set(),
    "eval": set(),
    "builtin": set(),
    "function": set(),
    "coproc": set(),
}
# the command comes after -c: su [user] -c, script -qc 'cmd' /dev/null, flock [FILE] -c 'cmd' (and flock -c 'cmd' FILE)
_SU_LIKE = {"su", "runuser", "script", "flock"}
# a wrapper → how many of its positional arguments come before the command: chroot NEW_ROOT, flock FILE, taskset MASK
_POSITIONAL = {"chroot": 1, "flock": 1, "taskset": 1}


def _neutralize(command: str) -> str:
    """Inside quotes and after a backslash <, > and # lose their force: neither a redirection nor a comment."""

    def blank(m):
        text = m.group(0)
        if text[0] == "\\":
            return "_" if text[1:] in ("#", "<", ">") else text
        return text.replace("<", "_").replace(">", "_").replace("#", "_")

    return _QUOTED.sub(blank, command)


def parse(command: str) -> Parsed:
    raw = _ANSI_QUOTE.sub(r"\1", command)  # $'rm' and $"rm" are just rm
    flat = _SLASHES.sub("/", _QUOTES.sub("", _neutralize(raw)))
    text = _BRACES.sub(" ; ", _OPERATORS.sub(" ; ", flat))
    segments = [s.split() for s in text.split(";") if s.split()]
    return Parsed(flat, segments, [command_argv(s) for s in segments], raw)


def _live(argv: Argv) -> Argv:
    """The words before a comment (#…): bash does not see what follows, so it cannot hold exemptions."""
    for i, word in enumerate(argv):
        if word.startswith("#"):
            return argv[:i]
    return argv


def command_argv(segment: Argv) -> Argv:
    """The argv of the real command: without VAR=…, sudo/env/timeout/bash -c … and their options."""
    i = 0
    while i < len(segment):
        word = segment[i]
        if _ASSIGNMENT.match(word):
            i += 1
            continue
        valued = _WRAPPERS.get(posixpath.basename(word))
        if valued is None:
            return segment[i:]
        if (
            posixpath.basename(word) in _SU_LIKE
        ):  # su [user] -c CMD, runuser user -c CMD
            j, users = (
                i + 1,
                1,
            )  # the search for -c ends at `--` and at the first word of the nested command
            while j < len(segment) and segment[j] != "--":
                token = segment[j]
                if token.startswith("--command="):
                    return command_argv([token.split("=", 1)[1]] + segment[j + 1 :])
                if token == "--command" or (
                    token.startswith("-")
                    and not token.startswith("--")
                    and token.endswith("c")
                ):
                    return command_argv(segment[j + 1 :])
                if token.startswith("-"):
                    if token in valued:
                        users = 0 if token in ("-u", "--user") else users
                        j += 1  # the option's value
                elif users:
                    users -= 1  # su [user]
                else:
                    break
                j += 1
        positional = _POSITIONAL.get(posixpath.basename(word), 0)
        # for wrappers with positional arguments a number (taskset 3 …) is the argument itself, not a duration
        i = _skip_options(segment, i + 1, valued, durations=not positional)
        if positional:  # options may follow the argument too: flock FILE -c 'cmd'
            i = _skip_options(segment, i + positional, valued, durations=False)
    return []


def _skip_options(segment: Argv, i: int, valued, durations: bool = True) -> int:
    """The index of the first word after the wrapper's options (with their values), VAR=… and durations (timeout 10, nice -n 5)."""
    while i < len(segment) and (
        segment[i].startswith("-")
        or (durations and _DURATION.match(segment[i]))
        or _ASSIGNMENT.match(segment[i])
    ):
        i += 2 if segment[i] in valued else 1
    return i


def _cmd(argv: Argv) -> str:
    return posixpath.basename(argv[0]) if argv else ""


def _short_has(args: Argv, letters: str) -> bool:
    """One of the short options (-xyz) holds one of the letters."""
    return any(
        a.startswith("-")
        and not a.startswith("--")
        and any(c in letters for c in a[1:])
        for a in args
    )


# --- option values ---------------------------------------------------------


def _mv_sources(args: Argv) -> Argv:
    positional = [a for a in args if not a.startswith("-")]
    target = _option_values(args, "t", ("target-directory",))
    if target:  # mv -t DIR src…: every operand except DIR is a source
        return [a for a in positional if a not in target]
    return positional[:-1]


def _option_values(args: Argv, shorts: str, longs) -> Argv:
    """Option values: -o V, -oV, -xyoV, --long V, --long=V."""
    values = []
    for i, token in enumerate(args):
        if token.startswith("--"):
            name, eq, value = token[2:].partition("=")
            if name in longs:
                if eq:
                    values.append(value)
                elif i + 1 < len(args):
                    values.append(args[i + 1])
        elif token.startswith("-") and len(token) > 1:
            for k, char in enumerate(token[1:], 1):
                if char in shorts:
                    if token[k + 1 :]:
                        values.append(token[k + 1 :])
                    elif i + 1 < len(args):
                        values.append(args[i + 1])
                    break
    return values


# --- writes into trees -------------------------------------------------------

_GLOB = re.compile(r"[*?\[]")  # shell glob characters in a path
_PATH_IN_TOKEN = re.compile(r"(?:^|[=:])(/\S*)")
_REDIRECT = re.compile(r">{1,2}\|?\s*([^\s&|;<>]+)")
_REDIR_TOKEN = re.compile(r"^(\d*|&)(<<<|<<-?|<|>>|>\||>&|>)")
_OVERWRITERS = {"tee", "mv", "truncate", "rm", "shred", "unlink"}
_COPIERS = {"cp", "install", "rsync", "ln"}
_CONFIG_INI = re.compile(r"^config\.ini(\.(new|tmp|orig|bak)([-.]?\d[\d.-]*)?)?$")


def _in_tree(p: str, tree: str) -> bool:
    if (
        "*" in tree[:-1]
    ):  # a glob inside the path: /etc/systemd/system/*.wants/digipaddock-* (the symlinks of enabled units)
        return fnmatch.fnmatchcase(p, tree)
    if tree.endswith("*"):  # a path prefix: /etc/systemd/system/digipaddock-*
        return p.startswith(tree[:-1])
    if tree.endswith(
        "/"
    ):  # only the directory itself — as a write's target, without its contents: /home/agent-ro/
        return p == tree[:-1]
    return p == tree or p.startswith(tree + "/")


def _under(path: str, trees) -> bool:
    """path is one of trees or inside it (the forms "…*" and "…/" — see _in_tree)."""
    p = posixpath.normpath(path)
    return any(_in_tree(p, tree) for tree in trees)


def _encloses(path: str, trees) -> bool:
    """path is the parent directory of one of trees: chmod/chown, a move and rm -r on it touch them too,
    and copying into it is copying into a directory."""
    if not path.startswith("/") or _GLOB.search(path):
        return False
    p = posixpath.normpath(path)
    prefix = p if p.endswith("/") else p + "/"
    return any(tree.rstrip("*").startswith(prefix) for tree in trees)


def _exempt(path: str) -> bool:
    """config.ini is not in git and is not deployed — it is edited atomically: the file itself and its .new/.tmp/.orig/.bak."""
    return bool(_CONFIG_INI.match(posixpath.basename(posixpath.normpath(path))))


def _hits(token: str, trees, cwds: Argv, exempt: bool = True) -> bool:
    """The argument points inside trees: by an absolute path (/x, of=/x, --opt=/x) or by a relative one after cd (cwds):
    a path from the directory, and if the directory's own files are in trees — any relative path.
    exempt — the exemption for config.ini (only for site directories)."""
    match = _PATH_IN_TOKEN.search(token)
    if match:
        path = match.group(1)
        return not (exempt and _exempt(path)) and _under(path, trees)
    relative = bool(token) and not token.startswith("-") and "=" not in token
    if not relative or (exempt and _exempt(token)):
        return False
    return any(
        _under(posixpath.join(cwd, token), trees)
        or _under(posixpath.join(cwd, "_"), trees)
        for cwd in cwds
    )


_TARGET_DIR = {
    "cp",
    "mv",
    "ln",
    "install",
}  # -t DIRECTORY; for rsync -t means keeping the modification time
_COPY_VALUED = {  # options that take a value as a separate word: the value is not an operand
    "cp": {"-t", "-S", "--target-directory", "--suffix"},
    "mv": {"-t", "-S", "--target-directory", "--suffix"},
    "ln": {"-t", "-S", "--target-directory", "--suffix"},
    "install": {
        "-t",
        "-S",
        "-m",
        "-o",
        "-g",
        "--target-directory",
        "--suffix",
        "--mode",
        "--owner",
        "--group",
    },
    "rsync": {
        "-e",
        "-f",
        "-T",
        "--rsh",
        "--filter",
        "--exclude",
        "--include",
        "--exclude-from",
        "--include-from",
        "--files-from",
        "--chmod",
        "--chown",
        "--backup-dir",
        "--log-file",
        "--password-file",
        "--temp-dir",
        "--partial-dir",
        "--compare-dest",
        "--copy-dest",
        "--link-dest",
    },
}


def _copy_plan(cmd: str, args: Argv) -> Tuple[str, Argv, bool]:
    """(the destination, the sources, the destination is certainly a directory) of cp/mv/install/ln/rsync. Certainly a
    directory: -t/--target-directory, "…/", "…/.", ".", ".." or several sources."""
    valued, operands, skip = _COPY_VALUED.get(cmd, set()), [], False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-"):
            skip = a in valued
        else:
            operands.append(a)
    target = (
        _option_values(args, "t", ("target-directory",)) if cmd in _TARGET_DIR else []
    )
    if target:
        return target[-1], operands, True
    if not operands:
        return "", [], False
    folder, sources = operands[-1], operands[:-1]
    return (
        folder,
        sources,
        len(sources) > 1 or folder in (".", "..") or folder.endswith(("/", "/.")),
    )


def _placed(folder: str, source: str) -> Argv:
    """What appears in the directory folder: folder/<the source's name>; for "src/", "src/.", "." (the contents) — folder itself."""
    name = posixpath.basename(source.rstrip("/"))
    placed = [posixpath.join(folder, name)] if name not in ("", ".", "..") else []
    if source.endswith(("/", "/.")) or source in (".", ".."):
        placed.append(folder)
    return placed


def _recursive(cmd: str, args: Argv) -> bool:
    letters = {"cp": "rRa", "rsync": "ra"}.get(cmd)
    return bool(letters) and (
        _short_has(args, letters) or bool({"--recursive", "--archive"} & set(args))
    )


def _strip_redirects(argv: Argv) -> Argv:
    """Without redirections (<, <<<, 2>&1 …) and their targets: they are not the command's operands."""
    out, skip = [], False
    for word in argv:
        if skip:
            skip = False
            continue
        match = _REDIR_TOKEN.match(word)
        if match:
            skip = match.end() == len(word)
            continue
        out.append(word)
    return out


def _sed_files(args: Argv) -> Argv:
    """sed's files: without the script (the first operand or the values of -e/-f) and the options."""
    files, has_script, skip = [], False, False
    for a in args:
        if skip:
            skip = False
        elif a in ("-e", "-f", "--expression", "--file"):
            has_script, skip = True, True
        elif a.startswith(("--expression=", "--file=")) or (
            a.startswith(("-e", "-f")) and len(a) > 2
        ):
            has_script = True  # -e'script', --expression=…
        elif not a.startswith("-"):
            files.append(a)
    return files if has_script else files[1:]


def _cd_target(argv: Argv) -> str:
    return next((a for a in argv[1:] if not a.startswith("-")), "")


def _extracts(args: Argv) -> bool:
    return (
        _short_has(args, "x")
        or bool({"--extract", "--get"} & set(args))
        or bool(args and not args[0].startswith("-") and "x" in args[0])
    )


def _writes_into(
    p: Parsed, trees, copiers, changers=frozenset(), exempt: bool = True
) -> bool:
    """A write, a deletion or a move inside trees; changers — commands whose path operands change too (chmod …)."""
    argvs = [_strip_redirects(_live(a)) for a in p.argvs]
    cwds = [
        _cd_target(a)
        for a in argvs
        if _cmd(a) in ("cd", "pushd") and _cd_target(a).startswith("/")
    ]

    def hits(token: str) -> bool:
        return _hits(token, trees, cwds, exempt)

    def absolute(path: str) -> Argv:
        return (
            [path]
            if path.startswith("/")
            else [posixpath.join(cwd, path) for cwd in cwds]
        )

    if any(hits(m.group(1)) for m in _REDIRECT.finditer(p.flat)):
        return True
    for argv in argvs:
        cmd, args = _cmd(argv), argv[1:]
        operands = [a for a in args if not a.startswith("-")]
        if (cmd in _OVERWRITERS or cmd in changers) and any(map(hits, operands)):
            return True
        if cmd in copiers or cmd == "mv":
            # into a directory: cp /tmp/sudoers /etc/ writes /etc/sudoers; the parent of a protected tree is certainly
            # a directory
            folder, sources, is_dir = _copy_plan(cmd, args)
            is_dir = is_dir or any(_encloses(f, trees) for f in absolute(folder))
            placed = [t for s in sources for t in _placed(folder, s)] if is_dir else []
            if (cmd in copiers and hits(folder)) or any(map(hits, placed)):
                return True
            if _recursive(cmd, args) and any(
                _encloses(f, trees) for t in placed for f in absolute(t)
            ):
                return True  # the directory merges with the tree's parent: cp -a /tmp/etc /, rsync -a /tmp/root/ /
        if (
            cmd == "sed"
            and (_short_has(args, "i") or any(a.startswith("--in-place") for a in args))
            and any(map(hits, _sed_files(args)))
        ):
            return True
        if cmd == "dd" and any(a.startswith("of=") and hits(a) for a in args):
            return True
        if (
            cmd == "rsync"
            and "--remove-source-files" in args
            and any(map(hits, operands[:-1]))
        ):
            return True
        written = []  # where the command downloads or unpacks to
        if cmd == "curl":
            written = _option_values(args, "o", ("output",))
        elif cmd == "wget":
            written = _option_values(
                args, "OP", ("output-document", "directory-prefix")
            )
        elif cmd == "unzip":
            written = _option_values(args, "d", ())
        elif cmd == "tar" and _extracts(args):
            written = _option_values(args, "C", ("directory",))
        if any(map(hits, written)):
            return True
    return False


_ATTR_CHANGERS = {
    "chmod",
    "chown",
    "chgrp",
    "chattr",
    "setfacl",
    "rmdir",
}  # permissions, the owner, removing a directory


def writes_into(p: Parsed, trees) -> bool:
    """The command writes, deletes, moves, changes permissions or the owner inside trees (no exemption for config.ini).
    The shared part of the cage-write rule and of the hook's local check (the agent's settings in /home/<base>)."""
    return _writes_into(p, trees, _COPIERS, _ATTR_CHANGERS, exempt=False)


# --- public names for the agents' guards (the same objects as the underscored ones) ---
cmd = _cmd
WRAPPERS = _WRAPPERS
REDIRECT = _REDIRECT
REDIR_TOKEN = _REDIR_TOKEN
short_has = _short_has
live = _live
strip_redirects = _strip_redirects
sed_files = _sed_files
copy_plan = _copy_plan


# --- critical commands (moved from sysadmin/critical.py) -------------------------------
#
# The list: network access, the production database, data removal, disks and file systems, reboot and
# stopping critical services. Such a command needs the owner's approval of the exact text (kd-admin-propose-action,
# then kd-admin-critical). The classifier works on the parse above (segments by shell operators, wrappers such as
# sudo, env and bash -c removed), matches command names and arguments in lower case and errs on the critical side.
# It recognizes known forms only: obfuscated, scripted or dynamically built commands pass (spec 5.11, residual
# risk), so it is not a boundary — the signature, the hard denylist and the journal are. Reading a firewall
# (iptables -S, nft list, wg show, ufw status, firewall-cmd --list-all) is not critical: only its known reading forms
# made of plain words (redirection targets included), with nothing a substitution, xargs or a quoted separator could
# add to them; any other form is critical. A write into the network or SSH access configuration on disk
# (/etc/wireguard, /etc/iptables, nftables, ufw, firewalld, netplan, interfaces, sshd_config …, the unit files,
# drop-ins and enabling links of SSH and network units in every systemd directory) by any form writes_into knows —
# a redirect, tee, cp, ln, sed -i — is critical, and so is a systemctl verb that redefines an SSH unit (edit, revert,
# set-property, link, enable …).

NETWORK = "сетевой доступ"
DATABASE = "производственная БД"
REMOVAL = "удаление данных"
DISKS = "диски и файловые системы"
SHUTDOWN = "перезагрузка или остановка"

# Never ordinary, whatever a catalog says: remote access, network and VPN, databases, the agents' own
# infrastructure (digipaddock-*, hindsight, forgejo), security and system plumbing (kd.admin_operations refuses them
# as catalog units; stopping one is critical). A deployment's own applications — its critical_units.
PROTECTED = (
    "ssh*",
    "systemd-*",
    "network*",
    "wpa_supplicant*",
    "mysql*",
    "mariadb*",
    "postgresql*",
    "redis*",
    "digipaddock-*",
    "hindsight*",
    "agent-*",
    "docker*",
    "containerd*",
    "wg-quick@*",
    "wireguard*",
    "danted*",
    "openvpn*",
    "sing-box*",
    "xray*",
    "cloudflared*",
    "gost*",
    "autossh*",
    "tor*",
    "privoxy*",
    "fail2ban*",
    "forgejo*",
    "ufw*",
    "nftables*",
    "iptables*",
    "ip6tables*",
    "netfilter-*",
    "firewalld*",
    "cron*",
    "atd*",
    "dbus*",
    "polkit*",
    "getty@*",
    "serial-getty@*",
    "user@*",
    "rsyslog*",
    "auditd*",
    "snapd*",
    "unattended-upgrades*",
    "chrony*",
    "qemu-guest-agent*",
    "multipathd*",
    "iscsid*",
)
# Without a list of the deployment's site units every unit's stop is critical (stricter).
ALL_UNITS = ("*",)

_FIREWALL = ("iptables*", "ip6tables*", "nft", "ufw", "firewall-cmd", "wg", "wg-quick")
# Reading forms of the firewall tools (case-sensitive: iptables -N is a new chain, -n numeric output).
_IPTABLES = re.compile(r"ip6?tables(-legacy|-nft)?")
_IPTABLES_SAVE = re.compile(r"ip6?tables(-legacy|-nft)?-save")
_IPTABLES_LONG = {
    "--list",
    "--list-rules",
    "--numeric",
    "--verbose",
    "--exact",
    "--line-numbers",
    "--table",
    "--wait",
}
_NFT_LONG = {
    "--handle",
    "--json",
    "--numeric",
    "--reversedns",
    "--numeric-protocol",
    "--stateless",
    "--service",
    "--terse",
    "--numeric-time",
    "--guid",
    "--numeric-priority",
}
_UFW_READS = {"status", "show", "version", "help"}
_FIREWALLD_FLAGS = {
    "--state",
    "--version",
    "--help",
    "-h",
    "-V",
    "--permanent",
    "-q",
    "--quiet",
    "--direct",
}
_FIREWALLD_READS = ("--list-", "--get-", "--query-", "--info-", "--zone=", "--policy=")
_FIREWALLD_VALUED = {"--zone", "--policy"}
_WG_SECRETS = {"private-key", "preshared-keys"}
# A plain word: no expansion, glob, quote or redirection can turn it into another option.
_PLAIN = re.compile(r"[\w.,:@/%+=-]+")
# What can feed a reading command words the parse does not see: substitution, xargs, a quoted or escaped separator.
_FED = re.compile(r"\$\(|`|[<>]\(|\bxargs\b")
# Network configuration on disk: a write here (redirect, tee, cp, sed -i …) changes access at the next reload or boot.
_NETWORK_FILES = (
    "/etc/wireguard",
    "/etc/iptables",
    "/etc/nftables*",
    "/etc/ufw",
    "/etc/default/ufw",
    "/etc/firewalld",
    "/etc/netplan",
    "/etc/network",
    "/etc/systemd/network",
    "/etc/NetworkManager",
    "/etc/iproute2",
    "/etc/sysconfig/iptables*",
    "/etc/sysconfig/ip6tables*",
    "/etc/sysconfig/nftables*",
    "/etc/sysconfig/network-scripts",
    "/etc/ssh/sshd_config*",
    "/etc/hosts.allow",
    "/etc/hosts.deny",
)
# Units of SSH and network access (file names, case kept): their unit files and drop-ins (<unit>.d/) in every
# systemd directory and the links that enable them (*.wants/, *.requires/). A write takes effect at the next
# daemon-reload, restart or boot: nft list ruleset > /etc/systemd/system/ssh.socket redefines the SSH socket.
_ACCESS_UNITS = (
    "ssh*",
    "wg-quick@*",
    "networking*",
    "systemd-networkd*",
    "NetworkManager*",
    "nftables*",
    "ufw*",
    "firewalld*",
    "netfilter-*",
    "iptables*",
    "ip6tables*",
)
_UNIT_DIRS = (
    "/etc/systemd/system",
    "/run/systemd/system",
    "/lib/systemd/system",
    "/usr/lib/systemd/system",
    "/usr/local/lib/systemd/system",
)
_ACCESS_UNIT_FILES = tuple(
    "%s/%s%s" % (folder, link, unit)
    for folder in _UNIT_DIRS
    for link in ("", "*.wants/", "*.requires/")
    for unit in _ACCESS_UNITS
)
# systemctl options whose value is the next word (lower case: classify lowers the arguments).
_SYSTEMCTL_VALUED = {
    "-t",
    "--type",
    "-p",
    "--property",
    "-s",
    "--signal",
    "-h",
    "--host",
    "-m",
    "--machine",
    "-n",
    "--lines",
    "-o",
    "--output",
    "--root",
    "--image",
    "--image-policy",
    "--state",
    "--what",
    "--job-mode",
    "--kill-whom",
    "--kill-value",
    "--preset-mode",
    "--boot-loader-entry",
    "--boot-loader-menu",
    "--timestamp",
    "--when",
    "--drop-in",
    "--reboot-argument",
    "--message",
    "--check-inhibitors",
    "--capsule",
}
# systemctl verbs that change a unit's definition or whether it starts (stop, disable, mask of SSH: the denylist).
_SYSTEMCTL_DEFINES = {
    "edit",
    "revert",
    "set-property",
    "link",
    "enable",
    "reenable",
    "preset",
    "unmask",
}
_NETWORK_UNITS = (
    "wg-quick@*",
    "networking",
    "systemd-networkd*",
    "networkmanager*",
    "nftables*",
    "ufw*",
    "firewalld*",
    "netfilter-*",
)
_IP_OBJECTS = ("route", "rule", "link", "address", "neighbour", "neighbor")
_IP_WRITES = ("add", "delete", "change", "replace", "flush", "set", "append", "prepend")
_IP_READS = ("show", "list", "lst", "get", "help", "monitor")
_SYSTEMCTL_READS = {
    "status",
    "show",
    "cat",
    "help",
    "is-active",
    "is-enabled",
    "is-failed",
    "is-system-running",
    "list-units",
    "list-unit-files",
    "list-timers",
    "list-sockets",
    "list-dependencies",
    "list-jobs",
}

_DB_CLIENTS = ("mysql", "mariadb", "psql", "mysqladmin", "mongo*", "redis-cli")
_DB_INLINE = ("-e", "-c", "--execute", "--command")
_DB_WRITES = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke|rename|flush|"
    r"shutdown|set\s+global|load\s+data)\b",
    re.I,
)

_KEPT_TREES = ("/var/lib", "/var/backups", "/srv", "/home", "/opt", "/root", "/data")
_KEPT_NAMES = ("/backup*", "*.sql*", "*backup*", "*.dump")

_DISK_TOOLS = (
    "mkfs*",
    "fdisk",
    "sfdisk",
    "gdisk",
    "parted",
    "wipefs",
    "mdadm",
    "lvremove",
    "lvreduce",
    "vgremove",
    "pvremove",
    "resize2fs",
    "xfs_growfs",
    "umount",
    "swapoff",
)

_POWER = ("reboot", "poweroff", "halt", "shutdown")
_SYSTEMCTL_POWER = {
    "reboot",
    "soft-reboot",
    "poweroff",
    "halt",
    "kexec",
    "rescue",
    "emergency",
    "isolate",
}
_STOPS = {"stop", "disable", "mask", "kill"}
_CRITICAL_UNITS = PROTECTED + (
    "nginx*",
    "apache2*",
    "php*-fpm*",
)
_DOCKER_VALUED = {"-H", "--host", "-c", "--context", "--config", "-l", "--log-level"}
_DOCKER_STOPS = {"stop", "kill", "rm"}
_COMPOSE_STOPS = {"down", "stop", "kill", "rm"}


def _any(word: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(word, pattern) for pattern in patterns)


def _words(args: Argv) -> Argv:
    """Operands: arguments that are not options (everything after ``--``)."""
    words, options = [], True
    for arg in args:
        if options and arg == "--":
            options = False
        elif not (options and arg.startswith("-")):
            words.append(arg)
    return words


def _unit(name: str) -> str:
    """The unit name: link and enable take a unit file's path (/var/tmp/ssh.socket is ssh.socket)."""
    name = posixpath.basename(name.rstrip("/")) or name
    return name[: -len(".service")] if name.endswith(".service") else name


def _systemctl(args: Argv) -> Tuple[str, Argv]:
    """(verb, units) of systemctl past its options and their values (--root / link … is link);
    ``service UNIT VERB`` reads the same way."""
    words, i = [], 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            words += args[i + 1 :]
            break
        if arg in _SYSTEMCTL_VALUED:
            i += 1
        elif not arg.startswith("-"):
            words.append(arg)
        i += 1
    return (words[0], [_unit(w) for w in words[1:]]) if words else ("", [])


def _units(verb: str, units: Argv, site_units) -> Optional[str]:
    if verb not in _SYSTEMCTL_READS and any(
        _any(unit, _NETWORK_UNITS) for unit in units
    ):
        return NETWORK
    if verb in _SYSTEMCTL_DEFINES and any(_any(unit, ("ssh*",)) for unit in units):
        return NETWORK
    if verb in _STOPS and any(
        _any(unit, _CRITICAL_UNITS + tuple(site_units)) for unit in units
    ):
        return SHUTDOWN
    return None


def _reads_only(args: Argv, shorts: str, longs) -> bool:
    """Every option is a reading one: each letter of -xyz in shorts, --long (or --long=value) in longs."""
    for arg in args:
        if arg.startswith("--"):
            if arg.partition("=")[0] not in longs:
                return False
        elif arg.startswith("-") and len(arg) > 1 and not set(arg[1:]) <= set(shorts):
            return False
    return True


def _lists(args: Argv) -> bool:
    return any(
        a in ("--list", "--list-rules")
        or (
            a.startswith("-")
            and not a.startswith("--")
            and bool(set(a[1:]) & set("SL"))
        )
        for a in args
    )


def _firewalld_reads(args: Argv) -> bool:
    valued = False
    for arg in args:
        if valued:
            valued = False
        elif arg in _FIREWALLD_VALUED:
            valued = True
        elif arg not in _FIREWALLD_FLAGS and not arg.startswith(_FIREWALLD_READS):
            return False
    return not valued


def _fed(text: str) -> bool:
    return bool(_FED.search(text)) or any(
        ";" in m.group(0) or "\n" in m.group(0) for m in _QUOTED.finditer(text)
    )


def _plain_word(word: str) -> bool:
    """A plain word, or a redirection whose target is one (a write into the network configuration is caught
    by classify; a target built by expansion is not plain)."""
    rest = _REDIR_TOKEN.sub("", word, count=1)
    return bool(_PLAIN.fullmatch(rest)) or (rest == "" and rest != word)


def _firewall_read(cmd: str, args: Argv, text: str) -> bool:
    """A known reading form of a firewall tool, made only of plain words (args in their original case)."""
    plain = _strip_redirects(args)
    words = _words(plain)
    if _IPTABLES_SAVE.fullmatch(cmd):
        reads = _reads_only(plain, "ct", {"--counters", "--table"})
    else:
        if _IPTABLES.fullmatch(cmd):
            reads = _lists(plain) and _reads_only(plain, "SLnvxtw", _IPTABLES_LONG)
        elif cmd == "nft":
            reads = words[:1] == ["list"] and _reads_only(
                plain, "ajnNpsStTuy", _NFT_LONG
            )
        elif cmd == "wg":
            reads = (not words or words[0] == "show") and not _WG_SECRETS & set(words)
        elif cmd == "ufw":
            reads = _reads_only(plain, "h", {"--version", "--help"}) and (
                not words
                or words[0] in _UFW_READS
                or words[:2] in (["app", "list"], ["app", "info"])
            )
        else:
            reads = cmd == "firewall-cmd" and _firewalld_reads(plain)
    return reads and all(_plain_word(a) for a in args) and not _fed(text)


def _network(cmd: str, args: Argv, original: Argv, text: str) -> bool:
    words = _words(args)
    if _any(cmd, _FIREWALL):
        return not _firewall_read(cmd, original, text)
    if cmd == "netplan":
        return "apply" in words
    if cmd == "route":
        return "add" in words or "del" in words
    if (
        cmd == "ip"
    ):  # an object (or its prefix: ip a, ip r) followed by a changing verb (ip a add, ip r del)
        return any(
            any(o.startswith(w) for o in _IP_OBJECTS) and _ip_write(words[i + 1])
            for i, w in enumerate(words[:-1])
        )
    return False


def _ip_write(word: str) -> bool:
    return any(v.startswith(word) for v in _IP_WRITES) and not any(
        v.startswith(word) for v in _IP_READS
    )


def _database(cmd: str, args: Argv, text: str) -> bool:
    if not _any(cmd, _DB_CLIENTS):
        return False
    inline = any(
        a in _DB_INLINE or a.startswith(("--execute=", "--command=")) for a in args
    )
    return not inline or bool(_DB_WRITES.search(text))


def _kept(operand: str) -> bool:
    path = posixpath.normpath(operand) if operand.startswith("/") else operand
    return any(
        path == tree or path.startswith(tree + "/") for tree in _KEPT_TREES
    ) or _any(path, _KEPT_NAMES)


def _removal(cmd: str, args: Argv) -> bool:
    if cmd == "rm":
        return (
            _short_has(args, "rR")
            or "--recursive" in args
            or any(_kept(w) for w in _words(args))
        )
    if cmd == "find":
        return "-delete" in args or any(
            a in ("-exec", "-execdir", "-ok", "-okdir")
            and posixpath.basename(b) == "rm"
            for a, b in zip(args, args[1:])
        )
    if cmd == "shred":
        return True
    if cmd == "truncate":
        values = set(_option_values(args, "sr", ("size", "reference")))
        operands = [w for w in _words(args) if w not in values]
        return any(not posixpath.normpath(w).startswith("/tmp/") for w in operands)
    if cmd == "dd":
        return any(a.startswith("of=") for a in args)
    if cmd == "docker":
        sub, rest = _docker(args)
        return (
            sub == "volume" and bool({"rm", "remove", "prune"} & set(rest[:1]))
        ) or (sub == "system" and "prune" in rest[:1])
    return False


def _docker(args: Argv) -> Tuple[str, Argv]:
    """(subcommand, its operands) of docker, past the global options."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in _DOCKER_VALUED else 1
    words = _words(args[i:])
    return (words[0], words[1:]) if words else ("", [])


def _disks(cmd: str, args: Argv) -> bool:
    if _any(cmd, _DISK_TOOLS):
        return True
    return cmd == "mount" and any(
        "remount" in v.split(",") for v in _option_values(args, "o", ("options",))
    )


def _shutdown(cmd: str, args: Argv) -> bool:
    words = _words(args)
    if cmd in _POWER:
        return True
    if cmd in ("init", "telinit"):
        return bool({"0", "6"} & set(words))
    if cmd == "docker":
        sub, rest = _docker(args)
        if sub == "container":
            return bool(_DOCKER_STOPS & set(rest[:1]))
        return sub in _DOCKER_STOPS or (
            sub == "compose" and bool(_COMPOSE_STOPS & set(rest))
        )
    if cmd == "docker-compose":
        return bool(_COMPOSE_STOPS & set(words))
    return False


def _segment(original: Argv, text: str, site_units) -> Optional[str]:
    argv = [word.lower() for word in original]
    cmd, args = _cmd(argv), argv[1:]
    if cmd == "systemctl":
        verb, units = _systemctl(args)
        return SHUTDOWN if verb in _SYSTEMCTL_POWER else _units(verb, units, site_units)
    if cmd == "service":  # service UNIT VERB
        words = _words(args)
        return (
            _units(words[1], [_unit(words[0])], site_units) if len(words) > 1 else None
        )
    if _network(cmd, args, original[1:], text):
        return NETWORK
    if _database(cmd, args, text):
        return DATABASE
    if _removal(cmd, args):
        return REMOVAL
    if _disks(cmd, args):
        return DISKS
    if _shutdown(cmd, args):
        return SHUTDOWN
    return None


def classify(command: str, site_units=ALL_UNITS) -> Optional[str]:
    """The critical group of the first recognized segment, or None for an ordinary command. ``site_units`` — the
    deployment's own site units (fnmatch patterns) whose stop is critical; by default every unit's."""
    parsed = parse(command)
    if writes_into(parsed, _NETWORK_FILES + _ACCESS_UNIT_FILES):
        return NETWORK
    for argv in parsed.argvs:
        # the typed helper's reboot (system.reboot) is a reboot, whoever calls it
        for word, after in zip(argv, argv[1:]):
            if posixpath.basename(word) == "agent-typed-op" and after == "reboot":
                return SHUTDOWN
        group = _segment(argv, parsed.raw, site_units)
        if group:
            return group
    return None


# --- packed payloads (moved from kd.admin_operations) ----------------------------------------------------
#
# What the owner could not read: a long run of base64 alphabet — on one line, or wrapped over lines that mix upper
# and lower case and digits (a path list does not) —, a decoder or unpacker (base64/base32/basenc -d with any other
# flags, openssl enc|base64 -d, python -m base64|gzip|zlib, b64decode/unhexlify, xxd -r, uudecode, zcat/gunzip/
# gzip -d and the like) or bytes spelled as \x or octal escapes (printf, echo -e, $'…').
_B64 = r"[A-Za-z0-9+/=_-]"
_B64_LINE = r"^(?=[^\n]*[A-Z])(?=[^\n]*[a-z])(?=[^\n]*[0-9])" + _B64 + r"{40,}\n"
PACKED = re.compile(
    _B64 + r"{120,}|(?:" + _B64_LINE + r"){3,}"
    r"|\b(?:base64|base32|basenc)\b[^\n|;&]*\s(?:-[A-Za-z]*[dD][A-Za-z]*|--decode)\b"
    r"|\bopenssl\s+(?:enc|base64)\b[^\n|;&]*\s-d\b"
    r"|\bpython[0-9.]*\s+(?:-[A-Za-z]+\s+)*-m\s*(?:base64|gzip|zlib|binascii|codecs)\b"
    r"|(?:b64decode|b32decode|b85decode|a85decode|unhexlify|decode_base64)\b"
    r"|\b(?:zlib|gzip|bz2|lzma)\.decompress\b"
    r"|\bxxd\s+(?:-[A-Za-z]+\s+)*-r|\buudecode\b"
    r"|\b(?:zcat|gunzip|bzcat|bunzip2|xzcat|unxz|zstdcat|unzstd)\b"
    r"|\b(?:gzip|bzip2|xz|zstd)\b[^\n|;&]*\s(?:-[A-Za-z]*d[A-Za-z]*|--decompress)\b"
    r"|\b(?:printf|echo)\b[^\n]*\\(?:x[0-9A-Fa-f]{2}|[0-7]{3})"
    r"|\$'[^'\n]*\\(?:x[0-9A-Fa-f]{2}|[0-7]{3})",
    re.MULTILINE,
)


# --- rule 1: self-protection ---------------------------------------------------------------------------------------

GUARD = "шлюз и доступ к серверу меняются только с машины владельца (deploy_target.sh)"
PACKED_FOUND = "упакованное или закодированное содержимое"
# Paths of the mechanism: the gates and snapshots, the once-marks, sudo, ssh and every user's keys. A path is
# guarded with everything below it; the two marked «prefix» are string prefixes (agent-*, sudoers.d, sudoers.bak).
_GUARDED_DIRS = (
    "/etc/agent-exec-gate",
    "/var/lib/agent-exec-gate",
    "/etc/agent-ro-gate",
    "/etc/ssh",
    "/root/.ssh",
)
_GUARDED_PREFIXES = ("/usr/local/bin/agent-", "/etc/sudoers")
_HOME_SSH = "/home/*/.ssh"
# The same paths as writes_into trees: a copy into their folder (cp x /usr/local/bin/), -t, a relative name after cd
_GUARDED_TREES = _GUARDED_DIRS + (
    "/usr/local/bin/agent-*",
    "/etc/sudoers*",
    _HOME_SSH,
    _HOME_SSH + "/*",
)
_USER_HOME = re.compile(r"/home/[^/]+")  # a user's home: it holds that user's .ssh
# Bare words anywhere — in quotes, a heredoc or a variable's value too (the text is read with quotes and
# backslashes taken out, so sudo''ers and sudo\ers are sudoers).
_GUARDED_WORDS = (
    "sudoers",
    "sshd_config",
    "authorized_keys",
    "agent-exec-gate",
    "agent-exec-policy",
    "agent-ro-gate",
    "agent-sec-snapshot",
    "agent-hindsight-usage",
)
_SSH_DIR = re.compile(
    r"(?<![\w.-])\.ssh(?![\w.-])"
)  # .ssh as a path part: ~/.ssh, $HOME/.ssh/x, cd .ssh
# A path in the text: at the start or after a separator (a default of ${X:-/…} and a value glued to a short option,
# -o/…, included), up to the next blank or shell operator.
_PATH = re.compile(r"(?:^|(?<=[\s=:@,{<>|;&()+?-])|(?<=-[A-Za-z]))(/[^\s|;&<>()]*)")
# A variable set to plain text: its uses are read with the value in place (a=/etc/su; … ${a}doers is /etc/sudoers).
_ASSIGNED = re.compile(
    r"(?:^|(?<=[\s;&|(]))([A-Za-z_][A-Za-z0-9_]*)=([^\s|;&<>()$`]*)(?=[\s|;&<>()]|$)"
)
# What a variable or a glob may stand for, taken as nothing: the deepest a .. after it can climb.
_DYNAMIC_PART = re.compile(
    r"\$\{[^}]*\}|\$[A-Za-z0-9_]+|`[^`]*`|[*?]|\[[^\]]*\]|\{[^}]*\}"
)
# A relative path that climbs (../etc/ssh): from an unknown folder enough .. reach /.
_CLIMBS = re.compile(r"(?:^|(?<=[\s=:@,{<>|;&()+?-]))(\.\.(?:/[^\s|;&<>()]*)?)")
# What makes a path dynamic: a glob, a brace, a variable or a substitution — only its literal prefix is known.
_DYNAMIC = re.compile(r"[*?\[{$`]")
_CONTINUATION = re.compile(r"\\\r?\n")
# Accounts and privilege: the commands (also after find -exec and xargs) and writes into the account files.
_ACCOUNTS = frozenset(
    (
        "useradd",
        "adduser",
        "usermod",
        "userdel",
        "deluser",
        "passwd",
        "chpasswd",
        "gpasswd",
        "chage",
        "visudo",
        "ssh-copy-id",
        "vipw",
        "vigr",
    )
)
_ACCOUNT_FILES = ("/etc/passwd", "/etc/shadow", "/etc/group", "/etc/gshadow")
_EXECS = ("-exec", "-execdir", "-ok", "-okdir")
_PYTHON = re.compile(r"python[0-9.]*")
_PYTHON_VALUED = (
    "cmWXQ"  # the letters after these in -xyz are their value (python3 -mcompileall)
)
_INLINE = {
    "perl": "eE",
    "ruby": "e",
    "php": "r",
}  # an option letter that takes the program as text
_NODE_INLINE = ("-e", "--eval", "-p", "--print")
_SHELLS = frozenset(("sh", "bash", "dash", "zsh", "ksh", "ash"))


def _clean(command: str) -> str:
    """The text bash reads: line continuations joined, $'…' as '…', quotes and backslashes taken out, repeated
    slashes collapsed."""
    text = _CONTINUATION.sub("", command)
    return _SLASHES.sub("/", _QUOTES.sub("", _ANSI_QUOTE.sub(r"\1", text)))


def _norm(path: str) -> str:
    """posixpath.normpath (//, /./, ..), keeping a trailing slash: «/etc/» + a glob is any entry of /etc."""
    if not path:
        return path
    norm = posixpath.normpath(path)
    norm = "/" + norm.lstrip("/") if norm.startswith("/") else norm
    return norm + "/" if path.endswith("/") and norm != "/" else norm


def _guarded(path: str) -> bool:
    """A fixed path inside the mechanism."""
    p = _norm(path).rstrip("/") or "/"
    return (
        any(p == d or p.startswith(d + "/") for d in _GUARDED_DIRS)
        or p.startswith(_GUARDED_PREFIXES)
        or fnmatch.fnmatchcase(p, _HOME_SSH)
        or fnmatch.fnmatchcase(p, _HOME_SSH + "/*")
    )


def _may_reach(prefix: str, trees, prefixes=()) -> bool:
    """A path known only by its literal prefix (before a glob or a variable) may name something in trees (each with
    everything below it) or starting with one of prefixes: the prefix leads to one or lies inside one."""
    p = _norm(prefix)
    if not p.startswith("/"):
        return False  # an empty or relative literal prefix: the residual risk
    inside = p.rstrip("/") or "/"
    return any(
        t.startswith(p) or inside == t or inside.startswith(t + "/") for t in trees
    ) or any(x.startswith(p) or p.startswith(x) for x in prefixes)


def _dynamic_guarded(prefix: str) -> bool:
    if _may_reach(prefix, _GUARDED_DIRS, _GUARDED_PREFIXES):
        return True
    p = _norm(prefix)
    if "/home/".startswith(p):
        return True
    if not p.startswith("/home/"):
        return False
    parts = p[len("/home/") :].split(
        "/"
    )  # <user>[/<rest>…]: the user part may still be anything
    return len(parts) == 1 or ".ssh".startswith(parts[1]) or parts[1].startswith(".ssh")


def _holds_guarded(folder: str) -> bool:
    """A folder whose entries include the mechanism: /usr/local/bin (agent-*), /etc (sudoers, ssh), /var/lib, /root,
    /home and a user's home (.ssh) — a glob or a relative name there may be one of them."""
    p = _norm(folder).rstrip("/") or "/"
    return (
        p in ("/", "/home")
        or bool(_USER_HOME.fullmatch(p))
        or any(
            posixpath.dirname(g.rstrip("/")) == p
            for g in _GUARDED_DIRS + _GUARDED_PREFIXES
        )
    )


def _dynamic_path(token: str) -> bool:
    """A path with a glob or a variable: its literal prefix may lead into the mechanism; a .. after the dynamic part
    climbs at most as if the part were empty (/tmp/$X/../../etc/ssh is /etc/ssh then)."""
    dynamic = _DYNAMIC.search(token)
    if dynamic is None:
        return _guarded(token)
    literal, rest = token[: dynamic.start()], token[dynamic.start() :]
    if _dynamic_guarded(literal):
        return True
    if "/.." not in rest:
        return False
    tail = rest.rsplit("/..", 1)[1]  # what follows the last climb
    landed = _norm(literal + _DYNAMIC_PART.sub("", rest))
    return _dynamic_guarded(landed) if _DYNAMIC.search(tail) else _guarded(landed)


def _paths(text: str) -> Optional[str]:
    for match in _PATH.finditer(text):
        token = match.group(1)
        if (
            "}" in token
        ):  # the default of ${X:-/etc/ssh}/y goes on after the brace: a literal prefix
            if _dynamic_path(token.split("}", 1)[0] + "$"):
                return "путь %s" % token
        elif _DYNAMIC.search(token) is None:
            if _guarded(token):
                return "путь %s" % _norm(token)
        elif _dynamic_path(token):
            return "путь %s" % token
    for match in _CLIMBS.finditer(text):
        climbed = "/" + re.sub(r"^(\.\./?)+", "", _norm(match.group(1)))
        if climbed != "/" and (
            _dynamic_path(climbed)
            if _DYNAMIC.search(climbed)
            else _guarded(climbed) or _holds_guarded(climbed)
        ):
            return "путь %s" % match.group(1)
    return None


def _expanded(text: str) -> str:
    """The text with each variable set to plain text earlier (NAME=value) read in place of its uses — $NAME,
    ${NAME} — one level, as bash would expand them in order (a later assignment wins from there on)."""
    out, values, at = [], {}, 0  # type: List[str], dict, int
    for match in _ASSIGNED.finditer(text):
        out.append(_substitute(text[at : match.start()], values))
        out.append(match.group(0))
        values[match.group(1)] = match.group(2)
        at = match.end()
    out.append(_substitute(text[at:], values))
    return "".join(out)


def _substitute(text: str, values: dict) -> str:
    for name, value in values.items():
        text = re.sub(
            r"\$\{%s\}|\$%s(?![A-Za-z0-9_])" % (name, name), lambda _m, v=value: v, text
        )
    return text


def _clusters(args: Argv, valued: str = "") -> str:
    """The letters of every short option cluster (-xyz), up to a letter whose value follows it (``valued``)."""
    letters = []
    for arg in args:
        if arg.startswith("-") and not arg.startswith("--") and len(arg) > 1:
            for letter in arg[1:]:
                letters.append(letter)
                if letter in valued:
                    break
    return "".join(letters)


def _operands(args: Argv) -> Argv:
    return [a for a in args if not a.startswith("-") and not _REDIR_TOKEN.match(a)]


def _interpreter(argv: Argv) -> Optional[str]:
    """An interpreter that runs a program given as text (-c, -e) or read from stdin (-, no script)."""
    cmd, args = _cmd(argv).lower(), _strip_redirects(argv[1:])
    if _PYTHON.fullmatch(cmd):
        letters = _clusters(args, _PYTHON_VALUED)
        if "-" in args or "c" in letters:
            return "%s -c" % cmd
        if (
            not _operands(args)
            and not {"-V", "--version", "-h", "--help"} & set(args)
            and "m" not in letters
        ):
            return "%s без скрипта (stdin)" % cmd
        return None
    if cmd in _INLINE:
        if "-" in args or set(_clusters(args)) & set(_INLINE[cmd]):
            return "%s -e" % cmd
        if not _operands(args) and not {"-v", "--version", "-h", "--help"} & set(args):
            return "%s без скрипта (stdin)" % cmd
        return None
    if cmd in ("node", "nodejs"):
        if "-" in args or any(
            a in _NODE_INLINE or a.startswith(("--eval=", "--print=")) for a in args
        ):
            return "%s -e" % cmd
    return None


def _encloses_guarded(path: str) -> bool:
    """A fixed folder that is, holds or lies inside a path of the mechanism: /, /etc, /usr, /usr/local/bin, /var/lib,
    /root, /home and a user's home; /etc/nginx, /var/www/site or /home/deploy/app do not."""
    p = _norm(path).rstrip("/") or "/"
    if p == "/" or _guarded(p) or p == "/home" or _USER_HOME.fullmatch(p):
        return True
    return any(g.startswith(p + "/") for g in _GUARDED_DIRS + _GUARDED_PREFIXES)


def _unpacks_over_guarded(argv: Argv, cwd: Optional[str]) -> Optional[str]:
    """tar -x, unzip or rsync that unpacks into a folder holding the mechanism (a system root, a user's home), into
    the folder it runs in when it names none (the gate's own: the exec user's home), or by the archive's absolute
    names (tar -P)."""
    cmd, args = _cmd(argv), _strip_redirects(argv[1:])
    if cmd == "tar" and _extracts(args):
        first = args[0] if args and not args[0].startswith("-") else ""
        if "P" in _clusters(args) + first or "--absolute-names" in args:
            return "tar -P (абсолютные имена из архива)"
        targets = _option_values(args, "C", ("directory",))
    elif cmd == "unzip":
        if set(_clusters(args, "d")) & set("ltvpZ"):
            return None  # lists, tests or prints to stdout: writes nothing
        targets = _option_values(args, "d", ())
    elif cmd == "rsync":
        folder = _copy_plan(cmd, args)[0]
        remote = re.match(r"([^/:]*):(.*)", folder)
        if remote:
            if remote.group(1).rpartition("@")[2] not in ("localhost", "127.0.0.1", ""):
                return None  # another host's
            folder = remote.group(2)
        if not folder:
            return None
        targets = [folder]
    else:
        return None
    for target in targets or ["."]:
        if not target.startswith("/"):
            if cwd is None:
                if _norm(target).startswith(".."):
                    return "%s в %s" % (cmd, target)
                if target in (".", "./") or _norm(target) == ".":
                    return "%s без каталога назначения" % cmd
                continue
            target = posixpath.join(cwd, target)
        if _DYNAMIC.search(target):
            if _dynamic_path(target):
                return "%s в %s" % (cmd, target)
        elif _encloses_guarded(target):
            return "%s в %s" % (cmd, target)
    return None


def _cwds(argvs: List[Argv]) -> List[Optional[str]]:
    """The folder each segment runs in, as far as cd/pushd tell (None — unknown: the gate's own, the exec user's
    home); a relative cd from a known folder is followed."""
    cwd, out = None, []  # type: Optional[str], List[Optional[str]]
    for argv in argvs:
        if _cmd(argv) in ("cd", "pushd"):
            target = _cd_target(argv)
            if target.startswith("/"):
                cwd = _norm(target)
            elif target and cwd is not None and not _DYNAMIC.search(target):
                cwd = _norm(posixpath.join(cwd, target))
            else:
                cwd = None
        out.append(cwd)
    return out


_WRITERS = (
    _OVERWRITERS | _COPIERS | _ATTR_CHANGERS | {"mv", "touch", "dd", "sed", "mkdir"}
)


def _resolved(path: str, cwd: Optional[str]) -> Optional[str]:
    if path.startswith("/"):
        return path
    return posixpath.join(cwd, path) if cwd is not None else None


def _writes_where_guarded(argv: Argv, cwd: Optional[str]) -> Optional[str]:
    """A write whose target the parse cannot name but which lands among the mechanism's entries: a relative path in a
    folder that holds them (cd /usr/local/bin && rm -f *), the folder itself or a glob in the folder the gate runs in
    (the exec user's home), or a glob or a whole tree copied into such a folder (cp /tmp/d/* /usr/local/bin/)."""
    cmd, words = _cmd(argv), argv[1:]
    operands, targets, redirected = (
        [],
        [],
        False,
    )  # targets: where a redirection writes, whatever the command
    for word in words:
        redirect = _REDIR_TOKEN.match(word)
        if redirected:
            targets.append(word)
        elif redirect and ">" in redirect.group(0) and redirect.end() < len(word):
            if not (
                redirect.group(0).endswith("&")
                and re.fullmatch(r"\d+-?|-", word[redirect.end() :])
            ):
                targets.append(
                    word[redirect.end() :]
                )  # 2>&1 duplicates a descriptor, writes no file
        elif not (word.startswith("-") or redirect):
            operands.append(
                word[3:] if cmd == "dd" and word.startswith(("of=", "if=")) else word
            )
        redirected = (
            bool(redirect) and ">" in redirect.group(0) and redirect.end() == len(word)
        )
    if cmd not in _WRITERS:
        operands = []
    operands += targets
    relative = [w for w in operands if w and not w.startswith(("/", "&"))]
    if cwd is not None and _holds_guarded(cwd) and relative:
        return "относительный путь %s в %s" % (relative[0], cwd)
    if cwd is None and any(
        _norm(w) in (".", "..") or w.startswith(("*", "?", "[")) for w in relative
    ):
        return "%s в каталоге шлюза (домашнем каталоге exec-пользователя)" % cmd
    if cmd in _COPIERS or cmd == "mv":
        folder, sources, _ = _copy_plan(cmd, _strip_redirects(words))
        dest = _resolved(folder, cwd) if folder else None
        whole = _recursive(cmd, words) or any(
            s.endswith(("/", "/.")) or s in (".", "..") for s in sources
        )
        if (
            dest
            and _holds_guarded(dest)
            and (whole or any(_DYNAMIC.search(s) for s in sources))
        ):
            return "%s в %s" % (cmd, folder)
    return None


def _shell_reads_stdin(segment: Argv) -> bool:
    """A shell that takes its program from stdin: no script and no -c (… | sh), -s, or busybox sh."""
    argv = command_argv(segment)
    if not argv and any(posixpath.basename(word) in _SHELLS for word in segment):
        return True
    for i, word in enumerate(segment):
        name, after = posixpath.basename(word), segment[i + 1 : i + 2]
        if (
            name in _SHELLS
            and after
            and after[0].startswith("-")
            and not after[0].startswith("--")
            and ("s" in after[0] and "c" not in after[0])
        ):
            return True
        if (
            name == "busybox"
            and after
            and posixpath.basename(after[0]) in _SHELLS
            and all(w.startswith("-") for w in segment[i + 2 :])
        ):
            return True
    return False


def _guard_check(command: str) -> Optional[str]:
    text = _clean(command)
    lowered = text.lower()
    for word in _GUARDED_WORDS:
        if word in lowered or word in command.lower():
            return "слово %s" % word
    if _SSH_DIR.search(text):
        return "слово .ssh"
    found = _paths(text)
    if found:
        return found
    parsed = parse(command)
    for segment in parsed.segments:
        argv = command_argv(segment)
        if "eval" in segment[: len(segment) - len(argv)]:
            return "eval"
        if _shell_reads_stdin(segment):
            return "оболочка без скрипта (stdin)"
    argvs = [_live(argv) for argv in parsed.argvs]
    for argv, cwd in zip(argvs, _cwds(argvs)):
        if cwd is not None and _guarded(cwd):
            return "каталог %s" % cwd
        if _cmd(argv) in _ACCOUNTS:
            return "команда %s" % _cmd(argv)
        for flag, nested in zip(argv, argv[1:]):
            if flag in _EXECS and posixpath.basename(nested) in _ACCOUNTS:
                return "команда %s" % posixpath.basename(nested)
        found = (
            _interpreter(argv)
            or _unpacks_over_guarded(argv, cwd)
            or _writes_where_guarded(argv, cwd)
        )
        if found:
            return found
    if writes_into(parsed, _GUARDED_TREES):
        return "запись в каталог механизма"
    if writes_into(parsed, _ACCOUNT_FILES):
        return "запись в файлы учёток"
    if PACKED.search(command) or PACKED.search(text):
        return PACKED_FOUND
    return None


def protect(command: str) -> Optional[str]:
    """Rule 1: the refusal's text when the command touches the mechanism (the gate, its state, sudo, ssh, accounts)
    or carries a way around reading it (an inline interpreter, a decoder, an archive into a system root); None
    otherwise. No exceptions: ssh, sudo and the gate itself on a server change only by deploy_target.sh or the
    owner's own hand."""
    found = _guard_check(command)
    if not found:
        text = _clean(command)
        expanded = _expanded(text)
        found = _guard_check(expanded) if expanded != text else None
    return "%s: %s" % (found, GUARD) if found else None


# --- rule 2: the content class matches the signature kind ------------------------------------------------------------


def critical_step(step: int, group: str) -> str:
    return (
        "шаг %d критичный (%s): владелец подписывает его отдельно, точной командой (kd-admin-propose-action)"
        % (step, group)
    )


def plan_step(step: int, command: str, site_units=ALL_UNITS) -> Optional[str]:
    """Rules 1 and 2 for the command of plan step ``step`` (a script's text or a typed step's formed command): the
    refusal's text, or None. ``site_units`` — the deployment's site units (see ``classify``)."""
    guarded = protect(command)
    if guarded:
        return guarded
    group = classify(command, site_units)
    return critical_step(step, group) if group else None


# --- typed content: cron.line and file.drop
# ---------------------------------------------------------------
#
# A typed step that writes a root cron line or one file of sysctl.d / logrotate.d carries text the system later reads
# as root. It stays narrow, so the step cannot be a script under another name: a cron line is one schedule and one
# command, and that command passes rules 1 and 2 like a script; a sysctl file is plain ``key = value`` lines with no
# program a kernel would start; a logrotate file names only logs under /var/log/ and has no script blocks and no option
# that runs or mails something or sets an owner, a mode or a directory of the files it writes.
# The stage checks it before the owner signs, the target's gate before it runs.

SYSCTL_DIR = "/etc/sysctl.d"
LOGROTATE_DIR = "/etc/logrotate.d"
DROP_DIRS = (SYSCTL_DIR, LOGROTATE_DIR)
MAX_CRON = 1000
MAX_DROP = 16 * 1024
_DROP_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\Z")
_CRON_SPECIAL = ("@reboot", "@yearly", "@annually", "@monthly", "@weekly", "@daily", "@midnight", "@hourly")
_CRON_FIELD = re.compile(r"(?:\*|[0-9]{1,2}(?:-[0-9]{1,2})?|[A-Za-z]{3})(?:/[0-9]{1,2})?\Z")
_CRON_LIMITS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
_SYSCTL_LINE = re.compile(r"-?([A-Za-z0-9_][A-Za-z0-9_.*/-]*)[ \t]*=[ \t]*(.*)\Z")
# keys whose value is a program the kernel starts (as root) or a mask of what such helpers may do; a binfmt_misc
# entry names the interpreter the kernel runs for every matching file
_SYSCTL_EXEC = ("kernel.core_pattern", "kernel.modprobe", "kernel.poweroff_cmd", "kernel.hotplug",
                "kernel.usermodehelper", "fs.binfmt_misc")
# the network's keys: applied at once, they change access (forwarding, filters, routes) — a critical action's
_SYSCTL_NET = "net"
# the kernel's own protections: ptrace between processes, links and FIFOs in sticky directories, setuid core dumps,
# kernel addresses and its log, perf, unprivileged BPF, user namespaces and their AppArmor limits, ASLR, the null page,
# userfaultfd, io_uring, tty line disciplines and TIOCSTI, magic SysRq. Applied at once, a weaker value is a critical
# action's, like the network's. A name ending in "_" is a family (every key it starts); the rest is a key or a subtree.
_SYSCTL_GUARDS = ("kernel.yama", "fs.protected_", "fs.suid_dumpable", "kernel.kptr_restrict", "kernel.dmesg_restrict",
                  "kernel.perf_event_paranoid", "kernel.unprivileged_", "kernel.apparmor_", "kernel.randomize_va_space",
                  "kernel.exec-shield", "kernel.sysrq", "kernel.io_uring_", "vm.mmap_min_addr", "vm.unprivileged_",
                  "dev.tty.ldisc_autoload", "dev.tty.legacy_tiocsti", "user.max_user_namespaces")


def _sysctl_guarded(key: str) -> bool:
    return any(key.startswith(guard) if guard.endswith("_") else key == guard or key.startswith(guard + ".")
               for guard in _SYSCTL_GUARDS)
# logrotate words that run a program (script blocks, compressors) or send the logs away (mail), or read another file
_LOGROTATE_DENY = ("prerotate", "postrotate", "firstaction", "lastaction", "preremove", "endscript", "sharedscripts",
                   "compresscmd", "uncompresscmd", "mail", "mailfirst", "maillast", "include", "tabooext", "taboopat")
# logrotate words that give the files it writes an owner and a mode, rotate as another user, or write a log's
# copies into another directory: with them a stanza hands a file to someone or puts one where the step does not say
_LOGROTATE_FILES = ("create", "su", "olddir", "createolddir", "copy")
LOG_ROOT = "/var/log/"
# A stanza's file name: under /var/log/, plain characters and the globs * and ?, no segment that starts with a dot
# (a glob there — .* — matches ..), no quote, backslash, bracket, brace, ~ or $. Application trees are not log
# roots: their account may write there, and root rotating a directory another user writes is the classic way up.
_LOG_PATH = re.compile(r"/var/log(?:/[A-Za-z0-9_*?+@:,=-][A-Za-z0-9_.*?+@:,=-]*)+\Z")


def _unreadable(text: str, allowed: str = "") -> bool:
    """A control or invisible character the owner could not see (a carriage return included)."""
    return any(ch not in allowed and not ch.isprintable() for ch in text)


def _cron_split(line: str) -> Tuple[Optional[str], List[str], str]:
    """(problem, schedule words, command) of one cron line."""
    words = line.split()
    if not words:
        return "строка cron пуста", [], ""
    if words[0].startswith("@"):
        if words[0] not in _CRON_SPECIAL:
            return "расписание cron %s неизвестно" % words[0], [], ""
        schedule = words[:1]
    else:
        schedule = words[:5]
        if len(schedule) < 5:
            return "в строке cron нет расписания из пяти полей", [], ""
        for field, (low, high) in zip(schedule, _CRON_LIMITS):
            for part in field.split(","):
                if not _CRON_FIELD.match(part):
                    return "поле расписания cron %s не читается" % field, [], ""
                for number in re.findall(r"(?<![/0-9])[0-9]+", part.split("/")[0]):
                    if not low <= int(number) <= high:
                        return "поле расписания cron %s вне %d–%d" % (field, low, high), [], ""
    rest = line.strip()
    for word in schedule:
        rest = rest[len(word):].lstrip(" \t")
    if not rest:
        return "в строке cron нет команды", schedule, ""
    return None, schedule, rest


def _cron_shell(command: str) -> Tuple[str, bool]:
    """(what cron hands to the shell, whether an unescaped % cut it). cron ends the command at the first % a
    backslash does not escape and feeds the rest to it on stdin (each further % a newline); ``\\%`` is a literal %
    and loses its backslash, any other backslash stays (vixie cron and cronie read it the same way)."""
    out, escaped = [], False
    for ch in command:
        if escaped:
            if ch == "%":
                out[-1] = ch
            else:
                out.append(ch)
            escaped = False
        elif ch == "%":
            return "".join(out), True
        else:
            out.append(ch)
            escaped = ch == "\\"
    return "".join(out), False


def cron_command(line: str) -> str:
    """The command of a cron line as cron hands it to the shell (after its schedule, ``\\%`` as %, up to an
    unescaped %); "" when the line has none."""
    return _cron_shell(_cron_split(line)[2])[0]


def cron_problem(line: str, site_units=ALL_UNITS) -> Optional[str]:
    """Why a cron.line step may not carry this line, or None: one line up to MAX_CRON characters, nothing invisible,
    a schedule of five fields or an @word, no unescaped % (cron would feed what follows it to the command on
    stdin — text rules 1 and 2 never read as one), and a command that passes rules 1 and 2."""
    if not isinstance(line, str) or not line.strip():
        return "строка cron пуста"
    if "\n" in line or "\r" in line:
        return "cron.line — одна строка"
    if len(line) > MAX_CRON:
        return "строка cron длиннее %d знаков" % MAX_CRON
    if _unreadable(line, "\t"):
        return "в строке cron невидимый или управляющий знак"
    problem, _, command = _cron_split(line)
    if problem:
        return problem
    command, cut = _cron_shell(command)
    if cut:
        return ("знак % в команде cron без \\ перед ним: cron обрывает на нём команду, а остаток подаёт ей на вход; "
                "буквальный % пишется как \\%")
    refusal = plan_step(0, command, site_units)
    return "команда строки cron: %s" % refusal if refusal else None


def drop_problem(directory: str, name: str, text: str) -> Optional[str]:
    """Why a file.drop step may not put this file, or None."""
    if directory not in DROP_DIRS:
        return "каталог %s не из списка (%s)" % (directory, ", ".join(DROP_DIRS))
    if not isinstance(name, str) or not _DROP_NAME.match(name) or ".." in name:
        return "имя файла — одно простое имя, без пути и без точки в начале"
    if directory == SYSCTL_DIR and not name.endswith(".conf"):
        return "файл sysctl.d читается, только если его имя кончается на .conf"
    if not isinstance(text, str) or not text.strip():
        return "текст файла пуст"
    if len(text) > MAX_DROP:
        return "текст файла длиннее %d знаков" % MAX_DROP
    if _unreadable(text, "\t\n"):
        return "в тексте файла невидимый или управляющий знак"
    if PACKED.search(text):
        return PACKED_FOUND
    if directory == SYSCTL_DIR:
        for line in text.split("\n"):
            body = line.strip()
            if not body or body[0] in "#;":
                continue
            match = _SYSCTL_LINE.match(body)
            if not match:
                return "строка sysctl не вида ключ = значение: %s" % body[:80]
            key = match.group(1).replace("/", ".")
            if "*" in key:  # systemd-sysctl expands it at boot: n*.ipv4… is net.ipv4…, kernel.core_patter* is one
                return "ключ sysctl %s с шаблоном * — ключ пишется полностью" % key[:80]
            if key == _SYSCTL_NET or key.startswith(_SYSCTL_NET + "."):
                return ("ключ %s меняет сетевой доступ сразу (sysctl -p) — только критичным действием с подписью "
                        "владельца (kd-admin-propose-action)" % key[:80])
            if _sysctl_guarded(key):
                return ("ключ %s меняет защиту ядра сразу (sysctl -p) — только критичным действием с подписью "
                        "владельца (kd-admin-propose-action)" % key[:80])
            for denied in _SYSCTL_EXEC:
                if key == denied or key.startswith(denied + "."):
                    return "ключ %s запускает программу ядром — только скриптом с подписью владельца" % denied
            if "|" in match.group(2):
                return "знак | в значении sysctl"
        return None
    return _logrotate_problem(text)


def _log_path(word: str) -> bool:
    """A file name of a stanza that names only logs under /var/log/ (one pair of quotes around it is no part of it)."""
    if len(word) > 1 and word[0] == word[-1] and word[0] in "\"'":
        word = word[1:-1]
    return bool(_LOG_PATH.match(word))


def _logrotate_problem(text: str) -> Optional[str]:
    """Read a logrotate file as logrotate does: file names up to a ``{`` — over several lines, comment lines among
    them included —, then directives up to ``}``; a directive is the first word of a line or of a part between
    braces (its ``# comment`` is no word). Every file name lies under /var/log/, no directive runs a program, sends
    or reads a file, or sets an owner, a mode or a directory."""
    depth, names = 0, []  # type: int, List[str]
    for line in text.split("\n"):
        if not depth and not names and line.lstrip().startswith("#"):
            continue  # a comment line; inside a list of file names logrotate reads it as names
        for part in re.split(r"([{}])", line):
            if part == "{":
                if depth or not names:
                    return "{ в logrotate без списка путей перед ним или внутри другой стансы"
                depth, names = 1, []
            elif part == "}":
                if not depth:
                    return "} в logrotate без открытой стансы"
                depth = 0
            elif part.split():
                if depth or (not names and part.split()[0][:1].isalpha()):
                    words = part.split("#", 1)[0].split()
                    if not words:
                        continue
                    word = words[0].lower()
                    if not word[:1].isalpha():
                        return "в стансе logrotate не директива: %s" % words[0][:80]
                    if word in _LOGROTATE_DENY:
                        return "%s в logrotate запускает программу, отправляет журналы или читает другой файл" % word
                    if word in _LOGROTATE_FILES:
                        return ("%s в logrotate задаёт файлам владельца, права или каталог — такой файл только "
                                "скриптом с подписью владельца" % word)
                else:
                    for word in part.split():
                        if not _log_path(word):
                            return "путь %s вне %s: logrotate.d ротирует только журналы под %s" % (
                                word[:80], LOG_ROOT, LOG_ROOT)
                    names += part.split()
    if depth:
        return "станса logrotate без закрывающей }"
    if names:
        return "пути logrotate без { после них"
    return None
