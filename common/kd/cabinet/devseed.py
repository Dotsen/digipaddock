"""The cabinet on a developer's machine: ``kd-cabinet serve --dev [--seed]``.

``dev_service(state_dir)`` builds the whole service over ``state_dir``: the base, a registry of the
repository's agents, the admin grant store and the BOSS proposal directories, a fresh secret and VAPID
pair (``config.load(dev=True)``); @analyst is on the session host (its turns: ``kd.cabinet.devhost``).
Nothing outside ``state_dir`` is touched; the dev servers of the
sysadmin's catalog (``web-1``, ``db-1``) are names, not hosts. A checkout without a deployment's ``host/host.json``
(a clone of the public repository) reads the synthetic ``host/host.example.json``.

``apply(svc, out=…)`` seeds it once (state ``devseed``): the owner and a partner (``anna``, project
``alert``), twenty-one cards over every status with AC and DoD, one thread of thirty messages of every kind,
and an open decision of each kind the cabinet shows (three sysadmin plans — one of three steps to sign
alone, two of one step to sign as a batch — a critical action, a BOSS promote, an agent's question); one more card's
brief is under its council: the first round read, one finding answered, the writer awaited.
Its texts are English (the public repository's language; the interface stays the reader's), and it is dated
over the past six days (``SeedClock``), so the feed and the agents read as a board in use; the documentation's
screenshots are of it (``npm run screenshots``).
Every run prints fresh sign-in codes (``owner code: …``) and, given ``codes_path``, writes them there.
"""

import copy
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kd import admin_approval, admin_proposal, boss_board, boss_promote, llm_models, naming, session_config
from kd import admin_operations as ops
from kd.cabinet import api, client, config, councils, decisions, notify, problems, questions, service, store, users

REPO = Path(__file__).resolve().parents[3]
EXAMPLE_HOST = REPO / "host" / "host.example.json"  # the deployment values of a clone without an overlay
# the board's projects (the cabinet's table is their only source; the dev base seeds its own)
PROJECTS = (
    {"id": "digipaddock", "name": "DIGIPADDOCK", "route": "@dev"},
    {"id": "admin", "name": "Servers", "route": "@sysadmin"},
    {"id": "alert", "name": "Alert.Example.Com", "route": "@owner"},
)
CODE_PROJECT = "digipaddock"  # the one with a repository
AGENTS = ("analyst", "boss", "dev", "sysadmin")
# the dev host runs their turns; every agent is on it, so the team shows each one's state
HOSTED = session_config.HostConfig({"analyst": 2, "dev": 1, "boss": 1, "sysadmin": 1})
SEED_KEY = "devseed"
CATALOG = {
    "schema_version": 1,
    "verbs": ["package.refresh", "service.restart", "service.start", "package.install"],
    "servers": {
        "web-1": {
            "packages": "apt",
            "install": True,
            "units": ["nginx.service", "worker.service"],
        },
        "db-1": {"packages": "apt", "install": False, "units": []},
    },
}
SERVERS = frozenset(CATALOG["servers"])
CABINET = api.Principal("cabinet", -1, "cabinet")
# the seeded /etc/cron.d/backup: a sha256, as the monitor records a file
CRON_DIGEST = "f1899cfe74a7c709d9cdd73f2069f453fcfd271b7b5207e07152f936148c638f"


def _dirs(root: str, *names) -> list:
    paths = [os.path.join(root, n) for n in names]
    for path in paths:
        os.makedirs(path, exist_ok=True)
    return paths


