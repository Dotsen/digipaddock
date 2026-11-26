"""kd.effort_probe: fixed snapshots, isolated probe sessions, medium against high."""

from __future__ import annotations

import json
import shlex
import sqlite3
from pathlib import Path

import pytest

from kd import effort_probe as ep

SETTINGS = json.dumps(
    {
        "hooks": {},
        "permissions": {"allow": ["Bash(kd-task view *)"], "deny": ["Read(//etc/shadow)"]},
        "sandbox": {
            "enabled": True,
            "filesystem": {
                "allowRead": ["/home/digi-analyst/.claude/projects", "/run/digipaddock/cap/analyst"],
                "denyRead": ["/etc/hindsight", "/home/digi-analyst/.claude"],
            },
        },
    }
)
FOLDER = "/srv/digipaddock-agents/analyst"
HOME = "/home/digi-analyst"
SNAPS = {
    "KD-190": ("draft r5", "Цель: grep.\n"),
    "KD-9041": ("brief v1", "Цель: вложения.\n"),
}


def _db(path: Path) -> str:
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE brief_versions (card TEXT, version INTEGER, origin TEXT, author TEXT, description TEXT,"
        " acceptance TEXT, scope_sha TEXT, created TEXT)"
    )
    conn.execute(
        "INSERT INTO brief_versions VALUES ('KD-9041', 1, 'handoff', 'analyst', 'Цель: v1', ?, 'x', 't')",
        (json.dumps(["AC один", "AC два"], ensure_ascii=False),),
    )
    conn.execute(
        "INSERT INTO brief_versions VALUES ('KD-9041', 2, 'handoff', 'analyst', 'Цель: v2', '[]', 'y', 't')"
    )
    conn.commit()
    conn.close()
    return str(path)


def test_brief_version_reads_the_handed_over_text(tmp_path):
    snap = ep.brief_version(_db(tmp_path / "c.db"), "KD-9041", 1)
    assert snap == ep.Snapshot("KD-9041", "brief v1", "Цель: v1", ("AC один", "AC два"))
    assert snap.text() == "Цель: v1\n\n## Acceptance Criteria\n\n- AC один\n- AC два\n"
    with pytest.raises(ep.ProbeError, match="no brief version 3"):
        ep.brief_version(str(tmp_path / "c.db"), "KD-9041", 3)


def _tool(command: str) -> str:
    block = {"type": "tool_use", "name": "Bash", "input": {"command": command}}
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}, block]}})


def test_draft_comes_from_the_agents_own_edit_at_that_revision(tmp_path):
    text = "[черновик] Цель: 'grep' по\nвсему файлу"
    lines = [
        _tool(f"kd-task edit KD-190 --revision 4 --description {shlex.quote('старый')}"),
        _tool(f"kd-task edit KD-190 --revision 5 --description {shlex.quote(text)}"),
        _tool(f"kd-task edit KD-1900 --revision 5 --description {shlex.quote('чужая')}"),
        _tool("kd-task edit KD-190 --revision 5 --append-notes x"),
        "not json",
        json.dumps({"type": "user", "message": {"content": "plain"}}),
    ]
    (tmp_path / "s1.jsonl").write_text("\n".join(lines) + "\n", "utf-8")
    snap = ep.draft(tmp_path, "KD-190", 5)
    assert snap == ep.Snapshot("KD-190", "draft r5", text, ())
    assert "(критериев приёмки ещё нет)" in snap.text()
    with pytest.raises(ep.ProbeError, match="0 drafts at revision 6"):
        ep.draft(tmp_path, "KD-190", 6)
    (tmp_path / "s2.jsonl").write_text(_tool("kd-task edit KD-190 --revision 5 --description other") + "\n")
    with pytest.raises(ep.ProbeError, match="2 drafts"):
        ep.draft(tmp_path, "KD-190", 5)


