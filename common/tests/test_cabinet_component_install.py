"""``component.install`` in the cabinet — a plan step signed for the deployed revision, a request the card's
grant covers (component, mode, that revision; repeatable while the grant lives), nothing outside the catalog, the
host's request naming the component, and its end in the card's thread with the codes and the masked tails."""

import sys
from pathlib import Path

import pytest

from kd.cabinet import api, operations

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cabinet_factory_plan import approve, env, open_plan  # noqa: E402,F401

REV_A, REV_B = "a" * 40, "b" * 40
STEP = {"verb": "component.install", "target": "factory", "component": "repo-attach", "mode": "install",
        "what": "Поставить kd-repo-attach", "effect": "Привязка репозиториев из кабинета",
        "risk": {"level": "low", "why": "Скрипт идемпотентен"}, "rollback": "repo_attach_setup.sh disable"}


@pytest.fixture
def granted(env):
    env.ctx.deployed_revision = lambda: REV_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    approve(env, open_plan(env, steps=(STEP,)))
    return env


def ask(env, key="c:1", component="repo-attach", mode="install", card="KD-50", **extra):
    return env.call("digi", {"op": "operation.request", "agent": "dev", "card": card, "kind": "component.install",
                            "component": component, "mode": mode, "key": key, **extra})["operation"]


def report(env, op, state, **extra):
    from kd.cabinet import sessions_api  # noqa: F401 — host.report

    env.call("root", {"op": "host.report", "items": [
        {"type": "operation", "operation": op["id"], "state": state, **extra}]})


def notices(env, card="KD-50"):
    return [m["payload"] for m in env.store.messages(card) if m["kind"] == "notice"]


def test_plan_step_is_pinned_to_the_deployed_revision(env):
    env.ctx.deployed_revision = lambda: REV_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    d = open_plan(env, steps=(STEP,))
    [step] = d["payload"]["proposal"]["operations"]
    assert step["revision"] == REV_A and "agents" not in step


def test_plan_for_another_tree_refused(env):
    env.ctx.deployed_revision = lambda: REV_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    with pytest.raises(api.ApiError) as exc:
        open_plan(env, steps=({**STEP, "revision": REV_B},))
    assert exc.value.kind == "refused" and "deployed" in str(exc.value)
    assert open_plan(env, steps=({**STEP, "revision": REV_A[:12]},))["state"] == "open"  # short sha: same tree


@pytest.mark.parametrize("change", [{"mode": "disable"}, {"component": "nginx"}, {"run": ["bash", "-c", "id"]}])
def test_plan_outside_the_catalog_refused(env, change):
    env.ctx.deployed_revision = lambda: REV_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    with pytest.raises(api.ApiError):
        open_plan(env, steps=({**STEP, **change},))


def test_request_covered_by_the_signed_step(granted):
    op = ask(granted)
    assert op["state"] == "pending" and op["agents"] == [] and op["revision"] == REV_A
    assert op["params"] == {"component": "repo-attach", "mode": "install", "drain": ""}
    assert op["grant_id"]
    assert ask(granted)["id"] == op["id"]  # the same key: the same operation
    assert ask(granted, key="c:2")["id"] == op["id"]  # the same work under way: joined, not run twice


def test_request_repeats_after_the_end_on_the_same_revision(granted):
    """The owner's choice: one signature covers repeated runs on its revision while the grant lives."""
    first = ask(granted)
    report(granted, first, "succeeded", component={"code": 0, "tail": "ok", "check": 0, "check_tail": "ok"})
    again = ask(granted, key="c:2")
    assert again["id"] != first["id"] and again["state"] == "pending"


