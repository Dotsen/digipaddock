// What the cabinet's API sends (kd.cabinet.store / decisions / web views).

/** Card statuses; the board shows Cancelled only when explicitly selected. */
export const STATUSES = ["Inbox", "To Do", "In Progress", "Review", "Blocked", "Done", "Cancelled"] as const;
export type Status = (typeof STATUSES)[number];

export interface CardCompact {
  id: string;
  n: number;
  status: Status | string;
  assignee: string;
  project: string;
  title: string;
  labels: string[];
  servers: string[];
  priority: string;
  due: string;
  parent: string;
  dependencies: string[];
  revision: number;
  created: string;
  updated: string;
  /** Put off until a time: the board's list sends it, null when not put off. */
  defer?: CardDefer | null;
}

/** A card put off until a time: the moment (UTC), who put it off, why. */
export interface CardDefer {
  until: string;
  source: "agent" | "owner" | "boss";
  by: string;
  reason: string;
  created: string;
}

export interface AcceptanceItem {
  text: string;
  checked: boolean;
}

/** What the person may do with a card next — the backend's list: label and hint keys of the catalog. */
export interface CardAction {
  id: string;
  label: string;
  primary: boolean;
  agent: string;
  enabled: boolean;
  reason: string; // flow.reason.<reason>, with params
  params: Record<string, string | number>; // a number too: pr_open's PR number
  message: string;
  comment: "required" | "optional" | "none";
  brief_version: number | null; // the brief version a start approves; sent back as shown (R17)
  operations?: FactoryStep[]; // what approving that version grants the factory: shown before the start
  stages?: Stage[]; // the child cards approving that version creates
  chain?: boolean; // those stages run as a chain: a branch and a pull request per repository, no acceptance of each
  council?: { round: number; rounds: number }; // the brief's council has not ended: the approval cancels it
}

/** A stage of a brief: a self-contained part with its role and, once approved, its child card. */
export interface Stage {
  key: string;
  title: string;
  role: string;
  spec?: string;
  depends: string[]; // keys of earlier stages
  description?: string;
  acceptance?: string[];
  project?: string;
  child?: string; // the card it became
  status?: string; // that card's status
  gate?: string; // in a chain plan: where it stands between its hand-in and the next stage (stage.gate.*)
  reason?: string; // why it is held for the owner: a code of the cabinet (stage.reason.*) or BOSS's own words
}

// --- the brief as a versioned document (R17) ---------------------------------------------------
export type BriefStateName =
  | "not_requested" | "queued" | "in_progress" | "waiting_user" | "ready" | "stale" | "failed"
  | "in_council"; // a ready version its council still reads

export interface BriefState {
  state: BriefStateName;
  version: number; // the last version; 0: none
  approved: boolean;
  legacy: boolean; // a brief-ready label from before the versions
  questions: number; // the analyst's open questions
  error: string;
  council?: CouncilFacts; // the card's last brief council; absent on a card that never had one
}

export interface DiffLine {
  op: "=" | "-" | "+" | "…"; // "…": that many unchanged lines (the text is the number)
  text: string;
}

/** A typed step of a brief's "Operations": what approving the brief grants the factory. */
export interface FactoryStep {
  verb: string;
  target: string;
  agents?: string[]; // agent.reinstall
  component?: string; // component.install: a catalog component, its fixed mode, the signed tree
  mode?: string;
  revision?: string;
  what: string;
  effect: string;
  risk: { level: string; why: string };
  rollback: string;
}

export interface BriefVersion {
  version: number;
  origin: "agent" | "person" | "legacy";
  author: string;
  created: string;
  approved_by: string;
  approved_at: string;
  description: string;
  acceptance: string[];
  operations?: FactoryStep[];
  stages?: Stage[];
  changes: DiffLine[]; // from the version before (none for v1)
  /** The council's round that read this version; null — none did. */
  council?: { council: string; round: number; state: string; runs: { model: string; state: string; verdict: string }[] } | null;
}

export interface BriefView {
  brief: BriefState;
  versions: BriefVersion[]; // oldest first
  current: { description: string; acceptance: string[]; operations?: FactoryStep[]; stages?: Stage[] };
  changes: DiffLine[]; // a stale brief: since its last version
}

/** The card's pipeline records: the last outcome of its holder's turn, its waits, its problems. */
export interface CardPipeline {
  mode: "observe" | "enforce";
  outcome: {
    outcome: "continue" | "wait_user" | "wait_dependency" | "wait_until" | "review" | "done";
    note: string;
    agent: string;
    state: "pending" | "final" | "void" | "superseded";
    reason: string;
    created: string;
  } | null;
  /** Open waits, and the pending ones of the last outcome; blocking first. */
  waits: {
    id: string; kind: "decision" | "card" | "operation" | "assist" | "contour" | "time" | "council"; ref: string; state: string; blocking: boolean;
    reason: string;
    /** An operation wait's source: a release's key not asked for yet is ``awaited``. */
    operation?: { id: string; agents: string[]; state: string; phase: string; done: string[]; component?: string };
    /** A help request's source. */
    assist?: AssistView;
  }[];
  waits_total: number;
  /** Open problems, oldest first. */
  problems: { kind: "missing_next_step" | "continuation_limit" | "wait_needs_routing" | "outcome_refused" | "assist_failed" | "assist_stalled"
    | "stage_missing" | "stage_cancelled" | "stage_reopened" | "release_degraded"
    | "council_failed" | "council_run_failed" | "council_stalled" | "council_unanswered"; agent: string; created: string }[];
  /** The card's help requests, open first. */
  assists?: AssistView[];
  /** The roles a factory operation holds. */
  pauses?: OperationPause[];
  /** The aggregate's stages: how many, how many Done, each with its child card; null for a plain card. */
  stages?: {
    total: number; done: number; chain?: boolean;
    items: { key: string; child: string; status: string; role: string; title: string; gate?: string; reason?: string }[];
  } | null;
  /** A chain plan's stage at its gate: its plan, its word (stage.gate.*), the review's verdict, why it is held;
   * null for any other card. */
  stage?: { parent: string; gate: string; verdict: string; job?: string; reason: string } | null;
  /** Put off until a time, null when not. */
  defer?: CardDefer | null;
}

