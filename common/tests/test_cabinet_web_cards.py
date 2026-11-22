"""cards, threads, files, inbox, feed and agents over HTTP, each seen through the person's projects."""

import json

import pytest
from web_env import WebEnv

from kd.cabinet import decisions


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    w.cfg = w.svc.cfg = type(w.cfg)(
        **{**w.cfg.__dict__, "files_dir": str(tmp_path / "files")}
    )
    return w


def _anna(w):
    anna = w.other_client()
    me = w.login("anna", client=anna)
    return anna, w.headers(me["csrf"])


def test_owner_creates_lists_edits_and_comments(w):
    w.login()
    r = w.client.post(
        "/api/cards",
        headers=w.headers(),
        json={"title": "Кеш nginx", "project": "digipaddock"},
    )
    assert r.status_code == 201
    card = r.json()
    assert card["status"] == "Inbox" and card["created_by"] == "@owner"
    assert [
        c["id"] for c in w.client.get("/api/cards", params={"project": "digipaddock"}).json()
    ] == [card["id"]]
    assert [
        c["id"] for c in w.client.get("/api/cards", params={"q": "nginx"}).json()
    ] == [card["id"]]
    assert (
        w.client.get("/api/cards", params={"q": "nginx", "status": "Done"}).json() == []
    )
    moved = w.client.patch(
        f"/api/cards/{card['id']}",
        headers=w.headers(),
        json={"revision": card["revision"], "changes": {"status": "To Do"}, "override_reason": "сразу в работу"},
    )
    assert moved.status_code == 200 and moved.json()["status"] == "To Do"
    stale = w.client.patch(
        f"/api/cards/{card['id']}",
        headers=w.headers(),
        json={"revision": card["revision"], "changes": {"status": "Done"}, "override_reason": "готово"},
    )
    assert stale.status_code == 409 and stale.json()["error"] == "conflict"
    r = w.client.post(
        f"/api/cards/{card['id']}/messages",
        headers=w.headers(),
        json={"body": "Посмотри логи"},
    )
    assert (
        r.status_code == 200
        and r.json()["author"] == "user:owner"
        and r.json()["author_name"] == "Владелец"
    )
    messages = w.client.get(f"/api/cards/{card['id']}/messages").json()["messages"]
    assert [m["body"] for m in messages] == [
        "[owner] вручную: Inbox → To Do: сразу в работу",
        "Посмотри логи",
    ] and messages[-1]["author_name"] == "Владелец"
    assert (
        w.client.get(
            f"/api/cards/{card['id']}/messages", params={"after": messages[-1]["seq"]}
        ).json()["messages"]
        == []
    )
    kinds = [e["kind"] for e in w.client.get(f"/api/cards/{card['id']}/events").json()]
    assert kinds[0] == "card.created" and "card.changed" in kinds


def test_thread_and_history_pages_over_http(w):
    w.login()
    card = w.client.post(
        "/api/cards", headers=w.headers(), json={"title": "Длинный тред", "project": "digipaddock"}
    ).json()
    for i in range(5):
        w.client.post(f"/api/cards/{card['id']}/messages", headers=w.headers(), json={"body": f"m{i}"})
    page = w.client.get(f"/api/cards/{card['id']}/messages", params={"limit": 2}).json()
    assert [m["body"] for m in page["messages"]] == ["m3", "m4"] and page["more"] is True
    assert page["messages"][0]["author_name"] == "Владелец" and page["watermark"] > 0
    older = w.client.get(
        f"/api/cards/{card['id']}/messages",
        params={"before": page["messages"][0]["seq"], "limit": 2},
    ).json()
    assert [m["body"] for m in older["messages"]] == ["m1", "m2"]
    assert w.client.get(f"/api/cards/{card['id']}/messages", params={"limit": 0}).status_code == 400
    events = w.client.get(f"/api/cards/{card['id']}/events", params={"limit": 2}).json()
    assert len(events["events"]) == 2 and events["next"] == events["events"][-1]["seq"]
    assert events["events"][0]["seq"] > events["events"][1]["seq"]
    rest = w.client.get(
        f"/api/cards/{card['id']}/events", params={"before": events["next"], "limit": 100}
    ).json()
    assert rest["next"] is None and rest["events"][-1]["kind"] == "card.created"


