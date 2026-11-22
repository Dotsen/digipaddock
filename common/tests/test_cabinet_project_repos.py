"""a project's repository from the cabinet — the owner's request, the root unit's queue and answer."""

import json

import pytest

from kd.cabinet import api, projects_api, schema, users
from cabinet_env import CabinetEnv


def person(user):
    return api.Principal("user", -1, user)


@pytest.fixture
def env(tmp_path):
    cab = CabinetEnv(tmp_path)
    people = users.Users(cab.store)
    people.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    people.create("anna", "Анна", roles=[("partner", ["web"])], actor="user:owner")
    cab.ctx.repos_path = str(tmp_path / "repos.json")
    cab.ctx.repo_doorbell = str(tmp_path / "request")
    return cab


def call(env, user, request):
    return api.dispatch(env.ctx, person(user), request)


def root(env, request):
    return api.dispatch(env.ctx, env.principal("root"), request)


def refused(env, user, request):
    with pytest.raises(api.ApiError) as exc:
        call(env, user, request)
    return exc.value


def registry(tmp_path, data):
    (tmp_path / "repos.json").write_text(json.dumps(data), encoding="utf-8")


def bind(env, project="web", repo="example/Site"):
    return call(env, "owner", {"op": "project.repo.bind", "id": project, "repo": repo})


def test_a_bind_is_a_request_the_root_unit_takes(env, tmp_path):
    out = bind(env)
    assert out["project"]["repo"] == "example/Site" and out["project"]["repo_state"] == "pending"
    assert out["rung"] and (tmp_path / "request").exists()
    assert root(env, {"op": "project.repo.queue"})["items"] == [
        {"id": "web", "repo": "example/Site", "action": "bind"}]
    # the screens see the request with its project
    item = next(p for p in call(env, "owner", {"op": "project.list"})["items"] if p["id"] == "web")
    assert (item["repo"], item["repo_state"], item["repo_release"]) == ("example/Site", "pending", False)
    kinds = [e["kind"] for e in env.store.events_since(0)]
    assert kinds[-1] == "project.repo"


@pytest.mark.parametrize("repo", [
    "Site", "/Site", "example/", "example/Site/x", "kon stantin/Site", "example/.claude", ".x/Site",
    "example/a..b", "-o/Site", "example/" + "x" * 101, "o" * 65 + "/Site", "example/Site;rm", 7, None, "",
])
def test_a_bad_owner_repo_is_refused_before_any_request(env, tmp_path, repo):
    error = refused(env, "owner", {"op": "project.repo.bind", "id": "web", "repo": repo})
    assert error.kind == "invalid" and "owner/repo" in str(error)
    assert env.store.project("web")["repo_state"] == "" and not (tmp_path / "request").exists()
    assert root(env, {"op": "project.repo.queue"})["items"] == []


def test_the_cabinet_rule_is_inside_add_repo_rule():
    """What the cabinet takes add_repo.sh takes too (dev_agent.repos.REPO_RE), and the root unit checks the same."""
    import re
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "agents" / "dev"))
    from dev_agent import repo_attach, repos

    assert repo_attach.REPO_RE.pattern == projects_api.REPO.pattern
    for value in ("example/DigiPaddock", "a/b", "A_1/r.e-p", "o" * 64 + "/" + "r" * 100):
        assert projects_api.REPO.fullmatch(value) and repos.REPO_RE.fullmatch(value)
    assert re.fullmatch(repos.REPO_RE, "o/.claude") and not projects_api.REPO.fullmatch("o/.claude")


def test_only_settings_binds_and_only_root_takes_the_queue(env):
    for request in ({"op": "project.repo.bind", "id": "web", "repo": "o/r"}, {"op": "project.repo.unbind", "id": "web"}):
        assert refused(env, "anna", request).kind == "refused"
        with pytest.raises(api.ApiError):
            env.call("digi", {**request, "agent": "dev"})
    for request in ({"op": "project.repo.queue"},
                    {"op": "project.repo.result", "id": "web", "action": "bind", "repo": "o/r", "state": "connected"}):
        assert refused(env, "owner", request).kind == "refused"
        with pytest.raises(api.ApiError):
            env.call("digi", {**request, "agent": "dev"})


def test_a_waiting_request_holds_the_next_one_and_the_delete(env, tmp_path):
    bind(env)
    assert refused(env, "owner", {"op": "project.repo.bind", "id": "web", "repo": "o/r"}).reason == "repo_pending"
    assert refused(env, "owner", {"op": "project.repo.unbind", "id": "web"}).reason == "repo_pending"
    assert refused(env, "owner", {"op": "project.delete", "id": "web", "target": "digipaddock"}).reason == "project_bound"
    assert call(env, "owner", {"op": "project.impact", "id": "web"})["bound"] is True


@pytest.mark.parametrize("state", schema.REPO_RESULTS)
def test_the_root_unit_answers_each_result(env, tmp_path, state):
    bind(env)
    if state == "connected":
        registry(tmp_path, {"web": {"repo": "example/Site", "dir": "work/Site"}})
    out = root(env, {"op": "project.repo.result", "id": "web", "action": "bind", "repo": "example/Site",
                     "state": state, "detail": "x" * 500})
    assert out["project"]["repo_state"] == state and len(out["project"]["repo_detail"]) == projects_api.DETAIL_MAX
    assert root(env, {"op": "project.repo.queue"})["items"] == []
    item = next(p for p in call(env, "owner", {"op": "project.list"})["items"] if p["id"] == "web")
    assert item["repo_state"] == state and item["repo"] == "example/Site"


