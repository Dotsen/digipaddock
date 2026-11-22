"""Models of other providers: the catalog of provider kinds and the checks every runtime shares.

A card (or its project) may name a cabinet model — a slug the owner chose — that resolves to a provider kind of the
catalog ``common/coordination/llm-providers.json`` and the model id the provider knows (``upstream``). The
session host and the worker runner start Claude Code with the kind's Anthropic-compatible ``base_url`` and the
owner's key for that kind (the catalog secret ``llm-<kind>``: one bare ``ANTHROPIC_AUTH_TOKEN=<key>`` line in
``/etc/digipaddock/llm/<kind>.env``, root 0600), instead of the host's Claude token. The slug is what the usage
ledger counts under; its prices live in the cabinet (``llm_models``).

a model of the Claude subscription is named the same way, by a reference ``claude:<alias>`` of the catalog
``common/coordination/claude-models.json`` (never a cabinet slug: a slug has no colon). Its spec's kind is
``subscription`` — no base URL, no key of its own: the runtimes start it on the host's Claude token with ``--model
<alias>`` only, and its usage is counted under the names Claude Code reports, as a session without a model.

Nothing here imports the cabinet: the host, the runner and ``kd-secret`` read this module too.
"""

import json
import os
import re
from pathlib import Path
from typing import Mapping

from kd.manifest import EFFORTS, _MODEL

PROVIDERS_PATH = Path(__file__).resolve().parents[2] / "common" / "coordination" / "llm-providers.json"
CLAUDE_MODELS_PATH = PROVIDERS_PATH.with_name("claude-models.json")
SUBSCRIPTION = "subscription"  # the kind of a subscription model's spec: not a provider of the catalog
CLAUDE_REF = re.compile(r"claude:[a-z][a-z0-9.-]{0,47}")
TITLE_MAX = 120
ENV_DIR = "/etc/digipaddock/llm"
SECRET_KEY = "ANTHROPIC_AUTH_TOKEN"
SPEC_FIELDS = ("id", "provider", "upstream", "base_url")
# the slug of a cabinet model: what the usage ledger accepts as a model name (kd.cabinet.usage.MODEL)
SLUG = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
# the model id a provider knows: openai/gpt-5, accounts/fireworks/models/x, deepseek-v4-pro[1m]
UPSTREAM = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}")
BASE_URL = re.compile(r"https://[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]{1,5})?(?:/[A-Za-z0-9._~/-]*)?")
BASE_URL_MAX = 200
_SECRET_VALUE = re.compile(r"[A-Za-z0-9_./+=-]+")
# every alias Claude Code may pick, so one process never spreads over several models
_ALIASES = ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL", "ANTHROPIC_DEFAULT_FABLE_MODEL", "CLAUDE_CODE_SUBAGENT_MODEL")
_PROVIDER_FIELDS = {"title", "base_url", "secret", "env", "caching", "hint"}
_PROVIDER_OPTIONAL = {"auth_env", "protocol"}
OPENAI_BRIDGE = "http://127.0.0.1:17841"

_cache = {}


def providers(path=None) -> dict:
    """The catalog's kinds: kind → ``{title, base_url, secret, env, caching, hint}``; ValueError on a bad file."""
    path = str(path or PROVIDERS_PATH)
    if path in _cache:
        return _cache[path]
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: unreadable ({exc.__class__.__name__})") from None
    kinds = data.get("providers") if isinstance(data, Mapping) else None
    if not isinstance(kinds, Mapping) or not kinds:
        raise ValueError(f"{path}: providers must be a non-empty object")
    out = {}
    for kind, entry in kinds.items():
        if not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", str(kind)) or not isinstance(entry, Mapping) \
                or not _PROVIDER_FIELDS <= set(entry) or set(entry) - _PROVIDER_FIELDS - _PROVIDER_OPTIONAL:
            raise ValueError(f"{path}: provider {kind!r} must have exactly {sorted(_PROVIDER_FIELDS)}")
        if not isinstance(entry["env"], Mapping) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in entry["env"].items()):
            raise ValueError(f"{path}: provider {kind!r}: env is an object of strings")
        if entry["base_url"] != "" and not _base_url_ok(entry["base_url"]):
            raise ValueError(f"{path}: provider {kind!r}: base_url must be https://… or empty")
        if entry.get("auth_env", SECRET_KEY) not in (SECRET_KEY, "ANTHROPIC_API_KEY"):
            raise ValueError("unsupported provider credential variable")
        if entry.get("protocol", "anthropic") not in ("anthropic", "openai-responses"):
            raise ValueError("unsupported provider protocol")
        out[kind] = {**entry, "env": dict(entry["env"])}
    _cache[path] = out
    return out


