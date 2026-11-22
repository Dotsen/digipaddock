"""The shared base of the agents' PreToolUse hooks (moved out of agents/sysadmin/sysadmin/guard.py on 06.10.2026; the behaviour did not change).

Parsing of a command before the parser (comments, newlines, shlex words), sudo in the command position, a local write
into the given trees and the hook's harness: parsing of the event and code 2 on any failure — Claude Code
lets the call through when a hook fails (a code ≠ 2). Parsing of shell commands — kd.shell; the rules are in each agent's guard.
"""
import json
import posixpath
import re
import shlex
import sys
from typing import Callable, List, Optional, Sequence, Tuple

from kd import naming, shell

OPERATORS = {";", "&&", "||", "|", "&", "(", ")", ";;", "|&", ";&"}
SEPARATORS = ";&|()\n"
BASE_HOME = naming.home(naming.base_user())
# ~, ~<base user>, $HOME, ${HOME}
HOME_REF = re.compile(r"(?<![^\s=:>])~(?:" + re.escape(naming.base_user()) + r")?(?=/|\s|$)|\$\{HOME\}|\$HOME(?!\w)")
# ~ and ~user at the start of a word (and after = : >), the way bash expands them; ~+ and ~- (the directory stack) are not included
TILDE = re.compile(r"(?<![^\s=:>])~([a-z_][a-z0-9_-]*)?(?=/|\s|$)")
HOME_VAR = re.compile(r"\$\{HOME\}|\$HOME(?!\w)")
LITERAL = re.compile(r"[\"'\\]")
AGENT_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def expand_home(text: str, home: str) -> str:
    """~, $HOME and ${HOME} → home (the home of the agent's user), ~user → that user's home (/root for root, otherwise /home/<name>).
    For agents under their own user (<base>-dev): HOME_REF takes ~<base> for its home and does not know ~<base>-dev.
    ~+, ~- and ~ with other characters stay as they are — the guard itself decides what to do with them."""
    def user_home(match) -> str:
        user = match.group(1)
        if not user:
            return home
        return "/root" if user == "root" else "/home/" + user
    return HOME_VAR.sub(home, TILDE.sub(user_home, text))


def strip_comments(command: str) -> str:
    """Removes comments: "#" at the start of a word (after the beginning, a space or ;&|() ) outside quotes — up to the
    end of the line. Otherwise an apostrophe in a comment ("it's") would be taken for a quote and hide substitutions and calls."""
    out: List[str] = []
    quote = ""
    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\" and i + 1 < n:
            out.append(ch)
            i += 1
            ch = command[i]
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (not out or out[-1].isspace() or out[-1] in ";&|()"):
            while i < n and command[i] != "\n":
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_newlines(command: str) -> str:
    """A newline outside quotes is a command boundary (" ; "); ">|" outside quotes is the same ">" (shlex would cut it
    into ">" and "|", and the write would get lost behind an imaginary pipeline). Inside quotes and after a backslash
    everything stays as it is."""
    out: List[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "\\" and i + 1 < len(command):
            out.append(ch)
            i += 1
            ch = command[i]
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in "'\"":
            quote = ch
        elif ch == "\n":
            ch = " ; "
        elif ch == ">" and command[i + 1:i + 2] == "|":
            i += 1  # "|" after ">" is part of the redirection, not a pipeline
        out.append(ch)
        i += 1
    return "".join(out)


def tokens(command: str) -> List[str]:
    """The command's words (quotes removed) and the operators ; & | ( ) as separate words; ValueError — unclosed quotes."""
    lexer = shlex.shlex(split_newlines(command), posix=True, punctuation_chars=";&|()")
    lexer.whitespace_split = True
    lexer.commenters = ""  # otherwise "a#b" would cut off the rest of the command
    return list(lexer)


def runs_sudo(local: List[str]) -> bool:
    """sudo in the command position of the local part — itself or behind wrappers (env, timeout, nice …)."""
    segment: List[str] = []
    for word in local + [";"]:
        if word not in OPERATORS:
            segment.append(word)
            continue
        wrappers = segment[:len(segment) - len(shell.command_argv(segment))]
        if any(posixpath.basename(w) == "sudo" for w in wrappers):
            return True
        segment = []
    return False


def sudo_in_parsed(text: str) -> bool:
    """sudo in the command position after the prohibition parser's pass (quotes removed, bash -c / eval unfolded)."""
    for segment in shell.parse(text).segments:
        wrappers = segment[:len(segment) - len(shell.command_argv(segment))]
        if any(posixpath.basename(w) == "sudo" for w in wrappers):
            return True
    return False


def writes_local(local: List[str], trees, *, home: str = BASE_HOME,
                 refs: Sequence[Tuple[re.Pattern, str]] = ()) -> bool:
    """A local write inside trees: a redirection, tee, cp/mv/install/ln, sed -i, truncate, chmod/chown, rm.
    The words are already without quotes (shlex): the remaining quotes, backslashes and a "#" at the start of a word are
    literal; for the parse they are removed, so that they do not stick together into new quotes or a comment. ~, ~<base>,
    $HOME and ${HOME} are home; refs — further substitutions (regex, path) after home."""
    words = [w if w in OPERATORS else LITERAL.sub("", w).lstrip("#") for w in local]
    text = HOME_REF.sub(home, " ".join(words))
    for pattern, path in refs:
        text = pattern.sub(path, text)
    return shell.writes_into(shell.parse(text), trees)


def run_hook(decide_bash: Callable[[str, Optional[str]], Optional[str]],
             stdin=None, stderr=None, stdout=None, allow_on_success: bool = False,
             after_bash: Optional[Callable[[dict], Optional[str]]] = None) -> int:
    """The hook's harness: a PreToolUse event from stdin → the refusal's reason → "⛔ reason" on stderr and code 2,
    otherwise 0. Bash — decide_bash(the command, the event's cwd or None), other tools and any failure — a block.
    allow_on_success prints the decision allow: that lets the bare rule Bash be removed from permissions.allow, so that
    a hook timeout returns the call to the ordinary permission flow instead of approving it silently; there the policy's
    dontAsk mode refuses without a prompt for the owner (probed on the factory host on 11.10.2026).
    after_bash(event) — only after decide_bash let the Bash call through (recording the channels' expected calls); its
    reason or failure is a refusal too."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    stderr = stderr if stderr is not None else sys.stderr
    try:
        event = json.load(stdin)
        tool_input = event.get("tool_input")
        if not isinstance(tool_input, dict):
            raise TypeError("tool_input отсутствует или не объект")
        tool_name = event.get("tool_name", "Bash")  # Claude Code always sends the name; without it — the former Bash behaviour
        if tool_name == "Bash":
            command = tool_input.get("command", "")
            if not isinstance(command, str):
                raise TypeError("tool_input.command — не строка")
            cwd = event.get("cwd")  # the Claude Code session's directory (it remembers cd between Bash calls)
            reason = decide_bash(command, cwd if isinstance(cwd, str) and cwd else None)
            if not reason and after_bash is not None:
                reason = after_bash(event)
        else:
            reason = f"хук не знает инструмент {tool_name} — вызов заблокирован"
    except Exception as exc:  # noqa: BLE001 — a hook failure = a block, otherwise Claude Code would let the call through
        reason = f"deny_guard: внутренняя ошибка ({exc.__class__.__name__}) — вызов заблокирован"
    if reason:
        print(f"⛔ {naming.rewrite(reason)}", file=stderr)  # the host's command names
        return 2
    if allow_on_success:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                                "permissionDecisionReason": "command accepted by policy guard"}},
                         ensure_ascii=False), file=stdout)
    return 0
