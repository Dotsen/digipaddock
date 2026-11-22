"""The one-lead probe L01–L03 runs on a copy of a cabinet base and says ok for each scenario."""

import io

from kd import one_lead_probe
from kd.cabinet import devseed


def _base(tmp_path, seed=True):
    svc = devseed.dev_service(str(tmp_path / "state"))
    if seed:
        devseed.apply(svc, out=io.StringIO())
    db = svc.ctx.store.path()
    svc.ctx.store.close()
    return db


def test_one_lead_probe_passes_on_a_seeded_base(tmp_path):
    out = io.StringIO()
    code = one_lead_probe.run(_base(tmp_path), out=out)
    lines = out.getvalue().splitlines()
    assert code == 0, out.getvalue()
    assert [line.split()[0] for line in lines] == ["L01", "L02", "L03"]
    assert all(" ... ok" in line for line in lines), out.getvalue()
    assert lines[2].endswith("(C01 counts: queued=4 merged=0 limited=0)")


def test_a_failed_scenario_says_why_and_fails_the_probe(tmp_path, monkeypatch):
    def broken(probe):
        probe.check(False, "two continuations")

    monkeypatch.setattr(one_lead_probe, "SCENARIOS", (("L01", "broken", broken),))
    out = io.StringIO()
    assert one_lead_probe.run(_base(tmp_path, seed=False), out=out) == 1
    assert out.getvalue().strip() == "L01 broken ... FAIL two continuations"


def test_live_base_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(one_lead_probe, "LIVE_BASE", str(tmp_path / "live.db"))
    assert one_lead_probe.run(str(tmp_path / "live.db"), out=io.StringIO()) == 2
