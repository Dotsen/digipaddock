"""Publish the sysadmin's existing monitor replies on their incidents.

An explicit incident reference in a trusted final text links evidence, never an instruction or an approval.
User messages, tool output and private chats cannot become an incident report.
"""

import json
import re

from kd import monitor_settings as ms

REFERENCES = re.compile(r"(?<![A-Za-z0-9_-])P-([1-9][0-9]{0,17})(?![A-Za-z0-9_-])")
# a group named by its range; a reversed or a wider one is only its two ends
RANGES = re.compile(r"(?<![A-Za-z0-9_-])P-([1-9][0-9]{0,17})\.\.P-([1-9][0-9]{0,17})(?![A-Za-z0-9_-])")
RANGE_MAX = 500


def numbers(body: str) -> list[int]:
    found = {int(n) for n in REFERENCES.findall(body)}
    for a, b in RANGES.findall(body):
        if int(a) <= int(b) < int(a) + RANGE_MAX:
            found.update(range(int(a), int(b) + 1))
    return sorted(found)


def summary(body: str, limit: int = 360) -> str:
    text = re.sub(r"\(\s*P-[0-9]+:\s*https?://[^)\s]+\s*\)", "", body)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"[*_`#>]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _investigated(said: list[int], woken: list[int], whole: bool = True) -> list[int]:
    """The incidents a reply reports on. Woken by the monitor's event of some P-n, the reply investigates
    those: the ones of them it names, or all of them when it names none — another P-n it lists in passing ("also
    open: …") is no report of that one. Woken without a P-n (the owner's word in "Monitor"), the ones it names.
    A wake cut at the command's limit lost the incidents past the cut, so its list bounds nothing."""
    if not woken:
        return said
    if not whole:
        return said or woken
    return [n for n in said if n in woken] or woken


def link_message(
    s, thread: int, seq: int, author: str, kind: str, body: str
) -> list[int]:
    if author != "agent:sysadmin" or kind != "text":
        return []
    found = s._conn.execute(
        "SELECT m.id,m.created,m.payload FROM messages m JOIN threads t ON t.id=m.thread"
        " WHERE m.thread=? AND m.seq=? AND t.kind='agent_monitor' AND t.agent='sysadmin'",
        (thread, seq),
    ).fetchone()
    if not found:
        return []
    command_id = json.loads(found["payload"]).get("command")
    command = s._conn.execute(
        "SELECT text FROM commands WHERE id=? AND thread=? AND agent='sysadmin'",
        (command_id, thread),
    ).fetchone() if command_id else None
    wake = command[0] if command else ""
    if wake.startswith(ms.SUMMARY_HEADS):
        return []  # the monitor's summary retells what is open, it investigates none of it
    from kd.cabinet.sessions_api import COMMAND_TEXT_MAX

    references = _investigated(numbers(body), numbers(wake), len(wake) < COMMAND_TEXT_MAX)
    linked = []
    for n in references:
        row = s._conn.execute(
            "SELECT opened_at FROM incidents WHERE n=?", (n,)
        ).fetchone()
        if row and row[0] <= found["created"]:
            s._conn.execute(
                "INSERT OR IGNORE INTO incident_reports VALUES (?,?)", (n, found["id"])
            )
            linked.append(n)
    return linked


def latest(s, n: int) -> dict | None:
    row = s._conn.execute(
        "SELECT m.body,m.created,m.author,m.thread,m.seq FROM incident_reports r"
        " JOIN messages m ON m.id=r.message LEFT JOIN commands c ON c.id=json_extract(m.payload,'$.command')"
        " WHERE r.incident=? AND COALESCE(c.state,'done')='done' ORDER BY m.created DESC,m.id DESC LIMIT 1",
        (n,),
    ).fetchone()
    return {**dict(row), "summary": summary(row["body"])} if row else None


def investigation(s, row: dict) -> dict:
    report = latest(s, row["n"])
    command = None
    for candidate in s._conn.execute(
        "SELECT c.id,c.state,c.error,c.created,c.text FROM commands c JOIN threads t ON t.id=c.thread"
        " WHERE c.agent='sysadmin' AND t.kind='agent_monitor' AND c.created>=? AND c.text LIKE ?"
        " ORDER BY c.created DESC,c.rowid DESC LIMIT 20",
        (row["opened_at"], f"%P-{row['n']}%"),
    ):
        if row["n"] in numbers(candidate["text"]) and not candidate["text"].startswith(ms.SUMMARY_HEADS):
            command = dict(candidate)
            break
    state = "reviewed" if report else "waiting"
    if command and (not report or command["created"] > report["created"]):
        state = {
            "queued": "queued",
            "delivered": "queued",
            "started": "running",
            "failed": "failed",
            "cancelled": "failed",
            "done": "awaiting_report",
        }.get(command["state"], "waiting")
    return {
        "state": state,
        "report": report,
        "detail": command["error"] if command else "",
    }
