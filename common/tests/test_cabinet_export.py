"""the nightly Markdown export of the cabinet and its git snapshot."""

import importlib.util
import io
import os
import subprocess
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

import pytest

import legacy_md
from kd.cabinet import client, exporter

sys.path.insert(0, str(Path(__file__).resolve().parent))  # cabinet_env
from cabinet_env import CabinetEnv  # noqa: E402

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "cabinet" / "legacy-card.md"
BIN = Path(__file__).resolve().parents[1] / "bin"
SYSTEMD = Path(__file__).resolve().parents[1] / "systemd"


@pytest.fixture
def cab(tmp_path):
    return CabinetEnv(tmp_path / "cab")


def test_render_round_trips_through_the_legacy_parser(tmp_path):
    cab = CabinetEnv(tmp_path)
    card = cab.card(
        title="Fix: nginx/cache?",
        status="Review",
        labels=["from:sysadmin"],
        servers=["node-c"],
        acceptance=["one", "two"],
        dependencies=[],
    )
    cab.store.edit_card(
        card.id,
        None,
        {"ac_check": [1], "comment": {"text": "[boss] hi", "author": "@boss"}},
        actor="agent:boss",
    )
    record = cab.store.card(card.id).to_dict()
    messages = cab.store.messages(card.id)
    text = exporter.render_card(record, messages)
    parsed, parsed_messages, warnings = legacy_md.parse_card(text.encode("utf-8"))
    assert warnings == [] and parsed["acceptance"] == [
        {"text": "one", "checked": True},
        {"text": "two", "checked": False},
    ]
    assert (
        parsed["servers"] == ["node-c"]
        and parsed["labels"] == ["from:sysadmin"]
        and parsed_messages[0]["author"] == "@boss"
    )
    assert exporter.file_name(record) == "kd-1 - Fix-nginx-cache.md"


def test_an_imported_card_exports_to_what_it_was_imported_from(cab):
    record, messages, _ = legacy_md.parse_card(FIXTURE.read_bytes())
    cab.store.import_card(record, messages)
    text = exporter.render_card(
        cab.store.card("KD-1").to_dict(), cab.store.messages("KD-1")
    )
    again, again_messages, warnings = legacy_md.parse_card(text.encode("utf-8"))
    assert warnings == [] and again == record and again_messages == messages
    assert (
        "  - 'server:example-web'\n" in text
        and "title: 'example-web: root filesystem is 85% full'\n" in text
    )


def test_header_values_are_quoted_and_one_line(cab):
    card = cab.card(title="it's\nmultiline", description="## not a section\ntext")
    text = exporter.render_card(cab.store.card(card.id).to_dict(), [])
    assert "title: 'it''s multiline'\n" in text
    parsed, _, warnings = legacy_md.parse_card(text.encode("utf-8"))
    assert parsed["title"] == "it's multiline" and warnings == []
    assert (
        parsed["description"] == "## not a section\ntext"
    )  # a header-like line of a text stays text


def test_messages_of_every_kind_become_comments_with_board_names(cab):
    card = cab.card()
    cab.store.add_message(
        card.id, author="user:owner", kind="text", body="от владельца"
    )
    cab.store.add_message(card.id, author="agent:dev", kind="progress", body="шаг 1")
    text = exporter.render_card(
        cab.store.card(card.id).to_dict(), cab.store.messages(card.id)
    )
    _, messages, _ = legacy_md.parse_card(text.encode("utf-8"))
    assert [(m["author"], m["body"]) for m in messages] == [
        ("@owner", "от владельца"),
        ("@dev", "[progress] шаг 1"),
    ]