def test_extract_writes_snapshots_with_their_hashes_and_run_refuses_a_changed_one(tmp_path, capsys):
    db = _db(tmp_path / "c.db")
    (tmp_path / "t").mkdir()
    (tmp_path / "t/s.jsonl").write_text(_tool("kd-task edit KD-190 --revision 5 --description 'Цель: grep'") + "\n")
    out = tmp_path / "snap"
    code = ep.main(
        ["extract", "--out", str(out), "--db", db, "--brief", "KD-9041:1", "--draft", "KD-190:5",
         "--transcripts", str(tmp_path / "t")]
    )
    assert code == 0, capsys.readouterr().err
    loaded = ep.load_snapshots(out)
    assert loaded["KD-9041"][0] == "brief v1" and loaded["KD-190"][0] == "draft r5"
    assert loaded["KD-190"][1].startswith("Цель: grep")
    (out / "KD-190.md").write_text("подменено", "utf-8")
    with pytest.raises(ep.ProbeError, match="changed after extraction"):
        ep.load_snapshots(out)


@pytest.mark.parametrize(
    "args",
    [["--brief", "KD-9041"], ["--brief", "190:1"], ["--draft", "KD-190:5"], ["--brief", "KD-9041:1", "--brief", "KD-9041:2"], []],
    ids=["no version", "bad id", "draft without transcripts", "card twice", "nothing"],
)
def test_extract_refuses_unclear_requests(tmp_path, capsys, args):
    assert ep.main(["extract", "--out", str(tmp_path / "o"), "--db", _db(tmp_path / "c.db"), *args]) == 1
    assert capsys.readouterr().err.startswith("analyst_effort_probe: ")


def test_prompt_holds_the_snapshot_and_neighbours_but_no_board_access_to_the_probed_cards():
    text = ep.prompt("KD-9041", SNAPS)
    assert "=== Brief KD-9041 (brief v1) ===\nЦель: вложения." in text
    assert "=== Neighbouring card KD-190 (draft r5), for context ===\nЦель: grep." in text
    assert "do not \nopen" not in text and "do not open KD-190, KD-9041 on the board" in text
    assert "Write nothing anywhere" in text and "answer in Russian" in text
    assert ep.prompt("KD-190", SNAPS).count("=== ") == 2


def test_probe_settings_add_the_denies_and_close_the_paths_to_the_remarks():
    merged = json.loads(ep.probe_settings(SETTINGS, SNAPS, FOLDER, HOME))
    deny = merged["permissions"]["deny"]
    assert deny == sorted(deny) and "Read(//etc/shadow)" in deny  # the agent's own denies stay
    for rule in (*ep.WRITE_DENY, "Bash(kd-task view KD-190*)", "Bash(kd-task view KD-9041*)",
                 "Read(//srv/digipaddock-agents/analyst/memory/**)", "Read(//home/digi-analyst/.claude/projects/**)",
                 "Read(//srv/digipaddock-agents/board/**)"):
        assert rule in deny, rule
    assert "Agent" in deny  # no subagent: one exact model, every call in the run's own stream
    assert merged["permissions"]["allow"] == ["Bash(kd-task view *)"]
    # Bash in the sandbox: the same paths are closed there, and the old sessions are no longer re-opened.
    filesystem = merged["sandbox"]["filesystem"]
    assert filesystem["allowRead"] == ["/run/digipaddock/cap/analyst"]
    assert filesystem["denyRead"] == sorted(
        ["/etc/hindsight", "/home/digi-analyst/.claude", "/home/digi-analyst/.claude/projects",
         "/srv/digipaddock-agents/analyst/memory", "/srv/digipaddock-agents/board"]
    )
    assert merged["sandbox"]["enabled"] is True and "enabledPlugins" not in merged


