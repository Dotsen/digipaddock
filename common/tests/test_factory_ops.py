"""The factory's operations — one grant format, a catalog of its own per role."""

import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from kd import admin_grant, admin_task, factory_ops

NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
AGENTS = ("analyst", "boss", "dev", "sysadmin")
CATALOG = {"schema_version": 1, "projects": ["digipaddock"], "requesters": ["dev"],
           "operations": {"agent.reinstall": {"target": "factory"}}}


def catalog(**over):
    return factory_ops.Catalog.from_dict({**CATALOG, **over})


def card(task_id="KD-9085", project="digipaddock", assignee="dev", status="In Progress"):
    return admin_task.Card(task_id=task_id, title="Переустановка", status=status, project=project,
                           assignees=(assignee,), servers=(), labels=(), sections={"Description": "Цель."},
                           revision=3)


def step(**over):
    data = {"verb": "agent.reinstall", "target": "factory", "agents": ["dev"], "what": "Переустановить dev",
            "effect": "Новые инструкции dev", "risk": {"level": "low", "why": "Очередь сохраняется"},
            "rollback": "Предыдущая выкатка и переустановка"}
    data.update(over)
    return data


def raw(task_id="KD-9085", **over):
    data = {"schema_version": 2, "task_id": task_id, "intent": "Обновить роли", "basis": "ТЗ v2",
            "operations": [step()]}
    data.update(over)
    return data


def store(tmp_path):
    root = tmp_path / "grants"
    root.mkdir(exist_ok=True)
    return admin_grant.Store(str(root), owner_uid=os.lstat(str(root)).st_uid)


def grant_for(tmp_path, *, agents=("dev",), the_card=None):
    the_card = the_card or card()
    prop = factory_ops.proposal(raw(operations=[step(agents=list(agents))]), catalog=catalog(), agents_known=AGENTS)
    s = store(tmp_path)
    g = s.approve(the_card, prop, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="test",
                  expect_card_sha256=the_card.scope_sha256, expect_proposal_sha256=prop.sha256,
                  policy=admin_grant.FACTORY, factory=catalog().scope)
    return s, the_card, g


# --- the proposal ---------------------------------------------------------------------------------
def test_proposal_normalizes_agents_and_hashes():
    prop = factory_ops.proposal(raw(operations=[step(agents=["dev", "boss"])]), catalog=catalog(),
                                agents_known=AGENTS)
    assert prop.as_dict()["operations"][0]["agents"] == ["boss", "dev"]
    assert len(prop.sha256) == 64 and prop.task_id == "KD-9085"


@pytest.mark.parametrize("change,why", [
    ({"agents": ["ghost"]}, "not a generated agent"),
    ({"agents": []}, "agents"),
    ({"agents": ["dev", "dev"]}, "twice"),
    ({"verb": "shell"}, "unknown"),
    ({"target": "node-f"}, "target"),
    ({"command": "rm -rf /"}, "unexpected"),
    ({"server": "example-prod"}, "unexpected"),
    ({"path": "/opt/x"}, "unexpected"),
])
def test_proposal_rejects_unknown_agent_and_shell_fields(change, why):
    with pytest.raises(factory_ops.ProposalError, match=why):
        factory_ops.proposal(raw(operations=[step(**change)]), catalog=catalog(), agents_known=AGENTS)


def test_catalog_must_be_exact():
    with pytest.raises(factory_ops.CatalogError):
        factory_ops.Catalog.from_dict({**CATALOG, "operations": {"agent.reinstall": {"target": "factory"},
                                                                 "node.reboot": {"target": "factory"}}})
    with pytest.raises(factory_ops.CatalogError):
        factory_ops.Catalog.from_dict({**CATALOG, "requesters": ["sysadmin"]})


