"""The release queue's correlation keys and root-owned result files."""

import hashlib
import json
import os
import re

from kd import atomic_file

ID = re.compile(r"[0-9a-f]{64}")
ACTIVE = "active.json"
LATEST = "result.json"
TERMINAL = frozenset(("refused", "degraded", "healthy"))


def forge_id(project, repo, pr, commit):
    """One attempt for a Forgejo merge, independent of when the caller retries."""
    value = json.dumps([project, repo.lower(), pr, commit], separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def result_name(request_id):
    if not isinstance(request_id, str) or not ID.fullmatch(request_id):
        raise ValueError("invalid release request ID")
    return f"result-{request_id}.json"


def read(directory, name):
    try:
        with open(os.path.join(directory, name), encoding="utf-8") as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{name}: expected a JSON object")
    return value


def write(directory, name, value, mode=0o644):
    data = json.dumps(value, ensure_ascii=False).encode("utf-8")
    atomic_file.replace(os.path.join(directory, name), data, mode)


def terminal(directory, request_id):
    result = read(directory, result_name(request_id))
    if result.get("request_id") == request_id and result.get("state") in TERMINAL:
        return result
    return {}