def test_the_signature_covers_the_tree_deployed_when_signed(env):
    """The owner's choice (form 5829a559): a release of another card between the plan and the signature does not
    void it — the grant names the tree deployed at the signature."""
    env.ctx.deployed_revision = lambda: REV_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    d = open_plan(env, steps=(STEP,))
    assert d["payload"]["proposal"]["operations"][0]["revision"] == REV_A
    env.ctx.deployed_revision = lambda: REV_B  # another card is released before the owner signs
    approve(env, d)
    from kd import factory_ops

    grant = env.admin.store.load("KD-50", policies=("factory",), factory=factory_ops.load_catalog().scope)
    assert grant.operations[0]["revision"] == REV_B
    assert ask(env)["revision"] == REV_B  # the request on the signed tree is covered


def test_a_release_between_the_challenge_and_the_approval_is_a_conflict(env):
    """Review I1: the page showed A, a release landed before the click — the signature never covers B unseen."""
    env.ctx.deployed_revision = lambda: REV_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    d = open_plan(env, steps=(STEP,))
    env.ctx.deployed_revision = lambda: REV_B
    with pytest.raises(api.ApiError) as exc:
        approve(env, d, deployed=REV_A)
    assert exc.value.kind == "conflict" and "deployed tree" in str(exc.value)
    from kd import factory_ops

    assert env.admin.store.load("KD-50", policies=("factory",), factory=factory_ops.load_catalog().scope) is None


def test_a_reinstall_plan_binds_no_tree(env):
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    from test_cabinet_factory_plan import STEP as REINSTALL

    d = open_plan(env, steps=(REINSTALL,))
    assert api.deployed_basis(env.ctx, d) is None


def test_a_brief_component_step_covers_the_tree_at_the_start(env):
    """The "Operations" of a brief written on A, the card started after a release of B — the grant names B."""
    from test_cabinet_factory_plan import _brief_card, _start, grant_of

    env.ctx.deployed_revision = lambda: REV_A
    card = _brief_card(env, steps=({**STEP, "revision": REV_A},))
    env.ctx.deployed_revision = lambda: REV_B
    _start(env, card)
    grant = grant_of(env, card.id)
    assert grant.operations[0]["revision"] == REV_B
    assert ask(env, card=card.id)["revision"] == REV_B


def test_request_after_a_release_refused(granted):
    granted.ctx.deployed_revision = lambda: REV_B
    with pytest.raises(api.ApiError) as exc:
        ask(granted)
    assert exc.value.kind == "refused" and "new plan" in str(exc.value)


@pytest.mark.parametrize("over,kind", [
    ({"mode": "check"}, "refused"),           # signed: install only
    ({"mode": "disable"}, "refused"),         # not in the catalog
    ({"component": "nginx"}, "refused"),      # not in the catalog
    ({"agents": ["dev"]}, "invalid"),         # a component names no roles
    ({"command": "id"}, "invalid"),           # no free field travels
])
def test_request_outside_grant_or_catalog_refused(granted, over, kind):
    with pytest.raises(api.ApiError) as exc:
        ask(granted, **over)
    assert exc.value.kind == kind


def test_reinstall_takes_no_component(granted):
    with pytest.raises(api.ApiError):
        granted.call("digi", {"op": "operation.request", "agent": "dev", "card": "KD-50", "kind": "agent.reinstall",
                             "agents": ["dev"], "component": "repo-attach", "mode": "install", "key": "r:1"})


def test_release_never_asks_for_a_pinned_component(granted):
    """A step signed for a tree is dev's to run; a release requests only steps on ``revision: "release"``."""
    with pytest.raises(api.ApiError) as exc:
        release_ask(granted, mode="install")
    assert exc.value.kind == "refused" and "the release requests it" not in str(exc.value)


# --- one signature — component steps on the card's own release ---
RELEASE_STEP = {**STEP, "mode": "check", "revision": "release", "what": "Проверить kd-repo-attach после выкатки"}


def release_ask(env, mode="check", card="KD-50", rev=REV_A, **extra):
    return env.call("root", {"op": "operation.request", "requester": "release", "card": card,
                             "kind": "component.install", "component": "repo-attach", "mode": mode,
                             "key": f"release:{rev}:repo-attach:{mode}", "revision": rev, **extra})["operation"]


