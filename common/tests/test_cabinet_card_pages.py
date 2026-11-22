"""scope, result, message and thread pages of a card — each within 8 KiB, cursors stable or refused."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

from kd.cabinet import api, pages, probe_cards, snapshot  # noqa: E402


@pytest.fixture
def cab(tmp_path):
    return CabinetEnv(tmp_path)


def call(cab, op, **request):
    return cab.call("digi-cabinet", {"op": op, **request})


def size(text: str) -> int:
    return len(text.encode("utf-8"))


def walk(cab, op, **request):
    """Every page of a document, from the start to its end."""
    out, cursor = [], None
    while True:
        found = call(cab, op, **request, **({"cursor": cursor} if cursor else {}))
        out.append(found)
        if found["next"] is None:
            return out
        cursor = f"{found['sha']}:{found['next']}"
        assert f"--cursor {cursor}" in found["text"]


def long_card(cab):
    description = "".join(f"Раздел {i}: описание поведения 🚀 и пределов исполнителя.\n" for i in range(600))
    return cab.card(status="In Progress", description=description,
                    acceptance=[{"text": f"Критерий {i}", "checked": i % 2 == 0} for i in range(5)])


def test_scope_pages_join_to_the_document(cab):
    card = long_card(cab)
    found = walk(cab, "card.scope", card=card.id)
    assert len(found) > 3
    for page in found:
        assert size(page["text"]) <= snapshot.LIMIT
        page["chunk"].encode("utf-8").decode("utf-8")
    assert "".join(p["chunk"] for p in found) == pages.scope_text(cab.store.card(card.id))
    assert found[-1]["text"].rstrip().endswith(f"end ({found[-1]['total']} bytes)")
    assert "## Acceptance Criteria" in pages.scope_text(cab.store.card(card.id))


def test_a_stale_scope_cursor_conflicts(cab):
    card = long_card(cab)
    first = call(cab, "card.scope", card=card.id)
    cab.store.edit_card(card.id, None, {"description": "Новое описание"}, actor="user:owner")
    with pytest.raises(api.ApiError) as exc:
        call(cab, "card.scope", card=card.id, cursor=f"{first['sha']}:{first['next']}")
    assert exc.value.kind == "conflict" and f"kd-task scope {card.id}" in str(exc.value)


def test_comment_does_not_stale_the_scope_cursor(cab):
    card = long_card(cab)
    first = call(cab, "card.scope", card=card.id)
    cab.store.edit_card(card.id, None, {"comment": {"author": "@owner", "text": "Комментарий"}}, actor="user:owner")
    second = call(cab, "card.scope", card=card.id, cursor=f"{first['sha']}:{first['next']}")
    assert second["offset"] == first["next"]


def test_bad_cursors_are_invalid(cab):
    card = long_card(cab)
    first = call(cab, "card.scope", card=card.id)
    for cursor in ("nope", f"{first['sha']}:{first['total'] + 1}"):
        with pytest.raises(api.ApiError) as exc:
            call(cab, "card.scope", card=card.id, cursor=cursor)
        assert exc.value.kind == "invalid"


def test_thread_walks_back_without_gaps(cab):
    card = probe_cards.kd9041_like(cab.store)
    expected = {m["seq"] for m in cab.store.messages(card) if snapshot.significant(m)}
    seen, request = [], {"card": card}
    while True:
        found = call(cab, "card.thread", **request)
        assert size(found["text"]) <= snapshot.LIMIT
        shown = [int(line.split()[0][1:]) for line in found["text"].splitlines() if line.startswith("#")]
        assert shown and not set(shown) & set(seen)
        seen += shown
        if found["older"] is None:
            assert found["text"].startswith("start of thread")
            break
        request = {"card": card, "before": found["older"]}
    assert set(seen) == expected


def test_thread_walks_forward_and_refuses_both_ends(cab):
    card = cab.card(status="In Progress")
    seqs = [cab.store.add_thread_message(card.thread, author="user:owner", kind="text", body=f"{i} " + "ё" * 400)["seq"]
            for i in range(40)]
    first = call(cab, "card.thread", card=card.id, after=0)
    assert first["text"].startswith("start of thread") and first["newer"] is not None
    rest = call(cab, "card.thread", card=card.id, after=first["newer"])
    assert f"#{first['newer'] + 1} " in rest["text"] or f"#{seqs[seqs.index(first['newer']) + 1]} " in rest["text"]
    with pytest.raises(api.ApiError) as exc:
        call(cab, "card.thread", card=card.id, before=5, after=1)
    assert exc.value.kind == "invalid"


def test_one_huge_message_still_moves_the_page(cab):
    card = cab.card(status="In Progress")
    seq = cab.store.add_thread_message(card.thread, author="user:owner", kind="text", body="ж" * 20_000)["seq"]
    older = cab.store.add_thread_message(card.thread, author="user:owner", kind="text", body="раньше")["seq"]
    assert older > seq
    found = call(cab, "card.thread", card=card.id)
    assert f"… (kd-task message {card.id} {seq})" in found["text"] and size(found["text"]) <= snapshot.LIMIT
    back = call(cab, "card.thread", card=card.id, before=seq + 1)
    assert f"#{seq} " in back["text"] and back["older"] is None


def test_tools_shows_calls_never_results(cab):
    card = probe_cards.kd9041_like(cab.store)
    plain = call(cab, "card.thread", card=card)["text"]
    tools = call(cab, "card.thread", card=card, tools=True)
    assert "· Bash: python -m pytest" in tools["text"] and "· Bash:" not in plain
    assert " tool_result" not in tools["text"]
    assert f"--before {tools['older']} --tools" in tools["text"]


def test_message_page_joins_to_its_body(cab):
    card = cab.card(status="In Progress")
    body = "Длинное сообщение 🚀\n" * 1500
    seq = cab.store.add_thread_message(card.thread, author="agent:dev", kind="text", body=body)["seq"]
    found = walk(cab, "card.message", card=card.id, seq=seq)
    assert len(found) > 2 and all(size(p["text"]) <= snapshot.LIMIT for p in found)
    joined = "".join(p["chunk"] for p in found)
    assert joined.startswith(f"#{seq} @dev ") and joined.endswith(body)
    with pytest.raises(api.ApiError) as exc:
        call(cab, "card.message", card=card.id, seq=seq + 100)
    assert exc.value.kind == "not_found"
    other = cab.card()
    with pytest.raises(api.ApiError) as exc:
        call(cab, "card.message", card=other.id, seq=seq)
    assert exc.value.kind == "not_found"


def test_result_text_has_reviews(cab):
    card = cab.card(status="In Progress", acceptance=[{"text": "Отчёт", "checked": True}])
    cab.put(card.id, summary="Готово: https://forge.example/pr/12", notes="Шаги", plan="План")
    cab.store.edit_card(card.id, None, {"status": "Review", "comment": {"author": "@dev", "text": "Сдаю"}}, actor="agent:dev")
    cab.store.edit_card(card.id, None, {"status": "In Progress", "comment": {"author": "@owner", "text": "Доработать"}}, actor="user:owner")
    text = call(cab, "card.result.text", card=card.id)["text"]
    for part in ("## Summary", "Готово: https://forge.example/pr/12", "- [x] #1 Отчёт", "## Implementation Plan",
                 "## Implementation Notes", "## Links", "- https://forge.example/pr/12", "## Review"):
        assert part in text, part
    assert "#1 entered " in text and "@dev: Сдаю → returned" in text and "@owner: Доработать" in text


def test_pages_hide_a_card_out_of_sight(cab):
    cab.users.create("anna", "Анна", roles=[("partner", ["ops"])], actor="root")
    card = cab.card(project="digipaddock")
    for op, extra in (("card.scope", {}), ("card.result.text", {}), ("card.thread", {}), ("card.message", {"seq": 1})):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch(cab.ctx, api.Principal("user", -1, "anna"), {"op": op, "card": card.id, **extra})
        assert exc.value.kind == "not_found", op


# --- small fixes of the card page ---------------------------------------------------------
def test_cursor_inside_a_character_is_invalid(cab):
    card = cab.card(status="In Progress", description="Ж" * 100)
    first = call(cab, "card.scope", card=card.id)
    raw = pages.scope_text(cab.store.card(card.id)).encode("utf-8")
    inside = next(i for i in range(1, len(raw)) if raw[i] & 0xC0 == 0x80)
    with pytest.raises(api.ApiError) as exc:
        call(cab, "card.scope", card=card.id, cursor=f"{first['sha']}:{inside}")
    assert exc.value.kind == "invalid" and "character boundary" in str(exc.value)


def test_page_cut_mid_line_says_so(cab):
    card = cab.card(status="In Progress", description="ы" * 12_000)
    found = walk(cab, "card.scope", card=card.id)
    assert "".join(p["chunk"] for p in found) == pages.scope_text(cab.store.card(card.id))
    cut = [p for p in found if p["next"] is not None and not p["chunk"].endswith("\n")]
    assert cut, "the long line is cut in the middle"
    for page in cut:
        assert page["text"].splitlines()[-1].endswith("(the page ends mid-line)")
    whole = [p for p in found if p["next"] is not None and p["chunk"].endswith("\n")]
    assert all("mid-line" not in p["text"] for p in whole)


def test_snapshot_scope_sha_is_the_cursor_sha(cab):
    card = long_card(cab)
    first = call(cab, "card.scope", card=card.id)
    assert snapshot.facts(cab.ctx, cab.store.card(card.id))["scope"]["sha"] == first["sha"]


def test_message_of_a_tool_result_has_no_body(cab):
    card = cab.card(status="In Progress")
    seq = cab.store.add_thread_message(card.thread, author="agent:dev", kind="tool_result", body="SECRET OUTPUT",
                                       payload={"id": "t1", "ok": True, "size": 1234})["seq"]
    text = call(cab, "card.message", card=card.id, seq=seq)["text"]
    assert "SECRET OUTPUT" not in text and "1234 bytes" in text and "✓" in text


def test_empty_thread_page_points_the_other_way(cab):
    card = cab.card(status="In Progress")
    seqs = [cab.store.add_thread_message(card.thread, author="user:owner", kind="text", body=f"m{i}")["seq"]
            for i in range(3)]
    before = call(cab, "card.thread", card=card.id, before=seqs[0])
    assert before["text"].startswith("start of thread") and f"newer: kd-task thread {card.id} --after 0" in before["text"]
    after = call(cab, "card.thread", card=card.id, after=seqs[-1])
    assert after["text"].rstrip().endswith("end of thread")
    assert f"older: kd-task thread {card.id} --before {seqs[-1] + 1}" in after["text"]
