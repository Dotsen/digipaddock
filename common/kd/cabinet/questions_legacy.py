"""Old text questions as forms (remediation spec 7.3).

An agent's question from before the form (a ``{text}`` payload) is often a numbered list whose points offer
А) / Б) (Cyrillic), A) / B) or 1) / 2) choices and a recommendation. ``recognize`` turns such a text into a form of the
same shape as ``kd.cabinet.questions`` builds, so the page can offer the choices — but only where the text is
unambiguous. A point whose markers are out of order, repeated, quoted or bracketed stays a text question with
its whole body; a text that is not a list of questions (an "understood it as" summary) is not a form at all. Nothing is
cut or reworded: an option's description is its full text, what follows the options is the point's ``note``.
The text is only read — never run, never taken as a command.
"""

import re
from typing import List, Optional, Tuple

from kd.cabinet import questions

ITEMS_MAX = questions.QUESTIONS_MAX
_ITEM = re.compile(r"(?m)^(\d{1,2})\.[ \t]+(?=\S)")
_MARK = re.compile(r"(?:(?<=\s)|^)([А-ЗA-H]|[1-8])\)(?=\s)")
_SEQUENCES = ("АБВГДЕЖЗ", "ABCDEFGH", "12345678")
_END = re.compile(r"[.?!](?=\s|$)")
_RECOMMEND = re.compile(r"Рекоменд(?:ую|ация:?)\s+([А-ЗA-H]|[1-8])(?=[\s.,;:—)]|$)")
_QUOTES = (re.compile(r"«[^»]*»"), re.compile(r'"[^"\n]*"'), re.compile(r"`[^`\n]*`"))
_LEAD = ".;:!?,"  # what an option marker follows, besides a line break: "at once or B)" is a phrase, not a list

Span = Tuple[int, int]


def _spans(text: str) -> List[Span]:
    """Quoted, coded and bracketed stretches: a marker or a sentence end inside one is not structure."""
    spans = [m.span() for pattern in _QUOTES for m in pattern.finditer(text)]
    depth, start = 0, 0
    for index, char in enumerate(text):
        if char == "(":
            if depth == 0:
                start = index
            depth += 1
        elif char == ")" and depth:
            depth -= 1
            if depth == 0:
                spans.append((start, index + 1))
    return spans


def _inside(position: int, spans: List[Span]) -> bool:
    return any(a <= position < b for a, b in spans)


def _first_end(text: str, offset: int, spans: List[Span]) -> Optional[int]:
    """Index (in ``text``) of the first sentence end at or after ``offset`` outside every span."""
    for match in _END.finditer(text, offset):
        if not _inside(match.start(), spans):
            return match.start()
    return None


def _sentence(text: str, spans: List[Span], base: int = 0) -> Tuple[str, str]:
    """The first sentence of ``text`` and the rest; ``spans`` are of the whole body, ``base`` is where ``text`` is."""
    shifted = [(a - base, b - base) for a, b in spans]
    end = _first_end(text, 0, shifted)
    if end is None:
        return text.strip(), ""
    return text[: end + 1].strip(), text[end + 1 :].strip()


def _text_question(number: int, body: str, spans: List[Span]) -> dict:
    title, context = _sentence(body, spans)
    return {
        "id": f"q{number}", "title": title, "context": context, "type": "text",
        "required": False, "allow_custom": False, "options": [],
    }


def _choice_question(number: int, body: str, spans: List[Span], marks) -> Optional[dict]:
    pre = body[: marks[0].start()]
    if not pre.strip():
        return None
    title, context = _sentence(pre, spans)
    options, note = [], ""
    for index, mark in enumerate(marks):
        start = mark.end()
        if index + 1 < len(marks):
            text = body[start : marks[index + 1].start()].strip().rstrip(";,").strip()
        else:
            end = _first_end(body, start, spans)
            text = (body[start : end + 1] if end is not None else body[start:]).strip()
            note = body[end + 1 :].strip() if end is not None else ""
        if not text:
            return None
        options.append({
            "id": f"o{index + 1}", "label": mark.group(1), "description": text, "recommended": False,
        })
    recommended = _RECOMMEND.search(note)
    if recommended:
        for option in options:
            option["recommended"] = option["label"] == recommended.group(1)
    question = {
        "id": f"q{number}", "title": title, "context": context, "type": "single_select",
        "required": False, "allow_custom": True, "options": options,
    }
    if note:
        question["note"] = note
    return question


def _leads(body: str, mark) -> bool:
    """Whether ``mark`` starts a line or follows a punctuation mark — where a list item can start."""
    before = body[: mark.start()]
    stripped = before.rstrip()
    return not stripped or "\n" in before[len(stripped) :] or stripped[-1] in _LEAD


def _question(number: int, body: str) -> dict:
    spans = _spans(body)
    marks = [m for m in _MARK.finditer(body) if not _inside(m.start(1), spans)]
    labels = "".join(m.group(1) for m in marks)
    if marks and all(_leads(body, m) for m in marks) and 2 <= len(marks) <= questions.OPTIONS_MAX and any(s.startswith(labels) for s in _SEQUENCES):
        question = _choice_question(number, body, spans, marks)
        if question is not None:
            return question
    return _text_question(number, body, spans)


def recognize(text: str) -> Optional[dict]:
    """The form a numbered text question reads as, or None when it is not one beyond doubt."""
    if not isinstance(text, str):
        return None
    starts = list(_ITEM.finditer(text))
    if not starts or len(starts) > ITEMS_MAX:
        return None
    if [int(m.group(1)) for m in starts] != list(range(1, len(starts) + 1)):
        return None
    out = []
    for index, start in enumerate(starts):
        end = starts[index + 1].start() if index + 1 < len(starts) else len(text)
        body = text[start.end() : end].strip()
        question = _question(index + 1, body)
        if question["type"] == "text" and "?" not in body:
            return None  # a summary point, not a question: the whole text is not a list of questions
        out.append(question)
    if len(out) == 1 and out[0]["type"] == "text":
        return None
    return {"schema_version": questions.SCHEMA_VERSION, "text": text[: starts[0].start()].strip(), "questions": out}