def test_repo_catalog_loads():
    cat = factory_ops.load_catalog()
    assert cat.projects == frozenset({"digipaddock", "digipaddock-deploy"}) and cat.requesters == frozenset({"dev"})
    assert set(cat.kinds) == {"agent.reinstall", "component.install"}
    # exactly the owner's list — nothing else is installable from a card
    assert {(c, m) for c, modes in cat.components.items() for m in modes} == {
        ("repo-attach", "install"), ("repo-attach", "check"), ("transcribe", "install"), ("transcribe", "check"),
        ("e2e-runtime", "install"), ("e2e-runtime", "check"), ("devcheck", "install"), ("devcheck", "check"),
        ("session-host", "check"), ("session-host", "restart")}
    restart = cat.mode("session-host", "restart")
    assert restart.run == ("systemctl", "restart", "digipaddock-session-host.service") and restart.drain == "all"
    assert cat.mode("repo-attach", "install").check == ("agents/dev/install/repo_attach_setup.sh", "check")
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for modes in cat.components.values():
        for mode in modes.values():
            for argv in (mode.run, mode.check):
                if argv and argv[0] != "systemctl":
                    assert os.path.isfile(os.path.join(root, argv[0])), argv[0]


# --- component.install -------------------------------------------------------------------
REV = "0123456789abcdef0123456789abcdef01234567"
COMPONENTS = {"target": "factory", "components": {
    "repo-attach": {"install": {"run": ["agents/dev/install/repo_attach_setup.sh", "install"],
                                "check": ["agents/dev/install/repo_attach_setup.sh", "check"]},
                    "check": {"run": ["agents/dev/install/repo_attach_setup.sh", "check"]}},
    "session-host": {"restart": {"run": ["systemctl", "restart", "digipaddock-session-host.service"], "drain": "all"}}}}


def ccatalog():
    return catalog(operations={"agent.reinstall": {"target": "factory"}, "component.install": COMPONENTS})


def cstep(**over):
    data = {"verb": "component.install", "target": "factory", "component": "repo-attach", "mode": "install",
            "revision": REV, "what": "Поставить kd-repo-attach", "effect": "Привязка репозиториев из кабинета",
            "risk": {"level": "low", "why": "Скрипт идемпотентен"}, "rollback": "repo_attach_setup.sh disable"}
    data.update(over)
    return data


def cgrant(tmp_path, *steps):
    the_card = card()
    prop = factory_ops.proposal(raw(operations=list(steps) or [cstep()]), catalog=ccatalog(), agents_known=AGENTS)
    s = store(tmp_path)
    return the_card, s.approve(the_card, prop, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="test",
                               expect_card_sha256=the_card.scope_sha256, expect_proposal_sha256=prop.sha256,
                               policy=admin_grant.FACTORY, factory=ccatalog().scope)


def _ccheck(g, the_card, **over):
    args = dict(card=the_card, requester="dev", kind="component.install", agents=(), catalog=ccatalog(), now=NOW,
                component="repo-attach", mode="install", revision=REV)
    args.update(over)
    return factory_ops.check_request(g, **args)


def test_component_step_normalized_without_agents():
    prop = factory_ops.proposal(raw(operations=[cstep(), step()]), catalog=ccatalog(), agents_known=AGENTS)
    first, second = prop.as_dict()["operations"]
    assert first["component"] == "repo-attach" and first["mode"] == "install" and first["revision"] == REV
    assert "agents" not in first and second["agents"] == ["dev"]


@pytest.mark.parametrize("change,why", [
    ({"component": "nginx"}, "unknown component"),
    ({"mode": "disable"}, "unknown mode"),
    ({"mode": "install --force"}, "unknown mode"),
    ({"revision": ""}, "revision"),
    ({"revision": "HEAD"}, "revision"),
    ({"agents": ["dev"]}, "unexpected"),
    ({"run": ["bash", "-c", "id"]}, "unexpected"),
    ({"args": ["--prod"]}, "unexpected"),
    ({"target": "example-prod"}, "target"),
])
def test_component_step_outside_catalog_refused(change, why):
    with pytest.raises(factory_ops.ProposalError, match=why):
        factory_ops.proposal(raw(operations=[cstep(**change)]), catalog=ccatalog(), agents_known=AGENTS)


