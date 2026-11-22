"""Recognized critical commands: a plan step that holds one is refused at proposal and
on the target.

The classifier itself lives in kd.exec_policy — the one rules module the exec gate also runs on a target, where the
deployment's site units come from gate.json. Here they come from the catalog's critical_units; an
unreadable catalog makes every unit's stop critical.
"""

import sys
from pathlib import Path
from typing import Optional, Tuple

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "common")
)  # …/DigiPaddock/common — as in denylist.py
from kd import admin_operations, exec_policy  # noqa: E402
from kd.exec_policy import DATABASE, DISKS, NETWORK, REMOVAL, SHUTDOWN  # noqa: E402,F401


def site_units() -> Tuple[str, ...]:
    """The deployment's own site units (catalog critical_units); an unreadable catalog makes every unit
    stop critical."""
    try:
        return admin_operations.load_catalog().critical_units
    except admin_operations.CatalogError:
        return exec_policy.ALL_UNITS


def classify(command: str) -> Optional[str]:
    """The critical group of the first recognized segment, or None for an ordinary command."""
    return exec_policy.classify(command, site_units())
