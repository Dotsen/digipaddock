"""kd-signers: the owner's signing passkeys in every registry that checks them, from one root command.

    kd-signers status          compare every registry with the cabinet
    kd-signers sync [--apply]  the plan for kd-review and kd-model-keys; --apply writes it, then status again

The cabinet's set is the truth: the passkeys of each active owner whose signing is on (``signer.keys``). A removed
or switched-off key leaves the copies, a new one joins them. Two services of this host keep their own copy — kd-review
(PR approvals, ``/var/lib/digipaddock/signers/<user>.json``) and kd-model-keys (provider keys,
``/etc/digipaddock/model-key-signers/<user>.json``) — and the exec gate of every enabled server keeps the set it was
installed with (``/etc/agent-exec-gate/signers.json``, with the cabinet's rp_id and origin).

Servers are only read, through their read-only gate as ``ssh-ro`` reads them (the sysadmin's read key): this host is
the one their exec gate guards against, so a captured host must not be able to put its own key there. A server whose
set differs gets the line the owner runs on his own machine, ``deploy_target.sh <alias> --replace-signers``, the
production server last. The read gate masks the public keys of that file (``"public_key": "***"``): a server is
compared by owner and credential id, and by public key where one comes through.

Exit codes: 0 — every registry matches, 1 — a registry differs, 2 — a registry was not read (the others were),
3 — refused (not root, an owner without a signing key on ``--apply``, the cabinet unavailable, usage).
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Tuple

_SYSADMIN = str(Path(__file__).resolve().parents[2] / "agents" / "sysadmin")
if _SYSADMIN not in sys.path:
    sys.path.insert(0, _SYSADMIN)
from sysadmin import inventory  # noqa: E402 — the servers and their order, as ssh-ro reads them

REVIEW_DIR = "/var/lib/digipaddock/signers"  # kd-review (dev_agent/review.py SIGNERS)
MODEL_KEYS_DIR = (
    "/etc/digipaddock/model-key-signers"  # kd-model-keys (kd/model_keys.py SIGNERS)
)
TARGET_REGISTRY = (
    "/etc/agent-exec-gate/signers.json"  # the exec gate's registry on a server
)
REVIEW_TYPE = "pr_approve"
MASKED = "***"
GATE_NOTE = "[agent-ro-gate:"
SSH_SECONDS = 60
PARALLEL = 8
EXIT_OK, EXIT_DIFFERS, EXIT_UNREAD, EXIT_REFUSED = 0, 1, 2, 3
USAGE = "usage: kd-signers status | sync [--apply]"
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


class Refused(Exception):
    """Nothing is written; the text says why."""


class Unread(Exception):
    """One registry could not be read; the others are still checked."""


class Key(NamedTuple):
    user: str
    credential_id: str
    public_key: str
    name: str
    created: str = ""


def label(key: Key) -> str:
    return f"{' '.join(key.name.split())[:40] or '-'} {key.credential_id[:8]}"


class Result(NamedTuple):
    """One registry's comparison: ``unread`` when it could not be read, else the keys it has too many and too few."""

    where: str
    extra: Tuple[Key, ...] = ()
    missing: Tuple[Key, ...] = ()
    other: Tuple[
        str, ...
    ] = ()  # a server's cabinet (rp_id, origin) that is not this cabinet's
    unread: str = ""

    @property
    def ok(self) -> bool:
        return not (self.unread or self.extra or self.missing or self.other)

    def line(self) -> str:
        if self.unread:
            return f"{self.where}: unread: {self.unread}"
        if self.ok:
            return f"{self.where}: ok"
        parts = (
            [f"extra {', '.join(label(k) for k in self.extra)}"] if self.extra else []
        )
        if self.missing:
            parts.append(f"missing {', '.join(label(k) for k in self.missing)}")
        return f"{self.where}: {'; '.join(parts + list(self.other))}"


# --- the cabinet --------------------------------------------------------------------------------------------------
def cabinet_set(client) -> Dict[str, List[Key]]:
    """{owner: the signing passkeys the cabinet holds now} for every active owner, in the cabinet's order."""
    owners = [
        p["id"]
        for p in client.call({"op": "user.list"})["users"]
        if p.get("status") == "active"
        and any(role == "owner" for role, _ in p.get("roles", []))
    ]
    found = {}
    for user in owners:
        keys = client.call({"op": "signer.keys", "user": user}).get("keys") or []
        found[user] = [
            Key(
                user,
                k["credential_id"],
                k["public_key"],
                str(k.get("name", "")),
                str(k.get("created", "")),
            )
            for k in keys
            if isinstance(k, dict)
            and isinstance(k.get("credential_id"), str)
            and isinstance(k.get("public_key"), str)
        ]
    return found