def dev_service(state_dir: str, *, origin=None, db=None, language=None) -> service.Service:
    """The service over ``state_dir`` (created as needed), with its hub; nothing listens yet."""
    from kd.cabinet.web import ws

    os.makedirs(state_dir, exist_ok=True)
    if not os.environ.get("KD_HOST_JSON") and not os.path.exists(naming.HOST_JSON):
        os.environ["KD_HOST_JSON"] = str(EXAMPLE_HOST)
    cfg = config.load(
        dev=True, origin=origin, files_dir=os.path.join(state_dir, "files"), language=language
    )
    base = store.Store.open(db or os.path.join(state_dir, "cabinet.db"))
    base.seed_projects(PROJECTS)
    registry = os.path.join(state_dir, "agents.json")
    with open(registry, "w", encoding="utf-8") as handle:
        json.dump({a: {"user": f"{naming.base_user()}-{a}"} for a in AGENTS}, handle)
    people = users.Users(base)
    ctx = api.Context(base, registry, str(REPO), require_root_owner=False, users=people)
    api.install_host(ctx, lambda: HOSTED)
    ctx.files_dir = cfg.files_dir
    # the repository registry is root's file on a host; here the code project is bound to a synthetic one, so
    # its code can have a council
    ctx.repos_path = os.path.join(state_dir, "repos.json")
    ctx.egress_path = os.path.join(state_dir, "egress.json")  # none: the dev cabinet grants no probe exit
    if not os.path.exists(ctx.repos_path):
        with open(ctx.repos_path, "w", encoding="utf-8") as handle:
            json.dump({CODE_PROJECT: {"repo": f"example/{CODE_PROJECT}", "dir": CODE_PROJECT}}, handle)
    cabinet = client.LocalClient(ctx, CABINET)
    proposals, grants = _dirs(os.path.join(state_dir, "admin"), "proposals", "grants")
    ctx.admin = admin_approval.Service(
        admin_approval.Paths(proposals=proposals, grants=grants),
        catalog=lambda: ops.Catalog.from_dict(copy.deepcopy(CATALOG)),
        inventory=lambda: SERVERS,
        enabled=lambda: SERVERS,
        client=cabinet,
        deny=lambda command, alias: None,
    )
    promote = os.path.join(state_dir, "promote")
    p_proposals, p_decisions, p_grants, p_children = _dirs(
        promote, "proposals", "decisions", "grants", "children"
    )
    switch = os.path.join(promote, "base.json")
    if not os.path.exists(switch):
        with open(switch, "w", encoding="utf-8") as handle:
            handle.write("{}")
    ctx.promote = boss_promote.Settings(
        proposals=p_proposals,
        decisions=p_decisions,
        client=cabinet,
        registry_path=registry,
        audit_path=os.path.join(promote, "boss.jsonl"),
        channel_switch=switch,
        grants=p_grants,
        children=p_children,
    )
    usage = os.path.join(state_dir, "usage.jsonl")
    svc = service.Service(
        ctx,
        cfg,
        users=people,
        decisions=decisions.Decisions(base),
        notifier=notify.Notifier(base, people, cfg, usage_path=usage),
    )
    svc.usage_path = usage
    from kd.cabinet.dev_model_keys import DevModelKeys

    svc.model_keys = DevModelKeys(svc, state_dir)
    svc.hub = ws.Hub(svc)
    return svc


# --- the seed -------------------------------------------------------------------------------------
class SeedClock:
    """The seed's time: a board that grew over the past days, not one written within a second. ``at`` sets a
    moment that far before the real now; every reading after it is ``step_s`` later. The seed writes its parts
    oldest first, so the times grow with the event order."""

    def __init__(self, now: str):
        self.now = users.parse_time(now)
        self.moment = self.now
        self.step = timedelta(seconds=30)

    def at(self, *, hours=0.0, minutes=0.0, step_s=30.0) -> None:
        moment = self.now - timedelta(hours=hours, minutes=minutes)
        if moment < self.moment and self.moment != self.now:
            raise ValueError("the seed's parts must be written oldest first")
        self.moment, self.step = moment, timedelta(seconds=step_s)

    def __call__(self) -> str:
        reading = users.format_time(self.moment)
        self.moment += self.step
        return reading


