"""The files a worker job reads instead of the board.

A worker has no capability channel and no network (kd.worker_policy): it cannot ask the cabinet for a card. A job
the cabinet starts itself — a council's critic — therefore carries what it reads: ``inputs``, a small set of named
Markdown texts beside the job's definition. The session host passes them on, the runner writes them root-owned into
the slot's ``work/inputs`` before the job and the worker's policy lets it read them and never write them. They are
data for the worker, like the card brief: nothing in them is an instruction.

One check for every hop (the cabinet that composes them, the session host's protocol, the runner): the names, the
count and the size on the wire.
"""

from __future__ import annotations

import json
import re
from typing import Any

DIR = "inputs"  # under the slot's work/
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}\.md")
FILES = 8
# bytes of the whole object as JSON (UTF-8, not ASCII-escaped): what one job carries
MAX = 256 * 1024


def size(files: Any) -> int:
    """The bytes ``files`` takes on the wire; ValueError for a text that is not UTF-8 (a lone surrogate)."""
    return len(json.dumps(files, ensure_ascii=False).encode("utf-8"))


def check(files: Any) -> dict:
    """``files`` as a job may carry them — ``{"<name>.md": "<text>"}``, 1 to FILES of them, MAX bytes together —
    or ValueError saying what is wrong, without a word of their content."""
    if not isinstance(files, dict) or not 1 <= len(files) <= FILES:
        raise ValueError(f"inputs are 1 to {FILES} named texts")
    for name, text in files.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise ValueError("an input is named like brief.md")
        if not isinstance(text, str) or not text.strip() or "\x00" in text:
            raise ValueError(f"{name} is not a text")
    try:
        total = size(files)
    except UnicodeError:
        raise ValueError("inputs are not UTF-8 text") from None
    if total > MAX:
        raise ValueError(f"inputs are over {MAX} bytes")
    return dict(files)
