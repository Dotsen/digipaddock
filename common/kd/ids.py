"""Card IDs: the one shape every module checks a card ID against.

The cabinet numbers its cards ``<prefix>-<n>`` from 1 (``kd.cabinet.store``): the host's card prefix
(``naming.CARDS``), a dash and a number without a leading zero, at most six digits. An earlier prefix of the host
(``naming.CARD_ALIASES``) still names the same card in what people type: ``normalize`` turns it into the current one
in a text, ``canonical`` in a value that is an ID alone (an argument, a field of a request).
kd-release, kd-pr and the monitor import it too; the gate on the targets (Python 3.6) checks no card. A pattern of
any card prefix (kd-boss-board's thread source, the session context of agent-launch) is a shape, not this host's ID.
"""
import re

from kd import naming

PREFIX = naming.CARDS
ALIASES = naming.CARD_ALIASES
_NUMBER = r"-[1-9][0-9]{0,5}"
CARD = re.compile(re.escape(PREFIX) + _NUMBER)  # fullmatch: an ID alone
# An ID inside text (a PR title): never the part of a longer word or number.
IN_TEXT = re.compile(r"(?<![A-Za-z0-9])" + CARD.pattern + r"(?![0-9])")
_ALIAS = re.compile(
    r"(?<![A-Za-z0-9])(?:" + "|".join(map(re.escape, ALIASES)) + r")(?=" + _NUMBER + r"(?![0-9]))"
) if ALIASES else None


_ALIAS_ID = re.compile("(?:" + "|".join(map(re.escape, ALIASES)) + ")" + _NUMBER) if ALIASES else None
# An ID of this prefix or an earlier one, as written in a name made before the move (a PR's branch agent/<ID>-…):
# ``canonical`` of the match is the card.
ANY = re.compile("(?:" + "|".join(map(re.escape, (PREFIX,) + tuple(ALIASES))) + ")" + _NUMBER)


def normalize(text: str) -> str:
    """``text`` with every card ID of an earlier prefix of this host written with the current one."""
    return _ALIAS.sub(PREFIX, text) if _ALIAS is not None else text


def canonical(value):
    """A value that is a card ID of an earlier prefix alone, as the current ID; any other value as it is."""
    if _ALIAS_ID is not None and isinstance(value, str) and _ALIAS_ID.fullmatch(value):
        return PREFIX + value[value.index("-"):]
    return value


def canonical_all(value):
    """``canonical`` of every string in a request's mappings and lists; a text that only names a card stays."""
    if isinstance(value, dict):
        return {k: canonical_all(v) for k, v in value.items()}
    if isinstance(value, list):
        return [canonical_all(v) for v in value]
    return canonical(value)