# --- the local registries -----------------------------------------------------------------------------------------
class Local(NamedTuple):
    """A registry of this host: where its files are and how one is read and written."""

    title: str
    directory: str
    owner: Callable[
        [], Tuple[int, int]
    ]  # (uid, gid) of a new file; an existing one keeps its own

    def path(self, user: str) -> Path:
        return Path(self.directory) / f"{user}.json"

    def _data(self, user: str) -> Optional[dict]:
        try:
            data = json.loads(self.path(user).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            raise Unread(f"{self.path(user)} ({exc.__class__.__name__})") from None
        if (
            not isinstance(data, dict)
            or data.get("user") != user
            or not isinstance(data.get("keys"), list)
        ):
            raise Unread(f"{self.path(user)} is not a registry of {user}")
        return data

    def entries(self, user: str) -> List[dict]:
        """Every entry of the person's file as it stands (``[]`` without one)."""
        data = self._data(user)
        return [] if data is None else [k for k in data["keys"] if isinstance(k, dict)]

    def keys(self, user: str) -> List[Key]:
        return [
            Key(
                user,
                str(k.get("credential_id")),
                str(k.get("public_key")),
                str(k.get("name", "")),
            )
            for k in self.entries(user)
            if self.counts(k)
        ]

    def counts(self, entry: dict) -> bool:
        """Whether the service verifies with this entry."""
        return isinstance(entry.get("credential_id"), str)

    def entry(self, key: Key, now: str) -> dict:
        raise NotImplementedError

    def document(self, user: str, keys: List[dict]) -> dict:
        raise NotImplementedError


class Review(Local):
    def counts(self, entry: dict) -> bool:
        return isinstance(entry.get("credential_id"), str) and REVIEW_TYPE in (
            entry.get("types") or []
        )

    def entry(self, key: Key, now: str) -> dict:
        return {
            "credential_id": key.credential_id,
            "public_key": key.public_key,
            "name": key.name,
            "types": [REVIEW_TYPE],
            "added": now,
            "added_by": "kd-signers",
        }

    def document(self, user: str, keys: List[dict]) -> dict:
        return {"user": user, "keys": keys}


class ModelKeys(Local):
    def _data(self, user: str) -> Optional[dict]:
        data = super()._data(user)
        if data is not None and data.get("owner") is not True:
            raise Unread(f"{self.path(user)} does not enroll {user} as an owner")
        return data

    def entry(
        self, key: Key, now: str
    ) -> dict:  # as kd-model-keys enroll writes the cabinet's own entry
        return {
            "credential_id": key.credential_id,
            "public_key": key.public_key,
            "name": key.name,
            "created": key.created or now,
        }

    def document(self, user: str, keys: List[dict]) -> dict:
        return {"user": user, "owner": True, "keys": keys}


def compare(
    where: str, want: List[Key], have: List[Key], *, masked_ok: bool = False
) -> Result:
    """Keys by owner, credential id and public key; ``masked_ok``: a public key the read gate masked matches any."""

    def same(a: Key, b: Key) -> bool:
        return (a.user, a.credential_id) == (b.user, b.credential_id) and (
            a.public_key == b.public_key
            or (masked_ok and MASKED in (a.public_key, b.public_key))
        )

    extra = tuple(k for k in have if not any(same(k, w) for w in want))
    missing = tuple(w for w in want if not any(same(k, w) for k in have))
    return Result(where, extra, missing)


def local_status(registry: Local, cabinet: Dict[str, List[Key]]) -> List[Result]:
    out = []
    for user, want in cabinet.items():
        where = f"{registry.title} {user}"
        try:
            out.append(compare(where, want, registry.keys(user)))
        except Unread as exc:
            out.append(Result(where, unread=str(exc)))
    return out


def _write(path: Path, data: dict, default_owner: Callable[[], Tuple[int, int]]):
    """Atomically; an existing file keeps its owner and mode, a new one is 0600 of ``default_owner``."""
    try:
        st = os.stat(path)
        owner, mode = (st.st_uid, st.st_gid), stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        owner, mode = default_owner(), 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        os.fchmod(fd, mode)
        os.fchown(fd, *owner)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, str(path))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class Change(NamedTuple):
    registry: Local
    user: str
    keys: List[dict]  # the file's new list of entries
    added: Tuple[Key, ...]
    removed: Tuple[Key, ...]

    def line(self) -> str:
        parts = [f"add {label(k)}" for k in self.added] + [
            f"remove {label(k)}" for k in self.removed
        ]
        return f"{self.registry.title} {self.user}: {'; '.join(parts)}"


