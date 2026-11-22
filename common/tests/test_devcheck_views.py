"""Where a dev.check is seen — the card's snapshot line, the log root of
kd-host-read, the root CLI (kd-cabinet devcheck|contour) and the contours over HTTP for the Servers page."""

import io
import json
import runpy
import sys
from pathlib import Path

import pytest

from kd import host_read
from kd.cabinet import api, snapshot
from kd.cabinet.client import CabinetError

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_cabinet_devchecks import HEAD, ask, cab, ended  # noqa: E402,F401
from test_cabinet_factory_plan import env  # noqa: E402,F401
from test_cabinet_outcomes import report  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]


def line(cab, card="KD-50"):
    text = cab.call("digi-cabinet", {"op": "card.snapshot", "card": card, "format": "text"})["text"]
    return next(row for row in text.splitlines() if row.startswith("checks:"))


def test_the_snapshot_says_none_without_a_check(cab):
    assert line(cab) == "checks: none"
    assert snapshot.build(cab.ctx, cab.store.card("KD-50"))["checks"] == {"items": [], "total": 0}


def test_the_snapshot_line_follows_the_check(cab):
    op = ask(cab)["operation"]["id"]
    assert line(cab) == f"checks: pending unit on {HEAD[:7]} ({op[:12]})"
    report(cab, {"type": "devcheck", "check": op, "state": "running"})
    assert line(cab) == f"checks: running unit on {HEAD[:7]} ({op[:12]})"
    ended(cab, op)
    assert line(cab) == (f"checks: succeeded unit on {HEAD[:7]} ({op[:12]}) · log kd-host-read tail -n 200 "
                         f"devcheck/{op}.log")
    built = snapshot.build(cab.ctx, cab.store.card("KD-50"))["checks"]
    assert built["total"] == 1 and built["items"][0]["id"] == op and built["items"][0]["class"] == "passed"


def test_a_red_check_names_its_class_and_the_older_ones_are_counted(cab):
    first = ask(cab)["operation"]["id"]
    ended(cab, first, "checks_failed")
    second = ask(cab)["operation"]["id"]
    ended(cab, second, "checks_failed")
    assert line(cab) == (f"checks: failed unit on {HEAD[:7]} ({second[:12]}) — checks_failed · log kd-host-read "
                         f"tail -n 200 devcheck/{second}.log (+1 earlier)")


def test_the_devcheck_log_is_a_host_read_root():
    catalog = host_read.load_catalog(ROOT / "common/coordination/host-read.json")
    assert "/var/log/digipaddock-devcheck" in catalog.roots
    named = json.loads((ROOT / "common/coordination/host-read.json").read_text(encoding="utf-8"))["roots"]
    assert named["devcheck"] == "/var/log/digipaddock-devcheck"


def cli(cab, *argv):
    mod = runpy.run_path(str(ROOT / "common" / "bin" / "kd-cabinet"), run_name="kd_cabinet")

    class Client:
        def call(self, request):
            try:
                return api.dispatch(cab.ctx, api.Principal("root", 0, "root"), request)
            except api.ApiError as exc:
                raise CabinetError(exc.kind, str(exc)) from None

    out, err = io.StringIO(), io.StringIO()
    rc = mod["main"](list(argv), client=Client(), out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_the_cli_lists_and_cancels_checks(cab):
    op = ask(cab)["operation"]["id"]
    rc, out, err = cli(cab, "devcheck", "list")
    assert rc == 0, err
    assert out.splitlines() == [f"{op} pending KD-50 digipaddock#7 {HEAD[:7]} unit app-ci -"]
    assert cli(cab, "devcheck", "list", "--card", "KD-50")[1] == out
    rc, out, err = cli(cab, "devcheck", "cancel", op)
    assert rc == 0 and out == f"devcheck {op} failed cancelled\n"


def test_the_cli_shows_and_tends_the_contours(cab):
    ask(cab)
    rc, out, err = cli(cab, "contour", "list")
    assert rc == 0, err
    assert out.splitlines() == ["app-ci idle capacity 2 · digipaddock, digipaddock-deploy"]
    rc, out, err = cli(cab, "contour", "maintenance", "app-ci", "--reason", "диск заполнен")
    assert rc == 0 and out == "contour app-ci maintenance: диск заполнен\n"
    assert cli(cab, "contour", "list")[1].startswith("app-ci maintenance capacity 2")
    rc, out, err = cli(cab, "contour", "release", "app-ci")
    assert rc == 0 and out == "contour app-ci idle\n"
    assert cli(cab, "contour", "release", "nope")[0] == 2


@pytest.mark.parametrize("argv", [["devcheck"], ["devcheck", "cancel"], ["contour"], ["contour", "maintenance", "x"],
                                  ["contour", "drop", "x"]])
def test_the_cli_refuses_bad_forms(cab, argv):
    assert cli(cab, *argv)[0] == 2


@pytest.fixture
def w(tmp_path):
    from web_env import WebEnv

    w = WebEnv(tmp_path)
    path = tmp_path / "contours.json"
    path.write_text(json.dumps({"app-ci": {"host": "local", "components": ["digipaddock"], "exclusive": True}}),
                    encoding="utf-8")
    w.env.ctx.contours_path = str(path)
    w.users.create("owner", "Владелец", roles=[("owner", "*")], actor="root")
    w.users.create("anna", "Анна", roles=[("partner", "*")], actor="root")
    return w


def test_contours_over_http_for_a_reader_only(w):
    anna = w.other_client()
    w.login("anna", client=anna)
    assert anna.get("/api/contours").status_code == 403
    w.login()
    rows = w.client.get("/api/contours").json()
    assert [(r["name"], r["state"], r["capacity"], r["running"]) for r in rows] == [("app-ci", "idle", 1, [])]
