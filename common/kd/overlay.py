"""Deployment overlay: the deployment's files laid over the code tree of a release.

The code repository carries no deployment values; the overlay repository carries host/host.json,
the inventory and the other files of ALLOWED, and nothing else of it reaches a host. A release is
one tar: the code archive, then the overlay members whose paths are in ALLOWED (a same-named code
file is replaced), then DEPLOY_REVISION. host/overlay.json declares the overlay schema the code
must support; check_overlay validates the overlay alone, check_tree the composed tree.

kd-release runs the composition with the code being released (its schema range and allowlist decide), in its
sandbox, after its own installed tree checked the overlay as data (data_errors):
    python3 -I -c '…overlay.main…' release <code.tar> <overlay.tar> <deploy_rev> <out.tar | ->
exit 0 — out.tar written (``-``: the tar on stdout); 3 — overlay_invalid: …; 4 — overlay_incompatible: … (one line
on stdout). An overlay carries data only: no executable file, no script, nothing a release runs.
An overlay's own check and a new overlay, from a code tree:
    python3 -I common/kd/overlay.py check <overlay dir>   exit 0 "overlay: ok", 3 — one error per line
    python3 -I common/kd/overlay.py init <dir>            host/*.example.json -> <dir>/host/*.json, kept if present
host/install.json and host/projects.json (kd.install_spec), when the overlay has them, are checked against the
roles of the code tree that runs the check; check also names the files a release needs that neither the overlay nor
that code tree carries (release_needs: the forge's known hosts, the closed scanner's list, the sysadmin's).
"""

import io
import json
import os
import re
import sys
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

if __package__ in (None, ""):  # run as a script: python3 -I common/kd/overlay.py check|init
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from kd import egress, install_spec, naming  # noqa: E402
from kd.privacy_scan import SECRET_PATTERNS  # noqa: E402

# The overlay is private: a public key (the forge's known_hosts) is its data; only real secrets are refused.
OVERLAY_SECRETS = tuple((kind, pattern) for kind, pattern in SECRET_PATTERNS if kind != "ssh-pubkey")

OVERLAY_SCHEMA_MIN = 1
OVERLAY_SCHEMA_MAX = 1
# A path ending in "/" admits everything under it.
ALLOWED = (
    "host/",
    "agents/sysadmin/inventory.json",
    "agents/sysadmin/monitor/config.json",
    "agents/sysadmin/operations.json",
    "agents/sysadmin/playbooks/security.md",
    "agents/sysadmin/servers.md",
    "common/install/forgejo_known_hosts",
)
# Only data of an overlay reaches a host: a file of an allowed path is JSON, Markdown or one of the named
# data files, text, with no executable bit and no "#!" line. Code — the closed scanner too — is the code tree's.
DATA_SUFFIXES = (".json", ".md")
DATA_FILES = ("host/allowed_signers", "common/install/forgejo_known_hosts")
_PROBE = 8192
_REV = re.compile(r"[0-9a-f]{7,40}\Z")


class OverlayError(ValueError):
    pass


def allowed(path):
    for entry in ALLOWED:
        if path == entry or (entry.endswith("/") and path.startswith(entry) and len(path) > len(entry)):
            return True
    return False


def _strip(name):
    while name.startswith("./"):
        name = name[2:]
    return name


def _name(member):
    name = _strip(member.name)
    parts = PurePosixPath(name).parts
    if not name or name.startswith("/") or ".." in parts:
        raise OverlayError("overlay: unsafe path {!r}".format(member.name))
    return name


def data_error(rel, mode, head):
    """Why the allowed overlay file ``rel`` (its mode, its first bytes) is not plain data; None when it is."""
    if mode & 0o111:
        return "{}: executable — an overlay carries data only".format(rel)
    if not (rel.endswith(DATA_SUFFIXES) or rel in DATA_FILES):
        return "{}: not a data file (.json, .md, {})".format(rel, ", ".join(DATA_FILES))
    if head.startswith(b"#!") or not _is_text(head):
        return "{}: not text data".format(rel)
    return None


