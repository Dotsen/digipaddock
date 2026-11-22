"""The overview over HTTP — the person's sections, their «seen» mark, rights before counts."""

import pytest
from web_env import WebEnv


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web"])], actor="root")
    return w


def test_the_dashboard_and_its_seen_mark(w):
    review = w.env.card(project="digipaddock", status="In Progress", title="R")
    w.env.store.edit_card(review.id, None, {"status": "Review"}, actor="agent:dev")
    w.login()
    found = w.client.get("/api/dashboard").json()
    assert [r["card"] for r in found["needs"]] == [review.id]
    assert {c["actor_name"] for c in found["changes"]} >= {"@dev"}
    assert found["seen"] == 0 and found["counts"]["changes_new"] == len(found["changes"])
    assert w.client.post("/api/dashboard/seen", json={"seq": found["watermark"]}).status_code == 403  # no CSRF
    r = w.client.post("/api/dashboard/seen", headers=w.headers(), json={"seq": found["watermark"]})
    assert r.status_code == 200 and r.json() == {"overview_seen": found["watermark"]}
    again = w.client.get("/api/dashboard", params={"period": "7d"}).json()
    assert again["seen"] == found["watermark"] and again["counts"]["changes_new"] == 0
    assert w.client.get("/api/dashboard", params={"period": "2d"}).status_code == 400
    bad = w.client.post("/api/dashboard/seen", headers=w.headers(), json={"seq": "x"})
    assert bad.status_code == 400


def test_a_partner_sees_her_projects_only(w):
    w.env.card(project="digipaddock", status="Review", title="Secret")
    mine = w.env.card(project="web", status="In Progress", title="Ops")
    anna = w.other_client()
    w.login("anna", client=anna)
    found = anna.get("/api/dashboard").json()
    assert [r["card"] for r in found["work"]] == [mine.id] and found["needs"] == []
    assert {c["card"] for c in found["changes"]} == {mine.id}
    assert anna.get("/api/dashboard", params={"project": "digipaddock"}).json()["changes"] == []