@pytest.mark.parametrize(
    "title,name",
    [
        ("Простой заголовок", "kd-1 - Простой-заголовок.md"),
        ('a\\b/c:d*e?f"g<h>i|j', "kd-1 - a-b-c-d-e-f-g-h-i-j.md"),
        (
            "ревью assign без --spec, create spec:docs",
            "kd-1 - ревью-assign-без-spec-create-spec-docs.md",
        ),
        (
            "postgresql@18-main и %TEMP% (site + admin)",
            "kd-1 - postgresql18-main-и-TEMP-site-admin.md",
        ),
        ("???", "kd-1.md"),
        # the names Backlog.md gave the same titles on the stage board
        (
            "node-b: /backups читается всеми (файлы 644, каталог 755)",
            "kd-1 - node-b-backups-читается-всеми-файлы-644-каталог-755.md",
        ),
        (
            "Проба выкатки: строка в docs/superpowers/probes/release-probe.md",
            "kd-1 - Проба-выкатки-строка-в-docs-superpowers-probes-release-probe.md.md",
        ),
        (
            "Подпроект 8: агент BOSS — координатор команды агентов (спека → план)",
            "kd-1 - Подпроект-8-агент-BOSS-—-координатор-команды-агентов-спека-→-план.md",
        ),
        ("x" * 200, "kd-1 - " + "x" * 120 + ".md"),
    ],
)
def test_file_name(title, name):
    assert exporter.file_name({"id": "KD-1", "title": title}) == name


# --- export ---------------------------------------------------------------------------------------------


def git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def board(tmp_path):
    root = tmp_path / "board"
    root.mkdir()
    git("init", "-q", cwd=root)
    git("config", "user.email", "t@t", cwd=root)
    git("config", "user.name", "t", cwd=root)
    return root


def test_export_writes_cards_and_commits(cab, board):
    first = cab.card(title="Первая")
    cab.card(title="Вторая")
    root = cab.client("root")
    err = io.StringIO()
    # milestones are gone — the files an earlier export wrote are history, neither rewritten nor removed
    (board / "backlog" / "milestones").mkdir(parents=True)
    (board / "backlog" / "milestones" / "m-9 - Идеи.md").write_text("old", encoding="utf-8")
    assert exporter.export(root, str(board), err=err) == "committed"
    tasks = sorted(os.listdir(board / "backlog" / "tasks"))
    assert tasks == ["kd-1 - Первая.md", "kd-2 - Вторая.md"]
    assert (board / "backlog" / "milestones" / "m-9 - Идеи.md").read_text(encoding="utf-8") == "old"
    assert "milestone" not in (board / "backlog" / "tasks" / "kd-2 - Вторая.md").read_text(encoding="utf-8")
    assert git("log", "--format=%s", cwd=board).startswith("board: ")
    # nothing changed: nothing written, nothing committed
    stamp = os.stat(board / "backlog" / "tasks" / "kd-1 - Первая.md").st_mtime_ns
    assert exporter.export(root, str(board), err=err) == "clean"
    assert (
        os.stat(board / "backlog" / "tasks" / "kd-1 - Первая.md").st_mtime_ns == stamp
    )
    # a card file under another name (an older title) and a card the cabinet does not have: both go
    (board / "backlog" / "tasks" / "kd-1 - Старое имя.md").write_text(
        "old", encoding="utf-8"
    )
    (board / "backlog" / "tasks" / "kd-77 - Нет такой.md").write_text(
        "old", encoding="utf-8"
    )
    cab.store.edit_card(first.id, None, {"status": "Done"}, actor="user:owner")
    assert exporter.export(root, str(board), err=err) == "committed"
    assert sorted(os.listdir(board / "backlog" / "tasks")) == [
        "kd-1 - Первая.md",
        "kd-2 - Вторая.md",
    ]
    assert "status: Done\n" in (
        board / "backlog" / "tasks" / "kd-1 - Первая.md"
    ).read_text(encoding="utf-8")
    assert err.getvalue() == ""


