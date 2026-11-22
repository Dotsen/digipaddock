#!/usr/bin/python3 -I
"""Entry point of the dev executor: the forge channel under <prefix>-forge; the logic is in dev_agent/forge.py."""
import os
import sys

os.umask(0o077)
sys.stdin.reconfigure(encoding="utf-8", errors="strict")  # the comment's text; the channel service sets LANG=C.UTF-8
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from dev_agent.forge import main  # noqa: E402

sys.exit(main())