@pytest.fixture
def released(env):
    env.ctx.deployed_revision = lambda: REV_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    approve(env, open_plan(env, steps=(RELEASE_STEP, STEP)))
    return env


def test_release_step_is_accepted_and_not_pinned_at_signature(env):
    from kd import factory_ops

    env.ctx.deployed_revision = lambda: REV_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    d = open_plan(env, steps=(RELEASE_STEP,))
    assert d["payload"]["proposal"]["operations"][0]["revision"] == "release"
    assert api.deployed_basis(env.ctx, d) is None  # nothing to bind: the signature names no tree
    env.ctx.deployed_revision = lambda: REV_B
    approve(env, d)
    grant = env.admin.store.load("KD-50", policies=("factory",), factory=factory_ops.load_catalog().scope)
    assert grant.operations[0]["revision"] == "release"
    assert env.call("root", {"op": "operation.release_steps", "card": "KD-50"})["steps"] == [
        {"component": "repo-attach", "mode": "check"}]


def test_release_requests_component_on_the_released_revision(released):
    op = release_ask(released)
    assert (op["state"], op["revision"], op["requested_by"]) == ("pending", REV_A, "release")
    assert op["params"]["component"] == "repo-attach" and op["params"]["mode"] == "check"
    again = release_ask(released)  # the same key: the same operation
    assert again["id"] == op["id"]
    assert released.call("root", {"op": "operation.release_steps", "card": "KD-50"})["steps"] == [
        {"component": "repo-attach", "mode": "check"}]


def test_dev_cannot_request_a_release_step_itself(released):
    with pytest.raises(api.ApiError) as exc:
        ask(released, mode="check")
    assert exc.value.kind == "refused" and "the release requests it" in str(exc.value)
    assert ask(released, mode="install")["revision"] == REV_A  # the pinned step stays dev's


def test_release_component_after_a_newer_release_fails_revision_moved_without_supersede(released):
    first = release_ask(released)
    released.ctx.deployed_revision = lambda: REV_B
    with pytest.raises(api.ApiError) as exc:
        release_ask(released, rev=REV_A)
    assert exc.value.kind == "refused" and operations.REVISION_MOVED in str(exc.value)
    assert operations.row(released.store, first["id"])["state"] == "pending"  # nothing superseded, nothing new
    assert released.store._conn.execute("SELECT COUNT(*) FROM operations").fetchone()[0] == 1


def test_release_steps_of_a_card_without_a_grant_are_none(env):
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    assert env.call("root", {"op": "operation.release_steps", "card": "KD-50"})["steps"] == []


def test_host_request_names_the_component_and_drain(granted):
    op = operations.row(granted.store, ask(granted)["id"])
    req = operations.host_request(op, 900)
    assert req["kind"] == "component.install" and req["agents"] == []
    assert (req["component"], req["mode"], req["drain"]) == ("repo-attach", "install", "")


def test_authorize_takes_an_empty_batch(granted):
    op = ask(granted)
    answer = granted.call("root", {"op": "operation.authorize", "operation": op["id"], "agents": []})
    assert answer["allowed"] is True
    with pytest.raises(api.ApiError):
        granted.call("root", {"op": "operation.authorize", "operation": op["id"], "agents": ["dev"]})
    granted.ctx.deployed_revision = lambda: REV_B
    answer = granted.call("root", {"op": "operation.authorize", "operation": op["id"], "agents": []})
    assert answer["allowed"] is False and answer["reason"] == "revision_moved"


# --- a release is the code and the overlay: the signature binds both ---
TREE_A, TREE_A2 = f"{REV_A[:7]}+1111111", f"{REV_A[:7]}+2222222"


def test_deployed_is_the_code_and_the_overlay_revision(tmp_path):
    from types import SimpleNamespace

    (tmp_path / "REVISION").write_text(REV_A[:7] + "\n")
    ctx = SimpleNamespace(repo_root=str(tmp_path))
    assert operations.deployed(ctx) == REV_A[:7]  # a host with no overlay keeps working
    (tmp_path / "DEPLOY_REVISION").write_text("1111111\n")
    assert operations.deployed(ctx) == TREE_A
    (tmp_path / "DEPLOY_REVISION").write_text("main\n")
    with pytest.raises(api.ApiError) as exc:
        operations.deployed(ctx)
    assert exc.value.kind == "unavailable"


