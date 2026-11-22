"""the models of other providers over HTTP — /api/models, the model fields of projects and cards."""

import json
from pathlib import Path

import pytest
from web_env import WebEnv

MODEL = {"id": "or-gpt5", "provider": "openrouter", "upstream": "openai/gpt-5", "title": "GPT-5 via OpenRouter",
         "price_in": 1.25, "price_out": 10, "price_cache_read": 0.125, "price_cache_write": 1.25,
         "context": 400000, "base_url": ""}


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web", "digipaddock"])], actor="root")
    w.env.ctx.llm_env_dir = str(tmp_path / "llm")
    w.env.ctx.claude_env = str(tmp_path / "claude.env")  # never the host's own token file
    return w


def _anna(w):
    anna = w.other_client()
    me = w.login("anna", client=anna)
    return anna, w.headers(me["csrf"])


def test_the_owner_manages_models_and_a_partner_only_sees_them(w, tmp_path):
    w.login()
    r = w.client.post("/api/models", headers=w.headers(), json=MODEL)
    assert r.status_code == 200, r.text
    made = r.json()
    assert made["id"] == "or-gpt5" and made["key"] is False and made["experimental"] is True and made["probe"] == {}
    # the subscription's models come first, builtin; the cabinet's own after them
    assert [m["id"] for m in w.client.get("/api/models").json()] == [
        "claude:opus", "claude:sonnet", "claude:haiku", "claude:fable", "or-gpt5"]
    assert w.client.get("/api/models").json()[0]["builtin"] is True
    r = w.client.patch("/api/models/claude:opus", headers=w.headers(), json={"title": "x"})
    assert r.status_code == 403 and r.json()["reason"] == "model_builtin"
    assert w.client.delete("/api/models/claude:opus", headers=w.headers()).status_code == 403
    r = w.client.post("/api/models/claude:opus/probe", headers=w.headers())
    assert r.status_code == 200 and r.json()["probe"]["state"] == "requested"
    assert w.client.post("/api/models", headers=w.headers(), json=MODEL).status_code == 400  # exists
    assert w.client.post("/api/models", headers=w.headers(), json={**MODEL, "id": "sonnet"}).status_code == 400
    r = w.client.patch("/api/models/or-gpt5", headers=w.headers(), json={"title": "GPT-5", "enabled": 0, "price_in": 2})
    assert r.status_code == 200 and (r.json()["title"], r.json()["enabled"], r.json()["price_in"]) == ("GPT-5", 0, 2)
    assert w.client.patch("/api/models/or-gpt5", headers=w.headers(), json={"provider": "fireworks"}).status_code == 400
    assert w.client.patch("/api/models/or-gpt5", headers=w.headers(), json={"probe": {}}).status_code == 400
    assert w.client.patch("/api/models/none", headers=w.headers(), json={"title": "x"}).status_code == 404
    # the key file of the kind shows as key: true
    (tmp_path / "llm").mkdir()
    (tmp_path / "llm" / "openrouter.env").write_text("ANTHROPIC_AUTH_TOKEN=x\n", encoding="utf-8")
    assert w.client.get("/api/models").json()[-1]["key"] is True
    # a partner: the names, not the prices; no writes
    anna, headers = _anna(w)
    listed = anna.get("/api/models").json()
    assert listed[-1] == {"id": "or-gpt5", "title": "GPT-5", "provider": "openrouter", "enabled": 0, "key": True,
                          "experimental": True, "builtin": False, "version": ""}
    assert listed[0] == {"id": "claude:opus", "title": "Claude Opus", "provider": "subscription", "enabled": 1,
                         "key": False, "experimental": False, "builtin": True, "version": ""}
    assert anna.post("/api/models", headers=headers, json={**MODEL, "id": "x"}).status_code == 403
    assert anna.patch("/api/models/or-gpt5", headers=headers, json={"title": "x"}).status_code == 403
    assert anna.delete("/api/models/or-gpt5", headers=headers).status_code == 403
    assert anna.post("/api/models/or-gpt5/probe", headers=headers).status_code == 403
    r = w.client.delete("/api/models/or-gpt5", headers=w.headers())
    assert r.status_code == 200 and r.json() == {"ok": True, "deleted": "or-gpt5"}
    assert [m for m in w.client.get("/api/models").json() if not m["builtin"]] == []


def test_the_subscription_key_is_the_claude_token_file(w, tmp_path):
    w.login()

    def opus():
        return next(m for m in w.client.get("/api/models").json() if m["id"] == "claude:opus")["key"]

    assert opus() is False
    (tmp_path / "claude.env").write_text("CLAUDE_CODE_OAUTH_TOKEN=x\n", encoding="utf-8")
    assert opus() is True


def test_the_providers_catalog_without_its_secrets(w, tmp_path):
    w.login()
    (tmp_path / "llm").mkdir()
    (tmp_path / "llm" / "fireworks.env").write_text("ANTHROPIC_AUTH_TOKEN=x\n", encoding="utf-8")
    kinds = w.client.get("/api/models/providers").json()
    assert {k["id"] for k in kinds} == {"anthropic", "openai", "openrouter", "fireworks", "deepseek", "custom"}
    fireworks = next(k for k in kinds if k["id"] == "fireworks")
    assert fireworks["key"] is True and fireworks["caching"] is False and fireworks["title"] == "Fireworks"
    assert fireworks["base_url"] == "https://api.fireworks.ai/inference" and fireworks["hint"]
    assert all("secret" not in k and k["key"] is (k["id"] == "fireworks") for k in kinds)
    anna, _ = _anna(w)
    assert anna.get("/api/models/providers").status_code == 200


