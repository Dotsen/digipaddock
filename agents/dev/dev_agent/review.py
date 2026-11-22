"""kd-review — the Forgejo approver for the owner's signature in the cabinet.

The cabinet has no network and never holds a Forgejo token. kd-review runs under its own user with its own token
(``/etc/digipaddock/review/<account>.env``, the account of host.json ``forge_review_user``) and is the one account besides the owner whose Approve the target
branches' rules take; its Approve body names the branch the owner signed, and kd-merge counts it only for a PR into
that branch. It takes nobody's word for an approval: before any Forgejo call it recomputes the value the person signed
(``kd.pr_signature.challenge``) from the decision it is shown, checks the passkey assertion with the public key root
registered for that person, one key at a time (``kd-review signers add <user> --credential <id>``), and then checks
that the live PR is a trusted author's (``forge.authors()``), open, into the signed target branch of the project's
passport, still at the signed head, with the passport's required checks green on it Without a
fresh assertion of a registered
key the cabinet's database gets no Approve out of it. What it cannot tell is which ceremony the person thought he was
in: the cabinet's service runs every passkey ceremony of the site, so a compromised service could present a PR's
value as the challenge of a sign-in. A key registered here only for signing (not used to sign in) narrows that.

One JSON line in, one out, on ``/run/digipaddock-review/review.sock`` (only the cabinet's user may connect): ``keys``,
``live``, ``approve`` and ``comment``; the answer is ``{"ok": true, ...}`` or ``{"ok": false, "error": kind,
"message": text}`` (a PR that ended adds ``"live": "merged" | "closed"``). A spent nonce is kept on disk for its term
(``used-nonces.json``), so a restart approves nothing twice. Each outcome is one JSON line on stderr (the journal) with
no token, assertion or text.
"""
import base64
import json
import os
import re
import socket
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "common"))  # …/DigiPaddock/common
from kd import pr_signature  # noqa: E402
from kd.cabinet import config, passkeys  # noqa: E402
from dev_agent import forge, repos  # noqa: E402

SOCKET = "/run/digipaddock-review/review.sock"
SIGNERS = "/var/lib/digipaddock/signers"
NONCES = "/var/lib/digipaddock/review/used-nonces.json"  # the unit's StateDirectory
TOKEN_DIR = "/etc/digipaddock/review"
CREDENTIAL_PREFIX_MIN = 8
TERM_MAX = 600  # seconds ahead a signature's term may end: the cabinet gives five minutes
CONNECTION_SECONDS = 30
LINE_MAX = 1 << 20
TYPE = "pr_approve"
KINDS = ("refused", "stale", "invalid", "unavailable")
USER_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
HEX32 = re.compile(r"[0-9a-f]{32}")
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
COMMENTS_MAX, COMMENT_MAX, PATH_MAX, BODY_MAX = 30, 1000, 500, 4000
REMARKS = "Замечания владельца — в кабинете"


class Refused(Exception):
    def __init__(self, kind: str, message: str, **extra):
        super().__init__(message)
        self.kind = kind if kind in KINDS else "refused"
        self.extra = extra  # more fields of the answer, e.g. ``live`` of a PR that ended


class Rp(NamedTuple):
    """What ``passkeys`` needs of the cabinet's settings: its origin and relying party id."""
    origin: str
    rp_id: str


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")


def _unb64(text) -> bytes:
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except (TypeError, ValueError) as exc:
        raise Refused("unavailable", "a signer's key is not base64url") from exc


def _term(text) -> float:
    try:
        return datetime.strptime(text, TIME_FORMAT).replace(tzinfo=timezone.utc).timestamp()
    except (TypeError, ValueError) as exc:
        raise Refused("invalid", "expires must be YYYY-MM-DDTHH:MM:SSZ") from exc


