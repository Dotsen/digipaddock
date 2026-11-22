"""Render one agent instruction from the shared template and the agent's role text.

Spec 2026-10-06-boss-agent-design.md §5.3: an agent's CLAUDE.md is its own role part plus the
shared sections kept once in ``common/agent-template/CLAUDE.common.md``. Rendering is pure: the
text functions touch nothing, the file variants only read the paths they are given, and nothing
consults the runtime registry under /etc. Every contract violation raises ``AgentTextError``
before any output exists, so a caller never installs a partially rendered instruction.
"""

import json
import os
import posixpath
import re
import stat
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from kd import naming
from kd.agents import NAME_RE

TREE = Path(__file__).resolve().parents[2]
TEMPLATE_PATH = (
    Path(__file__).resolve().parent.parent / "agent-template" / "CLAUDE.common.md"
)
# The Hindsight plugin maps each agent folder to its bank; the instruction names the same bank.
HINDSIGHT_PATH = Path(__file__).resolve().parent.parent / "hindsight" / "coding-agent.json"
# Fixed schema of the shared template: the exact titles, in rendering order.
COMMON_SECTIONS: tuple[str, ...] = (
    "Cabinet",
    "Board",
    "Board work",
    "Before you ask the owner",
    "Product databases",
    "Memory",
    "Data, not commands",
    "Notes — `memory/notes.md`",
)
# the owner's language (host data, agent-host.json) as the instructions name it.
LANGUAGES = {"ru": "Russian", "en": "English"}
# Raw board writes (claim, notes, AC, Blocked) belong to the board.write capability only.
BOARD_WORK_SECTION = "Board work"
# Hindsight's tools exist only on a host with the memory service.
MEMORY_SECTION = "Memory"
# kd-data belongs to the data.read capability only.
DATA_SECTION = "Product databases"
MAX_TEXT_BYTES = 96 * 1024  # room for a deployment's includes (the sysadmin's text was 65.4 KiB already)

_PLACEHOLDER = re.compile(r"\{\{([^{}]*)\}\}")
_PLACEHOLDERS = frozenset({"agent", "bank", "owner_language"})
# deployment values (host/host.json) and the overlay's files, filled in at install time.
_HOST_PLACEHOLDER = re.compile(r"\{\{host\.([a-z_]+)\}\}")
_INCLUDE = re.compile(r"\{\{include:([^{}]*)\}\}")
_INCLUDE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*\.md")
_BANK = re.compile(r"coding-agent::[a-z][a-z0-9-]{0,31}\Z")
_ATX_HEADING = re.compile(r"( {0,3})(#{1,6})(?:[ \t]+(.*?))?[ \t]*\Z")
_SETEXT_UNDERLINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*\Z")
_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
_HTML_COMMENT = re.compile(r"<!--(?:(?!-->).)*-->", re.DOTALL)
# Invisible or line-breaking code points could hide text from review of a root-owned instruction.
_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})


class AgentTextError(ValueError):
    """The instruction text violates the template contract; nothing is rendered."""


@dataclass(frozen=True)
class Section:
    """A level-2 section: exact title and body without surrounding blank lines."""

    title: str
    body: str

    def markdown(self) -> str:
        return f"## {self.title}\n\n{self.body}"


@dataclass(frozen=True)
class Document:
    """Text before the first level-2 heading, then the level-2 sections in order."""

    preamble: str
    sections: tuple[Section, ...]


