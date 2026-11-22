"""``kd-cabinet export``: the cabinet as Backlog.md files in a git repository, nightly.

The cabinet is the board; this is its human-readable history. Every card becomes
``backlog/tasks/<id> - <title>.md`` in the format the old board wrote (header, sections with their
markers, the thread as the COMMENTS block), so the tests' ``legacy_md`` reads it back to the same record and
messages. A file is rewritten only
when its bytes change (atomic replace: a link planted at the name is replaced, never followed); a
card file the cabinet does not have under that name (an older title, a card file from elsewhere) is
removed; other files stay. Then ``git_commit`` records the change (the old ``kd-board sync`` commit).

Everything is read from the cabinet before the first write: an unavailable cabinet leaves the files
as they were. ``backlog`` and ``backlog/tasks`` must be real directories, not links.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from typing import Dict, List, Mapping

from kd import atomic_file
from kd.cabinet import client as cabinet_client
from kd.cabinet import view

SECTIONS = (  # (header, column, begin marker, end marker, blank line after the header)
    (
        "Description",
        "description",
        "<!-- SECTION:DESCRIPTION:BEGIN -->",
        "<!-- SECTION:DESCRIPTION:END -->",
        True,
    ),
    (
        "Acceptance Criteria",
        "acceptance",
        "<!-- AC:BEGIN -->",
        "<!-- AC:END -->",
        False,
    ),
    ("Definition of Done", "dod", "<!-- DOD:BEGIN -->", "<!-- DOD:END -->", False),
    (
        "Implementation Plan",
        "plan",
        "<!-- SECTION:PLAN:BEGIN -->",
        "<!-- SECTION:PLAN:END -->",
        True,
    ),
    (
        "Implementation Notes",
        "notes",
        "<!-- SECTION:NOTES:BEGIN -->",
        "<!-- SECTION:NOTES:END -->",
        True,
    ),
    (
        "Final Summary",
        "summary",
        "<!-- SECTION:FINAL_SUMMARY:BEGIN -->",
        "<!-- SECTION:FINAL_SUMMARY:END -->",
        True,
    ),
)
SLUG_MAX = 120
MODE = 0o644
MAX_FILE = (
    4 * 1024 * 1024
)  # an existing file is compared up to this size (a bigger one is rewritten)
# Backlog.md's file names, so the first export renames only cards whose title changed since their
# file was made: a character Windows forbids becomes a dash, other punctuation is dropped, a run of
# whitespace and dashes is one dash ("a: b/c (d, e) --f" -> "a-b-c-d-e-f")
_WIN = re.compile(r'[<>:"/\\|?*]')
_DROP = re.compile(r"[()\[\]{},;@%+!#$^&~=`'\x00-\x1f\x7f]")
_DASHES = re.compile(r"[\s-]+")
_CARD_FILE = re.compile(r"[a-z][a-z0-9]{0,15}-[1-9][0-9]{0,5}(?: - .*)?\.md")


def file_name(record: Mapping) -> str:
    """``kd-1 - Title.md`` of a card (its ``id`` and ``title``)."""
    slug = slug_of(record.get("title", ""))
    return (
        f"{record['id'].lower()} - {slug}.md" if slug else f"{record['id'].lower()}.md"
    )


def slug_of(title: str) -> str:
    return _DASHES.sub("-", _DROP.sub("", _WIN.sub("-", title))).strip("-")[:SLUG_MAX]


def render_card(record: Mapping, messages: List[Mapping]) -> str:
    assignee = record.get("assignee") or ""
    labels = list(record.get("labels") or []) + [
        f"server:{s}" for s in record.get("servers") or []
    ]
    head = [
        "---",
        f"id: {record['id']}",
        f"title: {_quote(record.get('title', ''))}",
        f"status: {record['status']}",
    ]
    head += _list("assignee", [assignee] if assignee else [])
    head.append(f"created_date: {_quote(_md_time(record['created']))}")
    head.append(f"updated_date: {_quote(_md_time(record['updated']))}")
    head += _list("labels", labels)
    head += _list("dependencies", list(record.get("dependencies") or []))
    for key, column in (
        ("project", "project"),
        ("priority", "priority"),
        ("due_date", "due"),
        ("parent_task_id", "parent"),
    ):
        if record.get(column):
            head.append(f"{key}: {_quote(record[column])}")
    lines = head + ["---", ""]
    for header, column, begin, end, gap in SECTIONS:
        if column == "acceptance":
            body = "\n".join(
                f"- [{'x' if a.get('checked') else ' '}] #{i} {a['text']}"
                for i, a in enumerate(record.get("acceptance") or [], 1)
            )
        else:
            body = (record.get(column) or "").strip("\n")
        if body:
            lines += (
                [f"## {header}"] + ([""] if gap else []) + [begin, _text(body), end, ""]
            )
    messages = view.shown(messages)
    if messages:
        lines += ["## Comments", "", "<!-- COMMENTS:BEGIN -->"]
        for i, message in enumerate(messages):
            body = message.get("body", "")
            if message.get("kind", "text") != "text":  # a system message in words (key + parameters)
                body = f"[{message['kind']}] {view.message_text(message)}"
            lines += ([""] if i else []) + [
                f"author: {_board_name(message.get('author', ''))}",
                f"created: {_md_time(message.get('created', ''))}",
                "---",
                _text(body),
                "---",
            ]
        lines += ["<!-- COMMENTS:END -->", ""]
    return "\n".join(lines)


def export(client, board_dir, *, run=subprocess.run, err=None) -> str:
    """-> committed | clean | error (the reason on ``err``)."""
    err = err or sys.stderr
    try:
        cards: Dict[str, str] = {}
        for compact in cabinet_client.records(client):
            record = client.call({"op": "card.get", "card": compact["id"]})["card"]
            messages = client.call({"op": "thread.messages", "card": compact["id"]})[
                "messages"
            ]
            cards[file_name(record)] = render_card(record, messages)
    except (cabinet_client.CabinetError, KeyError, TypeError, ValueError) as exc:
        print(f"kd-cabinet export: кабинет недоступен ({exc})", file=err)
        return "error"
    try:
        _write_dir(os.path.join(str(board_dir), "backlog", "tasks"), cards, _CARD_FILE)
    except (OSError, ValueError) as exc:
        print(f"kd-cabinet export: файлы снимка не записаны ({exc})", file=err)
        return "error"
    return git_commit(board_dir, run=run, err=err)


def git_commit(board_dir, run=subprocess.run, err=None) -> str:
    """-> committed | clean | error. Git's exit codes are checked: a silent stop of the history is not allowed."""
    err = err or sys.stderr
    # the identity travels with the command: the board checkout need not carry a git config
    git = [
        "git",
        "-C",
        str(board_dir),
        "-c",
        "user.name=kd-cabinet",
        "-c",
        "user.email=digipaddock-cabinet@localhost",
    ]
    # a nested .git would turn backlog/ into a gitlink and hide its history
    if os.path.lexists(os.path.join(str(board_dir), "backlog", ".git")):
        print(
            "kd-cabinet export: внутри backlog/ появился .git — история не ведётся, разобраться вручную",
            file=err,
        )
        return "error"

    def call(*args):
        return run(
            [*git, *args],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    try:
        # -f: a .gitignore inside backlog/ must not hide files from the history
        add = call("add", "-A", "-f", "--", "backlog")
        if add.returncode != 0:
            print(
                f"kd-cabinet export: git add вернул {add.returncode}: {(add.stderr or '').strip()[:200]}",
                file=err,
            )
            return "error"
        # git add stores a nested .git at any depth as a gitlink (160000): its content would leave the history
        index = call("ls-files", "-s", "--", "backlog")
        if index.returncode != 0:
            print(
                f"kd-cabinet export: git ls-files вернул {index.returncode}: {(index.stderr or '').strip()[:200]}",
                file=err,
            )
            return "error"
        for entry in (index.stdout or "").splitlines():
            if entry.startswith("160000 "):
                print(
                    f"kd-cabinet export: внутри backlog/ вложенный git ({entry.partition(chr(9))[2][:200]!r})",
                    file=err,
                )
                return "error"
        if call("diff", "--cached", "--quiet").returncode == 0:
            return "clean"
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        commit = call("commit", "-q", "-m", f"board: {stamp}")
        if commit.returncode != 0:
            print(
                f"kd-cabinet export: git commit вернул {commit.returncode}: {(commit.stderr or '').strip()[:200]}",
                file=err,
            )
            return "error"
        return "committed"
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"kd-cabinet export: git недоступен ({exc.__class__.__name__})", file=err)
        return "error"


