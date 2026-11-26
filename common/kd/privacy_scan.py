"""Public privacy scanner: what must never enter the public code repository.

Generic signs only — public IPv4 addresses (outside private, loopback and documentation ranges),
domains of privacy-scan.json and secret-like strings, in text by line and in a binary file (a NUL in its first 8 KiB)
by byte offset, as UTF-8 and as UTF-16. The deployment's own literal list is the
closed scanner (kd/private_scan.py with the overlay's literal list); both gate kd-release and kd-mirror.
Stdlib only and free of kd imports: ``python3 -I common/kd/privacy_scan.py <path>...``.
"""

import argparse
import fnmatch
import ipaddress
import json
import os
import re
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

SECRET_PATTERNS = (
    # A header followed by the redaction mask (***) is a masked key, not a key.
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----(?!\*)")),
    ("telegram-token", re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b")),
    ("anthropic-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("github-token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b")),
    ("forgejo-token", re.compile(r"(?i:token).*\b[0-9a-f]{40}\b|\b[0-9a-f]{40}\b.*(?i:token)")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    # the keys of model providers
    ("openrouter-key", re.compile(r"\bsk-or-v1-[0-9a-f]{64}\b")),
    ("fireworks-key", re.compile(r"\bfw_[A-Za-z0-9]{20,}\b")),
    ("ssh-pubkey", re.compile(r"\bssh-(?:ed25519|rsa|ecdsa-sha2-nistp256) AAAA[0-9A-Za-z+/]{20,}")),
)
ALLOWED_NETS = tuple(ipaddress.ip_network(net) for net in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8", "0.0.0.0/32",
    "255.255.255.255/32", "169.254.0.0/16", "192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24",
    "224.0.0.0/4",
))
_IPV4 = re.compile(r"(?<![\w.])(\d{1,3}(?:\.\d{1,3}){3})(?![\w-]|\.\d)")
# the same rules over a binary, whose values touch the neighbouring bytes: no closing word boundary (a PNG chunk's text
# is followed by its CRC), an address only not glued to another number
_IPV4_BINARY = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?!\d|\.\d)")
_SECRETS_BINARY = tuple(
    (kind, re.compile(pattern.pattern[:-2]) if pattern.pattern.endswith("\\b") else pattern)
    for kind, pattern in SECRET_PATTERNS
)
_VERSION_BEFORE = re.compile(r"(?i:\bv|\bversion |\bpython )\Z")
ALLOW_MARKER = "privacy-scan: allow"
BINARY_PROBE = 8192

Finding = namedtuple("Finding", "path line kind text")
Config = namedtuple("Config", "forbidden_domains exclude")


def load_config(path):
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("{}: expected an object".format(path))
    domains, exclude = data.get("forbidden_domains", []), data.get("exclude", [])
    for value in (domains, exclude):
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ValueError("{}: forbidden_domains and exclude are lists of strings".format(path))
    return Config(tuple(domains), tuple(exclude))


def _domain_patterns(cfg, binary=False):
    # in a binary the value is often followed by the next field's bytes (a PNG chunk's CRC): no closing boundary there
    end = "" if binary else r"(?![\w-])"
    return [re.compile(r"(?<![\w-])(?:[A-Za-z0-9-]+\.)*" + re.escape(domain) + end, re.IGNORECASE)
            for domain in cfg.forbidden_domains]


def _public_ip(line, pattern=_IPV4):
    """The start of the first public address in ``line``, or None."""
    for match in pattern.finditer(line):
        text = match.group(1)
        if _VERSION_BEFORE.search(line[max(0, match.start() - 12):match.start()]):
            continue
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue  # an octet above 255
        if not any(address in net for net in ALLOWED_NETS):
            return match.start()
    return None


def _hits(line, domains, binary=False):
    """[(kind, start of the match)] of one line."""
    hits = []
    for kind, pattern in _SECRETS_BINARY if binary else SECRET_PATTERNS:
        match = pattern.search(line)
        if match:
            hits.append((kind, match.start()))
    start = _public_ip(line, _IPV4_BINARY if binary else _IPV4)
    if start is not None:
        hits.append(("ipv4", start))
    for pattern in domains:
        match = pattern.search(line)
        if match:
            hits.append(("domain", match.start()))
            break
    return hits


def scan_text(text, path, cfg, _domains=None):
    domains = _domains if _domains is not None else _domain_patterns(cfg)
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        findings += [Finding(path, number, kind, line.strip()[:200]) for kind, _ in _hits(line, domains)]
    return findings


def _views(data):
    """What a binary file may say, as (text, byte offset of the text's character i): its bytes as UTF-8 (each byte
    that is not UTF-8 kept apart), and as UTF-16 of either byte order from either alignment — the same rules as for
    text then apply to each. UTF-16 is the encoding of a text that has a NUL after every character."""
    yield (data.decode("utf-8", "surrogateescape"),
           lambda text, i: len(text[:i].encode("utf-8", "surrogateescape")))
    for codec in ("utf-16-le", "utf-16-be"):
        for skip in (0, 1):
            body = data[skip:len(data) - (len(data) - skip) % 2]
            yield body.decode(codec, "replace"), lambda text, i, skip=skip: skip + 2 * i


def _context(text, start):
    window = text[max(0, start - 30):start + 60]
    return "".join(ch if ch.isprintable() else "?" for ch in window)


def scan_binary(data, path, cfg, _domains=None):
    """Findings in a file that is not text, by the rules of the text: the line number of a finding is its byte offset
    (approximate for UTF-16 beyond the BMP), the text begins with ``byte offset N``. The allow marker counts when it is
    within 200 characters of the match on its line."""
    domains = _domains if _domains is not None else _domain_patterns(cfg, binary=True)
    found, seen = [], set()
    for text, offset in _views(data):
        here = set()
        position = 0
        for line in text.split("\n"):
            for kind, start in _hits(line, domains, binary=True):
                if ALLOW_MARKER in line[max(0, start - 200):start + 200]:
                    continue
                key = (kind, line[start:start + 16].lower())
                here.add(key)
                if key in seen:  # an ASCII text reads the same in the UTF-8 and the UTF-16 views
                    continue
                where = offset(text, position + start)
                found.append(Finding(path, where, kind, "byte offset {}: {}".format(where, _context(line, start))))
            position += len(line) + 1
        seen |= here
    return sorted(found, key=lambda f: (f.line, f.kind))


def _excluded(rel, cfg):
    return any(fnmatch.fnmatchcase(rel, glob) for glob in cfg.exclude)


def _candidates(root, tracked_only):
    if root.is_file():
        yield root
        return
    if not root.is_dir():
        raise FileNotFoundError(str(root))
    if tracked_only:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"], check=True,
                             stdout=subprocess.PIPE).stdout.decode("utf-8")
        for name in out.split("\0"):
            if name:
                yield root / name
        return
    for dirpath, dirnames, filenames in os.walk(str(root)):
        dirnames[:] = sorted(d for d in dirnames if d != ".git")
        for name in sorted(filenames):
            yield Path(dirpath) / name


