"""The developer's factory plan is the sysadmin's schema-2 plan decision, the grant is
admin_grant's under the factory policy; an approved brief with "Operations" writes the same grant without a second
signature; a note grants nothing."""

import sys
from pathlib import Path

import pytest

from kd import admin_grant, board, factory_ops
from kd.cabinet import api, commands

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cabinet_env import CabinetEnv  # noqa: E402

STEP = {"verb": "agent.reinstall", "target": "factory", "agents": ["dev"], "what": "Переустановить dev",
        "effect": "Новые инструкции dev", "risk": {"level": "low", "why": "Очередь сохраняется"},
        "rollback": "Предыдущая выкатка и переустановка"}


def proposal(card="KD-50", steps=(STEP,)):
    return {"schema_version": 2, "task_id": card, "intent": "Обновить роли", "basis": "PR #1 меняет инструкции",
            "operations": list(steps)}


@pytest.fixture
def env(tmp_path):
    e = CabinetEnv(tmp_path)
    e.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    commands.install(e.store, lambda agent: agent in ("dev", "analyst", "boss"))
    from test_cabinet_flow import HOSTED, SPECS

    api.install_host(e.ctx, lambda: HOSTED)
    e.ctx._specs = SPECS
    return e


def scope():
    return factory_ops.load_catalog().scope


def grant_of(env, card):
    return env.admin.store.load(card, policies=(admin_grant.FACTORY,), factory=scope())


def open_plan(env, card="KD-50", agent="dev", steps=(STEP,)):
    return env.call("digi", {"op": "decision.open", "agent": agent, "type": "admin_plan", "card": card,
                            "payload": {"policy": "factory", "proposal": proposal(card, steps)}})["decision"]


def approve(env, d, deployed=None):
    """As the web layer's challenge binds it: the card's revision and, for a component step, the tree
    deployed when the owner clicks (``deployed`` — the one the challenge saw, by default the current)."""
    revisions = {d["card"]: env.store.card(d["card"]).revision}
    tree = api.deployed_basis(env.ctx, d)
    if tree is not None:
        revisions[api.deployed_key(d)] = tree if deployed is None else deployed
    return env.call("digi-cabinet", {"op": "decision.approve", "ids": [d["id"]], "user": "owner",
                                    "signature": {"id": "cred"}, "credential": 1, "revisions": revisions})


def test_dev_opens_factory_plan(env):
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    d = open_plan(env)
    p = d["payload"]
    assert d["type"] == "admin_plan" and d["state"] == "open" and d["step_up"] is True
    assert p["policy"] == "factory" and p["task_id"] == "KD-50"
    assert p["proposal"]["operations"][0]["agents"] == ["dev"]
    assert len(p["proposal_sha256"]) == 64 and len(p["card_scope_sha256"]) == 64
    assert open_plan(env)["id"] == d["id"]  # asked again: the same open decision


@pytest.mark.parametrize("card,assignee,agent", [
    ("KD-51", "@analyst", "analyst"),  # not the catalog's requester
    ("KD-52", "@dev", "analyst"),      # not the caller's card
])
def test_factory_plan_from_other_agent_refused(env, card, assignee, agent):
    env.put(card, project="digipaddock", assignee=assignee, status="In Progress")
    with pytest.raises(api.ApiError) as exc:
        open_plan(env, card=card, agent=agent)
    assert exc.value.kind == "refused"


def test_factory_plan_outside_catalog_refused(env):
    env.put("KD-53", project="admin", assignee="@dev", status="In Progress")
    with pytest.raises(api.ApiError):
        open_plan(env, card="KD-53")  # the factory's projects only
    env.put("KD-54", project="digipaddock", assignee="@dev", status="In Progress")
    with pytest.raises(api.ApiError) as exc:
        open_plan(env, card="KD-54", steps=({**STEP, "command": "systemctl restart nginx"},))
    assert exc.value.kind == "invalid"


def test_factory_plan_approve_writes_grant(env):
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    d = open_plan(env)
    out = approve(env, d)["decisions"][0]
    assert out["state"] == "approved" and out["grant_id"]
    grant = grant_of(env, "KD-50")
    assert grant.state == "active" and grant.assignee == "dev" and grant.project == "digipaddock"
    assert grant.signer == "cred" and grant.approved_by == "user:owner"
    assert [op["agents"] for op in grant.operations] == [["dev"]]
    with pytest.raises(admin_grant.GrantError):
        env.admin.store.load("KD-50")  # the sysadmin's readers never see it as theirs
    woken = [c for c in env.store.commands(card="KD-50") if c["agent"] == "dev"]
    assert woken and woken[-1]["text"] == board.FACTORY_PLAN_WAKE.format(id="KD-50")


def test_factory_plan_revoke(env):
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    d = approve(env, open_plan(env))["decisions"][0]
    env.call("digi-cabinet", {"op": "decision.revoke", "id": d["id"], "user": "owner"})
    assert grant_of(env, "KD-50").state == "revoked"


