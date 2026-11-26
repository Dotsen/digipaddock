"""the dev service and its seed — people, cards in every status, a long thread, one decision of
each kind (three plans: one alone, two as a batch); seeding again adds nothing; the sign-in codes also go
to codes.json for the browser tests."""

import io
import json

from kd.cabinet import briefs, devseed
from kd.cabinet.web import decisions as web_decisions


def _counts(svc):
    store = svc.ctx.store
    cards = store.cards()
    return (
        len(cards),
        len(svc.decisions.list()),
        sum(len(store.messages(c.id)) for c in cards),
        sorted(u["id"] for u in svc.users.list()),
    )


def test_the_seed_fills_the_board_once(tmp_path):
    state = tmp_path / "state"
    svc = devseed.dev_service(str(state))
    out = io.StringIO()
    codes = tmp_path / "codes.json"
    devseed.apply(svc, out=out, codes_path=str(codes))
    printed = dict(
        line.split(" code: ")
        for line in out.getvalue().splitlines()
        if " code: " in line
    )
    assert json.loads(codes.read_text(encoding="utf-8")) == printed
    assert set(printed) == {"owner", "anna"}
    cards = svc.ctx.store.cards()
    assert len(cards) == len(devseed.CARDS) == 21
    (alert,) = svc.ctx.store.platform_notices()  # the incidents page's «Platform alerts» has one to show
    assert alert["payload"]["level"] == "critical" and alert["payload"]["key"] == "notice.alert.unit"
    assert {c.status for c in cards} == {
        "Inbox",
        "To Do",
        "In Progress",
        "Review",
        "Blocked",
        "Done",
    }
    assert max(len(svc.ctx.store.messages(c.id)) for c in cards) >= 30
    # A thread longer than its window in Review with a linked summary, a card blocked by an
    # open dependency, a failed run
    long = next(c for c in cards if c.title == "Card history in pages")
    assert long.status == "Review" and "https://" in long.summary
    assert len(svc.ctx.store.messages(long.id)) > devseed.LONG_THREAD
    (early,) = svc.decisions.list(card=long.id)  # a link to it opens far above the thread's window
    assert early["type"] == "question" and early["state"] == "approved"
    opened = [m["seq"] for m in svc.ctx.store.messages(long.id) if m["kind"] == "decision"]
    assert opened[0] <= devseed.LONG_QUESTION_AT + 1
    blocked = next(c for c in cards if c.title == "Passkey sign-in on desktop")
    assert [svc.ctx.store.card(d).status for d in blocked.dependencies] == ["To Do"]
    failed = svc.ctx.store._conn.execute("SELECT agent, reason FROM commands WHERE state='failed'").fetchall()
    assert [tuple(r) for r in failed] == [("dev", "exit 1")]
    kinds = {m["kind"] for c in cards for m in svc.ctx.store.messages(c.id)}
    assert {
        "text",
        "progress",
        "status",
        "tool_call",
        "tool_result",
        "decision",
    } <= kinds
    open_ = {d["type"]: d for d in svc.decisions.list(state="open")}
    assert set(open_) == {"admin_plan", "admin_critical", "promote", "question"}
    plans = [d for d in svc.decisions.list(state="open") if d["type"] == "admin_plan"]
    views = [web_decisions.view_of(svc, d) for d in plans]
    assert [len(v["steps"]) for v in views] == [3, 1, 1]
    assert not any(v["stale"] for v in views)
    assert not web_decisions.view_of(svc, open_["admin_critical"])["stale"]
    assert not web_decisions.view_of(svc, open_["promote"])["stale"]
    # an agent's form of three and an old question read as a form of five
    asked = [web_decisions.view_of(svc, d) for d in svc.decisions.list(state="open") if d["type"] == "question"]
    by_origin = {v["origin"]: v for v in asked}
    assert set(by_origin) == {"", "agent", "parsed"}
    assert [q["type"] for q in by_origin["agent"]["form"]["questions"]] == ["single_select", "multi_select", "text"]
    assert [q["type"] for q in by_origin["parsed"]["form"]["questions"]] == [
        "single_select", "single_select", "text", "text", "text"
    ]
    # the analyst's brief handed to @dev in Inbox, v1 ready for the owner's approval
    [handed] = [c for c in cards if c.title == "Daily error summary"]
    assert (handed.status, handed.assignee, briefs.state(svc.ctx.store, handed)["state"]) == ("Inbox", "@dev", "ready")
    assert [(v["version"], v["origin"]) for v in briefs.versions(svc.ctx.store, handed.id)] == [(1, "agent")]
    assert svc.users.get("anna") and svc.users.can("anna", "cards.read", "alert")
    # the dev base seeds its own projects, with their names; no ops
    assert [(p["id"], p["name"], p["route"]) for p in svc.ctx.store.project_rows()] == [
        ("admin", "Servers", "@sysadmin"), ("alert", "Alert.Example.Com", "@owner"),
        ("digipaddock", "DIGIPADDOCK", "@dev"),
    ]
    first = _counts(svc)
    again = io.StringIO()
    devseed.apply(svc, out=again)
    assert _counts(svc) == first and "owner code: " in again.getvalue()
    restarted = devseed.dev_service(str(state))
    devseed.apply(restarted, out=io.StringIO())
    assert _counts(restarted) == first


def test_the_seed_has_a_card_with_the_pipelines_records(tmp_path):
    from kd.cabinet import outcomes

    svc = devseed.dev_service(str(tmp_path / "state"))
    devseed.apply(svc, out=io.StringIO())
    [card] = [c for c in svc.ctx.store.cards() if c.title == "Agents' progress report"]
    facts = outcomes.card_facts(svc.ctx, card)
    assert facts["outcome"]["outcome"] == "wait_user" and facts["outcome"]["note"] == devseed.PIPELINE_NOTE
    assert [(w["kind"], w["blocking"]) for w in facts["waits"]] == [("decision", True)]
    assert [p["kind"] for p in facts["problems"]] == ["missing_next_step"]


