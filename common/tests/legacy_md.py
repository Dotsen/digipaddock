"""Backlog.md task files of the old board: the reader of the cut-over import, kept for the tests —
the nightly export must read back to the same record, and the admin samples are md text.

The strict header reader of the admin cards moved here from ``kd.admin_task``; the old
sync's lenient reader (``board.frontmatter``) is the fallback for a header the strict one refuses,
with the warning "the header was read leniently". The reader keeps every word of a card and says where
it put it: an unknown section goes into the description under its name, text before the first
section opens the description, a second assignee is dropped and named, an unreadable dependency is
dropped. It refuses what it cannot turn into one card without guessing: bytes that are not UTF-8, no
header or an unclosed one, a repeated header key or section, no valid id, an unknown status, no project,
an unreadable assignee or server label, a list where one value belongs, no creation date, a comment block it
cannot split.

Errors and warnings are Russian: they are the lines of the owner's import report.

Times: Backlog.md writes ``YYYY-MM-DD HH:MM`` without a zone; ``utc_offset_minutes`` is the offset
of that wall clock from UTC (0 when it is UTC).
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from kd.cabinet import schema

CARD_ID = re.compile(r"([A-Z][A-Z0-9]{0,15})-([1-9][0-9]{0,5})")
AGENT = re.compile(r"@[a-z][a-z0-9_-]{0,31}")
ALIAS = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")
SERVER_LABEL = "server:"
CREATED_BY = "@owner"
COLUMNS = {
    "Description": "description",
    "Acceptance Criteria": "acceptance",
    "Definition of Done": "dod",
    "Implementation Plan": "plan",
    "Implementation Notes": "notes",
    "Final Summary": "summary",
    "Comments": None,
}
STAMP = "%Y-%m-%dT%H:%M:%SZ"

_KEY = re.compile(r"([A-Za-z_][A-Za-z0-9_]*):(?:[ \t]+(.*))?")
_ITEM = re.compile(r"[ \t]+-[ \t]+(.*)")
_BLOCK = (">", ">-", ">+", "|", "|-", "|+")
_HEADER = re.compile(r"## (\S(?:.*\S)?)[ \t]*")
_MARKER = re.compile(r"[ \t]*<!--[ \t]*[A-Z_]+(?::[A-Z_]+)*:(BEGIN|END)[ \t]*-->[ \t]*")
_CRITERION = re.compile(r"- \[([ xX])\] (?:#[0-9]+ )?(.*)")
_AUTHOR = re.compile(r"author:[ \t]*(\S+)[ \t]*")
_PLAIN_AUTHOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_WHEN = re.compile(r"created:[ \t]*(\S.*?)[ \t]*")
_BEGIN, _END = "<!-- COMMENTS:BEGIN -->", "<!-- COMMENTS:END -->"


class CardError(ValueError):
    """The file cannot become one cabinet card; the text says why, for the owner's report."""


class _Strict(ValueError):
    """The strict header reader refused; the lenient one tries next."""