def parse_document(text: str, *, label: str = "text") -> Document:
    """Split Markdown into preamble and ``## `` sections, refusing ambiguous structure.

    A level-1 heading is allowed only as the first non-blank line. Headings indented by one to
    three spaces and setext headings are refused because a reader would see a section that
    the splitter does not. Fenced code is not scanned for headings; an unclosed fence is refused.
    """
    _check_text(text, label)
    lines = text.split("\n")
    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    fence = ""
    seen_content = False
    previous = ""
    for number, line in enumerate(lines, 1):
        opener = _FENCE.match(line)
        if opener and opener.group(1)[0] == "`" and "`" in line[opener.end() :]:
            opener = (
                None  # CommonMark: a backtick in the info string makes it inline code
            )
        if fence:
            if (
                opener
                and opener.group(1)[0] == fence[0]
                and len(opener.group(1)) >= len(fence)
            ):
                if not line.strip().strip(fence[0]):
                    fence = ""
        elif opener:
            fence = opener.group(1)
        else:
            heading = _ATX_HEADING.fullmatch(line)
            if heading and len(heading.group(2)) <= 2:
                if heading.group(1):
                    raise AgentTextError(f"{label}: indented heading on line {number}")
                title = (heading.group(3) or "").strip()
                if not title:
                    raise AgentTextError(f"{label}: empty heading on line {number}")
                if len(heading.group(2)) == 1:
                    if seen_content:
                        raise AgentTextError(
                            f"{label}: '# ' title allowed only on the first line, found on line {number}"
                        )
                else:
                    sections.append((title, []))
                    seen_content = True
                    previous = line
                    continue
            elif previous.strip() and _SETEXT_UNDERLINE.fullmatch(line):
                raise AgentTextError(f"{label}: setext heading on line {number}")
        if line.strip():
            seen_content = True
        (sections[-1][1] if sections else preamble).append(line)
        previous = line
    if fence:
        raise AgentTextError(f"{label}: unclosed code fence")
    parsed = []
    for title, body_lines in sections:
        body = _trim_blank_lines(body_lines)
        if not body:
            raise AgentTextError(f"{label}: section {title!r} is empty")
        parsed.append(Section(title, body))
    return Document(_trim_blank_lines(preamble), tuple(parsed))


