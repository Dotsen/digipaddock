"""A session's own working data: its notes and temp files, apart from every other thread of the agent.

All sessions of an agent run as its one Unix user in one folder, so this is the layout of the working data and the
control of its regular paths, not OS isolation between processes of the same UID:

- a card's session: ``memory/tasks/<card>.md`` and ``work/tmp/cards/<card>/``;
- the agent's chat and "Monitor" thread: ``memory/chats/<thread>.md`` and ``work/tmp/chats/<thread>/``;
- a worker job's temp files stay in its slot's ``work/tmp``, made fresh for every job (``worker_runner``); its patch
  comes to the card's folder, ``work/tmp/cards/<card>/worker-<job>.patch``.

The paths come from the session's context as the cabinet routed it (``agent-launch --context``: the card ID or the
thread number), never from a Claude runtime UUID, a PID or a slot, so a resumed thread keeps its files.

``memory/notes.md`` stays the agent's shared, durable notes. The model never edits it in place: ``add_shared``
(``kd-notes add``) appends one entry under an exclusive lock, so two sessions never lose each other's entry.
``migrate`` splits the notes of before once: the original goes to ``memory/legacy/``, an entry about exactly one
card moves to that card's notes, everything else stays, and a map says what went where.
"""

import errno
import fcntl
import json
import os
import re
import stat
import sys
import time
from typing import Dict, List, NamedTuple, Optional, Tuple

if __package__ in (None, ""):  # run as a script: python3 -I …/common/kd/workspace.py
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from kd import ids  # noqa: E402

# The shapes agent-launch checks the session context against (any card prefix; a thread number)
CARD = re.compile(r"[A-Z][A-Z0-9]{0,7}-[1-9][0-9]{0,5}")
THREAD = re.compile(r"[1-9][0-9]{0,17}")
SHARED = "memory/notes.md"
TASKS, CHATS, LEGACY = "memory/tasks", "memory/chats", "memory/legacy"
TMP = "work/tmp"
TMP_CARDS, TMP_CHATS = TMP + "/cards", TMP + "/chats"
# Created by the installer, owned by the agent; a session's own folders below them by agent-launch
DIRECTORIES = (TASKS, CHATS, LEGACY, TMP, TMP_CARDS, TMP_CHATS)
DIR_MODE, FILE_MODE = 0o750, 0o640
ENTRY_MAX = 2000
MAP_JSON, MAP_TEXT = LEGACY + "/migration.json", LEGACY + "/migration.md"
PENDING = LEGACY + "/migration.pending"
STAMP = re.compile(r"\d{8}T\d{6}Z")
DATED = re.compile(r"\d{4}-\d{2}-\d{2}\b")


class WorkspaceError(Exception):
    """A path that is not the agent's own regular one, or a text the shared notes do not take."""


class Paths(NamedTuple):
    notes: str  # the session's own notes file
    tmp: str  # the session's own temp folder


def session_paths(folder: str, card: str = "", thread: str = "") -> Paths:
    """The session's notes and temp folder: a card's by its ID, the chat's (or the "Monitor" thread's) by its thread number."""
    if card:
        if not CARD.fullmatch(card):
            raise WorkspaceError(f"bad card: {card!r}")
        return Paths(f"{folder}/{TASKS}/{card}.md", f"{folder}/{TMP_CARDS}/{card}")
    if not THREAD.fullmatch(str(thread)):
        raise WorkspaceError(f"bad thread: {thread!r}")
    return Paths(f"{folder}/{CHATS}/{thread}.md", f"{folder}/{TMP_CHATS}/{thread}")


def card_tmp(folder: str, card: str) -> str:
    """A card's temp folder (a worker's patch for it goes there)."""
    return session_paths(folder, card=card).tmp


def _mkdir(path: str) -> None:
    """One folder of the agent's own, never through a link."""
    try:
        os.mkdir(path, DIR_MODE)
    except FileExistsError:
        pass
    if os.path.islink(path) or not os.path.isdir(path):
        raise WorkspaceError(f"not a plain folder: {path}")


def prepare_base(folder: str) -> None:
    """The shared folders under the agent's folder (the installer, as the agent's user)."""
    for parent in ("memory", "work"):
        if os.path.islink(f"{folder}/{parent}") or not os.path.isdir(
            f"{folder}/{parent}"
        ):
            raise WorkspaceError(f"not a plain folder: {folder}/{parent}")
    for sub in DIRECTORIES:
        _mkdir(f"{folder}/{sub}")


def prepare(folder: str, card: str = "", thread: str = "") -> Paths:
    """The session's notes file and temp folder, created when missing; the same ones on every resume."""
    paths = session_paths(folder, card, thread)
    prepare_base(folder)
    _mkdir(paths.tmp)
    try:
        fd = os.open(
            paths.notes, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE
        )
        os.close(fd)
    except FileExistsError:
        pass
    if os.path.islink(paths.notes) or not os.path.isfile(paths.notes):
        raise WorkspaceError(f"not a plain file: {paths.notes}")
    return paths


