#!/usr/bin/env python3
"""Stand-ins for the overlay files no example can be (the ci profile of scripts/check.py).

overlay.py init copies the examples; a release also needs the forge's host key and the deployment's own
closed scanner list, which only a real overlay carries. CI checks an overlay made from the examples, so
it writes obvious stand-ins for them. The list must not repeat the example's literals.
"""

import sys
from pathlib import Path

STAND_INS = {
    "common/install/forgejo_known_hosts": "forge.example.test ssh-ed25519 stand-in\n",
    "host/private_scan.json": '{"literals": ["ci-stand-in"]}\n',
}


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: overlay_stand_ins.py <overlay dir>", file=sys.stderr)
        return 2
    for rel, text in STAND_INS.items():
        target = Path(args[0], rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
        print(f"stand-in {rel}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