def test_the_result_over_http(w):
    w.login()
    card = w.client.post(
        "/api/cards", headers=w.headers(), json={"title": "Итог", "project": "digipaddock"}
    ).json()
    w.env.store.edit_card(card["id"], None, {"status": "Review", "summary": "PR https://forge.example/pr/1"}, actor="agent:dev")
    found = w.client.get(f"/api/cards/{card['id']}/result").json()
    assert found["links"] == ["https://forge.example/pr/1"]
    (review,) = found["reviews"]
    assert review["by_name"] == "@dev" and review["outcome"] == "pending" and review["decided_by_name"] == ""
    anna, _ = _anna(w)
    assert anna.get(f"/api/cards/{card['id']}/result").status_code == 404


def test_checklists_save_through_the_card(w):
    w.login()
    card = w.client.post(
        "/api/cards", headers=w.headers(), json={"title": "AC", "project": "digipaddock"}
    ).json()
    changes = {
        "acceptance": [
            {"text": "one", "checked": False},
            {"text": "two", "checked": True},
        ],
        "dod": "- [ ] #1 Ревью\n- [x] #2 Тесты",
    }
    r = w.client.patch(
        f"/api/cards/{card['id']}",
        headers=w.headers(),
        json={"revision": card["revision"], "changes": changes},
    )
    assert r.status_code == 200, r.text
    got = w.client.get(f"/api/cards/{card['id']}").json()
    assert [a["checked"] for a in got["acceptance"]] == [False, True] and got[
        "dod"
    ] == changes["dod"]
    bad = w.client.patch(
        f"/api/cards/{card['id']}",
        headers=w.headers(),
        json={
            "revision": got["revision"],
            "changes": {"acceptance": [{"checked": True}]},
        },
    )
    assert bad.status_code == 400


def test_a_partner_does_not_see_other_projects(w):
    w.login()
    card = w.client.post(
        "/api/cards", headers=w.headers(), json={"title": "Тайна", "project": "digipaddock"}
    ).json()
    anna, headers = _anna(w)
    assert anna.get(f"/api/cards/{card['id']}").status_code == 404
    assert anna.get(f"/api/cards/{card['id']}/messages").status_code == 404
    assert anna.get(f"/api/cards/{card['id']}/events").status_code == 404
    patch = anna.patch(
        f"/api/cards/{card['id']}",
        headers=headers,
        json={"revision": 1, "changes": {"title": "x"}},
    )
    assert patch.status_code == 404
    upload = anna.post(
        f"/api/cards/{card['id']}/files",
        headers=headers,
        files={"file": ("a.txt", b"x", "text/plain")},
    )
    assert upload.status_code == 404
    assert anna.get("/api/cards").json() == []
    # the cabinet's table, with its names and the bound repository (empty until a repository is attached)
    assert anna.get("/api/projects").json() == [
        {"id": "web", "name": "WEB", "route": "@owner", "repo": "", "council": {}}]


def test_files_upload_and_download_as_attachments(w):
    w.login()
    card = w.client.post(
        "/api/cards", headers=w.headers(), json={"title": "Лог", "project": "web"}
    ).json()
    data = bytes(range(256)) * 4
    r = w.client.post(
        f"/api/cards/{card['id']}/files",
        headers=w.headers(),
        files={"file": ("../../etc/log.txt", data, "text/plain")},
    )
    assert r.status_code == 200, r.text
    message = r.json()
    [sent] = message["payload"]["files"]
    assert message["kind"] == "file" and sent["name"] == "log.txt" and sent["size"] == 1024
    message["payload"] = sent  # the old single-file shape for the checks below
    got = w.client.get(f"/api/files/{message['payload']['id']}")
    assert got.status_code == 200 and got.content == data
    assert (
        got.headers["content-disposition"].startswith("attachment;")
        and "log.txt" in got.headers["content-disposition"]
    )
    assert (
        got.headers["x-content-type-options"] == "nosniff"
        and got.headers["cache-control"] == "private, no-store"
    )
    html = w.client.post(
        f"/api/cards/{card['id']}/files",
        headers=w.headers(),
        files={"file": ("x.html", b"<script>", "text/html")},
    ).json()
    assert (
        w.client.get(f"/api/files/{html['payload']['files'][0]['id']}").headers["content-type"]
        == "application/octet-stream"
    )
    big = w.client.post(
        f"/api/cards/{card['id']}/files",
        headers=w.headers(),
        files={"file": ("big.bin", b"\0" * (21 << 20), "x/y")},
    )
    assert big.status_code == 413
    anna, _ = _anna(w)
    assert (
        anna.get(f"/api/files/{message['payload']['id']}").status_code == 200
    )  # ops is anna's project
    secret = w.client.post(
        "/api/cards", headers=w.headers(), json={"title": "Тайна", "project": "digipaddock"}
    ).json()
    hidden = w.client.post(
        f"/api/cards/{secret['id']}/files",
        headers=w.headers(),
        files={"file": ("s.txt", b"s", "text/plain")},
    ).json()
    assert anna.get(f"/api/files/{hidden['payload']['files'][0]['id']}").status_code == 404
    assert w.client.get("/api/files/" + "0" * 32).status_code == 404


