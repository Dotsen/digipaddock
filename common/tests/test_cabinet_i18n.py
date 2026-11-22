"""one ru/en catalog pair for the web and the service; server texts are stored as a key and parameters."""

import json
import re
from pathlib import Path

from cabinet_env import CabinetEnv

from kd import naming
from kd.cabinet import decisions, exporter, i18n, view

PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_catalogs_have_the_same_keys_and_placeholders():
    cats = i18n.load()
    assert set(cats) == {"ru", "en"} and not i18n.missing()
    for key, ru in cats["ru"].items():
        assert set(PLACEHOLDER.findall(ru)) == set(
            PLACEHOLDER.findall(cats["en"][key])
        ), key


def test_t_formats_and_falls_back():
    assert "KD-7" in i18n.t("ru", "push.decision.waiting", card="KD-7", title="x")
    assert i18n.t("en", "push.decision.waiting", card="KD-7", title="x") != i18n.t(
        "ru", "push.decision.waiting", card="KD-7", title="x"
    )
    assert i18n.t("de", "risk.low") == i18n.t("en", "risk.low")  # no catalog: the fallback, English
    assert i18n.t("ru", "no.such.key") == "no.such.key"
    assert i18n.t("ru", "push.reminder")  # a missing param does not raise
    assert i18n.t("ru", "risk.low", extra="x") == i18n.t("ru", "risk.low")
    assert i18n.languages() == ("ru", "en")


def test_the_service_keys_are_in_the_catalog():
    keys = set(i18n.load()["ru"])
    needed = {
        "sys.card.created",
        "sys.card.status",
        "sys.card.assigned",
        "sys.decision.opened",
        "push.decision.waiting",
        "push.question",
        "push.card.review",
        "push.card.done",
        "push.card.blocked_on_me",
        "push.session.failed",
        "push.digest.title",
        "push.digest.body",
        "push.digest.body.team",
        "push.digest.body.spend",
        "push.digest.body.spend_partial",
        "push.digest.body.spend_nodata",
        "push.reminder",
        "risk.low",
        "risk.medium",
        "risk.high",
    }
    needed |= {
        f"sys.decision.{s}"
        for s in ("approved", "rejected", "asked", "replied", "expired", "revoked")
    }
    needed |= {
        f"decision.type.{t}"
        for t in (
            "admin_plan",
            "admin_critical",
            "delegate",
            "monitor_trust",
            "pr_approve",
            "promote",
            "question",
            "permission",
        )
    }
    assert needed <= keys, needed - keys
    assert set(PLACEHOLDER.findall(i18n.load()["ru"]["push.digest.body"])) == {
        "decisions",
        "incidents",
        "done",
        "cost",
    }


def test_every_key_the_python_code_stores_is_in_the_catalog():
    """A status line of the session host or a push text is stored as a key: a key missing from the catalogs shows
    the owner the English fallback body."""
    root = Path(__file__).resolve().parents[2]
    literal = re.compile(r"""["']((?:sys|push)\.[a-z_]+(?:\.[a-z_]+)+)["']""")
    used = {
        key
        for base in ("common/kd", "agents")
        for path in (root / base).rglob("*.py")
        if "tests" not in path.parts
        for key in literal.findall(path.read_text(encoding="utf-8"))
    }
    assert "sys.session.failed_restart" in used
    assert used <= set(i18n.load()["ru"]), sorted(used - set(i18n.load()["ru"]))


def test_missing_reports_the_difference(tmp_path):
    (tmp_path / "ru.json").write_text(
        json.dumps({"$schema": "x", "a": "А", "b": "Б"}), encoding="utf-8"
    )
    (tmp_path / "en.json").write_text(
        json.dumps({"a": "A", "c": "C"}), encoding="utf-8"
    )
    assert i18n.missing(tmp_path) == {"en": {"b"}, "ru": {"c"}}
    assert "$schema" not in i18n.load(tmp_path)["ru"]


def test_render_translates_a_stored_key_with_its_decision_type():
    payload = {"key": "sys.decision.opened", "params": {"type": "admin_plan"}}
    assert i18n.t("ru", "decision.type.admin_plan") in i18n.render("ru", payload)
    assert i18n.render("en", payload) != i18n.render("ru", payload)
    assert i18n.render("ru", {"decision": "x"}) is None


def test_decision_messages_store_a_key_and_the_terminal_renders_it(tmp_path):
    env = CabinetEnv(tmp_path)
    ledger = decisions.Decisions(env.store)
    card = env.card()
    decision = ledger.open(
        "question",
        card=card.id,
        payload={"text": "Какой лимит?"},
        requested_by="agent:analyst",
        now=env.now,
    )
    ledger.reply(decision["id"], user="owner", text="500", now=env.now)
    opened, replied = [
        m for m in env.store.messages(card.id) if m["kind"] == "decision"
    ]
    assert opened["payload"]["key"] == "sys.decision.opened"
    assert (
        opened["payload"]["params"] == {"type": "question"}
        and opened["body"] == "Какой лимит?"
    )
    assert replied["payload"]["key"] == "sys.decision.replied"
    assert replied["payload"]["params"] == {"by": "@owner"} and replied["body"] == "500"
    text = view.card_text(
        env.store.card(card.id).to_dict(), env.store.messages(card.id)
    )
    assert (
        i18n.t("ru", "sys.decision.opened", type=i18n.t("ru", "decision.type.question"))
        in text
    )
    assert i18n.t("ru", "sys.decision.replied", by="@owner") + "\n500" in text


def test_the_md_snapshot_shows_a_decision_message_in_words(tmp_path):
    env = CabinetEnv(tmp_path)
    ledger = decisions.Decisions(env.store)
    card = env.card()
    decision = ledger.open(
        "question",
        card=card.id,
        payload={"text": "Какой лимит?"},
        requested_by="agent:analyst",
        now=env.now,
    )
    ledger.reply(decision["id"], user="owner", text="500", now=env.now)
    text = exporter.render_card(
        env.store.card(card.id).to_dict(), env.store.messages(card.id)
    )
    assert (
        "[decision] " + i18n.t("ru", "sys.decision.replied", by="@owner") + "\n500"
        in text
    )


def test_the_catalogs_name_the_hosts_commands_and_cards(tmp_path, monkeypatch):
    # a catalog spells commands kd-<word> and card forms KD-<n>; the reader is on this host
    (tmp_path / "ru.json").write_text(json.dumps({"x": "под root: kd-session-host resume, файл common/bin/kd-task, KD-<n>, KD-12"}))
    (tmp_path / "en.json").write_text(json.dumps({"x": "x"}))
    monkeypatch.setattr(naming, "COMMANDS", "acme")
    monkeypatch.setattr(naming, "CARDS", "AC")
    assert i18n.load(tmp_path)["ru"]["x"] == "под root: acme-session-host resume, файл common/bin/kd-task, AC-<n>, KD-12"