def plan(registry: Local, cabinet: Dict[str, List[Key]], now: str) -> List[Change]:
    """What brings the registry to the cabinet's set: entries the service verifies with that the cabinet still has stay
    as they are (their dates too), the cabinet's new keys join, the rest of those entries leave; entries the service
    does not verify with are not ours to judge and stay. Unread files raise ``Unread``."""
    changes = []
    for user, want in cabinet.items():
        entries = registry.entries(user)
        kept, removed = [], []
        for entry in entries:
            if not registry.counts(entry):
                kept.append(entry)
                continue
            key = Key(
                user,
                entry["credential_id"],
                str(entry.get("public_key")),
                str(entry.get("name", "")),
            )
            if any(
                (w.credential_id, w.public_key) == (key.credential_id, key.public_key)
                for w in want
            ):
                kept.append(entry)
            else:
                removed.append(key)
        have = {
            (e["credential_id"], str(e.get("public_key")))
            for e in kept
            if registry.counts(e)
        }
        added = [w for w in want if (w.credential_id, w.public_key) not in have]
        kept += [registry.entry(w, now) for w in added]
        if added or removed:
            changes.append(Change(registry, user, kept, tuple(added), tuple(removed)))
    return changes


# --- the servers --------------------------------------------------------------------------------------------------
class Server(NamedTuple):
    alias: str
    argv: List[str]  # ssh to its read gate with the command that prints the registry


def servers(
    inventory_path=None, enabled_path=inventory.ENABLED, home: Optional[str] = None
) -> List[Server]:
    """The enabled servers in the inventory's order, the production server last."""
    found = inventory.load(inventory_path)
    on = inventory.enabled(enabled_path)
    aliases = [a for a in found if a in on]
    try:
        prod = inventory.prod(inventory_path)
    except inventory.InventoryError:
        prod = None
    aliases = [a for a in aliases if a != prod] + [a for a in aliases if a == prod]
    if home is None:
        from kd import naming

        home = naming.home(naming.agent_user("sysadmin"))
    key, known = (
        f"{home}/{inventory.RO_KEY_FILE}",
        f"{home}/{inventory.RO_KNOWN_HOSTS_FILE}",
    )
    return [
        Server(
            a,
            inventory.ssh_argv(
                key, known, inventory.RO_USER, found[a].host, f"cat {TARGET_REGISTRY}"
            ),
        )
        for a in aliases
    ]


def ssh_read(argv: List[str]) -> str:
    try:
        done = subprocess.run(
            argv, stdin=subprocess.DEVNULL, capture_output=True, timeout=SSH_SECONDS
        )
    except subprocess.TimeoutExpired:
        raise Unread(f"no answer in {SSH_SECONDS} s") from None
    except OSError as exc:
        raise Unread(f"ssh did not start ({exc.__class__.__name__})") from None
    if done.returncode != 0:
        why = done.stderr.decode("utf-8", "replace").strip().splitlines()
        raise Unread(f"exit {done.returncode}" + (f": {why[-1][:200]}" if why else ""))
    return done.stdout.decode("utf-8", "replace")


def parse_target(text: str) -> dict:
    """The server's registry as its read gate printed it (its own notes after the JSON dropped)."""
    body = "\n".join(
        line for line in text.splitlines() if not line.startswith(GATE_NOTE)
    )
    try:
        data = json.loads(body)
    except ValueError:
        raise Unread(f"{TARGET_REGISTRY} is not JSON") from None
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 1
        or not isinstance(data.get("signers"), list)
    ):
        raise Unread(f"{TARGET_REGISTRY} is not a registry of schema 1")
    return data


def server_result(
    alias: str, data: dict, cabinet: Dict[str, List[Key]], rp_id: str, origin: str
) -> Result:
    have = [
        Key(
            str(s.get("user")),
            str(s.get("credential_id")),
            str(s.get("public_key")),
            str(s.get("name", "")),
        )
        for s in data["signers"]
        if isinstance(s, dict)
    ]
    want = [k for keys in cabinet.values() for k in keys]
    result = compare(f"server {alias}", want, have, masked_ok=True)
    if (data.get("rp_id"), data.get("origin")) != (rp_id, origin):
        result = result._replace(
            other=(f"trusts the cabinet {data.get('rp_id')} {data.get('origin')}",)
        )
    return result


def server_status(
    found: List[Server],
    cabinet: Dict[str, List[Key]],
    rp_id: str,
    origin: str,
    read: Callable[[List[str]], str] = ssh_read,
) -> List[Result]:
    """Every server read, in parallel; an unread one says why and stops none of the others."""

    def one(server: Server) -> Result:
        try:
            return server_result(
                server.alias, parse_target(read(server.argv)), cabinet, rp_id, origin
            )
        except Unread as exc:
            return Result(f"server {server.alias}", unread=str(exc))

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        return list(pool.map(one, found))


# --- the command --------------------------------------------------------------------------------------------------
def _code(results: List[Result]) -> int:
    if any(r.unread for r in results):
        return EXIT_UNREAD
    return EXIT_OK if all(r.ok for r in results) else EXIT_DIFFERS


