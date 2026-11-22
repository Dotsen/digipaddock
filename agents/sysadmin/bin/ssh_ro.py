#!/usr/bin/python3 -I
"""ssh-ro <server|server1,server2|all> '<command>' — a read through the agent-ro-gate gateway. /usr/local/bin/ssh-ro → here.
One server — exec ssh (the gateway's exit code as it is); several — in parallel, the output in order under "=== server" headings."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
from sysadmin import inventory, ro_many  # noqa: E402

args = sys.argv[1:]
try:
    targets = inventory.ro_targets(args)
except inventory.InventoryError as exc:
    print(f"ssh-ro: {exc}", file=sys.stderr)
    sys.exit(2)
if args[0] != "all" and "," not in args[0]:
    argv = targets[0][1]
    os.execv(argv[0], argv)
sys.exit(ro_many.run_many(targets, sys.stdout.buffer))
