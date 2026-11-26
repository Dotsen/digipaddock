"""PreToolUse (Bash) hook of the dev agent. It is the workflow's subject policy and an early stop, not a parser
boundary: Bash and its child processes are confined by Claude Code's mandatory OS sandbox; a merge by Forgejo's rights
and the owner's Approve of the exact SHA; the session by its own user <prefix>-dev and a narrow sudoers.
An internal error of the process exits 2, and a hook timeout returns the call to the ordinary permission flow (there is
no bare Bash allow), where the dontAsk mode refuses without a card for the owner."""
import fnmatch
import os
import posixpath
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))
from kd import cap_expect  # noqa: E402
from kd import guard_base as gb  # noqa: E402
from kd import naming  # noqa: E402
from kd import shell  # noqa: E402
from dev_agent import repos  # noqa: E402

AGENT = repos.AGENT_DIR
AGENT_HOME = naming.home(naming.agent_user("dev"))  # dev works under its own user
SLOT_RE = re.compile(r"^/srv/digipaddock-workers/[a-z][a-z0-9_-]{0,31}/worker[1-9]$")


def slot_root(env=os.environ) -> Optional[str]:
    """The worker slot this hook serves, from the root-set unit environment (C05); anything else is the developer."""
    value = env.get("KD_WORKER_SLOT")
    return value if isinstance(value, str) and SLOT_RE.match(value) else None


# Slot mode (a worker job of the session host): writes go to the slot's work/repo and work/tmp only, the
# agent's
# notes are not the worker's, and every command runs in the foreground — the session ends with the reply.
SLOT = slot_root()
WORK = (SLOT + "/work") if SLOT else AGENT + "/work"
TMP = WORK + "/tmp"
# a session writes its own notes (memory/tasks/<card>.md, memory/chats/<thread>.md); the shared
# memory/notes.md only through kd-notes add (appended under a lock), the split's memory/legacy/ never
SHARED_NOTES = AGENT + "/memory/notes.md"
SHARED_NOTES_DIR = AGENT + "/memory/legacy/"
NOTES_ROOTS = () if SLOT else (AGENT + "/memory/tasks/", AGENT + "/memory/chats/")
SHARED_NOTES_WHY = "общие заметки memory/notes.md — только через kd-notes add \"<текст>\" (дописывает под блокировкой)"
BACKGROUND_WHY = "a worker runs every command in the foreground: run_in_background is refused (the session ends with the reply)"
FORGE_USER, EXEC_USER = naming.service_user("forge"), naming.service_user("exec")
SECRETS = re.compile(r"/etc/digipaddock/forge|(?<![\w-])forge/|" + naming.token_files_re() + r"|\.ssh/|_ed25519"
                     r"|\.credentials\.json|claude\.env|pult\.env|/etc/digipaddock/llm|coding-agent\.json|coding-agent\.env|\.hindsight/|/etc/hindsight|"
                     + re.escape(FORGE_USER) + "|" + re.escape(EXEC_USER)
                     + r"|\.git-credentials|config\.ini|(?<![\w-])\.env\b")
SECRETS_WHY = f"секреты и чужие учётки недоступны (ключи, токены, память, {FORGE_USER}/{EXEC_USER})"
GIT_BAD = {"push": "git push запрещён — PR только через kd-pr", "rebase": "git rebase запрещён — историю не переписывать",
           "remote": "git remote запрещён", "config": "git config запрещён", "tag": "git tag запрещён",
           "filter-branch": "git filter-branch запрещён", "pull": "git pull запрещён — git fetch origin && git merge origin/<ветка>",
           "submodule": "git submodule запрещён (сетевой clone по адресам из .gitmodules)",
           # origin/* refs and objects are changed only by git fetch origin itself: otherwise kd-pr would compute the
           # cage from a substituted base
           "update-ref": "git update-ref запрещён — ссылки origin/* обновляет только git fetch origin",
           "symbolic-ref": "git symbolic-ref запрещён", "replace": "git replace запрещён (подмена объектов)",
           "-c": "git -c запрещён (обход правил)", "-C": "git -C запрещён — работай из клона"}
GIT_SUBCOMMANDS_BAD = {"push", "rebase", "remote", "config", "tag", "filter-branch", "pull", "submodule",
                       "update-ref", "symbolic-ref", "replace"}
GIT_WRAPPERS_OK = {"timeout", "nice"}
GIT_ENV = re.compile(r"GIT_[A-Z_]*=")
URL_LIKE = re.compile(r"://|@[^\s:/]*:")
NET_TOOLS = {"ssh": "ssh/ssh-ro/ssh-exec агенту закрыты: серверов у тебя нет", "ssh-ro": "ssh/ssh-ro/ssh-exec агенту закрыты",
             "ssh-exec": "ssh/ssh-ro/ssh-exec агенту закрыты", "scp": "scp закрыт", "sftp": "sftp закрыт", "rsync": "rsync закрыт",
             "curl": "curl закрыт (веб и API — через kd-forge)", "wget": "wget закрыт"}
DEPS_WHY = "напрямую закрыт: зависимости проекта по lock-файлу ставит kd-deps install (без установочных скриптов)"
PKG_TOOLS = {"pip": "pip запрещён — зависимости согласуй с владельцем", "pip3": "pip запрещён", "npm": "npm " + DEPS_WHY, "npx": "npx запрещён",
             "composer": "composer " + DEPS_WHY,  # kd-deps is the one door
             "bun": "bun запрещён", "apt": "apt запрещён", "apt-get": "apt запрещён", "dpkg": "dpkg запрещён"}
