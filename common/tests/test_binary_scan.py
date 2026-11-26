"""Binary files are scanned, not skipped: a value behind a NUL byte, in a PNG text chunk or in UTF-16 is found by the
public scanner, the closed scanner and the history scanner (KD-9477)."""

import json
import random
import struct
import subprocess
import sys
import zlib
from pathlib import Path

import pytest

from kd import history_scan, private_scan
from kd.privacy_scan import Config, load_config, scan_tree

KD = Path(__file__).resolve().parents[1] / "kd"
# synthetic values, assembled at run time: this file is itself under the repository's privacy gate
DOMAIN = "private-" + "example.co"
LITERAL = "acme-node-7"
PUBLIC = ".".join(["91", "0", "2", "3"])
CFG = Config(forbidden_domains=(DOMAIN,), exclude=())
BOM = chr(0xFEFF)


def png(*chunks):
    """A minimal valid 1x1 PNG with the extra ``chunks`` ((type, data) pairs) before IDAT."""

    def chunk(kind, data):
        body = kind + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + b"".join(chunk(kind, data) for kind, data in chunks)
        + chunk(b"IDAT", zlib.compress(b"\x00\x10\x20\x30"))
        + chunk(b"IEND", b"")
    )


def text_chunk(value):
    return (b"tEXt", b"Comment\0" + value.encode("utf-8"))


def random_bytes(seed, size=40_000):
    rng = random.Random(seed)
    return bytes(rng.getrandbits(8) for _ in range(size))


def kinds(findings):
    return sorted(f.kind for f in findings)


def public(tmp_path, name, data):
    (tmp_path / name).write_bytes(data)
    return scan_tree(tmp_path, CFG)


def listing(tmp_path):
    path = tmp_path / "list" / "private_scan.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"literals": [LITERAL]}), encoding="utf-8")
    return str(path)


def closed(tmp_path, name, data):
    path = tmp_path / name
    path.write_bytes(data)
    return private_scan.scan(str(path), private_scan.load(listing(tmp_path)))


# --- the public scanner -----------------------------------------------------------------------------------------


def test_a_domain_and_an_address_after_a_nul_byte_are_found(tmp_path):
    found = public(
        tmp_path,
        "notes.txt",
        b"\0\0 see board." + DOMAIN.encode() + b" and " + PUBLIC.encode() + b"\n",
    )
    assert kinds(found) == ["domain", "ipv4"]
    assert all(f.path == "notes.txt" for f in found)


def test_a_finding_in_a_binary_carries_the_byte_offset_in_place_of_a_line(tmp_path):
    data = b"\0" * 100 + b"xx " + PUBLIC.encode() + b" yy"
    (finding,) = public(tmp_path, "a.bin", data)
    assert finding.line == 103 and "byte offset 103" in finding.text


def test_a_png_text_chunk_is_read(tmp_path):
    assert kinds(public(tmp_path, "a.png", png(text_chunk("made on " + DOMAIN)))) == [
        "domain"
    ]
    (tmp_path / "a.png").unlink()
    assert kinds(public(tmp_path, "b.png", png(text_chunk("host " + PUBLIC)))) == [
        "ipv4"
    ]


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
def test_utf16_text_is_read(tmp_path, codec):
    data = BOM + "see " + DOMAIN + " " + PUBLIC + "\n"
    assert kinds(public(tmp_path, "u.txt", data.encode(codec))) == ["domain", "ipv4"]


def test_a_secret_in_a_binary_is_found(tmp_path):
    token = "123456789:" + "A" * 35
    assert kinds(public(tmp_path, "s.bin", b"\0\1" + token.encode() + b"\0")) == [
        "telegram-token"
    ]


def test_the_text_rules_hold_in_a_binary(tmp_path):
    """Documentation and private ranges, version-like strings and the allow marker are no findings."""
    lines = [
        b"203.0.113.10 10.0.0.1 192.168.1.1 127.0.0.1 python 3.12.1.0 v1.2.3.4 999.1.1.1 1.2.3.4.5 1.2.3.4567",
        b"not" + DOMAIN.encode()[:-1] + b" digipaddock /opt/digipaddock",
        PUBLIC.encode() + b" privacy-scan: allow",
    ]
    assert public(tmp_path, "r.bin", b"\0" + b"\n".join(lines)) == []


def test_a_value_glued_to_the_next_field_is_found_in_a_binary(tmp_path):
    """A PNG chunk's text is followed by its CRC: no word boundary can close the value there."""
    assert kinds(
        public(
            tmp_path,
            "g.bin",
            b"\0" + PUBLIC.encode() + b"abc " + DOMAIN.encode() + b"xyz",
        )
    ) == [
        "domain",
        "ipv4",
    ]


def test_clean_images_and_random_bytes_pass(tmp_path):
    (tmp_path / "clean.png").write_bytes(png(text_chunk("a plain comment")))
    for number in range(30):
        (tmp_path / f"rand{number}.bin").write_bytes(random_bytes(number))
    assert scan_tree(tmp_path, CFG) == []


def test_the_tracked_images_of_the_repository_are_clean():
    repo = KD.parents[1]
    cfg = load_config(repo / "privacy-scan.json")
    assert scan_tree(repo / "docs" / "images", cfg, base=repo) == []


# --- the closed scanner -----------------------------------------------------------------------------------------


