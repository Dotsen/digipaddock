"""Finite credential targets shared by the root writer and cabinet."""

import os
import time
from datetime import datetime, timezone

from kd import claude_token, llm_models

SUBSCRIPTION = "claude-subscription"


def known(target: str) -> bool:
    return target == SUBSCRIPTION or target in llm_models.providers()


def secret_name(target: str) -> str:
    if not known(target):
        raise ValueError("unknown credential target")
    return "claude-token" if target == SUBSCRIPTION else "llm-" + target


def variable(target: str) -> str:
    return claude_token.KEY if target == SUBSCRIPTION else llm_models.secret_key(target)


def subscription_status(path=claude_token.PATH, *, now=time.time) -> dict:
    """Metadata only. Expiry is an estimate from mtime, not a claim about the account's expiry."""
    try:
        modified = os.stat(path).st_mtime
    except OSError:
        return {
            "id": SUBSCRIPTION,
            "key": False,
            "age_days": None,
            "expires": "",
            "days_left": None,
            "warning": "missing",
            "estimated": True,
        }
    age = max(0, int((now() - modified) / claude_token.DAY))
    left = claude_token.LIFETIME_DAYS - age
    return {
        "id": SUBSCRIPTION,
        "key": True,
        "age_days": age,
        "days_left": left,
        "expires": datetime.fromtimestamp(
            modified + claude_token.LIFETIME_DAYS * claude_token.DAY, timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "warning": "expired"
        if left <= 0
        else "week"
        if left <= 7
        else "month"
        if left <= 30
        else "",
        "estimated": True,
    }
