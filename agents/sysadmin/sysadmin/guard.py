"""PreToolUse (Bash) hook of the sysadmin agent: a refusal before the confirmation card.

1) In the whole command: a direct call of the executor, the exec user and the exec key, the Claude account, the
   alert token, the Hindsight key, Forgejo tokens (the forge/ directory), the dev key and the forge user (names from
   kd.naming).
2) In the local part: sudo in command position (also behind env/timeout/…), files with passwords, a write to the
   agent's settings and environment (~/.claude, ~/.claude.json, ~/.ssh, ~/.local, ~/.bun, ~/.config, the shell
   profile), the board's nightly snapshot other than through kd-task, overriding KD_MONITOR_*, a write to
   /var/lib/digipaddock (the monitor's state; through the parent and by a glob in the path too).
3) Every ssh-exec call — the single list of prohibitions for its server (for a group a,b,c — for each server).
The main protection is in ssh_exec.py; the hook spares the owner cards that would be rejected anyway.
Claude Code lets a call through when a hook fails (code ≠ 2), so any failure is code 2.
The common base (command parsing, sudo, local writes, the hook's plumbing) is common/kd/guard_base.py.
"""
import fnmatch
import posixpath
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))  # …/DigiPaddock/common — as in denylist.py
from kd import cap_expect, guard_base, ids, naming, shell_calls  # noqa: E402
from kd.guard_base import (AGENT_NAME, HOME_REF, LITERAL, OPERATORS, SEPARATORS,  # noqa: E402,F401
                           runs_sudo, split_newlines, strip_comments, sudo_in_parsed, tokens, writes_local)
from sysadmin import denylist, inventory  # noqa: E402

AGENT = "sysadmin"
EXEC_USER = naming.service_user("exec")
FORGE_USER = naming.service_user("forge")
ALWAYS = (
    (re.compile(r"ssh_exec\.py"), "исполнитель вызывается только через ssh-exec"),
    # agent[-_]exec: the exec key file keeps its name (agent_exec_ed25519) and the legacy user stays covered
    (re.compile(r"agent[-_]exec|" + re.escape(EXEC_USER)), f"пользователь {EXEC_USER} и exec-ключ агенту недоступны"),
    (re.compile(r"\.credentials\.json|claude\.env|pult\.env|/etc/digipaddock/llm|coding-agent\.json|coding-agent\.env"
                r"|\.hindsight/|/etc/hindsight"),
     "учётка и токен Claude, ключ провайдера моделей, токен тревог в Telegram и память Hindsight недоступны"),
    # ssh-exec on the factory host runs as root: the denylist is frozen and closes only writes — reading tokens is
    # closed by the hook
    (re.compile(r"/etc/digipaddock/forge|(?<![\w-])forge/|" + naming.token_files_re() + "|" + re.escape(FORGE_USER)
                + r"|dev_ed25519"),
     f"токены Forgejo, ключ dev и учётка {FORGE_USER} недоступны"),
)
LOCAL = (
    (re.compile(r"\.ssh/|_ed25519"), "ключи SSH недоступны"),
    (re.compile(r"config\.ini|(?<![\w-])\.env\b|\.my\.cnf|debian\.cnf"), "файлы с паролями читать нельзя"),
    # KD_MONITOR_STATE/KD_MONITOR_CONFIG=… kd-monitor tick would publish pages from a planted state
    (re.compile(r"(^|[\s=])KD_MONITOR_[A-Z_]*="),
     "kd-monitor работает только со своим состоянием и конфигом: KD_MONITOR_* не переопределяются"),
    # the baseline changes only by a monitor_trust decision; its path in any local command (inside python3 -c too)
    (re.compile(r"monitor/+baseline\.json"),
     "эталон монитора (baseline.json) меняет только монитор по решению monitor_trust с подписью владельца: "
     "kd-monitor accept|baseline|trust — предложение, доверенные адреса — kd-monitor list --trusted"),
)
# the board's nightly md snapshot (kd-cabinet export): its files are neither read nor written — cards only through kd-task
BOARD = "/srv/digipaddock-agents/board"
BOARD_TOOLS = {naming.command("kd-task")}
BOARD_WHY = "доска — только через kd-task"
EXTRA_SLASHES = re.compile(r"/(?:\.?/)+")  # /srv//x and /srv/./x are the same /srv/x
FORMAT = "формат: ssh-exec --task KD-<n> <сервер> '<команда>' — напрямую, без обёрток"
TASK = ids.CARD  # ssh-exec --task KD-<n>: the executor checks the grant
PLAIN = FORMAT + "; команда — одна обычная строка в кавычках, без подстановок, «--», перенаправлений и переноса через «\\»"
ALL_EXEC = "ssh-exec: список серверов перечисли явно — all для выполнения не принимается"
# Claude Code's user settings, keys, PATH (~/.local/bin, ~/.bun/bin) and the agent's shell profile
# the sysadmin runs as its own user; the base user's home (the owner of digipaddock) is guarded the same way
AGENT_HOME = naming.home(naming.agent_user("sysadmin"))
BASE_HOME = naming.home(naming.base_user())
AGENT_CONFIG = tuple(home + "/" + name for home in (AGENT_HOME, BASE_HOME) for name in (
    ".claude", ".ssh", ".local", ".bun", ".config", ".bashrc", ".profile", ".bash_profile",
    ".claude.json"))
