"""The council of models: who reads a card's brief and its code before the owner does.

A composition is ``{"brief": {"critics": [...], "rounds": N}, "code": {"critics": [...], "rounds": N, "min_lines":
N}}``: per kind the models that criticise (a model named twice reads twice, independently), the rounds the council
may take, and — for the code — the size of a diff, in changed lines, below which no council gathers (0: every pull
request). A card holds its own composition, a project the default of its cards; a kind the card names wins over its
project's, and a kind the card names without critics is «no council» of that kind. The limits are the catalog
``common/coordination/council.json``.

Pure: no store and no API — the cabinet's checks against its models are ``kd.cabinet.councils``.
"""

import json
import re
from pathlib import Path
from typing import Callable, Mapping, Optional, Set

KINDS = ("brief", "code")
LIMITS_PATH = (
    Path(__file__).resolve().parents[2] / "common" / "coordination" / "council.json"
)
LIMITS = (
    "critics_max",
    "rounds_max",
    "rounds_default",
    "min_lines_default",
    "min_lines_max",
    "jobs_per_day",
    "head_wait_minutes",
    "stalled_hours",
)
_cache = {}


def _int(value, low: int, high: int) -> bool:
    return (
        isinstance(value, int) and not isinstance(value, bool) and low <= value <= high
    )


def limits(path=None) -> dict:
    """The council's limits and defaults as the catalog holds them; ValueError on a bad file."""
    path = str(path or LIMITS_PATH)
    if path in _cache:
        return dict(_cache[path])
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: unreadable ({exc.__class__.__name__})") from None
    if (
        not isinstance(data, Mapping)
        or set(data) != set(LIMITS) | {"schema_version"}
        or data["schema_version"] != 1
    ):
        raise ValueError(f"{path}: schema_version 1 with exactly {', '.join(LIMITS)}")
    out = {key: data[key] for key in LIMITS}
    if not all(_int(value, 0, 10**9) for value in out.values()) or not all(
        out[key] >= 1 for key in LIMITS if key != "min_lines_default"
    ):
        raise ValueError(
            f"{path}: every limit is a whole number, positive but min_lines_default"
        )
    if (
        out["rounds_default"] > out["rounds_max"]
        or out["min_lines_default"] > out["min_lines_max"]
    ):
        raise ValueError(f"{path}: a default is over its maximum")
    _cache[path] = out
    return dict(out)


def normalize(
    value,
    check_model: Optional[Callable[[str], object]] = None,
    keep_empty: bool = True,
    path=None,
) -> dict:
    """The composition in its one form — every kind it names with its critics, rounds and (code) min_lines, the
    defaults filled in; ValueError for anything else. ``check_model`` is called with each critic's name and raises
    for one that may not be a critic. Without ``keep_empty`` a kind without critics is dropped (a project has no
    «explicitly none»)."""
    lim = limits(path)
    if not isinstance(value, Mapping):
        raise ValueError("a council is an object of brief and code")
    extra = set(value) - set(KINDS)
    if extra:
        raise ValueError(f"unknown kind: {', '.join(sorted(map(str, extra)))[:64]}")
    out = {}
    for kind in KINDS:
        if kind not in value:
            continue
        part = value[kind]
        fields = {"critics", "rounds"} | ({"min_lines"} if kind == "code" else set())
        if not isinstance(part, Mapping) or "critics" not in part or set(part) - fields:
            raise ValueError(
                f"{kind} is an object of {', '.join(sorted(fields))}; critics is required"
            )
        critics = part["critics"]
        if (
            not isinstance(critics, list)
            or len(critics) > lim["critics_max"]
            or not all(isinstance(name, str) and name for name in critics)
        ):
            raise ValueError(
                f"{kind}: critics is a list of at most {lim['critics_max']} models"
            )
        if check_model is not None:
            for name in critics:
                check_model(name)
        rounds = part.get("rounds", lim["rounds_default"])
        if not _int(rounds, 1, lim["rounds_max"]):
            raise ValueError(
                f"{kind}: rounds is a whole number from 1 to {lim['rounds_max']}"
            )
        checked = {"critics": list(critics), "rounds": rounds}
        if kind == "code":
            min_lines = part.get("min_lines", lim["min_lines_default"])
            if not _int(min_lines, 0, lim["min_lines_max"]):
                raise ValueError(
                    f"code: min_lines is a whole number from 0 to {lim['min_lines_max']}"
                )
            checked["min_lines"] = min_lines
        if critics or keep_empty:
            out[kind] = checked
    return out