CARDS_FROM_H, CARDS_EVERY_H = 150, 6  # the first card six days ago, the last one over a day ago
CARDS = (
    # (key, project, status, assignee, title)
    ("promote", "alert", "Inbox", "@owner", "Supplier invoice for October"),
    (
        "plan",
        "admin",
        "Inbox",
        "@sysadmin",
        "web-1: refresh package indexes and restart nginx",
    ),
    ("plan2", "admin", "Inbox", "@sysadmin", "web-1: start the queue worker"),
    ("plan3", "admin", "Inbox", "@sysadmin", "web-1: refresh the package lists"),
    ("critical", "admin", "To Do", "@sysadmin", "db-1: remove a stale WireGuard peer"),
    ("todo", "digipaddock", "To Do", "@dev", "Cache the board's responses"),
    ("thread", "digipaddock", "In Progress", "@dev", "Card page in the cabinet"),
    ("progress", "alert", "In Progress", "@owner", "Contract with the supplier"),
    ("review1", "digipaddock", "Review", "@dev", "Push notifications: quiet hours"),
    ("review2", "alert", "Review", "@owner", "Update the price list"),
    ("blocked1", "digipaddock", "Blocked", "@dev", "Passkey sign-in on desktop"),
    ("blocked2", "admin", "Blocked", "@sysadmin", "web-1: renew the certificate"),
    ("done1", "digipaddock", "Done", "@dev", "Event feed"),
    ("done2", "alert", "Done", "@owner", "Payment reconciliation"),
    ("form", "digipaddock", "Inbox", "@analyst", "Brief: memory service probe"),
    ("oldq", "digipaddock", "Inbox", "@analyst", "Brief: weekly spend summary"),
    ("brief", "digipaddock", "Inbox", "@analyst", "Daily error summary"),
    # A long thread with a result in Review, a run that failed
    ("long", "digipaddock", "In Progress", "@dev", "Card history in pages"),
    ("failed", "digipaddock", "In Progress", "@dev", "Build the spend report"),
    # the pipeline's records — an outcome with a blocking wait and a turn that left nobody on the card
    ("pipeline", "digipaddock", "In Progress", "@dev", "Agents' progress report"),
    # a brief under its council — the last card, so the numbers of the others stay
    ("council", "digipaddock", "Inbox", "@analyst", "Nightly backup check"),
)
COUNCIL_TITLE = "Nightly backup check"
COUNCIL = {"brief": {"critics": ["claude:opus", "claude:sonnet"], "rounds": 3}}
COUNCIL_BRIEF = (
    "Goal: every night check that the last backup restores.\n\n"
    "Source: the backup log.\nFor: the owner — a notice when a restore fails.\nFormat: one line per database."
)
COUNCIL_AC = ("A failed restore raises a notice", "The check runs after the nightly backup")
# what the two critics hand back (kd.council.parse_block): the first found gaps, the second none
COUNCIL_REVIEWS = (
    {
        "status": "done",
        "verdict": "needs_changes",
        "findings": [
            {
                "severity": "major",
                "location": "AC 1",
                "evidence": "the brief names a notice on a failure only",
                "scenario": "the check itself stops running and nobody learns of it for weeks",
                "recommendation": "send a short notice on success too, or alert when no check ran for a day",
            },
            {
                "severity": "major",
                "location": "Source",
                "evidence": "the backup log says the backup was written, not that it restores",
                "scenario": "a truncated dump is logged as written and the check passes",
                "recommendation": "restore the dump into a scratch database and count its tables",
            },
            {
                "severity": "minor",
                "location": "Format",
                "evidence": "one line per database, no time of the backup",
                "scenario": "the owner cannot tell a fresh backup from yesterday's",
                "recommendation": "add the backup's time to each line",
            },
        ],
        "questions": [
            {
                "text": "Where does a restore run?",
                "options": ["on the database server", "on a separate host"],
                "recommendation": "on a separate host",
            }
        ],
        "missing_inputs": [],
    },
    {"status": "done", "verdict": "ready", "findings": [], "questions": [], "missing_inputs": []},
)
COUNCIL_USED = {"in": 5, "out": 900, "cache_read": 1000, "cache_write": 200, "cost": 0.12}


def council_usage(ref: str) -> dict:
    """What a seeded critic on the subscription model ``ref`` reports: its usage under the name Claude Code gives
    the alias's model (the block names what served the read, the list the version the alias served)."""
    upstream = llm_models.claude_models()[ref]["upstream"]
    return {"models": {f"claude-{upstream}-5-5": dict(COUNCIL_USED)}, "requests": 4}


COUNCIL_QUESTION = "The council asks to prove that a backup restores. Where may a restore run: on the database server or on a separate host?"
COUNCIL_ANSWER = "the owner asked for a notice on a failure only; a silent check is the monitor's to catch"
PIPELINE_QUESTION = "Count a retry as a separate row?"
PIPELINE_NOTE = "waiting for an answer: count a retry as a separate row"
LONG_THREAD = 450
LONG_QUESTION_AT = 10  # the long thread's answered question, far above the window a link to it opens on
LONG_SUMMARY = "Done: the thread and the history load in pages. PR: https://forge.example/pr/42."
# a question form v1 (a single choice with a recommendation, a multiple one with own words, a text) and
# an old text question whose options the cabinet recognizes
FORM_QUESTION = {
    "schema_version": 1,
    "text": "Questions on the brief, round 1",
    "questions": [
        {
            "id": "probe", "title": "How should the service be probed?", "context": "Today the monitor goes through the gateway.",
            "type": "single_select", "required": True, "allow_custom": True,
            "options": [
                {"id": "http", "label": "A direct HTTP request", "description": "no key in the arguments", "recommended": True},
                {"id": "gate", "label": "Through the gateway", "description": "as now"},
            ],
        },
        {
            "id": "extra", "title": "What else should be checked?", "type": "multi_select", "allow_custom": True,
            "options": [
                {"id": "worker", "label": "The worker", "description": "as a separate problem"},
                {"id": "queue", "label": "The queue", "description": "processing delay"},
            ],
        },
        {"id": "window", "title": "When can a stop be tried?", "type": "text"},
    ],
}
OLD_QUESTION = (
    "Questions on the brief.\n\n"
    "1. Where do the numbers come from. A) from the token ledger (already kept by day); B) from the session logs — "
    "more precise, but slower. I recommend A.\n\n"
    "2. Who sees it. A) only the owner; B) the owner and the partners. I recommend A.\n\n"
    "3. Which day of the week should the summary come?\n\n"
    "4. Is a breakdown by agent needed? I recommend yes.\n\n"
    "5. What is the spend alert threshold?"
)
DESCRIPTION = (
    "What to do and why — **briefly**.\n\n"
    "- context: the board in the cabinet\n- constraints: no new rights for the agents\n\n"
    "The details are in the thread."
)
# the brief the analyst hands to @dev (v1)
BRIEF = (
    "Goal: a daily summary of the agents' errors.\n\n"
    "Source: the session log.\nFor: the owner, at 09:00.\nFormat: one line per agent."
)
BRIEF_AC = ("The summary arrives at 09:00", "The errors are grouped by agent")
DOD = "- [ ] #1 Tests are green\n- [x] #2 Review passed\n\nA note between the items.\n- [ ] #3 Rolled out to stage"


