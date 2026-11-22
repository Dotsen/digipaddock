"""KEY=value files of secrets (/etc/digipaddock/*.env): blank lines and «#» comments are skipped, keys and values
stripped. The callers decide what a missing key means."""
from pathlib import Path


def load_env(path) -> dict:
    env = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.lstrip().startswith("#"):
            env[key.strip()] = value.strip()
    return env
