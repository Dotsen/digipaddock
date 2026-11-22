"""cabinet.db (owner cabinet spec section 3): the board and its journals in one SQLite file.

Only the kd-cabinet service opens the file. Every write is one transaction of rows plus an
``events`` row; ``cards.revision`` is the compare-and-set counter every writer must present.
``DDL`` is schema v1 (the board); each later step adds its tables as ``MIGRATIONS[v]``,
which ``Store.open`` applies in order, in one transaction, on a new base as on an old one.
"""

SCHEMA_VERSION = 46
STATUSES = ("Inbox", "To Do", "In Progress", "Review", "Blocked", "Done", "Cancelled")
# closed without the work — a duplicate of its canonical card, or cancelled. Done stays «the result was
# accepted»: a Cancelled card satisfies no dependency and counts as no finished work.
TERMINAL = ("Done", "Cancelled")
CLOSE_OUTCOMES = ("duplicate", "cancelled")
MESSAGE_KINDS = (
    "text",
    "progress",
    "directive",
    "status",
    "decision",
    "tool_call",
    "tool_result",
    "file",
    "diagnostic",
    "notice",
    "assist",
    # a critic's review in a council's round and the writer's answers to its findings
    "review",
    "review_reply",
)
DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS projects (id TEXT PRIMARY KEY, name TEXT NOT NULL, route TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cards (
  id TEXT PRIMARY KEY, n INTEGER NOT NULL UNIQUE, project TEXT NOT NULL, title TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', acceptance TEXT NOT NULL DEFAULT '[]', dod TEXT NOT NULL DEFAULT '',
  plan TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL, assignee TEXT NOT NULL DEFAULT '', labels TEXT NOT NULL DEFAULT '[]',
  milestone TEXT NOT NULL DEFAULT '', priority TEXT NOT NULL DEFAULT '', due TEXT NOT NULL DEFAULT '',
  parent TEXT NOT NULL DEFAULT '', created_by TEXT NOT NULL, revision INTEGER NOT NULL,
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS cards_status ON cards(status);
CREATE INDEX IF NOT EXISTS cards_assignee ON cards(assignee);
CREATE TABLE IF NOT EXISTS card_deps (card TEXT NOT NULL, depends_on TEXT NOT NULL, PRIMARY KEY (card, depends_on));
CREATE TABLE IF NOT EXISTS card_servers (card TEXT NOT NULL, server TEXT NOT NULL, PRIMARY KEY (card, server));
CREATE TABLE IF NOT EXISTS threads (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, card TEXT UNIQUE, agent TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY, thread INTEGER NOT NULL REFERENCES threads(id), seq INTEGER NOT NULL,
  author TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}',
  session TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, UNIQUE (thread, seq));
CREATE TABLE IF NOT EXISTS events (
  seq INTEGER PRIMARY KEY, ts TEXT NOT NULL, actor TEXT NOT NULL, kind TEXT NOT NULL,
  card TEXT NOT NULL DEFAULT '', thread INTEGER, payload TEXT NOT NULL DEFAULT '{}');
CREATE INDEX IF NOT EXISTS events_card ON events(card);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attention_acks (event TEXT PRIMARY KEY, at TEXT NOT NULL);
"""

# v2: people, their roles, keys and sessions; decisions; push; attachments.
DDL_V2 = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
  language TEXT NOT NULL DEFAULT '', timezone TEXT NOT NULL DEFAULT '',
  password_hash TEXT NOT NULL DEFAULT '', totp_secret TEXT NOT NULL DEFAULT '', totp_enabled INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL, last_login TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS roles (id TEXT PRIMARY KEY, perms TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_roles (user TEXT NOT NULL REFERENCES users(id), role TEXT NOT NULL REFERENCES roles(id),
  projects TEXT NOT NULL DEFAULT '"*"', PRIMARY KEY (user, role));
CREATE TABLE IF NOT EXISTS login_codes (code_hash TEXT PRIMARY KEY, user TEXT NOT NULL, expires TEXT NOT NULL, used TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS login_attempts (key TEXT NOT NULL, at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS login_attempts_key ON login_attempts(key);
CREATE TABLE IF NOT EXISTS credentials (
  id INTEGER PRIMARY KEY, user TEXT NOT NULL REFERENCES users(id), kind TEXT NOT NULL DEFAULT 'passkey',
  credential_id BLOB NOT NULL UNIQUE, public_key BLOB NOT NULL, sign_count INTEGER NOT NULL DEFAULT 0,
  transports TEXT NOT NULL DEFAULT '[]', name TEXT NOT NULL DEFAULT '', signing_enabled INTEGER NOT NULL DEFAULT 1,
  created TEXT NOT NULL, last_used TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS web_sessions (
  id TEXT PRIMARY KEY, user TEXT NOT NULL REFERENCES users(id), device TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL, last_seen TEXT NOT NULL, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, card TEXT NOT NULL DEFAULT '', thread INTEGER, message INTEGER,
  payload TEXT NOT NULL DEFAULT '{}', payload_sha TEXT NOT NULL, requested_by TEXT NOT NULL,
  decide_permission TEXT NOT NULL, step_up INTEGER NOT NULL, state TEXT NOT NULL DEFAULT 'open',
  created TEXT NOT NULL, expires TEXT NOT NULL DEFAULT '', decided_by TEXT NOT NULL DEFAULT '', decided_at TEXT NOT NULL DEFAULT '',
  signature TEXT NOT NULL DEFAULT '', credential INTEGER, batch_id TEXT NOT NULL DEFAULT '', grant_id TEXT NOT NULL DEFAULT '',
  reply TEXT NOT NULL DEFAULT '', question TEXT NOT NULL DEFAULT '', totp_failures INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS decisions_state ON decisions(state);
CREATE INDEX IF NOT EXISTS decisions_card ON decisions(card);
CREATE TABLE IF NOT EXISTS push_subscriptions (
  id INTEGER PRIMARY KEY, user TEXT NOT NULL REFERENCES users(id), endpoint TEXT NOT NULL UNIQUE,
  p256dh TEXT NOT NULL, auth TEXT NOT NULL, device TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, last_ok TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS notification_prefs (user TEXT NOT NULL, kind TEXT NOT NULL, mode TEXT NOT NULL, PRIMARY KEY (user, kind));
CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY, user TEXT NOT NULL, event INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL, tag TEXT NOT NULL DEFAULT '',
  title TEXT NOT NULL, body TEXT NOT NULL, url TEXT NOT NULL DEFAULT '', due TEXT NOT NULL, sent TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, tried TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS notifications_due ON notifications(sent, due);
CREATE INDEX IF NOT EXISTS events_card ON events(card);
CREATE INDEX IF NOT EXISTS messages_author ON messages(author, kind);
CREATE TABLE IF NOT EXISTS attachments (
  id TEXT PRIMARY KEY, thread INTEGER NOT NULL, seq INTEGER NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL,
  size INTEGER NOT NULL, sha256 TEXT NOT NULL, created TEXT NOT NULL);
"""
# v3 (global review 15.10.2026): the last accepted TOTP step of a person — a code is taken once (RFC 6238 §5.2).
DDL_V3 = """
ALTER TABLE users ADD COLUMN totp_last_step INTEGER NOT NULL DEFAULT 0;
"""
# v4: the outbox of commands to agents, their sessions on the session host, one chat per agent.
DDL_V4 = """
CREATE TABLE IF NOT EXISTS commands (
  id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, agent TEXT NOT NULL, thread INTEGER NOT NULL REFERENCES threads(id),
  card TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, text TEXT NOT NULL, initiator TEXT NOT NULL DEFAULT '',
  event INTEGER, state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0,
  next_try TEXT NOT NULL DEFAULT '', lease_until TEXT NOT NULL DEFAULT '', session TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS commands_due ON commands(state, next_try);
CREATE INDEX IF NOT EXISTS commands_card ON commands(card);
CREATE INDEX IF NOT EXISTS commands_thread ON commands(thread, agent, state);
CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, thread INTEGER NOT NULL REFERENCES threads(id), agent TEXT NOT NULL,
  runtime TEXT NOT NULL DEFAULT 'claude', slot TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'new',
  runtime_session_id TEXT NOT NULL DEFAULT '', usage TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
  started TEXT NOT NULL DEFAULT '', ended TEXT NOT NULL DEFAULT '', heartbeat TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL, updated TEXT NOT NULL, UNIQUE (thread, agent));
CREATE INDEX IF NOT EXISTS sessions_agent ON sessions(agent, state);
CREATE UNIQUE INDEX IF NOT EXISTS threads_agent_chat ON threads(agent) WHERE kind='agent_chat';
"""
# v5: the structured answer to a question form; the versions of a card's brief.
DDL_V5 = """
ALTER TABLE decisions ADD COLUMN answer TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS brief_versions (
  card TEXT NOT NULL, version INTEGER NOT NULL, origin TEXT NOT NULL, author TEXT NOT NULL,
  description TEXT NOT NULL, acceptance TEXT NOT NULL, scope_sha TEXT NOT NULL, created TEXT NOT NULL,
  approved_by TEXT NOT NULL DEFAULT '', approved_at TEXT NOT NULL DEFAULT '', PRIMARY KEY (card, version));
"""
# v6: a person's marks on their own screens — the last journal seq they saw on the overview.
DDL_V6 = """
CREATE TABLE IF NOT EXISTS user_marks (
  user TEXT NOT NULL, name TEXT NOT NULL, value INTEGER NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (user, name));
"""
# v7: token usage per turn and per job; a session's last cumulative total; worker jobs.
DDL_V7 = """
ALTER TABLE sessions ADD COLUMN usage_total TEXT NOT NULL DEFAULT '{}';
UPDATE sessions SET usage_total='null' WHERE runtime_session_id != '';
CREATE TABLE IF NOT EXISTS usage_log (
  id INTEGER PRIMARY KEY, at TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL, source TEXT NOT NULL,
  card TEXT NOT NULL DEFAULT '', model TEXT NOT NULL, requests INTEGER NOT NULL, input INTEGER NOT NULL,
  output INTEGER NOT NULL, cache_read INTEGER NOT NULL, cache_write INTEGER NOT NULL, cost REAL NOT NULL,
  runtime TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS usage_log_at ON usage_log(at);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, card TEXT NOT NULL DEFAULT '', agent TEXT NOT NULL, caller TEXT NOT NULL,
  requester TEXT NOT NULL DEFAULT '', spec TEXT NOT NULL, writer INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'queued', slot TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '',
  data TEXT NOT NULL DEFAULT '{}', attempts INTEGER NOT NULL DEFAULT 0, next_try TEXT NOT NULL DEFAULT '',
  lease_until TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL,
  started TEXT NOT NULL DEFAULT '', ended TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS jobs_writer ON jobs(card)
  WHERE writer=1 AND state IN ('queued','delivered','running','cancelling');
CREATE INDEX IF NOT EXISTS jobs_due ON jobs(state, next_try);
"""
# v8: a tool's masked output apart from the thread (only whoever controls the agent reads it); a usage
# row's turn (its command), so a session's turns show their cost.
DDL_V8 = """
CREATE TABLE IF NOT EXISTS tool_outputs (
  thread INTEGER NOT NULL, seq INTEGER NOT NULL, session TEXT NOT NULL, command TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL, size INTEGER NOT NULL, truncated INTEGER NOT NULL, created TEXT NOT NULL,
  PRIMARY KEY (thread, seq));
ALTER TABLE usage_log ADD COLUMN command TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS usage_log_command ON usage_log(command) WHERE command != '';
CREATE INDEX IF NOT EXISTS tool_outputs_created ON tool_outputs(created);
CREATE INDEX IF NOT EXISTS messages_command ON messages(thread, session, json_extract(payload, '$.command'), seq);
"""
# v9: dev's pull requests as the forge executor published them, the latest of each; its diff for the page.
DDL_V9 = """
CREATE TABLE IF NOT EXISTS prs (
  repo TEXT NOT NULL, number INTEGER NOT NULL, card TEXT NOT NULL, project TEXT NOT NULL DEFAULT '',
  url TEXT NOT NULL DEFAULT '', title TEXT NOT NULL DEFAULT '', head TEXT NOT NULL, state TEXT NOT NULL,
  files TEXT NOT NULL DEFAULT '[]', cage TEXT NOT NULL DEFAULT '[]', diff TEXT NOT NULL DEFAULT '',
  truncated INTEGER NOT NULL DEFAULT 0, release INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL,
  PRIMARY KEY (repo, number));
CREATE INDEX IF NOT EXISTS prs_card ON prs(card);
"""
# v10: a person's read cursor in each agent chat — the last message seq they were shown. Existing people
# start at each chat's end: what was there before the feature is not suddenly new.
DDL_V10 = """
CREATE TABLE IF NOT EXISTS chat_reads (
  user TEXT NOT NULL, thread INTEGER NOT NULL, seq INTEGER NOT NULL, updated TEXT NOT NULL, PRIMARY KEY (user, thread));
INSERT OR IGNORE INTO chat_reads (user, thread, seq, updated)
  SELECT u.id, t.id, COALESCE((SELECT MAX(m.seq) FROM messages m WHERE m.thread=t.id), 0),
         strftime('%Y-%m-%dT%H:%M:%SZ','now')
  FROM users u, threads t WHERE t.kind='agent_chat';
CREATE INDEX IF NOT EXISTS messages_thread_kind ON messages(thread, kind, seq);
"""
# v11: what each turn of an agent on its own card ended with, what it waits for, the
# continuations the cabinet queued for it (their limits count these rows) and the problems it raised; a command's
# basis — the card as it was when a continuation was queued, checked again before delivery. ``pipeline_since`` is
# the first moment turns are held to it: the sweep never looks at turns that ended before.
DDL_V11 = """
CREATE TABLE IF NOT EXISTS turn_outcomes (
  command TEXT PRIMARY KEY, card TEXT NOT NULL, thread INTEGER NOT NULL, agent TEXT NOT NULL, outcome TEXT NOT NULL,
  mode TEXT NOT NULL DEFAULT 'all', note TEXT NOT NULL, basis TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS turn_outcomes_card ON turn_outcomes(card, state);
CREATE TABLE IF NOT EXISTS waits (
  id TEXT PRIMARY KEY, command TEXT NOT NULL, card TEXT NOT NULL, agent TEXT NOT NULL, kind TEXT NOT NULL,
  ref TEXT NOT NULL, ref_version TEXT NOT NULL DEFAULT '', blocking INTEGER NOT NULL, mode TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending', reason TEXT NOT NULL DEFAULT '', next_check TEXT NOT NULL DEFAULT '',
  deadline TEXT NOT NULL DEFAULT '', resolved_by INTEGER, created TEXT NOT NULL, updated TEXT NOT NULL,
  UNIQUE (command, kind, ref));
CREATE INDEX IF NOT EXISTS waits_ref ON waits(kind, ref, state);
CREATE INDEX IF NOT EXISTS waits_card ON waits(card, state);
CREATE TABLE IF NOT EXISTS continuations (
  key TEXT PRIMARY KEY, card TEXT NOT NULL, agent TEXT NOT NULL, source TEXT NOT NULL,
  command TEXT NOT NULL DEFAULT '', state TEXT NOT NULL, mark TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS continuations_card ON continuations(card, created);
CREATE TABLE IF NOT EXISTS problems (
  id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, card TEXT NOT NULL, kind TEXT NOT NULL,
  agent TEXT NOT NULL DEFAULT '', command TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '{}',
  state TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL, updated TEXT NOT NULL,
  resolved_by TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS problems_card ON problems(card, state);
ALTER TABLE commands ADD COLUMN basis TEXT NOT NULL DEFAULT '{}';
INSERT OR IGNORE INTO meta (key, value) VALUES ('pipeline_since', strftime('%Y-%m-%dT%H:%M:%SZ','now'));
"""
# v12: the projects table is the board's only source of projects — no file mirrors into it any more. The
# repository a project is bound to (shown only; the repository request fills it) and, once, the names of the projects the old mirror
# wrote with name = id (a project the owner already named keeps its name). a deployment's site name is not
# renamed here — the owner names it in Settings.
DDL_V12 = """
ALTER TABLE projects ADD COLUMN repo TEXT NOT NULL DEFAULT '';
UPDATE projects SET name='DIGIPADDOCK' WHERE id='digipaddock' AND name='digipaddock';
UPDATE projects SET name='Servers' WHERE id='admin' AND name='admin';
"""
# v13: when a project's route last changed — a route holds for the cards made after it; an open card keeps
# its assignee (the stall rule ``route_mismatch`` does not count it).
DDL_V13 = """
ALTER TABLE projects ADD COLUMN routed TEXT NOT NULL DEFAULT '';
"""
# v14: push per device and per reply. A notification is delivered to each subscription of its
# person on its own (``push_deliveries``: one device's failure or success is not another's), only to subscriptions
# made before it was due (a new device does not get the old queue). An agent's chat reply is one row per person and
# reply (``reply``: the thread and the turn or message), checked against the person's read cursor and their presence
# in that chat (``chat_presence``: a lease a visible, focused chat page renews) right before it is sent.
DDL_V14 = """
ALTER TABLE notifications ADD COLUMN reply TEXT NOT NULL DEFAULT '';
ALTER TABLE notifications ADD COLUMN thread INTEGER NOT NULL DEFAULT 0;
ALTER TABLE notifications ADD COLUMN seq INTEGER NOT NULL DEFAULT 0;
CREATE UNIQUE INDEX IF NOT EXISTS notifications_reply ON notifications(user, reply) WHERE reply != '';
CREATE TABLE IF NOT EXISTS push_deliveries (
  notification INTEGER NOT NULL, subscription INTEGER NOT NULL, sent TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0, tried TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (notification, subscription));
CREATE TABLE IF NOT EXISTS chat_presence (
  user TEXT NOT NULL, thread INTEGER NOT NULL, device TEXT NOT NULL, until TEXT NOT NULL,
  PRIMARY KEY (user, thread, device));
UPDATE notifications SET sent=strftime('%Y-%m-%dT%H:%M:%SZ','now') WHERE sent='' AND attempts >= 5;
"""
# v15: a card closed without the work (Cancelled) says why — ``outcome`` duplicate|cancelled — and
# a duplicate names its canonical card; leaving Cancelled clears both (the history keeps them).
DDL_V15 = """
ALTER TABLE cards ADD COLUMN outcome TEXT NOT NULL DEFAULT '';
ALTER TABLE cards ADD COLUMN canonical TEXT NOT NULL DEFAULT '';
"""
# v16: the factory's operations — one row per logical request (``key``: a release's ``release:<rev>``, a
# lead's own key), its card, requester, agents and the revision it installs, the grant it runs under and how far it
# got; ``reconcile`` marks a row whose result was lost and must be read back from the host before anything else. A
# card's "Operations" (the typed steps its brief or plan names) and the brief version that froze them.
DDL_V16 = """
CREATE TABLE IF NOT EXISTS operations (
  id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, card TEXT NOT NULL DEFAULT '',
  requested_by TEXT NOT NULL, agents TEXT NOT NULL, revision TEXT NOT NULL, grant_id TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending', phase TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}',
  error TEXT NOT NULL DEFAULT '', reconcile INTEGER NOT NULL DEFAULT 0, superseded_by TEXT NOT NULL DEFAULT '',
  payload_sha TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_try TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS operations_state ON operations(state, kind);
CREATE INDEX IF NOT EXISTS operations_card ON operations(card, state);
ALTER TABLE cards ADD COLUMN operations TEXT NOT NULL DEFAULT '';
ALTER TABLE brief_versions ADD COLUMN operations TEXT NOT NULL DEFAULT '';
"""
# v17: BOSS looks before it creates. ``prepare-create`` records what it searched for (the intent,
# normalized), the project and the candidates it was shown with a hash of what makes each relevant; ``create-inbox``
# spends the token once — ``card`` is the card it made (a repeat returns it).
DDL_V17 = """
CREATE TABLE IF NOT EXISTS create_tokens (
  token TEXT PRIMARY KEY, intent TEXT NOT NULL, project TEXT NOT NULL, candidates TEXT NOT NULL,
  created TEXT NOT NULL, expires TEXT NOT NULL, card TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS create_tokens_intent ON create_tokens(intent, project);
"""
# v18: every accepted request key of a factory operation — the one that created it and each that joined it
# —
# with its card and requester: a joined card may wait on the operation, a key repeated after the end answers it.
DDL_V18 = """
CREATE TABLE IF NOT EXISTS operation_requests (
  key TEXT PRIMARY KEY, operation TEXT NOT NULL, card TEXT NOT NULL DEFAULT '', requester TEXT NOT NULL,
  payload_sha TEXT NOT NULL, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS operation_requests_operation ON operation_requests(operation, card);
INSERT OR IGNORE INTO operation_requests (key, operation, card, requester, payload_sha, created)
  SELECT key, id, card, requested_by, payload_sha, created FROM operations;
"""
# v19: the monitor's servers, the last result of each check, the check's status changes (the
# scheduler keeps 7 days) and the incidents P-n — the cabinet numbers them (``next_incident`` continues the monitor's
# own counter) and keeps them forever; their timeline is the ``incident.*`` events. ``monitor_applied`` holds the keys
# of the monitor's applied items a while, so a batch resent after a lost answer changes nothing twice.
DDL_V19 = """
CREATE TABLE IF NOT EXISTS servers (
  alias TEXT PRIMARY KEY, host TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1,
  reachable INTEGER NOT NULL DEFAULT 1, unreachable_count INTEGER NOT NULL DEFAULT 0, last_ok TEXT NOT NULL DEFAULT '',
  updated TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS server_checks (
  server TEXT NOT NULL, "check" TEXT NOT NULL, subject TEXT NOT NULL, level TEXT NOT NULL, value TEXT NOT NULL,
  text TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY (server, "check", subject));
CREATE TABLE IF NOT EXISTS check_history (
  id INTEGER PRIMARY KEY, server TEXT NOT NULL, "check" TEXT NOT NULL, subject TEXT NOT NULL, level TEXT NOT NULL,
  prev TEXT NOT NULL, value TEXT NOT NULL, text TEXT NOT NULL, at TEXT NOT NULL, created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS check_history_server ON check_history(server, at);
CREATE INDEX IF NOT EXISTS check_history_created ON check_history(created);
CREATE TABLE IF NOT EXISTS incidents (
  n INTEGER PRIMARY KEY, key TEXT NOT NULL, server TEXT NOT NULL, "check" TEXT NOT NULL, subject TEXT NOT NULL,
  level TEXT NOT NULL, max_level TEXT NOT NULL, value TEXT NOT NULL DEFAULT '', text TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'open', muted_until TEXT NOT NULL DEFAULT '', opened_at TEXT NOT NULL,
  last_seen TEXT NOT NULL, resolved_at TEXT NOT NULL DEFAULT '', fixed_card TEXT NOT NULL DEFAULT '',
  repeats INTEGER NOT NULL DEFAULT 0, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS incidents_key ON incidents(key, opened_at);
CREATE INDEX IF NOT EXISTS incidents_status ON incidents(status, n);
CREATE TABLE IF NOT EXISTS monitor_applied (key TEXT PRIMARY KEY, result TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS monitor_applied_created ON monitor_applied(created);
CREATE INDEX IF NOT EXISTS events_incident ON events(json_extract(payload, '$.incident'))
  WHERE json_extract(payload, '$.incident') IS NOT NULL;
INSERT OR IGNORE INTO meta (key, value) VALUES ('next_incident', '1');
"""
# v20: help inside an approved task — the lead's request to another role for one bounded read-only step,
# the
# role BOSS put on it, the helper's answer and the card as the lead held it (``basis``); an attention ack carries the
# action it rests on, its next check (minutes) and why.
DDL_V20 = """
CREATE TABLE IF NOT EXISTS assists (
  id TEXT PRIMARY KEY, key TEXT NOT NULL UNIQUE, payload_sha TEXT NOT NULL, card TEXT NOT NULL,
  thread INTEGER NOT NULL, lead TEXT NOT NULL, kind TEXT NOT NULL, role TEXT NOT NULL, helper TEXT NOT NULL DEFAULT '',
  step TEXT NOT NULL, ac INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '', proof_limit TEXT NOT NULL DEFAULT '', basis TEXT NOT NULL,
  signalled TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS assists_card ON assists(card, state);
ALTER TABLE attention_acks ADD COLUMN action TEXT NOT NULL DEFAULT '';
ALTER TABLE attention_acks ADD COLUMN next_check INTEGER NOT NULL DEFAULT 0;
ALTER TABLE attention_acks ADD COLUMN reason TEXT NOT NULL DEFAULT '';
"""
# v21: a project's repository from the cabinet — the owner's request (``pending`` to bind, ``unbinding``)
# waits here for the root unit kd-repo-attach, which writes back what add_repo.sh made of it and when.
DDL_V21 = """
ALTER TABLE projects ADD COLUMN repo_state TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN repo_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN repo_updated TEXT NOT NULL DEFAULT '';
"""
# v22: a factory operation's parameters beyond its roles — a ``component.install``'s component, mode and
# whether every hosted role drains first (``{}`` for ``agent.reinstall``).
DDL_V22 = """
ALTER TABLE operations ADD COLUMN params TEXT NOT NULL DEFAULT '{}';
"""
# v23: "Check now" — a person's request to check one server now, which the monitor's
# timer takes (one active per server; older than 10 minutes — failed), and the trusted lists of the monitor's last
# sync: the shared trusted addresses (scope '') and each server's identities, ports and WireGuard peers.
DDL_V23 = """
CREATE TABLE IF NOT EXISTS check_requests (
  id TEXT PRIMARY KEY, server TEXT NOT NULL, state TEXT NOT NULL, requested_by TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, started TEXT NOT NULL DEFAULT '',
  finished TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS check_requests_server ON check_requests(server, created);
CREATE UNIQUE INDEX IF NOT EXISTS check_requests_active ON check_requests(server)
  WHERE state IN ('queued', 'running');
CREATE TABLE IF NOT EXISTS server_trust (
  scope TEXT NOT NULL, kind TEXT NOT NULL, item TEXT NOT NULL, label TEXT NOT NULL DEFAULT '', at TEXT NOT NULL,
  PRIMARY KEY (scope, kind, item));
"""
# v24: an agent's second thread with people, "Monitor" — the monitor's wakes and what people write there,
# answered by its own session; one per agent, like the chat.
DDL_V24 = """
CREATE UNIQUE INDEX IF NOT EXISTS threads_agent_monitor ON threads(agent) WHERE kind='agent_monitor';
"""
# v25: people's audio to text — one row per audio file in the line (its message, author, language,
# state, attempts and result, when it was queued, started and finished) and the messages whose results woke their
# agent already (one wake per message, a retry included). Audio sent before it is never queued.
DDL_V25 = """
CREATE TABLE IF NOT EXISTS transcripts (
  file TEXT PRIMARY KEY, thread INTEGER NOT NULL, seq INTEGER NOT NULL, author TEXT NOT NULL, language TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'queued', status TEXT NOT NULL DEFAULT '', attempts INTEGER NOT NULL DEFAULT 0,
  duration REAL, queued TEXT NOT NULL, started TEXT NOT NULL DEFAULT '', finished TEXT NOT NULL DEFAULT '',
  created TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS transcripts_state ON transcripts(state, queued);
CREATE INDEX IF NOT EXISTS transcripts_message ON transcripts(thread, seq);
CREATE TABLE IF NOT EXISTS transcript_wakes (
  thread INTEGER NOT NULL, seq INTEGER NOT NULL, at TEXT NOT NULL, PRIMARY KEY (thread, seq));
"""
# v26: the owner's edits of the monitor's settings over config.json — a key, a server ('' — the
# whole monitor), the value as JSON, who and when; ``monitor_settings_revision`` counts the saves of the whole set.
DDL_V26 = """
CREATE TABLE IF NOT EXISTS monitor_settings (
  key TEXT NOT NULL, server TEXT NOT NULL DEFAULT '', value TEXT NOT NULL, author TEXT NOT NULL, at TEXT NOT NULL,
  PRIMARY KEY (key, server));
INSERT OR IGNORE INTO meta (key, value) VALUES ('monitor_settings_revision', '0');
"""
# v27: a signed monitor_trust decision waits for the monitor to apply it — how that went
# (pending, applied, error), why and when; an address taken off the trusted list (no signature) waits here the same way.
DDL_V27 = """
ALTER TABLE decisions ADD COLUMN apply_state TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN apply_detail TEXT NOT NULL DEFAULT '';
ALTER TABLE decisions ADD COLUMN applied_at TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS trust_removals (
  id TEXT PRIMARY KEY, item TEXT NOT NULL, requested_by TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'queued',
  detail TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS trust_removals_state ON trust_removals(state, created);
"""
# v28: monitoring observations retain their own clocks; an incident can own an explicit,
# durable investigation card without granting permission to change a server.
DDL_V28 = """
ALTER TABLE servers ADD COLUMN polled_at TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN resolution TEXT NOT NULL DEFAULT '';
ALTER TABLE server_checks ADD COLUMN timestamp_verified INTEGER NOT NULL DEFAULT 0;
ALTER TABLE server_checks ADD COLUMN value_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN value_hash TEXT NOT NULL DEFAULT '';
UPDATE incidents SET resolution=COALESCE((SELECT json_extract(e.payload,'$.how') FROM events e
  WHERE e.kind='incident.resolved' AND json_extract(e.payload,'$.incident')=incidents.n ORDER BY seq DESC LIMIT 1),'')
  WHERE status='resolved';
CREATE TABLE IF NOT EXISTS incident_cards (
  incident INTEGER NOT NULL REFERENCES incidents(n), card TEXT NOT NULL REFERENCES cards(id),
  linked_by TEXT NOT NULL, linked_at TEXT NOT NULL, PRIMARY KEY (incident, card));
CREATE INDEX IF NOT EXISTS incident_cards_card ON incident_cards(card);
CREATE TABLE IF NOT EXISTS incident_primary (
  incident INTEGER PRIMARY KEY REFERENCES incidents(n), card TEXT NOT NULL REFERENCES cards(id));
INSERT OR IGNORE INTO incident_cards (incident, card, linked_by, linked_at)
  SELECT i.n, c.id, 'system:migration', i.updated FROM incidents i JOIN cards c
  ON c.id=i.fixed_card OR EXISTS (SELECT 1 FROM json_each(c.labels) WHERE value='P-' || i.n);
CREATE TABLE IF NOT EXISTS check_runs (
  server TEXT NOT NULL, "check" TEXT NOT NULL, attempted_at TEXT NOT NULL,
  state TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '', PRIMARY KEY (server, "check"));
"""
# v29: the stages of a brief — part of the card's scope and of each version (JSON, '' — none);
# a ``stages`` row per (card, version, key) is the intent the approval materializes: ``child`` — the card it made,
# ``state`` — intent | created.
DDL_V29 = """
ALTER TABLE cards ADD COLUMN stages TEXT NOT NULL DEFAULT '';
ALTER TABLE brief_versions ADD COLUMN stages TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS stages (
  card TEXT NOT NULL, version INTEGER NOT NULL, key TEXT NOT NULL, child TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'intent', created TEXT NOT NULL, updated TEXT NOT NULL,
  PRIMARY KEY (card, version, key));
CREATE INDEX IF NOT EXISTS stages_child ON stages(child);
"""

# v30: confirmed observations are counted by the monitor, not inferred from elapsed time.
# Existing monitor replies retain their provenance and become visible on the incidents they name.
DDL_V30 = """
ALTER TABLE incidents ADD COLUMN confirmation_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE incidents ADD COLUMN confirmation_started_at TEXT NOT NULL DEFAULT '';
ALTER TABLE incidents ADD COLUMN confirmation_at TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS incident_reports (
  incident INTEGER NOT NULL REFERENCES incidents(n), message INTEGER NOT NULL REFERENCES messages(id),
  PRIMARY KEY (incident, message));
CREATE INDEX IF NOT EXISTS incident_reports_message ON incident_reports(message);
INSERT OR IGNORE INTO incident_reports (incident, message)
  SELECT i.n,m.id FROM messages m JOIN threads t ON t.id=m.thread
  LEFT JOIN commands c ON c.id=json_extract(m.payload,'$.command') AND c.thread=m.thread AND c.agent='sysadmin'
  JOIN incidents i ON (' ' || m.body || ' ') GLOB ('*[^A-Za-z0-9_-]P-' || i.n || '[^A-Za-z0-9_-]*')
  OR (' ' || COALESCE(c.text,'') || ' ') GLOB ('*[^A-Za-z0-9_-]P-' || i.n || '[^A-Za-z0-9_-]*')
  WHERE t.kind='agent_monitor' AND t.agent='sysadmin' AND m.author='agent:sysadmin'
  AND m.kind='text' AND m.created>=i.opened_at;
"""
# v31: the contours' health — a contour the dev.check reports or root put in maintenance (and why, since
# when) refuses new checks until root releases it; what its last check deployed. A contour without a row is idle.
DDL_V31 = """
CREATE TABLE IF NOT EXISTS contours (
  name TEXT PRIMARY KEY, state TEXT NOT NULL DEFAULT 'idle', deployed TEXT NOT NULL DEFAULT '{}',
  since TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL DEFAULT '');
"""
# v32: the Hindsight bank of a project's cards ('' — the role bank).
DDL_V32 = """
ALTER TABLE projects ADD COLUMN memory_bank TEXT NOT NULL DEFAULT '';
"""
# v33: the agent that closes the project's cards Done itself ('' — the owner's acceptance); until now the
# code's doneBy (common/coordination/pipeline.json).
DDL_V33 = """
ALTER TABLE projects ADD COLUMN done_by TEXT NOT NULL DEFAULT '';
"""
# v34: the cards' prefix is the host's (meta card_prefix; Store.open moves a base of an alias to it). No
# table changes: the version keeps the code before it, whose cards' prefix was fixed, from opening a moved base.
DDL_V34 = ""
# v35: the sysadmin's retelling of the monitor's summary is no incident's investigation (heads —
# monitor_settings.SUMMARY_HEADS); the summaries attached before are taken off their incidents.
DDL_V35 = """
DELETE FROM incident_reports WHERE message IN (SELECT m.id FROM messages m
  JOIN commands c ON c.id=json_extract(m.payload,'$.command') AND c.thread=m.thread AND c.agent='sysadmin'
  WHERE c.text LIKE '[монитор] сводка за неделю%' OR c.text LIKE '[монитор] утренняя сводка%');
"""
# v36: the windows of the sysadmin's plan steps (monitor_windows.py) and the dangerous objects a step
# changed, which link their incidents to the step's card for 24 hours.
DDL_V36 = """
CREATE TABLE IF NOT EXISTS monitor_windows (
  id TEXT PRIMARY KEY, server TEXT NOT NULL, card TEXT NOT NULL REFERENCES cards(id), step TEXT NOT NULL,
  run TEXT NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('open','closed','failed','applied','rejected','expired')),
  changes TEXT NOT NULL DEFAULT '{}', errors TEXT NOT NULL DEFAULT '[]', failed TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '{}', opened TEXT NOT NULL, closed TEXT NOT NULL DEFAULT '',
  applied TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS monitor_windows_state ON monitor_windows(state, opened);
CREATE INDEX IF NOT EXISTS monitor_windows_run ON monitor_windows(card, run);
CREATE TABLE IF NOT EXISTS monitor_window_marks (
  server TEXT NOT NULL, "check" TEXT NOT NULL, subject TEXT NOT NULL, card TEXT NOT NULL REFERENCES cards(id),
  at TEXT NOT NULL, PRIMARY KEY (server, "check", subject, card));
"""
# v37: the cabinet models of other providers (kd.llm_models: a slug the owner chose → a provider kind of
# the catalog and the model id it knows; prices per million tokens; the last probe) and the model and effort a card,
# a project or a session runs on ('' — the subscription, the agent's effort).
DDL_V37 = """
CREATE TABLE IF NOT EXISTS llm_models (
  id TEXT PRIMARY KEY, provider TEXT NOT NULL, base_url TEXT NOT NULL DEFAULT '', upstream TEXT NOT NULL,
  title TEXT NOT NULL, price_in REAL NOT NULL DEFAULT 0, price_out REAL NOT NULL DEFAULT 0,
  price_cache_read REAL NOT NULL DEFAULT 0, price_cache_write REAL NOT NULL DEFAULT 0,
  context INTEGER NOT NULL DEFAULT 200000, enabled INTEGER NOT NULL DEFAULT 1, probe TEXT NOT NULL DEFAULT '{}',
  created TEXT NOT NULL, updated TEXT NOT NULL);
ALTER TABLE cards ADD COLUMN model TEXT NOT NULL DEFAULT '';
ALTER TABLE cards ADD COLUMN effort TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN model TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN effort TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN model TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN effort TEXT NOT NULL DEFAULT '';
"""
# v38: a card put off until a time — by its agent (``kd-work finish --outcome wait_until``), the owner or
# BOSS: the moment (UTC), who, why, and how it ended (woken at the time, cleared by a person, replaced, closed). At most
# one is active per card; the ended ones stay: when the card last woke, and an agent's defers in a row (``streak``).
DDL_V38 = """
CREATE TABLE IF NOT EXISTS defers (
  id TEXT PRIMARY KEY, card TEXT NOT NULL REFERENCES cards(id), until TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('agent','owner','boss')), by TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
  command TEXT NOT NULL DEFAULT '', streak INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active','woken','cleared','replaced')),
  ended_how TEXT NOT NULL DEFAULT '', ended TEXT NOT NULL DEFAULT '', created TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS defers_active ON defers(card) WHERE state='active';
CREATE INDEX IF NOT EXISTS defers_due ON defers(state, until);
CREATE INDEX IF NOT EXISTS defers_card ON defers(card, created);
"""
# v39: a session waiting for one of its agent's slots — its place in the agent's queue as the host last
# reported it (0: not waiting) and since when it waits; never a time it would take.
DDL_V39 = """
ALTER TABLE sessions ADD COLUMN queue_position INTEGER NOT NULL DEFAULT 0;
ALTER TABLE sessions ADD COLUMN queued_since TEXT NOT NULL DEFAULT '';
"""
# v40: the council of models. ``council`` on a card and on a project is the composition (``kd.council``:
# per kind the critics, the rounds and — for the code — the least diff; '' — none of its own). ``councils`` is one
# gathering over a card's brief or code (``basis`` — what exactly it reads: the brief's version, the pull request's
# head), ``council_runs`` — one critic's read in one round: its job, verdict and findings.
DDL_V40 = """
ALTER TABLE cards ADD COLUMN council TEXT NOT NULL DEFAULT '';
ALTER TABLE projects ADD COLUMN council TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS councils (
  id TEXT PRIMARY KEY, card TEXT NOT NULL REFERENCES cards(id), kind TEXT NOT NULL CHECK (kind IN ('brief','code')),
  basis TEXT NOT NULL DEFAULT '{}', critics TEXT NOT NULL DEFAULT '[]', rounds_max INTEGER NOT NULL,
  round INTEGER NOT NULL DEFAULT 1,
  state TEXT NOT NULL
    CHECK (state IN ('running','needs_writer','clean','settled','limited','cancelled','failed')),
  created TEXT NOT NULL, updated TEXT NOT NULL, ended TEXT NOT NULL DEFAULT '', end_reason TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS councils_card ON councils(card, created);
CREATE INDEX IF NOT EXISTS councils_state ON councils(state);
CREATE TABLE IF NOT EXISTS council_runs (
  council TEXT NOT NULL REFERENCES councils(id), round INTEGER NOT NULL, n INTEGER NOT NULL, model TEXT NOT NULL,
  job TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL CHECK (state IN ('queued','running','done','failed','cancelled')),
  verdict TEXT NOT NULL DEFAULT '', findings TEXT NOT NULL DEFAULT '[]', questions TEXT NOT NULL DEFAULT '[]',
  fail_reason TEXT NOT NULL DEFAULT '', usage TEXT NOT NULL DEFAULT '', created TEXT NOT NULL,
  ended TEXT NOT NULL DEFAULT '', PRIMARY KEY (council, round, n));
CREATE INDEX IF NOT EXISTS council_runs_job ON council_runs(job);
"""

# v41: a reply to the monitor's event of some P-n reports on those only (incident_reports._investigated);
# the links it got to the P-n it listed in passing are taken off. A wake naming a range P-a..P-b is left as it is:
# the members of a range are not spelled out in its text.
DDL_V41 = """
DELETE FROM incident_reports WHERE rowid IN (SELECT r.rowid FROM incident_reports r
  JOIN messages m ON m.id=r.message
  JOIN commands c ON c.id=json_extract(m.payload,'$.command') AND c.thread=m.thread AND c.agent='sysadmin'
  WHERE length(c.text) < 2000 AND (' ' || c.text) GLOB '*[^A-Za-z0-9_-]P-[1-9]*' AND c.text NOT GLOB '*P-[1-9]*..P-[1-9]*'
  AND NOT (' ' || c.text || ' ') GLOB ('*[^A-Za-z0-9_-]P-' || r.incident || '[^A-Za-z0-9_-]*'));
"""
# v42: a job's rows of the usage ledger by the job — a council's block reads the cost and the models of
# its critics' jobs (councils.RUN_USAGE); only jobs are indexed, a turn is found by its command (v8).
DDL_V42 = """
CREATE INDEX IF NOT EXISTS usage_log_job ON usage_log(source) WHERE kind='job';
"""
# v43: milestones are gone — the list and the card's field. They came with the old board and nothing in
# the cabinet could make one; a group of cards under one goal is a parent card, an arbitrary group a label. The
# base DDL, run at every start, makes the list no more; the card's column stays in it for this step to drop.
DDL_V43 = """
DROP TABLE IF EXISTS milestones;
ALTER TABLE cards DROP COLUMN milestone;
"""
# v44: chain plans — a plan whose stages are one role in one repository runs them one after another on one branch
# and one pull request (``stages.chain``, set when the children are made and kept); each hand-in of a stage is a
# row here: the head a reviewer session read from ``base``, its verdict and findings, and what was decided on it —
# ``gate`` passed|returned|held, '' while nobody decided; ``message`` is the seq of the findings in the stage's
# thread. ``stage_pulls`` keeps the branch a chain plan's pull request goes into, as its stages published it
# (no decision holds its passport before the last stage): the reviewer of a stage is put on that pull
# request by its number, head and target.
DDL_V44 = """
ALTER TABLE stages ADD COLUMN chain INTEGER NOT NULL DEFAULT 0;
CREATE TABLE IF NOT EXISTS stage_pulls (
  repo TEXT NOT NULL, number INTEGER NOT NULL, target TEXT NOT NULL, PRIMARY KEY (repo, number));
CREATE TABLE IF NOT EXISTS stage_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT, card TEXT NOT NULL, parent TEXT NOT NULL, repo TEXT NOT NULL DEFAULT '',
  number INTEGER NOT NULL DEFAULT 0, head TEXT NOT NULL DEFAULT '', base TEXT NOT NULL DEFAULT '',
  target TEXT NOT NULL DEFAULT '', job TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
  verdict TEXT NOT NULL DEFAULT '', findings TEXT NOT NULL DEFAULT '[]', gate TEXT NOT NULL DEFAULT '',
  reason TEXT NOT NULL DEFAULT '', message INTEGER NOT NULL DEFAULT 0, created TEXT NOT NULL,
  updated TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS stage_reviews_card ON stage_reviews(card, id);
CREATE INDEX IF NOT EXISTS stage_reviews_job ON stage_reviews(job) WHERE job != '';
"""
# v45: a chain plan whose stages commit into several repositories has a pull request of the plan's card in each.
# A hand-in (``stage_reviews``) keeps its gate and its aggregate state and verdict; ``stage_review_parts`` holds
# one row per pull request the plan had open at the hand-in — its head, the base the stage's own commits begin
# at, its reviewer's job and what that review found (``skipped``: nothing new in that repository). The hand-ins
# made before get one part each from their own columns, which are not read any more.
DDL_V45 = """
CREATE TABLE IF NOT EXISTS stage_review_parts (
  review INTEGER NOT NULL, repo TEXT NOT NULL, number INTEGER NOT NULL, project TEXT NOT NULL DEFAULT '',
  head TEXT NOT NULL, base TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '', job TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL, verdict TEXT NOT NULL DEFAULT '', findings TEXT NOT NULL DEFAULT '[]',
  message INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL,
  PRIMARY KEY (review, repo, number));
CREATE INDEX IF NOT EXISTS stage_review_parts_job ON stage_review_parts(job) WHERE job != '';
INSERT OR IGNORE INTO stage_review_parts
  (review, repo, number, project, head, base, target, job, state, verdict, findings, message, reason, updated)
  SELECT r.id, r.repo, r.number,
         COALESCE((SELECT p.project FROM prs p WHERE p.repo = r.repo AND p.number = r.number), ''),
         r.head, r.base, r.target, r.job, r.state, r.verdict, r.findings, r.message,
         CASE WHEN r.state = 'reviewing' THEN r.reason ELSE '' END, r.updated
    FROM stage_reviews r WHERE r.repo != '' ORDER BY r.id;
"""
# v46: the platform's own alerts — notices without a card — are listed on the incidents page, where their push
# leads. The journal holds tens of thousands of events without a card; the list reads these few by an index of
# their own (``store.PLATFORM_NOTICES`` repeats its condition word for word, or SQLite does not take it).
DDL_V46 = """
CREATE INDEX IF NOT EXISTS events_platform_notice ON events(seq) WHERE kind = 'notice' AND card = '';
"""

MIGRATIONS = {
    2: DDL_V2, 3: DDL_V3, 4: DDL_V4, 5: DDL_V5, 6: DDL_V6, 7: DDL_V7, 8: DDL_V8, 9: DDL_V9, 10: DDL_V10, 11: DDL_V11,
    12: DDL_V12, 13: DDL_V13, 14: DDL_V14, 15: DDL_V15, 16: DDL_V16, 17: DDL_V17,
    18: DDL_V18, 19: DDL_V19, 20: DDL_V20, 21: DDL_V21, 22: DDL_V22, 23: DDL_V23, 24: DDL_V24,
    25: DDL_V25, 26: DDL_V26, 27: DDL_V27, 28: DDL_V28, 29: DDL_V29, 30: DDL_V30, 31: DDL_V31,
    32: DDL_V32, 33: DDL_V33, 34: DDL_V34, 35: DDL_V35, 36: DDL_V36, 37: DDL_V37, 38: DDL_V38, 39: DDL_V39,
    40: DDL_V40, 41: DDL_V41, 42: DDL_V42, 43: DDL_V43, 44: DDL_V44, 45: DDL_V45, 46: DDL_V46,
}
# the council of models: what it reads, how a gathering stands, how one critic's read stands
COUNCIL_KINDS = ("brief", "code")
COUNCIL_STATES = ("running", "needs_writer", "clean", "settled", "limited", "cancelled", "failed")
COUNCIL_RUN_STATES = ("queued", "running", "done", "failed", "cancelled")
# an agent's threads with people: its space → the thread's kind; a card's thread has none
AGENT_SPACES = {"chat": "agent_chat", "monitor": "agent_monitor"}
AGENT_THREAD_KINDS = tuple(AGENT_SPACES.values())
CHECK_STATES = ("queued", "running", "done", "failed")
# a project's repository: the requests the root unit takes, and what it answers
REPO_REQUESTS = ("pending", "unbinding")
REPO_RESULTS = ("connected", "no_access", "no_key", "hooks", "error")
MARKS = ("overview_seen",)
# ``urgent``: the wake of an approved critical command — the host starts it at once, over the agent's slots
COMMAND_KINDS = ("assigned", "message", "answer", "continue", "chat", "signal", "stop", "urgent")
COMMAND_STATES = ("queued", "delivered", "started", "done", "failed", "cancelled")
SESSION_STATES = ("new", "queued", "starting", "working", "idle", "exited", "failed", "stopped")
JOB_STATES = ("queued", "delivered", "running", "cancelling", "done", "failed", "no-result", "cancelled")
# the pipeline: a turn's outcome; wait_handoff comes with the typed handoffs (package C)
OUTCOMES = ("continue", "wait_user", "wait_dependency", "wait_until", "review", "done")
OUTCOME_STATES = ("pending", "final", "void", "superseded")
# contour — out of maintenance; time — a moment the agent put its card off until
# council — a round of the card's council
WAIT_KINDS = ("decision", "card", "operation", "assist", "contour", "time", "council")
# a factory operation: superseded — a newer request of the same work took its place before it ran
OPERATION_STATES = ("pending", "running", "succeeded", "failed", "superseded")
WAIT_STATES = ("pending", "open", "resolved", "rejected", "expired", "needs_routing", "cancelled")
CONTINUATION_STATES = ("queued", "merged", "observed", "limited")
PROBLEM_KINDS = ("missing_next_step", "continuation_limit", "wait_needs_routing", "outcome_refused", "assist_failed",
                 "assist_stalled",
                 # an approved stage without its child card; a stage's child closed without the work; a
                 # release whose health check (or an operation of it) failed
                 "stage_missing", "stage_cancelled", "release_degraded",
                 # a council no critic of which could start; one critic's failed run; a writer silent
                 # about the findings; a hand-in that left substantive findings unanswered (observe)
                 "council_failed", "council_run_failed", "council_stalled", "council_unanswered",
                 # a stage of a chain plan the coordinator passed was taken back into work
                 "stage_reopened")
STAGE_STATES = ("intent", "created")
# help inside an approved task: its states and why it failed (the helper's reasons; expired — the sweep's)
# or was cancelled (the card moved on)
ASSIST_STATES = ("requested", "assigned", "accepted", "done", "failed", "cancelled")
ASSIST_FAIL_REASONS = ("capability_missing", "access_denied", "not_found", "refused", "needs_approval", "expired")
ASSIST_CANCEL_REASONS = ("card_closed", "reassigned", "scope_changed", "withdrawn")