def _entry(text: str, today: str) -> str:
    """One line of the shared notes: the text on one line, dated unless it starts with a date."""
    if not isinstance(text, str) or "\x00" in text:
        raise WorkspaceError("the entry must be text")
    line = " ".join(text.split())
    if line.startswith("- "):
        line = line[2:]
    if not line:
        raise WorkspaceError("empty entry")
    if len(line) > ENTRY_MAX:
        raise WorkspaceError(
            f"entry over {ENTRY_MAX} characters: keep shared notes short, the rest goes to your own notes"
        )
    return f"- {line}\n" if DATED.match(line) else f"- {today}: {line}\n"


def _open_shared(folder: str, flags: int) -> int:
    try:
        return os.open(f"{folder}/{SHARED}", flags | os.O_NOFOLLOW, FILE_MODE)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise WorkspaceError(f"{SHARED} is a link") from None
        raise


def add_shared(folder: str, text: str, today: Optional[str] = None) -> str:
    """Append one entry to the shared notes under an exclusive lock: concurrent entries all stay, none is
    rewritten. Returns the line written."""
    line = _entry(text, today or time.strftime("%Y-%m-%d", time.gmtime()))
    fd = _open_shared(folder, os.O_RDWR | os.O_APPEND | os.O_CREAT)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise WorkspaceError(f"{SHARED} is not a plain file")
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = "\n" + line
        os.write(fd, line.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)  # the lock goes with the descriptor
    return line.lstrip("\n")


# --- the one-time split of the notes of before -------------------------------------------------------------------


class Entry(NamedTuple):
    first: int  # its first line, from 1
    text: str  # its lines, each ending with a newline


def entries(text: str) -> Tuple[str, List[Entry]]:
    """(the head before the first entry, the entries): an entry is a top-level ``- `` line with the blank and
    indented lines under it; any other top-level line (a heading, a paragraph) is an item of its own, never part of
    the entry above it."""
    head: List[str] = []
    found: List[Tuple[int, List[str]]] = []
    for number, line in enumerate(text.splitlines(keepends=True), 1):
        if not line.endswith("\n"):
            line += "\n"
        if line.startswith("- "):
            found.append((number, [line]))
        elif found and (not line.strip() or line[0] in " \t"):
            found[-1][1].append(line)
        elif found:
            found.append((number, [line]))
        else:
            head.append(line)
    return "".join(head), [Entry(first, "".join(lines)) for first, lines in found]


def classify(entry: Entry) -> Tuple[str, Optional[str], str]:
    """(``move`` | ``keep``, the card it moves to, why): an entry about exactly one card of this host (its current or
    an earlier prefix) moves; a pitfall is shared knowledge and stays; none or several cards — ambiguous, stays; a
    heading or paragraph between entries stays where it is."""
    if not entry.text.startswith("- "):
        return "keep", None, "not an entry"
    body = entry.text[2:].lstrip()
    if re.match(r"(?:\d{4}-\d{2}-\d{2}\s*[:—-]?\s*)?pitfall\b", body, re.I):
        return "keep", None, "pitfall"
    cards = sorted(
        {ids.canonical(match.group(0)) for match in ids.ANY.finditer(entry.text)}
    )
    if len(cards) == 1:
        return "move", cards[0], "one card"
    return "keep", None, "no card" if not cards else f"{len(cards)} cards"


