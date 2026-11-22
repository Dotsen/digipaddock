"""Monitoring read models and explicit read-only incident investigations.

The incident log and the last observation have different lifetimes. Enrichment never rewrites history,
and creating an investigation never approves an administration plan or a server command.
"""

import base64
import hashlib
import json
from datetime import datetime

from kd import monitor_settings as ms
from kd.cabinet import api, incidents, schema, store

INVESTIGATION = "monitor:investigation"
# The sysadmin works an investigation by its procedure; a transferred card leaves its old selection behind.
TRIAGE_SPEC = "spec:incident-triage"


def _triage_labels(labels) -> list:
    return sorted(
        {v for v in labels if not v.startswith(("spec:", "skill:"))}
        | {INVESTIGATION, TRIAGE_SPEC}
    )


def config(ctx) -> dict:
    from kd.cabinet import monitor_config

    try:
        return ms.merge(monitor_config.defaults(ctx), monitor_config.edits(ctx.store))
    except api.ApiError:
        return {}


def schedule(check: str, subject: str = "") -> str:
    if check == "health":
        check = subject
    if check.startswith("gate:"):
        check = check[5:]
    return "sec" if check == "posture" or check.startswith("sec:") else check


def age_seconds(at: str, now: str) -> float | None:
    if not at:
        return None
    try:
        return (
            datetime.fromisoformat(now.replace("Z", "+00:00"))
            - datetime.fromisoformat(at.replace("Z", "+00:00"))
        ).total_seconds()
    except (ValueError, TypeError):
        return None


def check_view(ctx, row: dict, cfg: dict) -> dict:
    name = schedule(row["check"], row.get("subject", ""))
    interval = int((cfg.get("every_min") or {}).get(name, 5)) * 60
    disabled = name in ms.disabled(cfg, row["server"])
    elapsed = age_seconds(row.get("at", ""), ctx.now())
    run = ctx.store._conn.execute(
        'SELECT * FROM check_runs WHERE server=? AND "check"=?',
        (row["server"], row["check"]),
    ).fetchone()
    freshness = (
        "disabled"
        if disabled
        else "unknown"
        if elapsed is None
        else (
            "stale"
            if elapsed > interval + max(600, int(cfg.get("call_timeout_s", 30)))
            else "fresh"
        )
    )
    if not disabled and run and run["state"] not in ("success", "not_due"):
        freshness = "error"
    elif not disabled and row.get("timestamp_verified", 1) == 0:
        freshness = "unknown"
    thresholds = None
    if name == "disk":
        thresholds = {**cfg.get("disk_pct", {}), "unit": "percent"}
    elif name == "reboot":
        thresholds = {
            "warn": cfg.get("security", {}).get("reboot_warn_days", 7),
            "unit": "days",
        }
    elif name == "mem" and row.get("subject") == "ram":
        mb = (cfg.get("mem_available_mb") or {}).get(row["server"])
        thresholds = {
            **(mb or cfg.get("mem_available_pct", {})),
            "unit": "mb" if mb else "percent",
        }
    return {
        **row,
        "freshness": freshness,
        "interval_s": interval,
        "attempted_at": run["attempted_at"] if run else row.get("at", ""),
        "run_state": run["state"] if run else "unknown",
        "run_detail": run["detail"] if run else "",
        "thresholds": thresholds,
        "recovery_runs": 1
        if name in cfg.get("every_min", {}) or row.get("fixed_card")
        else cfg.get("close_after", 3),
    }