def _cards(svc, clock=None) -> dict:
    made = {}
    for n, (key, project, status, assignee, title) in enumerate(CARDS):
        if clock:  # one card every few hours over the past days, oldest first
            clock.at(hours=CARDS_FROM_H - n * CARDS_EVERY_H, step_s=60)
        values = {
            "title": title,
            "project": project,
            "status": status,
            "assignee": assignee,
            "created_by": "@owner",
            "description": DESCRIPTION,
            "acceptance": [
                {"text": "Works on a phone", "checked": True},
                {"text": "Works on the **desktop**", "checked": False},
                {"text": "Texts in English and Russian", "checked": False},
            ],
            "dod": DOD,
        }
        if key in ("plan", "plan2", "plan3", "blocked2"):
            values["servers"] = ["web-1"]
        if key == "critical":
            values["servers"] = ["db-1"]
        if key in ("brief", "council"):
            values["labels"] = ["spec:task-brief"]
        if key == "blocked1":  # waits for a card still To Do
            values["dependencies"] = [made["todo"].id]
        dod = values.pop("dod")
        if key in ("plan", "plan2", "plan3", "critical", "promote"):
            values.pop("acceptance")
            dod = ""
        card = svc.ctx.store.create_card(values, actor="user:owner")
        if dod:  # creating takes no DoD; the owner writes it as the first edit
            card = svc.ctx.store.edit_card(
                card.id, card.revision, {"dod": dod}, actor="user:owner"
            )
        made[key] = card
    return made


def _handoff(svc, card) -> None:
    """The analyst's handoff (as ``kd-task handoff`` writes it): the brief, @dev, its spec and brief-ready — v1."""
    svc.ctx.store.edit_card(
        card.id,
        card.revision,
        {
            "description": BRIEF,
            "acceptance": [{"text": text, "checked": False} for text in BRIEF_AC],
            "assignee": "@dev",
            "labels_remove": ["spec:task-brief"],
            "labels_add": ["spec:implementer", "brief-ready"],
            "comment": {"text": "[analyst] Brief ready: v1", "author": "@analyst"},
        },
        actor="agent:analyst",
    )


def _council(svc, card) -> None:
    """the owner puts a council of two on the card, the analyst hands the brief in, both critics read it
    (their jobs end as the host reports them) and the analyst, woken, rejects the first finding in its turn and
    asks the owner what it needs for the second: the council waits for its writer with one finding still owed,
    the brief for the owner's answer. Nothing is left queued for the dev host."""
    store = svc.ctx.store
    root = api.Principal("root", 0, "root")
    card = store.edit_card(card.id, card.revision, {"council": COUNCIL}, actor="user:owner")
    store.edit_card(
        card.id,
        card.revision,
        {
            "description": COUNCIL_BRIEF,
            "acceptance": [{"text": text, "checked": False} for text in COUNCIL_AC],
            "assignee": "@dev",
            "labels_remove": ["spec:task-brief"],
            "labels_add": ["spec:implementer", "brief-ready"],
            "comment": {"text": "[analyst] Brief ready: v1", "author": "@analyst"},
        },
        actor="agent:analyst",
    )
    found = councils.latest(store, card.id, "brief")
    for run, review in zip(councils.runs(store, found["id"], found["round"]), COUNCIL_REVIEWS):
        for report in (
            {"state": "starting", "slot": "analyst/worker1"},
            {"state": "done", "usage": council_usage(run["model"]), "result": review},
        ):
            api.dispatch(svc.ctx, root, {"op": "host.report", "items": [{"type": "job", "job": run["job"], **report}]})
    [wake] = [
        c for c in store.commands(agent="analyst", limit=200) if c["thread"] == card.thread and c["state"] == "queued"
    ]
    session = store.session_for(card.thread, "analyst")
    for state in ("delivered", "started"):
        store.command_state(wake["id"], state, session=session["id"])
    api.dispatch(
        svc.ctx,
        api.Principal("executor", -1, naming.base_user()),
        {
            "op": "council.reply",
            "agent": "analyst",
            "card": card.id,
            "answers": [{"finding": "r1.f1", "answer": "rejected"}],
            "note": f"r1.f1: {COUNCIL_ANSWER}",
        },
    )
    svc.decisions.open(
        "question", card=card.id, payload={"text": COUNCIL_QUESTION}, requested_by="agent:analyst", now=svc.now()
    )
    store.command_state(wake["id"], "done", session=session["id"])