def data_errors(overlay_tar):
    """Errors of an overlay archive as data, read without unpacking or running anything of it: paths,
    member types, the allowed files' modes and content, the shape of host/overlay.json (its schema range is the
    released code's). kd-release runs it from the installed tree before any scanner."""
    errors, meta = [], None
    with tarfile.open(fileobj=io.BytesIO(overlay_tar)) as src:
        for member in src.getmembers():
            name = _name(member)
            if member.isdir():
                continue
            if not member.isfile():
                errors.append("{}: not a regular file".format(name))
                continue
            if not allowed(name):
                continue  # never reaches a host and nothing runs it
            data = src.extractfile(member).read()
            error = data_error(name, member.mode, data[:_PROBE])
            if error:
                errors.append(error)
            if name == "host/overlay.json":
                meta = data
    try:
        version = json.loads(meta.decode("utf-8")).get("schema_version") if meta is not None else None
    except (ValueError, AttributeError):
        version = None
    if type(version) is not int:
        errors.append("host/overlay.json: missing or without an integer schema_version")
    return errors


def compose(code_tar, overlay_tar, deploy_rev):
    """One release tar: code members, then the allowed overlay members, then DEPLOY_REVISION."""
    if not isinstance(deploy_rev, str) or not _REV.match(deploy_rev):
        raise OverlayError("overlay: bad deploy revision {!r}".format(deploy_rev))
    picked = []
    with tarfile.open(fileobj=io.BytesIO(overlay_tar)) as src:
        for member in src.getmembers():
            name = _name(member)
            if member.isdir():
                continue
            if not member.isfile():
                raise OverlayError("overlay: {} is not a regular file".format(name))
            if allowed(name):
                picked.append((name, member, src.extractfile(member).read()))
    replaced = {name for name, _, _ in picked} | {"DEPLOY_REVISION"}
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as dst:
        with tarfile.open(fileobj=io.BytesIO(code_tar)) as code:
            for member in code.getmembers():
                if _strip(member.name) in replaced and not member.isdir():
                    continue
                dst.addfile(member, code.extractfile(member) if member.isfile() else None)
        for name, member, data in picked:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), member.mode & 0o644, member.mtime  # data: never executable
            dst.addfile(info, io.BytesIO(data))
        data = (deploy_rev + "\n").encode("ascii")
        info = tarfile.TarInfo("DEPLOY_REVISION")
        info.size, info.mode = len(data), 0o644
        dst.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _files(tree):
    root = Path(tree)
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root).as_posix()
        if rel == ".git" or rel.startswith(".git/") or not path.is_file():
            continue
        yield rel, path


def _is_text(data):
    return b"\0" not in data[:8192]


def _host_errors(root):
    try:
        naming.load_host(str(Path(root) / "host" / "host.json"))
    except naming.NamingError as exc:
        return ["host/host.json: {}".format(exc)]
    return []


def _install_errors(root):
    """install.json and projects.json, when present: valid, roles of this code tree, projects routed to them."""
    errors, roles = [], None
    install, projects = root / "host" / "install.json", root / "host" / "projects.json"
    if install.is_file():
        try:
            roles = install_spec.load_install(install, known_roles=install_spec.known_roles()).roles
        except install_spec.InstallSpecError as exc:
            errors.append("host/install.json: {}".format(str(exc).split(": ", 1)[-1]))
    if projects.is_file():
        try:
            install_spec.load_projects(projects, roles=roles)
        except install_spec.InstallSpecError as exc:
            errors.append("host/projects.json: {}".format(str(exc).split(": ", 1)[-1]))
    return errors