# --- helpers ------------------------------------------------------------------------------------------


def _write_dir(directory: str, files: Mapping[str, str], ours: re.Pattern) -> None:
    """``files`` into ``directory`` (created if missing); files matching ``ours`` that are not in ``files`` go."""
    parent = os.path.dirname(directory)
    for path in (parent, directory):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            os.mkdir(path, 0o755)
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError(f"{path} — не каталог (ссылка?)")
    for name, text in files.items():
        path = os.path.join(directory, name)
        data = text.encode("utf-8")
        try:
            same = atomic_file.read(path, MAX_FILE) == data
        except OSError:  # a link or a special file at the name: replaced below
            same = False
        if not same:
            atomic_file.replace(path, data, MODE)
    for name in os.listdir(directory):
        if name not in files and ours.fullmatch(name):
            os.unlink(os.path.join(directory, name))


def _list(key: str, values: List[str]) -> List[str]:
    return (
        [f"{key}:"] + [f"  - {_quote(v)}" for v in values] if values else [f"{key}: []"]
    )


def _quote(value: str) -> str:
    return "'" + _one_line(value).replace("'", "''") + "'"


def _one_line(value: str) -> str:
    return " ".join(str(value).split("\n")).strip()


def _text(body: str) -> str:
    return "\n".join(
        line.rstrip() for line in body.replace("\r\n", "\n").split("\n")
    ).strip("\n")


def _md_time(stamp: str) -> str:
    """The cabinet's ``YYYY-MM-DDTHH:MM:SSZ`` as Backlog.md's ``YYYY-MM-DD HH:MM`` (UTC)."""
    try:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(stamp)


def _board_name(actor: str) -> str:
    """``agent:dev`` -> ``@dev``, ``user:owner`` -> ``@owner``; anything else keeps a readable name."""
    kind, _, name = actor.partition(":")
    if kind in ("agent", "user") and name:
        return "@" + name
    name = re.sub(r"[^a-z0-9_-]+", "-", actor.lower()).strip("-")[:32]
    return "@" + name if re.fullmatch(r"[a-z][a-z0-9_-]*", name) else "@unknown"
