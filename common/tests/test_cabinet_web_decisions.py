"""Decisions over HTTP — the view, the challenge, a passkey or TOTP signature, batches,
reject/ask/reply/revoke and who may do what."""

import hashlib
import json
import os

import pytest
from softauth import SoftAuthenticator, b64url, unb64url
from web_env import WebEnv
from cabinet_env import totp_codes

from kd import boss_board, boss_promote


@pytest.fixture
def w(tmp_path):
    w = WebEnv(tmp_path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", ["web", "admin"])], actor="root")
    return w


def _passkey(w):
    auth = SoftAuthenticator(w.cfg.rp_id, w.cfg.origin)
    opts = w.client.post("/api/me/passkeys/options", headers=w.headers()).json()
    r = w.client.post(
        "/api/me/passkeys",
        headers=w.headers(),
        json={"credential": auth.register(opts["options"]), "name": "iPhone"},
    )
    assert r.status_code == 200, r.text
    return auth


def _totp(w):
    """TOTP confirmed with the previous step's code; the current and the next step's codes come back."""
    b32 = w.client.post("/api/me/totp/setup", headers=w.headers()).json()["secret"]
    codes = totp_codes(b32)
    r = w.client.post(
        "/api/me/totp/confirm",
        headers=w.headers(),
        json={"code": codes[0]},
    )
    assert r.status_code == 200, r.text
    return codes[1:]


def _plan(w, admin_proposal, task_id):
    """The sysadmin's plan for an admin card in Inbox, as a decision."""
    w.env.put(task_id, project="admin", assignee="@sysadmin", status="Inbox")
    payload = admin_proposal(w.env, task_id)
    return w.env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_plan",
            "card": task_id,
            "payload": payload,
        },
    )["decision"]


def _question(w, card):
    return w.env.call(
        "digi",
        {
            "op": "decision.open",
            "type": "question",
            "agent": "dev",
            "card": card.id,
            "payload": {"text": "Платить?"},
        },
    )["decision"]


def _challenge(w, ids, client=None, headers=None):
    client = client or w.client
    return client.post(
        "/api/decisions/challenge", headers=headers or w.headers(), json={"ids": ids}
    )


def _approve(w, body, client=None, headers=None):
    client = client or w.client
    return client.post(
        "/api/decisions/approve", headers=headers or w.headers(), json=body
    )


def _script_plan(w, task_id, script):
    """the sysadmin's plan whose only step is a script — the exact text the owner reads and signs."""
    import json as _json

    from admin_samples import CATALOG, ENABLED, INVENTORY, explain, proposal
    from kd import admin_proposal as store
    from kd import admin_operations as ops
    from kd.cabinet import client as cabinet_client

    w.env.put(task_id, project="admin", assignee="@sysadmin", status="Inbox", servers=["node-c"])
    card = cabinet_client.admin_card(w.env.client("digi-cabinet"), task_id)
    data = proposal(task_id=task_id, operations=[
        explain({"server": "node-c", "verb": "script", "script": script}, what="Перечитать конфигурацию nginx")])
    # the sample catalog enables no script verb; this deployment's does (the overlay's operations.json)
    enabled = {**_json.loads(_json.dumps(CATALOG)), "verbs": [*CATALOG["verbs"], "script"]}
    w.env.admin.catalog = lambda: ops.Catalog.from_dict(_json.loads(_json.dumps(enabled)))
    plan = store.submit(w.env.admin.paths.proposals, _json.dumps(data).encode("utf-8"), agent="sysadmin", card=card,
                        catalog=w.env.admin.catalog(), inventory=INVENTORY, enabled=ENABLED, now=w.env.when())
    payload = {"task_id": task_id, "proposal_sha256": plan.sha256, "card_scope_sha256": card.scope_sha256}
    return w.env.call("digi-sysadmin", {"op": "decision.open", "type": "admin_plan", "card": task_id,
                                        "payload": payload})["decision"]


def test_a_script_step_is_shown_as_its_text(w):
    """The view names the step's kind: a script step's command is the script itself, unfolded on the page; a
    typed step stays a catalog command."""
    w.login()
    script = "set -e\nnginx -t\nsystemctl reload nginx.service\n"
    d = _script_plan(w, "KD-194", script)
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    [step] = view["steps"]
    assert step["kind"] == "script" and step["command"] == script
    assert step["what"] == "Перечитать конфигурацию nginx" and step["servers"] == ["node-c"]
    assert not view.get("stale")


def test_a_typed_step_is_typed(w, admin_proposal):
    w.login()
    d = _plan(w, admin_proposal, "KD-193")
    kinds = [s["kind"] for s in w.client.get(f"/api/decisions/{d['id']}").json()["view"]["steps"]]
    assert kinds == ["typed", "typed"]


