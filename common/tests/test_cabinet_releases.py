"""The state of a card's release from the facts — the forge's and the health check's notices and the
operations of the release — and the ``release_degraded`` problem held by the lead, re-signalled to BOSS."""

import pytest
from cabinet_env import CabinetEnv

from kd import pipeline_config, session_config
from kd.cabinet import api, outcomes, problems, releases, result, snapshot, users

HOSTED = session_config.HostConfig({"boss": 1, "dev": 2, "analyst": 1}, 600)
REV = "abc1234"


@pytest.fixture
def cab(tmp_path):
    env = CabinetEnv(tmp_path)
    users.Users(env.store).create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    api.install_host(env.ctx, lambda: HOSTED)
    env.ctx.pipeline = lambda: pipeline_config.Config(mode="enforce", since="2000-01-01T00:00:00Z")
    env.store._conn.execute("UPDATE meta SET value='2000-01-01T00:00:00Z' WHERE key='pipeline_since'")
    return env


def post(cab, card, key, source=None, level="info", **params):
    source = source or key.split(".")[1]
    user = "digi-forge" if source == "forge" else "root"
    return cab.call(user, {"op": "notice.post", "source": source, "level": level, "key": key, "card": card.id,
                           "params": params})


def operation(cab, card, key, state, kind="agent.reinstall", component=""):
    now = cab.now
    import json
    import secrets
    op_id = secrets.token_hex(16)
    cab.store._conn.execute(
        "INSERT INTO operations (id, key, kind, card, requested_by, agents, revision, state, payload_sha, params,"
        " created, updated) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (op_id, key, kind, card.id, "release", json.dumps(["dev"]), REV, state, "x",
         json.dumps({"component": component} if component else {}), now, now))
    cab.store._conn.commit()
    return op_id


def boss_signals(cab, card):
    return [c for c in cab.store.commands(agent="boss", card=card.id) if c["kind"] == "signal"]


def test_state_follows_the_notices_and_the_operations(cab):
    card = cab.card(status="In Progress")
    assert releases.state(cab.store, card.id)["state"] == "none"
    post(cab, card, "notice.forge.merged", pr=12, title="Кеш")
    assert releases.state(cab.store, card.id)["state"] == "merged"
    post(cab, card, "notice.forge.released", rev=REV, host="stage")
    found = releases.state(cab.store, card.id)
    assert (found["state"], found["rev"], found["host"]) == ("code_switched", REV, "stage")
    op = operation(cab, card, f"release:{REV}", "running")
    post(cab, card, "notice.release.healthy", rev=REV, checks="session_host, nginx")
    found = releases.state(cab.store, card.id)
    assert found["state"] == "activating" and [o["id"] for o in found["operations"]] == [op]
    cab.store._conn.execute("UPDATE operations SET state='succeeded' WHERE id=?", (op,))
    cab.store._conn.commit()
    assert releases.state(cab.store, card.id)["state"] == "healthy"
    cab.store._conn.execute("UPDATE operations SET state='failed' WHERE id=?", (op,))
    cab.store._conn.commit()
    assert releases.state(cab.store, card.id)["state"] == "degraded"


def test_the_forges_released_after_the_health_check_keeps_the_release_healthy(cab):
    """kd-merge posts «released» once the release's result is in — after the health check's own notice."""
    card = cab.card(status="In Progress")
    post(cab, card, "notice.release.healthy", rev=REV, checks="health")
    post(cab, card, "notice.forge.released", rev=REV, host="stage")
    found = releases.state(cab.store, card.id)
    assert (found["state"], found["rev"], found["host"]) == ("healthy", REV, "stage")


def test_a_degraded_health_notice_raises_one_problem_per_rev_and_healthy_resolves_it(cab):
    card = cab.card(status="In Progress", assignee="@dev")
    post(cab, card, "notice.forge.released", rev=REV, host="stage")
    post(cab, card, "notice.release.degraded", level="critical", rev=REV, failed="cabinet_schema", why="schema 28")
    post(cab, card, "notice.release.degraded", level="critical", rev=REV, failed="cabinet_schema", why="schema 28")
    [problem] = problems.open_for(cab.store, card.id)
    assert (problem["kind"], problem["agent"], problem["key"]) == ("release_degraded", "dev", f"release:{card.id}:{REV}")
    assert problem["detail"]["rev"] == REV and problem["detail"]["next_check"] > cab.now
    assert len(boss_signals(cab, card)) == 1 and "degraded" in boss_signals(cab, card)[0]["text"]
    assert releases.state(cab.store, card.id)["state"] == "degraded"
    assert snapshot.next_step(snapshot.facts(cab.ctx, cab.store.card(card.id)))["what"] == "release_degraded"
    post(cab, card, "notice.release.healthy", rev=REV, checks="session_host")
    assert problems.open_for(cab.store, card.id) == []
    assert releases.state(cab.store, card.id)["state"] == "healthy"