def _cabinet_lines(cabinet: Dict[str, List[Key]], out):
    for user, keys in cabinet.items():
        listed = (
            ", ".join(label(k) for k in keys)
            or "none — sync --apply refuses until one signs"
        )
        print(f"cabinet {user}: {listed}", file=out)


class Env(NamedTuple):
    """What a run reads and writes; tests give their own."""

    client: object
    registries: Tuple[Local, ...]
    servers: Callable[[], List[Server]]
    read: Callable[[List[str]], str]
    origin: Callable[[], Optional[str]]
    clock: Callable[[], float] = time.time


def default_env() -> Env:
    from kd import naming
    from kd.cabinet import client, config

    def review_owner():
        import pwd

        entry = pwd.getpwnam(naming.service_user("review"))
        return entry.pw_uid, entry.pw_gid

    return Env(
        client=client.Client(),
        registries=(
            Review("kd-review", REVIEW_DIR, review_owner),
            ModelKeys("kd-model-keys", MODEL_KEYS_DIR, lambda: (0, 0)),
        ),
        servers=servers,
        read=ssh_read,
        origin=config.origin_of,
    )


def run(argv: List[str], env: Env, out, err) -> int:
    from kd.cabinet import config

    if argv not in (["status"], ["sync"], ["sync", "--apply"]):
        print(USAGE, file=err)
        return EXIT_REFUSED
    origin = env.origin()
    if origin is None:
        raise Refused("no cabinet origin: cabinet.json is missing or invalid")
    rp_id = config.check_origin(origin)
    try:
        cabinet = cabinet_set(env.client)
    except Exception as exc:  # the cabinet client's own error: unavailable or refused
        raise Refused(f"the cabinet did not answer: {exc}") from None
    if not cabinet:
        raise Refused("the cabinet has no active owner")
    _cabinet_lines(cabinet, out)
    try:
        found = env.servers()
        far = server_status(found, cabinet, rp_id, origin, env.read)
    except inventory.InventoryError as exc:
        found, far = [], [Result("servers", unread=str(exc))]
    near = [r for registry in env.registries for r in local_status(registry, cabinet)]
    for result in near + far:
        print(result.line(), file=out)
    if argv == ["status"]:
        return _code(near + far)
    changes = _changes(env, cabinet)
    for change in changes:
        print(f"plan {change.line()}", file=out)
    if not changes:
        print("plan: kd-review and kd-model-keys match the cabinet already", file=out)
    differ = [
        r.where.split(" ", 1)[1]
        for r in far
        if r.where.startswith("server ") and not r.ok and not r.unread
    ]
    for alias in differ:
        print(
            f"server {alias}: on the owner's machine: deploy_target.sh {alias} --replace-signers",
            file=out,
        )
    if argv == ["sync"]:
        if changes:
            print(
                "sync: nothing written — kd-signers sync --apply writes the plan",
                file=out,
            )
        return _code(near + far)
    empty = [user for user, keys in cabinet.items() if not keys]
    if empty:
        raise Refused(
            f"{', '.join(empty)} has no passkey that signs in the cabinet: sync would leave no key to "
            f"approve with — switch one on first (kd-cabinet user signing <id> --credential <prefix> on); "
            f"nothing written"
        )
    for change in changes:
        _write(
            change.registry.path(change.user),
            change.registry.document(change.user, change.keys),
            change.registry.owner,
        )
        print(f"written {change.line()}", file=out)
    near = [r for registry in env.registries for r in local_status(registry, cabinet)]
    print("status after sync:", file=out)
    for result in near + far:
        print(result.line(), file=out)
    return _code(near + far)


def _changes(env: Env, cabinet: Dict[str, List[Key]]) -> List[Change]:
    """Every registry's plan; one unreadable file refuses the whole of it, before anything is written."""
    now = datetime.fromtimestamp(env.clock(), timezone.utc).strftime(TIME_FORMAT)
    changes = []
    for registry in env.registries:
        try:
            ours = plan(registry, cabinet, now)
        except Unread as exc:
            raise Refused(
                f"{registry.title}: {exc} — fix or remove the file by hand; nothing written"
            ) from None
        if ours and not Path(registry.directory).is_dir():
            raise Refused(
                f"{registry.title}: no directory {registry.directory} — its installer makes it; "
                f"nothing written"
            )
        changes += ours
    return changes


def main(
    argv=None,
    *,
    env: Optional[Env] = None,
    euid: Optional[int] = None,
    out=None,
    err=None,
) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    out, err = out or sys.stdout, err or sys.stderr
    if (os.geteuid() if euid is None else euid) != 0:
        print("kd-signers: root only", file=err)
        return EXIT_REFUSED
    try:
        return run(argv, env or default_env(), out, err)
    except Refused as exc:
        print(f"kd-signers: {exc}", file=err)
        return EXIT_REFUSED
