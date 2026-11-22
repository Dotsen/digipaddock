"""The cabinet's sockets: the local one (one JSON line in, one JSON line out, the caller is its uid) and,
since the change, the HTTP one for nginx (the web app, ``kd.cabinet.web``), both in one process on one loop.

systemd passes the listening sockets (socket activation) named ``local`` and ``http``
(``FileDescriptorName=``); without it the local path is bound here. Every call runs under one lock:
the store is a single writer and the rules read the card they write. After a card write the stamp
file is touched so the root sync path unit fires.
"""

import asyncio
import json
import os
import socket
import sys
import traceback

from kd import cap_serve
from kd.cabinet import api, projects_api, voice

SOCKET = "/run/digipaddock-cabinet/local.sock"
CHANGED = "/run/digipaddock-cabinet/changed"
MAX_REQUEST = 1 << 20
TIMEOUT = 30
FD_START = 3  # SD_LISTEN_FDS_START
NAMES = ("local", "http")


def listen_sockets() -> dict:
    """The sockets systemd passed, by name (``local``, ``http``); ``{}`` without socket activation.

    One socket under another name (the older unit is named after itself) is the local one."""
    if os.environ.get("LISTEN_PID") != str(os.getpid()):
        return {}
    try:
        count = int(os.environ.get("LISTEN_FDS", "0"))
    except ValueError:
        return {}
    names = os.environ.get("LISTEN_FDNAMES", "").split(":")
    found = {}
    for index in range(count):
        name = names[index] if index < len(names) else ""
        if name not in NAMES:
            if count != 1:
                continue
            name = "local"
        found[name] = socket.socket(fileno=FD_START + index)
    return found


def touch_changed(path=CHANGED):
    try:
        with open(path, "a"):
            pass
        os.utime(path, None)
    except OSError:
        pass


async def handle(reader, writer, *, call, principal_of, on_change=None):
    """One request: ``call(principal, request)`` runs it under the service's lock."""
    response = {"ok": False, "error": "invalid", "message": "bad request"}
    try:
        line = await asyncio.wait_for(reader.readline(), TIMEOUT)
        if len(line) > MAX_REQUEST:
            raise api.ApiError("invalid", "request too large")
        request = json.loads(line.decode("utf-8"))
        _, uid, _ = cap_serve.peer(writer.get_extra_info("socket"))
        response = await call(principal_of(uid), request)
        if on_change is not None and response.get("changed"):
            on_change()
    except api.ApiError as exc:
        response = {"ok": False, "error": exc.kind, "message": str(exc)}
        if exc.reason:
            response["reason"] = exc.reason
    except (
        ValueError,
        UnicodeDecodeError,
        asyncio.TimeoutError,
        asyncio.LimitOverrunError,
    ):
        response = {
            "ok": False,
            "error": "invalid",
            "message": "one JSON object per line is expected",
        }
    except Exception:  # noqa: BLE001 — the caller gets a plain answer, the journal the traceback
        traceback.print_exc(file=sys.stderr)
        response = {"ok": False, "error": "unavailable", "message": "internal error"}
    try:
        writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
        await writer.drain()
    except OSError:
        pass  # the caller went away; nothing to tell it
    finally:
        writer.close()


async def _local(cb, sock, path):
    if sock is not None:
        return await asyncio.start_unix_server(cb, sock=sock, limit=MAX_REQUEST)
    if os.path.exists(path):
        os.unlink(path)
    return await asyncio.start_unix_server(cb, path=path, limit=MAX_REQUEST)


async def serve(
    ctx,
    *,
    sock=None,
    path=SOCKET,
    principal_of=api.principal_of,
    on_change=touch_changed,
):
    """The local socket alone over a bare context (the older service, kept for its tests)."""
    lock = asyncio.Lock()

    async def call(principal, request):
        async with lock:
            return api.dispatch(ctx, principal, request)

    def cb(reader, writer):
        return handle(
            reader, writer, call=call, principal_of=principal_of, on_change=on_change
        )

    srv = await _local(cb, sock, path)
    async with srv:
        await srv.serve_forever()


