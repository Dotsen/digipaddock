#!/usr/bin/python3 -I
# -*- coding: utf-8 -*-
"""agent-exec-gate — the exec key's forced command on a target server.

The factory host (``kd-admin-run``, ``kd-admin-critical``) runs ``agent-exec-gate run`` over the exec key and feeds
an *envelope* on stdin: the decision the owner signed in the cabinet (its payload, the signed proposal, the term,
the nonce, the card's revision, a batch's digests), the browser's passkey assertion and the exact command of one
step. This gate recomputes the signed value the way the cabinet built it, checks the assertion against the owner's
public key from its own registry (``/etc/agent-exec-gate/signers.json``, written by the installer from
``kd-cabinet user signers``), checks that the command is the step's text (a script step) or the argv the stage forms
for a typed verb, and only then runs it. A host that holds the exec key but not the owner's signature can send
nothing a target runs.

Modes (``/etc/agent-exec-gate/gate.json``): ``observe`` logs a failed check to syslog and runs anyway (the first
week); ``enforce`` refuses with exit 126. A critical command runs once per server in both modes (its id is kept in
``/var/lib/agent-exec-gate/used``). ``agent-exec-gate snapshot <nonce>`` is the plan window's read-only snapshot
(what agent-ro may read anyway), allowed in both modes.

A signed plan's steps run once each and in order, in both modes (KD-9483): the same directory journals each step's
result under ``plan_key`` (the signed decision's id and nonce, the plan's hash, the step); a step that succeeded is
refused, a failed one may run again, and a step runs only when every earlier step of the plan on this server
succeeded — one run of a plan at a time (its lock). ``agent-exec-gate step <key>`` answers "ok" or "no" for the
executor, which checks a predecessor that ran on another server. An unsigned test-zone step keeps no marks.

The policy (``/usr/local/bin/agent-exec-policy`` — kd.exec_policy installed next to the gate) holds in both
modes, before the signature is checked: rule 1 (self-protection) on every command — a plan step, a critical action,
a legacy command —, rule 2 (a critical plan step needs its own signature) on plan steps, with the site units of
gate.json's ``critical_units`` (none listed: every unit's stop is critical). Its refusal is exit 126 and
«deny policy (…)» in the journal; observe tolerates only a signature that does not check out. A policy file that does
not load refuses every command but the snapshot.

Python 3.6+ and only stdlib (AlmaLinux 8 targets); signatures are verified with the system ``openssl``.
"""
import base64
import calendar
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import types
from typing import Any, Callable, Dict, List, Optional, Tuple

try:
    import syslog
except ImportError:  # Windows: local tests
    syslog = None
try:
    import fcntl
except ImportError:  # Windows: local tests
    fcntl = None

GATE_JSON = "/etc/agent-exec-gate/gate.json"
SIGNERS_JSON = "/etc/agent-exec-gate/signers.json"
USED_DIR = "/var/lib/agent-exec-gate/used"
POLICY_PATH = "/usr/local/bin/agent-exec-policy"
TIMEOUT = 600
SNAPSHOT_TIMEOUT = 120
MAX_ENVELOPE = 128 * 1024
SKEW = 300
PLAN_MAX_TTL = 7 * 86400
ACTION_MAX_TTL = 900
RUN_WORD = "agent-exec-gate run"
SNAPSHOT_WORD = "agent-exec-gate snapshot"
STEP_WORD = "agent-exec-gate step"  # + " <plan_key>": "ok" when that step of a signed plan succeeded here
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
MODES = ("observe", "enforce")
SAFE_PATH = "/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
MAX_DIGESTS = 50
KEYS = frozenset(("schema_version", "kind", "task_id", "server", "command", "payload", "proposal", "grant",
                  "decision", "assertion"))
PLAN_PAYLOAD = frozenset(("task_id", "proposal_sha256", "card_scope_sha256"))
ACTION_PAYLOAD = PLAN_PAYLOAD | frozenset(("proposal_id",))
RESPONSE = ("clientDataJSON", "authenticatorData", "signature")
# fullmatch only: re.match with ^…$ lets a trailing newline through
HEX64 = re.compile(r"[0-9a-f]{64}")
HEX32 = re.compile(r"[0-9a-f]{32}")
NONCE16 = re.compile(r"[0-9a-f]{16}")
SAFE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
SAFE_UNIT = re.compile(r"[A-Za-z0-9*][A-Za-z0-9@._*-]{0,120}")  # a critical_units pattern (kd.admin_operations)
TASK_ID = re.compile(r"[A-Z]{1,8}-[0-9]{1,8}")
B64URL = re.compile(r"[A-Za-z0-9_=-]*")
TOO_DEEP = "конверт вложен слишком глубоко"
# the plan window's snapshot (sysadmin/plan_window.py PARTS): the same read-only commands, the same markers
SNAPSHOT_PARTS = (
    ("snapshot", "/usr/local/bin/agent-sec-snapshot"),
    ("ports", "ss -tlnpu"),
    ("wgpeers", "if command -v wg >/dev/null 2>&1; then wg show all allowed-ips; fi"),
)
# SubjectPublicKeyInfo prefix of an uncompressed P-256 point (id-ecPublicKey, prime256v1, BIT STRING of 65 bytes)
EC_P256_SPKI = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d030107034200")
RSA_OID = bytes.fromhex("06092a864886f70d010101")


def which(name: str) -> Optional[str]:
    """A tool of this host on the gate's own PATH, never the one the ssh session inherited."""
    return shutil.which(name, path=SAFE_PATH)


def openssl_path() -> str:
    return shutil.which("openssl", path=SAFE_PATH) or "/usr/bin/openssl"


class Deny(Exception):
    """The step does not run; the text is the reason the stage sees — in Russian: the owner reads it in the card's
    thread (the gate's "agent-exec-gate: …" refusal line)."""


# --- bytes and encodings -----------------------------------------------------------------------------------------