def parse_card(
    data: bytes, *, utc_offset_minutes: int = 0
) -> Tuple[dict, List[dict], List[str]]:
    """(record for ``Store.import_card``, comments as ``[{author, created, body}]``, warnings)."""
    warnings: List[str] = []
    meta, body = _header(data, warnings)
    card_id = meta.get("id")
    match = CARD_ID.fullmatch(card_id) if isinstance(card_id, str) else None
    if not match:
        raise CardError("в шапке нет id карточки")
    status = _one(meta, "status")
    if status not in schema.STATUSES:
        raise CardError(f"неизвестный статус «{status}»")
    project = _one(meta, "project")
    if not project:  # no default project to guess (the board's projects are the cabinet's table)
        raise CardError("проект не указан")
    assignees = [
        a if a.startswith("@") else "@" + a
        for a in (x.strip() for x in _many(meta, "assignee"))
        if a
    ]
    for name in assignees:
        if not AGENT.fullmatch(name):
            raise CardError(f"исполнитель «{name}» не читается")
    if len(assignees) > 1:
        warnings.append(
            f"исполнителей несколько — оставлен {assignees[0]}, снят {', '.join(assignees[1:])}"
        )
    labels, servers = _labels(_many(meta, "labels"))
    dependencies = []
    for raw in _many(meta, "dependencies"):
        dep = raw.strip().upper()
        if CARD_ID.fullmatch(dep):
            if dep not in dependencies:
                dependencies.append(dep)
        elif raw.strip():
            warnings.append(f"зависимость «{raw.strip()}» не читается — снята")
    parent = _one(meta, "parent_task_id").upper()
    if parent and not CARD_ID.fullmatch(parent):
        warnings.append(f"родитель «{parent}» не читается — снят")
        parent = ""
    created_raw, updated_raw = _one(meta, "created_date"), _one(meta, "updated_date")
    if not created_raw:
        raise CardError("дата создания не указана")
    created = _stamp(created_raw, utc_offset_minutes)
    if created is None:
        raise CardError(f"дата создания «{created_raw}» не читается")
    updated = _stamp(updated_raw, utc_offset_minutes) if updated_raw else created
    if updated is None:
        warnings.append(
            f"дата изменения «{updated_raw}» не читается — взята дата создания"
        )
        updated = created
    columns, messages = _columns(body, warnings, utc_offset_minutes)
    extra = _extra_keys(meta, warnings)
    if extra:
        columns["description"] = "\n\n".join(
            p for p in (columns["description"], extra) if p
        )
    record = {
        "id": card_id,
        "n": int(match.group(2)),
        "project": project,
        "title": _one(meta, "title"),
        "status": status,
        "assignee": assignees[0] if assignees else "",
        "labels": labels,
        "servers": servers,
        "priority": _one(meta, "priority"),
        "due": _one(meta, "due_date"),
        "dependencies": dependencies,
        "parent": parent,
        **columns,
        "created": created,
        "updated": updated,
        "created_by": CREATED_BY,
        "revision": 1,
    }
    return record, messages, warnings


# --- the header ---------------------------------------------------------------------------------------


def _header(data: bytes, warnings: List[str]) -> Tuple[dict, List[str]]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CardError("файл не в UTF-8") from exc
    lines = text.replace("\r\n", "\n").split("\n")
    try:
        return _frontmatter(lines)
    except _Strict:
        lenient = _lenient(lines)
        if lenient is None:
            raise CardError(
                "шапка не читается: нет её, она не закрыта или ключ повторяется"
            ) from None
        warnings.append("шапка прочитана нестрого")
        return lenient