/** Help inside an approved task: the lead asked a role for one read-only step. */
export interface AssistView {
  id: string; kind: string; lead: string; role: string; helper: string; step: string;
  state: "requested" | "assigned" | "accepted" | "done" | "failed" | "cancelled" | "gone";
  reason: string; evidence: string; limit: string; updated: string;
}

/** A role paused by a factory operation: its card, the drain's deadline, the turns it waits for; or one a failed
 *  operation keeps until the owner resumes it (``held: "failed"``). */
export interface OperationPause {
  agent: string;
  held: "operation" | "failed";
  operation: string;
  card: string;
  phase: string;
  until: string;
  busy: { agent: string; card: string }[];
}

export interface Card extends CardCompact {
  /** Cancelled: duplicate | cancelled, and a duplicate's canonical card. */
  outcome?: string;
  canonical?: string;
  description: string;
  acceptance: AcceptanceItem[];
  dod: string;
  plan: string;
  notes: string;
  summary: string;
  created_by: string;
  thread: number;
  actions?: CardAction[];
  pipeline?: CardPipeline | null;
  /** The card's next step as the backend derives it — the same the agents' snapshot names. */
  next?: CardNext;
  /** The cabinet model its sessions run on, "" — the project's, else the subscription (the API always
   * sends it; optional for the fixtures of older tests). */
  model?: string;
  /** The effort of its sessions, "" — the project's, else the agent's own. */
  effort?: string;
  /** Its own council: a kind it names is its own (without critics — none), another is its project's. */
  council?: Council;
}

/** The council of models (kd.council): per kind — the brief, the code — the models that criticise (one
 * named twice reads twice), the rounds it may take and, for the code, the least diff in changed lines. */
export type CouncilKind = "brief" | "code";
export interface CouncilPart {
  critics: string[];
  rounds: number;
  min_lines?: number;
}
export type Council = Partial<Record<CouncilKind, CouncilPart>>;

// --- a gathering of the council (kd.cabinet.councils) --------------------------------------------
export type CouncilState = "running" | "needs_writer" | "clean" | "settled" | "limited" | "cancelled" | "failed";

/** A council in short: the brief's state, the next step and the brief tab read it. */
export interface CouncilFacts {
  id: string;
  kind: CouncilKind;
  state: CouncilState;
  round: number;
  rounds: number;
  done: number; // the critics of the current round that ended
  total: number;
  open_findings: number; // critical and major findings of the round without the writer's answer
  end_reason: string;
  version: number | null;
  number?: number; // a council of the code: its pull request and the head it reads
  head?: string;
}

export interface CouncilFinding {
  id: string; // r<round>.f<n>
  severity: "critical" | "major" | "minor";
  location: string;
  evidence: string;
  scenario: string;
  recommendation: string;
  answer?: "fixed" | "rejected" | "later";
  note?: string;
  card?: string; // «later»: the card it went to
}

/** A critic's question to the owner or the writer; `missing` — an input it had no access to. */
export interface CouncilQuestion {
  text: string;
  options?: string[];
  recommendation?: string;
  missing?: boolean;
}

/** One critic's read in one round. */
export interface CouncilRun {
  n: number;
  model: string;
  state: "queued" | "running" | "done" | "failed" | "cancelled";
  verdict: string; // brief: ready, needs_changes, blocked; code: ready, with_fixes, not_ready
  counts: { critical: number; major: number; minor: number };
  cost: number; // $, as the usage ledger priced its job
  served?: string[]; // the models that served its job, as the ledger names them, the costliest first
  fail_reason: string;
  same_model: boolean; // it read on the model the writer wrote on
  findings: CouncilFinding[];
  questions: CouncilQuestion[];
}

export interface CouncilRound {
  round: number;
  version: number | null; // the brief version it read
  head?: string; // the pull request's head a round of the code read
  state: string; // running, ended, superseded
  started: string;
  ended: string;
  runs: CouncilRun[];
}

/** A gathering as its block in the card's thread shows it. */
export interface CouncilView {
  id: string;
  kind: CouncilKind;
  card: string;
  state: CouncilState;
  end_reason: string;
  round: number;
  rounds: number;
  created: string;
  ended: string;
  /** What it stands on: a brief's version, or — the code — a pull request at its head. */
  basis: {
    version?: number | null; scope_sha?: string;
    repo?: string; project?: string; number?: number; head?: string; target?: string; url?: string;
  };
  rounds_list: CouncilRound[]; // oldest first
  /** The pull request an open council of the code holds back, as its decision will show it. */
  pull?: DecisionView;
}