def test_a_referenced_model_answers_409(w):
    w.login()
    w.client.post("/api/models", headers=w.headers(), json=MODEL)
    assert w.client.patch("/api/projects/digipaddock", headers=w.headers(), json={"model": "or-gpt5"}).status_code == 200
    r = w.client.delete("/api/models/or-gpt5", headers=w.headers())
    assert r.status_code == 409 and r.json()["reason"] == "model_referenced"
    assert w.client.get("/api/models").json()[-1]["id"] == "or-gpt5"


def test_a_probe_request_marks_the_model(w):
    w.login()
    w.client.post("/api/models", headers=w.headers(), json=MODEL)
    r = w.client.post("/api/models/or-gpt5/probe", headers=w.headers())
    assert r.status_code == 200 and r.json()["probe"]["state"] == "requested"


def test_projects_carry_the_defaults_with_settings_only(w):
    w.login()
    w.client.post("/api/models", headers=w.headers(), json=MODEL)
    r = w.client.patch("/api/projects/digipaddock", headers=w.headers(), json={"model": "or-gpt5", "effort": "low"})
    assert r.status_code == 200 and (r.json()["model"], r.json()["effort"]) == ("or-gpt5", "low")
    mine = next(p for p in w.client.get("/api/projects").json() if p["id"] == "digipaddock")
    assert (mine["model"], mine["effort"]) == ("or-gpt5", "low")
    assert w.client.patch("/api/projects/digipaddock", headers=w.headers(), json={"effort": "ultra"}).status_code == 400
    anna, headers = _anna(w)
    theirs = next(p for p in anna.get("/api/projects").json() if p["id"] == "digipaddock")
    assert "model" not in theirs and "effort" not in theirs
    assert anna.patch("/api/projects/digipaddock", headers=headers, json={"effort": "low"}).status_code == 403


def test_a_card_takes_a_model_and_effort_from_the_owner_only(w):
    w.login()
    w.client.post("/api/models", headers=w.headers(), json=MODEL)
    card = w.client.post("/api/cards", headers=w.headers(), json={"title": "На GPT", "project": "digipaddock"}).json()
    assert (card["model"], card["effort"]) == ("", "")
    r = w.client.patch(f"/api/cards/{card['id']}", headers=w.headers(),
                       json={"revision": card["revision"], "changes": {"model": "or-gpt5", "effort": "xhigh"}})
    assert r.status_code == 200, r.text
    assert (r.json()["model"], r.json()["effort"]) == ("or-gpt5", "xhigh")
    shown = w.client.get(f"/api/cards/{card['id']}").json()
    assert (shown["model"], shown["effort"]) == ("or-gpt5", "xhigh")
    anna, headers = _anna(w)
    r = anna.patch(f"/api/cards/{card['id']}", headers=headers,
                   json={"revision": shown["revision"], "changes": {"effort": "low"}})
    assert r.status_code == 403


def test_the_council_of_a_project_and_of_a_card_over_http(w):
    """the composition is the owner's to set (``settings``); a partner reads it — on the card and as the
    project's default — and may not change it."""
    w.login()
    # a code council reads the project's repository in the developer's worker slots
    w.env.ctx.repo_root = str(Path(__file__).resolve().parents[2])
    Path(w.env.ctx.repos_path).write_text(json.dumps({"digipaddock": {"repo": "acme/digipaddock"}}), "utf-8")
    council = {"code": {"critics": ["claude:opus"], "rounds": 3, "min_lines": 100}}
    r = w.client.patch("/api/projects/digipaddock", headers=w.headers(), json={"council": council})
    assert r.status_code == 200 and r.json()["council"] == council
    assert w.client.patch("/api/projects/digipaddock", headers=w.headers(),
                          json={"council": {"code": {"critics": ["nope"]}}}).status_code == 400
    card = w.client.post("/api/cards", headers=w.headers(), json={"title": "С советом", "project": "digipaddock"}).json()
    assert card["council"] == {}
    own = {"brief": {"critics": ["claude:sonnet", "claude:opus"], "rounds": 2}}
    r = w.client.patch(f"/api/cards/{card['id']}", headers=w.headers(),
                       json={"revision": card["revision"], "changes": {"council": own}})
    assert r.status_code == 200 and r.json()["council"] == own
    anna, headers = _anna(w)
    theirs = next(p for p in anna.get("/api/projects").json() if p["id"] == "digipaddock")
    assert theirs["council"] == council and "model" not in theirs
    shown = anna.get(f"/api/cards/{card['id']}").json()
    assert shown["council"] == own
    assert anna.patch("/api/projects/digipaddock", headers=headers, json={"council": {}}).status_code == 403
    r = anna.patch(f"/api/cards/{card['id']}", headers=headers,
                   json={"revision": shown["revision"], "changes": {"council": {}}})
    assert r.status_code == 403
