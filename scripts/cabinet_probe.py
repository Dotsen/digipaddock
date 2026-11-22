"""Read-only probe of the cabinet on a host: what one person's overview, a thread's newest
page, a card's history page and the agents' facts are, straight from the base, without the web.

Run on the host as the cabinet's user, after a rollout (it opens the base as the service does; at the current
schema nothing is migrated or written):

    cd / && runuser -u digi-cabinet -- /var/lib/digipaddock/venv/cabinet/bin/python -I \\
        /opt/digipaddock/scripts/cabinet_probe.py --user owner --cards KD-<n> KD-<n>

It prints one JSON object: the projects the person sees, the overview's counts and row kinds, each card's
page sizes and the agents' states.
"""

import argparse
import collections
import json
import sys

sys.path.insert(0, "/opt/digipaddock/common")

from kd import agents as registry  # noqa: E402
from kd import session_config  # noqa: E402
from kd.cabinet import api, store, team, users  # noqa: E402

DB = "/var/lib/digipaddock/cabinet/cabinet.db"
REGISTRY = "/etc/digipaddock/agents.json"
ROOT = "/opt/digipaddock"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--user", default="owner", help="the person whose view is probed")
    parser.add_argument("--cards", nargs="*", default=[], help="cards whose thread and history pages are read")
    args = parser.parse_args(argv)

    base = store.Store.open(DB)
    ctx = api.Context(base, REGISTRY, ROOT, users=users.Users(base))  # projects: the base's table
    api.install_host(ctx, session_config.Cached().get)
    person = api.Principal("user", -1, args.user)

    found = api.dispatch(ctx, person, {"op": "overview.get"})
    out = {
        "schema": base.schema_version(),
        "projects": api.dispatch(ctx, person, {"op": "project.list"})["items"],
        "overview": {
            "counts": found["counts"],
            "needs": collections.Counter(r["kind"] for r in found["needs"]),
            "work": collections.Counter(r["run"] for r in found["work"]),
            "problems": collections.Counter(f"{r['kind']}:{r['reason']}" for r in found["problems"]),
            "changes": collections.Counter(r["kind"] for r in found["changes"]),
            "team": {m["name"]: m["state"] for m in found["team"]},
        },
        "cards": {},
        "agents": {},
    }
    for card in args.cards:
        thread = api.dispatch(ctx, person, {"op": "thread.messages", "card": card, "limit": 200})
        events = api.dispatch(ctx, person, {"op": "card.events", "card": card, "limit": 100})
        out["cards"][card] = {
            "messages": len(thread["messages"]),
            "more": thread["more"],
            "total": len(base.messages(card)),
            "events": len(events["events"]),
            "next": events["next"],
        }
    visible = [api.compact(r) for r in base.cards() if api.visible(ctx, person, r.project)]
    chat = api.allowed(ctx, person, "agents.chat")
    config = ctx.host_config()
    for name in registry.names(REGISTRY):
        facts = team.facts(base, name, cards=visible, chat=chat, hosted=config.hosted(name), now=ctx.now())
        out["agents"][name] = {k: facts[k] for k in ("state", "working", "waiting_you", "queued", "failed", "stale", "heartbeat")}
    print(json.dumps(out, ensure_ascii=False, indent=1, default=dict))
    return 0


if __name__ == "__main__":
    sys.exit(main())