/** What the card's council of the code said of a pull request's head (councils.pull_review): how the
 * gathering over it stands, `skipped` — below the threshold of lines, `none` — nobody gathered. */
export interface PrCouncil {
  state: CouncilState | "skipped" | "none";
  council: string | null;
  lines: number;
  min_lines: number;
  rejected: number;
}

/** `GET /api/cards/<id>/councils`: newest first; `can_manage` — the person may cancel one (`settings`). */
export interface CardCouncils {
  councils: CouncilView[];
  can_manage: boolean;
}

export interface CardNext {
  source: "derived" | "recorded";
  /** done, asked, wait_decision, brief_writing, brief_ready, inbox, review, wait_deps, not_started, run_*, a problem… */
  what: string;
  /** Whose move: @owner, @boss, an agent, or "-" (nobody's). */
  who: string;
  since: string;
  /** The agents' wording (English); the page words the step itself. */
  text: string;
  params: {
    agent?: string; number?: number | null; version?: number;
    // a council's step: its round, the critics done, the findings the writer owes an answer to
    round?: number; rounds?: number; done?: number; total?: number; findings?: number;    // a chain plan's stage at its gate: the verdict of its code review (ready, with fixes, not ready, none)
    verdict?: string;
  };
  /** The open decision the step is. */
  decision?: { id: string; type: string };
}

export const COMPACT_KEYS: (keyof CardCompact)[] = [
  "id",
  "n",
  "status",
  "assignee",
  "project",
  "title",
  "labels",
  "servers",
  "priority",
  "due",
  "parent",
  "dependencies",
  "revision",
  "created",
  "updated",
];

export function compact(card: CardCompact): CardCompact {
  const out: Record<string, unknown> = {};
  for (const key of COMPACT_KEYS) out[key] = card[key];
  // the list sends the defer itself, the card's page in its pipeline
  const defer = card.defer ?? (card as Card).pipeline?.defer;
  if (defer) out.defer = defer;
  return out as unknown as CardCompact;
}

export interface Message {
  seq: number;
  author: string;
  author_name: string;
  kind: string;
  body: string;
  payload: Record<string, any>;
  created: string;
}

export interface Decision {
  id: string;
  type: string;
  card: string;
  project: string;
  state: "open" | "approved" | "rejected" | "expired" | "revoked" | string;
  step_up: boolean;
  decide_permission: string;
  payload: Record<string, any>;
  created: string;
  expires: string;
  requested_by: string;
  decided_by: string;
  decided_at: string;
  signature: Record<string, any> | null;
  reply: string;
  question: string;
  /** The structured answer to a question form; `reply` is its readable text. */
  answer?: FormAnswer | null;
  batch_id?: string;
  grant_id?: string;
  totp_failures?: number;
  /** A signed monitor_trust: how the monitor applied it — pending, applied, error — why and when. */
  apply_state?: TrustApplyState;
  apply_detail?: string;
  applied_at?: string;
}

export type TrustApplyState = "" | "pending" | "applied" | "error";
/** "revoke" — a login by a key closed as "access revoked", never trusted. */
export type TrustKind = "accept" | "baseline" | "trust" | "revoke";

/** A monitor_trust decision as the pages list it (kd.cabinet.monitor_trust.shown): no card, its own payload. */
export interface TrustDecision {
  id: string;
  type: "monitor_trust";
  state: string;
  payload: { kind: TrustKind; incident?: number; server?: string; cidr?: string; label?: string; check?: string;
    subject?: string; value?: string; proposed_by?: string; text?: string };
  requested_by: string;
  created: string;
  decided_by: string;
  decided_at: string;
  reply: string;
  apply_state: TrustApplyState;
  apply_detail: string;
  applied_at: string;
}

/** An address the owner or the sysadmin took off the trusted list (no signature). */
export interface TrustRemoval {
  id: string;
  item: string;
  requested_by: string;
  state: "queued" | "applied" | "error";
  detail: string;
  created: string;
  updated: string;
}

/** A question form v1 (kd.cabinet.questions): what an agent asks with options. */
export interface QuestionOption {
  id: string;
  label: string;
  description: string;
  recommended: boolean;
}

export interface FormQuestion {
  id: string;
  title: string;
  context: string;
  type: "single_select" | "multi_select" | "text";
  required: boolean;
  allow_custom: boolean;
  options: QuestionOption[];
  /** What followed the options in an old text question (a recommendation). */
  note?: string;
}

export interface QuestionFormV1 {
  schema_version: 1;
  text: string;
  questions: FormQuestion[];
}

export interface FormAnswerItem {
  selected_option_ids: string[];
  custom_text: string;
}

export interface FormAnswer {
  schema_version: 1;
  origin: "agent" | "parsed";
  form_sha256: string;
  answers: Record<string, FormAnswerItem>;
  comment: string;
}

export interface Risk {
  level: string;
  why: string;
}

export interface PlanStep {
  n: number;
  what: string;
  mechanics: string;
  effect: string;
  risk: Risk;
  rollback: string;
  command: string;
  servers: string[];
  production?: boolean;
  /** a `script` step's command is its exact text, shown unfolded; absent or `typed` — a catalog command. */
  kind?: "script" | "typed";
}