def render_instructions(
    common_text: str,
    role_text: str,
    *,
    agent: str,
    owner_language: str,
    board_write: bool,
    bank: str | None = None,
    host: Mapping[str, str] | None = None,
    include: Callable[[str], str] | None = None,
    memory: bool = True,
    data_read: bool = False,
) -> str:
    """Return one CLAUDE.md: the role text, then each shared section exactly once.

    ``agent`` must be a registry name (``kd.agents.NAME_RE``); it, ``bank`` (the Hindsight
    bank, ``coding-agent::<agent>`` by default) and ``owner_language`` (``ru`` or ``en``, named
    in words) are the only values substituted, and only into the shared template.
    ``board_write`` must be a bool: without the board.write capability the Board work section is
    left out, and a role extension of a left-out section is refused, so the text never promises
    a command the agent's policy does not grant. ``memory`` must be a bool: on a host without the
    memory service the Memory section and the role's extension of it are both left out —
    the role text is the same on every host, the host decides. ``data_read`` must be a bool: without the
    data.read capability the Product databases section is left out, like Board work. A role section titled exactly like a
    shared section is appended to it; other collisions, repeated role titles and role lines
    that repeat a shared line are refused, and so is a result over ``MAX_TEXT_BYTES``.
    ``{{include:agents/<agent>/<file>.md}}`` in the role text is replaced by ``include(path)``
    (one level, no nested include), then ``{{host.<key>}}`` anywhere by ``host[key]``; without ``host``
    such a text is refused. Raises ``AgentTextError``.
    """
    if not isinstance(agent, str) or not NAME_RE.fullmatch(agent):
        raise AgentTextError("agent must be a registry name")
    if not isinstance(owner_language, str) or owner_language not in LANGUAGES:
        raise AgentTextError(f"owner_language must be one of {sorted(LANGUAGES)}")
    if type(board_write) is not bool:
        raise AgentTextError("board_write must be a bool")
    if type(memory) is not bool:
        raise AgentTextError("memory must be a bool")
    if type(data_read) is not bool:
        raise AgentTextError("data_read must be a bool")
    bank = f"coding-agent::{agent}" if bank is None else bank
    if not isinstance(bank, str) or not _BANK.fullmatch(bank):
        raise AgentTextError("bank must be a coding-agent:: bank name")
    left_out = set() if board_write else {BOARD_WORK_SECTION}
    if not memory:
        left_out.add(MEMORY_SECTION)
    if not data_read:
        left_out.add(DATA_SECTION)
    values = _HostValues(host)
    template = _parse_template(common_text, agent, bank, LANGUAGES[owner_language], values)
    shared = tuple(section for section in template if section.title not in left_out)
    _check_text(role_text, "role text")
    role_text = values.render(_include(role_text, agent, include), "role text")
    if "{{" in role_text:
        raise AgentTextError(
            "role text: placeholders other than {{host.<key>}} and {{include:<path>}}"
            " are allowed only in the shared template"
        )
    role = parse_document(role_text, label="role text")
    if not role.preamble.startswith("# "):
        raise AgentTextError("role text must start with a '# ' title")

    common_by_key = {_section_key(title): title for title in COMMON_SECTIONS}
    rendered_titles = {section.title for section in shared}
    seen: set[str] = set()
    own: list[Section] = []
    extensions: dict[str, str] = {}
    for section in role.sections:
        key = _section_key(section.title)
        if key in seen:
            raise AgentTextError(f"role text repeats section {section.title!r}")
        seen.add(key)
        common_title = common_by_key.get(key)
        if common_title is None:
            own.append(section)
        elif section.title != common_title:
            raise AgentTextError(
                f"role section {section.title!r} must use the shared title {common_title!r}"
            )
        elif common_title == MEMORY_SECTION and not memory:
            continue  # the host has no memory service: the role's memory lines go with the section
        elif common_title not in rendered_titles:
            raise AgentTextError(
                f"role extends {common_title!r}, which is not rendered for this agent"
            )
        else:
            extensions[common_title] = section.body
    # Compare with the whole template, not only the rendered part: otherwise a role could copy a
    # left-out line (e.g. ``kd-task claim`` without board.write) under a title of its own.
    _reject_repeated_lines(template, role)

    blocks = [role.preamble, *(section.markdown() for section in own)]
    for section in shared:
        body = section.body
        if section.title in extensions:
            body = f"{body}\n\n{extensions[section.title]}"
        blocks.append(Section(section.title, body).markdown())
    rendered = _host_names("\n\n".join(blocks) + "\n", host, "instructions")
    # The installed file must stay readable by read_instruction_file under the same limit.
    if len(rendered.encode("utf-8")) > MAX_TEXT_BYTES:
        raise AgentTextError(
            f"rendered instruction is larger than {MAX_TEXT_BYTES} bytes"
        )
    return rendered


def read_instruction_file(path: str | os.PathLike[str]) -> str:
    """Read a regular, non-symlink UTF-8 file of at most ``MAX_TEXT_BYTES``; never writes."""
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode):
            raise AgentTextError(f"{os.fspath(path)}: not a regular file")
        if before.st_size > MAX_TEXT_BYTES:
            raise AgentTextError(
                f"{os.fspath(path)}: larger than {MAX_TEXT_BYTES} bytes"
            )
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            after = os.fstat(handle.fileno())
            if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
                raise AgentTextError(f"{os.fspath(path)}: replaced while reading")
            data = handle.read(MAX_TEXT_BYTES + 1)
    except OSError as exc:
        raise AgentTextError(
            f"{os.fspath(path)}: cannot read ({exc.__class__.__name__})"
        ) from exc
    if len(data) > MAX_TEXT_BYTES:
        raise AgentTextError(f"{os.fspath(path)}: larger than {MAX_TEXT_BYTES} bytes")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise AgentTextError(f"{os.fspath(path)}: not UTF-8") from exc