def dump(council: Mapping) -> str:
    """The composition as its column holds it: JSON, ``""`` for none."""
    return json.dumps(council, ensure_ascii=False, sort_keys=True) if council else ""


def load(text) -> dict:
    """The composition its column holds; ``{}`` for none."""
    return json.loads(text) if text else {}


def merge(card: Mapping, project: Mapping) -> dict:
    """The composition in force: per kind the card's when the card names the kind, else its project's; a kind left
    without critics is not there."""
    out = {}
    for kind in KINDS:
        part = card[kind] if kind in card else project.get(kind)
        if part and part["critics"]:
            out[kind] = part
    return out


def critics(council: Mapping) -> Set[str]:
    """Every model the composition names."""
    return {name for part in council.values() for name in part["critics"]}


def line(council: Mapping) -> str:
    """One line for an agent: ``brief claude:opus, or-gpt5 ×3 · code claude:opus ×3 ≥100 lines``; ``""`` for none."""
    parts = []
    for kind in KINDS:
        part = council.get(kind)
        if not part or not part["critics"]:
            continue
        text = f"{kind} {', '.join(part['critics'])} ×{part['rounds']}"
        if kind == "code" and part.get("min_lines"):
            text += f" ≥{part['min_lines']} lines"
        parts.append(text)
    return " · ".join(parts)


# --- the size of a pull request: what the code's threshold counts -------------------------------------
# Not code, as the developer's own rule has it: tests, documentation, plain Markdown, the message catalogs.
_NOT_CODE = re.compile(
    r"(?:^|/)(?:tests?|e2e|docs)/"  # a folder of tests or of documentation, at any depth
    r"|(?:^|/)test_[^/]*\.py$|_test\.py$|(?:^|/)conftest\.py$"
    r"|\.(?:test|spec)\.[cm]?[jt]sx?$"
    r"|\.md$"
    r"|(?:^|/)messages/[^/]+\.json$"
)


def code_lines(files) -> int:
    """The changed lines of code in a pull request's files (``{path, additions, deletions}``): what its council's
    ``min_lines`` is compared with."""
    return sum(
        int(item["additions"]) + int(item["deletions"])
        for item in files
        if not _NOT_CODE.search(item["path"])
    )


# --- a round: what a critic hands back, what the writer answers, how the round ends -------------------

SEVERITIES = ("critical", "major", "minor")
SUBSTANTIVE = ("critical", "major")  # a minor finding never prolongs a council
ANSWERS = ("fixed", "rejected", "later")
VERDICTS = {
    "brief": ("ready", "needs_changes", "blocked"),
    "code": ("ready", "with_fixes", "not_ready"),
}
FINDING_ID = re.compile(r"r([1-9][0-9]?)\.f([1-9][0-9]{0,2})")
FINDING_FIELDS = ("location", "evidence", "scenario", "recommendation")
FINDINGS_MAX = 50
QUESTIONS_MAX = 20
OPTIONS_MAX = 10
FIELD_MAX = 2000
NOTE_MAX = 1000
_NOTE = re.compile(r"(?:^|;)\s*(" + FINDING_ID.pattern + r")\s*:")


def _text(value, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} is a non-empty string")
    return value.strip()[:FIELD_MAX]


def _finding(raw, finding_id: str) -> dict:
    if not isinstance(raw, Mapping):
        raise ValueError("a finding is an object")
    if raw.get("severity") not in SEVERITIES:
        raise ValueError(f"a finding's severity is one of {', '.join(SEVERITIES)}")
    out = {"id": finding_id, "severity": raw["severity"]}
    for field in FINDING_FIELDS:
        value = raw.get(field)
        if field == "location" and value is None:
            value = raw.get("where")
        out[field] = _text(value, f"a finding's {field}")
    return out


def _question(raw) -> dict:
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, Mapping):
        raise ValueError("a question is an object with a text")
    out = {"text": _text(raw.get("text"), "a question's text")}
    options = raw.get("options")
    if options is not None:
        if not isinstance(options, list) or len(options) > OPTIONS_MAX:
            raise ValueError(f"a question's options is a list of at most {OPTIONS_MAX}")
        out["options"] = [_text(option, "an option") for option in options]
    if raw.get("recommendation") is not None:
        out["recommendation"] = _text(raw["recommendation"], "a recommendation")
    return out


