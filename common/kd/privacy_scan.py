"""Public privacy scanner: what must never enter the public code repository.

Generic signs only — public IPv4 addresses (outside private, loopback and documentation ranges),
domains of privacy-scan.json and secret-like strings. The deployment's own literal list is the
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


def _domain_patterns(cfg):
    return [re.compile(r"(?<![\w-])(?:[A-Za-z0-9-]+\.)*" + re.escape(domain) + r"(?![\w-])", re.IGNORECASE)
            for domain in cfg.forbidden_domains]


def _public_ip(line):
    for match in _IPV4.finditer(line):
        text = match.group(1)
        if _VERSION_BEFORE.search(line[:match.start()]):
            continue
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue  # an octet above 255
        if not any(address in net for net in ALLOWED_NETS):
            return True
    return False


def scan_text(text, path, cfg, _domains=None):
    domains = _domains if _domains is not None else _domain_patterns(cfg)
    findings = []
    for number, line in enumerate(text.splitlines(), 1):
        if ALLOW_MARKER in line:
            continue
        kinds = [kind for kind, pattern in SECRET_PATTERNS if pattern.search(line)]
        if _public_ip(line):
            kinds.append("ipv4")
        if any(pattern.search(line) for pattern in domains):
            kinds.append("domain")
        findings += [Finding(path, number, kind, line.strip()[:200]) for kind in kinds]
    return findings


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
    """Findings in the text files under ``root``; paths (and exclude globs) are relative to ``base``."""
    root = Path(root)
    base = Path(base) if base is not None else (root.parent if root.is_file() else root)
    domains = _domain_patterns(cfg)
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