/** The page's view of a decision (web/decisions.py VIEWS); fields depend on the type. */
export interface DecisionView {
  stale?: boolean;
  /** A pull request: the review of its head by the card's council of the code; absent without critics. */
  council?: PrCouncil;
  /** An admin plan under the factory policy (agent.reinstall, component.install): TOTP still signs it. */
  policy?: "factory";
  problem?: string;
  brief_stale?: boolean; // a promote of a card whose brief changed after it was ready: the approval would refuse
  /** Why it cannot be approved for now (`boss_off` — a release reinstalls BOSS); the page reads it again. */
  blocked?: string;
  /** An open decision a person asked about: true until the agent that asked for it writes in the thread. */
  asked_open?: boolean;
  card?: CardCompact | null;
  deployed?: string; // the tree an open factory plan's component step is signed for (the challenge binds it)
  intent?: string;
  basis?: string;
  steps?: PlanStep[];
  ttl_options?: number[];
  effect?: string;
  risk?: Risk;
  rollback?: string;
  command?: string;
  servers?: string[];
  production?: boolean;
  title?: string;
  reason?: string;
  revision?: number;
  project?: string;
  assignee?: string;
  description?: string;
  acceptance?: AcceptanceItem[];
  operations?: FactoryStep[]; // a promote: what approving the brief grants
  scope?: { max_children: number; max_active: number; days: number; projects: string[] };
  text?: string;
  /** A question: its form (the agent's, or one its old text reads as), where it comes from, the hash an answer binds. */
  form?: QuestionFormV1 | null;
  origin?: "agent" | "parsed" | "";
  form_sha256?: string;
  /** A question: what answers it — a file, or text (the default; a recording is not text). */
  expect?: "file" | "text";
  /** A pull request (pr_approve): as the forge executor published it; files and cage of this head only. */
  repo?: string;
  number?: number;
  url?: string;
  head?: string;
  release?: boolean;
  /** Open pull requests of the chain plan this one belongs to (0 — no chain plan): several are released once. */
  plan_pulls?: number;
  /** The owner's signature of another head carried here: that head was only brought up to date with `target`. */
  carried?: { decision: string; head: string; mode: PrMode };
  state?: string;
  files?: PrFile[];
  cage?: string[];
  truncated?: boolean;
  /** the passport's branch the PR goes into and the checks green on its head; `effect` (above) is then
   *  what a merge into that branch sets off. */
  target?: string;
  checks?: string[];
  /** The monitor's trust (monitor_trust): what becomes trusted, who proposed it and how it was applied. */
  kind?: TrustKind;
  incident?: { id: string; check: string; subject: string; value: string; status: string; level: string };
  server?: string;
  cidr?: string;
  label?: string;
  proposed_by?: string;
  apply?: { state: TrustApplyState; detail: string; at: string };
}

export interface PrFile {
  path: string;
  status: string;
  additions: number;
  deletions: number;
}

/** GET /api/decisions/{id}/diff: the PR's diff as published for the decision's head. */
export interface PrDiff {
  diff: string;
  truncated: boolean;
  head: string;
}

/** POST /api/decisions/{id}/reject of a PR: the rejection, and whether the remarks reached Forgejo. */
export interface PrAnswer {
  decision: Decision;
  forge: "posted" | "failed";
}

/** What the owner signs on a PR: approve only, approve and merge, approve, merge and release. */
export type PrMode = "approve" | "merge" | "release";

export interface DecisionItem {
  decision: Decision;
  view: DecisionView;
}

export interface Project {
  id: string;
  name: string;
  route: string;
  /** The repository bound to it (owner/repo), "" when none. */
  repo?: string;
  /** With `settings`: the request waiting for the root unit or its last outcome, "" when none. */
  repo_state?: "" | "pending" | "unbinding" | "connected" | "no_access" | "no_key" | "hooks" | "error";
  /** add_repo.sh's last words on a failure. */
  repo_detail?: string;
  repo_updated?: string;
  /** kd-merge --release deploys it: its repository is root's to change. */
  repo_release?: boolean;
  /** With `settings`: the Hindsight bank of its cards' sessions, "" — the assignee's role bank. */
  memory_bank?: string;
  /** With `settings`: the agent that closes its cards Done itself, "" — the owner's acceptance. */
  done_by?: string;
  /** With `settings`: the cabinet model its cards' sessions run on, "" — the subscription. */
  model?: string;
  /** With `settings`: the effort of its cards' sessions, "" — the agent's own. */
  effort?: string;
  /** The default council of its cards, {} — none; everyone who sees the project reads it. */
  council?: Council;
}

/** Claude Code's effort levels (kd.manifest.EFFORTS); "" is «as the agent's» / «as the project's». */
export const EFFORTS = ["low", "medium", "high", "xhigh", "max"] as const;

/** A model's probe: requested, running, or its last outcome. */
export interface LlmProbe {
  state?: "requested" | "running";
  at?: string;
  id?: string;
  ok?: boolean;
  model?: string;
  ms?: number;
  detail?: string;
}

/** A cabinet model of another provider (GET /api/models): a slug, a kind of the catalog, the provider's
 * model id and prices; without `settings` only the names, whether it is enabled and whether its kind's key is on
 * the host. `experimental`: not a Claude — Anthropic does not support it through Claude Code. */
