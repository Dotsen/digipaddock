import pytest

from kd.activity import parse_record


REVISION = 7  # the card's cabinet revision


def event(**changes):
    record = {
        "schemaVersion": 1,
        "cardId": "KD-122",
        "worker": "@dev",
        "action": "progress",
        "at": "2026-10-07T12:08:00Z",
        "cardRevision": REVISION,
    }
    record.update(changes)
    return record


@pytest.mark.parametrize(
    "change",
    [
        {"schemaVersion": True},
        {"schemaVersion": 2},
        {"cardId": "KD-0"},
        {"worker": "@boss"},
        {"action": "boss_comment"},
        {"action": "sync"},
        {"at": "2026-10-07T12:08:00+03:00"},
        {"at": "2026-02-30T12:08:00Z"},
        {"cardRevision": "a" * 64},
        {"cardRevision": "7"},
        {"cardRevision": 0},
        {"cardRevision": True},
        {"extra": "unexpected"},
    ],
)
def test_invalid_or_non_worker_events_are_rejected(change):
    with pytest.raises(ValueError):
        parse_record(event(**change))
