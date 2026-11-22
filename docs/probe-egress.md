# Exits: probe and standing

An agent's sandbox reaches only the hosts of its compiled `allowedDomains`, and its compiled policy denies the web
tools. An exit opens more, and only through the deployment's VPN, never from the host's own address. There are two
kinds, both from `host/egress.json` of the deployment:

- a **probe exit** lets one card's session reach a few more named external hosts — an aggregator's API a developer
  needs a live answer from — for exactly as long as the owner's consent on that card lives;
- the **standing exit** is held by every session of the agents it lists — cards, chats, the «Monitor» thread — with no
  card and no consent behind it. It is the deployment's decision that its agents work with the open web.

An exit names its hosts, or is of every host: `"domains": ["*"]`.

## The route

Claude Code's sandbox proxy checks every connection against `allowedDomains`. A session with an exit starts with:

- the exit's host names added to `allowedDomains`;
- `HTTPS_PROXY` and `HTTP_PROXY` of the Claude Code process pointing at the exit, a tinyproxy on loopback: the sandbox
  proxy then forwards the allowed connections to it (Claude Code's upstream proxy);
- `NO_PROXY` holding loopback, Claude Code's own endpoints (`api.anthropic.com`, `claude.ai`, `platform.claude.com`),
  the agent's other names (the forge) and the host of a card's provider model: they never go through the exit.

A session with an exit of every host starts differently in one point: Claude Code takes no `*` among
`allowedDomains`, so the launch adds the allow rule `WebFetch(domain:*)`, which its sandbox reads as every host, and
moves the tools `WebFetch` and `WebSearch` from deny to allow. `WebFetch` and sandboxed commands leave through the
exit; `WebSearch` runs on the model provider's side and never leaves the host. The compiled policy on disk stays
strict: the tools are opened at launch and only while the host's file names such an exit for the agent.

The exit allows only `CONNECT` to port 443 — of its own names, or of any name when it is an exit of every host — and
sends each through one SOCKS port of the VPN tunnel; any other name is refused, and the unit's network is loopback
only. A probe client takes the proxy from its
environment (the sandbox sets it); a client pointed at a tunnel port directly reaches nothing, because a sandboxed
command's loopback is its own.

An exit of named hosts is refused twice: a name outside `allowedDomains` by the sandbox proxy (403), a name outside
the exit's list by the exit (502). A tunnel that is down is an error (502), never a direct connection.

## Who gets an exit

`host/egress.json` of the deployment (`host/egress.example.json` shows the shape) names the exits, their names and
tunnel port, the agents that may use each, which project maps to which exit, and — optionally — the standing exit:

```json
{"schema_version": 1,
 "exits": {"web": {"port": 3140, "upstream": "127.0.0.1:1080", "domains": ["*"],
                   "agents": ["boss", "analyst", "dev", "sysadmin"], "max_clients": 32, "timeout": 60}},
 "standing": "web",
 "projects": {}}
```

**The standing exit.** `kd.launch` gives it to a session when the session host named no card exit, the agent's
compiled policy holds `net.egress` and the exit lists the agent. Neither the cabinet nor the session host takes part:
nothing is granted and nothing is revoked, the exit lasts while the file names it. An agent the exit does not list,
or a host whose file has no `standing`, starts as before. Worker jobs run under their own policy and get no exit.

**A probe exit.** A card's session gets its project's exit — it then replaces the standing one for that session —
when all of these hold at its delivery:

- the agent holds the capability `net.egress` and the exit lists it;
- the card is not closed, is assigned to that agent and is in `To Do`, `In Progress` or `Blocked`;
- when the card has brief versions, the latest one is approved and the card's scope is still that version's (an
  edit of the agreed scope ends the consent until a new version is approved).

These are the ends of the owner's «in progress» consent. A chat or monitor session never gets a probe exit.

When the consent ends, the session's process ends with it: the next delivery without the exit closes the process
(like a change of the card's model), and within about half a minute the cabinet asks the session host to revoke the
exit of a live process whose card no longer qualifies. An idle process closes; a working one is cut, and its turn
ends `egress_ended`. This is not a person's stop: the session goes on, and the inputs that wait start the next
process, which resumes the history (`--resume`) without the exit.

A session that waits for a free slot holds the exit its delivery named until its process starts. The session host's
status shows that exit for the waiting session too, so the same sweep revokes it: the session stays in the queue and
its first process starts without the exit. Should the host's file change between that sweep and the start — the exit
gone from `host/egress.json`, the file removed, or the exit no longer listing the agent — `kd.launch` does not fail
the launch: it starts the session as one no card named an exit for (with the standing exit when the agent has one,
otherwise with none) and writes one line naming the vanished exit to stderr, which reaches the session's journal. A
malformed exit name or a file that does not parse still stops the launch: that is a broken release, not a changed
exit. Inside a session
`printenv KD_EGRESS KD_EGRESS_HOSTS KD_EGRESS_COUNTRY` says whether it holds one, which hosts (`*` — every host) and,
for an exit of the country table below, its country. The developer's tools (its venv) include the probe client
`curl_cffi`.

## Country exits

An aggregator answers by the country the request comes from. The optional table `countries` of `host/egress.json`
maps an ISO 3166-1 code to an exit of every host that leaves through that country's tunnel port, one exit per
country:

```json
{"exits": {"web":    {"port": 3140, "upstream": "127.0.0.1:1080", "domains": ["*"], "...": "..."},
           "web-ae": {"port": 3141, "upstream": "127.0.0.1:1081", "domains": ["*"], "...": "..."}},
 "standing": "web",
 "countries": {"GB": "web", "AE": "web-ae"}}
```

A release refuses a country whose exit the file does not have, an exit of named hosts in the table and two countries
on one exit.

**The label.** A card chooses its country with the label `egress:<code>` in lower case (`egress:ae`). The card's
assignee sets it (`kd-task edit KD-<n> --add-label egress:ae`); the cabinet refuses a code the host's table does not
have, naming the known labels, and a second `egress:` label (a change removes the old one in the same edit:
`--remove-label egress:ae --add-label egress:gb`). A card's session gets the country's exit when the card is not
closed (any status but `Done` and `Cancelled`, `Inbox` too), is assigned to the agent, the exit lists the agent and
its policy holds `net.egress`. No approved brief is asked: the exit is of the standing exit's kind. The label beats
the project's probe exit; a card without it gets what it got before.

A new label takes effect at the next delivery: the session's process closes after its turn and the next one resumes
the history (`--resume`) with the new country's exit. The cabinet's sweep does not cut a turn because the country
changed — only because the card stopped qualifying (closed, reassigned) or its exit left the file. An agent that
changed the label ends its turn `--outcome continue`; its next turn runs from the new country.

A label whose country has left the file (or whose exit no longer lists the agent) does not stop the card: the
delivery goes on as one without the label (the project's exit, else the standing exit) and the cabinet writes a
status line in the card's thread naming the label. The label stays on the card through other edits.

Chat and «Monitor» sessions always hold the standing exit: `KD_EGRESS_COUNTRY` there is the standing exit's country.

**Which channel a request took.** Each exit writes its own log, `/var/log/digipaddock-egress/<exit>.log`
(`kd-host-read tail -n 50 egress/web-ae.log`); tinyproxy logs the host of each `CONNECT`, not the tunnel port, so the
file is the channel. The session host's journal (`host.jsonl`) records the exit each `deliver` named in its field
`egress` (`""` — none, the session holds the standing exit if any).

## What an exit of every host costs

The host list no longer bounds what a session reads or where it sends. A page an agent fetches can carry an injected
instruction that tries to send out what the agent can read: code, rows of a product database, a managed server's
logs. What still holds: the agents' instructions treat page content as data and forbid sending the deployment's data
out; changes of code and servers still need the owner's signature; secrets stay unreadable; the file sandbox is
unchanged; every connection through the exit is in its log (the host name, not the content). A deployment that does
not accept this keeps exits of named hosts and no `standing`.

## Install and switch off

The component `egress` (`install.json`) runs `common/install/egress_setup.sh install`: tinyproxy from the distribution
(its own unit masked), one `digipaddock-egress@<exit>.service` per exit running as the user `tinyproxy` with no
capabilities, configs in `/etc/digipaddock/egress`, one log per exit in `/var/log/digipaddock-egress` (made by the
setup, readable through `kd-host-read`). `check` verifies the same state. A release refuses a `host/egress.json` that
does not parse, names a missing exit as `standing` or lists an agent without `net.egress`.

An agent's launch with an exit — a card's or the standing one — drops any proxy variable it inherited: Claude Code
would keep one over the exit.

Kill switch: `systemctl stop digipaddock-egress@<exit>` (one exit) or `egress_setup.sh disable` (all). Sessions keep
working; requests through the exit fail — with the standing exit stopped, `WebFetch` and every command's request
fail, `WebSearch` goes on. To take the web tools away too, remove `standing` from the file and release.