def _number(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value < 10 ** 7:
        raise Refused("invalid", "number must be the PR's number")
    return value


def _head(value) -> str:
    if not isinstance(value, str) or not forge.SHA40_RE.fullmatch(value):
        raise Refused("invalid", "head must be the full 40-character sha")
    return value


def _ours(review: dict, head: str, target: str, mode: str) -> bool:
    """kd-review's Approve of this head, target and mode: a signature in another mode posts its own."""
    body = forge._str(review.get("body"))
    return (review.get("state") == "APPROVED" and forge._login(review) == forge.review_login()
            and forge._str(review.get("commit_id")).lower() == head
            and body.endswith(forge.INTO.format(target)) and forge.signed_mode(body) == mode
            and review.get("dismissed") is not True and review.get("stale") is not True)


class Reviewer:
    def __init__(self, *, api, repos_path=repos.REPOS, signers_dir=SIGNERS, origin: str, clock=time.time,
                 nonces_path=None):
        self.api, self.repos_path, self.signers_dir, self.clock = api, repos_path, Path(signers_dir), clock
        self.rp = Rp(origin, config.check_origin(origin))
        self.nonces_path = Path(nonces_path) if nonces_path else None
        self.used = {}  # nonce → the end of its term: a signature approves once (on disk too, with nonces_path)

    def handle(self, request) -> dict:
        if not isinstance(request, dict):
            raise Refused("invalid", "a request is a JSON object")
        handler = {"keys": self.keys, "live": self.live, "approve": self.approve,
                   "comment": self.comment}.get(request.get("op"))
        if handler is None:
            raise Refused("invalid", "op must be keys, live, approve or comment")
        return handler(request)

    # --- spent nonces -------------------------------------------------------------------------------------------
    def _spent(self, now: float) -> dict:
        """The nonces whose term has not ended; from the file when there is one — an unreadable one approves nothing."""
        if self.nonces_path is not None:
            try:
                data = json.loads(self.nonces_path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = {}
            except (OSError, ValueError) as exc:
                raise Refused("unavailable", f"spent nonces: unreadable ({exc.__class__.__name__})") from None
            if not isinstance(data, dict):
                raise Refused("unavailable", "spent nonces: not a table")
            self.used = {k: v for k, v in data.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
        self.used = {k: v for k, v in self.used.items() if v > now}
        return self.used

    def _spend(self, nonce: str, term: float):
        self.used[nonce] = term
        if self.nonces_path is None:
            return
        try:
            _write(self.nonces_path, self.used, None)
        except OSError as exc:
            self.used.pop(nonce, None)
            raise Refused("unavailable", f"spent nonces: not written ({exc.__class__.__name__})") from None

    # --- the registry -------------------------------------------------------------------------------------------
    def _registry(self, user) -> list:
        if not isinstance(user, str) or not USER_RE.fullmatch(user):
            raise Refused("invalid", "user must be a cabinet user id")
        try:
            data = json.loads((self.signers_dir / f"{user}.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            raise Refused("unavailable", f"signers of {user}: unreadable ({exc.__class__.__name__})") from None
        keys = data.get("keys") if isinstance(data, dict) and data.get("user") == user else None
        if not isinstance(keys, list):
            raise Refused("unavailable", f"signers of {user}: not a registry")
        return [k for k in keys if isinstance(k, dict) and isinstance(k.get("credential_id"), str)
                and isinstance(k.get("types"), list) and TYPE in k["types"]]

    def keys(self, request) -> dict:
        return {"credential_ids": [k["credential_id"] for k in self._registry(request.get("user"))]}

    # --- approve ------------------------------------------------------------------------------------------------
    def approve(self, request) -> dict:
        decision, payload, user = request.get("decision"), request.get("payload"), request.get("user")
        revision, expires, nonce = request.get("card_revision"), request.get("expires"), request.get("nonce")
        mode, assertion = request.get("mode"), request.get("assertion")
        if (not isinstance(decision, str) or not HEX32.fullmatch(decision) or not isinstance(payload, dict)
                or isinstance(revision, bool) or not isinstance(revision, int) or not isinstance(expires, str)
                or not isinstance(nonce, str) or not HEX32.fullmatch(nonce) or not isinstance(assertion, dict)
                or not isinstance(user, str) or not USER_RE.fullmatch(user)):
            raise Refused("invalid", "approve takes decision, payload, card_revision, user, expires, nonce, mode, "
                                     "assertion")
        if payload.get("kind") != TYPE:
            raise Refused("invalid", "not a pull request's approval")
        target = payload.get("target")
        if not isinstance(target, str) or not repos.is_branch(target):
            raise Refused("invalid", "a decision without its target branch (signed before KD-9180) — kd-forge publish "
                                     "the PR again")
        if mode not in pr_signature.MODES:
            raise Refused("invalid", f"mode must be one of {', '.join(pr_signature.MODES)}")
        if mode == "release" and payload.get("release") is not True:
            raise Refused("refused", "this project is not released from the cabinet")
        now, term = self.clock(), _term(expires)
        if not now < term <= now + TERM_MAX:
            raise Refused("refused", "the signature's term is over or too far ahead")
        if nonce in self._spent(now):
            raise Refused("refused", "this signature was used already")
        try:
            raw = _b64(passkeys.credential_id_of(assertion))
        except passkeys.PasskeyError as exc:
            raise Refused("invalid", str(exc)) from None
        key = next((k for k in self._registry(user) if k["credential_id"] == raw), None)
        if key is None:
            raise Refused("refused", f"the key is not one of {user}'s signers (kd-review signers add {user})")
        try:
            value = pr_signature.challenge(decision, payload, revision, user, expires, nonce, mode)
            passkeys.verify_authentication(self.rp, assertion, challenge=value,
                                           public_key=_unb64(key.get("public_key", "")), sign_count=0)
        except (ValueError, passkeys.PasskeyError):
            raise Refused("refused", "the signature does not match this decision") from None
        passport = self._repo(payload.get("project"), payload.get("repo"))
        repo, number, head = passport.repo, _number(payload.get("number")), _head(payload.get("head"))
        self._live(passport, number, head, target)
        missing = forge.missing_checks(forge.statuses(self.api, repo, head), repos.required(passport)) \
            if repos.required(passport) else []
        if missing:
            raise Refused("refused", f"required checks are not green on {head[:7]}: {', '.join(missing)}")
        found = next((r for r in forge._reviews(self.api, repo, number) if _ours(r, head, target, mode)), None)
        # spent only now: a Forgejo that did not answer, a stale head or red checks leave the signature for
        # a retry in its term; one request at a time (``serve``), so nothing spends it between the check and here
        self._spend(nonce, term)
        if found is not None and isinstance(found.get("id"), int):
            return {"review": found["id"], "head": head, "state": "already"}
        # the body names the signed branch: kd-merge counts this Approve only for a PR into it (a retarget keeps it)
        posted = self.api.post(f"/repos/{repo}/pulls/{number}/reviews", {
            "event": "APPROVED", "commit_id": head,
            "body": forge.SIGNED.format(forge.MODE_TEXT[mode]) + decision[:8] + forge.INTO.format(target)})
        review_id = posted.get("id") if isinstance(posted, dict) else None
        if isinstance(review_id, bool) or not isinstance(review_id, int):
            raise Refused("unavailable", "Forgejo answered no review id")
        return {"review": review_id, "head": head, "state": "approved"}

    def _repo(self, project, name) -> repos.Repo:
        try:
            repo = repos.get(project, self.repos_path)
        except repos.RepoError as exc:
            raise Refused("refused", str(exc)) from None
        if repo.repo != name:
            raise Refused("refused", f"project {project} is not {name}")
        return repo

    def _state(self, repo: repos.Repo, number: int):
        """(open | merged | closed, head, target) of a trusted author's PR into one of the passport's targets as
        Forgejo holds it now."""
        data = forge.pr(self.api, repo.repo, number)
        if forge._login(data) not in forge.authors():
            raise Refused("refused", f"PR #{number} is not of {', '.join(sorted(forge.authors()))}")
        target = forge._str(forge._dict(data.get("base")).get("ref"))
        if target not in repo.targets:
            raise Refused("refused", f"PR #{number} targets {target or '?'}, not one of {', '.join(repo.targets)}")
        state = "merged" if data.get("merged") is True else "open" if data.get("state") == "open" else "closed"
        return state, forge._str(forge._dict(data.get("head")).get("sha")).lower(), target

    def _live(self, repo: repos.Repo, number: int, head: str, target: Optional[str] = None):
        """The PR as Forgejo holds it now: open, at the head the person saw, into the branch they signed."""
        state, live, base = self._state(repo, number)
        if state != "open":
            raise Refused("stale", f"PR #{number} is {state}", live=state)
        if live != head:
            raise Refused("stale", f"PR #{number} moved on: head {live[:7] or '?'}, signed {head[:7]}")
        if target is not None and base != target:
            raise Refused("stale", f"PR #{number} targets {base} now, signed into {target}")

    def live(self, request) -> dict:
        """The cabinet asks before the person signs: is the PR still open, at which head and into which branch."""
        number = _number(request.get("number"))
        state, head, target = self._state(self._repo(request.get("project"), request.get("repo")), number)
        return {"state": state, "head": head, "target": target}

    # --- comment ------------------------------------------------------------------------------------------------
    def comment(self, request) -> dict:
        number, head = _number(request.get("number")), _head(request.get("head"))
        body, comments = request.get("body"), request.get("comments")
        if not isinstance(body, str) or len(body) > BODY_MAX:
            raise Refused("invalid", f"body must be text of at most {BODY_MAX} characters")
        if not isinstance(comments, list) or len(comments) > COMMENTS_MAX:
            raise Refused("invalid", f"comments: a list of at most {COMMENTS_MAX}")
        lines = []
        for c in comments:
            line = c.get("line") if isinstance(c, dict) else None
            if (not isinstance(c, dict) or not isinstance(c.get("path"), str) or not 0 < len(c["path"]) <= PATH_MAX
                    or isinstance(line, bool) or not isinstance(line, int) or line < 1
                    or c.get("side") not in ("new", "old") or not isinstance(c.get("body"), str)
                    or not 0 < len(c["body"]) <= COMMENT_MAX):
                raise Refused("invalid", "a comment is {path, line, side: new|old, body}")
            lines.append({"path": c["path"], "body": c["body"], f"{c['side']}_position": line})
        passport = self._repo(request.get("project"), request.get("repo"))
        self._live(passport, number, head)
        posted = self.api.post(f"/repos/{passport.repo}/pulls/{number}/reviews",
                               {"event": "COMMENT", "commit_id": head, "body": body or REMARKS, "comments": lines})
        review_id = posted.get("id") if isinstance(posted, dict) else None
        if isinstance(review_id, bool) or not isinstance(review_id, int):
            raise Refused("unavailable", "Forgejo answered no review id")
        return {"review": review_id}


# --- the socket ---------------------------------------------------------------------------------------------------
def _log(request, outcome: str, why: str = ""):
    request = request if isinstance(request, dict) else {}
    payload = request.get("payload") if isinstance(request.get("payload"), dict) else {}
    record = {"ts": datetime.now(timezone.utc).strftime(TIME_FORMAT), "op": str(request.get("op", ""))[:16],
              "outcome": outcome}
    for name, value in (("decision", request.get("decision")), ("user", request.get("user")),
                        ("mode", request.get("mode")), ("repo", payload.get("repo", request.get("repo"))),
                        ("number", payload.get("number", request.get("number"))),
                        ("head", payload.get("head", request.get("head")))):
        if isinstance(value, (str, int)) and not isinstance(value, bool):
            record[name] = str(value)[:100] if isinstance(value, str) else value
    if why:
        record["why"] = why[:300]
    print(json.dumps(record, ensure_ascii=False), file=sys.stderr, flush=True)


def answer(reviewer: Reviewer, line: bytes, allowed: bool = True) -> bytes:
    """One request line → one answer line; never raises."""
    request = None
    try:
        if not allowed:
            raise Refused("refused", "only the cabinet's service asks kd-review")
        try:
            request = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise Refused("invalid", "a request is one JSON line") from None
        out = {"ok": True, **reviewer.handle(request)}
        _log(request, out.get("state", "ok"))
    except Refused as exc:
        out = {**exc.extra, "ok": False, "error": exc.kind, "message": str(exc)}
        _log(request, exc.kind, str(exc))
    except forge.ForgeError as exc:  # its text is the server's, with the token masked
        out = {"ok": False, "error": "unavailable", "message": str(exc)}
        _log(request, "unavailable", str(exc))
    except Exception as exc:  # noqa: BLE001 — one request's fault never stops the service
        out = {"ok": False, "error": "unavailable", "message": f"kd-review failed ({exc.__class__.__name__})"}
        _log(request, "unavailable", exc.__class__.__name__)
    return json.dumps(out, ensure_ascii=False).encode("utf-8") + b"\n"


def serve_one(conn: socket.socket, reviewer: Reviewer, allowed_uid: int):
    from kd import cap_serve  # SO_PEERCRED: Linux only

    with conn:
        conn.settimeout(CONNECTION_SECONDS)
        try:
            _, uid, _ = cap_serve.peer(conn)
            line = conn.makefile("rb").readline(LINE_MAX + 1)
            if len(line) > LINE_MAX:
                line = b""
            conn.sendall(answer(reviewer, line, uid == allowed_uid))
        except OSError:
            pass


def serve(sock: socket.socket, reviewer: Reviewer, allowed_uid: int):
    """One connection at a time: only the cabinet's service connects, one request each."""
    while True:
        conn, _ = sock.accept()
        serve_one(conn, reviewer, allowed_uid)


def _activated() -> Optional[socket.socket]:
    if os.environ.get("LISTEN_PID") != str(os.getpid()) or os.environ.get("LISTEN_FDS") != "1":
        return None
    return socket.socket(fileno=3)


# --- the registry on disk -----------------------------------------------------------------------------------------
def _write(path: Path, data: dict, owner: Optional[tuple]):
    """Atomically, 0600; ``owner`` = (uid, gid) of the service user when root writes it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.chmod(tmp, 0o600)
        if owner is not None:
            os.chown(tmp, *owner)
        os.replace(tmp, str(path))
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _entries(path: Path, user: str) -> list:
    """Every key of the registry file as it stands (``[]`` when there is none)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        raise Refused("unavailable", f"signers of {user}: unreadable ({exc.__class__.__name__})") from None
    keys = data.get("keys") if isinstance(data, dict) and data.get("user") == user else None
    if not isinstance(keys, list):
        raise Refused("unavailable", f"signers of {user}: not a registry")
    return [k for k in keys if isinstance(k, dict) and isinstance(k.get("credential_id"), str)]


def _one(keys: list, prefix: str, where: str) -> dict:
    if len(prefix) < CREDENTIAL_PREFIX_MIN:
        raise Refused("invalid", f"--credential takes at least {CREDENTIAL_PREFIX_MIN} characters of the key's id")
    found = [k for k in keys if k["credential_id"].startswith(prefix)]
    if len(found) != 1:
        raise Refused("refused", f"{len(found)} keys {where} start with {prefix}: give exactly one")
    return found[0]


def _cabinet_keys(cabinet, user: str) -> list:
    keys = cabinet.call({"op": "signer.keys", "user": user}).get("keys")
    keys = [k for k in keys if isinstance(k, dict) and isinstance(k.get("credential_id"), str)
            and isinstance(k.get("public_key"), str)] if isinstance(keys, list) else []
    if not keys:
        raise Refused("refused", f"{user} has no passkey that signs in the cabinet")
    return keys


def signers_add(user: str, prefix: str, *, cabinet, signers_dir=SIGNERS, owner: Optional[tuple] = None,
                clock=time.time) -> dict:
    """One of the person's signing passkeys the cabinet holds now (``signer.keys``), chosen by root by its id, added
    for PRs; the keys already registered stay as they are."""
    if not USER_RE.fullmatch(user):
        raise Refused("invalid", "user must be a cabinet user id")
    key = _one(_cabinet_keys(cabinet, user), prefix, f"of {user} in the cabinet")
    path = Path(signers_dir) / f"{user}.json"
    entries = _entries(path, user)
    if any(k["credential_id"] == key["credential_id"] for k in entries):
        raise Refused("refused", f"this key of {user} signs pull requests already")
    entry = {"credential_id": key["credential_id"], "public_key": key["public_key"], "name": str(key.get("name", "")),
             "types": [TYPE], "added": datetime.fromtimestamp(clock(), timezone.utc).strftime(TIME_FORMAT),
             "added_by": "root"}
    _write(path, {"user": user, "keys": entries + [entry]}, owner)
    return {**entry, "created": str(key.get("created", "?"))}


def _service_ids(role: str) -> tuple:
    import pwd
    from kd import naming

    entry = pwd.getpwnam(naming.service_user(role))
    return entry.pw_uid, entry.pw_gid


USAGE = ("kd-review serve | signers add <user> [--credential <id>] | signers remove <user> [--credential <id>] | "
         "signers list <user> | check")


def main(argv=None, *, cabinet=None, signers_dir=SIGNERS, owner="review", clock=time.time, token_dir=TOKEN_DIR,
         settings=config.JSON, out=None, err=None) -> int:
    """Exit codes: 0 done, 1 unavailable, 2 refused or invalid."""
    argv = list(sys.argv[1:] if argv is None else argv)
    out, err = out or sys.stdout, err or sys.stderr
    try:
        if argv == ["serve"]:
            return _serve(token_dir, signers_dir, settings)
        if argv == ["check"]:
            return _check(token_dir, signers_dir, settings, out, err)
        if len(argv) == 3 and argv[0] == "signers" and argv[1] in ("add", "remove", "list"):
            return _signers(argv[1], argv[2], None, cabinet, signers_dir, owner, clock, out)
        if len(argv) == 5 and argv[0] == "signers" and argv[1] in ("add", "remove") and argv[3] == "--credential":
            return _signers(argv[1], argv[2], argv[4], cabinet, signers_dir, owner, clock, out)
    except Refused as exc:
        print(f"kd-review: {exc}", file=err)
        return 1 if exc.kind == "unavailable" else 2
    except forge.ForgeError as exc:
        print(f"kd-review: {exc}", file=err)
        return 1
    print(f"usage: {USAGE}", file=err)
    return 2


def _origin(settings) -> str:
    try:
        origin = config.public(settings).get("origin")
        config.check_origin(origin)
    except config.ConfigError as exc:
        raise Refused("unavailable", f"the cabinet's origin: {exc}") from None
    return origin


def _serve(token_dir, signers_dir, settings) -> int:
    sock = _activated()
    if sock is None:
        raise Refused("unavailable", "serve runs from digipaddock-review.socket")
    reviewer = Reviewer(api=forge.Api(forge.load_token(forge.review_login(), token_dir)), signers_dir=signers_dir,
                        origin=_origin(settings), nonces_path=NONCES)
    serve(sock, reviewer, _service_ids("cabinet")[0])
    return 0


def _check(token_dir, signers_dir, settings, out, err) -> int:
    problems = []
    for what, probe in (("token", lambda: forge.load_token(forge.review_login(), token_dir)),
                        ("origin", lambda: _origin(settings))):
        try:
            probe()
        except (forge.ForgeError, Refused) as exc:
            problems.append(f"{what}: {exc}")
    try:
        signers = sorted(p.stem for p in Path(signers_dir).glob("*.json"))
    except OSError:
        signers = []
    if not signers:
        problems.append("no signers: root runs kd-review signers add <user> (then --credential <id>)")
    for line in problems:
        print(f"kd-review check: {line}", file=err)
    if not problems:
        print(f"kd-review check: ok (signers: {', '.join(signers)})", file=out)
    return 1 if problems else 0


def _signers(action, user, prefix, cabinet, signers_dir, owner, clock, out) -> int:
    if not USER_RE.fullmatch(user):
        raise Refused("invalid", "user must be a cabinet user id")
    path = Path(signers_dir) / f"{user}.json"
    ids = (lambda: _service_ids(owner)) if isinstance(owner, str) else (lambda: owner)
    if action == "remove" and prefix is None:
        path.unlink(missing_ok=True)
        print(f"kd-review: {user} signs nothing now", file=out)
        return 0
    if action == "remove":
        entries = _entries(path, user)
        gone = _one(entries, prefix, f"of {user} in the registry")
        _write(path, {"user": user, "keys": [k for k in entries if k is not gone]}, ids())
        print(f"kd-review: {gone['credential_id'][:12]}…  {gone.get('name', '')} no longer signs for {user}", file=out)
        return 0
    if action == "list":
        reviewer = Reviewer(api=None, signers_dir=signers_dir, origin="https://localhost")
        for key in reviewer._registry(user):
            print(f"{key['credential_id'][:16]}…  {key.get('name', '')}  added {key.get('added', '?')}", file=out)
        return 0
    if cabinet is None:
        from kd.cabinet import client as cabinet_client

        cabinet = cabinet_client.Client()
    try:
        if prefix is None:  # what root chooses from; nothing is written
            for key in _cabinet_keys(cabinet, user):
                print(f"{key['credential_id'][:16]}…  {key.get('name', '')}  created {key.get('created', '?')}",
                      file=out)
            print(f"kd-review: add the key you sign with: kd-review signers add {user} --credential <id>", file=out)
            return 0
        entry = signers_add(user, prefix, cabinet=cabinet, signers_dir=signers_dir, owner=ids(), clock=clock)
    except Exception as exc:  # the cabinet's own error type: the board is unavailable or refused
        if isinstance(exc, Refused):
            raise
        raise Refused(getattr(exc, "kind", "unavailable"), str(exc)) from None
    print(f"kd-review: {user} signs pull requests with {entry['credential_id'][:12]}…  {entry['name']}  "
          f"(created {entry['created']})", file=out)
    return 0