def test_a_failed_release_operation_and_the_forge_degraded_notice_raise_it_too(cab):
    card = cab.card(status="In Progress", assignee="@dev")
    post(cab, card, "notice.forge.release_degraded", level="warn", rev=REV, host="stage", why="postflight")
    assert [p["kind"] for p in problems.open_for(cab.store, card.id)] == ["release_degraded"]
    post(cab, card, "notice.release.healthy", rev=REV, checks="x")
    assert problems.open_for(cab.store, card.id) == []
    op = operation(cab, card, f"release:{REV}:repo-attach:check", "pending", kind="component.install",
                   component="repo-attach")
    from kd.cabinet import operations as ops_mod
    with cab.store.transaction():
        ops_mod._event(cab.store, "host", "operation.failed", ops_mod.row(cab.store, op), reason="exit 1")
    [problem] = problems.open_for(cab.store, card.id)
    assert problem["kind"] == "release_degraded" and problem["detail"]["why"].startswith("repo-attach")
    # a later release of another revision supersedes the degraded one
    post(cab, card, "notice.forge.released", rev="def5678", host="stage")
    assert problems.open_for(cab.store, card.id) == []


def test_sweep_resignals_boss_after_next_check(cab):
    card = cab.card(status="In Progress", assignee="@dev")
    post(cab, card, "notice.release.degraded", level="critical", rev=REV, failed="nginx", why="test failed")
    assert releases.sweep(cab.ctx) == 0
    cab.now = "2026-10-14T10:31:00Z"
    assert releases.sweep(cab.ctx) == 1
    assert len(boss_signals(cab, card)) == 2
    [problem] = problems.open_for(cab.store, card.id)
    assert problem["detail"]["checks"] == 1 and problem["detail"]["next_check"] > cab.now
    assert releases.sweep(cab.ctx) == 0
    assert cab.call("digi-cabinet", {"op": "pipeline.sweep"})["releases"] == 0


def test_a_problem_on_an_unhosted_card_goes_to_the_owner_as_a_notice(cab):
    api.install_host(cab.ctx, lambda: session_config.HostConfig({"dev": 2}, 600))  # no BOSS
    card = cab.card(status="In Progress", assignee="@dev")
    before = cab.store.last_seq()
    post(cab, card, "notice.release.degraded", level="critical", rev=REV, failed="nginx", why="test failed")
    keys = [e["payload"].get("key") for e in cab.store.events_since(before) if e["kind"] == "notice"]
    assert "notice.pipeline.release_degraded" in keys
    assert boss_signals(cab, card) == []


def test_result_and_snapshot_carry_the_release_state(cab):
    card = cab.card(status="In Progress")
    post(cab, card, "notice.forge.released", rev=REV, host="stage")
    found = result.view(cab.store, cab.store.card(card.id))["release"]
    assert found["state"] == "code_switched" and found["rev"] == REV
    text = snapshot.render(snapshot.build(cab.ctx, cab.store.card(card.id)))
    assert f"release: code_switched {REV} on stage" in text
    post(cab, card, "notice.release.healthy", rev=REV, checks="x")
    text = snapshot.render(snapshot.build(cab.ctx, cab.store.card(card.id)))
    assert f"release: healthy {REV}" in text
    assert outcomes.card_facts(cab.ctx, cab.store.card(card.id))["release"]["state"] == "healthy"


