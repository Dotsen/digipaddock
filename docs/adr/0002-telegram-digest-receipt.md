# ADR 0002: An agent-reported Telegram ID does not suppress the digest fallback

Date: 2026-10-07
Status: superseded — the 09:15 mechanical digest to the Pult, its receipt and its units are gone; the owner's
morning summary is the cabinet's 09:00 digest, and the coordinator's own 09:00 report goes to its chat in the cabinet

## Context and evidence

BOSS is expected to send a 09:00 digest (in the host's time zone) through the Claude
Telegram channel `reply` tool. A root-owned `kd-board` timer can send a
mechanical fallback at 09:15. Suppressing that fallback based on a string
provided by BOSS would turn an agent statement into a delivery guarantee.

The installed plugin on **the factory's host** is `telegram@claude-plugins-official`
0.0.7. Its installed `server.ts` SHA-256 is
`d902d7195a527b579b889cfdbf251fc24fd224a68e319ac6b7c3b691515c3458`.
The `reply` handler awaits Telegram `sendMessage`, collects each returned
`message_id` (and IDs from optional photo/document sends), then returns MCP
**text**: `sent (id: N)` for one part or `sent K parts (ids: …)` for several.
Text chunks are sent one by one: an error can occur after some parts were
already sent. See the [upstream plugin implementation](https://github.com/anthropics/claude-plugins-official/blob/main/external_plugins/telegram/server.ts)
and the stage-pinned source hash above. The plugin tool result is evidence
*to the agent* that the Telegram API accepted a request, not a signed receipt
available to `kd-board`.

With the owner's explicit permission for test Telegram messages, one
transport probe on 2026-10-07 used the existing `dev` bot and its
single allowlisted owner DM. A short `[F02 TEST 2026-10-07]` message was sent
through Telegram Bot API `sendMessage`; the response had `ok: true`,
`message_id: 38`, and matching chat/text. The token, chat ID, raw API body,
and secret file contents were not printed or saved in the repository. The
one-off probe file was removed. This is **not** a live invocation of Claude's
`reply` tool: its exact return format is established from the installed
handler, while this probe verifies the underlying transport on stage. An API
success means Telegram accepted the send; it is not proof that the owner's
device displayed or the owner read the message. [Telegram's Bot API](https://core.telegram.org/bots/api#sendmessage)
returns a `Message` for a successful send, but does not offer a general
read-back of an arbitrary outgoing bot message by `(chat_id, message_id)`.

## Reproducible receipt test

Use a **dedicated test bot and allowlisted test chat** for future automated
rechecks; do not share the production bot token, start a second poller for
an active bot, or put token/chat IDs in a command line, log, fixture, PR or
model transcript. From a trusted process, read token and allowlisted chat ID
from private files into memory, call `sendMessage` over HTTPS with a unique
`[F02 TEST]` nonce, then assert `ok == true`, numeric `result.message_id`,
`result.chat.id` equals the allowlisted chat, and `result.text` equals the
nonce. Separately, invoke the plugin's `reply` in that test channel with a
short message and a multi-chunk message; capture its MCP tool result in a
trusted test harness, asserting the one-ID/multi-ID formats and partial-fail
behavior. Never use a BOSS-authored summary as the captured tool result.
After the probe, remove only the temporary test artifacts. The stage probe
above exercised the transport portion; the plugin result portion remains a
repeatable test for an isolated channel rather than a claim of live execution.

## Decision

For the first BOSS release, `kd-board digest-ack` **must not** write a marker
from a BOSS-supplied `message_id` or free text. The 09:15 mechanical fallback
remains active even when BOSS says it sent the 09:00 digest. A possible
duplicate is preferable to a silently missing digest. The fallback sender's
own Telegram API response may be logged as a delivery-*acceptance* result;
`digest()` must return failure on failed send, not report success merely
because it formatted a message.

An optimization that suppresses fallback requires a future trusted sender
or delivery adapter: root-owned code must perform/observe the actual API
send, bind the verified response to date, chat, bot identity and content
hash, and persist an atomic receipt inaccessible to BOSS. It must define
multi-part/partial-failure behavior and fail open **toward sending fallback**
on timeout, corrupt marker, mismatch or uncertainty. A bare numeric ID,
`getChat`, agent note, Telegram `reply` text, or file in the agent's writable
home is not that receipt. No such adapter is implemented by F02.

## B06 acceptance implications

Tests must show that fake/stale/incorrect BOSS IDs and `digest-ack` calls
cannot suppress fallback; the 09:15 timer sends on schedule after both a
claimed successful BOSS reply and a failed/partial one. Test date and
day boundary of the host's time zone, repeated timer runs, unavailable Telegram,
`digest()` success/failure exit codes, and absence of BOSS. No unit test
sends to a real Telegram chat. An integration probe may use only a dedicated
test channel or a new explicit owner authorization.

## Implementation (B06, 2026-10-08)

`kd-board digest` runs at 09:15 in the host's time zone every day and returns 1 when the Pult send fails. After its own accepted send it writes `/var/lib/digipaddock/board/digest-receipt.json` (root, 0600) for that day of the host's time zone, so a repeated or catch-up run that day does not send a duplicate; a missing, stale, malformed or symlinked receipt means "send". There is no `digest-ack`, and nothing BOSS can call writes the receipt. At 09:00 `kd-board digest-signal` wakes the coordinator with one fixed line; its result never affects the 09:15 digest.
