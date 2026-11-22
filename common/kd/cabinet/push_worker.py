"""``kd-cabinet push``: the Web Push sender, a process of its own under the cabinet's user.

It takes due notifications from the cabinet over its socket (``push.take``), sends each to the push
gateway of the person's browser and reports the gateway's answers (``push.sent``): sent, a dead
subscription (404/410) dropped, or one more failed attempt. The cabinet keeps the queue and the
retry rules; this process holds the VAPID key and is the only one that reaches the network, and only the
browsers' push services (``gateway_ok``): an endpoint is a URL a page hands in.
"""

import json
import re
import sys
import time
from urllib.parse import urlsplit

TTL = 3600
TIMEOUT = 10
LIMIT = 50
# the push services of Chrome and Android, Firefox, Safari and iOS, Edge
GATEWAY_HOSTS = frozenset({"fcm.googleapis.com"})
GATEWAY_SUFFIXES = (
    ".push.services.mozilla.com",
    ".push.apple.com",
    ".notify.windows.com",
)
GONE = 410  # what a refused endpoint is reported as: the cabinet drops its subscription
REASON_LIMIT = 300
_URL_PATH = re.compile(r"(url: )\S+")


def gateway_ok(endpoint) -> bool:
    """An https URL on a browser push service's host, port 443, without credentials."""
    try:
        url = urlsplit(endpoint)
        port = url.port
    except (TypeError, ValueError):
        return False
    host = (url.hostname or "").lower()
    return (
        url.scheme == "https"
        and url.username is None
        and url.password is None
        and port in (None, 443)
        and (host in GATEWAY_HOSTS or host.endswith(GATEWAY_SUFFIXES))
    )


def webpush_send(
    subscription: dict, payload: str, vapid_private: str, vapid_subject: str
) -> int:
    """One push through ``pywebpush``: the gateway's HTTP status. A failure to reach it raises (the caller
    logs why; a bare 0 once hid an unresolvable host)."""
    from pywebpush import WebPushException, webpush

    urgency = json.loads(payload).get("urgency", "normal")
    try:
        response = webpush(
            subscription_info=subscription,
            data=payload,
            vapid_private_key=vapid_private,
            vapid_claims={"sub": vapid_subject},
            ttl=TTL,
            timeout=TIMEOUT,
            headers={"Urgency": urgency},
        )
    except WebPushException as exc:
        status = getattr(exc.response, "status_code", 0) or 0
        if not status:
            raise
        return status
    return int(getattr(response, "status_code", 0) or 0)


def reason(exc: BaseException, endpoint: str) -> str:
    """The failure's type and text for the journal: the endpoint, which addresses the device, becomes its host, a
    request path is dropped, the gate's masks apply (kd.redact); never longer than REASON_LIMIT."""
    text = str(exc)
    try:
        url = urlsplit(endpoint)
        host, tail = url.hostname or "?", url.path + (f"?{url.query}" if url.query else "")
    except (TypeError, ValueError):
        host, tail = "?", ""
    text = text.replace(endpoint, host)
    if tail and tail != "/":
        text = text.replace(tail, "/…")
    text = _URL_PATH.sub(r"\1…", text)
    from kd import redact

    masked = redact.text(text)
    text = " ".join((masked if masked is not None else "(masks unavailable)").split())
    return f"{type(exc).__name__}: {text}"[:REASON_LIMIT]


def _one(item, send, vapid_private, vapid_subject) -> tuple:
    """(the result for push.sent, why the gateway was not reached or None)."""
    subscription = {
        "endpoint": item["endpoint"],
        "keys": {"p256dh": item["p256dh"], "auth": item["auth"]},
    }
    payload = json.dumps(
        {k: item[k] for k in ("title", "body", "tag", "url", "urgency", "badge") if k in item},
        ensure_ascii=False,
    )
    why = None
    if not gateway_ok(item["endpoint"]):
        status = GONE  # never contacted; a row from before the check or written past it
    else:
        try:
            status = int(send(subscription, payload, vapid_private, vapid_subject))
        except Exception as exc:  # noqa: BLE001 — a failed send is status 0 and the loop goes on; the journal says why
            status, why = 0, reason(exc, item["endpoint"])
    return {
        "id": item["id"],
        "endpoint": item["endpoint"],
        "ok": 200 <= status < 300,
        "status": status,
    }, why


def _journal_line(item, result, why=None) -> str:
    """One send in the unit's journal: the row, its tag, the push service's host and its answer — never
    the endpoint's path (it addresses the device) or the keys."""
    try:
        host = urlsplit(item["endpoint"]).hostname or "?"
    except (TypeError, ValueError):
        host = "?"
    line = f"kd-cabinet push: row {item['id']} tag={item.get('tag', '')} {host} status={result['status']}"
    return f"{line} error={why}\n" if why else f"{line}\n"


def run(
    client,
    *,
    send,
    interval=2.0,
    once=False,
    vapid_private="",
    vapid_subject="",
    limit=LIMIT,
    sleep=time.sleep,
    err=sys.stderr,
):
    """Take, send, report; sleep ``interval`` when nothing is due. ``once`` makes one pass (tests)."""
    from kd.cabinet import client as cabinet_client

    while True:
        try:
            items = client.call({"op": "push.take", "limit": limit})["items"]
            sent = [_one(item, send, vapid_private, vapid_subject) for item in items]
            results = [result for result, _ in sent]
            for item, (result, why) in zip(items, sent):
                err.write(_journal_line(item, result, why))
            if results:
                client.call({"op": "push.sent", "results": results})
        except cabinet_client.CabinetError as exc:
            if once:
                raise
            err.write(f"kd-cabinet push: {exc}\n")
            sleep(interval * 5)
            continue
        if once:
            return
        if not items:
            sleep(interval)
