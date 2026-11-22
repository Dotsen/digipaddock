import os
import shutil
import subprocess

import pytest

from kd import shell_calls

NAMES = {"ssh-ro", "ssh-exec", "backlog", "kd-task", "kd-work"}
LITERAL_CASES = [
    ("ssh-ro node-c 'uptime'", [["ssh-ro", "node-c", "uptime"]]),
    ("ssh-ro node-e 'echo $(uptime)'", [["ssh-ro", "node-e", "echo $(uptime)"]]),
    (
        "ssh-exec --task KD-193 node-d,node-a 'case $(cat /etc/hostname) in a) T=1;; esac; printf \"%s\\n\" x'",
        [
            [
                "ssh-exec",
                "--task",
                "KD-193",
                "node-d,node-a",
                'case $(cat /etc/hostname) in a) T=1;; esac; printf "%s\\n" x',
            ]
        ],
    ),
    (
        "ssh-ro node-c 'cat /etc/passwd' | grep -c x > work/tmp/out 2>&1",
        [["ssh-ro", "node-c", "cat /etc/passwd"]],
    ),
    ("ssh-ro node-c uptime 2>/dev/null", [["ssh-ro", "node-c", "uptime"]]),
    ("ssh-ro node-c uptime >>log &>/dev/null", [["ssh-ro", "node-c", "uptime"]]),
    ('ssh-ro node-c uptime > "$OUT"x', [["ssh-ro", "node-c", "uptime"]]),
    (
        'backlog task edit KD-1 --comment "готово" --plain',
        [["backlog", "task", "edit", "KD-1", "--comment", "готово", "--plain"]],
    ),
    (
        "echo a; kd-task claim KD-5 && backlog task view KD-5 --plain",
        [["kd-task", "claim", "KD-5"], ["backlog", "task", "view", "KD-5", "--plain"]],
    ),
    (
        "kd-work <<'EOF'\nssh-ro node-c 'x'\nEOF\nssh-ro node-l uptime",
        [["kd-work"], ["ssh-ro", "node-l", "uptime"]],
    ),
    ("kd-work <<-EOF\n\tssh-ro node-c $X\n\tEOF\n", [["kd-work"]]),
    ("kd-work <<< 'text'", [["kd-work"]]),
    ("ssh-ro node-c uptime # ssh-ro node-l $x", [["ssh-ro", "node-c", "uptime"]]),
    ("ssh-ro node-c a#b", [["ssh-ro", "node-c", "a#b"]]),
    (
        'ssh-ro node-c "a\\nb" "c\\"d" \'e\\f\' g\\ h',
        [["ssh-ro", "node-c", "a\\nb", 'c"d', "e\\f", "g h"]],
    ),
    ("ssh-ro node-c '' \"\"", [["ssh-ro", "node-c", "", ""]]),
    ("if ssh-ro node-c 'test -f x'; then echo y; fi", [["ssh-ro", "node-c", "test -f x"]]),
    ("{ ssh-ro node-c uptime; }", [["ssh-ro", "node-c", "uptime"]]),
    (
        "ssh-ro node-c echo } {",
        [["ssh-ro", "node-c", "echo", "}", "{"]],
    ),  # a lone brace off command position is a word
    ("(ssh-ro node-c uptime)", [["ssh-ro", "node-c", "uptime"]]),
    ("grep ssh-ro notes.md", []),
    ("/usr/local/bin/ssh-ro node-c uptime", []),
    ("timeout 5 ssh-ro node-c uptime", []),
]


@pytest.mark.parametrize("command, expected", LITERAL_CASES)
def test_literal_calls(command, expected):
    assert shell_calls.calls(command, NAMES) == expected


STUB = "{name}() {{ printf '%s\\0' {name} \"$@\" >&3; printf '\\1' >&3; }}"