AGENT_CONFIG_WHY = ("запись в настройки и окружение агента (~/.claude, ~/.claude.json, ~/.ssh, ~/.local, ~/.bun, ~/.config, ~/.bashrc, "
                    "~/.profile, ~/.bash_profile) запрещена")
# the monitor's state (registry, baseline) and its pages: only kd-monitor writes them (as the same agent) — writing
# is closed to the agent
MONITOR_TREE = "/var/lib/digipaddock"
MONITOR_WHY = "состояние монитора и его страницы (/var/lib/digipaddock) меняет только kd-monitor"
# the denylist does not count touch and mkdir as writes, but here they are: a directory in place of a page
# (mkdir -p …/index.html) is EISDIR for os.replace and every page stalls; touch -d … index.html silences the
# morning line about stale pages
MONITOR_CHANGERS = denylist._ATTR_CHANGERS | {"touch", "mkdir"}
GLOB = re.compile(r"[*?\[]")
PATH_ARG = re.compile(r"(^|[=:>])(/[^\s;&|()<>]*)")  # an absolute path in a word: /x, of=/x, --opt=/x, >/x
# Claude Code (2.1.289) sends the owner a card for an ssh-ro whose separate argument is a path (one that exists on
# the factory host outside the working folders) or "=", despite Bash(ssh-ro *) in allow; the hook's allow does not
# lift it. The same command as one quoted argument passes without a card — so it is refused earlier, with a hint.
RO_QUOTE = ("ssh-ro: удалённую команду с путём, ~ или «=» передай одним аргументом в одинарных кавычках — "
            "ssh-ro <сервер> 'cat /etc/passwd'; без кавычек Claude Code отправит вызов владельцу на карточку")
RO_RISKY = re.compile(r"^[/~=]")
# The gateway's exact forms (SUDO_EXACT) whose text names root's keys: only as an ssh-ro remote command —
# fingerprints, not keys; outside ssh-ro the rule "SSH keys are not available" holds as before
RO_EXACT = {"ssh-keygen -lf /root/.ssh/authorized_keys"}
# the exec gate's mode (gate.json: alias, origin, mode, site units — no keys) and its allow/deny records
# (syslog, authpriv) — read-only and only as the whole ssh-ro remote command; the exec user and key stay closed
RO_GATE = re.compile(r"cat /etc/agent-exec-gate/gate\.json"
                     r"|journalctl -t agent-exec-gate --no-pager(?: -n \d{1,5})?(?: --since \d{4}-\d{2}-\d{2})?"
                     r"|grep -F agent-exec-gate /var/log/(?:auth\.log|secure)")
EXEC_NAME = ALWAYS[1][0]
REDIRECT = re.compile(r"^\d*[<>]")  # 2>/dev/null, >x, and a bare operator (>, 2>) — with the next word as its target


def unplain_ssh_exec(command: str) -> bool:
    """True when the ssh-exec call has something bash would expand or re-parse before the start (substitutions and any
    $, the globs * ? [, a ~ at the start of a word or after = and :, `--`, redirections, a backslash line continuation):
    otherwise the hook and the card would see one command and the executor would get another. Single quotes are
    safe, double quotes are not. The parser is shared with the expected channel calls (kd.shell_calls)."""
    return shell_calls.unsafe_call_of(command, "ssh-exec")


def _parse_local(local: List[str]) -> denylist.Parsed:
    """The words are already unquoted (shlex): the remaining quotes, "\\" and a "#" at the start of a word are literal;
    for parsing they are removed so that they do not join into new quotes or a comment. ~, ~<base user>, $HOME and
    ${HOME} are AGENT_HOME."""
    words = [w if w in OPERATORS else LITERAL.sub("", w).lstrip("#") for w in local]
    return denylist.parse(HOME_REF.sub(AGENT_HOME, " ".join(words)))