def test_an_overlay_only_release_ends_the_signature(env):  # noqa: F811 - the imported fixture
    env.ctx.deployed_revision = lambda: TREE_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    approve(env, open_plan(env, steps=(STEP,)))
    op = ask(env)
    assert op["revision"] == TREE_A
    assert env.call("root", {"op": "operation.authorize", "operation": op["id"], "agents": []})["allowed"] is True
    env.ctx.deployed_revision = lambda: TREE_A2  # the same code, another overlay
    answer = env.call("root", {"op": "operation.authorize", "operation": op["id"], "agents": []})
    assert (answer["allowed"], answer["reason"]) == (False, "revision_moved")
    with pytest.raises(api.ApiError) as exc:
        ask(env, key="c:2")
    assert exc.value.kind == "refused" and "new plan" in str(exc.value)


def test_a_plan_naming_the_code_commit_alone_is_the_deployed_tree(env):  # noqa: F811 - the imported fixture
    env.ctx.deployed_revision = lambda: TREE_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    assert open_plan(env, steps=({**STEP, "revision": REV_A[:12]},))["state"] == "open"
    with pytest.raises(api.ApiError) as exc:
        open_plan(env, steps=({**STEP, "revision": f"{REV_A[:7]}+3333333"},))
    assert exc.value.kind == "refused" and "deployed" in str(exc.value)


def test_the_release_asks_by_its_code_commit_on_a_tree_with_an_overlay(env):  # noqa: F811 - the imported fixture
    """kd-release names the code's REVISION; the operation records the whole tree it runs on."""
    env.ctx.deployed_revision = lambda: TREE_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    approve(env, open_plan(env, steps=(RELEASE_STEP,)))
    assert release_ask(env, rev=REV_A[:7])["revision"] == TREE_A
    with pytest.raises(api.ApiError) as exc:
        release_ask(env, rev=f"{REV_A[:7]}+2222222", key="release:x")
    assert exc.value.kind == "refused" and operations.REVISION_MOVED in str(exc.value)


def test_the_host_gets_the_code_commit_and_the_runner_s_tree_is_checked(env):  # noqa: F811 - the imported fixture
    """The session host (maybe pinned to an older release) takes a commit; the overlay is checked at authorize, with
    the tree the runner itself read."""
    env.ctx.deployed_revision = lambda: TREE_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    approve(env, open_plan(env, steps=(STEP,)))
    op = ask(env)
    assert operations.host_request(operations.row(env.store, op["id"]), 900)["revision"] == REV_A[:7]

    def asks(tree):
        return env.call("root", {"op": "operation.authorize", "operation": op["id"], "agents": [], "tree": tree})

    assert asks(TREE_A)["allowed"] is True
    assert (asks(TREE_A2)["allowed"], asks(TREE_A2)["reason"]) == (False, "revision_moved")
    with pytest.raises(api.ApiError) as exc:
        asks("../x")
    assert exc.value.kind == "invalid"


def test_success_posts_codes_and_tails_in_the_thread(granted):
    op = ask(granted)
    report(granted, op, "succeeded", phase="installing",
           component={"code": 0, "tail": "digipaddock-repo-attach.path: active\n", "check": 0, "check_tail": ""})
    row = operations.row(granted.store, op["id"])
    assert row["state"] == "succeeded" and row["result"]["component"]["code"] == 0
    [notice] = notices(granted)
    assert notice["key"] == "notice.factory.component_done" and notice["level"] == "info"
    params = notice["params"]
    assert (params["component"], params["mode"], params["code"], params["check"]) == ("repo-attach", "install",
                                                                                      "0", "0")
    assert params["tail"] == "digipaddock-repo-attach.path: active" and params["check_tail"] == "—"