def test_the_plan_view_shows_an_app_step_as_typed_with_its_command(w):
    """an app step is a typed step — the page folds its command, and the command is the exact words the
    plan carries: the application's account, its directory, the catalog tool and the arguments."""
    import json as _json

    from admin_samples import CATALOG, ENABLED, INVENTORY, explain, proposal
    from kd import admin_operations as ops
    from kd import admin_proposal as store
    from kd.cabinet import client as cabinet_client

    w.login()
    w.env.put("KD-195", project="admin", assignee="@sysadmin", status="Inbox", servers=["node-c"])
    card = cabinet_client.admin_card(w.env.client("digi-cabinet"), "KD-195")
    app = {"server": "node-c", "user": "www-data", "dir": "/var/www/app.example.test", "python": "./venv/bin/python",
           "tools": ["tools/partner_admin.py"], "flags": {"file": "config.ini", "sections": ["partner"]}}
    catalog = {**_json.loads(_json.dumps(CATALOG)), "apps": {"app-staging": app},
               "verbs": [*CATALOG["verbs"], "service.reload", "app.tool", "app.flag"]}
    w.env.admin.catalog = lambda: ops.Catalog.from_dict(_json.loads(_json.dumps(catalog)))
    data = proposal(task_id="KD-195", operations=[
        explain({"server": "node-c", "verb": "app.tool", "app": "app-staging", "tool": "tools/partner_admin.py",
                 "args": ["set-type", "--partner-id", "6", "--type", "sales"]}, what="Сменить тип учётки"),
        explain({"server": "node-c", "verb": "app.flag", "app": "app-staging", "section": "partner",
                 "key": "clients_enabled", "value": "1"}, what="Включить методы клиентов"),
        explain({"server": "node-c", "verb": "service.reload", "unit": "nginx.service"}, what="Перечитать nginx")])
    plan = store.submit(w.env.admin.paths.proposals, _json.dumps(data).encode("utf-8"), agent="sysadmin", card=card,
                        catalog=w.env.admin.catalog(), inventory=INVENTORY, enabled=ENABLED, now=w.env.when())
    payload = {"task_id": "KD-195", "proposal_sha256": plan.sha256, "card_scope_sha256": card.scope_sha256}
    d = w.env.call("digi-sysadmin", {"op": "decision.open", "type": "admin_plan", "card": "KD-195",
                                     "payload": payload})["decision"]
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    tool, flag, reload = view["steps"]
    assert [s["kind"] for s in view["steps"]] == ["typed", "typed", "typed"] and not view.get("stale")
    prefix = "runuser -u www-data -- env -C /var/www/app.example.test "
    assert tool["command"] == prefix + "./venv/bin/python tools/partner_admin.py set-type --partner-id 6 --type sales"
    assert tool["mechanics"] == "node-c: app-staging — tools/partner_admin.py set-type --partner-id 6 --type sales"
    assert flag["command"] == prefix + "python3 -I /usr/local/libexec/agent-app-flag config.ini partner clients_enabled 1"
    assert flag["mechanics"] == "node-c: app-staging — выключатель [partner] clients_enabled = 1"
    assert reload["command"] == "systemctl reload nginx.service"
    assert reload["mechanics"] == "node-c: перечитать конфигурацию nginx.service"


def test_owner_signs_a_plan_with_a_passkey_and_revokes_it(w, admin_proposal):
    w.login()
    auth = _passkey(w)
    d = _plan(w, admin_proposal, "KD-193")
    got = w.client.get(f"/api/decisions/{d['id']}")
    assert got.status_code == 200, got.text
    view = got.json()["view"]
    assert view["intent"] == "Обновить индексы пакетов и перезапустить nginx"
    assert view["card"]["id"] == "KD-193" and view["ttl_options"] == [24, 72, 168]
    first, second = view["steps"]
    assert first["n"] == 1 and first["what"] == "Обновить индексы пакетов"
    assert first["command"] == "apt-get -q update" and first["servers"] == ["node-c"]
    assert first["production"] is False  # the page highlights a production step
    assert second["risk"] == {"level": "low", "why": "Конфигурация не меняется"}
    assert second["command"] == "systemctl restart nginx.service"
    assert not view.get("stale")
    ch = _challenge(w, [d["id"]])
    assert ch.status_code == 200, ch.text
    ch = ch.json()
    assert ch["step_up"] is True and ch["options"]["challenge"]
    r = _approve(
        w,
        {
            "challenge_id": ch["challenge_id"],
            "credential": auth.assert_(ch["options"]),
            "ttl_hours": 24,
        },
    )
    assert r.status_code == 200, r.text
    out = r.json()["decisions"][0]
    assert out["state"] == "approved" and out["decided_by"] == "user:owner"
    grant = w.env.admin.store.load("KD-193")
    assert grant.state == "active" and grant.signer == b64url(auth.credential_id)
    # an auditor recomputes the signed value from the decision, the term and the nonce
    signed = json.loads(grant.signature)
    binding = signed["binding"]
    bound = w.svc.decisions.challenge(
        [d["id"]], user="owner", expires=binding["expires"], revisions=ch["revisions"]
    )
    client_data = json.loads(unb64url(signed["response"]["clientDataJSON"]))
    expected = hashlib.sha256(bound + bytes.fromhex(binding["nonce"])).digest()
    assert unb64url(client_data["challenge"]) == expected
    assert w.env.store.card("KD-193").status == "To Do"
    said = [m for m in w.env.store.messages("KD-193") if m["kind"] == "decision"][-1]
    assert said["author"] == "user:owner" and said["payload"]["state"] == "approved"
    used = w.users.credentials("owner")[0]
    assert used["last_used"] == w.env.now
    # the same challenge twice: gone
    again = _approve(
        w,
        {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])},
    )
    assert again.status_code == 410 and again.json()["error"] == "expired"
    r = w.client.post(f"/api/decisions/{d['id']}/revoke", headers=w.headers())
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "revoked"
    assert w.env.admin.store.load("KD-193").state == "revoked"