@pytest.mark.parametrize("argv", [
    ["/usr/local/bin/x.sh", "install"],
    ["common/install/../../etc/x.sh"],
    ["agents/dev/install/repo_attach_setup.sh", "--prefix=/"],
    ["agents/dev/install/repo_attach_setup.sh", "install;id"],
    ["systemctl", "stop", "digipaddock-session-host.service"],
    ["systemctl", "restart", "nginx.service"],
    ["bash", "-c", "id"],
    [],
])
def test_catalog_refuses_free_argv(argv):
    bad = {"target": "factory", "components": {"x": {"install": {"run": argv}}}}
    with pytest.raises(factory_ops.CatalogError):
        catalog(operations={"component.install": bad})


def test_catalog_refuses_unknown_drain_and_keys():
    for spec in ({"run": ["common/install/x.sh"], "drain": "some"}, {"run": ["common/install/x.sh"], "env": {}}):
        with pytest.raises(factory_ops.CatalogError):
            catalog(operations={"component.install": {"target": "factory", "components": {"x": {"m": spec}}}})


def test_component_request_covered_on_its_revision_repeatedly(tmp_path):
    the_card, g = cgrant(tmp_path)
    assert _ccheck(g, the_card)["mode"] == "install"
    assert _ccheck(g, the_card, revision=REV[:12])["component"] == "repo-attach"  # short or full: one commit
    assert _ccheck(g, the_card)["mode"] == "install"  # the owner's choice: repeats while the grant lives


def test_component_request_after_a_release_refused(tmp_path):
    the_card, g = cgrant(tmp_path)
    with pytest.raises(admin_grant.GrantError, match="new plan for this revision"):
        _ccheck(g, the_card, revision="f" * 40)


def test_component_request_other_mode_or_component_refused(tmp_path):
    the_card, g = cgrant(tmp_path)
    with pytest.raises(admin_grant.GrantError, match="not covered"):
        _ccheck(g, the_card, mode="check")
    with pytest.raises(admin_grant.GrantError, match="unknown component"):
        _ccheck(g, the_card, component="nginx")
    with pytest.raises(admin_grant.GrantError, match="unknown mode"):
        _ccheck(g, the_card, mode="disable")


def test_at_revision_moves_only_component_steps():
    prop = factory_ops.proposal(raw(operations=[cstep(), step()]), catalog=ccatalog(), agents_known=AGENTS)
    moved = factory_ops.at_revision(prop, "f" * 40)
    first, second = moved.as_dict()["operations"]
    assert first["revision"] == "f" * 40 and second == prop.as_dict()["operations"][1]
    assert moved.sha256 != prop.sha256
    with pytest.raises(factory_ops.ProposalError):
        factory_ops.at_revision(prop, "")


def test_component_grant_does_not_cover_a_reinstall(tmp_path):
    the_card, g = cgrant(tmp_path)
    with pytest.raises(admin_grant.GrantError, match="not covered"):
        _check(g, the_card, catalog=ccatalog())


# --- one grant format, two policies ---------------------------------------------------------------
def test_factory_grant_roundtrip(tmp_path):
    s, the_card, g = grant_for(tmp_path)
    loaded = s.load("KD-9085", policies=(admin_grant.FACTORY,), factory=catalog().scope)
    assert loaded == g and loaded.assignee == "dev" and loaded.project == "digipaddock"
    admin_grant.check_usable(loaded, the_card, NOW, policy=admin_grant.FACTORY, factory=catalog().scope)


def test_factory_grant_refused_by_default_policy(tmp_path):
    s, the_card, g = grant_for(tmp_path)
    with pytest.raises(admin_grant.GrantError, match="sysadmin"):
        s.load("KD-9085")  # the sysadmin's executors read with the default: a factory grant is not theirs
    with pytest.raises((admin_grant.GrantError, admin_task.CardError)):
        admin_grant.check_usable(g, the_card, NOW)