def claude_models(path=None) -> dict:
    """The subscription's models: reference → ``{id, upstream, title}``, in the catalog's order; ValueError on a
    bad file. ``upstream`` is what ``claude --model`` takes: an alias or a full Claude id (``kd.manifest``)."""
    path = str(path or CLAUDE_MODELS_PATH)
    if path in _cache:
        return _cache[path]
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"{path}: unreadable ({exc.__class__.__name__})") from None
    models = data.get("models") if isinstance(data, Mapping) else None
    if not isinstance(models, list) or not models:
        raise ValueError(f"{path}: models must be a non-empty list")
    out = {}
    for entry in models:
        if not isinstance(entry, Mapping) or set(entry) != {"id", "upstream", "title"}:
            raise ValueError(f"{path}: a model has exactly id, upstream and title")
        ref, upstream, title = entry["id"], entry["upstream"], entry["title"]
        if not is_ref(ref) or ref in out:
            raise ValueError(f"{path}: a model's id is claude:<alias>, once")
        if not isinstance(upstream, str) or not _MODEL.fullmatch(upstream):
            raise ValueError(f"{path}: {ref}: upstream is a Claude alias or model id")
        if not isinstance(title, str) or not title.strip() or len(title) > TITLE_MAX:
            raise ValueError(f"{path}: {ref}: title is 1 to {TITLE_MAX} characters")
        out[ref] = {"id": ref, "upstream": upstream, "title": title.strip()}
    _cache[path] = out
    return out


def served(upstream, names) -> str:
    """Which of ``names`` — the models a run reported, in the caller's order — is the one ``upstream`` names
    : an alias is one of the name's ``-`` parts (``opus`` in ``claude-opus-5-5``), a full id its beginning
    (``claude-opus-4-1`` in ``claude-opus-4-1-20250805``). ``""`` when none is: a run may report only a smaller
    model Claude Code used on the side."""
    upstream = str(upstream)
    for name in names:
        if isinstance(name, str) and upstream and (upstream in name.split("-") or name.startswith(upstream)):
            return name
    return ""


def is_ref(value) -> bool:
    """Whether ``value`` has the shape of a subscription model's reference (the catalog may not hold it)."""
    return isinstance(value, str) and CLAUDE_REF.fullmatch(value) is not None


def subscription_spec(ref, path=None) -> dict:
    """The spec of a subscription model of the catalog: ``{id, provider: subscription, upstream, base_url: ""}``."""
    found = claude_models(path).get(ref) if is_ref(ref) else None
    if found is None:
        raise ValueError(f"unknown subscription model {str(ref)[:64]!r}")
    return {"id": found["id"], "provider": SUBSCRIPTION, "upstream": found["upstream"], "base_url": ""}


def is_subscription(spec) -> bool:
    """A spec of the Claude subscription: the host's own credential, no provider environment."""
    return isinstance(spec, Mapping) and spec.get("provider") == SUBSCRIPTION


def _base_url_ok(value) -> bool:
    return isinstance(value, str) and len(value) <= BASE_URL_MAX and BASE_URL.fullmatch(value) is not None


def check_slug(value) -> str:
    """A cabinet model's slug: the ledger's model-name shape, never the name of a Claude model or alias
    (``kd.manifest._MODEL``) — the cabinet's prices would shadow the real model's."""
    if not isinstance(value, str) or not SLUG.fullmatch(value):
        raise ValueError("a model id is 1 to 64 lower-case letters, digits, '.', '_' or '-', not starting with a dot")
    if _MODEL.fullmatch(value):
        raise ValueError(f"{value!r} is the name of a Claude model or alias; choose another id")
    return value


def check_upstream(value) -> str:
    """The model id the provider knows, as the owner typed it (``[1m]`` and all)."""
    if not isinstance(value, str) or not UPSTREAM.fullmatch(value):
        raise ValueError("the provider's model id is 1 to 128 letters, digits, '.', '_', ':', '/', '[', ']' or '-',"
                         " starting with a letter or a digit")
    return value


def check_effort(value) -> str:
    """One of Claude Code's effort levels, or ``""`` for «as the agent's»."""
    if value == "":
        return ""
    if not isinstance(value, str) or value not in EFFORTS:
        raise ValueError("effort is one of " + ", ".join(EFFORTS) + ", or empty")
    return value


