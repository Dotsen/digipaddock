"""The pipeline's mode and limits.

Two files. The repository's ``common/coordination/pipeline.json`` holds the limits of automatic continuations and of
an agent's questions to the people (each one a push: so many an hour across its cards, so many open on one card); it
ships with the code (who may close a card without the owner's acceptance is the project's ``done_by`` in the
cabinet). The mode is the root's: ``/etc/digipaddock/pipeline.json`` (root, 0644) says ``observe`` or ``enforce`` and since
when; no file is ``observe``. In ``observe`` the cabinet records outcomes, waits and problems and starts nothing;
in ``enforce`` it queues continuations and asks BOSS about problems. An agent's Done is held to the closing policy
(the project's ``done_by``, a chain stage's gate, nothing open) in both modes. No agent
writes either file: ``pipeline_setup.sh enable|disable`` does, after its readiness check.
"""

from dataclasses import dataclass
import json
import os
import sys
import tempfile
from typing import Optional

ETC = "/etc/digipaddock/pipeline.json"
DEFAULTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "coordination", "pipeline.json")
MODES = ("observe", "enforce")


@dataclass(frozen=True)
class Config:
    mode: str = "observe"
    since: str = ""
    window_s: int = 3600
    per_window: int = 6
    streak: int = 3
    # an agent's questions to the people, each a push: so many in a window across its cards, so many open on a card
    questions_window_s: int = 3600
    questions_per_window: int = 20
    questions_open: int = 5
    chain: bool = True  # chain plans (``kd.cabinet.stages``) for the plans approved from now on

    @property
    def enforcing(self) -> bool:
        return self.mode == "enforce"


def _int(value, low: int, high: int, what: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ValueError(f"pipeline: {what} must be {low}..{high}")
    return value


def _read(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ValueError(f"pipeline: {path}: {exc.strerror}") from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(f"pipeline: {path} is not JSON") from None
    if not isinstance(data, dict):
        raise ValueError(f"pipeline: {path} must be an object")
    return data


def _defaults(data: dict) -> dict:
    if set(data) - {"continuations", "questions"}:
        raise ValueError("pipeline: the defaults hold continuations and questions only")
    limits = data.get("continuations", {})
    if not isinstance(limits, dict) or set(limits) - {"windowMinutes", "perWindow", "streak"}:
        raise ValueError("pipeline: continuations are windowMinutes, perWindow and streak")
    asked = data.get("questions", {})
    if not isinstance(asked, dict) or set(asked) - {"windowMinutes", "perWindow", "openPerCard"}:
        raise ValueError("pipeline: questions are windowMinutes, perWindow and openPerCard")
    return {
        "window_s": _int(limits.get("windowMinutes", 60), 1, 24 * 60, "windowMinutes") * 60,
        "per_window": _int(limits.get("perWindow", 6), 1, 100, "perWindow"),
        "streak": _int(limits.get("streak", 3), 1, 100, "streak"),
        "questions_window_s": _int(asked.get("windowMinutes", 60), 1, 24 * 60, "questions.windowMinutes") * 60,
        "questions_per_window": _int(asked.get("perWindow", 20), 1, 1000, "questions.perWindow"),
        "questions_open": _int(asked.get("openPerCard", 5), 1, 100, "questions.openPerCard"),
    }


def _mode(data: dict) -> dict:
    if set(data) - {"mode", "since", "chain"}:
        raise ValueError("pipeline: the mode file holds mode, since and chain only")
    if data.get("mode") not in MODES:
        raise ValueError(f"pipeline: mode must be one of {', '.join(MODES)}")
    since = data.get("since", "")
    if not isinstance(since, str) or len(since) > 32:
        raise ValueError("pipeline: since must be a UTC stamp")
    chain = data.get("chain", True)
    if not isinstance(chain, bool):
        raise ValueError("pipeline: chain must be true or false")
    return {"mode": data["mode"], "since": since, "chain": chain}


def load(etc: str = ETC, defaults: str = DEFAULTS) -> Config:
    """The current config; a missing mode file is ``observe``, a broken one is an error."""
    base = _read(defaults)
    if base is None:
        raise ValueError(f"pipeline: {defaults} is missing")
    fields = _defaults(base)
    mode = _read(etc)
    if mode is not None:
        fields.update(_mode(mode))
    return Config(**fields)


class Cached:
    """The config re-read when a file changes; a broken file after a good one keeps the good one."""

    def __init__(self, etc: str = ETC, defaults: str = DEFAULTS):
        self.etc, self.defaults = etc, defaults
        self._stamp = None
        self._config = Config()

    def _stamps(self):
        out = []
        for path in (self.etc, self.defaults):
            try:
                st = os.stat(path)
                out.append((st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                out.append(None)
        return tuple(out)

    def get(self) -> Config:
        try:
            stamp = self._stamps()
        except OSError:
            return self._config
        if stamp != self._stamp:
            self._stamp = stamp
            try:
                self._config = load(self.etc, self.defaults)
            except ValueError as exc:
                print(f"{exc}; keeping the last good config", file=sys.stderr)
        return self._config


def write(mode: str, *, now: str, etc: str = ETC) -> None:
    """The root's switch: the mode and the moment it was set, written whole or not at all. The chain switch the
    file holds is kept: it is set by hand, and a change of the mode is no word about it."""
    data = _mode({"mode": mode, "since": now})
    try:
        data["chain"] = _mode(_read(etc) or {"mode": mode})["chain"]
    except ValueError:  # a broken file is replaced whole
        pass
    if data["chain"]:
        del data["chain"]  # the default is not written: the file stays as the setup script made it
    folder = os.path.dirname(etc) or "."
    fd, tmp = tempfile.mkstemp(prefix=".pipeline.", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, etc)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def main(argv, *, etc: str = ETC, out=None, err=None) -> int:
    """``write observe|enforce`` (root, pipeline_setup.sh) and ``show``; 0 — done, 1 — a file, 2 — usage."""
    from datetime import datetime, timezone

    out, err = out or sys.stdout, err or sys.stderr
    try:
        if len(argv) == 2 and argv[0] == "write" and argv[1] in MODES:
            write(argv[1], now=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), etc=etc)
            return 0
        if argv == ["show"]:
            config = load(etc)
            out.write(f"mode {config.mode} since {config.since or '-'}\n")
            return 0
    except (OSError, ValueError) as exc:
        print(f"pipeline: {exc}", file=err)
        return 1
    print("usage: python3 -m kd.pipeline_config write observe|enforce | show", file=err)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
