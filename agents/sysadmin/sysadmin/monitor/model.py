"""The monitor's shared types and the loading of the thresholds config."""
import json
import re
from pathlib import Path
from typing import List, NamedTuple

LEVELS = {"ok": 0, "warn": 1, "crit": 2}


class Observation(NamedTuple):
    server: str
    check: str
    subject: str
    level: str  # ok | warn | crit
    value: str
    text: str

    @property
    def key(self) -> str:
        return f"{self.server}:{self.check}:{self.subject}"


class CheckResult(NamedTuple):
    """The result of one check. (server, check) is the source of the observations.

    ran=False: the check did not run; the observations are ignored,
    the source's open problems are left alone."""
    server: str
    check: str
    ran: bool
    observations: List[Observation]


class Event(NamedTuple):
    kind: str  # open | worse | close | fix_failed (what was marked "fixed" did not pass the recheck)
    problem_id: str
    key: str
    level: str
    server: str
    check: str
    subject: str
    value: str
    text: str
    opened_at: str
    at: str  # the time the event was made, ISO in the host's zone
    repeats: int


def _check_mem_mb(value) -> None:
    """mem_available_mb: {server: {"warn": MB, "crit": MB}}, 0 < crit < warn: a crooked threshold would silently never fire."""
    if not isinstance(value, dict):
        raise ValueError("mem_available_mb: ожидается {сервер: {warn, crit}}")
    for server, t in value.items():
        ok = isinstance(t, dict) and set(t) == {"warn", "crit"} and all(
            isinstance(t[k], (int, float)) and not isinstance(t[k], bool) for k in t)
        if not ok or not 0 < t["crit"] < t["warn"]:
            raise ValueError(f"mem_available_mb.{server}: нужны числа 0 < crit < warn (МБ), а не {t!r}")


# The application's tables the monitor reads: the names come from the deployment's config, and only such go into SQL.
APP_TABLES = (("heartbeat", "table"), ("monitoring", "consumers_table"))
_TABLE = re.compile(r"[A-Za-z0-9_]{1,64}")


def _check_tables(cfg) -> None:
    for section, key in APP_TABLES:
        value = cfg.get(section, {}).get(key)
        if not isinstance(value, str) or not _TABLE.fullmatch(value):
            raise ValueError(f"{section}.{key}: нужно имя таблицы (буквы, цифры, _), а не {value!r}")


def load_config(path) -> dict:
    cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    _check_mem_mb(cfg.get("mem_available_mb", {}))
    _check_tables(cfg)
    return cfg