def scan_tree(root, cfg, *, tracked_only=False, base=None):
    """Findings in the files under ``root`` (text by line, binary by byte offset); paths (and exclude globs) are relative to ``base``."""
    root = Path(root)
    base = Path(base) if base is not None else (root.parent if root.is_file() else root)
    domains, binary_domains = _domain_patterns(cfg), _domain_patterns(cfg, binary=True)
    findings = []
    for path in _candidates(root, tracked_only):
        try:
            rel = path.resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        if _excluded(rel, cfg) or not path.is_file() or path.is_symlink():
            continue
        data = path.read_bytes()
        if b"\0" in data[:BINARY_PROBE]:
            findings += scan_binary(data, rel, cfg, binary_domains)
            continue
        findings += scan_text(data.decode("utf-8", "replace"), rel, cfg, domains)
    return findings


def main(argv=None):
    parser = argparse.ArgumentParser(prog="privacy_scan", description="Public privacy scanner")
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--config", type=Path, default=Path("privacy-scan.json"))
    parser.add_argument("--tracked", action="store_true", help="only files git tracks")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    try:
        cfg = load_config(args.config)
        base = args.config.resolve().parent
        findings = []
        for path in args.paths:
            inside = str(path.resolve()).startswith(str(base) + os.sep) or path.resolve() == base
            findings += scan_tree(path, cfg, tracked_only=args.tracked, base=base if inside else None)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("privacy_scan: {}".format(exc), file=sys.stderr)
        return 2
    for f in findings:
        print("{}:{}: {}: {}".format(f.path, f.line, f.kind, f.text))
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
