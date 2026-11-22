"""kd-notes add — one entry into the agent's shared notes, under the lock of kd.workspace."""

import importlib.machinery
import importlib.util
import io
from pathlib import Path

import pytest

BIN = Path(__file__).resolve().parents[1] / "bin" / "kd-notes"


@pytest.fixture
def kd_notes():
    loader = importlib.machinery.SourceFileLoader("kd_notes_bin", str(BIN))
    spec = importlib.util.spec_from_loader("kd_notes_bin", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


@pytest.fixture
def folder(tmp_path):
    (tmp_path / "memory").mkdir()
    (tmp_path / "work").mkdir()
    (tmp_path / "memory/notes.md").write_text("# notes\n", encoding="utf-8")
    return str(tmp_path)


def test_add_appends_one_dated_entry(kd_notes, folder):
    out = io.StringIO()
    assert kd_notes.main(["add", "pitfall:", "two words"], folder=folder, out=out) == 0
    text = Path(folder, "memory/notes.md").read_text(encoding="utf-8")
    assert text.startswith("# notes\n- ") and text.endswith(": pitfall: two words\n")
    assert out.getvalue().startswith("ok kd-notes - ")


@pytest.mark.parametrize("argv", [[], ["add"], ["edit", "x"], ["add", ""]])
def test_anything_but_add_with_text_is_refused(kd_notes, folder, argv):
    err = io.StringIO()
    assert kd_notes.main(argv, folder=folder, err=err) == 2
    assert Path(folder, "memory/notes.md").read_text(encoding="utf-8") == "# notes\n"