def test_an_old_challenge_expires(w, admin_proposal):
    w.login()
    auth = _passkey(w)
    d = _plan(w, admin_proposal, "KD-193")
    ch = _challenge(w, [d["id"]]).json()
    w.env.now = "2026-10-14T10:06:00Z"
    r = _approve(
        w,
        {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])},
    )
    assert r.status_code == 410
    assert w.env.admin.store.load("KD-193") is None


def test_an_assertion_over_another_challenge_is_refused(w, admin_proposal):
    w.login()
    auth = _passkey(w)
    d = _plan(w, admin_proposal, "KD-193")
    one = _challenge(w, [d["id"]]).json()
    other = _challenge(w, [d["id"]]).json()
    r = _approve(
        w,
        {
            "challenge_id": one["challenge_id"],
            "credential": auth.assert_(other["options"]),
        },
    )
    assert r.status_code == 403
    assert w.env.admin.store.load("KD-193") is None
    assert w.svc.decisions.get(d["id"])["state"] == "open"


def test_a_card_changed_after_the_challenge_is_a_conflict(w, admin_proposal):
    w.login()
    auth = _passkey(w)
    d = _plan(w, admin_proposal, "KD-193")
    ch = _challenge(w, [d["id"]]).json()
    w.env.put("KD-193", description="the agent changed the card")
    r = _approve(
        w,
        {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])},
    )
    assert r.status_code == 409 and r.json()["error"] == "conflict"
    assert w.env.admin.store.load("KD-193") is None


def test_a_batch_is_one_challenge_and_one_signature(w, admin_proposal):
    w.login()
    auth = _passkey(w)
    one = _plan(w, admin_proposal, "KD-193")
    two = _plan(w, admin_proposal, "KD-194")
    ch = _challenge(w, [one["id"], two["id"]]).json()
    r = _approve(
        w,
        {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])},
    )
    assert r.status_code == 200, r.text
    out = r.json()["decisions"]
    assert [x["state"] for x in out] == ["approved", "approved"]
    assert out[0]["batch_id"] and out[0]["batch_id"] == out[1]["batch_id"]
    assert w.env.admin.store.load("KD-193") and w.env.admin.store.load("KD-194")


def test_a_batch_holds_one_type(w, admin_proposal):
    w.login()
    _passkey(w)
    plan = _plan(w, admin_proposal, "KD-193")
    question = _question(w, w.env.card(project="web"))
    r = _challenge(w, [plan["id"], question["id"]])
    assert r.status_code == 400


def _factory_plan(w, task_id):
    """The developer's factory plan as a decision: it runs on the factory host, so a TOTP code still
    signs it (only the targets' plans are bound to a passkey)."""
    w.env.put(task_id, project="digipaddock", assignee="@dev", status="In Progress")
    step = {"verb": "agent.reinstall", "target": "factory", "agents": ["dev"], "what": "Переустановить dev",
            "effect": "Новые инструкции dev", "risk": {"level": "low", "why": "Очередь сохраняется"},
            "rollback": "Предыдущая выкатка и переустановка"}
    proposal = {"schema_version": 2, "task_id": task_id, "intent": "Обновить роли", "basis": "PR #1 меняет инструкции",
                "operations": [step]}
    return w.env.call("digi", {"op": "decision.open", "agent": "dev", "type": "admin_plan", "card": task_id,
                               "payload": {"policy": "factory", "proposal": proposal}})["decision"]


def _factory_grant(w, task_id):
    from kd import factory_ops

    return w.env.admin.store.load(task_id, policies=("factory",), factory=factory_ops.load_catalog().scope)


