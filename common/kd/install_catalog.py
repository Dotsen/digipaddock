"""The install catalogs: pinned versions and the secrets the owner puts on a host.

common/install/versions.json pins what an install takes from outside the distribution (Claude Code, Node's major,
Hindsight and its torch, PostgreSQL and pgvector, the Hindsight client plugin); a host is checked against it.
common/install/secrets.json lists the files only the owner can fill (forge tokens, the optional Telegram copy of
critical alerts): path, owner, mode, the installer that checks it, the roles or components that want it, the file's
format and where its value comes from. Paths and owners carry naming tokens (@forge@, @host:<key>@) — secrets()
resolves them for this host. Generated files (the Hindsight env, VAPID keys) are not here: their installer makes them.

    python3 -I common/kd/install_catalog.py version <key>     -> the pinned value; 2 — unknown key
    python3 -I common/kd/install_catalog.py secrets           -> JSON list of this host's secrets
"""

import json
import re
import sys
from pathlib import Path
from typing import NamedTuple, Tuple

if __package__ in (None, ""):  # run as a script: python3 -I common/kd/install_catalog.py
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kd import naming  # noqa: E402

INSTALL = Path(__file__).resolve().parents[1] / "install"
VERSIONS = INSTALL / "versions.json"
SECRETS = INSTALL / "secrets.json"
VERSION_KEYS = ("claude_code", "node_major", "hindsight_api", "torch", "postgresql", "pgvector_min",
                "hindsight_plugin")
UNIT_RE = re.compile(r"[a-z0-9][a-z0-9@._-]*\.service")
_SECRET_KEYS = {"name", "path", "owner", "group", "mode", "needed_by", "check", "wanted_by", "format", "how"}
_OPTIONAL_KEYS = {"restart"}


class CatalogError(ValueError):
    pass


class Secret(NamedTuple):
    name: str
    path: str
    owner: str
    group: str
    mode: int
    needed_by: str  # the installer (or command) that checks the file, relative to the tree
    wanted_by: Tuple[str, ...]  # roles and components that need it; () — optional
    format: str  # the file's lines: KEY=<what>, for the owner
    how: str  # where the owner gets the value
    check: str = ""  # the arguments of needed_by's check (forge_setup.sh: check <agent> <user>); "" — none to run
    restart: Tuple[str, ...] = ()  # services that read the file only at start: kd-secret put try-restarts them


def _read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CatalogError("{}: unreadable ({})".format(path, exc.__class__.__name__))


def versions(path=None) -> dict:
    """The pinned versions: exactly VERSION_KEYS, each a non-empty string."""
    source = path or VERSIONS
    data = _read(source)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise CatalogError("{}: expected {{schema_version: 1, ...}}".format(source))
    data = {k: v for k, v in data.items() if k != "schema_version"}
    if set(data) != set(VERSION_KEYS) or not all(isinstance(v, str) and v for v in data.values()):
        raise CatalogError("{}: expected exactly {} as strings".format(source, ", ".join(VERSION_KEYS)))
    return data


def secrets(path=None, *, host=None, prefix=None, names=None) -> list:
    """This host's secrets in catalog order, tokens resolved; ``names`` picks some (an unknown one is an error)."""
    source = path or SECRETS
    data = _read(source)
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("secrets"), list):
        raise CatalogError("{}: expected {{schema_version: 1, secrets: [...]}}".format(source))
    out = []
    for item in data["secrets"]:
        if not isinstance(item, dict) or set(item) - _OPTIONAL_KEYS != _SECRET_KEYS:
            raise CatalogError("{}: a secret has exactly {} (and maybe {})".format(
                source, ", ".join(sorted(_SECRET_KEYS)), ", ".join(sorted(_OPTIONAL_KEYS))))
        restart = item.get("restart", [])
        if not isinstance(restart, list) or not all(isinstance(u, str) and UNIT_RE.fullmatch(u) for u in restart):
            raise CatalogError("{}: {}: restart is a list of <name>.service".format(source, item.get("name")))
        try:
            resolved = {k: naming.render(item[k], prefix=prefix, host=host)
                        for k in ("path", "owner", "group", "check", "how")}
            mode = int(item["mode"], 8)
        except (naming.NamingError, ValueError, TypeError) as exc:
            raise CatalogError("{}: {}: {}".format(source, item.get("name"), exc))
        out.append(Secret(item["name"], resolved["path"], resolved["owner"], resolved["group"], mode,
                          item["needed_by"], tuple(item["wanted_by"]), item["format"], resolved["how"],
                          resolved["check"], tuple(restart)))
    if names is not None:
        unknown = set(names) - {s.name for s in out}
        if unknown:
            raise CatalogError("no secret {}".format(", ".join(sorted(unknown))))
        out = [s for s in out if s.name in names]
    return out


def main(argv) -> int:
    try:
        if len(argv) == 2 and argv[0] == "version":
            pinned = versions()
            if argv[1] not in pinned:
                print("unknown version key {!r}".format(argv[1]), file=sys.stderr)
                return 2
            print(pinned[argv[1]])
            return 0
        if argv == ["secrets"]:
            print(json.dumps([s._asdict() for s in secrets()], ensure_ascii=False))
            return 0
    except (CatalogError, naming.NamingError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print("usage: install_catalog.py version <key> | secrets", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
