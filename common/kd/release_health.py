"""The health check after a release: the step of ``kd-release`` after the cabinet
runs the new code and the roles are asked for.

A release ends in ``healthy`` or ``degraded``, not in «the code is switched»: ``run`` checks, in this order, the
session host (``session_host_setup.sh check``), that it runs this release's host code or its restart is asked
(the pinned release ``<base>/HOST``), nginx (``nginx -t``), the cabinet over nginx (``GET /api/health``),
the cabinet's schema against the switched code and that its queues answer (``pipeline.status``), and kd-e2e's
runtime is the release's where the host has one (``e2e_runtime_setup.sh check``). The two cabinet
checks retry: the service restarts right after ``cabinet_setup.sh install``. ``main`` posts the result as a notice
on the card of the release (root source ``release``; critical when degraded) — the fact the cabinet derives the
release state and the ``release_degraded`` problem from — and prints it as one JSON line.

Runs isolated (``python3 -I``) from the switched tree: ``schema.SCHEMA_VERSION`` is the code's.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional

SOURCE = "release"
HEALTHY, DEGRADED = "notice.release.healthy", "notice.release.degraded"
RETRIES, PAUSE = 12, 5.0  # the cabinet checks: up to a minute after the service's restart
T_CHECK = 120  # one shell check
T_HTTP = 10
SKIPPED = "skipped"
ORDER = ("session_host", "session_host_code", "nginx", "cabinet_http", "cabinet_schema", "queues", "e2e_runtime")
OLD_HOST = "хост сессий на другом коде: работает с {pinned}, выкачен {target}; перезапуск не запрошен"
# kd-e2e's prepared runtime: kd-release rebuilds it where the host has one (review I5)
E2E_RUNTIME = Path("/var/lib/digipaddock/e2e/runtime")


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"name": name, "ok": bool(ok), "detail": detail[-300:]}


def _shell(name: str, argv: List[str], run) -> dict:
    try:
        p = run(argv, capture_output=True, text=True, timeout=T_CHECK)
    except (OSError, subprocess.SubprocessError) as exc:
        return _check(name, False, f"{argv[0]}: {exc.__class__.__name__}")
    text = ((p.stdout or "") + (p.stderr or "")).strip()
    return _check(name, p.returncode == 0, text if p.returncode != 0 else "")


def _http(origin: str, http) -> dict:
    url = origin.rstrip("/") + "/api/health"
    try:
        with http(url, timeout=T_HTTP) as response:
            status = getattr(response, "status", 200)
    except urllib.error.HTTPError as exc:
        status = exc.code
    except (OSError, ValueError) as exc:
        return _check("cabinet_http", False, f"{url}: {exc.__class__.__name__}: {exc}")
    return _check("cabinet_http", status == 200, "" if status == 200 else f"{url}: HTTP {status}")


def _host_code(tree: Path, restart: str) -> dict:
    """The long-running host runs the code it loaded; on another release than ``tree`` with a different
    host code it is healthy only while this release's restart is asked (``restart`` — its operation)."""
    from kd import release_reinstall

    target = Path(os.path.realpath(tree))
    pinned = release_reinstall.pinned_release(target)
    if pinned is None:  # a host from before the pin: nothing to compare with
        return _check("session_host_code", True, SKIPPED)
    if pinned == target or restart or not release_reinstall.host_changed(pinned, target):
        return _check("session_host_code", True)
    return _check("session_host_code", False, OLD_HOST.format(pinned=pinned.name, target=target.name))


def _cabinet(client, schema_version: int, *, retries: int, pause: float, sleep=time.sleep) -> List[dict]:
    """``pipeline.status`` of the cabinet: the schema it serves and that its queues answer; retried while the
    socket does not answer (the service restarts after its install)."""
    from kd.cabinet.client import CabinetError

    error = ""
    for attempt in range(max(1, retries)):
        try:
            found = client.call({"op": "pipeline.status"})
            break
        except (CabinetError, OSError, ValueError) as exc:
            error = f"{getattr(exc, 'kind', exc.__class__.__name__)}: {exc}"
            if attempt + 1 < retries:
                sleep(pause)
    else:
        return [_check("cabinet_schema", False, error), _check("queues", False, error)]
    served = found.get("schema")
    schema_ok = served == schema_version
    queues_ok = all(isinstance(found.get(k), int) for k in ("pending", "open_waits"))
    return [
        _check("cabinet_schema", schema_ok, "" if schema_ok else f"schema {served} served, {schema_version} in the code"),
        _check("queues", queues_ok, "" if queues_ok else "pipeline.status has no queue counts"),
    ]


