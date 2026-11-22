"""The seccomp filter of kd-e2e's namespace (review 04.11.2026): what bubblewrap loads with ``--seccomp FD``.

Bubblewrap applies no syscall filter of its own; only Chromium's sandbox has one. The code under test — the cabinet,
node and its packages, the browser — needs none of the kernel's administrative and rarely used interfaces, which are
where kernel bugs reachable from an unprivileged user namespace keep turning up. This filter answers EPERM to them and
allows the rest:

- any call of another ABI than the host's (i386 through ``int 0x80`` on x86_64, x32) — the numbers below would not
  mean the same there;
- the kernel keyring (``add_key``, ``keyctl``, ``request_key``), ``bpf``, ``perf_event_open``, ``userfaultfd`` and
  ``io_uring_*`` (libuv falls back to its thread pool);
- modules, kexec, reboot, swap, accounting, quotas, the kernel log, clock setting, ``fanotify_init``, file handles
  (``open_by_handle_at`` — the classic container escape), ``vhangup`` and the legacy x86 calls.

Namespaces, mounts and chroot stay allowed: bubblewrap has set the namespace up already, and Chromium builds its own
sandbox from them. A host of another architecture has no table here, and kd-e2e reports the runtime unavailable
rather than running unfiltered.

The program is classic BPF as the kernel's ``struct sock_filter`` array, in native byte order.
"""

from __future__ import annotations

import platform
import struct

# struct seccomp_data: int nr; __u32 arch; …
_NR, _ARCH = 0, 4
_LD_W_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_JEQ_K = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_JGE_K = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_RET_K = 0x06  # BPF_RET | BPF_K
RET_ALLOW = 0x7FFF0000
EPERM = 1
RET_EPERM = 0x00050000 | EPERM  # SECCOMP_RET_ERRNO
X32_BIT = 0x40000000

DENIED = (
    "acct",
    "add_key",
    "adjtimex",
    "bpf",
    "clock_adjtime",
    "clock_settime",
    "delete_module",
    "fanotify_init",
    "finit_module",
    "init_module",
    "io_uring_enter",
    "io_uring_register",
    "io_uring_setup",
    "ioperm",
    "iopl",
    "kexec_file_load",
    "kexec_load",
    "keyctl",
    "lookup_dcookie",
    "name_to_handle_at",
    "nfsservctl",
    "open_by_handle_at",
    "perf_event_open",
    "quotactl",
    "quotactl_fd",
    "reboot",
    "request_key",
    "settimeofday",
    "swapoff",
    "swapon",
    "_sysctl",
    "sysfs",
    "syslog",
    "userfaultfd",
    "uselib",
    "ustat",
    "vhangup",
)

# The syscall numbers of each architecture (asm/unistd_64.h; aarch64 uses asm-generic/unistd.h). A name an
# architecture does not have is absent from its table. test_e2e_seccomp checks them against the headers.
TABLES = {
    "x86_64": {
        "arch": 0xC000003E,  # AUDIT_ARCH_X86_64
        "x32": True,
        "numbers": {
            "acct": 163,
            "add_key": 248,
            "adjtimex": 159,
            "bpf": 321,
            "clock_adjtime": 305,
            "clock_settime": 227,
            "delete_module": 176,
            "fanotify_init": 300,
            "finit_module": 313,
            "init_module": 175,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "io_uring_setup": 425,
            "ioperm": 173,
            "iopl": 172,
            "kexec_file_load": 320,
            "kexec_load": 246,
            "keyctl": 250,
            "lookup_dcookie": 212,
            "name_to_handle_at": 303,
            "nfsservctl": 180,
            "open_by_handle_at": 304,
            "perf_event_open": 298,
            "quotactl": 179,
            "quotactl_fd": 443,
            "reboot": 169,
            "request_key": 249,
            "settimeofday": 164,
            "swapoff": 168,
            "swapon": 167,
            "_sysctl": 156,
            "sysfs": 139,
            "syslog": 103,
            "userfaultfd": 323,
            "uselib": 134,
            "ustat": 136,
            "vhangup": 153,
        },
    },
    "aarch64": {
        "arch": 0xC00000B7,  # AUDIT_ARCH_AARCH64
        "x32": False,
        "numbers": {
            "acct": 89,
            "add_key": 217,
            "adjtimex": 171,
            "bpf": 280,
            "clock_adjtime": 266,
            "clock_settime": 112,
            "delete_module": 106,
            "fanotify_init": 262,
            "finit_module": 273,
            "init_module": 105,
            "io_uring_enter": 426,
            "io_uring_register": 427,
            "io_uring_setup": 425,
            "kexec_file_load": 294,
            "kexec_load": 104,
            "keyctl": 219,
            "lookup_dcookie": 18,
            "name_to_handle_at": 264,
            "nfsservctl": 42,
            "open_by_handle_at": 265,
            "perf_event_open": 241,
            "quotactl": 60,
            "quotactl_fd": 443,
            "reboot": 142,
            "request_key": 218,
            "settimeofday": 170,
            "swapoff": 225,
            "swapon": 224,
            "syslog": 116,
            "userfaultfd": 282,
            "vhangup": 58,
        },
    },
}
_INSN = struct.Struct("=HBBI")  # struct sock_filter: code, jt, jf, k


class NoFilter(Exception):
    """This host's architecture has no table: kd-e2e does not run without the filter."""


def machine() -> str:
    name = platform.machine()
    return {"amd64": "x86_64", "arm64": "aarch64"}.get(name.lower(), name)


def program(arch: str | None = None) -> bytes:
    """The filter for ``arch`` (this host's by default) as bytes for ``bwrap --seccomp``."""
    arch = arch or machine()
    table = TABLES.get(arch)
    if table is None:
        raise NoFilter(f"no seccomp filter for {arch}")
    numbers = sorted(
        table["numbers"][name] for name in DENIED if name in table["numbers"]
    )
    insns = [
        (_LD_W_ABS, 0, 0, _ARCH),
        (_JEQ_K, 1, 0, table["arch"]),
        (_RET_K, 0, 0, RET_EPERM),
        (_LD_W_ABS, 0, 0, _NR),
    ]
    checks = ([X32_BIT] if table["x32"] else []) + numbers
    # each check jumps to the EPERM return after the ALLOW one: from check i of n, n - i - 1 checks and ALLOW lie between
    for i, value in enumerate(checks):
        insns.append(
            (_JGE_K if table["x32"] and i == 0 else _JEQ_K, len(checks) - i, 0, value)
        )
    insns += [(_RET_K, 0, 0, RET_ALLOW), (_RET_K, 0, 0, RET_EPERM)]
    return b"".join(_INSN.pack(*insn) for insn in insns)