def server_view(ctx, row: dict, cfg: dict) -> dict:
    at = row.get("last_ok", "") if row["reachable"] else row.get("polled_at", "")
    elapsed = age_seconds(at, ctx.now())
    freshness = (
        "disabled"
        if not row["enabled"]
        else "unknown"
        if elapsed is None
        else ("stale" if elapsed > 900 else "fresh")
    )
    checks = [
        check_view(ctx, dict(c), cfg)
        for c in ctx.store._conn.execute(
            'SELECT * FROM server_checks WHERE server=? ORDER BY "check",subject',
            (row["alias"],),
        )
    ]
    useful = [
        c
        for c in checks
        if not c["check"].startswith("gate:")
        and c["check"] != "health"
        and c["freshness"] != "disabled"
    ]
    attention = [
        c
        for c in checks
        if c["freshness"] != "disabled"
        and (c["level"] != "ok" or c["freshness"] != "fresh")
    ]
    attention.sort(
        key=lambda c: (c["level"] != "crit", c["level"] != "warn", c["check"])
    )
    full = ctx.store._conn.execute(
        "SELECT value FROM meta WHERE key='monitor_full_at'"
    ).fetchone()
    full_age = age_seconds(full[0], ctx.now()) if full else None
    baseline = ctx.store._conn.execute(
        "SELECT value FROM meta WHERE key='monitor_baseline_state'"
    ).fetchone()
    levels = {c["level"] for c in checks if c["freshness"] != "disabled"}
    return {
        **row,
        "worst": next(
            (level for level in ("crit", "warn", "ok") if level in levels), ""
        ),
        "freshness": freshness,
        "observed_at": at,
        "user_checks": {
            level: sum(c["level"] == level for c in useful)
            for level in incidents.LEVELS
        },
        "attention_count": len(attention),
        "reason": attention[0] if attention else None,
        "coverage_freshness": "fresh"
        if useful
        and all(c["freshness"] == "fresh" for c in useful)
        and (ms.planned(cfg, row["alias"]) - ms.disabled(cfg, row["alias"]))
        <= {schedule(c["check"], c["subject"]) for c in useful}
        else "unknown",
        "monitor_freshness": "fresh"
        if full_age is not None and full_age <= 900
        else "unknown",
        "baseline_state": baseline[0] if baseline else "unknown",
    }


def linked_cards(ctx, principal, n: int) -> list:
    cards = incidents._cards(
        ctx,
        principal,
        "SELECT card FROM incident_cards WHERE incident=? UNION SELECT id FROM cards"
        " WHERE EXISTS (SELECT 1 FROM json_each(cards.labels) WHERE value=?)"
        " OR id=(SELECT fixed_card FROM incidents WHERE n=?)",
        (n, f"P-{n}", n),
    )
    primary = ctx.store._conn.execute(
        "SELECT card FROM incident_primary WHERE incident=?", (n,)
    ).fetchone()
    return sorted(cards, key=lambda c: not (primary and c["id"] == primary[0]))


def incident_view(ctx, principal, row: dict, cfg: dict) -> dict:
    from kd.cabinet import incident_reports, monitor_trust

    checks = ctx.store._conn.execute(
        'SELECT * FROM server_checks WHERE server=? AND "check"=? AND subject IN (?,?)'
        " ORDER BY subject=? DESC LIMIT 1",
        (
            row["server"],
            row["check"],
            row["subject"],
            "" if row["subject"] == "-" else row["subject"],
            row["subject"],
        ),
    ).fetchone()
    current = (
        check_view(ctx, dict(checks), cfg)
        if checks is not None and row["status"] != "resolved"
        else None
    )
    cards = linked_cards(ctx, principal, row["n"])
    active = [c for c in cards if c["status"] not in schema.TERMINAL]
    command = None
    for card in active:
        found = ctx.store._conn.execute(
            "SELECT id, state, error AS detail FROM commands WHERE card=? AND agent=?"
            " AND key LIKE ? ORDER BY created DESC, rowid DESC LIMIT 1",
            (card["id"], incidents.MONITOR_AGENT, f"incident:{row['n']}:%"),
        ).fetchone()
        if found is not None:
            command = dict(found)
            break
    baseline = None
    if row["check"].startswith("sec:"):
        found = ctx.store._conn.execute(
            "SELECT label FROM server_trust WHERE scope=? AND kind='snapshot' AND item=?",
            (row["server"], row["check"][4:] + ":" + row["subject"]),
        ).fetchone()
        if found:
            baseline = found[0]
    # what the owner accepts now may differ from what opened the incident (L6); his recheck's state (L7)
    changes = ctx.store._conn.execute(
        "SELECT COUNT(*), COALESCE(MAX(json_extract(payload, '$.at')), '') FROM events"
        " WHERE kind='incident.changed' AND json_extract(payload, '$.incident')=?", (row["n"],)).fetchone()
    return {
        **row,
        "baseline_value": baseline,
        "current": current,
        "cards": cards,
        "active_cards": len(active),
        "assignee": active[0]["assignee"] if active else "",
        "delivery": command,
        "investigation": incident_reports.investigation(ctx.store, row),
        "freshness": current["freshness"] if current else "unknown",
        "changes": {"count": changes[0], "last_at": changes[1]},
        "recheck": incidents._last_request(ctx.store, row["server"], ctx.now()),
        # signed into the baseline, the monitor's tick has not closed it yet — no second accept
        "accepted": monitor_trust.accepted(ctx.store, row["n"]) if row["status"] != "resolved" else None,
    }