def test_assignees_are_the_routes_agents_the_owner_and_the_coordinator(w):
    w.login()
    assert w.client.get("/api/assignees").json() == ["@analyst", "@boss", "@dev", "@owner", "@sysadmin"]


def test_the_big_buttons_start_a_card_and_ask_for_a_brief(w):
    card = w.env.card(project="digipaddock", status="Inbox", assignee="")
    w.login()
    r = w.client.post(
        f"/api/cards/{card.id}/start", headers=w.headers(), json={"revision": card.revision}
    )
    assert r.status_code == 200 and r.json()["status"] == "To Do" and r.json()["assignee"] == "@dev"
    stale = w.client.post(
        f"/api/cards/{card.id}/brief", headers=w.headers(), json={"revision": card.revision}
    )
    assert stale.status_code == 409
    r = w.client.post(
        f"/api/cards/{card.id}/brief", headers=w.headers(), json={"revision": r.json()["revision"]}
    )
    assert r.status_code == 200 and r.json()["assignee"] == "@analyst" and r.json()["status"] == "Inbox"
    kinds = [e["kind"] for e in w.client.get(f"/api/cards/{card.id}/events").json()]
    assert kinds.count("card.wake") == 1
    anna, headers = _anna(w)
    assert anna.post(f"/api/cards/{card.id}/start", headers=headers, json={"revision": 1}).status_code == 404


def test_inbox_holds_what_waits_for_me(w):
    d = decisions.Decisions(w.env.store)
    ops_card, main_card = (
        w.env.card(project="web"),
        w.env.card(project="digipaddock", status="Review"),
    )
    mine = d.open(
        "question",
        card=ops_card.id,
        payload={"text": "Лимит?"},
        requested_by="agent:analyst",
        now=w.env.now,
    )
    other = d.open(
        "question",
        card=main_card.id,
        payload={"text": "Сейчас?"},
        requested_by="agent:dev",
        now=w.env.now,
    )
    w.login()
    inbox = w.client.get("/api/inbox").json()
    assert {q["id"] for q in inbox["questions"]} == {mine["id"], other["id"]} and inbox[
        "decisions"
    ] == []
    assert [c["id"] for c in inbox["review"]] == [main_card.id]
    anna, _ = _anna(w)
    inbox = anna.get("/api/inbox").json()
    assert [q["id"] for q in inbox["questions"]] == [mine["id"]] and inbox[
        "review"
    ] == []
    decided = w.client.get(f"/api/cards/{ops_card.id}/decisions").json()
    assert [x["id"] for x in decided] == [mine["id"]]


def test_the_feed_of_a_partner_has_only_her_projects(w):
    w.env.card(project="digipaddock", title="Чужая")
    ops = w.env.card(project="web", title="Своя")
    anna, _ = _anna(w)
    feed = anna.get("/api/feed").json()
    cards = {e["card"] for e in feed["events"] if e["card"]}
    assert cards == {ops.id} and feed["last"] >= 1
    w.login()
    everything = w.client.get("/api/feed", params={"kinds": "card.created"}).json()[
        "events"
    ]
    assert {e["payload"]["title"] for e in everything} == {"Чужая", "Своя"}
    only = w.client.get("/api/feed", params={"project": "web"}).json()["events"]
    assert {e["card"] for e in only} == {ops.id}
    newer = w.client.get("/api/feed", params={"after": feed["last"]}).json()["events"]
    assert all(e["seq"] > feed["last"] for e in newer)
    first = w.client.get("/api/feed", params={"limit": 1}).json()
    assert len(first["events"]) == 1 and first["events"][0]["seq"] == max(
        e["seq"] for e in everything + newer
    )
    older = w.client.get(
        "/api/feed", params={"before": first["events"][0]["seq"], "limit": 100}
    ).json()["events"]
    assert older and all(e["seq"] < first["events"][0]["seq"] for e in older)