# (a progress line, the owner's remark, @dev's answer) of the long card thread
THREAD_TALK = (
    ("the description renders as Markdown", "Keep the AC above the thread, please.",
     "Done: the AC sit right under the description; `npm test` is green."),
    ("the acceptance criteria are editable", "Can a checked item be unchecked?",
     "Yes — a click toggles it, and the history keeps *who* changed it."),
    ("the thread loads newest first", "On the phone the composer hides the last message.",
     "Fixed: the thread scrolls above the composer now."),
    ("attachments show a preview", "Logs too, or only images?",
     "Images get a preview; text files are a link with the size, like `build.log`."),
    ("the page works at 360 px", "Looks good. What is left?",
     "Only the empty states; I will send the card to Review after them."),
)


def _thread(svc, card_id: str, files_dir: str):
    add = svc.ctx.store.add_message
    lines = [
        ("user:owner", "text", "We need a card page: description, AC, thread.", None),
        ("agent:dev", "text", "On it. Starting with the markup and the tests.", None),
        ("agent:dev", "progress", "The page markup is done", {"step": 1}),
        (
            "agent:dev",
            "tool_call",
            "```bash\nnpm test -- card\n```",
            {"tool": "Bash"},
        ),
        ("agent:dev", "tool_result", "Tests: 12 passed", {"tool": "Bash", "ok": True}),
        (
            "agent:boss",
            "directive",
            "Show the owner a draft by the end of the day.",
            {"directive": "d1", "state": "delivered"},
        ),
        (
            "system",
            "status",
            "",
            {
                "key": "sys.card.status",
                "params": {"by": "@dev", "from": "To Do", "to": "In Progress"},
            },
        ),
    ]
    # the rest of the thirty: a progress line, the owner's remark and @dev's answer, in turn
    for n in range(30 - len(lines) - 1):
        said = THREAD_TALK[n // 3 % len(THREAD_TALK)]
        if n % 3 == 0:
            lines.append(("agent:dev", "progress", f"Step {n + 2}: {said[0]}", {"step": n + 2}))
        elif n % 3 == 1:
            lines.append(("user:owner", "text", said[1], None))
        else:
            lines.append(("agent:dev", "text", said[2], None))
    for author, kind, body, payload in lines:
        add(card_id, author=author, kind=kind, body=body, payload=payload)
    os.makedirs(files_dir, mode=0o700, exist_ok=True)
    data = "Build log\nOK\n".encode("utf-8")
    file_id = "0" * 31 + "1"
    with open(os.path.join(files_dir, file_id), "wb") as handle:
        handle.write(data)
    import hashlib

    svc.ctx.store.add_file(
        card_id,
        author="agent:dev",
        file={
            "id": file_id,
            "name": "build.log",
            "mime": "text/plain",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )


# one-step plans, for a batch of two: (the step kept, intent, basis)
SHORT = {
    "worker": (
        2,
        "Start the queue worker",
        "ssh-ro: worker.service is stopped after the upgrade",
    ),
    "index": (0, "Refresh the package lists", "ssh-ro: the package lists are over a week old"),
}


def _long(svc, card) -> None:
    """a thread longer than its window and the card sent to Review with a summary and a link. The
    agent's lines come from its session (``via`` session, as the host reports them: not its board writes);
    near the top, a question the owner has answered (a deep link to it pages back)."""
    for n in range(LONG_THREAD):
        if n == LONG_QUESTION_AT:
            asked = svc.decisions.open(
                "question", card=card.id, payload={"text": "Load a long thread in pages?"},
                requested_by="agent:dev", now=svc.now(),
            )
            svc.decisions.reply(asked["id"], user="owner", text="Yes, 200 messages a page", now=svc.now())
        body = f"Message #{n + 1}"
        if n % 2:
            svc.ctx.store.add_message(card.id, author="user:owner", kind="text", body=body)
        else:
            svc.ctx.store.add_thread_message(card.thread, author="agent:dev", kind="text", body=body, via="session")
    card = svc.ctx.store.card(card.id)
    svc.ctx.store.edit_card(
        card.id,
        card.revision,
        {"status": "Review", "summary": LONG_SUMMARY, "comment": {"text": "Ready for review", "author": "@dev"}},
        actor="agent:dev",
    )


def _failed_run(svc, card) -> None:
    """a request whose session ended in a failure — the overview's error, the agent's failure."""
    store = svc.ctx.store
    command = store.enqueue(
        {"key": f"seed:{card.id}:run", "agent": "dev", "thread": card.thread, "kind": "assigned", "text": "[board] go"}
    )
    session = store.session_for(card.thread, "dev")
    store.command_state(command["id"], "delivered", session=session["id"])
    store.command_state(command["id"], "started", session=session["id"])
    store.command_state(command["id"], "failed", session=session["id"], reason="exit 1", error="exit 1")


def _pipeline(svc, card) -> None:
    """@dev's first turn recorded ``wait_user`` on its question (the outcome and its open wait, as the
    turn's end leaves them); its second turn ended without a next step — the problem."""
    store = svc.ctx.store
    asked = svc.decisions.open("question", card=card.id, payload={"text": PIPELINE_QUESTION},
                               requested_by="agent:dev", now=svc.now())
    session = store.session_for(card.thread, "dev")
    turns = []
    for n, kind in ((1, "assigned"), (2, "message")):
        command = store.enqueue({"key": f"seed:{card.id}:turn{n}", "agent": "dev", "thread": card.thread,
                                 "kind": kind, "text": f"[board] {card.id} turn {n}"})
        for state in ("delivered", "started", "done"):
            store.command_state(command["id"], state, session=session["id"])
        turns.append(command["id"])
    now = svc.now()
    with store.transaction():
        store._conn.execute(
            "INSERT INTO turn_outcomes (command, card, thread, agent, outcome, mode, note, basis, state, created,"
            " updated) VALUES (?,?,?,'dev','wait_user','all',?,'{}','final',?,?)",
            (turns[0], card.id, card.thread, PIPELINE_NOTE, now, now))
        store._conn.execute(
            "INSERT INTO waits (id, command, card, agent, kind, ref, blocking, mode, state, created, updated)"
            " VALUES (?,?,?,'dev','decision',?,1,'all','open',?,?)",
            (f"seed{turns[0][:12]}", turns[0], card.id, asked["id"], now, now))
    problems.raise_problem(svc.ctx, key=f"missing:{turns[1]}", record=store.card(card.id), kind="missing_next_step",
                           agent="dev", command=turns[1])


def _plan(svc, card_id: str, short: str = "") -> dict:
    """A sysadmin plan of three steps (``short``: one of them, see ``SHORT``)."""
    card = client.admin_card(client.LocalClient(svc.ctx, CABINET), card_id)
    step = {
        "what": "",
        "effect": "",
        "risk": {"level": "low", "why": ""},
        "rollback": "Not needed",
    }
    data = {
        "schema_version": 2,
        "task_id": card_id,
        "intent": "Refresh the package indexes, restart nginx and the worker",
        "basis": "ssh-ro: nginx returns 502 after the configuration update",
        "operations": [
            {
                **step,
                "server": "web-1",
                "verb": "package.refresh",
                "what": "Refresh the package indexes",
                "effect": "Nothing changes for users",
                "risk": {"level": "low", "why": "Only reads the lists"},
            },
            {
                **step,
                "server": "web-1",
                "verb": "service.restart",
                "unit": "nginx.service",
                "what": "Restart the web server",
                "effect": "The site is down for a couple of seconds",
                "risk": {
                    "level": "medium",
                    "why": "The new configuration may fail to start",
                },
                "rollback": "Restore the previous configuration and restart",
            },
            {
                **step,
                "server": "web-1",
                "verb": "service.start",
                "unit": "worker.service",
                "what": "Start the queue worker",
                "effect": "The queue is processed again",
                "risk": {"level": "low", "why": "The worker was already stopped"},
            },
        ],
    }
    if short:
        kept, data["intent"], data["basis"] = SHORT[short]
        data["operations"] = data["operations"][kept : kept + 1]
    plan = admin_proposal.submit(
        svc.ctx.admin.paths.proposals,
        json.dumps(data, ensure_ascii=False).encode("utf-8"),
        agent="sysadmin",
        card=card,
        catalog=ops.Catalog.from_dict(copy.deepcopy(CATALOG)),
        inventory=SERVERS,
        enabled=SERVERS,
        now=users.parse_time(svc.now()).astimezone(timezone.utc),
    )
    payload = {
        "task_id": card_id,
        "proposal_sha256": plan.sha256,
        "card_scope_sha256": card.scope_sha256,
    }
    return svc.decisions.open(
        "admin_plan",
        card=card_id,
        payload=payload,
        requested_by="agent:sysadmin",
        now=svc.now(),
    )


def _critical(svc, card_id: str) -> dict:
    card = client.admin_card(client.LocalClient(svc.ctx, CABINET), card_id)
    data = {
        "schema_version": 2,
        "task_id": card_id,
        "intent": "Remove a peer that no longer answers",
        "basis": "wg show: the last handshake was 40 days ago",
        "servers": ["db-1"],
        "command": "wg set wg0 peer 'abc=' remove",
        "effect": "The peer is disconnected from the network",
        "risk": {"level": "high", "why": "If the peer is still needed, the link to it is lost"},
        "rollback": "Restore the peer from the configuration backup",
    }
    action = admin_proposal.submit_action(
        svc.ctx.admin.paths.proposals,
        json.dumps(data, ensure_ascii=False).encode("utf-8"),
        agent="sysadmin",
        card=card,
        inventory=SERVERS,
        enabled=SERVERS,
        now=users.parse_time(svc.now()).astimezone(timezone.utc),
        deny=lambda command, alias: None,
    )
    payload = {
        "task_id": card_id,
        "proposal_id": action.proposal_id,
        "proposal_sha256": action.sha256,
        "card_scope_sha256": card.scope_sha256,
    }
    return svc.decisions.open(
        "admin_critical",
        card=card_id,
        payload=payload,
        requested_by="agent:sysadmin",
        now=svc.now(),
    )


def _promote(svc, card_id: str) -> dict:
    record = svc.ctx.store.card(card_id)
    proposal = boss_promote.propose(
        boss_board.card_from(record.to_dict()),
        svc.ctx.routing(),
        record.revision,
        "The brief is ready and the assignee is free",
        now=users.parse_time(svc.now()).timestamp(),
        root=svc.ctx.promote.proposals,
        decisions=svc.ctx.promote.decisions,
    )
    payload = {"proposal_id": proposal.id, "proposal_sha256": proposal.sha256}
    return svc.decisions.open(
        "promote",
        card=card_id,
        payload=payload,
        requested_by="agent:boss",
        now=svc.now(),
    )


def _monitoring(svc) -> None:
    """Representative monitoring data for browser acceptance; all addresses and servers are fictitious."""
    root = api.Principal("root", 0, "root")
    now = svc.now()
    problems = []
    for n, server, check, subject, value in (
        (330, "web-1", "reboot", "-", "15 d"),
        (333, "db-1", "sec:units", "ModemManager.service", "symlink -> /dev/null"),
        (334, "db-1", "sec:cron", "/etc/cron.d/backup", CRON_DIGEST),  # the e2e signs its acceptance
    ):
        problems.append({"number": n, "server": server, "check": check, "subject": subject,
                         "key": f"{server}:{check}:{subject}", "level": "warn", "value": value,
                         "text": "Deviation from the accepted rule", "opened_at": now,
                         "history": [{"kind": "open", "at": now, "level": "warn", "value": value}]})
    # one tick that opened the same unit change on 13 servers — one group of P-340…P-352
    tick = (datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for k in range(13):
        server, value = f"grp-{k + 1:02d}", "enabled -> masked"
        problems.append({"number": 340 + k, "server": server, "check": "sec:units", "subject": "snapd.service",
                         "key": f"{server}:sec:units:snapd.service", "level": "warn", "value": value,
                         "text": "Deviation from the accepted rule", "opened_at": tick,
                         "history": [{"kind": "open", "at": tick, "level": "warn", "value": value}]})
    api.dispatch(svc.ctx, root, {"op": "incident.import", "items": problems})
    checks = [{"server": server, "check": check, "rows": [{"subject": "", "level": "ok", "value": "", "text": ""}]}
              for server in ("web-1", "db-1") for check in
              ("autoupdates", "cores", "disk", "eol", "logins", "mem", "oom", "ports", "units", "reach", "load")]
    for server in ("web-1", "db-1"):
        checks += [{"server": server, "check": f"gate:{name}", "rows": [{"subject": "", "level": "ok"}]}
                   for name in ("disk", "mem", "reboot", "ports", "sec")]
        checks.append({"server": server, "check": "updates", "rows": [{"subject": "security", "level": "ok", "value": "0"}]})
        checks.append({"server": server, "check": "sec:units", "rows": [{"subject": "", "level": "ok"}]
                       if server == "web-1" else [{"subject": "ModemManager.service", "level": "warn", "value": "symlink -> /dev/null"}]})
        checks.append({"server": server, "check": "sec:cron", "rows": [{"subject": "", "level": "ok"}]
                       if server == "web-1" else [{"subject": "/etc/cron.d/backup", "level": "warn", "value": CRON_DIGEST}]})
        checks.append({"server": server, "check": "reboot", "rows": [{"subject": "-", "level": "warn" if server == "web-1" else "ok",
                        "value": "17 d" if server == "web-1" else "no", "text": "A reboot is pending" if server == "web-1" else ""}]})
    trust = {"trusted": {"192.0.2.20": "office (demo)"}, "servers": {server: {
        "identities": {"root@ED25519 SHA256:" + "a" * 64: "owner (demo)"},
        "ports": {"tcp:[2001:db8::1]:22": "sshd"}, "wgpeers": ["wg0:" + "b" * 64], "taken_at": now,
    } for server in ("web-1", "db-1")}}
    for check in checks:
        check["at"] = now
    api.dispatch(svc.ctx, root, {"op": "monitor.push", "items": [{"key": "seed:monitor", "kind": "sync", "at": now,
        "servers": [{"alias": "web-1", "host": "192.0.2.1", "unreachable_count": 0},
                    {"alias": "db-1", "host": "192.0.2.2", "unreachable_count": 0}], "checks": checks, "trust": trust}]})
    # the platform's own alert, with no card: the incidents page lists it above the incidents
    api.dispatch(svc.ctx, root, {"op": "notice.post", "source": "alert", "level": "critical", "key": "notice.alert.unit",
                                 "params": {"host": "factory-1", "unit": "digipaddock-mirror.service",
                                            "reason": "kd-mirror: git fetch: timed out"}})


def _seed(svc, clock) -> None:
    """The seed's parts, oldest first (``SeedClock``): the cards over the past days, the threads since, the
    decisions open within the last two hours (they have their expiry: the critical action, half an hour, is the
    newest), the monitor's sync a minute ago."""
    clock.at(hours=CARDS_FROM_H + 2)
    svc.users.create("owner", "Owner", roles=[("owner", "*")], actor="root")
    svc.users.create("anna", "Anna", roles=[("partner", ["alert"])], actor="root")
    made = _cards(svc, clock)
    clock.at(hours=29, step_s=8 * 60)
    _thread(svc, made["thread"].id, svc.cfg.files_dir)
    clock.at(hours=14, step_s=10)
    _long(svc, made["long"])
    clock.at(hours=9)
    _handoff(svc, made["brief"])
    clock.at(hours=6)
    _failed_run(svc, made["failed"])
    clock.at(hours=4)
    _pipeline(svc, made["pipeline"])
    clock.at(hours=3, step_s=10)
    _council(svc, made["council"])
    clock.at(minutes=110, step_s=60)
    _plan(svc, made["plan"].id)
    clock.at(minutes=95, step_s=60)
    _plan(svc, made["plan2"].id, short="worker")
    _plan(svc, made["plan3"].id, short="index")
    clock.at(minutes=50, step_s=60)
    _promote(svc, made["promote"].id)
    clock.at(minutes=35, step_s=30)
    svc.decisions.open(
        "question",
        card=made["thread"].id,
        payload={
            "text": "Show the card's draft to every partner or only to the owner?"
        },
        requested_by="agent:dev",
        now=svc.now(),
    )
    svc.decisions.open(
        "question", card=made["form"].id, payload=questions.validate_form(FORM_QUESTION),
        requested_by="agent:analyst", now=svc.now(),
    )
    svc.decisions.open(
        "question", card=made["oldq"].id, payload={"text": OLD_QUESTION}, requested_by="agent:analyst", now=svc.now()
    )
    clock.at(minutes=3, step_s=1)  # a critical action expires in half an hour (decisions.TTL_SECONDS)
    _critical(svc, made["critical"].id)
    clock.at(minutes=1, step_s=1)
    _monitoring(svc)


def apply(svc, *, out=sys.stdout, codes_path=None):
    """Seed the dev base once; print fresh sign-in codes every time (and write them to ``codes_path``, a
    JSON object person -> code, for the browser tests)."""
    base = svc.ctx.store
    if base.get_state(SEED_KEY) is None:
        clock = SeedClock(svc.now())
        real = (base._clock, svc.ctx.now, svc._now)
        base._clock = svc.ctx.now = svc._now = clock
        try:
            _seed(svc, clock)
        finally:
            base._clock, svc.ctx.now, svc._now = real
        # the requests the cards' writes queued are the dev host's to deliver now: dated days back, the dispatcher
        # would fail them undelivered (dispatcher.MAX_AGE) before the first delivery
        now = svc.now()
        with base.transaction():
            base._conn.execute("UPDATE commands SET created=?, updated=? WHERE state='queued'", (now, now))
        base.set_state(SEED_KEY, {"version": 1})
    codes = {}
    for person in ("owner", "anna"):
        codes[person] = svc.users.issue_code(person, svc.now(), actor="root")
        out.write(f"{person} code: {codes[person]}\n")
    out.flush()
    if codes_path:
        with open(codes_path, "w", encoding="utf-8") as handle:
            json.dump(codes, handle)