def group_sizes(db, pairs) -> dict:
    """(opened_at, check) → its group when one tick opened more than one incident of that check, any status:
    ``{"id": "<opened_at>|<check>", "size", "servers", "subjects"}``."""
    moments = sorted({at for at, _ in pairs})
    if not moments:
        return {}
    marks = ",".join("?" for _ in moments)
    out = {}
    for at, check, size, servers, subjects in db.execute(
            f'SELECT opened_at, "check", COUNT(*), COUNT(DISTINCT server), COUNT(DISTINCT subject) FROM incidents'
            f" WHERE opened_at IN ({marks}) GROUP BY 1, 2", moments):
        if size > 1 and (at, check) in pairs:
            out[(at, check)] = {"id": f"{at}|{check}", "size": size, "servers": servers, "subjects": subjects}
    return out


def _filters(ctx, principal, request: dict) -> tuple[list, list]:
    where, args = [], []
    status = request.get("status", "active")
    if not isinstance(status, str):
        raise api.ApiError("invalid", "unknown incident status")
    if status == "active":
        where.append("status!='resolved'")
    elif status not in ("", "all"):
        if status not in incidents.STATUSES:
            raise api.ApiError("invalid", "unknown incident status")
        where.append("status=?")
        args.append(status)
    for key in ("server", "level", "check"):
        value = request.get(key)
        if value:
            if (
                not isinstance(value, str)
                or len(value) > 200
                or (key == "level" and value not in ("warn", "crit"))
            ):
                raise api.ApiError("invalid", f"invalid {key}")
            where.append(f'"{key}"=?')
            args.append(value)
    group = request.get("group")
    if group:  # «<opened_at>|<check>» — the incidents one tick opened for one check
        opened_at, _, check = group.partition("|") if isinstance(group, str) else ("", "", "")
        try:
            opened_at = incidents.utc(opened_at)
        except (incidents.ItemError, TypeError):
            raise api.ApiError("invalid", "group is <opened_at>|<check>")
        if not check or len(check) > 200:
            raise api.ApiError("invalid", "group is <opened_at>|<check>")
        where.append('opened_at=? AND "check"=?')
        args.extend((opened_at, check))
    query = request.get("q", "")
    if not isinstance(query, str) or len(query) > 200:
        raise api.ApiError("invalid", "q is text of at most 200 characters")
    if query:
        where.append(
            "(instr(lower('P-' || n || ' ' || server || ' ' || \"check\" || ' ' || subject || ' ' || text),?)>0)"
        )
        args.append(query.lower())
    for key, operator in (("since", ">="), ("until", "<=")):
        value = request.get(key)
        if value:
            try:
                value = incidents.utc(
                    value + ("T23:59:59Z" if key == "until" else "T00:00:00Z")
                    if len(value) == 10
                    else value
                )
            except (incidents.ItemError, TypeError):
                raise api.ApiError("invalid", f"{key} is an ISO date or timestamp")
            where.append(f"opened_at{operator}?")
            args.append(value)
    assignee, processing = request.get("assignee", ""), request.get("processing", "")
    if not isinstance(assignee, str) or len(assignee) > 200:
        raise api.ApiError("invalid", "invalid assignee")
    if processing not in ("", "unassigned", "assigned", "waiting", "recheck"):
        raise api.ApiError("invalid", "invalid processing filter")
    if assignee or processing in ("unassigned", "assigned", "waiting"):
        visible = sorted(
            p for p in ctx.routing().projects if api.visible(ctx, principal, p)
        )
        marks = ",".join("?" for _ in visible) or "NULL"
        related = (
            "(EXISTS (SELECT 1 FROM incident_cards ic WHERE ic.incident=incidents.n AND ic.card=c.id)"
            " OR EXISTS (SELECT 1 FROM json_each(c.labels) WHERE value='P-' || incidents.n))"
        )
        exists = (
            f"SELECT 1 FROM cards c WHERE c.project IN ({marks}) AND c.status NOT IN ('Done','Cancelled') AND "
            + related
        )
        work_args = list(visible)
        if assignee:
            exists += " AND c.assignee=?"
            work_args.append(assignee)
        if processing == "waiting":
            exists += " AND EXISTS (SELECT 1 FROM decisions d WHERE d.card=c.id AND d.state='open')"
        where.append(
            ("NOT " if processing == "unassigned" else "") + "EXISTS (" + exists + ")"
        )
        args.extend(work_args)
    if processing == "recheck":
        where.append("fixed_card!='' AND status!='resolved'")
    return where, args


