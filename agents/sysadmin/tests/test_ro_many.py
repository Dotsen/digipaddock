import io
import subprocess
import time

from sysadmin import ro_many


def fake_run(results, delays=None):
    def run(argv, **kw):
        alias = argv[-1]
        time.sleep((delays or {}).get(alias, 0))
        rc, out = results[alias]
        if rc == "timeout":
            raise subprocess.TimeoutExpired(argv, 1, output=out)
        return subprocess.CompletedProcess(argv, rc, stdout=out)
    return run


def targets(*aliases):
    return [(a, ["ssh", a]) for a in aliases]


def test_outputs_in_given_order_with_headers_even_if_finished_out_of_order():
    out = io.BytesIO()
    run = fake_run({"a": (0, b"one\n"), "b": (0, b"two")}, delays={"a": 0.2})
    assert ro_many.run_many(targets("a", "b"), out, run=run) == 0
    assert out.getvalue() == "=== a\none\n=== b\ntwo\n".encode()


def test_nonzero_code_is_marked_and_overall_code_is_one():
    out = io.BytesIO()
    run = fake_run({"a": (0, b"ok\n"), "b": (255, b"ssh: connect timed out\n")})
    assert ro_many.run_many(targets("a", "b"), out, run=run) == 1
    assert "=== b\nssh: connect timed out\n(код 255)\n" in out.getvalue().decode()


def test_timeout_is_reported_as_124():
    out = io.BytesIO()
    run = fake_run({"a": ("timeout", b"partial")})
    assert ro_many.run_many(targets("a"), out, run=run) == 1
    text = out.getvalue().decode()
    assert text.startswith("=== a\npartial\n") and "(код 124" in text