def test_a_stale_or_wrong_answer_is_refused(env):
    bind(env)
    base = {"op": "project.repo.result", "id": "web", "action": "bind", "repo": "example/Site"}
    for over, kind in (({"repo": "o/r", "state": "connected"}, "refused"),
                       ({"action": "unbind", "state": ""}, "refused"),
                       ({"action": "drop", "state": ""}, "invalid"),
                       ({"state": "maybe"}, "invalid"),
                       ({"state": "connected", "detail": 5}, "invalid"),
                       ({"state": "connected", "extra": 1}, "invalid")):
        with pytest.raises(api.ApiError) as exc:
            root(env, {**base, **over})
        assert exc.value.kind == kind, over
    assert env.store.project("web")["repo_state"] == "pending"


def test_unbind_takes_the_entry_out_and_then_the_project_may_go(env, tmp_path):
    registry(tmp_path, {"web": {"repo": "example/Site", "dir": "work/Site"}})
    item = next(p for p in call(env, "owner", {"op": "project.list"})["items"] if p["id"] == "web")
    assert (item["repo"], item["repo_state"]) == ("example/Site", "connected")  # bound by hand: shown as such
    out = call(env, "owner", {"op": "project.repo.unbind", "id": "web"})
    assert out["project"]["repo_state"] == "unbinding" and out["rung"]
    assert root(env, {"op": "project.repo.queue"})["items"] == [
        {"id": "web", "repo": "example/Site", "action": "unbind"}]
    registry(tmp_path, {})  # the unit's add_repo.sh --remove
    root(env, {"op": "project.repo.result", "id": "web", "action": "unbind", "repo": "example/Site", "state": ""})
    assert env.store.project("web")["repo"] == "" and env.store.project("web")["repo_state"] == ""
    assert call(env, "owner", {"op": "project.delete", "id": "web", "target": "digipaddock"})["deleted"] == "web"


def test_a_failed_unbind_keeps_the_repository(env, tmp_path):
    registry(tmp_path, {"web": {"repo": "example/Site", "dir": "work/Site"}})
    call(env, "owner", {"op": "project.repo.unbind", "id": "web"})
    root(env, {"op": "project.repo.result", "id": "web", "action": "unbind", "repo": "example/Site",
               "state": "error", "detail": "registry unreadable"})
    item = next(p for p in call(env, "owner", {"op": "project.list"})["items"] if p["id"] == "web")
    assert (item["repo"], item["repo_state"], item["repo_detail"]) == ("example/Site", "error", "registry unreadable")
    assert refused(env, "owner", {"op": "project.delete", "id": "web", "target": "digipaddock"}).reason == "project_bound"


def test_a_request_that_never_bound_is_dropped_without_the_unit(env, tmp_path):
    bind(env)
    root(env, {"op": "project.repo.result", "id": "web", "action": "bind", "repo": "example/Site",
               "state": "no_access"})
    (tmp_path / "request").unlink()
    out = call(env, "owner", {"op": "project.repo.unbind", "id": "web"})
    assert out["project"]["repo"] == "" and out["project"]["repo_state"] == "" and not out["rung"]
    assert not (tmp_path / "request").exists()
    assert refused(env, "owner", {"op": "project.repo.unbind", "id": "web"}).kind == "invalid"  # nothing left


def test_a_released_project_keeps_its_repository(env, tmp_path):
    registry(tmp_path, {"digipaddock": {"repo": "example/DigiPaddock", "dir": "work/DigiPaddock",
                                  "release": "digipaddock"}})
    item = next(p for p in call(env, "owner", {"op": "project.list"})["items"] if p["id"] == "digipaddock")
    assert item["repo_release"] is True and item["repo_state"] == "connected"
    assert refused(env, "owner", {"op": "project.repo.unbind", "id": "digipaddock"}).reason == "repo_release"
    assert refused(env, "owner", {"op": "project.repo.bind", "id": "digipaddock", "repo": "o/r"}).reason == "repo_release"
    # the same repository again is a check, the unit keeps the release
    assert call(env, "owner", {"op": "project.repo.bind", "id": "digipaddock",
                               "repo": "example/DigiPaddock"})["project"]["repo_state"] == "pending"


def test_a_list_survives_an_unreadable_registry(env, tmp_path):
    (tmp_path / "repos.json").write_text("{not json", encoding="utf-8")
    items = call(env, "owner", {"op": "project.list"})["items"]
    assert items and all(p["repo_release"] is False for p in items)
    assert refused(env, "owner", {"op": "project.repo.bind", "id": "web", "repo": "o/r"}).kind == "unavailable"


def test_a_registry_entry_taken_out_by_hand_shows_unbound(env, tmp_path):
    bind(env)
    root(env, {"op": "project.repo.result", "id": "web", "action": "bind", "repo": "example/Site",
               "state": "connected"})
    item = next(p for p in call(env, "owner", {"op": "project.list"})["items"] if p["id"] == "web")
    assert (item["repo"], item["repo_state"]) == ("", "")


def test_a_bell_that_fails_leaves_the_request_waiting(env, tmp_path):
    env.ctx.repo_doorbell = str(tmp_path / "missing" / "request")
    out = bind(env)
    assert out["rung"] is False and root(env, {"op": "project.repo.queue"})["items"][0]["id"] == "web"
