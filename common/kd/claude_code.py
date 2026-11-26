"""The one Claude Code every agent session, worker job and model probe runs (KD-9474).

That binary receives the host's Claude token or a provider's key on a descriptor (``kd.secret_fd``), so it is
root's and outside every agent's home: ``BINARY``, root:root 0755 in the root-owned ``DIR``. A copy in the home
(the vendor's installer writes ``~/.local/bin/claude`` and ``~/.local/share/claude`` as the user) would be the
agent's to replace, and one escape from its sandbox would turn into the token that runs every agent.

``common/install/claude_setup.sh install`` puts there the vendor's release binary of the version pinned in
``common/install/versions.json`` (``claude_code``), checked against the sha256 pinned there for the machine's
architecture before it replaces the old one; root never runs the vendor's install script. Every launch execs
``BINARY`` by its absolute path with ``PATH`` below (no home directory in it), ``kd.secret_fd`` refuses to hand a
secret to a program another user could change, and the vendor's auto-update stays off (``DISABLE_AUTOUPDATER=1``):
it would only write into the home, which nothing runs. ``problems`` is the installer's verify of the same file.
"""

from __future__ import annotations

import hashlib
import os
import platform
from typing import Callable, Mapping, Optional

from kd import install_catalog, secret_fd

DIR = "/opt/digipaddock-claude"
BINARY = f"{DIR}/claude"
# The PATH of a launch: the binary's directory first, then the system's; never a directory of the agent's home.
PATH = f"{DIR}:/usr/local/bin:/usr/bin:/bin"
# uname -m -> the architecture of the sha256 key in versions.json (claude_code_sha256_<arch>)
ARCHES = {"x86_64": "amd64", "aarch64": "arm64"}
HINT = "claude_setup.sh install"
_CHUNK = 1024 * 1024
# one process verifies every role against the same ~250 MB file (kd.install_apply.outdated_agents): its digest
# is read once per version of the file, known by its inode, size and change times
_SEEN: dict[tuple, str] = {}


def _read_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256(path: str) -> str:
    info = os.stat(path)
    key = (
        path,
        info.st_dev,
        info.st_ino,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )
    if key not in _SEEN:
        _SEEN[key] = _read_sha256(path)
    return _SEEN[key]


def problem(
    path: str = BINARY, *, lstat: Callable[[str], os.stat_result] = os.lstat
) -> Optional[str]:
    """Why a launch may not exec ``path`` with a secret, or None (``kd.secret_fd.root_program``)."""
    why = secret_fd.root_program(path, lstat=lstat)
    return f"{why}; {HINT}" if why else None


def problems(
    path: str = BINARY,
    *,
    lstat: Callable[[str], os.stat_result] = os.lstat,
    digest: Callable[[str], str] = sha256,
    machine: Optional[str] = None,
    pinned: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """The installer's verify: the binary every launch execs is root's (the whole path) and the pinned build of
    this architecture. Empty when it is; the file is read only once its path is root's."""
    machine = machine or platform.machine()
    arch = ARCHES.get(machine)
    if arch is None:
        return [f"no pinned Claude Code build for {machine}"]
    why = problem(path, lstat=lstat)
    if why:
        return [why]
    pinned = pinned or install_catalog.versions()
    if digest(path) != pinned[f"claude_code_sha256_{arch}"]:
        return [
            f"{path} is not Claude Code {pinned['claude_code']} of common/install/versions.json (sha256); {HINT}"
        ]
    return []
