"""Structured questions v1 (remediation spec 7.2): the form an agent opens as a ``question`` decision,
the answer a person gives to it and the readable reply the agent and the thread get.

A form is ``{schema_version: 1, text, questions: [...]}``; a question is ``single_select``, ``multi_select`` or
``text`` with stable ids; an answer is ``{question_id: {selected_option_ids, custom_text}}`` plus a comment. The
server checks both: an agent learns what is wrong with its form before anything is written, a person's answer is
refused rather than stored half valid. A recommendation is only a mark — nothing here picks it for anyone.
"""

import re
from typing import List, Mapping, Optional, Tuple

from kd.cabinet import decisions

SCHEMA_VERSION = 1
TYPES = ("single_select", "multi_select", "text")
QUESTIONS_MAX = 8
OPTIONS_MIN, OPTIONS_MAX = 2, 8
TITLE_MAX = 300
CONTEXT_MAX = 2000
LABEL_MAX = 120
DESCRIPTION_MAX = 500
CUSTOM_MAX = 2000
COMMENT_MAX = 4000
FORM_MAX = 32 * 1024  # bytes of the canonical JSON
READABLE_MAX = 32 * 1024  # the reply text built from an answer: the longest valid answer (~30 300) fits
ID = re.compile(r"[a-z][a-z0-9_]{0,31}")
_FORM_KEYS = frozenset({"schema_version", "text", "questions"})
_QUESTION_KEYS = frozenset({"id", "title", "context", "type", "required", "allow_custom", "options"})
_OPTION_KEYS = frozenset({"id", "label", "description", "recommended"})
_ANSWER_KEYS = frozenset({"selected_option_ids", "custom_text"})
NO_ANSWER = "—"


class FormError(ValueError):
    """The form is not valid; the message names the place (``questions[2].options[0].id``)."""


class AnswerError(ValueError):
    """The answer does not fit its form; the message names the question."""


def _keys(value, allowed, where: str, error) -> Mapping:
    if not isinstance(value, Mapping):
        raise error(f"{where} must be an object")
    for key in value:
        if key not in allowed:
            raise error(f"{where}: unknown key {key!r}" if where else f"unknown key {key!r}")
    return value


def _string(value, where: str, limit: int, *, required: bool = True) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise FormError(f"{where} must be text")
    value = value.strip()
    if (required and not value) or len(value) > limit:
        low = 1 if required else 0
        raise FormError(f"{where} must be text of {low} to {limit} characters")
    return value


def _flag(value, where: str) -> bool:
    if value is None:
        return False
    if not isinstance(value, bool):
        raise FormError(f"{where} must be true or false")
    return value