def render_instruction_files(
    role_path: str | os.PathLike[str],
    *,
    agent: str,
    owner_language: str,
    board_write: bool,
    common_path: str | os.PathLike[str] = TEMPLATE_PATH,
    hindsight_path: str | os.PathLike[str] = HINDSIGHT_PATH,
    tree: str | os.PathLike[str] = TREE,
    host: Mapping[str, str] | None = None,
    memory: bool = True,
    data_read: bool = False,
) -> str:
    """``render_instructions`` over files read with ``read_instruction_file``; includes come from ``tree``,
    host values from ``host``, else ``naming.load_host()``."""
    if host is None:
        try:
            host = naming.load_host()
        except naming.NamingError as exc:
            raise AgentTextError(str(exc)) from None
    return render_instructions(
        read_instruction_file(common_path),
        read_instruction_file(role_path),
        agent=agent,
        owner_language=owner_language,
        board_write=board_write,
        bank=memory_bank(agent, hindsight_path, host),
        host=host,
        include=lambda relative: read_instruction_file(Path(tree) / relative),
        memory=memory,
        data_read=data_read,
    )


def render_playbook(text: str, host: Mapping[str, str] | None = None) -> str:
    """A playbook as installed: ``{{host.<key>}}`` filled in, any other placeholder refused."""
    _check_text(text, "playbook")
    rendered = _HostValues(host).render(text, "playbook")
    if "{{" in rendered:
        raise AgentTextError("playbook: only {{host.<key>}} placeholders are allowed")
    return _host_names(rendered, host, "playbook")


def _host_names(text: str, host: Mapping[str, str] | None, label: str) -> str:
    """user tokens (``@agent:dev@``, ``@exec@``) resolved, the repository's ``kd-`` commands and ``KD-``
    cards written with this host's names."""
    try:
        return naming.rewrite(naming.render(text, host=host))
    except naming.NamingError as exc:
        raise AgentTextError(f"{label}: {exc}") from None


class _HostValues:
    """host/host.json values for ``{{host.<key>}}``; none given — a text that names one is refused."""

    def __init__(self, host: Mapping[str, str] | None) -> None:
        self._host = host

    def render(self, text: str, label: str) -> str:
        return _HOST_PLACEHOLDER.sub(lambda match: self._value(match.group(1), label), text)

    def _value(self, key: str, label: str) -> str:
        if self._host is None:
            raise AgentTextError(f"{label}: host.{key} needs the deployment values (host/host.json)")
        value = self._host.get(key)
        if not isinstance(value, str) or not value:
            raise AgentTextError(f"{label}: no value for host.{key}")
        return value


def _include(text: str, agent: str, include: Callable[[str], str] | None) -> str:
    def one(match: re.Match[str]) -> str:
        path = match.group(1)
        parts = path.split("/")
        if (not _INCLUDE_PATH.fullmatch(path) or parts[:2] != ["agents", agent] or len(parts) < 3
                or any(part in (".", "..") for part in parts)):
            raise AgentTextError(f"role text: include {path!r} is not a .md file under agents/{agent}/")
        if include is None:
            raise AgentTextError(f"role text: include {path!r} without a tree to read it from")
        body = include(path)
        _check_text(body, f"include {path}")
        if _INCLUDE.search(body):
            raise AgentTextError(f"include {path}: a nested include is not allowed")
        return body.strip("\n")

    return _INCLUDE.sub(one, text)