export interface LlmModel {
  id: string;
  title: string;
  provider: string;
  enabled: number;
  key: boolean;
  experimental: boolean;
  /** A model of the Claude subscription (`claude:<alias>`): a row of the catalog — listed first, probed
   * like any, never changed, disabled or deleted; `key` — whether the host holds the subscription's token. */
  builtin?: boolean;
  /** the model a subscription model's alias served last (`claude-opus-5-5`), as Claude Code named it in a
   * turn, a critic's job or a probe; `""` until the first, and for a cabinet model (its `upstream` says it). */
  version?: string;
  upstream?: string;
  base_url?: string;
  price_in?: number;
  price_out?: number;
  price_cache_read?: number;
  price_cache_write?: number;
  context?: number;
  probe?: LlmProbe;
  created?: string;
  updated?: string;
}

/** A provider kind of the catalog (GET /api/models/providers): its base URL (empty — the model's own), the owner's
 * hint and whether the host holds its key. */
export interface LlmProvider {
  id: string;
  title: string;
  base_url: string;
  hint: string;
  caching: boolean;
  key: boolean;
  manage_key?: boolean;
  key_type?: "anthropic_api" | "openai_api" | "provider_api" | "claude_subscription";
  replace_only?: boolean;
  age_days?: number | null;
  days_left?: number | null;
  expires?: string;
  warning?: string;
  probe?: LlmModel["probe"];
}

/** What deleting a project touches, for its dialog (GET /api/projects/{id}/impact). */
export interface ProjectImpact {
  id: string;
  cards: number;
  users: { id: string; name: string }[];
  bound: boolean;
}

/** One time the card went to Review and what came of it (GET /api/cards/{id}/result). */
export interface ReviewIteration {
  iteration: number;
  entered: string;
  by: string;
  by_name: string;
  /** The comment sent with it: what is proposed. */
  entered_note: string;
  outcome: "pending" | "accepted" | "returned" | "other";
  decided: string;
  decided_by: string;
  decided_by_name: string;
  /** The decider's comment (a return's remarks). */
  note: string;
}

/** What the owner is asked to accept: the card's result fields and its Review iterations. */
export interface CardResult {
  summary: string;
  plan: string;
  notes: string;
  acceptance: AcceptanceItem[];
  dod: string;
  links: string[];
  reviews: ReviewIteration[];
  /** The release's state from the facts; `none` without a release notice. */
  release?: ReleaseState;
}

/** A release's operation (the factory's `release:<rev>…` keys). */
export interface ReleaseOperation { id: string; key: string; kind: string; agents: string[]; component: string; mode: string; state: string; error: string }

/** `merged` → `code_switched` → `activating` → `healthy` | `degraded`; `refused` by the forge. */
export interface ReleaseState {
  state: "none" | "merged" | "code_switched" | "activating" | "healthy" | "degraded" | "refused";
  rev: string; host: string; why: string; at: string; seq: number; key: string; operations: ReleaseOperation[];
}

/** A page of a card's history, newest first; `next` is the `before` of the next older page. */
export interface EventsPage {
  events: JournalEvent[];
  next: number | null;
}

export interface JournalEvent {
  seq: number;
  ts?: string;
  actor?: string;
  kind: string;
  card?: string | null;
  payload: Record<string, any>;
}

/** What was asked of an agent and how far it got (the cabinet's outbox to the session host). */
export interface Command {
  id: string;
  key: string;
  agent: string;
  thread: number;
  card: string;
  kind: "assigned" | "message" | "answer" | "continue" | "chat" | "signal" | "stop" | "urgent" | string;
  text: string;
  initiator: string;
  state: "queued" | "delivered" | "started" | "done" | "failed" | "cancelled" | string;
  attempts: number;
  session: string;
  error: string;
  reason: string;
  created: string;
  updated: string;
}

export const LIVE_SESSION = ["queued", "starting", "working", "idle"];
export const ENDED_SESSION = ["exited", "failed", "stopped"];

/** An agent's session in one thread on the session host. */
export interface AgentSession {
  id: string;
  thread: number;
  agent: string;
  runtime: string;
  slot: string;
  state: "new" | "queued" | "starting" | "working" | "idle" | "exited" | "failed" | "stopped" | string;
  error: string;
  started: string;
  ended: string;
  heartbeat: string;
  created: string;
  updated: string;
  usage: Record<string, number>;
  /** The cabinet model the session's process runs on, "" — the subscription; and its effort. */
  model?: string;
  effort?: string;
  /** Waiting for one of its agent's slots: its place in the agent's queue (0 — not reported) and since when. */
  queue_position?: number;
  queued_since?: string;
}

/** One step of a session's turn: the agent's text, a tool call (its masked summary), a tool result (ok,
 * size, whether its masked output is kept) or a status line. ``id`` pairs a call with its result. */
export interface TurnStep {
  seq: number;
  kind: "text" | "tool_call" | "tool_result" | "status" | string;
  body: string;
  tool: string;
  id: string;
  ok: boolean | null;
  size: number | null;
  output: boolean;
  status: { key: string; params: Record<string, string> } | null;
}

/** A session's turn: the command that started it, how it went, its cost and its steps. */
export interface SessionTurn {
  command: string;
  kind: string;
  text: string;
  initiator: string;
  state: string;
  error: string;
  created: string;
  updated: string;
  cost: number;
  /** the models that served the turn, as the usage ledger names them, the costliest first. */
  served?: string[];
  steps: TurnStep[];
}

