"""Pages of what a card's snapshot only points to: its scope, its result, one message, its thread;
and one message of the agent's own chat, which ``kd-task chat`` cuts the same way.

Each page an agent reads fits in ``snapshot.LIMIT`` bytes. A document (the scope, the result, a message) is cut in
byte ranges with a cursor ``<sha>:<offset>``: the sha is of the document's text, so a cursor outlives a comment on
the card and is refused once the document itself changed — the reader starts again rather than gluing two
versions. The thread is paged by message seq, the newest page first, with the exact command for the older one.
"""

import hashlib
import re
from typing import Optional

from kd.cabinet import api, result, sessions_api, snapshot, view

CURSOR = re.compile(r"([0-9a-f]{12}):([0-9]{1,9})")
CHUNK = snapshot.LIMIT - 256  # bytes of a document's piece; the rest is the page's service line
SEQ_MAX = 10 ** 9


def doc_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def page(text: str, cursor: Optional[str], *, command: str) -> dict:
    """The piece of ``text`` from ``cursor`` (its start without one): ≤ ``CHUNK`` bytes cut on a character, at
    the last line break of the piece's last quarter when it has one, and the line that reads on or says the end."""
    raw, sha = text.encode("utf-8"), doc_sha(text)
    total, offset = len(raw), 0
    if cursor is not None:
        found = CURSOR.fullmatch(cursor) if isinstance(cursor, str) else None
        if found is None:
            raise api.ApiError("invalid", "cursor must be <sha>:<offset> as a page printed it")
        if found.group(1) != sha:
            raise api.ApiError("conflict", f"the document changed since this cursor; read it again from the start: "
                                           f"{command}")
        offset = int(found.group(2))
        if offset > total:
            raise api.ApiError("invalid", "cursor is past the end of the document")
        if offset < total and raw[offset] & 0xC0 == 0x80:
            raise api.ApiError("invalid", "the cursor does not point at a character boundary")
    end = min(offset + CHUNK, total)
    if end < total:
        while end > offset and raw[end] & 0xC0 == 0x80:
            end -= 1
        brk = raw.rfind(b"\n", offset + (end - offset) * 3 // 4, end)
        if brk != -1:
            end = brk + 1
    chunk = raw[offset:end].decode("utf-8")
    nxt = end if end < total else None
    mid = bool(chunk) and not chunk.endswith("\n")  # the break before the service line is not the document's
    line = f"more: {command} --cursor {sha}:{nxt}" if nxt is not None else f"end ({total} bytes)"
    if mid and nxt is not None:
        line += " (the page ends mid-line)"
    return {"chunk": chunk, "text": chunk + ("\n" if mid else "") + line + "\n",
            "next": nxt, "sha": sha, "offset": offset, "total": total}


def scope_text(record) -> str:
    return view.scope_text(record.to_dict())


def result_text(found: dict) -> str:
    """``result.view`` as a document: the summary, the criteria, the sections, the links and the Review rounds."""
    parts = ["## Summary\n\n" + (found["summary"] or "(none)")]
    if found["acceptance"]:
        parts.append("## Acceptance Criteria\n\n" + "\n".join(
            f"- [{'x' if a['checked'] else ' '}] #{i} {a['text']}" for i, a in enumerate(found["acceptance"], 1)))
    for key, name in (("dod", "Definition of Done"), ("plan", "Implementation Plan"),
                      ("notes", "Implementation Notes")):
        if found[key]:
            parts.append(f"## {name}\n\n{found[key]}")
    if found["links"]:
        parts.append("## Links\n\n" + "\n".join(f"- {link}" for link in found["links"]))
    if found["reviews"]:
        rounds = []
        for r in found["reviews"]:
            line = (f"#{r['iteration']} entered {r['entered']} by {view.who(r['by'])}: {r['entered_note'] or '-'} "
                    f"→ {r['outcome']}")
            if r["decided"]:
                line += f" {r['decided']} by {view.who(r['decided_by'])}: {r['note'] or '-'}"
            rounds.append(line)
        parts.append("## Review\n\n" + "\n".join(rounds))
    return "\n\n".join(parts) + "\n"


def _line(card: str, message: dict) -> str:
    head = f"#{message['seq']} {view.who(message['author'])} {message['created']}"
    if message["kind"] == "tool_call":
        text = "· " + (message.get("body") or "")
        sep = " "
    else:
        text = view.message_text(message, with_files=False)
        sep = f" {message['kind']}: "
    text = view.continued(snapshot._cut_bytes(text, snapshot.MESSAGE_CUT, f"… (kd-task message {card} {message['seq']})"))
    files = [view.file_line(f) for f in view.files_of(message)]  # never cut: kd-task file get takes the id
    return head + sep + "\n  ".join(([text] if text else []) + files)


def thread_page(store, record, *, before: Optional[int] = None, after: Optional[int] = None,
                tools: bool = False) -> dict:
    """A page of the card's thread in order: its significant messages (with ``tools`` the session's tool calls
    too, never their results), the newest page without ``before``/``after``; always at least one message."""
    card = record.id
    found = [m for m in store.messages(card)
             if snapshot.significant(m) or (tools and m["kind"] == "tool_call")]
    if after is not None:
        window = [m for m in found if m["seq"] > after]
    else:
        window = [m for m in found if before is None or m["seq"] < before][::-1]
    lines, used = [], 0
    for message in window:
        line = _line(card, message)
        cost = len(line.encode("utf-8")) + 1
        if lines and used + cost > CHUNK:
            break
        lines.append((message["seq"], line))
        used += cost
    if after is None:
        lines.reverse()
    flag = " --tools" if tools else ""
    if not lines:  # an empty page still says where the messages are
        if after is not None:
            return {"text": f"older: kd-task thread {card} --before {after + 1}{flag}\nend of thread\n",
                    "older": after + 1, "newer": None}
        return {"text": f"start of thread\nnewer: kd-task thread {card} --after 0{flag}\n", "older": None,
                "newer": 0}
    first, last = lines[0][0], lines[-1][0]
    older = first if any(m["seq"] < first for m in found) else None
    newer = last if any(m["seq"] > last for m in found) else None
    out = [f"older: kd-task thread {card} --before {older}{flag}" if older is not None else "start of thread"]
    out += [line for _, line in lines]
    out.append(f"newer: kd-task thread {card} --after {newer}{flag}" if newer is not None else "end of thread")
    return {"text": "\n".join(out) + "\n", "older": older, "newer": newer}


def _seq(value, name: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= SEQ_MAX:
        raise api.ApiError("invalid", f"{name} must be a message number")
    return value


def _scope(ctx, principal, request) -> dict:
    record = api._visible_card(ctx, principal, request.get("card"))
    return page(scope_text(record), request.get("cursor"), command=f"kd-task scope {record.id}")


def _result(ctx, principal, request) -> dict:
    record = api._visible_card(ctx, principal, request.get("card"))
    return page(result_text(result.view(ctx.store, record)), request.get("cursor"),
                command=f"kd-task result {record.id}")


def _message(ctx, principal, request) -> dict:
    record = api._visible_card(ctx, principal, request.get("card"))
    seq = _seq(request.get("seq"), "seq")
    found = ctx.store.messages(record.id, seq - 1, before=seq + 1) if seq else []
    if not found:
        raise api.ApiError("not_found", f"{record.id}: no message {seq}")
    message = found[0]
    if message["kind"] == "tool_result":  # a result's body stays out of every page (spec 4.1)
        payload = message.get("payload") or {}
        mark = {True: "✓", False: "✗"}.get(payload.get("ok"), "?")
        body = f"{mark} {payload.get('size', '?')} bytes (the output itself is not shown)"
    elif message["kind"] == "tool_call":
        body = message.get("body") or ""
    else:
        body = view.message_text(message)
    text = f"#{seq} {view.who(message['author'])} {message['created']} {message['kind']}\n{body}"
    return page(text, request.get("cursor"), command=f"kd-task message {record.id} {seq}")


def _chat_message(ctx, principal, request) -> dict:
    """One message of the agent's own chat in full (``kd-task chat --message SEQ``): a chat line cuts a long one."""
    agent = sessions_api._own_chat(ctx, principal, request)
    seq = _seq(request.get("seq"), "seq")
    thread = ctx.store.agent_thread(agent, sessions_api.caller_space(ctx, agent, request))  # "Monitor"
    found = ctx.store.thread_messages(thread, seq - 1, before=seq + 1) if seq else []
    if not found:
        raise api.ApiError("not_found", f"chat: no message {seq}")
    message = found[0]
    text = f"#{seq} {message['author']} {message['created']} {message['kind']}\n{view.message_text(message)}"
    flag = " --in-chat" if "space" in request else ""  # the next page from the same thread
    return page(text, request.get("cursor"), command=f"kd-task chat{flag} --message {seq}")


def _thread(ctx, principal, request) -> dict:
    record = api._visible_card(ctx, principal, request.get("card"))
    before, after = _seq(request.get("before"), "before"), _seq(request.get("after"), "after")
    if before is not None and after is not None:
        raise api.ApiError("invalid", "give before or after, not both")
    tools = request.get("tools", False)
    if not isinstance(tools, bool):
        raise api.ApiError("invalid", "tools must be true or false")
    return thread_page(ctx.store, record, before=before, after=after, tools=tools)


OPS = {
    "card.scope": (api.READERS, _scope),
    "card.result.text": (api.READERS, _result),
    "card.message": (api.READERS, _message),
    "card.thread": (api.READERS, _thread),
    "agent.chat.message": (frozenset({"executor"}), _chat_message),
}
api.OPS.update(OPS)  # the board API serves them (api imports this module at its end)