def canonical(value: Any) -> bytes:
    """The one JSON form the cabinet hashes (kd.admin_task.canonical)."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def b64url(text: Any) -> bytes:
    """base64url without padding, as WebAuthn JSON carries it; padding tolerated."""
    if not isinstance(text, str) or not B64URL.fullmatch(text):
        raise Deny("значение base64url повреждено")
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (ValueError, TypeError):
        raise Deny("значение base64url повреждено")


def _b64url_text(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def cbor_decode(data: bytes) -> Any:
    """CBOR of a COSE key: unsigned and negative integers, byte and text strings, arrays and maps with definite
    lengths; anything else — Deny (a COSE key from WebAuthn needs no more)."""
    value, end = _cbor_item(data, 0)
    if end != len(data):
        raise Deny("в CBOR лишние байты в конце")
    return value


def _cbor_item(data: bytes, at: int) -> Tuple[Any, int]:
    if at >= len(data):
        raise Deny("CBOR обрывается раньше времени")
    initial = data[at]
    major, info = initial >> 5, initial & 0x1F
    at += 1
    if info < 24:
        length = info
    elif info in (24, 25, 26, 27):
        size = 1 << (info - 24)
        if at + size > len(data):
            raise Deny("CBOR обрывается раньше времени")
        length = int.from_bytes(data[at:at + size], "big")
        at += size
    else:
        raise Deny("в CBOR неопределённая или зарезервированная длина")
    if major == 0:
        return length, at
    if major == 1:
        return -1 - length, at
    if major in (2, 3):
        if at + length > len(data):
            raise Deny("CBOR обрывается раньше времени")
        chunk = data[at:at + length]
        if major == 2:
            return bytes(chunk), at + length
        try:
            return chunk.decode("utf-8"), at + length
        except UnicodeDecodeError:
            raise Deny("текст в CBOR не в UTF-8")
    if major == 4:
        items = []
        for _ in range(length):
            item, at = _cbor_item(data, at)
            items.append(item)
        return items, at
    if major == 5:
        mapping = {}  # type: Dict[Any, Any]
        for _ in range(length):
            key, at = _cbor_item(data, at)
            if not isinstance(key, (int, str)):
                raise Deny("ключ словаря CBOR — не число и не текст")
            value, at = _cbor_item(data, at)
            mapping[key] = value
        return mapping, at
    raise Deny("тип CBOR %d не бывает в ключе COSE" % major)


def _der(tag: int, content: bytes) -> bytes:
    length = len(content)
    if length < 0x80:
        head = bytes([length])
    else:
        size = (length.bit_length() + 7) // 8
        head = bytes([0x80 | size]) + length.to_bytes(size, "big")
    return bytes([tag]) + head + content


def _der_int(raw: bytes) -> bytes:
    raw = raw.lstrip(b"\x00") or b"\x00"
    if raw[0] & 0x80:
        raw = b"\x00" + raw
    return _der(0x02, raw)


def _pem(spki: bytes) -> str:
    body = base64.b64encode(spki).decode("ascii")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


def cose_to_pem(cose: bytes) -> Tuple[str, str]:
    """A WebAuthn COSE public key as the SubjectPublicKeyInfo PEM openssl verifies with, and its algorithm:
    EC2 P-256 (ES256) or RSA (RS256); other keys — Deny."""
    key = cbor_decode(cose)
    if not isinstance(key, dict):
        raise Deny("ключ COSE — не словарь")
    kty, alg = key.get(1), key.get(3)
    if kty == 2 and alg == -7:
        x, y = key.get(-2), key.get(-3)
        if key.get(-1) != 1 or not isinstance(x, bytes) or not isinstance(y, bytes) or len(x) != 32 or len(y) != 32:
            raise Deny("ключ EC2 — не точка P-256")
        return _pem(EC_P256_SPKI + b"\x04" + x + y), "ES256"
    if kty == 3 and alg == -257:
        n, e = key.get(-1), key.get(-2)
        if not isinstance(n, bytes) or not isinstance(e, bytes) or len(n) < 256 or not e:
            raise Deny("ключ RSA повреждён или слишком короткий")
        algorithm = _der(0x30, RSA_OID + b"\x05\x00")
        public = _der(0x30, _der_int(n) + _der_int(e))
        return _pem(_der(0x30, algorithm + _der(0x03, b"\x00" + public))), "RS256"
    raise Deny("тип ключа подписанта не поддерживается (нужен ES256 или RS256)")


# --- the signed value ----------------------------------------------------------------------------------------------

def _hex64(value: Any, what: str) -> str:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise Deny("%s — не sha256 в hex" % what)
    return value


def challenge(decision: Dict[str, Any], payload: Dict[str, Any]) -> bytes:
    """The bytes the owner's passkey signed, recomputed the cabinet's way: the decision digest over the payload's
    hash, the card's revision, the person and the term; a batch — sha256 over the sorted digests of all its
    decisions; then sha256 with the challenge nonce."""
    for key in ("id", "user", "expires", "nonce"):
        if not isinstance(decision.get(key), str) or not decision[key]:
            raise Deny("в решении нет поля %s" % key)
    if "revision" not in decision or isinstance(decision["revision"], bool) \
            or not isinstance(decision["revision"], (int, str)):
        raise Deny("в решении не указана ревизия карточки")
    if not HEX32.fullmatch(decision["nonce"]):
        raise Deny("nonce вызова (challenge) повреждён")
    payload_sha = hashlib.sha256(canonical(payload)).hexdigest()
    text = "%s|%s|%s|%s|%s" % (decision["id"], payload_sha, decision["revision"], decision["user"],
                               decision["expires"])
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    digests = decision.get("digests")
    if digests is None:
        bound = digest
    else:
        if not isinstance(digests, list) or not 1 < len(digests) <= MAX_DIGESTS:
            raise Deny("дайджесты пакета решений повреждены")
        for item in digests:
            _hex64(item, "дайджест пакета")
        if digest.hex() not in digests:
            raise Deny("дайджеста решения (digest) нет в его пакете")
        bound = hashlib.sha256("".join(sorted(digests)).encode("ascii")).digest()
    return hashlib.sha256(bound + bytes.fromhex(decision["nonce"])).digest()


def openssl_verify(pem: str, data: bytes, signature: bytes, openssl: Optional[str] = None) -> bool:
    """``openssl dgst -sha256 -verify`` (the gate's own PATH) in a private temporary directory; True only on
    «Verified OK»."""
    openssl = openssl or openssl_path()
    folder = tempfile.mkdtemp(prefix="agent-exec-gate.")
    try:
        paths = {}
        for name, content in (("key.pem", pem.encode("ascii")), ("data.bin", data), ("sig.bin", signature)):
            path = os.path.join(folder, name)
            with open(path, "wb") as handle:
                handle.write(content)
            paths[name] = path
        proc = subprocess.run([openssl, "dgst", "-sha256", "-verify", paths["key.pem"], "-signature",
                               paths["sig.bin"], paths["data.bin"]],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    return proc.returncode == 0 and proc.stdout.strip() == b"Verified OK"


def openssl_version(openssl: Optional[str] = None) -> Optional[str]:
    openssl = openssl or openssl_path()
    try:
        proc = subprocess.run([openssl, "version"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else None


# --- the expected command --------------------------------------------------------------------------------------------

def package_tool() -> str:
    """The package manager of this host — what the stage's catalog says of it (apt or dnf)."""
    if which("apt-get"):
        return "apt"
    if which("dnf"):
        return "dnf"
    return ""


# an app step. The stage resolves it from its catalog and puts the words into the plan the owner signs;
# this
# host has no copy of that catalog, so it cannot derive them — it checks that they have the one shape an app step
# has (kd.admin_operations._app_step): an account of this server that is not uid 0, its directory through env -C,
# then the interpreter and the tool, or the flag helper with a switch. Anything else under these verbs is refused.
APP_FLAG_HELPER = "/usr/local/libexec/agent-app-flag"
UPGRADE_UNIT = ["systemd-run", "--unit=kd-package-upgrade", "--collect"]  # kd.admin_operations.UPGRADE_UNIT
# the helper of cron.line, file.drop and gate.script (agent_typed_op.py), and the switch it keeps: while
# the
# file exists this server runs no text through bash for a plan — no script step, no legacy command, no step observe
# would let through unchecked; typed steps, critical actions and the snapshot stay
TYPED_HELPER = "/usr/local/libexec/agent-typed-op"
SCRIPT_OFF = "/etc/agent-exec-gate/script-off"
SCRIPT_OFF_DENY = "шаги-скрипты на этом сервере выключены (gate.script off): только типовые шаги и критичные действия"
APP_DENY = "в шаге приложения нет команды из подписанного плана"
APP_USER = re.compile(r"[a-z_][a-z0-9_-]{0,31}\Z")
APP_DIR = re.compile(r"(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)+\Z")  # absolute, plain segments: no "." and no ".."
APP_WORD = re.compile(r"[A-Za-z0-9_.][A-Za-z0-9_./-]*\Z")  # the interpreter, the tool: no option, no NAME=value
APP_FILE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*\Z")
APP_SECTION = re.compile(r"[a-z][a-z0-9_]{0,40}\Z")
APP_SWITCH = re.compile(r"[a-z][a-z0-9_]{0,60}_enabled\Z")
APP_ENV_FILE = re.compile(r"\.?[A-Za-z0-9_][A-Za-z0-9._-]*\Z")  # one name, a leading dot allowed (.env)
APP_ENV_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_.]{0,80}\Z")
APP_ENV_VALUE = re.compile(r"[A-Za-z0-9_.-]{1,40}\Z")


def account_uid(name: str) -> Optional[int]:
    """The uid of an account of this server; None when there is no such account."""
    try:
        import pwd

        return pwd.getpwnam(name).pw_uid
    except (ImportError, KeyError):
        return None