/** A tool's output as the cabinet keeps it: masked, at most 4000 characters. */
export interface ToolOutput {
  text: string;
  size: number;
  truncated: boolean;
}

/** A worker job of a card (kd.cabinet.jobs.view): one bounded session of the agent in a worker slot. */
export interface WorkerJob {
  id: string;
  card: string;
  agent: string;
  spec: string;
  skills: string[];
  write: string;
  state: "queued" | "delivered" | "running" | "cancelling" | "done" | "failed" | "no-result" | "cancelled" | string;
  slot: string;
  reason: string;
  created: string;
  updated: string;
  started: string;
  ended: string;
  result: Record<string, unknown> | null;
  usage: {
    models: Record<string, { in: number; out: number; cache_read: number; cache_write: number; cost: number }>;
    requests: number;
  } | null;
}

/** An agent on the session host: its sessions at work, waiting for a slot, and its slots. */
export interface HostCounts {
  working: number;
  queued: number;
  slots: number;
}

/** An agent's cards the person may read, by status (Done left out). */
export interface AgentCards {
  inbox: number;
  to_do: number;
  in_progress: number;
  review: number;
  blocked: number;
}

/** What the state is made of (kd.cabinet.team), on the threads the person may read. */
export interface AgentFacts {
  working: number;
  waiting_you: number;
  queued: number;
  failed: number;
  /** sessions marked working whose heartbeat went quiet */
  stale: number;
  questions: { decision: string; card: string; title: string }[];
  failures: { card: string; title: string; reason: string; at: string }[];
  heartbeat: string;
  last_action: { at: string; card: string; text: string } | null;
  /** when it last reported progress; "": never */
  progress: string;
}

/** /api/agents: one agent of the host's registry as the person sees it. */
export interface AgentSummary {
  name: string;
  state: AgentState;
  facts: AgentFacts;
  cards: AgentCards;
  specs: string[];
  usage_today: { cost: number; calls: number } | null;
  /** null: the agent still lives in its digipaddock panel. */
  host: HostCounts | null;
}

/** /api/agents/{name}: the counts, its readable cards, its latest progress lines and its sessions. */
export interface AgentDetail extends Omit<AgentSummary, "cards"> {
  counts: AgentCards;
  cards: CardCompact[];
  progress: { card: string; seq: number; body: string; created: string }[];
  sessions: AgentSession[];
  /** The agent has a "Monitor" thread (shown to who reads its chat). */
  monitor?: boolean;
}

/** An agent's state as the facts show it (kd.cabinet.team). */
export type AgentState = "working" | "waiting_you" | "queued" | "free" | "error" | "unavailable" | "unknown";

export type OverviewPeriod = "1d" | "7d" | "30d";

/** What waits for the person: an open decision they may take, or a card in Review they may accept. */
export interface OverviewNeed {
  kind: string; // a decision type, or "review"
  card: string;
  title: string;
  project: string;
  decision: string;
  waiting: string; // who waits: "@dev", "@boss"
  since: string;
  link: string;
}

export type OverviewRun = "working" | "stale" | "queued" | "delayed" | "failed" | "none";

/** A card under way: In Progress, To Do with a request on its way, Inbox with the brief being written. */
export interface OverviewWork {
  card: string;
  title: string;
  project: string;
  assignee: string;
  status: string;
  stage: string; // the status, or the brief's state for an Inbox card
  run: OverviewRun;
  last_step: { at: string; text: string } | null;
  council?: { kind: CouncilKind; round: number; rounds: number; state: CouncilState }; // its open council
}

/** A failure (`error`) apart from normal waiting (`waiting`: delayed, slot, deps); `stuck` — work that stopped
 * with nobody on it (`reason` is the pipeline problem's kind). */
export interface OverviewProblem {
  card: string;
  title: string;
  project: string;
  agent: string;
  kind: "error" | "waiting" | "stuck";
  reason: string;
  since: string;
  deps?: string[];
}

/** an open crit incident nobody acknowledged or muted, first among the overview's problems. */
export interface OverviewAlarm {
  kind: "incident";
  incident: string;
  server: string;
  check: string;
  subject: string;
  value: string;
  since: string;
  /** one tick opened it for several servers — one row, its P-n and servers. */
  members?: string[];
  servers?: string[];
  group?: IncidentGroup;
}

/** the incidents one monitor tick opened for one check: «<opened_at>|<check>», how many, on how many
 * servers and objects. */
export interface IncidentGroup {
  id: string;
  size: number;
  servers: number;
  subjects: number;
}

/** The overview's "Servers" tile: checked servers by their worst check (null without servers.read). */
export interface OverviewServers {
  servers: number;
  crit: number;
  warn: number;
  unreachable: number;
}

export type Level = "ok" | "warn" | "crit";

/** "Check now": one request of a server; queued → running → done | failed (10 minutes at most). */
export interface CheckRequest {
  id: string;
  server: string;
  state: "queued" | "running" | "done" | "failed";
  requested_by: string;
  detail: string;
  created: string;
  started: string;
  finished: string;
}

/** A dev.check contour (``GET /api/contours``): what runs there and whether it takes checks. */
export interface ContourRow {
  name: string;
  host: string;
  components: string[];
  exclusive: boolean;
  capacity: number;
  state: "idle" | "busy" | "maintenance";
  running: { operation: string; card: string; project: string; pr: number; head: string }[];
  deployed: Record<string, string>;
  reason: string;
  since: string;
}