def _call(id_, command, error=False):
    use = {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": id_, "name": "Bash", "input": {"command": command}}]}}
    result = {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": id_, "is_error": error, "content": "x"}]}}
    return [json.dumps(use), json.dumps(result)]


def test_leaks_are_injected_memory_or_a_read_of_a_closed_path_that_was_not_refused():
    closed = ep.closed_paths(FOLDER, HOME)
    clean = [
        *_call("a", "cat /srv/digipaddock-agents/analyst/memory/notes.md", error=True),  # refused by the sandbox
        *_call("b", "kd-task view KD-19000"),  # another card
        *_call("c", "grep -rn MAX_REQUEST /srv/digipaddock-repos/digipaddock/common/kd/cabinet/server.py"),
    ]
    assert ep.leaks(clean, SNAPS, closed) == []
    dirty = [
        *clean,
        *_call("d", "kd-task view KD-9041 | tail -40"),
        *_call("e", "grep -r KD-190 /srv/digipaddock-agents/board/tasks"),
        json.dumps({"type": "system", "subtype": "hook", "output": "<hindsight_knowledge>…"}),
    ]
    assert ep.leaks(dirty, SNAPS, closed) == [
        "/srv/digipaddock-agents/board", "<hindsight_knowledge", "kd-task view KD-9041"
    ]


def test_schedule_interleaves_and_alternates_the_first_effort():
    order = ep.schedule(["KD-190", "KD-9041"], ["medium", "high"], 2)
    assert order == [
        (1, "KD-190", "medium"), (1, "KD-190", "high"),
        (1, "KD-9041", "high"), (1, "KD-9041", "medium"),
        (2, "KD-190", "high"), (2, "KD-190", "medium"),
        (2, "KD-9041", "medium"), (2, "KD-9041", "high"),
    ]
    assert len(ep.schedule(["a", "b", "c"], ["medium", "high"], 3)) == 18


def _stream(answer="Находки: 1.", error=False, model="claude-opus-5-5"):
    return [
        json.dumps({"type": "system", "subtype": "init", "model": model, "claude_code_version": "2.9.0",
                    "session_id": "s"}),
        json.dumps({"type": "assistant", "message": {"content": []}}),
        json.dumps({"type": "result", "is_error": error, "duration_ms": 61000, "num_turns": 7,
                    "total_cost_usd": 0.5, "result": answer, "session_id": "s",
                    "usage": {"input_tokens": 10, "output_tokens": 900, "cache_read_input_tokens": 5,
                              "cache_creation_input_tokens": 3}}),
    ]


class FakeClaude:
    def __init__(self, help_text="  --effort <level>  reasoning effort"):
        self.calls = []
        self.help = help_text

    def __call__(self, argv, stdin, cwd, env, timeout):
        self.calls.append((argv, stdin, cwd, env, timeout))
        if argv == ["/opt/digipaddock-claude/claude", "--help"]:
            return 0, self.help.splitlines()
        return 0, _stream(answer=f"ответ {argv[argv.index('--effort') + 1]}")


def test_run_starts_one_isolated_session_per_card_effort_and_repeat(tmp_path):
    fake = FakeClaude()
    ticks = iter(range(0, 1000, 10))
    rows = ep.run(
        name="analyst", folder=FOLDER, settings=SETTINGS, snapshots=SNAPS, out=tmp_path / "out",
        model="claude-opus-5-5", efforts=["medium", "high"], repeats=3, home=str(tmp_path),
        runner=fake, clock=lambda: next(ticks),
    )
    assert len(rows) == 12 and len(fake.calls) == 13
    argv, stdin, cwd, env, timeout = fake.calls[1]
    assert argv[0] == "/opt/digipaddock-claude/claude" and argv[argv.index("--model") + 1] == "claude-opus-5-5"
    assert argv[argv.index("--add-dir") + 1] == FOLDER and argv[-4:] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert "--strict-mcp-config" in argv and "--mcp-config" not in argv  # no MCP server at all
    assert "--dangerously-skip-permissions" not in argv and "--resume" not in argv
    assert "Bash(kd-work *)" in json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
    assert cwd == f"{FOLDER}/work" and timeout == 1800 and "=== Brief KD-190" in stdin
    assert env["KD_AGENT"] == "analyst" and env["DISABLE_AUTOUPDATER"] == "1"
    memory = json.loads(Path(env["HINDSIGHT_CONFIG"]).read_text())
    assert memory["optInOnly"] is True and memory["optInPaths"] == [] and memory["mapPathToBank"] == {}
    assert memory["autoInject"] == "none" and memory["retainSessions"] is False
    assert fake.calls[0][3]["HINDSIGHT_CONFIG"] == env["HINDSIGHT_CONFIG"]
    assert all(row["leaks"] == [] for row in rows)
    assert {(r["card"], r["effort"]) for r in rows} == {(c, e) for c in SNAPS for e in ("medium", "high")}
    first = rows[0]
    assert (first["model"], first["output_tokens"], first["num_turns"], first["wall_s"]) == ("claude-opus-5-5", 900, 7, 10)
    out = tmp_path / "out"
    journal = [json.loads(line) for line in (out / "runs.jsonl").read_text().splitlines()]
    assert len(journal) == 12 and "answer" not in journal[0]
    assert (out / "KD-190-high-2.md").read_text() == "ответ high\n"
    assert (out / "KD-190-high-2.stream.jsonl").exists()


def test_run_refuses_without_the_effort_option_or_with_an_unknown_effort_or_model(tmp_path):
    common = dict(name="analyst", folder=FOLDER, settings=SETTINGS, snapshots=SNAPS, repeats=1, home=str(tmp_path))
    fake = FakeClaude(help_text="  --model <model>")
    with pytest.raises(ep.ProbeError, match="no --effort option"):
        ep.run(model="claude-opus-5-5", efforts=["medium"], runner=fake, out=tmp_path / "o1", **common)
    assert len(fake.calls) == 1  # only --help ran
    assert not (tmp_path / "o1").exists()  # the same --out serves the retry
    with pytest.raises(ep.ProbeError, match="unknown effort"):
        ep.run(model="claude-opus-5-5", efforts=["turbo"], runner=FakeClaude(), out=tmp_path / "o2", **common)
    with pytest.raises(ValueError, match="invalid model"):
        ep.run(model="opus --verbose", efforts=["high"], runner=FakeClaude(), out=tmp_path / "o3", **common)


def test_the_control_card_is_closed_like_the_probed_ones(tmp_path):
    # The owner's reproduction: kd-task view on the card with the known remarks succeeded and leaks() was
    # empty. A hidden card is denied, named in the prompt, and its successful read is a leak.
    fake = FakeClaude()
    ep.run(name="analyst", folder=FOLDER, settings=SETTINGS, snapshots=SNAPS, out=tmp_path / "o",
           model="claude-opus-5-5", efforts=["high"], repeats=1, home=HOME, hidden=["KD-9044", "KD-9059"],
           runner=fake)
    argv, stdin = fake.calls[1][0], fake.calls[1][1]
    deny = json.loads(argv[argv.index("--settings") + 1])["permissions"]["deny"]
    assert "Bash(kd-task view KD-9044*)" in deny and "Bash(kd-task view KD-9059*)" in deny
    assert "do not open KD-190, KD-9041, KD-9044, KD-9059 on the board" in stdin
    closed = ep.closed_paths(FOLDER, HOME)
    cards = ["KD-190", "KD-9041", "KD-9044", "KD-9059"]
    assert ep.leaks(_call("a", "kd-task view KD-9044"), cards, closed) == ["kd-task view KD-9044"]
    assert ep.leaks(_call("a", "kd-task view KD-9044", error=True), cards, closed) == []
    for quoted in ('kd-task view "KD-9044"', "kd-task view 'KD-9044' | tail"):
        assert ep.leaks(_call("a", quoted), cards, closed) == ["kd-task view KD-9044"], quoted
    # The agent's chat with the owner may have discussed the briefs.
    assert "Bash(kd-task chat*)" in deny
    assert ep.leaks(_call("a", "kd-task chat"), cards, closed) == ["kd-task chat"]
    with pytest.raises(ep.ProbeError, match="not a card id"):
        ep.run(name="analyst", folder=FOLDER, settings=SETTINGS, snapshots=SNAPS, out=tmp_path / "o2",
               model="claude-opus-5-5", efforts=["high"], repeats=1, home=HOME, hidden=["9044"], runner=FakeClaude())


def test_the_probe_never_reads_the_answers_of_the_runs_before_it(tmp_path):
    # --out lies under the probe's cwd (the agent's work/tmp): run 2 must not read run 1's answer.
    out = tmp_path / "o"
    fake = FakeClaude()
    ep.run(name="analyst", folder=FOLDER, settings=SETTINGS, snapshots=SNAPS, out=out, model="claude-opus-5-5",
           efforts=["high"], repeats=1, home=HOME, hidden=["KD-9044"], runner=fake)
    argv = fake.calls[1][0]
    merged = json.loads(argv[argv.index("--settings") + 1])
    own = str(out.resolve())
    assert f"Read(/{own}/**)" in merged["permissions"]["deny"]
    assert own in merged["sandbox"]["filesystem"]["denyRead"]
    closed = ep.closed_paths(FOLDER, HOME, out=own)
    assert ep.leaks(_call("a", f"cat {own}/KD-190-medium-1.md"), ["KD-190"], closed) == [own]


def test_run_needs_the_control_card(tmp_path, capsys):
    with pytest.raises(SystemExit):
        ep.main(["run", "--snapshots", str(tmp_path), "--out", str(tmp_path / "o"), "--model", "opus"])
    assert "--hide" in capsys.readouterr().err


def _git_repo(path: Path) -> str:
    import subprocess

    def git(*args):
        return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout

    path.mkdir()
    git("init", "-q")
    (path / "gate.py").write_text("LIMIT = 1\n")
    (path / "agents/x").mkdir(parents=True)
    (path / "CLAUDE.md").write_text("owner rules\n")
    (path / "agents/x/CLAUDE.md").write_text("agent rules\n")
    (path / "agents/x/notes.md").write_text("doc\n")
    git("add", "gate.py", "CLAUDE.md", "agents")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "old")
    old = git("rev-parse", "HEAD").strip()
    (path / "gate.py").write_text("LIMIT = 2\n")
    (path / "zgrep.py").write_text("new\n")
    git("add", "gate.py", "zgrep.py")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "new")
    return old