def _binding(w, task_id):
    return json.loads(w.env.admin.store.load(task_id).signature)["binding"]


def test_approval_binding_carries_revisions_and_digests(w, admin_proposal):
    """two server plans signed at once — the stored binding holds what a target recomputes: the cards'
    revisions the challenge saw and the hex digest of each decision."""
    w.login()
    auth = _passkey(w)
    one = _plan(w, admin_proposal, "KD-193")
    two = _plan(w, admin_proposal, "KD-194")
    before = {t: w.env.store.card(t).revision for t in ("KD-193", "KD-194")}
    ch = _challenge(w, [one["id"], two["id"]]).json()
    r = _approve(w, {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])})
    assert r.status_code == 200, r.text
    binding = _binding(w, "KD-193")
    assert set(binding) == {"expires", "nonce", "revisions", "digests"} and len(binding["digests"]) == 2
    assert binding["revisions"] == ch["revisions"] == before
    assert binding == _binding(w, "KD-194")  # one signature, one binding on both grants
    # each grant's signature names its own decision — a target digests that id
    named = {t: json.loads(w.env.admin.store.load(t).signature)["decision"] for t in ("KD-193", "KD-194")}
    assert named == {"KD-193": one["id"], "KD-194": two["id"]}
    assert binding["digests"] == w.svc.decisions.digests(
        [one["id"], two["id"]], user="owner", expires=binding["expires"], revisions=binding["revisions"]
    )
    # a target recomputes the signed value from the binding alone: sorted digests, then the nonce
    bound = hashlib.sha256("".join(sorted(binding["digests"])).encode("ascii")).digest()
    signed = json.loads(w.env.admin.store.load("KD-193").signature)
    client_data = json.loads(unb64url(signed["response"]["clientDataJSON"]))
    assert unb64url(client_data["challenge"]) == hashlib.sha256(bound + bytes.fromhex(binding["nonce"])).digest()


def test_single_approval_binding_has_no_digests(w, admin_proposal):
    w.login()
    auth = _passkey(w)
    d = _plan(w, admin_proposal, "KD-193")
    revision = w.env.store.card("KD-193").revision
    ch = _challenge(w, [d["id"]]).json()
    r = _approve(w, {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])})
    assert r.status_code == 200, r.text
    binding = _binding(w, "KD-193")
    assert set(binding) == {"expires", "nonce", "revisions"} and binding["revisions"] == {"KD-193": revision}


def test_server_plan_refuses_totp(w, admin_proposal):
    """a server plan is signed for the targets' gate, which checks a passkey assertion — a TOTP code is
    no signature: the challenge offers none, a code sent anyway is refused before it is checked, the passkey
    still signs."""
    w.login()
    auth = _passkey(w)
    d = _plan(w, admin_proposal, "KD-193")
    ch = _challenge(w, [d["id"]]).json()
    assert ch["step_up"] is True and ch["totp"] is False and ch["options"]["challenge"]
    r = _approve(w, {"challenge_id": ch["challenge_id"], "totp": "123456"})
    assert r.status_code == 403 and r.json()["error"] == "stepup_required", r.text
    assert r.json()["message"] == "a server plan or a critical command is signed with a passkey"
    assert w.env.admin.store.load("KD-193") is None
    ch = _challenge(w, [d["id"]]).json()
    r = _approve(w, {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])})
    assert r.status_code == 200, r.text


def test_a_server_plan_with_totp_alone_has_no_challenge(w, admin_proposal):
    w.login()
    _totp(w)
    d = _plan(w, admin_proposal, "KD-193")
    r = _challenge(w, [d["id"]])
    assert r.status_code == 403 and r.json()["error"] == "setup_required", r.text
    assert "passkey" in r.json()["message"] and "TOTP" not in r.json()["message"]


def test_a_critical_command_refuses_totp(w, admin_proposal):
    from admin_samples import ENABLED, INVENTORY, action as action_data
    from kd import admin_proposal as proposals
    from kd.cabinet import client as cabinet_client

    w.login()
    _passkey(w)
    w.env.put("KD-193", project="admin", assignee="@sysadmin", status="To Do")
    admin_proposal(w.env, "KD-193")  # the card's server label
    card = cabinet_client.admin_card(w.env.client("digi-cabinet"), "KD-193")
    action = proposals.submit_action(
        w.env.admin.paths.proposals, json.dumps(action_data(task_id="KD-193")).encode("utf-8"), agent="sysadmin",
        card=card, inventory=INVENTORY, enabled=ENABLED, now=w.env.when(), deny=lambda command, alias: None,
    )
    payload = {"task_id": "KD-193", "proposal_id": action.proposal_id, "proposal_sha256": action.sha256,
               "card_scope_sha256": card.scope_sha256}
    d = w.env.call("digi-sysadmin", {"op": "decision.open", "type": "admin_critical", "card": "KD-193",
                                     "payload": payload})["decision"]
    ch = _challenge(w, [d["id"]]).json()
    assert ch["totp"] is False
    r = _approve(w, {"challenge_id": ch["challenge_id"], "totp": "123456"})
    assert r.status_code == 403 and r.json()["error"] == "stepup_required", r.text


