"""the stages probe (protocol B) runs green on a fresh base and refuses the live one."""

import io

from kd import stages_probe


def test_probe_runs_green_on_a_fresh_base(tmp_path):
    out = io.StringIO()
    assert stages_probe.run(str(tmp_path / "copy.db"), out) == 0
    lines = out.getvalue().splitlines()
    assert [line[:3] for line in lines] == ["B01", "B02", "B03", "B04"] and all(line.endswith("ok") for line in lines)


def test_probe_refuses_the_live_base():
    out = io.StringIO()
    assert stages_probe.run(stages_probe.LIVE, out) == 1 and "live base" in out.getvalue()