def _query(ctx, principal, request) -> dict:
    incidents._reader(ctx, principal, request)
    where, args = _filters(ctx, principal, request)
    clause = " WHERE " + " AND ".join(where) if where else ""
    limit = request.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise api.ApiError("invalid", "limit is between 1 and 100")
    fingerprint = hashlib.sha256(json.dumps([where, args]).encode()).hexdigest()[:16]
    offset, top = (
        0,
        ctx.store._conn.execute("SELECT COALESCE(MAX(n),0) FROM incidents").fetchone()[
            0
        ],
    )
    if request.get("cursor"):
        try:
            if not isinstance(request["cursor"], str) or len(request["cursor"]) > 1000:
                raise ValueError
            cursor = json.loads(base64.urlsafe_b64decode(request["cursor"]))
            if cursor["filter"] != fingerprint or not all(
                type(cursor[k]) is int and 0 <= cursor[k] < 2**63
                for k in ("offset", "top")
            ):
                raise ValueError
            offset, top = cursor["offset"], cursor["top"]
        except (ValueError, TypeError, KeyError):
            raise api.ApiError("invalid", "invalid incident cursor")
    bounded = clause + (" AND " if clause else " WHERE ") + "n<=?"
    db = ctx.store._conn
    total = db.execute(
        "SELECT COUNT(*) FROM incidents" + bounded, (*args, top)
    ).fetchone()[0]
    counts = dict(
        db.execute(
            "SELECT level, COUNT(*) FROM incidents" + bounded + " GROUP BY level",
            (*args, top),
        )
    )
    rows = db.execute(
        "SELECT * FROM incidents"
        + bounded
        + " ORDER BY status='resolved', level='crit' DESC, opened_at, n LIMIT ? OFFSET ?",
        (*args, top, limit, offset),
    ).fetchall()
    cfg = config(ctx)
    items = [incident_view(ctx, principal, incidents._row(row), cfg) for row in rows]
    groups = group_sizes(db, {(i["opened_at"], i["check"]) for i in items})
    for item in items:
        if (item["opened_at"], item["check"]) in groups:
            item["group"] = groups[(item["opened_at"], item["check"])]
    next_cursor = (
        base64.urlsafe_b64encode(
            json.dumps(
                {"filter": fingerprint, "offset": offset + len(rows), "top": top}
            ).encode()
        ).decode()
        if offset + len(rows) < total
        else ""
    )
    return {
        "items": items,
        "total": total,
        "counts": counts,
        "next_cursor": next_cursor,
    }