def _egress_errors(root):
    """host/egress.json, when present: valid, and every agent an exit lists holds net.egress in this code tree
    (the cabinet grants and kd.launch refuses otherwise: every card of the project would fail to start)."""
    path = root / "host" / "egress.json"
    if not path.is_file():
        return []
    try:
        config = egress.load(str(path))
    except egress.EgressError as exc:
        return ["host/egress.json: {}".format(str(exc).split(": ", 1)[-1])]
    errors = []
    for name, entry in sorted(config["exits"].items()):
        for agent in entry["agents"]:
            try:
                manifest = json.loads((CODE / "agents" / agent / "agent.json").read_text("utf-8"))
                held = "net.egress" in manifest.get("capabilities", [])
            except (OSError, ValueError, AttributeError):
                held = False
            if not held:
                errors.append("host/egress.json: exit {} lists {}, whose manifest has no net.egress".format(
                    name, agent))
    return errors


def check_overlay(tree):
    """Errors of an overlay tree on its own; an empty list is a valid overlay."""
    root = Path(tree)
    errors = []
    try:
        meta = json.loads((root / "host" / "overlay.json").read_text("utf-8"))
        version = meta.get("schema_version") if isinstance(meta, dict) else None
        if type(version) is not int or not OVERLAY_SCHEMA_MIN <= version <= OVERLAY_SCHEMA_MAX:
            errors.append("host/overlay.json: schema_version {!r} not in {}..{}".format(
                version, OVERLAY_SCHEMA_MIN, OVERLAY_SCHEMA_MAX))
    except (OSError, ValueError) as exc:
        errors.append("host/overlay.json: unreadable ({})".format(exc.__class__.__name__))
    errors += _host_errors(root)
    errors += _install_errors(root)
    errors += _egress_errors(root)
    for rel, path in _files(root):
        if not allowed(rel):
            continue  # never reaches a host (foreign() lists it): internal docs must not refuse a release
        data = path.read_bytes()
        error = data_error(rel, path.stat().st_mode if os.name == "posix" else 0, data[:_PROBE])
        if error:
            errors.append(error)
        if rel.endswith(".json"):
            try:
                json.loads(data.decode("utf-8"))
            except ValueError as exc:
                errors.append("{}: invalid JSON ({})".format(rel, exc.__class__.__name__))
        if not _is_text(data):
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            for kind, pattern in OVERLAY_SECRETS:
                if pattern.search(line):
                    errors.append("{}:{}: {}".format(rel, number, kind))
    return errors


CODE = Path(__file__).resolve().parents[2]


def release_needs(tree):
    """What kd-release needs of this overlay beyond its own validity (``check``, before a push): every file a
    composed tree requires that this code tree does not carry, and the closed scanner's list."""
    root = Path(tree)
    needed = [path for path in ALLOWED if not path.endswith("/") and not (CODE / path).is_file()]
    said = ["missing: {} (a release needs it)".format(path) for path in needed + ["host/private_scan.json"]
            if not (root / path).is_file()]
    return said + _example_literals(root / "host" / "private_scan.json")


def _example_literals(listing):
    """The example's literals still in the deployment's list: the code tree's own example list names them, so the
    closed scanner refuses every release (release: privacy) — the list is the deployment's names."""
    try:
        example = json.loads((EXAMPLES / "private_scan.example.json").read_text("utf-8"))["literals"]
        literals = json.loads(listing.read_text("utf-8")).get("literals") or []
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return []  # a missing or broken list is said elsewhere
    left = sorted({x for x in literals if isinstance(x, str)} & set(example))
    return ["host/private_scan.json: {} — literal of the example, a release refuses it: list this deployment's "
            "names and addresses".format(", ".join(left))] if left else []


def foreign(tree):
    """Overlay paths outside ALLOWED: they never reach a host (information, not an error)."""
    return [rel for rel, _ in _files(tree) if not allowed(rel)]


def check_tree(tree):
    """Errors of a composed release tree: valid host.json and every required overlay file present."""
    root = Path(tree)
    errors = _host_errors(root)
    for path in ALLOWED:
        if not path.endswith("/") and not (root / path).is_file():
            errors.append("missing: {}".format(path))
    return errors