def _brief_card(env, steps=(STEP,), notes=""):
    card = env.store.create_card({"title": "Роли", "project": "digipaddock", "status": "Inbox", "assignee": "@analyst",
                                  "labels": ["spec:task-brief"], "created_by": "@owner"}, actor="user:owner")
    changes = {"description": "Цель.", "ac_add": ["Готово"]}
    if steps:
        changes["operations"] = list(steps)
    if notes:
        changes["notes_append"] = notes
    card = env.store.edit_card(card.id, card.revision, changes, actor="agent:analyst")
    return env.store.edit_card(card.id, card.revision, {
        "assignee": "@dev", "labels_remove": ["spec:task-brief"], "labels_add": ["spec:implementer", "brief-ready"],
    }, actor="agent:analyst")


def _start(env, card):
    owner = api.Principal("user", -1, "owner")
    from kd.cabinet import flow

    item = next(a for a in flow.actions(env.ctx, owner, env.store.card(card.id)) if a["id"] == "start")
    return api.dispatch(env.ctx, owner, {"op": "user.card.act", "card": card.id, "action": "start",
                                         "revision": env.store.card(card.id).revision,
                                         "brief_version": item["brief_version"]})


def test_brief_start_with_operations_writes_grant_without_signature(env):
    card = _brief_card(env)
    _start(env, card)
    grant = grant_of(env, card.id)
    assert grant is not None and grant.state == "active" and grant.assignee == "dev"
    assert grant.signer == "" and grant.approved_by == "user:owner"
    assert [op["verb"] for op in grant.operations] == ["agent.reinstall"]


def test_brief_grant_needs_a_person_who_may_approve_plans(env):
    """Review I3: a partner's promote (decide.promote) approves no factory steps; they wait for a signed plan."""
    from kd.cabinet import effects

    env.users.create("anna", "Анна", roles=[("partner", "*")], actor="root")
    card = _brief_card(env)
    from kd.cabinet import briefs

    briefs.approve(env.store, env.store.card(card.id), by="user:anna")
    assert effects.brief_grant(env.ctx, env.store.card(card.id), by="user:anna") is None
    assert effects.brief_grant(env.ctx, env.store.card(card.id), by="root") is None
    assert grant_of(env, card.id) is None
    _start(env, card)  # the owner's start grants them
    assert grant_of(env, card.id) is not None


def test_brief_grant_failure_refuses_the_start_with_its_reason(env, monkeypatch):
    """Review minor 1: a grant that cannot be written refuses the start with a reason, not an internal error."""
    from kd.cabinet import effects

    card = _brief_card(env)

    def broken(ctx, record, *, by):
        raise effects.EffectError("unavailable", "the grant store is not served here")

    monkeypatch.setattr(effects, "brief_grant", broken)
    with pytest.raises(api.ApiError) as exc:
        _start(env, card)
    assert exc.value.kind == "unavailable" and "grant store" in str(exc.value)


def test_note_without_plan_gives_no_grant(env):
    card = _brief_card(env, steps=(), notes="agent.reinstall dev, boss")
    _start(env, card)
    assert grant_of(env, card.id) is None


def test_web_view_of_factory_plan(env):
    from kd.cabinet.web import decisions as web_decisions

    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    d = open_plan(env)
    service = type("S", (), {"ctx": env.ctx, "now": lambda self: env.now})()
    seen = web_decisions._admin_plan(service, d)
    assert seen["policy"] == "factory" and seen["stale"] is False
    assert seen["steps"][0]["agents"] == ["dev"] and seen["steps"][0]["risk"]["level"] == "low"
    env.store.edit_card("KD-50", env.store.card("KD-50").revision, {"description": "Другое."}, actor="user:owner")
    assert web_decisions._admin_plan(service, d)["stale"] is True


def test_brief_view_shows_operations(env):
    from kd.cabinet import flow

    card = _brief_card(env)
    seen = flow.brief_view(env.ctx, api.Principal("user", -1, "owner"), {"card": card.id})
    assert seen["versions"][-1]["operations"][0]["verb"] == "agent.reinstall"


def test_root_cli_requests_operation(env):
    import importlib.machinery
    import importlib.util
    import io

    path = Path(__file__).resolve().parents[1] / "bin" / "kd-cabinet"
    loader = importlib.machinery.SourceFileLoader("kd_cabinet_ops", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader("kd_cabinet_ops", loader))
    loader.exec_module(module)
    env.ctx.deployed_revision = lambda: "c" * 40
    client = type("C", (), {"call": lambda self, req: api.dispatch(env.ctx, env.principal("root"), req)})()
    out = io.StringIO()
    assert module.main(["operation", "request", "--agents", "analyst", "--key", "root:t"], client=client, out=out) == 0
    op_id = out.getvalue().split()[1]
    out = io.StringIO()
    assert module.main(["operation", "get", op_id], client=client, out=out) == 0
    assert out.getvalue().startswith(f"operation {op_id} pending - agents analyst")
