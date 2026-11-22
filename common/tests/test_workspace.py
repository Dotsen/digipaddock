"""a session's own notes and temp files; the shared notes appended under a lock; the one-time split."""

import json
import multiprocessing
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kd import workspace

SCRIPT = Path(workspace.__file__)


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "work").mkdir()
    (tmp_path / "memory/notes.md").write_text("# notes\n", encoding="utf-8")
    return str(tmp_path)


def test_two_cards_and_the_chat_get_separate_documented_paths(folder):
    one = workspace.prepare(folder, card="KD-1")
    two = workspace.prepare(folder, card="KD-2")
    chat = workspace.prepare(folder, thread="160")
    assert one == (f"{folder}/memory/tasks/KD-1.md", f"{folder}/work/tmp/cards/KD-1")
    assert two.notes.endswith("memory/tasks/KD-2.md") and two.tmp.endswith(
        "work/tmp/cards/KD-2"
    )
    assert chat == (f"{folder}/memory/chats/160.md", f"{folder}/work/tmp/chats/160")
    assert (
        len({one.tmp, two.tmp, chat.tmp}) == 3
        and len({one.notes, two.notes, chat.notes}) == 3
    )
    for paths in (one, two, chat):
        assert os.path.isdir(paths.tmp) and os.path.isfile(paths.notes)


def test_a_resumed_thread_gets_the_same_paths_and_content(folder):
    first = workspace.prepare(folder, card="KD-7")
    Path(first.notes).write_text("plan: step 2\n", encoding="utf-8")
    Path(first.tmp, "kd-pr.md").write_text("body", encoding="utf-8")
    again = workspace.prepare(folder, card="KD-7")
    assert again == first
    assert Path(again.notes).read_text(encoding="utf-8") == "plan: step 2\n"
    assert Path(again.tmp, "kd-pr.md").read_text(encoding="utf-8") == "body"


@pytest.mark.parametrize(
    "card,thread",
    [("../x", ""), ("KD-0", ""), ("kd-1", ""), ("", "0"), ("", "1/2"), ("", "")],
)
def test_a_context_outside_the_shapes_is_refused(folder, card, thread):
    with pytest.raises(workspace.WorkspaceError):
        workspace.session_paths(folder, card=card, thread=thread)


def test_a_link_in_place_of_the_session_folder_is_refused(folder, tmp_path_factory):
    workspace.prepare_base(folder)
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    os.symlink(elsewhere, f"{folder}/work/tmp/cards/KD-3")
    with pytest.raises(workspace.WorkspaceError):
        workspace.prepare(folder, card="KD-3")


def test_same_tmp_names_of_two_cards_do_not_overwrite_each_other(folder):
    one, two = (
        workspace.prepare(folder, card="KD-1"),
        workspace.prepare(folder, card="KD-2"),
    )
    Path(one.tmp, "kd-pr-body.md").write_text("one", encoding="utf-8")
    Path(two.tmp, "kd-pr-body.md").write_text("two", encoding="utf-8")
    assert Path(one.tmp, "kd-pr-body.md").read_text(encoding="utf-8") == "one"


def test_add_shared_appends_one_dated_line(folder):
    line = workspace.add_shared(folder, "pitfall:\n  two   lines", today="2026-11-05")
    assert line == "- 2026-11-05: pitfall: two lines\n"
    workspace.add_shared(folder, "2026-11-04: already dated")
    assert Path(folder, "memory/notes.md").read_text(encoding="utf-8") == (
        "# notes\n- 2026-11-05: pitfall: two lines\n- 2026-11-04: already dated\n"
    )


def test_add_shared_ends_an_unterminated_last_line_first(folder):
    Path(folder, "memory/notes.md").write_text("- old", encoding="utf-8")
    workspace.add_shared(folder, "new", today="2026-11-05")
    assert (
        Path(folder, "memory/notes.md").read_text(encoding="utf-8")
        == "- old\n- 2026-11-05: new\n"
    )


@pytest.mark.parametrize("text", ["", "   ", "x" * (workspace.ENTRY_MAX + 1), "a\x00b"])
def test_add_shared_refuses_a_bad_entry(folder, text):
    with pytest.raises(workspace.WorkspaceError):
        workspace.add_shared(folder, text)


def test_add_shared_refuses_a_link(folder, tmp_path_factory):
    target = tmp_path_factory.mktemp("t") / "other.md"
    target.write_text("", encoding="utf-8")
    os.remove(f"{folder}/memory/notes.md")
    os.symlink(target, f"{folder}/memory/notes.md")
    with pytest.raises(workspace.WorkspaceError):
        workspace.add_shared(folder, "x")
    assert target.read_text(encoding="utf-8") == ""


