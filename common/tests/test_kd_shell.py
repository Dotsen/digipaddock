from kd import shell


def test_parse_strips_quotes_and_splits_segments():
    p = shell.parse("""VAR=1 timeout 10 git -C /x 'pu''sh' origin && echo "a;b" > /etc/f""")
    assert p.flat.startswith("VAR=1 timeout 10 git -C /x push origin")
    assert p.argvs[0] == ["git", "-C", "/x", "push", "origin"]
    assert p.argvs[1][0] == "echo"


def test_command_argv_skips_wrappers_and_su_like():
    assert shell.command_argv(["sudo", "-u", "x", "env", "A=1", "rm", "-rf", "/"]) == ["rm", "-rf", "/"]
    assert shell.command_argv(["runuser", "-u", "digi", "--", "git", "push"]) == ["git", "push"]
    assert shell.command_argv(["bash", "-c", "git push"]) == ["git push"]
    assert shell.command_argv(["flock", "/run/l", "-c", "x"]) == ["x"]


def test_writes_into_sees_redirects_copies_and_in_place_edits():
    trees = ("/srv/digipaddock-agents/dev/.claude",)
    assert shell.writes_into(shell.parse("echo x > /srv/digipaddock-agents/dev/.claude/settings.json"), trees)
    assert shell.writes_into(shell.parse("cp /tmp/s.json /srv/digipaddock-agents/dev/.claude/"), trees)
    assert shell.writes_into(shell.parse("sed -i s/a/b/ /srv/digipaddock-agents/dev/.claude/settings.json"), trees)
    assert not shell.writes_into(shell.parse("cat /srv/digipaddock-agents/dev/.claude/settings.json"), trees)


def test_denylist_reexports_same_objects():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents/sysadmin"))
    from sysadmin import denylist
    assert denylist.parse is shell.parse and denylist.command_argv is shell.command_argv
    assert denylist.writes_into is shell.writes_into


def test_denylist_keeps_names_used_by_sysadmin_guard():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents/sysadmin"))
    from sysadmin import denylist
    for name in ("_strip_redirects", "_live", "_cmd", "_short_has", "_ATTR_CHANGERS", "_encloses", "_mv_sources",
                 "_writes_into", "_COPIERS", "Parsed", "parse", "command_argv", "writes_into", "_GLOB"):
        assert hasattr(denylist, name), name
