"""kd.secret_fd — the secret leaves the environment for a pipe whose write end is closed."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kd import secret_fd

MODULE = Path(secret_fd.__file__).resolve()
pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="pipes passed across exec: POSIX only"
)


def _drain(fd):
    with os.fdopen(fd, "rb") as f:
        return f.read()


def test_supported_secrets_move_into_closed_pipes():
    env, fds = secret_fd.move(
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "tok-1",
            "ANTHROPIC_API_KEY": "key-2",
            "PATH": "/bin",
        }
    )
    assert set(env) == {
        "PATH",
        "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR",
    }
    oauth, api = (
        int(env["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"]),
        int(env["CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR"]),
    )
    assert sorted(fds) == sorted([oauth, api])
    # the write end is closed: the read ends at EOF, it never waits (a Read of the descriptor must not hang)
    assert (_drain(oauth), _drain(api)) == (b"tok-1", b"key-2")


def test_a_provider_bearer_key_goes_by_the_api_keys_descriptor():
    # the owner's decision (card #422): ANTHROPIC_AUTH_TOKEN has no descriptor of its own, the API key's carries it;
    # OpenRouter's explicitly empty ANTHROPIC_API_KEY goes with it
    env, fds = secret_fd.move(
        {
            "ANTHROPIC_AUTH_TOKEN": "k",
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_BASE_URL": "https://x.test",
        }
    )
    assert env == {
        "ANTHROPIC_BASE_URL": "https://x.test",
        "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR": str(fds[0]),
    }
    assert [_drain(fd) for fd in fds] == [b"k"]


def test_two_keys_for_one_descriptor_are_refused():
    with pytest.raises(ValueError, match="two secrets"):
        secret_fd.move({"ANTHROPIC_AUTH_TOKEN": "k", "ANTHROPIC_API_KEY": "a"})


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"ANTHROPIC_API_KEY": ""},
        {"CLAUDE_CODE_OAUTH_TOKEN": ""},
        {"ANTHROPIC_AUTH_TOKEN": "", "ANTHROPIC_API_KEY": ""},
    ],
    ids=repr,
)
def test_no_secret_no_descriptor(env):
    # AC 6: no token file — neither a variable nor an empty pipe; OpenRouter's empty API key stays empty
    assert secret_fd.move(env) == (env, [])


def test_an_inherited_descriptor_variable_is_dropped():
    env, fds = secret_fd.move(
        {
            "CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR": "0",
            "CLAUDE_CODE_API_KEY_FILE_DESCRIPTOR": "5",
        }
    )
    assert (env, fds) == ({}, [])


def test_an_oversized_secret_is_refused_and_leaks_no_descriptor():
    before = (
        set(os.listdir("/proc/self/fd")) if os.path.isdir("/proc/self/fd") else None
    )
    with pytest.raises(ValueError):
        secret_fd.move(
            {
                "CLAUDE_CODE_OAUTH_TOKEN": "t",
                "ANTHROPIC_API_KEY": "x" * (secret_fd.MAX_SECRET + 1),
            }
        )
    if before is not None:
        assert set(os.listdir("/proc/self/fd")) == before


CHILD = (
    "import json, os, sys\n"
    "fd = os.environ.get('CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR')\n"
    "raw = open('/proc/self/environ', 'rb').read() if os.path.exists('/proc/self/environ') else b''\n"
    "print(json.dumps({'env': sorted(k for k in os.environ if 'TOKEN' in k or 'KEY' in k),"
    " 'secret': os.read(int(fd), 100).decode() if fd else None, 'in_environ': b'tok-9' in raw}))\n"
)


def test_main_execs_the_program_with_the_descriptor_only():
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "CLAUDE_CODE_OAUTH_TOKEN": "tok-9",
    }
    done = subprocess.run(
        [sys.executable, "-I", str(MODULE), sys.executable, "-c", CHILD],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    seen = json.loads(done.stdout)
    assert seen == {
        "env": ["CLAUDE_CODE_OAUTH_TOKEN_FILE_DESCRIPTOR"],
        "secret": "tok-9",
        "in_environ": False,
    }


def test_main_without_a_program_or_with_a_missing_one():
    assert secret_fd.main([]) == 2
    done = subprocess.run(
        [sys.executable, "-I", str(MODULE), "/nonexistent/claude"],
        env={"PATH": "/bin"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 127 and "/nonexistent/claude" in done.stderr
