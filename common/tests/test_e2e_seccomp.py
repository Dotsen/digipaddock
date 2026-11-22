"""kd-e2e's seccomp filter (review 04.11.2026): the numbers, the program's jumps, and a run under bubblewrap."""

import os
import re
import shutil
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from kd import e2e, e2e_seccomp

HEADERS = {
    "x86_64": Path("/usr/include/x86_64-linux-gnu/asm/unistd_64.h"),
    "aarch64": Path("/usr/include/asm-generic/unistd.h"),
}


def run_filter(program: bytes, arch: int, nr: int) -> int:
    """The kernel's verdict for one call: a classic BPF interpreter of the opcodes the filter uses."""
    insns = [
        struct.unpack("=HBBI", program[i : i + 8]) for i in range(0, len(program), 8)
    ]
    data = struct.pack("=iI", nr, arch)
    pc, acc = 0, 0
    while True:
        code, jt, jf, k = insns[pc]
        if code == 0x20:
            acc = struct.unpack_from("=I", data, k)[0]
            pc += 1
        elif code == 0x15:
            pc += 1 + (jt if acc == k else jf)
        elif code == 0x35:
            pc += 1 + (jt if acc >= k else jf)
        elif code == 0x06:
            return k
        else:
            raise AssertionError(f"unexpected opcode {code:#x}")


@pytest.mark.parametrize("arch", sorted(e2e_seccomp.TABLES))
def test_the_numbers_are_the_kernels(arch):
    header = HEADERS[arch]
    if not header.is_file():
        pytest.skip(f"no {header}")
    defined = dict(
        re.findall(r"^#define __NR_(\w+)\s+(\d+)\s*$", header.read_text(), re.M)
    )
    table = e2e_seccomp.TABLES[arch]["numbers"]
    assert set(table) <= set(e2e_seccomp.DENIED)
    for name in e2e_seccomp.DENIED:
        if name in defined:
            assert table.get(name) == int(defined[name]), name
        else:
            assert name not in table, name


@pytest.mark.parametrize("arch", sorted(e2e_seccomp.TABLES))
def test_denied_calls_get_eperm_and_the_rest_pass(arch):
    table = e2e_seccomp.TABLES[arch]
    program = e2e_seccomp.program(arch)
    for name, nr in table["numbers"].items():
        assert run_filter(program, table["arch"], nr) == e2e_seccomp.RET_EPERM, name
    denied = set(table["numbers"].values())
    for nr in range(0, 460):
        if nr not in denied:
            assert run_filter(program, table["arch"], nr) == e2e_seccomp.RET_ALLOW, nr
    # another ABI (i386 through int 0x80 on x86_64, or anything foreign) — none of these numbers mean the same there
    assert run_filter(program, 0x40000003, 39) == e2e_seccomp.RET_EPERM
    if table["x32"]:
        assert (
            run_filter(program, table["arch"], e2e_seccomp.X32_BIT | 39)
            == e2e_seccomp.RET_EPERM
        )


def test_a_host_without_a_table_runs_no_suite():
    with pytest.raises(e2e_seccomp.NoFilter):
        e2e_seccomp.program("riscv64")


def _bwrap_works():
    if (
        not shutil.which("bwrap")
        or sys.platform != "linux"
        or e2e_seccomp.machine() not in e2e_seccomp.TABLES
    ):
        return False
    p = subprocess.run(
        [
            "bwrap",
            "--unshare-all",
            "--ro-bind",
            "/",
            "/",
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "true",
        ],
        capture_output=True,
    )
    return p.returncode == 0


PROBE = """
import ctypes, os
libc = ctypes.CDLL(None, use_errno=True)
def call(nr, *a):
    r = libc.syscall(ctypes.c_long(nr), *[ctypes.c_long(x) for x in a])
    return "ok" if r >= 0 else os.strerror(ctypes.get_errno())
n = {numbers}
print(call(n["keyctl"], 0, 0), call(n["bpf"], 0, 0, 0), call(n["io_uring_setup"], 1, 0), os.getpid() > 0)
"""


@pytest.mark.skipif(not _bwrap_works(), reason="needs a working bubblewrap")
def test_under_bubblewrap_the_kernel_never_sees_a_denied_call(tmp_path):
    numbers = e2e_seccomp.TABLES[e2e_seccomp.machine()]["numbers"]
    probe = PROBE.format(numbers=dict(numbers))
    python = os.path.realpath(sys.executable)
    read, write = os.pipe()
    os.write(write, e2e_seccomp.program())
    os.close(write)
    try:
        p = subprocess.run(
            [
                "bwrap",
                "--unshare-all",
                "--die-with-parent",
                "--seccomp",
                str(read),
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev",
                "/dev",
                "--tmpfs",
                "/tmp",
                python,
                "-I",
                "-c",
                probe,
            ],
            pass_fds=(read,),
            capture_output=True,
            encoding="utf-8",
        )
    finally:
        os.close(read)
    eperm = os.strerror(1)
    assert p.returncode == 0, p.stderr
    assert p.stdout.strip() == " ".join([eperm] * 3 + ["True"]), p.stdout


def test_the_suite_starts_with_the_filter_on_a_descriptor_bwrap_reads(
    tmp_path, monkeypatch
):
    from test_e2e_executor import _fake_bwrap, prepared

    root = prepared(tmp_path)
    _fake_bwrap(monkeypatch)
    seen = []

    class Process:
        def wait(self, timeout):
            return 0

    def popen(argv, pass_fds=(), **kwargs):
        fd = int(argv[argv.index("--seccomp") + 1])
        assert pass_fds == (fd,)
        seen.append(os.read(fd, 1 << 16))
        return Process()

    result = e2e.run_suite(
        "cabinet-smoke",
        runtime=root,
        runs=tmp_path / "runs",
        popen=popen,
        require_root_owner=False,
    )
    assert result["state"] == "passed" and seen == [e2e_seccomp.program()]
    monkeypatch.setattr(e2e_seccomp, "machine", lambda: "riscv64")
    with pytest.raises(e2e.Unavailable, match="no seccomp filter"):
        e2e.run_suite(
            "cabinet-smoke",
            runtime=root,
            runs=tmp_path / "runs",
            popen=popen,
            require_root_owner=False,
        )