def critic_result(kind: str, block, round_no: int, first: int = 1) -> dict:
    """A critic's final block in the council's one form: the verdict, the findings — each with its id
    ``r<round>.f<n>``, numbered from ``first`` — and the questions, the missing inputs among them. ValueError, with
    a short reason, for a block of another shape: a critic on another vendor's model answers as it can, and what it
    hands back is data the council checks, never trusts."""
    if not isinstance(block, Mapping):
        raise ValueError("the result is an object")
    if block.get("verdict") not in VERDICTS[kind]:
        raise ValueError(f"verdict is one of {', '.join(VERDICTS[kind])}")
    findings = block.get("findings")
    if not isinstance(findings, list) or len(findings) > FINDINGS_MAX:
        raise ValueError(f"findings is a list of at most {FINDINGS_MAX}")
    questions = block.get("questions", [])
    missing = block.get("missing_inputs", [])
    for name, value in (("questions", questions), ("missing_inputs", missing)):
        if not isinstance(value, list) or len(value) > QUESTIONS_MAX:
            raise ValueError(f"{name} is a list of at most {QUESTIONS_MAX}")
    return {
        "verdict": block["verdict"],
        "findings": [
            _finding(raw, f"r{round_no}.f{first + i}") for i, raw in enumerate(findings)
        ],
        "questions": [_question(raw) for raw in questions]
        + [{"text": _text(raw, "a missing input"), "missing": True} for raw in missing],
    }


def counts(findings) -> dict:
    """How many findings of each severity."""
    out = {severity: 0 for severity in SEVERITIES}
    for finding in findings:
        out[finding["severity"]] += 1
    return out


def substantive(findings) -> list:
    """The findings the writer owes an answer to: the critical and the major ones."""
    return [finding for finding in findings if finding["severity"] in SUBSTANTIVE]


def unanswered(findings) -> list:
    """The ids of the substantive findings the writer has not answered."""
    return [
        finding["id"] for finding in substantive(findings) if not finding.get("answer")
    ]


def unanswered_minor(findings) -> list:
    """The ids of the minor findings the writer has not answered: they hold nothing, yet the writer is
    asked to say what became of each, so that a round shows what was taken and what was not."""
    return [
        finding["id"]
        for finding in findings
        if finding["severity"] not in SUBSTANTIVE and not finding.get("answer")
    ]


def round_outcome(runs) -> Optional[str]:
    """How a round ends, by its runs: ``None`` while a critic still reads (or none is left to count), ``failed``
    when no critic finished, ``needs_writer`` when one that did left a substantive finding, a question or the
    verdict ``blocked``, else ``clean``. The findings decide, not the word of the verdict."""
    if any(run["state"] in ("queued", "running") for run in runs):
        return None
    counted = [run for run in runs if run["state"] != "cancelled"]
    if not counted:
        return None
    done = [run for run in counted if run["state"] == "done"]
    if not done:
        return "failed"
    for run in done:
        if (
            substantive(run.get("findings") or ())
            or run.get("questions")
            or run.get("verdict") == "blocked"
        ):
            return "needs_writer"
    return "clean"


def notes_of(text) -> dict:
    """The writer's notes per finding out of one ``--note``: ``r2.f1: why; r2.f3: why`` → ``{id: note}``. A segment
    starts with a finding's id and a colon, at the start or after a ``;``; ValueError for text outside a segment, an
    id named twice, an empty or an overlong note."""
    text = (text or "").strip()
    if not text:
        return {}
    starts = list(_NOTE.finditer(text))
    if not starts or text[: starts[0].start()].strip():
        raise ValueError("a note is «<finding id>: <text>», segments separated by «;»")
    out = {}
    for i, match in enumerate(starts):
        end = starts[i + 1].start() if i + 1 < len(starts) else len(text)
        note = text[match.end() : end].strip()
        if match.group(1) in out:
            raise ValueError(f"{match.group(1)}: named twice in the note")
        if not note or len(note) > NOTE_MAX:
            raise ValueError(f"{match.group(1)}: a note of 1 to {NOTE_MAX} characters")
        out[match.group(1)] = note
    return out