def run(tree: Path, *, origin: Optional[str], schema_version: int, run=subprocess.run, http=urllib.request.urlopen,
        client=None, retries: int = RETRIES, pause: float = PAUSE, which: Callable = shutil.which,
        sleep=time.sleep, postflight: str = "", e2e_runtime: Optional[Path] = None, restart: str = "") -> dict:
    """``{"ok", "checks": [{"name", "ok", "detail"}]}`` — every check of ``ORDER`` runs; a check whose tool is not on
    this host (nginx) or that has no origin (the HTTP probe) is ``ok`` with detail ``skipped``."""
    checks = [_shell("session_host", ["bash", str(Path(tree) / "common" / "install" / "session_host_setup.sh"), "check"],
                     run), _host_code(Path(tree), restart)]
    if postflight:  # kd-release's postflight refused generated agents (review F4): the release is not healthy
        checks.append(_check("generated_runtime", False, postflight[:400]))
    if which("nginx"):
        checks.append(_shell("nginx", ["nginx", "-t"], run))
    else:
        checks.append(_check("nginx", True, SKIPPED))
    checks.append(_http(origin, http) if origin else _check("cabinet_http", True, SKIPPED))
    if client is None:
        from kd.cabinet.client import Client

        client = Client()
    checks += _cabinet(client, schema_version, retries=retries, pause=pause, sleep=sleep)
    if Path(E2E_RUNTIME if e2e_runtime is None else e2e_runtime).is_dir():  # a stale runtime would test old code: its refresh did not take
        checks.append(_shell("e2e_runtime", ["bash", str(Path(tree) / "common" / "install" / "e2e_runtime_setup.sh"),
                                             "check"], run))
    else:
        checks.append(_check("e2e_runtime", True, SKIPPED))
    return {"ok": all(c["ok"] for c in checks), "checks": checks}


def failed(out: dict) -> List[str]:
    return [c["name"] for c in out["checks"] if not c["ok"]]


def notice(out: dict, rev: str, card: str, *, post) -> bool:
    """The result as the release's notice on ``card`` (or in the feed without one): healthy — info, degraded —
    critical with the failed checks and why."""
    names = [c["name"] for c in out["checks"] if c["detail"] != SKIPPED]
    if out["ok"]:
        return post(SOURCE, "info", HEALTHY, {"rev": rev, "checks": ", ".join(names)}, card)
    bad = [c for c in out["checks"] if not c["ok"]]
    why = "; ".join(f"{c['name']}: {c['detail']}" if c["detail"] else c["name"] for c in bad)
    return post(SOURCE, "critical", DEGRADED, {"rev": rev, "failed": ", ".join(c["name"] for c in bad),
                                                "why": why[:500]}, card)


def main(argv: Optional[list] = None, *, out=sys.stdout) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    options = {"--postflight": "", "--host-restart": ""}
    for flag in options:
        if flag in args:
            at = args.index(flag)
            options[flag] = args[at + 1] if at + 1 < len(args) else ""
            del args[at:at + 2]
    if len(args) not in (2, 3):
        print("usage: release_health <tree> <revision> [<card>] [--postflight <why>] [--host-restart <operation>]",
              file=sys.stderr)
        return 2
    from kd.cabinet import client as cabinet_client, config, schema

    tree, rev, card = Path(args[0]), args[1], args[2] if len(args) == 3 else ""
    result = run(tree, origin=config.origin_of(), schema_version=schema.SCHEMA_VERSION,
                 postflight=options["--postflight"], restart=options["--host-restart"])
    notice(result, rev, card, post=cabinet_client.post_notice)
    print(json.dumps(result, ensure_ascii=False), file=out)
    return 0 if result["ok"] else 1