def test_forward_feed_reports_a_gap_after_an_empty_filtered_scan(w, monkeypatch):
    from kd.cabinet.web import feed as feed_module

    w.login()
    after = w.env.store.last_seq()
    for n in range(3):
        w.env.card(project="digipaddock", title=f"Hidden {n}")
    mine = w.env.card(project="web", title="Visible after the gap")
    monkeypatch.setattr(feed_module, "PAGE", 2)
    monkeypatch.setattr(feed_module, "SCAN_MAX", 1)
    first = w.client.get("/api/feed", params={"after": after, "project": "web"}).json()
    assert first["events"] == [] and first["last"] > after and first["more"] is True
    second = w.client.get("/api/feed", params={"after": first["last"], "project": "web"}).json()
    assert [e["card"] for e in second["events"]] == [mine.id]
    assert second["more"] is False


def test_the_feed_shows_each_person_only_their_own_digest(w):
    with w.env.store._tx():
        for user in ("owner", "anna"):
            w.env.store._event(
                "system", "digest", "", {"user": user, "key": "push.digest.body"}
            )
    w.login()
    mine = w.client.get("/api/feed", params={"kinds": "digest"}).json()["events"]
    assert [e["payload"]["user"] for e in mine] == ["owner"]
    anna, _ = _anna(w)
    hers = anna.get("/api/feed", params={"kinds": "digest"}).json()["events"]
    assert [e["payload"]["user"] for e in hers] == ["anna"]


def test_hindsight_spend_is_in_the_ledger_not_among_the_agents(w, tmp_path):
    """kd-usage's category hindsight goes to /api/usage as is; the agents and their spend never show it."""
    usage = tmp_path / "usage.jsonl"
    rows = [
        {"day": "2026-10-14", "agent": "dev", "model": "a", "requests": 3, "cost_usd": 0.5},
        {"day": "2026-10-14", "agent": "hindsight", "category": "hindsight", "coverage": "full",
         "model": "claude-haiku-4-5", "requests": 40, "input": 10, "output": 5, "cache_write": 1, "cache_read": 0},
        {"day": "2026-10-14", "agent": "dev", "category": "hindsight", "coverage": "full", "model": "x",
         "requests": 7},  # even under an agent's name, the category is not the agent's spend
    ]
    usage.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    w.svc.usage_path = str(usage)
    w.login()
    agents = {a["name"]: a for a in w.client.get("/api/agents").json()}
    assert "hindsight" not in agents and agents["dev"]["usage_today"] == {"cost": 0.5, "calls": 3}
    assert w.client.get("/api/agents/hindsight").status_code == 404
    ledger = w.client.get("/api/usage", params={"days": 7}).json()
    assert [r.get("category") for r in ledger] == [None, "hindsight", "hindsight"]


def test_usage_prices_a_cabinet_models_rows_by_its_table(w, tmp_path):
    """a ledger row under a cabinet model's slug without a cost is priced by the model's table."""
    w.env.store.create_llm_model(
        {"id": "or-x", "provider": "openrouter", "base_url": "", "upstream": "anthropic/claude-sonnet-4.5",
         "title": "T", "price_in": 2, "price_out": 10, "price_cache_read": 0.2, "price_cache_write": 2.5,
         "context": 200000}, actor="user:owner")
    usage = tmp_path / "usage.jsonl"
    rows = [{"day": "2026-10-14", "agent": "dev", "model": "or-x", "requests": 3,
             "input": 1000000, "output": 0, "cache_write": 0, "cache_read": 0}]
    usage.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    w.svc.usage_path = str(usage)
    w.login()
    [row] = w.client.get("/api/usage", params={"days": 7}).json()
    assert row["cost_usd"] == 2.0 and row["cost_source"] == "estimated" and row["price"]["source"] == "cabinet"
    agents = {a["name"]: a for a in w.client.get("/api/agents").json()}
    assert agents["dev"]["usage_today"] == {"cost": 2.0, "calls": 3}