def _writer(folder, session, count, start):
    start.wait()
    for n in range(count):
        workspace.add_shared(folder, f"session {session} entry {n}", today="2026-11-05")
        own = workspace.prepare(folder, card=f"KD-{session}")
        with open(own.notes, "a", encoding="utf-8") as notes:
            notes.write(f"step {n}\n")
        Path(own.tmp, "same-name.txt").write_text(f"{session}:{n}", encoding="utf-8")


def test_two_parallel_sessions_lose_nothing(folder):
    """AC2/AC3: two processes of one agent at once — every shared entry stays, each card keeps its own files."""
    count = 150
    ctx = multiprocessing.get_context("fork")
    start = ctx.Event()
    workers = [
        ctx.Process(target=_writer, args=(folder, s, count, start)) for s in (1, 2)
    ]
    for worker in workers:
        worker.start()
    start.set()
    for worker in workers:
        worker.join(60)
        assert worker.exitcode == 0
    lines = Path(folder, "memory/notes.md").read_text(encoding="utf-8").splitlines()
    for session in (1, 2):
        mine = [
            line
            for line in lines
            if line.startswith(f"- 2026-11-05: session {session} ")
        ]
        assert mine == [
            f"- 2026-11-05: session {session} entry {n}" for n in range(count)
        ]
        own = workspace.session_paths(folder, card=f"KD-{session}")
        assert Path(own.notes).read_text(encoding="utf-8") == "".join(
            f"step {n}\n" for n in range(count)
        )
        assert (
            Path(own.tmp, "same-name.txt").read_text(encoding="utf-8")
            == f"{session}:{count - 1}"
        )
    assert len(lines) == 1 + 2 * count


NOTES = """# dev notes

- 2026-10-06: KD-104 reassigned to @sysadmin.
- 2026-10-09: OLD-117 claimed, branch agent/OLD-117-release-probe, PR #5.
  continued under it
- 2026-10-10: OLD-67 claimed; found something about KD-29 too.
- Pitfall (2026-10-20): KD-9044 taught: read the whole thread.
- 2026-10-21: no card here at all.
"""


def test_migrate_moves_only_entries_of_one_card_and_keeps_the_original(folder):
    Path(folder, "memory/notes.md").write_text(NOTES, encoding="utf-8")
    report = workspace.migrate(folder, stamp="20261105T120000Z")
    snapshot = Path(folder, "memory/legacy/notes-20261105T120000Z.md")
    assert snapshot.read_text(encoding="utf-8") == NOTES
    assert [(m["line"], m["card"]) for m in report["moved"]] == [
        (3, "KD-104"),
        (4, "KD-117"),
    ]
    assert [(k["line"], k["why"]) for k in report["kept"]] == [
        (6, "2 cards"),
        (7, "pitfall"),
        (8, "no card"),
    ]
    assert Path(folder, "memory/tasks/KD-117.md").read_text(encoding="utf-8") == (
        "- 2026-10-09: OLD-117 claimed, branch agent/OLD-117-release-probe, PR #5.\n  continued under it\n"
    )
    shared = Path(folder, "memory/notes.md").read_text(encoding="utf-8")
    assert shared.startswith("# dev notes\n\n- 2026-10-10: OLD-67")
    assert (
        "Pitfall (2026-10-20)" in shared
        and "no card here" in shared
        and "KD-104" not in shared
    )
    assert "memory/legacy/notes-20261105T120000Z.md" in shared
    text_map = Path(folder, "memory/legacy/migration.md").read_text(encoding="utf-8")
    assert (
        "line 4 → `memory/tasks/KD-117.md`" in text_map
        and "line 6: 2 cards" in text_map
    )
    assert (
        json.loads(
            Path(folder, "memory/legacy/migration.json").read_text(encoding="utf-8")
        )["state"]
        == "done"
    )


def test_migrate_runs_once(folder):
    Path(folder, "memory/notes.md").write_text(NOTES, encoding="utf-8")
    workspace.migrate(folder, stamp="20261105T120000Z")
    workspace.add_shared(folder, "after the move", today="2026-11-06")
    before = {
        p: p.read_bytes() for p in Path(folder, "memory").rglob("*") if p.is_file()
    }
    again = workspace.migrate(folder, stamp="20261106T120000Z")
    assert again["state"] == "already"
    assert {
        p: p.read_bytes() for p in Path(folder, "memory").rglob("*") if p.is_file()
    } == before