def test_factory_plan_still_takes_totp(w):
    """A factory plan runs on the factory host, not on a target: a TOTP code signs it as before, and the code that
    signed once signs nothing else (global review 15.10.2026); five misses lock the decision."""
    from kd.cabinet import commands

    commands.install(w.env.store, lambda agent: agent in ("dev", "analyst", "boss"))
    w.login()
    current, following = _totp(w)
    one = _factory_plan(w, "KD-50")
    ch = _challenge(w, [one["id"]]).json()
    assert ch["step_up"] is True and ch["options"] is None and ch["totp"] is True
    r = _approve(w, {"challenge_id": ch["challenge_id"], "totp": current})
    assert r.status_code == 200, r.text
    assert r.json()["decisions"][0]["state"] == "approved"
    grant = _factory_grant(w, "KD-50")
    assert grant.signer == "totp" and set(json.loads(grant.signature)["binding"]) == {"expires", "nonce", "revisions"}
    two = _factory_plan(w, "KD-51")
    ch = _challenge(w, [two["id"]]).json()
    r = _approve(w, {"challenge_id": ch["challenge_id"], "totp": current})
    assert r.status_code == 403, r.text
    for _ in range(4):
        ch = _challenge(w, [two["id"]]).json()
        r = _approve(w, {"challenge_id": ch["challenge_id"], "totp": "000000"})
        assert r.status_code == 403, r.text
    ch = _challenge(w, [two["id"]]).json()
    r = _approve(w, {"challenge_id": ch["challenge_id"], "totp": following})
    assert r.status_code == 423 and r.json()["error"] == "locked"
    assert _factory_grant(w, "KD-51") is None


def test_a_step_up_decision_needs_a_signing_factor_first(w, admin_proposal):
    w.login()
    d = _plan(w, admin_proposal, "KD-193")
    r = _challenge(w, [d["id"]])
    assert r.status_code == 403 and r.json()["error"] == "setup_required"


def test_reject_needs_text_and_ask_keeps_the_decision_open(w, admin_proposal):
    w.login()
    d = _plan(w, admin_proposal, "KD-193")
    r = w.client.post(f"/api/decisions/{d['id']}/ask", headers=w.headers(), json={})
    assert r.status_code == 400
    r = w.client.post(
        f"/api/decisions/{d['id']}/ask",
        headers=w.headers(),
        json={"text": "Зачем обновлять индексы?"},
    )
    assert r.status_code == 200 and r.json()["state"] == "open"
    assert r.json()["question"] == "Зачем обновлять индексы?"
    shown = w.client.get(f"/api/decisions/{d['id']}", headers=w.headers()).json()
    assert shown["view"]["asked_open"] is True  # the page says the agent owes the answer
    r = w.client.post(
        f"/api/decisions/{d['id']}/reject", headers=w.headers(), json={"text": " "}
    )
    assert r.status_code == 400
    r = w.client.post(
        f"/api/decisions/{d['id']}/reject",
        headers=w.headers(),
        json={"text": "Не сейчас"},
    )
    assert r.status_code == 200 and r.json()["state"] == "rejected"
    said = [m for m in w.env.store.messages("KD-193") if m["kind"] == "decision"][-1]
    assert said["author"] == "user:owner" and said["body"] == "Не сейчас"
    assert w.env.admin.store.load("KD-193") is None


def test_a_question_is_answered_never_approved(w):
    """Global review 15.10.2026: "approve" over a question would close it with an empty answer."""
    w.login()
    q = _question(w, w.env.card(project="web"))
    ch = _challenge(w, [q["id"]]).json()
    r = _approve(w, {"challenge_id": ch["challenge_id"]})
    assert r.status_code == 403, r.text
    assert w.svc.decisions.get(q["id"])["state"] == "open"