def _unglob(path: str, tree: str) -> str:
    """A path with a glob that may match tree or its parent (/var/lib/digi*/x, /var/l?b, /var/*) becomes the same path
    with tree's components in place of the globs; other paths stay as they are. bash expands the glob, and the
    prohibition parser does not see it."""
    if not GLOB.search(path):
        return path
    parts = [c for c in posixpath.normpath(path).split("/") if c]
    tree_parts = tree.strip("/").split("/")
    n = min(len(parts), len(tree_parts))
    if not all(fnmatch.fnmatchcase(t, g) for t, g in zip(tree_parts, parts[:n])):
        return path
    return "/" + "/".join(tree_parts[:n] + parts[n:])


def writes_monitor(local: List[str]) -> bool:
    """A local write to the monitor's state and pages: as writes_local (the same parser as the denylist's), plus touch
    and mkdir, the tree's parent (rm -r /var/lib, chmod -R /var, mv /var/lib …) and globs in the path (/var/lib/digi*/…)."""
    words = [w if w in OPERATORS else PATH_ARG.sub(lambda m: m.group(1) + _unglob(m.group(2), MONITOR_TREE), w)
             for w in local]
    p = _parse_local(words)
    trees = (MONITOR_TREE,)
    return (denylist._writes_into(p, trees, denylist._COPIERS, MONITOR_CHANGERS, exempt=False)
            or _encloses_tree(p, trees))


def writes_agent_config(local: List[str]) -> bool:
    """A local write to the agent's settings and environment (~/.claude, ~/.ssh, the shell profile …)."""
    return writes_local(local, AGENT_CONFIG, home=AGENT_HOME)


def touches_board(local: List[str]) -> bool:
    """A board path in a segment whose command (the first word) is not kd-task, and a write to the board by
    redirection, tee, cp … under any command (kd-task … > a board file). Each segment of ; && | separately."""
    segment: List[str] = []
    for word in local + [";"]:
        if word not in OPERATORS:
            segment.append(EXTRA_SLASHES.sub("/", word))
            continue
        if segment and segment[0] not in BOARD_TOOLS and any(BOARD in w for w in segment):
            return True
        segment = []
    return writes_local(local, (BOARD,), home=AGENT_HOME)


def _encloses_tree(p, trees) -> bool:
    """The command touches trees through a parent directory: chmod/chown/chgrp/chattr/setfacl/rmdir or rm -r on it,
    or mv of it. The same check as the second part of the denylist's cage-write (its code is not touched: the denylist is frozen)."""
    for argv in (denylist._strip_redirects(denylist._live(a)) for a in p.argvs):
        cmd, args = denylist._cmd(argv), argv[1:]
        operands = [a for a in args if not a.startswith("-")]
        recursive_rm = cmd == "rm" and (denylist._short_has(args, "rR") or "--recursive" in args)
        if (cmd in denylist._ATTR_CHANGERS or recursive_rm) and any(denylist._encloses(a, trees) for a in operands):
            return True
        if cmd == "mv" and any(denylist._encloses(a, trees) for a in denylist._mv_sources(args)):
            return True
    return False


def split_calls(words: List[str]) -> Tuple[List[str], List[Tuple[str, str]], bool]:
    """(local words, [(server, remote command)], whether there is an ssh-exec outside command position)."""
    local: List[str] = []
    calls: List[Tuple[str, str]] = []
    stray = False
    i, at_start = 0, True
    while i < len(words):
        word = words[i]
        if word in OPERATORS:
            local.append(word)
            at_start = True
            i += 1
            continue
        if at_start and posixpath.basename(word) == "ssh-exec":
            j = i + 1
            while j < len(words) and words[j] not in OPERATORS:
                j += 1
            args = words[i + 1:j]
            # a card of an earlier prefix too: the channel turns it into the card (kd-cap-serve)
            if len(args) > 1 and args[0] == "--task" and TASK.fullmatch(ids.canonical(args[1])):
                args = args[2:]
            try:
                remote = inventory.join_command(args[1:])
            except inventory.InventoryError:
                remote = ""  # an empty command — refused for its format in decide
            calls.append((args[0] if args else "", remote))
            i, at_start = j, False
            continue
        if "ssh-exec" in word:
            stray = True
        local.append(word)
        at_start = False
        i += 1
    return local, calls, stray


