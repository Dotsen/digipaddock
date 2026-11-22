"""anonymized replicas of the 20.10 audit's two cards hold the facts the snapshot must show."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

from kd.cabinet import decisions, probe_cards  # noqa: E402


def thread_bytes(store, card_id: str) -> int:
    """UTF-8 bytes of the bodies and payloads of every message in the card's thread."""
    return sum(
        len(m["body"].encode("utf-8")) + len(json.dumps(m["payload"], ensure_ascii=False).encode("utf-8"))
        for m in store.messages(card_id)
    )


def test_replicas_hold_the_audit_facts(tmp_path):
    cab = CabinetEnv(tmp_path)
    a, b = probe_cards.kd9041_like(cab.store), probe_cards.kd9032_like(cab.store)
    assert (a, b) == ("KD-9041", "KD-9032")
    assert thread_bytes(cab.store, a) >= 98_000
    assert thread_bytes(cab.store, b) >= 68_000
    assert [(p["number"], p["state"]) for p in cab.store.prs(card=a)] == [(18, "merged")]
    keys = [m["payload"].get("key") for m in cab.store.messages(a) if m["kind"] == "notice"]
    assert "notice.forge.merged" in keys and "notice.forge.release_degraded" in keys
    approved = [d for d in decisions.Decisions(cab.store).list(card=a) if d["type"] == "pr_approve"]
    assert [d["state"] for d in approved] == ["approved"]
    assert approved[0]["payload"]["head"] == cab.store.prs(card=a)[0]["head"]
    assert cab.store.card(a).status == "In Progress" and not cab.store.card(a).dependencies
    assert not cab.store.card(b).dependencies and cab.store.card(b).status == "Blocked"
    assert "KD-9041a" in cab.store.card(b).description
    assert [d["state"] for d in decisions.Decisions(cab.store).list(card=b)] == ["approved"]
    for card in (a, b):
        assert cab.store.commands(card=card, states=("queued", "delivered", "started")) == []
        assert [c["state"] for c in cab.store.commands(card=card)][:1] == ["done"]
    assert [s["state"] for s in cab.store.sessions(thread=cab.store.card(a).thread)] == ["exited"]


def test_replicas_carry_no_platform_names(tmp_path):
    cab = CabinetEnv(tmp_path)
    for card in (probe_cards.kd9041_like(cab.store), probe_cards.kd9032_like(cab.store)):
        record = cab.store.card(card)
        texts = [record.title, record.description, record.notes] + [m["body"] for m in cab.store.messages(card)]
        assert not any("digipaddock" in t.lower() for t in texts)
