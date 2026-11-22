"""The cabinet's client of kd-review: the approver that holds the Forgejo token the cabinet never has.

One JSON line each way over ``/run/digipaddock-review/review.sock``, the cabinet's own socket server's shape: ``{"ok": true,
...}`` or ``{"ok": false, "error": kind, "message": text}``. The web calls it outside the service's lock (a Forgejo
round trip must not hold the board's one writer).
"""

import asyncio
import json

SOCKET = "/run/digipaddock-review/review.sock"
TIMEOUT = 40.0
LINE_MAX = 1 << 20


class ReviewUnavailable(Exception):
    """No socket, or an answer that is not one."""


class ReviewTimeout(ReviewUnavailable):
    """No answer in time: kd-review may still have acted (an Approve may be in Forgejo)."""


class ReviewRefused(Exception):
    def __init__(self, kind: str, message: str, live: str = ""):
        super().__init__(message)
        self.kind = kind
        self.live = live  # "merged" | "closed": the PR ended in Forgejo


class ReviewClient:
    def __init__(self, path: str = SOCKET):
        self.path = path

    async def call(self, request: dict, timeout: float = TIMEOUT) -> dict:
        try:
            return await asyncio.wait_for(self._call(request), timeout)
        except asyncio.TimeoutError as exc:
            raise ReviewTimeout("kd-review did not answer in time") from exc

    async def _call(self, request: dict) -> dict:
        try:
            reader, writer = await asyncio.open_unix_connection(self.path, limit=LINE_MAX)
        except (AttributeError, OSError) as exc:  # AttributeError: no unix sockets on this system
            raise ReviewUnavailable(f"kd-review unavailable ({exc.__class__.__name__})") from exc
        try:
            writer.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
            await writer.drain()
            line = await reader.readline()
        except (OSError, ValueError) as exc:
            raise ReviewUnavailable(f"kd-review unavailable ({exc.__class__.__name__})") from exc
        finally:
            writer.close()
        try:
            answer = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ReviewUnavailable("kd-review answered nonsense") from exc
        if not isinstance(answer, dict):
            raise ReviewUnavailable("kd-review answered nonsense")
        if not answer.get("ok"):
            live = answer.get("live") if answer.get("live") in ("merged", "closed") else ""
            raise ReviewRefused(str(answer.get("error", "refused")), str(answer.get("message", "refused")), live)
        return {k: v for k, v in answer.items() if k != "ok"}