def _frontmatter(lines: List[str]) -> Tuple[dict, List[str]]:
    """The YAML header as {key: str | [str]}; anything outside the forms Backlog.md writes is refused."""
    if not lines or lines[0] != "---":
        raise _Strict("no frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise _Strict("frontmatter not closed") from exc
    meta: dict = {}
    i = 1
    while i < end:
        line = lines[i]
        i += 1
        if not line.strip():
            continue
        match = _KEY.fullmatch(line)
        if not match:
            raise _Strict(f"unexpected line {line[:60]!r}")
        key, rest = match.group(1), (match.group(2) or "").strip()
        if key in meta:
            raise _Strict(f"duplicate key {key!r}")
        if not rest:
            items = []
            while i < end and _ITEM.fullmatch(lines[i]):
                items.append(_scalar(_ITEM.fullmatch(lines[i]).group(1)))
                i += 1
            meta[key] = items
        elif rest in _BLOCK:
            body = []
            while i < end and (not lines[i].strip() or lines[i][:1] in " \t"):
                body.append(lines[i].strip())
                i += 1
            meta[key] = (
                " ".join(s for s in body if s)
                if rest[0] == ">"
                else "\n".join(body).rstrip("\n")
            )
        elif rest.startswith("["):
            meta[key] = _flow(rest)
        else:
            meta[key] = _scalar(rest)
    return meta, lines[end + 1 :]


def _flow(text: str) -> List[str]:
    if not text.endswith("]"):
        raise _Strict(f"unclosed list {text[:40]!r}")
    inner = text[1:-1].strip()
    if not inner:
        return []
    items = re.findall(
        r"\s*('(?:[^']|'')*'|\"(?:[^\"\\]|\\.)*\"|[^,'\"]+)\s*(?:,|$)", inner
    )
    if ",".join(items).replace(" ", "") != inner.replace(" ", ""):
        raise _Strict(f"unreadable list {text[:40]!r}")
    return [_scalar(item.strip()) for item in items]


def _scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == "'":
        inner = value[1:-1]
        if re.search(r"(?<!')'(?!')", inner.replace("''", "")):
            raise _Strict(f"unreadable quoted value {value[:40]!r}")
        return inner.replace("''", "'")
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            decoded = json.loads(value)
        except ValueError as exc:
            raise _Strict(f"unreadable quoted value {value[:40]!r}") from exc
        if not isinstance(decoded, str):
            raise _Strict(f"unreadable quoted value {value[:40]!r}")
        return decoded
    if value[:1] in ("'", '"', "[", "{", "&", "*", "!", "|", ">"):
        raise _Strict(f"unreadable value {value[:40]!r}")
    return value


def _lenient(lines: List[str]) -> Optional[Tuple[dict, List[str]]]:
    """The old sync's reader (``board.frontmatter``): None for no header, an unclosed one or a repeated key."""
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    data: dict = {}
    key, i = None, 1
    while i < end:
        line = lines[i]
        i += 1
        item = re.match(r"^\s+-\s+(.*)$", line)
        if item:
            if key is None:
                continue
            if not isinstance(data.setdefault(key, []), list):
                return None
            data[key].append(_unquote(item.group(1)))
            continue
        match = re.match(r"^([A-Za-z_]+):(.*)$", line)
        if not match:
            key = None
            continue
        key, value = match.group(1), match.group(2).strip()
        if key in data:
            return None
        if value in _BLOCK:
            body = []
            while i < end and (not lines[i].strip() or lines[i][:1] in " \t"):
                body.append(lines[i].strip())
                i += 1
            data[key] = (
                " ".join(s for s in body if s)
                if value[0] == ">"
                else "\n".join(body).rstrip("\n")
            )
        elif value == "[]":
            data[key] = []
        elif value.startswith("[") and value.endswith("]"):
            data[key] = [_unquote(v) for v in value[1:-1].split(",") if v.strip()]
        elif value:
            data[key] = _unquote(value)
        else:
            data[key] = []
    return data, lines[end + 1 :]


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


_KNOWN_KEYS = frozenset(
    {
        "id",
        "title",
        "status",
        "project",
        "assignee",
        "labels",
        "dependencies",
        "parent_task_id",
        "created_date",
        "updated_date",
        "milestone",  # an old board's file may name one: read and dropped
        "priority",
        "due_date",
        "ordinal",  # Backlog.md's column order: the cabinet orders by id
        "type",  # every card was a task
    }
)


def _extra_keys(meta: dict, warnings: List[str]) -> str:
    """Header keys the cabinet has no column for (``references``, ``tests``, ``commit`` …) stay
    readable: a bold name and a list at the end of the description, like an unknown section."""
    parts = []
    for key in meta:
        if key in _KNOWN_KEYS:
            continue
        values = [v.strip() for v in _many(meta, key) if v and v.strip()]
        if not values:
            continue
        parts.append(f"**{key}**\n" + "\n".join(f"- {v}" for v in values))
        warnings.append(f"ключ «{key}» перенесён в описание")
    return "\n\n".join(parts)


def _one(meta: dict, key: str) -> str:
    value = meta.get(key, "")
    if isinstance(value, list):
        if value:
            raise CardError(f"поле {key} — список, а не одно значение")
        return ""
    return value.strip()


def _many(meta: dict, key: str) -> List[str]:
    value = meta.get(key, [])
    if isinstance(value, str):
        return [value] if value else []
    return list(value)


def _labels(raw: List[str]) -> Tuple[List[str], List[str]]:
    labels: List[str] = []
    servers = set()
    for label in (x.strip() for x in raw):
        if label.startswith(SERVER_LABEL):
            alias = label[len(SERVER_LABEL) :]
            if not ALIAS.fullmatch(alias):
                raise CardError(f"метка сервера «{label}» не читается")
            servers.add(alias)
        elif label and label not in labels:
            labels.append(label)
    return labels, sorted(servers)


def _stamp(value: str, offset_minutes: int) -> Optional[str]:
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            moment = datetime.strptime(value.strip(), fmt)
        except ValueError:
            continue
        return (moment - timedelta(minutes=offset_minutes)).strftime(STAMP)
    return None


# --- the sections -------------------------------------------------------------------------------------


def _columns(
    body: List[str], warnings: List[str], offset: int
) -> Tuple[Dict[str, object], List[dict]]:
    known: Dict[str, List[str]] = {}
    unknown: List[Tuple[str, List[str]]] = []
    preface: List[str] = []
    current = preface
    # between a BEGIN and its END marker a "## " line is text, not a section — when the markers pair up
    marks = [m.group(1) for m in map(_MARKER.fullmatch, body) if m]
    paired = (
        marks[0::2] == ["BEGIN"] * len(marks[0::2])
        and marks[1::2] == ["END"] * len(marks[1::2])
        and len(marks) % 2 == 0
    )
    if not paired:
        warnings.append("маркеры секций не парные — заголовки прочитаны как есть")
    inside = False
    for line in body:
        marker = _MARKER.fullmatch(line)
        if marker and paired:
            inside = marker.group(1) == "BEGIN"
        match = None if inside or marker else _HEADER.fullmatch(line)
        if match:
            name = match.group(1)
            if name in COLUMNS:
                if name in known:
                    raise CardError(f"секция «{name}» встречается дважды")
                current = known[name] = []
            else:
                current = []
                unknown.append((name, current))
            continue
        current.append(line)
    description = _clean(known.get("Description", []))
    if _clean(preface):
        warnings.append("текст до первой секции перенесён в описание")
        description = "\n\n".join(p for p in (_clean(preface), description) if p)
    acceptance, stray = [], []
    for line in _clean(known.get("Acceptance Criteria", [])).split("\n"):
        criterion = _CRITERION.fullmatch(line.strip())
        if criterion:
            acceptance.append(
                {
                    "text": criterion.group(2).strip(),
                    "checked": criterion.group(1) != " ",
                }
            )
        elif line.strip():
            stray.append(line)
    if stray:
        warnings.append("строки критериев без флажка перенесены в описание")
        description = "\n\n".join(
            p for p in (description, "**Acceptance Criteria**", "\n".join(stray)) if p
        )
    for name, lines in unknown:
        warnings.append(f"секция «{name}» перенесена в описание")
        description = "\n\n".join(
            p for p in (description, f"**{name}**", _clean(lines)) if p
        )
    columns = {"description": description, "acceptance": acceptance}
    for name, column in COLUMNS.items():
        if column and column not in columns:
            columns[column] = _clean(known.get(name, []))
    return columns, _comments(known.get("Comments", []), offset, warnings)


def _clean(lines: List[str]) -> str:
    return "\n".join(
        line.rstrip() for line in lines if not _MARKER.fullmatch(line)
    ).strip("\n")


def _comments(lines: List[str], offset: int, warnings: List[str]) -> List[dict]:
    """The blocks ``author: @x`` / ``created: …`` / ``---`` / body / ``---`` between the COMMENTS markers.
    A ``---`` inside a body is text: a block ends at the ``---`` before the next author or the end marker.
    An author that is not an agent (the owner's own sessions wrote ``owner-session``) becomes ``@owner``."""
    if not any(line.strip() for line in lines):
        return []
    marks = [line.strip() for line in lines]
    if _BEGIN not in marks or _END not in marks[marks.index(_BEGIN) :]:
        raise CardError("комментарии не читаются: нет блока COMMENTS")
    begin = marks.index(_BEGIN)
    end = marks.index(_END, begin)
    if any(marks[:begin]) or any(marks[end + 1 :]):
        raise CardError("комментарии не читаются: текст вне блока COMMENTS")
    inner = lines[begin + 1 : end]
    out, i = [], 0
    while i < len(inner):
        if not inner[i].strip():
            i += 1
            continue
        author = _AUTHOR.fullmatch(inner[i].strip())
        when = _WHEN.fullmatch(inner[i + 1].strip()) if i + 1 < len(inner) else None
        if (
            not author
            or not when
            or i + 2 >= len(inner)
            or inner[i + 2].strip() != "---"
        ):
            raise CardError(
                "комментарии не читаются: блок не по форме author/created/---"
            )
        name = author.group(1)
        if not AGENT.fullmatch(name):
            if not _PLAIN_AUTHOR.fullmatch(name):
                raise CardError(f"автор комментария «{name}» не читается")
            warnings.append(
                f"автор комментария «{name}» — не агент, записан как владелец"
            )
            name = "@owner"
        created = _stamp(when.group(1), offset)
        if created is None:
            raise CardError(f"дата комментария «{when.group(1)}» не читается")
        close = None
        for k in range(i + 3, len(inner)):
            if inner[k].strip() == "---":
                after = next((line for line in inner[k + 1 :] if line.strip()), None)
                if after is None or _AUTHOR.fullmatch(after.strip()):
                    close = k
                    break
        if close is None:
            raise CardError("комментарии не читаются: блок не закрыт")
        out.append(
            {
                "author": name,
                "created": created,
                "body": "\n".join(line.rstrip() for line in inner[i + 3 : close]).strip(
                    "\n"
                ),
            }
        )
        i = close + 1
    return out