def app_argv(verb: str, argv: Any) -> List[str]:
    if not isinstance(argv, list) or len(argv) < 9 or not all(isinstance(w, str) and w for w in argv):
        raise Deny(APP_DENY)
    if argv[:2] != ["runuser", "-u"] or argv[3:6] != ["--", "env", "-C"]:
        raise Deny(APP_DENY)
    user, where = argv[2], argv[6]
    uid = account_uid(user) if APP_USER.match(user) else None
    if uid is None or uid == 0 or not APP_DIR.match(where):  # root under any name is not an application
        raise Deny(APP_DENY)
    if verb == "app.flag":
        if len(argv) != 14 or argv[7:10] != ["python3", "-I", APP_FLAG_HELPER]:
            raise Deny(APP_DENY)
        ini = (APP_FILE.match(argv[10]) and APP_SECTION.match(argv[11]) and APP_SWITCH.match(argv[12])
               and argv[13] in ("0", "1"))
        # a switch of an env file — the helper's --env form, a key and one plain word
        env = (argv[10] == "--env" and APP_ENV_FILE.match(argv[11]) and APP_ENV_KEY.match(argv[12])
               and APP_ENV_VALUE.match(argv[13]))
        if not (ini or env):
            raise Deny(APP_DENY)
    else:
        for word in argv[7:9]:  # the interpreter and the tool
            if not APP_WORD.match(word) or ".." in word.split("/"):
                raise Deny(APP_DENY)
    return list(argv)


def typed_argv(verb: str, op: Dict[str, Any]) -> List[str]:
    """The helper's words for a cron.line, file.drop or gate.script step (kd.admin_operations.remote_argv), after
    this server's own policy has read the content: what the stage let through is read again here."""
    helper = ["python3", "-I", TYPED_HELPER]
    if verb == "gate.script":
        if op.get("value") not in ("on", "off"):
            raise Deny("переключатель скриптов — on или off")
        return helper + ["script", op["value"]]
    mark, action = op.get("mark"), op.get("action")
    if not isinstance(mark, str) or not TASK_ID.fullmatch(mark) or action not in ("set", "remove"):
        raise Deny("в шаге нет метки карточки или действия set|remove")
    policy = load_policy()
    key = "line" if verb == "cron.line" else "text"
    if (key in op) != (action == "set"):
        raise Deny("%s идёт только с set" % key)
    if verb == "cron.line":
        line = op.get("line")
        problem = policy.cron_problem(line) if action == "set" else None
        if problem:
            raise Deny(problem)
        return helper + ["cron", mark, action] + ([line] if action == "set" else [])
    folder, name, text = op.get("dir"), op.get("name"), op.get("text")
    problem = policy.drop_problem(folder, name, text if action == "set" else "# remove")  # remove: dir and name
    if problem:
        raise Deny(problem)
    return helper + ["file", folder, name, mark, action] + ([text] if action == "set" else [])


def expected_argv(op: Dict[str, Any], tool: str) -> List[str]:
    """The argv the stage forms for a typed step (kd.admin_operations.remote_argv), mirrored here."""
    verb = op.get("verb")
    if verb == "package.refresh":
        return ["apt-get", "-q", "update"] if tool == "apt" else ["dnf", "-q", "makecache"]
    if verb == "package.upgrade":  # in a transient unit of its own, no reboot
        if tool == "apt":
            return UPGRADE_UNIT + ["--setenv=NEEDRESTART_SUSPEND=1", "--setenv=DEBIAN_FRONTEND=noninteractive",
                                   "apt-get", "-q", "-y", "-o", "Dpkg::Options::=--force-confdef", "-o",
                                   "Dpkg::Options::=--force-confold", "full-upgrade"]
        return UPGRADE_UNIT + ["dnf", "-q", "-y", "upgrade"]
    if verb in ("service.restart", "service.start", "service.reload"):
        unit = op.get("unit")
        if not isinstance(unit, str) or not unit:
            raise Deny("в шаге не указан юнит")
        return ["systemctl", verb.split(".")[1], unit]
    if verb in ("cron.line", "file.drop", "gate.script"):
        return typed_argv(verb, op)
    if verb in ("app.tool", "app.flag"):
        # the stage resolves an app step from its catalog and puts the argv into the plan the owner signs;
        # this host needs no copy of the catalog — only that the words are the application's own account's
        return app_argv(verb, op.get("argv"))
    if verb == "package.install":
        packages = op.get("packages")
        if not isinstance(packages, list) or not packages or not all(
                isinstance(p, dict) and isinstance(p.get("name"), str) and isinstance(p.get("version"), str)
                for p in packages):
            raise Deny("в шаге не указаны пакеты")
        if tool == "apt":
            return (["env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "-q", "-y", "-o",
                     "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold", "--no-remove",
                     "install"] + ["%s=%s" % (p["name"], p["version"]) for p in packages])
        return ["dnf", "-q", "-y", "install"] + ["%s-%s" % (p["name"], p["version"]) for p in packages]
    raise Deny("неизвестный глагол шага %r" % (verb,))


def quote_argv(argv: List[str]) -> str:
    """``shlex.join`` of Python 3.8+, which the stage uses (kd.admin_operations.remote_command)."""
    return " ".join(shlex.quote(word) for word in argv)


def check_step_command(op: Dict[str, Any], command: str) -> Optional[List[str]]:
    """A script step — exactly its text (None: bash runs it); a typed step — exactly the quoted argv the stage forms,
    compared as text, not its shlex reading (``bash -c '…'`` or ``$(…)`` may split to the same words), and that argv
    is what runs — without a shell."""
    if op.get("verb") == "script":
        if command != op.get("script"):
            raise Deny("команда — не текст шага-скрипта из подписанного плана")
        return None
    argv = expected_argv(op, package_tool())
    if command != quote_argv(argv):
        raise Deny("команда — не команда типового шага из подписанного плана")
    return argv


# --- the test zone
# ----------------------------------------------------------------------------------------
# This server's own list of test environments: root's file, put here by deploy_target.sh from the owner's machine.
# A plan step that fits an entry word for word runs without the owner's signature — the list is the boundary, so a
# factory host that holds the exec key can run what the list names and nothing more. No file — no zone.
TEST_ENVS_JSON = "/etc/agent-exec-gate/test-envs.json"
TEST_ENVS_OWNER = 0
MAX_TEST_ENVS = 64 * 1024
ENV_NAME = re.compile(r"[a-z][a-z0-9-]{0,40}\Z")
ENV_KEYS = frozenset(("user", "dir", "python", "tools", "php", "spark", "flags", "reload", "read"))
ENV_PHP = re.compile(r"php(?:[0-9]\.[0-9])?\Z")
ENV_SPARK = re.compile(r"[a-z][a-z0-9:_-]{0,60}\Z")
ENV_UNIT = re.compile(r"[A-Za-z0-9][A-Za-z0-9@._-]{0,120}\.service\Z")
ENV_ARG = re.compile(r"[A-Za-z0-9_.:=@/,+-]{1,200}\Z")  # one word of a tool's command line (the stage's APP_ARG)
# a directory of the environment the read-only gate's account reads whole — relative, plain segments
ENV_READ = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*(?:/[A-Za-z0-9_][A-Za-z0-9._-]*)*\Z")
MAX_ENV_READS = 10
ENV_BAD = "список тестовых окружений повреждён"


def _words(value: Any, pattern: Any) -> bool:
    return isinstance(value, list) and bool(value) and all(isinstance(w, str) and pattern.match(w) for w in value)


def _plain_path(word: Any) -> bool:
    return isinstance(word, str) and bool(APP_WORD.match(word)) and ".." not in word.split("/")


