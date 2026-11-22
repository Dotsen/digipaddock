"""The cabinet's own clock: every ``interval`` seconds, expire overdue decisions, run the
notification tick (batches, reminders, the morning digest, the purge) and, once a day from 03:30 in
the cabinet's time zone, copy the base to ``backups/cabinet-YYYYMMDD.db`` (seven are kept); then the pipeline's
sweep and the monitor's purge (the checks' history keeps 7 days).

Each step goes through the service's lock like any other write; a failing step reaches the journal
and the next tick tries again.
"""

import asyncio
import os
import re
import sqlite3
import sys
import traceback
from datetime import time
from typing import Optional
from zoneinfo import ZoneInfo

from kd.cabinet import api, users

INTERVAL = 30
BACKUP_AT = time(3, 30)
KEEP = 7
BACKUP_NAME = re.compile(r"cabinet-\d{8}\.db")
CABINET = api.Principal("cabinet", -1, "cabinet")


def backup_dir_of(store_) -> str:
    return os.path.join(os.path.dirname(store_.path()), "backups")


def backup(store_, directory: str, now: str, timezone: str) -> Optional[str]:
    """Today's copy of the base when it is due and not made yet (the path), else None."""
    local = users.parse_time(now).astimezone(ZoneInfo(timezone))
    if local.time() < BACKUP_AT:
        return None
    target = os.path.join(directory, f"cabinet-{local:%Y%m%d}.db")
    if os.path.exists(target):
        return None
    os.makedirs(directory, mode=0o700, exist_ok=True)
    partial = target + ".part"
    if os.path.exists(partial):
        os.unlink(partial)
    copy = sqlite3.connect(partial)
    try:
        store_._conn.backup(copy)
    finally:
        copy.close()
    os.chmod(partial, 0o600)
    os.replace(partial, target)
    kept = sorted(n for n in os.listdir(directory) if BACKUP_NAME.fullmatch(n))
    for old in kept[:-KEEP]:
        os.unlink(os.path.join(directory, old))
    return target


async def tick(svc, *, backup_dir: str) -> dict:
    expired = (await svc.call(CABINET, {"op": "decision.expire"}))["decisions"]
    ticked = None
    if svc.notifier is not None:
        ticked = (await svc.call(CABINET, {"op": "notify.tick"}))["tick"]
    made = await svc.locked(
        lambda: backup(
            svc.ctx.store, backup_dir, svc.now(), svc.cfg.timezone if svc.cfg else "UTC"
        )
    )
    swept = None
    try:  # what the events missed: last, so its failure never costs the push tick or the backup
        swept = await svc.call(CABINET, {"op": "pipeline.sweep"})
    except Exception:  # noqa: BLE001 — the next tick sweeps again; the journal has the traceback
        traceback.print_exc(file=sys.stderr)
    purged = None
    try:  # the checks' history past 7 days, the same way
        purged = await svc.call(CABINET, {"op": "monitor.purge"})
    except Exception:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
    return {"expired": expired, "tick": ticked, "backup": made, "pipeline": swept, "monitor": purged}


async def run(
    svc,
    *,
    interval: float = INTERVAL,
    backup_dir: Optional[str] = None,
    sleep=asyncio.sleep,
):
    backup_dir = backup_dir or backup_dir_of(svc.ctx.store)
    while True:
        try:
            await tick(svc, backup_dir=backup_dir)
        except Exception:  # noqa: BLE001 — the next tick tries again
            traceback.print_exc(file=sys.stderr)
        await sleep(interval)