def unpack(data, dest):
    """Extract a tar of regular files and directories into ``dest``; anything else, or an unsafe path, refuses."""
    root = Path(dest)
    root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data)) as src:
        for member in src.getmembers():
            name = _name(member)
            target = root.joinpath(*PurePosixPath(name).parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(src.extractfile(member).read())
            else:
                raise OverlayError("overlay: {} is not a regular file".format(name))


def release(code_tar, overlay_tar, deploy_rev):
    """The release tar of code + overlay; OverlayError("overlay_invalid: …" | "overlay_incompatible: …")."""
    with tempfile.TemporaryDirectory(prefix="kd-overlay.") as tmp:
        try:
            errors = data_errors(overlay_tar)
            if errors:
                raise OverlayError("; ".join(errors[:3]))
            unpack(overlay_tar, os.path.join(tmp, "overlay"))
            errors = check_overlay(os.path.join(tmp, "overlay"))
            if errors:
                raise OverlayError("; ".join(errors[:3]))
            tar = compose(code_tar, overlay_tar, deploy_rev)
        except OverlayError as exc:
            raise OverlayError("overlay_invalid: {}".format(exc)) from None
        unpack(tar, os.path.join(tmp, "tree"))
        errors = check_tree(os.path.join(tmp, "tree"))
        if errors:
            raise OverlayError("overlay_incompatible: {}".format("; ".join(errors[:3])))
    return tar


EXAMPLES = Path(__file__).resolve().parents[2] / "host"


def _examples():
    """(overlay path, its example in this code tree): host/*.example.json and every ALLOWED file with one."""
    for example in sorted(EXAMPLES.glob("*.example.json")):
        yield "host/" + example.name.replace(".example.json", ".json"), example
    for rel in ALLOWED:
        suffix = PurePosixPath(rel).suffix
        example = CODE / (rel[:-len(suffix)] + ".example" + suffix) if suffix else None
        if example is not None and example.is_file():
            yield rel, example


def init(dest):
    """Every example of this code tree -> <dest> under the overlay's own name (host/*.example.json -> host/*.json,
    the sysadmin's files); an existing file is kept. (created, kept)."""
    made, kept = [], []
    for rel, example in _examples():
        target = Path(dest).joinpath(*PurePosixPath(rel).parts)
        if target.exists() or target.is_symlink():
            kept.append(rel)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(example.read_bytes())
        made.append(rel)
    return made, kept


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) == 2 and args[0] == "check":
        errors = check_overlay(args[1]) + release_needs(args[1])
        for line in errors:
            print(line)
        if not errors:
            print("overlay: ok")
        return 3 if errors else 0
    if len(args) == 2 and args[0] == "init":
        made, kept = init(args[1])
        for rel in made:
            print("created {} — replace the example.test and example-* values with your own".format(rel))
        for rel in kept:
            print("kept {} (already there)".format(rel))
        for line in release_needs(args[1]):
            print("still needed for a release: {}".format(line.split(" ", 1)[1].split(" (", 1)[0]))
        print("next: python3 -I common/kd/overlay.py check {}".format(args[1]))
        return 0
    if len(args) != 5 or args[0] != "release":
        print("usage: overlay release <code.tar> <overlay.tar> <deploy_rev> <out.tar> | check <dir> | init <dir>",
              file=sys.stderr)
        return 2
    _, code, over, deploy_rev, out = args
    try:
        tar = release(Path(code).read_bytes(), Path(over).read_bytes(), deploy_rev)
    except OverlayError as exc:
        print(str(exc)[:500])
        return 4 if str(exc).startswith("overlay_incompatible") else 3
    except (OSError, tarfile.TarError) as exc:
        print("overlay_invalid: unreadable ({})".format(exc.__class__.__name__))
        return 3
    if out == "-":  # kd-release's sandbox writes nowhere: the tar comes back on stdout
        sys.stdout.buffer.write(tar)
        sys.stdout.flush()
    else:
        Path(out).write_bytes(tar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
