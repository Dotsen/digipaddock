"""The policy baseline: what the sysadmin and dev policies granted on 29.10.2026, frozen.

fixtures/policy/baseline.json holds the effective view (kd.policy.effective_view: the managed floor plus the agent's
settings, sandbox and grants) of each generated policy as compiled that day, and the executors its channels reach. A
compiled policy may narrow it freely; whatever widens it is listed under ``widenings`` with its reason, after review.
It replaced the F03 baseline (the hand-written settings before the manifests), whose differences file had grown with
every removal since.
"""
from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Any, Mapping

from kd.policy import compare_views, effective_view

FILE = Path(__file__).parent / "fixtures" / "policy" / "baseline.json"
AGENTS = ("sysadmin", "dev")


def view(policy: Mapping[str, Any], floor: Mapping[str, Any]) -> dict[str, Any]:
    return effective_view(
        floor=floor,
        settings=policy["settings"],
        launch_settings=policy["launch_settings"],
        bash_approval=policy["bash_approval"],
        acl={f"{a['access']}:{a['resource']}" for a in policy["acl"]},
    )


def channels(policy: Mapping[str, Any]) -> set[str]:
    """``<channel>:<user>:<program>`` per executor, ``<channel>:<user>:<command> <form>`` per command form."""
    items = set()
    for channel in policy["channels"]:
        prefix = f"{channel['key']}:{channel['run_as']}"
        items |= {f"{prefix}:{PurePosixPath(p).name}" for p in channel["programs"]}
        for command, entry in channel["commands"].items():
            items |= {f"{prefix}:{command} {form}" for form in entry["subcommands"] or ["*"]}
    return items


def freeze(policy: Mapping[str, Any], floor: Mapping[str, Any]) -> dict[str, Any]:
    def plain(value: Any) -> Any:
        if isinstance(value, set):
            return sorted(value)
        if isinstance(value, dict):
            return {key: plain(item) for key, item in value.items()}
        return value

    return {"view": plain(view(policy, floor)), "channels": sorted(channels(policy))}


def _thaw(frozen: Mapping[str, Any]) -> dict[str, Any]:
    thawed = {key: set(value) for key, value in frozen.items() if key != "sandbox"}
    box = frozen["sandbox"]
    thawed["sandbox"] = None if box is None else {key: set(value) for key, value in box.items()}
    return thawed


def load() -> dict[str, Any]:
    return json.loads(FILE.read_text("utf-8"))


def widenings(name: str, policy: Mapping[str, Any], floor: Mapping[str, Any]) -> list[str]:
    """What ``policy`` grants beyond the frozen baseline of agent ``name``."""
    frozen = load()["agents"][name]
    found = [
        f"{d.change} {d.item}" for d in compare_views(_thaw(frozen["view"]), view(policy, floor)) if d.widens
    ]
    reached = set(frozen["channels"])
    for item in sorted(channels(policy) - reached):
        command = item.rsplit(" ", 1)[0]
        if " " not in item or f"{command} *" not in reached:
            found.append(f"added channel:{item}")
    return sorted(found)
