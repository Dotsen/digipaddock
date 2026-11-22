#!/usr/bin/python3
"""agent-sec-snapshot: a snapshot of security settings for the sysadmin's monitor.

The agent-ro-gate gateway runs it as root (sudo -n), without arguments. Read-only. It prints lines
`section<TAB>object<TAB>value`, sorted within a section; no secret content — only sha256,
line counts and settings. A section's error is the line `section<TAB>error<TAB>reason`, the other sections are
printed; sections that did not fit LIMIT are `error<TAB>truncated`. Files are read without following symlinks, at
most FILE_MAX. SSH keys — only the type, the SHA256 fingerprint, option names and a cut comment: neither the key
body nor option values; the key sections come last, so that truncation loses only them.

Python 3.6+ and the stdlib only: targets with AlmaLinux 8 have Python 3.6 as the system Python."""
import base64
import glob
import hashlib
import os
import posixpath
import shutil
import stat
import struct
import subprocess
import sys

VERSION = "# agent-sec-snapshot v1"
LIMIT = 64 * 1024
VALUE_MAX = 300
LINK_MAX = 120  # a symlink's target in a value — no longer than this
FILE_MAX = 1 << 20  # larger — the size is printed, not the hash: a megabyte of authorized_keys is a signal by itself
CMD_TIMEOUT = 8  # three commands × 8 s fit into the monitor's 30 s call
ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C", "LC_ALL": "C"}
NOLOGIN = {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false", "/bin/sync", "/sbin/halt",
           "/sbin/shutdown"}
DYNAMIC_UID = (61184, 65519)  # systemd DynamicUser: they come and go, not counted
NOBODY_UID = (65534, 65535, 4294967294, 4294967295)  # nobody/nfsnobody and (uid_t)-1/-2; other large uids are shown
SSHD_KEYS = ("port", "permitrootlogin", "passwordauthentication", "pubkeyauthentication", "allowusers",
             "denyusers", "allowgroups", "authorizedkeysfile", "kbdinteractiveauthentication",
             "challengeresponseauthentication", "permitemptypasswords", "authorizedkeyscommand",
             "authorizedkeyscommanduser", "trustedusercakeys", "authorizedprincipalsfile", "permituserenvironment",
             "usepam")
SSHD_FILES = ("etc/ssh/sshd_config", "etc/ssh/sshd_config.d/*")
KEY_FILES = (".ssh/authorized_keys", ".ssh/authorized_keys2")
SKIP_HOMES = ("/", "/nonexistent", "/proc", "/dev", "/sys", "/run")  # homes where keys are not looked for
ADMIN_GROUPS = ("root", "sudo", "admin", "wheel", "adm", "docker", "lxd", "staff", "google-sudoers")
CRON_GLOBS = ("etc/crontab", "etc/anacrontab", "etc/cron.d/*", "etc/cron.hourly/*", "etc/cron.daily/*",
              "etc/cron.weekly/*", "etc/cron.monthly/*", "var/spool/cron/crontabs/*", "var/spool/cron/*")
PERSIST = ("etc/ld.so.preload", "etc/rc.local")
UFW_SKIP = ("Status:", "Logging:", "Default:", "New profiles:", "To ", "--")
# key type → how `ssh-keygen -l` and sshd write it in "Accepted publickey … ssh2: ED25519 SHA256:…"
KEY_TYPES = {"ssh-rsa": "RSA", "ssh-dss": "DSA", "ssh-ed25519": "ED25519", "ecdsa-sha2-nistp256": "ECDSA",
             "ecdsa-sha2-nistp384": "ECDSA", "ecdsa-sha2-nistp521": "ECDSA", "sk-ssh-ed25519@openssh.com": "ED25519-SK",
             "sk-ecdsa-sha2-nistp256@openssh.com": "ECDSA-SK"}