def test_a_migration_cut_short_does_not_duplicate_a_moved_entry(folder):
    Path(folder, "memory/notes.md").write_text(NOTES, encoding="utf-8")
    workspace.prepare_base(folder)
    entry = "- 2026-10-06: KD-104 reassigned to @sysadmin.\n"
    Path(folder, "memory/tasks/KD-104.md").write_text(
        "own line\n" + entry, encoding="utf-8"
    )
    workspace.migrate(folder, stamp="20261105T120000Z")
    assert (
        Path(folder, "memory/tasks/KD-104.md").read_text(encoding="utf-8")
        == "own line\n" + entry
    )


def _cut_short(folder):
    """The state of a migration that stopped after the split of notes.md, before its maps."""
    Path(folder, "memory/legacy/migration.json").unlink()
    Path(folder, "memory/legacy/migration.md").unlink()
    Path(folder, "memory/legacy/migration.pending").write_text(
        '{"stamp": "20261105T120000Z"}\n', encoding="utf-8"
    )


def test_a_migration_cut_after_the_split_finishes_without_a_second_pass(folder):
    # review: a second run must not snapshot the split file again nor move its own line to a card
    Path(folder, "memory/notes.md").write_text(NOTES, encoding="utf-8")
    workspace.migrate(folder, stamp="20261105T120000Z")
    split = Path(folder, "memory/notes.md").read_bytes()
    tasks = {p.name: p.read_bytes() for p in Path(folder, "memory/tasks").iterdir()}
    _cut_short(folder)
    report = workspace.migrate(folder, stamp="20261106T000000Z")
    assert report["state"] == "done" and report["stamp"] == "20261105T120000Z"
    assert Path(folder, "memory/notes.md").read_bytes() == split
    assert {
        p.name: p.read_bytes() for p in Path(folder, "memory/tasks").iterdir()
    } == tasks
    assert [p.name for p in Path(folder, "memory/legacy").glob("notes-*.md")] == [
        "notes-20261105T120000Z.md"
    ]
    assert not Path(folder, "memory/legacy/migration.pending").exists()
    assert workspace.migrate(folder)["state"] == "already"


def test_a_migration_cut_before_the_split_keeps_an_entry_added_meanwhile(folder):
    Path(folder, "memory/notes.md").write_text(NOTES, encoding="utf-8")
    workspace.prepare_base(folder)
    Path(folder, "memory/legacy/migration.pending").write_text(
        '{"stamp": "20261105T120000Z"}\n', encoding="utf-8"
    )
    Path(folder, "memory/legacy/notes-20261105T120000Z.md").write_text(
        NOTES, encoding="utf-8"
    )
    workspace.add_shared(folder, "added between the runs", today="2026-11-05")
    workspace.migrate(folder)
    shared = Path(folder, "memory/notes.md").read_text(encoding="utf-8")
    assert (
        shared.endswith("- 2026-11-05: added between the runs\n")
        and "KD-104" not in shared
    )


def test_a_heading_between_entries_stays_in_place_and_never_moves(folder):
    text = "# notes\n\n## October\n- 2026-10-06: KD-104 x\n  more\n\n## November\n- Pitfall: y\nplain paragraph\n"
    Path(folder, "memory/notes.md").write_text(text, encoding="utf-8")
    workspace.migrate(folder, stamp="20261105T120000Z")
    assert (
        Path(folder, "memory/tasks/KD-104.md").read_text(encoding="utf-8")
        == "- 2026-10-06: KD-104 x\n  more\n\n"
    )
    shared = Path(folder, "memory/notes.md").read_text(encoding="utf-8")
    assert shared.startswith(
        "# notes\n\n## October\n## November\n- Pitfall: y\nplain paragraph\n- 2026-11-05: the shared"
    )


def test_migrate_of_no_notes_only_records_it(folder):
    Path(folder, "memory/notes.md").write_text("", encoding="utf-8")
    report = workspace.migrate(folder, stamp="20261105T120000Z")
    assert report["original"] is None and report["moved"] == []
    assert Path(folder, "memory/notes.md").read_text(encoding="utf-8") == ""


def test_main_prepare_prints_the_paths(folder):
    out = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "prepare", folder, "chat", "42"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out.splitlines() == [
        f"{folder}/memory/chats/42.md",
        f"{folder}/work/tmp/chats/42",
    ]
    bad = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "prepare", folder, "../x", "42"],
        capture_output=True,
        text=True,
    )
    assert bad.returncode == 1