def test_closed_literal_after_a_nul_byte_is_found(tmp_path):
    (hit,) = closed(tmp_path, "a.txt", b"\0 host " + LITERAL.upper().encode() + b"\n")
    assert hit.endswith(":7: " + LITERAL + " (byte offset)")


def test_closed_literal_in_a_png_text_chunk_is_found(tmp_path):
    assert len(closed(tmp_path, "a.png", png(text_chunk("node " + LITERAL)))) == 1


@pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
def test_closed_literal_in_utf16_is_found(tmp_path, codec):
    assert len(closed(tmp_path, "u.txt", ("x " + LITERAL).encode(codec))) == 1
    assert len(closed(tmp_path, "v.txt", (BOM + LITERAL).encode(codec))) == 1


def test_closed_scanner_wants_the_whole_literal_and_an_opening_boundary(tmp_path):
    for name, data in (
        ("a", b"\0" + LITERAL[:-1].encode()),
        ("b", b"\0x" + LITERAL.encode()),
    ):
        assert closed(tmp_path, name, data) == []


def test_closed_scanner_passes_clean_images_and_random_bytes(tmp_path):
    assert closed(tmp_path, "clean.png", png(text_chunk("a plain comment"))) == []
    for number in range(30):
        assert closed(tmp_path, f"r{number}", random_bytes(number)) == []


def test_closed_scanner_cli_reports_a_binary(tmp_path):
    (tmp_path / "t").mkdir()
    (tmp_path / "t" / "a.png").write_bytes(png(text_chunk("node " + LITERAL)))
    p = subprocess.run(
        [
            sys.executable,
            "-I",
            str(KD / "private_scan.py"),
            "--list",
            listing(tmp_path),
            str(tmp_path / "t"),
        ],
        capture_output=True,
        text=True,
    )
    assert p.returncode == 1 and p.stdout.strip().endswith(
        ": " + LITERAL + " (byte offset)"
    )


# --- the history scanner ----------------------------------------------------------------------------------------


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "History Test")
    git(root, "config", "user.email", "history@example.invalid")
    (root / "privacy-scan.json").write_text(
        json.dumps({"forbidden_domains": [DOMAIN], "exclude": []})
    )
    (root / "README.md").write_text("base\n")
    git(root, "add", "--", "README.md", "privacy-scan.json")
    git(root, "commit", "-qm", "docs: base")
    git(root, "update-ref", "refs/kd/base", "HEAD")
    (tmp_path / "private_scan.json").write_text(json.dumps({"literals": [LITERAL]}))
    return root


def history(repo):
    return subprocess.run(
        [
            sys.executable,
            "-I",
            str(KD / "history_scan.py"),
            "--config",
            "privacy-scan.json",
            "--list",
            str(repo.parent / "private_scan.json"),
            ".",
            "refs/kd/base",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
    )


def add_binary(repo, name, data):
    (repo / name).write_bytes(data)
    git(repo, "add", "--", name)
    git(repo, "commit", "-qm", "docs: add " + name)


@pytest.mark.parametrize("what", ["nul", "png", "utf16"])
def test_a_value_added_inside_a_binary_is_found_even_when_removed_again(repo, what):
    data = {
        "nul": b"\0\0\1 host " + LITERAL.encode() + b" " + PUBLIC.encode() + b"\n",
        "png": png(text_chunk("node " + LITERAL + " " + PUBLIC)),
        "utf16": ("node " + LITERAL + " " + PUBLIC + "\n").encode("utf-16-le"),
    }[what]
    add_binary(repo, "blob.bin", data)
    git(repo, "rm", "-q", "blob.bin")
    git(repo, "commit", "-qm", "docs: remove it")
    p = history(repo)
    assert p.returncode == 1, p.stdout + p.stderr
    assert (
        "blob.bin:" in p.stdout
        and ": private literal" in p.stdout
        and ": ipv4 (byte offset) (allow " in p.stdout
    )
    assert LITERAL not in p.stdout + p.stderr


def test_the_forbidden_domain_in_a_binary_is_found_and_a_clean_one_passes(repo):
    add_binary(repo, "a.png", png(text_chunk("made on " + DOMAIN)))
    assert ": domain (byte offset) (allow " in history(repo).stdout
    git(repo, "reset", "-q", "--hard", "refs/kd/base")
    add_binary(repo, "b.png", png(text_chunk("a plain comment")))
    add_binary(repo, "c.bin", random_bytes(1))
    p = history(repo)
    assert p.returncode == 0 and "clean" in p.stdout, p.stdout


def test_added_binary_files_are_records_of_bytes_scanned_by_the_binary_rules(tmp_path):
    records = history_scan.history(
        "", "", binaries=[("x.png", png(text_chunk("host " + LITERAL + " " + PUBLIC)))]
    )
    assert [(where, number) for where, number, _ in records] == [("x.png", 0)]
    (public,) = history_scan.public_history_findings(records, CFG)
    assert public.startswith("x.png:") and ": ipv4 (byte offset) (allow " in public
    (secret,) = history_scan.closed_findings(
        records, private_scan.load(listing(tmp_path))
    )
    assert secret.startswith("x.png:") and secret.endswith(
        ": private literal (byte offset)"
    )
    assert LITERAL not in secret
    assert history_scan.history_text(records) == ""


def test_closed_literal_glued_to_the_next_field_is_found(tmp_path):
    assert len(closed(tmp_path, "g", b"\0" + LITERAL.encode() + b"9\xff")) == 1