@pytest.mark.skipif(os.name != "posix" or not shutil.which("bash"), reason="needs bash")
@pytest.mark.parametrize(
    "command, expected", [c for c in LITERAL_CASES if not c[0].startswith("/")]
)
def test_words_match_what_bash_passes(command, expected, tmp_path):
    """The same text run by bash with stub functions: the stubs see exactly the parsed argv."""
    (tmp_path / "work" / "tmp").mkdir(parents=True)
    stubs = "\n".join(STUB.format(name=name) for name in sorted(NAMES))
    run = subprocess.run(
        ["bash", "-c", stubs + "\nexec 3>&1 1>/dev/null 2>&1\n" + command + "\n"],
        capture_output=True,
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin"},
    )
    seen = [
        [w.decode() for w in chunk.split(b"\0")[:-1]]
        for chunk in run.stdout.split(b"\1")[:-1]
    ]
    assert seen == expected == shell_calls.calls(command, NAMES)


@pytest.mark.parametrize(
    "command",
    [
        'ssh-exec --task KD-1 node-e "$(hostname)"',
        "ssh-ro $HOST 'uptime'",
        "ssh-ro node-c `cat x`",
        "ssh-ro node-c /etc/*.conf",
        "ssh-ro node-c ~/x",
        "ssh-ro node-c --opt=~/x",
        "ssh-ro {node-c,node-l} uptime",
        "ssh-ro node-c \\\n'uptime'",
        "ssh-ro node-c $'a\\tb'",
        "ssh-ro node-c <(echo x)",
        "for s in a b; do ssh-ro $s uptime; done",
    ],
)
def test_not_literal(command):
    with pytest.raises(shell_calls.NotLiteral) as info:
        shell_calls.calls(command, NAMES)
    assert info.value.command in NAMES


def test_expansion_outside_channel_calls_is_fine():
    assert shell_calls.calls(
        "X=$(date); echo $X | grep *; ssh-ro node-c uptime", NAMES
    ) == [["ssh-ro", "node-c", "uptime"]]


def test_unclosed_quote_is_not_literal():
    with pytest.raises(shell_calls.NotLiteral):
        shell_calls.calls("ssh-ro node-c 'uptime", NAMES)


@pytest.mark.parametrize(
    "command, compound",
    [
        ("cd work && cat file", True),
        ("cd /srv; ls", True),
        ("cd work || true", True),
        ("cd work\nls", True),
        ("cd work", False),
        ("cd", False),
        ("cat /srv/digipaddock-agents/sysadmin/work/file", False),
        ("echo 'cd x && y'", False),
        ("git -C repo log && cd x", True),
        ("echo cd && ls", False),
    ],
)
def test_cd_compound(command, compound):
    assert shell_calls.cd_compound(command) is compound


@pytest.mark.parametrize(
    "command, unsafe",
    [
        ("ssh-exec node-c 'uptime'", False),
        ("ssh-exec node-c,node-l 'echo $(uptime) > /tmp/x'", False),
        ("ssh-exec node-c uptime > out", True),
        ("ssh-exec node-c -- uptime", True),
        ('ssh-exec node-c "$X"', True),
        ("/usr/local/bin/ssh-exec node-c $X", True),
        ("ssh-ro node-c $X", False),
        ("ssh-exec node-c uptime <<EOF\nx\nEOF", True),
    ],
)
def test_unsafe_call_of(command, unsafe):
    assert shell_calls.unsafe_call_of(command, "ssh-exec") is unsafe


def test_a_diagram_in_a_single_quoted_argument_is_literal():
    # the analyst passes a brief with a mermaid block in --description; nothing in it is an expansion
    block = "```mermaid\nflowchart TD\n  A[Запрос] --> B{Тип?}\n  B -- api --> C[(БД)]\n```"
    text = "Подход\n---\nтекст\n\n" + block
    command = "kd-task edit KD-1 --revision 3 --description '" + text + "'"
    assert shell_calls.calls(command, {"kd-task"}) == [["kd-task", "edit", "KD-1", "--revision", "3", "--description", text]]
