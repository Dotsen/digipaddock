"""Plain-text views of cards for terminals: one line per card, and one card with its thread."""

from kd.cabinet import i18n, voice

LANG = "ru"  # the terminal speaks the owner's default language

SECTIONS = (
    ("description", "Description"),
    ("dod", "Definition of Done"),
    ("plan", "Implementation Plan"),
    ("notes", "Implementation Notes"),
    ("summary", "Final Summary"),
)


def who(author: str) -> str:
    """``agent:dev`` -> ``@dev``, ``user:owner`` -> ``@owner``; anything else as it is."""
    for prefix in ("agent:", "user:"):
        if author.startswith(prefix):
            return "@" + author[len(prefix) :]
    return author


def labels_of(card: dict) -> list:
    return list(card.get("labels") or []) + [
        f"server:{s}" for s in card.get("servers") or []
    ]


def line(card: dict) -> str:
    """``KD-n · STATUS · @who · title [labels]``."""
    text = f"{card['id']} · {card['status']} · {card.get('assignee') or '-'} · {card['title']}"
    labels = labels_of(card)
    return text + (f" [{', '.join(labels)}]" if labels else "")


def size_text(size) -> str:
    """Bytes as people read them: 512 B, 3 KB, 1.2 MB."""
    size = int(size or 0)
    if size < 1024:
        return f"{size} B"
    if size < 1 << 20:
        return f"{round(size / 1024)} KB"
    return f"{size / (1 << 20):.1f} MB"


def files_of(message: dict) -> list:
    """A message's files: ``payload.files``, or the one file of an older ``file`` message."""
    payload = message.get("payload") or {}
    if isinstance(payload.get("files"), list):
        return [f for f in payload["files"] if isinstance(f, dict) and isinstance(f.get("id"), str)]
    if message.get("kind") == "file" and isinstance(payload.get("id"), str):
        return [payload]
    return []


def file_line(file: dict) -> str:
    """``file <id> · <name> · <type> · <size>`` — the id is what ``kd-task file get`` takes; an audio a person sent
    has its transcription on the next line (after ``voice.PREFIX``)."""
    if file.get("deleted"):
        return f"file {file['id']} · {file.get('name', '')} · deleted"
    line = f"file {file['id']} · {file.get('name', '')} · {file.get('mime', '')} · {size_text(file.get('size'))}"
    spoken = voice.file_text(file)
    return f"{line}\n{spoken}" if spoken else line


def message_text(message: dict, lang: str = LANG, *, with_files: bool = True) -> str:
    """A system message's catalog text (key + parameters), then its own body; a plain one as it is.
    A message's files follow, one line each (a ``file`` message's body only names them)."""
    system = i18n.render(lang, message.get("payload") or {})
    body = message.get("body") or ""
    files = files_of(message)
    if files and message.get("kind") == "file":
        body = ""
    if system is not None:
        body = f"{system}\n{body}" if body else system
    lines = [file_line(f) for f in files] if with_files else []
    return "\n".join(([body] if body else []) + lines)


def continued(text: str) -> str:
    """A message body as an agent's thread listing prints it: every line after the first indented, at every line
    break Python knows (\\r, \\u2028 …), so a line that starts a listing — ``#<seq> @<who> …`` — is always a message's
    own header, never a body's line that mimics one (review 04.11.2026: an agent's text faked the owner's word)."""
    return "\n  ".join(text.splitlines()) if text else text


def shown(messages: list) -> list:
    """The thread without the session's tool results: the tool call's line stands for both."""
    return [m for m in messages if m.get("kind") != "tool_result"]


def thread_text(message: dict, lang: str = LANG) -> str:
    """A thread line: a session's tool call as ``· <tool>: <what it was asked>``, anything else in words."""
    if message.get("kind") == "tool_call":
        return "· " + (message.get("body") or "")
    return message_text(message, lang)


def scope_text(card: dict) -> str:
    """What the card asks for: its description, acceptance criteria and Definition of Done, as in ``card_text``."""
    parts = _scope_parts(card)
    if card.get("dod"):
        parts.append(f"## {SECTIONS[1][1]}\n\n{card['dod']}")
    return "\n\n".join(parts) + "\n" if parts else ""


def _scope_parts(card: dict) -> list:
    parts = []
    if card.get("description"):
        parts.append("## Description\n\n" + card["description"])
    if card.get("acceptance"):
        parts.append(
            "## Acceptance Criteria\n\n"
            + "\n".join(
                f"- [{'x' if a['checked'] else ' '}] #{i} {a['text']}"
                for i, a in enumerate(card["acceptance"], 1)
            )
        )
    return parts


def card_text(card: dict, messages: list) -> str:
    """The card's head, its sections and its thread."""
    head = [line(card)]
    facts = [
        f"project {card['project']}",
        f"revision {card['revision']}",
        f"updated {card['updated']}",
    ]
    for key, name in (
        ("priority", "priority"),
        ("due", "due"),
        ("parent", "parent"),
    ):
        if card.get(key):
            facts.append(f"{name} {card[key]}")
    if card.get("dependencies"):
        facts.append("depends on " + ", ".join(card["dependencies"]))
    head.append(" · ".join(facts))
    parts = ["\n".join(head)] + _scope_parts(card)
    for key, name in SECTIONS[1:]:
        if card.get(key):
            parts.append(f"## {name}\n\n{card[key]}")
    if shown(messages):
        parts.append(
            "## Comments\n\n"
            + "\n\n".join(
                f"{who(m['author'])} · {m['created']}\n{thread_text(m)}"
                for m in shown(messages)
            )
        )
    return "\n\n".join(parts) + "\n"
