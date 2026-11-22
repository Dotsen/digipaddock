"""The time the journals, the cabinet and the coordinator's lines write: an aware UTC moment and its ISO
text to the second, with a Z. A day — of the spending ledger, of the monitor's clock — is the host's: host.json
``timezone`` (an IANA name, UTC by default)."""
import functools
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kd import naming

ISO = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(moment) -> str:
    """An aware datetime or a POSIX time as ``2026-10-29T12:00:00Z``."""
    if isinstance(moment, (int, float)):
        moment = datetime.fromtimestamp(moment, timezone.utc)
    return moment.astimezone(timezone.utc).strftime(ISO)


@functools.lru_cache(maxsize=1)
def zone() -> ZoneInfo:
    """The host's zone; a broken host.json is an error, as for every host value."""
    return ZoneInfo(naming.load_host()["timezone"])


def day(moment) -> str:
    """The host's day of an aware datetime or a POSIX time: ``2026-10-29``."""
    if isinstance(moment, (int, float)):
        moment = datetime.fromtimestamp(moment, timezone.utc)
    return moment.astimezone(zone()).strftime("%Y-%m-%d")