# --- the final review's findings (24.10) ---
def test_the_lead_s_turn_does_not_resolve_release_degraded_but_a_closed_card_does(cab):
    """Review F3: the problem lives until a healthy check or a later release; a Done card ends it in the sweep."""
    card = cab.card(status="In Progress", assignee="@dev")
    post(cab, card, "notice.release.degraded", level="critical", rev=REV, failed="nginx", why="502")
    command = cab.store.enqueue({"key": "t1", "agent": "dev", "thread": card.thread, "card": card.id,
                                 "kind": "assigned", "text": "[board] go"})
    outcomes.started(cab.ctx, command)
    assert [p["kind"] for p in problems.open_for(cab.store, card.id)] == ["release_degraded"]
    cab.store.edit_card(card.id, None, {"status": "Review"}, actor="user:owner")
    assert [p["kind"] for p in problems.open_for(cab.store, card.id)] == ["release_degraded"]
    cab.store.edit_card(card.id, None, {"status": "Done"}, actor="user:owner")
    cab.now = "2026-10-14T10:31:00Z"
    assert releases.sweep(cab.ctx) == 0
    assert problems.open_for(cab.store, card.id) == []


def test_the_sweep_is_quiet_in_observe_mode(cab):
    card = cab.card(status="In Progress", assignee="@dev")
    post(cab, card, "notice.release.degraded", level="critical", rev=REV, failed="nginx", why="502")
    cab.ctx.pipeline = lambda: pipeline_config.Config(mode="observe", since="2000-01-01T00:00:00Z")
    cab.now = "2026-10-14T10:31:00Z"
    before = len(boss_signals(cab, card))
    assert releases.sweep(cab.ctx) == 0 and len(boss_signals(cab, card)) == before


def test_a_newer_notice_without_a_rev_decides_the_state(cab):
    """Review F5: merged and refused carry no rev; they are still the newest fact."""
    card = cab.card(status="In Progress")
    post(cab, card, "notice.forge.released", rev=REV, host="stage")
    post(cab, card, "notice.release.healthy", rev=REV, checks="x")
    post(cab, card, "notice.forge.release_refused", why="main ушёл дальше")
    found = releases.state(cab.store, card.id)
    assert found["state"] == "refused" and found["why"] == "main ушёл дальше"
    post(cab, card, "notice.forge.merged", pr=13, title="Ещё")
    assert releases.state(cab.store, card.id)["state"] == "merged"


# --- a pair release (code + overlay) ---
def test_a_failed_operation_of_a_pair_release_names_the_code_rev(cab):
    card = cab.card(status="In Progress", assignee="@dev")
    op = operation(cab, card, f"release:{REV}+1234567:repo-attach:install", "pending", kind="component.install",
                   component="repo-attach")
    from kd.cabinet import operations as ops_mod
    with cab.store.transaction():
        ops_mod._event(cab.store, "host", "operation.failed", ops_mod.row(cab.store, op), reason="exit 1")
    [problem] = problems.open_for(cab.store, card.id)
    assert problem["detail"]["rev"] == REV


def test_operations_of_another_pair_with_the_same_code_rev_are_not_this_release_s(cab):
    """Review M6: an overlay-only release keeps the code rev — the failed operation of the earlier pair is not
    the current pair's; neither is one of a longer rev that starts with this one."""
    card = cab.card(status="In Progress")
    operation(cab, card, f"release:{REV}+1111111:repo-attach:install", "failed", kind="component.install",
              component="repo-attach")
    operation(cab, card, f"release:{REV}0+2222222", "running")
    mine = operation(cab, card, f"release:{REV}+2222222", "succeeded")
    post(cab, card, "notice.forge.released", rev=REV, host="stage", deploy_rev="2222222", release_id=f"{REV}+2222222")
    post(cab, card, "notice.release.healthy", rev=REV, checks="x")
    found = releases.state(cab.store, card.id)
    assert found["state"] == "healthy" and [o["id"] for o in found["operations"]] == [mine]


def test_snapshot_names_the_pair_release(cab):
    card = cab.card(status="In Progress")
    post(cab, card, "notice.forge.released", rev=REV, host="stage", deploy_rev="1234567", release_id=f"{REV}+1234567")
    post(cab, card, "notice.release.healthy", rev=REV, checks="x")
    found = releases.state(cab.store, card.id)
    assert (found["state"], found["rev"], found["deploy_rev"]) == ("healthy", REV, "1234567")
    text = snapshot.render(snapshot.build(cab.ctx, cab.store.card(card.id)))
    assert f"release: healthy {REV}+1234567 on stage" in text