def test_server_grant_refused_for_factory(tmp_path):
    from test_admin_grant import approve

    s, admin_card, _, g = approve(tmp_path)
    with pytest.raises(admin_grant.GrantError):
        s.load("KD-123", policies=(admin_grant.FACTORY,), factory=catalog().scope)
    with pytest.raises(admin_grant.GrantError):
        admin_grant.check_usable(g, admin_card, datetime(2026, 10, 9, 18, 0, tzinfo=timezone.utc),
                                 policy=admin_grant.FACTORY, factory=catalog().scope)


@pytest.mark.parametrize("assignee,project", [("sysadmin", "digipaddock"), ("dev", "admin"), ("analyst", "digipaddock")])
def test_factory_policy_needs_dev_and_factory_project(tmp_path, assignee, project):
    the_card = card(assignee=assignee, project=project)
    prop = factory_ops.proposal(raw(), catalog=catalog(), agents_known=AGENTS)
    with pytest.raises((admin_grant.GrantError, admin_task.CardError)):
        store(tmp_path).approve(the_card, prop, now=NOW, ttl=admin_grant.DEFAULT_TTL, via="test",
                                expect_card_sha256=the_card.scope_sha256, expect_proposal_sha256=prop.sha256,
                                policy=admin_grant.FACTORY, factory=catalog().scope)


# --- a request against the grant -----------------------------------------------------------------
def _check(g, the_card, **over):
    args = dict(card=the_card, requester="dev", kind="agent.reinstall", agents=("dev",), catalog=catalog(), now=NOW)
    args.update(over)
    return factory_ops.check_request(g, **args)


def test_check_request_covered(tmp_path):
    _, the_card, g = grant_for(tmp_path, agents=("dev", "boss"))
    assert _check(g, the_card, agents=("boss",))["verb"] == "agent.reinstall"


def test_check_request_agent_outside_grant_refused(tmp_path):
    _, the_card, g = grant_for(tmp_path)
    with pytest.raises(admin_grant.GrantError, match="sysadmin"):
        _check(g, the_card, agents=("dev", "sysadmin"))


def test_check_request_unknown_kind_refused(tmp_path):
    _, the_card, g = grant_for(tmp_path)
    with pytest.raises(admin_grant.GrantError, match="kind"):
        _check(g, the_card, kind="service.restart")


def test_check_request_requester_not_dev_refused(tmp_path):
    _, the_card, g = grant_for(tmp_path)
    with pytest.raises(admin_grant.GrantError, match="requester"):
        _check(g, the_card, requester="sysadmin")


def test_revoked_or_expired_factory_grant_unusable(tmp_path):
    s, the_card, g = grant_for(tmp_path)
    with pytest.raises(admin_grant.GrantError, match="expired"):
        _check(g, the_card, now=NOW + admin_grant.DEFAULT_TTL + timedelta(seconds=1))
    revoked = s.revoke("KD-9085", now=NOW, via="test", policies=(admin_grant.FACTORY,), factory=catalog().scope)
    with pytest.raises(admin_grant.GrantError, match="revoked"):
        _check(revoked, the_card)


def test_changed_scope_stops_the_factory_grant(tmp_path):
    _, the_card, g = grant_for(tmp_path)
    moved = admin_task.Card(**{**the_card.__dict__, "sections": {"Description": "Другая цель."}})
    with pytest.raises(admin_grant.GrantError, match="scope"):
        _check(g, moved)


def test_old_sysadmin_grant_parses_unchanged(tmp_path):
    from test_admin_grant import approve

    s, admin_card, _, g = approve(tmp_path)
    assert s.load("KD-123") == g
    raw_file = json.loads((tmp_path / "grants" / "KD-123.json").read_text(encoding="utf-8"))
    assert set(raw_file) == set(g.to_dict())  # the file format is the same, no policy key