KEY_OPTIONS = ("agent-forwarding", "cert-authority", "command", "environment", "expiry-time", "from",
               "no-agent-forwarding", "no-port-forwarding", "no-pty", "no-touch-required", "no-user-rc",
               "no-x11-forwarding", "permitlisten", "permitopen", "port-forwarding", "principals", "pty", "restrict",
               "tunnel", "user-rc", "verify-required", "x11-forwarding")  # other names are "other"
COMMENT_MAX = 80
PUB_MAX = 50  # *.pub in one ~/.ssh — no more than this
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)


def _rel(root, path):
    """The path as it looks on the server (in tests root is a temporary directory)."""
    if root in ("/", ""):
        return path
    return "/" + os.path.relpath(path, root).replace(os.sep, "/")


def _read(path):
    """The bytes of a regular file without following a symlink; not regular — 'other', over FILE_MAX — 'big N' (a string)."""
    fd = os.open(path, os.O_RDONLY | _NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return "other"
        if st.st_size > FILE_MAX:
            return "big %d" % st.st_size
        f = os.fdopen(fd, "rb")
        fd = None
    finally:
        if fd is not None:
            os.close(fd)
    with f:
        return f.read(FILE_MAX + 1)


def _sha(path, skip_comments):
    """(sha256, line count) of a regular file without following a symlink; a big file — ('big N', 0)."""
    data = _read(path)
    if isinstance(data, str):
        return data, 0
    if skip_comments:  # the header "# (… installed on …)" changes with every reinstall of a crontab
        data = b"\n".join(ln for ln in data.split(b"\n") if not ln.lstrip().startswith(b"#"))
    return hashlib.sha256(data).hexdigest(), data.count(b"\n")


def _file_value(path, lines=False, skip_comments=False):
    """'-' | 'symlink -> target' | 'sha256[ N]' | 'big N' | 'other' | 'error Type'."""
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "-"
    except OSError as exc:  # EACCES (NFS root_squash) and the like — not "the keys were removed"
        return "error %s" % type(exc).__name__
    try:
        if stat.S_ISLNK(st.st_mode):
            # the target is free text of the link's unprivileged owner: it is cut; the race "symlink removed" — error
            return "symlink -> " + os.readlink(path)[:LINK_MAX]
        digest, n = _sha(path, skip_comments)
    except OSError as exc:
        return "error %s" % type(exc).__name__
    if lines and len(digest) == 64:
        return "%s %d" % (digest, n)
    return digest


def _files(root, pattern):
    """Entries by a glob except directories (symlinks stay: _file_value shows them), sorted."""
    return sorted(p for p in glob.glob(os.path.join(root, pattern)) if os.path.islink(p) or not os.path.isdir(p))


def _run(run, argv):
    r = run(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV, timeout=CMD_TIMEOUT)
    return r.returncode, r.stdout.decode("utf-8", "replace")


def _table(root, name):
    """The lines of /etc/passwd or /etc/group split by ':' (surrogateescape: non-UTF-8 names do not crash the snapshot)."""
    with open(os.path.join(root, "etc", name), "rb") as f:
        raw = f.read()
    return [ln.decode("utf-8", "surrogateescape").split(":") for ln in raw.splitlines() if ln]


def _accounts(root):
    """(name, uid, home, shell): uid 0, everyone with a login shell (any uid, DynamicUser and nobody included)
    and uid >= 1000 (except DynamicUser and nobody with nologin — they come and go and give no login).
    /etc/passwd is read directly: NSS (DynamicUser, OS Login) comes and goes."""
    out = []
    for f in _table(root, "passwd"):
        if len(f) < 7 or not f[2].isdigit():
            continue
        uid = int(f[2])
        noisy = DYNAMIC_UID[0] <= uid <= DYNAMIC_UID[1] or uid in NOBODY_UID
        if uid == 0 or f[6] not in NOLOGIN or (uid >= 1000 and not noisy):
            out.append((f[0], uid, f[5], f[6]))
    return out


def _home(root, home):
    """The home from /etc/passwd as a path under root; None — not absolute or a system one (/, /proc…): no keys are looked for there."""
    if not home.startswith("/"):
        return None
    home = "/" + posixpath.normpath(home).lstrip("/")  # "//x", "/x/", "/a/.." — to one form
    if home in SKIP_HOMES:
        return None
    return os.path.join(root, *home[1:].split("/"))


def users(root, env):
    return [(name, "%d %s" % (uid, shell)) for name, uid, home, shell in _accounts(root)]


def groups(root, env):
    """Members of the admin groups: from /etc/group and the accounts with that group as primary (`usermod -g sudo` does not show in group)."""
    primary = {}
    for f in _table(root, "passwd"):
        if len(f) >= 4:
            primary.setdefault(f[3], set()).add(f[0])
    out = []
    for g in _table(root, "group"):
        if len(g) >= 4 and g[0] in ADMIN_GROUPS:
            members = {m for m in g[3].split(",") if m} | primary.get(g[2], set())
            out.append((g[0], " ".join(sorted(members)) or "-"))
    return out


def _shown_homes(root):
    """The homes /root, /home/* and those of the accounts in users: their "-" (no authorized_keys) is printed, *.pub is looked for in them."""
    shown = {os.path.join(root, "root")}
    shown.update(p for p in glob.glob(os.path.join(root, "home", "*")) if os.path.isdir(p))
    for name, uid, home, shell in _accounts(root):
        p = _home(root, home)
        if p:
            shown.add(p)
    return shown


def _key_files(root, env):
    """[(path, whether to show "-")]: authorized_keys in the homes of all /etc/passwd entries (plus /root and /home/*)
    and at the AuthorizedKeysFile paths of sshd -T. `ssh -N` by key works for an account with nologin too, so all
    homes are looked at; "-" (no file) — only for authorized_keys of the homes in _shown_homes."""
    shown = _shown_homes(root)
    quiet = set()
    for f in _table(root, "passwd"):
        p = _home(root, f[5]) if len(f) >= 6 else None
        if p and p not in shown:
            quiet.add(p)
    names = set(KEY_FILES)
    for rel in env["sshd"].get("authorizedkeysfile", "").split():
        rel = rel.replace("%h/", "")
        if rel and not rel.startswith("/") and "%" not in rel:
            names.add(rel)
    return [(os.path.join(home, *name.split("/")), name == ".ssh/authorized_keys" and home in shown)
            for home in sorted(shown | quiet) for name in sorted(names)]


def keys(root, env):
    out = []
    for p, always in _key_files(root, env):
        value = _file_value(p, lines=True)
        if value != "-" or always:
            out.append((_rel(root, p), value))
    return out


def _split_options(line):
    """(options, the rest of the line): authorized_keys options run to the first space outside quotes; `\\"` is
    skipped as a whole, as in sshd (sshkey_advance_past_options)."""
    quoted, i = False, 0
    while i < len(line):
        c = line[i]
        if c == "\\" and line[i + 1:i + 2] == '"':
            i += 2
            continue
        if c == '"':
            quoted = not quoted
        elif c in " \t" and not quoted:
            return line[:i], line[i:].lstrip(" \t")
        i += 1
    return line, ""


def _option_names(options):
    """Option names split by commas outside quotes (without values), sorted; "-" — no options."""
    names, quoted, start = set(), False, 0
    for i, c in enumerate(options + ","):
        if c == '"' and options[i - 1:i] != "\\":
            quoted = not quoted
        elif c == "," and not quoted:
            name = options[start:i].split("=", 1)[0].strip().lower()
            if name:
                names.add(name if name in KEY_OPTIONS else "other")
            start = i + 1
    return ",".join(sorted(names)) or "-"


def _key_line(line):
    """(type, SHA256 fingerprint, option names, comment) of an authorized_keys or .pub line; None — not a key.
    The fingerprint is as `ssh-keygen -l` prints it: sha256 of the key body in base64 without "=". The type in the body must match the line's type."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    options = ""
    if line.split(None, 1)[0] not in KEY_TYPES:
        options, line = _split_options(line)
    parts = line.split(None, 2)
    if len(parts) < 2 or parts[0] not in KEY_TYPES:
        return None
    try:
        blob = base64.b64decode(parts[1].encode("ascii"), validate=True)
        n = struct.unpack(">I", blob[:4])[0]
    except (ValueError, struct.error):  # binascii.Error and UnicodeEncodeError are also ValueError
        return None
    if blob[4:4 + n] != parts[0].encode("ascii"):
        return None
    fp = "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode("ascii").rstrip("=")
    comment = "".join(c if " " <= c <= "~" else "?" for c in (parts[2] if len(parts) > 2 else ""))
    return KEY_TYPES[parts[0]], fp, _option_names(options), comment.strip()[:COMMENT_MAX]


def _prints(path):
    """[(type, fingerprint, options, comment)] of the file's keys and its value as in keys; None — not a regular file."""
    try:
        data = _read(path)
    except OSError:
        return None, None
    if isinstance(data, str):
        return None, None
    found = [_key_line(ln) for ln in data.decode("utf-8", "replace").splitlines()]
    return [k for k in found if k], "%s %d" % (hashlib.sha256(data).hexdigest(), data.count(b"\n"))


def keyprints(root, env):
    """The keys in authorized_keys of the same files as keys: `path type fingerprint` → `options comment`. The line of
    the file itself carries its value from keys — the monitor compares keys only of the file version they belong to."""
    out = []
    for p, always in _key_files(root, env):
        found, value = _prints(p)
        if found is None:
            continue
        rel = _rel(root, p)
        out.append((rel, value))
        for kind, fp, flags, comment in found:
            out.append(("%s %s %s" % (rel, kind, fp), (flags + " " + comment) if comment else flags))
    return out


def pubkeys(root, env):
    """Outgoing keys: ~/.ssh/*.pub of the homes in _shown_homes — `path type fingerprint` → the comment or "-"."""
    out = []
    for home in sorted(_shown_homes(root)):
        for p in sorted(glob.glob(os.path.join(home, ".ssh", "*.pub")))[:PUB_MAX]:
            if os.path.islink(p):
                continue
            found, value = _prints(p)
            for kind, fp, flags, comment in found or ():
                out.append(("%s %s %s" % (_rel(root, p), kind, fp), comment or "-"))
    return out


def sudoers(root, env):
    return [(_rel(root, p), _file_value(p)) for p in _files(root, "etc/sudoers") + _files(root, "etc/sudoers.d/*")]


def shadow(root, env):
    return [("/etc/shadow", _file_value(os.path.join(root, "etc", "shadow")))]


def sshd_conf(run):
    """The effective settings of `sshd -T` (key → space-separated values); an error is a RuntimeError."""
    rc, out = _run(run, ["sshd", "-T"])
    if rc != 0:
        raise RuntimeError("sshd -T: код %d" % rc)
    conf = {}
    for ln in out.splitlines():
        parts = ln.strip().split(None, 1)
        if parts:
            conf.setdefault(parts[0].lower(), []).append(parts[1] if len(parts) > 1 else "")
    return {k: " ".join(v for v in vs if v) for k, vs in conf.items()}


def sshd(root, env):
    conf = env["sshd"]
    if "error" in conf:  # already "Type: reason" from render — not wrapped a second time
        return [("error", conf["error"])]
    out = []
    for k in SSHD_KEYS:
        v = conf.get(k, "-") or "-"
        if k == "permitrootlogin" and v == "without-password":
            v = "prohibit-password"  # the old name of the same value (OpenSSH < 7.0 prints it this way)
        out.append((k, v))
    for pattern in SSHD_FILES:  # sshd -T without -C does not show Match blocks — the files are watched
        for p in _files(root, pattern):
            out.append(("file:" + _rel(root, p), _file_value(p)))
    return out


def cron(root, env):
    return [(_rel(root, p), _file_value(p, skip_comments=True)) for pat in CRON_GLOBS for p in _files(root, pat)]


def persist(root, env):
    return [("/" + rel, _file_value(os.path.join(root, *rel.split("/")))) for rel in PERSIST]


def units(root, env):
    base = os.path.join(root, "etc", "systemd", "system")
    out = []
    if not os.path.isdir(base):
        return out
    for name in sorted(os.listdir(base)):
        p = os.path.join(base, name)
        if os.path.islink(p) or os.path.isfile(p):
            out.append((name, _file_value(p)))
        elif os.path.isdir(p) and name.endswith((".wants", ".requires")):
            for entry in sorted(os.listdir(p)):
                out.append((name + "/" + entry, _file_value(os.path.join(p, entry))))
        elif os.path.isdir(p) and name.endswith(".d"):
            for entry in sorted(os.listdir(p)):
                if entry.endswith(".conf"):
                    out.append((name + "/" + entry, _file_value(os.path.join(p, entry))))
    return out


def fw(root, env):
    run, which = env["run"], env["which"]
    out = []
    if which("ufw", path=ENV["PATH"]) is None:
        out.append(("ufw", "absent"))
    else:
        rc, text = _run(run, ["ufw", "status", "verbose"])
        if rc != 0:
            raise RuntimeError("ufw status: код %d" % rc)
        lines = [ln.strip() for ln in text.splitlines()]
        status = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("Status:")), "")
        if status not in ("active", "inactive"):
            raise RuntimeError("ufw status: %r" % status[:40])
        out.append(("ufw", status))
        default = next((ln.split(":", 1)[1].strip() for ln in lines if ln.startswith("Default:")), "")
        if default:
            out.append(("ufw:default", " ".join(default.split())))
        for ln in lines:  # the object is the rule's text: `ufw insert 1` does not shift other objects
            if ln and not ln.startswith(UFW_SKIP):
                out.append(("ufw:" + " ".join(ln.split()), "rule"))
    rc, text = _run(run, ["systemctl", "is-active", "fail2ban"])
    if not text.strip():
        raise RuntimeError("systemctl is-active: нет вывода (код %d)" % rc)
    state = text.strip().splitlines()[0]
    if state != "active" and which("fail2ban-client", path=ENV["PATH"]) is None:
        state = "absent"
    out.append(("fail2ban", state))
    return out