def test_usage_month_prices_older_rows_and_includes_hindsight(w, tmp_path):
    usage = tmp_path / "usage.jsonl"
    rows = [
        {"day": "2026-10-14", "agent": "dev", "model": "claude-opus-5-5", "cost_usd": 7},
        {"day": "2026-10-01", "agent": "dev", "model": "claude-opus-4-8",
         "input": 1000000, "output": 0, "cache_write": 0, "cache_read": 0},
        {"day": "2026-10-14", "agent": "hindsight", "category": "hindsight", "coverage": "full",
         "model": "claude-haiku-4-5-20251001", "input": 0, "output": 1000000, "cache_write": 0, "cache_read": 0},
        {"day": "2026-10-15", "agent": "dev", "cost_usd": 999},  # a future row is outside both periods
    ]
    usage.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    w.svc.usage_path = str(usage)
    w.login()
    week = w.client.get("/api/usage", params={"days": 7}).json()
    month = w.client.get("/api/usage", params={"days": 30}).json()
    assert sum(r["cost_usd"] for r in week) == 12
    assert sum(r["cost_usd"] for r in month) == 17
    assert month[1]["cost_source"] == "estimated" and month[1]["price"]["input"] == 5
    assert month[2]["cost_source"] == "estimated"
    assert json.loads(usage.read_text().splitlines()[1]).get("cost_usd") is None


def test_agents_show_their_cards_activity_and_spend(w, tmp_path):
    usage = tmp_path / "usage.jsonl"
    rows = [
        {
            "day": "2026-10-14",
            "agent": "dev",
            "model": "a",
            "requests": 3,
            "cost_usd": 0.5,
        },
        {
            "day": "2026-10-14",
            "agent": "dev",
            "model": "b",
            "requests": 2,
            "cost_usd": 0.25,
        },
        {
            "day": "2026-10-13",
            "agent": "dev",
            "model": "a",
            "requests": 9,
            "cost_usd": 9.0,
        },
    ]
    usage.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    w.svc.usage_path = str(usage)
    card = w.env.card(project="digipaddock", status="In Progress", assignee="@dev")
    w.env.card(project="digipaddock", status="To Do", assignee="@dev")
    w.env.store.add_message(card.id, author="agent:dev", kind="progress", body="step 1")
    w.login()
    agents = {a["name"]: a for a in w.client.get("/api/agents").json()}
    assert set(agents) == {"analyst", "dev", "sysadmin", "boss"}
    dev = agents["dev"]
    assert "user" not in dev  # no Linux names to the page
    assert dev["cards"] == {"inbox": 0, "to_do": 1, "in_progress": 1, "review": 0, "blocked": 0}
    assert dev["usage_today"] == {"cost": 0.75, "calls": 5}
    # not on the session host: the cabinet does not watch it, a progress line is no proof of work (R23)
    assert dev["state"] == "unknown" and dev["host"] is None
    assert dev["facts"]["progress"] == w.env.now and agents["boss"]["usage_today"] is None
    one = w.client.get("/api/agents/dev").json()
    assert {c["id"] for c in one["cards"]} >= {card.id}
    assert [(p["card"], p["body"]) for p in one["progress"]] == [(card.id, "step 1")]
    assert w.client.get("/api/agents/nobody").status_code == 404
    assert len(w.client.get("/api/usage", params={"days": 7}).json()) == 3
    anna, _ = _anna(w)
    assert anna.get("/api/usage").status_code == 403
    assert set(anna.get("/api/agents").json()[0]["cards"].values()) == {0}
    assert anna.get("/api/agents/dev").json()["facts"]["progress"] == ""
    assert anna.get("/api/agents/dev").json()["progress"] == []
    # spend is the owner's business: without settings a person sees no money
    assert {a["usage_today"] for a in anna.get("/api/agents").json()} == {None}
    assert anna.get("/api/agents/dev").json()["usage_today"] is None


