"""A role's session context limit: the size, in tokens, at which Claude Code compacts a conversation.

The owner sets one value per agent role in the cabinet (``kd.cabinet.context_limits``; 400000 by default, or no
limit). It is never written into a role's installed settings: every launch carries the value in force, so a change
applies from the role's next process without a reinstall. A session's delivery names it as ``context_limit``
(``kd.session_host.protocol``) and the session host starts the process with ``CLAUDE_CODE_AUTO_COMPACT_WINDOW``
(``environment``); a worker job's definition carries it the same way and the runner sets the same variable. No limit
sends nothing: Claude Code keeps its own window, the model's.

Nothing here imports the cabinet: the session host and the worker runner read this module too.
"""

ENV = "CLAUDE_CODE_AUTO_COMPACT_WINDOW"  # Claude Code's auto-compact window, in tokens
MIN, MAX = 100_000, 1_000_000  # the range Claude Code accepts for the window
DEFAULT = 400_000  # a role the owner never set


def check(value) -> int:
    """A limit in tokens, ``MIN`` to ``MAX``; ValueError otherwise."""
    if isinstance(value, bool) or not isinstance(value, int) or not MIN <= value <= MAX:
        raise ValueError(
            f"a context limit is a whole number of tokens from {MIN} to {MAX}"
        )
    return value


def environment(limit: int) -> dict:
    """The variable a launch takes for ``limit``; ``{}`` for 0 — no limit."""
    return {ENV: str(check(limit))} if limit else {}
