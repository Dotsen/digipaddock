"""the files a job of the cabinet reads instead of the board. A council's critic is a worker job — no
channel, no network — so what it reads travels with the job and lands read-only in its slot's ``work/inputs``."""

import pytest

from kd import worker_inputs


def test_inputs_are_named_markdown_files_of_text():
    files = {"brief.md": "# KD-5\n\nthe brief\n", "thread.md": "#1 @owner: go\n"}

    assert worker_inputs.check(files) == files
    assert worker_inputs.DIR == "inputs"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        [],
        {},
        {"brief.txt": "x"},
        {"../brief.md": "x"},
        {"Brief.md": "x"},
        {".md": "x"},
        {"a/b.md": "x"},
        {"brief.md": ""},
        {"brief.md": "  \n"},
        {"brief.md": 1},
        {"brief.md": "a\x00b"},
        {"brief.md": "\ud800"},
        {1: "x"},
        {f"f{n}.md": "x" for n in range(worker_inputs.FILES + 1)},
        {"brief.md": "x" * (worker_inputs.MAX + 1)},
    ],
)
def test_other_inputs_are_refused(bad):
    with pytest.raises(ValueError):
        worker_inputs.check(bad)


def test_the_limit_counts_the_bytes_on_the_wire():
    # a Cyrillic letter is two bytes of UTF-8: half the limit in letters is already over it with the file's name
    half = worker_inputs.MAX // 2
    assert worker_inputs.check({"brief.md": "я" * (half - 64)})
    with pytest.raises(ValueError, match="bytes"):
        worker_inputs.check({"brief.md": "я" * half})


def test_size_is_what_check_measures():
    files = {"brief.md": 'a "quoted" line\n' * 10}
    # the quotes and the line ends are escaped
    assert worker_inputs.size(files) > len(files["brief.md"])
    assert worker_inputs.size({"a.md": "я"}) == len('{"a.md": "я"}'.encode("utf-8"))
