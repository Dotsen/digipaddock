"""The optional install step ``bootstrap.sh starter``: a minimal start on an empty board, through the cabinet's own
API as root (the dev service's base, no seed)."""

import io
import json
from pathlib import Path

import pytest

from kd import bootstrap, starter
from kd.cabinet import api, client, devseed

REPO = Path(__file__).resolve().parents[2]
INSTALL = REPO / "host" / "install.example.json"
ROOT = api.Principal("root", 0, "root")


@pytest.fixture
def cabinet(tmp_path):
    svc = devseed.dev_service(str(tmp_path / "state"))
    return svc, client.LocalClient(svc.ctx, ROOT)


def _install(tmp_path, roles):
    data = json.loads(INSTALL.read_text(encoding="utf-8"))
    data["roles"] = {role: {"slots": 1} for role in roles}
    path = tmp_path / "install.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def _run(action, install, local):
    out, err = io.StringIO(), io.StringIO()
    return (
        starter.main(action, install, client=local, out=out, err=err),
        out.getvalue(),
        err.getvalue(),
    )


def test_apply_puts_the_owners_cards_on_the_board_once(cabinet, tmp_path):
    svc, local = cabinet
    install = _install(tmp_path, ["analyst", "boss", "dev", "sysadmin"])
    rc, _, err = _run("check", install, local)
    assert rc == 1 and "tour" in err
    assert _run("apply", install, local)[0] == 0
    cards = svc.ctx.store.cards(project="start")
    keys = {label for c in cards for label in c.labels if label.startswith("starter:")}
    assert keys == {
        "starter:tour",
        "starter:first-task",
        "starter:first-server",
        "starter:people",
    }
    # the owner's own To Do, each with items to tick: no agent is asked to do anything
    assert {(c.status, c.assignee) for c in cards} == {("To Do", "@owner")}
    assert all(c.acceptance and not any(a.checked for a in c.acceptance) for c in cards)
    assert {p["id"]: p["route"] for p in local.call({"op": "project.list"})["items"]}[
        "start"
    ] == "@owner"
    assert not svc.ctx.store._conn.execute("SELECT 1 FROM commands").fetchall()
    assert _run("check", install, local)[0] == 0
    assert _run("apply", install, local)[1] == ""  # again: nothing more
    assert len(svc.ctx.store.cards(project="start")) == 4


def test_a_card_for_a_role_comes_only_with_the_role(cabinet, tmp_path):
    svc, local = cabinet
    assert _run("apply", _install(tmp_path, ["boss", "dev"]), local)[0] == 0
    titles = {c.title for c in svc.ctx.store.cards(project="start")}
    assert titles == {
        "Знакомство с кабинетом",
        "Пригласить партнёра",
    }  # no owner's language: the default


def test_the_language_is_the_first_owners(cabinet, tmp_path):
    svc, local = cabinet
    svc.users.create("zed", "Zed", roles=[("owner", "*")], actor="root")
    svc.users.create("amy", "Amy", language="en", roles=[("owner", "*")], actor="root")
    assert _run("apply", _install(tmp_path, ["analyst"]), local)[0] == 0
    names = {p["id"]: p["name"] for p in local.call({"op": "project.list"})["items"]}
    assert names["start"] == "Getting started"
    assert "The first task: an idea for the analyst" in {
        c.title for c in svc.ctx.store.cards(project="start")
    }


def test_a_run_cut_short_is_finished_and_the_owners_changes_stay(cabinet, tmp_path):
    svc, local = cabinet
    install = _install(tmp_path, ["dev"])
    assert _run("apply", install, local)[0] == 0
    tour, people = sorted(svc.ctx.store.cards(project="start"), key=lambda c: c.title)
    local.call(
        {
            "op": "card.edit",
            "card": tour.id,
            "revision": tour.revision,
            "changes": {"status": "Done"},
        }
    )
    svc.ctx.store._conn.execute(
        "DELETE FROM cards WHERE id=?", (people.id,)
    )  # as if the run stopped before it
    assert _run("check", install, local)[0] == 1
    assert _run("apply", install, local)[0] == 0
    cards = svc.ctx.store.cards(project="start")
    assert len(cards) == 2 and svc.ctx.store.card(tour.id).status == "Done"


def test_a_card_made_but_not_labelled_is_finished_not_made_again(cabinet, tmp_path):
    svc, local = cabinet
    install = _install(tmp_path, ["dev"])
    local.call(
        {"op": "project.create", "id": "start", "name": "Начало работы", "route": "@owner"}
    )
    tour = next(c for c in starter.load()["cards"] if c["key"] == "tour")
    local.call(  # as if the run stopped between card.create and card.edit
        {
            "op": "card.create",
            "project": "start",
            "assignee": "@owner",
            "title": tour["title"]["ru"],
            "description": tour["description"]["ru"],
        }
    )
    assert _run("apply", install, local)[0] == 0
    cards = svc.ctx.store.cards(project="start")
    assert len(cards) == 2
    healed = next(c for c in cards if c.title == tour["title"]["ru"])
    assert "starter:tour" in healed.labels and healed.status == "To Do"
    assert healed.acceptance


def test_the_step_is_not_part_of_all_and_runs_by_name():
    assert "starter" not in bootstrap.PHASES and "starter" in bootstrap.OPTIONAL
    assert bootstrap._parse(["starter"]) == ("starter", False, "", "")
    install = bootstrap.install_spec.load_install(INSTALL)
    projects = bootstrap.install_spec.load_projects(
        REPO / "host" / "projects.example.json"
    )
    host = bootstrap.naming.load_host(str(REPO / "host" / "host.example.json"))
    steps = bootstrap.steps(
        install, projects, bootstrap.Where("/opt/digipaddock", "", "/tmp/x"), host=host
    )
    [step] = steps["starter"]
    assert step.check[-3:] == ("starter", "check", "/opt/digipaddock/host/install.json")
    assert not any(
        s.name == step.name for s in steps["check"]
    )  # check never asks for it


def test_every_card_has_both_languages():
    data = starter.load()
    for text in (
        [data["project"]["name"]]
        + [c[f] for c in data["cards"] for f in ("title", "description")]
        + [a for c in data["cards"] for a in c["acceptance"]]
    ):
        assert set(text) == set(starter.LANGUAGES) and all(text.values())