def test_failure_is_critical_and_names_the_reason(granted):
    op = ask(granted)
    report(granted, op, "failed", phase="installing", reason="check_failed",
           component={"code": 0, "tail": "", "check": 1, "check_tail": "digipaddock-repo-attach.path не запущен"})
    [notice] = notices(granted)
    assert notice["key"] == "notice.factory.component_failed" and notice["level"] == "critical"
    assert notice["params"]["reason"] == "check_failed" and notice["params"]["check"] == "1"


def test_revision_moved_fails_and_is_never_superseded(granted):
    op = ask(granted)
    report(granted, op, "failed", phase="installing", reason="revision_moved")
    assert operations.row(granted.store, op["id"])["state"] == "failed"


def test_lost_component_says_unknown(granted):
    op = ask(granted)
    report(granted, op, "running", phase="installing")
    operations.answered(granted.ctx, op["id"], {"state": "unknown"})
    [notice] = notices(granted)
    assert notice["key"] == "notice.factory.component_unknown"


def test_bad_component_result_refused(granted):
    op = ask(granted)
    with pytest.raises(api.ApiError):
        report(granted, op, "succeeded", component={"code": "0"})
    with pytest.raises(api.ApiError):
        report(granted, op, "succeeded", component={"code": 0, "env": "x"})


def test_the_signing_view_shows_the_exact_commands(env):
    """The owner signs exactly this: the component, mode, revision and the catalog's commands it runs."""
    from types import SimpleNamespace

    from kd.cabinet.web import decisions as web_decisions

    env.ctx.deployed_revision = lambda: REV_A
    env.put("KD-50", project="digipaddock", assignee="@dev", status="In Progress")
    restart = {**STEP, "component": "session-host", "mode": "restart"}
    d = open_plan(env, steps=(STEP, restart))
    view = web_decisions._factory_plan(SimpleNamespace(ctx=env.ctx), d)
    first, second = view["steps"]
    assert first["mechanics"] == ("component.install · repo-attach install · "
                                  f"ревизия на момент подписи (сейчас {REV_A[:12]})")
    assert first["command"] == ("agents/dev/install/repo_attach_setup.sh install\n"
                                "agents/dev/install/repo_attach_setup.sh check")
    assert second["command"].splitlines()[1:] == ["systemctl restart digipaddock-session-host.service",
                                                  "common/install/session_host_setup.sh check"]
    assert "после конца ходов всех ролей" in second["command"] and not view["stale"]


def test_owner_messages_have_the_component_keys():
    import json

    root = Path(__file__).resolve().parents[1] / "cabinet" / "web" / "messages"
    for lang in ("ru", "en"):
        catalog = json.loads((root / f"{lang}.json").read_text(encoding="utf-8"))
        for key in ("component_done", "component_failed", "component_unknown"):
            assert f"notice.factory.{key}" in catalog


def test_a_brief_with_stages_refuses_release_steps(env):
    """Review F7: the PR of a staged brief names a stage's card, not the parent — a release step there would never
    run; it belongs to the stage's own plan."""
    from test_cabinet_factory_plan import _brief_card

    env.ctx.deployed_revision = lambda: REV_A
    card = _brief_card(env, steps=())
    stage = {"key": "a", "title": "A", "role": "@dev", "spec": "implementer", "depends": [], "acceptance": ["ок"]}
    env.store.edit_card(card.id, None, {"stages": [stage]}, actor="agent:analyst")
    with pytest.raises(api.ApiError, match="stage"):
        env.call("digi", {"op": "worker.edit", "agent": "dev", "card": card.id,
                         "revision": env.store.card(card.id).revision, "operations": [RELEASE_STEP]})
    env.store.edit_card(card.id, None, {"operations": [RELEASE_STEP]}, actor="agent:analyst")
    with pytest.raises(api.ApiError, match="stage"):
        env.call("digi", {"op": "worker.edit", "agent": "dev", "card": card.id,
                         "revision": env.store.card(card.id).revision, "stages": [stage]})