def test_a_partner_answers_questions_but_does_not_approve_plans(w, admin_proposal):
    d = _plan(w, admin_proposal, "KD-193")
    q = _question(w, w.env.card(project="web"))
    anna = w.other_client()
    headers = w.headers(w.login("anna", client=anna)["csrf"])
    assert anna.get(f"/api/decisions/{d['id']}").status_code == 200
    assert _challenge(w, [d["id"]], client=anna, headers=headers).status_code == 403
    r = anna.post(
        f"/api/decisions/{d['id']}/reject", headers=headers, json={"text": "нет"}
    )
    assert r.status_code == 403
    assert anna.get(f"/api/decisions/{q['id']}").json()["view"] == {
        "text": "Платить?", "expect": "text", "form": None, "origin": "", "form_sha256": ""
    }
    r = anna.post(
        f"/api/decisions/{q['id']}/reply", headers=headers, json={"text": "Да"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "approved" and r.json()["reply"] == "Да"
    r = anna.post(
        f"/api/decisions/{d['id']}/reply", headers=headers, json={"text": "Да"}
    )
    assert r.status_code == 403


def test_a_promote_of_a_card_whose_brief_changed_shows_it_stale(w):
    """A BOSS proposal made after a person edited the ready brief: the page says so before the click (R17)."""
    card = w.env.briefed()
    record = w.env.store.edit_card(card.id, None, {"description": "Цель: отчёт и график."}, actor="user:owner")
    proposal = boss_promote.propose(
        boss_board.card_from(record.to_dict()),
        w.env.ctx.routing(),
        record.revision,
        "готово к работе",
        now=w.env.epoch(),
        root=w.env.promote.proposals,
        decisions=w.env.promote.decisions,
    )
    d = w.env.call(
        "digi",
        {
            "op": "decision.open",
            "type": "promote",
            "agent": "boss",
            "card": card.id,
            "payload": {"proposal_id": proposal.id, "proposal_sha256": proposal.sha256},
        },
    )["decision"]
    w.login()
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    assert view["revision"] == record.revision
    assert (view["stale"], view["brief_stale"]) == (True, True)


def test_a_promote_is_approved_without_a_signature(w):
    card = w.env.card(project="web", status="Inbox", assignee="@owner", title="Счёт")
    record = w.env.store.card(card.id)
    proposal = boss_promote.propose(
        boss_board.card_from(record.to_dict()),
        w.env.ctx.routing(),
        record.revision,
        "готово к работе",
        now=w.env.epoch(),
        root=w.env.promote.proposals,
        decisions=w.env.promote.decisions,
    )
    d = w.env.call(
        "digi",
        {
            "op": "decision.open",
            "type": "promote",
            "agent": "boss",
            "card": card.id,
            "payload": {"proposal_id": proposal.id, "proposal_sha256": proposal.sha256},
        },
    )["decision"]
    w.login()
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    assert view["title"] == "Счёт" and view["reason"] == "готово к работе"
    assert view["revision"] == record.revision and view["acceptance"] == []
    assert not view["stale"]
    ch = _challenge(w, [d["id"]]).json()
    assert ch["step_up"] is False and ch["options"] is None
    r = _approve(w, {"challenge_id": ch["challenge_id"]})
    assert r.status_code == 200, r.text
    assert r.json()["decisions"][0]["state"] == "approved"
    assert w.env.store.card(card.id).status == "To Do"
    assert (
        boss_promote.decision(proposal.id, w.env.promote.decisions)["caller"]
        == "user:owner"
    )
    # the old page's link (/boss/promotions/<proposal>) finds the decision by its proposal (the nginx redirect)
    found = w.client.get(
        "/api/decisions", params={"state": "all", "proposal": proposal.id}
    ).json()
    assert [x["decision"]["id"] for x in found] == [d["id"]]
    assert (
        w.client.get(
            "/api/decisions", params={"state": "all", "proposal": "f" * 32}
        ).json()
        == []
    )
    assert (
        w.client.get("/api/decisions", params={"proposal": "../x"}).status_code == 400
    )


def test_the_list_shows_open_decisions_with_their_views(w, admin_proposal):
    d = _plan(w, admin_proposal, "KD-193")
    ops_card = w.env.card(project="web")
    q = _question(w, ops_card)
    w.login()
    listed = w.client.get("/api/decisions", params={"state": "open"}).json()
    assert {x["decision"]["id"] for x in listed} == {d["id"], q["id"]}
    assert all("view" in x for x in listed)
    only = w.client.get("/api/decisions", params={"card": ops_card.id}).json()
    assert [x["decision"]["id"] for x in only] == [q["id"]]
    assert w.client.get("/api/decisions", params={"state": "bogus"}).status_code == 400
    assert w.client.get("/api/decisions/" + "0" * 32).status_code == 404


def test_a_critical_action_shows_its_command_and_whether_it_touches_production(
    w, admin_proposal
):
    from admin_samples import ENABLED, INVENTORY
    from admin_samples import action as action_data
    from kd import admin_proposal as admin_proposal_store
    from kd.cabinet import client as cabinet_client

    w.env.put("KD-193", project="admin", assignee="@sysadmin", status="To Do")
    admin_proposal(w.env, "KD-193")
    card = cabinet_client.admin_card(w.env.client("digi-cabinet"), "KD-193")
    action = admin_proposal_store.submit_action(
        w.env.admin.paths.proposals,
        json.dumps(action_data(task_id="KD-193")).encode("utf-8"),
        agent="sysadmin",
        card=card,
        inventory=INVENTORY,
        enabled=ENABLED,
        now=w.env.when(),
        deny=lambda command, alias: None,
    )
    d = w.env.call(
        "digi-sysadmin",
        {
            "op": "decision.open",
            "type": "admin_critical",
            "card": "KD-193",
            "payload": {
                "task_id": "KD-193",
                "proposal_id": action.proposal_id,
                "proposal_sha256": action.sha256,
                "card_scope_sha256": card.scope_sha256,
            },
        },
    )["decision"]
    w.login()
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    assert view["command"] == "wg set wg0 peer 'abc=' remove" and view["servers"] == [
        "node-c"
    ]
    assert view["risk"] == {"level": "high", "why": "Связь с пиром пропадёт"}
    assert view["production"] is False and view["rollback"].startswith("Вернуть")


def test_decision_writes_need_csrf(w, admin_proposal):
    d = _plan(w, admin_proposal, "KD-193")
    w.login()
    r = w.client.post(f"/api/decisions/{d['id']}/reject", json={"text": "нет"})
    assert r.status_code == 403
    assert (
        w.client.post("/api/decisions/challenge", json={"ids": [d["id"]]}).status_code
        == 403
    )


# --- a question form over HTTP -------------------------------------------------------
def _ask(w, card, payload):
    return w.env.call(
        "digi",
        {"op": "decision.open", "type": "question", "agent": "dev", "card": card.id, "payload": payload},
    )["decision"]


def test_reply_passes_the_form_fields(w):
    from kd.cabinet import questions
    from question_samples import form

    w.login()
    q = _ask(w, w.env.card(project="web"), form())
    r = w.client.post(
        f"/api/decisions/{q['id']}/reply",
        headers=w.headers(),
        json={
            "answers": {"method": {"selected_option_ids": ["ssh"], "custom_text": "по ключу"}},
            "comment": "Срок — неделя",
            "form_sha256": questions.form_sha(q["payload"]),
        },
    )
    assert r.status_code == 200, r.text
    answer = r.json()["answer"]
    assert answer["answers"]["method"] == {"selected_option_ids": ["ssh"], "custom_text": "по ключу"}
    assert answer["comment"] == "Срок — неделя" and answer["origin"] == "agent"
    assert r.json()["reply"].startswith("1. Как проверять?: Через SSH — по ключу\n")
    r = w.client.post(f"/api/decisions/{q['id']}/reply", headers=w.headers(), json={"text": "да"})
    assert r.status_code == 400, r.text  # the form is answered with the form


def test_question_view_has_form_origin_and_sha(w):
    from kd.cabinet import questions
    from kd.cabinet.questions_legacy import recognize
    from question_samples import LEGACY, form

    w.login()
    agent = _ask(w, w.env.card(project="web"), form())
    view = w.client.get(f"/api/decisions/{agent['id']}").json()["view"]
    assert view == {
        "text": "Уточнения по проверке", "expect": "text", "form": agent["payload"], "origin": "agent",
        "form_sha256": questions.form_sha(agent["payload"]),
    }
    old = _ask(w, w.env.card(project="web"), {"text": LEGACY})
    view = w.client.get(f"/api/decisions/{old['id']}").json()["view"]
    assert view == {
        "text": LEGACY, "expect": "text", "form": recognize(LEGACY), "origin": "parsed",
        "form_sha256": questions.form_sha(recognize(LEGACY)),
    }


def test_a_component_plan_binds_the_tree_shown_to_the_signature(w):
    """the page shows the deployed tree, the challenge binds it; a release before the click is a conflict,
    a fresh challenge signs the new tree."""
    from kd import factory_ops
    from kd.cabinet import commands

    trees = ["a" * 40]
    w.env.ctx.deployed_revision = lambda: trees[0]
    commands.install(w.env.store, lambda agent: agent in ("dev", "analyst", "boss"))
    w.env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    step = {"verb": "component.install", "target": "factory", "component": "repo-attach", "mode": "check",
            "what": "Проверить kd-repo-attach", "effect": "Только чтение", "risk": {"level": "low", "why": "check"},
            "rollback": "Не нужен"}
    proposal = {"schema_version": 2, "task_id": "KD-50", "intent": "Проверка", "basis": "KD-9108",
                "operations": [step]}
    d = w.env.call("digi", {"op": "decision.open", "agent": "dev", "type": "admin_plan", "card": "KD-50",
                           "payload": {"policy": "factory", "proposal": proposal}})["decision"]
    w.login()
    auth = _passkey(w)
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    assert view["deployed"] == "a" * 40
    ch = _challenge(w, [d["id"]]).json()
    assert ch["revisions"]["deployed:KD-50"] == "a" * 40
    trees[0] = "b" * 40  # another card is released before the click
    r = _approve(w, {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])})
    assert r.status_code == 409, r.text
    ch = _challenge(w, [d["id"]]).json()
    r = _approve(w, {"challenge_id": ch["challenge_id"], "credential": auth.assert_(ch["options"])})
    assert r.status_code == 200, r.text
    grant = w.env.admin.store.load("KD-50", policies=("factory",), factory=factory_ops.load_catalog().scope)
    assert grant.operations[0]["revision"] == "b" * 40
    # the tree is in the signed value itself: the same challenge over another tree is another value
    expires = json.loads(grant.signature)["binding"]["expires"]
    other = {**ch["revisions"], "deployed:KD-50": "a" * 40}
    assert (w.svc.decisions.challenge([d["id"]], user="owner", expires=expires, revisions=ch["revisions"])
            != w.svc.decisions.challenge([d["id"]], user="owner", expires=expires, revisions=other))
    [event] = [e for e in w.env.store.events_since(0) if e["kind"] == "decision.approved"
               and e["payload"]["decision"] == d["id"]]
    assert event["payload"]["revision"] == "b" * 40  # the history names the tree the grant covers