def _id(value, where: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise FormError(f"{where} must match {ID.pattern}")
    return value


def _option(raw, where: str) -> dict:
    _keys(raw, _OPTION_KEYS, where, FormError)
    return {
        "id": _id(raw.get("id"), f"{where}.id"),
        "label": _string(raw.get("label"), f"{where}.label", LABEL_MAX),
        "description": _string(raw.get("description"), f"{where}.description", DESCRIPTION_MAX, required=False),
        "recommended": _flag(raw.get("recommended"), f"{where}.recommended"),
    }


def _question(raw, where: str) -> dict:
    _keys(raw, _QUESTION_KEYS, where, FormError)
    kind = raw.get("type")
    if kind not in TYPES:
        raise FormError(f"{where}.type must be one of {', '.join(TYPES)}")
    question = {
        "id": _id(raw.get("id"), f"{where}.id"),
        "title": _string(raw.get("title"), f"{where}.title", TITLE_MAX),
        "context": _string(raw.get("context"), f"{where}.context", CONTEXT_MAX, required=False),
        "type": kind,
        "required": _flag(raw.get("required"), f"{where}.required"),
        "allow_custom": _flag(raw.get("allow_custom"), f"{where}.allow_custom"),
        "options": [],
    }
    options = raw.get("options")
    if kind == "text":
        if options:
            raise FormError(f"{where}: a text question has no options")
        if question["allow_custom"]:
            raise FormError(f"{where}: allow_custom is only for select questions")
        return question
    if not isinstance(options, list) or not OPTIONS_MIN <= len(options) <= OPTIONS_MAX:
        raise FormError(f"{where}: {OPTIONS_MIN} to {OPTIONS_MAX} options")
    seen = set()
    for index, option in enumerate(options):
        item = _option(option, f"{where}.options[{index}]")
        if item["id"] in seen:
            raise FormError(f"{where}.options[{index}]: duplicate option id {item['id']!r}")
        seen.add(item["id"])
        question["options"].append(item)
    if kind == "single_select" and sum(o["recommended"] for o in question["options"]) > 1:
        raise FormError(f"{where}: at most one recommended option in a single_select")
    return question


def validate_form(payload: Mapping) -> dict:
    """The agent's form as the cabinet stores it: every key present, defaults filled; ``FormError`` otherwise."""
    _keys(payload, _FORM_KEYS, "", FormError)
    if payload.get("schema_version") != SCHEMA_VERSION or isinstance(payload.get("schema_version"), bool):
        raise FormError(f"schema_version must be {SCHEMA_VERSION}")
    text = _string(payload.get("text"), "text", decisions.TEXT_MAX)
    raw = payload.get("questions")
    if not isinstance(raw, list) or not 1 <= len(raw) <= QUESTIONS_MAX:
        raise FormError(f"questions: 1 to {QUESTIONS_MAX} questions")
    out, seen = [], set()
    for index, question in enumerate(raw):
        item = _question(question, f"questions[{index}]")
        if item["id"] in seen:
            raise FormError(f"questions[{index}]: duplicate question id {item['id']!r}")
        seen.add(item["id"])
        out.append(item)
    form = {"schema_version": SCHEMA_VERSION, "text": text, "questions": out}
    if len(decisions.canonical(form)) > FORM_MAX:
        raise FormError(f"the form is too large ({FORM_MAX} bytes of JSON at most)")
    return form


def form_sha(form: Mapping) -> str:
    return decisions.payload_sha(form)


def form_of(payload: Mapping) -> Tuple[Optional[dict], str]:
    """A question's form and where it comes from: the agent's own (``agent``), the one its old text reads as
    (``parsed``, ``kd.cabinet.questions_legacy``), or none (``""``)."""
    from kd.cabinet import questions_legacy

    if "questions" in payload:
        return dict(payload), "agent"
    form = questions_legacy.recognize(payload.get("text", ""))
    return (form, "parsed") if form is not None else (None, "")


EXPECTS = ("file", "text")  # what answers a question: a file, or text — the default, never written


def expect_of(payload: Mapping) -> str:
    """``file`` when the question asked for a file; ``text`` otherwise (a question older than the field too)."""
    return "file" if payload.get("expect") == "file" else "text"


def view(decision: Mapping) -> dict:
    """What the page shows for a question: its text, its form if it has one, and the hash an answer binds."""
    form, origin = form_of(decision["payload"])
    return {
        "text": decision["payload"].get("text", ""),
        "expect": expect_of(decision["payload"]),
        "form": form,
        "origin": origin,
        "form_sha256": form_sha(form) if form is not None else "",
    }


def comment_of(comment) -> str:
    """The answer's shared comment, stripped; ``AnswerError`` if it is not text or too long."""
    if comment is None:
        return ""
    if not isinstance(comment, str):
        raise AnswerError("comment must be text")
    comment = comment.strip()
    if len(comment) > COMMENT_MAX:
        raise AnswerError(f"comment is too long ({COMMENT_MAX} characters at most)")
    return comment


def _answer_item(question: Mapping, raw) -> dict:
    qid = question["id"]
    if raw is None:
        raw = {}
    _keys(raw, _ANSWER_KEYS, qid, AnswerError)
    selected = raw.get("selected_option_ids", [])
    if not isinstance(selected, list) or not all(isinstance(i, str) for i in selected):
        raise AnswerError(f"{qid}.selected_option_ids must be a list of option ids")
    custom = raw.get("custom_text", "")
    if not isinstance(custom, str):
        raise AnswerError(f"{qid}.custom_text must be text")
    custom = custom.strip()
    if len(custom) > CUSTOM_MAX:
        raise AnswerError(f"{qid}.custom_text is too long ({CUSTOM_MAX} characters at most)")
    if question["type"] == "text":
        if selected:
            raise AnswerError(f"{qid}: a text question takes text, not options")
    else:
        known = {o["id"] for o in question["options"]}
        for option in selected:
            if option not in known:
                raise AnswerError(f"{qid}: unknown option {option!r}")
        if len(set(selected)) != len(selected):
            raise AnswerError(f"{qid}: duplicate option in the answer")
        if question["type"] == "single_select" and len(selected) > 1:
            raise AnswerError(f"{qid}: choose one option")
        if custom and not question["allow_custom"]:
            raise AnswerError(f"{qid}: no custom answer here, choose an option")
    if question["required"] and not selected and not custom:
        raise AnswerError(f"{qid} is required")
    return {"selected_option_ids": list(selected), "custom_text": custom}


def validate_answer(form: Mapping, answers: Mapping, comment: str) -> dict:
    """Every question of the form, in its order, with what the person chose; ``AnswerError`` otherwise."""
    if not isinstance(answers, Mapping):
        raise AnswerError("answers must be an object of question ids")
    ids = [q["id"] for q in form["questions"]]
    for qid in answers:
        if qid not in ids:
            raise AnswerError(f"unknown question {qid!r}")
    comment_of(comment)
    return {q["id"]: _answer_item(q, answers.get(q["id"])) for q in form["questions"]}


def _title(question: Mapping) -> str:
    return question["title"].rstrip(" .:")


def readable(form: Mapping, answers: Mapping, comment: str) -> str:
    """The answer as text: one numbered line per question — the chosen labels, the person's own words after
    a dash, a dash alone for no answer — then the comment."""
    lines: List[str] = []
    for number, question in enumerate(form["questions"], 1):
        item = answers.get(question["id"]) or {}
        chosen = set(item.get("selected_option_ids", []))
        labels = "; ".join(o["label"] for o in question["options"] if o["id"] in chosen)
        custom = item.get("custom_text", "")
        value = f"{labels} — {custom}" if labels and custom else labels or custom or NO_ANSWER
        lines.append(f"{number}. {_title(question)}: {value}")
    text = "\n".join(lines)
    comment = (comment or "").strip()
    return f"{text}\n\n{comment}" if comment else text