export interface ServerRow {
  baseline_state?: string;
  user_checks?: Record<Level, number>;
  reason?: ServerCheck | null;
  coverage_freshness?: string;
  monitor_freshness?: string;
  freshness?: string;
  observed_at?: string;
  alias: string;
  host: string;
  enabled: number;
  reachable: number;
  unreachable_count: number;
  last_ok: string;
  updated: string;
  worst: Level | "";
  checks: Record<Level, number>;
  incidents: number;
  check_now: CheckRequest | null;
}

export interface ServerCheck {
  thresholds?: { warn?: number; crit?: number; unit: string } | null;
  recovery_runs?: number;
  freshness?: string;
  interval_s?: number;
  run_state?: string;
  run_detail?: string;
  server: string;
  check: string;
  subject: string;
  level: Level;
  value: string;
  text: string;
  at: string;
}

export interface CheckChange {
  id: number;
  server: string;
  check: string;
  subject: string;
  level: Level;
  prev: Level;
  value: string;
  text: string;
  at: string;
}

/** A platform alert: a warn or critical notice without a card (`GET /api/alerts`), a catalog key and its facts. */
export interface PlatformAlert {
  seq: number;
  ts: string;
  source: string;
  level: "warn" | "critical";
  key: string;
  params: Record<string, string | number>;
}

/** A page of them, newest first; `next` is the `before` of the older page, null at the end. */
export interface PlatformAlertPage {
  items: PlatformAlert[];
  next: number | null;
}

export interface Incident {
  confirmation_count?: number;
  confirmation_started_at?: string;
  confirmation_at?: string;
  investigation?: {
    state: "waiting" | "queued" | "running" | "failed" | "awaiting_report" | "reviewed";
    detail: string;
    report: {body:string; summary:string; author:string; created:string; thread:number; seq:number} | null;
  };
  baseline_value?: string | null;
  current?: ServerCheck | null;
  cards?: LinkedCard[];
  active_cards?: number;
  assignee?: string;
  freshness?: string;
  resolution?: string;
  delivery?: { id: string; state: string; detail: string } | null;
  /** a difference's content changes after the opening (L6) and the server's last recheck (L7). */
  changes?: IncidentChanges;
  recheck?: CheckRequest | null;
  /** the signed accept the monitor has not applied (or has, before its tick closed the P-n). */
  accepted?: { decision: string; apply_state: TrustApplyState; decided_at: string; kind?: "accept" | "revoke" } | null;
  /** one tick opened more than one incident of this check; a collapsed inbox row carries its P-n and
   * servers too. */
  group?: IncidentGroup;
  members?: string[];
  servers?: string[];
  n: number;
  id: string; // P-n
  key: string;
  server: string;
  check: string;
  subject: string;
  level: Level;
  max_level: Level;
  value: string;
  text: string;
  status: "open" | "acknowledged" | "resolved";
  muted_until: string;
  opened_at: string;
  last_seen: string;
  resolved_at: string;
  fixed_card: string;
  repeats: number;
}

export interface IncidentChanges {
  count: number;
  last_at: string;
}

export interface LinkedCard {
  summary?: string;
  pending_decision?: boolean;
  id: string;
  title: string;
  status: string;
  project: string;
  assignee: string;
}

export interface TrustItem {
  item: string;
  label: string;
}

export interface ServerPage {
  diagnostics?: { queued_alerts?: number; queued_changes?: number };
  missing_checks?: string[];
  monitor_full_at?: string;
  server: Omit<ServerRow, "checks" | "incidents" | "check_now">;
  checks: ServerCheck[];
  history: CheckChange[];
  incidents: Incident[];
  cards: LinkedCard[];
  trust: { trusted: TrustItem[]; identities: TrustItem[]; ports: TrustItem[]; wgpeers: TrustItem[]; at: string;
    /** When the server's baseline was taken; "" — not sent yet. */
    taken_at?: string };
  check_now: CheckRequest | null;
  /** The server's baselines and the shared list's addresses proposed or signed, the removals. */
  proposals?: { decisions: TrustDecision[]; removals: TrustRemoval[] };
}

export interface IncidentEvent {
  seq: number;
  ts: string;
  actor: string;
  actor_name: string;
  kind: string;
  payload: Record<string, any>;
}

export interface IncidentPage {
  incident: Incident;
  timeline: IncidentEvent[];
  cards: LinkedCard[];
  /** Its accepts into the baseline and whether its check has a baseline at all. */
  trust?: TrustDecision[];
  acceptable?: boolean;
  /** A login by a key that may close as "access revoked". */
  revocable?: boolean;
}

export interface OverviewChange {
  seq: number;
  kind: "created" | "review" | "done" | "blocked" | "handoff" | "brief";
  card: string;
  title: string;
  actor: string;
  actor_name: string;
  at: string;
  new: boolean;
  to?: string;
  version?: number;
}

export interface OverviewMember {
  name: string;
  state: AgentState;
  working: number;
  waiting_you: number;
  queued: number;
  failed: number;
}

/** /api/dashboard: the first screen, counted from the cards the person may read. */
export interface Overview {
  needs: OverviewNeed[];
  work: OverviewWork[];
  problems: (OverviewProblem | OverviewAlarm)[];
  changes: OverviewChange[];
  team: OverviewMember[];
  servers: OverviewServers | null;
  counts: { needs: number; work: number; problems: number; changes_new: number };
  seen: number;
  watermark: number;
}