def _promote(w, **card):
    card = w.env.card(**{"project": "web", "status": "Inbox", "assignee": "@owner", "title": "Счёт", **card})
    record = w.env.store.card(card.id)
    proposal = boss_promote.propose(
        boss_board.card_from(record.to_dict()), w.env.ctx.routing(), record.revision, "готово к работе",
        now=w.env.epoch(), root=w.env.promote.proposals, decisions=w.env.promote.decisions,
    )
    payload = {"proposal_id": proposal.id, "proposal_sha256": proposal.sha256}
    return card, w.env.call(
        "digi", {"op": "decision.open", "type": "promote", "agent": "boss", "card": card.id, "payload": payload}
    )["decision"]


def test_a_promote_while_boss_is_off_is_blocked_then_approved_once_it_is_back(w):
    """A release reinstalls BOSS, its switch is off for minutes. The page offers no approval then
    (``blocked``), and an approval sent anyway is a retryable 503 with its reason, never «the card changed»."""
    card, d = _promote(w)
    switch = w.env.promote.channel_switch
    with open(switch, encoding="utf-8") as f:
        saved = f.read()
    os.unlink(switch)
    w.login()
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    assert view["blocked"] == boss_promote.BOSS_OFF and not view["stale"]
    ch = _challenge(w, [d["id"]]).json()
    r = _approve(w, {"challenge_id": ch["challenge_id"]})
    assert r.status_code == 503 and r.json()["reason"] == boss_promote.BOSS_OFF, r.text
    assert w.env.store.card(card.id).status == "Inbox"
    with open(switch, "w", encoding="utf-8") as f:
        f.write(saved)
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    assert "blocked" not in view
    ch = _challenge(w, [d["id"]]).json()
    r = _approve(w, {"challenge_id": ch["challenge_id"]})
    assert r.status_code == 200 and r.json()["decisions"][0]["state"] == "approved", r.text
    assert w.env.store.card(card.id).status == "To Do"