def test_the_repository_is_pinned_at_the_snapshots_commit(tmp_path):
    old = _git_repo(tmp_path / "src")
    out = tmp_path / "snap"
    assert ep.pin_repo(out, "digipaddock", str(tmp_path / "src"), old[:7]) == old
    assert (out / "repo/digipaddock/gate.py").read_text() == "LIMIT = 1\n"
    assert not (out / "repo/digipaddock/zgrep.py").exists()  # the later implementation is not there
    # Under the probe's cwd Claude Code would load the repository's CLAUDE.md files as instructions.
    assert not (out / "repo/digipaddock/CLAUDE.md").exists() and not (out / "repo/digipaddock/agents/x/CLAUDE.md").exists()
    assert (out / "repo/digipaddock/agents/x/notes.md").read_text() == "doc\n"
    assert ep.pin_repo(tmp_path / "z", "digipaddock", str(tmp_path / "src") + "/", old) == old  # a trailing slash
    ep.write_snapshots(out, [ep.Snapshot("KD-190", "draft r5", "Цель", ())], {"digipaddock": old})
    repos = ep.load_repos(out)
    assert repos == {"digipaddock": (old, str((out / "repo/digipaddock").resolve()))}
    with pytest.raises(ep.ProbeError, match="no commit"):
        ep.pin_repo(tmp_path / "x", "digipaddock", str(tmp_path / "src"), "deadbeef")
    for name, rev in (("Digipaddock", old), ("digipaddock", "HEAD; rm")):
        with pytest.raises(ep.ProbeError, match="expected"):
            ep.pin_repo(tmp_path / "y", name, str(tmp_path / "src"), rev)


