"""The sysadmin gate's secret masks for any text the cabinet shows (a tool's output in a session's turn).

The gate (``agents/sysadmin/gate/agent_ro_gate.py``) is the one place that knows how a secret looks in a command's
output: PEM keys, credentials in URLs, bearer and basic auth, bot tokens, ``mysql -p``, ``-p``/``--password``,
``-u user:pass``, and name/value pairs (``DB_PASSWORD=…``, PHP ``define('DB_PASSWORD', '…')``). It is loaded from
its file once, not copied: one set of rules. Without it nothing is masked, so ``text`` returns None and the caller
shows nothing.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Callable, Dict, Optional

GATE = Path(__file__).resolve().parents[2] / "agents" / "sysadmin" / "gate" / "agent_ro_gate.py"
_loaded: Dict[str, Optional[Callable[[bytes], bytes]]] = {}


def _gate() -> Optional[Callable[[bytes], bytes]]:
    path = str(GATE)
    if path not in _loaded:
        try:
            spec = importlib.util.spec_from_file_location("kd_redact_gate", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            _loaded[path] = module.redact
        except Exception as exc:  # noqa: BLE001 — any failure means: no masks, show nothing
            sys.stderr.write(f"kd.redact: the gate's masks are unavailable ({type(exc).__name__}: {exc})\n")
            _loaded[path] = None
    return _loaded[path]


def text(value: str) -> Optional[str]:
    """``value`` with the gate's masks applied, or None when the gate cannot be loaded."""
    redact = _gate()
    if redact is None:
        return None
    return redact(value.encode("utf-8", "replace")).decode("utf-8", "replace")