def _test_entry(name: Any, entry: Any) -> Dict[str, Any]:
    if not isinstance(name, str) or not ENV_NAME.match(name) or not isinstance(entry, dict) \
            or not {"user", "dir"} <= set(entry) or not set(entry) <= ENV_KEYS:
        raise Deny(ENV_BAD)
    user, where = entry["user"], entry["dir"]
    if not isinstance(user, str) or not APP_USER.match(user) or user == "root" \
            or not isinstance(where, str) or not APP_DIR.match(where):
        raise Deny(ENV_BAD)
    pythons, phps = {"python", "tools"} & set(entry), {"php", "spark"} & set(entry)
    if len(pythons) == 1 or len(phps) == 1 or not (pythons or phps):
        raise Deny(ENV_BAD)
    if pythons and not (_plain_path(entry["python"]) and isinstance(entry["tools"], list) and entry["tools"]
                        and all(_plain_path(tool) for tool in entry["tools"])):
        raise Deny(ENV_BAD)
    if phps and not (isinstance(entry["php"], str) and ENV_PHP.match(entry["php"])
                     and _words(entry["spark"], ENV_SPARK)):
        raise Deny(ENV_BAD)
    flags = entry.get("flags")
    if flags is not None:
        if not isinstance(flags, dict) or not isinstance(flags.get("file"), str):
            raise Deny(ENV_BAD)
        if set(flags) == {"file", "format", "sections"} and flags["format"] == "ini":
            if not APP_FILE.match(flags["file"]) or not _words(flags["sections"], APP_SECTION):
                raise Deny(ENV_BAD)
        elif set(flags) == {"file", "format", "keys"} and flags["format"] == "env":
            keys = flags["keys"]
            if not APP_ENV_FILE.match(flags["file"]) or not isinstance(keys, dict) or not keys or not all(
                    isinstance(key, str) and APP_ENV_KEY.match(key) and _words(values, APP_ENV_VALUE)
                    for key, values in keys.items()):
                raise Deny(ENV_BAD)
        else:
            raise Deny(ENV_BAD)
    if "reload" in entry and not _words(entry["reload"], ENV_UNIT):
        raise Deny(ENV_BAD)
    if "read" in entry and not (_words(entry["read"], ENV_READ) and len(entry["read"]) <= MAX_ENV_READS):
        raise Deny(ENV_BAD)
    return entry


def test_env_reads(envs: Dict[str, Dict[str, Any]]) -> List[str]:
    """The absolute directories a list opens to the read-only gate's account, sorted, each once."""
    return sorted({entry["dir"] + "/" + read for entry in envs.values() for read in entry.get("read", ())})


def validate_test_envs(data: Any) -> Dict[str, Dict[str, Any]]:
    """The entries of a list ``{"schema_version": 1, "envs": {name: entry}}``; Deny when any part of it is not the
    shape a catalog's test entry has — a list is taken whole or not at all."""
    if not isinstance(data, dict) or set(data) != {"schema_version", "envs"} or data["schema_version"] != 1 \
            or isinstance(data["schema_version"], bool) or not isinstance(data["envs"], dict):
        raise Deny(ENV_BAD)
    return {name: _test_entry(name, entry) for name, entry in data["envs"].items()}