def test_a_pinned_repository_closes_the_live_copies_and_kd_repo(tmp_path):
    repos = {"digipaddock": ("a" * 40, "/srv/digipaddock-agents/analyst/work/tmp/kd9044/snapshots/repo/digipaddock")}
    fake = FakeClaude()
    ep.run(name="analyst", folder=FOLDER, settings=SETTINGS, snapshots=SNAPS, out=tmp_path / "o",
           model="claude-opus-5-5", efforts=["high"], repeats=1, home=HOME, hidden=["KD-9044"], repos=repos,
           runner=fake)
    argv, stdin = fake.calls[1][0], fake.calls[1][1]
    merged = json.loads(argv[argv.index("--settings") + 1])
    assert "Bash(kd-repo *)" in merged["permissions"]["deny"]
    assert "Read(//srv/digipaddock-repos/**)" in merged["permissions"]["deny"]
    assert "/srv/digipaddock-repos" in merged["sandbox"]["filesystem"]["denyRead"]
    assert f"commit {'a' * 12}) is at {repos['digipaddock'][1]}: read the code there" in stdin
    assert "/srv/digipaddock-repos and kd-repo show a later state" in stdin
    closed = ep.closed_paths(FOLDER, HOME, pinned=True)
    seen = [*_call("a", "grep -n LIMIT /srv/digipaddock-repos/digipaddock/gate.py"), *_call("b", "kd-repo log digipaddock -n 5")]
    assert ep.leaks(seen, ["KD-190"], closed, pinned=True) == ["/srv/digipaddock-repos", "kd-repo "]
    # Without a pinned copy the live repository is the analyst's normal source.
    plain = json.loads(ep.probe_settings(SETTINGS, ["KD-190"], FOLDER, HOME))
    assert "Bash(kd-repo *)" not in plain["permissions"]["deny"]