def test_export_keeps_files_that_are_not_cards(cab, board):
    (board / "backlog" / "tasks").mkdir(parents=True)
    (board / "backlog" / "tasks" / "README.txt").write_text("x", encoding="utf-8")
    (board / "backlog" / "config.yml").write_text("x", encoding="utf-8")
    cab.card()
    assert (
        exporter.export(cab.client("root"), str(board), err=io.StringIO())
        == "committed"
    )
    assert (board / "backlog" / "tasks" / "README.txt").exists() and (
        board / "backlog" / "config.yml"
    ).exists()


class Down:
    def call(self, request):
        raise client.CabinetError("unavailable", "board unavailable")


def test_export_with_the_cabinet_down_writes_nothing(board):
    err = io.StringIO()
    assert exporter.export(Down(), str(board), err=err) == "error"
    assert not (board / "backlog").exists() and "кабинет недоступен" in err.getvalue()


def test_git_failure_is_an_error(cab, board):
    cab.card()
    calls = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1, "", "boom")

    err = io.StringIO()
    assert exporter.export(cab.client("root"), str(board), run=run, err=err) == "error"
    assert "git add" in err.getvalue() and calls[0][-2:] == ["--", "backlog"]


def test_a_nested_git_in_backlog_stops_the_history(cab, board):
    (board / "backlog" / ".git").mkdir(parents=True)
    cab.card()
    err = io.StringIO()
    assert (
        exporter.export(cab.client("root"), str(board), err=err) == "error"
        and ".git" in err.getvalue()
    )


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_a_linked_tasks_directory_is_refused(cab, board, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (board / "backlog").mkdir()
    os.symlink(tmp_path / "elsewhere", board / "backlog" / "tasks")
    cab.card()
    err = io.StringIO()
    assert exporter.export(cab.client("root"), str(board), err=err) == "error"
    assert os.listdir(tmp_path / "elsewhere") == []


@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_a_planted_link_is_replaced_not_followed(cab, board, tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("keep", encoding="utf-8")
    (board / "backlog" / "tasks").mkdir(parents=True)
    os.symlink(target, board / "backlog" / "tasks" / "kd-1 - Card.md")
    os.symlink(target, board / "backlog" / "tasks" / "kd-9 - stale.md")
    cab.card()
    assert (
        exporter.export(cab.client("root"), str(board), err=io.StringIO())
        == "committed"
    )
    assert target.read_text(encoding="utf-8") == "keep"
    assert not os.path.islink(board / "backlog" / "tasks" / "kd-1 - Card.md")
    assert not os.path.lexists(board / "backlog" / "tasks" / "kd-9 - stale.md")


# --- kd-cabinet export and its units ---------------------------------------------------------------------


def _kd_cabinet():
    loader = SourceFileLoader("kd_cabinet", str(BIN / "kd-cabinet"))
    spec = importlib.util.spec_from_loader("kd_cabinet", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_kd_cabinet_export(cab, board):
    cab.card()
    out, err = io.StringIO(), io.StringIO()
    tool = _kd_cabinet()
    assert (
        tool.main(["export", str(board)], client=cab.client("root"), out=out, err=err)
        == 0
    )
    assert out.getvalue() == "committed\n"
    assert tool.main(["export", str(board)], client=Down(), out=out, err=err) == 1


def test_export_units():
    service = (SYSTEMD / "digipaddock-cabinet-export.service").read_text(encoding="utf-8")
    timer = (SYSTEMD / "digipaddock-cabinet-export.timer").read_text(encoding="utf-8")
    assert (
        "ExecStart=/opt/digipaddock/common/bin/kd-cabinet export /srv/digipaddock-agents/board\n"
        in service
    )
    assert (
        "User=" not in service
        and "Type=oneshot" in service
        and "OnFailure=digipaddock-alert@" in service
    )
    assert "OnCalendar=*-*-* 04:10:00\n" in timer and "Persistent=true" in timer


def test_export_makes_no_milestones_directory(cab, board):
    # a board that never had milestones gets no directory for them
    cab.card(title="Первая")
    assert exporter.export(cab.client("root"), str(board), err=io.StringIO()) == "committed"
    assert sorted(os.listdir(board / "backlog")) == ["tasks"]