SHELLS = {"sh", "bash", "dash", "zsh", "eval", "exec"}
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
WRITERS = {"tee", "cp", "mv", "install", "ln", "rm", "rmdir", "unlink", "shred", "truncate", "touch", "mkdir", "chmod", "chown",
           "chgrp", "dd", "rsync", "sed"}
OWNER_FIRST = {"chmod", "chown", "chgrp"}  # the first operand is a mode or an owner, not a path
# .claude and .mcp.json anywhere in a path (through a link too: ln -s . y and y/.claude/… is the clone's .claude)
FORBIDDEN = re.compile(r"(^|/)\.claude(/|$)|(^|/)\.mcp\.json$")
FORBIDDEN_WHY = "запись в .claude/ и .mcp.json запрещена (клетка: правила агента меняет только владелец)"
# the root of work/ is the session's cwd (agent-launch): Claude Code loads CLAUDE.md and CLAUDE.local.md there as the
# agent's instructions (root's stubs, the agent installer). CLAUDE.md of the clones (work/<Repo>/…) the agent edits in
# a PR — this does not concern them
WORK_MEMORY = ("CLAUDE.md", "CLAUDE.local.md")
WORK_MEMORY_WHY = ("запись в work/CLAUDE.md и work/CLAUDE.local.md запрещена: их грузит каждая сессия агента "
                   "(заглушки владельца); как и CLAUDE.local.md в любой папке work/ и CLAUDE.md в work/tmp/ — их "
                   "сессия грузит, прочитав файл рядом; CLAUDE.md клона — можно")
WRITE_WHY = ((f"запись разрешена только в {WORK}/repo (кроме .claude и .mcp.json), {TMP}/ (тексты и вывод команд) "
              "и /dev/null; папка агента, /tmp — нет") if SLOT else
             (f"запись разрешена только в work/<Repo> (кроме .claude и .mcp.json), {TMP}/ (тексты, вложения и вывод команд), "
              "свои заметки memory/tasks/ и memory/chats/ папки агента и /dev/null; общий memory/notes.md "
              "(только kd-notes add), /tmp и прежний tmp/ папки агента — нет"))
GIT_DIR_WHY = ("запись в .git/ клона запрещена: его пишет сам git (commit, checkout); конфиг и хуки клона — не цель "
               "записи")
# Slot mode: the job's copy only, never the slot's other folders (review 04.11.2026)
WRITE_ROOTS = (WORK + "/repo/", TMP + "/") if SLOT else (WORK + "/", TMP + "/")
UNRESOLVED_WHY = ("цель записи не проверить (подстановка $…, `…`, маска, {…}, ~+/~-, xargs, env -C или cd, который мог "
                  "не сработать) — пиши путь явно; " + WRITE_WHY)
ANSI_WHY = "$'…' и $\"…\" запрещены: bash раскроет \\xNN/\\NNN, а проверка их не видит — пиши слова как есть"
MERGE_WHY = "kd-forge merge запрещён — merge только через kd-merge (карточка владельца)"
FORGE_EXPAND_WHY = "kd-forge: без {…} и масок (*, ?, […]) в словах — bash подставил бы другое слово (например, merge)"
ENV_OPTS_WHY = ("env с опциями запрещён (env -i, env -u, env -C/--chdir, env -S …): опции прячут команду и каталог "
                "записи — только env ИМЯ=значение команда")
CMD_NAME_WHY = "имя команды из подстановки, маски или скобок ($X, `…`, *, ?, […], {…}) запрещено — пиши команду как есть"
EXPANDS = re.compile(r"[{*?\[]")  # brace expansion and globs: bash will replace the word
ODD_NAME = re.compile(r"[$`{*?\[]")
NAME_OK = {"{", "}", "[", "[[", "]]"}  # the group { …; } and test
ARRAY_SET = re.compile(r"[A-Za-z_]\w*\[[^]]*\]\+?=")  # arr[0]=x is an assignment, not a command
SUBST = re.compile(r"\$\(|`|\$\{|\$[A-Za-z_]")
# parsing a write: the same command boundaries as shell.parse, but the operators are kept (for cd and subshells)
OPS = re.compile(r"(\$\(|`|&&|\|\||[;&|()\n])")
BRACE_WORD = re.compile(r"((?<!\S)[{}](?!\S))")  # { and } as separate words
DUP_FD = re.compile(r"\d*[<>]&(?:\d+|-)(?=[\s;&|()<>]|$)")  # 2>&1, >&2, <&0, >&- duplicate a descriptor, not a file
AMP_REDIR = re.compile(r"(?<!&)&(?=>)")  # &> and &>> are the same as > and >>
DANGLING = re.compile(r">\s*$")  # > without a target: a substitution follows ($(…), `…`, >(…)) — the target cannot be computed
BREAKS = {";", "\n", "||", "&", "{", "}"}  # after these the command runs even if a cd before && failed
CD_OPTS = re.compile(r"-[LPe@]+")
CHDIR_OPT = re.compile(r"-[^-]*[CDw]|--(chdir|working-directory|wd)\b")  # env -C, nsenter/unshare -w … — another directory
GLOB = re.compile(r"[*?\[]")
DOTGLOB = re.compile(r"dotglob|GLOBIGNORE")  # then * finds .claude too
VAR_REF = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
DQ = re.compile(r"""'[^']*'|"((?:[^"\\]|\\.)*)"|\\.""", re.S)
LOOP_MAX = 256  # for-substitutions per target
# parsing as bash does (_lex): the contexts where # is a comment and << a heredoc
CODE = ("code", "subst", "tick")
# arith is $((…)), arithc a bare ((…)) / for ((…)): a complete command, a # after it is a comment (it does not join
# the word)
CLOSERS = {"subst": ("(", ")"), "arith": ("(", ")"), "arithc": ("(", ")"), "param": ("{", "}"), "arithb": ("[", "]")}
WORD_START = " \t\n;&|()"
HEREDOC_DELIM = re.compile(r"[A-Za-z0-9_.-]+")
HEREDOC_WHY = ("heredoc без кавычек с $(…), `…` или \\ в конце строки тела (или с необычным разделителем) запрещён: "
               "тело выполняет подстановки — пиши <<'EOF'")
