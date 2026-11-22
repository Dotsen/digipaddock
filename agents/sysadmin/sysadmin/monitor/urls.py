"""The addresses of cabinet pages for the monitor (P-n is a cabinet page, there are no /monitor/ pages) and the forms
of identifiers in them. No dependencies: deliver imports the module at load. Compare with fullmatch only: "$" would
let a trailing \\n through. The cabinet's address is origin from the host's cabinet.json, without it — cabinet_origin
from host/host.json of the release tree; with neither — the links are relative."""
import json
import os
import re
from pathlib import Path

CABINET_JSON = "/etc/digipaddock/cabinet.json"
HOST_JSON = str(Path(__file__).resolve().parents[4] / "host" / "host.json")


def _origin(path, key):
    try:
        with open(path, encoding="utf-8") as f:
            value = json.load(f).get(key)
    except (OSError, ValueError, AttributeError):
        return ""
    return value.rstrip("/") if isinstance(value, str) and value.startswith("https://") else ""


def board_url() -> str:
    return _origin(CABINET_JSON, "origin") or _origin(os.environ.get("KD_HOST_JSON") or HOST_JSON, "cabinet_origin")
PID_RE = re.compile(r"P-[0-9]{1,7}")


def problem_url(pid) -> str:
    """The address of an incident's page in the cabinet; not a P-n number — empty (no link)."""
    return f"{board_url()}/incidents/{pid}" if isinstance(pid, str) and PID_RE.fullmatch(pid) else ""


DECISION_RE = re.compile(r"[0-9a-f]{32}")


def decision_url(decision_id) -> str:
    """The address of a decision in the cabinet (monitor_trust); not a decision id — empty."""
    return f"{board_url()}/decisions/{decision_id}" if isinstance(decision_id, str) and DECISION_RE.fullmatch(
        decision_id) else ""