def load_test_envs(path: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """The installed list: root's regular file nobody else can write. A missing file is no zone; a file of another
    owner, behind a link, writable by others or malformed is ignored whole, with one warning in the log."""
    path = TEST_ENVS_JSON if path is None else path
    try:
        info = os.lstat(path)
    except OSError:
        return {}
    try:
        if not stat.S_ISREG(info.st_mode):
            raise Deny("не обычный файл")
        if os.name == "posix" and (info.st_uid != TEST_ENVS_OWNER or info.st_mode & 0o022):
            raise Deny("файл не только root'а")
        with open(path, "rb") as handle:
            raw = handle.read(MAX_TEST_ENVS + 1)
        if len(raw) > MAX_TEST_ENVS:
            raise Deny("файл слишком большой")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            raise Deny("не JSON")
        return validate_test_envs(data)
    except (Deny, OSError) as exc:
        _log("test-envs ignored (%s): %s" % (exc, path), syslog.LOG_WARNING if syslog else None)
        return {}


def test_env(op: Any, envs: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """The name of the entry a plan step fits word for word, or None: the entry's account and directory, then one
    of its tools or spark commands (the rest — plain argument words), one of its switches with a value it lists, or
    a reload of a unit it lists."""
    if not isinstance(op, dict) or not envs:
        return None
    verb = op.get("verb")
    if verb == "service.reload":
        unit = op.get("unit")
        for name in sorted(envs):
            if isinstance(unit, str) and unit in envs[name].get("reload", ()):
                return name
        return None
    argv = op.get("argv")
    if verb not in ("app.tool", "app.flag") or not isinstance(argv, list) \
            or not all(isinstance(word, str) for word in argv):
        return None
    for name in sorted(envs):
        entry = envs[name]
        if argv[:7] != ["runuser", "-u", entry["user"], "--", "env", "-C", entry["dir"]]:
            continue
        rest = argv[7:]
        if verb == "app.tool" and _fits_tool(rest, entry):
            return name
        if verb == "app.flag" and _fits_flag(rest, entry.get("flags")):
            return name
    return None


def _fits_tool(rest: List[str], entry: Dict[str, Any]) -> bool:
    if "python" in entry and len(rest) >= 2 and rest[0] == entry["python"] and rest[1] in entry["tools"]:
        return all(ENV_ARG.match(word) for word in rest[2:])
    if "php" in entry and len(rest) >= 3 and rest[:2] == [entry["php"], "spark"] and rest[2] in entry["spark"]:
        return all(ENV_ARG.match(word) for word in rest[3:])
    return False


def _fits_flag(rest: List[str], flags: Any) -> bool:
    if not isinstance(flags, dict) or len(rest) != 7 or rest[:3] != ["python3", "-I", APP_FLAG_HELPER]:
        return False
    if flags["format"] == "ini":
        return (rest[3] == flags["file"] and rest[4] in flags["sections"] and bool(APP_SWITCH.match(rest[5]))
                and rest[6] in ("0", "1"))
    return rest[3] == "--env" and rest[4] == flags["file"] and rest[6] in flags["keys"].get(rest[5], ())


def server_test_envs(catalog: Any, alias: str) -> Dict[str, Any]:
    """The list of one server out of the catalog (kd.admin_operations.test_envs, mirrored: this file runs alone on
    the owner's machine and on targets); Deny for a catalog whose test entries are not well formed."""
    apps = catalog.get("apps", {}) if isinstance(catalog, dict) else None
    if not isinstance(apps, dict):
        raise Deny("каталог повреждён: apps — не объект")
    envs = {}
    for name in sorted(apps, key=str):
        entry = apps[name]
        if not isinstance(entry, dict) or not isinstance(entry.get("test", False), bool):
            raise Deny("каталог повреждён: запись %s" % _plain(name))
        if not entry.get("test") or entry.get("server") != alias:
            continue
        env = {key: entry[key] for key in ("user", "dir") if key in entry}
        for interpreter, tools in (("python", "tools"), ("php", "spark")):
            if interpreter in entry or tools in entry:
                env[interpreter] = entry.get(interpreter)
                env[tools] = sorted(entry[tools]) if _strings(entry.get(tools)) else entry.get(tools)
        flags = entry.get("flags")
        if isinstance(flags, dict) and "keys" in flags:
            keys = flags["keys"]
            env["flags"] = {"file": flags.get("file"), "format": flags.get("format"),
                            "keys": ({key: sorted(values) if _strings(values) else values
                                      for key, values in keys.items()} if isinstance(keys, dict) else keys)}
        elif isinstance(flags, dict):
            sections = flags.get("sections")
            env["flags"] = {"file": flags.get("file"), "format": "ini",
                            "sections": sorted(sections) if _strings(sections) else sections}
        elif flags is not None:
            env["flags"] = flags
        if entry.get("reload"):
            env["reload"] = sorted(entry["reload"]) if _strings(entry["reload"]) else entry["reload"]
        if entry.get("read"):
            env["read"] = sorted(entry["read"]) if _strings(entry["read"]) else entry["read"]
        envs[name] = env
    data = {"schema_version": 1, "envs": envs}
    validate_test_envs(data)
    return data


def _strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


# --- verification ---------------------------------------------------------------------------------------------------

def _moment(value: Any, what: str) -> float:
    if not isinstance(value, str):
        raise Deny("%s — не время" % what)
    try:
        return float(calendar.timegm(time.strptime(value, TIME_FORMAT)))
    except ValueError:
        raise Deny("%s — не время" % what)


def _check_times(grant: Dict[str, Any], decision: Dict[str, Any], now: float, max_ttl: int) -> None:
    """The grant's times are the cabinet's, not signed; the signed term of the challenge (``decision.expires``)
    bounds them: the approval cannot postdate it, so with the kind's longest life the grant ends at a moment the
    owner's signature fixes — rewritten times cannot replay an old envelope."""
    approved = _moment(grant.get("approved_at"), "approved_at одобрения")
    expires = _moment(grant.get("expires_at"), "expires_at одобрения")
    term = _moment(decision.get("expires"), "подписанный срок")
    if approved > now + SKEW:
        raise Deny("одобрение выдано в будущем (%s): проверь часы этого сервера" % grant["approved_at"])
    if approved > term:
        raise Deny("одобрение выдано после срока его подписи (%s > %s)" % (grant["approved_at"],
                                                                         decision["expires"]))
    if now >= expires:
        raise Deny("одобрение истекло в %s" % grant["expires_at"])
    if not 0 < expires - approved <= max_ttl:
        raise Deny("одобрение живёт дольше допустимого (не больше %d с)" % max_ttl)


def once_key(decision: Any) -> str:
    """The once-per-server key of a critical action: the signed decision's id and nonce (the grant's action_id is
    not signed, a fresh one must not buy a second run); "" when the decision has none."""
    if not isinstance(decision, dict) or not all(isinstance(decision.get(k), str) and decision[k]
                                                 for k in ("id", "nonce")):
        return ""
    return hashlib.sha256(("%s|%s" % (decision["id"], decision["nonce"])).encode("utf-8")).hexdigest()


def _check_assertion(assertion: Any, decision: Dict[str, Any], payload: Dict[str, Any], gate: Dict[str, Any],
                     signers: Dict[str, Any], verify_sig: Callable[[str, bytes, bytes], bool]) -> str:
    """The owner's passkey assertion over this decision's challenge; the signer's credential id."""
    if assertion is None:
        raise Deny("одобрению нужна подпись владельца passkey")
    response = assertion.get("response") if isinstance(assertion, dict) else None
    if (not isinstance(response, dict) or assertion.get("type") != "public-key"
            or not isinstance(assertion.get("rawId"), str)
            or not all(isinstance(response.get(k), str) for k in RESPONSE)):
        raise Deny("подпись passkey (assertion) повреждена")
    client_bytes = b64url(response["clientDataJSON"])
    try:
        client = json.loads(client_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Deny("clientDataJSON — не JSON")
    if not isinstance(client, dict) or client.get("type") != "webauthn.get":
        raise Deny("тип подписи passkey — не webauthn.get")
    if client.get("origin") != gate.get("origin"):
        raise Deny("подпись passkey сделана не на странице кабинета (origin)")
    if b64url(client.get("challenge")) != challenge(decision, payload):
        raise Deny("подпись покрывает другой вызов (challenge): этот шаг владелец не подписывал")
    auth = b64url(response["authenticatorData"])
    if len(auth) < 37:
        raise Deny("authenticatorData слишком короткие")
    if auth[:32] != hashlib.sha256(gate.get("rp_id", "").encode("utf-8")).digest():
        raise Deny("подпись passkey для другого rp id")
    flags = auth[32]
    if not flags & 0x01:
        raise Deny("в подписи passkey нет присутствия пользователя")
    if not flags & 0x04:
        raise Deny("в подписи passkey нет проверки пользователя")
    entries = signers.get("signers") if isinstance(signers, dict) else None
    if not isinstance(entries, list):
        raise Deny("реестр подписантов (signers) повреждён")
    user = decision.get("user")
    match = [s for s in entries if isinstance(s, dict) and s.get("credential_id") == assertion["rawId"]
             and s.get("user") == user]
    if len(match) != 1:
        raise Deny("ключ — не ключ подписи %s на этом сервере" % user)
    pem, _ = cose_to_pem(b64url(match[0].get("public_key")))
    signed = auth + hashlib.sha256(client_bytes).digest()
    if not verify_sig(pem, signed, b64url(response["signature"])):
        raise Deny("подпись не сходится")
    return assertion["rawId"]


def verify(envelope: Any, gate: Dict[str, Any], signers: Dict[str, Any], now: float,
           verify_sig: Callable[[str, bytes, bytes], bool] = openssl_verify,
           test_envs: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The checked operation ``{"kind", "task_id", "step", "command", "argv", "action_id", "once", "test_env"}``
    (``argv`` — the words a typed plan step runs without a shell, None for a script or an action; ``once`` —
    the action's once-per-server key from the signed decision, "" for a plan step; ``test_env`` — the entry of this
    server's list an unsigned plan step ran by, "" for a signed one); Deny otherwise — a nesting too deep to hash
    or decode too (RecursionError would end the gate with exit 1, not a refusal)."""
    try:
        return _verify(envelope, gate, signers, now, verify_sig, test_envs)
    except RecursionError:
        raise Deny(TOO_DEEP)


def _verify(envelope: Any, gate: Dict[str, Any], signers: Dict[str, Any], now: float,
            verify_sig: Callable[[str, bytes, bytes], bool],
            test_envs: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Any]:
    if not isinstance(envelope, dict) or not KEYS <= set(envelope) or set(envelope) - KEYS - {"step"}:
        raise Deny("поля конверта не совпадают со схемой")
    if envelope["schema_version"] != 1:
        raise Deny("неизвестная схема конверта")
    kind = envelope["kind"]
    if kind not in ("plan", "action") or ("step" in envelope) != (kind == "plan"):
        raise Deny("неизвестный вид конверта")
    task_id, server, command = envelope["task_id"], envelope["server"], envelope["command"]
    if not isinstance(task_id, str) or not TASK_ID.fullmatch(task_id) or not isinstance(server, str) \
            or not isinstance(command, str):
        raise Deny("в конверте не указаны задача, сервер или команда")
    payload, proposal, grant, decision = (envelope["payload"], envelope["proposal"], envelope["grant"],
                                          envelope["decision"])
    if not all(isinstance(part, dict) for part in (payload, proposal, grant, decision)):
        raise Deny("части конверта повреждены")
    alias = gate.get("alias")
    if server != alias:
        raise Deny("конверт для сервера %s, а это %s" % (server, alias))
    if set(payload) != (PLAN_PAYLOAD if kind == "plan" else ACTION_PAYLOAD) or payload["task_id"] != task_id:
        raise Deny("содержимое решения (payload) не совпадает с конвертом")
    _hex64(payload["card_scope_sha256"], "card_scope_sha256")
    if hashlib.sha256(canonical(proposal)).hexdigest() != _hex64(payload["proposal_sha256"], "proposal_sha256"):
        raise Deny("предложение (proposal) — не то, что подписал владелец")
    if proposal.get("task_id") != task_id:
        raise Deny("предложение для другой задачи")
    action_id, zone, argv = "", None, None
    if kind == "plan":
        step, operations = envelope["step"], proposal.get("operations")
        if isinstance(step, bool) or not isinstance(step, int) or not isinstance(operations, list) \
                or not 1 <= step <= len(operations) or not isinstance(operations[step - 1], dict):
            raise Deny("шага с таким номером в плане нет")
        op = operations[step - 1]
        if op.get("server") != alias:
            raise Deny("шаг %d для сервера %s, а это %s" % (step, op.get("server"), alias))
        argv = check_step_command(op, command)
        if envelope["assertion"] is None:
            # an unsigned step of this server's own list. Its plan, its command and its server are checked
            # above; the grant's times are attested by nothing here — the factory host keeps them
            zone = test_env(op, test_envs or {})
    else:
        step = None
        if payload["proposal_id"] != payload["proposal_sha256"][:16]:
            raise Deny("id критичного действия — не его хэш")
        servers = proposal.get("servers")
        if not isinstance(servers, list) or alias not in servers:
            raise Deny("в подписанном действии нет сервера %s" % alias)
        if command != proposal.get("command"):
            raise Deny("команда — не команда подписанного действия")
        action_id = grant.get("action_id")
        if not isinstance(action_id, str) or not SAFE_ID.fullmatch(action_id):
            raise Deny("у одобрения действия нет id")
    if zone is None:
        _check_assertion(envelope["assertion"], decision, payload, gate, signers, verify_sig)
        _check_times(grant, decision, now, PLAN_MAX_TTL if kind == "plan" else ACTION_MAX_TTL)
    return {"kind": kind, "task_id": task_id, "step": step, "command": command, "argv": argv, "action_id": action_id,
            "once": once_key(decision) if kind == "action" else "", "test_env": zone or ""}


def take_once(used_dir: str, key: str) -> None:
    """A critical command runs once on this server: its key (``once_key`` of the signed decision) becomes a file;
    a second run — Deny, an unwritable directory — Deny (fail closed)."""
    if not isinstance(key, str) or not SAFE_ID.fullmatch(key):
        raise Deny("ключ однократности действия повреждён")
    path = os.path.join(used_dir, key)
    try:
        os.makedirs(used_dir, mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise Deny("действие уже выполнено на этом сервере")
    except OSError as exc:
        raise Deny("не удалось записать, что действие выполнено (%s)" % exc.__class__.__name__)
    with os.fdopen(fd, "w") as handle:
        handle.write(time.strftime(TIME_FORMAT, time.gmtime()) + "\n")


def _plan_base(decision_id: Any, nonce: Any, proposal_sha256: Any) -> str:
    if not all(isinstance(v, str) and v for v in (decision_id, nonce)) or not isinstance(proposal_sha256, str) \
            or not HEX64.fullmatch(proposal_sha256):
        return ""
    return "plan|%s|%s|%s" % (decision_id, nonce, proposal_sha256)


def plan_key(decision_id: Any, nonce: Any, proposal_sha256: Any, step: Any) -> str:
    """The journal key of step ``step`` of a signed plan (kd.exec_envelope.plan_key): the signed decision's id and
    nonce and the plan's hash; "" when one is missing — an unsigned step keeps no mark."""
    base = _plan_base(decision_id, nonce, proposal_sha256)
    if not base or isinstance(step, bool) or not isinstance(step, int) or step < 1:
        return ""
    return hashlib.sha256(("%s|%d" % (base, step)).encode("utf-8")).hexdigest()


def plan_lock(decision_id: Any, nonce: Any, proposal_sha256: Any) -> str:
    """The lock file of a signed plan's runs on this server."""
    base = _plan_base(decision_id, nonce, proposal_sha256)
    return hashlib.sha256(base.encode("utf-8")).hexdigest() + ".lock" if base else ""


def plan_steps(envelope: Any, alias: Any) -> Optional[Tuple[int, str, List[Tuple[int, str]], str]]:
    """(the step, its key, the plan's earlier steps on this server with their keys, the plan's lock) of a plan
    envelope that names its signed decision; None otherwise (an action, an unsigned test-zone step). Total: in
    observe a step that failed its check keeps the marks too, as an action's once-mark."""
    if not isinstance(envelope, dict) or envelope.get("kind") != "plan":
        return None
    decision, payload, proposal = envelope.get("decision"), envelope.get("payload"), envelope.get("proposal")
    if not all(isinstance(part, dict) for part in (decision, payload, proposal)):
        return None
    step, operations = envelope.get("step"), proposal.get("operations")
    parts = (decision.get("id"), decision.get("nonce"), payload.get("proposal_sha256"))
    key = plan_key(*parts, step)
    if not key or not isinstance(operations, list) or step > len(operations):
        return None
    earlier = [(n, plan_key(*parts, n)) for n in range(1, step)
               if isinstance(operations[n - 1], dict) and operations[n - 1].get("server") == alias]
    return step, key, earlier, plan_lock(*parts)


def step_done(used_dir: str, key: str) -> bool:
    """The step's mark says it succeeded; OSError other than a missing mark — the caller refuses (fail closed)."""
    try:
        with open(os.path.join(used_dir, key), "rb") as handle:
            return handle.read(64).startswith(b"ok ")
    except FileNotFoundError:
        return False


def begin_step(used_dir: str, steps: Tuple[int, str, List[Tuple[int, str]], str]) -> Any:
    """Before a signed plan step: the plan's lock (one run of a plan at a time on a server), no success of this step
    yet, a success of every earlier step of the plan on this server; the held lock, Deny otherwise."""
    step, key, earlier, lock = steps
    handle = None
    try:
        os.makedirs(used_dir, mode=0o700, exist_ok=True)
        handle = open(os.path.join(used_dir, lock), "a")
        if fcntl is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Deny("шаг этого плана уже выполняется на этом сервере")
        if step_done(used_dir, key):
            raise Deny("шаг %d этого плана уже выполнен на этом сервере: повтор — только новым планом с подписью "
                       "владельца" % step)
        for number, earlier_key in earlier:
            if not step_done(used_dir, earlier_key):
                raise Deny("шаг %d этого плана на этом сервере ещё не выполнен успешно: сначала он" % number)
        return handle
    except Deny:
        if handle is not None:
            handle.close()
        raise
    except OSError as exc:
        if handle is not None:
            handle.close()
        raise Deny("журнал шагов плана не прочитан (%s)" % exc.__class__.__name__)


def finish_step(used_dir: str, key: str, code: int) -> None:
    """After the run: the step's result, "ok" or its exit code (a failed step may run again), replaced atomically;
    OSError to the caller."""
    text = "%s %s\n" % ("ok" if code == 0 else "rc %d" % code, time.strftime(TIME_FORMAT, time.gmtime()))
    fd, tmp = tempfile.mkstemp(dir=used_dir, prefix=".step-")
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(text)
        os.replace(tmp, os.path.join(used_dir, key))
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- the policy
# -------------------------------------------------------------------------------------------

_POLICY = {}  # type: Dict[str, Any]


def load_policy(path: Optional[str] = None) -> Any:
    """The policy module from its installed file, compiled from its source — never from a cached .pyc, which
    /usr/local/bin (writable by the staff group on Ubuntu) could hold; once per process. A file that is missing or
    does not compile — Deny: without the policy nothing runs (fail closed)."""
    path = POLICY_PATH if path is None else path
    if path in _POLICY:
        return _POLICY[path]
    try:
        with open(path, "rb") as handle:
            source = handle.read()
        module = types.ModuleType("agent_exec_policy")
        module.__file__ = path
        sys.modules[module.__name__] = module  # typing.NamedTuple reads its defining module
        exec(compile(source, path, "exec"), module.__dict__)  # noqa: S102 - the root-owned policy file, not input
        for name in ("protect", "plan_step", "ALL_UNITS", "cron_problem", "drop_problem"):
            getattr(module, name)
    except Exception as exc:  # noqa: BLE001 - any failure to load is a refusal
        sys.modules.pop("agent_exec_policy", None)
        raise Deny("политика шлюза (%s) не загружается: %s" % (path, exc.__class__.__name__))
    _POLICY[path] = module
    return module


def site_units(gate: Dict[str, Any]) -> Tuple[str, ...]:
    """The deployment's site units of gate.json (``critical_units``, deploy_target.sh puts the catalog's list
    there); none or a malformed list — every unit's stop is critical (stricter)."""
    units = gate.get("critical_units")
    if isinstance(units, list) and all(isinstance(u, str) and SAFE_UNIT.fullmatch(u) for u in units):
        return tuple(units)
    return ("*",)


class PolicyDeny(Deny):
    """A refusal of the policy: it holds in both modes."""


def check_policy(gate: Dict[str, Any], command: Any, envelope: Any = None) -> None:
    """PolicyDeny when the policy refuses the command, cannot be loaded or fails; a command that is not text is left
    to the envelope's own checks (nothing runs it)."""
    if not isinstance(command, str):
        return
    try:
        refusal = policy_refusal(gate, command, envelope)
    except Deny as exc:
        raise PolicyDeny(str(exc))
    except Exception as exc:  # noqa: BLE001 - a failing check refuses
        raise PolicyDeny("проверка политики завершилась ошибкой (%s)" % exc.__class__.__name__)
    if refusal:
        raise PolicyDeny(refusal)


def policy_refusal(gate: Dict[str, Any], command: str, envelope: Any = None) -> Optional[str]:
    """The refusal's text or None: rule 1 for every command; rule 2 too for an envelope that is not a critical
    action (an action is critical by its kind; a legacy command, ``envelope`` None, carries no kind to match)."""
    policy = load_policy()
    if not isinstance(envelope, dict) or envelope.get("kind") == "action":
        return policy.protect(command)
    step = envelope.get("step")
    number = step if isinstance(step, int) and not isinstance(step, bool) else 0
    return policy.plan_step(number, command, site_units(gate))


# --- running -----------------------------------------------------------------------------------------------------

def run(command: Any, as_root: bool, timeout: float = TIMEOUT) -> int:
    """``bash -c command`` for a text, the words themselves for a typed step's argv (no shell reads them),
    as root (``sudo -n`` when the gate runs under another user), stdin closed, output to the caller; the program is
    found on the gate's own PATH (127 when there is none); the process group is stopped at ``timeout`` (SIGTERM,
    SIGKILL after 3 s) — exit 124 then."""
    words = list(command) if isinstance(command, list) else ["/bin/bash", "-c", command]
    if isinstance(command, list) and as_root:
        found = shutil.which(words[0], path=SAFE_PATH)
        if found is None:
            return 127
        words[0] = found
    argv = ([] if as_root else ["sudo", "-n"]) + words
    env = {"PATH": SAFE_PATH, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
           "HOME": "/root" if as_root else os.environ.get("HOME", "/"), "DEBIAN_FRONTEND": "noninteractive"}
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 10)):
            try:
                os.killpg(proc.pid, sig)
            except OSError:
                pass
            try:
                proc.wait(timeout=wait)
                break
            except subprocess.TimeoutExpired:
                continue
        return 124


def snapshot_script(nonce: str) -> str:
    """The plan window's snapshot (sysadmin/plan_window._script): each part, then its marker with the exit code."""
    return "; ".join(
        "%s; printf '\\n@@kd-window-%s %s rc=%%s\\n' \"$?\"" % (command, nonce, part) for part, command in SNAPSHOT_PARTS
    )


# --- main ------------------------------------------------------------------------------------------------------

def _safe(text: str) -> str:
    return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")


def _log(message: str, level: Optional[int] = None) -> None:
    if syslog is not None:
        syslog.openlog("agent-exec-gate", 0, syslog.LOG_AUTHPRIV)
        syslog.syslog(syslog.LOG_INFO if level is None else level, _safe(message)[:1000])


def _write(stream, text: str) -> None:
    stream.write(text.encode("utf-8", "surrogateescape"))
    stream.flush()


def _read_json(path: str, what: str) -> Dict[str, Any]:
    try:
        with open(path, "rb") as handle:
            data = json.loads(handle.read(MAX_ENVELOPE).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise Deny("%s (%s) нет или не читается: %s" % (what, path, exc.__class__.__name__))
    if not isinstance(data, dict):
        raise Deny("%s (%s) — не объект JSON" % (what, path))
    return data


def load_gate(path: Optional[str] = None) -> Dict[str, Any]:
    gate = _read_json(GATE_JSON if path is None else path, "настройки шлюза")
    if gate.get("mode") not in MODES or not all(isinstance(gate.get(k), str) and gate[k]
                                               for k in ("alias", "rp_id", "origin")):
        raise Deny("в настройках шлюза нужны alias, rp_id, origin и mode observe|enforce")
    return gate


def load_signers(path: Optional[str] = None) -> Dict[str, Any]:
    signers = _read_json(SIGNERS_JSON if path is None else path, "реестр подписантов (signers)")
    if signers.get("schema_version") != 1 or not isinstance(signers.get("signers"), list):
        raise Deny("реестр подписантов (signers, %s) повреждён" % (SIGNERS_JSON if path is None else path))
    return signers


def parse_envelope(raw: bytes) -> Dict[str, Any]:
    if len(raw) > MAX_ENVELOPE:
        raise Deny("конверт слишком большой (не больше %d байт)" % MAX_ENVELOPE)
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise Deny("конверт — не JSON")
    except RecursionError:
        raise Deny(TOO_DEEP)
    if not isinstance(envelope, dict):
        raise Deny("конверт — не объект JSON")
    return envelope


def _read_stdin(stdin) -> bytes:
    chunks, size = [], 0
    while size <= MAX_ENVELOPE:
        chunk = stdin.read(MAX_ENVELOPE + 1 - size)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
    return b"".join(chunks)


def _is_root() -> bool:
    geteuid = getattr(os, "geteuid", None)
    return geteuid() == 0 if geteuid is not None else False


def _check(argv: List[str], out, err) -> int:
    """``--check``: settings, registry, openssl and the policy file; ``--check-signers [<file>]``: every key of the
    registry (the named file — the installer's candidate on a target that may have no settings yet) opens as a
    public key."""
    try:
        if len(argv) == 2:
            signers = load_signers(argv[1])
        else:
            gate = load_gate()
            signers = load_signers()
    except Deny as exc:
        _write(err, "agent-exec-gate: %s\n" % exc)
        return 1
    if argv[0] == "--check-signers":
        named = []
        for entry in signers["signers"]:
            try:
                _, alg = cose_to_pem(b64url(entry.get("public_key")))
            except Deny as exc:
                _write(err, "agent-exec-gate: signer %s of %s: %s\n" % (entry.get("name"), entry.get("user"), exc))
                return 1
            named.append("%s: %s %s" % (entry.get("user"), entry.get("name"), alg))
        _write(out, "signers: %d (%s)\n" % (len(named), ", ".join(named)))
        return 0
    version = openssl_version()
    try:
        load_policy()
        policy = "ok (%s)" % POLICY_PATH
    except Deny as exc:
        policy = "MISSING: %s" % exc
    envs = sorted(load_test_envs())
    _write(out, "mode: %s\nalias: %s\nsigners: %d\nopenssl: %s\npolicy: %s\ntest-envs: %s\n"
           % (gate["mode"], gate["alias"], len(signers["signers"]), version or "missing", policy,
              "%d (%s)" % (len(envs), ", ".join(envs)) if envs else "0"))
    return 0 if version and policy.startswith("ok") else 1


def _plain(value: Any) -> str:
    """A field for the log line: text or a number as is, anything else by its type (a deep list would recurse)."""
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return str(value)[:100]
    return "?" if value is None else type(value).__name__


def _describe(envelope: Any) -> str:
    if not isinstance(envelope, dict):
        return "malformed envelope"
    parts = [_plain(envelope.get("task_id")), _plain(envelope.get("kind"))]
    if "step" in envelope:
        parts.append("step %s" % _plain(envelope["step"]))
    if isinstance(envelope.get("grant"), dict) and envelope["grant"].get("action_id"):
        parts.append("action %s" % _plain(envelope["grant"]["action_id"]))
    parts.append(_plain(envelope.get("server")))
    return " ".join(parts)


def main(argv=None, environ=None, stdin=None, out=None, err=None, now: Callable[[], float] = time.time,
         verify_sig: Callable[[str, bytes, bytes], bool] = openssl_verify) -> int:
    argv = sys.argv[1:] if argv is None else argv
    environ = os.environ if environ is None else environ
    stdin = sys.stdin.buffer if stdin is None else stdin
    out = sys.stdout.buffer if out is None else out
    err = sys.stderr.buffer if err is None else err
    if argv in (["--check"], ["--check-signers"]) or (len(argv) == 2 and argv[0] == "--check-signers"):
        return _check(argv, out, err)
    if len(argv) == 2 and argv[0] == "--check-test-envs":
        # the installer's candidate: the number of its entries, or exit 1 for a list the gate would ignore
        try:
            with open(argv[1], "rb") as handle:
                count = len(validate_test_envs(parse_envelope(handle.read(MAX_TEST_ENVS + 1))))
        except (Deny, OSError) as exc:
            _write(err, "agent-exec-gate: %s\n" % exc)
            return 1
        _write(out, "%d\n" % count)
        return 0
    if len(argv) == 2 and argv[0] == "--test-env-reads":
        # the installer: the directories a list opens to the read-only gate's account, one per line
        try:
            with open(argv[1], "rb") as handle:
                reads = test_env_reads(validate_test_envs(parse_envelope(handle.read(MAX_TEST_ENVS + 1))))
        except (Deny, OSError) as exc:
            _write(err, "agent-exec-gate: %s\n" % exc)
            return 1
        _write(out, "".join(path + "\n" for path in reads))
        return 0
    if len(argv) == 2 and argv[0] == "--test-envs":
        # the owner's machine (deploy_target.sh): the overlay's catalog on stdin — the list of one server
        try:
            data = server_test_envs(parse_envelope(_read_stdin(stdin)), argv[1])
        except Deny as exc:
            _write(err, "agent-exec-gate: %s\n" % exc)
            return 2
        _write(out, json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1) + "\n")
        return 0
    if argv:
        _write(err, "usage: agent-exec-gate [--check|--check-signers [<file>]|--test-envs <alias>|--check-test-envs <file>"
                    "|--test-env-reads <file>]\n")
        return 2
    raw = environ.get("SSH_ORIGINAL_COMMAND", "")
    try:
        gate = load_gate()
    except Deny as exc:
        _log("deny (%s): %s" % (exc, raw[:200]), syslog.LOG_WARNING if syslog else None)
        _write(err, "agent-exec-gate: отказ: %s\n" % exc)
        return 126
    observe, as_root = gate["mode"] == "observe", _is_root()
    if raw.startswith(SNAPSHOT_WORD + " "):
        nonce = raw[len(SNAPSHOT_WORD) + 1:]
        if not NONCE16.fullmatch(nonce):
            _write(err, "agent-exec-gate: отказ: снимку нужен nonce из 16 hex-символов\n")
            return 126
        _log("snapshot %s" % nonce)
        return run(snapshot_script(nonce), as_root, SNAPSHOT_TIMEOUT)
    if raw.startswith(STEP_WORD + " "):
        # the executor asks before a step whose predecessor ran here: did it succeed (a read of the journal)
        key = raw[len(STEP_WORD) + 1:]
        if not HEX64.fullmatch(key):
            _write(err, "agent-exec-gate: отказ: шагу нужен ключ из 64 hex-символов\n")
            return 126
        try:
            done = step_done(USED_DIR, key)
        except OSError as exc:
            _write(err, "agent-exec-gate: отказ: журнал шагов плана не прочитан (%s)\n" % exc.__class__.__name__)
            return 126
        _write(out, "ok\n" if done else "no\n")
        return 0
    scripts_off = os.path.lexists(SCRIPT_OFF)
    if raw != RUN_WORD:
        if not raw:
            _write(err, "agent-exec-gate: отказ: нет команды\n")
            return 126
        if scripts_off:
            _log("deny script-off: legacy: %s" % raw[:200], syslog.LOG_WARNING if syslog else None)
            _write(err, "agent-exec-gate: отказ: %s\n" % SCRIPT_OFF_DENY)
            return 126
        try:
            check_policy(gate, raw)
        except PolicyDeny as exc:
            _log("deny policy (%s): legacy: %s" % (exc, raw[:200]), syslog.LOG_WARNING if syslog else None)
            _write(err, "agent-exec-gate: отказ: %s\n" % exc)
            return 126
        if observe:
            _log("legacy: %s" % raw[:1000], syslog.LOG_WARNING if syslog else None)
            return run(raw, as_root)
        _log("deny legacy: %s" % raw[:1000], syslog.LOG_WARNING if syslog else None)
        _write(err, "agent-exec-gate: отказ: exec-ключ выполняет только шаги подписанного плана (%s)\n" % RUN_WORD)
        return 126
    envelope = None  # type: Any
    try:
        envelope = parse_envelope(_read_stdin(stdin))
        check_policy(gate, envelope.get("command"), envelope)
        signers = load_signers()
        checked = verify(envelope, gate, signers, now(), verify_sig, load_test_envs())
    except PolicyDeny as exc:
        _log("deny policy (%s): %s" % (exc, _describe(envelope)), syslog.LOG_WARNING if syslog else None)
        _write(err, "agent-exec-gate: отказ: %s\n" % exc)
        return 126
    except Deny as exc:
        if not observe or not isinstance(envelope, dict) or not isinstance(envelope.get("command"), str):
            _log("deny (%s): %s" % (exc, _describe(envelope)), syslog.LOG_WARNING if syslog else None)
            _write(err, "agent-exec-gate: отказ: %s\n" % exc)
            return 126
        _log("observe FAIL (%s): %s" % (exc, _describe(envelope)), syslog.LOG_WARNING if syslog else None)
        checked = {"kind": envelope.get("kind"), "command": envelope["command"],
                   "once": once_key(envelope.get("decision")) if envelope.get("kind") == "action" else ""}
    else:
        zone = checked.get("test_env")
        _log("allow %s%s" % ("test-env %s " % zone if zone else "", _describe(envelope)))
    if scripts_off and checked["kind"] != "action" and not checked.get("argv"):
        _log("deny script-off: %s" % _describe(envelope), syslog.LOG_WARNING if syslog else None)
        _write(err, "agent-exec-gate: отказ: %s\n" % SCRIPT_OFF_DENY)
        return 126
    if checked["kind"] == "action" and checked["once"]:
        try:
            take_once(USED_DIR, checked["once"])
        except Deny as exc:
            _log("deny (%s): %s" % (exc, _describe(envelope)), syslog.LOG_WARNING if syslog else None)
            _write(err, "agent-exec-gate: отказ: %s\n" % exc)
            return 126
    # a checked typed step runs its words without a shell; a script, an action and a step observe let
    # through unchecked stay text for bash
    command = checked.get("argv") or checked["command"]
    steps = None if checked.get("test_env") else plan_steps(envelope, gate.get("alias"))
    if steps is None:
        return run(command, as_root)
    try:
        lock = begin_step(USED_DIR, steps)
    except Deny as exc:
        _log("deny (%s): %s" % (exc, _describe(envelope)), syslog.LOG_WARNING if syslog else None)
        _write(err, "agent-exec-gate: отказ: %s\n" % exc)
        return 126
    try:
        code = run(command, as_root)
        try:
            finish_step(USED_DIR, steps[1], code)
        except OSError as exc:  # the step ran: its code stands; unrecorded, a success could run again
            _log("step result not recorded (%s): %s" % (exc.__class__.__name__, _describe(envelope)),
                 syslog.LOG_WARNING if syslog else None)
        return code
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
