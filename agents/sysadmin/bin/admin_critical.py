#!/usr/bin/python3 -I
"""Executor entry of kd-admin-critical: the exec channel as <prefix>-exec; logic in sysadmin/admin_critical.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from sysadmin.admin_critical import main  # noqa: E402
from sysadmin.execute import install_signal_handlers  # noqa: E402

install_signal_handlers()
sys.exit(main())
