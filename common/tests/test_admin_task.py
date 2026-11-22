"""An admin card read the same way by the approval service and the executor.

the card is the cabinet's record (``admin_task.from_record``). The md samples reach it through
the reader of the cut-over import (``admin_samples.md_card``).
"""
import sys
from pathlib import Path

import pytest

from kd import admin_task

sys.path.insert(0, str(Path(__file__).resolve().parent))  # admin_samples
from admin_samples import CARD, md_card  # noqa: E402


def card(text=CARD):
    return md_card(text)


def test_reads_the_card_fields():
    c = card()
    assert c.task_id == "KD-123"
    assert c.title == "node-c: restart the app workers"
    assert c.status == "To Do"
    assert c.project == "admin"
    assert c.assignees == ("sysadmin",)
    assert c.servers == ("node-c", "node-e")
    assert "Restart the app workers" in c.sections["Description"]
    assert c.revision == 1


def test_admin_card_checks_pass():
    c = card()
    admin_task.check_admin(c)
    admin_task.check_active(c)


@pytest.mark.parametrize("old,new,why", [
    ("project: admin", "project: digipaddock", "project"),
    ("  - '@sysadmin'\n", "  - '@dev'\n", "assignee"),
    ("assignee:\n  - '@sysadmin'\n", "assignee: []\n", "assignee"),
])
def test_admin_card_checks_refuse(old, new, why):
    c = card(CARD.replace(old, new, 1))
    with pytest.raises(admin_task.CardError, match=why):
        admin_task.check_admin(c)


@pytest.mark.parametrize("status", ["Inbox", "Blocked", "Review", "Done"])
def test_only_to_do_and_in_progress_are_active(status):
    c = card(CARD.replace("status: To Do", f"status: {status}"))
    with pytest.raises(admin_task.CardError, match="status"):
        admin_task.check_active(c)


def test_in_progress_is_active():
    admin_task.check_active(card(CARD.replace("status: To Do", "status: In Progress")))


def test_scope_ignores_notes_status_dates_ordinal_and_checkboxes():
    base = card().scope_sha256
    variants = [
        CARD.replace("[sysadmin] started", "[sysadmin] started\n[sysadmin] more notes"),
        CARD.replace("status: To Do", "status: In Progress"),
        CARD.replace("updated_date: '2026-10-09 16:26'", "updated_date: '2026-10-10 09:00'"),
        CARD.replace("ordinal: 183000", "ordinal: 1000"),
        CARD.replace("- [ ] #1 workers are active", "- [x] #1 workers are active"),
        CARD.replace("- [ ] #1 checked with ssh-ro", "- [x] #1 checked with ssh-ro"),
        CARD.replace("\n", "\r\n"),
        CARD.replace("<!-- SECTION:NOTES:END -->\n",
                     "<!-- SECTION:NOTES:END -->\n\n## Final Summary\n\n<!-- SECTION:FINAL_SUMMARY:BEGIN -->\n"
                     "done\n<!-- SECTION:FINAL_SUMMARY:END -->\n"),
        # a blocked card gets a dependency and comments (the agent's and BOSS unblock), as Backlog writes
        # them
        CARD.replace("dependencies: []", "dependencies:\n  - KD-5").replace(
            "<!-- SECTION:NOTES:END -->\n",
            "<!-- SECTION:NOTES:END -->\n\n## Comments\n\n<!-- COMMENTS:BEGIN -->\nauthor: @boss\n"
            "created: 2026-10-10 18:58\n---\n[boss] unblock: KD-5 Done\n---\n<!-- COMMENTS:END -->\n"),
    ]
    for i, text in enumerate(variants):
        assert card(text).scope_sha256 == base, i


