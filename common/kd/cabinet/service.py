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
CABINET = api.Principal("cabinet", -1, "cabinet")
CARRY_TIMEOUT = 20.0  # kd-review's carry inside a publish: the forge executor waits for the answer at most 30 s
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

    async def call_local(self, principal, request) -> dict:
        """An operation of the local socket. A PR's publish whose new head may take the owner's signature of another
        head (``prs_api.carry_from``) asks kd-review before it answers — outside the lock, as the web does."""
        answer = await self.call(principal, request)
        carry = answer.get("carry") if isinstance(answer, dict) and request.get("op") == "pr.publish" else None
        if not carry:
            return answer
        rest = {k: v for k, v in answer.items() if k != "carry"}
        return {**rest, **await self._carry(answer["pr"], carry)}

    async def _carry(self, row: dict, carry: dict) -> dict:
        """kd-review carries the signature (``carried``: from which head, in which mode) and the head's decision is
        recorded approved; else the head goes the usual way and ``uncarried`` says why."""
        held = carry["held"]
        named = {"repo": row["repo"], "number": row["number"], "held": held}
        try:
            review = await self.review.call(
                {"op": "carry", "project": held["payload"]["project"], "repo": row["repo"],
                 "number": row["number"], "from": carry["from"], "head": row["head"]}, timeout=CARRY_TIMEOUT)
            done = await self.call(CABINET, {
                "op": "pr.carried", **named, "decision": carry["decision"],
                "review": {"id": review.get("review"), "head": review.get("head"), "from": review.get("from"),
                           "mode": review.get("mode")}})
            return {"decision": done["decision"], "council": None,
                    "carried": {"from": carry["from"], "mode": review.get("mode"),
                                "target": held["payload"].get("target", "")}}
        except (review_client.ReviewRefused, review_client.ReviewUnavailable, api.ApiError) as exc:
            why = str(exc)
        try:
            decided = await self.call(CABINET, {"op": "pr.uncarried", **named})
        except api.ApiError as exc:
            return {"decision": None, "council": None, "uncarried": f"{why}; {exc}"}
        return {**decided, "uncarried": why}

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