def _handoff(ctx, principal, request) -> dict:
    incidents._reader(ctx, principal)
    if principal.kind != "user":
        raise api.ApiError(
            "refused", "only a person requests an incident investigation"
        )
    project = request.get("project")
    if not isinstance(project, str) or project not in ctx.routing().projects:
        raise api.ApiError("invalid", "choose a registered project")
    api._require(ctx, principal, "cards.create", project)
    api._assignee(ctx, "@" + incidents.MONITOR_AGENT)
    comment = api._text(request.get("comment", ""), "comment", limit=2000, empty=True)
    actor, s = api.card_actor(principal), ctx.store
    with s.transaction():
        found = incidents._active(ctx, request)
        for card in linked_cards(ctx, principal, found["n"]):
            if card["status"] not in schema.TERMINAL:
                return {"card": card, "repeat": True}
        # Hidden work must neither leak nor be duplicated by somebody who cannot read its project.
        hidden = [
            c
            for c in linked_cards(ctx, api.Principal("root", 0, "root"), found["n"])
            if c["status"] not in schema.TERMINAL
        ]
        if hidden:
            raise api.ApiError(
                "refused", "an investigation exists in a project you cannot access"
            )
        pid = f"P-{found['n']}"
        values = {
            "title": f"{found['server']}: разобрать {pid} ({found['check']} {found['subject']})"[
                : store.TITLE_MAX
            ],
            "project": project,
            "status": "Inbox",
            "assignee": "@" + incidents.MONITOR_AGENT,
            "created_by": f"@{principal.user}",
            "servers": [found["server"]],
            "labels": [pid, INVESTIGATION, TRIAGE_SPEC],
            "description": f"Разобрать инцидент [{pid}](/incidents/{pid})\n\n"
            f"Сервер: {found['server']}\nПроверка: {found['check']} {found['subject']}\n"
            f"Наблюдение на {found['last_seen']}: {found['value']}\n{found['text']}\n\n"
            "Цель: диагностика только для чтения, причина, последствия и предложение решения. "
            "Изменения сервера — только после отдельного одобрения плана владельцем.\n\n"
            + comment,
        }
        card = s.create_card(values, actor=actor)
        s._conn.execute(
            "INSERT INTO incident_cards VALUES (?,?,?,?)",
            (found["n"], card.id, actor, ctx.now()),
        )
        s._conn.execute(
            "INSERT INTO incident_primary VALUES (?,?) ON CONFLICT(incident) DO UPDATE SET card=excluded.card",
            (found["n"], card.id),
        )
        command = s._enqueue_in_tx(
            {
                "key": f"incident:{found['n']}:{card.id}",
                "agent": incidents.MONITOR_AGENT,
                "thread": card.thread,
                "card": card.id,
                "kind": "assigned",
                "initiator": actor,
                "text": f"[monitor] {card.id}: read the linked incident and investigate read-only. "
                "Report evidence, impact and the proposed next step in this card. "
                "Do not change servers or accept a baseline without a separate owner approval.",
            }
        )
        incidents._event(
            s,
            actor,
            "assigned",
            found["n"],
            card=card.id,
            server=found["server"],
            at=ctx.now(),
        )
    return {"card": card.to_dict(), "repeat": False, "delivery": command["state"]}


