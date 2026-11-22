"""the board of the admin tests — the Backlog.md samples imported into a cabinet store.

The samples stay md text (``admin_samples.CARD``) so a test can still change a card by editing its
text: ``put(text)`` replaces that card, bumps its revision and keeps every other card as the store
holds it (a promote included). The board is reached as a service user, the way the code under test
reaches the socket: the cabinet's web as the cabinet user, the executors as the exec user,
``kd-admin-propose`` as the sysadmin.
"""

import copy
from pathlib import Path

from admin_samples import CATALOG, ENABLED, INVENTORY
from cabinet_env import CabinetEnv

from kd import admin_approval, admin_task
from kd import admin_operations as ops
from kd.cabinet import client as cabinet_client

import legacy_md


STAMP = "2026-10-09 16:26"  # the creation date of a sample without one (Backlog.md always writes it)


def record_of(text: str, revision: int) -> dict:
    """The cabinet record of one md card, as the cut-over import made it."""
    if "\ncreated_date:" not in text:
        text = text.replace("\n---\n", f"\ncreated_date: '{STAMP}'\n---\n", 1)
    record, _, warnings = legacy_md.parse_card(text.encode("utf-8"))
    if warnings:
        raise ValueError(f"the sample does not import cleanly: {warnings}")
    return {**record, "revision": revision}


class AdminBoard:
    """A cabinet store of md sample cards; a client (``call``) as one service user."""

    def __init__(self, root, *texts, user="digi-cabinet", admin_root=None, catalog=None):
        self.root, self.user, self.generation, self.cab = Path(root), user, 0, None
        self.admin_root, self.catalog = admin_root, catalog if catalog is not None else CATALOG
        for text in texts:
            self.put(text)

    def put(self, text: str) -> None:
        """Replace (or add) the card of ``text``: a new revision, the other cards as the store holds them."""
        new = record_of(text, 1)
        kept = []
        if self.cab is not None:
            for record in self.cab.store.cards():
                if record.id == new["id"]:
                    new["revision"] = record.revision + 1
                else:
                    kept.append(record.to_dict())
            self.cab.store.close()
        self.generation += 1
        self.cab = CabinetEnv(self.root / f"gen{self.generation}")
        if self.admin_root is not None and (self.admin_root / "proposals").is_dir():
            self.cab.admin = self.cab.ctx.admin = admin_approval.Service(
                admin_approval.Paths(proposals=str(self.admin_root / "proposals"),
                                     grants=str(self.admin_root / "grants")),
                catalog=lambda: ops.Catalog.from_dict(copy.deepcopy(self.catalog)), inventory=lambda: INVENTORY,
                enabled=lambda: ENABLED, client=self.cab.client("digi-cabinet"), deny=lambda command, alias: None)
        for record in kept + [new]:
            self.cab.store.import_card(record, [], actor="user:owner")

    def call(self, request: dict) -> dict:
        return cabinet_client.LocalClient(
            self.cab.ctx, self.cab.principal(self.user)
        ).call(request)

    def as_user(self, user: str) -> "_As":
        """A client of the same board as another user (the sysadmin's kd-admin-propose, the exec user)."""
        return _As(self, user)

    def card(self, task_id: str = "KD-123") -> admin_task.Card:
        return cabinet_client.admin_card(self, task_id)

    def record(self, task_id: str = "KD-123"):
        return self.cab.store.card(task_id)

    def promotes(self) -> list:
        return [
            e
            for e in self.cab.store.events_since(0)
            if e["kind"] == "card.changed" and e["actor"] == "user:owner"
        ]


class _As:
    def __init__(self, board: AdminBoard, user: str):
        self.board, self.user = board, user

    def call(self, request: dict) -> dict:
        cab = self.board.cab
        return cabinet_client.LocalClient(cab.ctx, cab.principal(self.user)).call(
            request
        )


class AdminEnv:
    """A temp directory as before (``env / "grants"``) with the board as a cabinet (``env.board``)."""

    def __init__(self, root, *texts, user, catalog=None):
        self.root = Path(root)
        self.board = AdminBoard(self.root / "cabinet", *texts, user=user, admin_root=self.root, catalog=catalog)

    def __truediv__(self, name):
        return self.root / name

    def joinpath(self, *parts):
        return self.root.joinpath(*parts)