def _write_new(path: str, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, FILE_MODE)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def _append_once(path: str, block: str) -> bool:
    """Append ``block`` unless the file already holds it (a migration cut short and run again)."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, FILE_MODE)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        current = os.pread(fd, size, 0).decode("utf-8", "replace") if size else ""
        if block in current:
            return False
        prefix = "\n" if current and not current.endswith("\n") else ""
        os.write(fd, (prefix + block).encode("utf-8"))
        os.fsync(fd)
        return True
    finally:
        os.close(fd)


def _write_replace(path: str, data: bytes) -> None:
    """A whole file written beside and renamed into place (a map written again by a resumed migration)."""
    temp = f"{path}.new"
    if os.path.lexists(temp):
        os.unlink(temp)
    _write_new(temp, data)
    os.rename(temp, path)


def _read(path: str) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        size = os.fstat(fd).st_size
        return os.pread(fd, size, 0) if size else b""
    finally:
        os.close(fd)


def migrate(
    folder: str, stamp: Optional[str] = None, card: str = "KD-9046"
) -> Dict[str, object]:
    """Split the shared notes once (the installer, as the agent's user, before any session). The original is kept
    byte for byte in ``memory/legacy/notes-<stamp>.md``; a done migration (its map) is never repeated, and one cut
    short is finished from its own snapshot (``migration.pending`` names it): nothing moves twice, no second
    snapshot is taken of the already split file, and an entry appended meanwhile stays."""
    prepare_base(folder)
    if os.path.lexists(f"{folder}/{MAP_JSON}"):
        with open(f"{folder}/{MAP_JSON}", encoding="utf-8") as source:
            return {**json.load(source), "state": "already"}
    pending = f"{folder}/{PENDING}"
    if os.path.lexists(pending):
        stamp = json.loads(_read(pending).decode("utf-8"))["stamp"]
        if not STAMP.fullmatch(stamp):
            raise WorkspaceError(f"bad stamp in {PENDING}: {stamp!r}")
    else:
        stamp = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        _write_new(pending, (json.dumps({"stamp": stamp}) + "\n").encode("utf-8"))
    fd = _open_shared(folder, os.O_RDWR | os.O_CREAT)
    try:
        # kd-notes add waits; nothing writes the file in place meanwhile
        fcntl.flock(fd, fcntl.LOCK_EX)
        size = os.fstat(fd).st_size
        current = os.pread(fd, size, 0) if size else b""
        snapshot = f"{LEGACY}/notes-{stamp}.md"
        if os.path.lexists(f"{folder}/{snapshot}"):
            original = _read(
                f"{folder}/{snapshot}"
            )  # resumed: the split works from what it kept
        else:
            original = current
            if original:
                _write_new(f"{folder}/{snapshot}", original)
        report: Dict[str, object] = {
            "schema_version": 1,
            "state": "done",
            "stamp": stamp,
            "card": card,
            "original": snapshot if original else None,
            "bytes": len(original),
            "moved": [],
            "kept": [],
        }
        if original:
            head, found = entries(original.decode("utf-8", "replace"))
            kept_text = [head]
            for entry in found:
                action, target, why = classify(entry)
                if action == "move":
                    path = session_paths(folder, card=target).notes
                    _append_once(path, entry.text)
                    report["moved"].append(
                        {
                            "line": entry.first,
                            "card": target,
                            "to": f"{TASKS}/{target}.md",
                        }
                    )
                else:
                    kept_text.append(entry.text)
                    report["kept"].append({"line": entry.first, "why": why})
            if report["moved"]:
                today = stamp[:4] + "-" + stamp[4:6] + "-" + stamp[6:8]
                # no card ID in it: the line itself is shared, never an entry to move
                kept_text.append(
                    f"- {today}: the shared notes were split once: entries about one card moved to "
                    f"{TASKS}/<card>.md; the original is {snapshot}, the map {MAP_TEXT}.\n"
                )
                split = "".join(kept_text).encode("utf-8")
                if current.startswith(original):
                    # in place, under the lock: a kd-notes add waiting on this file writes after it, never to a
                    # lost inode (a rename would leave it one); what was appended after the snapshot stays
                    data = split + current[len(original) :]
                    os.ftruncate(fd, 0)
                    os.pwrite(fd, data, 0)
                    os.fsync(fd)
                elif not current.startswith(split):
                    report["notes"] = "changed since the snapshot: left as it is"
        _write_replace(f"{folder}/{MAP_TEXT}", _map_text(report).encode("utf-8"))
        # the map in JSON is the mark of a done migration: written last
        _write_replace(
            f"{folder}/{MAP_JSON}",
            (json.dumps(report, ensure_ascii=False, indent=1) + "\n").encode("utf-8"),
        )
        os.unlink(pending)
    finally:
        os.close(fd)
    return report


def _map_text(report: Dict[str, object]) -> str:
    lines = [
        f"# Notes migration {report['stamp']} ({report['card']})\n",
        "\n",
        f"Original: `{report['original']}` ({report['bytes']} bytes), kept byte for byte.\n"
        if report["original"]
        else "There were no shared notes to split.\n",
        "\n",
    ]
    if report["moved"]:
        lines.append("## Moved (an entry about exactly one card)\n\n")
        lines += [
            f"- line {item['line']} → `{item['to']}`\n" for item in report["moved"]
        ]
        lines.append("\n")
    if report["kept"]:
        lines.append(
            "## Kept in memory/notes.md (pitfalls, entries naming no card or several — not classified)\n\n"
        )
        lines += [f"- line {item['line']}: {item['why']}\n" for item in report["kept"]]
    return "".join(lines)


def main(argv: List[str]) -> int:
    """``workspace.py migrate <folder>`` (the installer) | ``prepare <folder> <card|chat|monitor> <thread>``
    (agent-launch: prints the notes file and the temp folder, one per line)."""
    try:
        if len(argv) == 2 and argv[0] == "migrate":
            report = migrate(argv[1])
            print(
                f"notes migration {report['state']}: {len(report['moved'])} moved, {len(report['kept'])} kept"
            )
            return 0
        if len(argv) == 4 and argv[0] == "prepare":
            card = "" if argv[2] in ("chat", "monitor") else argv[2]
            paths = prepare(argv[1], card=card, thread=argv[3])
            print(paths.notes)
            print(paths.tmp)
            return 0
    except (WorkspaceError, OSError, ValueError) as exc:
        print(f"workspace: {exc}", file=sys.stderr)
        return 1
    print(
        "usage: workspace.py migrate <folder> | prepare <folder> <card|chat|monitor> <thread>",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