def test_run_never_writes_into_a_used_directory(tmp_path):
    # A second run into the same --out would mix its journal with answers the first one left.
    (tmp_path / "o").mkdir()
    (tmp_path / "o/runs.jsonl").write_text("{}\n")
    fake = FakeClaude()
    with pytest.raises(ep.ProbeError, match="is not empty"):
        ep.run(name="analyst", folder=FOLDER, settings=SETTINGS, snapshots=SNAPS, out=tmp_path / "o",
               model="claude-opus-5-5", efforts=["high"], repeats=1, home=str(tmp_path), runner=fake)
    assert fake.calls == []


def test_outcome_of_a_broken_session_is_an_error():
    assert ep.outcome(["garbage"])["is_error"] is True
    assert ep.outcome(_stream(error=True))["is_error"] is True
    good = ep.outcome(_stream())
    assert good["is_error"] is False and good["answer"] == "Находки: 1." and good["claude_code_version"] == "2.9.0"


def test_summary_table_per_card_and_effort():
    rows = [
        {"card": "KD-190", "effort": "high", "exit": 0, "is_error": False, "model": "claude-opus-5-5",
         "duration_ms": 60000, "num_turns": 6, "output_tokens": 1000, "total_cost_usd": 1.0, "leaks": []},
        {"card": "KD-190", "effort": "high", "exit": 0, "is_error": False, "model": "claude-opus-5-5",
         "duration_ms": 120000, "num_turns": 8, "output_tokens": 2000, "total_cost_usd": 1.5, "leaks": []},
        {"card": "KD-190", "effort": "high", "exit": 0, "is_error": False, "model": "claude-opus-5-5",
         "duration_ms": 9000, "num_turns": 1, "output_tokens": 9, "total_cost_usd": 0.5,
         "leaks": ["kd-task view KD-190"]},
        {"card": "KD-190", "effort": "medium", "exit": 124, "is_error": True, "model": None,
         "duration_ms": None, "num_turns": None, "output_tokens": None, "total_cost_usd": None, "leaks": []},
    ]
    table = ep.summary(rows).splitlines()
    assert table[0].startswith("card\teffort\truns\terrors\tleaked")
    assert table[1] == "KD-190\thigh\t3\t0\t1\tclaude-opus-5-5\t90.0\t7.0\t1500\t3.0"
    assert table[2] == "KD-190\tmedium\t1\t1\t0\tNone\t-\t-\t-\t0"


def test_run_as_another_user_is_refused(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(ep.agents, "user_of", lambda name: "digi-analyst")
    (tmp_path / "snapshots.json").write_text("{}")
    assert ep.main(["run", "--snapshots", str(tmp_path), "--out", str(tmp_path / "o"), "--model", "opus",
                    "--hide", "KD-9044"]) == 1
    assert "run as the agent's own user digi-analyst" in capsys.readouterr().err
