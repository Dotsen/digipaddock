"""Every wake of an agent goes through here: an agent on the session host gets a command in the
cabinet's outbox (``command.enqueue``). there is no other way — an agent that is not on the host is not
woken (``absent``); the session host is the only runtime (the panels are gone).

Callers keep their fixed texts. The text becomes one line of at most 2000 characters, and the same text for the
same agent and card within a minute is one command (its key), unless the caller names a key.
"""

import hashlib
import re
import sys
import time

from kd import session_config

TEXT_MAX = 2000
_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def one_line(text: str) -> str:
    line = " ".join(_CTRL.sub(" ", _ANSI.sub("", str(text))).split())
    return line[:TEXT_MAX]


def wake(agent: str, text: str, *, card: str = "", key: str = "", urgent: bool = False, space: str = "chat",
         config=None, config_path: str = session_config.CONFIG, client=None, now=time.time) -> str:
    """sent (the command is queued) | absent (the agent is not on the session host) | error. ``urgent`` is the
    callers' word for a wake that must reach a working agent: a command always does. ``space`` — the agent's own
    thread of a wake without a card: its chat, or "Monitor" (the monitor's wakes)."""
    try:
        config = config if config is not None else session_config.load(config_path)
    except ValueError as exc:
        print(f"kd-wake: {exc}", file=sys.stderr)
        return "error"
    if not config.hosted(agent):
        print(f"kd-wake: {agent} is not on the session host", file=sys.stderr)
        return "absent"
    from kd.cabinet import client as cabinet_client

    line = one_line(text)
    if not key:
        minute = int(now() // 60)
        key = hashlib.sha256(f"{agent}|{card}|{line}|{minute}".encode("utf-8")).hexdigest()[:32]
    client = client or cabinet_client.Client()
    request = {"op": "command.enqueue", "agent": agent, "text": line, "card": card, "key": key}
    if space != "chat":
        request["space"] = space
    try:
        client.call(request)
    except cabinet_client.CabinetError as exc:
        print(f"kd-wake: {agent}: the cabinet did not take the command ({exc.kind}: {exc})", file=sys.stderr)
        return "error"
    return "sent"