def unquoted_ro(words: List[str]) -> bool:
    """An ssh-ro call (in command position) has its remote part as separate words, and one of them is a path, a ~ or
    "=": Claude Code asks the owner about that. Redirections and their targets are local and do not count."""
    i, at_start = 0, True
    while i < len(words):
        word = words[i]
        if word in OPERATORS:
            i, at_start = i + 1, True
            continue
        if at_start and posixpath.basename(word) == "ssh-ro":
            j = i + 1
            # ( and ) among the arguments come only from quotes (bash would not parse them otherwise): ss -tn '(' dport = :443 ')'
            while j < len(words) and (words[j] not in OPERATORS or words[j] in "()"):
                j += 1
            args, k = words[i + 2:j], 0  # without the server's name
            while k < len(args):
                if REDIRECT.match(args[k]):
                    k += 2 if re.fullmatch(r"\d*[<>]+", args[k]) else 1
                    continue
                if RO_RISKY.match(args[k]) and not any(c.isspace() for c in args[k]):  # spaces: one quoted command
                    return True
                k += 1
            i, at_start = j, False
            continue
        i, at_start = i + 1, False
    return False


def _without_ro_exact(words: List[str]) -> List[str]:
    """The words with an ssh-ro remote command (in command position, as one argument) from RO_EXACT replaced by its name."""
    out: List[str] = []
    for i, word in enumerate(words):
        if (word in RO_EXACT and i >= 2 and posixpath.basename(words[i - 2]) == "ssh-ro"
                and (i == 2 or words[i - 3] in OPERATORS)):
            word = word.split()[0]
        out.append(word)
    return out


def _ro_gate_reads(command: str) -> int:
    """How many times the exec gate's name occurs inside ssh-ro remote commands of RO_GATE: each one ssh-ro's single
    argument after the server, ssh-ro in command position. 0 when the command does not parse."""
    try:
        words = tokens(command)
    except ValueError:
        return 0
    count = 0
    for i, word in enumerate(words):
        if (RO_GATE.fullmatch(word) and i >= 2 and posixpath.basename(words[i - 2]) == "ssh-ro"
                and (i == 2 or words[i - 3] in OPERATORS)
                and (i + 1 == len(words) or words[i + 1] in OPERATORS)):
            count += len(EXEC_NAME.findall(word))
    return count


def decide(command: str) -> Optional[str]:
    """The reason to refuse, or None."""
    flat = denylist.parse(command).flat  # unquoted: ssh_ex""ec.py → ssh_exec.py
    gate_reads = _ro_gate_reads(command)
    for pattern, why in ALWAYS:  # over the original text, comments included — the stricter way
        if pattern is EXEC_NAME and gate_reads:
            # every occurrence of the name must be one of the gate's read forms — in the text and without
            # quotes
            if len(pattern.findall(command)) != gate_reads or len(pattern.findall(flat)) != gate_reads:
                return why
            continue
        if pattern.search(command) or pattern.search(flat):
            return why
    command = strip_comments(command)
    try:
        words = tokens(command)
    except ValueError:
        return "не удалось разобрать команду (незакрытые кавычки)"
    local, calls, stray = split_calls(words)
    if stray:
        return FORMAT
    if unplain_ssh_exec(command):
        return PLAIN
    local_text = " ".join(local)
    if runs_sudo(local) or sudo_in_parsed(local_text):
        return "sudo агенту запрещён"
    pattern_text = " ".join(_without_ro_exact(local))
    for pattern, why in LOCAL:
        if pattern.search(pattern_text):
            return why
    if touches_board(local):
        return BOARD_WHY
    if writes_agent_config(local):
        return AGENT_CONFIG_WHY
    if writes_monitor(local):
        return MONITOR_WHY
    if unquoted_ro(local):
        return RO_QUOTE
    for server, remote in calls:
        if not server or server.startswith("-") or not remote.strip():
            return FORMAT
        aliases = server.split(",")  # a group call a,b,c: the card names each server explicitly
        if "all" in aliases:
            return ALL_EXEC
        if "" in aliases or len(aliases) > inventory.EXEC_MAX:
            return FORMAT
        if len(aliases) > 1:
            try:
                inventory.exec_aliases(server)  # a repeat, production not last — the executor would refuse anyway
            except inventory.InventoryError as exc:
                return str(exc)
        for alias in aliases:
            rule = denylist.check(remote, alias)
            if rule:
                return f"запрещено правилом {rule.name}: {rule.why}. Такое делает владелец руками"
    return None


def main(stdin=None, stderr=None, stdout=None, after_bash=None) -> int:
    """The single PreToolUse of the sysadmin: all the guard's checks first, then the refusal of `cd X && …` and the record
    of expected channel calls; a command that passed gets allow — its local part runs in the sandbox without a card."""
    # the sysadmin does not need the event's cwd: its writes are checked against absolute trees
    return guard_base.run_hook(lambda command, cwd: decide(command), stdin, stderr, stdout=stdout,
                               allow_on_success=True,
                               after_bash=after_bash if after_bash is not None else cap_expect.after_bash(AGENT))