def check_spec(spec, path=None, claude_path=None) -> dict:
    """Exactly ``{id, provider, upstream, base_url}``: the kind from the catalog, ``base_url`` only for ``custom``;
    a subscription model's spec is exactly what its catalog gives (``subscription_spec``)."""
    if not isinstance(spec, Mapping) or set(spec) != set(SPEC_FIELDS):
        raise ValueError("a model spec has exactly the fields " + ", ".join(SPEC_FIELDS))
    if spec["provider"] == SUBSCRIPTION:
        if dict(spec) != subscription_spec(spec["id"], claude_path):
            raise ValueError("a subscription model's spec is the catalog's: its alias as upstream, no base URL")
        return dict(spec)
    kinds = providers(path)
    if not isinstance(spec["provider"], str) or spec["provider"] not in kinds:
        raise ValueError(f"unknown provider {str(spec['provider'])[:40]!r}; the catalog has " + ", ".join(kinds))
    out = {"id": check_slug(spec["id"]), "provider": spec["provider"], "upstream": check_upstream(spec["upstream"])}
    base_url = spec["base_url"]
    if kinds[spec["provider"]]["base_url"]:
        if base_url != "":
            raise ValueError(f"provider {spec['provider']} has its own base URL; base_url must be empty")
    elif not _base_url_ok(base_url) or not base_url:
        raise ValueError("a custom provider's base_url is an https:// URL of at most 200 characters")
    out["base_url"] = base_url
    return out


def env_file(provider, env_dir=ENV_DIR, path=None) -> str:
    """The root-only file with the kind's key."""
    if provider not in providers(path):
        raise ValueError(f"unknown provider {str(provider)[:40]!r}")
    return f"{env_dir}/{provider}.env"


def key_present(provider, env_dir=ENV_DIR, path=None) -> bool:
    """Whether the kind's key file exists (its content is root's: the cabinet sees only the name)."""
    return os.path.isfile(env_file(provider, env_dir, path))


def environment(spec: Mapping, path=None) -> dict:
    """The non-secret variables of a process on ``spec``: the base URL, every model alias as the upstream, and the
    kind's own (OpenRouter needs ``ANTHROPIC_API_KEY`` explicitly empty); none for a subscription model."""
    if is_subscription(spec):
        return {}
    kind = providers(path)[spec["provider"]]
    out = {"ANTHROPIC_BASE_URL": kind["base_url"] or spec["base_url"]}
    out.update({alias: spec["upstream"] for alias in _ALIASES})
    out.update(kind["env"])
    if kind.get("protocol") == "openai-responses":
        out["ANTHROPIC_BASE_URL"] = OPENAI_BRIDGE
        out["ENABLE_TOOL_SEARCH"] = "false"
    return out


def secret_key(provider: str, path=None) -> str:
    return providers(path)[provider].get("auth_env", SECRET_KEY)


def secret_problem(text, provider=None) -> str:
    """Why ``text`` is not the key file's one bare line, or ``""`` when it is (kd-secret put refuses it before
    writing; the runner reads such a file as no key at all)."""
    wanted = secret_key(provider) if provider is not None else SECRET_KEY
    lines = [line for line in str(text).splitlines() if line.strip()]
    if len(lines) != 1:
        return "one line is expected: " + wanted + "=<key>"
    key, sep, value = lines[0].partition("=")
    if key != wanted or not sep:
        return "the line must start with " + wanted + "="
    if not value:
        return "no value after " + wanted + "="
    if not _SECRET_VALUE.fullmatch(value):
        return "the key is letters, digits, '_', '.', '/', '+', '=' or '-', without quotes"
    return ""


def read_secret(provider, env_dir=ENV_DIR, path=None) -> dict:
    """``{ANTHROPIC_AUTH_TOKEN: key}`` from the kind's file, read by root; ``{}`` without a file or on a bad one."""
    try:
        text = Path(env_file(provider, env_dir, path)).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    if secret_problem(text, provider):
        return {}
    return {secret_key(provider, path): text.strip().partition("=")[2]}


def cost(prices: Mapping, fields: Mapping) -> float:
    """USD of one usage row by the cabinet model's prices (per million tokens)."""
    total = (fields["in"] * prices["price_in"] + fields["out"] * prices["price_out"]
             + fields["cache_read"] * prices["price_cache_read"] + fields["cache_write"] * prices["price_cache_write"])
    return round(total / 1_000_000, 6)


def experimental(upstream) -> bool:
    """A model Anthropic does not support through Claude Code: anything that is not a Claude."""
    return "claude" not in str(upstream).lower()
