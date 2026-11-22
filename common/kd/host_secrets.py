"""kd-secret: the secrets of the install catalog on the host itself, under root.

    kd-secret status [--json]   one line per catalog secret: present, missing or wrong (owner, mode, not a file)
    kd-secret put <name>        the value from stdin (a terminal: typed without echo) -> the catalog's path, owner
                                and mode, through the same writer as put_secret.sh (common/install/secret_write.sh)

status is a report: exit 0 whatever it finds. put refuses an unknown name, an empty value, a value without the
keys of the file's format, a multi-line format typed at a terminal (2) and a missing directory — its installer
makes it (1). The value never reaches argv or the output.
"""

import getpass
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from kd import claude_token, install_catalog, llm_models

TREE = Path(__file__).resolve().parents[2]
WRITER = TREE / "common" / "install" / "secret_write.sh"


def _look(path):
    """(kind, user, group, mode) of ``path`` without following a link; None — it does not exist."""
    import grp  # noqa: PLC0415 — Linux only
    import pwd  # noqa: PLC0415

    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    kind = "file" if stat.S_ISREG(st.st_mode) else "link" if stat.S_ISLNK(st.st_mode) else "other"

    def name(lookup, ident):
        try:
            return lookup(ident)[0]
        except KeyError:
            return str(ident)

    return kind, name(pwd.getpwuid, st.st_uid), name(grp.getgrgid, st.st_gid), stat.S_IMODE(st.st_mode)


def status(catalog, *, look=None) -> list:
    """One row per secret: name, path, state (ok | missing | wrong), detail, wanted_by."""
    look = look or _look
    rows = []
    for s in catalog:
        found = look(s.path)
        want = "{}:{} {:o}".format(s.owner, s.group, s.mode)
        if found is None:
            state, detail = "missing", "no file"
        elif found[0] != "file":
            state, detail = "wrong", "not a file ({}) — remove it and put it again".format(found[0])
        else:
            have = "{}:{} {:o}".format(found[1], found[2], found[3])
            state, detail = ("ok", have) if have == want else ("wrong", "{}, must be {}".format(have, want))
        rows.append({"name": s.name, "path": s.path, "state": state, "detail": detail,
                     "wanted_by": list(s.wanted_by)})
    return rows


def _value(secret, stdin):
    """The value to write, or (None, reason)."""
    lines = secret.format.split("\n")
    if stdin.isatty():
        if len(lines) > 1:
            return None, "{} has several lines ({}) — pass it as a file: kd-secret put {} < file".format(
                secret.name, " ".join(lines), secret.name)
        raw = getpass.getpass("{} ({}): ".format(secret.name, secret.format)).encode("utf-8")
    else:
        raw = stdin.read()
    if not raw.strip():
        return None, "empty value"
    text = raw.decode("utf-8", "replace")
    keys = [line.split("=", 1)[0] + "=" for line in lines if "=" in line]
    missing = [k for k in keys if not any(row.startswith(k) for row in text.splitlines())]
    if missing:
        return None, "file {} — lines {}: missing {}".format(secret.name, secret.format.replace("\n", "; "),
                                                          ", ".join(missing))
    if secret.path == claude_token.PATH:  # read alike by systemd and the root worker runner
        reason = claude_token.problem_text(text)
        if reason:
            return None, "{} ({}): {}".format(secret.name, secret.format, reason)
    if secret.path.startswith(llm_models.ENV_DIR + "/"):  # one bare line, read alike by systemd and the runner
        reason = llm_models.secret_problem(text, Path(secret.path).stem)
        if reason:
            return None, "{} ({}): {}".format(secret.name, secret.format, reason)
    return raw.rstrip(b"\n") + b"\n", None


def _run(cmd, value):
    return subprocess.run(cmd, input=value).returncode


def main(argv=None, *, catalog=None, root="", is_root=None, stdin=None, run=None, look=None, out=None,
         err=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    out, err = out or sys.stdout, err or sys.stderr
    if is_root is None:
        is_root = os.geteuid() == 0
    as_json = argv == ["status", "--json"]
    if not (argv in (["status"], ["status", "--json"]) or (len(argv) == 2 and argv[0] == "put")):
        print("usage: kd-secret status [--json] | put <name> < value", file=err)
        return 2
    if not is_root:
        print("root is required", file=err)
        return 1
    try:
        catalog = install_catalog.secrets() if catalog is None else catalog
    except install_catalog.CatalogError as exc:
        print(exc, file=err)
        return 1
    if argv[0] == "status":
        rows = status(catalog, look=look)
        if as_json:
            print(json.dumps(rows, ensure_ascii=False), file=out)
        else:
            for r in rows:
                need = ", ".join(r["wanted_by"]) or "optional"
                print("{:<12} {:<8} {}  ({}; {})".format(r["name"], r["state"], r["path"], r["detail"], need),
                      file=out)
        return 0
    secret = next((s for s in catalog if s.name == argv[1]), None)
    if secret is None:
        print("no secret {} in the catalog; there are: {}".format(argv[1], ", ".join(s.name for s in catalog)), file=err)
        return 2
    value, reason = _value(secret, stdin or sys.stdin.buffer)
    if value is None:
        print("kd-secret: " + reason, file=err)
        return 2
    folder = os.path.dirname(secret.path)
    if not os.path.isdir(root + folder):
        print("no directory {} — {} installs it; run that first".format(folder, secret.needed_by), file=err)
        return 1
    code = (run or _run)(["bash", WRITER.as_posix(), secret.path, "{}:{}".format(secret.owner, secret.group),
                          "{:04o}".format(secret.mode)], value)
    if code != 0:
        return 1
    if secret.restart:  # they read the file only at start (Hindsight and the Claude token)
        if (run or _run)(["systemctl", "try-restart"] + list(secret.restart), None) != 0:
            print("kd-secret: {} is written, but systemctl try-restart {} failed — see their journal".format(
                secret.name, " ".join(secret.restart)), file=err)
            return 1
        print("restarted (if running): " + " ".join(secret.restart), file=out)
    if secret.check:
        print("check: bash {} {}".format((TREE / secret.needed_by).as_posix(), secret.check), file=out)
    return 0
