import json

import pytest

from kd import directives as d

NOW = 1_000_000.0
REV = 4  # the card's cabinet revision


@pytest.fixture
def store(tmp_path):
    return str(tmp_path / "directives.json")


def send(store, agent="dev", card="KD-5", kind="work", message="Начни с тестов", now=NOW):
    return d.send(store, agent=agent, card=card, kind=kind, message=message, revision=REV, now=now)


def test_send_stores_a_pending_directive_with_an_id_and_a_deadline(store):
    directive, created = send(store)
    assert created and len(directive.id) == 16
    assert (directive.state, directive.expires) == ("pending", NOW + d.TTL)
    assert d.listing(store) == [directive]


def test_repeat_of_an_open_directive_returns_it_instead_of_a_second_one(store):
    first, _ = send(store)
    again, created = send(store, now=NOW + 60)
    assert not created and again.id == first.id and len(d.listing(store)) == 1
    other, created = send(store, message="Другое")
    assert created and other.id != first.id


@pytest.mark.parametrize(
    "message",
    ["", "   ", "две\nстроки", "tab\tinside", "esc\x1b[31m", "bidi ‮", "x" * 4097, "ж" * 2049],
)
def test_message_must_be_one_short_line_of_text(store, message):
    with pytest.raises(d.Refused):
        send(store, message=message)


@pytest.mark.parametrize(
    "field, value",
    [("agent", "../dev"), ("agent", ""), ("card", "kd-5"), ("card", "KD-5 x"), ("kind", "deploy")],
)
def test_fields_are_checked(store, field, value):
    with pytest.raises(d.Refused):
        send(store, **{field: value})


def test_too_many_open_directives_are_refused(store, monkeypatch):
    monkeypatch.setattr(d, "MAX_OPEN", 2)
    send(store, message="1")
    send(store, message="2")
    with pytest.raises(d.Refused, match="open directives"):
        send(store, message="3")


def test_delivery_and_attempts(store):
    directive, _ = send(store)
    assert [x.id for x in d.due(store, NOW + 1)] == [directive.id]
    d.mark_attempt(store, directive.id, NOW + 1)
    assert d.listing(store)[0].attempts == 1
    d.mark_delivered(store, directive.id, NOW + 2)
    assert d.listing(store)[0].state == "delivered" and d.due(store, NOW + 3) == []


def test_only_the_addressee_reads_and_reading_marks_it_read(store):
    directive, _ = send(store)
    with pytest.raises(d.NotYours):
        d.read_for(store, directive.id, "sysadmin", NOW + 1)
    with pytest.raises(d.Missing):
        d.read_for(store, "0" * 16, "dev", NOW + 1)
    with pytest.raises(d.Missing):
        d.read_for(store, "../x", "dev", NOW + 1)
    assert d.read_for(store, directive.id, "dev", NOW + 1).state == "read"


def test_reply_is_bound_to_the_addressee_and_is_idempotent(store):
    directive, _ = send(store)
    with pytest.raises(d.NotYours):
        d.reply(store, directive.id, "sysadmin", "done", "готово", NOW + 1)
    replied, changed = d.reply(store, directive.id, "dev", "accepted", "беру", NOW + 2)
    assert changed and (replied.state, replied.reply, replied.reply_message) == ("replied", "accepted", "беру")
    again, changed = d.reply(store, directive.id, "dev", "accepted", "беру", NOW + 3)
    assert not changed and again.replied == NOW + 2
    with pytest.raises(d.Closed):
        d.reply(store, directive.id, "dev", "done", "другое", NOW + 4)
    with pytest.raises(d.Refused):
        d.reply(store, directive.id, "dev", "maybe", "x", NOW + 4)


def test_expired_directive_cannot_be_read_or_answered_and_is_reported_once(store):
    directive, _ = send(store)
    later = NOW + d.TTL + 1
    assert [x.id for x in d.expire(store, later)] == [directive.id]
    assert d.expire(store, later + 1) == []
    with pytest.raises(d.Closed):
        d.read_for(store, directive.id, "dev", later)
    with pytest.raises(d.Closed):
        d.reply(store, directive.id, "dev", "done", "поздно", later)
    assert d.due(store, later) == []


def test_unseen_replies_and_expiries_until_the_coordinator_lists_them(store):
    one, _ = send(store, message="1")
    two, _ = send(store, message="2")
    d.reply(store, one.id, "dev", "done", "ok", NOW + 1)
    d.expire(store, NOW + d.TTL + 1)
    assert {x.id for x in d.unseen(store)} == {one.id, two.id}
    d.mark_seen(store, [one.id, two.id], NOW + d.TTL + 2)
    assert d.unseen(store) == []


def test_coordinator_wakes_at_most_hourly(store):
    send(store)
    assert d.boss_wake_due(store, NOW)
    d.mark_boss_woken(store, NOW)
    assert not d.boss_wake_due(store, NOW + 3599)
    assert d.boss_wake_due(store, NOW + 3600)




def test_closed_directives_are_pruned_after_a_week(store):
    one, _ = send(store, message="1")
    d.reply(store, one.id, "dev", "done", "ok", NOW + 1)
    send(store, message="2", now=NOW + d.KEEP + 10)
    assert [x.message for x in d.listing(store)] == ["2"]


def test_a_damaged_store_is_never_read_as_empty(store):
    with open(store, "w", encoding="utf-8") as handle:
        handle.write("{not json")
    with pytest.raises(d.CorruptStore):
        send(store)
    with open(store, "w", encoding="utf-8") as handle:
        json.dump({"schemaVersion": 1, "bossWoken": 0, "directives": [{"id": "x"}]}, handle)
    with pytest.raises(d.CorruptStore):
        d.listing(store)
