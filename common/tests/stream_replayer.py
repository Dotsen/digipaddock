"""A stand-in for ``claude -p --input-format stream-json``: for each input line it prints the next turn of a
fixture (the events up to and including the next ``result``); EOF on stdin ends it with the given exit code.
The input ``crash`` exits 3 at once; ``hang`` prints nothing until EOF."""

import json
import sys


def main(fixture: str, code: int) -> int:
    with open(fixture, encoding="utf-8") as f:
        events = [line.rstrip("\n") for line in f if line.strip()]
    turns, current = [], []
    for raw in events:
        current.append(raw)
        try:
            if json.loads(raw).get("type") == "result":
                turns.append(current)
                current = []
        except (ValueError, AttributeError):
            pass
    if current:
        turns.append(current)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            text = json.loads(line)["message"]["content"]
        except (ValueError, KeyError, TypeError):
            text = ""
        if text == "crash" or not turns:
            return 3
        if text == "hang":  # no output at all: the host never sees the turn start
            for _ in sys.stdin:
                pass
            return code
        for raw in turns.pop(0):
            sys.stdout.write(raw + "\n")
        sys.stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 0))