/** /api/feed: newest first; `before` pages older, `last` is the newest seq for `after`. */
export interface FeedPage {
  events: JournalEvent[];
  last: number;
  before: number | null;
  more?: boolean; // forward scan has not reached the journal snapshot yet
}

/** How much of a day kd-usage read of the Hindsight server's spend. */
export type Coverage = "full" | "partial" | "none";

/** A row of kd-usage's ledger (/api/usage): one agent × model × day. */
export interface UsageRow {
  day: string;
  agent: string;
  /** "hindsight" — the Hindsight server's own calls: a category of spend, not an agent. */
  category?: string;
  coverage?: Coverage;
  model?: string;
  requests?: number;
  sessions?: number;
  input?: number;
  output?: number;
  cache_write?: number;
  cache_read?: number;
  cost_usd?: number;
  cost_max_usd?: number;
  cost_source?: "reported" | "estimated" | "unknown";
  price?: TokenPrice;
}

export interface TokenRates {
  input: number;
  output: number;
  cache_write: number;
  cache_write_1h: number;
  cache_read: number;
}

export interface TokenPrice extends TokenRates {
  model: string;
  source: string;
  updated: string;
  long_context?: TokenRates & { threshold: number };
}

export type Scope = "*" | string[];

export interface Person {
  id: string;
  name: string;
  status: "active" | "blocked";
  language: string;
  timezone: string;
  created: string;
  last_login: string | null;
  roles: [string, Scope][];
}

export interface Passkey {
  id: number;
  name: string;
  signing_enabled: boolean;
  created: string;
  last_used: string | null;
}

export interface WebSession {
  id: string;
  device: string;
  created: string;
  last_seen: string;
  expires: string;
  current: boolean;
}

/** One deploy run: a Forgejo Actions run of an environment, or the platform's release (project "platform"). */
export interface DeployRun {
  project: string;
  env: string;
  run: number;
  number: number;
  workflow: string;
  status: "unknown" | "waiting" | "running" | "success" | "failure" | "cancelled" | "skipped" | "blocked";
  event: string;
  sha: string;
  ref: string;
  url: string;
  actor: string;
  inputs: Record<string, string>;
  title: string;
  created: string;
  started: string;
  stopped: string;
  card: string; // the platform's release: the card it came from
}

/** An environment of «Deploys» with its last run; stale — no good read for two minutes. */
export interface DeployEnv {
  project: string;
  name: string;
  repo: string;
  env: string;
  workflow: string;
  branch: string;
  manual: boolean;
  read_at: string;
  ok_at: string;
  error: string;
  stale: boolean;
  run: DeployRun | null;
  waiting: { card: string; sha: string }[]; // cards whose deploy wait names this environment
  confirm: string; // a manual workflow's input kd-review fills with the word «deploy»
  skip_tests: boolean; // a manual workflow takes skip_tests
  launch: boolean; // the button «Deploy to prod»: a manual environment, for the owner
  launched: DeployLaunch | null; // the newest launch from the cabinet
  deployed: DeployRun | null; // its newest successful run: what runs there
  head: string; // its branch's head as the reader last saw it, '' unknown
  pending: number | null; // commits of the branch not deployed there yet; null unknown
  pending_commits: DeployCommit[]; // the newest of them (at most 20)
  ahead: number | null; // commits it runs that ahead_of (prod) does not; null unknown
  ahead_commits: DeployCommit[]; // the newest of them (at most 20)
  in_flight: boolean; // a deploy under way (a manual environment): the button waits, the server refuses another
  ahead_of: string; // the environment it is measured against, '' none (prod itself)
}

/** A commit behind a count of «Деплой»: its title and description, its author, when it landed on the branch (UTC,
 *  '' unknown) and its page in Forgejo ('' none). */
export interface DeployCommit {
  sha: string;
  title: string;
  body: string;
  author: string;
  date: string;
  url: string;
  status?: CheckState | ""; // its checks' worst state; '' (or absent — a reader of before the checks) none known
  checks?: DeployCheck[]; // the checks that did not pass, the failed first, each with its log
}

export type CheckState = "pending" | "success" | "warning" | "failure";

/** A commit's check in Forgejo (an Actions job, a devcheck) that did not pass. */
export interface DeployCheck {
  context: string;
  state: CheckState;
  url: string;
}

/** A launch the owner signed in the cabinet: dispatched, or refused with kd-review's reason. */
export interface DeployLaunch {
  id: string;
  user: string;
  project: string;
  env: string;
  sha: string;
  skip_tests: boolean;
  outcome: string; // dispatched | stale | refused | invalid | unavailable | review_timeout
  message: string;
  at: string;
}

/** The passkey request over a launch: the commit at the branch's head it binds. */
export interface DeployChallenge {
  challenge_id: string;
  options: Record<string, unknown>;
  expires: string;
  launch: { project: string; env: string; branch: string; sha: string; skip_tests: boolean };
}

/** A failed run that ended cards' deploy waits, in the inbox until someone saw it. */
export interface DeployAlert extends DeployRun {
  cards: string[];
  alerted: string;
}

export interface DeployList {
  envs: DeployEnv[];
  platform: DeployRun | null;
  now: string;
  stale_after: number;
}

export interface DeployPage {
  items: DeployRun[];
  next: string | null;
}