def test_the_seed_has_a_card_under_its_council(tmp_path):
    """a brief its council read once — one critic found gaps, the other none — and the analyst answered
    one finding and asked the owner about another: the council waits for its writer, the card's thread shows the block; nothing of it is left for
    the dev host to run."""
    from kd.cabinet import api, councils

    svc = devseed.dev_service(str(tmp_path / "state"))
    devseed.apply(svc, out=io.StringIO())
    store = svc.ctx.store
    [card] = [c for c in store.cards() if c.title == devseed.COUNCIL_TITLE]
    found = councils.latest(store, card.id, "brief")
    assert (found["state"], found["round"], found["rounds_max"]) == ("needs_writer", 1, 3)
    owner = api.Principal("user", -1, "owner")
    [seen] = api.dispatch(svc.ctx, owner, {"op": "card.councils", "card": card.id})["councils"]
    first, second = seen["rounds_list"][0]["runs"]
    assert (first["state"], first["verdict"], second["verdict"]) == ("done", "needs_changes", "ready")
    assert [(f["id"], f["severity"], f.get("answer")) for f in first["findings"]] == [
        ("r1.f1", "major", "rejected"), ("r1.f2", "major", None), ("r1.f3", "minor", None),
    ]
    assert first["findings"][0]["note"] and first["cost"] > 0 and len(first["questions"]) == 1
    kinds = [m["kind"] for m in store.messages(card.id)]
    assert (kinds.count("review"), kinds.count("review_reply")) == (2, 1)
    # the analyst asked the owner what it needs for the finding left: the brief waits for the answer
    brief = briefs.state(store, store.card(card.id))
    assert (brief["state"], brief["council"]["state"], brief["council"]["open_findings"]) == (
        "waiting_user", "needs_writer", 1,
    )
    assert api.dispatch(svc.ctx, owner, {"op": "card.get", "card": card.id})["next"]["what"] == "wait_decision"
    left = store._conn.execute("SELECT COUNT(*) FROM jobs WHERE state NOT IN ('done','failed','cancelled')").fetchone()[0]
    assert left == 0
    waiting = [c for c in store.commands(agent="analyst", limit=200) if c["thread"] == card.thread and c["state"] == "queued"]
    assert waiting == []


def test_the_seed_grew_over_days_and_its_decisions_are_live(tmp_path):
    """A board written over the past days, oldest first (the feed is not «0 min» everywhere); the decisions are
    the newest part and none has expired; every agent is on the dev host, so none is of unknown state."""
    from kd.cabinet import api, team, users

    svc = devseed.dev_service(str(tmp_path / "state"))
    devseed.apply(svc, out=io.StringIO())
    store, now = svc.ctx.store, users.parse_time(svc.now())
    created = [users.parse_time(c.created) for c in sorted(store.cards(), key=lambda c: int(c.id.split("-")[1]))]
    assert created == sorted(created)
    assert (now - created[0]).days >= 5 and (now - created[-1]).total_seconds() > 3600
    events = [r[0] for r in store._conn.execute("SELECT ts FROM events ORDER BY seq")]
    assert events == sorted(events) and max(users.parse_time(e) for e in events) <= now
    for decision in svc.decisions.list(state="open"):
        assert decision["expires"] == "" or users.parse_time(decision["expires"]) > now
    owner = api.Principal("user", -1, "owner")
    cards = [api.compact(r) for r in store.cards()]
    states = {
        name: team.facts(store, name, cards=cards, spaces=api.agent_spaces(svc.ctx, owner), hosted=svc.ctx.host_config().hosted(name),
                         now=svc.now())["state"]
        for name in devseed.AGENTS
    }
    assert "unknown" not in states.values() and api.allowed(svc.ctx, owner, "agents.chat")


def test_a_clone_without_host_json_reads_the_example(tmp_path, monkeypatch):
    """a clone of the public repository has no deployment's host/host.json; the dev cabinet and its seed
    read the synthetic host/host.example.json (a critical action needs the production alias)."""
    from kd import naming

    monkeypatch.delenv("KD_HOST_JSON", raising=False)
    monkeypatch.setattr(naming, "HOST_JSON", str(tmp_path / "absent" / "host.json"))
    svc = devseed.dev_service(str(tmp_path / "state"))
    assert naming.host_json_path() == str(devseed.EXAMPLE_HOST)
    devseed.apply(svc, out=io.StringIO())
    assert {d["type"] for d in svc.decisions.list(state="open")} >= {"admin_critical"}


def test_the_dev_projects_code_can_have_a_council(tmp_path):
    """a code council needs the project's repository and the developer's worker slots; the dev cabinet
    has both for its code project — its own registry, never the host's."""
    from kd.cabinet import api

    state = tmp_path / "state"
    svc = devseed.dev_service(str(state))
    owner = api.Principal("user", -1, "owner")
    devseed.apply(svc, out=io.StringIO())
    assert svc.ctx.repos_path == str(state / "repos.json")
    code = {"code": {"critics": ["claude:opus"]}}
    out = api.dispatch(svc.ctx, owner, {"op": "project.update", "id": "digipaddock", "council": code})
    assert out["project"]["council"]["code"]["critics"] == ["claude:opus"]
    try:
        api.dispatch(svc.ctx, owner, {"op": "project.update", "id": "alert", "council": code})
    except api.ApiError as exc:
        assert "project alert has no repository" in str(exc)
    else:
        raise AssertionError("a project without a repository took a code council")