def test_uploads_stop_at_the_quota(w, monkeypatch):
    """Global review 15.10.2026: files share the volume with the base — a person, and everyone, has a cap."""
    from kd.cabinet import api

    w.login()
    card = w.client.post("/api/cards", headers=w.headers(), json={"title": "Лог", "project": "web"}).json()
    monkeypatch.setattr(api, "FILES_USER_MAX", 1500)
    for size, status in ((1024, 200), (1024, 403), (400, 200), (100, 403)):
        r = w.client.post(
            f"/api/cards/{card['id']}/files", headers=w.headers(), files={"file": ("a.bin", b"x" * size, "text/plain")}
        )
        assert r.status_code == status, (size, r.text)
    assert w.env.store.files_size("user:owner") == 1424 == w.env.store.files_size()
    stored = {p.name for p in __import__("pathlib").Path(w.cfg.files_dir).iterdir()}
    assert len(stored) == 2  # a refused upload leaves no file behind
    monkeypatch.setattr(api, "FILES_USER_MAX", 10**9)
    monkeypatch.setattr(api, "FILES_TOTAL_MAX", 1500)
    r = w.client.post(
        f"/api/cards/{card['id']}/files", headers=w.headers(), files={"file": ("b.bin", b"x" * 100, "text/plain")}
    )
    assert r.status_code == 403 and "owner" in r.json()["message"]


def test_file_names_lose_their_path_and_control_characters():
    from kd.cabinet.web import files

    assert files.clean_name("../../etc/log\x01.txt") == "log.txt"
    assert files.clean_name("C:\\Users\\x\\отчёт.pdf") == "отчёт.pdf"
    assert (
        files.clean_name("\x00\x1f") == "file"
        and len(files.clean_name("a" * 500)) == 200
    )


def test_reads_need_a_session(w):
    for path in (
        "/api/cards",
        "/api/inbox",
        "/api/feed",
        "/api/agents",
        "/api/projects",
    ):
        assert w.client.get(path).status_code == 401, path


def test_the_feed_shows_an_edit_with_its_comment_once(w):
    """the comment of an agent's edit is a live thread message, but the feed keeps one line."""
    card = w.env.card(project="web", assignee="@dev", status="In Progress")
    w.env.call("digi", {"op": "worker.edit", "agent": "dev", "card": card.id, "comment": "шаг"})
    w.login()
    events = w.client.get(f"/api/feed?card={card.id}").json()["events"]
    assert [e["kind"] for e in events] == ["card.changed", "card.created"]


def test_messages_come_with_the_journal_watermark(w):
    """The snapshot says up to which journal event it is current; ``after`` in SQL."""
    w.login()
    card = w.client.post(
        "/api/cards", headers=w.headers(), json={"title": "Кеш", "project": "digipaddock"}
    ).json()["id"]
    for text in ("раз", "два"):
        w.client.post(f"/api/cards/{card}/messages", headers=w.headers(), json={"body": text})
    body = w.client.get(f"/api/cards/{card}/messages?after=1").json()
    assert [m["body"] for m in body["messages"]] == ["два"]
    assert body["watermark"] == w.env.store.last_seq()
    assert [m["body"] for m in w.env.store.messages(card, after=1)] == ["два"]


def test_the_dev_executor_runs_an_agents_write_only_in_dev(w, tmp_path):
    """e2e plays an agent through the dev cabinet; the production app has no such route."""
    import dataclasses

    from fastapi.testclient import TestClient

    from kd.cabinet.web import app as web_app

    card = w.env.card(project="web", assignee="@dev", status="In Progress")
    w.login()
    r = w.client.post(
        "/api/dev/executor",
        headers=w.headers(),
        json={"op": "worker.edit", "agent": "dev", "card": card.id, "comment": "шаг"},
    )
    assert r.status_code == 200, r.text
    assert w.env.store.messages(card.id)[-1]["body"] == "[dev] шаг"
    assert w.client.post("/api/dev/executor", json={"op": "card.list"}).status_code == 403  # no CSRF
    w.svc.cfg = dataclasses.replace(w.cfg, dev=False)
    prod = TestClient(web_app.create_app(w.svc), base_url=w.cfg.origin, headers={"Origin": w.cfg.origin})
    assert prod.post("/api/dev/executor", json={"op": "card.list"}).status_code == 404


