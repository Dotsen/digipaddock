import io
import json

from kd import guard_base as gb


def test_tokens_and_comments():
    assert gb.tokens(gb.strip_comments("echo a # it's\necho b")) == ["echo", "a", ";", "echo", "b"]
    try:
        gb.tokens("echo 'open")
    except ValueError:
        pass
    else:
        raise AssertionError("незакрытая кавычка должна давать ValueError")


def test_runs_sudo_behind_wrappers():
    assert gb.runs_sudo(gb.tokens("env X=1 timeout 5 sudo ls"))
    assert gb.sudo_in_parsed("bash -c 'sudo ls'")
    assert not gb.runs_sudo(gb.tokens("echo sudo"))


def test_writes_local_resolves_home_and_refs():
    trees = ("/home/digi/.claude",)
    assert gb.writes_local(gb.tokens("echo x > ~/.claude/settings.json"), trees)
    assert gb.writes_local(gb.tokens("cp a $HOME/.claude/"), trees)
    assert not gb.writes_local(gb.tokens("cat ~/.claude/settings.json"), trees)
    import re
    assert gb.writes_local(gb.tokens("cp a $B/x"), ("/srv/b",), refs=((re.compile(r"\$B(?!\w)"), "/srv/b"),))


def test_writes_local_home_is_a_parameter():
    trees = ("/home/digi-dev/.claude",)
    assert gb.writes_local(gb.tokens("echo x > ~/.claude/settings.json"), trees, home="/home/digi-dev")
    assert not gb.writes_local(gb.tokens("echo x > ~/.claude/settings.json"), trees)


def test_expand_home_knows_users():
    """Like bash: ~ and $HOME — the agent's home, ~user — that user's home (for agents under their own user)."""
    home = "/home/digi-dev"
    assert gb.expand_home("echo x > ~/a ~digi-dev/b ~digi/c ~root/d ~x/e", home) \
        == "echo x > /home/digi-dev/a /home/digi-dev/b /home/digi/c /root/d /home/x/e"
    assert gb.expand_home("cd ~ && ls $HOME ${HOME}/x $HOMEDIR", home) \
        == "cd /home/digi-dev && ls /home/digi-dev /home/digi-dev/x $HOMEDIR"
    assert gb.expand_home("cp a b=~/x --o:~digi/y >~/z", home) == "cp a b=/home/digi-dev/x --o:/home/digi/y >/home/digi-dev/z"
    # not at the start of a word, ~+ and ~- (the directory stack) — as they are: the hook does not know their expansion
    assert gb.expand_home("git diff HEAD~1 a~b ~+/x ~-/y ~Agent/z", home) == "git diff HEAD~1 a~b ~+/x ~-/y ~Agent/z"


def test_home_ref_for_agents_under_agent_unchanged():
    # HOME_REF (the sysadmin under digi): ~ and ~digi — /home/digi, other ~user are not touched
    assert gb.HOME_REF.sub("/home/digi", "~/a ~digi/b ~digi-dev/c") == "/home/digi/a /home/digi/b ~digi-dev/c"


def test_run_hook_blocks_on_bad_input_and_unknown_tool():
    err = io.StringIO()
    code = gb.run_hook(lambda c, cwd: None, stdin=io.StringIO("not json"), stderr=err)
    assert code == 2 and "внутренняя ошибка" in err.getvalue()
    err = io.StringIO()
    ev = json.dumps({"tool_name": "Write", "tool_input": {}})
    assert gb.run_hook(lambda c, cwd: None, stdin=io.StringIO(ev), stderr=err) == 2
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    assert gb.run_hook(lambda c, cwd: None, stdin=io.StringIO(ev), stderr=io.StringIO()) == 0
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "rm"}})
    err = io.StringIO()
    assert gb.run_hook(lambda c, cwd: "нельзя", stdin=io.StringIO(ev), stderr=err) == 2
    assert err.getvalue().strip() == "⛔ нельзя"


def test_run_hook_passes_event_cwd():
    seen = []
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/srv/digipaddock-agents/x/work/R"})
    gb.run_hook(lambda c, cwd: seen.append(cwd), stdin=io.StringIO(ev), stderr=io.StringIO())
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": 5})
    gb.run_hook(lambda c, cwd: seen.append(cwd), stdin=io.StringIO(ev), stderr=io.StringIO())
    assert seen == ["/srv/digipaddock-agents/x/work/R", None]


def test_run_hook_empty_or_missing_cwd_is_none():
    seen = []
    for extra in ({}, {"cwd": ""}, {"cwd": None}):
        ev = json.dumps(dict({"tool_name": "Bash", "tool_input": {"command": "ls"}}, **extra))
        gb.run_hook(lambda c, cwd: seen.append(cwd), stdin=io.StringIO(ev), stderr=io.StringIO())
    assert seen == [None, None, None]


def test_run_hook_can_allow_on_success_but_never_on_refusal():
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git status"}})
    out = io.StringIO()
    assert gb.run_hook(lambda c, cwd: None, stdin=io.StringIO(ev), stdout=out,
                       stderr=io.StringIO(), allow_on_success=True) == 0
    decision = json.loads(out.getvalue())["hookSpecificOutput"]
    assert decision["hookEventName"] == "PreToolUse" and decision["permissionDecision"] == "allow"
    out, err = io.StringIO(), io.StringIO()
    assert gb.run_hook(lambda c, cwd: "нет", stdin=io.StringIO(ev), stdout=out, stderr=err,
                       allow_on_success=True) == 2
    assert out.getvalue() == "" and "нет" in err.getvalue()


def test_run_hook_after_bash_runs_only_after_the_guard_passed():
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ssh-ro node-c uptime"}, "tool_use_id": "t1"})
    seen = []
    err = io.StringIO()
    assert gb.run_hook(lambda c, cwd: "денилист", stdin=io.StringIO(ev), stderr=err,
                       after_bash=lambda event: seen.append(event)) == 2
    assert seen == [] and "денилист" in err.getvalue()
    out = io.StringIO()
    assert gb.run_hook(lambda c, cwd: None, stdin=io.StringIO(ev), stdout=out,
                       stderr=io.StringIO(), allow_on_success=True, after_bash=lambda event: seen.append(event)) == 0
    assert seen[0]["tool_use_id"] == "t1" and "allow" in out.getvalue()


def test_run_hook_after_bash_refusal_and_failure_block():
    ev = json.dumps({"tool_name": "Bash", "tool_input": {"command": "cd x && ls"}})
    out, err = io.StringIO(), io.StringIO()
    assert gb.run_hook(lambda c, cwd: None, stdin=io.StringIO(ev), stdout=out, stderr=err,
                       allow_on_success=True, after_bash=lambda event: "отдельным вызовом") == 2
    assert out.getvalue() == "" and "отдельным вызовом" in err.getvalue()

    def broken(event):
        raise OSError("store")
    err = io.StringIO()
    assert gb.run_hook(lambda c, cwd: None, stdin=io.StringIO(ev), stderr=err,
                       after_bash=broken) == 2
    assert "внутренняя ошибка" in err.getvalue()


def test_sysadmin_guard_uses_base_and_guards_its_config():
    """The sysadmin's guard — on the shared base; a write into ~/.claude through ../… is still caught."""
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents/sysadmin"))
    from sysadmin import guard
    assert guard.tokens is gb.tokens and guard.strip_comments is gb.strip_comments and guard.runs_sudo is gb.runs_sudo
    assert guard.decide("kd-task list > /srv/digipaddock-agents/board/../../../home/digi/.claude/settings.json") \
        == guard.AGENT_CONFIG_WHY