SECTIONS = (("users", users), ("groups", groups), ("keys", keys), ("sudoers", sudoers), ("shadow", shadow),
            ("sshd", sshd), ("cron", cron), ("persist", persist), ("units", units), ("fw", fw),
            ("keyprints", keyprints), ("pubkeys", pubkeys))


def _value(v):
    v = str(v).replace("\t", " ").replace("\r", " ").replace("\n", " ")
    v = v.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    return v[:VALUE_MAX]


def render(root="/", run=subprocess.run, which=shutil.which):
    try:
        conf = sshd_conf(run)
    except Exception as exc:  # noqa: BLE001
        conf = {"error": "%s: %s" % (type(exc).__name__, exc)}
    env = {"run": run, "which": which, "sshd": conf}
    blocks = []
    for name, fn in SECTIONS:
        try:
            items = fn(root, env)
        except Exception as exc:  # noqa: BLE001 — one section must not bring the snapshot down
            items = [("error", "%s: %s" % (type(exc).__name__, exc))]
        blocks.append((name, ["%s\t%s\t%s" % (name, _value(i), _value(v)) for i, v in sorted(items)]))
    lines, size, cut = [VERSION], len(VERSION) + 1, False
    for name, block in blocks:
        text = "".join(ln + "\n" for ln in block)
        if cut or size + len(text.encode("utf-8")) > LIMIT:
            cut = True  # everything further is marked truncated: the monitor will not take a loss for "disappeared"
            lines.append("%s\terror\ttruncated" % name)
            continue
        lines.extend(block)
        size += len(text.encode("utf-8"))
    return "\n".join(lines) + "\n"


def main(argv):
    if argv:
        sys.stderr.write("usage: agent-sec-snapshot (без аргументов)\n")
        return 2
    sys.stdout.buffer.write(render().encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