def test_a_promote_whose_card_moved_off_the_proposal_is_stale_before_the_click(w):
    """The view asks what the approval asks (``boss_promote.problem``): the page says why, not only that."""
    card, d = _promote(w)
    w.env.store.edit_card(card.id, None, {"title": "Счёт и акт"}, actor="user:owner")
    w.login()
    view = w.client.get(f"/api/decisions/{d['id']}").json()["view"]
    assert view["stale"] is True and view["problem"] == "карточка изменилась после предложения"


def test_a_promote_shows_the_operations_its_approval_grants(w):
    """Approving BOSS's proposal approves the brief — the page lists its "Operations" first."""
    step = {"verb": "agent.reinstall", "target": "factory", "agents": ["dev"], "what": "Переустановить dev",
            "effect": "Новые инструкции dev", "risk": {"level": "low", "why": "Очередь сохраняется"},
            "rollback": "Предыдущая выкатка"}
    card = w.env.briefed()
    record = w.env.store.edit_card(card.id, None, {"operations": [step], "labels_add": ["brief-ready"]},
                                   actor="agent:analyst")
    proposal = boss_promote.propose(boss_board.card_from(record.to_dict()), w.env.ctx.routing(), record.revision,
                                    "готово к работе", now=w.env.epoch(), root=w.env.promote.proposals,
                                    decisions=w.env.promote.decisions)
    d = w.env.call("digi", {"op": "decision.open", "type": "promote", "agent": "boss", "card": card.id,
                           "payload": {"proposal_id": proposal.id, "proposal_sha256": proposal.sha256}})["decision"]
    w.login()
    assert w.client.get(f"/api/decisions/{d['id']}").json()["view"]["operations"] == [step]