async def serve_all(
    svc,
    *,
    local_sock=None,
    local_path=None,
    http_sock=None,
    dev_listen=None,
    principal_of=api.principal_of,
    scheduler_interval=None,
    backup_dir=None,
    host=None,
):
    """The whole service on one loop: the local socket (when given), the web app on the http socket (or on
    ``dev_listen = (host, port)`` in dev), the scheduler and, given the session ``host``'s client, the
    dispatcher of commands to it. Every write goes through ``svc``."""
    from kd.cabinet import dispatcher, scheduler

    jobs = []
    if local_sock is not None or local_path:

        def cb(reader, writer):
            return handle(reader, writer, call=svc.call, principal_of=principal_of)

        local = await _local(cb, local_sock, local_path)
        jobs.append(local.serve_forever())
    if http_sock is not None or dev_listen:
        import uvicorn

        from kd.cabinet.web import app as web_app

        options = dict(
            loop="asyncio",
            lifespan="off",
            proxy_headers=True,
            forwarded_allow_ips="*",
            ws="websockets-sansio",
            log_level="warning",
        )
        if dev_listen:
            options.update(host=dev_listen[0], port=dev_listen[1])
        web = uvicorn.Server(uvicorn.Config(web_app.create_app(svc), **options))
        jobs.append(web.serve(sockets=[http_sock] if http_sock is not None else None))
    if host is not None:
        jobs.append(dispatcher.run(svc, host=host))
    jobs.append(voice.run(svc))  # people's audio to text, one at a time; off — it closes the line
    jobs.append(
        scheduler.run(
            svc,
            interval=scheduler_interval or scheduler.INTERVAL,
            backup_dir=backup_dir,
        )
    )
    await asyncio.gather(*jobs)


def backfill_signed(admin, err) -> None:
    """the live grants approved before the signed copies get theirs; a failure only leaves those grants
    refusing their steps (a new approval is needed), the cabinet starts anyway."""
    from kd.clock import utcnow

    try:
        lines = admin.backfill(utcnow())
    except (ValueError, OSError) as exc:  # the grant store's and the copies' errors are ValueErrors
        lines = [f"not done ({exc.__class__.__name__}: {exc})"]
    for line in lines:
        err.write(f"kd-cabinet: signed copies: {line}\n")


def build(db, registry, repo_root, *, cfg, err=sys.stderr):
    """The production service: the base, people, decisions with their effects (the admin grant store and the
    BOSS proposals, reached in-process), notifications and the WebSocket hub. The base's projects table is the
    board's projects: a start never rewrites it."""
    from kd import admin_cli, boss_promote, pipeline_config, session_config
    from kd.cabinet import client, decisions, notify, service, store, users
    from kd.cabinet.web import ws

    base = store.Store.open(db)
    people = users.Users(base)
    ctx = api.Context(base, registry, repo_root, users=people)
    ctx.files_dir = cfg.files_dir  # the agents' kd-task file get|add read and write the web's files
    # people's audio to text: cabinet.json "transcribe": false turns it off
    ctx.voice = voice.Settings(enabled=cfg.transcribe, language=cfg.language)
    ctx.repo_doorbell = projects_api.DOORBELL  # a repository request rings the root unit kd-repo-attach
    if base.repo_requests():  # requests the unit could not take while the cabinet was away
        projects_api.ring(ctx)
    ctx.pipeline = pipeline_config.Cached().get  # the root's mode file, re-read when it changes
    api.install_host(ctx, session_config.Cached().get)  # the session host's agents get commands
    cabinet = client.LocalClient(ctx, api.Principal("cabinet", os.getuid(), "cabinet"))
    try:
        ctx.admin = admin_cli.default_service(client=cabinet)
    except (ImportError, KeyError, OSError, ValueError) as exc:
        # no sysadmin on this host: admin decisions answer 503, the rest works
        err.write(
            f"kd-cabinet: admin approvals are off ({exc.__class__.__name__}: {exc})\n"
        )
    else:
        backfill_signed(ctx.admin, err)
    ctx.promote = boss_promote.Settings(client=cabinet)
    svc = service.Service(
        ctx,
        cfg,
        users=people,
        decisions=decisions.Decisions(base),
        notifier=notify.Notifier(base, people, cfg),
        on_change=touch_changed,
    )
    svc.hub = ws.Hub(svc)
    return svc