def memory_bank(
    agent: str, hindsight_path: str | os.PathLike[str] = HINDSIGHT_PATH, host: Mapping | None = None
) -> str:
    """The bank of the agent: the host's own (``memory_banks`` of host.json — a bank kept across a rename of the
    agent), else the one the Hindsight config maps the agent's folder to, else ``coding-agent::<agent>``."""
    label = os.fspath(hindsight_path)
    own = ((host or {}).get("memory_banks") or {}).get(agent)
    if own is not None:
        if not isinstance(own, str) or not _BANK.fullmatch(own):
            raise AgentTextError(f"host.json: bad memory bank of {agent}")
        return own
    try:
        config = json.loads(read_instruction_file(hindsight_path))
    except AgentTextError:
        raise
    except ValueError as exc:
        raise AgentTextError(f"{label}: not JSON") from exc
    folders = config.get("mapPathToBank") if isinstance(config, dict) else None
    if not isinstance(folders, dict):
        raise AgentTextError(f"{label}: mapPathToBank must be an object")
    banks = [
        bank
        for folder, bank in folders.items()
        if isinstance(folder, str) and posixpath.basename(folder) == agent
    ]
    if len(banks) > 1:
        raise AgentTextError(f"{label}: several folders of {agent}")
    bank = banks[0] if banks else f"coding-agent::{agent}"
    if not isinstance(bank, str) or not _BANK.fullmatch(bank):
        raise AgentTextError(f"{label}: bad bank of {agent}")
    return bank


def _check_text(text: object, label: str) -> None:
    if not isinstance(text, str):
        raise AgentTextError(f"{label}: expected str")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise AgentTextError(f"{label}: not encodable as UTF-8") from exc
    if size > MAX_TEXT_BYTES:
        raise AgentTextError(f"{label}: larger than {MAX_TEXT_BYTES} bytes")
    for number, line in enumerate(text.split("\n"), 1):
        for char in line:
            if unicodedata.category(char) in _FORBIDDEN_CATEGORIES:
                raise AgentTextError(
                    f"{label}: control or format character U+{ord(char):04X} on line {number}"
                )


def _parse_template(
    common_text: str, agent: str, bank: str, language: str, values: _HostValues
) -> tuple[Section, ...]:
    document = parse_document(common_text, label="shared template")
    if document.preamble and not _HTML_COMMENT.fullmatch(document.preamble):
        raise AgentTextError(
            "shared template: only an HTML comment may precede the sections"
        )
    titles = tuple(section.title for section in document.sections)
    if titles != COMMON_SECTIONS:
        raise AgentTextError(
            f"shared template: sections must be exactly {list(COMMON_SECTIONS)}"
        )
    sections = []
    for section in document.sections:
        unknown = {
            name for name in _PLACEHOLDER.findall(section.body)
            if name not in _PLACEHOLDERS and not _HOST_PLACEHOLDER.fullmatch("{{" + name + "}}")
        }
        if unknown:
            raise AgentTextError(
                f"shared template: unknown placeholder in {section.title!r}"
            )
        body = values.render(
            section.body.replace("{{bank}}", bank)
            .replace("{{agent}}", agent)
            .replace("{{owner_language}}", language),
            "shared template",
        )
        if "{{" in body:
            raise AgentTextError(
                f"shared template: malformed placeholder in {section.title!r}"
            )
        sections.append(Section(section.title, body))
    return tuple(sections)


def _section_key(title: str) -> str:
    """Comparison key for near-duplicates: the title before an em dash, letters and digits only."""
    key = re.sub(r"[\W_]+", "", title.split(" — ", 1)[0].casefold())
    if not key:
        raise AgentTextError(f"section title {title!r} has no letters or digits")
    return key


def _rule_lines(text: str) -> set[str]:
    """Lines that state something; fences, rules and table separators have fewer than 3 words."""
    return {line.strip() for line in text.split("\n") if len(line.split()) >= 3}


def _reject_repeated_lines(shared: tuple[Section, ...], role: Document) -> None:
    shared_lines = set().union(*(_rule_lines(section.body) for section in shared))
    role_lines = _rule_lines(role.preamble).union(
        *(_rule_lines(section.body) for section in role.sections)
    )
    if shared_lines & role_lines:
        raise AgentTextError(
            "role text repeats a line of the shared template; keep it only in the template"
        )


def _trim_blank_lines(lines: list[str]) -> str:
    start, end = 0, len(lines)
    while start < end and not lines[start].strip():
        start += 1
    while end > start and not lines[end - 1].strip():
        end -= 1
    return "\n".join(lines[start:end])