FORGE_SUBCOMMANDS = {"prs", "pr", "comments", "comment", "publish", "check", "status", "-h", "--help"}  # what the kd-forge wrapper accepts
FORGE_SUB_WHY = "kd-forge: только prs | pr | comments | comment | publish | check | status (merge — через kd-merge)"
SHA_RE = re.compile(r"[0-9a-f]{40}")
PR_RE = re.compile(r"[1-9][0-9]{0,6}")
CD_CMDS = {"cd", "pushd", "popd"}
CD_WHY = ("cd — только отдельным вызовом: Claude Code отправляет связку cd+команда владельцу на подтверждение. Сначала "
          "`cd <каталог>` одной командой, потом команды следующими вызовами (каталог сохраняется); pushd и popd — так же")


def _segments(words: List[str]) -> List[List[str]]:
    out, cur = [], []
    for w in words + [";"]:
        if w in gb.OPERATORS:
            if cur:
                out.append(cur)
            cur = []
        else:
            cur.append(w)
    return out


def _check_git(argv: List[str]) -> Optional[str]:
    """Global options before the subcommand (-c, -C, --git-dir bypass the rules), then the subcommand (push, pull, fetch by address, …)."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        opt = argv[i]
        if opt in ("-c", "-C") or (opt.startswith("-c") and len(opt) > 2) or opt.startswith("-C"):
            return GIT_BAD[opt[:2]]
        if opt == "--config-env" or opt.startswith("--config-env="):  # the same as -c, but the value comes from a variable
            return GIT_BAD["-c"]
        if opt in ("--git-dir", "--work-tree", "--exec-path", "--namespace") or opt.startswith(("--git-dir=", "--work-tree=", "--exec-path=")):
            return "git --git-dir/--work-tree запрещены — работай из клона"
        i += 1
    sub = argv[i] if i < len(argv) else ""
    if sub in GIT_SUBCOMMANDS_BAD:
        return GIT_BAD[sub]
    if sub == "reset" and "--hard" in argv:
        return "git reset --hard запрещён"
    if sub == "fetch":
        rest = argv[i + 1:]
        if any(a.startswith("--upload-pack") for a in rest) or any(URL_LIKE.search(a) for a in rest if not a.startswith("-")):
            return "git fetch — только origin или github, по имени (без адресов и --upload-pack)"
        # +refs/pull/N/head:refs/remotes/origin/main would replace the base of kd-pr's cage; an ordinary
        # fetch origin [branch] has no colon
        if any(a.startswith("--refmap") for a in rest) or any(":" in a for a in rest if not a.startswith("-")):
            return "git fetch — без refspec и --refmap: ссылки origin/* обновляет только сам fetch"
    return None


def _check_merge(seg: List[str], nseg: int) -> Optional[str]:
    if nseg != 1:
        return "kd-merge — одиночной командой, без ; && |"
    args = seg[1:]
    flags = [a for a in args if a.startswith("--")]
    pos = [a for a in args if not a.startswith("--")]
    if flags not in ([], ["--release"]) or len(pos) != 3:
        return "kd-merge <project> <N> <sha> [--release]"
    if not repos.PROJECT_RE.fullmatch(pos[0]) or not PR_RE.fullmatch(pos[1]) or not SHA_RE.fullmatch(pos[2]):
        return "kd-merge: project — [a-z0-9-], N — число, sha — полный, 40 hex (из kd-forge pr)"
    return None


Event = Tuple[str, object]  # ("op", operator) or ("seg", the words of a simple command)


def _lex(command: str) -> Tuple[str, bool]:
    """The command as bash reads it before splitting words — for all the guard's checks. Returns (text, dangerous heredoc).
    • \\+newline is removed (mer\\<nl>ge is merge), except in '…', $'…', comments and heredoc bodies;
    • a comment — # at the start of a word in code (not in quotes, ${…} or arithmetic) — runs to the end of the line; a
      \\ in it joins nothing;
    • heredoc bodies are removed — they are data. An opener is looked for only in code: in quotes, ${…}, $((…)), ((…)),
      $[…] a << is not a heredoc. The delimiter is exactly a line (<<- strips only tabs), as in bash; bodies are read
      after the nearest newline in code, in order. Not found — the rest stays code (stricter than bash, which takes
      it for the body).
    • a "dangerous heredoc" is an unquoted body (<<EOF) with $(…), `…` or a \\ at the end of a line (bash runs
      substitutions even in a line with #, and \\ joins the line to the delimiter), or a delimiter that cannot be
      parsed ($X, empty …)."""
    out: List[str] = []
    stack: List[List] = [["code", 0]]  # [context, bracket depth]
    # heredocs awaiting the newline: (delimiter, <<-, unquoted, depth of the context stack at <<)
    pending: List[Tuple[str, bool, bool, int]] = []
    unsafe = False
    glued_at = -1  # len(out) right after the ) of $(…)/$((…)): the word goes on, a # after it is not a comment
    n = len(command)

    def skip(k: int) -> int:  # bash removes \+newline before parsing
        while command.startswith("\\\n", k):
            k += 2
        return k

    def last() -> str:
        return out[-1][-1] if out and out[-1] else ""

    def in_tick() -> bool:
        return any(c == "tick" for c, _ in stack)

    def close_tick() -> None:  # The first unescaped backtick closes the backtick substitution, even inside quotes.
        while stack[-1][0] != "tick":
            stack.pop()
        stack.pop()

    def heredoc(k: int) -> int:
        """k is right after <<: [-], spaces, the delimiter (quotes are stripped); the body is queued. Returns the end of the word."""
        nonlocal unsafe
        dash = command[k:k + 1] == "-"
        k = skip(k + 1) if dash else k
        while k < n and command[k] in " \t":
            k = skip(k + 1)
        word: List[str] = []
        quoted = False
        while k < n and command[k] not in " \t\n;&|()<>":
            c = command[k]
            if c == "'":
                e = command.find("'", k + 1)
                if e < 0:
                    break
                word.append(command[k + 1:e])
                quoted, k = True, skip(e + 1)
            elif c == '"':
                e = k + 1
                while e < n and command[e] != '"':
                    if command[e] == "\\" and e + 1 < n:
                        word.append(command[e + 1] if command[e + 1] in '"\\$`' else command[e:e + 2])
                        e += 2
                    else:
                        word.append(command[e])
                        e += 1
                if e >= n:
                    break
                quoted, k = True, skip(e + 1)
            elif c == "\\" and k + 1 < n:
                word.append(command[k + 1])
                quoted, k = True, skip(k + 2)
            else:
                word.append(c)
                k = skip(k + 1)
        delim = "".join(word)
        if HEREDOC_DELIM.fullmatch(delim):
            pending.append((delim, dash, not quoted, len(stack)))
            out.append("<<" + ("-" if dash else "") + (f"'{delim}'" if quoted else delim))
        else:
            unsafe = True
            out.append("<<")
        return k

    def bodies(k: int) -> int:
        """k is the start of the line after the line with <<: the bodies in turn; returns where the code continues."""
        nonlocal unsafe
        for delim, dash, unquoted, _ in pending:
            lines: List[str] = []
            end, pos = None, k
            while True:
                e = command.find("\n", pos)
                stop = n if e < 0 else e
                line = command[pos:stop]
                if (line.lstrip("\t") if dash else line) == delim:
                    end = stop
                    break
                lines.append(line)
                if e < 0:
                    break
                pos = e + 1
            if unquoted and any("$(" in s or "`" in s or s.endswith("\\") for s in lines):
                unsafe = True
            if end is None:
                break  # no delimiter — the rest (and the bodies further in the queue) stays code
            k = min(end + 1, n)
        pending.clear()
        return k

    i = 0
    while i < n:
        ctx, ch = stack[-1][0], command[i]
        if ctx in ("sq", "ansi"):
            if ch == "`" and in_tick():
                close_tick()
            elif ctx == "ansi" and ch == "\\" and i + 1 < n:
                out.append(command[i:i + 2])
                i += 2
                continue
            elif ch == "'":
                stack.pop()
            out.append(ch)
            i += 1
            continue
        if command.startswith("\\\n", i):
            i += 2
            continue
        if ch == "\\":
            out.append(command[i:i + 2])
            i += 2
            continue
        j = skip(i + 1)
        c2 = command[j] if j < n else ""
        if ch == "`":
            close_tick() if in_tick() else stack.append(["tick", 0])
            out.append(ch)
            i += 1
            continue
        if ch == "$" and c2 in "({[" and c2:
            k = skip(j + 1)
            kind = {"(": "arith" if command[k:k + 1] == "(" else "subst", "{": "param", "[": "arithb"}[c2]
            stack.append([kind, 0])
            out.append("$((" if kind == "arith" else "$" + c2)
            i = k + 1 if kind == "arith" else j + 1
            continue
        if ctx == "dq":
            if ch == '"':
                stack.pop()
            out.append(ch)
            i += 1
            continue
        if ch == "$" and c2 == "'":
            stack.append(["ansi", 0])
            out.append("$'")
            i = j + 1
            continue
        if ch in "'\"":
            stack.append(["sq" if ch == "'" else "dq", 0])
            out.append(ch)
            i += 1
            continue
        if ctx in CODE and ch == "(" and c2 == "(":
            stack.append(["arithc", 0])
            out.append("((")
            i = j + 1
            continue
        if ctx in CLOSERS:
            opener, closer = CLOSERS[ctx]
            if ch == opener:
                stack[-1][1] += 1
            elif ch == closer and stack[-1][1]:
                stack[-1][1] -= 1
            elif ch == closer:
                stack.pop()
                out.append(ch)
                i += 1
                if ctx in ("arith", "arithc") and command[skip(i):skip(i) + 1] == ")":  # )) closes the arithmetic
                    out.append(")")
                    i = skip(i) + 1
                if ctx != "arithc":  # $(true)#; … — a # in the middle of a word, the command after ; runs
                    glued_at = len(out)
                continue
        if ctx in CODE:
            if ch == "#" and len(out) != glued_at and (not last() or last() in WORD_START):
                while i < n and command[i] != "\n":
                    i += 1
                continue
            if ch == "<" and c2 == "<" and last() != "<" and command[skip(j + 1):skip(j + 1) + 1] != "<":
                if in_tick():  # The first backtick in the body would close the substitution and leave the rest as surrounding code.
                    unsafe = True
                i = heredoc(skip(j + 1))
                continue
            if ch == "\n":
                out.append(ch)
                if pending and any(p[3] != len(stack) for p in pending):
                    # A newline inside a substitution opened after << on the same line is part of the
                    # substitution. Bash reads the heredoc body after the outer line ends.
                    unsafe = True
                    pending.clear()
                    i += 1
                else:
                    i = bodies(i + 1) if pending else i + 1
                continue
        out.append(ch)
        i += 1
    return "".join(out), unsafe


def _env_options(prefix: List[str]) -> bool:
    """env with options before the command: -iC/-vC/--chdir/-iu … the wrapper parser (kd.shell) does not know them and
    would take an option's value for the command (env -iC <clone> tee …) — so env is allowed only as env NAME=value command."""
    return any(posixpath.basename(w) == "env" and prefix[i + 1:i + 2] and prefix[i + 1].startswith("-")
               for i, w in enumerate(prefix))


def _odd_name(argv: List[str]) -> bool:
    """Bash can derive the command name from variables, command substitution, globbing or brace expansion."""
    name = gb.expand_home(argv[0], AGENT_HOME) if argv else ""
    return bool(name) and name not in NAME_OK and not ARRAY_SET.match(name) and bool(ODD_NAME.search(name))


def _forge_problem(argv: List[str]) -> Optional[str]:
    """kd-forge: merge (as the second word or any) and words bash will expand ({merge,}, me{r,}ge, m?rge, [m]erge)."""
    if len(argv) < 2 or "merge" in argv[1:]:
        return MERGE_WHY
    if any(EXPANDS.search(w) for w in argv[1:]):
        return FORGE_EXPAND_WHY
    if argv[1] not in FORGE_SUBCOMMANDS:  # mer\<nl><nl>ge, -- merge and whatever else the wrapper will not accept anyway
        return FORGE_SUB_WHY
    return None


def _ansi_quoted(command: str) -> bool:
    """$'…' or $"…" outside quotes: bash expands \\xNN and \\NNN (ANSI-C) or translates the string, shlex and the parser
    do not (kd-forge $'\\x6derge' is merge, $'\\x2eclaude' is .claude). Inside '…' and "…" it is just text."""
    quote, i = "", 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif quote == '"':
            if ch == "\\":
                i += 1
            elif ch == '"':
                quote = ""
        elif ch == "\\":
            i += 1
        elif ch == "$" and command[i + 1:i + 2] in ("'", '"'):
            return True
        elif ch in "'\"":
            quote = ch
        i += 1
    return False


def _redirect_text(flat: str) -> str:
    """Redirections in canonical form: 2>&1, >&2 are not a write (removed), &>/&>> and >&file become > and >>, >| — >,
    |& — |. Otherwise their & and | would be taken for operators (background, pipeline) and break the tracking of cd."""
    flat = AMP_REDIR.sub(" ", DUP_FD.sub(" ", flat))
    return flat.replace(">&", ">").replace(">|", "> ").replace("|&", "|")


def _events(text: str) -> List[Event]:
    """Simple commands and the operators between them, in order; a newline after && || | is a continuation, not a boundary."""
    out: List[Event] = []
    for i, part in enumerate(OPS.split(text)):
        if i % 2:
            if not (part == "\n" and out and out[-1] in (("op", "&&"), ("op", "||"), ("op", "|"))):
                out.append(("op", part))
            continue
        for j, piece in enumerate(BRACE_WORD.split(part)):
            if j % 2:
                out.append(("op", piece))
            elif piece.split():
                out.append(("seg", piece.split()))
    return out


def _targets(segment: List[str], argv: List[str]) -> Tuple[List[str], bool, List[str]]:
    """(where a simple command writes — redirections and the writers' operands, whether it is a writer, the sources of ln)."""
    out = [m.group(1) for m in shell.REDIRECT.finditer(" ".join(segment))]
    argv = shell.strip_redirects(shell.live(argv))
    c, args = shell.cmd(argv), argv[1:]
    if c not in WRITERS:
        return out, False, []
    links: List[str] = []
    if c == "sed":
        if not (shell.short_has(args, "i") or any(a.startswith("--in-place") for a in args)):
            return out, False, []
        out += shell.sed_files(args)
    elif c == "dd":
        out += [a[3:] for a in args if a.startswith("of=")]
    elif c in ("cp", "mv", "install", "ln", "rsync"):
        folder, sources, _ = shell.copy_plan(c, args)
        out += [folder] if folder else []
        if c == "mv":
            out += sources  # mv removes the source: taking CLAUDE.md out of the agent's folder is a write to it too
        if c == "ln" or (c == "cp" and (shell.short_has(args, "l") or "--link" in args)):
            links = sources  # ln -s .claude y: a later write to y/… is a write to .claude; cp -l is a hard link
    else:
        operands = [a for a in args if not a.startswith("-")]
        out += operands[1:] if c in OWNER_FIRST else operands
    return out, True, links


def _loop_words(events: List[Event]) -> Dict[str, Optional[List[str]]]:
    """The variables of for/select and their words (for f in *.py → f: [*.py]). None — the words are unknown: a
    substitution in the list, the name occurs elsewhere as a word (f=…, read f, printf -v f …) or it has more than one loop."""
    loops: Dict[str, Optional[List[str]]] = {}
    for k, (kind, seg) in enumerate(events):
        argv = shell.command_argv(seg) if kind == "seg" else []
        if len(argv) < 2 or argv[0] not in ("for", "select"):
            continue
        name, values = argv[1], (argv[3:] if argv[2:3] == ["in"] else None)
        after = events[k + 1][1] if k + 1 < len(events) else ""
        if values is not None and (after in ("$(", "`", "(") or any("$" in v or "`" in v for v in values)):
            values = None
        loops[name] = None if name in loops else values
    words = [w for kind, seg in events if kind == "seg" for w in seg]
    for name in loops:
        if sum(w == name or w.startswith((name + "=", name + "+=", name + "[")) for w in words) > 1:
            loops[name] = None
    return loops


def _with_loops(target: str, loops: Dict[str, Optional[List[str]]]) -> Optional[List[str]]:
    """The target with every for-variable substituted ($f, ${f}); None — there is a $ that cannot be computed ($X, $PWD, $1, ${f%x})."""
    if "$" not in target:
        return [target]
    names = {m.group(1) or m.group(2) for m in VAR_REF.finditer(target)}
    if "$" in VAR_REF.sub("", target) or any(loops.get(n) is None for n in names):
        return None
    out = [target]
    for name in names:
        out = [VAR_REF.sub(lambda m, v=v: v if (m.group(1) or m.group(2)) == name else m.group(0), t)
               for t in out for v in loops[name]]
        if len(out) > LOOP_MAX:
            return None
    return out


def _opaque(path: str, dotglob: bool) -> bool:
    """A word whose path the hook cannot compute: ~+/~- and other ~… (homes are expanded by expand_home), `…`, {…} (bash
    expands braces into several paths), a glob in a component with a dot (.cla*) or any glob under dotglob/GLOBIGNORE."""
    return (path.startswith("~") or "`" in path or "{" in path or "$" in path
            or any(GLOB.search(c) and (c.startswith(".") or dotglob) for c in path.split("/")))


def _resolve(cur: Optional[str], target: str) -> Optional[str]:
    """The target's absolute path from the current directory; None — a relative path with the directory unknown."""
    if target.startswith("/"):
        return posixpath.normpath(target)
    return posixpath.normpath(posixpath.join(cur, target)) if cur else None


def _work_memory(path: str) -> bool:
    """The path is work/CLAUDE.md or work/CLAUDE.local.md, also by a glob in the root of work/ (rm work/C*, work/*.md);
    or below work/ a CLAUDE.local.md, or a CLAUDE.md in tmp/ (no clone there): Claude Code loads a folder's
    instructions once a file there is read (KD-9482). A clone's tracked CLAUDE.md stays editable."""
    folder, name = posixpath.split(path)
    if folder == WORK:
        return any(fnmatch.fnmatchcase(m, name) for m in WORK_MEMORY)
    return path.startswith(WORK + "/") and (name == "CLAUDE.local.md"
                                            or (name == "CLAUDE.md" and path.startswith(TMP + "/")))


def _target_problem(target: str, cur: Optional[str], loops, dotglob: bool, link: bool = False) -> Optional[str]:
    """The reason to refuse one write target (link — a source of ln: only .claude/.mcp.json and the work/ stubs are checked)."""
    for value in _with_loops(target, loops) or [None]:
        path = None if value is None or _opaque(value, dotglob) else _resolve(cur, value)
        if path is None:
            return UNRESOLVED_WHY
        if FORBIDDEN.search(path):
            return FORBIDDEN_WHY
        if _work_memory(path):
            return WORK_MEMORY_WHY
        if not link and "/.git/" in path + "/" and path.startswith(WORK + "/"):
            return GIT_DIR_WHY
        # and as a source of ln/cp -l: a hard link to memory/tasks/ would write the same file past kd-notes
        if not SLOT and (path == SHARED_NOTES or path.startswith(SHARED_NOTES_DIR)):
            return SHARED_NOTES_WHY
        if not link and not (path == "/dev/null" or path.startswith(NOTES_ROOTS) or path.startswith(WRITE_ROOTS)):
            return WRITE_WHY
    return None


def _chdir(c: str, argv: List[str], cur: Optional[str], cdpath: bool) -> Optional[str]:
    """Where cd/pushd/popd leads; None — unknown: popd, pushd without a directory, pushd -n/+N, cd -, options other than
    -L/-P/-e/-@, several directories, a substitution or a glob in the name, a relative name under CDPATH."""
    if c == "popd":
        return None
    args, operands = argv[1:], []
    for i, a in enumerate(args):
        if a == "--":
            operands += args[i + 1:]
            break
        if a.startswith("-") and len(a) > 1:
            if c == "cd" and CD_OPTS.fullmatch(a):
                continue
            return None
        operands.append(a)
    if not operands:
        return AGENT_HOME if c == "cd" else None
    target = operands[0]
    if len(operands) > 1 or target == "-" or (c == "pushd" and target.startswith("+")) or _opaque(target, True):
        return None
    if cdpath and not target.startswith(("/", "./", "../")) and target not in (".", ".."):
        return None
    return _resolve(cur, target)


def _quoted_redirects(text: str) -> Tuple[List[str], bool]:
    """Redirections inside "$(…)" and "`…`": shell.parse takes a > in double quotes for text, while bash runs the
    substitution. Returns (targets, whether there is a > without a target). text is what _lex returned: heredoc bodies
    are already gone (git commit -m "$(cat <<'EOF' …)")."""
    targets, dangling = [], False
    for m in DQ.finditer(text):
        body = m.group(1)
        if not body or not ("$(" in body or "`" in body):
            continue
        for kind, seg in _events(_redirect_text(shell.parse(body).flat)):
            if kind == "seg":
                joined = " ".join(seg)
                targets += [r.group(1) for r in shell.REDIRECT.finditer(joined)]
                dangling = dangling or bool(DANGLING.search(joined))
    return targets, dangling


def _write_problem(command: str, cwd: Optional[str]) -> Optional[str]:
    """A write outside work/<Repo> (less .claude, .mcp.json and the work/CLAUDE*.md stubs), tmp/, memory/tasks/ and
    memory/chats/ of the agent's folder and /dev/null.
    Relative paths count from the cwd of the hook event and from cd/pushd in the same command. A cd takes effect only
    when && follows it (and no | or || precedes it, and it has no prefix: ! cd, CDPATH=… cd); after ; newline || & the
    directory is unknown (the cd may have failed). Subshells ( … ), $(…), `…` do not change the directory. ~,
    ~<prefix>-dev and $HOME are the home of <prefix>-dev, ~digi and other ~user their homes (gb.expand_home)."""
    text = gb.expand_home(command.replace("\\\n", ""), AGENT_HOME)
    events = _events(_redirect_text(shell.parse(text).flat))
    loops = _loop_words(events)
    dotglob, cdpath = bool(DOTGLOB.search(command)), "CDPATH" in command
    inner, dangling = _quoted_redirects(text)
    if dangling:
        return UNRESOLVED_WHY
    for t in inner:  # the directory of a substitution inside quotes is not tracked — absolute paths only
        why = _target_problem(t, None, loops, dotglob)
        if why:
            return why
    cur: Optional[str] = posixpath.normpath(cwd) if cwd and cwd.startswith("/") else AGENT
    pending, stack, tick = False, [], False  # pending — the directory of a cd through &&; stack — directories outside subshells
    closed = False  # a segment right after ) or a closing ` continues the words, it is not a command
    for k, (kind, val) in enumerate(events):
        if kind == "op":
            closed = False
            if val in ("(", "$(") or (val == "`" and not tick):
                stack.append((cur, pending))
                pending, tick = False, tick or val == "`"
            elif val in (")", "`"):
                cur, pending = stack.pop() if stack else (None, False)
                tick, closed = tick and val != "`", True
            elif val in BREAKS and pending:
                cur, pending = None, False
            continue
        seg: List[str] = val  # type: ignore[assignment]
        prev_op = events[k - 1][1] if k else ""
        next_op = events[k + 1][1] if k + 1 < len(events) else ""
        argv = shell.command_argv(seg)
        prefix = seg[:len(seg) - len(argv)]
        if _env_options(prefix):  # inside "$(…)" too, where the word check by shlex does not look
            return ENV_OPTS_WHY
        if not closed and _odd_name(argv):
            return CMD_NAME_WHY
        targets, writer, links = _targets(seg, argv)
        if DANGLING.search(" ".join(seg)) or ((targets or writer) and next_op in ("$(", "`", "(")):
            return UNRESOLVED_WHY  # > $(…), tee $(…), tee @(…) — a target from a substitution or an extglob pattern
        if writer and any(posixpath.basename(w) == "xargs" for w in prefix):
            return UNRESOLVED_WHY  # the writer's operands come from stdin
        seg_cur = None if any(CHDIR_OPT.match(w) for w in prefix) else cur
        for t, link in [(t, False) for t in targets] + [(t, True) for t in links]:
            why = _target_problem(t, seg_cur, loops, dotglob, link)
            if why:
                return why
        c = shell.cmd(argv)
        if c in ("cd", "pushd", "popd"):
            effective = next_op == "&&" and prev_op not in ("|", "||")
            cur = (None if prefix else _chdir(c, shell.strip_redirects(argv), cur, cdpath)) if effective else None
            pending = pending or effective
    return None


def _code_text(text: str) -> str:
    """The text without quoted data: '…', \\x and "…" without $(…) or `…` are one word "_" (a cd in a commit message or a
    note is not a command); "…" with a substitution stays as it is — the commands in it run."""
    def keep(m) -> str:
        body = m.group(1)
        return m.group(0) if body and ("$(" in body or "`" in body) else "_"
    return DQ.sub(keep, text)


def _cd_problem(command: str) -> Optional[str]:
    """cd/pushd/popd together with any other command: cd X && Y, Y; cd X, (cd X && Y), { cd X; }, $(cd …), cd "$(…)".
    Claude Code itself sends such a chain to the owner for confirmation — even with Bash allowed and the hook's "allow"
    (probed on a live host, 2.1.289). A lone cd is allowed: Bash keeps the directory between calls, and the cwd of the
    next hook event is already the new directory."""
    events = _events(_redirect_text(shell.parse(_code_text(command.strip())).flat))
    has_cd = any(kind == "seg" and shell.cmd(shell.command_argv(val)) in CD_CMDS for kind, val in events)
    return CD_WHY if has_cd and len(events) > 1 else None


def _position(words: List[str], segs: List[List[str]], tool: str) -> bool:
    """The word tool is in the command, but in no segment is it the command (echo kd-forge …, $(kd-merge …))."""
    def named(word: str) -> bool:
        return naming.canonical_command(posixpath.basename(word)) == tool

    return any(named(w) for w in words) and not any(named(shell.cmd(shell.command_argv(s))) for s in segs)


def decide(command: str, cwd: Optional[str] = None) -> Optional[str]:
    for text in (command, command.replace("\\\n", "")):  # both with comments and after joining lines — the stricter way
        if SECRETS.search(text) or SECRETS.search(shell.parse(text).flat):
            return SECRETS_WHY
    # as bash does: \+newline joins, comments and heredoc bodies are data (* item, [link], # in a PR body)
    command, unsafe_heredoc = _lex(command)
    if unsafe_heredoc:
        return HEREDOC_WHY
    if _ansi_quoted(command):
        return ANSI_WHY
    try:
        words = gb.tokens(command)
    except ValueError:
        return "не удалось разобрать команду (незакрытые кавычки)"
    if gb.runs_sudo(words) or gb.sudo_in_parsed(" ".join(words)):
        return "sudo агенту запрещён (merge и комментарии — через kd-merge/kd-forge)"
    segs = _segments(words)
    for seg in segs:
        name = naming.canonical_command(posixpath.basename(seg[0])) if seg else ""  # kd-<word> on any host
        argv = shell.command_argv(seg)
        cmd = naming.canonical_command(shell.cmd(argv))
        prefix = seg[:len(seg) - len(argv)]
        wrappers = [w for w in map(posixpath.basename, prefix) if w in shell.WRAPPERS]
        bad_shell = next((w for w in [*wrappers, name, cmd] if w in SHELLS), None)
        if bad_shell:
            return f"{bad_shell} -c/eval/exec запрещены (обход правил)"
        if _env_options(prefix):
            return ENV_OPTS_WHY
        if _odd_name(argv):
            return CMD_NAME_WHY
        if cmd == "git":
            if any(GIT_ENV.match(w) for w in prefix):
                return "переменные GIT_* перед git запрещены (GIT_DIR, GIT_WORK_TREE … — обход правил)"
            extra = [w for w in wrappers if w not in GIT_WRAPPERS_OK]
            if extra:
                return f"git — только напрямую (без {extra[0]}; можно timeout и nice)"
            why = _check_git(argv)
            if why:
                return why
        if cmd == "find" and FIND_EXEC & set(argv[1:]):
            return "find -exec/-ok запрещены — запускай команды напрямую"
        if cmd in NET_TOOLS:
            return NET_TOOLS[cmd]
        if cmd in PKG_TOOLS or (cmd in ("python", "python3") and "-m" in argv and "pip" in argv):
            return PKG_TOOLS.get(cmd, PKG_TOOLS["pip"])
        if cmd in ("kd-pr", "kd-forge") and name != cmd:
            return f"{cmd}: kd-pr/kd-forge — только в позиции команды"
        if cmd == "kd-forge" and _forge_problem(argv):
            return _forge_problem(argv)
        if cmd == "kd-merge":
            if name != "kd-merge":
                return "kd-merge — только в позиции команды"
            if "&" in words:
                return "kd-merge — без & (фоновый запуск прятал бы итог)"
            why = _check_merge(argv, len(segs))
            if why:
                return why
        if cmd in ("kd-merge", "kd-pr", "kd-forge") and SUBST.search(" ".join(seg)):
            return f"{cmd}: без подстановок ($(…), `…`, ${{…}}, $ИМЯ) — карточка и хук должны видеть ту же команду"
    # the host's name or the tree's file (it keeps the repository's): either is merge
    merges = {"kd-merge", naming.command("kd-merge")}
    if any(m in w for w in words for m in merges) and not any(
            naming.canonical_command(shell.cmd(shell.command_argv(s))) == "kd-merge" for s in segs):
        return "kd-merge — только в позиции команды"
    for tool in ("kd-pr", "kd-forge"):
        if _position(words, segs, tool):
            return f"{tool}: kd-pr/kd-forge — только в позиции команды"
    # shlex keeps "$(…)" and `…` as one word — a merge inside them shows only after parsing (the quotes stripped)
    for argv in shell.parse(command).argvs:
        why = _forge_problem(argv) if naming.canonical_command(shell.cmd(argv)) == "kd-forge" else None
        if why:
            return why
    # the write goes before cd: its reason matters more; the cd tracking in _write_problem stays the second line
    return _write_problem(command, cwd) or _cd_problem(command)


def _foreground_only(event: dict) -> Optional[str]:
    """Slot mode: a Bash call that would run in the background outlives the worker's reply."""
    tool_input = event.get("tool_input") or {}
    return BACKGROUND_WHY if SLOT and tool_input.get("run_in_background") else None


def main(stdin=None, stderr=None, stdout=None, after_bash=None) -> int:
    """The single PreToolUse of dev: the guard's checks, then the refusal of `cd X && …` and the record of expected
    channel calls; in slot mode — the refusal of background commands first."""
    then = after_bash if after_bash is not None else cap_expect.after_bash("dev")
    return gb.run_hook(decide, stdin=stdin, stdout=stdout, stderr=stderr, allow_on_success=True,
                       after_bash=lambda event: _foreground_only(event) or then(event))
