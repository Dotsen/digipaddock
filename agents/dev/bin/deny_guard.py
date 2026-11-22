#!/usr/bin/python3 -I
"""PreToolUse hook of the dev agent; the logic is in dev_agent/guard.py. Any failure exits 2 (block)."""
import os
import sys

try:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    from dev_agent.guard import main

    code = main()
except Exception as exc:  # noqa: BLE001 — even a broken import must block the call
    print(f"⛔ deny_guard не загрузился: {exc.__class__.__name__}", file=sys.stderr)
    code = 2
sys.exit(code)
