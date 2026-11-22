"""The cabinet's ru/en message catalogs, shared by the web (Paraglide) and the service.

Catalogs are flat JSON objects ``{"key": "text"}`` with ``{name}`` placeholders (inlang message format; a
``$schema`` key is allowed). The service stores server texts — system messages in a thread, push, digests —
as a key and parameters, and renders them in the reader's language with ``t`` or ``render``. A catalog spells
commands and card forms the repository's way; it is read with this host's names.
"""

import functools
import json
import re
from pathlib import Path
from typing import Dict, Mapping, Optional, Set

from kd import naming

DIR = Path(__file__).resolve().parents[2] / "cabinet" / "web" / "messages"
LANGUAGES = ("ru", "en")
DEFAULT = "en"  # the fallback catalog; the owner's language is host.json owner_language
# Parameters that are themselves catalog keys under a prefix: a decision's type, a risk level, why a diagnostic
# probe failed (R31).
NESTED = {"type": "decision.type.", "risk": "risk.", "category": "diag.category."}


class _Keep(dict):
    """A missing placeholder stays as it is instead of raising."""

    def __missing__(self, name):
        return "{" + name + "}"


@functools.lru_cache(maxsize=8)
def _load(directory: str) -> Dict[str, Dict[str, str]]:
    catalogs = {}
    for lang in LANGUAGES:
        with open(Path(directory) / f"{lang}.json", encoding="utf-8") as fh:
            data = json.load(fh)
        catalogs[lang] = {k: naming.rewrite(str(v)) for k, v in data.items() if not k.startswith("$")}
    return catalogs


def load(dir=DIR) -> Dict[str, Dict[str, str]]:
    """``{lang: {key: text}}`` for every language; cached per directory."""
    return _load(str(dir))


def languages() -> tuple:
    return LANGUAGES


def missing(dir=DIR) -> Dict[str, Set[str]]:
    """Keys some language has and another lacks: ``{lang: {key, ...}}``, only languages with gaps."""
    catalogs = load(dir)
    every = set().union(*(set(c) for c in catalogs.values()))
    gaps = {lang: every - set(c) for lang, c in catalogs.items()}
    return {lang: keys for lang, keys in gaps.items() if keys}


def t(lang: str, key: str, **params) -> str:
    """The text of ``key`` in ``lang`` (an unknown language falls back to ru, an unknown key is returned as is);
    extra parameters are ignored and missing ones stay as ``{name}``."""
    catalogs = load()
    text = catalogs.get(lang, catalogs[DEFAULT]).get(key)
    if text is None:
        text = catalogs[DEFAULT].get(key, key)
    return text.format_map(_Keep({k: str(v) for k, v in params.items()}))


_EXIT = re.compile(r"exit (-?\d+)")


def reason(lang: str, code: str) -> str:
    """Why a run or a session failed, in words — the service's twin of the web's ``reasonText``: the host and the
    cabinet keep machine codes (``host_restart``, ``exit 143``, ``refused: …``), a person reads ``run.reason.*``."""
    code = (code or "").strip()
    exit_code = _EXIT.fullmatch(code)
    if exit_code:
        return t(lang, "run.reason.exit", code=exit_code.group(1))
    key = "run.reason." + re.sub(r"[^a-z0-9_]+", "_", code.split(":", 1)[0].strip().lower())
    known = key != "run.reason.exit" and key in load()[DEFAULT]
    return t(lang, key if known else "run.reason.other")


def render(lang: str, payload: Mapping) -> Optional[str]:
    """The text of a stored ``{"key", "params"}`` payload, or None when it carries no key. Parameters named in
    ``NESTED`` are catalog keys themselves (``type: admin_plan`` -> the type's name in ``lang``)."""
    key = payload.get("key") if isinstance(payload, Mapping) else None
    if not isinstance(key, str):
        return None
    params = dict(payload.get("params") or {})
    for name, prefix in NESTED.items():
        if isinstance(params.get(name), str):
            params[name] = t(lang, prefix + params[name])
    return t(lang, key, **params)
