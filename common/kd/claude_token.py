"""the host's Claude token — one ``claude setup-token`` token (a year, the owner's subscription) for every
agent session, worker job and Hindsight, instead of a ``/login`` per user whose refresh token dies in about four
weeks.

The token is the catalog secret ``claude-token``: /etc/digipaddock/claude.env, ``CLAUDE_CODE_OAUTH_TOKEN=<token>``,
root 0600 (``kd-secret put claude-token``). systemd reads it as root for the session units and the Hindsight units
(``EnvironmentFile=-``), the root worker runner into the job's environment; no agent user can read the file.
``claude`` itself never gets the variable — ``kd.secret_fd`` moves it into a pipe right before claude starts
(``CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR``), and the fail-closed sandbox (``sandbox.credentials``) still unsets it
before every Bash command, which runs in its own PID namespace. Without the file
everything authenticates as before, with the user's stored login. The token's age is the file's mtime.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

PATH = "/etc/digipaddock/claude.env"
KEY = "CLAUDE_CODE_OAUTH_TOKEN"
LIFETIME_DAYS = 365
DAY = 86400
# a bare value: systemd would strip quotes and expand escapes the runner keeps, and log a line it rejects, value too
VALUE = re.compile(r"[A-Za-z0-9_-]+")


def _parse(text: str) -> tuple[str, str]:
    """(token, "") for exactly one ``KEY=<bare value>`` line besides blank and comment lines; ("", reason) else.
    The reason never carries the value."""
    lines = [
        s
        for s in (line.strip() for line in text.splitlines())
        if s and s[0] not in "#;"
    ]
    if not lines:
        return "", f"no {KEY}= line"
    keyed = [s.partition("=") for s in lines]
    if any(key.strip() != KEY or not sep for key, sep, _ in keyed):
        return "", f"only one {KEY}=<token> line, no other"
    if len(keyed) > 1:
        return "", f"one line {KEY}=<token>, not {len(keyed)}"
    value = keyed[0][2].strip()
    if not value:
        return "", "no value"
    if value[0] in "\"'" or value[-1] in "\"'":
        return "", "the value in quotes — put it bare"
    if not VALUE.fullmatch(value):
        return "", "the value has more than letters, digits, '-' and '_'"
    return value, ""


def _text(path) -> str | None:
    """The file's text; None — no file. Raises OSError/UnicodeDecodeError when unreadable."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def problem_text(text: str) -> str:
    """``problem`` of a value about to be put: "" when fine."""
    return _parse(text)[1]


def problem(path=PATH) -> str:
    """ "" when the file holds the token as systemd and the runner both read it; else why not ("no file" too)."""
    try:
        text = _text(path)
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    return "no file" if text is None else _parse(text)[1]


def read(path=PATH) -> str:
    """The token, or "" (no file, unreadable, or anything ``problem`` refuses). Never raises."""
    try:
        text = _text(path)
    except (OSError, UnicodeDecodeError):
        return ""
    return "" if text is None else _parse(text)[0]


def env(path=PATH) -> dict:
    """``{KEY: token}`` for a process's environment, or {} — the process then uses the stored login."""
    token = read(path)
    return {KEY: token} if token else {}


def age_days(path=PATH, now=time.time):
    """Days since the token was put (the file's mtime), or None without the file."""
    try:
        return (now() - os.stat(path).st_mtime) / DAY
    except OSError:
        return None


def main(argv) -> int:
    """``check [path]``: exit 0 when the file is fine, 1 with the reason (installers call it as root)."""
    if not argv or argv[0] != "check" or len(argv) > 2:
        print("usage: claude_token.py check [path]", file=sys.stderr)
        return 2
    reason = problem(argv[1] if len(argv) == 2 else PATH)
    if reason:
        print(reason)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