def test_card_answer_carries_its_actions(w):
    card = w.env.card(project="digipaddock", status="Review", assignee="@dev")
    w.login()
    got = w.client.get(f"/api/cards/{card.id}").json()
    assert [(a["id"], a["primary"], a["comment"]) for a in got["actions"]] == [
        ("accept", True, "none"), ("return", False, "required")]
    refused = w.client.patch(
        f"/api/cards/{card.id}", headers=w.headers(), json={"revision": card.revision, "changes": {"status": "Done"}}
    )
    assert refused.status_code == 403 and refused.json()["reason"] == "use_action"
    anna, headers = _anna(w)
    ops_card = w.env.card(project="web", status="Review", assignee="@dev")
    assert anna.get(f"/api/cards/{ops_card.id}").json()["actions"] == []  # a partner does not move cards


def test_act_route(w):
    card = w.env.card(project="digipaddock", status="Review", assignee="@dev")
    w.login()
    r = w.client.post(f"/api/cards/{card.id}/act", headers=w.headers(),
                      json={"action": "return", "revision": card.revision})
    assert r.status_code == 400  # a return needs the comment
    r = w.client.post(f"/api/cards/{card.id}/act", headers=w.headers(),
                      json={"action": "return", "revision": card.revision, "comment": "Нет теста"})
    assert r.status_code == 200
    body = r.json()
    assert body["card"]["status"] == "In Progress" and body["card"]["actions"] == []
    assert body["commands"] == []  # dev is not on the session host here: its panel is woken by kd-board sync
    dep = w.env.card(project="digipaddock", status="To Do")
    blocked = w.env.card(project="digipaddock", status="Blocked", dependencies=[dep.id])
    r = w.client.post(f"/api/cards/{blocked.id}/act", headers=w.headers(),
                      json={"action": "resume", "revision": blocked.revision})
    assert r.status_code == 403 and r.json()["reason"] == "deps_open"


def test_act_passes_the_brief_version(w):
    card = w.env.briefed(project="digipaddock")
    w.env.ctx._specs = {"@dev": frozenset({"implementer"})}  # the registry's answer for the card's spec label
    w.login()
    assert w.client.get(f"/api/cards/{card.id}").json()["actions"][0]["brief_version"] == 1
    r = w.client.post(f"/api/cards/{card.id}/act", headers=w.headers(),
                      json={"action": "start", "revision": card.revision, "brief_version": 0})
    assert r.status_code == 409 and r.json()["reason"] == "brief_changed"
    r = w.client.post(f"/api/cards/{card.id}/act", headers=w.headers(),
                      json={"action": "start", "revision": card.revision, "brief_version": 1})
    assert r.status_code == 200 and r.json()["card"]["status"] == "To Do"


def test_get_brief(w):
    card = w.env.briefed(project="digipaddock")
    w.login()
    r = w.client.get(f"/api/cards/{card.id}/brief")
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"brief", "versions", "current", "changes"}
    assert body["brief"]["state"] == "ready" and [v["version"] for v in body["versions"]] == [1]
    anna, _ = _anna(w)
    assert anna.get(f"/api/cards/{card.id}/brief").status_code == 404


def test_a_card_is_created_with_its_optional_fields_over_http(w):
    # the new card's form sends what an edit could set, and the card is made with it
    me = w.login()
    body = {"title": "Форма", "project": "web", "description": "Зачем", "assignee": "@dev", "labels": ["ui"],
            "acceptance": [{"text": "A"}], "milestone": "m-2", "model": "claude:opus", "effort": "low",
            "council": {"brief": {"critics": ["claude:opus"], "rounds": 1}}, "status": "Done"}
    r = w.client.post("/api/cards", json=body, headers=w.headers(me["csrf"]))
    assert r.status_code == 201, r.text
    card = r.json()
    assert (card["status"], card["assignee"], card["labels"]) == ("Inbox", "@dev", ["ui"])
    assert "milestone" not in card  # an older page still sends it; a card has none
    assert w.client.get("/api/milestones").status_code == 404
    assert [c["id"] for c in w.client.get("/api/cards?milestone=m-2").json()] == [card["id"]]  # no such filter
    assert (card["model"], card["effort"]) == ("claude:opus", "low")
    full = w.client.get(f"/api/cards/{card['id']}").json()
    assert [a["text"] for a in full["acceptance"]] == ["A"]
    assert full["council"]["brief"]["critics"] == ["claude:opus"]
    anna, headers = _anna(w)
    r = anna.post("/api/cards", json={"title": "x", "project": "web", "model": "claude:opus"}, headers=headers)
    assert r.status_code == 403, r.text
