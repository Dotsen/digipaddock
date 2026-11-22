"""A cabinet store with its projects, an agent registry and principals for the executors' tests."""

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

from kd import admin_approval, boss_promote
from kd import admin_operations as ops
from kd.cabinet import api, store, users
from admin_samples import CATALOG, ENABLED, INVENTORY


def totp_codes(b32: str) -> list:
    """The codes of the previous, the current and the next step: a person's codes are taken once (RFC 6238 §5.2,
    global review 15.10.2026), so a test that needs several uses them in this order."""
    from datetime import timedelta

    import pyotp

    totp, now = pyotp.TOTP(b32), datetime.now(timezone.utc)
    return [totp.at(now + timedelta(seconds=30 * k)) for k in (-1, 0, 1)]

MONITOR_CONFIG = Path(__file__).resolve().parents[2] / "agents" / "sysadmin" / "tests" / "fixtures" / "monitor-config.json"  # the overlay's config.json, mapped to synthetic servers
ROUTES = {"digipaddock": "@dev", "admin": "@sysadmin", "web": "@owner", "alert": "@owner"}
# the board's projects as the cabinet's table holds them (its only source)
PROJECTS = [{"id": p, "name": p.upper(), "route": r} for p, r in ROUTES.items()]
AGENTS = {
    "analyst": {"user": "digi-analyst"},
    "dev": {"user": "digi-dev"},
    "sysadmin": {"user": "digi-sysadmin"},
    "boss": {"user": "digi-boss"},
}
UIDS = {
    "root": 0,
    "digi": 1000,
    "digi-exec": 1001,
    "digi-approve": 1002,
    "digi-cabinet": 1003,
    "digi-push": 1004,
    "digi-forge": 1005,
    "digi-observe": 1006,
    "digi-dev": 2001,
    "digi-boss": 2002,
    "digi-sysadmin": 2003,
    "digi-analyst": 2004,
    "nobody": 65534,
}
SERVICES = {"exec": "digi-exec", "forge": "digi-forge", "cabinet": "digi-cabinet", "push": "digi-push",
            "observe": "digi-observe"}  # digi-approve: retired, nobody


class CabinetEnv:
    def __init__(self, tmp_path: Path, repo_root=None):
        tmp_path = Path(tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.now = "2026-10-14T10:00:00Z"
        self.store = store.Store.open(str(tmp_path / "cabinet.db"), clock=self.clock)
        self.registry = tmp_path / "agents.json"
        self.registry.write_text(json.dumps(AGENTS), encoding="utf-8")
        self.store.seed_projects(PROJECTS)
        self.users = users.Users(self.store)
        self.ctx = api.Context(
            self.store,
            str(self.registry),
            repo_root and str(repo_root),
            require_root_owner=False,
            now=lambda: self.now,
            users=self.users,
        )
        # the host's repository registry is not the test's (project.list shows what it binds)
        self.ctx.repos_path = str(tmp_path / "repos.json")
        # the monitor's defaults are the repository's config.json, never the host's deployed one
        self.ctx.monitor_config = str(MONITOR_CONFIG)
        # the decisions' effects: the admin grant store and the BOSS promotions, in temp dirs
        cabinet = self.client("digi-cabinet")
        admin = tmp_path / "admin"
        for sub in ("proposals", "grants"):
            (admin / sub).mkdir(parents=True, exist_ok=True)
        self.admin = admin_approval.Service(
            admin_approval.Paths(proposals=str(admin / "proposals"), grants=str(admin / "grants")),
            catalog=lambda: ops.Catalog.from_dict(copy.deepcopy(CATALOG)),
            inventory=lambda: INVENTORY,
            enabled=lambda: ENABLED,
            client=cabinet,
            deny=lambda command, alias: None,
        )
        promote = tmp_path / "promote"
        for sub in ("proposals", "decisions", "grants", "children"):
            (promote / sub).mkdir(parents=True, exist_ok=True)
        (promote / "base.json").write_text("{}", encoding="utf-8")
        self.promote = boss_promote.Settings(
            proposals=str(promote / "proposals"),
            decisions=str(promote / "decisions"),
            client=cabinet,
            registry_path=str(self.registry),
            audit_path=str(promote / "boss.jsonl"),
            channel_switch=str(promote / "base.json"),
            grants=str(promote / "grants"),
            children=str(promote / "children"),
        )
        self.ctx.admin, self.ctx.promote = self.admin, self.promote

    def clock(self):
        return self.now

    def when(self) -> datetime:
        return datetime.strptime(self.now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    def epoch(self) -> float:
        return self.when().timestamp()

    def principal(self, user: str) -> api.Principal:
        names = {uid: name for name, uid in UIDS.items()}
        return api.principal_of(
            UIDS[user],
            getpwuid=lambda uid: type("pw", (), {"pw_name": names[uid]})(),
            registry=str(self.registry),
            base="digi",
            services=SERVICES,
        )

    def call(self, user: str, request: dict) -> dict:
        return api.dispatch(self.ctx, self.principal(user), request)

    def card(self, **over):
        values = {
            "title": "Card",
            "project": "digipaddock",
            "status": "To Do",
            "assignee": "@dev",
            "created_by": "@owner",
        }
        values.update(over)
        return self.store.create_card(values, actor="user:owner")

    def briefed(self, description="Цель: отчёт.", ac=("Есть отчёт",), **over):
        """A card the analyst handed to @dev in Inbox with a ready brief, as ``kd-task handoff`` writes it: v1."""
        card = self.card(**{"status": "Inbox", "assignee": "@analyst", "labels": ["spec:task-brief"], **over})
        changes = {
            "description": description,
            "acceptance": [{"text": t, "checked": False} for t in ac],
            "assignee": "@dev",
            "labels_remove": ["spec:task-brief"],
            "labels_add": ["spec:implementer", "brief-ready"],
        }
        return self.store.edit_card(card.id, None, changes, actor="agent:analyst")

    def put(self, card_id: str, **fields) -> int:
        """A card under a fixed id, as the md tests named theirs: imported on first use, then edited by the
        owner (each call one new revision); returns the card's revision."""
        try:
            self.store.card(card_id)
        except store.NotFound:
            values = {
                "title": "Card",
                "project": "digipaddock",
                "status": "To Do",
                "assignee": "@dev",
                "created_by": "@owner",
            }
            values.update(fields)
            record = {
                **values,
                "id": card_id,
                "n": int(card_id.split("-")[1]),
                "created": self.now,
                "updated": self.now,
            }
            return self.store.import_card(record, [], actor="user:owner").revision
        return self.store.edit_card(card_id, None, fields, actor="user:owner").revision

    def client(self, user: str):
        """The cabinet as ``user`` reaches its socket (a JSON round-trip, like the socket)."""
        from kd.cabinet import client as cabinet_client

        return cabinet_client.LocalClient(self.ctx, self.principal(user))
