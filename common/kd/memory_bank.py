"""The Hindsight bank of a card's session.

A cabinet project may name a bank (``memory_bank``); the session host passes it to ``agent-launch --memory <bank>``
for the sessions of the project's cards only. The launcher, as the agent's user, calls ``main`` here: the role config
``~/.hindsight/coding-agent.json`` is copied to ``~/.hindsight/bank-<bank>.json`` with ``bankId`` set and without
``mapPathToBank`` (the plugin prefers a mapped folder to ``bankId``, and the agent's folder is mapped to its role
bank), and the launcher points ``HINDSIGHT_CONFIG`` at it. The plugin reads its config per process, so every hook and
the MCP server of the session read and write that one bank. A chat, an unbound project or a failure here keep the
role bank; the copy is rewritten at each launch (a new key in the role config reaches it).
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence
from urllib.parse import quote

# a bank id as the cabinet stores it and the host and the launcher take it (agent-launch repeats it in bash)
BANK = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.:-]{0,99}")
ROLE_CONFIG = Path(".hindsight") / "coding-agent.json"
# what picks a bank other than bankId: a session pinned to its project's bank has none of it
ROUTING = ("mapPathToBank", "bankIdTemplate", "dynamicBankId")


def bank_config(home: Path, bank: str) -> Optional[Path]:
    """The bank config of ``bank`` written beside the role config under ``home``; None when there is no role config
    (Hindsight is not set up for the agent). A bad bank or a role config that is not a JSON object is ValueError."""
    if not isinstance(bank, str) or not BANK.fullmatch(bank):
        raise ValueError(f"bad bank id {bank!r}")
    role = Path(home) / ROLE_CONFIG
    try:
        data = json.loads(role.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    if not isinstance(data, dict):
        raise ValueError(f"{role} is not a JSON object")
    pinned = {k: v for k, v in data.items() if k not in ROUTING}
    pinned["bankId"] = bank
    folder = role.parent
    os.chmod(folder, 0o700)  # the plugin's installer leaves it 0775
    target = folder / f"bank-{quote(bank, safe='')}.json"
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".bank-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(pinned, handle, ensure_ascii=False, indent=2)
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return target


def main(argv: Sequence[str]) -> int:
    """``memory_bank <bank> <home>`` under the agent's user (the launcher's ``$HOME``): prints the bank config's
    path (nothing without a role config) and exits 0; bad arguments 2; an unreadable or unwritable config 1 (the
    launcher keeps the role bank)."""
    if len(argv) != 2 or not BANK.fullmatch(argv[0]) or not os.path.isabs(argv[1]):
        print("usage: memory_bank <bank id> <home>", file=sys.stderr)
        return 2
    try:
        path = bank_config(Path(argv[1]), argv[0])
    except (OSError, ValueError) as exc:
        print(f"memory_bank: {exc.__class__.__name__}: {exc}", file=sys.stderr)
        return 1
    if path is not None:
        print(path.as_posix())
    return 0