def test_scope_changes_with_title_description_criteria_assignee_and_servers():
    base = card().scope_sha256
    variants = [
        CARD.replace("node-c: restart the app workers", "node-c: restart everything"),
        CARD.replace("Restart the app workers after", "Restart and upgrade the app workers after"),
        CARD.replace("#1 workers are active", "#1 workers and nginx are active"),
        CARD.replace("#1 checked with ssh-ro", "#1 checked"),
        CARD.replace("  - 'server:node-e'\n", ""),
        CARD.replace("  - 'server:node-e'\n", "  - 'server:node-e'\n  - 'server:node-l'\n"),
        CARD.replace("  - '@sysadmin'\n", "  - '@dev'\n"),
        CARD.replace("<!-- AC:END -->\n", "- [ ] #2 also reboot\n<!-- AC:END -->\n"),
    ]
    for i, text in enumerate(variants):
        assert card(text).scope_sha256 != base, i


def test_missing_scope_sections_are_empty_strings():
    c = card(CARD.split("## Description")[0])
    assert c.sections == {}
    assert c.scope()["description"] == "" and c.scope()["acceptance_criteria"] == ""


def test_long_title_in_folded_block():
    text = CARD.replace("title: 'node-c: restart the app workers'", "title: >-\n  node-c: restart the app\n  workers today")
    assert card(text).title == "node-c: restart the app workers today"


def test_single_quotes_are_unescaped():
    text = CARD.replace("title: 'node-c: restart the app workers'", "title: 'node-c: it''s time'")
    assert card(text).title == "node-c: it's time"


@pytest.mark.parametrize("task_id", ["KD-0", "kd-123", "KD-123 ", "KD-1234567", "../KD-1", "", None])
def test_bad_task_ids_are_refused(task_id):
    with pytest.raises(admin_task.CardError, match="task id"):
        admin_task.check_task_id(task_id)


# --- the card as the cabinet stores it ---------------------------------------------------------

RECORD = {"id": "KD-123", "title": "T", "status": "To Do", "project": "admin", "assignee": "@sysadmin",
          "servers": ["node-f", "node-c"], "labels": ["server:node-c", "server:node-f"], "revision": 4,
          "description": "fix it", "acceptance": [{"text": "a", "checked": True}, {"text": "b", "checked": False}],
          "dod": "- [ ] tests", "plan": "", "notes": "", "summary": ""}


def test_from_record_scope_matches_the_markdown_scope():
    card = admin_task.from_record(RECORD)
    assert card.servers == ("node-c", "node-f") and card.assignees == ("sysadmin",) and card.revision == 4
    assert (card.task_id, card.title, card.status, card.project, card.path) == ("KD-123", "T", "To Do", "admin", "")
    assert card.scope()["acceptance_criteria"] == "- [ ] a\n- [ ] b"
    assert card.scope()["description"] == "fix it" and card.scope()["definition_of_done"] == "- [ ] tests"
    unchecked = [{"text": "a", "checked": False}, {"text": "b", "checked": False}]
    assert card.scope_sha256 == admin_task.from_record({**RECORD, "acceptance": unchecked}).scope_sha256
    # notes, summary, status and the revision are progress: the owner's decision stays valid
    progress = {**RECORD, "notes": "сделал шаг 1", "summary": "готово", "status": "In Progress", "revision": 9}
    assert admin_task.from_record(progress).scope_sha256 == card.scope_sha256
    assert admin_task.from_record({**RECORD, "description": "other"}).scope_sha256 != card.scope_sha256
    admin_task.check_admin(card)
    admin_task.check_active(card)


def test_from_record_sections_skip_empty_columns():
    card = admin_task.from_record({**RECORD, "acceptance": [], "dod": "", "notes": "n"})
    assert card.sections == {"Description": "fix it", "Implementation Notes": "n"}


def test_from_record_an_unassigned_card_has_no_assignee():
    assert admin_task.from_record({**RECORD, "assignee": ""}).assignees == ()


@pytest.mark.parametrize("broken", [
    {"id": None}, {"id": "KD-0"}, {"status": None}, {"servers": ["Bad Alias"]}, {"revision": "abc"},
    {"revision": True}, {"servers": "node-c"},
])
def test_from_record_refuses_a_record_it_cannot_trust(broken):
    with pytest.raises(admin_task.CardError):
        admin_task.from_record({**RECORD, **broken})