def _link(ctx, principal, request) -> dict:
    incidents._reader(ctx, principal)
    transfer = request.get("transfer", False)
    if not isinstance(transfer, bool):
        raise api.ApiError("invalid", "transfer is a boolean")
    actor, s = api.card_actor(principal), ctx.store
    with s.transaction():
        found = incidents._active(ctx, request)
        record = api._visible_card(ctx, principal, request.get("card"))
        api._require(ctx, principal, "cards.edit", record.project)
        if record.terminal:
            raise api.ApiError("invalid", "choose an active card")
        changes = {
            "servers": sorted(set(record.servers) | {found["server"]}),
            "labels": sorted(set(record.labels) | {f"P-{found['n']}"}),
        }
        if transfer:
            changes.update(
                {
                    "status": "Inbox",
                    "assignee": "@" + incidents.MONITOR_AGENT,
                    "labels": _triage_labels(changes["labels"]),
                }
            )
        # Reuse the normal card writer: project permissions, revision checks and reassignment cleanup apply.
        result = api._user_card_edit(
            ctx,
            principal,
            {
                "card": record.id,
                "revision": request.get("revision"),
                "changes": changes,
                "override_reason": f"Передача инцидента P-{found['n']} на диагностику только для чтения",
            },
        )
        s._conn.execute(
            "INSERT OR IGNORE INTO incident_cards VALUES (?,?,?,?)",
            (found["n"], record.id, actor, ctx.now()),
        )
        s._conn.execute(
            "INSERT INTO incident_primary VALUES (?,?) ON CONFLICT(incident) DO UPDATE SET card=excluded.card",
            (found["n"], record.id),
        )
        if transfer:
            s._enqueue_in_tx(
                {
                    "key": f"incident:{found['n']}:{record.id}",
                    "agent": incidents.MONITOR_AGENT,
                    "thread": record.thread,
                    "card": record.id,
                    "kind": "assigned",
                    "initiator": actor,
                    "text": f"[monitor] {record.id}: read the linked incident and investigate read-only. "
                    "Report evidence, impact and the proposed next step in this card. "
                    "Do not change servers or accept a baseline without a separate owner approval.",
                }
            )
        incidents._event(
            s,
            actor,
            "assigned" if transfer else "linked",
            found["n"],
            card=record.id,
            server=found["server"],
            at=ctx.now(),
        )
    return {"card": result["card"]}


def _retry(ctx, principal, request) -> dict:
    incidents._reader(ctx, principal)
    with ctx.store.transaction():
        found = incidents._active(ctx, request)
        cards = [
            c
            for c in linked_cards(ctx, principal, found["n"])
            if c["status"] not in schema.TERMINAL
        ]
        if not cards:
            raise api.ApiError("invalid", "no active investigation")
        card = api._visible_card(ctx, principal, cards[0]["id"])
        api._require(ctx, principal, "cards.edit", card.project)
        if card.assignee != "@" + incidents.MONITOR_AGENT:
            raise api.ApiError("invalid", "the investigation was reassigned")
        rows = ctx.store.commands(card=card.id, agent=incidents.MONITOR_AGENT)
        previous = next(
            (r for r in rows if r["key"].startswith(f"incident:{found['n']}:")), None
        )
        if previous is None:
            raise api.ApiError("invalid", "no investigation delivery to retry")
        if previous["state"] not in ("failed", "cancelled"):
            return {"delivery": previous["state"], "repeat": True}
        queued = ctx.store._enqueue_in_tx(
            {
                "key": f"incident:{found['n']}:{card.id}:retry:{previous['id']}",
                "agent": incidents.MONITOR_AGENT,
                "thread": card.thread,
                "card": card.id,
                "kind": "assigned",
                "text": previous["text"],
                "initiator": api.card_actor(principal),
            }
        )
    return {"delivery": queued["state"], "repeat": False}


api.OPS.update(
    {
        "incident.query": (incidents.READERS, _query),
        "incident.handoff": (frozenset({"user"}), _handoff),
        "incident.link": (frozenset({"user"}), _link),
        "incident.retry": (frozenset({"user"}), _retry),
    }
)
