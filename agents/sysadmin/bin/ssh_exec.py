#!/usr/bin/python3 -I
"""Entry point of the executor: the exec channel under <prefix>-exec; the logic is in sysadmin/execute.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from sysadmin.execute import install_signal_handlers, main  # noqa: E402

install_signal_handlers()
sys.exit(main())
