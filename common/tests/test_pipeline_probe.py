"""The pipeline probe B01–B06 runs on a copy of a cabinet base and says ok for each scenario."""

import io

from kd import pipeline_probe
from kd.cabinet import devseed


def test_pipeline_probe_passes_on_a_seeded_base(tmp_path):
    svc = devseed.dev_service(str(tmp_path / "state"))
    devseed.apply(svc, out=io.StringIO())
    db = svc.ctx.store.path()
    svc.ctx.store.close()
    out = io.StringIO()
    code = pipeline_probe.run(db, out=out)
    lines = out.getvalue().splitlines()
    assert code == 0, out.getvalue()
    assert [line.split()[0] for line in lines] == ["B01", "B02", "B03", "B04", "B05", "B06"]
    assert all(line.endswith(" ok") for line in lines), out.getvalue()


def test_a_failed_scenario_says_why_and_fails_the_probe(tmp_path, monkeypatch):
    svc = devseed.dev_service(str(tmp_path / "state"))
    db = svc.ctx.store.path()
    svc.ctx.store.close()

    def broken(probe):
        probe.check(False, "the continuation was queued twice")

    monkeypatch.setattr(pipeline_probe, "SCENARIOS", (("B01", "broken", broken),))
    out = io.StringIO()
    assert pipeline_probe.run(db, out=out) == 1
    assert out.getvalue().strip() == "B01 broken ... FAIL the continuation was queued twice"
