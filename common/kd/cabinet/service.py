"""The cabinet service in one process: the board operations, people, decisions and the push queue.

Every write runs under one ``asyncio.Lock`` — the store has one writer — whether it comes from the
local socket or from the web. After a write the notifier queues pushes for the new journal events and
the WebSocket hub (when there is one) tells the open pages. ``challenges`` keeps WebAuthn challenges
in memory for five minutes: a restart only makes a person press the button again.
"""

import asyncio
import sys
import time
import traceback
from typing import Any, Callable, Dict, Optional, Tuple

from kd.cabinet import api, model_keys_client, review_client

CHALLENGE_TTL = 300
USAGE = "/var/lib/digipaddock/usage.jsonl"
CHALLENGES_MAX = 10000
PAGE = 500  # journal events read at once after a write


class Challenges:
    """Single-use values by (kind, key) that expire after ``ttl`` seconds."""

    def __init__(self, ttl=CHALLENGE_TTL, clock=time.monotonic):
        self.ttl, self.clock = ttl, clock
        self._items: Dict[Tuple[str, str], Tuple[float, Any]] = {}

    def put(self, kind: str, key: str, value):
        now = self.clock()
        if len(self._items) >= CHALLENGES_MAX:
            self._items = {k: v for k, v in self._items.items() if v[0] > now}
        self._items[(kind, key)] = (now + self.ttl, value)

    def pop(self, kind: str, key: str):
        item = self._items.pop((kind, key), None)
        if item is None or item[0] <= self.clock():
            return None
        return item[1]


class Service:
    def __init__(
        self,
        ctx,
        cfg,
        *,
        users,
        decisions,
        notifier,
        hub=None,
        now: Optional[Callable[[], str]] = None,
        on_change: Optional[Callable[[], None]] = None,
        review=None,
    ):
        self.ctx, self.cfg, self.users, self.decisions = ctx, cfg, users, decisions
        self.review = review or review_client.ReviewClient()  # kd-review: a PR's Approve in Forgejo
        self.model_keys = model_keys_client.ModelKeysClient()
        self.notifier, self.hub = notifier, hub
        self.ctx.users = users
        self.ctx.notifier = notifier
        self._now = now or ctx.now
        self._on_change = on_change
        self.lock = asyncio.Lock()
        self.kick = asyncio.Event()  # a write queued a command for the session host: the dispatcher runs now
        self.voice_kick = asyncio.Event()  # a write queued an audio to transcribe: the voice loop runs now
        self.job_sweep = {}  # the dispatcher's view of jobs the host no longer has (dispatcher._sweep)
        self.egress_sweep = {}  # when the dispatcher last compared live probe exits with their cards (dispatcher._egress_sweep)
        self.operation_poll = {}  # the factory operation in flight -> when the host was last asked
        self.devcheck_poll = {}  # each contour's check the host has -> when it was last asked
        self.challenges = Challenges()
        self.usage_path = USAGE  # kd-usage's ledger: the agents' spend

    def now(self) -> str:
        return self._now()

    async def call(self, principal, request) -> dict:
        """One operation of the board API (``api.dispatch``) under the lock; ``ApiError`` as it is."""
        return await self.locked(lambda: api.dispatch(self.ctx, principal, request))

    async def locked(self, fn: Callable[[], Any]):
        """Run ``fn`` (any write: sessions, codes, keys) under the lock, then queue pushes and tell the pages."""
        async with self.lock:
            last = self.ctx.store.last_seq()
            try:
                return fn()
            finally:
                self._after(last)

    def _after(self, last: int):
        """The write stands whatever happens here. Every event it made reaches the pages, however many
        (page by page up to the journal's end now), and the push queue, the pages and the sync signal are
        told each on its own: one failing only reaches the journal, the others still run."""
        events = []
        try:
            high = self.ctx.store.last_seq()
            while last < high:
                page = self.ctx.store.events_since(last, PAGE)
                if not page:
                    break
                events.extend(page)
                last = page[-1]["seq"]
        except Exception:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
        if not events:
            return
        consumers = []
        if self.notifier is not None:
            consumers.append(lambda: self.notifier.catch_up(self.decisions))
        if self.hub is not None:
            consumers.append(lambda: self.hub.publish(events))
        if self._on_change is not None and any(e["card"] for e in events):
            consumers.append(self._on_change)
        if any(e["kind"] in ("command.queued", "job.queued", "job.cancelling") for e in events):
            consumers.append(self.kick.set)
        if any(e["kind"] == "voice.queued" for e in events):
            consumers.append(self.voice_kick.set)
        for consumer in consumers:
            try:
                consumer()
            except Exception:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
