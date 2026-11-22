"""Notices: what root services and executors tell people, in the cabinet instead of the Pult.

``notice.post {source, level, key, params, card?}`` writes one; ``notice.list {before?, limit?}`` reads the
platform's own alerts — warn and critical notices without a card, newest first — for the incidents page, where a
push about one leads (people with ``settings``, the feed's rule for them, and root). The text is a catalog key with flat parameters
(spec section 8), rendered in the reader's language. With a card the notice is a ``notice`` message of the card's
thread from ``system:<source>`` (written ``via`` the notice, so the feed and the history show it once); with or
without one it is a ``notice`` event, which the notifier pushes by level. Root posts for the platform (an alert, the
monitor's fallback, watch); the exec user and the Forgejo executor's user only their own source, on a card, never
``critical``.
"""

import json
import re

from kd.cabinet import api, i18n, store

# ``release``: the health check of a release, a root step of kd-release
SOURCES = {"root": ("alert", "monitor", "watch", "release"), "exec": ("exec",), "forge": ("forge",)}
LEVELS = ("info", "warn", "critical")
KEY = re.compile(r"notice\.[a-z_]+\.[a-z_]+")
NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
PARAMS_MAX = 16
PARAM_MAX = 2000
PARAMS_BYTES = 4096
LIST_DEFAULT, LIST_MAX = 20, 100


def _invalid(message):
    return api.ApiError("invalid", message)


def _params(value) -> dict:
    if not isinstance(value, dict) or len(value) > PARAMS_MAX:
        raise _invalid(f"params is an object of at most {PARAMS_MAX} entries")
    out = {}
    for name, item in value.items():
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise _invalid("a parameter name is [a-z][a-z0-9_]*")
        if isinstance(item, bool) or not isinstance(item, (str, int)):
            raise _invalid(f"parameter {name} is text or a number")
        if isinstance(item, str) and len(item) > PARAM_MAX:
            raise _invalid(f"parameter {name} is longer than {PARAM_MAX} characters")
        out[name] = item
    if len(json.dumps(out, ensure_ascii=False).encode("utf-8")) > PARAMS_BYTES:
        raise _invalid(f"params are larger than {PARAMS_BYTES} bytes")
    return out


def _post(ctx, principal, request):
    source, level, key = request.get("source"), request.get("level"), request.get("key")
    card_id = request.get("card") or ""
    if level not in LEVELS:
        raise _invalid(f"level is one of {', '.join(LEVELS)}")
    if not isinstance(key, str) or not KEY.fullmatch(key) or key not in i18n.load()[i18n.DEFAULT]:
        raise _invalid("key must be a notice.* key of the catalogs")
    params = _params(request.get("params", {}))
    if source not in SOURCES[principal.kind]:
        raise api.ApiError("refused", f"{principal.kind} does not post {source!r} notices")
    if principal.kind != "root" and (not card_id or level == "critical"):
        raise api.ApiError("refused", f"{principal.kind} posts info and warn notices on a card only")
    if card_id and not (isinstance(card_id, str) and api.CARD.fullmatch(card_id)):
        raise _invalid("card is KD-<n>")
    try:
        return post_in_tx(ctx.store, source=source, level=level, key=key, params=params, card=card_id)
    except store.NotFound:
        raise api.ApiError("not_found", f"{card_id} not found")


def post_in_tx(s, *, source: str, level: str, key: str, params: dict, card: str = "") -> dict:
    """The notice itself, joining the caller's write: the cabinet's own sources (``pipeline``) post here,
    past the operation's checks, which are for the principals outside it."""
    author = f"system:{source}"
    with s.transaction():
        message = None
        thread = None
        if card:
            thread = s.card(card).thread
            message = s.add_thread_message(
                thread, author=author, kind="notice", body="",
                payload={"key": key, "params": params, "level": level, "source": source}, via="notice",
            )["seq"]
        event = s._event(author, "notice", card, {"source": source, "level": level, "key": key, "params": params},
                         thread)
    return {"event": event, "message": message}


def _count(value, name, low, high=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        raise _invalid(f"{name} is a number from {low}" + (f" to {high}" if high is not None else ""))
    return value


def _list(ctx, principal, request):
    """The platform's alerts, a page of them: ``next`` is the ``before`` of the older page, None at the end."""
    api._require(ctx, principal, "settings")
    limit = _count(request.get("limit", LIST_DEFAULT), "limit", 1, LIST_MAX)
    before = request.get("before")
    if before is not None:
        _count(before, "before", 0, store.SEQ_MAX)
    events = ctx.store.platform_notices(before, limit)
    items = [
        {"seq": e["seq"], "ts": e["ts"], "source": e["payload"].get("source", ""), "level": e["payload"].get("level"),
         "key": e["payload"].get("key", ""), "params": e["payload"].get("params") or {}}
        for e in events
    ]
    more = len(items) == limit and bool(ctx.store.platform_notices(items[-1]["seq"], 1))
    return {"items": items, "next": items[-1]["seq"] if more else None}


OPS = {"notice.post": (frozenset(SOURCES), _post), "notice.list": (api.ROOTISH | {"user"}, _list)}
api.OPS.update(OPS)  # the board API serves it (api imports this module at its end)
